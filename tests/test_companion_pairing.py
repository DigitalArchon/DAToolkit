"""Pairing a phone (companion.py): a code spent once, the desktop's answer, a token of its own
given once and kept only as its hash, each phone disconnected by itself. SealedLore's tests of
the same pairing (tests/test_phone_pairing.py), and the companion's use of it."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from datoolkit import companion
from datoolkit.companion import CompanionServer, Pairing, PairingRefused, device_for, token_hash


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def refused(call) -> int:
    with pytest.raises(PairingRefused) as caught:
        call()
    return caught.value.status


def test_a_phone_is_paired_and_given_its_token_once():
    p = Pairing(Clock())
    code = p.open()
    assert len(code) == 26 and code == code.lower()
    claim = p.claim(code, "Jason's phone")
    assert claim.name == "Jason's phone"
    assert len(claim.check) == 4 and claim.check.isdigit()
    assert p.poll(claim.id) == ("waiting", "")
    assert [c.id for c in p.waiting()] == [claim.id]
    token = p.answer(claim.id, allow=True)
    assert token
    assert p.poll(claim.id) == ("approved", token)
    assert p.poll(claim.id) == ("expired", "")  # told once, then forgotten


def test_a_code_is_spent_by_its_first_claim_whatever_the_answer():
    p = Pairing(Clock())
    code = p.open()
    claim = p.claim(code, "first")
    assert refused(lambda: p.claim(code, "second")) == 410
    assert p.answer(claim.id, allow=False) is None
    assert p.poll(claim.id) == ("refused", "")


def test_wrong_codes_spend_the_code_after_a_few():
    p = Pairing(Clock())
    code = p.open()
    for _ in range(companion.CODE_FAILURES):
        assert refused(lambda: p.claim("wrong", "x")) == 403
    assert refused(lambda: p.claim(code, "x")) == 410
    p.open()
    assert refused(lambda: p.claim("ünïcode", "x")) == 403        # whatever is sent, a refusal, not a crash


def test_claims_are_limited_per_minute():
    clock = Clock()
    p = Pairing(clock)
    for _ in range(companion.CLAIMS_PER_MINUTE):
        p.open()
        refused(lambda: p.claim("wrong", "x"))
    code = p.open()
    assert refused(lambda: p.claim(code, "x")) == 429
    clock.now += 61
    assert p.claim(code, "x").state == "waiting"


def test_a_code_and_a_claim_expire():
    clock = Clock()
    p = Pairing(clock)
    code = p.open()
    clock.now += companion.CODE_SECONDS + 1
    assert p.code is None
    assert refused(lambda: p.claim(code, "x")) == 410
    claim = p.claim(p.open(), "x")
    clock.now += companion.CLAIM_SECONDS + 1
    assert p.answer(claim.id, allow=True) is None
    assert p.poll(claim.id) == ("expired", "")


def test_closing_spends_the_code_and_expires_what_waits():
    p = Pairing(Clock())
    claim = p.claim(p.open(), "x")
    p.open()
    p.close()
    assert p.code is None
    assert p.poll(claim.id) == ("expired", "")


def test_a_name_is_one_short_printable_line():
    assert companion.clean_name("  Pixel\n8\x00 ") == "Pixel 8"
    assert companion.clean_name("") == "A phone"
    assert companion.clean_name(None) == "A phone"
    assert len(companion.clean_name("x" * 500)) == companion.NAME_LENGTH


def test_a_token_is_found_by_its_hash_alone():
    token = companion.new_token()
    devices = [("one", token_hash("other")), ("two", token_hash(token))]
    assert device_for(token, devices) == "two"
    assert device_for("nope", devices) is None
    assert device_for("", devices) is None
    assert token not in str(devices)


async def test_each_phone_has_its_own_token_and_is_disconnected_by_itself(tmp_path):
    """Every phone shared one token, from the second QR code, with no say of the desktop's: anyone
    who saw the code (a photo, a shoulder) was paired, and one phone couldn't be told from another
    or cut off alone."""
    srv = CompanionServer(FastAPI(), tmp_path)
    one, two = srv.add_device("Pixel"), srv.add_device("iPhone")
    assert one != two and srv.check(one) and srv.check(two) and not srv.check("") and not srv.check(None)
    assert one not in str(srv.devices) and two not in str(srv.devices)              # their hashes only
    first = srv.device_of(one)
    await srv.revoke(first)
    assert not srv.check(one) and srv.check(two)
    # the desktop's answer is what pairs: refused, or gone, nothing is kept
    srv.pairing = Pairing(Clock())
    claim = srv.pairing.claim(srv.open_code(), "Tablet")
    assert srv.answer(claim.id, allow=False) is None and len(srv.devices) == 1
    claim = srv.pairing.claim(srv.open_code(), "Tablet")
    device = srv.answer(claim.id, allow=True)
    state, token = srv.pairing.poll(claim.id)
    assert (device.name, state) == ("Tablet", "approved") and srv.device_of(token) == device.id
    assert srv.answer(claim.id, allow=True) is None                                   # answered once
