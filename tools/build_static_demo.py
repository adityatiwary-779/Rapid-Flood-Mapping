#!/usr/bin/env python3
"""Build a STATIC demo of the site (no backend needed) into public/.

It runs the local mock stack once, asks the real API handlers for exactly what the browser would ask
(/api/config, /api/events, /api/events/<id>, /api/regions and every /files/... it references) and saves
the answers as plain files. public/vercel.json rewrites /api/... to those files.

  python tools/build_static_demo.py            # -> public/demo/api/*.json and public/files/presets/*

Only the Alappuzha 2018 preset is shipped: its flood polygons and area figures come from a real pipeline run;
the radar images are synthetic. Anything the mock stack invents (roads, settlements, unmasked area, severity,
GeoTIFFs) is stripped out, see strip_fabricated().
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))


# The mock stack invents some things (road km, "Sample village" places, the unmasked-area figure, a constant
# severity raster, a GeoTIFF whose georeferencing does not match its pixel grid). Fine for local development,
# NOT for a public demo: remove them. What stays is real: flood polygons, flooded area, land-cover and
# population figures, threshold/method; the radar images stay but are synthetic and labelled as such.
DROP_FILES = ("flood.tif", "dvv_db.tif", "flooded_roads.geojson", "affected_settlements.geojson",
              "affected_settlements.csv", "roads_flooded_by_type.csv")
DROP_PREVIEWS = ("severity", "flood_unmasked")


def blank_column(csv_bytes, column):
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode())))
    for r in rows:
        r[column] = ""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().encode()


def strip_fabricated(m):
    m["roads"] = None
    m["stats"]["flood_unmasked_km2"] = None
    m["files"] = {k: v for k, v in m["files"].items() if k not in DROP_FILES}
    m["previews"] = {k: v for k, v in m["previews"].items() if k not in DROP_PREVIEWS}
    m["warnings"] = (m.get("warnings") or []) + [{
        "level": "warning", "code": "static_demo",
        "message": "Static demo: the flood extent, areas, land cover and population figures come from one real "
                   "pipeline run, but the radar images are synthetic. Road analysis, severity, the unmasked "
                   "layer and the GeoTIFFs are not included here."}]
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "public"))
    ap.add_argument("--only", nargs="+", default=["kerala_2018_alappuzha"])
    a = ap.parse_args()

    os.environ["BACKEND_MODE"] = "mock"
    os.environ["MOCK_DIR"] = tempfile.mkdtemp()
    from fastapi.testclient import TestClient
    from common import services
    from common.config import load_settings
    services.set_services(services.build_services(load_settings()))
    import api.index as index
    c = TestClient(index.app)

    def get(path):
        r = c.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        return r

    def write(rel, data):
        p = os.path.join(a.out, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else json.dumps(data, separators=(",", ":")).encode())

    shutil.rmtree(os.path.join(a.out, "demo"), ignore_errors=True)
    shutil.rmtree(os.path.join(a.out, "files"), ignore_errors=True)

    cfg = get("/api/config").json()
    cfg.update(static_demo=True, poll_ms=3000)
    write("demo/api/config.json", cfg)
    write("demo/api/regions.json", get("/api/regions").json())
    events = get("/api/events").json()
    events["events"] = [e for e in events["events"] if e["id"] in a.only]
    assert events["events"], "no matching preset"
    write("demo/api/events.json", events)
    total = 0
    for e in events["events"]:
        m = strip_fabricated(get(f"/api/events/{e['id']}").json())
        write(f"demo/api/events/{e['id']}.json", m)
        for url in list(m["files"].values()) + list(m["previews"].values()):
            data = get(url).content
            if url.endswith("/stats.csv"):
                data = blank_column(data, "flood_unmasked_km2")
            write(url.lstrip("/"), data)
            total += len(data)
    print(f"wrote {len(events['events'])} preset(s), {total / 1e6:.1f} MB of files into {a.out}")


if __name__ == "__main__":
    main()
