#!/usr/bin/env python3
"""Add REAL road/settlement results (from pipeline/roads_exposure.py) to the static demo.

  python pipeline/roads_exposure.py --flood data/fixtures/alappuzha_2018/flood_rf.geojson \
      --place "Alappuzha district, Kerala, India" --out-dir results/alappuzha --motorable-only --settlement-buffer-m 250
  python tools/add_roads_to_demo.py --results results/alappuzha

Copies the flooded-roads / settlements layers (coordinates rounded to ~1 m to keep them small), the two CSVs,
and fills the manifest so the site shows flooded road km, roads by type, the settlements list and the map layers.
"""
import argparse
import csv
import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOTE = ("Static demo: the radar images, flood layers and figures were rendered by Earth Engine from the pipeline for "
        "this event. Roads and settlements come from OpenStreetMap (motorable roads only; a settlement counts as "
        "affected if its centre is within {buffer} m of flood water). GeoTIFF downloads are not included in the static demo.")


def _round(coords, nd=5):
    if isinstance(coords, (int, float)):
        return round(coords, nd)
    return [_round(c, nd) for c in coords]


def slim_geojson(src, dst, keep):
    with open(src, encoding="utf-8") as f:
        gj = json.load(f)
    feats = []
    for ft in gj.get("features", []):
        if not ft.get("geometry"):
            continue
        props = {k: ft["properties"].get(k) for k in keep if ft.get("properties", {}).get(k) is not None}
        if "flooded_km" in props:
            props["flooded_km"] = round(float(props["flooded_km"]), 3)
        g = ft["geometry"]
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": g["type"], "coordinates": _round(g["coordinates"])}})
    with open(dst, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, separators=(",", ":"), ensure_ascii=False)
    return len(feats)


def apply_roads(public, preset_id, results, buffer_m=250, motorable=True, source="OpenStreetMap"):
    base = os.path.join(public, "files", "presets", preset_id)
    with open(os.path.join(results, "exposure_summary.csv"), newline="") as f:
        s = next(csv.DictReader(f))
    with open(os.path.join(results, "roads_flooded_by_type.csv"), newline="") as f:
        by_type = [{"highway": r["highway"], "flooded_km": round(float(r["flooded_km"]), 2)} for r in csv.DictReader(f)]
    with open(os.path.join(results, "affected_settlements.csv"), newline="", encoding="utf-8") as f:
        settlements = [{"name": (r.get("name") or None), "place": (r.get("place") or None)} for r in csv.DictReader(f)]
    n_roads = slim_geojson(os.path.join(results, "flooded_roads.geojson"), os.path.join(base, "flooded_roads.geojson"),
                           ("highway", "name", "flooded_km"))
    n_places = slim_geojson(os.path.join(results, "affected_settlements.geojson"),
                            os.path.join(base, "affected_settlements.geojson"), ("place", "name"))
    for name in ("roads_flooded_by_type.csv", "affected_settlements.csv"):
        shutil.copyfile(os.path.join(results, name), os.path.join(base, name))

    roads = {"place": s["place"], "road_km_total": float(s["road_km_total"]), "road_km_flooded": float(s["road_km_flooded"]),
             "settlements_total": int(float(s["settlements_total"])), "settlements_affected": int(float(s["settlements_affected"])),
             "by_type": by_type, "settlements": settlements, "motorable_only": bool(motorable),
             "settlement_buffer_m": int(buffer_m), "source": source}
    mpath = os.path.join(public, "demo", "api", "events", f"{preset_id}.json")
    with open(mpath) as f:
        m = json.load(f)
    m["roads"] = roads
    for n in ("flooded_roads.geojson", "affected_settlements.geojson", "roads_flooded_by_type.csv", "affected_settlements.csv"):
        m["files"][n] = f"/files/presets/{preset_id}/{n}"
    m["warnings"] = [w for w in (m.get("warnings") or []) if w.get("code") != "static_demo"] + [
        {"level": "info", "code": "static_demo", "message": NOTE.format(buffer=buffer_m)}]
    with open(mpath, "w") as f:
        json.dump(m, f, separators=(",", ":"))
    return roads, n_roads, n_places


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, help="folder written by roads_exposure.py")
    ap.add_argument("--preset", default="kerala_2018_alappuzha")
    ap.add_argument("--public", default=os.path.join(ROOT, "public"))
    ap.add_argument("--buffer-m", type=int, default=250, help="the --settlement-buffer-m you used")
    ap.add_argument("--all-road-types", action="store_true", help="you ran roads_exposure WITHOUT --motorable-only")
    ap.add_argument("--source", default="OpenStreetMap", help='use "local file" if you ran with --roads-file')
    a = ap.parse_args(argv)
    need = ["exposure_summary.csv", "roads_flooded_by_type.csv", "affected_settlements.csv", "flooded_roads.geojson",
            "affected_settlements.geojson"]
    missing = [n for n in need if not os.path.exists(os.path.join(a.results, n))]
    if missing:
        raise SystemExit(f"{a.results} is missing: {', '.join(missing)}. Run pipeline/roads_exposure.py first.")
    roads, nr, npl = apply_roads(a.public, a.preset, a.results, a.buffer_m, not a.all_road_types, a.source)
    size = sum(os.path.getsize(os.path.join(a.public, "files", "presets", a.preset, n)) for n in ("flooded_roads.geojson", "affected_settlements.geojson"))
    print(f"roads: {roads['road_km_flooded']} km flooded of {roads['road_km_total']} km; settlements {roads['settlements_affected']} of {roads['settlements_total']}")
    print(f"wrote {nr} road segments and {npl} settlements ({size / 1e6:.1f} MB) into the demo")
    print("Next:  git add public && git commit -m \"Real roads and settlements in the demo\" && git push   then   cd public && vercel --prod")


if __name__ == "__main__":
    main()
