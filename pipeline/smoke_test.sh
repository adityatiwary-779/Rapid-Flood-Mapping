#!/usr/bin/env bash
# One-district smoke test. Usage: ./smoke_test.sh YOUR_PROJECT
# Every failure prints the Earth Engine error message; paste it back for diagnosis.
set -euo pipefail
P="${1:?usage: ./smoke_test.sh YOUR_EE_PROJECT}"
export FLOOD_DEBUG=1
echo "== 1. valid Kerala district names (check 'Alappuzha' is spelled exactly)"
python pipeline/flood_pipeline.py --project "$P" --list-regions --level 2 --parent Kerala
echo "== 2. graph + stats evaluated NOW (no export); RF sample counts and Otsu are printed"
python pipeline/flood_pipeline.py --project "$P" --event kerala_2018_alappuzha --print-stats --no-export
echo "== 3. real exports, wait for tasks and surface server-side errors"
python pipeline/flood_pipeline.py --project "$P" --event kerala_2018_alappuzha --vectors --wait
echo "== 4. now download the *_flood_vec.geojson from Drive/flood_outputs and run:"
echo "   python roads_exposure.py --flood kerala_2018_alappuzha_flood_vec.geojson --place 'Alappuzha, Kerala, India' --out-dir results/alappuzha"
