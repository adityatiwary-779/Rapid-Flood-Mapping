"""Pure parts of tools/build_risk_package.py (Earth Engine and OSM calls themselves cannot run in the sandbox)."""
import json
import os
import sys

import networkx as nx
import pytest

from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_risk_package as b  # noqa: E402


def _sample(v=0.5, rows=3, cols=3):
    s = {k: [[v] * cols for _ in range(rows)] for k in b.LAYERS}
    s["inside"] = [[1] * cols for _ in range(rows)]
    return s


def test_pack_layers_rounds_clamps_and_flattens():
    s = _sample(0.123456)
    s["flood"][0][0] = 1.7
    s["slope"][1][1] = -0.2
    out = b.pack_layers(s, 3, 3)
    assert out["flood"][0] == 1.0 and out["slope"][4] == 0.0 and out["rainfall"][0] == 0.12
    assert len(out["inside"]) == 9 and set(out["inside"]) <= {0, 1}


@pytest.mark.parametrize("mut,msg", [(lambda s: s.pop("slope"), "missing"), (lambda s: s["flood"].pop(), "shape"),
                                     (lambda s: s["flood"][0].__setitem__(0, float("nan")), "empty")])
def test_pack_layers_fails_loudly(mut, msg):
    s = _sample()
    mut(s)
    with pytest.raises(ValueError, match=msg):
        b.pack_layers(s, 3, 3)


def test_graph_from_nx_dedupes_parallel_and_directed_edges():
    G = nx.MultiDiGraph()
    G.add_node(1, x=76.1, y=9.1); G.add_node(2, x=76.2, y=9.2); G.add_node(3, x=76.3, y=9.3)
    G.add_edge(1, 2, length=100.0); G.add_edge(2, 1, length=90.0); G.add_edge(1, 2, length=120.0); G.add_edge(2, 3, length=50.0)
    G.add_edge(3, 3, length=5.0)
    g = b.graph_from_nx(G)
    assert g["nodes"][0] == [9.1, 76.1] and g["edges"] == [[0, 1, 90.0], [1, 2, 50.0]]


def test_pois_named_first_deduped_capped():
    recs = [dict(name=None, lat=9.1, lon=76.1), dict(name="B", lat=9.2, lon=76.2), dict(name="A", lat=9.3, lon=76.3),
            dict(name="Dup", lat=9.2, lon=76.2)]
    out = b.pois_from_records(recs, "hospital", 3)
    assert [p["name"] for p in out] == ["A", "B", "Unnamed hospital"] and all(p["type"] == "hospital" for p in out)


def test_write_package_updates_index_and_real_sorts_first(tmp_path):
    base = dict(state="Kerala", calamities=["flood"], events="x")
    b.write_package(tmp_path, dict(base, id="a", name="Aaa", provenance=dict(kind="synthetic")))
    b.write_package(tmp_path, dict(base, id="z", name="Zzz", provenance=dict(kind="real")))
    b.write_package(tmp_path, dict(base, id="a", name="Aaa", provenance=dict(kind="real")))
    idx = json.load(open(tmp_path / "index.json"))["areas"]
    assert [a["id"] for a in idx] == ["a", "z"] and all(a["kind"] == "real" for a in idx)


def test_refuses_placeholders(monkeypatch):
    with pytest.raises(SystemExit, match="placeholder"):
        b.main(["--district", "Pathanamthitta", "--project", "YOUR_PROJECT", "--stage", "ee"])


def test_one_stray_edge_row_is_trimmed_but_bigger_errors_are_not():
    s = _sample(0.5, 4, 3)          # 4 rows when 3 expected
    assert len(b.pack_layers(s, 3, 3)["flood"]) == 9
    with pytest.raises(ValueError, match="shape"):
        b.pack_layers(_sample(0.5, 6, 3), 3, 3)


def test_with_mirrors_falls_through_then_succeeds_and_reports_all_errors():
    class Cfg: overpass_url = ""
    calls = []

    def fn():
        calls.append(Cfg.overpass_url)
        if len(calls) < 4:
            raise ConnectionError("closed")
        return "ok"
    assert b.with_mirrors(Cfg, fn, ["a", "b", "c"], sleep=lambda s: None, log=lambda m: None) == "ok"
    assert calls == ["a", "a", "b", "b"]

    def bad():
        raise TimeoutError("slow")
    with pytest.raises(SystemExit) as ei:
        b.with_mirrors(Cfg, bad, ["a", "b"], sleep=lambda s: None, log=lambda m: None)
    assert "a:" in str(ei.value) and "b:" in str(ei.value)


def test_graph_from_lines_merges_stretches_and_keeps_largest_piece():
    # a T junction at (76.2, 9.2); the stem has an extra middle vertex that must disappear
    lines = [[(76.1, 9.2), (76.2, 9.2)], [(76.2, 9.2), (76.3, 9.2)], [(76.2, 9.2), (76.2, 9.15), (76.2, 9.1)],
             [(77.0, 10.0), (77.01, 10.0)]]   # a separate island, dropped
    g = b.graph_from_lines(lines)
    assert len(g["nodes"]) == 4 and len(g["edges"]) == 3
    stem = max(g["edges"], key=lambda e: e[2])
    assert 10000 < stem[2] < 12000   # ~0.1 degree of latitude


def test_records_from_gdf_filters_by_fclass_and_uses_polygon_points():
    import geopandas as gpd
    from shapely.geometry import Point, box
    gdf = gpd.GeoDataFrame({"fclass": ["hospital", "school", "bank"], "name": ["H", None, "B"],
                            "geometry": [Point(76.1, 9.1), box(76.2, 9.2, 76.3, 9.3), Point(76.4, 9.4)]}, crs=4326)
    recs = b.records_from_gdf(gdf, {"hospital", "school"})
    assert [r["name"] for r in recs] == ["H", None] and 76.2 < recs[1]["lon"] < 76.3


def test_offline_stage_end_to_end_with_small_files(tmp_path):
    import geopandas as gpd
    from shapely.geometry import LineString, Point
    base = dict(id="d", name="D", state="Kerala", bounds=[9.0, 76.0, 9.5, 76.5], rows=3, cols=3, calamities=["flood"], events="x",
                layers={}, graph=dict(nodes=[], edges=[]), pois=[], provenance=dict(kind="real", sources="s"))
    b.write_package(tmp_path, base)
    gpd.GeoDataFrame({"fclass": ["primary", "footway", "residential"],
                      "geometry": [LineString([(76.1, 9.1), (76.2, 9.1)]), LineString([(76.1, 9.1), (76.1, 9.2)]),
                                   LineString([(76.2, 9.1), (76.3, 9.1)])]}, crs=4326).to_file(tmp_path / "roads.shp")
    gpd.GeoDataFrame({"fclass": ["hospital", "school"], "name": ["Gen Hosp", "Sch"],
                      "geometry": [Point(76.2, 9.1), Point(76.3, 9.1)]}, crs=4326).to_file(tmp_path / "pois.shp")
    gpd.GeoDataFrame({"fclass": ["village"], "name": ["Vill"], "geometry": [Point(76.15, 9.1)]}, crs=4326).to_file(tmp_path / "places.shp")
    import argparse
    a = argparse.Namespace(out=str(tmp_path), id="d", district="D", pbf=None, roads_file=str(tmp_path / "roads.shp"),
                           pois_file=[str(tmp_path / "pois.shp")], places_file=str(tmp_path / "places.shp"))
    b.offline_stage(a)
    pkg = json.load(open(tmp_path / "d.json"))
    assert len(pkg["graph"]["edges"]) == 2 and {p["type"] for p in pkg["pois"]} == {"hospital", "shelter"}
    assert pkg["settlements"][0]["name"] == "Vill"


OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' generator='test'>
 <node id='1' lat='9.10' lon='76.10'/><node id='2' lat='9.10' lon='76.20'/><node id='3' lat='9.10' lon='76.30'/>
 <node id='4' lat='9.20' lon='76.20'/>
 <node id='5' lat='9.101' lon='76.201'><tag k='amenity' v='hospital'/><tag k='name' v='Gen Hosp'/></node>
 <node id='6' lat='9.102' lon='76.202'><tag k='amenity' v='school'/><tag k='name' v='Sch'/></node>
 <node id='7' lat='9.103' lon='76.15'><tag k='place' v='village'/><tag k='name' v='Vill'/></node>
 <way id='10'><nd ref='1'/><nd ref='2'/><tag k='highway' v='primary'/></way>
 <way id='11'><nd ref='2'/><nd ref='3'/><tag k='highway' v='residential'/></way>
 <way id='12'><nd ref='2'/><nd ref='4'/><tag k='highway' v='footway'/></way>
</osm>"""


def test_offline_stage_from_a_raw_osm_extract(tmp_path):
    import argparse
    base = dict(id="d", name="D", state="Kerala", bounds=[9.0, 76.0, 9.5, 76.5], rows=3, cols=3, calamities=["flood"], events="x",
                layers={}, graph=dict(nodes=[], edges=[]), pois=[], provenance=dict(kind="real", sources="s"))
    b.write_package(tmp_path, base)
    (tmp_path / "x.osm").write_text(OSM_XML)
    a = argparse.Namespace(out=str(tmp_path), id="d", district="D", pbf=str(tmp_path / "x.osm"), roads_file=None, pois_file=None, places_file=None)
    b.offline_stage(a)
    pkg = json.load(open(tmp_path / "d.json"))
    assert len(pkg["graph"]["edges"]) == 2                       # footway excluded
    assert {(p["type"], p["name"]) for p in pkg["pois"]} == {("hospital", "Gen Hosp"), ("shelter", "Sch")}
    assert [x["name"] for x in pkg["settlements"]] == ["Vill"]
