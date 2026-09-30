"""Sensitive-data flag and automatic second opinions."""

import asyncio

import pytest

from datoolkit.config import Provider
from datoolkit.engine import Engine
from datoolkit.queue import Queue
from datoolkit.safety.sensitive import sensitive

from test_engine import env  # noqa: F401


@pytest.mark.parametrize("cmd", [
    "cat /etc/shadow", "sudo cat ~/.ssh/id_ed25519", "cat /etc/ssl/private/site.key", "cat /opt/app/.env",
    "less .env.production", "env", "printenv", "env | grep DB", "docker exec -it web env", "history | tail -50",
    "kubectl get secret db -o yaml", "docker inspect seafile", "mysqldump -u root db > x.sql", "show running-config",
    'mysql -uroot -pHunter2 -e "show databases"', "curl -u admin:secret https://x", "curl https://bob:pw@host/",
    "sshpass -p hunter2 ssh x", "DB_PASSWORD=abc ./run.sh", "export API_KEY=sk123", "echo bob:pw | chpasswd",
    "Get-ChildItem env:", r"reg save HKLM\SAM sam.hiv", "netsh wlan show profile name=x key=clear",
    "grep -ri password /etc/myapp", "cat /proc/1/environ", "docker login --password hunter2",
    r"gc C:\Windows\Panther\unattend.xml",
])
def test_sensitive_flagged(cmd):
    assert sensitive(cmd)


@pytest.mark.parametrize("cmd", [
    "sudo ls -la /opt/seafile-mysql /opt/seafile-data/ssl 2>&1 | head -40; getent passwd 1000 999",
    "ls -la /etc/ssl/private", "stat .env", "cat .env.example", "cat ~/.ssh/id_rsa.pub",
    'grep "Failed password" /var/log/auth.log', "printenv PATH", "set -euo pipefail", "docker login --password-stdin",
    "mysql -u root -p", "curl -u $USER https://x", "TOKEN=$(cat /run/secrets/t) ./x", "show running-config interface Gi0/1",
    "show ip int brief", "df -h", "cat /etc/passwd", "sha256sum /etc/ssl/private/site.key", "net user bob /domain",
    "systemctl status sshd", "cat /etc/nginx/nginx.conf", "history -c",
])
def test_sensitive_not_flagged(cmd):
    assert sensitive(cmd) == []


def test_queue_flags_and_edit_clears_review():
    q = Queue()
    [p] = q.add("c", [{"session_id": "s", "command": "cat /etc/shadow", "risk": "read_only"}])
    assert p.risk == "read_only" and p.sensitive
    p.review = {"status": "done", "level": "ok"}
    q.update(p.num, command="cat /etc/hostname")
    assert p.sensitive == [] and p.review == {}


def test_resumed_queue_drops_an_unfinished_review():
    q = Queue()
    q.add("c", [{"session_id": "s", "command": "sudo reboot", "risk": "disruptive"}])
    q.items[0].review = {"status": "checking", "auto": True}
    assert Queue.from_list(q.to_list()).items[0].review == {}


@pytest.mark.parametrize("text,level,data", [
    ("Reboots.\nSUMMARY: Reboots the host.\nDATA: none\nVERDICT: proceed with care", "care", ""),
    ("**SUMMARY:** Prints keys.\n**DATA:** the private key, to the AI\n**VERDICT:** do not run", "stop", "the private key, to the AI"),
    ("SUMMARY: Harmless.\nDATA: None.\nVERDICT: proceed", "ok", ""),
    ("no closing lines at all", "", ""),
])
def test_parse_review(text, level, data):
    r = Engine._parse_review(text)
    assert r["level"] == level and r["data"] == data


def _setup(engine, monkeypatch, reply="Reboots it.\nSUMMARY: Reboots the host.\nDATA: none\nVERDICT: proceed with care"):
    engine.cfg.providers.append(Provider("Local", "http://localhost:11434/v1"))
    engine.new_case("s", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sent = []

    class Reviewer:
        async def complete(self, model, messages, params=None):
            sent.append((model, messages[1]["content"]))
            await asyncio.sleep(0)
            return reply

    monkeypatch.setattr(engine, "_client", lambda prov, model="": Reviewer())
    return engine.sessions.roster()[0]["id"], sent


async def _drain(engine):
    while engine._background:
        await asyncio.gather(*list(engine._background), return_exceptions=True)


async def test_auto_review_flagged_items(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid, sent = _setup(engine, monkeypatch)
    engine.save_settings({"review_model": "Local|llama3", "auto_review": "flagged"})
    added = engine.queue.add("c", [
        {"session_id": sid, "command": "df -h", "risk": "read_only"},
        {"session_id": sid, "command": "cat /etc/shadow", "risk": "read_only"},
        {"session_id": sid, "command": "sudo systemctl restart nginx", "risk": "disruptive", "rollback": "n/a"},
    ])
    engine._auto_review(added)
    assert added[1].review["status"] == "checking"
    await _drain(engine)
    plain, sens, disr = added
    assert plain.review == {}
    assert sens.review["status"] == "done" and disr.review["level"] == "care" and disr.review["auto"]
    assert {m for m, _ in sent} == {"llama3"}
    assert any("may expose sensitive data" in text for _, text in sent)


async def test_auto_review_disruptive_only_and_needs_a_reviewer(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid, sent = _setup(engine, monkeypatch)
    engine.save_settings({"auto_review": "disruptive"})     # no reviewer model: never falls back to the chat model
    added = engine.queue.add("c", [{"session_id": sid, "command": "sudo reboot", "risk": "disruptive"}])
    engine._auto_review(added)
    await _drain(engine)
    assert added[0].review == {} and not sent
    engine.save_settings({"review_model": "Local|llama3"})   # turning it on catches up on pending items
    added += engine.queue.add("c", [{"session_id": sid, "command": "cat /etc/shadow", "risk": "read_only"}])
    engine._auto_review(added)
    await _drain(engine)
    assert added[0].review["status"] == "done" and added[1].review == {}


async def test_auto_review_result_dropped_when_command_changes(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid, _ = _setup(engine, monkeypatch)
    gate = asyncio.Event()

    class Echo:
        async def complete(self, model, messages, params=None):
            cmd = messages[1]["content"].split("): ", 1)[1].split("\n", 1)[0]
            if cmd == "sudo reboot":
                await gate.wait()                   # the first review is still out when the command is edited
            return f"SUMMARY: {cmd}\nDATA: none\nVERDICT: proceed"

    monkeypatch.setattr(engine, "_client", lambda prov, model="": Echo())
    engine.save_settings({"review_model": "Local|llama3", "auto_review": "flagged"})
    [p] = engine.queue.add("c", [{"session_id": sid, "command": "sudo reboot", "risk": "disruptive"}])
    engine._auto_review([p])
    await asyncio.sleep(0.05)
    engine.update_item(p.num, command="sudo systemctl restart nginx")
    await asyncio.sleep(0.05)
    gate.set()
    await _drain(engine)
    assert p.review["status"] == "done" and p.review["summary"] == "sudo systemctl restart nginx"


async def test_auto_review_refused_tier_shows_error(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid, sent = _setup(engine, monkeypatch)
    engine.save_settings({"review_model": "Fake|anthropic/claude-opus-5.5", "auto_review": "flagged"})
    engine.new_case("secret", "confidential")
    engine.select_model("Local", "llama3")
    added = engine.queue.add("c", [{"session_id": sid, "command": "sudo reboot", "risk": "disruptive"}])
    engine._auto_review(added)
    await _drain(engine)
    assert added[0].review["status"] == "error" and "CONFIDENTIAL" in added[0].review["error"] and not sent


async def test_manual_review_is_kept_and_preview_warns(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid, _ = _setup(engine, monkeypatch, reply="SUMMARY: Prints the key.\nDATA: the private key\nVERDICT: do not run")
    [p] = engine.queue.add("c", [{"session_id": sid, "command": "cat ~/.ssh/id_rsa", "risk": "read_only"}])
    r = await engine.review_command(p.num)
    assert r["level"] == "stop" and p.review["status"] == "done" and not p.review["auto"]
    prev = engine.preview(["some output"], [p.num])[0]
    assert "reads a private SSH key" in prev["sensitive"] and "reviewer: the private key" in prev["sensitive"]
