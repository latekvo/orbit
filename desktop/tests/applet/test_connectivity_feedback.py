"""What the applet tells the user when orbitd, the link or Bluetooth misbehave."""

import pytest
from applet_helpers import make_mock
from mock_orbitd import PIXEL_7A, PIXEL_8, WORK_PHONE
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QToolButton
from shiboken6 import isValid

from orbit.applet import model
from orbit.applet import panel as panel_mod
from orbit.applet.client import DaemonClient
from orbit.applet.pairing import PairingManager
from orbit.applet.panel import Panel
from orbit.applet.theme import LIGHT
from orbit.applet.tray import TrayController


def make_panel(qtbot, client):
    panel = Panel(client, LIGHT, popup=False)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def fast_heartbeat(client):
    client._heartbeat.setInterval(100)
    client.probe_timeout_ms = 100


def menu_texts(tray):
    tray._rebuild_menu()
    return [a.text() for a in tray.menu.actions()]


def connect(qtbot, sock_path):
    c = DaemonClient(sock_path)
    c.start()
    qtbot.waitUntil(lambda: c.connected and bool(c.status), timeout=5000)
    return c


# -- Bluetooth -------------------------------------------------------------------------------

# bleError strings as the real orbitd reports them (str() of bleak's exceptions).
REAL_BLE_ERRORS = [
    (
        "('Bluetooth device is turned off', <BleakBluetoothNotAvailableReason.POWERED_OFF: 3>)",
        "Bluetooth is turned off",
    ),
    (
        "('Bluetooth access is denied by the user for the current application. Check macOS privacy settings.', "
        "<BleakBluetoothNotAvailableReason.DENIED_BY_USER: 4>)",
        "Orbit isn't allowed to use Bluetooth",
    ),
    ("('Bluetooth is unsupported', <BleakBluetoothNotAvailableReason.NO_BLUETOOTH: 1>)", "No Bluetooth adapter found"),
    (
        "('No powered Bluetooth adapters found. Turn on Bluetooth and try again.', "
        "<BleakBluetoothNotAvailableReason.POWERED_OFF: 3>)",
        "Bluetooth is turned off",
    ),
    (
        "('No Bluetooth adapters found.', <BleakBluetoothNotAvailableReason.NO_BLUETOOTH: 1>)",
        "No Bluetooth adapter found",
    ),
    ("[Errno 2] No such file or directory", "The Bluetooth service isn't running"),
    ("org.freedesktop.DBus.Error.ServiceUnknown", "The Bluetooth service isn't running"),
    (
        "('Connection to the Bluetooth system service was lost. Currently trying to reconnect...', "
        "<BleakBluetoothNotAvailableReason.UNKNOWN: 7>)",
        "Bluetooth is restarting",
    ),
    ("something new", "Bluetooth isn't available"),
    (None, "Bluetooth isn't available"),
]


@pytest.mark.parametrize("raw, headline", REAL_BLE_ERRORS)
def test_bluetooth_problem_reads_real_ble_errors(raw, headline):
    assert model.bluetooth_problem({"ble": "unavailable", "bleError": raw}, "darwin")[0] == headline


@pytest.mark.parametrize("ble", ["scanning", "starting", "off", None])
def test_working_or_disabled_bluetooth_is_not_a_problem(ble):
    assert model.bluetooth_problem({"ble": ble, "bleError": None}) is None


def test_bluetooth_permission_advice_names_the_platforms_setting():
    status = {"ble": "unavailable", "bleError": "BLE is not authorized"}
    assert "Privacy & Security" in model.bluetooth_problem(status, "darwin")[1]
    assert "BlueZ" in model.bluetooth_problem(status, "linux")[1]


def test_bluetooth_unavailable_is_shown_and_cleared_without_reconnecting(qtbot, client, mock):
    fast_heartbeat(client)
    tray = TrayController(client, LIGHT)
    page = tray.panel.devices_page
    summary = model.devices_summary(client.devices)
    assert page.bluetooth.isHidden() and tray.tray.toolTip() == f"Orbit - {summary}"
    connections = mock.connection_count

    raw = "[Errno 2] No such file or directory"
    mock.status_extra = {"ble": "unavailable", "bleError": raw}
    qtbot.waitUntil(lambda: not page.bluetooth.isHidden(), timeout=2000)
    assert page.bluetooth.text() == (
        "<b>The Bluetooth service isn't running</b> - phones are found over Wi-Fi only. "
        "Start BlueZ (bluetooth.service) to use Bluetooth."
    )
    assert page.bluetooth.toolTip() == raw
    assert tray.tray.toolTip() == f"Orbit - {summary}\nThe Bluetooth service isn't running"
    assert menu_texts(tray)[1] == "The Bluetooth service isn't running"

    mock.status_extra = {"ble": "scanning", "bleError": None}
    qtbot.waitUntil(lambda: page.bluetooth.isHidden(), timeout=2000)
    assert tray.tray.toolTip() == f"Orbit - {summary}"
    assert menu_texts(tray)[1] == "Show activities"
    assert mock.connection_count == connections


def test_empty_states_say_bluetooth_is_off(qtbot, sock_path):
    m = make_mock(sock_path, scenario="empty")
    m.status_extra = {"ble": "unavailable", "bleError": "Bluetooth device is turned off"}
    try:
        c = connect(qtbot, sock_path)
        panel = make_panel(qtbot, c)
        empty = panel.activities_page.empty
        assert empty.headline.text() == "No phone paired yet"
        assert empty.hint.text() == (
            "Open Orbit on your phone on the same Wi-Fi, then pair it here. "
            "Bluetooth is turned off, so Orbit can only use Wi-Fi."
        )
        panel.show_devices()
        hints = [w.text() for w in panel.devices_page._extras]
        assert hints == [
            "No devices found",
            "Open Orbit on your phone on the same Wi-Fi network and it will appear here. "
            "You can also add it by address.",
        ]
        phone = {"id": PIXEL_8, "name": "Pixel 8", "paired": True, "connected": False, "state": "offline"}
        m.set_devices([phone])
        qtbot.waitUntil(lambda: empty.headline.text() == "No phone connected")
        assert empty.hint.text().endswith(" Bluetooth is turned off, so Orbit can only use Wi-Fi.")
        c.stop()
    finally:
        m.stop()


# -- pairing ---------------------------------------------------------------------------------


def pending(session="sess-1", **kw):
    return {
        "session": session,
        "deviceId": PIXEL_7A,
        "name": "Pixel 7a",
        "sas": "042917",
        "localAccepted": False,
        "remoteAccepted": False,
        **kw,
    }


def open_dialog(qtbot, client, mock, **kw):
    mgr = PairingManager(client, LIGHT)
    failures = []
    mgr.pairingFailed.connect(lambda p, reason: failures.append(reason))
    mock.set_pairings([pending(**kw)])
    qtbot.waitUntil(lambda: "sess-1" in mgr.dialogs)
    dlg = mgr.dialogs["sess-1"]
    qtbot.mouseClick(dlg.accept_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "Waiting" in dlg.state.text())
    return mgr, dlg, failures


@pytest.mark.parametrize(
    "remote, reason",
    [
        (False, "Pixel 7a declined the code, or the connection was lost before it confirmed."),
        (True, "The connection to Pixel 7a was lost before pairing finished."),
    ],
)
def test_pairing_ended_by_the_link_says_so_and_retries(qtbot, client, mock, remote, reason):
    mgr, dlg, failures = open_dialog(qtbot, client, mock, remoteAccepted=remote)
    mock.set_pairings([])  # the session is gone; the phone is still unpaired
    qtbot.waitUntil(lambda: dlg.failed, timeout=2000)
    assert dlg.isVisible() and "sess-1" not in mgr.dialogs
    assert (dlg.headline.text(), dlg.hint.text()) == ("Pairing with Pixel 7a failed", reason)
    assert failures == [reason]
    assert (dlg.cancel.text(), dlg.accept_btn.text(), dlg.accept_btn.isEnabled()) == ("Close", "Try again", True)

    mock.clear_recorded()
    qtbot.mouseClick(dlg.accept_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(mgr.dialogs), timeout=2000)
    assert {"method": "devices.connect", "params": {"id": PIXEL_7A}} in mock.requests
    assert not any(r["method"] == "pairing.respond" for r in mock.requests)
    [new] = mgr.dialogs.values()
    assert new is not dlg and new.isVisible() and not new.failed
    qtbot.waitUntil(lambda: not isValid(dlg) or not dlg.isVisible())


def test_pairing_ended_by_orbitd_stopping_waits_for_it_to_come_back(qtbot, client, mock):
    mgr, dlg, failures = open_dialog(qtbot, client, mock)
    mock.set_pairings([])  # a stopping orbitd ends its sessions first, then closes the socket
    mock.stop()
    qtbot.waitUntil(lambda: dlg.failed, timeout=2000)
    assert dlg.hint.text() == "orbitd stopped before pairing finished."
    assert not dlg.accept_btn.isEnabled()
    mock.start()
    qtbot.waitUntil(lambda: dlg.accept_btn.isEnabled(), timeout=5000)
    qtbot.mouseClick(dlg.cancel, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not isValid(dlg) or not dlg.isVisible())
    assert failures == ["orbitd stopped before pairing finished."]


@pytest.mark.parametrize("accept", [True, False])
def test_paired_or_declined_pairing_is_not_a_failure(qtbot, client, mock, accept):
    mgr = PairingManager(client, LIGHT)
    failures = []
    mgr.pairingFailed.connect(lambda p, reason: failures.append(reason))
    mock.set_pairings([pending(remoteAccepted=True)])
    qtbot.waitUntil(lambda: "sess-1" in mgr.dialogs)
    dlg = mgr.dialogs["sess-1"]
    qtbot.mouseClick(dlg.accept_btn if accept else dlg.cancel, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not isValid(dlg) or not dlg.isVisible())
    qtbot.wait(700)
    assert failures == [] and mgr.failed == []


# -- an action in flight when orbitd dies ------------------------------------------------------


def test_action_in_flight_when_orbitd_dies_may_have_gone_through(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = next(a for a in mock.activities if a["kind"] == "media" and a["device"] == PIXEL_8)
    mock.action_delay_s = 5
    card = panel.activities_page.cards[(PIXEL_8, media["id"])]
    qtbot.mouseClick(card.findChild(QToolButton, "action:media.next"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(mock.actions()))
    mock.stop()
    qtbot.waitUntil(lambda: not client.connected, timeout=3000)
    empty = panel.activities_page.empty
    assert empty.headline.text() == "orbitd is not running"
    assert empty.hint.text().endswith(" Your last action may have gone through before it stopped.")

    mock.action_delay_s = 0
    mock.start()
    qtbot.waitUntil(lambda: (PIXEL_8, media["id"]) in panel.activities_page.cards, timeout=8000)
    card = panel.activities_page.cards[(PIXEL_8, media["id"])]
    assert card.error.isVisibleTo(panel)
    assert (card.error.text(), card.error.property("role")) == (
        "orbitd stopped - the action may have gone through",
        "notice",
    )
    others = [c for k, c in panel.activities_page.cards.items() if k != (PIXEL_8, media["id"])]
    assert others and not any(c.error.isVisibleTo(panel) for c in others)


def test_lost_action_notice_expires(qtbot, client, mock, monkeypatch):
    monkeypatch.setattr(panel_mod, "LOST_NOTICE_S", 0.2)
    panel = make_panel(qtbot, client)
    media = next(a for a in mock.activities if a["kind"] == "media" and a["device"] == PIXEL_8)
    mock.action_delay_s = 5
    card = panel.activities_page.cards[(PIXEL_8, media["id"])]
    qtbot.mouseClick(card.findChild(QToolButton, "action:media.next"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(mock.actions()))
    mock.stop()
    qtbot.waitUntil(lambda: not client.connected, timeout=3000)
    qtbot.wait(300)
    mock.action_delay_s = 0
    mock.start()
    qtbot.waitUntil(lambda: (PIXEL_8, media["id"]) in panel.activities_page.cards, timeout=8000)
    assert not panel.activities_page.cards[(PIXEL_8, media["id"])].error.isVisibleTo(panel)


# -- device rows -----------------------------------------------------------------------------

REFUSED = (
    "all 1 addresses failed: cannot connect to 192.168.1.44:47820: "
    "[Errno 61] Connect call failed ('192.168.1.44', 47820)"
)


@pytest.mark.parametrize(
    "error, text",
    [
        ({"code": "failed", "message": REFUSED}, "Work Phone isn't reachable"),
        (
            {"code": "failed", "message": "cannot connect to 10.0.0.5:47820: [Errno 61] Connect call failed"},
            "Work Phone isn't reachable",
        ),
        ({"code": "timeout", "message": "connecting to 10.0.0.5:47820 timed out"}, "Work Phone didn't answer in time"),
        ({"code": "not_connected", "message": "no known address for this device"}, "Work Phone isn't nearby"),
        ({"code": "not_found", "message": "unknown device abc"}, "Work Phone is no longer listed"),
        (
            {"code": "failed", "message": "10.0.0.5:47820 is device 1234, expected 5678"},
            "A different device answered for Work Phone",
        ),
        (
            {"code": "failed", "message": "handshake with 10.0.0.5:47820 failed: bad frame"},
            "Work Phone answered, but the secure connection failed",
        ),
        (
            {"code": "failed", "message": "sending pair_request failed: connection reset"},
            "The connection to Work Phone dropped before pairing started",
        ),
        (
            {"code": "failed", "message": "gave up on AA:BB: the phone advertised a newer address"},
            "Work Phone moved to a new address - try again",
        ),
        ({"code": model.ORBITD_DOWN, "message": "orbitd is not running"}, "orbitd is not running"),
    ],
)
def test_connect_failure_text(error, text):
    assert model.connect_failure(error, "Work Phone") == text


def test_connect_failure_shows_on_the_device_row(qtbot, client, mock, monkeypatch):
    monkeypatch.setattr(panel_mod, "MESSAGE_VISIBLE_MS", 300)
    panel = make_panel(qtbot, client)
    panel.show_devices()
    page = panel.devices_page
    row = page._rows[WORK_PHONE]
    assert row.status.fullText() == "Offline"
    mock.fail_next["devices.connect"] = {"code": "failed", "message": REFUSED}
    qtbot.mouseClick(row.buttons["Connect"], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: row.error is not None, timeout=2000)
    assert (row.status.fullText(), row.status.property("role")) == ("Work Phone isn't reachable", "error")
    assert row.status.toolTip() == REFUSED
    assert page.message.isHidden()
    assert row.buttons["Connect"].isEnabled() and row.buttons["Connect"].text() == "Connect"
    qtbot.waitUntil(lambda: row.status.fullText() == "Offline", timeout=2000)
    assert row.status.property("role") == "muted" and row.status.toolTip() == ""


def test_disconnect_failure_names_the_device(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    panel.show_devices()
    row = panel.devices_page._rows[PIXEL_8]
    mock.fail_next["devices.disconnect"] = {"code": "not_found", "message": f"unknown device {PIXEL_8}"}
    qtbot.mouseClick(row.buttons["Disconnect"], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: row.error is not None, timeout=2000)
    assert row.status.fullText() == "Couldn't disconnect Pixel 8"


# -- a frozen orbitd -------------------------------------------------------------------------


def test_frozen_orbitd_is_shown_and_clicks_are_not_queued(qtbot, client, mock):
    fast_heartbeat(client)
    tray = TrayController(client, LIGHT)
    panel = make_panel(qtbot, client)
    summary = model.devices_summary(client.devices)
    assert panel.status.fullText() == summary
    mock.unresponsive = True
    qtbot.waitUntil(lambda: panel.status.fullText() == "orbitd isn't responding", timeout=2000)
    assert client.connected and not client.responsive
    assert tray.tray.toolTip() == "Orbit - orbitd isn't responding"
    assert menu_texts(tray)[0] == "orbitd 0.0.0-mock - not responding"

    mock.clear_recorded()
    card = next(c for c in panel.activities_page.cards.values() if c.activity["kind"] == "media")
    qtbot.mouseClick(card.findChild(QToolButton, "action:media.next"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: card.error.isVisibleTo(panel), timeout=500)
    assert card.error.text() == "orbitd isn't responding"
    qtbot.wait(300)
    assert not any(r["method"] == "activities.action" for r in mock.requests)
    assert any(r["method"] == "status" for r in mock.requests)  # the heartbeat keeps probing

    mock.unresponsive = False
    qtbot.waitUntil(lambda: panel.status.fullText() == summary, timeout=2000)
    assert client.responsive and tray.tray.toolTip() == f"Orbit - {summary}"
    assert menu_texts(tray)[0] == "orbitd 0.0.0-mock - connected"


def test_slow_status_with_other_traffic_is_not_frozen(qtbot, client, mock):
    """Anything orbitd sends proves it alive, even when the heartbeat's own answer is late."""
    fast_heartbeat(client)
    mock.method_delay_s["status"] = 0.4
    flips = []
    client.responsiveChanged.connect(flips.append)
    media = next(a for a in mock.activities if a["kind"] == "media")
    for i in range(10):
        mock.update_activity(media["device"], media["id"], title=f"Song {i}")
        qtbot.wait(60)
    assert flips == []
