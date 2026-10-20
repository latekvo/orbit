import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from harness import World  # noqa: E402


@pytest.fixture
async def world():
    w = World()
    yield w
    await w.close()


@pytest.fixture
async def paired(world):
    await world.start_phone()
    await world.start_daemon()
    await world.pair()
    return world
