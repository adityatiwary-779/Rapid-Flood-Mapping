#!/usr/bin/env python3
"""
Sentinel-1 rapid flood mapping pipeline (Google Earth Engine, Python API).

Steps per event/region:
  1. Pick Sentinel-1 IW pass (ASC/DESC) with images in both windows
  2. Pre/post median composites + speckle smoothing
  3. Change detection: dVV = post - pre, threshold chosen by Otsu (clamped)
  4. Masks: permanent water (JRC), steep slope (SRTM), high HAND (MERIT), tiny patches
  5. Optional Random Forest refinement (weak labels from step 3)
  6. Exposure stats: cropland / built-up (ESA WorldCover), population (WorldPop)
  7. Exports (Drive, optional Asset): flood GeoTIFF, dVV GeoTIFF, stats CSV, optional GeoJSON
"""
import argparse
import json
import math
import os
import re

import ee

DEFAULT_CFG = {
    "post_vv_db": -15.0,          # water is darker than this in post image
    "diff_clamp": [-6.0, -2.5],   # Otsu threshold is clamped to this dB range
    "max_slope_deg": 5.0,
    "max_hand_m": 15.0,
    "max_jrc_occurrence": 80,     # % of time water -> treated as permanent
    "min_patch_pixels": 10,       # at export scale
    "smooth_radius_m": 50,
    "export_scale": 30,
    "rf_trees": 100,
    "rf_points_per_class": 2000,
}


# ----------------------------------------------------------------- helpers
def init_ee(project):
    try:
        ee.Initialize(project=project)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=project)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def gaul(country, level):
    return (ee.FeatureCollection(f"FAO/GAUL/2015/level{level}")
            .filter(ee.Filter.eq("ADM0_NAME", country)))


def list_regions(country, level):
    return gaul(country, level).aggregate_array(f"ADM{level}_NAME").distinct().sort().getInfo()


def get_region(country, level, name):
    fc = gaul(country, level).filter(ee.Filter.eq(f"ADM{level}_NAME", name))
    if fc.size().getInfo() == 0:
        raise ValueError(f"Region '{name}' not found (country={country}, level={level}). "
                         f"Run with --list-regions to see valid names.")
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
    """Otsu threshold from an ee.Reducer.histogram() result."""
    hist = ee.Dictionary(hist)
    counts = ee.Array(hist.get("histogram"))
    means = ee.Array(hist.get("bucketMeans"))
    size = ee.Number(means.length().get([0]))
    total = ee.Number(counts.reduce(ee.Reducer.sum(), [0]).get([0]))
    total_sum = ee.Number(means.multiply(counts).reduce(ee.Reducer.sum(), [0]).get([0]))
    mean = total_sum.divide(total)
    indices = ee.List.sequence(1, size)

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

    bss = indices.map(bss_fn)
    return means.sort(bss).get([-1])


def diff_threshold(diff_vv, region, cfg):
    hist = diff_vv.reduceRegion(
        reducer=ee.Reducer.histogram(255, 0.1), geometry=region, scale=100,
        maxPixels=1e10, bestEffort=True, tileScale=4).get("VV")
    lo, hi = cfg["diff_clamp"]
    try:
        raw = float(otsu(hist).getInfo())
        if math.isnan(raw):
            raise ValueError("NaN")
    except Exception as e:
        print(f"   ! Otsu failed ({e}); falling back to {hi} dB")
        raw = hi
    return max(lo, min(hi, raw)), raw


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
    feats = ee.Image.cat([
        pre.rename(["VV_pre", "VH_pre"]), post.rename(["VV_post", "VH_post"]),
        diff.rename("dVV"), slope, hand, occ]).float()
    conf_flood = rule_flood.And(diff.lt(thr - 1))
    conf_non = diff.gt(-1).And(valid)
    label = (ee.Image(0).where(conf_flood, 1)
             .updateMask(conf_flood.Or(conf_non)).rename("label"))
    n = cfg["rf_points_per_class"]
    samples = feats.addBands(label).stratifiedSample(
        numPoints=n, classBand="label", region=region, scale=30, seed=42,
        classValues=[0, 1], classPoints=[n, n], tileScale=4)
    clf = (ee.Classifier.smileRandomForest(cfg["rf_trees"])
           .train(samples, "label", feats.bandNames()))
    return clean(feats.classify(clf).eq(1).And(valid), cfg)


def compute_stats(flood, rule_flood, region, cfg, meta):
    scale = cfg["export_scale"]
    wc = ee.Image("ESA/WorldCover/v200").select("Map")
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


# ------------------------------------------------------------------ driver
def run_region(key, ev, name, args):
    cfg = {**DEFAULT_CFG, **ev.get("cfg", {})}
    tag = f"{key}_{slug(name)}"
    print(f"\n=== {tag} ===")
    region = get_region(ev["country"], ev.get("level", 1), name)
    pre_rng, post_rng = ev["pre"], ev["post"]

    orbit = ev.get("orbit_pass")
    if orbit:
        counts = (s1_collection(region, *pre_rng, orbit).size().getInfo(),
                  s1_collection(region, *post_rng, orbit).size().getInfo())
    else:
        orbit, counts = choose_pass(region, pre_rng, post_rng)
    print(f"   pass={orbit}  images pre/post={counts}")

    pre = composite(s1_collection(region, *pre_rng, orbit), region, cfg)
    post = composite(s1_collection(region, *post_rng, orbit), region, cfg)
    vv_pre, vv_post = pre.select("VV"), post.select("VV")
    diff = vv_post.subtract(vv_pre)  # band name stays "VV"

    thr, raw = diff_threshold(diff, region, cfg)
    print(f"   Otsu dVV={raw:.2f} dB -> threshold used={thr:.2f} dB")
    diff = diff.rename("dVV")

    slope, hand, occ = terrain_layers()
    valid = (slope.lt(cfg["max_slope_deg"]).And(hand.lt(cfg["max_hand_m"]))
             .And(occ.lt(cfg["max_jrc_occurrence"])))
    raw_mask = (diff.lt(thr).And(vv_post.lt(cfg["post_vv_db"]))
                .And(vv_pre.gte(cfg["post_vv_db"])))
    rule_flood = clean(raw_mask.And(valid), cfg)

    final = rule_flood if args.no_rf else rf_refine(
        pre, post, diff, thr, slope, hand, occ, valid, rule_flood, region, cfg)
    final = final.rename("flood").unmask(0).uint8()

    meta = {"event": key, "region": name, "country": ev["country"],
            "pre_start": pre_rng[0], "pre_end": pre_rng[1],
            "post_start": post_rng[0], "post_end": post_rng[1],
            "orbit_pass": orbit, "n_pre_images": counts[0], "n_post_images": counts[1],
            "otsu_raw_db": raw, "threshold_db": thr, "used_rf": not args.no_rf}
    stats = compute_stats(final.selfMask(), rule_flood, region, cfg, meta)

    if args.print_stats:
        print("   stats:", json.dumps(stats.first().toDictionary().getInfo(), indent=2))

    scale, folder = cfg["export_scale"], args.drive_folder
    tasks = [
        ee.batch.Export.image.toDrive(
            image=final, description=f"{tag}_flood", folder=folder,
            fileNamePrefix=f"{tag}_flood", region=region, scale=scale,
            crs="EPSG:4326", maxPixels=1e13),
        ee.batch.Export.image.toDrive(
            image=diff.float(), description=f"{tag}_dvv_db", folder=folder,
            fileNamePrefix=f"{tag}_dvv_db", region=region, scale=scale,
            crs="EPSG:4326", maxPixels=1e13),
        ee.batch.Export.table.toDrive(
            collection=stats, description=f"{tag}_stats", folder=folder,
            fileNamePrefix=f"{tag}_stats", fileFormat="CSV"),
    ]
    if args.vectors:
        vec = final.selfMask().reduceToVectors(
            geometry=region, scale=scale, eightConnected=True,
            maxPixels=1e13, tileScale=8)
        tasks.append(ee.batch.Export.table.toDrive(
            collection=vec, description=f"{tag}_flood_vec", folder=folder,
            fileNamePrefix=f"{tag}_flood_vec", fileFormat="GeoJSON"))
    if args.asset_root:
        tasks.append(ee.batch.Export.image.toAsset(
            image=final, description=f"{tag}_flood_asset",
            assetId=f"{args.asset_root}/{tag}_flood", region=region, scale=scale,
            maxPixels=1e13, pyramidingPolicy={".default": "mode"}))
    for t in tasks:
        t.start()
        print(f"   started: {t.config['description']}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="Earth Engine Cloud project ID")
    ap.add_argument("--events", default=os.path.join(here, "events.json"))
    ap.add_argument("--event", help="event key from events.json")
    ap.add_argument("--all", action="store_true", help="run every event except templates")
    ap.add_argument("--list-regions", action="store_true")
    ap.add_argument("--country", default="India")
    ap.add_argument("--level", type=int, default=1, help="GAUL level: 1=state, 2=district")
    ap.add_argument("--no-rf", action="store_true", help="skip Random Forest refinement")
    ap.add_argument("--vectors", action="store_true", help="also export flood polygons (GeoJSON)")
    ap.add_argument("--drive-folder", default="flood_outputs")
    ap.add_argument("--asset-root", default=None,
                    help="e.g. projects/YOUR_PROJECT/assets/flood (folder must exist)")
    ap.add_argument("--print-stats", action="store_true",
                    help="also print stats now (small regions only)")
    args = ap.parse_args()

    init_ee(args.project)

    if args.list_regions:
        print("\n".join(list_regions(args.country, args.level)))
        return

    with open(args.events) as f:
        events = json.load(f)
    if args.all:
        keys = [k for k in events if not k.startswith("template")]
    elif args.event:
        keys = [args.event]
    else:
        ap.error("give --event KEY, --all, or --list-regions")

    for key in keys:
        ev = events[key]
        names = ev["name"]
        if names == "ALL":
            names = list_regions(ev["country"], ev.get("level", 1))
        elif isinstance(names, str):
            names = [names]
        for name in names:
            try:
                run_region(key, ev, name, args)
            except Exception as e:
                print(f"   ! skipped {name}: {e}")


if __name__ == "__main__":
    main()
