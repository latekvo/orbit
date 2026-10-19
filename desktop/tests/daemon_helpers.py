"""Non-fixture helpers for the daemon tests (imported as ``daemon_helpers``)."""

import asyncio
from pathlib import Path

from orbit.proto import FrameStream, KeyPair, respond
from orbit.store import PeerStore, PinnedPeer


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
