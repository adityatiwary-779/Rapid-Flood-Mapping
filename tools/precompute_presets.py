#!/usr/bin/env python3
"""Build the preset events (data/presets.json) with the REAL pipeline and store them in Cloud Storage
under presets/<id>/ (+ presets/index.json). Uses exactly the same worker code as a custom job.

  python tools/precompute_presets.py                       # all presets
  python tools/precompute_presets.py --only kerala_2018_alappuzha
  python tools/precompute_presets.py --force               # rebuild even if it already exists
  python tools/precompute_presets.py --mock                # dry run of this tool on the local mock stack

Needs the environment variables from .env.example (real mode). Takes a few minutes per preset.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", help="preset ids to build")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--mock", action="store_true", help="use the local mock stack (no cloud accounts)")
    ap.add_argument("--poll-scale", type=float, default=1.0, help="multiply poll waits (tests use 0)")
    a = ap.parse_args(argv)
    if a.mock:
        os.environ["BACKEND_MODE"] = "mock"

    from common import presets, services
    from common.config import load_settings
    svc = services.build_services(load_settings())
    todo = [p for p in presets.load_presets() if not a.only or p["id"] in a.only]
    if not todo:
        raise SystemExit("No matching presets. Available: " + ", ".join(p["id"] for p in presets.load_presets()))
    failed = []
    for p in todo:
        if not a.force and svc.storage.exists(f"presets/{p['id']}/manifest.json"):
            print(f"= {p['id']}: already built (use --force to rebuild)")
            continue
        print(f"\n== building {p['id']}  ({p['event']} / {p['district']})")
        t0 = time.time()
        try:
            presets.run_preset(svc, p, sleep=time.sleep, delay_scale=a.poll_scale,
                               log=lambda m: print(m, flush=True))
            print(f"   done in {time.time() - t0:.0f} s")
        except Exception as e:  # noqa: BLE001 - keep going, report at the end
            failed.append(p["id"])
            print(f"   ! FAILED: {str(e)[:300]}")
    print("\nbuilt:", ", ".join(p["id"] for p in todo if p["id"] not in failed) or "nothing")
    if failed:
        print("failed:", ", ".join(failed), " (check the error above; wrong district spelling is the usual cause: "
              "run tools/build_regions.py and fix data/presets.json)")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
