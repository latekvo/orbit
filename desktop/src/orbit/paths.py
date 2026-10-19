"""Filesystem locations and private-file helpers shared by orbitd, its clients and the fake phone."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path


def config_dir() -> Path:
    if env := os.environ.get("ORBIT_CONFIG_DIR"):
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "orbit"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "orbit"


def socket_path() -> Path:
    """The local API socket, per spec/LOCAL_API.md."""
    if env := os.environ.get("ORBIT_SOCKET"):
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "orbit" / "orbitd.sock"
    if runtime := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(runtime) / "orbit" / "orbitd.sock"
    return Path.home() / ".local" / "state" / "orbit" / "orbitd.sock"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_private(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, readable only by the owner."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
