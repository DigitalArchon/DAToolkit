"""Engine tests against a fake OpenAI-compatible streaming endpoint."""

import asyncio
import json

import httpx
import pytest

from datoolkit import creds
from datoolkit.config import Config, Host, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm.client import LLMClient

BASE = "https://fake.example/api/v1"


def sse(*chunks):
    lines = []
    for delta, finish in chunks:
        body = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        lines.append(f"data: {json.dumps(body)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def tool_call_stream(args: dict, text="Let's check disk space."):
    raw = json.dumps(args)
    half = len(raw) // 2
    return sse(
        ({"role": "assistant", "content": text}, None),
        ({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                          "function": {"name": "propose_commands", "arguments": raw[:half]}}]}, None),
        ({"tool_calls": [{"index": 0, "function": {"arguments": raw[half:]}}]}, None),
        ({}, "tool_calls"),
    )


class FakeAPI:
    def __init__(self):
        self.responses = []   # queued SSE bodies
        self.requests = []    # captured request JSON

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "anthropic/claude-opus-5.5", "object": "model", "created": 0, "owned_by": "x"},
                {"id": "TEE/glm-5.3", "object": "model", "created": 0, "owned_by": "x"},
                {"id": "private/glm-5-3", "object": "model", "created": 0, "owned_by": "x"}]})
        self.requests.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, text=self.responses.pop(0), headers={"content-type": "text/event-stream"})


@pytest.fixture
async def env(tmp_path, monkeypatch):
    fake = FakeAPI()
    events = []
    cfg = Config(providers=[Provider("Fake", BASE)], hosts=[Host("web01", "ssh", "web01.lan", user="bob", auth="password")])
    engine = Engine(cfg, events.append, tmp_path / "rt", save_config=lambda c: None)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(engine, "_client", lambda prov, model="": LLMClient(prov.base_url, creds.get_secret("provider", prov.name), http))
    creds.set_secret("provider", "Fake", "sk-test")
    await engine.start()
    yield engine, fake, events
    await engine.stop()


async def wait_turn(engine):
    for _ in range(200):
        if not engine.busy:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


async def test_models_default_and_tiers(env):
    engine, fake, _ = env
    models = await engine.list_models("Fake")
    assert {m["id"]: m["tier"] for m in models} == {"anthropic/claude-opus-5.5": "standard", "TEE/glm-5.3": "tee",
                                                   "private/glm-5-3": "e2ee"}
    assert engine.cfg.provider("Fake").default_model == "anthropic/claude-opus-5.5"
    assert engine.cfg.active_model == "anthropic/claude-opus-5.5"  # auto-selected on first test


async def test_sensitivity_gates_models(env):
    engine, _, _ = env
    engine.new_case("secret client", "confidential")
    await engine.list_models("Fake")
    assert engine.cfg.active_model == ""  # Opus is the default but isn't permitted here
    for blocked in ("anthropic/claude-opus-5.5", "TEE/glm-5.3"):  # TEE/ prompts pass the gateway in clear
        with pytest.raises(UserError):
            engine.select_model("Fake", blocked)
    models = {m["id"]: m["allowed"] for m in await engine.list_models("Fake")}
    assert models == {"anthropic/claude-opus-5.5": False, "TEE/glm-5.3": False, "private/glm-5-3": True}
    engine.new_case("lab", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    assert engine.active_tier() == "tee"


async def test_full_loop(env):
    engine, fake, events = env
    engine.new_case("TKT-1 disk full", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]

    fake.responses.append(tool_call_stream({"items": [
        {"session_id": sid, "command": "df -h", "purpose": "disk usage", "risk": "read_only"},
        {"session_id": sid, "command": "sudo reboot", "purpose": "yolo", "risk": "read_only"},
    ]}))
    engine.send("Server says disk full")
    await wait_turn(engine)

    req = fake.requests[0]
    assert req["model"] == "anthropic/claude-opus-5.5"
    assert req["tools"][0]["function"]["name"] == "propose_commands"
    assert sid in req["messages"][0]["content"]  # roster in the system prompt
    assert req["messages"][-1] == {"role": "user", "content": "Server says disk full"}

    q = engine.queue.items
    assert [p.command for p in q] == ["df -h", "sudo reboot"]
    assert q[1].risk == "disruptive"            # local rule overrides the model's label
    assert engine.chat[-1]["proposals"] == [1, 2]
    # tool call is answered immediately so the history stays valid
    assert engine.conv[-1]["role"] == "tool" and "#1, #2" in engine.conv[-1]["content"]

    engine.update_item(1, status="ran")
    engine.update_item(2, status="skipped", note="not on prod")
    fake.responses.append(sse(({"role": "assistant", "content": "Root is 99% full."}, "stop")))
    engine.send("", results=[{"num": 1, "text": "/dev/sda1  20G  19.8G  0.2G  99% /"}, {"num": 2}])
    await wait_turn(engine)

    user_msg = fake.requests[1]["messages"][-1]
    assert user_msg["role"] == "user"
    assert "#1 on session" in user_msg["content"] and "99% /" in user_msg["content"]
    assert "#2" in user_msg["content"] and "SKIPPED" in user_msg["content"] and "not on prod" in user_msg["content"]
    roles = [m["role"] for m in fake.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert all(p.status == "sent" for p in engine.queue.items)
    assert engine.chat[-1]["text"] == "Root is 99% full."
    types = [e["type"] for e in events]
    assert "delta" in types and "turn_end" in types

    log = (engine.case.dir / "events.jsonl").read_text()
    assert "sent_to_ai" in log and "proposal_skipped" in log
    md = engine.export_markdown()["path"]
    assert "TKT-1 disk full" in open(md).read()


async def test_invalid_tool_args_retry(env):
    engine, fake, _ = env
    engine.new_case("x", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    bad = sse(({"role": "assistant", "tool_calls": [{"index": 0, "id": "c0", "type": "function",
               "function": {"name": "propose_commands", "arguments": "{not json"}}]}, "tool_calls"))
    fake.responses += [bad, sse(({"role": "assistant", "content": "Sorry, fixed."}, "stop"))]
    engine.send("hi")
    await wait_turn(engine)
    assert len(fake.requests) == 2
    assert fake.requests[1]["messages"][-1]["role"] == "tool"
    assert "Invalid" in fake.requests[1]["messages"][-1]["content"]


async def test_send_requires_model_and_case(env):
    engine, _, _ = env
    with pytest.raises(UserError):
        engine.send("hi")
    engine.new_case("x", "sovereign")
    with pytest.raises(UserError):
        engine.send("hi")


async def test_askpass_uses_keyring_once_then_asks(env):
    engine, _, events = env
    engine.new_case("x", "open")
    creds.set_secret("host", "web01", "pw1")
    engine.sessions.spawn("web01", ["/bin/cat"], {}, name="web01", kind="ssh", host_name="web01")
    assert await engine._askpass("web01", "bob@web01.lan's password: ", "askpass") == "pw1"
    # second password prompt (e.g. wrong password) must go to the technician, not loop on the keyring
    task = asyncio.create_task(engine._askpass("web01", "bob@web01.lan's password: ", "askpass"))
    await asyncio.sleep(0.05)
    prompt = next(e for e in events if e["type"] == "prompt")["prompt"]
    assert prompt["secret"] and prompt["can_save"]
    engine.answer_prompt(prompt["id"], "pw2", save=True)
    assert await task == "pw2"
    assert creds.get_secret("host", "web01") == "pw2"


async def test_askpass_jump_host_prompt_not_answered_from_keyring(env):
    engine, _, events = env
    engine.new_case("x", "open")
    creds.set_secret("host", "web01", "pw1")
    engine.sessions.spawn("web01", ["/bin/cat"], {}, name="web01", kind="ssh", host_name="web01")
    task = asyncio.create_task(engine._askpass("web01", "me@bastion's password: ", "askpass"))
    await asyncio.sleep(0.05)
    prompt = next(e for e in events if e["type"] == "prompt")["prompt"]
    engine.answer_prompt(prompt["id"], None)
    assert await task is None


async def test_missing_session_id_defaults_to_only_open_session(env):
    engine, fake, _ = env
    engine.new_case("x", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    fake.responses.append(tool_call_stream({"items": [
        {"session_id": "", "command": "uptime", "purpose": "load", "risk": "read_only"},
        {"command": "free -h", "purpose": "memory", "risk": "read_only"}]}))
    engine.send("slow box")
    await wait_turn(engine)
    assert [p.session_id for p in engine.queue.items] == ["local", "local"]
    assert "do not exist" not in engine.conv[-1]["content"]

    # with two sessions open, an unknown id is left for the technician to pick
    engine.open_session("local")
    fake.responses.append(tool_call_stream({"items": [
        {"session_id": "nope", "command": "df -h", "purpose": "disk", "risk": "read_only"}]}))
    engine.send("and disk?")
    await wait_turn(engine)
    assert engine.queue.items[-1].session_id == "nope"
    assert "do not exist" in engine.conv[-1]["content"]
