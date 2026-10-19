"""`orbit-applet` entry point."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import logging
import signal
import sys
from pathlib import Path

log = logging.getLogger("orbit.applet")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from .model import default_socket_path

    p = argparse.ArgumentParser(prog="orbit-applet", description="Orbit tray applet (client of orbitd)")
    p.add_argument("--socket", type=Path, default=None,
                   help=f"orbitd socket (default: $ORBIT_SOCKET or {default_socket_path()})")
    p.add_argument("--theme", choices=["auto", "light", "dark"], default="auto")
    p.add_argument("--show-panel", action="store_true", help="open the panel on startup (debug)")
    p.add_argument("--screenshot", type=Path, metavar="PNG",
                   help="debug: once data has loaded, save the panel to PNG and quit")
    p.add_argument("--quit-after", type=float, metavar="SECONDS", help="debug: quit after N seconds")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args(argv)


def hide_dock_icon() -> bool:
    """macOS: run as an accessory app (menu-bar only, no Dock icon or app switcher entry)."""
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = objc.objc_msgSend
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        app = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        set_policy = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
            ctypes.cast(send, ctypes.c_void_p).value)
        get_policy = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(
            ctypes.cast(send, ctypes.c_void_p).value)
        set_policy(app, objc.sel_registerName(b"setActivationPolicy:"), 1)  # Accessory
        return get_policy(app, objc.sel_registerName(b"activationPolicy")) == 1
    except (OSError, AttributeError, TypeError) as exc:
        log.warning("could not hide the Dock icon: %s", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose > 1 else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from . import theme as themes
    from .client import DaemonClient
    from .model import default_socket_path
    from .tray import TrayController

    QApplication.setApplicationName("Orbit")
    QApplication.setDesktopFileName("orbit-applet")
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    if sys.platform == "darwin":
        log.info("dock icon hidden: %s", hide_dock_icon())

    def apply_theme():
        t = themes.resolve(args.theme)
        app.setPalette(themes.palette(t))
        app.setStyleSheet(themes.stylesheet(t))
        return t

    theme = apply_theme()
    socket_path = args.socket or default_socket_path()
    log.info("platform=%s, theme=%s, socket=%s", app.platformName(), "dark" if theme.dark else "light", socket_path)

    client = DaemonClient(str(socket_path))
    tray = TrayController(client, theme)
    tray.show()

    def on_scheme_changed(*_):
        t = apply_theme()
        if t != tray.theme:
            tray.set_theme(t)

    QGuiApplication.styleHints().colorSchemeChanged.connect(on_scheme_changed)
    client.start()

    if args.show_panel:
        QTimer.singleShot(0, tray.show_panel)
    if args.screenshot:
        _schedule_screenshot(app, client, tray, args.screenshot)
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), app.quit)

    # Let Python see SIGINT/SIGTERM while Qt's loop runs.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(200)

    def shutdown():
        log.info("quitting")
        client.stop()
        tray.hide()

    app.aboutToQuit.connect(shutdown)
    code = app.exec()
    log.info("exited cleanly (code %d)", code)
    return code


def _schedule_screenshot(app, client, tray, path: Path) -> None:
    from PySide6.QtCore import QTimer

    from .model import now_ms

    def grab():
        tray.panel.activities_page.tick(now_ms())
        path.parent.mkdir(parents=True, exist_ok=True)
        ok = tray.panel.grab().save(str(path))
        log.info("screenshot saved=%s to %s (%dx%d)", ok, path, tray.panel.width(), tray.panel.height())
        app.exit(0 if ok else 1)

    # Wait for the first data and art to arrive (or for the connection to fail) before grabbing.
    QTimer.singleShot(2500, grab)


if __name__ == "__main__":
    sys.exit(main())
