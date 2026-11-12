"""Engine tests against a fake OpenAI-compatible streaming endpoint."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from datoolkit import creds
from datoolkit.config import Config, Host, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm.client import LLMClient
from datoolkit.llm.tee import TeeClient

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
        self.completions = []  # queued texts for non-streaming requests
        self.requests = []    # captured request JSON
        self.refuse = {}      # model -> a setting its route rejects with HTTP 400 (as NanoGPT does)
        self.refusal = {}     # model -> what that refusal says, when not "does not support <setting>"
        self.overflow = None  # an error message for "too long for the context window" (HTTP 400)
        self.stream_headers = {}  # more headers on each stream (a TEE reply's X-Receipt-Id, say)
        self.receipt_ids = []     # a TEE reply's X-Receipt-Id for each stream in turn, while there are any

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "anthropic/claude-opus-5.5", "object": "model", "created": 0, "owned_by": "x"},
                {"id": "TEE/glm-5.3", "object": "model", "created": 0, "owned_by": "x"},
                {"id": "private/glm-5-3", "object": "model", "created": 0, "owned_by": "x"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        assert request.headers["authorization"] == "Bearer sk-test"
        setting, _, value = self.refuse.get(body["model"], "").partition("=")   # "key" or "key=value"
        if setting and setting in body and (not value or str(body[setting]) == value):
            return httpx.Response(400, json={"error": {"message": self.refusal.get(body["model"]) or (
                f"{body['model']} does not support {setting} on the selected route. Omit it or select another model.")}})
        if self.overflow:
            return httpx.Response(400, json={"error": {"message": self.overflow, "code": "context_length_exceeded"}})
        if not body.get("stream"):                  # plain completion (write-ups, vision helper)
            return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": body["model"],
                                             "choices": [{"index": 0, "finish_reason": "stop",
                                                          "message": {"role": "assistant", "content": self.completions.pop(0)}}]})
        receipt = {"X-Receipt-Id": self.receipt_ids.pop(0)} if self.receipt_ids else {}
        return httpx.Response(200, text=self.responses.pop(0),
                              headers={"content-type": "text/event-stream", **self.stream_headers, **receipt})


@pytest.fixture
async def env(tmp_path, monkeypatch):
    fake = FakeAPI()
    events = []
    cfg = Config(providers=[Provider("Fake", BASE, vision_overrides={"anthropic/claude-opus-5.5": "yes"})], hosts=[Host("web01", "ssh", "web01.lan", user="bob", auth="password")])
    engine = Engine(cfg, events.append, tmp_path / "rt", save_config=lambda c: None)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(engine, "_client", lambda prov, model="": LLMClient(prov.base_url, creds.get_secret("provider", prov.name), http))
    # TEE attestation never reaches the network in tests: this server offers none (tests/test_tee_engine.py
    # stands in a real one)
    engine._tee_factory = lambda base, key, model: TeeClient(
        base, key, model, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    creds.set_secret("provider", "Fake", "sk-test")
    await engine.start()
    yield engine, fake, events
    await engine.stop()


async def prompt_of(events, secs: float = 5.0) -> dict:
    """The credential prompt the engine sent, once it has: it may ask ssh for the host's
    address first (an alias's HostName), which takes longer on a busy machine."""
    for _ in range(int(secs * 100)):
        found = [e["prompt"] for e in events if e["type"] == "prompt"]
        if found:
            return found[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no credential prompt was shown")


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
    assert "TKT-1 disk full" in Path(md).read_text()


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
    prompt = await prompt_of(events)
    assert prompt["secret"] and prompt["can_save"]
    engine.answer_prompt(prompt["id"], "pw2", save=True)
    assert await task == "pw2"
    assert creds.get_secret("host", "web01") == "pw2"


async def test_askpass_jump_host_prompt_not_answered_from_keyring(env):
    engine, _, events = env
    engine.new_case("x", "open")
    creds.set_secret("host", "web01", "pw1")
    engine.sessions.spawn("web01", ["/bin/cat"], {}, name="web01", kind="ssh", host_name="web01")
    # a jump host's prompt: not answered from the keyring, and not offered for saving under web01
    # (a bastion's password saved as web01's would be sent to web01 next time)
    for text in ("me@bastion's password: ", "(me@web01.lan-bastion) Password: ", "Password for me@web01.lan.evil:"):
        events.clear()
        task = asyncio.create_task(engine._askpass("web01", text, "askpass"))
        prompt = await prompt_of(events)
        assert prompt["can_save"] is False, text
        engine.answer_prompt(prompt["id"], None)
        assert await task is None
    # the host itself, however the prompt is phrased
    for text in ("bob@web01.lan's password: ", "(bob@WEB01.LAN) Password: ", "Password:"):
        engine.sessions.sessions["web01"].used_stored_password = False
        assert await engine._askpass("web01", text, "askpass") == "pw1", text


async def test_a_prompt_naming_no_host_isnt_answered_for_a_host_behind_a_jump_host(env):
    engine, _, events = env
    engine.new_case("x", "open")
    engine.cfg.hosts.append(Host("inner", "ssh", "inner.lan", user="bob", auth="password", jump="me@bastion"))
    creds.set_secret("host", "inner", "inner-pw")
    engine.sessions.spawn("inner", ["/bin/cat"], {}, name="inner", kind="ssh", host_name="inner")
    task = asyncio.create_task(engine._askpass("inner", "Password: ", "askpass"))   # the bastion's, from an old client
    prompt = await prompt_of(events)
    assert prompt["can_save"] is False
    engine.answer_prompt(prompt["id"], None)
    assert await task is None
    assert await engine._askpass("inner", "bob@inner.lan's password: ", "askpass") == "inner-pw"


async def test_an_ssh_config_alias_still_gets_its_saved_password(env, monkeypatch):
    # Host web01 / HostName 10.0.0.5 in ~/.ssh/config: ssh's prompt names 10.0.0.5, not web01
    from datoolkit.sessions import ssh as ssh_mod
    engine, _, _ = env
    engine.new_case("x", "open")
    engine.cfg.hosts.append(Host("aliased", "ssh", "web01", user="bob", auth="password"))
    creds.set_secret("host", "aliased", "pw-alias")
    looked_up = []
    monkeypatch.setattr(ssh_mod, "resolved_hostname", lambda host: looked_up.append(host.host) or "10.0.0.5")
    engine.sessions.spawn("aliased", ["/bin/cat"], {}, name="aliased", kind="ssh", host_name="aliased")
    assert await engine._askpass("aliased", "bob@10.0.0.5's password: ", "askpass") == "pw-alias"
    engine.sessions.sessions["aliased"].used_stored_password = False
    assert await engine._askpass("aliased", "bob@10.0.0.5's password: ", "askpass") == "pw-alias"
    assert looked_up == ["web01"]                                         # once per host


def test_resolved_hostname_reads_what_ssh_would_use(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from datoolkit.sessions import ssh as ssh_mod
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: seen.append(argv) or SimpleNamespace(
        stdout="user bob\nhostname 10.0.0.5\nport 22\n"))
    assert ssh_mod.resolved_hostname(Host("a", "ssh", "web01", user="bob", port=2222)) == "10.0.0.5"
    assert seen[0][:2] == ["ssh", "-G"] and seen[0][-2:] == ["--", "web01"] and "2222" in seen[0]


async def test_a_notice_from_ssh_is_shown_not_asked(env):
    engine, _, events = env
    engine.new_case("x", "open")
    assert await engine._askpass("web01", "Confirm user presence for key ED25519-SK SHA256:abc", "notice") == ""
    assert not engine._prompts and any(e["type"] == "toast" and "Confirm user presence" in e["text"] for e in events)


def test_prompt_host():
    from datoolkit.engine import prompt_host

    assert prompt_host("bob@web01.lan's password: ") == "web01.lan"
    assert prompt_host("(bob@jump) Password: ") == "jump"
    assert prompt_host("me@[fe80::1]'s password:") == "fe80::1"
    assert prompt_host("Password:") == ""
    # OpenSSH writes IPv6 unbracketed; a user name may hold an @ (Kerberos/NTLM: bob@corp.com)
    assert prompt_host("Password for bob@fd00::10 (WinRM): ") == "fd00::10"
    assert prompt_host("bob@2001:db8::5's password: ") == "2001:db8::5"
    assert prompt_host("Password for bob@corp.com@winsrv.corp.com (WinRM): ") == "winsrv.corp.com"
    assert prompt_host("(bob@corp.com@web01.lan) Password: ") == "web01.lan"
    assert prompt_host("Enter passphrase for key '/home/bob@corp/.ssh/id': ") == ""
    assert prompt_host("Enter bob@web01.lan's old password: ") == "web01.lan"       # a password being changed
    assert prompt_host("bob@[fd00::10]'s password: ") == "fd00::10"
    assert prompt_host("(bob@fd00::10) Password: ") == "fd00::10"
    # After ssh's "(user@host) " comes text the server chose: the asking host is the one in the
    # prefix, whatever the text names. Without the prefix only ssh's own whole prompt names a host
    # (and the WinRM console's): a host somewhere in other text is nobody's word.
    assert prompt_host("(me@bastion) bob@inner.lan's password: ") == "bastion"
    assert prompt_host("(me@bastion) Password for bob@inner.lan (WinRM): ") == "bastion"
    assert prompt_host("(me@bastion) (bob@inner.lan) Password: ") == "bastion"
    assert prompt_host("(maintenance) bob@inner.lan's password: ") == ""
    assert prompt_host("Password for me@10.0.0.5:") == ""
    assert prompt_host("RDP password for bob@127.0.0.1") == ""
    assert prompt_host("Welcome to bastion. bob@inner.lan's password: ") == ""
    assert prompt_host("bob@inner.lan's password: \nreally") == ""


async def test_a_jump_host_cant_ask_for_the_targets_saved_password(env, monkeypatch):
    from datoolkit.sessions import ssh as ssh_mod
    engine, _, events = env
    engine.new_case("x", "open")
    engine.cfg.hosts.append(Host("inner", "ssh", "inner.lan", user="bob", auth="password", jump="me@bastion"))
    creds.set_secret("host", "inner", "inner-pw")
    sess = engine.sessions.spawn("inner", ["/bin/cat"], {}, name="inner", kind="ssh", host_name="inner")
    # the bastion's keyboard-interactive prompt: ssh's "(user@host) ", then text the bastion chose
    for text in ("(me@bastion) bob@inner.lan's password: ", "(me@bastion) (bob@inner.lan) Password: ",
                 "(me@bastion) Password for bob@inner.lan (WinRM): "):
        events.clear()
        task = asyncio.create_task(engine._askpass("inner", text, "askpass"))
        prompt = await prompt_of(events)                # shown to the technician, who sees who asks
        assert prompt["text"] == text.strip() and prompt["can_save"] is False, text
        engine.answer_prompt(prompt["id"], None)
        assert await task is None and not sess.used_stored_password
    # the target's own, through keyboard-interactive or password authentication
    for text in ("(bob@inner.lan) Password: ", "bob@inner.lan's password: ", "(bob@INNER.LAN.) password: "):
        sess.used_stored_password = False
        assert await engine._askpass("inner", text, "askpass") == "inner-pw", text
    # an OpenSSH that doesn't mark a server's text (before 8.4): any prompt may be the bastion's
    monkeypatch.setattr(ssh_mod, "marks_server_text", lambda: False)
    for text in ("bob@inner.lan's password: ", "(bob@inner.lan) Password: "):
        sess.used_stored_password = False
        events.clear()
        task = asyncio.create_task(engine._askpass("inner", text, "askpass"))
        prompt = await prompt_of(events)
        assert prompt["can_save"] is False, text
        engine.answer_prompt(prompt["id"], None)
        assert await task is None


async def test_a_jump_host_of_the_targets_own_name_gets_no_saved_password(env):
    engine, _, events = env
    engine.new_case("x", "open")
    # another port of the bastion: its prompts and the target's name the same host
    engine.cfg.hosts.append(Host("fwd", "ssh", "gw.lan", port=2222, user="bob", auth="password", jump="me@GW.lan:22"))
    creds.set_secret("host", "fwd", "fwd-pw")
    engine.sessions.spawn("fwd", ["/bin/cat"], {}, name="fwd", kind="ssh", host_name="fwd")
    task = asyncio.create_task(engine._askpass("fwd", "(me@gw.lan) Password: ", "askpass"))
    prompt = await prompt_of(events)
    assert prompt["can_save"] is False
    engine.answer_prompt(prompt["id"], None)
    assert await task is None


def test_jump_hosts_and_the_ssh_version(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from datoolkit.sessions import ssh as ssh_mod
    assert ssh_mod.jump_hosts("me@Bastion:2222, ssh://x@[FD00::1]:22,10.0.0.1,a@b@gw.lan.") == {
        "bastion", "fd00::1", "10.0.0.1", "gw.lan"}
    assert ssh_mod.jump_hosts("") == set()
    for banner, marks in (("OpenSSH_9.6p1 Ubuntu-3ubuntu13.20, OpenSSL 3.0.13", True), ("OpenSSH_8.4p1", True),
                          ("OpenSSH_8.2p1 Ubuntu-4ubuntu0.13", False), ("OpenSSH_10.0p2", True), ("Dropbear v2024.86", False)):
        ssh_mod.marks_server_text.cache_clear()
        monkeypatch.setattr(subprocess, "run", lambda argv, banner=banner, **k: SimpleNamespace(stderr=banner + "\n", stdout=""))
        assert ssh_mod.marks_server_text() is marks, banner
    ssh_mod.marks_server_text.cache_clear()

    def missing(argv, **k):
        raise FileNotFoundError("ssh")
    monkeypatch.setattr(subprocess, "run", missing)
    assert ssh_mod.marks_server_text() is True          # no ssh: nothing prompts
    ssh_mod.marks_server_text.cache_clear()


async def test_askpass_answers_a_user_at_domain_or_ipv6_host_from_the_keyring(env):
    engine, _, _ = env
    engine.new_case("x", "open")
    for name, address, prompt in (("win", "winsrv.corp.com", "Password for bob@corp.com@winsrv.corp.com (WinRM): "),
                                  ("v6", "fd00::10", "bob@fd00::10's password: ")):
        engine.cfg.hosts.append(Host(name, "ssh", address, user="bob", auth="password"))
        creds.set_secret("host", name, f"pw-{name}")
        engine.sessions.spawn(name, ["/bin/cat"], {}, name=name, kind="ssh", host_name=name)
        assert await engine._askpass(name, prompt, "askpass") == f"pw-{name}", prompt


async def test_a_pin_or_passcode_prompt_never_gets_the_host_password(env):
    engine, _, events = env
    engine.new_case("x", "open")
    creds.set_secret("host", "web01", "pw1")
    engine.sessions.spawn("web01", ["/bin/cat"], {}, name="web01", kind="ssh", host_name="web01")
    # each wrong PIN counts against a smart card's few tries
    for text in ("Enter PIN for 'PIV Card Holder pin (PIV_II)': ", "Enter PIN for ED25519-SK key /home/bob/.ssh/id_ed25519_sk: ",
                 "(bob@web01.lan) Enter PASSCODE: ", "Enter passphrase for key '/home/bob/.ssh/id_ed25519': "):
        events.clear()
        task = asyncio.create_task(engine._askpass("web01", text, "askpass"))
        prompt = await prompt_of(events)
        assert prompt["secret"] and prompt["can_save"] is False, text
        engine.answer_prompt(prompt["id"], "123456", save=True)
        assert await task == "123456"
    assert creds.get_secret("host", "web01") == "pw1"


async def test_a_password_asked_before_its_session_exists_is_saved_under_its_host(env):
    engine, _, events = env
    engine.new_case("x", "open")
    task = asyncio.create_task(engine.ask_user("rdp-dc01", "RDP password for bob@dc01.lan", secret=True, can_save=True,
                                               host_name="dc01"))
    prompt = await prompt_of(events)
    engine.answer_prompt(prompt["id"], "s3cret", save=True)
    assert await task == "s3cret" and creds.get_secret("host", "dc01") == "s3cret"


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


async def test_ai_is_told_when_hidden_characters_were_taken_out(env):
    engine, fake, _ = env
    engine.new_case("smuggle", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    fake.responses.append(tool_call_stream({"items": [
        {"session_id": sid, "command": "df -h", "purpose": "disk", "risk": "read_only"},
        {"session_id": sid, "command": "uptime​\x1b[201~", "purpose": "load", "risk": "read_only"},
    ]}))
    engine.send("slow server")
    await wait_turn(engine)
    reply = next(m["content"] for m in reversed(engine.conv) if m["role"] == "tool")
    assert "Invisible or control characters were taken out of #2 " in reply and "#1" not in reply.split("taken out of")[1]
    assert engine.queue.get(2).command == "uptime[201~" and engine.queue.get(2).hidden
    assert engine.snapshot()["queue"][1]["hidden"]                  # the page shows the warning
