# cvat.apps.geospatial

GeoTIFF ingestion and tiling for CVAT. Implements Phase 1 of the
`CVAT_GeoTIFF_ML_Integration_Design.md` design doc: teaches CVAT to accept large,
georeferenced rasters by tiling them into a synthetic sequence of CVAT "frames" that
reuse CVAT's existing job/segment splitting logic unmodified.

## How it fits together

* **`ingestion.py`** — pure, Django-free tiling primitives: `build_tile_grid`,
  `read_tile_as_png_bytes`, `ensure_cog`, `is_georeferenced_raster`, etc. Built on
  `rasterio` (GDAL) windowed reads specifically to avoid the whole-file PIL decode that
  fails on very large TIFFs today (see
  [cvat-ai/cvat#531](https://github.com/cvat-ai/cvat/issues/531) and
  [#2205](https://github.com/cvat-ai/cvat/issues/2205)). Fully unit tested with no
  Django/database dependency — see `tests/test_ingestion.py` and
  `tests/test_transforms.py`.
* **`transforms.py`** — pure coordinate conversion between tile-pixel, raster-pixel,
  and geographic (CRS) space, for converting annotations back to real-world
  coordinates.
* **`media_extractor.py`** — `GeoTiffTileReader`, a `cvat.apps.engine.media_extractors
  .ImageListReader` subclass that "unrolls" one GeoTIFF into N tile frames, exactly the
  way `ArchiveReader`/`DirectoryReader` already unroll one archive/directory into many
  frames. Registers itself as a new `"geotiff"` entry in
  `cvat.apps.engine.media_extractors.MEDIA_TYPES` (ordered *before* `"image"` so a
  georeferenced TIFF is claimed before the generic extension-based image check sees
  it) via `AppConfig.ready()`.
* **`models.py`** — `RasterSource` (one row per ingested GeoTIFF: CRS, affine
  transform, band/dtype info), `RasterTile` (one row per tile: frame index -> pixel
  window), `RasterTaskConfig` (per-task tiling settings).
* **`services.py`** — `persist_raster_metadata()`, the Django-facing bridge that
  writes a `GeoTiffTileReader`'s already-computed tile grid into the DB models above,
  called from `cvat.apps.engine.task.initialize_task()` right after the extractor is
  built (see the `media_type == "geotiff"` branch there).

## Verifying this in the CVAT dev stack

Migrations: `python manage.py migrate geospatial`. `rasterio` needs adding to
`cvat/requirements/base.in`/`.txt` (not done automatically by this patch — see "Known
integration gaps" below) and the Docker image needs GDAL runtime libraries, which the
`rasterio` manylinux wheel bundles, so a plain `pip install rasterio` is normally
sufficient without extra system packages.

## How this was verified in this sandbox

This code was developed and tested inside a cloud sandbox that could not install
CVAT's full dependency set — most notably the exact pinned `datumaro` fork CVAT
requires isn't fetchable here (network access to that specific GitHub commit is
blocked, and the public PyPI `datumaro` package is a different, incompatible
version — several unrelated CVAT apps, like `quality_control`/`consensus`, fail to
import against it). None of that is needed by this app, so verification used a
curated Django settings module, `dev/sandbox_verification_settings/` (see the README
there for exactly what it changes and why), rather than `manage.py test` against the
real 40-app `cvat.settings.testing`.

What that verified for real, against a real (sqlite) Django ORM and a real
rasterio-backed synthetic GeoTIFF:

* Tile grid math (exact-multiple, padded edge tiles, overlap/stride, determinism,
  input validation) — 32 pure unit tests, no Django needed at all.
* COG detection/re-encoding round-trips pixel values exactly.
* `read_tile_as_png_bytes` produces correctly-sized, correctly-padded PNGs; band
  selection and shared brightness/contrast stats across tiles.
* Coordinate transforms round-trip correctly in both directions (tile-pixel <->
  raster-pixel <-> geographic), including the "point falls outside this tile" case.
* `GeoTiffTileReader` constructed directly and iterated end-to-end against a real
  Task/Data row: produces the expected frame count, every frame is a valid PNG at the
  configured tile size, and `persist_raster_metadata()` writes matching `RasterSource`
  + `RasterTile` rows, including the DB-level uniqueness constraint on
  `(raster_source, frame)`.

What was **not** verified against a running CVAT instance (documented gaps, not silent
omissions):

* The actual upload -> `initialize_task()` call path end-to-end through the real HTTP
  API (task creation, chunk generation, frame serving to the browser). The
  `media_type == "geotiff"` branch added to `cvat/apps/engine/task.py` was confirmed to
  **import** cleanly (no circular imports, no `NameError`s) under the same curated
  settings, but was not exercised via an actual `POST /api/tasks/{id}/data` call, which
  would need the full stack (Postgres, Redis, a real RQ worker, ffmpeg/av) running.
* Cloud-storage-backed uploads (`remote_files`): `_is_geotiff()`'s georeferencing check
  needs the file to be locally readable at MIME-detection time; a GeoTIFF sitting only
  in cloud storage at that point falls back to ordinary "image" handling rather than
  being tiled. This is a known, documented limitation, not a crash.
* Manifest generation, honeypot/ground-truth frame allocation, and the cloud-storage
  manifest code paths in `task.py` were not specifically exercised against a tiled
  GeoTIFF task — they're generic over any `IImageReader`, so they *should* work
  unmodified, but "should" is not "verified."

## Known integration gaps / follow-ups for a real deployment

1. **`rasterio` has been added to `cvat/requirements/base.in`** (pinned `~=1.4`,
   installs cleanly from a manylinux wheel with GDAL bundled in — no extra system
   packages needed). `rasterio==1.4.4` plus its 3 small pure-Python dependencies
   (`affine`, `click-plugins`, `cligj`) were also hand-added to `base.txt`, in the
   right alphabetical spot with `# via` annotations, using the exact versions pip
   actually resolved when installing `rasterio~=1.4` during this work -- this is
   enough for a Docker build to install them correctly. What was *not* done is a real
   `pip-compile` regeneration, so `base.txt`'s SHA1 content-hash header comment is now
   stale/inconsistent with the file's actual contents. Run the documented
   regeneration step (`cvat/requirements/README.txt` -> `regenerate.sh`) in a real dev
   environment with full network access at some point to get a clean, consistent
   lockfile -- but a build should work correctly without that being done first.
2. **`tile_size`/`overlap`/`reencode_as_cog` aren't first-class API fields yet.**
   `task.py` reads them via `data.get("tile_size")` etc. with sane defaults, but
   `cvat.apps.engine.serializers.DataSerializer` doesn't declare them, so a client
   can't actually set them through the public REST API today without that small,
   separate serializer change (plus a `RasterTaskConfig` write path to persist the
   *requested* settings, not just what the extractor happened to use).
3. **No GeoJSON/shapefile export wired up yet.** `transforms.py` has everything needed
   (`shape_tile_pixels_to_geo`) but no export-format integration exists in
   `cvat.apps.dataset_manager` yet to actually offer it as a download option.
4. **Multi-task splitting for extremely large scenes** (one raster split across
   several Tasks under one Project) is discussed in the design doc as a policy option
   but isn't implemented — today one GeoTIFF maps to exactly one Task
   (`GeoTiffTileReader` raises `ValueError` if given more than one source file, by
   design, but doesn't yet offer the "split across N tasks" path).
