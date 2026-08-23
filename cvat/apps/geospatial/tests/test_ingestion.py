# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Pure unit tests for the tiling/ingestion primitives, run against a synthetic
georeferenced GeoTIFF generated on the fly with rasterio. No Django settings or
database are needed for anything in this file.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from PIL import Image
from rasterio.crs import CRS

from cvat.apps.geospatial.ingestion import (
    build_tile_grid,
    compute_display_band_stats,
    ensure_cog,
    is_cog,
    is_georeferenced_raster,
    pick_display_bands,
    read_tile_as_png_bytes,
    read_tile_raw,
)


def _write_synthetic_geotiff(
    path: Path, *, width: int, height: int, bands: int = 3, tiled: bool = False
) -> Affine:
    """Write a small georeferenced raster with a deterministic gradient so pixel values
    at known (row, col) positions can be asserted on later."""
    transform = Affine.translation(499_000, 4_500_000) * Affine.scale(0.5, -0.5)

    row_idx, col_idx = np.indices((height, width))
    data = np.zeros((bands, height, width), dtype=np.uint16)
    for b in range(bands):
        data[b] = ((row_idx + col_idx * 2 + b * 37) % 4096).astype(np.uint16)

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": bands,
        "dtype": "uint16",
        "crs": CRS.from_epsg(32648),
        "transform": transform,
    }
    if tiled:
        profile.update(tiled=True, blockxsize=256, blockysize=256)

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        if tiled:
            dst.build_overviews([2, 4], rasterio.enums.Resampling.average)

    return transform


class TestIsGeoreferencedRaster:
    def test_true_for_georeferenced_tiff(self, tmp_path):
        path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(path, width=64, height=64)
        assert is_georeferenced_raster(path) is True

    def test_false_for_plain_tiff(self, tmp_path):
        path = tmp_path / "plain.tif"
        with rasterio.open(
            path, "w", driver="GTiff", height=16, width=16, count=1, dtype="uint8"
        ) as dst:
            dst.write(np.zeros((1, 16, 16), dtype=np.uint8))
        assert is_georeferenced_raster(path) is False

    def test_false_for_missing_file(self, tmp_path):
        assert is_georeferenced_raster(tmp_path / "does-not-exist.tif") is False


class TestBuildTileGrid:
    def test_exact_multiple_no_overlap(self):
        tiles = build_tile_grid(raster_width=2048, raster_height=1024, tile_size=512, overlap=0)
        # 4 columns x 2 rows
        assert len(tiles) == 8
        assert {t.col for t in tiles} == {0, 1, 2, 3}
        assert {t.row for t in tiles} == {0, 1}
        # No padding needed anywhere since it divides evenly
        assert all(t.pad_right == 0 and t.pad_bottom == 0 for t in tiles)
        # Frame indices are row-major and contiguous
        assert [t.index for t in tiles] == list(range(8))

    def test_edge_tiles_are_padded(self):
        tiles = build_tile_grid(raster_width=1200, raster_height=600, tile_size=512, overlap=0)
        # ceil(1200/512) = 3 cols, ceil(600/512) = 2 rows
        assert {t.col for t in tiles} == {0, 1, 2}
        assert {t.row for t in tiles} == {0, 1}
        last_col_tiles = [t for t in tiles if t.col == 2]
        assert all(t.width == 1200 - 2 * 512 for t in last_col_tiles)
        assert all(t.pad_right == 512 - t.width for t in last_col_tiles)

    def test_overlap_shrinks_stride_not_tile_size(self):
        tiles = build_tile_grid(raster_width=2048, raster_height=512, tile_size=512, overlap=64)
        col_offs = sorted({t.col_off for t in tiles})
        # stride = tile_size - overlap = 448
        assert col_offs[1] - col_offs[0] == 448
        # every tile before the last column should still be full width (512) since
        # overlap only changes *spacing*, not the amount of data read into a tile
        non_edge = [t for t in tiles if t.col_off + t.tile_size <= 2048]
        assert all(t.width == 512 for t in non_edge)

    def test_grid_is_deterministic_and_row_major(self):
        tiles = build_tile_grid(raster_width=1500, raster_height=1500, tile_size=512, overlap=32)
        # index must increase left-to-right, then top-to-bottom
        ordered = sorted(tiles, key=lambda t: t.index)
        for i in range(1, len(ordered)):
            prev, cur = ordered[i - 1], ordered[i]
            assert (cur.row, cur.col) > (prev.row, prev.col)

    @pytest.mark.parametrize("tile_size,overlap", [(0, 0), (-10, 0), (100, 100), (100, 150)])
    def test_rejects_invalid_tile_size_or_overlap(self, tile_size, overlap):
        with pytest.raises(ValueError):
            build_tile_grid(raster_width=1000, raster_height=1000, tile_size=tile_size, overlap=overlap)

    def test_single_tile_covers_small_raster(self):
        tiles = build_tile_grid(raster_width=200, raster_height=150, tile_size=512, overlap=64)
        assert len(tiles) == 1
        t = tiles[0]
        assert t.width == 200 and t.height == 150
        assert t.pad_right == 312 and t.pad_bottom == 362


class TestCogHandling:
    def test_is_cog_false_for_untiled_geotiff(self, tmp_path):
        path = tmp_path / "untiled.tif"
        _write_synthetic_geotiff(path, width=64, height=64, tiled=False)
        assert is_cog(path) is False

    def test_is_cog_true_for_tiled_geotiff_with_overviews(self, tmp_path):
        path = tmp_path / "tiled.tif"
        _write_synthetic_geotiff(path, width=1024, height=1024, tiled=True)
        assert is_cog(path) is True

    def test_ensure_cog_returns_source_path_when_already_cog(self, tmp_path):
        path = tmp_path / "tiled.tif"
        _write_synthetic_geotiff(path, width=1024, height=1024, tiled=True)
        dst = tmp_path / "reencoded.tif"
        result = ensure_cog(path, dst)
        assert result == path
        assert not dst.exists()

    def test_ensure_cog_reencodes_untiled_geotiff(self, tmp_path):
        path = tmp_path / "untiled.tif"
        _write_synthetic_geotiff(path, width=1024, height=1024, tiled=False)
        dst = tmp_path / "reencoded.tif"
        result = ensure_cog(path, dst)
        assert result == dst
        assert dst.exists()
        assert is_cog(dst) is True

        # Pixel values must survive the re-encode untouched.
        with rasterio.open(path) as src, rasterio.open(dst) as reencoded:
            np.testing.assert_array_equal(src.read(), reencoded.read())


class TestTileReading:
    def test_read_tile_as_png_matches_source_pixels(self, tmp_path):
        path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(path, width=256, height=256, bands=3)

        tiles = build_tile_grid(raster_width=256, raster_height=256, tile_size=128, overlap=0)
        assert len(tiles) == 4

        with rasterio.open(path) as dataset:
            bands = pick_display_bands(dataset)
            assert bands == [1, 2, 3]

            for tile in tiles:
                png_bytes = read_tile_as_png_bytes(dataset, tile, band_indices=bands)
                image = Image.open(io.BytesIO(png_bytes))
                assert image.size == (tile.tile_size, tile.tile_size)
                assert image.mode == "RGB"

                # No padding in this exact-multiple case, so every pixel should be real data.
                assert tile.pad_right == 0 and tile.pad_bottom == 0

    def test_read_tile_pads_edge_tiles_to_uniform_size(self, tmp_path):
        path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(path, width=300, height=200, bands=3)

        tiles = build_tile_grid(raster_width=300, raster_height=200, tile_size=256, overlap=0)
        with rasterio.open(path) as dataset:
            for tile in tiles:
                png_bytes = read_tile_as_png_bytes(dataset, tile)
                image = Image.open(io.BytesIO(png_bytes))
                # Every tile is the same on-disk size regardless of how much of it is
                # real raster data vs. padding -- this is what keeps zoom/pan uniform.
                assert image.size == (256, 256)

    def test_read_tile_raw_preserves_dtype_and_band_count(self, tmp_path):
        path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(path, width=128, height=128, bands=4)

        tiles = build_tile_grid(raster_width=128, raster_height=128, tile_size=128, overlap=0)
        with rasterio.open(path) as dataset:
            raw = read_tile_raw(dataset, tiles[0])
            assert raw.dtype == np.uint16
            assert raw.shape == (4, 128, 128)

    def test_display_band_stats_are_consistent_across_tiles(self, tmp_path):
        path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(path, width=512, height=512, bands=3)
        tiles = build_tile_grid(raster_width=512, raster_height=512, tile_size=256, overlap=0)

        with rasterio.open(path) as dataset:
            bands = pick_display_bands(dataset)
            stats = compute_display_band_stats(dataset, bands)
            assert len(stats) == 3
            for lo, hi in stats:
                assert hi > lo

            # Using shared stats shouldn't blow up or reject any tile.
            for tile in tiles:
                png_bytes = read_tile_as_png_bytes(dataset, tile, band_indices=bands, band_stats=stats)
                assert Image.open(io.BytesIO(png_bytes)).size == (256, 256)
