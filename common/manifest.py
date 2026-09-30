"""manifest.json: the single description of a finished run that the frontend reads (presets and custom runs alike)."""
import csv
import io

CAVEAT = ("The Random Forest is trained on labels produced by the threshold step, so it is NOT "
          "independent validation. Compare these results with a Copernicus EMS map or "
          "news-reported flooded areas before relying on them.")

PREVIEW_NAMES = ("pre_vv", "post_vv", "flood", "flood_rule", "flood_unmasked", "severity")
OPTIONAL_FILES = ("flood.tif", "dvv_db.tif", "stats.csv", "flood_vec.geojson", "flooded_roads.geojson",
                  "affected_settlements.geojson", "affected_settlements.csv",
                  "roads_flooded_by_type.csv", "config.json")


def parse_stats_csv(data):
    row = next(csv.DictReader(io.StringIO(data.decode() if isinstance(data, bytes) else data)))
    out = {}
    for k, v in row.items():
        if v in ("", None):
            out[k] = None
        elif v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = float(v) if any(c in v for c in ".eE") and k not in ("event", "region") else int(v)
            except ValueError:
                out[k] = v
    return out


def build(job_id, kind, title, place, spec, stats, config, roads, bounds, previews, files,
          warnings=None, mock=False):
    stats = dict(stats)
    fk, cr, bu, tr = (stats.get(k) or 0 for k in
                      ("flood_km2", "cropland_km2", "builtup_km2", "tree_cover_km2"))
    stats["other_km2"] = max(0.0, fk - cr - bu - tr)   # WorldCover water/wetland/grass/shrub/bare...
    return {"id": job_id, "kind": kind, "title": title, "place": place, "mock": mock,
            "windows": {"pre": spec["pre"], "post": spec["post"]},
            "bounds": bounds, "stats": stats, "config": config, "roads": roads,
            "previews": previews, "files": files, "warnings": warnings or [], "caveat": CAVEAT}


def resolve(manifest, storage):
    """Turn stored bucket paths into browser URLs at request time (signed URLs expire, so never store them)."""
    m = dict(manifest)
    m["files"] = {k: storage.url(v) for k, v in manifest["files"].items()}
    m["previews"] = {k: storage.url(v) for k, v in manifest["previews"].items()}
    return m
