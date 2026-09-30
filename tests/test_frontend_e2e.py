"""Browser tests (real Chromium via Playwright) against a real uvicorn server in MOCK mode.
Run: python -m pytest tests/test_frontend_e2e.py   (needs `pip install playwright` + Chromium)
External map tiles are stubbed, so these tests need no internet. They test the UI + API together;
they do not test real Earth Engine."""
import itertools
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from conftest import ROOT

_SANDBOX_CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
CHROME = os.environ.get("CHROME_PATH") or (_SANDBOX_CHROME if os.path.exists(_SANDBOX_CHROME) else None)   # None = Playwright's own Chromium
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
_ips = itertools.count(1)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(tmp, **env):
    port = _free_port()
    e = {**os.environ, "BACKEND_MODE": "mock", "MOCK_DIR": str(tmp), "RATE_JOBS_PER_HOUR": "500",
         "GLOBAL_JOBS_PER_DAY": "5000", "RATE_PREFLIGHT_PER_HOUR": "500", **{k: str(v) for k, v in env.items()}}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.index:app", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1)
            return proc, url
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    proc, url = _start_server(tmp_path_factory.mktemp("srv"))
    yield url
    proc.terminate()


@pytest.fixture(scope="module")
def limited_server(tmp_path_factory):
    proc, url = _start_server(tmp_path_factory.mktemp("lim"), RATE_JOBS_PER_HOUR=1)
    yield url
    proc.terminate()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}), args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture
def make_page(browser):
    """make_page(viewport=..., mobile=False) -> page. Fresh client id per page; external tiles stubbed."""
    made = []

    def make(viewport=None, mobile=False):
        ctx = browser.new_context(viewport=viewport or {"width": 1366, "height": 900}, is_mobile=mobile, has_touch=mobile,
                                  accept_downloads=True, permissions=["clipboard-read", "clipboard-write"] if not mobile else [])
        ctx.set_extra_http_headers({"x-forwarded-for": f"10.1.{next(_ips)}.7"})
        ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.fulfill(status=200, content_type="image/png", body=PNG))
        pg = ctx.new_page()
        pg.errors = []
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: pg.errors.append("PAGEERROR " + str(e)))
        made.append(ctx)
        return pg
    yield make
    for c in made:
        c.close()


def open_preset(pg, url, event="kerala_2018_alappuzha"):
    pg.goto(f"{url}/#event={event}")
    pg.wait_for_selector("#content:not([hidden])", timeout=20000)
    pg.wait_for_function("document.getElementById('mapLoading').hidden", timeout=15000)


def kpi(pg, label):
    import re
    return pg.locator(".kpi").filter(has=pg.locator(".k", has_text=re.compile(rf"^{label}$"))).inner_text()


# ------------------------------------------------------------------ preset flow: every feature visible
def test_preset_shows_all_features(make_page, server):
    pg = make_page()
    open_preset(pg, server)
    assert pg.locator("#evTitle").inner_text() == "Kerala floods 2018 · Alappuzha"
    assert "Demo data" in pg.locator("#mockBanner").inner_text() and pg.locator("#mockBanner").is_visible()
    # caveat: sticky at the top AND repeated under the results
    assert "not independent validation" in pg.locator("#caveatTop").inner_text()
    assert "Copernicus EMS" in pg.locator("#caveatTop").inner_text()
    assert "NOT independent validation" in pg.locator("#caveatBottom").inner_text()
    pg.evaluate("window.scrollTo(0, 3000)")
    pg.wait_for_timeout(200)
    assert pg.locator("#caveatTop").bounding_box()["y"] < 5                       # still pinned while scrolled
    # KPIs (feature 5): areas, population, roads km, settlements
    assert "151" in kpi(pg, "Flooded area") and "11%" in kpi(pg, "Flooded area")
    assert "102" in kpi(pg, "Cropland") and "0.03" in kpi(pg, "Built-up") and "3.3" in kpi(pg, "Tree cover")
    assert "73,600" in kpi(pg, "Population exposed")
    assert "51.4" in kpi(pg, "Flooded roads") and "4" in kpi(pg, "Affected settlements")
    assert pg.locator("#compList li").count() == 4 and pg.locator("#roadBox li").count() == 4
    assert "residential" in pg.locator("#roadBox").inner_text()
    # method + masks shown (features 2 and 3)
    meth = pg.locator("#method").inner_text()
    assert "-3.90 dB" in meth and "Descending" in meth and "6 before, 2 after" in meth and "not independent validation" in meth
    masks = pg.locator("#masks").inner_text()
    assert "5°" in masks and "15 m" in masks and "80%" in masks and "10 pixels" in masks
    assert "Before · 1–31 Jul 2018" == pg.locator("#lblBefore").inner_text()
    assert pg.evaluate("getComputedStyle(document.getElementById('mapLoading')).display") == "none"
    assert pg.errors == []


# ------------------------------------------------------------------ swipe slider (feature 4)
def test_swipe_slider_mouse_and_keyboard(make_page, server):
    pg = make_page()
    open_preset(pg, server)
    clip = lambda pane: pg.evaluate(f"floodApp.state.map.getPane('{pane}').style.clip")  # noqa: E731
    box, h = pg.locator("#map").bounding_box(), pg.locator("#swipe").bounding_box()
    pg.mouse.move(h["x"] + h["width"] / 2, h["y"] + h["height"] / 2)
    pg.mouse.down()
    pg.mouse.move(box["x"] + box["width"] * 0.25, h["y"] + 80, steps=6)
    pg.mouse.up()
    assert pg.evaluate("floodApp.state.ratio") == pytest.approx(0.25, abs=0.01)
    assert pg.locator("#swipe").get_attribute("aria-valuenow") == "25"
    import re
    nums = lambda c: [float(x) for x in re.findall(r"-?\d+\.?\d*", c)]  # noqa: E731
    l, r = nums(clip("swipeL")), nums(clip("swipeR"))          # rect(top, right, bottom, left)
    assert l[1] == pytest.approx(r[3], abs=0.5)                # left pane ends exactly where right pane starts
    width = r[1] - l[3]
    assert (l[1] - l[3]) / width == pytest.approx(0.25, abs=0.02)
    pg.locator("#swipe").focus()
    pg.keyboard.press("ArrowRight")
    assert pg.evaluate("floodApp.state.ratio") == pytest.approx(0.27, abs=0.005)
    pg.keyboard.press("Shift+ArrowRight")
    assert pg.evaluate("floodApp.state.ratio") == pytest.approx(0.37, abs=0.005)
    pg.keyboard.press("Home")
    assert pg.evaluate("floodApp.state.ratio") == 0 and pg.locator("#swipe").get_attribute("aria-valuenow") == "0"
    pg.keyboard.press("End")
    assert pg.evaluate("floodApp.state.ratio") == 1
    # panning/zooming the map keeps the clip consistent with the slider
    pg.evaluate("floodApp.setRatio(0.5); floodApp.state.map.panBy([120, 40], {animate: false})")
    l2, r2 = nums(clip("swipeL")), nums(clip("swipeR"))
    assert (l2[1] - l2[3]) / (r2[1] - l2[3]) == pytest.approx(0.5, abs=0.02)
    assert pg.errors == []


def test_left_and_right_show_different_imagery(make_page, server):
    """Pixel check: pre-flood radar on the left half, post-flood radar + flood overlay on the right half."""
    pg = make_page()
    open_preset(pg, server)
    pg.evaluate("floodApp.state.map.fitBounds(floodApp.state.manifest.bounds, {animate: false, padding: [0, 0]}); floodApp.setRatio(0.5)")
    pg.wait_for_timeout(600)
    left_layer = pg.evaluate("floodApp.state.layers.pre_vv.getElement().src")
    right_layer = pg.evaluate("floodApp.state.layers.post_vv.getElement().src")
    assert left_layer.endswith("previews/pre_vv.png") and right_layer.endswith("previews/post_vv.png")
    assert pg.evaluate("floodApp.state.layers.pre_vv.options.pane") == "swipeL"
    assert pg.evaluate("floodApp.state.layers.post_vv.options.pane") == "swipeR"
    assert pg.evaluate("floodApp.state.layers.flood.options.pane") == "swipeR"          # flood only on the "after" side


# ------------------------------------------------------------------ layer toggles (features 3 and 4)
def test_layer_toggles(make_page, server):
    pg = make_page()
    open_preset(pg, server)
    has = lambda name: pg.evaluate(f"floodApp.state.map.hasLayer(floodApp.state.layers.{name})")  # noqa: E731
    assert has("flood") and not has("flood_unmasked") and not has("severity")
    pg.check("input[name=floodMode][value=unmasked]")
    assert has("flood_unmasked") and not has("flood")
    assert "Flood (unmasked)" in pg.locator("#legend").inner_text()
    pg.check("input[name=floodMode][value=off]")
    assert not has("flood") and not has("flood_unmasked")
    pg.check("input[name=floodMode][value=masked]")
    pg.check("#lyrSeverity")
    assert has("severity") and "Backscatter drop" in pg.locator("#legend").inner_text()
    pg.uncheck("#lyrSeverity")
    assert not has("severity")
    pg.evaluate("document.getElementById('radarOpacity').value = 30; document.getElementById('radarOpacity').dispatchEvent(new Event('input'))")
    assert pg.evaluate("floodApp.state.layers.pre_vv.options.opacity") == pytest.approx(0.3)
    pg.check("#lyrRoads")
    pg.wait_for_function("floodApp.state.map.eachLayer && [...Array(1)].length", timeout=5000)
    pg.wait_for_function("(() => { let n = 0; floodApp.state.map.eachLayer(l => { if (l.feature || (l.getLayers && l.getLayers().length)) n++; }); return n > 0; })()", timeout=8000)
    pg.uncheck("#lyrRoads")
    pg.wait_for_function("(() => { let n = 0; floodApp.state.map.eachLayer(l => { if (l.feature) n++; }); return n === 0; })()", timeout=5000)
    assert pg.errors == []


# ------------------------------------------------------------------ downloads + summary (feature 6)
def test_downloads_open_correctly(make_page, server, tmp_path):
    pg = make_page()
    open_preset(pg, server)
    got = {}
    for label, name in (("Stats CSV", "stats.csv"), ("Flood extent GeoTIFF", "flood.tif"), ("Severity GeoTIFF", "dvv_db.tif"),
                        ("Flood polygons GeoJSON", "flood_vec.geojson"), ("Flooded roads GeoJSON", "flooded_roads.geojson"),
                        ("Affected settlements GeoJSON", "affected_settlements.geojson"), ("Method and masks JSON", "config.json")):
        with pg.expect_download() as d:
            pg.locator("#downloads a", has_text=label).click()
        assert d.value.suggested_filename == name
        path = tmp_path / name
        d.value.save_as(path)
        got[name] = path.read_bytes()
    import csv
    import io
    row = next(csv.DictReader(io.StringIO(got["stats.csv"].decode())))
    assert float(row["flood_km2"]) == pytest.approx(151.13, abs=0.05) and "population_exposed" in row and "flood_unmasked_km2" in row
    assert got["flood.tif"][:4] == b"II*\x00" and got["dvv_db.tif"][:4] == b"II*\x00"
    import rasterio
    with rasterio.open(tmp_path / "flood.tif") as ds:
        assert ds.crs.to_epsg() == 4326 and ds.read(1).max() == 1
    for n in ("flood_vec.geojson", "flooded_roads.geojson", "affected_settlements.geojson"):
        assert json.loads(got[n])["type"] == "FeatureCollection"
    assert "masks" in json.loads(got["config.json"])


def test_summary_generate_copy_save(make_page, server, tmp_path):
    pg = make_page()
    open_preset(pg, server)
    assert pg.locator("#summaryBox").is_hidden()
    pg.click("#btnSummary")
    txt = pg.locator("#summaryText").inner_text()
    assert "about 151 km² of flooding in Alappuzha" in txt and "73,600 people" in txt and "51.4 km of roads" in txt
    assert "NOT independent validation" in txt and "Copernicus EMS" in txt
    pg.click("#btnCopy")
    assert pg.evaluate("navigator.clipboard.readText()").startswith("Kerala floods 2018 · Alappuzha:")
    assert "Copied" in pg.locator("#btnCopy").inner_text()
    with pg.expect_download() as d:
        pg.click("#btnSaveTxt")
    assert d.value.suggested_filename == "kerala_2018_alappuzha_summary.txt"
    d.value.save_as(tmp_path / "s.txt")
    assert "about 151 km²" in (tmp_path / "s.txt").read_text()


# ------------------------------------------------------------------ preset picker + hash routing
def test_preset_picker_and_deep_link(make_page, server):
    pg = make_page()
    pg.goto(server + "/")
    pg.wait_for_selector("#presetEvent option", state="attached")
    assert pg.locator("#presetEvent option").all_inner_texts() == ["Kerala floods 2018", "Assam floods 2022", "Chennai floods 2023"]
    pg.select_option("#presetEvent", "Assam floods 2022")
    assert pg.locator("#presetDistrict option").all_inner_texts() == ["Nagaon"]
    assert "18 Jun 2022" in pg.locator("#presetWindows").inner_text()
    assert pg.locator("#empty").is_visible() and pg.locator("#content").is_hidden()
    pg.click("#btnLoadPreset")
    pg.wait_for_selector("#content:not([hidden])")
    assert pg.locator("#evTitle").inner_text() == "Assam floods 2022 · Nagaon" and pg.url.endswith("#event=assam_2022_nagaon")
    pg.goto(server + "/#event=chennai_2023_chennai")          # deep link (same page: hashchange)
    pg.wait_for_function("document.getElementById('evTitle').textContent.includes('Chennai')")
    assert pg.locator("#presetEvent").input_value() == "Chennai floods 2023"


# ------------------------------------------------------------------ custom dates: inline validation (feature 1)
def test_custom_inline_validation(make_page, server):
    pg = make_page()
    pg.goto(server + "/")
    pg.click("#tabCustom")
    pg.wait_for_selector("#paneCustom:not([hidden])")
    assert pg.locator("#cIssues .issue").count() == 0 and pg.locator("#btnRun").is_enabled()
    pg.fill("#postStart", "2018-07-20"); pg.fill("#postEnd", "2018-07-31")             # overlaps the pre window
    assert "overlap" in pg.locator("#cIssues").inner_text() and pg.locator("#btnRun").is_disabled() and pg.locator("#btnCheck").is_disabled()
    pg.fill("#postStart", "2018-08-15"); pg.fill("#postEnd", "2018-08-25")
    assert pg.locator("#btnRun").is_enabled()
    pg.fill("#preEnd", "2018-08-10")                                                    # 41-day pre window
    assert "must be 7-30 days" in pg.locator("#cIssues").inner_text()
    pg.fill("#preEnd", "2018-07-30"); pg.fill("#postEnd", "2018-08-16")               # 2-day post window
    assert "must be 7-14 days" in pg.locator("#cIssues").inner_text()
    pg.fill("#postEnd", "2018-08-25"); pg.fill("#cDistrict", "")
    assert "Enter a state and a district" in pg.locator("#cIssues").inner_text()
    pg.fill("#cDistrict", "Alappuzha"); pg.fill("#postStart", "2099-01-01"); pg.fill("#postEnd", "2099-01-10")
    assert "may not be available yet" in pg.locator("#cIssues").inner_text()


def test_custom_preflight_warnings_and_errors(make_page, server):
    pg = make_page()
    pg.goto(server + "/")
    pg.click("#tabCustom")
    pg.click("#btnCheck")
    pg.wait_for_selector("#preflightOut table")
    out = pg.locator("#preflightOut").inner_text()
    assert "Descending (used)" in out and "1,317 km²" in out and "Ready to run" in out
    pg.fill("#cDistrict", "Sparse District")
    pg.click("#btnCheck")
    pg.wait_for_function("document.getElementById('preflightOut').innerText.includes('few')")
    assert "Only 1 pre-flood and 1 post-flood" in pg.locator("#preflightOut").inner_text()
    pg.fill("#cDistrict", "NoImages District")
    pg.click("#btnCheck")
    pg.wait_for_function("document.getElementById('preflightOut').innerText.includes('No Sentinel-1')")
    pg.fill("#cDistrict", "Huge District")
    pg.click("#btnCheck")
    pg.wait_for_function("document.getElementById('preflightOut').innerText.includes('above the')")
    assert "one district at a time" in pg.locator("#preflightOut").inner_text()
    pg.fill("#cDistrict", "Bad <name>")
    pg.click("#btnRun")
    pg.wait_for_function("document.getElementById('sIssues').innerText.includes('not allowed')")
    txt = pg.locator("#sIssues").inner_text()
    assert "District name contains characters that are not allowed" in txt and "{" not in txt and "Value error" not in txt


# ------------------------------------------------------------------ custom run end to end + failures
def test_custom_run_end_to_end_and_resume(make_page, server):
    pg = make_page()
    pg.goto(server + "/")
    pg.click("#tabCustom")
    pg.fill("#cDistrict", "Alappuzha")
    pg.click("#btnRun")
    pg.wait_for_selector("#jobBox:not([hidden])")
    seen = []
    for _ in range(80):
        seen.append(int(pg.locator("#jobBar").get_attribute("aria-valuenow")))
        if pg.locator("#content").is_visible():
            break
        pg.wait_for_timeout(150)
    assert pg.locator("#content").is_visible(), "results never appeared"
    assert seen == sorted(seen) and max(seen) >= 5
    assert pg.locator("#evTitle").inner_text() == "Alappuzha · 2018-07-01 to 2018-08-25"
    assert "Custom run" in pg.locator("#evChips").inner_text() and "Demo data" in pg.locator("#evChips").inner_text()
    assert "151" in kpi(pg, "Flooded area")
    assert "few images" not in pg.locator("#warnings").inner_text().lower()
    job_hash = pg.evaluate("location.hash")
    assert job_hash.startswith("#job=")
    pg2 = make_page()                                                                    # someone opens the link later
    pg2.goto(server + "/" + job_hash)
    pg2.wait_for_selector("#content:not([hidden])", timeout=15000)
    assert pg2.locator("#evTitle").inner_text() == "Alappuzha · 2018-07-01 to 2018-08-25"
    assert pg.errors == [] and pg2.errors == []


@pytest.mark.parametrize("district,needle", [("FailStart District", "smaller district"), ("FailExport District", "could not finish")])
def test_failed_run_shows_readable_error(make_page, server, district, needle):
    pg = make_page()
    pg.goto(server + "/")
    pg.click("#tabCustom")
    pg.fill("#cDistrict", district)
    pg.click("#btnRun")
    pg.wait_for_selector("#jobError:not([hidden])", timeout=20000)
    msg = pg.locator("#jobError").inner_text()
    assert needle in msg and "Traceback" not in msg and "RuntimeError" not in msg and "{" not in msg
    assert pg.locator("#btnRetry").is_visible() and pg.locator("#content").is_hidden()
    pg.click("#btnRetry")
    assert pg.locator("#jobBox").is_hidden() and pg.locator("#cState").is_visible()
    assert pg.locator("#btnRun").is_enabled()                                            # user can try again


def test_rate_limit_message_in_ui(make_page, limited_server):
    pg = make_page()
    pg.goto(limited_server + "/")
    pg.click("#tabCustom")
    pg.click("#btnRun")
    pg.wait_for_selector("#content:not([hidden])", timeout=20000)
    pg.click("#tabCustom")
    pg.fill("#postEnd", "2018-08-24")                                                  # different request -> not cached
    pg.click("#btnRun")
    pg.wait_for_function("document.getElementById('sIssues').innerText.includes('limit')", timeout=10000)
    txt = pg.locator("#sIssues").inner_text()
    assert "Job limit reached" in txt and "Try again in about" in txt
    pg.fill("#postEnd", "2018-08-23")                                                  # editing the form clears the old message
    assert pg.locator("#sIssues").inner_text() == ""


def test_network_failure_is_reported(make_page, server):
    pg = make_page()
    pg.goto(server + "/")
    pg.wait_for_selector("#presetEvent option", state="attached")
    pg.context.route("**/api/events/**", lambda r: r.abort())
    pg.click("#btnLoadPreset")
    pg.wait_for_selector("#toast:not([hidden])")
    assert "Cannot reach the server" in pg.locator("#toast").inner_text()
    assert pg.locator("#content").is_hidden()


def test_server_500_is_reported(make_page, server):
    pg = make_page()
    pg.goto(server + "/")
    pg.wait_for_selector("#presetEvent option", state="attached")
    pg.context.route("**/api/events/**", lambda r: r.fulfill(status=500, body="<html>boom</html>", content_type="text/html"))
    pg.click("#btnLoadPreset")
    pg.wait_for_selector("#toast:not([hidden])")
    assert "HTTP 500" in pg.locator("#toast").inner_text() and "boom" not in pg.locator("#toast").inner_text()


# ------------------------------------------------------------------ security in the browser
def test_server_text_is_never_interpreted_as_html(make_page, server):
    pg = make_page()
    evil = '<img src=x onerror="window.__xss=1">'
    orig = json.loads(urllib.request.urlopen(server + "/api/events/kerala_2018_alappuzha").read())
    orig["title"] = "T " + evil
    orig["place"] = evil
    orig["roads"]["settlements"][0]["name"] = evil
    orig["warnings"] = [{"level": "warning", "code": "x", "message": evil}]
    pg.context.route("**/api/events/kerala_2018_alappuzha", lambda r: r.fulfill(json=orig))
    open_preset(pg, server)
    pg.click("#btnSummary")
    pg.wait_for_timeout(300)
    assert pg.evaluate("window.__xss") is None
    assert evil in pg.locator("#evTitle").inner_text() + pg.locator("#roadBox").inner_text()      # shown as plain text
    assert pg.locator("#roadBox img, #evTitle img, #warnings img, #summaryText img").count() == 0


def test_no_secrets_reach_the_browser(make_page, server):
    pg = make_page()
    bodies = []
    pg.on("response", lambda r: bodies.append((r.url, r.headers, r.body())) if r.url.startswith(server) and "/files/" not in r.url else None)
    open_preset(pg, server)
    pg.click("#tabCustom"); pg.click("#btnRun")
    pg.wait_for_selector("#content:not([hidden])", timeout=20000)
    blob = b"\n".join(b + json.dumps(dict(h)).encode() for _, h, b in bodies).lower()
    for needle in (b"private_key", b"begin private", b"ya29.", b"aiza", b"upstash", b"refresh_token", b"client_secret", b"ee_service_account", b'"cid"', b'"work"'):
        assert needle not in blob, needle
    assert pg.evaluate("Object.keys(window).filter(k => /secret|token|apikey|api_key|private_key|credentials?$/i.test(k))") == []
    assert pg.evaluate("document.cookie") == "" and pg.evaluate("Object.keys(localStorage).length") == 0


# ------------------------------------------------------------------ phone layout
def test_phone_layout(make_page, server):
    pg = make_page(viewport={"width": 390, "height": 844}, mobile=True)
    overflow = lambda: pg.evaluate("document.documentElement.scrollWidth - innerWidth")  # noqa: E731
    pg.goto(server + "/")
    pg.wait_for_selector("#presetEvent option", state="attached")
    assert overflow() <= 0
    pg.click("#tabCustom")
    pg.fill("#postStart", "2018-07-20")
    assert overflow() <= 0 and pg.locator("#cIssues .issue").count() == 1
    pg.fill("#postStart", "2018-08-15")
    pg.click("#tabPreset")
    pg.click("#btnLoadPreset")
    pg.wait_for_selector("#content:not([hidden])")
    pg.wait_for_function("document.getElementById('mapLoading').hidden", timeout=15000)
    pg.wait_for_timeout(700)
    assert overflow() <= 0
    pg.click("#btnSummary")
    assert overflow() <= 0
    # nothing is wider than the screen; text is not clipped inside cards
    too_wide = pg.evaluate("[...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1 && getComputedStyle(e).position !== 'fixed' && !e.closest('.leaflet-pane, .leaflet-container') && !e.closest('.scroll')).map(e => e.tagName + '.' + e.className).slice(0, 5)")
    assert too_wide == []
    # touch targets
    h = pg.locator("#swipe").bounding_box()
    assert h["width"] >= 44
    small = pg.evaluate("[...document.querySelectorAll('button, .btn, input:not([type=range]):not([type=radio]):not([type=checkbox]), select')].filter(e => e.offsetParent && e.getBoundingClientRect().height < 40).map(e => e.id || e.className)")
    assert small == []
    # the map is usable: taller than 300 px and starts within the first two screens
    mb = pg.locator(".mapbox").bounding_box()
    assert mb["height"] >= 300 and mb["width"] >= 360
    # sticky caveat stays visible
    pg.evaluate("window.scrollTo(0, 1500)")
    pg.wait_for_timeout(200)
    assert pg.locator("#caveatTop").bounding_box()["y"] < 5
    cav = pg.locator("#caveatTop").inner_text()                       # short version on phones, still says the essentials
    assert "not independent validation" in cav and "Copernicus EMS" in cav and pg.locator("#caveatTop").bounding_box()["height"] < 80
    # slider works at phone size
    pg.evaluate("window.scrollTo(0, document.querySelector('.mapbox').offsetTop - 80)")
    pg.wait_for_timeout(200)
    h = pg.locator("#swipe").bounding_box()
    mbox = pg.locator(".mapbox").bounding_box()
    pg.mouse.move(h["x"] + h["width"] / 2, h["y"] + 150)
    pg.mouse.down(); pg.mouse.move(mbox["x"] + mbox["width"] * 0.7, h["y"] + 150, steps=5); pg.mouse.up()
    assert pg.evaluate("floodApp.state.ratio") == pytest.approx(0.7, abs=0.03)
    assert pg.errors == []


def test_wheel_over_map_scrolls_page_until_map_is_clicked(make_page, server):
    pg = make_page()
    open_preset(pg, server)
    pg.evaluate("window.scrollTo(0, 0)")
    box = pg.locator("#map").bounding_box()
    pg.mouse.move(box["x"] + 300, box["y"] + 200)
    z0 = pg.evaluate("floodApp.state.map.getZoom()")
    pg.mouse.wheel(0, 400)
    pg.wait_for_timeout(400)
    assert pg.evaluate("scrollY") > 0                                        # the page scrolled
    assert pg.evaluate("floodApp.state.map.getZoom()") == z0                  # the map did not zoom
    box = pg.locator("#map").bounding_box()
    pg.mouse.click(box["x"] + 300, max(box["y"], 0) + 100)                    # click = "I want to use the map"
    assert pg.evaluate("floodApp.state.map.scrollWheelZoom.enabled()")


def test_slow_events_response_does_not_break_job_polling(make_page, server):
    """Regression: polling used state.config before it had loaded when /api/events was slow."""
    import time as _t
    pg = make_page()

    def slow(route):
        _t.sleep(3)
        route.continue_()
    pg.context.route("**/api/events", slow)
    pg.goto(server + "/")
    pg.click("#tabCustom")
    pg.click("#btnRun")                                     # starts a job while /api/events is still pending
    pg.wait_for_selector("#content:not([hidden])", timeout=25000)
    assert pg.locator("#evTitle").inner_text().startswith("Alappuzha")
    assert pg.errors == []
