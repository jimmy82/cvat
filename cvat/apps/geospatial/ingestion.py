# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Pure, Django-free GeoTIFF tiling primitives.

This module deliberately has no dependency on Django models or CVAT's ORM so that the
tiling math, windowed reads, and pixel-value normalization can be unit tested in
isolation with nothing more than rasterio/numpy/Pillow. Django-facing orchestration
(persisting tile metadata against a Task) lives in `services.py`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil as rio_shutil
from affine import Affine
from PIL import Image
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.transform import from_gcps
from rasterio.windows import Window

DEFAULT_TILE_SIZE = 8096
DEFAULT_OVERLAP = 128

# Cap on how many bands we ever pull for the *display* PNG a browser renders. The
# original bands/dtype are always readable later straight from the source raster via
# the stored pixel window, so no radiometric information is lost by capping this.
MAX_DISPLAY_BANDS = 3

# Fallback CRS for GCP-georeferenced rasters whose GCPs don't carry their own CRS
# (common for simple corner-tagged aerial/satellite quicklooks): if a raster is
# georeferenced by GCPs at all, those coordinates are overwhelmingly given as WGS84
# lon/lat in practice, so that's a reasonable default rather than refusing the file.
GCP_FALLBACK_CRS = CRS.from_epsg(4326)


def resolve_georeferencing(dataset: rasterio.DatasetReader) -> tuple[Affine, CRS] | None:
    """Return an `(affine, crs)` pair usable for pixel<->geo conversion, or `None` if
    `dataset` has no georeferencing at all.

    Handles two distinct ways GDAL represents georeferencing:

    * The common case -- a real CRS plus a direct affine geotransform.
    * GCPs ("ground control points"): a handful of (pixel, line) <-> (x, y)
      correspondences instead of a transform, typical of raw aerial/satellite
      quicklooks. There's no direct affine in this case, so one is *fit* from the GCPs
      via `rasterio.transform.from_gcps` (GDAL's own least-squares/exact affine fit --
      exact for 3 non-collinear points, a best fit for more). This is an approximation
      good enough for annotation-coordinate purposes, not a substitute for actually
      orthorectifying the raster.
    """
    if dataset.crs is not None and dataset.transform is not None:
        if dataset.transform != Affine.identity():
            return dataset.transform, dataset.crs

    gcps, gcp_crs = dataset.gcps
    if gcps:
        return from_gcps(gcps), gcp_crs or GCP_FALLBACK_CRS

    return None


def is_georeferenced_raster(path: Path) -> bool:
    """True if `path` opens as a raster with real georeferencing (a direct affine
    transform + CRS, GCPs, or RPCs) -- i.e. it's worth routing through the tiling
    pipeline instead of CVAT's ordinary PIL-based image loader."""
    try:
        with rasterio.open(path) as dataset:
            return resolve_georeferencing(dataset) is not None or bool(dataset.rpcs)
    except RasterioIOError:
        return False


def needs_rpc_georeferencing(dataset: rasterio.DatasetReader) -> bool:
    """True if `dataset`'s only usable georeferencing is an RPC (Rational Polynomial
    Coefficients) model -- common for raw satellite/aerial imagery -- rather than a
    direct affine transform or GCPs. RPCs relate *ground* coordinates to image pixels
    through a pair of cubic rational polynomials (plus a sensor-dependent, generally
    non-affine correction across the scene), not a single affine transform, so there's
    no equivalent to `resolve_georeferencing`'s GCP handling here.

    The raster is tiled from its own native pixel grid exactly like any other
    GeoTIFF -- deliberately *not* pre-warped onto an affine grid, which would resample
    every pixel through an approximation and change the raster's own dimensions.
    Instead, `cvat.apps.geospatial.rpc` evaluates the RPC model directly (forward for
    ground->image, an iterative inverse for image->ground) wherever a pixel<->geo
    conversion is actually needed -- see `RasterSource.rpc` and
    `GeoreferencingKind.RPC`.
    """
    return resolve_georeferencing(dataset) is None and bool(dataset.rpcs)


@dataclass(frozen=True)
class TileSpec:
    """Describes one tile's location within its source raster.

    `width`/`height` are the pixels actually available from the source raster for this
    tile (<= tile_size); tiles at the right/bottom edge of the raster are smaller before
    padding. `pad_right`/`pad_bottom` record how much padding was added to bring the
    rendered PNG up to a uniform `tile_size` x `tile_size`, which is what keeps zoom/pan
    behavior in the CVAT frontend identical across every tile regardless of where it
    sits in the raster.
    """

    index: int
    row: int
    col: int
    col_off: int
    row_off: int
    width: int
    height: int
    pad_right: int
    pad_bottom: int
    tile_size: int

    @property
    def window(self) -> Window:
        return Window(self.col_off, self.row_off, self.width, self.height)


def build_tile_grid(
    raster_width: int,
    raster_height: int,
    tile_size: int = DEFAULT_TILE_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[TileSpec]:
    """Compute a deterministic, row-major grid of tiles covering a raster of the given
    size. Adjacent tiles share `overlap` pixels on their shared edge so that objects
    straddling a tile boundary aren't fully invisible to any single annotator; tile
    frame indices are assigned in raster-scan order (row 0 left-to-right, then row 1,
    ...), which is what lets CVAT's ordinary segment/job splitting distribute tiles
    across annotators without any changes to that logic.
    """
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    if overlap < 0 or overlap >= tile_size:
        raise ValueError("overlap must be non-negative and smaller than tile_size")
    if raster_width <= 0 or raster_height <= 0:
        raise ValueError("raster_width and raster_height must be positive")

    stride = tile_size - overlap
    row_offs = list(range(0, raster_height, stride))
    col_offs = list(range(0, raster_width, stride))
    n_cols = len(col_offs)

    tiles: list[TileSpec] = []
    for row_i, row_off in enumerate(row_offs):
        height = min(tile_size, raster_height - row_off)
        for col_i, col_off in enumerate(col_offs):
            width = min(tile_size, raster_width - col_off)
            tiles.append(
                TileSpec(
                    index=row_i * n_cols + col_i,
                    row=row_i,
                    col=col_i,
                    col_off=col_off,
                    row_off=row_off,
                    width=width,
                    height=height,
                    pad_right=tile_size - width,
                    pad_bottom=tile_size - height,
                    tile_size=tile_size,
                )
            )

    return tiles


def is_cog(path: Path) -> bool:
    """Best-effort check for whether a raster is already laid out as a (Cloud-Optimized)
    tiled GeoTIFF with overviews, in which case re-encoding it is unnecessary."""
    try:
        with rasterio.open(path) as dataset:
            return bool(dataset.profile.get("tiled")) and len(dataset.overviews(1)) > 0
    except (RasterioIOError, IndexError):
        return False


def ensure_cog(src_path: Path, dst_path: Path) -> Path:
    """Return a path to a tiled GeoTIFF with overviews for `src_path`, re-encoding to
    `dst_path` only if the source isn't already laid out that way. Re-encoding is what
    makes the later windowed tile reads cheap and is what lets very large rasters (the
    scenario that fails with plain PIL, see cvat-ai/cvat#531 and #2205) be ingested
    without decoding the whole file into memory at once.
    """
    if is_cog(src_path):
        return src_path

    with rasterio.open(src_path) as src:
        try:
            rio_shutil.copy(
                src,
                dst_path,
                driver="COG",
                compress="DEFLATE",
                BIGTIFF="IF_SAFER",
            )
            return dst_path
        except Exception:
            # Not every GDAL build ships the COG driver. Fall back to a plain tiled
            # GeoTIFF with a manually built overview pyramid, which still gives us
            # windowed reads and a cheap low-res preview.
            profile = src.profile.copy()
            profile.update(driver="GTiff", tiled=True, blockxsize=512, blockysize=512)
            with rasterio.open(dst_path, "w", **profile) as dst:
                dst.write(src.read())
                # `.profile` only covers crs/transform, not GCPs or RPCs -- carry those
                # over explicitly so a GCP- or RPC-georeferenced source doesn't lose its
                # georeferencing entirely when this fallback path is taken.
                gcps, gcp_crs = src.gcps
                if gcps:
                    dst.gcps = (gcps, gcp_crs)
                if src.rpcs:
                    dst.rpcs = src.rpcs
                dst.build_overviews([2, 4, 8, 16], Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")
            return dst_path


def compute_display_band_stats(
    dataset: rasterio.DatasetReader,
    band_indices: list[int],
    *,
    sample_size: int = 2048,
) -> list[tuple[float, float]]:
    """Compute one 2nd/98th-percentile stretch per band from a downsampled read of the
    *whole* raster (via its overview pyramid, so this stays cheap even on huge scenes).
    Reusing the same stats for every tile keeps brightness/contrast consistent across
    tiles instead of each tile being stretched independently against its own, possibly
    unrepresentative, local pixel values.
    """
    out_shape = (
        len(band_indices),
        max(1, min(sample_size, dataset.height)),
        max(1, min(sample_size, dataset.width)),
    )
    data = dataset.read(band_indices, out_shape=out_shape, resampling=Resampling.average)

    stats: list[tuple[float, float]] = []
    for band in data:
        valid = band[np.isfinite(band.astype(np.float64))]
        if valid.size == 0:
            stats.append((0.0, 1.0))
            continue
        lo, hi = (float(v) for v in np.percentile(valid.astype(np.float64), [2, 98]))
        if hi <= lo:
            hi = lo + 1.0
        stats.append((lo, hi))
    return stats


def _normalize_to_uint8(
    array: np.ndarray, *, band_stats: list[tuple[float, float]] | None
) -> np.ndarray:
    """Stretch a (bands, height, width) array to uint8, band by band."""
    out_bands = []
    for i, band in enumerate(array):
        band = band.astype(np.float64)
        if band_stats is not None:
            lo, hi = band_stats[i]
        else:
            valid = band[np.isfinite(band)]
            if valid.size == 0:
                lo, hi = 0.0, 1.0
            else:
                lo, hi = (float(v) for v in np.percentile(valid, [2, 98]))
                if hi <= lo:
                    hi = lo + 1.0
        scaled = np.clip((band - lo) / (hi - lo), 0.0, 1.0) * 255.0
        out_bands.append(scaled.astype(np.uint8))
    return np.stack(out_bands, axis=0)


def pick_display_bands(dataset: rasterio.DatasetReader) -> list[int]:
    """Choose which 1-indexed band numbers to render for the browser-facing PNG. A
    3+-band raster is assumed RGB-like and uses its first three bands; anything with
    fewer bands (panchromatic, single-band DEM/NDVI, etc.) is shown as grayscale.
    """
    return list(range(1, min(dataset.count, MAX_DISPLAY_BANDS) + 1))


def read_tile_as_png_bytes(
    dataset: rasterio.DatasetReader,
    tile: TileSpec,
    *,
    band_indices: list[int] | None = None,
    band_stats: list[tuple[float, float]] | None = None,
) -> bytes:
    """Windowed-read one tile from an *already open* rasterio dataset and encode it as
    an 8-bit PNG padded to `tile.tile_size` x `tile.tile_size`. This never touches
    pixels outside `tile.window`, which is the property that makes tiling large rasters
    memory-safe (contrast with PIL's whole-file decode).
    """
    if band_indices is None:
        band_indices = pick_display_bands(dataset)

    data = dataset.read(band_indices, window=tile.window)  # (bands, height, width)
    normalized = _normalize_to_uint8(data, band_stats=band_stats)

    canvas = np.zeros((len(band_indices), tile.tile_size, tile.tile_size), dtype=np.uint8)
    canvas[:, : tile.height, : tile.width] = normalized

    if len(band_indices) == 1:
        image = Image.fromarray(canvas[0], mode="L").convert("RGB")
    else:
        image = Image.fromarray(np.moveaxis(canvas[:3], 0, -1), mode="RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def read_tile_raw(dataset: rasterio.DatasetReader, tile: TileSpec) -> np.ndarray:
    """Windowed-read a tile's *original* pixel data (all bands, native dtype), for
    handing to the Python processing engine when it needs radiometric data rather than
    the display-oriented 8-bit PNG (e.g. NDVI, multispectral classification)."""
    return dataset.read(window=tile.window)
