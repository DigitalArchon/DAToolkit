"""Attached images carry nothing but their pixels: no metadata, no embedded thumbnail, no
hidden frames or trailing bytes, and blacked-out areas stay black."""

import base64
import os
import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from datoolkit.safety import images

from test_engine import env, sse, wait_turn  # noqa: F401

SECRETS = [b"GPS-SECRET", b"THUMB-SECRET", b"XMP-SECRET", b"COMMENT-SECRET", b"PHOTOSHOP-SECRET",
           b"TRAILING-SECRET", b"PNGTEXT-SECRET", b"ICC-SECRET", b"FRAME2"]


def _segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


def jpeg_with_everything() -> bytes:
    """A real JPEG with EXIF (+ an embedded thumbnail JPEG), XMP, Photoshop IRB, a comment,
    and bytes after the end-of-image marker."""
    buf = BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, "JPEG")
    body = buf.getvalue()
    thumb = BytesIO()
    Image.new("RGB", (8, 8), (0, 255, 0)).save(thumb, "JPEG", comment=b"THUMB-SECRET")
    exif = b"Exif\x00\x00" + b"GPS-SECRET " + thumb.getvalue()
    extra = (_segment(0xE1, exif) + _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00XMP-SECRET")
             + _segment(0xED, b"Photoshop 3.0\x00PHOTOSHOP-SECRET") + _segment(0xFE, b"COMMENT-SECRET"))
    return body[:2] + extra + body[2:] + b"TRAILING-SECRET"


def png_chunks(data: bytes) -> list[str]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    out, i = [], 8
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        out.append(data[i + 4:i + 8].decode())
        i += 12 + n
    return out


def jpeg_markers(data: bytes) -> list[int]:
    """Marker bytes of the segments before the image data."""
    out, i = [], 2
    while i < len(data):
        assert data[i] == 0xFF
        m = data[i + 1]
        out.append(m)
        if m == 0xDA:          # start of scan: image data follows
            break
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return out


def test_jpeg_metadata_thumbnail_and_trailing_bytes_are_gone():
    dirty = jpeg_with_everything()
    for s in (b"GPS-SECRET", b"THUMB-SECRET", b"XMP-SECRET", b"COMMENT-SECRET", b"PHOTOSHOP-SECRET", b"TRAILING-SECRET"):
        assert s in dirty
    clean, ext = images.clean(dirty)
    assert ext == "jpg"
    for s in SECRETS:
        assert s not in clean
    assert clean.count(b"\xff\xd8") == 1                        # exactly one image: no embedded thumbnail
    assert clean.endswith(b"\xff\xd9")                          # nothing after end-of-image
    markers = jpeg_markers(clean)
    assert not [m for m in markers if 0xE1 <= m <= 0xEF or m == 0xFE]   # no APP1-15, no comments
    with Image.open(BytesIO(clean)) as im:
        assert "exif" not in im.info and "comment" not in im.info and not im.getexif()
        assert im.size == (64, 48)


def test_png_text_icc_exif_and_trailing_bytes_are_gone():
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "PNGTEXT-SECRET")
    info.add_itxt("Comment", "PNGTEXT-SECRET", zip=True)
    info.add_text("Software", "PNGTEXT-SECRET", zip=True)
    exif = Image.Exif()
    exif[0x010F] = "GPS-SECRET"
    buf = BytesIO()
    Image.new("RGBA", (40, 30), (10, 20, 30, 128)).save(buf, "PNG", pnginfo=info, exif=exif,
                                                          icc_profile=b"ICC-SECRET" * 20)
    dirty = buf.getvalue() + b"TRAILING-SECRET"
    assert {"tEXt", "iTXt", "zTXt", "eXIf", "iCCP"} <= set(png_chunks(dirty[:-15]))
    clean, ext = images.clean(dirty)
    assert ext == "png"
    assert set(png_chunks(clean)) == {"IHDR", "IDAT", "IEND"}
    pixels = zlib.decompress(b"".join(_idat(clean)))
    for s in SECRETS:
        assert s not in clean and s not in pixels
    with Image.open(BytesIO(clean)) as im:
        assert im.mode == "RGBA" and im.getpixel((0, 0)) == (10, 20, 30, 128)   # transparency kept


def _idat(data: bytes) -> list[bytes]:
    out, i = [], 8
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        if data[i + 4:i + 8] == b"IDAT":
            out.append(data[i + 8:i + 8 + n])
        i += 12 + n
    return out


def test_animated_png_keeps_only_the_first_frame():
    frames = [Image.new("RGB", (20, 20), (0, 0, 0)), Image.new("RGB", (20, 20), (255, 255, 255))]
    buf = BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:])
    assert "acTL" in png_chunks(buf.getvalue())
    clean, _ = images.clean(buf.getvalue())
    assert set(png_chunks(clean)) == {"IHDR", "IDAT", "IEND"}
    with Image.open(BytesIO(clean)) as im:
        assert im.getpixel((10, 10)) == (0, 0, 0) and getattr(im, "n_frames", 1) == 1


def test_blacked_out_pixels_are_black_after_cleaning():
    """The editor paints black into the bitmap; cleaning must keep exactly that, losslessly."""
    noise = Image.frombytes("RGB", (120, 80), os.urandom(120 * 80 * 3))
    for x in range(30, 70):                           # the redacted box, as the editor makes it
        for y in range(20, 50):
            noise.putpixel((x, y), (0, 0, 0))
    buf = BytesIO()
    noise.save(buf, "PNG")
    clean, _ = images.clean(buf.getvalue())
    with Image.open(BytesIO(clean)) as im:
        assert all(im.getpixel((x, y)) == (0, 0, 0) for x in range(30, 70) for y in range(20, 50))
        assert im.tobytes() == noise.tobytes()        # nothing else changed (lossless)


def test_other_formats_and_oversize_are_refused():
    for fmt in ("GIF", "WEBP", "BMP"):
        buf = BytesIO()
        Image.new("RGB", (4, 4)).save(buf, fmt)
        with pytest.raises(ValueError, match="unsupported"):
            images.clean(buf.getvalue())
    with pytest.raises(ValueError):
        images.clean(b"not an image")
    buf = BytesIO()
    Image.new("L", (12000, 1)).save(buf, "PNG")
    with pytest.raises(ValueError, match="over"):
        images.clean(buf.getvalue())


async def test_only_the_cleaned_image_is_stored_and_sent(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("img", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    dirty = jpeg_with_everything()
    fake.responses.append(sse(({"role": "assistant", "content": "I see a red square."}, "stop")))
    engine.send("what is this", images=["data:image/jpeg;base64," + base64.b64encode(dirty).decode()])
    await wait_turn(engine)

    sent = fake.requests[0]["messages"][-1]["content"][1]["image_url"]["url"]
    assert sent.startswith("data:image/jpeg;base64,")
    sent_bytes = base64.b64decode(sent.split(",", 1)[1])
    stored = (engine.case.dir / "img-1.jpg").read_bytes()
    assert sent_bytes == stored                       # the model gets exactly the stored, cleaned file
    for s in SECRETS:
        assert s not in sent_bytes
    assert engine.chat[0]["images"] == ["img-1.jpg"]  # generic name
    log = (engine.case.dir / "events.jsonl").read_text()
    assert "stripped" in log and "SECRET" not in log
    with pytest.raises(Exception, match="PNG or JPEG"):
        engine.send("x", images=["data:image/webp;base64,AAAA"])
