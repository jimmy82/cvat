# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Registers a new CVAT media type, "geotiff", so that uploading a georeferenced TIFF
routes through `GeoTiffTileReader` instead of CVAT's ordinary PIL-based `ImageListReader`.

`GeoTiffTileReader` subclasses `ImageListReader` and "unrolls" a single GeoTIFF file
into N synthetic frames -- one PNG per tile -- so every downstream piece of CVAT that
already knows how to split a sequence of image frames into segments/jobs, cache chunks,
and serve them to the browser keeps working completely unmodified. This is the same
trick `ArchiveReader`/`DirectoryReader` already use to turn "one archive" or "one
directory" into "many frames"; we're doing the same thing for "one big raster".

Why a new media type instead of teaching `ImageListReader`/PIL to read GeoTIFFs
directly: PIL decodes a whole image into memory before CVAT ever gets to choose how to
present it, which is exactly what makes very large TIFFs fail today (see
https://github.com/cvat-ai/cvat/issues/531 and
https://github.com/cvat-ai/cvat/issues/2205). rasterio/GDAL's windowed reads let us
pull out only the bytes for one tile at a time.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import rasterio

from cvat.apps.engine.media_extractors import ImageListReader, IMediaReader
from cvat.apps.engine.models import DimensionType, SortingMethod, TaskMode
from cvat.apps.geospatial.ingestion import (
    DEFAULT_OVERLAP,
    DEFAULT_TILE_SIZE,
    TileSpec,
    build_tile_grid,
    compute_display_band_stats,
    ensure_cog,
    is_cog,
    is_georeferenced_raster,
    pick_display_bands,
    read_tile_as_png_bytes,
)

GEOTIFF_EXTENSIONS = (".tif", ".tiff")


def _is_geotiff(path) -> bool:
    """Media-type detector used by CVAT's `MEDIA_TYPES` registry (see `get_mime`).

    Deliberately conservative: if the extension doesn't match, or the file can't be
    opened yet (e.g. a cloud-storage listing that hasn't been downloaded locally at
    detection time), this returns False and the file falls back to CVAT's ordinary
    "image" handling -- a plain (non-georeferenced) TIFF, or a GeoTIFF whose
    georeferencing can't be checked yet, still works as an ordinary single-frame image;
    it just won't get tiled.
    """
    lower = str(path).lower()
    if not lower.endswith(GEOTIFF_EXTENSIONS):
        return False

    try:
        return is_georeferenced_raster(Path(path))
    except Exception:
        return False


class GeoTiffTileReader(ImageListReader):
    """`IImageReader` that presents the tiles of a single georeferenced raster as a
    sequence of CVAT frames, in row-major order (see `build_tile_grid`).

    Constructed with the same keyword arguments CVAT's task-creation code already
    passes to every other extractor (`source_paths`, `step`, `start`, `stop`,
    `sorting_method`) -- see `MEDIA_TYPES[media_type]["extractor"](**details)` in
    `cvat.apps.engine.task`. `tile_size`/`overlap`/`reencode_as_cog` are CVAT-specific
    extras threaded through from the task creation request when `media_type ==
    "geotiff"` (see the integration notes in this package's README for the exact call
    site).
    """

    def __init__(
        self,
        source_paths: list[Path],
        step: int = 1,
        start: int = 0,
        stop: int | None = None,
        dimension: DimensionType = DimensionType.DIM_2D,
        sorting_method: SortingMethod = SortingMethod.LEXICOGRAPHICAL,
        *,
        tile_size: int = DEFAULT_TILE_SIZE,
        overlap: int = DEFAULT_OVERLAP,
        reencode_as_cog: bool = True,
    ):
        # Set first so `__del__` never fails with AttributeError if we raise below
        # before the dataset is ever opened (e.g. the validation error right after).
        self._dataset: rasterio.DatasetReader | None = None

        if len(source_paths) != 1:
            raise ValueError(
                "GeoTiffTileReader expects exactly one source raster per task; "
                f"got {len(source_paths)}. Split multiple large scenes into separate "
                "tasks under one Project instead."
            )

        original_path = Path(source_paths[0])
        self.original_raster_path = original_path
        self.was_reencoded_as_cog = False
        self.raster_path = original_path

        # Tiled directly from the raster's own native pixel grid regardless of how it's
        # georeferenced (direct affine, GCPs, or RPCs) -- no resampling/pre-warping step
        # here. RPC-referenced rasters need special handling for pixel<->geo
        # conversion (see cvat.apps.geospatial.rpc), not for tiling itself.
        if reencode_as_cog and not is_cog(original_path):
            cog_path = original_path.with_suffix(".cog.tif")
            self.raster_path = ensure_cog(original_path, cog_path)
            self.was_reencoded_as_cog = self.raster_path != original_path

        with rasterio.open(self.raster_path) as dataset:
            self.tile_specs: list[TileSpec] = build_tile_grid(
                raster_width=dataset.width,
                raster_height=dataset.height,
                tile_size=tile_size,
                overlap=overlap,
            )
            self.display_bands = pick_display_bands(dataset)
            self.display_band_stats = compute_display_band_stats(dataset, self.display_bands)

            tile_paths = [
                original_path.parent / f"{original_path.stem}__tile_r{t.row:05d}_c{t.col:05d}.png"
                for t in self.tile_specs
            ]

            # Actually write each tile's PNG to disk now, rather than only generating it
            # on demand from `get_image()`: CVAT's manifest generation
            # (`ImageManifestManager.create()`, called on `extractor.absolute_source_paths`
            # right after task creation) opens every frame's path directly to read its
            # size, with no path through `get_image()` at all -- it has no idea these
            # paths are backed by a raster window instead of an ordinary file. This is
            # the same reason `ArchiveReader`/`DirectoryReader` unroll to real files
            # instead of lazily-generated ones.
            for tile, tile_path in zip(self.tile_specs, tile_paths):
                png_bytes = read_tile_as_png_bytes(
                    dataset,
                    tile,
                    band_indices=self.display_bands,
                    band_stats=self.display_band_stats,
                )
                tile_path.write_bytes(png_bytes)

        self.tile_size = tile_size
        self.overlap = overlap

        # Tile order is already the authoritative row-major order; re-sorting by
        # filename would be redundant at best and risky at worst, so it's forced here
        # regardless of what the caller asked for.
        super().__init__(
            source_paths=tile_paths,
            step=step,
            start=start,
            stop=stop,
            dimension=dimension,
            sorting_method=SortingMethod.PREDEFINED,
        )

    def _get_dataset(self) -> rasterio.DatasetReader:
        if self._dataset is None:
            self._dataset = rasterio.open(self.raster_path)
        return self._dataset

    def close(self) -> None:
        if self._dataset is not None:
            self._dataset.close()
            self._dataset = None

    def __del__(self):  # pragma: no cover - best-effort cleanup
        self.close()

    def get_image(self, i: int) -> io.BytesIO:
        tile = self.tile_specs[i]
        png_bytes = read_tile_as_png_bytes(
            self._get_dataset(),
            tile,
            band_indices=self.display_bands,
            band_stats=self.display_band_stats,
        )
        return io.BytesIO(png_bytes)

    def get_image_size(self, i) -> tuple[int, int]:
        # Every tile is padded to a uniform size, so this never needs to touch the
        # dataset -- unlike the base class, which opens the image to inspect it.
        return (self.tile_size, self.tile_size)

    def __iter__(self) -> Iterator[IMediaReader.ImageFrame]:
        for i in self.frame_range:
            yield (self.get_image(i), self.get_path(i))


def register_geotiff_media_type() -> None:
    """Insert a "geotiff" entry into `cvat.apps.engine.media_extractors.MEDIA_TYPES`,
    ordered *before* "image" so a georeferenced TIFF is claimed by `_is_geotiff` before
    the generic, extension-based `_is_image` check ever sees it (dict iteration order
    is what `get_mime()` uses to pick the first matching type).

    Mutates the dict in place (`clear()` + `update()`) rather than rebinding the module
    attribute, since other modules already hold a reference to the same dict object
    (`from cvat.apps.engine.media_extractors import MEDIA_TYPES`).
    """
    from cvat.apps.engine import media_extractors

    if "geotiff" in media_extractors.MEDIA_TYPES:
        return  # idempotent - AppConfig.ready() can run more than once under autoreload

    reordered = {
        "geotiff": {
            "has_mime_type": _is_geotiff,
            "extractor": GeoTiffTileReader,
            "mode": TaskMode.ANNOTATION,
            "unique": True,
        },
        **media_extractors.MEDIA_TYPES,
    }
    media_extractors.MEDIA_TYPES.clear()
    media_extractors.MEDIA_TYPES.update(reordered)
