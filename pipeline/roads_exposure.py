#!/usr/bin/env python3
"""
Road + settlement exposure from flood polygons (OpenStreetMap via osmnx).

Usage:
  python roads_exposure.py --flood kerala_2018_alappuzha_flood_vec.geojson \
      --place "Alappuzha district, Kerala, India" --out-dir results

Tip: run per district (not whole state) - OSM downloads for big areas are slow.
If --place is omitted (or cannot be geocoded to a polygon), the bounding box of the
flood polygons is used as the OSM query area instead.

If this computer cannot reach OpenStreetMap (firewall, VPN, college network), the script STOPS with a clear
message and writes no results. Options: another network, --overpass-url, or a downloaded extract via
--roads-file / --places-file (Geofabrik shapefiles or HDX/HOT exports work).

Settlements are points/centroids; a flood polygon rarely covers a village centre exactly,
so use --settlement-buffer-m (e.g. 250) to count settlements within that distance of water.
"""
import argparse
import json
import os

import geopandas as gpd
import osmnx as ox
import pandas as pd
from shapely.geometry import box

# highway values that are not motorable roads (excluded with --motorable-only)
NON_MOTORABLE = {"footway", "path", "steps", "pedestrian", "cycleway", "bridleway",
                 "corridor", "track", "proposed", "construction", "elevator", "platform"}
PLACE_TYPES = ["city", "town", "village", "hamlet", "suburb"]
OVERPASS_MIRRORS = ["https://overpass-api.de/api", "https://overpass.kumi.systems/api",
                    "https://overpass.private.coffee/api"]


class OSMUnavailable(RuntimeError):
    """OpenStreetMap could not be reached (different from: reached, but nothing is mapped here)."""


def union(gdf):
    return gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union


def _empty():
    return gpd.GeoDataFrame(geometry=[], crs=4326)


def _no_data_error():
    try:
        from osmnx._errors import InsufficientResponseError
        return InsufficientResponseError
    except Exception:  # noqa: BLE001
        return ()                                  # `except ()` catches nothing


def _set_overpass(url):
    for attr in ("overpass_url", "overpass_endpoint"):   # osmnx 2.x / 1.x
        if hasattr(ox.settings, attr):
            setattr(ox.settings, attr, url)


def _query(place, poly_4326, tags):
    nodata = _no_data_error()
    if place:
        try:
            return ox.features_from_place(place, tags=tags)
        except nodata:
            return _empty()                        # reached OSM: nothing mapped there
        except Exception as e:  # noqa: BLE001 - geocode returned no polygon / geocoder blocked / API error
            print(f"   ! could not use place '{place}' ({type(e).__name__}); using the flood bounding box instead")
    try:
        return ox.features_from_polygon(poly_4326, tags=tags)
    except nodata:
        return _empty()
    except Exception as e:  # noqa: BLE001
        raise OSMUnavailable(f"{type(e).__name__}: {str(e)[:150]}") from e


def fetch(place, poly_4326, tags, mirrors=None):
    """OSM features for a place name (or the flood bbox), trying mirror servers. Raises OSMUnavailable if none works."""
    errors = []
    for url in (mirrors or OVERPASS_MIRRORS):
        _set_overpass(url)
        try:
            return _query(place, poly_4326, tags)
        except OSMUnavailable as e:
            errors.append(f"{url}: {e}")
            print(f"   ! OpenStreetMap server {url} is not usable from here; trying the next one")
    raise OSMUnavailable("Could not reach any OpenStreetMap server:\n  " + "\n  ".join(errors))


def load_local(path, bounds, kind):
    """Read a downloaded OSM extract (Geofabrik .shp, HDX/HOT .gpkg/.geojson) limited to the flood area."""
    g = gpd.read_file(path, bbox=tuple(bounds))
    g = g.set_crs(4326) if g.crs is None else g.to_crs(4326)
    col = "highway" if kind == "roads" else "place"
    if col not in g.columns and "fclass" in g.columns:          # Geofabrik naming
        g = g.rename(columns={"fclass": col})
    if col not in g.columns:
        raise ValueError(f"{path}: no '{col}' (or 'fclass') column. Columns: {list(g.columns)[:12]}")
    if "name" not in g.columns:
        g["name"] = None
    if kind == "places":
        g = g[g[col].isin(PLACE_TYPES)]
    return g


def write_geojson(gdf, path):
    if len(gdf) == 0:  # some drivers refuse to write empty frames
        with open(path, "w") as f:
            json.dump({"type": "FeatureCollection", "features": []}, f)
    else:
        gdf.to_crs(4326).reset_index().to_file(path, driver="GeoJSON")


def compute_exposure(flood_path, place=None, out_dir="results", motorable_only=False,
                     settlement_buffer_m=0.0, roads_file=None, places_file=None, overpass_urls=None):
    """Flooded road km + affected settlements. Writes files to out_dir, returns a summary dict.
    Raises OSMUnavailable (and writes nothing) if OpenStreetMap cannot be reached."""
    flood = gpd.read_file(flood_path)
    flood = flood[flood.geometry.notnull() & ~flood.geometry.is_empty]
    if len(flood) == 0:
        raise ValueError(f"{flood_path} contains no flood polygons - nothing to intersect.")
    if flood.crs is None:
        flood = flood.set_crs(4326)
    utm = flood.estimate_utm_crs()
    fu = union(flood.to_crs(utm))
    # query area used when place geocoding fails (flood bbox, padded ~1 km)
    minx, miny, maxx, maxy = flood.to_crs(4326).total_bounds
    bounds = (minx - 0.01, miny - 0.01, maxx + 0.01, maxy + 0.01)
    query_poly = box(*bounds)

    # ---- get ALL data first, so a network failure leaves no half-written results
    if roads_file:
        roads = load_local(roads_file, bounds, "roads")
    else:
        roads = fetch(place, query_poly, {"highway": True}, overpass_urls)
    if places_file:
        places = load_local(places_file, bounds, "places")
    else:
        places = fetch(place, query_poly, {"place": PLACE_TYPES}, overpass_urls)
    os.makedirs(out_dir, exist_ok=True)

    # ---- roads
    roads = roads[roads.geom_type.isin(["LineString", "MultiLineString"])] if len(roads) else roads
    if len(roads):
        roads = roads.reindex(columns=["highway", "name", "geometry"]).to_crs(utm)
        roads["highway"] = roads["highway"].astype(str)
        if motorable_only:
            roads = roads[~roads["highway"].isin(NON_MOTORABLE)]
    else:
        roads = gpd.GeoDataFrame({"highway": [], "name": []}, geometry=[], crs=utm)
    hit = roads.iloc[roads.sindex.query(fu, predicate="intersects")].copy()
    hit["flooded_km"] = hit.geometry.intersection(fu).length / 1000
    total_km = roads.length.sum() / 1000
    by_type = (hit.groupby("highway")["flooded_km"].sum().sort_values(ascending=False)
               .reset_index())
    by_type.to_csv(os.path.join(out_dir, "roads_flooded_by_type.csv"), index=False)
    write_geojson(hit, os.path.join(out_dir, "flooded_roads.geojson"))

    # ---- settlements
    if len(places):
        places = places.reindex(columns=["place", "name", "geometry"]).to_crs(utm)
        places["geometry"] = places.geometry.centroid
    else:
        places = gpd.GeoDataFrame({"place": [], "name": []}, geometry=[], crs=utm)
    target = fu.buffer(settlement_buffer_m) if settlement_buffer_m > 0 else fu
    aff = places.iloc[places.sindex.query(target, predicate="intersects")]
    write_geojson(aff, os.path.join(out_dir, "affected_settlements.geojson"))
    aff.drop(columns="geometry").to_csv(
        os.path.join(out_dir, "affected_settlements.csv"), index=False)

    summary = {
        "place": place or "flood bbox",
        "road_km_total": round(float(total_km), 1),
        "road_km_flooded": round(float(hit["flooded_km"].sum()), 1),
        "settlements_total": int(len(places)),
        "settlements_affected": int(len(aff))}
    pd.DataFrame([summary]).to_csv(os.path.join(out_dir, "exposure_summary.csv"), index=False)
    return {**summary,
            "source": "local file" if (roads_file or places_file) else "OpenStreetMap",
            "by_type": [{"highway": r.highway, "flooded_km": round(float(r.flooded_km), 2)}
                        for r in by_type.itertuples()],
            "settlements": [{"name": (None if pd.isna(r.name) else str(r.name)),
                             "place": (None if pd.isna(r.place) else str(r.place))}
                            for r in aff.itertuples()]}


UNREACHABLE_HELP = """
OpenStreetMap could not be reached from this computer, so NO road results were produced (nothing was written).
Things to try:
  1. A different network (for example a phone hotspot), or turn your VPN off/on. College and office networks
     and some antivirus programs block overpass-api.de.
  2. Open https://overpass-api.de/api/status in your browser. If that fails too, it is your network.
  3. Use another Overpass server:  --overpass-url https://your.mirror/api
  4. Work offline with a downloaded extract (Geofabrik shapefiles or an HDX/HOT export for your state):
       --roads-file path\\to\\roads.shp  --places-file path\\to\\places.shp
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flood", required=True, help="flood polygons GeoJSON from Earth Engine")
    ap.add_argument("--place", default=None, help='OSM place name, e.g. "Chennai, India"')
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--motorable-only", action="store_true",
                    help="drop footpaths/tracks/steps etc. from road totals")
    ap.add_argument("--settlement-buffer-m", type=float, default=0.0,
                    help="count a settlement as affected if within this distance of flood water")
    ap.add_argument("--overpass-url", default=None, help="try this Overpass server first, e.g. https://host/api")
    ap.add_argument("--roads-file", default=None, help="downloaded roads layer instead of querying OSM")
    ap.add_argument("--places-file", default=None, help="downloaded places layer instead of querying OSM")
    args = ap.parse_args()
    mirrors = ([args.overpass_url] if args.overpass_url else []) + OVERPASS_MIRRORS
    try:
        res = compute_exposure(args.flood, args.place, args.out_dir, args.motorable_only,
                               args.settlement_buffer_m, args.roads_file, args.places_file, mirrors)
    except OSMUnavailable as e:
        raise SystemExit(f"{e}\n{UNREACHABLE_HELP}")
    except ValueError as e:
        raise SystemExit(str(e))
    print(pd.DataFrame([{k: v for k, v in res.items()
                         if k not in ("by_type", "settlements")}]).to_string(index=False))


if __name__ == "__main__":
    main()
