"""roads_exposure: what happens when OpenStreetMap cannot be reached, and the offline-file fallback."""
import json
import os
import sys

import geopandas as gpd
import pytest
import requests
from shapely.geometry import LineString, Point, box

from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import osmnx as ox  # noqa: E402
import roads_exposure as rx  # noqa: E402

X0, Y0 = 76.34, 9.49


@pytest.fixture
def flood(tmp_path):
    p = tmp_path / "flood.geojson"
    gpd.GeoDataFrame(geometry=[box(X0, Y0, X0 + 0.01, Y0 + 0.01)], crs=4326).to_file(p, driver="GeoJSON")
    return str(p)


def roads_gdf():
    return gpd.GeoDataFrame({"highway": ["primary", "footway"], "name": ["A", "B"], "geometry": [
        LineString([(X0 - .01, Y0 + .005), (X0 + .02, Y0 + .005)]),
        LineString([(X0 + .002, Y0 + .004), (X0 + .004, Y0 + .004)])]}, crs=4326)


def places_gdf():
    return gpd.GeoDataFrame({"place": ["village", "island"], "name": ["in", "skip me"],
                             "geometry": [Point(X0 + .005, Y0 + .005), Point(X0 + .006, Y0 + .006)]}, crs=4326)


def test_unreachable_osm_raises_and_writes_nothing(flood, tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("actively refused")
    monkeypatch.setattr(ox, "features_from_place", boom)
    monkeypatch.setattr(ox, "features_from_polygon", boom)
    out = tmp_path / "res"
    with pytest.raises(rx.OSMUnavailable) as e:
        rx.compute_exposure(flood, "Somewhere", str(out), overpass_urls=["https://a/api", "https://b/api"])
    assert "https://a/api" in str(e.value) and "https://b/api" in str(e.value)
    assert not out.exists()                                          # no half-written / zero results
    monkeypatch.setattr(sys, "argv", ["x", "--flood", flood, "--place", "Somewhere", "--out-dir", str(out)])
    with pytest.raises(SystemExit) as s:
        rx.main()
    msg = str(s.value)
    assert "NO road results were produced" in msg and "--roads-file" in msg and "phone hotspot" in msg
    assert not out.exists()


def test_falls_back_to_next_mirror(flood, tmp_path, monkeypatch):
    tried = []

    def fake(_arg, tags, *a, **k):
        url = getattr(ox.settings, "overpass_url", None) or getattr(ox.settings, "overpass_endpoint", None)
        tried.append(url)
        if url == "https://dead.example/api":
            raise requests.exceptions.ConnectionError("refused")
        return roads_gdf() if "highway" in tags else places_gdf()
    monkeypatch.setattr(ox, "features_from_place", fake)
    monkeypatch.setattr(ox, "features_from_polygon", fake)
    res = rx.compute_exposure(flood, "P", str(tmp_path / "r"), motorable_only=True, settlement_buffer_m=0,
                              overpass_urls=["https://dead.example/api", "https://alive.example/api"])
    assert "https://dead.example/api" in tried and tried[-1] == "https://alive.example/api"
    assert res["road_km_flooded"] > 1.0 and res["source"] == "OpenStreetMap"
    assert res["settlements_affected"] == 2                          # both places are inside the flood square (no type filter on OSM path)


def test_place_geocode_failure_uses_bbox_on_same_server(flood, tmp_path, monkeypatch, capsys):
    calls = {"place": 0, "poly": 0}

    def place(*a, **k):
        calls["place"] += 1
        raise ValueError("geocoder returned a point, not a polygon")

    def poly(_p, tags, *a, **k):
        calls["poly"] += 1
        return roads_gdf() if "highway" in tags else places_gdf()
    monkeypatch.setattr(ox, "features_from_place", place)
    monkeypatch.setattr(ox, "features_from_polygon", poly)
    res = rx.compute_exposure(flood, "Nowhere", str(tmp_path / "r"), overpass_urls=["https://one.example/api"])
    assert calls["place"] == 2 and calls["poly"] == 2 and res["road_km_flooded"] > 1.0
    assert "using the flood bounding box" in capsys.readouterr().out


def test_nothing_mapped_is_a_legitimate_zero_not_an_error(flood, tmp_path, monkeypatch):
    try:
        from osmnx._errors import InsufficientResponseError as Err
    except Exception:
        pytest.skip("osmnx has no InsufficientResponseError")

    def none(*a, **k):
        raise Err("no data")
    monkeypatch.setattr(ox, "features_from_place", none)
    monkeypatch.setattr(ox, "features_from_polygon", none)
    res = rx.compute_exposure(flood, "P", str(tmp_path / "r"), overpass_urls=["https://one.example/api"])
    assert res["road_km_total"] == 0 and res["settlements_total"] == 0
    assert (tmp_path / "r" / "exposure_summary.csv").exists()


@pytest.mark.parametrize("road_col,place_col", [("highway", "place"), ("fclass", "fclass")])   # OSM/HOT names, Geofabrik names
def test_offline_files_work_without_any_network(flood, tmp_path, monkeypatch, road_col, place_col):
    def never(*a, **k):
        raise AssertionError("OSM must not be contacted when local files are given")
    monkeypatch.setattr(ox, "features_from_place", never)
    monkeypatch.setattr(ox, "features_from_polygon", never)
    r, p = roads_gdf().rename(columns={"highway": road_col}), places_gdf().rename(columns={"place": place_col})
    if road_col == "fclass":
        p["fclass"] = ["village", "island"]
    rp, pp = tmp_path / "roads.geojson", tmp_path / "places.geojson"
    r.to_file(rp, driver="GeoJSON")
    p.to_file(pp, driver="GeoJSON")
    res = rx.compute_exposure(flood, None, str(tmp_path / "r"), motorable_only=True, roads_file=str(rp), places_file=str(pp))
    assert res["source"] == "local file" and res["road_km_flooded"] > 1.0
    assert res["settlements_total"] == 1 and res["settlements"][0]["name"] == "in"      # 'island' is not a settlement type
    assert [b["highway"] for b in res["by_type"]] == ["primary"]                        # footway dropped by motorable-only
    assert json.load(open(tmp_path / "r" / "flooded_roads.geojson"))["features"]


def test_offline_file_without_expected_column_is_explained(flood, tmp_path):
    bad = tmp_path / "bad.geojson"
    gpd.GeoDataFrame({"x": [1], "geometry": [LineString([(X0, Y0), (X0 + .001, Y0)])]}, crs=4326).to_file(bad, driver="GeoJSON")
    with pytest.raises(ValueError) as e:
        rx.compute_exposure(flood, None, str(tmp_path / "r"), roads_file=str(bad), places_file=str(bad))
    assert "highway" in str(e.value) and "Columns" in str(e.value)
