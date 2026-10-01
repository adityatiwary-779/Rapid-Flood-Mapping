"""Build a REAL risk package for one district: tools/make_synthetic_risk_package.py writes the same format.

Two stages so each runs on the network that can reach its service:
  --stage ee    Earth Engine: hazard/exposure grids (needs `earthengine authenticate`)
  --stage osm   OpenStreetMap: road graph, hospitals, shelters (needs a network that reaches Overpass)
  --stage all   both (default)

    python tools/build_risk_package.py --project YOUR_PROJECT --district Pathanamthitta \\
        --calamities flood landslide --events "Floods 2018; landslides in the eastern hills" --stage ee
    python tools/build_risk_package.py --district Pathanamthitta --stage osm

Layers (all 0..1, row 0 = north, ~900 m cells):
  flood      0.5*proximity to permanent/seasonal water (JRC) + 0.5*low-lying, raised to the observed Sentinel-1 flood
             fraction where --s1-event is given
  lowland    1 - relative elevation inside the district (NASADEM, 2nd-98th percentile)
  slope      NASADEM slope, 0-45 degrees
  rainfall   mean June-September CHIRPS total 2015-2023, fixed range 1000-3500 mm
  landcover  judgement table over ESA WorldCover classes (LANDCOVER_VULN below)
  population log(1+WorldPop 2020 people per cell), scaled to the district's 99th percentile
  inside     1 inside the district boundary, 0 outside
"""
import argparse
import json
import math
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

GRID = 60
LAYERS = ["flood", "lowland", "slope", "rainfall", "landcover", "population"]
# Judgement-based vulnerability of each ESA WorldCover class to flooding/landslides (0 = least, 1 = most).
LANDCOVER_VULN = {10: 0.2, 20: 0.4, 30: 0.5, 40: 0.7, 50: 0.8, 60: 1.0, 70: 0.1, 80: 0.3, 90: 0.6, 95: 0.2, 100: 0.3}
ROAD_FILTER = '["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential"]'
SOURCES = ("NASADEM, JRC Global Surface Water, CHIRPS, ESA WorldCover, WorldPop (Google Earth Engine); "
           "roads and facilities from OpenStreetMap contributors")


# ---------------------------------------------------------------- pure helpers (unit-tested)
def _trim(g, rows, cols):
    """Drop at most one stray edge row/column (a sampling rectangle that touches a cell boundary can return one extra)."""
    if g and rows <= len(g) <= rows + 1 and cols <= len(g[0]) <= cols + 1:
        return [r[:cols] for r in g[:rows]]
    return g


def pack_layers(sample, rows=GRID, cols=GRID):
    """sample: {band: 2D list rows x cols} -> flat 2-decimal lists. Fails loudly on a bad shape or NaN."""
    out = {}
    for k in LAYERS + ["inside"]:
        if k not in sample:
            raise ValueError(f"layer '{k}' missing from the Earth Engine sample")
        g = _trim(sample[k], rows, cols)
        if len(g) != rows or any(len(r) != cols for r in g):
            raise ValueError(f"layer '{k}' has shape {len(g)}x{len(g[0]) if g else 0}, expected {rows}x{cols}")
        flat = [v for r in g for v in r]
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in flat):
            raise ValueError(f"layer '{k}' contains empty cells")
        out[k] = [round(min(1.0, max(0.0, float(v))), 2) for v in flat]
    out["inside"] = [1 if v >= 0.5 else 0 for v in out["inside"]]
    return out


def graph_from_nx(G):
    """networkx (multi)graph with node attrs x (lon), y (lat) and edge attr length (m) -> compact undirected graph."""
    ids = {n: i for i, n in enumerate(G.nodes)}
    nodes = [[round(G.nodes[n]["y"], 5), round(G.nodes[n]["x"], 5)] for n in G.nodes]
    best = {}
    for u, v, d in G.edges(data=True):
        if u == v:
            continue
        key = (min(ids[u], ids[v]), max(ids[u], ids[v]))
        ln = float(d.get("length", 0)) or 1.0
        if key not in best or ln < best[key]:
            best[key] = ln
    return dict(nodes=nodes, edges=[[a, b, round(ln, 1)] for (a, b), ln in sorted(best.items())])


def pois_from_records(records, kind, cap):
    """records: [{name, lat, lon}] -> unique, named-first, capped list of POIs of one kind."""
    seen, out = set(), []
    for r in sorted(records, key=lambda r: (not r.get("name"), r.get("name") or "")):
        key = (round(r["lat"], 4), round(r["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(name=r.get("name") or f"Unnamed {kind}", type=kind, lat=round(r["lat"], 5), lon=round(r["lon"], 5)))
        if len(out) >= cap:
            break
    return out


MIRRORS = ["https://overpass-api.de/api", "https://overpass.private.coffee/api", "https://overpass.kumi.systems/api",
           "https://maps.mail.ru/osm/tools/overpass/api"]


def with_mirrors(settings, fn, mirrors, attempts_per_mirror=2, wait=5, log=print, sleep=None):
    """Run fn() against each Overpass mirror in turn (and retry once each) until one works; fail with every error listed."""
    import time
    sleep = sleep or time.sleep
    attr = "overpass_url" if hasattr(settings, "overpass_url") else "overpass_endpoint"
    errors = []
    for m in mirrors:
        setattr(settings, attr, m)
        for i in range(attempts_per_mirror):
            try:
                return fn()
            except Exception as exc:   # network, timeout, rate limit, server closed the connection
                errors.append(f"{m}: {type(exc).__name__}: {exc}")
                log(f"  {m} failed ({type(exc).__name__}); " + ("retrying" if i + 1 < attempts_per_mirror else "trying the next server"))
                sleep(wait)
    raise SystemExit("OpenStreetMap download failed on every server:\n  " + "\n  ".join(errors) +
                     "\nTry again in a few minutes, or on a different network (phone hotspot).")


def write_package(out_dir, pkg):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{pkg['id']}.json").write_text(json.dumps(pkg, separators=(",", ":")))
    idx_path = out / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"areas": []}
    row = dict(id=pkg["id"], name=pkg["name"], state=pkg["state"], calamities=pkg["calamities"], events=pkg["events"],
               kind=pkg["provenance"]["kind"])
    idx["areas"] = [a for a in idx["areas"] if a["id"] != pkg["id"]] + [row]
    idx["areas"].sort(key=lambda a: (a["kind"] != "real", a["name"]))  # real districts first
    idx_path.write_text(json.dumps(idx, indent=1))


def load_package(out_dir, area_id):
    p = pathlib.Path(out_dir) / f"{area_id}.json"
    return json.loads(p.read_text()) if p.exists() else None


# ---------------------------------------------------------------- Earth Engine stage (run on your machine)
def ee_stage(args):
    import ee
    import flood_pipeline as fp
    fp.init_ee(args.project)
    region = fp.get_region("India", 2, args.gaul_name or args.district, args.state)
    s, w, n, e = _bounds(region)
    dx, dy = (e - w) / GRID, (n - s) / GRID
    rect = ee.Geometry.Rectangle([w + dx / 2, s + dy / 2, e - dx / 2, n - dy / 2], "EPSG:4326", False)   # inset half a cell so exactly GRID x GRID pixels fall inside
    xf = [dx, 0, w, 0, -dy, n]

    dem = ee.Image("NASA/NASADEM_HGT/001").select("elevation")
    p = dem.reduceRegion(ee.Reducer.percentile([2, 98]), region, 90, maxPixels=1e9, bestEffort=True)
    lo, hi = ee.Number(p.get("elevation_p2")), ee.Number(p.get("elevation_p98"))
    lowland = dem.subtract(lo).divide(hi.subtract(lo)).clamp(0, 1).multiply(-1).add(1).rename("lowland")
    slope = ee.Terrain.slope(dem).divide(45).clamp(0, 1).rename("slope")

    occ = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").unmask(0)
    dist_m = occ.gte(10).distance(ee.Kernel.euclidean(5000, "meters"), False).unmask(5000)
    prox = dist_m.multiply(-1 / 1500).exp()
    flood = prox.multiply(0.5).add(lowland.multiply(0.5)).rename("flood")
    if args.s1_event:
        events = json.load(open(ROOT / "pipeline" / "events.json"))
        run = fp.prepare_run(args.s1_event, events[args.s1_event], events[args.s1_event]["name"], no_rf=True)
        obs = run.final.unmask(0).multiply(2.5).clamp(0, 1)   # fraction of the cell flooded, boosted: a flooded 900 m cell is high hazard
        flood = flood.max(obs).rename("flood")

    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
    rain = (chirps.filterDate("2015-06-01", "2024-01-01").filter(ee.Filter.calendarRange(6, 9, "month")).sum()
            .setDefaultProjection(chirps.first().projection())   # a collection reduction has no fixed projection
            .divide(9).subtract(1000).divide(2500).clamp(0, 1).rename("rainfall"))

    wc = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    keys = sorted(LANDCOVER_VULN)
    cover = wc.remap(keys, [LANDCOVER_VULN[k] for k in keys], 0.5).rename("landcover")

    wp = ee.ImageCollection("WorldPop/GP/100m/pop").filter(ee.Filter.eq("country", "IND")).filter(ee.Filter.eq("year", 2020))
    pop = wp.mosaic().setDefaultProjection(wp.first().projection()).unmask(0)
    popg = pop.reduceResolution(ee.Reducer.sum(), bestEffort=True, maxPixels=65536).reproject("EPSG:4326", xf)
    pc = popg.add(1).log()
    top = pc.reduceRegion(ee.Reducer.percentile([99]), region, 1000, maxPixels=1e9, bestEffort=True).values().get(0)
    population = pc.divide(ee.Number(top)).clamp(0, 1).rename("population")

    inside = ee.Image.constant(1).clip(region).unmask(0).rename("inside").reproject("EPSG:4326", xf)

    def on_grid(img):
        return img.reduceResolution(ee.Reducer.mean(), bestEffort=True, maxPixels=65536).reproject("EPSG:4326", xf)

    stack = ee.Image.cat([on_grid(flood), on_grid(lowland), on_grid(slope), on_grid(rain), on_grid(cover),
                          population.reproject("EPSG:4326", xf), inside]).unmask(0)
    print("Sampling the grid from Earth Engine (a few minutes) ...")
    props = stack.sampleRectangle(region=rect, defaultValue=0).getInfo()["properties"]
    layers = pack_layers(props)

    old = load_package(args.out, args.id) or {}
    graph, pois = old.get("graph", {}), old.get("pois", [])
    if old.get("provenance", {}).get("kind") != "real":
        graph, pois = dict(nodes=[], edges=[]), []   # never keep synthetic roads next to real layers
    pkg = dict(id=args.id, name=args.district, state=args.state, bounds=[round(s, 5), round(w, 5), round(n, 5), round(e, 5)],
               rows=GRID, cols=GRID, calamities=args.calamities, events=args.events, layers=layers, graph=graph, pois=pois,
               provenance=dict(kind="real", sources=SOURCES + ("; observed flood from the Sentinel-1 pipeline (" + args.s1_event + ")" if args.s1_event else ""),
                               note="Hazard layers are built from open datasets with judgement-based scaling; not validated against recorded losses."))
    write_package(args.out, pkg)
    print(f"wrote {args.out}/{args.id}.json (layers real; roads {'kept' if graph.get('nodes') else 'EMPTY: run --stage osm next'})")


def _bounds(region):
    ring = region.bounds().getInfo()["coordinates"][0]
    xs, ys = [c[0] for c in ring], [c[1] for c in ring]
    return min(ys), min(xs), max(ys), max(xs)


# ---------------------------------------------------------------- OSM stage (needs a network that reaches Overpass)
def osm_stage(args):
    import osmnx as ox
    pkg = load_package(args.out, args.id)
    if not pkg or pkg["provenance"]["kind"] != "real":
        raise SystemExit(f"Run --stage ee for {args.district} first (no real layers found in {args.out}/{args.id}.json).")
    poly = _district_polygon(args)
    print("Downloading the road network from OpenStreetMap (can take several minutes) ...")
    ox.settings.requests_timeout = 300
    ox.settings.max_query_area_size = 4e8   # smaller pieces: big single queries are what public servers drop

    def download():
        G = ox.graph_from_polygon(poly, custom_filter=ROAD_FILTER, simplify=True, retain_all=False)
        G = ox.truncate.largest_component(G, strongly=False)
        recs = {}
        for kind, tags in (("hospital", {"amenity": "hospital"}),
                           ("shelter", {"amenity": ["shelter", "community_centre", "school"], "emergency": "assembly_point"})):
            gdf = ox.features_from_polygon(poly, tags)
            recs[kind] = [dict(name=_clean_name(r.get("name")), lat=g.representative_point().y, lon=g.representative_point().x)
                          for g, (_, r) in zip(gdf.geometry, gdf.iterrows())]
        gdf = ox.features_from_polygon(poly, {"place": sorted(PLACE_CLASSES)})
        recs["settlement"] = [dict(name=_clean_name(r.get("name")), lat=g.representative_point().y, lon=g.representative_point().x)
                              for g, (_, r) in zip(gdf.geometry, gdf.iterrows())]
        return G, recs

    G, recs = with_mirrors(ox.settings, download, MIRRORS, log=print)
    graph = graph_from_nx(G)
    pois = pois_from_records(recs["hospital"], "hospital", 60) + pois_from_records(recs["shelter"], "shelter", 80)
    if not pois or not graph["edges"]:
        raise SystemExit("OpenStreetMap returned no roads or no facilities for this district; not writing an empty package.")
    pkg["graph"], pkg["pois"] = graph, pois
    pkg["settlements"] = pois_from_records(recs["settlement"], "settlement", 150)
    write_package(args.out, pkg)
    print(f"wrote roads ({len(graph['nodes'])} nodes, {len(graph['edges'])} segments) and {len(pois)} facilities")


MOTORABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
             "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
HOSPITAL_CLASSES = {"hospital"}
SHELTER_CLASSES = {"shelter", "community_centre", "school"}
PLACE_CLASSES = {"city", "town", "village", "suburb", "hamlet"}


def _hav(a, b):
    r = 6371000
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def graph_from_lines(lines):
    """lines: [[(lon, lat), ...], ...] (road centre-lines sharing vertices at junctions) -> compact undirected graph.
    Only line ends and shared vertices become nodes; the stretches between them are merged into one edge.
    Keeps the largest connected piece."""
    import networkx as nx
    lines = [[(round(x, 5), round(y, 5)) for x, y in ln] for ln in lines if len(ln) >= 2]
    seen, key = {}, set()
    for ln in lines:
        key.add(ln[0]); key.add(ln[-1])
        for pt in set(ln):
            seen[pt] = seen.get(pt, 0) + 1
    key |= {pt for pt, c in seen.items() if c > 1}
    G = nx.Graph()
    for ln in lines:
        start, acc = ln[0], 0.0
        for a, b in zip(ln, ln[1:]):
            acc += _hav(a, b)
            if b in key:
                if start != b and acc > 0:
                    if not G.has_edge(start, b) or G[start][b]["length"] > acc:
                        G.add_edge(start, b, length=acc)
                start, acc = b, 0.0
    if not G.number_of_nodes():
        return dict(nodes=[], edges=[])
    comp = max(nx.connected_components(G), key=len)
    G = G.subgraph(comp)
    ids = {n: i for i, n in enumerate(G.nodes)}
    return dict(nodes=[[n[1], n[0]] for n in G.nodes],
                edges=sorted([ids[u], ids[v], round(d["length"], 1)] for u, v, d in G.edges(data=True)))


def records_from_gdf(gdf, classes):
    """Geofabrik layer -> [{name, lat, lon}] for rows whose fclass is in classes (polygons use an interior point)."""
    out = []
    if gdf is None or not len(gdf):
        return out
    for _, r in gdf.iterrows():
        if r.get("fclass") in classes and r.geometry is not None and not r.geometry.is_empty:
            pt = r.geometry.representative_point()
            out.append(dict(name=_clean_name(r.get("name")), lat=pt.y, lon=pt.x))
    return out


def offline_stage(args):
    """Roads, facilities and settlements from downloaded Geofabrik files (gis_osm_*_free_1.shp). No network needed."""
    import geopandas as gpd
    import pandas as pd
    pkg = load_package(args.out, args.id)
    if not pkg or pkg["provenance"]["kind"] != "real":
        raise SystemExit(f"Run --stage ee for {args.district} first (no real layers found in {args.out}/{args.id}.json).")
    s, w, n, e = pkg["bounds"]
    bbox = (w, s, e, n)

    def read(path):
        g = gpd.read_file(path, bbox=bbox)
        return g.set_crs(4326) if g.crs is None else g.to_crs(4326)

    roads = read(args.roads_file)
    if "fclass" not in roads.columns:
        raise SystemExit(f"{args.roads_file} has no 'fclass' column; use Geofabrik's gis_osm_roads_free_1.shp.")
    roads = roads[roads["fclass"].isin(MOTORABLE)]
    lines = []
    for g in roads.geometry:
        if g is None or g.is_empty:
            continue
        for part in (g.geoms if g.geom_type == "MultiLineString" else [g]):
            lines.append(list(part.coords))
    graph = graph_from_lines(lines)
    pois_gdf = pd.concat([read(f) for f in args.pois_file], ignore_index=True) if args.pois_file else None
    pois = (pois_from_records(records_from_gdf(pois_gdf, HOSPITAL_CLASSES), "hospital", 60)
            + pois_from_records(records_from_gdf(pois_gdf, SHELTER_CLASSES), "shelter", 80))
    places = read(args.places_file) if args.places_file else None
    settlements = pois_from_records(records_from_gdf(places, PLACE_CLASSES), "settlement", 150)
    if not graph["edges"] or not pois:
        raise SystemExit("The files gave no motorable roads or no hospitals/shelters inside this district's box. "
                         "Check you downloaded the Kerala extract and passed gis_osm_roads_free_1.shp and the pois files.")
    pkg["graph"], pkg["pois"], pkg["settlements"] = graph, pois, settlements
    write_package(args.out, pkg)
    print(f"wrote roads ({len(graph['nodes'])} nodes, {len(graph['edges'])} segments), {len(pois)} facilities, "
          f"{len(settlements)} settlements (from local files)")


def _clean_name(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) or not str(v).strip() else str(v).strip()


def _district_polygon(args):
    import flood_pipeline as fp
    from shapely.geometry import shape
    fp.init_ee(args.project)
    geom = fp.get_region("India", 2, args.gaul_name or args.district, args.state).simplify(300).getInfo()
    return shape(geom)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--district", required=True, help="GAUL spelling, e.g. Pathanamthitta")
    ap.add_argument("--state", default="Kerala")
    ap.add_argument("--gaul-name", help="spelling in the FAO GAUL boundaries if it differs, e.g. Pattanamtitta for Pathanamthitta")
    ap.add_argument("--project", default=os.environ.get("EARTHENGINE_PROJECT"))
    ap.add_argument("--calamities", nargs="+", choices=["flood", "landslide"], default=["flood"])
    ap.add_argument("--events", default="", help="short text shown under the district name")
    ap.add_argument("--s1-event", help="pipeline/events.json key whose Sentinel-1 flood is blended into the flood layer")
    ap.add_argument("--roads-file", help="OFFLINE: Geofabrik gis_osm_roads_free_1.shp (skips Overpass)")
    ap.add_argument("--pois-file", nargs="+", help="OFFLINE: gis_osm_pois_free_1.shp and optionally gis_osm_pois_a_free_1.shp")
    ap.add_argument("--places-file", help="OFFLINE: gis_osm_places_free_1.shp (settlements)")
    ap.add_argument("--stage", choices=["ee", "osm", "all"], default="all")
    ap.add_argument("--out", default=str(ROOT / "public/data/risk"))
    args = ap.parse_args(argv)
    args.id = args.district.lower().replace(" ", "_")
    if "<" in args.district or "YOUR" in (args.project or "").upper():
        raise SystemExit("Replace the placeholder values with real ones.")
    if args.stage in ("ee", "all"):
        ee_stage(args)
    if args.stage in ("osm", "all"):
        (offline_stage if args.roads_file else osm_stage)(args)


if __name__ == "__main__":
    main()
