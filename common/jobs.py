"""Job records + creation logic shared by the API (and the worker, which updates them)."""
import hashlib
import json
import time
import uuid

from . import ratelimit

JOB_TTL = 30 * 24 * 3600
ACTIVE_TTL = 2 * 3600


def spec_key(spec):
    """Same region + dates (+ pipeline settings version) -> same result, so it can be cached."""
    r = spec["region"]
    canon = json.dumps([r["country"], r["level"], r.get("parent"), r["name"],
                        spec["pre"], spec["post"], "v1"], sort_keys=True)
    return hashlib.sha1(canon.encode()).hexdigest()[:16]


def get_job(store, job_id):
    raw = store.get(f"job:{job_id}")
    return json.loads(raw) if raw else None


def save_job(store, job):
    job["updated"] = time.time()
    store.set(f"job:{job['id']}", json.dumps(job), ex=JOB_TTL)


def update_job(store, job_id, **fields):
    job = get_job(store, job_id)
    if job is None:
        return None
    job.update(fields)
    save_job(store, job)
    return job


def public_view(job):
    """What the browser may see: no client id, no internal worker state."""
    keys = ("id", "status", "stage", "message", "progress", "spec", "error", "created",
            "updated", "warnings", "result")
    return {k: job.get(k) for k in keys}


def release_active(store, job):
    cid = job.get("cid")
    if cid and store.get(f"active:{cid}") == job["id"]:
        store.delete(f"active:{cid}")


def create_job(services, spec, cid, warnings=None):
    """Returns (job, cached). Raises ratelimit.RateLimited. The caller has already validated `spec`."""
    st, s = services.store, services.settings
    key = spec_key(spec)
    cached_id = st.get(f"cache:{key}")
    if cached_id:
        cached = get_job(st, cached_id)
        if cached and cached["status"] in ("queued", "running", "done"):
            return cached, True

    ratelimit.hit(st, "jobs", cid, s.rate_jobs_per_hour, 3600,
                  f"Job limit reached ({s.rate_jobs_per_hour} new analyses per hour).")
    day = int(st.now() // 86400)
    if st.incr(f"global:jobs:{day}", ex=86400) > s.global_jobs_per_day:
        raise ratelimit.RateLimited("The daily analysis limit for this demo has been reached. "
                                    "Try again tomorrow, or open a preset event.", 3600)
    job_id = uuid.uuid4().hex[:12]
    if not st.set(f"active:{cid}", job_id, ex=ACTIVE_TTL, nx=True):
        raise ratelimit.RateLimited("You already have an analysis running. Wait for it to finish "
                                    "before starting another.", 60)
    now = time.time()
    job = {"id": job_id, "key": key, "cid": cid, "status": "queued", "stage": "queued",
           "message": "Waiting for a worker to pick up the job", "progress": 0, "spec": spec,
           "error": None, "created": now, "updated": now, "warnings": warnings or [],
           "result": None, "work": {}}
    save_job(st, job)
    st.set(f"cache:{key}", job_id, ex=JOB_TTL)
    try:
        services.queue.enqueue(job_id)
    except Exception as e:  # noqa: BLE001 - surface as a failed job, free the user's slot
        job.update(status="failed", stage="failed", message="Could not start the job.",
                   error={"code": "queue_error", "message": "Could not queue the analysis. Please try again in a minute."})
        save_job(st, job)
        st.delete(f"cache:{key}")
        release_active(st, job)
        print(f"enqueue failed for {job_id}: {type(e).__name__}")
    return job, False
