"""The static demo (what gets deployed to Vercel) served through a server that applies public/vercel.json's rewrites.
This checks our routing rules and the demo content; it cannot prove Vercel itself behaves the same."""
import json
import os
import threading
import urllib.request
import csv
import io

import pytest
from playwright.sync_api import sync_playwright

from conftest import ROOT
from static_server import serve
from test_frontend_e2e import CHROME, PNG, _free_port

PUBLIC = os.path.join(ROOT, "public")


@pytest.fixture(scope="module")
def demo_url():
    port = _free_port()
    srv = serve(PUBLIC, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
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


def test_rewrites_serve_api_paths(demo_url):
    for path in ("/api/config", "/api/regions", "/api/events", "/api/events/kerala_2018_alappuzha"):
        status, body = _get(demo_url + path)
        assert status == 200 and json.loads(body), path
    cfg = json.loads(_get(demo_url + "/api/config")[1])
    assert cfg["static_demo"] is True and cfg["limits"]["post_days"] == [7, 14]
    ev = json.loads(_get(demo_url + "/api/events")[1])["events"]
    assert [e["id"] for e in ev] == ["kerala_2018_alappuzha"]
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(demo_url + "/api/events/doesnotexist")
    assert e.value.code == 404


def test_demo_contains_no_fabricated_data(demo_url):
    m = json.loads(_get(demo_url + "/api/events/kerala_2018_alappuzha")[1])
    assert m["roads"] is None and m["stats"]["flood_unmasked_km2"] is None
    assert set(m["previews"]) == {"pre_vv", "post_vv", "flood"} and "flood.tif" not in m["files"] and "dvv_db.tif" not in m["files"]
    assert m["mock"] is True and any(w["code"] == "static_demo" for w in m["warnings"]) and "NOT independent validation" in m["caveat"]
    st = m["stats"]                                               # real pipeline numbers from the user's own run
    assert st["flood_km2"] == pytest.approx(151.1338, abs=1e-3) and st["rule_based_km2"] == pytest.approx(83.1698, abs=1e-3)
    assert st["cropland_km2"] == pytest.approx(101.8527, abs=1e-3) and st["population_exposed"] == pytest.approx(73575.02, abs=0.1)
    assert st["threshold_db"] == pytest.approx(-3.9002, abs=1e-3)
    row = next(csv.DictReader(io.StringIO(_get(demo_url + m["files"]["stats.csv"])[1].decode())))
    assert row["flood_unmasked_km2"] == "" and float(row["flood_km2"]) == pytest.approx(151.1338, abs=1e-3)
    for url in list(m["files"].values()) + list(m["previews"].values()):
        assert _get(demo_url + url)[0] == 200, url
    # files that must not ship at all
    for name in ("flooded_roads.geojson", "affected_settlements.geojson", "flood.tif", "dvv_db.tif"):
        with pytest.raises(urllib.error.HTTPError):
            _get(demo_url + "/files/presets/kerala_2018_alappuzha/" + name)


def test_static_demo_in_browser(pw, demo_url, tmp_path):
    ctx = pw.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.fulfill(status=200, content_type="image/png", body=PNG))
    pg = ctx.new_page()
    errors, api_calls = [], []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("request", lambda r: api_calls.append(r.url) if "/api/" in r.url else None)
    pg.goto(demo_url + "/")
    pg.wait_for_selector("#presetEvent option", state="attached")
    # custom dates are disabled, with an explanation
    assert pg.locator("#tabCustom").is_disabled() and "not switched on" in pg.locator("#staticNote").inner_text()
    assert pg.locator("#presetEvent option").all_inner_texts() == ["Kerala floods 2018"]
    pg.click("#btnLoadPreset")
    pg.wait_for_selector("#content:not([hidden])")
    pg.wait_for_function("document.getElementById('mapLoading').hidden", timeout=15000)
    kp = lambda label: pg.locator(".kpi").filter(has=pg.locator(".k", has_text=__import__("re").compile(f"^{label}$"))).inner_text()  # noqa: E731
    assert "151" in kp("Flooded area") and "102" in kp("Cropland") and "73,600" in kp("Population exposed")
    assert "n/a" in kp("Flooded roads")                                            # honest: no road data in this demo
    assert "not available" in pg.locator("#roadBox").inner_text()
    assert "Demo data" in pg.locator("#mockBanner").inner_text() and pg.locator("#mockBanner").is_visible()
    assert "not independent validation" in pg.locator("#caveatTop").inner_text()
    assert "NOT independent validation" in pg.locator("#caveatBottom").inner_text()
    assert "Static demo" in pg.locator("#warnings").inner_text()
    # controls for missing data are disabled, not silently dead
    for sel in ("input[name=floodMode][value=unmasked]", "#lyrSeverity", "#lyrRoads", "#lyrSettle"):
        assert pg.locator(sel).is_disabled(), sel
    assert pg.locator("input[name=floodMode][value=masked]").is_checked()
    # slider works
    box, h = pg.locator("#map").bounding_box(), pg.locator("#swipe").bounding_box()
    pg.mouse.move(h["x"] + h["width"] / 2, h["y"] + 100); pg.mouse.down()
    pg.mouse.move(box["x"] + box["width"] * 0.7, h["y"] + 100, steps=5); pg.mouse.up()
    assert pg.evaluate("floodApp.state.ratio") == pytest.approx(0.7, abs=0.02)
    # downloads that exist work; the rest are shown as unavailable
    with pg.expect_download() as d:
        pg.locator("#downloads a", has_text="Stats CSV").click()
    d.value.save_as(tmp_path / "s.csv")
    assert "flood_km2" in (tmp_path / "s.csv").read_text()
    with pg.expect_download() as d:
        pg.locator("#downloads a", has_text="Flood polygons").click()
    d.value.save_as(tmp_path / "f.geojson")
    assert json.loads((tmp_path / "f.geojson").read_text())["type"] == "FeatureCollection"
    assert pg.locator("#downloads a").count() == 3 and pg.locator("#downloads span[aria-disabled]").count() == 5
    # summary works and stays honest
    pg.click("#btnSummary")
    txt = pg.locator("#summaryText").inner_text()
    assert "about 151 km²" in txt and "NOT independent validation" in txt and "roads" not in txt.lower().replace("no road", "")
    assert "Without the terrain" not in txt
    assert errors == [], errors
    assert all(u.split("?")[0].split("/api/")[1] in ("config", "regions", "events", "events/kerala_2018_alappuzha") for u in api_calls), api_calls
    ctx.close()


def test_deep_link_and_phone(pw, demo_url):
    ctx = pw.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.fulfill(status=200, content_type="image/png", body=PNG))
    pg = ctx.new_page()
    pg.goto(demo_url + "/#event=kerala_2018_alappuzha")
    pg.wait_for_selector("#content:not([hidden])")
    pg.wait_for_function("document.getElementById('mapLoading').hidden", timeout=15000)
    assert pg.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0
    assert pg.locator("#swipe").bounding_box()["width"] >= 44
    ctx.close()
