"""Vercel entry point (FastAPI). Every endpoint finishes in seconds and never waits on Earth Engine
work: it validates, reads/writes small state in Redis, and (for jobs) enqueues a Cloud Task."""
import json
import mimetypes
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Literal  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from pydantic import BaseModel, field_validator  # noqa: E402

from common import jobs, manifest as mf, ratelimit, services, validation  # noqa: E402

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def lifespan(_app):
    """Mock mode only: build the demo presets once at start-up so the first page load is not slow."""
    svc = services.get_services()
    if svc.settings.mode == "mock":
        from common import presets
        presets.seed_mock_presets(svc)
    yield


app = FastAPI(title="Flood mapping API", docs_url=None, redoc_url=None, lifespan=lifespan)

NAME_RE = re.compile(r"^[A-Za-z0-9 .,'()&/-]{1,80}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
PRESET_ID_RE = re.compile(r"^[a-z0-9_]{1,60}$")


def err(http_status, code, message, **extra):
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=http_status)


class Region(BaseModel):
    country: Literal["India"] = "India"
    level: Literal[2] = 2                 # districts only: roads run one district at a time
    parent: str
    name: str

    @field_validator("parent", "name")
    @classmethod
    def _clean(cls, v):
        if not NAME_RE.match(v):
            raise ValueError("contains characters that are not allowed")
        return v


class AnalysisRequest(BaseModel):
    region: Region
    pre: list[str]
    post: list[str]

    @field_validator("pre", "post")
    @classmethod
    def _dates(cls, v):
        if len(v) != 2 or not all(DATE_RE.match(x) for x in v):
            raise ValueError("must be [start, end] in YYYY-MM-DD format")
        return v


FIELD_LABELS = {"region.name": "District name", "region.parent": "State name", "region.level": "Region level",
                "region.country": "Country", "region": "Region", "pre": "Pre-flood dates", "post": "Post-flood dates"}


def _friendly(loc, msg):
    field = ".".join(str(p) for p in loc[1:])
    label = FIELD_LABELS.get(field) or FIELD_LABELS.get(field.split(".")[0]) or "The request"
    msg = msg.removeprefix("Value error, ")
    if msg == "Field required":
        msg = "is required"
    elif msg.startswith("Input should be"):
        msg = "has an unsupported value (only districts in India can be analysed)"
    return label + " " + msg


@app.exception_handler(RequestValidationError)
async def _bad_request(request, exc):
    issues = [{"level": "error", "code": "invalid_request", "field": ".".join(str(p) for p in e["loc"][1:]),
               "message": _friendly(e["loc"], e["msg"])} for e in exc.errors()]
    return err(422, "invalid_request", "The request was not valid: " + "; ".join(i["message"] for i in issues) + ".",
               issues=issues)


def _cid(request, svc):
    return ratelimit.client_id(request.headers, svc.settings.ip_salt)


def _spec(body):
    return {"region": body.region.model_dump(), "pre": body.pre, "post": body.post}


def _validate(body, svc):
    """Cheap checks that need no Earth Engine call. Returns issues."""
    s = svc.settings
    issues = validation.check_windows(body.pre, body.post, s)
    r = body.region
    if s.region_allowlist and svc.regions and not validation.region_listed(
            svc.regions, r.country, r.level, r.parent, r.name):
        issues.append(validation.issue("error", "region_unknown",
                                       "Unknown district. Pick one from the list."))
    return issues


def _info(body, svc):
    r = body.region
    return svc.info_fn(r.country, r.level, r.name, r.parent, body.pre, body.post)


@app.exception_handler(ratelimit.RateLimited)
async def _limited(request, exc):
    return JSONResponse({"error": {"code": "rate_limited", "message": exc.message,
                                   "retry_after": exc.retry_after}}, status_code=429,
                        headers={"Retry-After": str(exc.retry_after)})


@app.get("/api/health")
def health():
    return {"ok": True, "mode": services.get_services().settings.mode}


@app.get("/api/config")
def config():
    s = services.get_services().settings
    return {"mode": s.mode, "limits": {
        "pre_days": [s.pre_min_days, s.pre_max_days], "post_days": [s.post_min_days, s.post_max_days],
        "max_area_km2": s.max_area_km2, "min_images_warn": s.min_images_warn,
        "max_gap_days": s.max_gap_days, "data_lag_days": s.data_lag_days}, "poll_ms": 1500 if s.mode == "mock" else 6000}


@app.get("/api/regions")
def regions():
    return services.get_services().regions or {}


@app.get("/api/events")
def events():
    svc = services.get_services()
    st = svc.storage
    if svc.settings.mode == "mock":              # demo only: build the preset events on first use
        from common import presets
        presets.seed_mock_presets(svc)
    try:
        return json.loads(st.get_bytes("presets/index.json"))
    except FileNotFoundError:
        return {"events": []}


@app.get("/api/events/{event_id}")
def event(event_id: str):
    svc = services.get_services()
    if not PRESET_ID_RE.match(event_id):
        return err(404, "not_found", "Unknown event.")
    try:
        m = json.loads(svc.storage.get_bytes(f"presets/{event_id}/manifest.json"))
    except FileNotFoundError:
        return err(404, "not_found", "Unknown event.")
    return mf.resolve(m, svc.storage)


@app.post("/api/preflight")
def preflight(body: AnalysisRequest, request: Request):
    svc = services.get_services()
    s = svc.settings
    ratelimit.hit(svc.store, "preflight", _cid(request, svc), s.rate_preflight_per_hour, 3600,
                  "Too many checks. Please wait a bit before checking again.")
    issues = _validate(body, svc)
    if validation.has_errors(issues):
        return {"ok": False, "issues": issues}
    try:
        info = _info(body, svc)
    except Exception as e:  # noqa: BLE001
        print(f"preflight EE failure: {type(e).__name__}: {e}")
        return err(502, "ee_unavailable", "Earth Engine could not be reached. Please try again shortly.")
    more, chosen = validation.check_info(info, s)
    issues += more
    return {"ok": not validation.has_errors(issues), "issues": issues, "chosen_pass": chosen,
            "counts": info["counts"], "area_km2": info["area_km2"], "bounds": info["bounds"]}


@app.post("/api/jobs", status_code=202)
def create_job(body: AnalysisRequest, request: Request):
    svc = services.get_services()
    s, cid, spec = svc.settings, _cid(request, svc), _spec(body)
    issues = _validate(body, svc)
    if validation.has_errors(issues):
        return err(400, "invalid_input", issues[0]["message"], issues=issues)
    cached_id = svc.store.get(f"cache:{jobs.spec_key(spec)}")
    if cached_id and (j := jobs.get_job(svc.store, cached_id)) and j["status"] in ("queued", "running", "done"):
        return {"job_id": j["id"], "cached": True, "status": j["status"], "warnings": j.get("warnings", [])}
    ratelimit.hit(svc.store, "jobs_try", cid, s.rate_preflight_per_hour, 3600,
                  "Too many requests. Please wait a bit.")
    try:
        info = _info(body, svc)
    except Exception as e:  # noqa: BLE001
        print(f"jobs EE failure: {type(e).__name__}: {e}")
        return err(502, "ee_unavailable", "Earth Engine could not be reached. Please try again shortly.")
    more, _ = validation.check_info(info, s)
    issues += more
    if validation.has_errors(issues):
        return err(400, "invalid_input", next(i for i in issues if i["level"] == "error")["message"],
                   issues=issues)
    job, cached = jobs.create_job(svc, spec, cid, warnings=[i for i in issues if i["level"] == "warning"])
    return {"job_id": job["id"], "cached": cached, "status": job["status"], "warnings": job["warnings"]}


def _job_or_404(job_id):
    svc = services.get_services()
    if not JOB_ID_RE.match(job_id):
        return svc, None
    return svc, jobs.get_job(svc.store, job_id)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    svc, job = _job_or_404(job_id)
    if not job:
        return err(404, "not_found", "Job not found (results are kept for 30 days).")
    return jobs.public_view(job)


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str):
    svc, job = _job_or_404(job_id)
    if not job:
        return err(404, "not_found", "Job not found (results are kept for 30 days).")
    if job["status"] != "done":
        return err(409, "not_ready", "This analysis is not finished yet.", status=job["status"])
    m = json.loads(svc.storage.get_bytes(job["result"]["manifest"]))
    return mf.resolve(m, svc.storage)


# ---- mock/local mode only: serve stored files and the frontend
_boot = services.get_services if os.environ.get("BACKEND_MODE", "real").lower() == "mock" else None
if _boot is not None:
    @app.get("/files/{path:path}")
    def files(path: str):
        try:
            data = services.get_services().storage.get_bytes(path)
        except (FileNotFoundError, ValueError):
            return err(404, "not_found", "File not found.")
        return Response(data, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")

    from fastapi.staticfiles import StaticFiles
    _pub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
    if os.path.isdir(_pub):
        app.mount("/", StaticFiles(directory=_pub, html=True), name="public")
