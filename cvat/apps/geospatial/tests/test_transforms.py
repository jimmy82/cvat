# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import math

import pytest
from affine import Affine

from cvat.apps.geospatial.ingestion import TileSpec
from cvat.apps.geospatial.transforms import (
    geo_to_raster_pixel,
    geo_to_tile_pixel,
    raster_pixel_to_geo,
    raster_pixel_to_tile_pixel,
    shape_tile_pixels_to_geo,
    tile_pixel_to_geo,
    tile_pixel_to_raster_pixel,
)


@pytest.fixture
def transform() -> Affine:
    # 0.5m pixels, origin at (499000, 4500000), north-up (negative y scale)
    return Affine.translation(499_000, 4_500_000) * Affine.scale(0.5, -0.5)


@pytest.fixture
def tile() -> TileSpec:
    return TileSpec(
        index=5,
        row=1,
        col=2,
        col_off=2048,
        row_off=1024,
        width=1024,
        height=1024,
        pad_right=0,
        pad_bottom=0,
        tile_size=1024,
    )


class TestTilePixelToRasterPixel:
    def test_origin_maps_to_tile_offset(self, tile):
        assert tile_pixel_to_raster_pixel(tile, 0, 0) == (2048, 1024)

    def test_arbitrary_point(self, tile):
        assert tile_pixel_to_raster_pixel(tile, 10, 20) == (2058, 1044)

    def test_inverse_recovers_original_point(self, tile):
        col, row = tile_pixel_to_raster_pixel(tile, 300, 400)
        assert raster_pixel_to_tile_pixel(tile, col, row) == (300, 400)

    def test_point_outside_tile_returns_none(self, tile):
        # Well outside this tile's window (belongs to a different tile).
        assert raster_pixel_to_tile_pixel(tile, 0, 0) is None

    def test_point_within_padding_margin_is_still_valid(self, tile):
        # tile_size is the *rendered* size; a point exactly on the far edge is valid.
        assert raster_pixel_to_tile_pixel(tile, tile.col_off + tile.tile_size, tile.row_off) == (
            tile.tile_size,
            0,
        )


class TestGeoConversion:
    def test_raster_origin_maps_to_transform_translation(self, transform):
        x, y = raster_pixel_to_geo(transform, 0, 0)
        assert (x, y) == (499_000, 4_500_000)

    def test_round_trip_raster_pixel_geo(self, transform):
        col, row = 1234.5, 987.25
        x, y = raster_pixel_to_geo(transform, col, row)
        col2, row2 = geo_to_raster_pixel(transform, x, y)
        assert math.isclose(col, col2, abs_tol=1e-6)
        assert math.isclose(row, row2, abs_tol=1e-6)

    def test_y_decreases_as_row_increases_for_north_up_raster(self, transform):
        _, y0 = raster_pixel_to_geo(transform, 0, 0)
        _, y1 = raster_pixel_to_geo(transform, 0, 100)
        assert y1 < y0


class TestTilePixelToGeoRoundTrip:
    def test_round_trip_through_full_chain(self, transform, tile):
        x, y = 512.0, 256.0
        gx, gy = tile_pixel_to_geo(transform, tile, x, y)

        recovered = geo_to_tile_pixel(transform, tile, gx, gy)
        assert recovered is not None
        rx, ry = recovered
        assert math.isclose(rx, x, abs_tol=1e-6)
        assert math.isclose(ry, y, abs_tol=1e-6)

    def test_point_outside_tile_round_trips_to_none(self, transform, tile):
        # Origin of the whole raster is well outside this tile.
        assert geo_to_tile_pixel(transform, tile, 499_000, 4_500_000) is None


class TestShapeConversion:
    def test_flat_points_list_length_must_be_even(self, transform, tile):
        with pytest.raises(ValueError):
            shape_tile_pixels_to_geo(transform, tile, [1.0, 2.0, 3.0])

    def test_converts_rectangle_points(self, transform, tile):
        # A rectangle as CVAT would store it: [x1, y1, x2, y2]
        points = [10.0, 20.0, 500.0, 300.0]
        geo_points = shape_tile_pixels_to_geo(transform, tile, points)
        assert len(geo_points) == 4

        # Cross-check the first vertex against the single-point helper.
        expected = tile_pixel_to_geo(transform, tile, 10.0, 20.0)
        assert geo_points[0] == expected[0]
        assert geo_points[1] == expected[1]
