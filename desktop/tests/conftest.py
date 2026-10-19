import shutil
import tempfile
from pathlib import Path

import pytest

from orbit.client import LocalClient
from orbit.daemon.core import Daemon
from orbit.fakephone import FakePhone


@pytest.fixture
def short_tmp():
    # Unix socket paths are limited to ~104 bytes on macOS; pytest's tmp_path is too long.
    path = Path(tempfile.mkdtemp(prefix="orbit-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def make_daemon(short_tmp):
    """Factory for started daemons (mDNS/BLE off, fast timeouts overridable); all are stopped at teardown."""
    started = []

    async def make(name="Test Laptop", config="daemon", **kwargs):
        kwargs.setdefault("mdns", False)
        kwargs.setdefault("ble", False)
        kwargs.setdefault("connect_timeout", 3.0)
        d = Daemon(short_tmp / config, name, short_tmp / f"{config}.sock", **kwargs)
        await d.start()
        started.append(d)
        return d

    yield make
    for d in started:
        await d.stop()


@pytest.fixture
async def make_phone(short_tmp):
    """Factory for started fake phones on 127.0.0.1 (random port, no mDNS)."""
    phones = []

    async def make(config="phone", **kw):
        kw.setdefault("mdns", False)
        kw.setdefault("host", "127.0.0.1")
        kw.setdefault("port", 0)
        phone = FakePhone(short_tmp / config, **kw)
        await phone.start()
        phones.append(phone)
        return phone

    yield make
    for p in phones:
        await p.stop()


@pytest.fixture
async def api_for():
    clients = []

    async def connect(daemon):
        c = await LocalClient.connect(daemon.api.path)
        clients.append(c)
        return c

    yield connect
    for c in clients:
        await c.close()
