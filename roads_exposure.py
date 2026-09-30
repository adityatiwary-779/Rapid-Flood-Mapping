#!/usr/bin/env python3
"""
Road + settlement exposure from flood polygons (OpenStreetMap via osmnx).

Usage:
  python roads_exposure.py --flood kerala_2018_kerala_flood_vec.geojson \
      --place "Kerala, India" --out-dir results

Tip: run per district (not whole state) - OSM downloads for big areas are slow.
"""
import argparse
import os

import geopandas as gpd
import osmnx as ox
import pandas as pd


def union(gdf):
    return gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flood", required=True, help="flood polygons GeoJSON from Earth Engine")
    ap.add_argument("--place", required=True, help='OSM place name, e.g. "Chennai, India"')
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    flood = gpd.read_file(args.flood)
    flood = flood[flood.geometry.notnull()]
    if flood.crs is None:
        flood = flood.set_crs(4326)
    utm = flood.estimate_utm_crs()
    flood_u = flood.to_crs(utm)
    fu = union(flood_u)

    # ---- roads
    roads = ox.features_from_place(args.place, tags={"highway": True})
    roads = roads[roads.geom_type.isin(["LineString", "MultiLineString"])]
    roads = roads.reindex(columns=["highway", "name", "geometry"]).to_crs(utm)
    roads["highway"] = roads["highway"].astype(str)
    hit = roads[roads.intersects(fu)].copy()
    hit["flooded_km"] = hit.geometry.intersection(fu).length / 1000
    total_km = roads.length.sum() / 1000
    by_type = (hit.groupby("highway")["flooded_km"].sum().sort_values(ascending=False)
               .reset_index())
    by_type.to_csv(os.path.join(args.out_dir, "roads_flooded_by_type.csv"), index=False)
    hit.to_crs(4326).to_file(os.path.join(args.out_dir, "flooded_roads.geojson"), driver="GeoJSON")

    # ---- settlements
    places = ox.features_from_place(
        args.place, tags={"place": ["city", "town", "village", "hamlet", "suburb"]})
    places = places.reindex(columns=["place", "name", "geometry"]).to_crs(utm)
    places["geometry"] = places.geometry.centroid
    aff = places[places.within(fu)]
    aff.to_crs(4326).to_file(os.path.join(args.out_dir, "affected_settlements.geojson"),
                             driver="GeoJSON")
    aff.drop(columns="geometry").to_csv(
        os.path.join(args.out_dir, "affected_settlements.csv"), index=False)

    summary = pd.DataFrame([{
        "place": args.place,
        "road_km_total": round(total_km, 1),
        "road_km_flooded": round(hit["flooded_km"].sum(), 1),
        "settlements_total": len(places),
        "settlements_affected": len(aff)}])
    summary.to_csv(os.path.join(args.out_dir, "exposure_summary.csv"), index=False)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
