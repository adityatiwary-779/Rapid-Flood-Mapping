"""Real Earth Engine driver: wraps the tested pipeline. Runs only on the worker (needs EE credentials)."""
import requests

from common import google_auth
from pipeline import flood_pipeline as fp

EXPORT_NAMES = ("flood", "dvv_db", "stats", "flood_vec")


class EEDriver:
    def __init__(self, settings):
        self.s = settings
        google_auth.init_ee(settings)

    def start(self, spec, job_id, prefix):
        r = spec["region"]
        ev = {"country": r["country"], "level": r["level"], "parent": r["parent"],
              "name": r["name"], "pre": spec["pre"], "post": spec["post"]}
        run = fp.prepare_run(job_id, ev, r["name"], no_rf=False)
        dest = {"gcs": {"bucket": self.s.bucket, "prefix": prefix}}
        tasks = fp.start_exports(run, dest, vectors=True)
        by_name = {}
        for t in tasks:
            desc = t.config["description"]                      # "<job>_<tag>_<name>" -> match by suffix
            name = next(n for n in sorted(EXPORT_NAMES, key=len, reverse=True) if desc.endswith("_" + n))
            by_name[name] = t.id
        urls, bounds = fp.preview_urls(run)
        previews = {}
        for name, url in urls.items():
            resp = requests.get(url, timeout=300)
            resp.raise_for_status()
            previews[name] = resp.content
        return {"task_ids": by_name, "config": fp.config_report(run), "bounds": bounds,
                "previews": previews}

    def poll(self, task_ids):
        import ee
        ids = list(task_ids.values())
        statuses = {s["id"]: s for s in ee.data.getTaskStatus(ids)}
        return {name: {"state": statuses[i]["state"], "error": statuses[i].get("error_message")}
                for name, i in task_ids.items()}
