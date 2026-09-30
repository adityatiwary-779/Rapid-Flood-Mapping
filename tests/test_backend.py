"""Backend tests in MOCK mode (no Earth Engine / GCS / Redis / OSM). They exercise the real API,
validation, job state machine, rate limiting and file writers; they do NOT prove Earth Engine behaviour."""
import base64
import csv
import io
import json
import os
import subprocess

import pytest

from conftest import ROOT, body, wait_done


# ------------------------------------------------------------------ basics
def test_health_config_regions(make_client):
    c, _ = make_client()
    assert c.get("/api/health").json() == {"ok": True, "mode": "mock"}
    lim = c.get("/api/config").json()["limits"]
    assert lim["post_days"] == [7, 14] and lim["pre_days"] == [7, 30] and lim["max_area_km2"] == 5000
    assert c.get("/api/regions").status_code == 200
    ev = c.get("/api/events").json()["events"]                     # mock mode seeds the demo presets
    assert [e["id"] for e in ev] == ["kerala_2018_alappuzha", "assam_2022_nagaon", "chennai_2023_chennai"]
    assert all(e["mock"] for e in ev) and ev[0]["windows"]["pre"] == ["2018-07-01", "2018-07-31"]
    assert c.get("/api/events/nope").status_code == 404
    assert c.get("/api/events/..%2Fsecret").status_code in (404, 422)


def test_preset_manifest_resolves_and_files_download(make_client):
    c, _ = make_client()
    c.get("/api/events")
    m = c.get("/api/events/kerala_2018_alappuzha").json()
    assert m["kind"] == "preset" and m["id"] == "kerala_2018_alappuzha" and m["mock"] is True
    assert m["title"] == "Kerala floods 2018 · Alappuzha"
    assert m["windows"]["pre"] == ["2018-07-01", "2018-07-31"]        # presets are not bound by the 7-30 day rule
    assert m["files"]["flood.tif"].startswith("/files/presets/kerala_2018_alappuzha/")
    assert c.get(m["files"]["stats.csv"]).status_code == 200 and c.get(m["previews"]["pre_vv"]).status_code == 200


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("mut,code_part", [
    (dict(pre=("2018-07-01", "2018-07-03")), "pre_length"),
    (dict(pre=("2018-07-01", "2018-09-30")), "pre_length"),
    (dict(post=("2018-08-15", "2018-08-16")), "post_length"),
    (dict(post=("2018-08-15", "2018-09-15")), "post_length"),
    (dict(post=("2018-08-25", "2018-08-15")), "post_order"),
    (dict(post=("2018-07-25", "2018-08-05")), "windows_overlap"),           # overlaps pre window
    (dict(pre=("2018-07-20", "2018-08-10"), post=("2018-08-05", "2018-08-15")), "windows_overlap"),
    (dict(pre=("2013-01-01", "2013-01-20")), "pre_too_early"),
    (dict(post=("2099-01-01", "2099-01-10")), "post_in_future"),
    (dict(pre=("2010-01-01", "2010-01-10"), post=("2018-08-15", "2018-08-25")), "pre_too_early"),
    (dict(pre=("2015-01-01", "2015-01-10"), post=("2018-08-15", "2018-08-25")), "gap_too_long"),
])
def test_bad_dates_rejected(make_client, mut, code_part):
    c, _ = make_client()
    r = c.post("/api/jobs", json=body(**mut))
    assert r.status_code == 400, r.text
    codes = [i["code"] for i in r.json()["error"]["issues"]]
    assert any(code_part in x for x in codes), codes
    assert r.json()["error"]["message"]                      # human-readable
    p = c.post("/api/preflight", json=body(**mut)).json()    # preflight reports the same problem
    assert p["ok"] is False and any(code_part in i["code"] for i in p["issues"])


@pytest.mark.parametrize("payload", [
    {}, {"region": {"parent": "Kerala", "name": "<script>alert(1)</script>"}, "pre": ["2018-07-01", "2018-07-31"], "post": ["2018-08-15", "2018-08-25"]},
    {"region": {"level": 1, "parent": "Kerala", "name": "Kerala"}, "pre": ["2018-07-01", "2018-07-31"], "post": ["2018-08-15", "2018-08-25"]},
    {"region": {"parent": "Kerala", "name": "Alappuzha"}, "pre": ["18-07-01", "2018-07-31"], "post": ["2018-08-15", "2018-08-25"]},
    {"region": {"parent": "Kerala", "name": "Alappuzha"}, "pre": ["2018-07-01"], "post": ["2018-08-15", "2018-08-25"]},
    {"region": {"country": "France", "parent": "X", "name": "Y"}, "pre": ["2018-07-01", "2018-07-31"], "post": ["2018-08-15", "2018-08-25"]},
])
def test_malformed_requests_422(make_client, payload):
    c, _ = make_client()
    for path in ("/api/jobs", "/api/preflight"):
        r = c.post(path, json=payload)
        assert r.status_code == 422
        err_ = r.json()["error"]
        assert err_["code"] == "invalid_request" and err_["message"]
        assert "Value error" not in err_["message"] and all(i["level"] == "error" and i["message"] for i in err_["issues"])


def test_calendar_invalid_date(make_client):
    c, _ = make_client()
    r = c.post("/api/jobs", json=body(pre=("2018-02-30", "2018-03-10")))
    assert r.status_code == 400 and r.json()["error"]["issues"][0]["code"] == "bad_date"


def test_region_rules(make_client):
    c, _ = make_client()
    big = c.post("/api/preflight", json=body(name="HugeDistrict")).json()
    assert not big["ok"] and any(i["code"] == "region_too_large" for i in big["issues"])
    assert "one district at a time" in big["issues"][0]["message"]
    r = c.post("/api/jobs", json=body(name="HugeDistrict"))
    assert r.status_code == 400 and "20000" in r.json()["error"]["message"]
    twin = c.post("/api/preflight", json=body(name="TwinDistrict")).json()
    assert any(i["code"] == "region_ambiguous" for i in twin["issues"])
    none = c.post("/api/preflight", json=body(name="NoImagesDistrict")).json()
    assert any(i["code"] == "no_images" for i in none["issues"])


def test_preflight_ok_and_few_images_warning(make_client):
    c, _ = make_client()
    ok = c.post("/api/preflight", json=body()).json()
    assert ok["ok"] and ok["chosen_pass"] == "DESCENDING" and ok["counts"]["DESCENDING"]["pre"] >= 1
    assert ok["area_km2"] == pytest.approx(1316.69) and len(ok["bounds"]) == 2
    sparse = c.post("/api/preflight", json=body(name="SparseDistrict")).json()
    assert sparse["ok"] and [i["code"] for i in sparse["issues"]] == ["few_images"]
    r = c.post("/api/jobs", json=body(name="SparseDistrict"))     # warning does not block a job
    assert r.status_code == 202 and r.json()["warnings"][0]["code"] == "few_images"


def test_region_allowlist_when_enabled(make_client):
    c, svc = make_client(REGION_ALLOWLIST=1)
    svc.regions = {"India": {"2": [{"parent": "Kerala", "name": "Alappuzha", "area_km2": 1316.69}]}}
    assert c.post("/api/preflight", json=body()).json()["ok"]
    r = c.post("/api/preflight", json=body(name="Atlantis")).json()
    assert not r["ok"] and r["issues"][0]["code"] == "region_unknown"


# ------------------------------------------------------------------ full job lifecycle + downloads
def test_job_end_to_end_and_downloads(make_client):
    import rasterio
    c, svc = make_client()
    r = c.post("/api/jobs", json=body())
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert c.get(f"/api/jobs/{job_id}/result").status_code in (409, 200)
    j, seen = wait_done(c, job_id)
    assert j["status"] == "done", j
    prog = [s["progress"] for s in seen if s["status"] != "failed"]
    assert prog == sorted(prog) and prog[-1] == 100
    assert {"exporting"} & {s["stage"] for s in seen} or len(seen) >= 1
    assert "cid" not in j and "work" not in j                                   # internals never exposed

    m = c.get(f"/api/jobs/{job_id}/result").json()
    assert m["mock"] is True and "NOT independent validation" in m["caveat"]
    st = m["stats"]
    for k in ("flood_km2", "cropland_km2", "builtup_km2", "tree_cover_km2", "population_exposed",
              "flood_unmasked_km2", "other_km2", "threshold_db"):
        assert st[k] is not None, k
    assert st["other_km2"] == pytest.approx(st["flood_km2"] - st["cropland_km2"] - st["builtup_km2"] - st["tree_cover_km2"])
    cfg = m["config"]
    assert cfg["threshold_db"] == pytest.approx(-3.9002, abs=1e-3) and cfg["masks"]["max_slope_deg"] == 5.0
    assert m["roads"]["road_km_flooded"] > 0 and m["roads"]["settlements"] and m["roads"]["by_type"]
    assert m["roads"]["motorable_only"] is True and m["roads"]["settlement_buffer_m"] == 250
    assert m["windows"] == {"pre": ["2018-07-01", "2018-07-30"], "post": ["2018-08-15", "2018-08-25"]}
    assert set(m["previews"]) == {"pre_vv", "post_vv", "flood", "flood_unmasked", "severity"}

    def get(url):
        resp = c.get(url)
        assert resp.status_code == 200, url
        return resp.content

    # CSV: real pipeline columns
    rows = list(csv.DictReader(io.StringIO(get(m["files"]["stats.csv"]).decode())))
    assert len(rows) == 1 and rows[0]["flood_km2"] and "population_exposed" in rows[0] and ".geo" not in rows[0]
    # GeoJSON
    gj = json.loads(get(m["files"]["flood_vec.geojson"]))
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) > 100
    for k in ("flooded_roads.geojson", "affected_settlements.geojson"):
        assert json.loads(get(m["files"][k]))["type"] == "FeatureCollection"
    # GeoTIFFs open in rasterio with correct CRS, bounds, nodata
    with rasterio.MemoryFile(get(m["files"]["flood.tif"])) as mf_:
        with mf_.open() as ds:
            assert ds.crs.to_epsg() == 4326 and ds.dtypes[0] == "uint8" and ds.count == 1
            (s_, w_), (n_, e_) = m["bounds"]
            assert ds.bounds.left == pytest.approx(w_) and ds.bounds.top == pytest.approx(n_)
            assert ds.read(1).max() == 1 and ds.read(1).sum() > 1000
    with rasterio.MemoryFile(get(m["files"]["dvv_db.tif"])) as mf_:
        with mf_.open() as ds:
            assert ds.dtypes[0] == "float32" and ds.nodata == -9999 and ds.read(1).min() == -9999
    # PNG previews
    for name, url in m["previews"].items():
        data = get(url)
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name
    # unmasked layer is larger than masked (toggle is meaningful)
    assert st["flood_unmasked_km2"] > st["flood_km2"]

    # identical request -> cached, no new job, no extra rate-limit use
    r2 = c.post("/api/jobs", json=body())
    assert r2.status_code == 202 and r2.json()["cached"] is True and r2.json()["job_id"] == job_id


# ------------------------------------------------------------------ failures are readable
def test_failed_start_is_readable_and_frees_user(make_client):
    c, svc = make_client()
    r = c.post("/api/jobs", json=body(name="FailStartDistrict"))
    j, _ = wait_done(c, r.json()["job_id"])
    assert j["status"] == "failed" and j["error"]["code"] == "timed_out"
    assert "smaller district" in j["error"]["message"] and "Traceback" not in json.dumps(j)
    assert c.get(f"/api/jobs/{j['id']}/result").status_code == 409
    # slot freed + failed jobs are not cached: a retry is a NEW job
    r2 = c.post("/api/jobs", json=body(name="FailStartDistrict"))
    assert r2.status_code == 202 and r2.json()["cached"] is False and r2.json()["job_id"] != j["id"]


def test_failed_export_is_readable(make_client):
    c, _ = make_client()
    r = c.post("/api/jobs", json=body(name="FailExportDistrict"))
    j, _ = wait_done(c, r.json()["job_id"])
    assert j["status"] == "failed" and j["error"]["code"] == "timed_out" and j["message"]


def test_enqueue_failure_reported(make_client):
    c, svc = make_client()

    class Boom:
        def enqueue(self, *a, **k):
            raise RuntimeError("secret internal detail https://x?token=abc")
    svc.queue = Boom()
    r = c.post("/api/jobs", json=body())
    j = c.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert j["status"] == "failed" and j["error"]["code"] == "queue_error"
    assert "secret" not in json.dumps(j) and "token" not in json.dumps(j)
    assert c.post("/api/jobs", json=body()).status_code == 202          # user not stuck


def test_roads_failure_is_not_fatal(make_client):
    c, svc = make_client()
    svc.roads_fn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Overpass 429"))
    r = c.post("/api/jobs", json=body())
    j, _ = wait_done(c, r.json()["job_id"])
    assert j["status"] == "done"
    m = c.get(f"/api/jobs/{j['id']}/result").json()
    assert "error" in m["roads"] and any(w["code"] == "roads_failed" for w in m["warnings"])
    assert m["stats"]["flood_km2"] > 0


def test_worker_timeout_and_duplicate_delivery(make_client):
    from worker import runner
    from common import services, jobs
    c, svc = make_client(null_queue=True)
    r = c.post("/api/jobs", json=body())
    job_id = r.json()["job_id"]
    deps = services.worker_deps(svc)
    assert runner.step(job_id, deps) == {"again": 1}                    # start step
    assert svc.store.set(f"lock:{job_id}", "1", ex=60, nx=True)        # simulate a concurrent delivery
    assert runner.step(job_id, deps) == {"again": 15}
    svc.store.delete(f"lock:{job_id}")
    deps.clock = lambda: 10 ** 12                                        # far in the future
    runner.step(job_id, deps)
    j = jobs.get_job(svc.store, job_id)
    assert j["status"] == "failed" and j["error"]["code"] == "timeout"


def test_unknown_and_malformed_job_ids(make_client):
    c, _ = make_client()
    assert c.get("/api/jobs/doesnotexist").status_code == 404
    assert c.get("/api/jobs/aaaaaaaaaaaa").status_code == 404
    assert c.get("/api/jobs/..%2F..%2Fetc").status_code in (404, 422)
    assert c.get("/files/../../etc/passwd").status_code in (404, 400)


# ------------------------------------------------------------------ rate limiting
def test_rate_limit_jobs_per_hour(make_client):
    c, svc = make_client(RATE_JOBS_PER_HOUR=2, GLOBAL_JOBS_PER_DAY=100)
    for i in range(2):
        r = c.post("/api/jobs", json=body(post=(f"2018-08-{15 + i:02d}", f"2018-08-{25 + i:02d}")))
        assert r.status_code == 202, r.text
        wait_done(c, r.json()["job_id"])                                 # finish so the active lock frees
    r = c.post("/api/jobs", json=body(post=("2018-08-17", "2018-08-27")))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert r.json()["error"]["code"] == "rate_limited" and "per hour" in r.json()["error"]["message"]
    # another client is unaffected
    r = c.post("/api/jobs", json=body(post=("2018-08-17", "2018-08-27")), headers={"x-forwarded-for": "9.9.9.9"})
    assert r.status_code == 202


def test_one_active_job_per_client(make_client):
    c, _ = make_client(null_queue=True)
    assert c.post("/api/jobs", json=body()).status_code == 202
    r = c.post("/api/jobs", json=body(post=("2018-08-16", "2018-08-26")))
    assert r.status_code == 429 and "already have an analysis running" in r.json()["error"]["message"]


def test_global_daily_cap(make_client):
    c, _ = make_client(null_queue=True, GLOBAL_JOBS_PER_DAY=1)
    assert c.post("/api/jobs", json=body(), headers={"x-forwarded-for": "1.1.1.1"}).status_code == 202
    r = c.post("/api/jobs", json=body(post=("2018-08-16", "2018-08-26")), headers={"x-forwarded-for": "2.2.2.2"})
    assert r.status_code == 429 and "daily" in r.json()["error"]["message"]


def test_rate_limit_preflight(make_client):
    c, _ = make_client(RATE_PREFLIGHT_PER_HOUR=2)
    assert c.post("/api/preflight", json=body()).status_code == 200
    assert c.post("/api/preflight", json=body()).status_code == 200
    r = c.post("/api/preflight", json=body())
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_rate_limit_window_resets():
    from common.ratelimit import RateLimited, hit
    from common.store import MemoryStore
    t = [1000.0]
    st = MemoryStore(clock=lambda: t[0])
    for _ in range(3):
        hit(st, "x", "c", 3, 60, "m")
    with pytest.raises(RateLimited) as e:
        hit(st, "x", "c", 3, 60, "m")
    assert 0 < e.value.retry_after <= 60
    t[0] += 61
    hit(st, "x", "c", 3, 60, "m")                                         # new window


def test_client_id_is_salted_hash_not_ip():
    from common.ratelimit import client_id
    cid = client_id({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, "salt")
    assert "203" not in cid and len(cid) == 16
    assert cid != client_id({"x-forwarded-for": "203.0.113.7"}, "other-salt")


# ------------------------------------------------------------------ stores / queue / auth adapters (no network)
def test_upstash_store_commands(monkeypatch):
    from common import store
    calls = []

    class R:
        def __init__(self, result):
            self._r = result

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": self._r}

    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers))
        return R("OK" if json[0] == "SET" else 1)
    monkeypatch.setattr(store.requests, "post", fake_post)
    s = store.UpstashStore("https://x.upstash.io/", "tok")
    assert s.set("k", "v", ex=10, nx=True) is True
    assert calls[0][1] == ["SET", "k", "v", "EX", "10", "NX"]
    assert s.incr("c", ex=60) == 1 and calls[-1][1] == ["EXPIRE", "c", "60"]
    assert calls[0][2] == {"Authorization": "Bearer tok"}


def test_cloud_tasks_payload():
    from common.config import load_settings
    from common.queue import CloudTasksQueue
    posted = {}

    class Sess:
        def post(self, url, json, timeout):
            posted.update(url=url, json=json)
            return type("R", (), {"status_code": 200})()
    s = load_settings()
    s.tasks_project, s.worker_url, s.tasks_invoker_sa = "proj", "https://w.run.app/", "inv@proj.iam.gserviceaccount.com"
    q = CloudTasksQueue.__new__(CloudTasksQueue)
    q.s, q.session = s, Sess()
    q.endpoint = "https://cloudtasks.googleapis.com/v2/projects/proj/locations/l/queues/q/tasks"
    q.enqueue("abc123", delay=20)
    t = posted["json"]["task"]
    assert t["httpRequest"]["url"] == "https://w.run.app/step"
    assert json.loads(base64.b64decode(t["httpRequest"]["body"])) == {"job_id": "abc123"}
    assert t["httpRequest"]["oidcToken"]["serviceAccountEmail"].startswith("inv@")
    assert "scheduleTime" in t and t["dispatchDeadline"] == "900s"


def test_service_account_parsing_never_leaks_key():
    from common import google_auth
    key = {"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----SECRETSECRET", "client_email": "a@b"}
    assert google_auth.service_account_info(json.dumps(key))["client_email"] == "a@b"
    assert google_auth.service_account_info(base64.b64encode(json.dumps(key).encode()).decode())["client_email"] == "a@b"
    with pytest.raises(RuntimeError) as e:
        google_auth.service_account_info(json.dumps({"type": "authorized_user", "refresh_token": "SECRETSECRET"}))
    assert "SECRETSECRET" not in str(e.value)
    with pytest.raises(RuntimeError):
        google_auth.service_account_info("")


def test_local_storage_rejects_traversal(tmp_path):
    from common.storage import LocalStorage
    st = LocalStorage(tmp_path)
    for bad in ("../x", "/etc/passwd", "a/../../b"):
        with pytest.raises(ValueError):
            st.put_bytes(bad, b"x")


# ------------------------------------------------------------------ no secrets in the repo
def test_no_credentials_in_repository():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    hits = []
    for f in files:
        if f.endswith((".png", ".tif", ".geojson", ".css", ".js")) and "vendor" in f or f.startswith("data/fixtures"):
            continue
        try:
            text = open(os.path.join(ROOT, f), errors="ignore").read()
        except OSError:
            continue
        for needle in ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY", '"private_key"', "refresh_token\":", "AIza", "ya29."):
            if needle in text and not f.startswith("tests/"):
                hits.append((f, needle))
    assert not hits, hits
    assert ".env" in open(os.path.join(ROOT, ".gitignore")).read()


# ------------------------------------------------------------------ shared window rules (same cases run in JS)
def test_window_rules_match_shared_cases():
    from datetime import date
    from common.config import load_settings
    from common.validation import check_windows
    spec = json.load(open(os.path.join(ROOT, "tests", "fixtures", "window_cases.json")))
    s = load_settings()
    assert [s.pre_min_days, s.pre_max_days] == spec["limits"]["pre_days"]
    assert [s.post_min_days, s.post_max_days] == spec["limits"]["post_days"]
    assert s.max_gap_days == spec["limits"]["max_gap_days"] and s.data_lag_days == spec["limits"]["data_lag_days"]
    for c in spec["cases"]:
        got = [i["code"] for i in check_windows(c["pre"], c["post"], s, today=date.fromisoformat(spec["today"]))]
        assert got == c["codes"], (c["name"], got)


def test_preflight_info_is_cached_between_check_and_run(make_client):
    c, svc = make_client()
    calls = []
    real = svc.info_fn
    svc.info_fn = lambda *a: (calls.append(a), real(*a))[1]
    assert c.post("/api/preflight", json=body()).status_code == 200
    assert c.post("/api/preflight", json=body()).status_code == 200
    assert c.post("/api/jobs", json=body()).status_code == 202
    assert len(calls) == 1                                  # one Earth Engine round trip for all three requests
    assert c.post("/api/preflight", json=body(name="Other District")).status_code == 200
    assert len(calls) == 2                                  # a different region is a different key
