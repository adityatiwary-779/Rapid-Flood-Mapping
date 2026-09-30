"""Input validation. Issues are dicts: {level: "error"|"warning", code, message}."""
import json
import os
from datetime import date, timedelta

S1_START = date(2014, 10, 3)   # first Sentinel-1 GRD scene in Earth Engine


def issue(level, code, message):
    return {"level": level, "code": code, "message": message}


def has_errors(issues):
    return any(i["level"] == "error" for i in issues)


def check_windows(pre, post, s, today=None):
    """pre/post = [start, end] ISO strings."""
    today = today or date.today()
    issues = []
    try:
        pre_s, pre_e = (date.fromisoformat(x) for x in pre)
        post_s, post_e = (date.fromisoformat(x) for x in post)
    except (ValueError, TypeError):
        return [issue("error", "bad_date", "Dates must be valid, in YYYY-MM-DD format.")]
    for label, key, a, b, lo, hi in (("Pre-flood", "pre", pre_s, pre_e, s.pre_min_days, s.pre_max_days),
                                     ("Post-flood", "post", post_s, post_e, s.post_min_days, s.post_max_days)):
        if b < a:
            issues.append(issue("error", f"{key}_order",
                                f"{label} window ends before it starts."))
            continue
        days = (b - a).days + 1
        if not lo <= days <= hi:
            issues.append(issue("error", f"{key}_length",
                                f"{label} window is {days} days; it must be {lo}-{hi} days."))
        if a < S1_START:
            issues.append(issue("error", f"{key}_too_early",
                                f"{label} window starts before Sentinel-1 data exists (2014-10-03)."))
    if has_errors(issues):
        return issues
    if post_s <= pre_e:
        issues.append(issue("error", "windows_overlap",
                            "The pre-flood and post-flood windows overlap. The post-flood window "
                            "must start after the pre-flood window ends."))
    elif (post_s - pre_e).days > s.max_gap_days:
        issues.append(issue("error", "gap_too_long",
                            f"The windows are more than {s.max_gap_days} days apart."))
    if post_e > today - timedelta(days=s.data_lag_days):
        issues.append(issue("error", "post_in_future",
                            f"Sentinel-1 scenes from the last {s.data_lag_days} days may not be "
                            "available yet. Choose an earlier post-flood window."))
    return issues


def best_pass(counts):
    """Same rule as the pipeline: the pass with the most images in the thinner window."""
    best, score = None, 0
    for p in ("DESCENDING", "ASCENDING"):
        m = min(counts[p]["pre"], counts[p]["post"])
        if m > score:
            best, score = p, m
    return best


def check_info(info, s):
    """info = flood_pipeline.preflight_info() output. Returns (issues, chosen_pass)."""
    issues = []
    if info["n_regions"] == 0:
        return [issue("error", "region_not_found", "That region was not found.")], None
    if info["n_regions"] > 1:
        issues.append(issue("error", "region_ambiguous",
                            "That name matches more than one district. Choose district + state."))
    if info["area_km2"] > s.max_area_km2:
        issues.append(issue("error", "region_too_large",
                            f"This district is {info['area_km2']:.0f} km², above the "
                            f"{s.max_area_km2} km² limit. Flood mapping and road analysis run "
                            "one district at a time; choose a smaller district."))
    chosen = best_pass(info["counts"])
    c = info["counts"]
    summary = ", ".join(f"{p.lower()}: {c[p]['pre']} pre / {c[p]['post']} post" for p in c)
    if chosen is None:
        issues.append(issue("error", "no_images",
                            f"No Sentinel-1 orbit pass has images in both windows ({summary}). "
                            "Widen the windows or choose different dates."))
    else:
        n_pre, n_post = c[chosen]["pre"], c[chosen]["post"]
        low = [n for n, v in (("pre-flood", n_pre), ("post-flood", n_post)) if v < s.min_images_warn]
        if low:
            issues.append(issue("warning", "few_images",
                                f"Only {n_pre} pre-flood and {n_post} post-flood image(s) on the "
                                f"{chosen.lower()} pass. The {' and '.join(low)} composite rests on "
                                "very few scenes, so results are less reliable."))
    return issues, chosen


def load_regions(path=None):
    """data/regions.json: {"India": {"2": [{"parent","name","area_km2"}]}} (built by tools/build_regions.py)."""
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "regions.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def region_listed(regions, country, level, parent, name):
    rows = regions.get(country, {}).get(str(level), [])
    return any(r["name"] == name and r.get("parent") == parent for r in rows)
