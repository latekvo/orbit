import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from orbit.client import LocalClient
from orbit.daemon.core import Daemon
from orbit.fakephone import FakePhone
from orbit.proto import FrameStream, KeyPair, respond
from orbit.store import PeerStore, PinnedPeer


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


class RawPhone:
    """Minimal scripted responder over real TCP for protocol edge cases.

    ``script(channel)`` runs after the handshake of every accepted connection.
    """

    def __init__(self, script, identity=None, name="Raw Phone"):
        self.identity = identity or KeyPair.generate()
        self.name = name
        self.script = script
        self.port = 0
        self.results = []
        self._server = None
        self._writers = []
        self._tasks = []

    @property
    def id(self):
        from orbit.proto import device_id

        return device_id(self.identity.public)

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        for w in self._writers:
            w.close()
        for t in self._tasks:
            t.cancel()
        self._server.close()

    def _on_conn(self, reader, writer):
        self._writers.append(writer)
        self._tasks.append(asyncio.ensure_future(self._run(FrameStream(reader, writer))))

    async def _run(self, stream):
        try:
            channel = await respond(stream, self.identity, self.name, "android")
            self.results.append(await self.script(channel))
        except Exception as e:  # recorded for assertions
            self.results.append(e)
        finally:
            stream.close()


def pin_in(directory: Path, identity: KeyPair, name="Raw Phone", address=None):
    from orbit.proto import device_id

    store = PeerStore(directory)
    store.pin(PinnedPeer(device_id(identity.public), name, "android", identity.public, address))


async def until(predicate, timeout=5.0, interval=0.02):
    """Poll ``predicate`` (sync or async) until truthy; returns its value."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)
