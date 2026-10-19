import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import pytest

from orbit.proto import FrameStream, KeyPair, ProtocolError, device_id, initiate, respond
from orbit.proto.crypto import CipherState, b64d, sas_code, session_keys, transcript_hash

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "spec" / "test-vectors.json").read_text())


@contextlib.asynccontextmanager
async def tcp_pair(server_cb):
    """Run server_cb(FrameStream) on a real localhost TCP socket; yield the client FrameStream."""
    accepted = []

    def on_connect(r, w):
        accepted.append(w)
        asyncio.ensure_future(server_cb(FrameStream(r, w)))

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    try:
        yield FrameStream(reader, writer)
    finally:
        writer.close()
        for w in accepted:
            w.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)


async def test_handshake_roundtrip_and_messages():
    phone, laptop = KeyPair.generate(), KeyPair.generate()
    result = asyncio.get_running_loop().create_future()

    async def serve(stream):
        ch = await respond(stream, phone, "Phone", "android")
        await ch.send({"t": "ready", "paired": False, "now": 1})
        result.set_result((ch.peer, ch.sas, await ch.recv()))

    async with tcp_pair(serve) as stream:
        ch = await initiate(stream, laptop, "Laptop", "macos")
        assert await ch.recv() == {"t": "ready", "paired": False, "now": 1}
        await ch.send({"t": "pair_request"})
        peer, sas, msg = await asyncio.wait_for(result, 5)
    assert msg == {"t": "pair_request"}
    assert peer.id == device_id(laptop.public) and peer.name == "Laptop"
    assert ch.peer.id == device_id(phone.public) and ch.peer.platform == "android"
    assert sas == ch.sas and len(sas) == 6


async def test_commit_mismatch_is_rejected():
    phone = KeyPair.generate()
    outcome = asyncio.get_running_loop().create_future()

    async def serve(stream):
        try:
            await respond(stream, phone, "Phone", "android")
        except ProtocolError as e:
            outcome.set_result(e.code)

    async with tcp_pair(serve) as stream:
        await stream.write_frame(b'{"t":"commit","v":1,"c":"' + b"A" * 43 + b'="}')
        await stream.read_frame()
        await stream.write_frame(json.dumps({"t": "hello"}).encode())
        assert await asyncio.wait_for(outcome, 5) == "bad_commit"


async def test_unsupported_version_gets_error():
    async with tcp_pair(lambda s: respond(s, KeyPair.generate(), "P", "android")) as stream:
        await stream.write_frame(b'{"t":"commit","v":99,"c":""}')
        reply = json.loads(await stream.read_frame())
    assert reply["code"] == "unsupported_version" and reply["supported"] == [1]


def test_vectors():
    v = VECTORS
    keys = {k: KeyPair.from_scalar(bytes.fromhex(h)) for k, h in v["private_keys_hex"].items()}
    for k, pub in v["public_keys_b64"].items():
        assert keys[k].public == b64d(pub)
    commit, hello_r, hello_i = (v[k].encode() for k in ("commit_body", "hello_responder_body", "hello_initiator_body"))
    assert json.loads(commit)["c"] == __import__("base64").b64encode(hashlib.sha256(hello_i).digest()).decode()
    th = transcript_hash(commit, hello_r, hello_i)
    assert th.hex() == v["transcript_hash_hex"]
    dh_ee, dh_ss = keys["eph_i"].dh(keys["eph_r"].public), keys["static_r"].dh(keys["static_i"].public)
    assert (dh_ee.hex(), dh_ss.hex()) == (v["dh_ee_hex"], v["dh_ss_hex"])
    k_i2r, k_r2i = session_keys(th, dh_ee, dh_ss)
    assert (k_i2r.hex(), k_r2i.hex(), sas_code(th)) == (v["k_i2r_hex"], v["k_r2i_hex"], v["sas"])
    for key, recs in ((k_i2r, v["records_i2r"]), (k_r2i, v["records_r2i"])):
        cs = CipherState(key)
        for rec in recs:
            assert cs.decrypt(bytes.fromhex(rec["record_hex"])).decode() == rec["plaintext"]


def test_tampered_record_fails():
    rec = bytearray(bytes.fromhex(VECTORS["records_i2r"][0]["record_hex"]))
    rec[0] ^= 1
    with pytest.raises(Exception):
        CipherState(bytes.fromhex(VECTORS["k_i2r_hex"])).decrypt(bytes(rec))


async def test_oversize_record_is_refused_without_desyncing_the_channel():
    phone, laptop = KeyPair.generate(), KeyPair.generate()
    received = asyncio.get_running_loop().create_future()

    async def serve(stream):
        ch = await respond(stream, phone, "Phone", "android")
        received.set_result(await ch.recv())

    async with tcp_pair(serve) as stream:
        ch = await initiate(stream, laptop, "Laptop", "macos")
        with pytest.raises(ProtocolError) as e:
            await ch.send({"t": "action", "value": "x" * (1 << 20)})
        assert e.value.code == "bad_frame"
        await ch.send({"t": "ping", "req": 1})
        assert await asyncio.wait_for(received, 5) == {"t": "ping", "req": 1}
