"""Full case export: everything the AI was sent and everything it returned.

A ZIP with a readable full-transcript.md (every model request: the system prompt whenever it
changed, the messages sent, the reasoning and the reply with its tool calls), the exact
images the model received, and the raw data (request log, conversation, chat, queue,
hypotheses, audit log). Raw terminal transcripts are included only on request: they are
unredacted, unlike anything that was sent to a model."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from .case import fence
from .llm.prompts import STATE_HEADER

README = """\
DAToolkit full export of case "{name}" ({id}), made {when}.

full-transcript.md   Every request DAToolkit made to an AI model, in order: the system prompt
                     (shown in full whenever it changed), the messages sent, the model's
                     reasoning as the provider returned it, and its reply including tool calls.
images/              The images exactly as the model received them (already redacted by the
                     technician and re-encoded without metadata).
documents/           Markdown transcript and any AI-written ticket summary, client update, runbook.
data/                Raw data: requests.jsonl (the request log), conversation.json (the messages
                     as kept for the model, images replaced by file names), chat.json, queue.json,
                     hypotheses.json, events.jsonl (audit log), case.json.
terminals/           {terminals}

Reasoning is whatever the provider streamed back; some providers return a summary of the
model's thinking rather than all of it, and some return none.
"""


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"


def _content_md(content, images_dir: str = "images") -> list[str]:
    if content is None:
        return []
    if isinstance(content, str):
        return [content] if content.strip() else []
    out = []
    for part in content:
        if part.get("type") == "text" and part.get("text", "").strip():
            out.append(part["text"])
        elif part.get("type") in ("image", "image_url"):
            name = part.get("file", "image")
            out.append(f"![{name}]({images_dir}/{name})")
    return out


def _tool_calls_md(calls: list[dict]) -> list[str]:
    out = []
    for c in calls:
        name = c.get("name") or c.get("function", {}).get("name", "?")
        args = c.get("arguments") if "arguments" in c else c.get("function", {}).get("arguments", "")
        try:
            args = json.dumps(json.loads(args or "{}"), indent=2, ensure_ascii=False)
        except ValueError:
            pass
        out += [f"Tool call `{name}`:", "", fence(args, "json"), ""]
    return out


def _message_md(m: dict) -> list[str]:
    role = m.get("role")
    if role == "user" and str(m.get("content", "")).startswith(STATE_HEADER):
        return ["**Current state (sent by DAToolkit after the conversation, so the prompt cache keeps the rest)**", "",
                fence(str(m["content"])[len(STATE_HEADER):].strip()), ""]
    if role == "user":
        return ["**Technician → AI**", ""] + [x for c in _content_md(m.get("content")) for x in (c, "")]
    if role == "assistant":
        names = ", ".join(c.get("function", {}).get("name", "?") for c in m.get("tool_calls", []))
        return ["**AI's previous reply, sent back as context**"
                + (f" (with tool calls: {names})" if names else ""), ""]
    if role == "tool":
        return [f"**Tool result → AI** (call `{m.get('tool_call_id', '')}`)", "", fence(str(m.get("content", ""))), ""]
    if role == "system":
        return ["**System prompt**", "", fence(str(m.get("content", ""))), ""]
    return [f"**{role}**", ""] + _content_md(m.get("content"))


def full_transcript(case: dict, requests: list[dict], conv: list[dict], chat: list[dict],
                    system_now: str) -> str:
    out = [f"# Full export: {case['name']}", "",
           f"- Case id: `{case['id']}`", f"- Started: {case.get('started', '?')}",
           f"- Sensitivity: {case.get('sensitivity', '?')}",
           f"- Exported: {datetime.now():%Y-%m-%d %H:%M:%S}"]
    models = sorted({f"{r.get('model')} ({r.get('tier')})" for r in requests})
    if models:
        out.append(f"- Models: {', '.join(models)}")
    out.append(f"- Model requests logged: {len(requests)}")
    if case.get("notes"):
        out += ["", "## Case notes", "", case["notes"]]
    out.append("")

    chat_requests = [r for r in requests if r.get("purpose") == "chat"]
    first_logged = chat_requests[0].get("conv_index", 0) if chat_requests else len(conv)
    if first_logged > 0:
        out += ["## Conversation before request logging (reconstructed)", "",
                "This case began before DAToolkit logged each model request, so the requests themselves "
                "cannot be shown. These are the messages as kept for the model; the system prompt at the "
                "end is the one that would be sent now, and earlier requests' prompts differed in detail.", ""]
        for m in conv[:first_logged]:
            if m.get("role") == "assistant":
                out += ["**AI**", ""] + _content_md(m.get("content")) + [""] + _tool_calls_md(m.get("tool_calls", []))
            else:
                out += _message_md(m)
        reasoning = [e for e in chat if e.get("kind") == "assistant" and e.get("reasoning")]
        if reasoning:
            out += ["### Reasoning shown in the chat, by AI message", ""]
            for i, e in enumerate(reasoning, 1):
                start = (e.get("text") or "").strip().split("\n")[0][:100]
                out += [f"**AI message {i}** (begins: \"{start}\")", "", fence(e["reasoning"]), ""]
        out += ["### System prompt (as it would be sent now)", "", fence(system_now), ""]

    if requests:
        out += ["## Model requests", ""]
    last_system = {}
    for n, r in enumerate(requests, 1):
        purpose = r.get("purpose", "chat")
        out += [f"### Request {n} · {purpose} · {r.get('model')} ({r.get('tier')}) · {_when(r.get('ts'))}", ""]
        sha = r.get("system_sha256", "")
        if "system" in r:
            out += ["**System prompt**" + (" (changed)" if purpose == "chat" and last_system.get("chat") else ""), "",
                    fence(r["system"]), ""]
            last_system[purpose if purpose == "chat" else sha] = n
        else:
            out += [f"*System prompt unchanged since request {last_system.get('chat', '?')}.*", ""]
        msgs = r.get("messages", [])
        if purpose == "chat" and r.get("conv_index") == 0 and n > 1 and len(msgs) > 1:
            out += ["*The conversation was trimmed or rebuilt before this request, so all of it was sent again:*", ""]
        if msgs:
            out += ["**Sent**" + (" (new since the previous request)" if purpose == "chat" or
                                  (purpose == "compact" and r.get("conv_index")) else ""), ""]
            for m in msgs:
                out += _message_md(m)
        resp = r.get("response", {})
        if resp.get("reasoning"):
            out += ["**Reasoning**", "", fence(resp["reasoning"]), ""]
        if resp.get("content"):
            out += ["**Reply**", "", resp["content"], ""]
        out += _tool_calls_md(resp.get("tool_calls", []))
        if resp.get("error"):
            out += [f"**Request ended with an error:** {resp['error']}", ""]
        usage = resp.get("usage") or {}
        if usage.get("prompt_tokens"):
            out += [f"*Tokens: {usage.get('prompt_tokens')} in, {usage.get('completion_tokens', 0)} out.*", ""]
    return "\n".join(out).rstrip() + "\n"


def build_zip(case: dict, case_dir: Path, requests: list[dict], conv: list[dict], chat: list[dict],
              queue: list[dict], hypotheses: list[dict], system_now: str, transcript_md: str,
              include_terminals: bool) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        dump = lambda obj: json.dumps(obj, ensure_ascii=False, indent=1)  # noqa: E731
        z.writestr("README.txt", README.format(
            name=case["name"], id=case["id"], when=f"{datetime.now():%Y-%m-%d %H:%M}",
            terminals="Raw terminal transcripts (UNREDACTED: everything the terminals showed)."
            if include_terminals else "Not included (raw terminal transcripts are unredacted; tick the option to add them)."))
        z.writestr("full-transcript.md", full_transcript(case, requests, conv, chat, system_now))
        for f in sorted(case_dir.glob("img-*")):
            z.write(f, f"images/{f.name}")
        z.writestr("documents/transcript.md", transcript_md)
        for name in ("ticket-summary.md", "client-update.md", "runbook.md"):
            if (case_dir / name).exists():
                z.write(case_dir / name, f"documents/{name}")
        z.writestr("data/requests.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
        z.writestr("data/conversation.json", dump(conv))
        z.writestr("data/chat.json", dump(chat))
        z.writestr("data/queue.json", dump(queue))
        z.writestr("data/hypotheses.json", dump(hypotheses))
        z.writestr("data/case.json", dump(case))
        if (case_dir / "events.jsonl").exists():
            z.write(case_dir / "events.jsonl", "data/events.jsonl")
        if include_terminals:
            for f in sorted(case_dir.glob("term-*.log")):
                z.write(f, f"terminals/{f.name}")
    return buf.getvalue()
