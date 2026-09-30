"""MOCK MODE ONLY. Synthetic stand-ins for Earth Engine / OSM so the whole app runs locally with no cloud
accounts. Flood polygons and area numbers come from the real Alappuzha 2018 run, but everything is
labelled mock=true in the manifest and the UI. Radar images here are synthetic, not real imagery."""
import csv
import io
import json
import math
import os
import struct
import threading
import zlib
from functools import lru_cache

import numpy as np

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "fixtures", "alappuzha_2018")


# ------------------------------------------------------------ file writers (no PIL / rasterio needed)
def png_bytes(rgba):
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def geotiff_bytes(arr, west, south, east, north, nodata=None):
    """Uncompressed single-band GeoTIFF in EPSG:4326 (arr: 2-D uint8 or float32)."""
    h, w = arr.shape
    is_float = arr.dtype.kind == "f"
    pix = arr.astype("<f4" if is_float else "u1").tobytes()
    doubles_scale = struct.pack("<3d", (east - west) / w, (north - south) / h, 0)
    doubles_tie = struct.pack("<6d", 0, 0, 0, west, north, 0)
    geokeys = struct.pack("<16H", 1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)
    nd = (str(nodata) + "\0").encode() if nodata is not None else None
    entries = [  # (tag, type, count, value-or-blob)
        (256, 4, 1, w), (257, 4, 1, h), (258, 3, 1, 32 if is_float else 8), (259, 3, 1, 1),
        (262, 3, 1, 1), (273, 4, 1, None), (277, 3, 1, 1), (278, 4, 1, h), (279, 4, 1, len(pix)),
        (339, 3, 1, 3 if is_float else 1),
        (33550, 12, 3, doubles_scale), (33922, 12, 6, doubles_tie), (34735, 3, 16, geokeys)]
    if nd:
        entries.append((42113, 2, len(nd), nd))
    entries.sort(key=lambda e: e[0])
    ifd_size = 2 + 12 * len(entries) + 4
    off = 8 + ifd_size
    blobs, offsets = b"", {}
    for tag, _, _, val in entries:
        if isinstance(val, bytes) and len(val) > 4:
            offsets[tag] = off + len(blobs)
            blobs += val + (b"\0" if len(val) % 2 else b"")
    pix_off = off + len(blobs)
    out = struct.pack("<2sHI", b"II", 42, 8) + struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        if tag == 273:
            val = pix_off
        if isinstance(val, bytes):
            field = (struct.pack("<I", offsets[tag]) if len(val) > 4 else val.ljust(4, b"\0"))
        elif typ == 3:
            field = struct.pack("<HH", val, 0)
        else:
            field = struct.pack("<I", val)
        out += struct.pack("<HHI", tag, typ, cnt) + field
    return out + struct.pack("<I", 0) + blobs + pix


# ------------------------------------------------------------ fixture rasters
_GEOS_LOCK = threading.RLock()   # shapely/GEOS geometries must not be used from two threads at once


@lru_cache(maxsize=1)
def _fixture():
    import shapely
    from shapely.geometry import shape
    with open(os.path.join(FIX, "flood_rf.geojson")) as f:
        fc = json.load(f)
    geoms = [shape(x["geometry"]) for x in fc["features"]]
    union = shapely.union_all(geoms)
    w, s, e, n = union.bounds
    return fc, union, (s, w, n, e)


def _merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


@lru_cache(maxsize=4)
def _grid(max_dim=700):
    """Web-Mercator pixel grid over the fixture bounds (matches what EE thumbnails use)."""
    import shapely
    _, union, (s, w, n, e) = _fixture()
    x0, x1 = math.radians(w), math.radians(e)
    y0, y1 = _merc_y(s), _merc_y(n)
    sc = max_dim / max(x1 - x0, y1 - y0)
    W, H = int((x1 - x0) * sc), int((y1 - y0) * sc)
    xs = x0 + (np.arange(W) + .5) / sc
    ys = y1 - (np.arange(H) + .5) / sc
    lons = np.degrees(xs)[None, :].repeat(H, 0)
    lats = np.degrees(2 * np.arctan(np.exp(ys)) - math.pi / 2)[:, None].repeat(W, 1)
    mask = shapely.contains_xy(union, lons, lats)
    return W, H, lons, lats, mask


def _texture(lons, lats, seed):
    rng = np.random.default_rng(seed)
    base = np.sin(lons * 260) * np.cos(lats * 230) * .5 + rng.normal(0, .25, lons.shape)
    return base


@lru_cache(maxsize=1)
def _previews_cached():
    with _GEOS_LOCK:
        return _previews()


def _previews():
    W, H, lons, lats, mask = _grid()
    tex = _texture(lons, lats, 1)
    grey = lambda v: np.dstack([v, v, v, np.full(v.shape, 255)]).astype("u1")  # noqa: E731
    pre = np.clip(150 + 45 * tex, 0, 255)
    post = np.where(mask, np.clip(45 + 25 * tex, 0, 255), pre)
    flood = np.zeros((H, W, 4), "u1")
    flood[mask] = (34, 211, 238, 217)
    rule = np.zeros((H, W, 4), "u1")
    rule[mask & (tex > -0.15)] = (167, 139, 250, 217)
    wide = mask | (np.sin(lons * 90) * np.cos(lats * 80) > .82)
    unm = np.zeros((H, W, 4), "u1")
    unm[wide] = (245, 165, 36, 217)
    sev = np.zeros((H, W, 4), "u1")
    t = np.clip((tex + .6) / 1.4, 0, 1)[mask][:, None]
    sev[mask] = np.hstack([253 - 72 * t, 230 - 190 * t, 138 - 100 * t, np.full(t.shape, 235)]).astype("u1")
    return {"pre_vv": png_bytes(grey(pre)), "post_vv": png_bytes(grey(post)), "flood": png_bytes(flood), "flood_rule": png_bytes(rule),
            "flood_unmasked": png_bytes(unm), "severity": png_bytes(sev)}


# ------------------------------------------------------------ fake preflight
class FakeInfo:
    def __init__(self, regions=None):
        self.regions = regions or {}

    def __call__(self, country, level, name, parent, pre, post):
        from datetime import date
        with _GEOS_LOCK:
            _, _, bounds = _fixture()
        area = 1316.69
        for r in self.regions.get(country, {}).get(str(level), []):
            if r["name"] == name and r.get("parent") == parent and r.get("area_km2"):
                area = r["area_km2"]
        n_regions = 2 if "Twin" in name else 1
        if "Huge" in name:
            area = 20000.0

        def n(win):
            days = (date.fromisoformat(win[1]) - date.fromisoformat(win[0])).days + 1
            if "NoImages" in name:
                return 0
            if "Sparse" in name:
                return 1
            return max(1, math.ceil(days / 12))
        return {"n_regions": n_regions, "area_km2": area, "bounds": [list(bounds[:2]), list(bounds[2:])],
                "counts": {"DESCENDING": {"pre": n(pre), "post": n(post)},
                           "ASCENDING": {"pre": 0, "post": 0}}}


# ------------------------------------------------------------ fake driver (same interface as EEDriver)
class FakeDriver:
    """Name hooks for tests: region names containing 'FailStart' raise at start; 'FailExport' fails an export."""

    def __init__(self, storage):
        self.storage, self.jobs = storage, {}

    def start(self, spec, job_id, prefix):
        from pipeline import flood_pipeline as fp
        r = spec["region"]
        if "FailStart" in r["name"]:
            raise RuntimeError("Earth Engine computation timed out after 300 seconds")
        with _GEOS_LOCK:
            fc, _, (s, w, n, e) = _fixture()
        with open(os.path.join(FIX, "stats_rf.json")) as f:
            st = json.load(f)
        st.update(event=job_id, region=r["name"], pre_start=spec["pre"][0], pre_end=spec["pre"][1],
                  post_start=spec["post"][0], post_end=spec["post"][1],
                  flood_unmasked_km2=st["flood_km2"] * 1.31)
        buf = io.StringIO()
        wr = csv.DictWriter(buf, fieldnames=fp.STATS_COLUMNS, extrasaction="ignore")
        wr.writeheader()
        wr.writerow(st)
        with _GEOS_LOCK:
            _, _, _, _, mask = _grid()
        files = {"stats.csv": buf.getvalue().encode(),
                 "flood_vec.geojson": json.dumps(fc).encode(),
                 "flood.tif": geotiff_bytes(mask.astype("u1"), w, s, e, n),
                 "dvv_db.tif": geotiff_bytes(np.where(mask, -6.0, -9999.0).astype("f4"), w, s, e, n,
                                             nodata=-9999)}
        self.jobs[job_id] = {"prefix": prefix, "files": files, "name": r["name"], "polls": 0}
        cfg = fp.DEFAULT_CFG
        config = {"method": "MOCK: fixture from the real Alappuzha 2018 run",
                  "threshold_db": st["threshold_db"], "otsu_raw_db": st["otsu_raw_db"], "otsu_ok": True,
                  "diff_clamp_db": cfg["diff_clamp"], "post_vv_db": cfg["post_vv_db"], "used_rf": True,
                  "orbit_pass": "DESCENDING",
                  "speckle": f"median composite + {cfg['smooth_radius_m']} m circular focal median",
                  "masks": {"max_slope_deg": cfg["max_slope_deg"], "max_hand_m": cfg["max_hand_m"],
                            "max_jrc_occurrence_pct": cfg["max_jrc_occurrence"],
                            "min_patch_pixels": cfg["min_patch_pixels"]},
                  "export_scale_m": cfg["export_scale"], "n_pre_images": st["n_pre_images"],
                  "n_post_images": st["n_post_images"]}
        return {"task_ids": {k: f"{job_id}:{k}" for k in ("flood", "dvv_db", "stats", "flood_vec")},
                "config": config, "bounds": [[s, w], [n, e]], "previews": _previews_cached(), "mock": True}

    def poll(self, task_ids):
        job_id = next(iter(task_ids.values())).split(":")[0]
        j = self.jobs[job_id]
        j["polls"] += 1
        out = {name: {"state": "COMPLETED" if j["polls"] > i else "RUNNING", "error": None}
               for i, name in enumerate(task_ids)}
        if "FailExport" in j["name"] and j["polls"] >= 2:
            out["flood_vec"] = {"state": "FAILED", "error": "Computation timed out."}
        if all(v["state"] == "COMPLETED" for v in out.values()):
            for fname, data in j["files"].items():
                self.storage.put_bytes(f"{j['prefix']}/{fname}", data)
        return out


# ------------------------------------------------------------ fake roads (same signature as compute_exposure)
def fake_roads(flood_path, place, out_dir, motorable_only=True, settlement_buffer_m=250.0):
    os.makedirs(out_dir, exist_ok=True)
    with _GEOS_LOCK:
        _, _, (s, w, n, e) = _fixture()
    cy, cx = (s + n) / 2, (w + e) / 2
    line = lambda dx, dy: {"type": "Feature", "properties": {"highway": "primary"},  # noqa: E731
                           "geometry": {"type": "LineString", "coordinates": [[cx, cy], [cx + dx, cy + dy]]}}
    pts = [{"type": "Feature", "properties": {"name": f"Sample village {i + 1}", "place": "village"},
            "geometry": {"type": "Point", "coordinates": [cx + .01 * i, cy + .008 * i]}} for i in range(4)]
    fc = lambda feats: json.dumps({"type": "FeatureCollection", "features": feats})  # noqa: E731
    open(os.path.join(out_dir, "flooded_roads.geojson"), "w").write(fc([line(.05, .02), line(-.04, .03)]))
    open(os.path.join(out_dir, "affected_settlements.geojson"), "w").write(fc(pts))
    open(os.path.join(out_dir, "affected_settlements.csv"), "w").write(
        "place,name\n" + "".join(f"village,Sample village {i + 1}\n" for i in range(4)))
    open(os.path.join(out_dir, "roads_flooded_by_type.csv"), "w").write(
        "highway,flooded_km\nprimary,12.4\nresidential,30.1\ntertiary,8.9\n")
    return {"place": place, "road_km_total": 1520.3, "road_km_flooded": 51.4, "settlements_total": 212,
            "settlements_affected": 4,
            "by_type": [{"highway": "residential", "flooded_km": 30.1}, {"highway": "primary", "flooded_km": 12.4},
                        {"highway": "tertiary", "flooded_km": 8.9}],
            "settlements": [{"name": f"Sample village {i + 1}", "place": "village"} for i in range(4)]}
