"""Orbit v1 cryptographic primitives (spec/PROTOCOL.md sections 3 and 4)."""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TRANSCRIPT_LABEL = b"orbit/1 transcript"
KEYS_INFO = b"orbit/1 session keys"
SAS_LABEL = b"orbit/1 sas"
TAG_LEN = 16
MAX_COUNTER = 2**64 - 1


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def load_public(data: bytes) -> ec.EllipticCurvePublicKey:
    if len(data) != 65 or data[0] != 4:
        raise ValueError("public key must be a 65-byte uncompressed P-256 point")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), data)


def device_id(static_pub: bytes) -> str:
    return hashlib.sha256(static_pub).digest()[:16].hex()


@dataclass(frozen=True)
class KeyPair:
    private: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> KeyPair:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_scalar(cls, scalar: bytes) -> KeyPair:
        return cls(ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: bytes) -> KeyPair:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("identity key must be a P-256 EC key")
        return cls(key)

    def to_pem(self) -> bytes:
        return self.private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )

    @property
    def public(self) -> bytes:
        return public_bytes(self.private.public_key())

    def dh(self, peer_public: bytes) -> bytes:
        return self.private.exchange(ec.ECDH(), load_public(peer_public))


def _lp(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def transcript_hash(commit_body: bytes, hello_r_body: bytes, hello_i_body: bytes) -> bytes:
    return hashlib.sha256(TRANSCRIPT_LABEL + _lp(commit_body) + _lp(hello_r_body) + _lp(hello_i_body)).digest()


def session_keys(th: bytes, dh_ee: bytes, dh_ss: bytes) -> tuple[bytes, bytes]:
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=th, info=KEYS_INFO).derive(dh_ee + dh_ss)
    return okm[:32], okm[32:]


def sas_code(th: bytes) -> str:
    value = struct.unpack(">I", hashlib.sha256(SAS_LABEL + th).digest()[:4])[0]
    return f"{value % 1_000_000:06d}"


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack(">Q", counter)


class CipherState:
    """One direction of an encrypted session: AES-256-GCM with a counter nonce."""

    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)
        self.counter = 0

    def _next(self) -> bytes:
        if self.counter >= MAX_COUNTER:
            raise ValueError("nonce counter exhausted")
        nonce = _nonce(self.counter)
        self.counter += 1
        return nonce

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._aead.encrypt(self._next(), plaintext, None)

    def decrypt(self, record: bytes) -> bytes:
        # Raises cryptography.exceptions.InvalidTag on tampering or key mismatch.
        return self._aead.decrypt(self._next(), record, None)
