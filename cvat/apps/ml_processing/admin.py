# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Registered mainly so `ProcessingEngineConfig` rows can be created through Django admin
(/admin/) rather than requiring a `manage.py shell` session -- there's no dedicated
CVAT UI for configuring a processing engine yet (see INTEGRATION.md).
"""

from django.contrib import admin

from .models import MLProcessingRequest, ProcessingEngineConfig


@admin.register(ProcessingEngineConfig)
class ProcessingEngineConfigAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "task", "base_url", "enabled", "timeout_seconds")
    list_filter = ("enabled",)
    search_fields = ("base_url",)


@admin.register(MLProcessingRequest)
class MLProcessingRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "status", "created_date", "updated_date")
    list_filter = ("status",)
    readonly_fields = (
        "id",
        "job",
        "status",
        "submitted_by",
        "engine_job_id",
        "error_message",
        "result_summary",
        "created_date",
        "updated_date",
    )
