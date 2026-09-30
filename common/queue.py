"""Job queue. Cloud Tasks in production (each task = one short worker step), a thread in mock mode."""
import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone


class InlineQueue:
    """Mock/dev: runs worker steps in a background thread, honouring the requested delays."""

    def __init__(self, step_fn, sleep=time.sleep, delay_scale=1.0):
        self.step_fn, self.sleep, self.delay_scale = step_fn, sleep, delay_scale
        self.threads = []

    def join(self, timeout=30):
        for t in list(self.threads):
            t.join(timeout)

    def enqueue(self, job_id, delay=0):
        def loop():
            d = delay
            while True:
                self.sleep(d * self.delay_scale)
                res = self.step_fn(job_id) or {}
                if not res.get("again"):
                    return
                d = res["again"]
        t = threading.Thread(target=loop, daemon=True)
        self.threads.append(t)
        t.start()


class CloudTasksQueue:
    def __init__(self, settings, credentials):
        from google.auth.transport.requests import AuthorizedSession
        self.s, self.session = settings, AuthorizedSession(credentials)
        self.endpoint = (f"https://cloudtasks.googleapis.com/v2/projects/{settings.tasks_project}"
                         f"/locations/{settings.tasks_location}/queues/{settings.tasks_queue}/tasks")

    def enqueue(self, job_id, delay=0):
        body = base64.b64encode(json.dumps({"job_id": job_id}).encode()).decode()
        task = {"httpRequest": {
            "url": self.s.worker_url.rstrip("/") + "/step", "httpMethod": "POST",
            "headers": {"Content-Type": "application/json"}, "body": body,
            "oidcToken": {"serviceAccountEmail": self.s.tasks_invoker_sa,
                          "audience": self.s.worker_url.rstrip("/")}},
            "dispatchDeadline": "900s"}
        if delay:
            when = datetime.now(timezone.utc) + timedelta(seconds=delay)
            task["scheduleTime"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self.session.post(self.endpoint, json={"task": task}, timeout=10)
        if r.status_code >= 300:
            raise RuntimeError(f"Could not enqueue job (Cloud Tasks HTTP {r.status_code}).")
