# Review, test status and method flags

## Test status (read this first)

| Check | Status |
|---|---|
| Syntax / pyflakes | pass |
| `roads_exposure.py` end to end, OSM mocked (`tests/test_offline.py`) | pass |
| Otsu split logic (NumPy mirror) | pass |
| **`flood_pipeline.py` against real Earth Engine** | **NOT RUN.** The review sandbox had no EE credentials (`earthengine authenticate` needs the owner's Google login). Every fix below is from static review plus the EE catalog/API docs, not from an observed runtime error. Run `./smoke_test.sh YOUR_PROJECT` and send back any error text. |

## Architecture

`events.json` -> `flood_pipeline.py` (all EE server-side; only `getInfo()` for pass choice, Otsu
value, RF sample counts, optional stats) -> Drive exports (flood GeoTIFF, dVV GeoTIFF, stats CSV,
flood GeoJSON) -> `roads_exposure.py` (local, geopandas + OSM via osmnx) -> roads/settlements.

## Defects found and fixed

| # | Where | Problem (expected error) | Fix |
|---|---|---|---|
| 1 | `compute_stats` | `ee.Image("ESA/WorldCover/v200")`: v200 is an **ImageCollection**. Expect `Image.load: Asset 'ESA/WorldCover/v200' is not an Image.` The stats task would fail only in the Tasks tab. | `ee.ImageCollection(...).first().select("Map")` |
| 2 | `otsu` | Split loop runs to `size`; the last split has an empty class B (0/0 = NaN BSS). A NaN sort key can win `sort(...).get([-1])`. Also a `List` passed where `Array.sort` wants an `Array`. Reproduced in `tests/test_offline.py`. | Loop `1..size-1`, sort only `means[0:size-1]`, wrap keys in `ee.Array` |
| 3 | `diff_threshold` | Bare `except Exception` turned real errors (e.g. empty collection -> "Image has no bands") into a silent fallback to -2.5 dB. | Catch only `ee.EEException/ValueError/TypeError`; return `otsu_ok` and write it to the stats CSV; `run_region` now raises if a window has 0 images |
| 4 | `diff_threshold` | Histogram at `scale=100` but the map is built at 30 m. `focalMedian(50 m)` is evaluated per request scale, so the smoothing (and thus the dVV distribution) differs. | Histogram at `export_scale` (`bestEffort` still coarsens very large regions) |
| 5 | `rf_refine` | If one class has no samples (little/no flood), `train` fails **server-side after tasks are started**, visible only in the Tasks tab. Sampling scale was hard-coded 30. | `aggregate_histogram("label")` pre-check; falls back to the rule-based map and records `used_rf=false` |
| 6 | exports | Masked dVV pixels are written as `0` (= "no change") with no nodata flag. Flood image was unmasked to 0 and unclipped. | dVV `unmask(-9999)`; flood `.clip(region)`; explicit `fileFormat`, `crs` on the asset export; `reduceToVectors` gets `crs` + `geometryType` |
| 7 | CLI | `--project` was `required=True`, so `python flood_pipeline.py --list-regions` (as in your run steps) died with an argparse error. | `--project` optional, falls back to `EARTHENGINE_PROJECT` / `GOOGLE_CLOUD_PROJECT` |
| 8 | `init_ee` | Bare `except` started the browser auth flow on *any* error (wrong project, API not enabled). | Authenticate only for credential errors; otherwise print the real message |
| 9 | `get_region` | GAUL district names repeat across states (e.g. several "Aurangabad"). Geometry would be a union of several. Unknown names gave no hint. | Optional `"parent"` (state) key in events; `--parent` for `--list-regions`; "did you mean" suggestions |
| 10 | `main` | Failures only printed a one-liner; no way to see server-side task errors without opening the web UI. | `--wait` polls tasks and prints `error_message`; `--no-export --print-stats` evaluates stats without exports; `FLOOD_DEBUG=1` prints tracebacks; nonzero exit if any region failed |
| 11 | `roads_exposure.py` | Empty flood file -> `estimate_utm_crs` traceback. OSM place that geocodes to a *point* -> `features_from_place` raises. Full `intersects(giant_union)` is slow. Empty result frames could not be written. | Clean exit on empty input; falls back to the flood bounding box; spatial-index query; empty GeoJSON written safely |

`events.json`: added `kerala_2018_alappuzha` (one district) for the smoke test. The original
`kerala_2018` event is a **whole state**, not a small district, so it should not be the first run.
Exact GAUL spelling of "Alappuzha" (and the Chennai-event districts) could not be verified offline;
use `--list-regions --level 2 --parent Kerala`.

## Scientific / method flags (not changed unless noted)

1. **Random Forest is circular (the main limitation).** Training labels come from the threshold rule,
   and the features include `dVV` and `VV_post`, the very variables that define those labels. The RF
   can reproduce the rule near-perfectly, so `rule_rf_overlap_km2 / flood_km2` measures agreement, not
   accuracy. Terrain features add little because every sample already passed the same terrain mask
   (`valid`). Treat the RF as a smoother of the rule-based map. To make it less circular, drop `dVV`/`VV_post`
   from the features or train on independent labels (Copernicus EMS, hand-digitised points).
2. **Otsu on a whole-region histogram is weak.** Flood is a few % of pixels, so the histogram is nearly
   unimodal and Otsu mostly lands outside the clamp, i.e. the clamp (-6 to -2.5 dB) decides the
   threshold. Option added: `"cfg": {"otsu_candidates_only": true}` builds the histogram only from valid,
   dark (post VV < -15 dB) pixels, where it is closer to bimodal. Default left unchanged; compare both.
3. **Masking order.** Terrain and JRC masks are applied after thresholding, which is fine for the rule map, but
   the Otsu histogram (default) includes the pixels that are later masked out. HAND < 15 m and slope < 5 deg can
   also remove real flooding along steep-sided valleys; JRC >= 80 % removes rivers that widened during the flood
   only where they were already mostly water (flooded river *expansion* is kept, which is desired).
4. **"Pre" is not dry for Kerala 2018.** Jul 1-31 is monsoon (and Idukki/Wayanad already flooded mid-July),
   so permanent-plus-early-flood water is in the baseline and the true extent is under-estimated. Use late
   May to early June, or check S1 image dates.
5. **Composites mix orbits/incidence angles** across a state (median of all scenes in the pass). Edge/border
   noise and shadow can produce dark false positives. Consider per-relative-orbit comparison for states.
6. **Population.** Sampling the 100 m WorldPop cell centre against a 30 m flood mask is noisy and biased
   (all-or-nothing per cell). Better: flood *fraction* per cell, or population density x flooded area.
7. **Detection limits.** Dense urban areas (double bounce raises backscatter) and flooded vegetation are
   under- or mis-detected by a "dark water" rule. Built-up exposure is therefore a lower bound.
8. **Roads/settlements.** All OSM `highway=*` lines count by default (footpaths included); use `--motorable-only`.
   A village centroid is rarely inside a 30 m flood polygon, so "affected settlements" is a strict lower bound;
   `--settlement-buffer-m 250` is a more useful demo number. OSM completeness varies by district.
9. **Single scene pair, no accuracy assessment.** No uncertainty, no confusion matrix. Validate before publishing.
