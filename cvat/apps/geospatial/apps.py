# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from django.apps import AppConfig


class GeospatialConfig(AppConfig):
    name = "cvat.apps.geospatial"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Registering the GeoTIFF media type here (rather than at import time in
        # media_extractors.py) keeps the registration import-order-safe: engine.apps
        # is guaranteed to be ready before geospatial.apps, since geospatial is listed
        # after engine in INSTALLED_APPS.
        from cvat.apps.geospatial.media_extractor import register_geotiff_media_type

        register_geotiff_media_type()
