# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Django-facing orchestration that bridges the pure tiling primitives in `ingestion.py`
to CVAT's database. Kept separate from `ingestion.py` so the tiling math stays fully
unit-testable without Django installed/configured.
"""

from __future__ import annotations

from pathlib import Path

import rasterio

from cvat.apps.engine.models import Task
from cvat.apps.geospatial.ingestion import (
    TileSpec,
    build_tile_grid,
    pick_display_bands,
    resolve_georeferencing,
)
from cvat.apps.geospatial.models import RasterSource, RasterTile


def persist_raster_metadata(
    *,
    task: Task,
    raster_path: Path,
    tile_specs: list[TileSpec],
    tile_size: int,
    overlap: int,
    was_reencoded_as_cog: bool,
) -> RasterSource:
    """Persist a `RasterSource` + one `RasterTile` per tile for a Task that was ingested
    through `GeoTiffTileReader`. Called once, right after the extractor has finished
    building the tile grid for a Task (see the `geospatial` hook in
    `cvat.apps.engine.task`), reusing the exact same `tile_specs` the extractor computed
    rather than recomputing the grid, so the two can never drift apart.
    """
    with rasterio.open(raster_path) as dataset:
        georeferencing = resolve_georeferencing(dataset)
        if georeferencing is None:
            raise ValueError(
                f"{raster_path} has no georeferencing (checked for both a direct "
                "affine transform and GCPs) -- persist_raster_metadata should only be "
                "called for a raster GeoTiffTileReader accepted, which implies "
                "is_georeferenced_raster(raster_path) was already true."
            )
        transform, crs = georeferencing
        crs_wkt = crs.to_wkt() if crs else ""
        width, height = dataset.width, dataset.height
        band_count = dataset.count
        dtype = dataset.dtypes[0] if dataset.dtypes else ""
        nodata_value = dataset.nodata
        display_bands = pick_display_bands(dataset)

    raster_source = RasterSource.objects.create(
        task=task,
        source_path=str(raster_path),
        crs_wkt=crs_wkt,
        transform_a=transform.a,
        transform_b=transform.b,
        transform_c=transform.c,
        transform_d=transform.d,
        transform_e=transform.e,
        transform_f=transform.f,
        width=width,
        height=height,
        band_count=band_count,
        dtype=dtype,
        nodata_value=nodata_value,
        tile_size=tile_size,
        overlap=overlap,
        was_reencoded_as_cog=was_reencoded_as_cog,
        display_bands=display_bands,
    )

    RasterTile.objects.bulk_create(
        RasterTile(
            raster_source=raster_source,
            frame=tile.index,
            row=tile.row,
            col=tile.col,
            col_off=tile.col_off,
            row_off=tile.row_off,
            width=tile.width,
            height=tile.height,
            pad_right=tile.pad_right,
            pad_bottom=tile.pad_bottom,
        )
        for tile in tile_specs
    )

    return raster_source


def recompute_tile_grid_for_source(raster_source: RasterSource) -> list[TileSpec]:
    """Rebuild the in-memory tile grid for an already-persisted `RasterSource`, e.g. for
    tools that need `TileSpec` objects (coordinate transforms, re-reading a tile) but
    only have the DB row, not the original extractor instance."""
    return build_tile_grid(
        raster_width=raster_source.width,
        raster_height=raster_source.height,
        tile_size=raster_source.tile_size,
        overlap=raster_source.overlap,
    )
