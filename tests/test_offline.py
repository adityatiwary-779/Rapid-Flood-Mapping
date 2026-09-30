"""Offline tests (no Earth Engine needed).

Run:  python -m pytest -q tests/     or     python tests/test_offline.py
These cover roads_exposure.py end to end (with OSM mocked) and the Otsu split logic
(NumPy mirror of flood_pipeline.otsu). They do NOT exercise any ee.* call.
"""
import os
import sys
import tempfile

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, box

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------- Otsu logic
def otsu_np(counts, means, fixed=True):
    """Mirror of flood_pipeline.otsu. fixed=False reproduces the original last-split bug."""
    counts, means = np.asarray(counts, float), np.asarray(means, float)
    total, tsum = counts.sum(), (counts * means).sum()
    mean = tsum / total
    size = len(means)
    last = size - 1 if fixed else size          # original: sequence(1, size)
    bss = []
    with np.errstate(divide="ignore", invalid="ignore"):
        for i in range(1, last + 1):
            ac = counts[:i].sum()
            am = (counts[:i] * means[:i]).sum() / ac
            bc = total - ac
            bm = (tsum - ac * am) / bc
            bss.append(ac * (am - mean) ** 2 + bc * (bm - mean) ** 2)
    bss = np.array(bss)
    cand = means[: len(bss)] if not fixed else means[: size - 1]
    return cand[np.argmax(bss)], bss


def test_otsu_bimodal():
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(-0.3, 1.0, 90000), rng.normal(-8, 1.5, 10000)])
    counts, edges = np.histogram(x, bins=255)
    means = (edges[:-1] + edges[1:]) / 2
    t, _ = otsu_np(counts, means)
    assert -6.0 < t < -1.0, t
    print(f"otsu bimodal threshold = {t:.2f} dB (modes at -0.3 / -8)")


def test_original_last_split_is_nan():
    counts, means = np.array([5, 10, 3, 7.0]), np.array([-8, -3, -1, 0.0])
    _, bss = otsu_np(counts, means, fixed=False)
    assert np.isnan(bss[-1]), "original code's final split is 0/0 -> NaN"
    _, bss_fixed = otsu_np(counts, means, fixed=True)
    assert not np.isnan(bss_fixed).any()
    print("original: last BSS =", bss[-1], "| fixed: no NaN")


# ------------------------------------------------------------ roads_exposure
def test_roads_exposure_mocked_osm():
    import osmnx as ox
    import roads_exposure as rx

    # flood square 0.01 deg (~1.1 km) near Alappuzha
    x0, y0 = 76.34, 9.49
    flood = gpd.GeoDataFrame(geometry=[box(x0, y0, x0 + 0.01, y0 + 0.01)], crs=4326)
    roads = gpd.GeoDataFrame({
        "highway": ["primary", "residential", "footway", "primary"],
        "name": ["A", "B", "C", "D"],
        "geometry": [
            LineString([(x0 - .01, y0 + .005), (x0 + .02, y0 + .005)]),   # crosses flood
            LineString([(x0 + .002, y0 + .002), (x0 + .004, y0 + .002)]),  # inside flood
            LineString([(x0 + .002, y0 + .004), (x0 + .004, y0 + .004)]),  # inside, footway
            LineString([(x0 - .03, y0 + .05), (x0 - .02, y0 + .05)]),      # far away
        ]}, crs=4326)
    places = gpd.GeoDataFrame({
        "place": ["village", "village", "town"], "name": ["in", "near", "out"],
        "geometry": [Point(x0 + .005, y0 + .005), Point(x0 + .0115, y0 + .005),
                     Point(x0 - .05, y0 - .05)]}, crs=4326)

    def fake(place_or_poly, tags, *a, **k):
        return roads if "highway" in tags else places
    ox.features_from_place = fake
    ox.features_from_polygon = fake

    with tempfile.TemporaryDirectory() as d:
        fp = os.path.join(d, "flood.geojson")
        flood.to_file(fp, driver="GeoJSON")
        for extra, exp_sett in (([], 1), (["--settlement-buffer-m", "300"], 2)):
            sys.argv = ["x", "--flood", fp, "--place", "Test", "--out-dir", d] + extra
            rx.main()
            s = pd.read_csv(os.path.join(d, "exposure_summary.csv")).iloc[0]
            assert s.settlements_affected == exp_sett, (extra, s.to_dict())
        by = pd.read_csv(os.path.join(d, "roads_flooded_by_type.csv")).set_index("highway")
        # primary A crosses the 0.01 deg square: ~1.09 km at this latitude
        assert 1.0 < by.loc["primary", "flooded_km"] < 1.2, by
        assert s.road_km_flooded > 1.2  # + residential + footway
        # motorable-only drops the footway
        sys.argv = ["x", "--flood", fp, "--place", "Test", "--out-dir", d, "--motorable-only"]
        rx.main()
        by2 = pd.read_csv(os.path.join(d, "roads_flooded_by_type.csv"))
        assert "footway" not in set(by2.highway)
        # place=None falls back to bbox polygon
        sys.argv = ["x", "--flood", fp, "--out-dir", d]
        rx.main()
        # empty flood file -> clean exit message, not a traceback
        empty = os.path.join(d, "empty.geojson")
        open(empty, "w").write('{"type":"FeatureCollection","features":[]}')
        sys.argv = ["x", "--flood", empty, "--out-dir", d]
        try:
            rx.main()
            raise AssertionError("expected SystemExit")
        except SystemExit as e:
            assert "no flood polygons" in str(e)
    print("roads_exposure mocked-OSM tests passed")


if __name__ == "__main__":
    test_otsu_bimodal()
    test_original_last_split_is_nan()
    test_roads_exposure_mocked_osm()
