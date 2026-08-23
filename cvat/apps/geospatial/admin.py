# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""Read-only admin registration, mainly useful for inspecting/debugging a task's
tiling metadata (e.g. "why does this task have 340 frames?") without a DB shell."""

from django.contrib import admin

from .models import RasterSource, RasterTaskConfig, RasterTile


class RasterTileInline(admin.TabularInline):
    model = RasterTile
    extra = 0
    can_delete = False
    readonly_fields = (
        "frame",
        "row",
        "col",
        "col_off",
        "row_off",
        "width",
        "height",
        "pad_right",
        "pad_bottom",
    )
    max_num = 0  # never show an "add another" row; this data is generated, not hand-entered


@admin.register(RasterSource)
class RasterSourceAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "width", "height", "band_count", "tile_size", "overlap")
    readonly_fields = [f.name for f in RasterSource._meta.fields]
    inlines = [RasterTileInline]


@admin.register(RasterTaskConfig)
class RasterTaskConfigAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "tile_size", "overlap", "reencode_as_cog")
