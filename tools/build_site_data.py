#!/usr/bin/env python3
"""Turn pipeline outputs into the small files the website reads (website/data/<event>/).

  python tools/build_site_data.py --event alappuzha_2018 --variant rf \
      --flood-geojson kerala_2018_alappuzha_alappuzha_flood_vec.geojson \
      --stats-csv kerala_2018_alappuzha_alappuzha_stats.csv
  python tools/build_site_data.py --event alappuzha_2018 --roads-dir results/alappuzha

--variant is "rf" (default pipeline run) or "rule" (run with --no-rf).
Any of the three inputs can be given on its own.
"""
import argparse
import json
import os

import pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "website", "data")


def polygons(src, dst, tol_deg=0.00005):
    import geopandas as gpd
    import shapely
    g = gpd.read_file(src).to_crs(4326)
    g = g[g.geometry.notnull() & ~g.geometry.is_empty]
    geoms = g.geometry.simplify(tol_deg, preserve_topology=True)        # ~5 m, finer than a 30 m pixel
    geoms = geoms.apply(lambda x: shapely.set_precision(x, 1e-5))       # ~1 m coordinates
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": json.loads(shapely.to_geojson(x))}
        for x in geoms if not x.is_empty]}
    with open(dst, "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    print(f"{dst}: {len(fc['features'])} polygons, {os.path.getsize(dst)/1e6:.2f} MB")


def stats(src, dst):
    row = pd.read_csv(src).iloc[0].to_dict()
    row = {k: (v.item() if hasattr(v, "item") else v) for k, v in row.items()
           if not k.startswith(".") and k != "system:index"}
    with open(dst, "w") as f:
        json.dump(row, f, indent=2)
    print("wrote", dst)


def roads(folder, dst):
    s = pd.read_csv(os.path.join(folder, "exposure_summary.csv")).iloc[0].to_dict()
    by = pd.read_csv(os.path.join(folder, "roads_flooded_by_type.csv"))
    s = {k: (v.item() if hasattr(v, "item") else v) for k, v in s.items()}
    s["by_type"] = by.round(2).to_dict(orient="records")
    with open(dst, "w") as f:
        json.dump(s, f, indent=2)
    print("wrote", dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--variant", choices=["rf", "rule"], default="rf")
    ap.add_argument("--flood-geojson")
    ap.add_argument("--stats-csv")
    ap.add_argument("--roads-dir")
    a = ap.parse_args()
    out = os.path.join(ROOT, a.event)
    os.makedirs(out, exist_ok=True)
    if a.flood_geojson:
        polygons(a.flood_geojson, os.path.join(out, f"flood_{a.variant}.geojson"))
    if a.stats_csv:
        stats(a.stats_csv, os.path.join(out, f"stats_{a.variant}.json"))
    if a.roads_dir:
        roads(a.roads_dir, os.path.join(out, f"roads_{a.variant}.json"))


if __name__ == "__main__":
    main()
