import os
import shutil
import tempfile
from pathlib import Path

import pytest

from .harness import CLASSPATH_FILE, FaultProxy, JvmPhone, Orbitd

# Real processes and minutes of wall clock: opt in with ORBIT_CONNECTIVITY=1.
if not os.environ.get("ORBIT_CONNECTIVITY"):
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture
def workdir():
    path = Path(tempfile.mkdtemp(prefix="conn-jvm-", dir="/tmp"))
    yield path
    if not os.environ.get("ORBIT_KEEP_LOGS"):
        shutil.rmtree(path, ignore_errors=True)
    else:
        print(f"logs kept in {path}")


@pytest.fixture
async def rig(workdir):
    """Factory: started JvmPhone + FaultProxy + orbitd; everything is stopped at teardown."""
    if not CLASSPATH_FILE.exists():
        pytest.skip("build the JVM harness first: cd android/companion && ./gradlew :protocol:harnessClasspath")
    started = []

    async def make(phone_opts=None, laptop_kwargs=None, laptop=True):
        phone = JvmPhone(workdir, **(phone_opts or {}))
        await phone.start()
        started.append(phone)
        proxy = FaultProxy(phone.port)
        await proxy.start()
        started.append(proxy)
        orbitd = None
        if laptop:
            orbitd = Orbitd(workdir, **(laptop_kwargs or {}))
            await orbitd.start()
            started.append(orbitd)
        return phone, proxy, orbitd

    yield make
    for thing in reversed(started):
        await thing.stop()
