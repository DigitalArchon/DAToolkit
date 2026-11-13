"""What says who or where a computer is, in a search query: the names a case gives (its client, each
session's host, domain and user) in the forms they may be written, addresses in every notation, and
not the common words a login or a host name can be."""

import time

import pytest

from datoolkit.safety import identity

# a case's: the client, a session's host, its domain, its user, as they are stored
CASE = ["Acme Dental", "dc01.acme-corp.local", "acme-corp.local", "ACME\\jsmith", "ACME-WS01", "bob@acme-corp.local"]


@pytest.mark.parametrize("query,named", [
    # a host by its short name when its full one is known, and the reverse
    ("dc01 replication error 8453", "dc01"), ("DC01.ACME-CORP.LOCAL event 4625", "dc01.acme-corp.local"),
    ("https://vpn.acme-corp.local/remote/login", "acme-corp.local"), ("acme-corp SPF record", "acme-corp"),
    # with _ - . or a space between a name's parts, or none
    ("error on ACME_WS01", "ACME-WS01"), ("acme ws01 blue screen", "ACME-WS01"), ("acmews01", "ACME-WS01"),
    ("acme dental backup failed", "Acme Dental"), ("acme-dental.com mx record", "Acme Dental"), ("acme_dental", "Acme Dental"),
    # an account by its user and by its domain, in either notation
    ("ACME\\jsmith locked out", "ACME\\jsmith"), ("jsmith profile corrupt", "jsmith"), ("jsmith@acme mailbox", "jsmith"),
    ("C:\\Users\\jsmith\\AppData", "jsmith"), ("backup_jsmith.zip", "jsmith"), ("bob mailbox full", "bob"),
    ("HKLM\\Software\\Acme", "ACME"),
])
def test_a_name_is_found_in_the_forms_it_may_be_written(query, named):
    assert named in identity.check(query, CASE)


def test_the_short_name_and_the_full_one_find_each_other():
    assert identity.check("error on mintdev", ["mintdev.acme.local"]) == ["mintdev"]
    assert identity.check("error on mintdev.acme.local", ["mintdev"]) == ["mintdev"]
    assert identity.check("mail.acme.local bounce", ["mail.acme.local"]) == ["mail.acme.local"]
    assert identity.check("mail server bounce", ["mail.acme.local"]) == []            # "mail" alone names no one


@pytest.mark.parametrize("query,found", [
    ("\\\\acme-fs01\\share access denied", ["\\\\acme-fs01"]), ("net use \\\\fileserver01\\data$ error 5", ["\\\\fileserver01"]),
    ("fe80::1 unreachable", ["fe80::1"]), ("ping fe80::1%eth0", ["fe80::1"]), ("fd12:3456:789a::1 no route", ["fd12:3456:789a::1"]),
    ("https://[fd00::1]:443/login", ["fd00::1"]), ("::1 refused", ["::1"]), ("2001:db8::1", ["2001:db8::1"]),
    ("arp 001A.2B3C.4D5E incomplete", ["001A.2B3C.4D5E"]), ("mac 001a2b-3c4d5e flapping", ["001a2b-3c4d5e"]),
    ("00:1A:2B:3C:4D:5E", ["00:1A:2B:3C:4D:5E"]), ("010.000.000.001 timeout", ["010.000.000.001"]),
    ("192.168.001.010", ["192.168.001.010"]), ("10.0.0.1:8080", ["10.0.0.1"]),
])
def test_addresses_and_servers_are_found_in_every_notation(query, found):
    assert identity.check(query, []) == found


@pytest.mark.parametrize("query", [
    "2a02:8071:1234::1 traceroute", "8.8.8.8 dns", "008.008.008.008", "std::vector<int> error", "a :: b", "12:30:45 clock skew",
    "\\\\server\\share error 53", "\\\\.\\pipe\\docker_engine", "\\\\?\\C:\\long\\path", "\\\\wsl$\\Ubuntu\\home", "C:\\Windows\\System32",
    "part 123456-234567", "1234.5678.9012", "version 10.0.19045.3803", "OPNsense 25.1 OpenVPN 1.1.1.1 dns", "bobcat driver", "jsmiths",
])
def test_what_names_no_one_goes_through(query):
    assert identity.check(query, CASE) == []


@pytest.mark.parametrize("common", ["support", "tech", "office", "service", "admin", "user", "Digital", "server", "windows", "backup",
                                    "Tech Support", "office-pc", "/home/user", "101", "ab", "", "   "])
def test_a_common_word_is_no_name(common):
    # a login "support" or a real name's "Digital" made every search with the word wait for a yes
    query = "windows server 2022 backup service support lifecycle error 101 digital signature office tech preview admin user ab /home/user"
    assert identity.check(query, [common]) == []
    assert identity.check(query, [common, "ACME-WS01"]) == [] and identity.check(query + " acme-ws01", [common, "ACME-WS01"]) == ["ACME-WS01"]


def test_this_computers_own_common_names_are_passed_over(monkeypatch):
    import pwd
    import socket

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: pwd.struct_passwd(("support", "x", 1000, 1000, "Tech Support,,,", "/home/support", "/bin/sh")))
    monkeypatch.setattr(socket, "gethostname", lambda: "office-pc")
    assert identity.names() == [] and identity.check("office pc support for a tech at home") == []
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: pwd.struct_passwd(("jsmith", "x", 1000, 1000, "John Smith,,,", "/home/jsmith", "/bin/sh")))
    monkeypatch.setattr(socket, "gethostname", lambda: "acme-ws01.acme-corp.local")
    assert identity.names() == ["jsmith", "/home/jsmith", "John", "Smith", "acme-ws01.acme-corp.local"]
    assert identity.check("acme ws01 will not boot") == ["acme-ws01"] and identity.check("/home/jsmith/.config") == ["jsmith", "/home/jsmith"]


def test_a_cases_names_are_gathered_whatever_holds_them(monkeypatch):
    monkeypatch.setattr(identity, "names", lambda: ["mintdev"])
    # as a session stores them: with a port, a scheme, brackets; twice; empty; not text at all
    names = identity.known_names("Acme Dental", "ssh://dc01.acme-corp.local:22", "dc01.acme-corp.local", None, "", "  ", 5,
                                 "ACME\\jsmith", "[fd00::5]:5986", "203.0.113.7:3389", "ssh://dc01.acme-corp.local:22", "ab")
    assert names == ["mintdev", "Acme Dental", "ssh://dc01.acme-corp.local:22", "dc01.acme-corp.local", "ACME\\jsmith", "[fd00::5]:5986",
                     "203.0.113.7:3389", "ab"]
    assert identity.known_names("x", own=False) == ["x"] and identity.known_names() == ["mintdev"]
    assert identity.check("dc01 kerberos error on mintdev", names) == ["mintdev", "dc01"]
    assert identity.check("rdp to 203.0.113.7 fails", names) == ["203.0.113.7"]          # the session's public address
    assert identity.check("winrm fd00::5 refused", names) == ["fd00::5"]
    assert identity.check("kerberos error 0x7 ab", names) == []
    assert identity.check("anything", [None, "", 5]) == []                                # a list built without the helper


@pytest.mark.parametrize("query", ["0" * 20000, "a" * 20000, ":" * 20000, "a@" * 10000, "a.b@" * 5000, "\\" * 20000, "0:" * 10000,
                                   "a-" * 10000, "1.2.3." * 4000, "ab12." * 4000, "acme " * 4000, "dc01." * 4000])
def test_a_long_query_is_read_at_once(query):
    # a research agent's own query has no length limit: 20,000 digits took a second and a half
    started = time.perf_counter()
    identity.check(query, CASE)
    assert time.perf_counter() - started < 0.5
