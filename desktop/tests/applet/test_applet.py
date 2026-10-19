import time

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QSlider, QToolButton

from conftest import make_mock
from orbit.applet import model
from orbit.applet.cards import ActivityCard
from orbit.applet.client import DaemonClient
from orbit.applet.pairing import PairingManager
from orbit.applet.panel import Panel, compute_panel_position
from orbit.applet.theme import LIGHT

# -- pure helpers ----------------------------------------------------------------------------


def test_media_position_extrapolates_only_while_playing():
    m = {"state": "playing", "positionMs": 61000, "positionAtMs": 1000, "durationMs": 215000, "rate": 1.0}
    assert model.media_position_ms(m, 3500) == 63500
    assert model.media_position_ms({**m, "rate": 2.0}, 3500) == 66000
    assert model.media_position_ms({**m, "state": "paused"}, 999999) == 61000
    assert model.media_position_ms(m, 10_000_000) == 215000  # clamped to duration


def test_chrono_values():
    assert model.chrono_value_ms({"baseMs": 1000, "countDown": False, "running": True}, 6000) == 5000
    assert model.chrono_value_ms({"baseMs": 10000, "countDown": True, "running": True}, 4000) == 6000
    assert model.chrono_value_ms({"baseMs": 10000, "countDown": True, "running": False, "valueMs": 42000}, 0) == 42000


@pytest.mark.parametrize("ms,text", [(0, "0:00"), (61999, "1:01"), (3_725_000, "1:02:05"), (-5000, "-0:05")])
def test_format_clock(ms, text):
    assert model.format_clock(ms) == text


def test_parse_host_port():
    assert model.parse_host_port(" 192.168.1.20:47820 ") == ("192.168.1.20", 47820)
    assert model.parse_host_port("[fe80::1]:9") == ("fe80::1", 9)
    for bad in ("", "host", "host:", ":80", "host:0", "host:70000", "host:abc"):
        with pytest.raises(ValueError):
            model.parse_host_port(bad)


def test_default_socket_path():
    assert str(model.default_socket_path({"ORBIT_SOCKET": "/x.sock"}, "linux")) == "/x.sock"
    assert str(model.default_socket_path({"HOME": "/h"}, "darwin")) == "/h/Library/Application Support/orbit/orbitd.sock"
    assert str(model.default_socket_path({"HOME": "/h", "XDG_RUNTIME_DIR": "/run/u"}, "linux")) == "/run/u/orbit/orbitd.sock"
    assert str(model.default_socket_path({"HOME": "/h"}, "linux")) == "/h/.local/state/orbit/orbitd.sock"


def test_panel_position_below_top_anchor_above_bottom_anchor_and_clamped():
    screen = QRect(0, 25, 1440, 875)  # available area under a 25 px menu bar
    size = QSize(384, 500)
    below = compute_panel_position(QRect(1000, 0, 30, 24), size, screen)
    assert below == QPoint(1014 - 192, 25 + 4)  # clamped under the menu bar
    above = compute_panel_position(QRect(700, 870, 24, 30), QSize(384, 300), QRect(0, 0, 1440, 870))
    assert above.y() + 300 <= 870 and above.y() > 500
    right = compute_panel_position(QRect(1430, 0, 10, 24), size, screen)
    assert right.x() == 1440 - 8 - 384


# -- helpers ---------------------------------------------------------------------------------


def dev_id(mock, name):
    return next(d["id"] for d in mock.devices if d["name"] == name)


def activity(mock, kind, device=None):
    return next(a for a in mock.activities if a["kind"] == kind and (device is None or a["device"] == device))


def make_panel(qtbot, client):
    panel = Panel(client, LIGHT, popup=False)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def card_for(panel, a) -> ActivityCard:
    return panel.activities_page.cards[(a["device"], a["id"])]


def button(card, action_id) -> QToolButton:
    return card.findChild(QToolButton, f"action:{action_id}")


# -- cards -----------------------------------------------------------------------------------


def test_cards_render_daemon_data_grouped_by_device(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    page = panel.activities_page
    assert {(a["device"], a["id"]) for a in mock.activities} == set(page.cards)

    media = activity(mock, "media")
    card = card_for(panel, media)
    assert card.title.fullText() == media["title"]
    assert card.subtitle.fullText() == media["subtitle"]
    assert card.app_name.fullText() == media["app"]["name"]
    assert card.seek.maximum() == media["media"]["durationMs"]
    assert card.duration.text() == model.format_clock(media["media"]["durationMs"])
    qtbot.waitUntil(lambda: card.art.has_pixmap() and card.app_icon.has_pixmap(), timeout=3000)
    assert card.art.isVisibleTo(panel)

    dl = activity(mock, "progress")
    dl_card = card_for(panel, dl)
    assert dl_card.progress.maximum() == dl["progress"]["max"]
    assert dl_card.progress.value() == dl["progress"]["value"]
    assert dl_card.progress_text.text() == f"{round(100 * dl['progress']['value'] / dl['progress']['max'])}%"
    assert not dl_card.seek_box.isVisibleTo(panel)

    # Two devices have activities -> one section header per device, in order.
    headers = [w.text() for w in page.body.findChildren(QLabel, "groupHeader") if w.isVisibleTo(panel)]
    assert headers == ["Pixel 8", "Galaxy Tab S9"]
    assert "Pixel 8" in panel.status.fullText()


def test_single_device_has_no_group_headers(qtbot, client, mock):
    tab = dev_id(mock, "Galaxy Tab S9")
    mock.set_activities([a for a in mock.activities if a["device"] != tab])
    panel = make_panel(qtbot, client)
    qtbot.waitUntil(lambda: all(k[0] != tab for k in panel.activities_page.cards))
    assert not [w for w in panel.activities_page.body.findChildren(QLabel, "groupHeader") if w.isVisibleTo(panel)]


def test_indeterminate_progress(qtbot, client, mock):
    dl = activity(mock, "progress")
    mock.update_activity(dl["device"], dl["id"], progress={"value": 0, "max": 0, "indeterminate": True})
    panel = make_panel(qtbot, client)
    card = card_for(panel, dl)
    qtbot.waitUntil(lambda: card.progress.maximum() == 0)
    assert card.progress.minimum() == 0 and not card.progress_text.isVisibleTo(panel)


def test_pause_button_sends_exact_action_and_swaps_to_play(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = activity(mock, "media")
    card = card_for(panel, media)
    mock.clear_recorded()
    qtbot.mouseClick(button(card, "media.pause"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(mock.actions()) == 1)
    assert mock.actions() == [{"device": media["device"], "activity": media["id"], "action": "media.pause"}]
    qtbot.waitUntil(lambda: button(card, "media.play") is not None)
    assert button(card, "media.pause") is None


def test_text_action_buttons_send_their_ids(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    timer = activity(mock, "timer")
    card = card_for(panel, timer)
    plus = next(x for x in timer["actions"] if x.get("icon") == "add")
    btn = button(card, plus["id"])
    assert btn.text() == plus["label"]
    mock.clear_recorded()
    qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: mock.actions() == [{"device": timer["device"], "activity": timer["id"], "action": plus["id"]}])


def test_seek_click_sends_position_at_click(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = activity(mock, "media")
    card = card_for(panel, media)
    slider: QSlider = card.seek
    mock.clear_recorded()
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() // 2, slider.height() // 2))
    qtbot.waitUntil(lambda: len(mock.actions()) == 1)
    sent = mock.actions()[0]
    assert sent["action"] == "media.seek" and sent["device"] == media["device"] and sent["activity"] == media["id"]
    assert isinstance(sent["value"], int)
    duration = media["media"]["durationMs"]
    assert abs(sent["value"] - duration / 2) < duration * 0.05


def test_seek_drag_release_sends_exact_value(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = activity(mock, "media")
    card = card_for(panel, media)
    mock.clear_recorded()
    card.seek.setSliderDown(True)
    card.seek.setValue(123456)
    card.tick(model.now_ms())
    assert card.elapsed.text() == "2:03"  # follows the handle while dragging
    assert mock.actions() == []
    card.seek.setSliderDown(False)
    qtbot.waitUntil(lambda: len(mock.actions()) == 1)
    assert mock.actions() == [{"device": media["device"], "activity": media["id"], "action": "media.seek",
                               "value": 123456}]
    qtbot.waitUntil(lambda: activity(mock, "media")["media"]["positionMs"] == 123456)


def test_reply_sends_text_and_clears_field(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    msg = next(a for a in mock.activities if any(x.get("input") for x in a.get("actions", [])))
    reply = next(x for x in msg["actions"] if x.get("input"))
    card = card_for(panel, msg)
    field = card.findChild(QLineEdit, f"input:{reply['id']}")
    assert field.placeholderText() == reply["input"]["label"]
    assert not button(card, reply["id"]).isEnabled()  # nothing to send yet
    mock.clear_recorded()
    qtbot.keyClicks(field, "See you at 7")
    qtbot.keyClick(field, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: len(mock.actions()) == 1)
    assert mock.actions() == [{"device": msg["device"], "activity": msg["id"], "action": reply["id"],
                               "value": "See you at 7"}]
    qtbot.waitUntil(lambda: field.text() == "")


def test_reply_draft_survives_unrelated_update(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    msg = next(a for a in mock.activities if any(x.get("input") for x in a.get("actions", [])))
    reply = next(x for x in msg["actions"] if x.get("input"))
    card = card_for(panel, msg)
    field = card.findChild(QLineEdit, f"input:{reply['id']}")
    qtbot.keyClicks(field, "half-typed")
    mock.update_activity(msg["device"], msg["id"], title="Alice (2)")
    qtbot.waitUntil(lambda: card.title.fullText() == "Alice (2)")
    assert card.findChild(QLineEdit, f"input:{reply['id']}").text() == "half-typed"


def test_action_failure_is_shown(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = activity(mock, "media")
    card = card_for(panel, media)
    mock.fail_next_action = "phone is locked"
    qtbot.mouseClick(button(card, "media.next"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: card.error.isVisibleTo(panel))
    assert "phone is locked" in card.error.text()
    assert button(card, "media.next").isEnabled()


def test_live_media_position_and_chrono_advance(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = activity(mock, "media")
    timer = activity(mock, "timer")
    mcard, tcard = card_for(panel, media), card_for(panel, timer)
    v0, c0 = mcard.seek.value(), model.chrono_value_ms(timer["chrono"], model.now_ms())
    t0 = tcard.chrono.text()
    qtbot.wait(1300)  # the panel's own ticker drives the update
    assert 1000 <= mcard.seek.value() - v0 <= 2000
    assert tcard.chrono.text() != t0
    assert tcard.chrono.text() == model.format_clock(model.chrono_value_ms(timer["chrono"], model.now_ms()))
    assert model.chrono_value_ms(timer["chrono"], model.now_ms()) < c0  # counting down

    # Paused media and a stopped clock freeze.
    pos = mcard.seek.value()
    mock.update_activity(media["device"], media["id"],
                         media={**media["media"], "state": "paused", "positionMs": 90000, "positionAtMs": model.now_ms()})
    mock.update_activity(timer["device"], timer["id"],
                         chrono={**timer["chrono"], "running": False, "valueMs": 300000})
    qtbot.waitUntil(lambda: mcard.seek.value() == 90000 and tcard.chrono.text() == "5:00")
    qtbot.wait(1200)
    assert mcard.seek.value() == 90000 and mcard.elapsed.text() == "1:30" and tcard.chrono.text() == "5:00"
    assert pos != 90000


def test_daemon_disconnect_and_reconnect(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    n = len(panel.activities_page.cards)
    assert n > 0
    mock.stop()
    qtbot.waitUntil(lambda: not client.connected, timeout=3000)
    assert panel.activities_page.cards == {}
    assert panel.activities_page.empty.headline.text() == "orbitd is not running"
    assert "not running" in panel.status.fullText()
    assert client.action("x", "y", "media.play") is None
    mock.start()
    qtbot.waitUntil(lambda: client.connected and len(panel.activities_page.cards) == n, timeout=8000)
    assert "Pixel 8" in panel.status.fullText()


def test_pending_requests_fail_on_disconnect(qtbot, client, mock):
    mock.action_delay_s = 5
    results = []
    media = activity(mock, "media")
    client.action(media["device"], media["id"], "media.next", callback=lambda r, e: results.append((r, e)))
    qtbot.wait(200)
    mock.stop()
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == [(None, {"code": "not_connected", "message": "orbitd is not running"})]


def test_starts_without_daemon_then_connects(qtbot, sock_path):
    c = DaemonClient(sock_path)
    c.start()
    qtbot.wait(700)
    assert not c.connected
    m = make_mock(sock_path)
    try:
        qtbot.waitUntil(lambda: bool(c.connected and c.devices), timeout=8000)
        assert c.status.get("version")
    finally:
        c.stop()
        m.stop()


@pytest.mark.parametrize("scenario,headline,button_text", [
    ("empty", "No phone paired yet", "Pair a device"),
    ("idle", "Nothing live right now", None),
])
def test_empty_states(qtbot, sock_path, scenario, headline, button_text):
    m = make_mock(sock_path, scenario=scenario)
    c = DaemonClient(sock_path)
    c.start()
    try:
        panel = make_panel(qtbot, c)
        qtbot.waitUntil(lambda: c.connected)
        qtbot.waitUntil(lambda: panel.activities_page.empty.headline.text() == headline)
        btn = panel.activities_page.empty.button
        assert btn.isVisibleTo(panel) == bool(button_text)
        if button_text:
            assert btn.text() == button_text
            qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
            assert panel.pages.currentWidget() is panel.devices_page
    finally:
        c.stop()
        m.stop()


# -- devices ---------------------------------------------------------------------------------


def device_row_button(panel, name, text):
    for b in panel.devices_page.findChildren(QPushButton, f"device:{text.lower()}"):
        if b.parent().device.get("name") == name and b.isVisibleTo(panel):
            return b
    return None


def test_devices_page_lists_and_acts(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    panel.show_devices()
    statuses = {w.parent().device["name"]: w.fullText() for w in panel.devices_page.findChildren(QLabel, "deviceStatus")}
    assert statuses["Pixel 8"] == "Connected via Wi-Fi"
    assert statuses["Galaxy Tab S9"] == "Connected via Bluetooth"
    assert statuses["Work Phone"] == "Offline"
    assert statuses["Pixel 7a"].startswith("Nearby")
    assert device_row_button(panel, "Pixel 7a", "Forget") is None

    mock.clear_recorded()
    qtbot.mouseClick(device_row_button(panel, "Pixel 7a", "Pair"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: any(r["method"] == "devices.connect" for r in mock.requests))
    assert [r for r in mock.requests if r["method"] == "devices.connect"] == [
        {"method": "devices.connect", "params": {"id": dev_id(mock, "Pixel 7a")}}]

    # Forget needs a confirming second click.
    mock.clear_recorded()
    work = dev_id(mock, "Work Phone")
    qtbot.mouseClick(device_row_button(panel, "Work Phone", "Forget"), Qt.MouseButton.LeftButton)
    qtbot.wait(300)
    assert not any(r["method"] == "devices.forget" for r in mock.requests)
    qtbot.mouseClick(device_row_button(panel, "Work Phone", "Forget"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: {"method": "devices.forget", "params": {"id": work}} in mock.requests)
    qtbot.waitUntil(lambda: "Work Phone" not in {
        w.parent().device["name"] for w in panel.devices_page.findChildren(QLabel, "deviceStatus")})

    mock.clear_recorded()
    tab = dev_id(mock, "Galaxy Tab S9")
    qtbot.mouseClick(device_row_button(panel, "Galaxy Tab S9", "Disconnect"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: {"method": "devices.disconnect", "params": {"id": tab}} in mock.requests)
    qtbot.waitUntil(lambda: device_row_button(panel, "Galaxy Tab S9", "Connect") is not None)


def test_add_by_address(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    panel.show_devices(focus_address=True)
    page = panel.devices_page
    mock.clear_recorded()
    qtbot.keyClicks(page.address, "nonsense")
    qtbot.keyClick(page.address, Qt.Key.Key_Return)
    assert page.message.isVisibleTo(panel) and "192.168.1.20:47820" in page.message.text()
    qtbot.wait(200)
    assert not any(r["method"] == "devices.connect" for r in mock.requests)

    page.address.clear()
    qtbot.keyClicks(page.address, "10.0.0.5:47820")
    qtbot.mouseClick(page.add_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: {"method": "devices.connect", "params": {"host": "10.0.0.5", "port": 47820}}
                    in mock.requests)
    qtbot.waitUntil(lambda: page.address.text() == "")

    page.address.setText("unreachable.invalid:47820")
    qtbot.mouseClick(page.add_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "connection refused" in page.message.text())
    assert page.address.text() == "unreachable.invalid:47820"


# -- pairing ---------------------------------------------------------------------------------


def pending(mock, name="Pixel 7a", session="sess-1", sas="042917", **kw):
    return {"session": session, "deviceId": dev_id(mock, name), "name": name, "sas": sas,
            "localAccepted": False, "remoteAccepted": True, **kw}


@pytest.mark.parametrize("accept", [True, False])
def test_pairing_dialog_responds(qtbot, client, mock, accept):
    mgr = PairingManager(client, LIGHT)
    mock.set_pairings([pending(mock)])
    qtbot.waitUntil(lambda: "sess-1" in mgr.dialogs)
    dlg = mgr.dialogs["sess-1"]
    assert dlg.code.text() == "042 917"
    assert "Pixel 7a" in dlg.headline.text()
    assert dlg.isVisible()
    mock.clear_recorded()
    qtbot.mouseClick(dlg.accept_btn if accept else dlg.cancel, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: any(r["method"] == "pairing.respond" for r in mock.requests))
    assert [r for r in mock.requests if r["method"] == "pairing.respond"] == [
        {"method": "pairing.respond", "params": {"session": "sess-1", "accept": accept}}]
    # The daemon resolves the pairing -> the dialog goes away.
    qtbot.waitUntil(lambda: "sess-1" not in mgr.dialogs)
    paired = next(d for d in mock.devices if d["name"] == "Pixel 7a")["paired"]
    assert paired is accept


def test_pairing_dialog_close_rejects_and_tracks_state(qtbot, client, mock):
    mgr = PairingManager(client, LIGHT)
    mock.set_pairings([pending(mock, remoteAccepted=False)])
    qtbot.waitUntil(lambda: "sess-1" in mgr.dialogs)
    dlg = mgr.dialogs["sess-1"]
    assert not dlg.state.isVisibleTo(dlg)
    mock.set_pairings([pending(mock, localAccepted=True, remoteAccepted=False)])
    qtbot.waitUntil(lambda: "Waiting" in dlg.state.text())
    assert not dlg.accept_btn.isEnabled()
    mock.clear_recorded()
    dlg.close()
    qtbot.waitUntil(lambda: {"method": "pairing.respond", "params": {"session": "sess-1", "accept": False}}
                    in mock.requests)
