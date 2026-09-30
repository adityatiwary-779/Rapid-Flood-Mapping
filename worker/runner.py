"""Worker job logic as short steps: start -> (poll ... poll) -> finalize.

Each call to step() finishes quickly and returns {"again": seconds} when the job needs another step,
so no request ever waits on Earth Engine. Cloud Tasks (or the mock InlineQueue) re-invokes it.
"""
import json
import os
import tempfile
import time
import traceback
from dataclasses import dataclass

from common import jobs, manifest as mf

FAILED_STATES = ("FAILED", "CANCELLED", "CANCEL_REQUESTED")
TASK_NAMES = ("flood", "dvv_db", "stats", "flood_vec")


class JobError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Deps:
    store: object
    storage: object
    driver: object      # start(spec, job_id, prefix) -> dict ; poll(task_ids) -> dict
    settings: object
    roads_fn: object    # pipeline.roads_exposure.compute_exposure (or a fake)
    clock: object = time.time


def friendly_ee_error(msg):
    m = (msg or "").lower()
    if "timed out" in m or "memory limit" in m or "too many pixels" in m:
        return ("timed_out", "Earth Engine could not finish this region in time. Try a smaller "
                             "district or shorter date windows.")
    if "quota" in m or "rate limit" in m or "concurrent" in m:
        return ("quota", "The Earth Engine quota for this demo is exhausted right now. "
                          "Try again later.")
    if "empty" in m or "no features" in m:
        return ("no_flood", "No flooded area was detected for these dates, so there is nothing to export.")
    return ("export_failed", f"Earth Engine export failed: {(msg or 'unknown error')[:200]}")


def _fail(job, d, code, message):
    jobs.update_job(d.store, job["id"], status="failed", stage="failed", message=message,
                    error={"code": code, "message": message}, progress=job.get("progress", 0))
    if d.store.get(f"cache:{job['key']}") == job["id"]:
        d.store.delete(f"cache:{job['key']}")
    jobs.release_active(d.store, job)


def step(job_id, d):
    st = d.store
    job = jobs.get_job(st, job_id)
    if not job or job["status"] in ("done", "failed"):
        return {}
    if not st.set(f"lock:{job_id}", "1", ex=900, nx=True):
        return {"again": 15}          # another delivery of this job is running right now
    try:
        if job["stage"] == "queued":
            return _start(job, d)
        if job["stage"] == "exporting":
            return _poll(job, d)
        return {}
    except JobError as e:
        _fail(job, d, e.code, e.message)
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"job {job_id} crashed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        code, msg = friendly_pipeline_error(e)
        _fail(job, d, code, msg)
        return {}
    finally:
        st.delete(f"lock:{job_id}")


def friendly_pipeline_error(e):
    text = str(e)
    if "No Sentinel-1" in text:
        return "no_images", "No Sentinel-1 images were found in both windows. Widen the dates."
    if "not found" in text and "Region" in text:
        return "region_not_found", "That region was not found."
    code, msg = friendly_ee_error(text)
    if code == "export_failed":
        return "pipeline_error", f"The flood analysis failed: {text[:200]}"
    return code, msg


def _start(job, d):
    jobs.update_job(d.store, job["id"], status="running", stage="starting", progress=5,
                    message="Selecting Sentinel-1 images and building the flood map (1-3 min)")
    prefix = f"results/{job['id']}"
    out = d.driver.start(job["spec"], job["id"], prefix)
    for name, data in out["previews"].items():
        d.storage.put_bytes(f"{prefix}/previews/{name}.png", data, "image/png")
    d.storage.put_bytes(f"{prefix}/config.json", json.dumps(out["config"], indent=2).encode(),
                        "application/json")
    n = len(out["task_ids"])
    jobs.update_job(d.store, job["id"], stage="exporting", progress=25,
                    message=f"Earth Engine is exporting results (0 of {n} done)",
                    work={"task_ids": out["task_ids"], "prefix": prefix, "bounds": out["bounds"],
                          "config": out["config"], "previews": sorted(out["previews"]),
                          "started": d.clock(), "mock": out.get("mock", False)})
    return {"again": d.settings.poll_seconds}


def _poll(job, d):
    w = job["work"]
    if d.clock() - w["started"] > d.settings.job_timeout_min * 60:
        raise JobError("timeout", f"The analysis did not finish within {d.settings.job_timeout_min} "
                                  "minutes. Try a smaller district or shorter windows.")
    status = d.driver.poll(w["task_ids"])
    bad = {n: s for n, s in status.items() if s["state"] in FAILED_STATES}
    if bad:
        name, s = next(iter(bad.items()))
        code, msg = friendly_ee_error(s.get("error"))
        raise JobError(code, msg if code != "export_failed" else f"Earth Engine export '{name}' failed: {(s.get('error') or 'unknown error')[:200]}")
    done = sum(1 for s in status.values() if s["state"] == "COMPLETED")
    total = len(status)
    if done < total:
        jobs.update_job(d.store, job["id"], progress=25 + int(45 * done / total),
                        message=f"Earth Engine is exporting results ({done} of {total} done)")
        return {"again": d.settings.poll_seconds}
    return _finalize(job, d)


def _place(spec):
    r = spec["region"]
    return f"{r['name']} district, {r['parent']}, {r['country']}"


def _finalize(job, d):
    s, spec, w = d.settings, job["spec"], job["work"]
    prefix = w["prefix"]
    jobs.update_job(d.store, job["id"], stage="postprocess", progress=75,
                    message="Finding flooded roads and settlements (OpenStreetMap)")
    stats = mf.parse_stats_csv(d.storage.get_bytes(f"{prefix}/stats.csv"))
    warnings = list(job.get("warnings") or [])
    roads = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "flood_vec.geojson")
            d.storage.download_to(f"{prefix}/flood_vec.geojson", src)
            out_dir = os.path.join(tmp, "roads")
            roads = d.roads_fn(src, _place(spec), out_dir, s.roads_motorable_only,
                               float(s.settlement_buffer_m))
            for fname in ("flooded_roads.geojson", "affected_settlements.geojson",
                          "affected_settlements.csv", "roads_flooded_by_type.csv"):
                with open(os.path.join(out_dir, fname), "rb") as f:
                    d.storage.put_bytes(f"{prefix}/{fname}", f.read())
        roads["motorable_only"] = bool(s.roads_motorable_only)
        roads["settlement_buffer_m"] = int(s.settlement_buffer_m)
    except Exception as e:  # noqa: BLE001 - roads are optional; the flood maps are still valid
        print(f"roads failed for {job['id']}: {type(e).__name__}: {e}")
        roads = {"error": "Road and settlement analysis failed (OpenStreetMap may be unavailable). "
                          "The flood maps and area statistics are still valid."}
        warnings.append({"level": "warning", "code": "roads_failed", "message": roads["error"]})

    jobs.update_job(d.store, job["id"], stage="finishing", progress=92, message="Preparing results")
    files = {n: f"{prefix}/{n}" for n in mf.OPTIONAL_FILES if d.storage.exists(f"{prefix}/{n}")}
    previews = {n: f"{prefix}/previews/{n}.png" for n in w["previews"]}
    r = spec["region"]
    m = mf.build(job["id"], "custom",
                 f"{r['name']} · {spec['pre'][0]} to {spec['post'][1]}", _place(spec), spec, stats,
                 w["config"], roads, w["bounds"], previews, files, warnings, mock=w.get("mock", False))
    d.storage.put_bytes(f"{prefix}/manifest.json", json.dumps(m).encode(), "application/json")
    jobs.update_job(d.store, job["id"], status="done", stage="done", progress=100,
                    message="Done", result={"manifest": f"{prefix}/manifest.json"}, warnings=warnings)
    jobs.release_active(d.store, job)
    return {}
