"""Tool calls a model wrote into its message instead of making them.

Some models, mostly smaller and local ones, sometimes print their native tool-call syntax as
text when the serving stack doesn't turn it into a call: seen live from Qwen 3.8 27B
(`<tool_call><function=propose_commands><parameter=items>[...]`) and DeepSeek V4 Pro
(`<｜DSML｜invoke name=...>`). The technician would see markup and get nothing queued. These are
read back into ordinary tool calls, which then go through the same checks and the same review
queue as any other.

**Only markup that ends the message is a call.** A model that makes a call this way stops there;
markup with prose after it is a model writing *about* markup: quoting a line it read in command
output or on a web page, or explaining the syntax. Until Nov 2026 markup anywhere in a message
became a call and vanished from it, so a quoted `web_search` ran (unasked, in search mode
"auto") and the technician never saw the quote. So a block is taken only where nothing but
whitespace (or more such blocks) follows it, where it begins a line of its own, and outside a
code fence: quoted inline, in backticks, in a `>` quote or in a fenced example, it stays text.

**What is taken out is said.** The markup leaves the shown message, and a short note stands in
its place, so the technician can see that something was written there and what it became. A
message that was nothing but the call gets no note: there is nothing to read around it, the call
itself is shown, and the engine then asks the model for a word about what it queued, as it does
after any call made without one.

**No pattern here can be made to search for long.** The first version matched each form with a
lazy group between `\\s*`, which tries every split of a run of whitespace against every later
position: `<tool_call>` followed by 3,000 spaces and no closing tag held the event loop, and
with it every terminal, for 9 seconds. The text is now read with plain finds, each from where
the last one ended, and nothing at all is looked for unless the message ends with a closing tag."""

from __future__ import annotations

import json
import re
import uuid

from .client import ToolCall

_OPEN, _CLOSE = "<tool_call>", "</tool_call>"
# DeepSeek's: a block of invokes under either name, closed by either
_DSML_OPEN = ("<｜DSML｜tool_calls>", "<｜DSML｜function_calls>")
_DSML_CLOSE = ("</｜DSML｜tool_calls>", "</｜DSML｜function_calls>")
_NAME = re.compile(r"[\w.-]+")
# what stands in a DSML parameter's tag after `name="`: the name, and whether the value is a string
_DSML_PARAM = re.compile(r'([\w.-]+)"(?: string="(true|false)")?')
_FENCES = ("```", "~~~")


def _value(raw: str, as_string: bool = False):
    if as_string:
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _call(name: str, args: dict, n: int) -> ToolCall:
    # unique across rounds and turns: text_call_0 in every reply gave the history duplicate ids
    return ToolCall(id=f"text_call_{n}_{uuid.uuid4().hex[:12]}", name=name, arguments=json.dumps(args))


def _elements(text: str, opener: str, closer: str):
    """(what stands between `opener` and the next ">", the body up to `closer`) of each such
    element in `text`, in order. Each find starts where the last ended, and the first one that
    finds nothing ends the reading: the whole costs one pass, whatever the text."""
    at = 0
    while True:
        start = text.find(opener, at)
        if start < 0:
            return
        head_end = text.find(">", start + len(opener))
        if head_end < 0:
            return
        end = text.find(closer, head_end + 1)
        if end < 0:
            return
        yield text[start + len(opener):head_end], text[head_end + 1:end]
        at = end + len(closer)


def _wrapped(body: str) -> tuple[str, dict] | None:
    """The call in a `<tool_call>` block: Qwen's `<function=name><parameter=key>value...`, or
    the JSON object other models write (`{"name": ..., "arguments": {...}}`). None when it is
    neither."""
    body = body.strip()
    if body.startswith("<function=") and body.endswith("</function>"):
        head_end = body.find(">")
        name = body[len("<function="):head_end]
        if not _NAME.fullmatch(name):
            return None
        inner = body[head_end + 1:-len("</function>")]
        return name, {key: _value(value.strip()) for key, value in _elements(inner, "<parameter=", "</parameter>")
                      if _NAME.fullmatch(key)}
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        args = _value(args)
    return (obj["name"], args) if isinstance(args, dict) else None


def _dsml(inner: str) -> list[tuple[str, dict]]:
    """The calls in a DSML block: each `invoke` with its parameters."""
    found = []
    for head, body in _elements(inner, '<｜DSML｜invoke name="', "</｜DSML｜invoke>"):
        if not (head.endswith('"') and _NAME.fullmatch(head[:-1])):
            continue
        args = {}
        for param, value in _elements(body, '<｜DSML｜parameter name="', "</｜DSML｜parameter>"):
            m = _DSML_PARAM.fullmatch(param)
            if m:
                args[m.group(1)] = _value(value.strip(), m.group(2) == "true")
        found.append((head[:-1], args))
    return found


def _before_space(text: str, end: int) -> int:
    """`end` moved back over any whitespace before it."""
    while end > 0 and text[end - 1].isspace():
        end -= 1
    return end


def _block_ending_at(text: str, end: int) -> tuple[int, list[tuple[str, dict]]] | None:
    """(where it begins, its calls) for the block of tool-call markup that ends exactly at `end`,
    or None when what ends there is not one, or can't be read as a call."""
    if text.endswith(_CLOSE, 0, end):
        start = text.rfind(_OPEN, 0, end - len(_CLOSE))
        if start < 0:
            return None
        call = _wrapped(text[start + len(_OPEN):end - len(_CLOSE)])
        return (start, [call]) if call else None
    for closer in _DSML_CLOSE:
        if text.endswith(closer, 0, end):
            start, opener = max((text.rfind(o, 0, end - len(closer)), o) for o in _DSML_OPEN)
            if start < 0:
                return None
            calls = _dsml(text[start + len(opener):end - len(closer)])
            return (start, calls) if calls else None
    return None


def _begins_a_line(text: str, start: int) -> bool:
    """Whether only spaces or tabs stand before `start` on its line: not prose, a backtick, a
    quotation mark or a `>`."""
    while start > 0 and text[start - 1] in " \t":
        start -= 1
    return start == 0 or text[start - 1] in "\r\n"


def _in_a_fence(text: str, start: int) -> bool:
    """Whether a code fence opened before `start` is still open there."""
    return sum(1 for line in text[:start].split("\n") if line.lstrip().startswith(_FENCES)) % 2 == 1


def note(calls: list[ToolCall]) -> str:
    """What stands in the shown message where the markup was."""
    names = ", ".join(dict.fromkeys(c.name for c in calls))
    if len(calls) == 1:
        return f"[A tool call written here as text was read as a call: {names}]"
    return f"[{len(calls)} tool calls written here as text were read as calls: {names}]"


def extract(text: str) -> tuple[str, list[ToolCall]]:
    """(the message as it is shown, the calls found in it). The calls are those written as markup
    at the very end of the message, each block beginning a line outside any code fence; they
    leave the shown message, and a note takes their place when anything else is left of it.
    Markup anywhere else, or that can't be read as a call, is left as it is."""
    blocks: list[tuple[int, list[tuple[str, dict]]]] = []     # the blocks the message ends with, last first
    end = _before_space(text, len(text))
    while True:
        block = _block_ending_at(text, end)
        if block is None:
            break
        blocks.append(block)
        end = _before_space(text, block[0])
    blocks.reverse()
    # from the first of them that begins a line: one written after prose on the same line is
    # quoted, and whatever follows a block that was taken belongs with it
    first = next((i for i, (start, _) in enumerate(blocks) if _begins_a_line(text, start)), None)
    if first is None or _in_a_fence(text, blocks[first][0]):
        return text, []
    calls: list[ToolCall] = []
    for _, found in blocks[first:]:
        for name, args in found:
            calls.append(_call(name, args, len(calls)))
    kept = text[:blocks[first][0]].rstrip()
    return (f"{kept}\n\n{note(calls)}" if kept.strip() else ""), calls
