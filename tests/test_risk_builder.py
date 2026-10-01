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
