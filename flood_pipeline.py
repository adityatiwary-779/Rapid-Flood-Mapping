#!/usr/bin/env python3
"""
Sentinel-1 rapid flood mapping pipeline (Google Earth Engine, Python API).

Steps per event/region:
  1. Pick Sentinel-1 IW pass (ASC/DESC) with images in both windows
  2. Pre/post median composites + speckle smoothing
  3. Change detection: dVV = post - pre, threshold chosen by Otsu (clamped)
  4. Masks: permanent water (JRC), steep slope (SRTM), high HAND (MERIT), tiny patches
  5. Optional Random Forest refinement (weak labels from step 3 -- NOT independent validation)
  6. Exposure stats: cropland / built-up / tree cover (ESA WorldCover), population (WorldPop)
  7. Exports (Drive, optional Asset): flood GeoTIFF, dVV GeoTIFF, stats CSV, optional GeoJSON

KNOWN LIMITATION: the Random Forest is trained on labels produced by the threshold
step, so agreement between the two is NOT accuracy. Validate against a Copernicus EMS
map or news-reported flooded areas before quoting any number.
"""
import argparse
import difflib
import json
import math
import os
import re
import time
import traceback

import ee

DEFAULT_CFG = {
    "post_vv_db": -15.0,          # water is darker than this in post image
    "diff_clamp": [-6.0, -2.5],   # Otsu threshold is clamped to this dB range
    "otsu_candidates_only": False,  # True: build the Otsu histogram only from dark, valid pixels
    "max_slope_deg": 5.0,
    "max_hand_m": 15.0,
    "max_jrc_occurrence": 80,     # % of time water -> treated as permanent
    "min_patch_pixels": 10,       # at export scale
    "smooth_radius_m": 50,
    "export_scale": 30,
    "rf_trees": 100,
    "rf_points_per_class": 2000,
    "rf_min_samples_per_class": 100,  # below this the RF is skipped (rule-based result used)
}

NODATA_DVV = -9999


# ----------------------------------------------------------------- helpers
def init_ee(project):
    """Initialize EE; only start the auth flow when credentials are actually missing."""
    project = project or os.environ.get("EARTHENGINE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("No Cloud project given. Use --project ID or set EARTHENGINE_PROJECT.")
    try:
        ee.Initialize(project=project)
    except ee.EEException as e:
        msg = str(e).lower()
        if "authorize" in msg or "credentials" in msg or "authenticate" in msg:
            ee.Authenticate()
            ee.Initialize(project=project)
        else:
            raise SystemExit(f"Earth Engine init failed for project '{project}': {e}")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def gaul(country, level, parent=None):
    fc = (ee.FeatureCollection(f"FAO/GAUL/2015/level{level}")
          .filter(ee.Filter.eq("ADM0_NAME", country)))
    if parent and level >= 2:
        fc = fc.filter(ee.Filter.eq("ADM1_NAME", parent))
    return fc


def list_regions(country, level, parent=None):
    return (gaul(country, level, parent).aggregate_array(f"ADM{level}_NAME")
            .distinct().sort().getInfo())


def get_region(country, level, name, parent=None):
    fc = gaul(country, level, parent).filter(ee.Filter.eq(f"ADM{level}_NAME", name))
    if fc.size().getInfo() == 0:
        hint = ""
        try:
            close = difflib.get_close_matches(name, list_regions(country, level, parent), n=5)
            if close:
                hint = f" Did you mean: {', '.join(close)}?"
        except Exception:
            pass
        raise ValueError(f"Region '{name}' not found (country={country}, level={level}, "
                         f"parent={parent}). Run with --list-regions to see valid names.{hint}")
    return fc.geometry()


def s1_collection(region, start, end, orbit_pass=None):
    col = (ee.ImageCollection("COPERNICUS/S1_GRD")
           .filterBounds(region).filterDate(start, end)
           .filter(ee.Filter.eq("instrumentMode", "IW"))
           .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
           .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
           .select(["VV", "VH"]))
    if orbit_pass:
        col = col.filter(ee.Filter.eq("orbitProperties_pass", orbit_pass))
    return col


def choose_pass(region, pre, post):
    best, best_score, counts = None, 0, None
    for p in ("DESCENDING", "ASCENDING"):
        n_pre = s1_collection(region, pre[0], pre[1], p).size().getInfo()
        n_post = s1_collection(region, post[0], post[1], p).size().getInfo()
        score = min(n_pre, n_post)
        if score > best_score:
            best, best_score, counts = p, score, (n_pre, n_post)
    if best is None:
        raise RuntimeError("No Sentinel-1 IW images in both windows. Widen the date ranges.")
    return best, counts


def composite(col, region, cfg):
    return (col.median()
            .focalMedian(cfg["smooth_radius_m"], "circle", "meters")
            .clip(region))


def otsu(hist):
    """Otsu threshold from an ee.Reducer.histogram() result.

    Only splits at indices 1..size-1: the last split leaves class B empty (0/0 -> NaN),
    and a NaN sort key can silently win the argmax.
    """
    hist = ee.Dictionary(hist)
    counts = ee.Array(hist.get("histogram"))
    means = ee.Array(hist.get("bucketMeans"))
    size = ee.Number(means.length().get([0]))
    total = ee.Number(counts.reduce(ee.Reducer.sum(), [0]).get([0]))
    total_sum = ee.Number(means.multiply(counts).reduce(ee.Reducer.sum(), [0]).get([0]))
    mean = total_sum.divide(total)
    indices = ee.List.sequence(1, size.subtract(1))

    def bss_fn(i):
        i = ee.Number(i)
        a_counts = counts.slice(0, 0, i)
        a_count = ee.Number(a_counts.reduce(ee.Reducer.sum(), [0]).get([0]))
        a_means = means.slice(0, 0, i)
        a_mean = ee.Number(a_means.multiply(a_counts).reduce(ee.Reducer.sum(), [0])
                           .get([0])).divide(a_count)
        b_count = total.subtract(a_count)
        b_mean = total_sum.subtract(a_count.multiply(a_mean)).divide(b_count)
        return (a_count.multiply(a_mean.subtract(mean).pow(2))
                .add(b_count.multiply(b_mean.subtract(mean).pow(2))))

    bss = ee.Array(indices.map(bss_fn))
    # bss[k] is the split after bucket k (k = 0..size-2) -> sort only those buckets
    candidates = means.slice(0, 0, size.subtract(1))
    return candidates.sort(bss).get([-1])


def diff_threshold(diff_vv, region, cfg):
    """Returns (clamped threshold, raw Otsu value, otsu_ok)."""
    hist = diff_vv.reduceRegion(
        reducer=ee.Reducer.histogram(255, 0.1), geometry=region,
        scale=cfg["export_scale"],           # same grid the flood map is built on
        maxPixels=1e10, bestEffort=True, tileScale=4).get("VV")
    lo, hi = cfg["diff_clamp"]
    try:
        raw = float(otsu(hist).getInfo())
        if math.isnan(raw):
            raise ValueError("NaN")
    except (ee.EEException, ValueError, TypeError) as e:
        print(f"   ! Otsu failed ({e}); falling back to {hi} dB")
        return hi, hi, False
    return max(lo, min(hi, raw)), raw, True


def terrain_layers():
    slope = ee.Terrain.slope(ee.Image("USGS/SRTMGL1_003")).rename("slope")
    hand = ee.Image("MERIT/Hydro/v1_0_1").select("hnd").unmask(0).rename("hand")
    occ = (ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence")
           .unmask(0).rename("occ"))
    return slope, hand, occ


def clean(mask, cfg):
    ok = mask.selfMask().connectedPixelCount(100, True).gte(cfg["min_patch_pixels"])
    return mask.And(ok.unmask(0))


def rf_refine(pre, post, diff, thr, slope, hand, occ, valid, rule_flood, region, cfg):
    """Returns (flood image, used_rf). Falls back to rule_flood if training data is unusable."""
    feats = ee.Image.cat([
        pre.rename(["VV_pre", "VH_pre"]), post.rename(["VV_post", "VH_post"]),
        diff.rename("dVV"), slope, hand, occ]).float()
    conf_flood = rule_flood.And(diff.lt(thr - 1))
    conf_non = diff.gt(-1).And(valid)
    label = (ee.Image(0).where(conf_flood, 1)
             .updateMask(conf_flood.Or(conf_non)).rename("label"))
    n = cfg["rf_points_per_class"]
    samples = feats.addBands(label).stratifiedSample(
        numPoints=n, classBand="label", region=region, scale=cfg["export_scale"], seed=42,
        classValues=[0, 1], classPoints=[n, n], tileScale=4)

    # Server-side training errors only show up later in the Tasks tab, so check first.
    try:
        hist = ee.Dictionary(samples.aggregate_histogram("label")).getInfo() or {}
    except ee.EEException as e:
        print(f"   ! RF sampling failed ({e}); using rule-based result")
        return rule_flood, False
    n0, n1 = int(hist.get("0", 0)), int(hist.get("1", 0))
    print(f"   RF training samples: non-flood={n0} flood={n1}")
    if min(n0, n1) < cfg["rf_min_samples_per_class"]:
        print("   ! too few samples in one class (little/no flood?); using rule-based result")
        return rule_flood, False

    clf = (ee.Classifier.smileRandomForest(cfg["rf_trees"])
           .train(samples, "label", feats.bandNames()))
    return clean(feats.classify(clf).eq(1).And(valid), cfg), True


def compute_stats(flood, rule_flood, region, cfg, meta):
    scale = cfg["export_scale"]
    # ESA WorldCover v200 is an ImageCollection (one global mosaic image), not an Image
    wc = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    area = ee.Image.pixelArea().divide(1e6)
    stack = ee.Image.cat([
        area.updateMask(flood).rename("flood_km2"),
        area.updateMask(flood.And(wc.eq(40))).rename("cropland_km2"),
        area.updateMask(flood.And(wc.eq(50))).rename("builtup_km2"),
        area.updateMask(flood.And(wc.eq(10))).rename("tree_cover_km2"),
        area.updateMask(rule_flood).rename("rule_based_km2"),
        area.updateMask(rule_flood.And(flood)).rename("rule_rf_overlap_km2"),
    ])
    d = stack.reduceRegion(ee.Reducer.sum(), region, scale, maxPixels=1e13, tileScale=16)
    pop = (ee.ImageCollection("WorldPop/GP/100m/pop").filter(ee.Filter.eq("year", 2020))
           .filterBounds(region).mosaic().rename("population_exposed"))
    p = pop.updateMask(flood).reduceRegion(ee.Reducer.sum(), region, 100,
                                           maxPixels=1e13, tileScale=16)
    meta = dict(meta, region_area_km2=region.area(1).divide(1e6))
    props = ee.Dictionary(meta).combine(d).combine(p)
    return ee.FeatureCollection([ee.Feature(None, props)])


def wait_for(tasks, poll_s=20):
    """Poll tasks until all finish; print the server-side error message on failure."""
    pending = list(tasks)
    failed = 0
    while pending:
        time.sleep(poll_s)
        still = []
        for t in pending:
            st = t.status()
            state = st.get("state")
            if state in ("COMPLETED",):
                print(f"   done: {st.get('description')}")
            elif state in ("FAILED", "CANCELLED", "CANCEL_REQUESTED"):
                failed += 1
                print(f"   ! {state}: {st.get('description')}: {st.get('error_message')}")
            else:
                still.append(t)
        pending = still
    return failed


# ------------------------------------------------------------------ driver
def run_region(key, ev, name, args):
    cfg = {**DEFAULT_CFG, **ev.get("cfg", {})}
    tag = f"{key}_{slug(name)}"
    print(f"\n=== {tag} ===")
    region = get_region(ev["country"], ev.get("level", 1), name, ev.get("parent"))
    pre_rng, post_rng = ev["pre"], ev["post"]

    orbit = ev.get("orbit_pass")
    if orbit:
        counts = (s1_collection(region, *pre_rng, orbit).size().getInfo(),
                  s1_collection(region, *post_rng, orbit).size().getInfo())
        if min(counts) == 0:
            raise RuntimeError(f"No {orbit} images in a window (pre/post={counts}).")
    else:
        orbit, counts = choose_pass(region, pre_rng, post_rng)
    print(f"   pass={orbit}  images pre/post={counts}")

    pre = composite(s1_collection(region, *pre_rng, orbit), region, cfg)
    post = composite(s1_collection(region, *post_rng, orbit), region, cfg)
    vv_pre, vv_post = pre.select("VV"), post.select("VV")
    diff = vv_post.subtract(vv_pre)  # band name stays "VV"

    slope, hand, occ = terrain_layers()
    valid = (slope.lt(cfg["max_slope_deg"]).And(hand.lt(cfg["max_hand_m"]))
             .And(occ.lt(cfg["max_jrc_occurrence"])))

    hist_img = diff
    if cfg["otsu_candidates_only"]:
        hist_img = diff.updateMask(valid.And(vv_post.lt(cfg["post_vv_db"])))
    thr, raw, otsu_ok = diff_threshold(hist_img, region, cfg)
    print(f"   Otsu dVV={raw:.2f} dB -> threshold used={thr:.2f} dB (otsu_ok={otsu_ok})")
    diff = diff.rename("dVV")

    raw_mask = (diff.lt(thr).And(vv_post.lt(cfg["post_vv_db"]))
                .And(vv_pre.gte(cfg["post_vv_db"])))
    rule_flood = clean(raw_mask.And(valid), cfg)

    if args.no_rf:
        final, used_rf = rule_flood, False
    else:
        final, used_rf = rf_refine(pre, post, diff, thr, slope, hand, occ, valid,
                                   rule_flood, region, cfg)
    final = final.rename("flood").unmask(0).uint8().clip(region)

    meta = {"event": key, "region": name, "country": ev["country"],
            "pre_start": pre_rng[0], "pre_end": pre_rng[1],
            "post_start": post_rng[0], "post_end": post_rng[1],
            "orbit_pass": orbit, "n_pre_images": counts[0], "n_post_images": counts[1],
            "otsu_raw_db": raw, "threshold_db": thr, "otsu_ok": otsu_ok,
            "used_rf": used_rf}
    stats = compute_stats(final.selfMask(), rule_flood, region, cfg, meta)

    if args.print_stats:
        print("   stats:", json.dumps(stats.first().toDictionary().getInfo(), indent=2))
    if args.no_export:
        return []

    scale, folder = cfg["export_scale"], args.drive_folder
    # masked pixels would be written as 0 dB ("no change"), so give dVV an explicit nodata value
    dvv_out = diff.float().unmask(NODATA_DVV)
    tasks = [
        ee.batch.Export.image.toDrive(
            image=final, description=f"{tag}_flood", folder=folder,
            fileNamePrefix=f"{tag}_flood", region=region, scale=scale,
            crs="EPSG:4326", maxPixels=1e13, fileFormat="GeoTIFF"),
        ee.batch.Export.image.toDrive(
            image=dvv_out, description=f"{tag}_dvv_db", folder=folder,
            fileNamePrefix=f"{tag}_dvv_db", region=region, scale=scale,
            crs="EPSG:4326", maxPixels=1e13, fileFormat="GeoTIFF"),
        ee.batch.Export.table.toDrive(
            collection=stats, description=f"{tag}_stats", folder=folder,
            fileNamePrefix=f"{tag}_stats", fileFormat="CSV"),
    ]
    if args.vectors:
        vec = final.selfMask().reduceToVectors(
            geometry=region, scale=scale, crs="EPSG:4326", geometryType="polygon",
            eightConnected=True, maxPixels=1e13, tileScale=8)
        tasks.append(ee.batch.Export.table.toDrive(
            collection=vec, description=f"{tag}_flood_vec", folder=folder,
            fileNamePrefix=f"{tag}_flood_vec", fileFormat="GeoJSON"))
    if args.asset_root:
        tasks.append(ee.batch.Export.image.toAsset(
            image=final, description=f"{tag}_flood_asset",
            assetId=f"{args.asset_root}/{tag}_flood", region=region, scale=scale,
            crs="EPSG:4326", maxPixels=1e13, pyramidingPolicy={".default": "mode"}))
    for t in tasks:
        t.start()
        print(f"   started: {t.config['description']}  (id {t.id})")
    return tasks


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=None,
                    help="Earth Engine Cloud project ID (or set EARTHENGINE_PROJECT)")
    ap.add_argument("--events", default=os.path.join(here, "events.json"))
    ap.add_argument("--event", help="event key from events.json")
    ap.add_argument("--all", action="store_true", help="run every event except templates")
    ap.add_argument("--list-regions", action="store_true")
    ap.add_argument("--country", default="India")
    ap.add_argument("--level", type=int, default=1, help="GAUL level: 1=state, 2=district")
    ap.add_argument("--parent", default=None,
                    help="with --level 2 --list-regions: only districts of this state")
    ap.add_argument("--no-rf", action="store_true", help="skip Random Forest refinement")
    ap.add_argument("--vectors", action="store_true", help="also export flood polygons (GeoJSON)")
    ap.add_argument("--drive-folder", default="flood_outputs")
    ap.add_argument("--asset-root", default=None,
                    help="e.g. projects/YOUR_PROJECT/assets/flood (folder must exist)")
    ap.add_argument("--print-stats", action="store_true",
                    help="also print stats now (small regions only)")
    ap.add_argument("--no-export", action="store_true",
                    help="build the graph and (with --print-stats) evaluate stats, but start no export tasks")
    ap.add_argument("--wait", action="store_true",
                    help="poll export tasks until they finish and print any server-side errors")
    args = ap.parse_args()

    init_ee(args.project)

    if args.list_regions:
        print("\n".join(list_regions(args.country, args.level, args.parent)))
        return

    with open(args.events) as f:
        events = json.load(f)
    if args.all:
        keys = [k for k in events if not k.startswith("template")]
    elif args.event:
        if args.event not in events:
            ap.error(f"event '{args.event}' not in {args.events}. Available: {', '.join(events)}")
        keys = [args.event]
    else:
        ap.error("give --event KEY, --all, or --list-regions")

    all_tasks, failed_regions = [], []
    for key in keys:
        ev = events[key]
        names = ev["name"]
        if names == "ALL":
            names = list_regions(ev["country"], ev.get("level", 1), ev.get("parent"))
        elif isinstance(names, str):
            names = [names]
        for name in names:
            try:
                all_tasks += run_region(key, ev, name, args)
            except Exception as e:
                failed_regions.append(name)
                print(f"   ! skipped {name}: {type(e).__name__}: {e}")
                if os.environ.get("FLOOD_DEBUG"):
                    traceback.print_exc()

    if args.wait and all_tasks:
        failed = wait_for(all_tasks)
        print(f"\n{len(all_tasks) - failed}/{len(all_tasks)} tasks completed")
    if failed_regions:
        print(f"\nRegions skipped: {', '.join(failed_regions)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
