# Sentinel-1 Rapid Flood Mapping – how to run

> **Status:** code reviewed and hardened; roads script and Otsu logic tested offline. The Earth Engine
> part has **not yet been run end to end** – start with `./smoke_test.sh YOUR_PROJECT` (one district).
> See `REVIEW.md` for the defect list and method flags.

> **Known limitation (must appear in any demo):** the Random Forest is trained on labels produced by the
> threshold step, so it is **not independent validation**. The RF/rule "agreement" is not accuracy. Compare
> results with a Copernicus EMS map or news-reported flooded areas and say so on screen.

## 0. Datasets: nothing to download
Everything is read directly inside Google Earth Engine (GEE):

| Purpose | GEE asset ID |
|---|---|
| Radar images (core data) | `COPERNICUS/S1_GRD` |
| Permanent water mask | `JRC/GSW1_4/GlobalSurfaceWater` |
| Slope | `USGS/SRTMGL1_003` |
| Height above nearest drainage (HAND) | `MERIT/Hydro/v1_0_1` (band `hnd`) |
| Cropland / built-up | `ESA/WorldCover/v200` |
| Population | `WorldPop/GP/100m/pop` |
| State / district boundaries | `FAO/GAUL/2015/level1`, `level2` |

Only roads/settlements come from OpenStreetMap, and `roads_exposure.py` downloads them automatically (osmnx).

## 1. One-time setup
1. Sign up at https://earthengine.google.com and register a **Cloud project** (free for non-commercial use). Note the project ID.
2. Install Python 3.10+, then:
   ```
   pip install -r requirements.txt
   earthengine authenticate
   ```

## 2. See valid state / district names
```
python flood_pipeline.py --project YOUR_PROJECT --list-regions --country India --level 1
python flood_pipeline.py --project YOUR_PROJECT --list-regions --country India --level 2 --parent Kerala
```
(`--project` can be omitted if `EARTHENGINE_PROJECT` is set. Use `"parent": "<state>"` in an event to avoid districts that share a name.)
(Names follow GAUL 2015, e.g. Orissa, Uttaranchal. Copy them exactly into `events.json`.)

## 3. Define flood events in `events.json`
- `pre`: 2–4 weeks of dry conditions BEFORE the flood
- `post`: dates when flood water was at peak (Sentinel-1 revisits every ~6–12 days, so use 7–14 day windows)
- `name`: one region, a list of regions, or `"ALL"` (every state/district of that country)
- `level`: 1 = state, 2 = district

## 4. Run the model
```
# first test: one small district, evaluate stats now, no exports
python flood_pipeline.py --project YOUR_PROJECT --event kerala_2018_alappuzha --print-stats --no-export
# then real exports, waiting for tasks and printing any server-side error
python flood_pipeline.py --project YOUR_PROJECT --event kerala_2018_alappuzha --vectors --wait

# one event
python flood_pipeline.py --project YOUR_PROJECT --event kerala_2018

# every event in the file
python flood_pipeline.py --project YOUR_PROJECT --all

# all states for one event: set "name": "ALL" in the event, then run it
# extras:
#   --vectors        also export flood polygons (needed for roads_exposure.py)
#   --asset-root projects/YOUR_PROJECT/assets/flood   also save result as a GEE asset
#   --no-rf          skip Random Forest, use threshold method only
#   --print-stats    print stats in terminal (small regions only)
#   --no-export      build + evaluate only, start no export tasks
#   --wait           poll export tasks and print failures
```
Jobs run on Google's servers. Track them at https://code.earthengine.google.com/tasks (a state can take 10–60 min).

## 5. Outputs (appear in your Google Drive folder `flood_outputs`)
- `*_flood.tif` – binary flood map (1 = flooded)
- `*_dvv_db.tif` – backscatter change in dB (severity proxy, more negative = stronger change)
- `*_stats.csv` – flood km², cropland km², built-up km², tree cover km², population exposed, threshold used, image counts
- `*_flood_vec.geojson` – flood polygons (with `--vectors`)

## 6. Roads and settlements exposure (run per district)
```
python flood_pipeline.py --project YOUR_PROJECT --event chennai_2023 --vectors
# download the GeoJSON from Drive, then:
python roads_exposure.py --flood chennai_2023_chennai_flood_vec.geojson --place "Chennai, India" --out-dir results/chennai
# optional: --motorable-only  --settlement-buffer-m 250
```
Gives flooded road km by road type, affected settlements, and GeoJSONs for your map.

## 7. Use in your website
Load the GeoTIFFs, GeoJSONs and CSVs into your dashboard (or use the GEE Asset if you build a GEE App).

## How the model works
1. Choose the satellite pass (ascending/descending) with images in both windows
2. Median composites + 50 m median smoothing (speckle reduction)
3. dVV = post − pre; the threshold comes from Otsu, clamped to [-6, -2.5] dB
4. Flood = big drop AND post VV below -15 dB AND pre VV not already water
5. Remove permanent water (JRC ≥ 80%), slope > 5°, HAND > 15 m, patches under 10 pixels
6. Random Forest (100 trees) trained on high-confidence pixels from step 4 adds terrain and both polarizations, then the same cleanup (skipped automatically if either class has too few samples)

## Tuning (in `events.json`, add `"cfg": {...}` to an event)
- Missing flood in wet areas: raise `max_hand_m`, loosen `diff_clamp` (e.g. `[-5, -2]`)
- Too much noise: raise `min_patch_pixels`, lower `max_slope_deg`
- Different water darkness: change `post_vv_db` (-16 to -14)

## Honest limitations (mention in your presentation)
- **Random Forest labels come from the threshold method (circular, not independent validation),** so the "agreement" (`rule_rf_overlap_km2` vs `flood_km2`) is not true accuracy. For real validation, compare with Copernicus EMS maps, news-reported areas, or hand-drawn sample points.
- Radar under-detects flooding in dense cities (buildings) and under thick vegetation.
- Wind-roughened water and radar shadow can cause errors; the terrain masks reduce them.
- Population exposure is an estimate from WorldPop (100 m), not a census count.
