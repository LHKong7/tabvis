"""Broker IPC framing & message schemas (design §6.2).

A tiny length-prefixed JSON framing over an asyncio stream, plus the fixed request/response schemas.
The IPC schema uses fixed fields and rejects extras (design §6.2 "IPC Schema 使用固定字段，拒绝额外字
段") — that is enforced by the ``extra="forbid"`` on :class:`AuthenticationRequest` /
:class:`AuthenticationResult`, which are the wire types here.
"""

from __future__ import annotations

import asyncio
import json
import struct

_LEN = struct.Struct(">I")
_MAX_FRAME = 1 << 20  # 1 MiB — an authentication request/response is tiny; anything larger is rejected.


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one length-prefixed frame. Raises on EOF or an oversized frame."""
    header = await reader.readexactly(_LEN.size)
    (length,) = _LEN.unpack(header)
    if length > _MAX_FRAME:
        raise ValueError("frame too large")
    return await reader.readexactly(length)


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if len(payload) > _MAX_FRAME:
        raise ValueError("frame too large")
    writer.write(_LEN.pack(len(payload)))
    writer.write(payload)
    await writer.drain()


def encode(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def decode(payload: bytes) -> dict:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data
