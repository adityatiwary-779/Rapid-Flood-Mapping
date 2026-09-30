#!/usr/bin/env python3
"""Check every REAL service, one step at a time, and print a paste-friendly report. Never prints secrets.

  # 1. backend services (env vars from .env.example must be set in this shell)
  python tools/integration_check.py services
  # 2. a deployed API (Vercel URL, or a local uvicorn):
  python tools/integration_check.py api --url https://YOUR-APP.vercel.app
  python tools/integration_check.py api --url https://YOUR-APP.vercel.app --run-job     # also runs ONE real custom job
"""
import argparse
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

results = []


def redact(text):
    text = re.sub(r"[A-Za-z0-9_\-]{32,}", "<redacted>", str(text))
    return re.sub(r"(private_key|token|secret)[^,}\s]*", r"\1=<redacted>", text, flags=re.I)[:300]


def reachable(url):
    """HTTP status of a 1-byte ranged GET (signed GCS URLs are valid for GET only, so HEAD would give false failures)."""
    r = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=60)
    r.close()
    return 200 if r.status_code in (200, 206) else r.status_code


def step(name):
    def deco(fn):
        def run(*a, **k):
            t0 = time.time()
            try:
                note = fn(*a, **k)
                results.append(("PASS", name, note or "", time.time() - t0))
            except Skip as e:
                results.append(("SKIP", name, str(e), 0))
            except Exception as e:  # noqa: BLE001
                results.append(("FAIL", name, f"{type(e).__name__}: {redact(e)}", time.time() - t0))
                return None
        return run
    return deco


class Skip(Exception):
    pass


def report():
    print("\n" + "=" * 70)
    for status, name, note, secs in results:
        print(f"{status:5} {name:34} {secs:6.1f}s  {note}")
    bad = [r for r in results if r[0] == "FAIL"]
    print("=" * 70)
    print(f"{len([r for r in results if r[0] == 'PASS'])} passed, {len(bad)} failed, "
          f"{len([r for r in results if r[0] == 'SKIP'])} skipped")
    return 1 if bad else 0


# ------------------------------------------------------------------ backend services
REQUIRED = ["EE_PROJECT", "EE_SERVICE_ACCOUNT_JSON", "GCS_BUCKET", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
            "WORKER_URL", "TASKS_INVOKER_SA", "TASKS_LOCATION", "TASKS_QUEUE"]


@step("environment variables set")
def check_env():
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError("missing: " + ", ".join(missing))
    return "all %d present (values not shown)" % len(REQUIRED)


@step("service-account key parses")
def check_sa(s):
    from common import google_auth
    info = google_auth.service_account_info(s.ee_service_account_json)
    return f"{info['client_email']} (project {info.get('project_id')})"


@step("Earth Engine login + computation")
def check_ee(s):
    import ee
    from common import google_auth
    google_auth.init_ee(s)
    assert ee.Number(1).add(1).getInfo() == 2
    crs = ee.Image("USGS/SRTMGL1_003").projection().crs().getInfo()
    return f"project {s.ee_project}, SRTM crs {crs}"


@step("Earth Engine datasets readable")
def check_datasets(s):
    import ee
    for label, expr in (("WorldCover", ee.ImageCollection("ESA/WorldCover/v200").first().bandNames()),
                        ("WorldPop", ee.ImageCollection("WorldPop/GP/100m/pop").first().bandNames()),
                        ("JRC", ee.Image("JRC/GSW1_4/GlobalSurfaceWater").bandNames()),
                        ("MERIT", ee.Image("MERIT/Hydro/v1_0_1").bandNames()),
                        ("Sentinel-1", ee.ImageCollection("COPERNICUS/S1_GRD").limit(1).size()),
                        ("GAUL", ee.FeatureCollection("FAO/GAUL/2015/level2").limit(1).size())):
        expr.getInfo()
    return "WorldCover, WorldPop, JRC, MERIT, Sentinel-1, GAUL"


@step("Cloud Storage write/read/delete")
def check_gcs(svc):
    path = f"_check/{int(time.time())}.txt"
    svc.storage.put_bytes(path, b"hello", "text/plain")
    assert svc.storage.get_bytes(path) == b"hello"
    url = svc.storage.url(path)
    r = requests.get(url, timeout=15)
    public = r.status_code == 200
    svc.storage.bucket.blob(path).delete()
    return "public URL readable" if public else f"private bucket (HTTP {r.status_code}); set SIGNED_URLS=1 or make results public"


@step("Cloud Storage CORS allows the site")
def check_cors(svc):
    svc.storage.bucket.reload()
    rules = svc.storage.bucket.cors
    if not rules:
        raise RuntimeError("no CORS rules on the bucket; loading roads/settlement layers in the browser will fail")
    return f"{len(rules)} rule(s): origins {rules[0].get('origin')}"


@step("Redis set/get/incr")
def check_redis(svc):
    st = svc.store
    st.set("_check:k", "v", ex=30)
    assert st.get("_check:k") == "v"
    assert st.incr("_check:n", ex=30) >= 1
    st.delete("_check:k")
    st.delete("_check:n")
    return "ok"


@step("Cloud Run worker reachable (with identity token)")
def check_worker(s):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    from common import google_auth
    creds = service_account.IDTokenCredentials.from_service_account_info(
        google_auth.service_account_info(s.ee_service_account_json), target_audience=s.worker_url.rstrip("/"))
    r = AuthorizedSession(creds).get(s.worker_url.rstrip("/") + "/healthz", timeout=30)
    if r.status_code == 403:
        raise RuntimeError("HTTP 403: the service account needs roles/run.invoker on the worker service")
    r.raise_for_status()
    return "healthz ok"


@step("Cloud Tasks accepts a task")
def check_tasks(svc):
    svc.queue.enqueue("checkjob0000")           # unknown job id: the worker replies 200 and does nothing
    return "enqueued (see Cloud Run logs: POST /step 200 within a minute)"


@step("preflight round trip (Alappuzha)")
def check_preflight(svc):
    t0 = time.time()
    info = svc.info_fn("India", 2, "Alappuzha", "Kerala", ["2018-07-01", "2018-07-30"], ["2018-08-15", "2018-08-25"])
    secs = time.time() - t0
    note = f"{secs:.1f}s, area {info['area_km2']:.0f} km2, descending {info['counts']['DESCENDING']}"
    if secs > 8:
        note += "  <-- slow: the Vercel function may time out; see docs/DEPLOY.md 'Function time limit'"
    if info["n_regions"] != 1:
        raise RuntimeError(f"expected 1 region, got {info['n_regions']}")
    return note


@step("preset district names exist")
def check_preset_names(svc):
    from common import presets
    bad = []
    for p in presets.load_presets():
        r = p["spec"]["region"]
        info = svc.info_fn(r["country"], r["level"], r["name"], r["parent"], p["spec"]["pre"], p["spec"]["post"])
        if info["n_regions"] != 1:
            bad.append(f"{p['id']} ({info['n_regions']} matches)")
    if bad:
        raise RuntimeError("fix spelling in data/presets.json: " + ", ".join(bad))
    return "all presets resolve to exactly one district"


def cmd_services(_):
    from common import services
    from common.config import load_settings
    os.environ.setdefault("BACKEND_MODE", "real")
    check_env()
    s = load_settings()
    if any(r[0] == "FAIL" for r in results):
        return report()
    check_sa(s)
    check_ee(s)
    check_datasets(s)
    svc = None
    try:
        svc = services.build_services(s)
    except Exception as e:  # noqa: BLE001
        results.append(("FAIL", "build services", f"{type(e).__name__}: {redact(e)}", 0))
    if svc:
        check_gcs(svc)
        check_cors(svc)
        check_redis(svc)
        check_worker(s)
        check_tasks(svc)
        check_preflight(svc)
        check_preset_names(svc)
    return report()


# ------------------------------------------------------------------ deployed API
def cmd_api(a):
    base = a.url.rstrip("/")
    S = requests.Session()
    S.headers["x-forwarded-for"] = "203.0.113.%d" % (int(time.time()) % 200 + 1)   # distinct client for rate limits
    good = {"region": {"parent": "Kerala", "name": "Alappuzha"}, "pre": ["2018-07-01", "2018-07-30"],
            "post": ["2018-08-15", "2018-08-25"]}

    @step("GET /api/health")
    def health():
        r = S.get(base + "/api/health", timeout=30); r.raise_for_status()
        return f"mode={r.json()['mode']}"

    @step("GET /api/config")
    def cfg():
        r = S.get(base + "/api/config", timeout=30); r.raise_for_status()
        return f"limits {r.json()['limits']['post_days']} d post, {r.json()['limits']['max_area_km2']} km2"

    @step("GET /api/events (presets)")
    def events():
        ev = S.get(base + "/api/events", timeout=30).json()["events"]
        if not ev:
            raise RuntimeError("no presets stored: run tools/precompute_presets.py")
        for e in ev:
            r = S.get(f"{base}/api/events/{e['id']}", timeout=30); r.raise_for_status()
            m = r.json()
            for url in list(m["files"].values()) + list(m["previews"].values()):
                u = url if url.startswith("http") else base + url
                code = reachable(u)
                if code != 200:
                    raise RuntimeError(f"{e['id']}: {u.split('?')[0]} -> HTTP {code}")
        return ", ".join(e["id"] for e in ev) + " (every file and image reachable)"

    @step("bad request rejected (overlap)")
    def bad():
        b = json.loads(json.dumps(good)); b["post"] = ["2018-07-25", "2018-08-05"]
        r = S.post(base + "/api/jobs", json=b, timeout=30)
        if r.status_code != 400 or "overlap" not in r.text:
            raise RuntimeError(f"expected 400 overlap, got {r.status_code}: {r.text[:120]}")
        return "400 with readable message"

    @step("POST /api/preflight (real Earth Engine)")
    def pre():
        t0 = time.time()
        r = S.post(base + "/api/preflight", json=good, timeout=120)
        secs = time.time() - t0
        r.raise_for_status()
        j = r.json()
        return f"{secs:.1f}s ok={j['ok']} pass={j.get('chosen_pass')} issues={[i['code'] for i in j['issues']]}"

    @step("live custom job (start -> poll -> result -> files)")
    def job():
        if not a.run_job:
            raise Skip("add --run-job to run one real analysis (uses Earth Engine quota)")
        r = S.post(base + "/api/jobs", json=good, timeout=120)
        r.raise_for_status()
        jid = r.json()["job_id"]
        last = None
        t0 = time.time()
        while time.time() - t0 < a.timeout * 60:
            j = S.get(f"{base}/api/jobs/{jid}", timeout=30).json()
            line = f"{j['status']} {j['progress']}% {j['message']}"
            if line != last:
                print(f"    [{time.time() - t0:5.0f}s] {line}", flush=True)
                last = line
            if j["status"] == "failed":
                raise RuntimeError(f"job failed: {j['error']}")
            if j["status"] == "done":
                break
            time.sleep(8)
        else:
            raise RuntimeError(f"job {jid} not finished after {a.timeout} min")
        m = S.get(f"{base}/api/jobs/{jid}/result", timeout=60).json()
        for k, url in list(m["files"].items()) + list(m["previews"].items()):
            u = url if url.startswith("http") else base + url
            code = reachable(u)
            if code != 200:
                raise RuntimeError(f"{k}: HTTP {code}")
        st = m["stats"]
        return (f"flood {st['flood_km2']:.1f} km2, roads {'ok' if m['roads'] and 'error' not in m['roads'] else m['roads']}, "
                f"{len(m['files'])} files + {len(m['previews'])} images reachable, mock={m['mock']}")

    for fn in (health, cfg, events, bad, pre, job):
        fn()
    return report()


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("services")
    p = sub.add_parser("api")
    p.add_argument("--url", required=True)
    p.add_argument("--run-job", action="store_true")
    p.add_argument("--timeout", type=int, default=45, help="minutes to wait for the live job")
    a = ap.parse_args(argv)
    code = cmd_services(a) if a.cmd == "services" else cmd_api(a)
    sys.exit(code)


if __name__ == "__main__":
    main()
