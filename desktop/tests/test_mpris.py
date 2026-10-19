import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from orbit import mpris
from orbit.mpris import (
    ActionFailed,
    ArtCache,
    Bridge,
    MediaView,
    bus_name,
    plan_next,
    plan_pause,
    plan_play,
    plan_play_pause,
    plan_previous,
    plan_seek,
    plan_set_position,
    plan_stop,
    seek_jump,
)

_spec = importlib.util.spec_from_file_location(
    "stub_orbitd", Path(__file__).resolve().parents[1] / "tools" / "linux-e2e" / "stub_orbitd.py"
)
stub_orbitd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stub_orbitd)

DEV = stub_orbitd.DEVICE_ID
MEDIA = stub_orbitd.MEDIA_ID
NOW = 1_760_000_000_000
BUS_ELEMENT = re.compile(r"[A-Za-z_-][A-Za-z0-9_-]*")
OBJECT_PATH = re.compile(r"(/[A-Za-z0-9_]+)+")


def act(state="playing", actions=("media.previous", "media.pause", "media.next"), media=None, **over):
    a = {
        "device": DEV,
        "id": MEDIA,
        "kind": "media",
        "app": {"id": "com.spotify.music", "name": "Spotify"},
        "title": "Song One",
        "subtitle": "Artist A",
        "text": "Album X",
        "media": {
            "state": state,
            "positionMs": 61000,
            "positionAtMs": NOW,
            "durationMs": 215000,
            "rate": 1.0,
            "seekable": True,
            **(media or {}),
        },
        "actions": [{"id": i, "label": i} for i in actions],
        "updatedMs": NOW,
    }
    a.update(over)
    return a


def view(*args, **kw):
    return MediaView(act(*args, **kw), "Pixel 8")


# --- pure mapping -------------------------------------------------------------------------


def test_bus_name_is_deterministic_and_valid():
    name = bus_name(DEV, MEDIA)
    expected_hash = hashlib.sha256(f"{DEV}\0{MEDIA}".encode()).hexdigest()[:8]
    assert name == f"org.mpris.MediaPlayer2.orbit_a1b2c3d4_{expected_hash}"
    assert all(BUS_ELEMENT.fullmatch(e) for e in name.split("."))
    assert bus_name(DEV, "media:other") != name
    assert bus_name("b" + DEV[1:], MEDIA) != name
    odd = bus_name("!!-..", MEDIA)
    assert odd.startswith("org.mpris.MediaPlayer2.orbit_x_")
    assert all(BUS_ELEMENT.fullmatch(e) for e in odd.split("."))


@pytest.mark.parametrize(
    ("state", "status"),
    [("playing", "Playing"), ("paused", "Paused"), ("buffering", "Paused"), ("stopped", "Stopped"), ("?", "Stopped")],
)
def test_playback_status(state, status):
    assert view(state).playback_status == status


def test_media_kind_detection():
    assert mpris.is_media(act())
    assert mpris.is_media({"kind": "generic", "media": {"state": "paused"}})
    assert not mpris.is_media({"kind": "timer", "chrono": {}})


def test_identity():
    assert view().identity == "Spotify (Pixel 8)"
    assert MediaView(act()).identity == "Spotify"
    assert MediaView(act(app={"id": "org.app"}), "Pixel").identity == "org.app (Pixel)"


def test_metadata_full(tmp_path):
    art = tmp_path / "k.png"
    v = MediaView(act(), "Pixel 8", art)
    assert v.metadata() == {
        "mpris:trackid": ("o", v.track_id),
        "mpris:length": ("x", 215_000_000),
        "xesam:title": ("s", "Song One"),
        "xesam:artist": ("as", ["Artist A"]),
        "xesam:album": ("s", "Album X"),
        "mpris:artUrl": ("s", art.as_uri()),
    }
    assert art.as_uri().startswith("file:///")


def test_metadata_minimal():
    a = act(media={"durationMs": None})
    del a["subtitle"], a["text"]
    v = MediaView(a)
    assert v.metadata() == {"mpris:trackid": ("o", v.track_id), "xesam:title": ("s", "Song One")}


def test_track_id():
    tid = view().track_id
    assert OBJECT_PATH.fullmatch(tid)
    assert not tid.startswith("/org/mpris")
    assert view(state="paused").track_id == tid
    assert view(title="Song Two").track_id != tid
    assert MediaView(act(id="media:other")).track_id != tid


def test_position_extrapolates_only_while_playing():
    assert view().position_us(NOW) == 61_000_000
    assert view().position_us(NOW + 2500) == 63_500_000
    assert view(media={"rate": 2.0}).position_us(NOW + 2500) == 66_000_000
    assert view("paused").position_us(NOW + 2500) == 61_000_000
    assert view("buffering").position_us(NOW + 2500) == 61_000_000
    assert view().position_us(NOW + 10_000_000) == 215_000_000
    assert view().position_us(NOW - 5000) == 61_000_000
    assert view(media={"positionAtMs": None}).position_us(NOW + 2500) == 61_000_000
    assert view(media={"rate": float("nan")}).position_us(NOW + 1000) == 62_000_000


def test_player_props():
    p = view(media={"rate": 1.5, "seekable": False}).player_props()
    assert p["Rate"] == 1.5 and p["MinimumRate"] == 1.0 and p["MaximumRate"] == 1.5
    assert p["CanSeek"] is False
    assert p["CanControl"] is True and p["Volume"] == 1.0
    assert "Position" not in p


@pytest.mark.parametrize(
    ("state", "actions", "can_play", "can_pause"),
    [
        ("playing", ["media.pause"], True, True),
        ("paused", ["media.play"], True, True),
        ("playing", ["media.play"], True, False),
        ("paused", ["media.pause"], False, True),
        ("playing", [], False, False),
    ],
)
def test_play_pause_capabilities(state, actions, can_play, can_pause):
    p = view(state, actions).player_props()
    assert (p["CanPlay"], p["CanPause"]) == (can_play, can_pause)


def test_next_previous_capabilities():
    assert view(actions=["media.next"]).player_props()["CanGoNext"] is True
    assert view(actions=["media.next"]).player_props()["CanGoPrevious"] is False
    assert view(actions=["media.previous"]).player_props()["CanGoPrevious"] is True


def test_transport_plans():
    assert plan_play_pause(view("playing")) == ("media.pause", None)
    assert plan_play_pause(view("paused")) == ("media.play", None)
    assert plan_play_pause(view("buffering")) == ("media.play", None)
    assert plan_play(view("playing")) is None
    assert plan_play(view("paused")) == ("media.play", None)
    assert plan_pause(view("paused")) is None
    assert plan_pause(view("stopped")) is None
    assert plan_pause(view("buffering")) == ("media.pause", None)
    assert plan_stop(view(actions=["media.stop"])) == ("media.stop", None)
    assert plan_stop(view("playing", [])) == ("media.pause", None)
    assert plan_stop(view("paused", [])) is None
    assert plan_next(view()) == ("media.next", None)
    assert plan_next(view(actions=[])) is None
    assert plan_previous(view()) == ("media.previous", None)
    assert plan_previous(view(actions=[])) is None


def test_seek_plans():
    v = view("paused")
    assert plan_seek(v, 10_000_000, NOW) == ("media.seek", 71000)
    assert plan_seek(v, -100_000_000, NOW) == ("media.seek", 0)
    assert plan_seek(v, 999_000_000, NOW) == ("media.seek", 215000)
    assert plan_seek(view("playing"), 1_000_000, NOW + 2000) == ("media.seek", 64000)
    assert plan_seek(view(media={"seekable": False}), 1_000_000, NOW) is None
    no_len = view("paused", media={"durationMs": None})
    assert plan_seek(no_len, 999_000_000, NOW) == ("media.seek", 1_060_000)


def test_set_position_plans():
    v = view()
    assert plan_set_position(v, v.track_id, 30_000_000) == ("media.seek", 30000)
    assert plan_set_position(v, v.track_id, 215_000_000) == ("media.seek", 215000)
    assert plan_set_position(v, "/org/orbit/track/tstale", 30_000_000) is None
    assert plan_set_position(v, v.track_id, -1) is None
    assert plan_set_position(v, v.track_id, 215_000_001) is None
    assert plan_set_position(view(media={"seekable": False}), v.track_id, 1) is None


def test_seek_jump():
    old = view()
    drift = view(media={"positionMs": 62000, "positionAtMs": NOW + 1000})
    assert seek_jump(old, drift, NOW + 3000) is None
    jumped = view(media={"positionMs": 30000, "positionAtMs": NOW + 1000})
    assert seek_jump(old, jumped, NOW + 3000) == 32_000_000
    assert seek_jump(old, view(media={"positionMs": 63600}), NOW) == 63_600_000
    assert seek_jump(old, view(media={"positionMs": 62400}), NOW) is None
    assert seek_jump(old, view(title="Song Two", media={"positionMs": 0}), NOW) is None


def test_art_cache(tmp_path):
    cache = ArtCache(tmp_path / "art", max_files=2)
    data = stub_orbitd.ART[0]
    key = stub_orbitd.art_key(data)
    assert cache.lookup(key) is None
    path = cache.store(key, "image/png", base64.b64encode(data).decode())
    assert path == tmp_path / "art" / f"{key}.png"
    assert path.read_bytes() == data
    assert cache.lookup(key) == path
    assert cache.store("cafe", "image/jpeg", "AAAA").name == "cafe.jpg"
    os.utime(path, (1, 1))
    cache.store("beef", "image/png", "AAAA")
    assert sorted(p.name for p in (tmp_path / "art").iterdir()) == ["beef.png", "cafe.jpg"]
    with pytest.raises(ValueError):
        cache.store("../evil", "image/png", "AAAA")
    with pytest.raises(ValueError):
        cache.store("abcd", "image/gif", "AAAA")
    with pytest.raises(ValueError):
        cache.store("abcd", "image/png", "not base64!")


def test_main_refuses_without_linux_dbus(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert mpris.main([]) == 2
    assert "Linux" in capsys.readouterr().err


# --- bridge against the stub orbitd -------------------------------------------------------


class FakePlayer:
    def __init__(self, view, perform):
        self.view = view
        self.perform = perform
        self.updates = 0
        self.closed = False
        self.alive = True

    def update(self, view):
        self.view = view
        self.updates += 1

    async def close(self):
        self.closed = True


async def wait_for(cond, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@contextlib.asynccontextmanager
async def stub_bridge():
    # Short directory: AF_UNIX paths are limited to ~104 bytes on macOS.
    tmp = Path(tempfile.mkdtemp(prefix="orbit-mpris-", dir="/tmp"))
    sock = tmp / "orbitd.sock"
    stub = stub_orbitd.Stub()
    await stub.start(sock)
    made: list[FakePlayer] = []

    async def factory(view, perform):
        made.append(FakePlayer(view, perform))
        return made[-1]

    bridge = Bridge(factory, ArtCache(tmp / "art"))
    task = asyncio.create_task(bridge.run(sock))
    try:
        yield stub, bridge, made, sock
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


async def test_bridge_mirrors_media_activities_and_routes_actions():
    async with stub_bridge() as (stub, bridge, made, _):
        await wait_for(lambda: bridge.players)
        assert list(bridge.players) == [(DEV, MEDIA)]
        p = bridge.players[(DEV, MEDIA)]
        assert p.view.identity == "Spotify (Pixel 8)"

        await wait_for(lambda: p.view.art_path is not None)
        assert p.view.art_path.read_bytes() == stub_orbitd.ART[0]
        assert p.view.metadata()["mpris:artUrl"] == ("s", p.view.art_path.as_uri())

        await p.perform(DEV, MEDIA, *plan_play_pause(p.view))
        assert stub.calls[-1] == {"device": DEV, "activity": MEDIA, "action": "media.pause"}
        await wait_for(lambda: p.view.playback_status == "Paused")

        await p.perform(DEV, MEDIA, *plan_seek(p.view, 10_000_000, mpris.now_ms()))
        assert stub.calls[-1]["action"] == "media.seek"
        assert 71000 <= stub.calls[-1]["value"] <= 72000

        await p.perform(DEV, MEDIA, *plan_next(p.view))
        await wait_for(lambda: p.view.act["title"] == "Song Two")
        await wait_for(lambda: p.view.art_path is not None and p.view.art_path.read_bytes() == stub_orbitd.ART[1])

        with pytest.raises(ActionFailed, match="unknown action"):
            await p.perform(DEV, MEDIA, "media.bogus", None)

        stub.handle("stub.device", {"set": {"connected": False}})
        await wait_for(lambda: not bridge.players)
        assert p.closed
        stub.handle("stub.device", {"set": {"connected": True}})
        await wait_for(lambda: bridge.players)
        assert len(made) == 2

        stub.handle("stub.remove", {"id": MEDIA})
        await wait_for(lambda: not bridge.players)
        assert made[-1].closed


async def test_bridge_drops_players_while_daemon_is_away_and_reconnects():
    async with stub_bridge() as (stub, bridge, made, sock):
        await wait_for(lambda: bridge.players)
        await stub.close()
        await wait_for(lambda: not bridge.players)
        assert made[0].closed
        with pytest.raises(ConnectionError):
            await bridge.perform(DEV, MEDIA, "media.play")

        stub2 = stub_orbitd.Stub()
        await stub2.start(sock)
        try:
            await wait_for(lambda: bridge.players, seconds=8)
            assert len(made) == 2
            await bridge.players[(DEV, MEDIA)].perform(DEV, MEDIA, "media.pause", None)
            assert stub2.calls == [{"device": DEV, "activity": MEDIA, "action": "media.pause"}]
        finally:
            await stub2.close()


async def test_bridge_replaces_dead_player():
    async with stub_bridge() as (stub, bridge, made, _):
        await wait_for(lambda: bridge.players)
        made[0].alive = False
        stub.handle("stub.patch", {"id": MEDIA, "set": {"title": "Renamed"}})
        await wait_for(lambda: len(made) == 2)
        assert made[0].closed and made[1].view.act["title"] == "Renamed"


# --- real D-Bus (Linux with a session bus) ------------------------------------------------

dbus = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
    reason="needs Linux and a D-Bus session bus",
)


@dbus
async def test_dbus_player_properties_methods_and_signals():
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.errors import DBusError

    calls = []

    async def perform(device, activity, action, value):
        calls.append((device, activity, action, value))
        if action == "media.previous":
            raise ActionFailed("phone said no")

    create = mpris.dbus_player_factory()
    v = MediaView(act(media={"positionAtMs": mpris.now_ms()}), "Pixel 8")
    player = await create(v, perform)
    client = await MessageBus().connect()
    try:
        name = bus_name(DEV, MEDIA)
        obj = client.get_proxy_object(name, mpris.MPRIS_PATH, await client.introspect(name, mpris.MPRIS_PATH))
        root = obj.get_interface("org.mpris.MediaPlayer2")
        iface = obj.get_interface("org.mpris.MediaPlayer2.Player")
        props = obj.get_interface("org.freedesktop.DBus.Properties")

        assert await root.get_identity() == "Spotify (Pixel 8)"
        assert await root.get_can_raise() is True
        assert await root.get_can_quit() is False
        assert await iface.get_playback_status() == "Playing"
        md = await iface.get_metadata()
        assert md["xesam:title"].value == "Song One"
        assert md["xesam:artist"].value == ["Artist A"]
        assert md["mpris:length"].value == 215_000_000
        assert md["mpris:trackid"].value == v.track_id
        assert await iface.get_can_go_next() is True

        await iface.call_play_pause()
        await iface.call_next()
        await iface.call_seek(5_000_000)
        await iface.call_set_position(v.track_id, 30_000_000)
        await iface.call_set_position("/org/orbit/track/tstale", 30_000_000)
        await root.call_raise()
        assert [c[2:] for c in calls] == [
            ("media.pause", None),
            ("media.next", None),
            ("media.seek", calls[2][3]),
            ("media.seek", 30000),
            ("open", None),
        ]
        assert calls[0][:2] == (DEV, MEDIA)
        assert 66000 <= calls[2][3] <= 67000
        with pytest.raises(DBusError) as err:
            await iface.call_previous()
        assert err.value.type == "org.orbit.Error.ActionFailed" and "phone said no" in err.value.text
        with pytest.raises(DBusError) as err:
            await iface.call_open_uri("file:///x")
        assert err.value.type == "org.freedesktop.DBus.Error.NotSupported"

        changes, seeks = [], []
        props.on_properties_changed(lambda i, c, inv: changes.append((i, c)))
        iface.on_seeked(seeks.append)
        player.update(MediaView(act("paused", ["media.play"], media={"positionMs": 10000}), "Pixel 8"))
        await wait_for(lambda: changes and seeks)
        iface_name, changed = changes[0]
        assert iface_name == "org.mpris.MediaPlayer2.Player"
        assert changed["PlaybackStatus"].value == "Paused"
        assert set(changed) == {"PlaybackStatus", "CanGoNext", "CanGoPrevious"}
        assert seeks == [10_000_000]

        with pytest.raises(RuntimeError, match="already owned"):
            await create(v, perform)

        await player.close()
        reply = await client.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="NameHasOwner",
                signature="s",
                body=[name],
            )
        )
        assert reply.message_type == MessageType.METHOD_RETURN and reply.body == [False]
    finally:
        client.disconnect()
        await player.close()
