"""Framing, handshake and encrypted channel for Orbit v1 (spec/PROTOCOL.md sections 2-4).

Transport-agnostic: anything offering asyncio ``StreamReader.readexactly`` and
``StreamWriter.write/drain/close`` semantics works (TCP, or BLE chunks fed into a StreamReader).
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag

from .crypto import CipherState, KeyPair, b64d, b64e, device_id, load_public, sas_code, session_keys, transcript_hash

VERSION = 1
MAX_FRAME = 1 << 20


class ProtocolError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class Reader(Protocol):
    async def readexactly(self, n: int) -> bytes: ...


class Writer(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


def encode(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode(body: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError("bad_message", str(e)) from e
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("bad_message", "message must be an object with a string 't'")
    return msg


class FrameStream:
    def __init__(self, reader: Reader, writer: Writer) -> None:
        self.reader = reader
        self.writer = writer

    async def read_frame(self) -> bytes:
        (length,) = struct.unpack(">I", await self.reader.readexactly(4))
        if length == 0 or length > MAX_FRAME:
            raise ProtocolError("bad_frame", f"frame length {length}")
        return await self.reader.readexactly(length)

    async def write_frame(self, body: bytes) -> None:
        if not 0 < len(body) <= MAX_FRAME:
            raise ProtocolError("bad_frame", f"frame length {len(body)}")
        self.writer.write(struct.pack(">I", len(body)) + body)
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()


@dataclass(frozen=True)
class Peer:
    id: str
    name: str
    platform: str
    static: bytes


class SecureChannel:
    """Encrypted, authenticated message channel produced by a completed handshake."""

    def __init__(self, stream: FrameStream, peer: Peer, sas: str, send_key: bytes, recv_key: bytes) -> None:
        self.stream = stream
        self.peer = peer
        self.sas = sas
        self._tx = CipherState(send_key)
        self._rx = CipherState(recv_key)

    async def send(self, msg: dict[str, Any]) -> None:
        await self.stream.write_frame(self._tx.encrypt(encode(msg)))

    async def recv(self) -> dict[str, Any]:
        record = await self.stream.read_frame()
        try:
            plaintext = self._rx.decrypt(record)
        except InvalidTag as e:
            raise ProtocolError("decrypt_failed", "record failed authentication") from e
        return decode(plaintext)

    def close(self) -> None:
        self.stream.close()


def _hello(role: str, identity: KeyPair, eph: KeyPair, nonce: bytes, name: str, platform: str) -> bytes:
    return encode(
        {
            "t": "hello",
            "v": VERSION,
            "role": role,
            "id": device_id(identity.public),
            "name": name,
            "platform": platform,
            "static": b64e(identity.public),
            "eph": b64e(eph.public),
            "nonce": b64e(nonce),
        }
    )


def _parse_hello(body: bytes, expected_role: str) -> tuple[Peer, bytes]:
    msg = decode(body)
    if msg["t"] == "error":
        raise ProtocolError(str(msg.get("code", "error")), str(msg.get("message", "")))
    if msg["t"] != "hello" or msg.get("v") != VERSION or msg.get("role") != expected_role:
        raise ProtocolError("bad_hello", f"expected {expected_role} hello v{VERSION}")
    try:
        static = b64d(msg["static"])
        eph = b64d(msg["eph"])
        nonce = b64d(msg["nonce"])
        load_public(static)
        load_public(eph)
        name = str(msg["name"])
        platform = str(msg["platform"])
        peer_id = str(msg["id"])
    except (KeyError, ValueError, TypeError) as e:
        raise ProtocolError("bad_hello", str(e)) from e
    if len(nonce) != 16:
        raise ProtocolError("bad_hello", "nonce must be 16 bytes")
    if peer_id != device_id(static):
        raise ProtocolError("bad_hello", "id does not match static key")
    return Peer(peer_id, name, platform, static), eph


async def initiate(
    stream: FrameStream,
    identity: KeyPair,
    name: str,
    platform: str,
    *,
    eph: KeyPair | None = None,
    nonce: bytes | None = None,
) -> SecureChannel:
    eph = eph or KeyPair.generate()
    hello_i = _hello("initiator", identity, eph, nonce or os.urandom(16), name, platform)
    commit = encode({"t": "commit", "v": VERSION, "c": b64e(hashlib.sha256(hello_i).digest())})
    await stream.write_frame(commit)
    hello_r = await stream.read_frame()
    peer, peer_eph = _parse_hello(hello_r, "responder")
    await stream.write_frame(hello_i)
    th = transcript_hash(commit, hello_r, hello_i)
    k_i2r, k_r2i = session_keys(th, eph.dh(peer_eph), identity.dh(peer.static))
    return SecureChannel(stream, peer, sas_code(th), send_key=k_i2r, recv_key=k_r2i)


async def respond(
    stream: FrameStream,
    identity: KeyPair,
    name: str,
    platform: str,
    *,
    eph: KeyPair | None = None,
    nonce: bytes | None = None,
) -> SecureChannel:
    eph = eph or KeyPair.generate()
    commit = await stream.read_frame()
    msg = decode(commit)
    if msg["t"] != "commit":
        raise ProtocolError("bad_commit", "expected commit")
    if msg.get("v") != VERSION:
        await stream.write_frame(
            encode({"t": "error", "code": "unsupported_version", "message": "", "supported": [VERSION]})
        )
        raise ProtocolError("unsupported_version", f"peer offered v{msg.get('v')}")
    try:
        commitment = b64d(msg["c"])
    except (KeyError, ValueError, TypeError) as e:
        raise ProtocolError("bad_commit", str(e)) from e
    hello_r = _hello("responder", identity, eph, nonce or os.urandom(16), name, platform)
    await stream.write_frame(hello_r)
    hello_i = await stream.read_frame()
    if hashlib.sha256(hello_i).digest() != commitment:
        raise ProtocolError("bad_commit", "hello does not match commitment")
    peer, peer_eph = _parse_hello(hello_i, "initiator")
    th = transcript_hash(commit, hello_r, hello_i)
    k_i2r, k_r2i = session_keys(th, eph.dh(peer_eph), identity.dh(peer.static))
    return SecureChannel(stream, peer, sas_code(th), send_key=k_r2i, recv_key=k_i2r)
