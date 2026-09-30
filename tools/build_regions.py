#!/usr/bin/env python3
"""Write data/regions.json (state -> districts with area in km2) from Earth Engine's GAUL boundaries, and check
that every district named in data/presets.json exists with exactly that spelling.

  earthengine authenticate            # once
  python tools/build_regions.py --project YOUR_PROJECT
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))


def build(fp, country, log=print):
    ee = fp.ee
    rows = []
    for state in fp.list_regions(country, 1):
        fc = fp.gaul(country, 2, state)
        fc = fc.map(lambda f: f.set("area_km2", f.geometry().area(1).divide(1e6)))
        names = fc.reduceColumns(ee.Reducer.toList(2), ["ADM2_NAME", "area_km2"]).get("list").getInfo()
        for name, area in names:
            rows.append({"parent": state, "name": name, "area_km2": round(area, 2)})
        log(f"  {state}: {len(names)} districts")
    rows.sort(key=lambda r: (r["parent"], r["name"]))
    return {country: {"2": rows}}


def check_presets(regions, presets):
    have = {(r["parent"], r["name"]) for rs in regions.values() for r in rs["2"]}
    problems = []
    for p in presets:
        r = p["spec"]["region"]
        if (r["parent"], r["name"]) not in have:
            problems.append(f"{p['id']}: '{r['name']}' in '{r['parent']}' is not a GAUL district name")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--country", default="India")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "regions.json"))
    a = ap.parse_args(argv)
    if a.project and a.project.upper().startswith("YOUR"):
        raise SystemExit(f"'{a.project}' is a placeholder. Use your real Earth Engine Cloud project ID "
                         "(the one you used with flood_pipeline.py --project).")
    from pipeline import flood_pipeline as fp
    from common import presets
    fp.init_ee(a.project)
    regions = build(fp, a.country)
    with open(a.out, "w") as f:
        json.dump(regions, f, separators=(",", ":"))
    print(f"wrote {sum(len(v['2']) for v in regions.values())} districts to {a.out}")
    problems = check_presets(regions, presets.load_presets())
    for p in problems:
        print("  !", p)
    if problems:
        print("Fix data/presets.json with the exact spellings from regions.json, then rerun.")
        raise SystemExit(1)
    print("all preset districts exist")


if __name__ == "__main__":
    main()
