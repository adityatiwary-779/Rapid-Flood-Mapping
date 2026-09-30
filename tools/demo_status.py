#!/usr/bin/env python3
"""Show exactly what the static demo in public/ currently contains (and what is inconsistent). No guessing.

  python tools/demo_status.py
"""
import argparse
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def status(public, preset_id="kerala_2018_alappuzha"):
    """Returns (lines, problems)."""
    mpath = os.path.join(public, "demo", "api", "events", f"{preset_id}.json")
    if not os.path.exists(mpath):
        return [f"no manifest at {mpath}"], ["demo not built: run python tools/build_static_demo.py"]
    with open(mpath) as f:
        m = json.load(f)
    base = os.path.join(public, "files", "presets", preset_id)
    disk = set(os.listdir(base)) if os.path.isdir(base) else set()
    prev_disk = set(os.listdir(os.path.join(base, "previews"))) if os.path.isdir(os.path.join(base, "previews")) else set()
    st, r = m["stats"], m.get("roads")
    lines = [f"folder        : {os.path.abspath(public)}",
             f"imagery       : {'SYNTHETIC (mock)' if m.get('mock') else 'real (Earth Engine)'}",
             f"previews      : {', '.join(sorted(m['previews']))}",
             f"flood area    : {st['flood_km2']:.2f} km2   (threshold-only {st.get('rule_based_km2') or float('nan'):.2f}, "
             f"no masks {st.get('flood_unmasked_km2') or float('nan'):.2f})",
             "roads         : " + (f"{r['road_km_flooded']} km flooded of {r['road_km_total']} km, settlements "
                                  f"{r['settlements_affected']} of {r['settlements_total']} ({r.get('source')})" if r else "NONE in the manifest")]
    problems = []
    road_files = {"flooded_roads.geojson", "affected_settlements.geojson", "roads_flooded_by_type.csv", "affected_settlements.csv"}
    if r and not road_files <= set(m["files"]):
        problems.append("manifest has roads but is missing road file entries: rerun tools/add_roads_to_demo.py")
    if not r and road_files & disk:
        problems.append("road files are on disk but the manifest has no roads (an older script run wiped them): "
                        "run  python tools/add_roads_to_demo.py --results results/alappuzha")
    for name in m["files"].values():
        if not os.path.exists(os.path.join(public, name.lstrip("/"))):
            problems.append(f"manifest lists a file that is not on disk: {name}")
    for name in m["previews"].values():
        if not os.path.exists(os.path.join(public, name.lstrip("/"))):
            problems.append(f"manifest lists an image that is not on disk: {name}")
    if "flood_rule.png" not in prev_disk:
        problems.append("no flood_rule.png: 'Threshold, masked' stays disabled (rerun tools/make_real_previews.py)")
    return lines, problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", default=os.path.join(ROOT, "public"))
    ap.add_argument("--preset", default="kerala_2018_alappuzha")
    a = ap.parse_args(argv)
    lines, problems = status(a.public, a.preset)
    print("\n".join(lines))
    print("\nproblems      : " + ("none, everything is consistent" if not problems else ""))
    for p in problems:
        print("   - " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
