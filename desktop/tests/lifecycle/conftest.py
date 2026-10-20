"""Process-level session lifecycle scenarios (slow: real timeouts). Run with ORBIT_LIFECYCLE=1:

ORBIT_LIFECYCLE=1 uv run pytest tests/lifecycle -v -s
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Orbitd, Phone, Proc, cleanup, new_root  # noqa: E402

if os.environ.get("ORBIT_LIFECYCLE") != "1":
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture
async def lab(request):
    """Factory for processes in a private /tmp root; every process and API client is killed at teardown."""

    class Lab:
        def __init__(self) -> None:
            self.root = new_root(request.node.name[:40])
            self.procs: list[Proc] = []
            self.apis = []

        async def daemon(self, **kw) -> Orbitd:
            d = Orbitd(self.root, **kw)
            self.procs.append(d)
            await d.start()
            return d

        async def phone(self, **kw) -> Phone:
            p = Phone(self.root, **kw)
            self.procs.append(p)
            await p.start()
            return p

        async def api(self, d: Orbitd):
            a = await d.api()
            self.apis.append(a)
            return a

    lab = Lab()
    yield lab
    for a in lab.apis:
        try:
            await a.close()
        except Exception:
            pass
    for p in lab.procs:
        await p.stop()
    print(f"\n[lab logs kept in {lab.root}]" if request.node.rep_failed else "")
    if not request.node.rep_failed:
        cleanup(lab.root)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_failed = rep.failed
    elif not hasattr(item, "rep_failed"):
        item.rep_failed = rep.failed
