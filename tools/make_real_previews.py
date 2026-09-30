#!/usr/bin/env python3
"""Replace the synthetic radar/flood images in the static demo with REAL ones rendered by Earth Engine from the
pipeline, and refresh the demo's statistics from the same run. Run once, on your machine, with your EE login:

  earthengine authenticate                      # if you have not already
  python tools/make_real_previews.py --project graceful-karma-457005-v6

It rebuilds the Alappuzha 2018 graph (1-3 minutes), downloads 6 PNGs (pre/post radar, final flood, threshold-masked, threshold-unmasked, severity)
and updates public/demo + public/files. The demo then no longer claims "synthetic radar". Roads and GeoTIFFs stay out
of the static demo (they need the worker). Afterwards:  python -m pytest -q tests/test_static_demo.py
"""
import argparse
import csv
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

PREVIEWS = ("pre_vv", "post_vv", "flood", "flood_rule", "flood_unmasked", "severity")
from tools import demo_notes  # noqa: E402


def apply_real_assets(public, preset_id, stats, config, bounds, pngs):
    """Write real images/stats into the static demo files. Pure file work (unit-tested without Earth Engine)."""
    from flood_pipeline import STATS_COLUMNS
    base = os.path.join(public, "files", "presets", preset_id)
    api = os.path.join(public, "demo", "api")
    for name in PREVIEWS:
        assert pngs[name][:8] == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
        with open(os.path.join(base, "previews", f"{name}.png"), "wb") as f:
            f.write(pngs[name])

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=STATS_COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerow({k: stats.get(k) for k in STATS_COLUMNS})
    with open(os.path.join(base, "stats.csv"), "w", newline="") as f:
        f.write(out.getvalue())
    with open(os.path.join(base, "config.json"), "w") as f:
        json.dump(config, f, indent=2)

    mpath = os.path.join(api, "events", f"{preset_id}.json")
    with open(mpath) as f:
        m = json.load(f)
    s = dict(stats)
    s["other_km2"] = max(0.0, s["flood_km2"] - (s.get("cropland_km2") or 0) - (s.get("builtup_km2") or 0)
                         - (s.get("tree_cover_km2") or 0))
    roads = m.get("roads")                       # keep roads that add_roads_to_demo.py already put in (order must not matter)
    note = demo_notes.note_with_roads(roads.get("settlement_buffer_m", 250)) if roads else demo_notes.NOTE_NO_ROADS
    m.update(mock=False, bounds=bounds, stats=s, config=config, roads=roads,
             previews={n: f"/files/presets/{preset_id}/previews/{n}.png" for n in PREVIEWS},
             warnings=[{"level": "info", "code": "static_demo", "message": note}])
    with open(mpath, "w") as f:
        json.dump(m, f, separators=(",", ":"))

    epath = os.path.join(api, "events.json")
    with open(epath) as f:
        ev = json.load(f)
    for e in ev["events"]:
        if e["id"] == preset_id:
            e["mock"] = False
    with open(epath, "w") as f:
        json.dump(ev, f, separators=(",", ":"))
    cpath = os.path.join(api, "config.json")
    with open(cpath) as f:
        c = json.load(f)
    c.update(mode="static", static_demo=True)
    with open(cpath, "w") as f:
        json.dump(c, f, separators=(",", ":"))
    return m


def download(name, url, tries=3):
    import requests
    last = ""
    for i in range(tries):
        r = requests.get(url, timeout=600)
        if r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n":
            return r.content
        last = f"HTTP {r.status_code}: {r.text[:300]}"
        print(f"   ! {name}: {last} (attempt {i + 1}/{tries})")
        time.sleep(5)
    raise RuntimeError(f"could not download {name}: {last}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="Earth Engine Cloud project ID")
    ap.add_argument("--preset", default="kerala_2018_alappuzha")
    ap.add_argument("--max-dim", type=int, default=1600, help="longest image side in pixels")
    ap.add_argument("--public", default=os.path.join(ROOT, "public"))
    a = ap.parse_args(argv)
    if a.project.upper().startswith("YOUR"):
        raise SystemExit("Use your real Earth Engine project ID, not a placeholder.")

    from pipeline import flood_pipeline as fp
    from common import presets
    preset = next((p for p in presets.load_presets() if p["id"] == a.preset), None)
    if not preset:
        raise SystemExit(f"unknown preset {a.preset}")
    r, spec = preset["spec"]["region"], preset["spec"]
    ev = {"country": r["country"], "level": r["level"], "parent": r["parent"], "name": r["name"],
          "pre": spec["pre"], "post": spec["post"]}
    t0 = time.time()
    fp.init_ee(a.project)
    print("1/4 building the pipeline graph (pass choice, Otsu, Random Forest check) ...", flush=True)
    run = fp.prepare_run(a.preset, ev, r["name"], no_rf=False)
    print("2/4 computing statistics ...", flush=True)
    stats = run.stats.first().toDictionary().getInfo()
    print(f"    flood {stats['flood_km2']:.2f} km2, rule-only {stats['rule_based_km2']:.2f}, "
          f"unmasked {stats['flood_unmasked_km2']:.2f}, population {stats['population_exposed']:.0f}")
    print("3/4 requesting map images ...", flush=True)
    urls, bounds = fp.preview_urls(run, max_dim=a.max_dim)
    pngs = {}
    for name in PREVIEWS:
        print(f"    downloading {name} ...", flush=True)
        pngs[name] = download(name, urls[name])
        print(f"      {len(pngs[name]) / 1e3:.0f} KB")
    print("4/4 writing files ...")
    apply_real_assets(a.public, a.preset, stats, fp.config_report(run), bounds, pngs)
    print(f"\nDone in {time.time() - t0:.0f} s. Next:")
    print("  python -m pytest -q tests/test_static_demo.py        (should pass)")
    print("  python tests/static_server.py public 8080            (look at it: http://localhost:8080)")
    print("  git add public && git commit -m \"Real imagery for the static demo\" && git push")


if __name__ == "__main__":
    main()
