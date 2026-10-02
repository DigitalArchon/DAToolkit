"""The AI revising its own queue, the no-message nudge, and gated web search via NanoGPT."""

import asyncio
import json

import httpx
import pytest

from datoolkit import creds
from datoolkit.config import Provider
from datoolkit.engine import NO_MESSAGE_NUDGE, UserError
from datoolkit.llm import prompts, websearch
from datoolkit.queue import Queue

from test_engine import env, sse, wait_turn  # noqa: F401
from test_features import multi_tool_stream


def setup(engine, sensitivity="open"):
    engine.new_case("c", sensitivity)
    engine.select_model("Fake", "anthropic/claude-opus-5.5") if sensitivity == "open" else None
    engine.open_session("local")
    return engine.sessions.roster()[0]["id"]


# ---------------------------------------------------------------- revise_queue

def test_queue_reorder_keeps_other_items_in_place():
    q = Queue()
    q.add("c", [{"session_id": "s", "command": f"c{i}", "risk": "read_only"} for i in range(1, 6)])
    q.update(2, status="ran")
    assert q.reorder([5, 3, 99, "x", 2]) == [5, 3]         # unknown and non-pending numbers ignored
    assert [p.num for p in q.items] == [1, 2, 5, 4, 3]


async def test_ai_withdraws_and_reorders_pending_items(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    engine.queue.add("c", [{"session_id": sid, "command": c, "risk": "read_only"}
                           for c in ("ls -la | sh-only", "uptime", "df -h", "echo hi")])
    engine.update_item(2, status="ran")
    fake.responses.append(multi_tool_stream([
        ("revise_queue", {"withdraw": [{"num": 1, "reason": "sh syntax; this shell is csh"},
                                       {"num": 2, "reason": "x"}, {"num": 42, "reason": "y"}],
                          "order": [4, 3]}),
    ], text="The root shell is csh, so #1 won't work. I've withdrawn it."))
    engine.send("fyi the shell is csh")
    await wait_turn(engine)

    assert engine.queue.get(1).status == "withdrawn" and "csh" in engine.queue.get(1).note
    assert engine.queue.get(2).status == "ran"             # already run: untouched
    assert [p.num for p in engine.queue.items if p.status == "pending"] == [4, 3]
    entry = engine.chat[-1]
    assert entry["withdrawn"] == [{"num": 1, "reason": "sh syntax; this shell is csh"}]
    assert entry["reordered"] == [4, 3]
    reply = [m for m in engine.conv if m["role"] == "tool"][-1]["content"]
    assert "Withdrew #1" in reply and "#2 is already ran" in reply and "#42 is not in the queue" in reply
    # the next system prompt lists what is still pending, and the ran item awaiting results
    sysprompt = engine._system_prompt()
    assert "Pending, not yet run" in sysprompt and "`echo hi`" in sysprompt and "`uptime`" in sysprompt
    assert "ls -la" not in sysprompt.split("Technician's queue now:")[1].split("\n\n")[0]
    # a withdrawn item is never sent as a result; the technician can restore it
    engine.update_item(1, status="pending", note="")
    assert engine.queue.get(1).status == "pending"
    assert "withdrawn" in (engine.case.dir / "events.jsonl").read_text()


def test_prompt_mentions_revising_and_reasoning_visibility():
    assert "revise_queue" in [t["function"]["name"] for t in prompts.TOOLS]
    assert "reasoning is not shown" in prompts.SYSTEM_PROMPT


# ---------------------------------------------------------------- no-message nudge

async def test_tool_calls_without_a_message_get_one_nudge(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    silent = sse(({"role": "assistant", "reasoning": "I think csh..."}, None),
                 ({"tool_calls": [{"index": 0, "id": "c0", "type": "function", "function": {
                     "name": "propose_commands", "arguments": json.dumps({"items": [
                         {"session_id": sid, "command": "uname -a", "purpose": "os", "risk": "read_only"}]})}}]}, None),
                 ({}, "tool_calls"))
    fake.responses += [silent, sse(({"role": "assistant", "content": "Run #1 to identify the OS."}, "stop"))]
    engine.send("router")
    await wait_turn(engine)
    assert len(fake.requests) == 2
    nudge = fake.requests[1]["messages"][-1]["content"]
    assert NO_MESSAGE_NUDGE.strip()[:20] in nudge and "(queued #1)" in nudge
    assert engine.chat[-1]["text"] == "Run #1 to identify the OS." and engine.chat[-1]["proposals"] == [1]


async def test_nudge_happens_only_once(env):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    hyp = lambda: multi_tool_stream([("update_hypotheses", {"items": []})], text="")  # noqa: E731
    fake.responses += [hyp(), hyp()]
    engine.send("x")
    await wait_turn(engine)
    assert len(fake.requests) == 2


# ---------------------------------------------------------------- web search

class FakeSearch:
    def __init__(self, payload=None, status=200):
        self.calls = []
        self.payload = payload if payload is not None else {"data": [
            {"title": "OPNsense 25.1 advisory", "url": "https://opnsense.org/adv", "content": "Fixes OpenVPN renegotiation."},
            {"title": "js", "url": "javascript:alert(1)", "snippet": "bad link"}], "metadata": {"cost": 0.01}}
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                           "body": json.loads(request.content)})
        return httpx.Response(self.status, json=self.payload)


@pytest.fixture
def search(env):  # noqa: F811
    engine, fake, events = env
    engine.cfg.providers.append(Provider("NanoGPT", "https://nano-gpt.com/api/v1"))
    creds.set_secret("provider", "NanoGPT", "sk-nano")
    fs = FakeSearch()
    engine._search_http = httpx.AsyncClient(transport=httpx.MockTransport(fs.handler))
    return engine, fake, events, fs


def search_call(query="OPNsense 25.1 OpenVPN TLS renegotiation bug", text="Let me check advisories."):
    return multi_tool_stream([("web_search", {"query": query, "reason": "known bugs"})], text=text)


async def wait_for(pred):
    for _ in range(300):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_search_waits_for_approval_and_can_be_edited(search):
    engine, fake, events, fs = search
    setup(engine)
    fake.responses += [search_call(), sse(({"role": "assistant", "content": "The advisory fixes it."}, "stop"))]
    engine.send("vpn drops")
    await wait_for(lambda: engine._search_reqs)
    assert fs.calls == []                                   # nothing leaves before approval
    rid = next(iter(engine._search_reqs))
    assert engine.snapshot()["search_requests"][0]["status"] == "awaiting"
    engine.answer_search(rid, True, "OPNsense 25.1 OpenVPN renegotiation")
    await wait_turn(engine)

    assert fs.calls[0]["url"] == "https://nano-gpt.com/api/web"
    assert fs.calls[0]["auth"] == "Bearer sk-nano"
    assert fs.calls[0]["body"] == {"query": "OPNsense 25.1 OpenVPN renegotiation", "provider": "perplexity", "outputType": "searchResults",
                                   "excludeDomains": list(websearch.UNREADABLE)}
    tool_reply = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "OPNsense 25.1 advisory" in tool_reply and "Untrusted" in tool_reply and "edited your query" in tool_reply
    rec = engine.chat[-1]["searches"][0]
    assert rec["status"] == "done" and rec["results"][0]["url"] == "https://opnsense.org/adv"
    assert rec["results"][1]["url"] == ""                   # non-http links dropped
    assert engine.chat[-1]["text"].endswith("The advisory fixes it.")
    assert any(e["type"] == "search" and e["search"]["status"] == "awaiting" for e in events)


async def test_declined_search_is_reported_to_the_ai(search):
    engine, fake, _, fs = search
    setup(engine)
    fake.responses += [search_call(), sse(({"role": "assistant", "content": "OK, without it then."}, "stop"))]
    engine.send("vpn drops")
    await wait_for(lambda: engine._search_reqs)
    engine.answer_search(next(iter(engine._search_reqs)), False)
    await wait_turn(engine)
    assert fs.calls == []
    assert "declined" in [m for m in fake.requests[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert engine.chat[-1]["searches"][0]["status"] == "declined"


async def test_auto_mode_searches_and_redacts(search):
    engine, fake, _, fs = search
    engine.cfg.settings.search_mode = "auto"
    setup(engine)
    fake.responses += [search_call("error with password=hunter2 on OPNsense"), sse(({"role": "assistant", "content": "done"}, "stop"))]
    engine.send("x")
    await wait_turn(engine)
    assert "hunter2" not in fs.calls[0]["body"]["query"]


async def test_confidential_forces_ask_and_sovereign_disables(search):
    engine, fake, _, fs = search
    engine.cfg.settings.search_mode = "auto"
    engine.new_case("conf", "confidential")
    assert engine.search_status()["mode"] == "ask"
    engine.new_case("sov", "sovereign")
    assert engine.search_status()["mode"] == "off"
    engine.new_case("open", "open")
    engine.cfg.settings.search_mode = "off"
    assert engine.search_status()["mode"] == "off"
    engine.cfg.settings.search_mode = "ask"
    assert engine.search_status()["mode"] == "ask" and engine.search_status()["via"] == "NanoGPT"


async def test_search_tool_offered_only_when_available(search):
    engine, fake, _, fs = search
    engine.cfg.settings.search_mode = "off"
    setup(engine)
    fake.responses.append(sse(({"role": "assistant", "content": "hi"}, "stop")))
    engine.send("x")
    await wait_turn(engine)
    names = [t["function"]["name"] for t in fake.requests[0]["tools"]]
    assert "web_search" not in names and "revise_queue" in names
    assert "Web search (web_search) is available" not in fake.requests[0]["messages"][0]["content"]


async def test_search_failure_and_unavailable_are_tool_replies(search):
    engine, fake, _, fs = search
    engine.cfg.settings.search_mode = "auto"
    fs.status, fs.payload = 402, {"error": {"message": "Balance too low"}}
    setup(engine)
    fake.responses += [search_call(), sse(({"role": "assistant", "content": "k"}, "stop"))]
    engine.send("x")
    await wait_turn(engine)
    reply = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "insufficient NanoGPT balance" in reply and "Balance too low" in reply
    with pytest.raises(UserError):
        await engine.test_search("x")


async def test_settings_validate_search_fields(search):
    engine, *_ = search
    engine.save_settings({"search_mode": "auto", "search_provider": "perplexity", "search_via": "NanoGPT"})
    assert engine.cfg.settings.search_provider == "perplexity"
    with pytest.raises(UserError):
        engine.save_settings({"search_provider": "google"})
    with pytest.raises(UserError):
        engine.save_settings({"search_mode": "always"})


def test_normalize_accepts_common_shapes():
    assert websearch.normalize({"results": [{"name": "A", "link": "https://a", "description": "d", "age": "2d"}]}) == [
        {"title": "A", "url": "https://a", "snippet": "d", "date": "2d"}]
    assert websearch.normalize([{"title": "B", "url": "https://b", "text": "t"}])[0]["snippet"] == "t"
    assert websearch.normalize({"answer": "summary"})[0]["snippet"] == "summary"
    assert websearch.normalize("plain text")[0]["snippet"] == "plain text"
    assert websearch.normalize(None) == []
    assert websearch.search_url("https://nano-gpt.com/api/v1") == "https://nano-gpt.com/api/web"
    assert websearch.is_nanogpt("https://api.nano-gpt.com/v1") and not websearch.is_nanogpt("https://evil-nano-gpt.com")


async def test_zero_data_retention_falls_back_to_linkup(search):
    engine, fake, _, fs = search
    engine.cfg.settings.search_mode = "auto"
    zdr = {"error": {"type": "invalid_request_error", "code": "zero_data_retention", "message": "not compatible with ZDR"}}

    def handler(request):
        body = json.loads(request.content)
        fs.calls.append(body)
        if body["provider"] != "linkup":
            return httpx.Response(400, json=zdr)
        return httpx.Response(200, json={"data": [{"title": "It&#x27;s fixed", "url": "https://x.example", "content": "a &amp; b"}],
                                         "metadata": {"cost": 0.006}})
    engine._search_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine.cfg.settings.search_provider = "kagi"
    r = await engine.test_search("q")
    assert [c["provider"] for c in fs.calls] == ["kagi", "linkup"]
    assert r["provider"] == "linkup" and r["cost"] == 0.006 and "Zero Data Retention" in r["note"]
    assert r["results"][0]["title"] == "It's fixed" and r["results"][0]["snippet"] == "a & b"


async def test_a_provider_failing_on_nanogpts_side_falls_back_to_valyu(search):
    engine, fake, _, fs = search
    engine.cfg.settings.search_provider = "perplexity"

    def handler(request):
        body = json.loads(request.content)
        fs.calls.append(body["provider"])
        if body["provider"] == "perplexity":
            return httpx.Response(504, json={"error": "Search returned no usable results."})
        return httpx.Response(200, json={"data": [{"title": "T", "url": "https://x.example", "content": "c"}],
                                         "metadata": {"cost": 0.006}})
    engine._search_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = await engine.test_search("q")
    assert fs.calls == ["perplexity", "valyu"] and r["provider"] == "valyu" and r["count"] == 1
    assert r["note"] == "perplexity failed on NanoGPT's side (HTTP 504); used valyu" and r["note"] and r["note"].endswith("used valyu")
    # a request error (4xx other than Zero Data Retention) is not retried elsewhere
    fs.calls.clear()
    engine._search_http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (fs.calls.append(1), httpx.Response(402, json={"error": "Insufficient balance"}))[1]))
    with pytest.raises(UserError, match="insufficient NanoGPT balance"):
        await engine.test_search("q")
    assert fs.calls == [1]


def test_snippets_are_cut_at_the_given_length():
    long = [{"title": "T", "url": "https://x", "snippet": "a" * 3000, "date": ""}]
    assert 2000 <= websearch.format_for_model("q", "p", long).count("a") < 2100
    assert websearch.format_for_model("q", "p", long, snippet_chars=2500).count("a") >= 2500
