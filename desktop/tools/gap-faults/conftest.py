import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harness import Lab, Rig, build_image  # noqa: E402

RESULTS = Path(__file__).parent / "results"


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: takes minutes")


@pytest.fixture(scope="session", autouse=True)
def image():
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode:
        pytest.skip("needs Docker")
    build_image()


@pytest.fixture
def lab(request):
    labs = []

    def make(n_nets=1, ipv6=False):
        lab = Lab.create(request.node.name.split("[")[0].removeprefix("test_")[:20].replace("_", "-"), n_nets, ipv6)
        labs.append(lab)
        return lab

    yield make
    for lab in labs:
        if request.node.rep_call.failed if hasattr(request.node, "rep_call") else False:
            for c in lab.containers:
                out = subprocess.run(["docker", "exec", c, "sh", "-c", "tail -n 60 /tmp/*.log"], capture_output=True, text=True)
                print(f"--- {c}\n{out.stdout}")
        lab.close()


@pytest.fixture
def rig(lab):
    def make(n_nets=1, ipv6=False, pair=True, **kw):
        r = Rig(lab(n_nets, ipv6), **kw)
        if pair:
            r.pair()
        return r

    return make


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


@pytest.fixture
def record(request):
    """record(**measured): saved to results/<test>.json and printed (shown with -s or on failure)."""
    data = {}

    def rec(**kw):
        data.update(kw)
        print("MEASURED", json.dumps(kw, default=str))

    yield rec
    RESULTS.mkdir(exist_ok=True)
    data["_outcome"] = getattr(getattr(request.node, "rep_call", None), "outcome", "?")
    data["_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (RESULTS / f"{request.node.name}.json").write_text(json.dumps(data, indent=1, default=str))
