"""Wording of the static-demo notice, shared by make_real_previews.py and add_roads_to_demo.py so either can run last."""

NOTE_NO_ROADS = ("Static demo: the radar images, flood layers and figures were rendered by Earth Engine from the pipeline for "
                 "this event. Road analysis, settlements and GeoTIFF downloads are not included in the static demo.")


def note_with_roads(buffer_m=250):
    return ("Static demo: the radar images, flood layers and figures were rendered by Earth Engine from the pipeline for "
            "this event. Roads and settlements come from OpenStreetMap (motorable roads only; a settlement counts as "
            f"affected if its centre is within {buffer_m} m of flood water). GeoTIFF downloads are not included in the static demo.")
