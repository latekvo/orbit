"""Render the applet's main states to PNGs against the mock daemon (for visual review).

    uv run python tests/applet/render_screenshots.py OUT_DIR [--platform cocoa|xcb]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--platform", default="offscreen",
                    help="QT_QPA_PLATFORM; a native one puts the windows on the real screen")
    args = ap.parse_args()
    if args.platform:
        os.environ["QT_QPA_PLATFORM"] = args.platform

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from mock_orbitd import PIXEL_7A, PIXEL_8, MockOrbitd
    from orbit.applet import theme as themes
    from orbit.applet.client import DaemonClient
    from orbit.applet.model import now_ms
    from orbit.applet.pairing import PairingDialog
    from orbit.applet.panel import Panel

    app = QApplication(["render"])
    app.setStyle("Fusion")
    args.out.mkdir(parents=True, exist_ok=True)

    def wait(ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def save(widget, name: str) -> None:
        widget.activities_page.tick(now_ms())
        path = args.out / f"{name}.png"
        widget.grab().save(str(path))
        print(path)

    for scenario in ("demo", "stale", "crowded", "idle", "empty", "down"):
        sock = str(Path(tempfile.mkdtemp(prefix="orb", dir="/tmp")) / "s.sock")
        mock = MockOrbitd(sock, scenario={"down": "empty", "stale": "demo"}.get(scenario, scenario), evolve=False)
        mock.reconnect_hold_s = 0.2
        if scenario != "down":
            mock.start()
        for t in (themes.LIGHT, themes.DARK):
            mode = "dark" if t.dark else "light"
            app.setPalette(themes.palette(t))
            app.setStyleSheet(themes.stylesheet(t))
            client = DaemonClient(sock)
            client.start()
            panel = Panel(client, t, popup=False)
            panel.show()
            wait(1200)
            if scenario == "stale":
                mock.drop_device(PIXEL_8)
                wait(400)
                save(panel, f"stale-{mode}")
                mock.restore_device(PIXEL_8)
                wait(300)
                # The link drops just as play/pause is clicked: the action's result is the one in-card line.
                media = next(c for c in panel.activities_page.cards.values() if c.activity["kind"] == "media")
                mock.drop_device(PIXEL_8)
                media.trigger("media.pause")
                wait(800)
                save(panel, f"stale-notice-{mode}")
                panel.show_devices()
                wait(200)
                save(panel, f"stale-devices-{mode}")
                mock.restore_device(PIXEL_8)
                panel.close()
                client.stop()
                continue
            save(panel, f"{scenario}-{mode}")
            if scenario == "demo":
                media = next(c for c in panel.activities_page.cards.values() if c.activity["kind"] == "media")
                mock.fail_next_action = "phone is locked"
                media.trigger("media.next")
                wait(300)
                save(panel, f"action-error-{mode}")
                media.trigger("media.pause" if mode == "light" else "media.play")
                wait(300)
                save(panel, f"toggled-{mode}")
                mock.action_delay_s = 3
                media.trigger("media.next")
                download = next(c for c in panel.activities_page.cards.values() if c.activity["kind"] == "progress")
                download.trigger(download.activity["actions"][0]["id"])
                wait(400)
                save(panel, f"pending-{mode}")
                mock.action_delay_s = 0
                wait(3000)
                bar = panel.activities_page.scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
                wait(100)
                save(panel, f"demo-scrolled-{mode}")
                panel.show_devices()
                wait(200)
                save(panel, f"devices-{mode}")
                pairing = {"session": "s1", "deviceId": PIXEL_7A, "name": "Pixel 7a", "sas": "042917",
                           "localAccepted": False, "remoteAccepted": True}
                dlg = PairingDialog(client, t, pairing)
                dlg.show()
                wait(200)
                path = args.out / f"pairing-{mode}.png"
                dlg.grab().save(str(path))
                print(path)
                dlg.done(0)
            panel.close()
            client.stop()
        if scenario != "down":
            mock.stop()


if __name__ == "__main__":
    main()
