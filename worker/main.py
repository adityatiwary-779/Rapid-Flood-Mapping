"""Cloud Run worker. Cloud Tasks POSTs /step {job_id}; the response is always 200 (job state lives in Redis),
so Cloud Tasks never retries a job that failed for a legitimate reason."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from common import services  # noqa: E402
from worker import runner  # noqa: E402

app = FastAPI(title="flood-worker")


class StepBody(BaseModel):
    job_id: str


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/step")
def step(body: StepBody):
    svc = services.get_services()
    res = runner.step(body.job_id, services.worker_deps(svc)) or {}
    if res.get("again"):
        svc.queue.enqueue(body.job_id, delay=res["again"])
    return {"ok": True, "again": res.get("again", 0)}
