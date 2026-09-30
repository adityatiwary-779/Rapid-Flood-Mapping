"""The static demo (what gets deployed to Vercel) served through a server that applies public/vercel.json's rewrites.
Works for both states of the demo: synthetic imagery (default build) and real imagery (after tools/make_real_previews.py).
The 'real imagery' state is also exercised here on a temporary copy, using stand-in PNGs and stand-in statistics.
This checks routing rules and demo content; it cannot prove Vercel itself behaves the same."""
import csv
import io
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from conftest import ROOT
from static_server import serve
from test_frontend_e2e import CHROME, PNG, _free_port

PUBLIC = os.path.join(ROOT, "public")
PID = "kerala_2018_alappuzha"


def _serve(root):
    port = _free_port()
    srv = serve(root, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def demo_url():
    srv, url = _serve(PUBLIC)
    yield url
    srv.shutdown()


@pytest.fixture(scope="module")
def real_demo_url(tmp_path_factory):
    """A copy of public/ with the 'real imagery' script's output applied (stand-in PNGs and numbers)."""
    root = tmp_path_factory.mktemp("realpub") / "public"
    shutil.copytree(PUBLIC, root)
    sys_path = os.path.join(ROOT, "tools")
    import sys
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    from tools import make_real_previews as mrp
    base = json.load(open(os.path.join(root, "demo/api/events", f"{PID}.json")))
    st = dict(base["stats"], flood_unmasked_km2=198.0, flood_km2=150.0, cropland_km2=100.0)
    cfg = dict(base["config"], method="real")
    pngs = {n: PNG for n in mrp.PREVIEWS}
    os.makedirs(os.path.join(root, "files/presets", PID, "previews"), exist_ok=True)
    mrp.apply_real_assets(str(root), PID, st, cfg, [[9.12, 76.27], [9.86, 76.65]], pngs)
    del sys_path
    srv, url = _serve(str(root))
    yield url
    srv.shutdown()


@pytest.fixture(scope="module")
def roads_demo_url(tmp_path_factory):
    """A copy of public/ after tools/add_roads_to_demo.py ran on a synthetic roads_exposure.py output folder."""
    import sys
    sys.path.insert(0, ROOT)
    from tools import add_roads_to_demo as ard
    tmp = tmp_path_factory.mktemp("roadspub")
    root, res = tmp / "public", tmp / "results"
    shutil.copytree(PUBLIC, root)
    res.mkdir()
    (res / "exposure_summary.csv").write_text("place,road_km_total,road_km_flooded,settlements_total,settlements_affected\nAlappuzha,7569.7,117.8,165,22\n")
    (res / "roads_flooded_by_type.csv").write_text("highway,flooded_km\nresidential,60.5\ntertiary,30.3\nprimary,27.0\n")
    (res / "affected_settlements.csv").write_text("place,name\nvillage,Test Village\nhamlet,\n")
    line = lambda pts, **pr: {"type": "Feature", "properties": {"element": "way", "id": 1, **pr}, "geometry": {"type": "LineString", "coordinates": pts}}  # noqa: E731
    (res / "flooded_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        line([[76.3401234567, 9.4912345678], [76.3501234567, 9.4912345678]], highway="primary", name="NH 66", flooded_km=1.234567, osmid=5),
        line([[76.3401234567, 9.4512345678], [76.3501234567, 9.4512345678]], highway="residential", flooded_km=0.5)]}))
    pt = lambda x, y, **pr: {"type": "Feature", "properties": pr, "geometry": {"type": "Point", "coordinates": [x, y]}}  # noqa: E731
    (res / "affected_settlements.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        pt(76.3451234567, 9.4912345678, place="village", name="Test Village"), pt(76.3551234567, 9.4912345678, place="hamlet")]}))
    ard.apply_roads(str(root), PID, str(res))
    srv, url = _serve(str(root))
    yield url
    srv.shutdown()


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as p:
        b = p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}), args=["--no-sandbox"])
        yield b
        b.close()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read()


def _manifest(url):
    return json.loads(_get(url + f"/api/events/{PID}")[1])


def test_rewrites_serve_api_paths(demo_url):
    for path in ("/api/config", "/api/regions", "/api/events", f"/api/events/{PID}"):
        status, body = _get(demo_url + path)
        assert status == 200 and json.loads(body), path
    cfg = json.loads(_get(demo_url + "/api/config")[1])
    assert cfg["static_demo"] is True and cfg["limits"]["post_days"] == [7, 14]
    ev = json.loads(_get(demo_url + "/api/events")[1])["events"]
    assert [e["id"] for e in ev] == [PID]
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(demo_url + "/api/events/doesnotexist")
    assert e.value.code == 404


def test_demo_never_ships_invented_data(demo_url):
    m = _manifest(demo_url)
    assert "flood.tif" not in m["files"] and "dvv_db.tif" not in m["files"]      # GeoTIFFs are not in the static demo
    for name in ("flood.tif", "dvv_db.tif"):
        with pytest.raises(urllib.error.HTTPError):
            _get(demo_url + f"/files/presets/{PID}/{name}")
    if m["roads"] is None:                                           # no roads yet: nothing road-related may ship
        for name in ("flooded_roads.geojson", "affected_settlements.geojson"):
            with pytest.raises(urllib.error.HTTPError):
                _get(demo_url + f"/files/presets/{PID}/{name}")
    else:                                                            # roads present: must be real OSM output, never the mock stack's
        r = m["roads"]
        assert r["source"] in ("OpenStreetMap", "local file") and "error" not in r
        assert not any("Sample village" in (x["name"] or "") for x in r["settlements"]) and r["road_km_total"] != 1520.3
        assert 0 <= r["road_km_flooded"] <= r["road_km_total"] and r["settlements_affected"] <= r["settlements_total"]
        assert r["settlements_affected"] == len(r["settlements"]) and abs(sum(b["flooded_km"] for b in r["by_type"]) - r["road_km_flooded"]) < 1.0
    assert "NOT independent validation" in m["caveat"]
    st = m["stats"]
    if m["mock"]:                                                    # default build: synthetic radar, mock-only layers stripped
        assert m["stats"]["flood_unmasked_km2"] is None and set(m["previews"]) == {"pre_vv", "post_vv", "flood"}
        assert any(w["code"] == "static_demo" and w["level"] == "warning" for w in m["warnings"])
        assert st["flood_km2"] == pytest.approx(151.1338, abs=1e-3) and st["rule_based_km2"] == pytest.approx(83.1698, abs=1e-3)
        assert st["cropland_km2"] == pytest.approx(101.8527, abs=1e-3) and st["population_exposed"] == pytest.approx(73575.02, abs=0.1)
        assert st["threshold_db"] == pytest.approx(-3.9002, abs=1e-3)
    else:                                                            # after make_real_previews.py
        assert set(m["previews"]) >= {"pre_vv", "post_vv", "flood", "flood_unmasked", "severity"}     # flood_rule optional (older exports)
        assert 50 < st["flood_km2"] < 400 and st["flood_unmasked_km2"] >= st["rule_based_km2"]   # masks can only REMOVE area from the threshold result
        assert not any("synthetic" in w["message"].lower() for w in m["warnings"])
    row = next(csv.DictReader(io.StringIO(_get(demo_url + m["files"]["stats.csv"])[1].decode())))
    assert float(row["flood_km2"]) == pytest.approx(st["flood_km2"], abs=1e-6)
    assert (row["flood_unmasked_km2"] == "") == (st.get("flood_unmasked_km2") is None)     # CSV and manifest agree
    for url in list(m["files"].values()) + list(m["previews"].values()):
        assert _get(demo_url + url)[0] == 200, url


def test_real_imagery_mode_content(real_demo_url):
    m = _manifest(real_demo_url)
    assert m["mock"] is False and set(m["previews"]) == {"pre_vv", "post_vv", "flood", "flood_rule", "flood_unmasked", "severity"}
    assert m["stats"]["flood_unmasked_km2"] == 198.0 and m["stats"]["other_km2"] == pytest.approx(150 - 100 - m["stats"]["builtup_km2"] - m["stats"]["tree_cover_km2"])
    assert m["warnings"][0]["level"] == "info" and "synthetic" not in m["warnings"][0]["message"].lower()
    assert json.loads(_get(real_demo_url + "/api/config")[1])["mode"] == "static"
    assert json.loads(_get(real_demo_url + "/api/events")[1])["events"][0]["mock"] is False
    row = next(csv.DictReader(io.StringIO(_get(real_demo_url + m["files"]["stats.csv"])[1].decode())))
    assert row["flood_unmasked_km2"] == "198.0" and row["flood_km2"] == "150.0"
    assert json.loads(_get(real_demo_url + m["files"]["config.json"])[1])["method"] == "real"
    for u in m["previews"].values():
        assert _get(real_demo_url + u)[1][:8] == b"\x89PNG\r\n\x1a\n"


def _browse(pw, url, tmp_path):
    ctx = pw.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.fulfill(status=200, content_type="image/png", body=PNG))
    pg = ctx.new_page()
    errors, api_calls = [], []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("request", lambda r: api_calls.append(r.url) if "/api/" in r.url else None)
    m = _manifest(url)
    real = not m["mock"]
    pg.goto(url + "/")
    pg.wait_for_selector("#presetEvent option", state="attached")
    assert pg.locator("#tabCustom").is_disabled() and "not switched on" in pg.locator("#staticNote").inner_text()
    assert pg.locator("#presetEvent option").all_inner_texts() == ["Kerala floods 2018"]
    pg.click("#btnLoadPreset")
    pg.wait_for_selector("#content:not([hidden])")
    pg.wait_for_function("document.getElementById('mapLoading').hidden", timeout=15000)
    kp = lambda label: pg.locator(".kpi").filter(has=pg.locator(".k", has_text=re.compile(f"^{label}$"))).inner_text()  # noqa: E731
    st = m["stats"]
    assert f"{round(st['flood_km2'])}" in kp("Flooded area")
    has_roads = m["roads"] is not None
    if has_roads:
        shown = lambda v: str(round(v)) if v >= 100 else (f"{v:.1f}" if v >= 1 else f"{v:.2f}")  # noqa: E731  (same rule as the UI: 118, 51.4, 0.03)
        assert shown(m["roads"]["road_km_flooded"]) in kp("Flooded roads") and str(m["roads"]["settlements_affected"]) in kp("Affected settlements")
        assert pg.locator("#roadBox li").count() == min(200, len(m["roads"]["settlements"]))
    else:
        assert "n/a" in kp("Flooded roads") and "not available" in pg.locator("#roadBox").inner_text()
    assert "not independent validation" in pg.locator("#caveatTop").inner_text()
    assert "NOT independent validation" in pg.locator("#caveatBottom").inner_text()
    banner_visible = pg.locator("#mockBanner").is_visible()
    chips = pg.locator("#evChips").inner_text()
    chip = pg.locator("#modeChip").inner_text()
    if real:
        assert not banner_visible and "Demo data" not in chips and chip == "Static demo"
        assert "rendered by Earth Engine" in pg.locator("#warnings").inner_text()
        assert pg.locator("input[name=floodMode][value=unmasked]").is_enabled() and pg.locator("#lyrSeverity").is_enabled()
        assert pg.locator("input[name=floodMode][value=rule]").is_enabled() == ("flood_rule" in m["previews"])
        pg.check("input[name=floodMode][value=unmasked]")
        assert pg.evaluate("floodApp.state.map.hasLayer(floodApp.state.layers.flood_unmasked)")
        pg.check("#lyrSeverity")
        assert pg.evaluate("floodApp.state.map.hasLayer(floodApp.state.layers.severity)")
        pg.check("input[name=floodMode][value=masked]")
    else:
        assert banner_visible and "Demo data" in chips
        assert pg.locator("input[name=floodMode][value=unmasked]").is_disabled() and pg.locator("#lyrSeverity").is_disabled()
        assert "Static demo" in pg.locator("#warnings").inner_text()
    assert pg.locator("#lyrRoads").is_disabled() == (not has_roads) and pg.locator("#lyrSettle").is_disabled() == (not has_roads)
    if has_roads:
        pg.check("#lyrRoads")
        pg.wait_for_function("(() => { let n = 0; floodApp.state.map.eachLayer(l => { if (l.feature) n++; }); return n > 0; })()", timeout=10000)
        pg.check("#lyrSettle")
        pg.wait_for_function("(() => { let n = 0; floodApp.state.map.eachLayer(l => { if (l.feature && l.feature.geometry.type === 'Point') n++; }); return n > 0; })()", timeout=10000)
        assert "Flooded roads" in pg.locator("#legend").inner_text()
    box, h = pg.locator("#map").bounding_box(), pg.locator("#swipe").bounding_box()
    pg.mouse.move(h["x"] + h["width"] / 2, h["y"] + 100)
    pg.mouse.down()
    pg.mouse.move(box["x"] + box["width"] * 0.7, h["y"] + 100, steps=5)
    pg.mouse.up()
    assert pg.evaluate("floodApp.state.ratio") == pytest.approx(0.7, abs=0.02)
    with pg.expect_download() as d:
        pg.locator("#downloads a", has_text="Stats CSV").click()
    d.value.save_as(tmp_path / "s.csv")
    assert "flood_km2" in (tmp_path / "s.csv").read_text()
    if has_roads:
        with pg.expect_download() as d:
            pg.locator("#downloads a", has_text="Flooded roads").click()
        d.value.save_as(tmp_path / "r.geojson")
        assert json.loads((tmp_path / "r.geojson").read_text())["features"]
    with pg.expect_download() as d:
        pg.locator("#downloads a", has_text="Flood polygons").click()
    d.value.save_as(tmp_path / "f.geojson")
    assert json.loads((tmp_path / "f.geojson").read_text())["type"] == "FeatureCollection"
    wanted = ["stats.csv", "flood.tif", "dvv_db.tif", "flood_vec.geojson", "flooded_roads.geojson",
              "affected_settlements.geojson", "roads_flooded_by_type.csv", "config.json"]
    have = sum(n in m["files"] for n in wanted)
    assert pg.locator("#downloads a").count() == have and pg.locator("#downloads span[aria-disabled]").count() == len(wanted) - have
    pg.click("#btnSummary")
    txt = pg.locator("#summaryText").inner_text()
    assert f"about {round(st['flood_km2'])} km²" in txt and "NOT independent validation" in txt
    assert ("permanent-water masks removed" in txt) == real and "would show" not in txt
    assert ("km of roads" in txt) == has_roads                        # roads paragraph only when real roads exist
    assert errors == [], errors
    assert all(u.split("?")[0].split("/api/")[1] in ("config", "regions", "events", f"events/{PID}") for u in api_calls), api_calls
    ctx.close()


def test_static_demo_in_browser(pw, demo_url, tmp_path):
    _browse(pw, demo_url, tmp_path)


def test_static_demo_with_real_imagery_in_browser(pw, real_demo_url, tmp_path):
    _browse(pw, real_demo_url, tmp_path)


def test_deep_link_and_phone(pw, demo_url):
    ctx = pw.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.fulfill(status=200, content_type="image/png", body=PNG))
    pg = ctx.new_page()
    pg.goto(demo_url + f"/#event={PID}")
    pg.wait_for_selector("#content:not([hidden])")
    pg.wait_for_function("document.getElementById('mapLoading').hidden", timeout=15000)
    assert pg.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0
    assert pg.locator("#swipe").bounding_box()["width"] >= 44
    ctx.close()


def test_make_real_previews_refuses_placeholder_project():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "tools/make_real_previews.py", "--project", "YOUR_PROJECT"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "placeholder" in (r.stdout + r.stderr) and "Traceback" not in r.stderr


def test_add_roads_tool_output(roads_demo_url):
    m = _manifest(roads_demo_url)
    r = m["roads"]
    assert (r["road_km_total"], r["road_km_flooded"], r["settlements_total"], r["settlements_affected"]) == (7569.7, 117.8, 165, 22)
    assert r["source"] == "OpenStreetMap" and r["motorable_only"] is True and r["settlement_buffer_m"] == 250
    assert r["by_type"][0] == {"highway": "residential", "flooded_km": 60.5}
    assert r["settlements"] == [{"name": "Test Village", "place": "village"}, {"name": None, "place": "hamlet"}]
    for n in ("flooded_roads.geojson", "affected_settlements.geojson", "roads_flooded_by_type.csv", "affected_settlements.csv"):
        assert m["files"][n].endswith(n) and _get(roads_demo_url + m["files"][n])[0] == 200
    gj = json.loads(_get(roads_demo_url + m["files"]["flooded_roads.geojson"])[1])
    f0 = gj["features"][0]
    assert f0["properties"] == {"highway": "primary", "name": "NH 66", "flooded_km": 1.235}          # slimmed properties
    assert f0["geometry"]["coordinates"][0] == [76.34012, 9.49123]                                      # ~1 m precision
    info = [w for w in m["warnings"] if w["code"] == "static_demo"]
    assert len(info) == 1 and "OpenStreetMap" in info[0]["message"] and "within 250 m" in info[0]["message"]
    assert "not included here" not in info[0]["message"]


def test_static_demo_with_roads_in_browser(pw, roads_demo_url, tmp_path):
    _browse(pw, roads_demo_url, tmp_path)


def test_add_roads_tool_complains_about_missing_files(tmp_path):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "tools/add_roads_to_demo.py", "--results", str(tmp_path)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "roads_exposure.py first" in (r.stdout + r.stderr) and "Traceback" not in r.stderr
