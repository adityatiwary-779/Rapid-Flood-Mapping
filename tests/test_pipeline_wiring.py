"""Wiring tests for the REAL Earth Engine code paths, using a stand-in `ee` module.
They catch typos, wrong argument names and file-naming mistakes between pipeline -> driver -> worker.
They do NOT prove Earth Engine semantics: that needs the smoke test / integration check on real EE."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pipeline import flood_pipeline as fp


class FakeTask:
    def __init__(self, kw):
        self.config, self.id, self.started = {"description": kw["description"]}, "T-" + kw["description"], False

    def start(self):
        self.started = True


def fake_ee():
    ee = MagicMock()
    for path in ("image.toDrive", "image.toCloudStorage", "table.toDrive", "table.toCloudStorage", "image.toAsset"):
        obj = ee.batch.Export
        for part in path.split("."):
            obj = getattr(obj, part)
        obj.side_effect = lambda **kw: FakeTask(kw)
    return ee


def build_run(ee):
    ev = {"country": "India", "level": 2, "parent": "Kerala", "pre": ["2018-07-01", "2018-07-30"],
          "post": ["2018-08-15", "2018-08-25"]}
    with patch.object(fp, "ee", ee), patch.object(fp, "get_region", return_value=MagicMock()), \
            patch.object(fp, "choose_pass", return_value=("DESCENDING", (6, 2))), \
            patch.object(fp, "diff_threshold", return_value=(-3.9, -3.9, True)), \
            patch.object(fp, "rf_refine", side_effect=lambda *a, **k: (a[9] if False else MagicMock(), True)):
        return fp.prepare_run("job1", ev, "Alappuzha", log=lambda *_: None)


def test_prepare_run_builds_everything():
    ee = fake_ee()
    run = build_run(ee)
    assert run.tag == "job1_alappuzha" and run.meta["threshold_db"] == -3.9 and run.meta["used_rf"] is True
    assert run.unmasked is not None and run.stats is not None
    cfg = fp.config_report(run)
    assert cfg["masks"]["max_slope_deg"] == 5.0 and cfg["threshold_db"] == -3.9 and cfg["n_post_images"] == 2
    assert "flood_unmasked_km2" in fp.STATS_COLUMNS


def test_gcs_export_names_and_args():
    ee = fake_ee()
    run = build_run(ee)
    dest = {"gcs": {"bucket": "my-bucket", "prefix": "results/job1"}}
    with patch.object(fp, "ee", ee):
        tasks = fp.start_exports(run, dest, vectors=True, log=lambda *_: None)
    assert all(t.started for t in tasks) and len(tasks) == 4
    ee.batch.Export.image.toDrive.assert_not_called()
    img = ee.batch.Export.image.toCloudStorage.call_args_list
    assert [c.kwargs["fileNamePrefix"] for c in img] == ["results/job1/flood", "results/job1/dvv_db"]
    assert all(c.kwargs["bucket"] == "my-bucket" and c.kwargs["crs"] == "EPSG:4326"
               and c.kwargs["fileFormat"] == "GeoTIFF" and c.kwargs["scale"] == 30 for c in img)
    tab = ee.batch.Export.table.toCloudStorage.call_args_list
    assert [c.kwargs["fileNamePrefix"] for c in tab] == ["results/job1/stats", "results/job1/flood_vec"]
    assert tab[0].kwargs["selectors"] == fp.STATS_COLUMNS and tab[0].kwargs["fileFormat"] == "CSV"
    assert tab[1].kwargs["fileFormat"] == "GeoJSON"


def test_drive_export_names_unchanged_for_cli():
    ee = fake_ee()
    run = build_run(ee)
    with patch.object(fp, "ee", ee):
        fp.start_exports(run, {"drive": "flood_outputs"}, vectors=False, log=lambda *_: None)
    prefixes = [c.kwargs["fileNamePrefix"] for c in ee.batch.Export.image.toDrive.call_args_list]
    assert prefixes == ["job1_alappuzha_flood", "job1_alappuzha_dvv_db"]
    assert ee.batch.Export.table.toDrive.call_args.kwargs["folder"] == "flood_outputs"


def test_driver_maps_tasks_previews_and_polls():
    from worker import driver as drv
    ee = fake_ee()
    run = build_run(ee)
    settings = SimpleNamespace(bucket="b")
    urls = {n: f"https://ee/{n}.png" for n in fp.PREVIEW_LAYERS}
    resp = SimpleNamespace(content=b"\x89PNG...", raise_for_status=lambda: None)
    with patch.object(drv.google_auth, "init_ee"), patch.object(fp, "ee", ee), \
            patch.object(fp, "prepare_run", return_value=run), \
            patch.object(fp, "preview_urls", return_value=(urls, [[9.1, 76.2], [9.8, 76.6]])), \
            patch.object(drv.requests, "get", return_value=resp) as get:
        d = drv.EEDriver(settings)
        out = d.start({"region": {"country": "India", "level": 2, "parent": "Kerala", "name": "Alappuzha"},
                       "pre": ["2018-07-01", "2018-07-30"], "post": ["2018-08-15", "2018-08-25"]},
                      "job1", "results/job1")
    assert set(out["task_ids"]) == {"flood", "dvv_db", "stats", "flood_vec"}
    assert out["task_ids"]["flood_vec"] == "T-job1_alappuzha_flood_vec" and out["task_ids"]["flood"] == "T-job1_alappuzha_flood"
    assert set(out["previews"]) == set(fp.PREVIEW_LAYERS) and get.call_count == 5
    assert out["bounds"] == [[9.1, 76.2], [9.8, 76.6]] and out["config"]["masks"]["max_hand_m"] == 15.0

    import ee as real_ee
    fake = [{"id": "T-job1_alappuzha_flood", "state": "COMPLETED"},
            {"id": "T-job1_alappuzha_dvv_db", "state": "RUNNING"},
            {"id": "T-job1_alappuzha_stats", "state": "COMPLETED"},
            {"id": "T-job1_alappuzha_flood_vec", "state": "FAILED", "error_message": "Computation timed out."}]
    with patch.object(real_ee.data, "getTaskStatus", return_value=fake):
        st = d.poll(out["task_ids"])
    assert st["flood_vec"] == {"state": "FAILED", "error": "Computation timed out."}
    assert st["dvv_db"]["state"] == "RUNNING"


def test_preflight_info_parsing():
    ee = MagicMock()
    ee.Dictionary.return_value.getInfo.return_value = {
        "n_regions": 1, "area_km2": 1316.69, "bounds": [[76.27, 9.12], [76.65, 9.12], [76.65, 9.86], [76.27, 9.86], [76.27, 9.12]],
        "DESCENDING_pre": 6, "DESCENDING_post": 2, "ASCENDING_pre": 0, "ASCENDING_post": 0}
    with patch.object(fp, "ee", ee), patch.object(fp, "gaul", return_value=MagicMock()), \
            patch.object(fp, "s1_collection", return_value=MagicMock()):
        info = fp.preflight_info("India", 2, "Alappuzha", "Kerala", ["2018-07-01", "2018-07-30"], ["2018-08-15", "2018-08-25"])
    assert info["bounds"] == [[9.12, 76.27], [9.86, 76.65]]
    assert info["counts"]["DESCENDING"] == {"pre": 6, "post": 2} and info["area_km2"] == 1316.69
    from common import validation
    from common.config import load_settings
    issues, chosen = validation.check_info(info, load_settings())
    assert chosen == "DESCENDING" and issues == []


def test_preview_layers_cover_the_frontend_contract():
    from common import manifest
    assert tuple(fp.PREVIEW_LAYERS) == tuple(manifest.PREVIEW_NAMES)
    ee = fake_ee()
    run = build_run(ee)
    with patch.object(fp, "ee", ee):
        assert set(fp.preview_images(run)) == set(fp.PREVIEW_LAYERS)


def test_api_import_graph_avoids_heavy_geo_libs():
    import subprocess
    import sys
    code = ("import os,sys; os.environ['BACKEND_MODE']='real'; sys.path.insert(0,'.'); "
            "import api.index; bad={'geopandas','osmnx','rasterio','shapely','pandas','numpy'} & set(sys.modules); "
            "print(sorted(bad)); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=__import__("conftest").ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
