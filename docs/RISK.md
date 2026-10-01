# Risk and Routes (multi-hazard decision support)

Page: `public/risk/index.html`. Static, no backend: all risk and routing maths runs in the browser (`public/risk/engine.js`).

## Method
1. **Indicators** (each scaled 0 to 1): flood susceptibility, low-lying land, slope, rainfall, land-cover vulnerability. Population is the exposure layer.
2. **Hazard** = weighted mean of the indicators. Weights come from a calamity preset (flood / landslide / combined) and can be changed live with sliders; they are rescaled to sum to 1.
3. **Risk** = hazard x (0.25 + 0.75 x population index). The 0.25 floor keeps empty land from being scored zero (roads and land are exposed too).
4. **High-risk zones** = top N% of cells inside the district (slider, default 10%).
5. **Routing**: road graph from OpenStreetMap, node risk sampled from the risk grid, edge cost = length x (1 + alpha x mean node risk). Dijkstra gives the least-risk route; alpha = 0 gives the shortest route. Both are shown with distance, average and maximum risk, and distance through high-risk zones.

## Limitations (also shown on the page)
- Weights are judgement-based, not fitted; the index is not validated against recorded losses.
- ~900 m grid: district-scale pattern, not street-scale.
- Static layers: no live rainfall, no live road closures or damage.
- Flood layer = water proximity + low elevation, blended with the Sentinel-1 flood map where the pipeline was run for that district. The Sentinel-1 Random Forest is trained on threshold labels, so it is not independent validation.
- Facilities are whatever OpenStreetMap contains (hospitals; schools, community centres, shelters as possible relief points). Not an official relief-camp list.

## Data packages
`public/data/risk/<district>.json` plus `index.json`. Districts marked `synthetic` in the file are placeholders and show a banner on the page.

```
python tools/make_synthetic_risk_package.py              # placeholders for development
python tools/build_risk_package.py --project <id> --district Pathanamthitta --calamities flood landslide \
    --events "Floods 2018; landslides in the eastern hills" --s1-event kerala_2018_pathanamthitta --stage ee
python tools/build_risk_package.py --project <id> --district Pathanamthitta --stage osm   # needs a network that reaches OpenStreetMap
```
