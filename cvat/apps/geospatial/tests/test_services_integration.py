# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Integration tests that exercise the real Django ORM: persisting RasterSource/RasterTile
rows against an actual Task, and round-tripping GeoTiffTileReader's in-memory tile grid
into those rows. Run via the test-only bootstrap in /tmp/bootstrap_settings.py (see this
package's README for why a hand-rolled bootstrap is used instead of `manage.py test` in
this verification environment).
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS

from cvat.apps.engine.models import Data, Task
from cvat.apps.geospatial.media_extractor import GeoTiffTileReader
from cvat.apps.geospatial.models import RasterSource, RasterTile
from cvat.apps.geospatial.services import persist_raster_metadata, recompute_tile_grid_for_source


def _write_synthetic_geotiff(path, *, width, height, bands=3):
    transform = Affine.translation(499_000, 4_500_000) * Affine.scale(0.5, -0.5)
    row_idx, col_idx = np.indices((height, width))
    data = np.zeros((bands, height, width), dtype=np.uint16)
    for b in range(bands):
        data[b] = ((row_idx + col_idx * 2 + b * 37) % 4096).astype(np.uint16)

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=bands,
        dtype="uint16",
        crs=CRS.from_epsg(32648),
        transform=transform,
    ) as dst:
        dst.write(data)
    return transform


@pytest.mark.django_db
class TestPersistRasterMetadata:
    def _make_task(self) -> Task:
        data = Data.objects.create()
        return Task.objects.create(name="geotiff-task", data=data)

    def test_persists_source_and_one_tile_row_per_tile(self, tmp_path):
        raster_path = tmp_path / "scene.tif"
        transform = _write_synthetic_geotiff(raster_path, width=1200, height=600)
        task = self._make_task()

        reader = GeoTiffTileReader(
            source_paths=[raster_path],
            tile_size=512,
            overlap=0,
            reencode_as_cog=False,
        )

        raster_source = persist_raster_metadata(
            task=task,
            raster_path=reader.raster_path,
            tile_specs=reader.tile_specs,
            tile_size=reader.tile_size,
            overlap=reader.overlap,
            was_reencoded_as_cog=reader.was_reencoded_as_cog,
        )

        assert raster_source.task_id == task.id
        assert raster_source.width == 1200
        assert raster_source.height == 600
        assert raster_source.tile_size == 512
        assert (
            raster_source.transform_a,
            raster_source.transform_e,
        ) == pytest.approx((transform.a, transform.e))

        db_tiles = list(RasterTile.objects.filter(raster_source=raster_source).order_by("frame"))
        assert len(db_tiles) == len(reader.tile_specs) == len(reader)

        # Frame numbering in the DB must exactly match the extractor's frame_range,
        # since that's the join CVAT's ml_processing app uses to map a job's frame
        # index back to a tile's pixel window.
        assert [t.frame for t in db_tiles] == list(reader.frame_range)

        first_tile_row = db_tiles[0]
        first_tile_spec = reader.tile_specs[0]
        assert first_tile_row.col_off == first_tile_spec.col_off
        assert first_tile_row.row_off == first_tile_spec.row_off
        assert first_tile_row.width == first_tile_spec.width
        assert first_tile_row.pad_right == first_tile_spec.pad_right

    def test_recompute_tile_grid_matches_original(self, tmp_path):
        raster_path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(raster_path, width=2048, height=1024)
        task = self._make_task()

        reader = GeoTiffTileReader(
            source_paths=[raster_path], tile_size=512, overlap=64, reencode_as_cog=False
        )
        raster_source = persist_raster_metadata(
            task=task,
            raster_path=reader.raster_path,
            tile_specs=reader.tile_specs,
            tile_size=reader.tile_size,
            overlap=reader.overlap,
            was_reencoded_as_cog=False,
        )

        recomputed = recompute_tile_grid_for_source(raster_source)
        assert len(recomputed) == len(reader.tile_specs)
        assert [t.col_off for t in recomputed] == [t.col_off for t in reader.tile_specs]
        assert [t.row_off for t in recomputed] == [t.row_off for t in reader.tile_specs]

    def test_duplicate_frame_for_same_source_is_rejected(self, tmp_path):
        raster_path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(raster_path, width=256, height=256)
        task = self._make_task()

        reader = GeoTiffTileReader(
            source_paths=[raster_path], tile_size=256, overlap=0, reencode_as_cog=False
        )
        raster_source = persist_raster_metadata(
            task=task,
            raster_path=reader.raster_path,
            tile_specs=reader.tile_specs,
            tile_size=reader.tile_size,
            overlap=reader.overlap,
            was_reencoded_as_cog=False,
        )

        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            RasterTile.objects.create(
                raster_source=raster_source,
                frame=0,
                row=0,
                col=0,
                col_off=0,
                row_off=0,
                width=256,
                height=256,
            )


@pytest.mark.django_db
class TestGeoTiffTileReaderEndToEnd:
    def test_frame_count_matches_tile_grid_and_images_are_valid_pngs(self, tmp_path):
        import io

        from PIL import Image

        raster_path = tmp_path / "scene.tif"
        _write_synthetic_geotiff(raster_path, width=1500, height=1000, bands=3)

        reader = GeoTiffTileReader(
            source_paths=[raster_path], tile_size=512, overlap=32, reencode_as_cog=False
        )

        frames = list(reader)
        assert len(frames) == len(reader.tile_specs)

        for image_bytes, virtual_path in frames:
            image = Image.open(image_bytes)
            assert image.size == (512, 512)
            assert str(virtual_path).endswith(".png")

        reader.close()

    def test_rejects_multiple_source_files(self, tmp_path):
        raster_a = tmp_path / "a.tif"
        raster_b = tmp_path / "b.tif"
        _write_synthetic_geotiff(raster_a, width=64, height=64)
        _write_synthetic_geotiff(raster_b, width=64, height=64)

        with pytest.raises(ValueError):
            GeoTiffTileReader(source_paths=[raster_a, raster_b])
