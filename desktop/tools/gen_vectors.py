"""Generate spec/test-vectors.json: a fully deterministic handshake + records for cross-implementation tests."""

import hashlib
import json
import sys
from pathlib import Path

from orbit.proto.crypto import CipherState, KeyPair, b64e, device_id, sas_code, session_keys, transcript_hash
from orbit.proto.handshake import _hello, encode


def scalar(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def main() -> None:
    keys = {name: KeyPair.from_scalar(scalar(f"orbit test {name}")) for name in ("static_i", "eph_i", "static_r", "eph_r")}
    nonce_i, nonce_r = bytes(range(16)), bytes(range(16, 32))
    hello_i = _hello("initiator", keys["static_i"], keys["eph_i"], nonce_i, "Test Laptop", "macos")
    hello_r = _hello("responder", keys["static_r"], keys["eph_r"], nonce_r, "Test Phone", "android")
    commit = encode({"t": "commit", "v": 1, "c": b64e(hashlib.sha256(hello_i).digest())})
    th = transcript_hash(commit, hello_r, hello_i)
    dh_ee = keys["eph_i"].dh(keys["eph_r"].public)
    dh_ss = keys["static_i"].dh(keys["static_r"].public)
    assert dh_ee == keys["eph_r"].dh(keys["eph_i"].public)
    k_i2r, k_r2i = session_keys(th, dh_ee, dh_ss)

    def records(key: bytes, plaintexts: list[bytes]) -> list[dict]:
        cs = CipherState(key)
        return [{"counter": i, "plaintext": p.decode(), "record_hex": cs.encrypt(p).hex()} for i, p in enumerate(plaintexts)]

    vectors = {
        "description": "Orbit v1 deterministic handshake. Private keys are big-endian P-256 scalars. "
        "Bodies are exact frame bodies (UTF-8). Implementations must reproduce every derived value.",
        "private_keys_hex": {k: scalar(f"orbit test {k}").hex() for k in keys},
        "public_keys_b64": {k: b64e(v.public) for k, v in keys.items()},
        "device_id_initiator": device_id(keys["static_i"].public),
        "device_id_responder": device_id(keys["static_r"].public),
        "nonce_initiator_hex": nonce_i.hex(),
        "nonce_responder_hex": nonce_r.hex(),
        "commit_body": commit.decode(),
        "hello_responder_body": hello_r.decode(),
        "hello_initiator_body": hello_i.decode(),
        "transcript_hash_hex": th.hex(),
        "dh_ee_hex": dh_ee.hex(),
        "dh_ss_hex": dh_ss.hex(),
        "k_i2r_hex": k_i2r.hex(),
        "k_r2i_hex": k_r2i.hex(),
        "sas": sas_code(th),
        "records_i2r": records(k_i2r, [b'{"t":"ready","paired":false,"now":1760000000000}', b'{"t":"pair_request"}']),
        "records_r2i": records(k_r2i, [b'{"t":"ready","paired":false,"now":1760000000001}', b'{"t":"pair_decision","accepted":true}']),
    }
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "spec" / "test-vectors.json"
    out.write_text(json.dumps(vectors, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
