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
from rasterio.crs import CRS
from rasterio.warp import transform as warp_transform

from cvat.apps.engine.models import Task
from cvat.apps.geospatial.ingestion import (
    TileSpec,
    build_tile_grid,
    needs_rpc_georeferencing,
    pick_display_bands,
    resolve_georeferencing,
)
from cvat.apps.geospatial.models import GeoreferencingKind, RasterSource, RasterTile
from cvat.apps.geospatial.rpc import rpc_forward, rpc_inverse
from cvat.apps.geospatial.transforms import (
    geo_to_tile_pixel,
    raster_pixel_to_tile_pixel,
    tile_pixel_to_geo,
    tile_pixel_to_raster_pixel,
)

WGS84 = CRS.from_epsg(4326)


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
        if needs_rpc_georeferencing(dataset):
            georeferencing_kind = GeoreferencingKind.RPC
            rpc_coefficients = dataset.rpcs.to_dict()
            transform_fields = dict.fromkeys(
                ("transform_a", "transform_b", "transform_c", "transform_d", "transform_e", "transform_f"),
                None,
            )
            # RPC ground coordinates are always WGS84 by the RPC00B specification --
            # see GeoreferencingKind's docstring -- so there's no real CRS to record.
            crs_wkt = ""
        else:
            georeferencing = resolve_georeferencing(dataset)
            if georeferencing is None:
                raise ValueError(
                    f"{raster_path} has no georeferencing (checked for a direct affine "
                    "transform, GCPs, and RPCs) -- persist_raster_metadata should only "
                    "be called for a raster GeoTiffTileReader accepted, which implies "
                    "is_georeferenced_raster(raster_path) was already true."
                )
            transform, crs = georeferencing
            georeferencing_kind = GeoreferencingKind.AFFINE
            rpc_coefficients = None
            crs_wkt = crs.to_wkt() if crs else ""
            transform_fields = dict(
                transform_a=transform.a,
                transform_b=transform.b,
                transform_c=transform.c,
                transform_d=transform.d,
                transform_e=transform.e,
                transform_f=transform.f,
            )

        width, height = dataset.width, dataset.height
        band_count = dataset.count
        dtype = dataset.dtypes[0] if dataset.dtypes else ""
        nodata_value = dataset.nodata
        display_bands = pick_display_bands(dataset)

    raster_source = RasterSource.objects.create(
        task=task,
        source_path=str(raster_path),
        crs_wkt=crs_wkt,
        georeferencing_kind=georeferencing_kind,
        rpc_coefficients=rpc_coefficients,
        width=width,
        height=height,
        band_count=band_count,
        dtype=dtype,
        nodata_value=nodata_value,
        tile_size=tile_size,
        overlap=overlap,
        was_reencoded_as_cog=was_reencoded_as_cog,
        display_bands=display_bands,
        **transform_fields,
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


def pixel_pairs_to_wgs84(
    raster_source: RasterSource, tile_spec, pixel_pairs: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Tile-pixel (x, y) pairs -> WGS84 (lon, lat) pairs, dispatching on
    `raster_source.georeferencing_kind` -- callers (GeoJSON export, the live cursor
    status bar, the ruler tool) don't need to know or care whether the underlying
    raster is georeferenced via a direct/GCP-fitted affine transform or RPCs.
    """
    if raster_source.georeferencing_kind == GeoreferencingKind.RPC:
        rpc = raster_source.rpc
        geo_pairs = []
        for x, y in pixel_pairs:
            raster_col, raster_row = tile_pixel_to_raster_pixel(tile_spec, x, y)
            geo_pairs.append(rpc_inverse(rpc, raster_col, raster_row))
        return geo_pairs

    native_pairs = [tile_pixel_to_geo(raster_source.affine, tile_spec, x, y) for x, y in pixel_pairs]
    xs, ys = zip(*native_pairs)
    src_crs = CRS.from_wkt(raster_source.crs_wkt)
    lons, lats = warp_transform(src_crs, WGS84, xs, ys)
    return list(zip(lons, lats))


def wgs84_pairs_to_tile_pixel(
    raster_source: RasterSource, tile_spec, geo_pairs: list[tuple[float, float]]
) -> list[tuple[float, float]] | None:
    """WGS84 (lon, lat) pairs -> tile-pixel (x, y) pairs, or `None` if any point falls
    outside this tile's window. Inverse of `pixel_pairs_to_wgs84`, same dispatch."""
    if raster_source.georeferencing_kind == GeoreferencingKind.RPC:
        rpc = raster_source.rpc
        pixel_pairs = []
        for lon, lat in geo_pairs:
            raster_col, raster_row = rpc_forward(rpc, lon, lat)
            tile_pixel = raster_pixel_to_tile_pixel(tile_spec, raster_col, raster_row)
            if tile_pixel is None:
                return None
            pixel_pairs.append(tile_pixel)
        return pixel_pairs

    src_crs = CRS.from_wkt(raster_source.crs_wkt)
    lons, lats = zip(*geo_pairs)
    xs, ys = warp_transform(WGS84, src_crs, lons, lats)
    pixel_pairs = [geo_to_tile_pixel(raster_source.affine, tile_spec, x, y) for x, y in zip(xs, ys)]
    if any(pixel is None for pixel in pixel_pairs):
        return None
    return pixel_pairs
