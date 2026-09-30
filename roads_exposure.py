#!/usr/bin/env python3
"""
Road + settlement exposure from flood polygons (OpenStreetMap via osmnx).

Usage:
  python roads_exposure.py --flood kerala_2018_alappuzha_flood_vec.geojson \
      --place "Alappuzha district, Kerala, India" --out-dir results

Tip: run per district (not whole state) - OSM downloads for big areas are slow.
If --place is omitted (or cannot be geocoded to a polygon), the bounding box of the
flood polygons is used as the OSM query area instead.

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


def union(gdf):
    return gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union


def fetch(place, poly_4326, tags):
    """OSM features for a place name, falling back to the flood bbox polygon."""
    if place:
        try:
            return ox.features_from_place(place, tags=tags)
        except Exception as e:  # geocode returned no polygon / nothing found / API error
            print(f"   ! OSM query for '{place}' failed ({type(e).__name__}: {e}); "
                  "using flood bounding box instead")
    try:
        return ox.features_from_polygon(poly_4326, tags=tags)
    except Exception as e:
        print(f"   ! OSM query returned nothing ({type(e).__name__}: {e})")
        return gpd.GeoDataFrame(geometry=[], crs=4326)


def write_geojson(gdf, path):
    if len(gdf) == 0:  # some drivers refuse to write empty frames
        with open(path, "w") as f:
            json.dump({"type": "FeatureCollection", "features": []}, f)
    else:
        gdf.to_crs(4326).reset_index().to_file(path, driver="GeoJSON")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flood", required=True, help="flood polygons GeoJSON from Earth Engine")
    ap.add_argument("--place", default=None, help='OSM place name, e.g. "Chennai, India"')
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--motorable-only", action="store_true",
                    help="drop footpaths/tracks/steps etc. from road totals")
    ap.add_argument("--settlement-buffer-m", type=float, default=0.0,
                    help="count a settlement as affected if within this distance of flood water")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    flood = gpd.read_file(args.flood)
    flood = flood[flood.geometry.notnull() & ~flood.geometry.is_empty]
    if len(flood) == 0:
        raise SystemExit(f"{args.flood} contains no flood polygons - nothing to intersect.")
    if flood.crs is None:
        flood = flood.set_crs(4326)
    utm = flood.estimate_utm_crs()
    fu = union(flood.to_crs(utm))
    # OSM query polygon used when place geocoding fails (flood bbox, padded ~1 km)
    minx, miny, maxx, maxy = flood.to_crs(4326).total_bounds
    query_poly = box(minx - 0.01, miny - 0.01, maxx + 0.01, maxy + 0.01)

    # ---- roads
    roads = fetch(args.place, query_poly, {"highway": True})
    roads = roads[roads.geom_type.isin(["LineString", "MultiLineString"])] if len(roads) else roads
    if len(roads):
        roads = roads.reindex(columns=["highway", "name", "geometry"]).to_crs(utm)
        roads["highway"] = roads["highway"].astype(str)
        if args.motorable_only:
            roads = roads[~roads["highway"].isin(NON_MOTORABLE)]
    else:
        roads = gpd.GeoDataFrame({"highway": [], "name": []}, geometry=[], crs=utm)
    hit = roads.iloc[roads.sindex.query(fu, predicate="intersects")].copy()
    hit["flooded_km"] = hit.geometry.intersection(fu).length / 1000
    total_km = roads.length.sum() / 1000
    by_type = (hit.groupby("highway")["flooded_km"].sum().sort_values(ascending=False)
               .reset_index())
    by_type.to_csv(os.path.join(args.out_dir, "roads_flooded_by_type.csv"), index=False)
    write_geojson(hit, os.path.join(args.out_dir, "flooded_roads.geojson"))

    # ---- settlements
    places = fetch(args.place, query_poly,
                   {"place": ["city", "town", "village", "hamlet", "suburb"]})
    if len(places):
        places = places.reindex(columns=["place", "name", "geometry"]).to_crs(utm)
        places["geometry"] = places.geometry.centroid
    else:
        places = gpd.GeoDataFrame({"place": [], "name": []}, geometry=[], crs=utm)
    target = fu.buffer(args.settlement_buffer_m) if args.settlement_buffer_m > 0 else fu
    aff = places.iloc[places.sindex.query(target, predicate="intersects")]
    write_geojson(aff, os.path.join(args.out_dir, "affected_settlements.geojson"))
    aff.drop(columns="geometry").to_csv(
        os.path.join(args.out_dir, "affected_settlements.csv"), index=False)

    summary = pd.DataFrame([{
        "place": args.place or "flood bbox",
        "road_km_total": round(total_km, 1),
        "road_km_flooded": round(hit["flooded_km"].sum(), 1),
        "settlements_total": len(places),
        "settlements_affected": len(aff)}])
    summary.to_csv(os.path.join(args.out_dir, "exposure_summary.csv"), index=False)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
