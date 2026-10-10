"""Tool calls a model wrote into its message instead of making them.

Some models, mostly smaller and local ones, sometimes print their native tool-call syntax as
text when the serving stack doesn't turn it into a call: seen live from Qwen 3.8 27B
(`<tool_call><function=propose_commands><parameter=items>[...]`) and DeepSeek V4 Pro
(`<｜DSML｜invoke name=...>`). The technician would see markup and get nothing queued. These are
read back into ordinary tool calls, which then go through the same checks and the same review
queue as any other; the markup is taken out of the visible message."""

from __future__ import annotations

import json
import re

from .client import ToolCall

_WRAPPER = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
_QWEN_FN = re.compile(r"<function=([\w.-]+)>\s*(.*?)\s*</function>", re.S)
_QWEN_PARAM = re.compile(r"<parameter=([\w.-]+)>\s*(.*?)\s*</parameter>", re.S)
_DSML_BLOCK = re.compile(r"<｜DSML｜(?:tool_calls|function_calls)>(.*?)</｜DSML｜(?:tool_calls|function_calls)>", re.S)
_DSML_INVOKE = re.compile(r"<｜DSML｜invoke name=\"([\w.-]+)\">(.*?)</｜DSML｜invoke>", re.S)
_DSML_PARAM = re.compile(r"<｜DSML｜parameter name=\"([\w.-]+)\"(?: string=\"(true|false)\")?>(.*?)</｜DSML｜parameter>", re.S)


def _value(raw: str, as_string: bool = False):
    if as_string:
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _call(name: str, args: dict, n: int) -> ToolCall:
    return ToolCall(id=f"text_call_{n}", name=name, arguments=json.dumps(args))


def extract(text: str) -> tuple[str, list[ToolCall]]:
    """(the message without tool-call markup, the calls found in it). Markup that can't be read
    as a call is left in place."""
    calls: list[ToolCall] = []

    def wrapped(m: re.Match) -> str:
        body = m.group(1)
        fn = _QWEN_FN.fullmatch(body)
        if fn:
            args = {p: _value(v) for p, v in _QWEN_PARAM.findall(fn.group(2))}
            calls.append(_call(fn.group(1), args, len(calls)))
            return ""
        try:
            obj = json.loads(body)
        except ValueError:
            return m.group(0)
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            return m.group(0)
        args = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            args = _value(args)
        if not isinstance(args, dict):
            return m.group(0)
        calls.append(_call(obj["name"], args, len(calls)))
        return ""

    def dsml(m: re.Match) -> str:
        found = []
        for name, body in _DSML_INVOKE.findall(m.group(1)):
            found.append(_call(name, {p: _value(v.strip(), s == "true") for p, s, v in _DSML_PARAM.findall(body)},
                               len(calls) + len(found)))
        if not found:
            return m.group(0)
        calls.extend(found)
        return ""

    out = _WRAPPER.sub(wrapped, text)
    out = _DSML_BLOCK.sub(dsml, out)
    return re.sub(r"\n{3,}", "\n\n", out).strip(), calls
