# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from django.apps import AppConfig


class MlProcessingConfig(AppConfig):
    name = "cvat.apps.ml_processing"
    verbose_name = "ML Processing"
    default_auto_field = "django.db.models.BigAutoField"
