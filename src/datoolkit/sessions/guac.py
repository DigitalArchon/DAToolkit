"""Guacamole protocol: the handshake with guacd and the relay between guacd and the browser.

guacd (Apache Guacamole's proxy daemon, `apt install guacd`) makes the RDP connection with
FreeRDP and streams the desktop as Guacamole instructions. The server does the handshake
itself so connection parameters, including the password, never reach the browser; after
`ready` it relays instructions both ways. The browser side is guacamole-common-js's
WebSocketTunnel, which expects the tunnel UUID as its first instruction and has its internal
pings echoed back rather than forwarded.

Wire format: each element is LENGTH.VALUE, where LENGTH counts Unicode code points;
elements are separated by commas and the instruction ends with a semicolon."""

from __future__ import annotations

import asyncio
import codecs
import uuid as uuid_mod

INTERNAL = ""          # opcode of tunnel-internal instructions (uuid, ping)
HANDSHAKE_TIMEOUT = 20


class GuacError(Exception):
    pass


def encode(*elements) -> str:
    return ",".join(f"{len(s)}.{s}" for s in (str(e) for e in elements)) + ";"


class Parser:
    """Incremental parser: feed text, get complete instructions as lists of strings."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[list[str]]:
        self._buf += text
        buf, out, start = self._buf, [], 0
        while True:
            elems, i, complete = [], start, False
            while True:
                dot = buf.find(".", i)
                if dot == -1:
                    break                          # length not complete yet
                try:
                    n = int(buf[i:dot])
                except ValueError as e:
                    raise GuacError(f"bad element length {buf[i:dot][:20]!r}") from e
                end = dot + 1 + n
                if end >= len(buf):
                    break                          # value or terminator not here yet
                elems.append(buf[dot + 1:end])
                i = end + 1
                if buf[end] == ";":
                    complete = True
                    break
                if buf[end] != ",":
                    raise GuacError(f"bad element terminator {buf[end]!r}")
            if not complete:
                break
            out.append(elems)
            start = i
        self._buf = buf[start:]
        return out


async def _read_instruction(reader: asyncio.StreamReader, parser: Parser, decoder, pending: list) -> list[str]:
    while not pending:
        data = await reader.read(65536)
        if not data:
            raise GuacError("guacd closed the connection during the handshake")
        pending.extend(parser.feed(decoder.decode(data)))
    return pending.pop(0)


async def handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, protocol: str,
                    params: dict[str, str], width: int, height: int, dpi: int = 96,
                    timezone: str = "") -> tuple[str, list[str], str, object]:
    """Select the protocol, answer `args` from `params`, and wait for `ready`. Returns
    (connection id, the parameter names guacd asked for, text that arrived after `ready` and
    must be passed on, the UTF-8 decoder holding any split character)."""
    parser, decoder, pending = Parser(), codecs.getincrementaldecoder("utf-8")(), []

    async def expect(opcode: str) -> list[str]:
        ins = await asyncio.wait_for(_read_instruction(reader, parser, decoder, pending), HANDSHAKE_TIMEOUT)
        if ins[0] == "error":
            raise GuacError(f"guacd: {ins[1] if len(ins) > 1 else 'error'}")
        if ins[0] != opcode:
            raise GuacError(f"expected {opcode} from guacd, got {ins[0]}")
        return ins[1:]

    writer.write(encode("select", protocol).encode())
    await writer.drain()
    names = await expect("args")
    values = []
    for name in names:
        if name.startswith("VERSION_"):
            values.append(name)            # speak whatever protocol version guacd offers
        else:
            values.append(params.get(name, ""))
    writer.write((encode("size", width, height, dpi) + encode("audio") + encode("video")
                  + encode("image", "image/png", "image/jpeg", "image/webp")
                  + (encode("timezone", timezone) if timezone else "")
                  + encode("connect", *values)).encode())
    await writer.drain()
    ready = await expect("ready")
    leftover = "".join(encode(*ins) for ins in pending) + parser._buf
    return (ready[0] if ready else ""), names, leftover, decoder


def tunnel_uuid() -> str:
    return encode(INTERNAL, str(uuid_mod.uuid4()))


def is_internal(message: str) -> bool:
    return message.startswith("0.,")
