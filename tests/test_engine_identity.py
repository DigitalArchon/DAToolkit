"""A search or a research brief that names the case's client, or a host or login the case has a
session to, is shown to the technician first whatever the search mode, as one that names the
technician's own computer is."""

import pytest

from datoolkit.config import Host

from test_engine import env, sse, wait_turn  # noqa: F401
from test_features import multi_tool_stream
from test_research import research_call, rs, say  # noqa: F401
from test_revise_search import search, search_call, wait_for  # noqa: F401


def _case(engine, client="Acme Dental"):
    """An Open case of a client, with a session to its domain controller (a local shell standing in)."""
    engine.add_client(client)
    engine.cfg.hosts.append(Host("DC", "ssh", "dc01.acme-corp.local", user="ACME\\jsmith", auth="password"))
    engine.new_case("c", "open", client=client)
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sess = next(iter(engine.sessions.sessions.values()))
    sess.kind, sess.host_name, sess.target = "ssh", "DC", "ACME\\jsmith@dc01.acme-corp.local:22"
    return sess


async def test_the_cases_names_are_its_client_and_its_sessions_hosts_and_logins(env):  # noqa: F811
    engine, _, _ = env
    sess = _case(engine)
    assert engine._case_names() == ["Acme Dental", "ACME", "jsmith", "dc01.acme-corp.local", "dc01", "acme-corp.local"]
    assert set(engine._case_names()) < set(engine._known_names())
    # words too common to tell from a query's own name no one: a login root, a host called firewall, .lan
    sess.host_name, sess.target = "", "root@firewall.lan"
    assert engine._case_names() == ["Acme Dental", "firewall.lan"]
    sess.target = "administrator@[fd00::10]:5986"
    assert engine._case_names() == ["Acme Dental", "fd00::10"]
    sess.target = "svc-backup@corp.example@203.0.113.7:3389"              # a UPN login, a public address
    assert engine._case_names() == ["Acme Dental", "svc-backup", "corp.example", "203.0.113.7"]
    sess.kind, sess.target = "window", 'ScreenConnect window "alice@wonder.land"'     # a title is no login
    assert engine._case_names() == ["Acme Dental"]


@pytest.mark.parametrize("query, names", [
    ("dc01.acme-corp.local kerberos KRB_AP_ERR_MODIFIED", "dc01.acme-corp.local"),
    ("event 4771 dc01 pre-authentication failed", "dc01"),
    ("acme-corp.local dns delegation", "acme-corp.local"),
    ("jsmith account lockout source", "jsmith"),
    ("Acme Dental sophos licence", "Acme Dental"),
])
async def test_auto_mode_asks_first_when_a_query_names_the_cases_host_or_client(search, query, names):  # noqa: F811
    engine, fake, _, fs = search
    engine.cfg.settings.search_mode = "auto"
    _case(engine)
    fake.responses += [search_call(query), sse(({"role": "assistant", "content": "OK."}, "stop"))]
    engine.send("x")
    await wait_for(lambda: engine._search_reqs)
    assert fs.calls == []                                   # nothing leaves unasked
    rec = engine.snapshot()["search_requests"][0]
    assert rec["status"] == "awaiting" and rec["reason"].startswith("Asked first: the query names ")
    assert names in rec["reason"].split(". ")[0].split(", ") or f"names {names}" in rec["reason"]
    engine.answer_search(next(iter(engine._search_reqs)), False)
    await wait_turn(engine)
    assert fs.calls == []


async def test_a_query_of_general_words_still_runs_unasked(search):  # noqa: F811
    engine, fake, _, fs = search
    engine.cfg.settings.search_mode = "auto"
    _case(engine)
    fake.responses += [search_call("windows server 2025 kerberos event 4771 dental practice software"),
                       sse(({"role": "assistant", "content": "OK."}, "stop"))]
    engine.send("x")
    await wait_turn(engine)
    assert len(fs.calls) == 1 and engine.chat[-1]["searches"][0]["status"] == "done"


async def test_a_brief_naming_the_cases_host_is_asked_about_and_the_agent_may_then_search_for_what_it_named(rs):  # noqa: F811
    engine, fake, _, web = rs
    engine.cfg.settings.search_mode = "auto"
    _case(engine)
    fake.responses += [research_call("Windows Server 2025 on dc01: KRB_AP_ERR_MODIFIED after a rename"),
                       multi_tool_stream([("web_search", {"query": "dc01 KRB_AP_ERR_MODIFIED rename"})], text=""),
                       multi_tool_stream([("web_search", {"query": "acme-corp.local KRB_AP_ERR_MODIFIED"})], text=""),
                       say("## Answer\nReset the machine account."), say("ok")]
    engine.send("x")
    await wait_for(lambda: engine._research_reqs)
    assert web.calls == []
    assert "it names dc01" in engine.snapshot()["research_requests"][0]["reason"]
    engine.answer_research(next(iter(engine._research_reqs)), True)
    await wait_turn(engine)
    queries = [c[1]["query"] for c in web.calls if c[0] == "/api/web"]
    assert queries == ["dc01 KRB_AP_ERR_MODIFIED rename"]              # named in the brief the technician approved
    replies = [m["content"] for m in fake.requests[3]["messages"] if m["role"] == "tool"]
    assert "not searched: the query names" in replies[-1] and "acme-corp.local" in replies[-1]   # the brief never named the domain
