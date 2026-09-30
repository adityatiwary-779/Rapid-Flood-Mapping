"""Preset (precomputed) events: run once with the same worker code as custom jobs, stored under presets/<id>/."""
import json
import os
import threading
import time

from . import jobs

PRESETS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "presets.json")
_lock = threading.Lock()


def load_presets(path=PRESETS_FILE):
    with open(path) as f:
        return json.load(f)["presets"]


def _index(storage):
    try:
        return json.loads(storage.get_bytes("presets/index.json"))
    except FileNotFoundError:
        return {"events": []}


def run_preset(svc, preset, sleep=time.sleep, delay_scale=1.0, log=print):
    """Run one preset to completion in this process (blocking) and add it to presets/index.json."""
    from common.services import worker_deps
    from worker import runner
    spec = preset["spec"]
    job = {"id": preset["id"].replace("_", "")[:12].ljust(12, "0"), "key": "preset:" + preset["id"], "cid": None,
           "status": "queued", "stage": "queued", "message": "", "progress": 0, "spec": spec, "error": None,
           "created": time.time(), "updated": time.time(), "warnings": [], "result": None, "work": {},
           "kind": "preset", "preset_id": preset["id"], "prefix": f"presets/{preset['id']}",
           "title": f"{preset['event']} · {preset['district']}"}
    jobs.save_job(svc.store, job)
    deps = worker_deps(svc)
    while True:
        res = runner.step(job["id"], deps) or {}
        cur = jobs.get_job(svc.store, job["id"])
        log(f"  [{preset['id']}] {cur['status']} {cur['progress']}% {cur['message']}")
        if not res.get("again"):
            break
        sleep(res["again"] * delay_scale)
    cur = jobs.get_job(svc.store, job["id"])
    if cur["status"] != "done":
        raise RuntimeError(f"preset {preset['id']} failed: {cur['error']}")
    with _lock:
        idx = _index(svc.storage)
        idx["events"] = [e for e in idx["events"] if e["id"] != preset["id"]]
        idx["events"].append({"id": preset["id"], "event": preset["event"], "district": preset["district"],
                              "title": job["title"], "windows": {"pre": spec["pre"], "post": spec["post"]},
                              "mock": svc.settings.mode == "mock"})
        svc.storage.put_bytes("presets/index.json", json.dumps(idx).encode(), "application/json")


def seed_mock_presets(svc):
    """Mock mode only: make sure the demo has preset events to show."""
    with _lock:
        have = {e["id"] for e in _index(svc.storage)["events"]}
    for p in load_presets():
        if p["id"] not in have:
            run_preset(svc, p, sleep=lambda s: None, log=lambda *_: None)
