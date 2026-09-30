"""Tests for the Stage 3 tools, on the mock stack / stand-in ee (no cloud accounts)."""
import json
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from conftest import ROOT


def test_precompute_presets_dry_run_on_mock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MOCK_DIR", str(tmp_path))
    monkeypatch.setenv("BACKEND_MODE", "mock")
    from tools import precompute_presets
    precompute_presets.main(["--mock", "--only", "assam_2022_nagaon", "--poll-scale", "0"])
    out = capsys.readouterr().out
    assert "building assam_2022_nagaon" in out and "built: assam_2022_nagaon" in out
    idx = json.loads((tmp_path / "presets" / "index.json").read_text())
    assert [e["id"] for e in idx["events"]] == ["assam_2022_nagaon"]
    man = json.loads((tmp_path / "presets" / "assam_2022_nagaon" / "manifest.json").read_text())
    assert man["kind"] == "preset" and man["title"] == "Assam floods 2022 · Nagaon"
    assert (tmp_path / "presets" / "assam_2022_nagaon" / "flood_vec.geojson").exists()
    precompute_presets.main(["--mock", "--only", "assam_2022_nagaon"])                # second run: skipped
    assert "already built" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        precompute_presets.main(["--mock", "--only", "nope"])


def test_precompute_reports_failure_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MOCK_DIR", str(tmp_path))
    from common import presets
    from tools import precompute_presets
    bad = [{"id": "bad_fail", "event": "E", "district": "FailStart District",
            "spec": {"region": {"country": "India", "level": 2, "parent": "X", "name": "FailStart District"},
                     "pre": ["2018-07-01", "2018-07-30"], "post": ["2018-08-15", "2018-08-25"]}}]
    monkeypatch.setattr(presets, "load_presets", lambda *a, **k: bad)
    with pytest.raises(SystemExit) as e:
        precompute_presets.main(["--mock", "--poll-scale", "0"])
    assert e.value.code == 1 and "failed: bad_fail" in capsys.readouterr().out


def test_build_regions_output_and_preset_check():
    from tools import build_regions
    fp = MagicMock()
    fp.list_regions.return_value = ["Kerala", "Assam"]
    lists = {"Kerala": [["Alappuzha", 1316.694], ["Idukki", 4358.1]], "Assam": [["Nagaon", 3831.2]]}
    state = {}
    fp.gaul.side_effect = lambda country, level, st: state.__setitem__("s", st) or MagicMock()

    def reduce_cols(*a, **k):
        m = MagicMock()
        m.get.return_value.getInfo.side_effect = lambda: lists[state["s"]]
        return m
    fp.gaul.return_value.map.return_value.reduceColumns.side_effect = reduce_cols
    fp.gaul.side_effect = lambda country, level, st: (state.__setitem__("s", st), fp.gaul.return_value)[1]
    reg = build_regions.build(fp, "India", log=lambda *_: None)
    rows = reg["India"]["2"]
    assert [r["name"] for r in rows] == ["Nagaon", "Alappuzha", "Idukki"] and rows[1]["area_km2"] == 1316.69
    presets = [{"id": "a", "spec": {"region": {"parent": "Kerala", "name": "Alappuzha"}}},
               {"id": "b", "spec": {"region": {"parent": "Assam", "name": "Nagaon"}}},
               {"id": "c", "spec": {"region": {"parent": "Tamil Nadu", "name": "Chennai"}}}]
    problems = build_regions.check_presets(reg, presets)
    assert len(problems) == 1 and "c:" in problems[0] and "Chennai" in problems[0]


def test_integration_check_api_mode_against_local_server(tmp_path):
    """The api checks (health, config, presets, bad request, preflight, one live job) against a mock server."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "BACKEND_MODE": "mock", "MOCK_DIR": str(tmp_path), "RATE_JOBS_PER_HOUR": "50",
           "RATE_PREFLIGHT_PER_HOUR": "500", "GLOBAL_JOBS_PER_DAY": "500"}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.index:app", "--port", str(port), "--log-level", "warning"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import urllib.request
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(.25)
        r = subprocess.run([sys.executable, "tools/integration_check.py", "api", "--url", f"http://127.0.0.1:{port}",
                            "--run-job", "--timeout", "2"], cwd=ROOT, capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        for name in ("GET /api/health", "GET /api/events (presets)", "bad request rejected", "live custom job"):
            assert any(l.startswith("PASS") and name in l for l in r.stdout.splitlines()), (name, r.stdout)
        assert "0 failed" in r.stdout
        # without --run-job the live job is skipped, not silently passed
        r2 = subprocess.run([sys.executable, "tools/integration_check.py", "api", "--url", f"http://127.0.0.1:{port}"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert any(l.startswith("SKIP") and "live custom job" in l for l in r2.stdout.splitlines())
    finally:
        srv.terminate()


def test_integration_check_fails_loudly_and_never_prints_secrets():
    secret = "SUPERSECRETKEYMATERIAL" + "x" * 40
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EE_", "GCS_", "UPSTASH", "WORKER", "TASKS"))}
    env["EE_SERVICE_ACCOUNT_JSON"] = secret
    r = subprocess.run([sys.executable, "tools/integration_check.py", "services"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "FAIL" in r.stdout and "missing:" in r.stdout
    assert secret not in r.stdout + r.stderr
    from tools import integration_check as ic
    assert "SUPERSECRET" not in ic.redact("bad key " + secret) and "<redacted>" in ic.redact("bad key " + secret)


def test_tools_refuse_placeholders_and_explain_missing_setup():
    def run(*args, env=None):
        return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env={**{k: v for k, v in os.environ.items() if not k.startswith(("EE_", "GCS_", "UPSTASH", "WORKER", "TASKS"))}, **(env or {})})
    r = run("tools/integration_check.py", "api", "--url", "https://YOUR-APP.vercel.app")
    assert r.returncode == 2 and "placeholder" in r.stdout and "FAIL" not in r.stdout          # no request was made
    r = run("tools/build_regions.py", "--project", "YOUR_PROJECT")
    assert r.returncode != 0 and "placeholder" in (r.stdout + r.stderr) and "Traceback" not in r.stderr
    r = run("tools/precompute_presets.py")
    assert r.returncode != 0 and "Traceback" not in r.stderr and "DEPLOY.md" in r.stderr and "--mock" in r.stderr
    from tools import integration_check as ic
    msg = ic.redact("missing: EE_PROJECT, UPSTASH_REDIS_REST_TOKEN, WORKER_URL")
    assert "UPSTASH_REDIS_REST_TOKEN" in msg                                                    # env var NAMES stay readable
    assert "abc123" not in ic.redact("bad token=abc123 and private_key: abc123")
