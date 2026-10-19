"""Cards with many/long actions and texts (as real phones send them) must fit and stay usable."""

import copy
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QToolButton, QWidget

from conftest import make_mock
from orbit.applet import actions as acts
from orbit.applet import icons
from orbit.applet.client import DaemonClient
from orbit.applet.panel import Panel
from orbit.applet.theme import LIGHT

LIVE = json.loads((Path(__file__).parent / "fixtures" / "live_activities.json").read_text())
LIVE_DEVICE = {"id": LIVE[0]["device"], "name": "OPPO Find X5", "platform": "android", "paired": True,
               "connected": True, "transport": "lan", "address": "192.168.1.50:47820", "discovered": ["lan"]}


# -- pure rules ------------------------------------------------------------------------------

@pytest.mark.parametrize("action,glyph", [
    ({"id": "media.custom.TOGGLE_LIKE", "label": "Remove like"}, "like"),
    ({"id": "media.custom.TOGGLE_LIKE", "label": "Like"}, "like"),
    ({"id": "media.custom.THUMBS_DOWN", "label": "Dislike"}, "dislike"),
    ({"id": "media.custom.TOGGLE_DISLIKE", "label": "Remove dislike"}, "dislike"),
    ({"id": "media.custom.TOGGLE_SHUFFLE", "label": "Shuffle on", "icon": "shuffle"}, "shuffle"),
    ({"id": "media.custom.TOGGLE_REPEAT_MODE", "label": "Repeat mode one"}, "repeat"),
    ({"id": "media.custom.TOGGLE_START_RADIO", "label": "Start radio"}, "radio"),
    ({"id": "media.custom.ADD_TO_TARGET_PLAYLIST", "label": "Quick-add destination"}, "playlist_add"),
    ({"id": "media.custom.ADD_TO_LIBRARY", "label": "Save"}, "add"),
    ({"id": "media.custom.X", "label": "Whatever", "icon": "like"}, "like"),  # icon hint when id is opaque
    ({"id": "open", "label": "Open"}, "open"),
    ({"id": "media.pause", "label": "Pause", "icon": "pause"}, "pause"),
    ({"id": "notif.0", "label": "Mark as read"}, "check"),
    ({"id": "notif.1", "label": "Archive this chat"}, None),
    ({"id": "notif.3", "label": "Call back now"}, None),
    ({"id": "notif.2", "label": "Unknown", "icon": "sparkles"}, None),
])
def test_icon_for(action, glyph):
    assert acts.icon_for(action) == glyph
    if glyph:
        assert icons.svg_for(glyph, "#000000") is not None


@pytest.mark.parametrize("label,state", [
    ("Remove like", "active"), ("Unlike", "active"), ("Liked", "active"), ("Like", "inactive"),
    ("Toggle like", None),
])
def test_like_state(label, state):
    assert acts.toggle_state({"id": "media.custom.TOGGLE_LIKE", "label": label}) == state


@pytest.mark.parametrize("aid,label,state", [
    ("media.custom.TOGGLE_SHUFFLE", "Shuffle on", "active"),
    ("media.custom.TOGGLE_SHUFFLE", "Shuffle off", "inactive"),
    ("media.custom.TOGGLE_SHUFFLE", "Shuffle", None),
    ("media.custom.TOGGLE_REPEAT_MODE", "Repeat mode off", "inactive"),
    ("media.custom.TOGGLE_REPEAT_MODE", "Repeat mode one", "one"),
    ("media.custom.TOGGLE_REPEAT_MODE", "Repeat mode all", "active"),
    ("media.custom.TOGGLE_START_RADIO", "Start radio", None),  # not a toggle glyph
])
def test_mode_state(aid, label, state):
    assert acts.toggle_state({"id": aid, "label": label}) == state


# -- layout helpers --------------------------------------------------------------------------

def setup(qtbot, sock_path, activities, devices=None, scenario="empty"):
    mock = make_mock(sock_path, scenario=scenario)
    if devices is not None:
        mock.set_devices(devices)
    if activities is not None:
        mock.set_activities(activities)
    client = DaemonClient(sock_path)
    client.start()
    panel = Panel(client, LIGHT, popup=False)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: bool(panel.activities_page.cards), timeout=5000)
    qtbot.wait(100)  # let layouts settle
    return mock, client, panel


def assert_fits(panel):
    """No visible widget sticks out of its card; no card sticks out of the scroll viewport."""
    viewport = panel.activities_page.scroll.viewport()
    vp_rect = QRect(QPoint(0, 0), viewport.size())
    assert panel.activities_page.body.width() <= viewport.width()
    for card in panel.activities_page.cards.values():
        top_left = card.mapTo(viewport, QPoint(0, 0))
        card_in_vp = QRect(top_left, card.size())
        assert card_in_vp.left() >= vp_rect.left() and card_in_vp.right() <= vp_rect.right(), card.activity["id"]
        for w in card.findChildren(QWidget):
            if not w.isVisibleTo(card) or w.width() == 0:
                continue
            r = QRect(w.mapTo(card, QPoint(0, 0)), w.size())
            assert card.rect().contains(r), (card.activity["id"], w.objectName() or type(w).__name__, r, card.rect())


def reach(card, action_id):
    """Fire `action_id` the way a user can: its visible button, else its overflow menu entry."""
    for btn in card.findChildren(QToolButton, f"action:{action_id}"):
        if btn.isVisibleTo(card):
            btn.click()
            return "button"
    for strip in (card.transport_strip, card.secondary_strip):
        if strip is not None and strip.more.isVisibleTo(card):
            for entry in strip.populate_menu():
                if entry.data() == action_id:
                    assert entry.isEnabled()
                    entry.trigger()
                    return "menu"
    raise AssertionError(f"{action_id} is not reachable")


def run_all_actions(qtbot, mock, card):
    a = card.activity
    for action in [x for x in a["actions"] if not x.get("input")]:
        mock.clear_recorded()
        btn = card._buttons.get(action["id"])
        how = reach(card, action["id"])
        qtbot.waitUntil(lambda: len(mock.actions()) == 1, timeout=3000)
        assert mock.actions() == [{"device": a["device"], "activity": a["id"], "action": action["id"]}], how
        if btn is not None:
            qtbot.waitUntil(btn.isEnabled)


# -- tests -----------------------------------------------------------------------------------

def test_live_metrolist_payload_fits_and_every_action_is_reachable(qtbot, sock_path):
    mock, client, panel = setup(qtbot, sock_path, copy.deepcopy(LIVE), devices=[LIVE_DEVICE])
    try:
        card = next(iter(panel.activities_page.cards.values()))
        assert_fits(panel)
        assert card.duration.isVisibleTo(card) and card.duration.text() == "4:12"
        # Transport is its own prominent row; nothing in it is a text pill.
        assert [b.property("actionId") for b in card.transport_strip.buttons()] == \
            ["media.previous", "media.pause", "media.next"]
        for aid in ("media.custom.TOGGLE_LIKE", "media.custom.TOGGLE_REPEAT_MODE", "media.custom.TOGGLE_SHUFFLE",
                    "media.custom.TOGGLE_START_RADIO", "media.custom.ADD_TO_TARGET_PLAYLIST", "media.stop"):
            btn = card.findChild(QToolButton, f"action:{aid}")
            assert btn is not None and btn.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
            assert btn.text() == "" and not btn.icon().isNull()
        run_all_actions(qtbot, mock, card)
    finally:
        client.stop()
        mock.stop()


def test_narrow_panel_moves_actions_into_overflow_menu(qtbot, sock_path):
    mock, client, panel = setup(qtbot, sock_path, copy.deepcopy(LIVE), devices=[LIVE_DEVICE])
    try:
        panel.setFixedWidth(250)
        qtbot.wait(150)
        card = next(iter(panel.activities_page.cards.values()))
        assert_fits(panel)
        assert card.secondary_strip.more.isVisibleTo(card)
        assert any(e.data() for e in card.secondary_strip.populate_menu())
        run_all_actions(qtbot, mock, card)
    finally:
        client.stop()
        mock.stop()


def test_crowded_scenario_fits_and_is_reachable(qtbot, sock_path):
    mock, client, panel = setup(qtbot, sock_path, None, scenario="crowded")
    try:
        qtbot.waitUntil(lambda: len(panel.activities_page.cards) == 3)
        qtbot.wait(100)
        assert_fits(panel)
        cards = {c.activity["kind"]: c for c in panel.activities_page.cards.values()}
        chat = cards["generic"]
        assert chat.title.toolTip() == chat.activity["title"]
        assert chat.title.text() != chat.activity["title"]  # elided
        assert chat.secondary_strip.more.isVisibleTo(chat)  # six long pills don't fit
        assert len([b for b in chat.secondary_strip.buttons() if b.isVisibleTo(chat)]) >= 3  # two rows
        assert chat.text.text().count("\n") == 2 and chat.text.text().endswith("\u2026")
        assert chat.text.toolTip() == chat.activity["text"]
        for card in cards.values():
            run_all_actions(qtbot, mock, card)
    finally:
        client.stop()
        mock.stop()


def test_metrolist_toggles_keep_their_button_when_relabelled(qtbot, sock_path):
    """Metrolist flips labels on every press; the button must stay put and only restyle."""
    mock, client, panel = setup(qtbot, sock_path, copy.deepcopy(LIVE), devices=[LIVE_DEVICE])
    try:
        card = next(iter(panel.activities_page.cards.values()))
        ids = ["media.custom.TOGGLE_LIKE", "media.custom.TOGGLE_SHUFFLE", "media.custom.TOGGLE_REPEAT_MODE"]

        def snapshot():
            out = {}
            for aid in ids:
                b = card.findChild(QToolButton, f"action:{aid}")
                out[aid] = (b, b.geometry(), b.toolButtonStyle(), b.text(), b.property("toggleState"),
                            b.icon().pixmap(20, 20).toImage(), b.toolTip())
            return out

        before = snapshot()
        assert [before[a][4] for a in ids] == ["active", "active", "inactive"]

        def relabel(labels):
            a = copy.deepcopy(mock.activities[0])
            for x in a["actions"]:
                x["label"] = labels.get(x["id"], x["label"])
            mock.update_activity(a["device"], a["id"], actions=a["actions"])
            qtbot.waitUntil(lambda: card.findChild(QToolButton, f"action:{ids[0]}").toolTip() == labels[ids[0]])

        relabel({ids[0]: "Like", ids[1]: "Shuffle off", ids[2]: "Repeat mode one"})
        after = snapshot()
        for aid in ids:
            btn, geo, style, text, _, img, _ = before[aid]
            assert after[aid][0] is btn  # same widget
            assert after[aid][1] == geo  # same slot and size
            assert after[aid][2] == Qt.ToolButtonStyle.ToolButtonIconOnly and after[aid][3] == "" == text
            assert after[aid][5] != img  # restyled
        assert [after[a][4] for a in ids] == ["inactive", "inactive", "one"]
        assert [after[a][6] for a in ids] == ["Like", "Shuffle off", "Repeat mode one"]

        relabel({ids[0]: "Remove like", ids[1]: "Shuffle on", ids[2]: "Repeat mode all"})
        final = snapshot()
        assert [final[a][4] for a in ids] == ["active", "active", "active"]
        assert all(final[a][0] is before[a][0] and final[a][1] == before[a][1] for a in ids)
        assert_fits(panel)
    finally:
        client.stop()
        mock.stop()


def test_pressing_a_toggle_against_the_mock_restyles_it(qtbot, sock_path):
    mock, client, panel = setup(qtbot, sock_path, None, scenario="crowded")
    try:
        card = next(c for c in panel.activities_page.cards.values() if c.activity["kind"] == "media")
        btn = card.findChild(QToolButton, "action:media.custom.TOGGLE_REPEAT_MODE")
        geo = btn.geometry()
        states = []
        for _ in range(3):
            prev = btn.property("toggleState")
            qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
            qtbot.waitUntil(lambda: btn.property("toggleState") != prev and btn.isEnabled())
            states.append(btn.property("toggleState"))
            assert card.findChild(QToolButton, "action:media.custom.TOGGLE_REPEAT_MODE") is btn
            assert btn.geometry() == geo
        assert states == ["one", "active", "inactive"]
    finally:
        client.stop()
        mock.stop()
