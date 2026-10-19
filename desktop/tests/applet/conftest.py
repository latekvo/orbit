import os
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from applet_helpers import make_mock  # noqa: E402


@pytest.fixture(scope="session")
def qapp_args():
    return ["orbit-applet-tests"]


@pytest.fixture(autouse=True)
def _styled(qapp):
    from orbit.applet import theme

    qapp.setStyle("Fusion")
    qapp.setStyleSheet(theme.stylesheet(theme.LIGHT))
    qapp.setPalette(theme.palette(theme.LIGHT))


@pytest.fixture
def sock_path():
    # AF_UNIX paths are limited to 104 bytes on macOS; pytest's tmp_path can be longer.
    d = tempfile.mkdtemp(prefix="orb", dir="/tmp")
    yield str(Path(d) / "orbitd.sock")
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mock(sock_path):
    m = make_mock(sock_path)
    yield m
    m.stop()


@pytest.fixture
def client(qtbot, mock, sock_path):
    from orbit.applet.client import DaemonClient

    c = DaemonClient(sock_path)
    c.start()
    qtbot.waitUntil(lambda: bool(c.connected and c.activities), timeout=5000)
    yield c
    c.stop()
