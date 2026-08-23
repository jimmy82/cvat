# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Pure coordinate-conversion helpers between three spaces:

  tile-pixel space   -- (x, y) in a single tile's rendered PNG, [0, tile_size]
  raster-pixel space -- (col, row) in the full source raster
  geographic space   -- (x, y) in the raster's CRS, via its affine geotransform

Annotations drawn in CVAT live in tile-pixel space (same as any other CVAT annotation).
These functions convert them into the other two spaces so results can be handed to the
Python processing engine in whichever space it needs, or exported as GeoJSON/shapefile
in the source CRS.
"""

from __future__ import annotations

from affine import Affine

from cvat.apps.geospatial.ingestion import TileSpec


def tile_pixel_to_raster_pixel(tile: TileSpec, x: float, y: float) -> tuple[float, float]:
    return (x + tile.col_off, y + tile.row_off)


def raster_pixel_to_tile_pixel(tile: TileSpec, col: float, row: float) -> tuple[float, float] | None:
    """Inverse of `tile_pixel_to_raster_pixel`. Returns None if the point falls outside
    this tile's pixel window (including its padding area), which callers can use to
    detect, e.g., that an annotation belongs to a neighboring tile instead."""
    x = col - tile.col_off
    y = row - tile.row_off
    if 0 <= x <= tile.tile_size and 0 <= y <= tile.tile_size:
        return (x, y)
    return None


def raster_pixel_to_geo(transform: Affine, col: float, row: float) -> tuple[float, float]:
    x, y = transform * (col, row)
    return (x, y)


def geo_to_raster_pixel(transform: Affine, x: float, y: float) -> tuple[float, float]:
    col, row = (~transform) * (x, y)
    return (col, row)


def tile_pixel_to_geo(transform: Affine, tile: TileSpec, x: float, y: float) -> tuple[float, float]:
    col, row = tile_pixel_to_raster_pixel(tile, x, y)
    return raster_pixel_to_geo(transform, col, row)


def geo_to_tile_pixel(transform: Affine, tile: TileSpec, x: float, y: float) -> tuple[float, float] | None:
    col, row = geo_to_raster_pixel(transform, x, y)
    return raster_pixel_to_tile_pixel(tile, col, row)


def shape_tile_pixels_to_geo(
    transform: Affine, tile: TileSpec, points: list[float]
) -> list[float]:
    """Convert a CVAT-style flat points list `[x0, y0, x1, y1, ...]` (tile-pixel space)
    into a flat `[lon0, lat0, lon1, lat1, ...]` list in the raster's CRS. This is the
    shape used by CVAT's rectangle/polygon/polyline annotation points."""
    if len(points) % 2 != 0:
        raise ValueError("points must be a flat [x0, y0, x1, y1, ...] list")

    geo_points: list[float] = []
    for i in range(0, len(points), 2):
        gx, gy = tile_pixel_to_geo(transform, tile, points[i], points[i + 1])
        geo_points.extend([gx, gy])
    return geo_points
