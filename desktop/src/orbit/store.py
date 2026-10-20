"""Persistent identity key and pinned peers (spec/PROTOCOL.md 3.1 and 3.4)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import ensure_private_dir, write_private
from .proto import KeyPair
from .proto.crypto import b64d, b64e

IDENTITY_FILE = "identity.pem"
PEERS_FILE = "peers.json"


def load_or_create_identity(directory: Path) -> KeyPair:
    path = directory / IDENTITY_FILE
    if path.exists():
        return KeyPair.from_pem(path.read_bytes())
    ensure_private_dir(directory)
    key = KeyPair.generate()
    write_private(path, key.to_pem())
    return key


@dataclass
class PinnedPeer:
    id: str
    name: str
    platform: str
    static: bytes
    # "host:port" entered manually (or via adb forward); retried on startup and after disconnects.
    address: str | None = None
    # The discovered LAN "host:port" that last worked; tried first, also when mDNS finds nothing.
    last_lan: str | None = None


class PeerStore:
    """Pinned peers in ``<dir>/peers.json`` (mode 0600)."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / PEERS_FILE
        self.peers: dict[str, PinnedPeer] = {}
        if self.path.exists():
            for pid, raw in json.loads(self.path.read_text()).items():
                self.peers[pid] = PinnedPeer(
                    id=pid,
                    name=raw.get("name", ""),
                    platform=raw.get("platform", ""),
                    static=b64d(raw["static"]),
                    address=raw.get("address"),
                    last_lan=raw.get("last_lan"),
                )

    def get(self, peer_id: str) -> PinnedPeer | None:
        return self.peers.get(peer_id)

    def is_pinned(self, peer_id: str, static: bytes) -> bool:
        peer = self.peers.get(peer_id)
        return peer is not None and peer.static == static

    def pin(self, peer: PinnedPeer) -> None:
        self.peers[peer.id] = peer
        self.save()

    def remove(self, peer_id: str) -> bool:
        if self.peers.pop(peer_id, None) is None:
            return False
        self.save()
        return True

    def save(self) -> None:
        data = {}
        for pid, p in self.peers.items():
            raw = asdict(p)
            del raw["id"]
            raw["static"] = b64e(p.static)
            data[pid] = raw
        write_private(self.path, json.dumps(data, indent=2, sort_keys=True).encode())
