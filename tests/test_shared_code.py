"""The code every DA app shares is one copy, byte for byte (the cross-app standard, Nov 2026). DA
Toolkit's llm/ holds the canonical tee.py, private_mode.py, dcap.py, nras.py and ethsig.py (DA Vibe
Manager's and DA Private Coder's tests hold theirs to these); sdk_guard.py's canonical copy is
SealedLore's providers/sdk_guard.py, and this one may differ from it only in its imports and its
docstrings' paths. A drift is a failing test here, not a finding of the next review: a change is made
in the canonical copy first, then copied. Skipped where SealedLore's repository isn't beside this one."""

import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "src" / "datoolkit" / "llm"
SEALEDLORE = Path(__file__).resolve().parents[1].parent / "SealedLore" / "src" / "sealedlore" / "providers"

_NAMES = re.compile(r"datoolkit|sealedlore")
# the docstrings' paths: llm/tee.py here, providers/tee.py there
_PATHS = re.compile(r"\b(llm|providers)/")
# a top-level import statement (never a docstring's line that begins "from SealedLore (its …")
_IMPORT = re.compile(r"(import [\w.]+|from [\w.]+ import )")


def normalised(text: str) -> list[str]:
    """`text` without its top-level import statements (a parenthesised one with its continuation lines),
    each app's package the same word, and the docstrings' paths the same."""
    out, in_import = [], False
    for line in text.splitlines():
        if in_import:
            in_import = ")" not in line
            continue
        if _IMPORT.match(line):
            in_import = "(" in line and ")" not in line
            continue
        out.append(_PATHS.sub("DIR/", _NAMES.sub("APP", line)))
    return out


@pytest.mark.skipif(not SEALEDLORE.is_dir(), reason="SealedLore's repository isn't beside this one")
def test_sdk_guard_is_sealedlores_copy():
    ours, theirs = normalised((HERE / "sdk_guard.py").read_text()), normalised((SEALEDLORE / "sdk_guard.py").read_text())
    if ours != theirs:
        first = next((i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b), min(len(ours), len(theirs)))
        pytest.fail(f"sdk_guard.py differs from SealedLore's at line {first + 1} (normalised): "
                    f"{ours[first:first + 1]!r} here, {theirs[first:first + 1]!r} there. Change SealedLore's copy "
                    "first, then copy it here.")


def test_imports_are_dropped_whole_and_names_and_paths_mapped():
    text = ("import a\nfrom .x import (b,\n    c)\nfrom sealedlore.providers.y import d\n"
            "x = 'datoolkit llm/tee.py providers/tee.py'\nfrom SealedLore (its copy, kept)\n    import json\n")
    assert normalised(text) == ["x = 'APP DIR/tee.py DIR/tee.py'", "from SealedLore (its copy, kept)", "    import json"]
