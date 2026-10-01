"""Risk and Routes page: loads each district package, sliders change the result, routes are drawn and compared.
Uses the synthetic packages in public/data/risk; tile requests are blocked (sandbox has no internet)."""
import json
import os
import threading

import pytest
from playwright.sync_api import sync_playwright

from conftest import ROOT
from static_server import serve
from test_frontend_e2e import CHROME, PNG, _free_port

PUBLIC = os.path.join(ROOT, "public")
PKG = os.path.join(PUBLIC, "data/risk")


@pytest.fixture(scope="module")
def url():
    port = _free_port()
    srv = serve(PUBLIC, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/risk/index.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}), args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture()
def page(browser, url):
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/tile.openstreetmap.org/**", lambda r: r.fulfill(status=200, body=PNG, content_type="image/png"))
    pg.goto(url)
    pg.wait_for_function("document.querySelectorAll('#area option').length >= 3 && document.querySelectorAll('#kpis dd').length > 0")
    yield pg
    assert errors == []
    pg.close()


def test_packages_have_the_contract():
    idx = json.load(open(os.path.join(PKG, "index.json")))
    assert len(idx["areas"]) >= 3 and "pathanamthitta" in [a["id"] for a in idx["areas"]]
    for a in idx["areas"]:
        p = json.load(open(os.path.join(PKG, a["id"] + ".json")))
        n = p["rows"] * p["cols"]
        for k in ("flood", "lowland", "slope", "rainfall", "landcover", "population"):
            assert len(p["layers"][k]) == n and 0 <= min(p["layers"][k]) and max(p["layers"][k]) <= 1
        nn = len(p["graph"]["nodes"])
        assert all(0 <= a_ < nn and 0 <= b < nn and d > 0 for a_, b, d in p["graph"]["edges"])
        assert {q["type"] for q in p["pois"]} == {"hospital", "shelter"}
        assert p["provenance"]["kind"] in ("synthetic", "real")


def test_dropdown_lists_districts_and_caveat_is_visible(page):
    opts = page.locator("#area option").all_inner_texts()
    assert any("Pathanamthitta" in o for o in opts) and len(opts) >= 3
    assert "judgement-based" in page.locator(".banner.caveat").inner_text()
    assert page.locator("#provBanner").is_visible()  # synthetic data must announce itself


def test_calamity_options_follow_the_district(page):
    page.select_option("#area", "pathanamthitta")
    page.wait_for_function("document.querySelectorAll('#calamity option').length === 3")
    page.select_option("#area", "alappuzha")
    page.wait_for_function("document.querySelectorAll('#calamity option').length === 1")
    assert page.locator("#calamity option").first.inner_text() == "Flood"


def test_slider_changes_the_summary_and_weights_rescale(page):
    before = page.locator("#kpis dd").first.inner_text()
    page.select_option("#calamity", "combined") if page.locator("#calamity option").count() > 1 else None
    page.fill("#topPct", "35")
    page.dispatch_event("#topPct", "input")
    page.wait_for_function("document.querySelector('#kpis dd').textContent !== %s" % json.dumps(before))
    page.fill("#w_flood", "100")
    page.dispatch_event("#w_flood", "input")
    shown = [page.locator("#weights output").nth(i).inner_text() for i in range(5)]
    assert sum(int(s.rstrip("%")) for s in shown) in range(98, 103)


def test_example_route_draws_both_routes_and_table(page):
    page.click("#btnExample")
    page.wait_for_selector("#routeTable:not([hidden])")
    assert page.locator("#routeTable tbody tr").count() == 5
    assert page.locator("path.leaflet-interactive[stroke='#22c55e']").count() == 1
    assert page.locator("path.leaflet-interactive[stroke='#f5a524']").count() == 1
    page.click("#btnClear")
    assert page.locator("#routeTable").is_hidden()


def test_zero_avoidance_makes_routes_identical(page):
    page.fill("#alpha", "0")
    page.dispatch_event("#alpha", "input")
    page.click("#btnExample")
    page.wait_for_selector("#routeTable:not([hidden])")
    assert "same" in page.locator("#routeMsg").inner_text()


def test_clicking_the_map_sets_a_start(page):
    box = page.locator("#map").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_selector("#routeTable:not([hidden])")
