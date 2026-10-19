import base64
import hashlib
import json
import os
import shutil
import socket
import stat
import struct
import tempfile
import time
import zlib

import pytest
from mock_orbitd import (
    DEMO_SAS,
    DOWNLOAD,
    MESSAGE,
    PIXEL_7A,
    PIXEL_8,
    PIXEL_9_PRO,
    SPOTIFY,
    TIMER,
    WORK_PHONE,
    MockOrbitd,
)


class Client:
    """Blocking NDJSON client over a raw Unix socket."""

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(path)
        self.buf = b""
        self.next_id = 1
        self.events: list[dict] = []

    def send(self, method: str, params: dict | None = None) -> int:
        req_id = self.next_id
        self.next_id += 1
        msg = {"id": req_id, "method": method, "params": params or {}}
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        return req_id

    def read(self) -> dict | None:
        """Next message, or None on EOF."""
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def reply(self, req_id: int) -> dict:
        while True:
            msg = self.read()
            assert msg is not None, "unexpected EOF"
            if msg.get("id") == req_id:
                return msg
            self.events.append(msg)

    def call(self, method: str, params: dict | None = None):
        msg = self.reply(self.send(method, params))
        assert "error" not in msg, msg
        return msg["result"]

    def error(self, method: str, params: dict | None = None) -> str:
        msg = self.reply(self.send(method, params))
        assert "result" not in msg, msg
        return msg["error"]["code"]

    def subscribe(self) -> None:
        """Subscribe and consume the initial snapshot events."""
        assert self.call("subscribe") is True
        self.events += [self.read() for _ in range(3)]
        self.events.clear()

    def close(self):
        self.sock.close()


@pytest.fixture
def sock_path():
    d = tempfile.mkdtemp(prefix="orb", dir="/tmp")
    yield os.path.join(d, "sub", "o.sock")
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mock(sock_path):
    m = MockOrbitd(sock_path, evolve=False)
    m.start()
    yield m
    m.stop()


@pytest.fixture
def client(mock, sock_path):
    c = Client(sock_path)
    yield c
    c.close()


def act(activities, device, activity_id):
    return next(a for a in activities if a["device"] == device and a["id"] == activity_id)


def action_ids(activity):
    return [a["id"] for a in activity["actions"]]


def test_socket_created_private(mock, sock_path):
    assert stat.S_ISSOCK(os.stat(sock_path).st_mode)
    assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600


def test_path_too_long_rejected():
    with pytest.raises(ValueError, match="AF_UNIX"):
        MockOrbitd("/tmp/" + "x" * 200 + ".sock").start()


def test_status(client):
    status = client.call("status")
    assert status["version"] == "0.0.0-mock"
    assert status["name"] and len(status["deviceId"]) == 32


def test_subscribe_result_then_one_of_each_event(client):
    req = client.send("subscribe")
    msgs = [client.read() for _ in range(4)]
    assert msgs[0] == {"id": req, "result": True}
    assert [m["event"] for m in msgs[1:]] == ["devices.changed", "pairing.changed", "activities.changed"]
    assert {d["name"] for d in msgs[1]["data"]["devices"]} == {
        "Pixel 8",
        "Galaxy Tab S9",
        "Work Phone",
        "Pixel 7a",
        "Pixel 9 Pro",
    }
    assert msgs[2]["data"] == {"pairings": []}
    assert len(msgs[3]["data"]["activities"]) == 6


def test_demo_devices_and_ids(client):
    devices = {d["name"]: d for d in client.call("devices.list")}
    assert devices["Pixel 8"]["id"] == PIXEL_8
    assert all(len(d["id"]) == (16 if d["provisional"] else 32) and int(d["id"], 16) >= 0 for d in devices.values())
    assert devices["Pixel 9 Pro"] == {
        "id": PIXEL_9_PRO[:16],
        "name": "Pixel 9 Pro",
        "platform": "",
        "paired": False,
        "connected": False,
        "transport": None,
        "address": None,
        "discovered": ["ble"],
        "provisional": True,
    }
    assert devices["Work Phone"]["connected"] is False
    assert devices["Pixel 7a"]["paired"] is False


def test_play_pause_toggle(mock, client):
    client.subscribe()
    before = act(client.call("activities.list"), PIXEL_8, SPOTIFY)
    assert before["media"]["state"] == "playing"
    assert action_ids(before) == ["media.previous", "media.pause", "media.next", "notif.0"]

    params = {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.pause"}
    assert client.call("activities.action", params) == {"ok": True}
    paused = act(mock.activities, PIXEL_8, SPOTIFY)
    assert paused["media"]["state"] == "paused"
    assert paused["media"]["positionMs"] >= 61000
    assert action_ids(paused) == ["media.previous", "media.play", "media.next", "notif.0"]
    assert paused["actions"][1]["icon"] == "play"
    assert any(e.get("event") == "activities.changed" for e in client.events)

    params2 = {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.play"}
    assert client.call("activities.action", params2) == {"ok": True}
    playing = act(mock.activities, PIXEL_8, SPOTIFY)
    assert playing["media"]["state"] == "playing"
    assert action_ids(playing)[1] == "media.pause"
    assert mock.actions() == [params, params2]


def test_next_switches_track_and_art(mock, client):
    first = act(mock.activities, PIXEL_8, SPOTIFY)
    client.call("activities.action", {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.next"})
    second = act(mock.activities, PIXEL_8, SPOTIFY)
    assert second["title"] != first["title"]
    assert second["artKey"] != first["artKey"]
    assert second["media"]["positionMs"] == 0


def test_seek(mock, client):
    base = {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.seek"}
    assert client.call("activities.action", {**base, "value": 120000}) == {"ok": True}
    media = act(mock.activities, PIXEL_8, SPOTIFY)["media"]
    assert media["positionMs"] == 120000
    assert abs(media["positionAtMs"] - time.time() * 1000) < 2000
    client.call("activities.action", {**base, "value": 10**9})
    assert act(mock.activities, PIXEL_8, SPOTIFY)["media"]["positionMs"] == media["durationMs"]
    assert client.error("activities.action", {**base, "value": "12"}) == "bad_request"
    assert client.error("activities.action", base) == "bad_request"


def test_reply_sets_text(mock, client):
    base = {"device": PIXEL_8, "activity": MESSAGE, "action": "notif.0"}
    assert client.call("activities.action", base) == {"ok": False, "error": "missing text"}
    assert client.call("activities.action", {**base, "value": "  "})["ok"] is False
    assert client.call("activities.action", {**base, "value": "Yes!"}) == {"ok": True}
    assert act(mock.activities, PIXEL_8, MESSAGE)["text"] == "You: Yes!"


def test_action_errors(client):
    def code(**p):
        return client.error("activities.action", {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.pause", **p})

    assert code(device="0" * 32) == "not_found"
    assert code(activity="nope") == "not_found"
    assert code(device=WORK_PHONE) == "not_connected"
    assert client.call("activities.action", {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.play"}) == {
        "ok": False,
        "error": "unsupported action",
    }


def test_fail_next_action(mock, client):
    mock.fail_next_action = "phone said no"
    params = {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.pause"}
    assert client.call("activities.action", params) == {"ok": False, "error": "phone said no"}
    assert act(mock.activities, PIXEL_8, SPOTIFY)["media"]["state"] == "playing"
    assert mock.fail_next_action is None
    assert client.call("activities.action", params) == {"ok": True}


def test_action_delay_does_not_block_other_requests(mock, client):
    mock.action_delay_s = 0.5
    slow = client.send("activities.action", {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.pause"})
    fast = client.send("status")
    t0 = time.monotonic()
    assert client.read()["id"] == fast
    assert client.read() == {"id": slow, "result": {"ok": True}}
    assert time.monotonic() - t0 >= 0.4


def test_timer_pause_resume_add_stop(mock, client):
    base = {"device": PIXEL_8, "activity": TIMER}
    client.call("activities.action", {**base, "action": "notif.0"})
    timer = act(mock.activities, PIXEL_8, TIMER)
    assert timer["chrono"]["running"] is False
    assert 440_000 < timer["chrono"]["valueMs"] <= 450_000
    assert timer["actions"][0]["label"] == "Resume"
    client.call("activities.action", {**base, "action": "notif.1"})
    assert act(mock.activities, PIXEL_8, TIMER)["chrono"]["valueMs"] > 500_000
    client.call("activities.action", {**base, "action": "notif.0"})
    chrono = act(mock.activities, PIXEL_8, TIMER)["chrono"]
    assert chrono["running"] is True and "valueMs" not in chrono
    client.call("activities.action", {**base, "action": "notif.2"})
    assert all(a["id"] != TIMER for a in mock.activities)


def test_download_pause_and_cancel(mock, client):
    dl = act(mock.activities, PIXEL_8, DOWNLOAD)
    assert dl["subtitle"] == "1.2 GB of 5.7 GB"
    client.call("activities.action", {"device": PIXEL_8, "activity": DOWNLOAD, "action": "notif.0"})
    assert act(mock.activities, PIXEL_8, DOWNLOAD)["actions"][0]["label"] == "Resume"
    client.call("activities.action", {"device": PIXEL_8, "activity": DOWNLOAD, "action": "notif.1"})
    assert all(a["id"] != DOWNLOAD for a in mock.activities)


def test_connect_host_port_pair_accept(mock, client):
    client.subscribe()
    assert client.error("devices.connect", {"host": "unreachable.invalid", "port": 1}) == "failed"
    assert client.error("devices.connect", {"host": "10.0.0.9", "port": 70000}) == "failed"
    assert client.error("devices.connect", {}) == "bad_request"

    new_id = client.call("devices.connect", {"host": "10.0.0.9", "port": 47820})["id"]
    dev = next(d for d in client.call("devices.list") if d["id"] == new_id)
    assert dev["name"] == "Phone at 10.0.0.9:47820"
    assert dev["paired"] is False and dev["connected"] is True
    [pairing] = client.call("pairing.list")
    assert pairing["deviceId"] == new_id and len(pairing["sas"]) == 6 and pairing["sas"].isdigit()
    assert pairing["localAccepted"] is False and pairing["remoteAccepted"] is True

    client.events.clear()
    assert client.call("pairing.respond", {"session": pairing["session"], "accept": True}) is True
    assert {e["event"] for e in client.events} >= {"pairing.changed", "devices.changed"}
    assert client.call("pairing.list") == []
    dev = next(d for d in mock.devices if d["id"] == new_id)
    assert dev["paired"] is True and dev["connected"] is True
    assert client.error("pairing.respond", {"session": pairing["session"], "accept": True}) == "not_found"


def test_connect_unpaired_discovered_then_reject(mock, client):
    assert client.call("devices.connect", {"id": PIXEL_7A}) == {"id": PIXEL_7A}
    [pairing] = mock.pairings
    client.call("pairing.respond", {"session": pairing["session"], "accept": False})
    dev = next(d for d in mock.devices if d["id"] == PIXEL_7A)
    assert dev["connected"] is False and dev["paired"] is False
    assert mock.pairings == []


def test_pairing_on_start(sock_path):
    m = MockOrbitd(sock_path, evolve=False, pairing_on_start=True)
    [pairing] = m.pairings
    assert pairing["deviceId"] == PIXEL_7A and pairing["sas"] == DEMO_SAS


def test_disconnect_hides_activities_and_reconnect_restores(mock, client):
    n = len(client.call("activities.list"))
    assert client.call("devices.disconnect", {"id": PIXEL_8}) is True
    assert all(a["device"] != PIXEL_8 for a in client.call("activities.list"))
    dev = next(d for d in mock.devices if d["id"] == PIXEL_8)
    assert dev["connected"] is False and dev["transport"] is None
    client.call("devices.connect", {"id": PIXEL_8})
    assert len(client.call("activities.list")) == n
    assert next(d for d in mock.devices if d["id"] == PIXEL_8)["transport"] == "lan"
    assert client.call("devices.forget", {"id": PIXEL_8}) is True
    assert all(a["device"] != PIXEL_8 for a in mock.activities)
    assert client.error("devices.forget", {"id": PIXEL_8}) == "not_found"


def test_stop_gives_eof_and_restart_keeps_state(mock, client, sock_path):
    client.call("activities.action", {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.pause"})
    assert mock.connection_count == 1
    t0 = time.monotonic()
    mock.stop()
    assert time.monotonic() - t0 < 1
    assert client.read() is None
    assert not os.path.exists(sock_path)

    mock.start()
    c2 = Client(sock_path)
    try:
        assert act(c2.call("activities.list"), PIXEL_8, SPOTIFY)["media"]["state"] == "paused"
    finally:
        c2.close()


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, size, idat = 8, None, b""
    while pos < len(data):
        (length,), tag = struct.unpack(">I", data[pos : pos + 4]), data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(tag + body)
        if tag == b"IHDR":
            size = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    w, h = size
    assert len(zlib.decompress(idat)) == h * (1 + w * 4)
    return size


def test_art_get_valid_png_keyed_by_sha256(client):
    activities = client.call("activities.list")
    spotify = act(activities, PIXEL_8, SPOTIFY)
    keys = {spotify["artKey"]: (192, 192), spotify["iconKey"]: (96, 96)}
    for key, size in keys.items():
        art = client.call("art.get", {"device": PIXEL_8, "key": key})
        data = base64.b64decode(art["data"])
        assert art["mime"] == "image/png"
        assert hashlib.sha256(data).hexdigest()[:16] == key
        assert _png_size(data) == size
    assert client.error("art.get", {"device": PIXEL_8, "key": "0" * 16}) == "not_found"


def test_mutators_broadcast(mock, client):
    client.subscribe()
    mock.update_activity(PIXEL_8, MESSAGE, title="Bob")
    client.call("status")  # flush: events sent before this reply
    [event] = client.events
    assert event["event"] == "activities.changed"
    assert act(event["data"]["activities"], PIXEL_8, MESSAGE)["title"] == "Bob"

    client.events.clear()
    mock.set_devices([])
    mock.set_pairings([])
    mock.set_activities([])
    client.call("status")
    assert [e["event"] for e in client.events] == ["devices.changed", "pairing.changed", "activities.changed"]


def test_unparseable_lines_ignored_and_recorded(mock, client):
    client.sock.sendall(b"this is not json\n\n[1,2]\n")
    assert client.call("status")["version"] == "0.0.0-mock"
    assert [r["method"] for r in mock.requests] == ["status"]
    mock.clear_recorded()
    assert mock.requests == []


def test_scenarios(sock_path):
    assert MockOrbitd(sock_path, scenario="empty").devices == []
    idle = MockOrbitd(sock_path, scenario="idle")
    assert [d["name"] for d in idle.devices] == ["Pixel 8"] and idle.activities == []


def test_evolve_ticks_progress(sock_path):
    m = MockOrbitd(sock_path, tick_s=0.05)
    m.start()
    try:
        before = act(m.activities, PIXEL_8, DOWNLOAD)["progress"]["value"]
        time.sleep(0.3)
        after = act(m.activities, PIXEL_8, DOWNLOAD)
        assert after["progress"]["value"] > before
        gb = 5.7 * after["progress"]["value"] / 100
        assert after["subtitle"] == f"{gb:.1f} GB of 5.7 GB"
    finally:
        m.stop()


def test_evolve_auto_advances_track(sock_path):
    m = MockOrbitd(sock_path, tick_s=0.05)
    m.start()
    try:
        c = Client(sock_path)
        first = act(m.activities, PIXEL_8, SPOTIFY)
        end = first["media"]["durationMs"] - 50
        c.call("activities.action", {"device": PIXEL_8, "activity": SPOTIFY, "action": "media.seek", "value": end})
        time.sleep(0.4)
        second = act(m.activities, PIXEL_8, SPOTIFY)
        assert second["title"] != first["title"]
        assert second["media"]["positionMs"] == 0
        c.close()
    finally:
        m.stop()


def test_connect_provisional_ble_device_yields_real_id_and_pairing(mock, client):
    client.subscribe()
    reply = client.call("devices.connect", {"id": PIXEL_9_PRO[:16]})
    assert reply == {"id": PIXEL_9_PRO}
    ids = [d["id"] for d in client.call("devices.list")]
    assert PIXEL_9_PRO in ids and PIXEL_9_PRO[:16] not in ids
    real = next(d for d in client.call("devices.list") if d["id"] == PIXEL_9_PRO)
    assert real["provisional"] is False and real["connected"] is True and real["transport"] == "ble"
    assert [p["deviceId"] for p in client.call("pairing.list")] == [PIXEL_9_PRO]
