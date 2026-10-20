"""Runs the real orbit-applet UI (TrayController + Panel + DaemonClient) offscreen and records
what the user would see.

    python applet_probe.py SOCKET OUTDIR [--theme light|dark]

Every 100 ms the visible state (header, empty state, cards with stale/error/pending, device
rows, pairing dialogs, tray tooltip, menu head) is diffed and appended to OUTDIR/ui.jsonl as
{"t": <s since start>, ...}. Commands are read from OUTDIR/cmd (one per line, appended):
    shot NAME              save the panel to OUTDIR/NAME.png
    devices | activities   switch panel page
    click ACTIVITY ACTION  press an action button on a card
    devbtn DEVICE TEXT     press a device-row button (Connect, Disconnect, Forget, Pair)
    accept | reject        answer the first pairing dialog
    quit
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QToolButton  # noqa: E402

from orbit.applet import theme as themes  # noqa: E402
from orbit.applet.client import DaemonClient  # noqa: E402
from orbit.applet.model import now_ms  # noqa: E402
from orbit.applet.tray import TrayController  # noqa: E402


def main() -> int:
    import faulthandler
    import signal as _signal

    faulthandler.register(_signal.SIGUSR1, all_threads=True)
    sock, out = sys.argv[1], Path(sys.argv[2])
    theme_name = sys.argv[sys.argv.index("--theme") + 1] if "--theme" in sys.argv else "light"
    out.mkdir(parents=True, exist_ok=True)
    cmd_file = out / "cmd"
    cmd_file.touch()
    app = QApplication(["orbit-applet-probe"])
    app.setQuitOnLastWindowClosed(False)  # as orbit-applet does
    app.setStyle("Fusion")
    t = themes.DARK if theme_name == "dark" else themes.LIGHT
    app.setPalette(themes.palette(t))
    app.setStyleSheet(themes.stylesheet(t))
    client = DaemonClient(sock)
    tray = TrayController(client, t)
    tray.panel.popup = False  # offscreen windows never become active; keep it from auto-hiding
    client.start()
    t0 = time.monotonic()
    last: dict = {}
    offset = 0
    log = open(out / "ui.jsonl", "a")  # noqa: SIM115

    def snapshot() -> dict:
        p = tray.panel
        ap = p.activities_page
        cards = []
        for (dev, aid), c in ap.cards.items():
            cards.append({
                "id": aid,
                "stale": c.stale,
                "error": c.error.text() if not c.error.isHidden() else None,
                "errorRole": c.error.property("role") if not c.error.isHidden() else None,
                "pending": sorted(c._pending),
                "enabled": {k: b.isEnabled() for k, b in c._buttons.items()},
            })
        rows = []
        for did, r in p.devices_page._rows.items():
            rows.append({"id": did[:8], "status": r.status.fullText(),
                         "buttons": {k: (b.text(), b.isEnabled()) for k, b in r.buttons.items() if _alive(b)}})
        msg = p.devices_page.message
        tray._rebuild_menu()
        dialogs = [{"headline": d.headline.text(), "state": d.state.text(), "accept": d.accept_btn.isEnabled()}
                   for d in tray.pairing.dialogs.values()]
        return {
            "connected": client.connected,
            "header": p.status.fullText(),
            "empty": [ap.empty.headline.text(), ap.empty.hint.text()] if ap.stack.currentWidget() is ap.empty else None,
            "cards": cards,
            "rows": rows,
            "devicesMessage": msg.text() if not msg.isHidden() else None,
            "tooltip": tray.tray.toolTip(),
            "menuHead": tray.menu.actions()[0].text() if tray.menu.actions() else None,
            "dialogs": dialogs,
        }

    def poll() -> None:
        try:
            _poll()
        except Exception:
            import traceback

            with open(out / "probe-errors.log", "a") as f:
                traceback.print_exc(file=f)

    def _poll() -> None:
        nonlocal last, offset
        ap = tray.panel.activities_page
        ap.tick(now_ms())
        snap = snapshot()
        if snap != last:
            last = snap
            log.write(json.dumps({"t": round(time.monotonic() - t0, 2), "m": time.monotonic(), **snap}) + "\n")
            log.flush()
        data = cmd_file.read_text()
        new, offset = data[offset:], len(data)
        for line in new.splitlines():
            run(line.split())

    def run(cmd: list[str]) -> None:
        if not cmd:
            return
        p = tray.panel
        if cmd[0] == "shot":
            p.activities_page.tick(now_ms())
            p.grab().save(str(out / f"{cmd[1]}.png"))
        elif cmd[0] == "devices":
            p.show_devices()
        elif cmd[0] == "activities":
            p.show_activities()
        elif cmd[0] == "click":
            for (dev, aid), c in p.activities_page.cards.items():
                if aid == cmd[1]:
                    btn = c.findChild(QToolButton, f"action:{cmd[2]}")
                    if btn is not None:
                        btn.click()
                    else:
                        c.trigger(cmd[2])
        elif cmd[0] == "devbtn":
            for did, r in p.devices_page._rows.items():
                if did.startswith(cmd[1]) and cmd[2] in r.buttons:
                    r.buttons[cmd[2]].click()
        elif cmd[0] in ("accept", "reject"):
            for d in tray.pairing.dialogs.values():
                d.respond(cmd[0] == "accept")
                break
        elif cmd[0] == "quit":
            app.quit()

    tray.show_panel()
    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(100)
    code = app.exec()
    client.stop()
    return code


def _alive(obj) -> bool:
    from shiboken6 import isValid

    return isValid(obj)


if __name__ == "__main__":
    sys.exit(main())
