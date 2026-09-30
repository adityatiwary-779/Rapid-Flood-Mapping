import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
os.environ["BACKEND_MODE"] = "mock"          # must be set before api.index is imported


class NullQueue:
    """Keeps jobs 'queued' forever (used to test rate limits / active-job locks)."""
    def __init__(self):
        self.jobs = []

    def enqueue(self, job_id, delay=0):
        self.jobs.append(job_id)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """make_client(**env) -> (TestClient, services). Fresh in-memory store + storage every time."""
    from fastapi.testclient import TestClient
    from common import services
    from common.config import load_settings

    def make(null_queue=False, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        monkeypatch.setenv("MOCK_DIR", str(tmp_path / "store"))
        monkeypatch.setenv("POLL_SECONDS", "1")
        svc = services.build_services(load_settings())
        if null_queue:
            svc.queue = NullQueue()
        services.set_services(svc)
        import api.index as index
        return TestClient(index.app), svc
    yield make
    svc = services._services
    if svc is not None and hasattr(svc.queue, "join"):
        svc.queue.join()                     # never leave a background job running into the next test
    services.set_services(None)


def body(name="Alappuzha", pre=("2018-07-01", "2018-07-30"), post=("2018-08-15", "2018-08-25"), parent="Kerala"):
    return {"region": {"country": "India", "level": 2, "parent": parent, "name": name},
            "pre": list(pre), "post": list(post)}


def wait_done(client, job_id, timeout=40):
    seen, t0 = [], time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        seen.append(j)
        if j["status"] in ("done", "failed"):
            return j, seen
        time.sleep(0.15)
    raise AssertionError(f"job did not finish: {seen[-1]}")
