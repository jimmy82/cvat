# cvat.apps.geospatial

GeoTIFF ingestion, tiling, and geocoordinate export/import for CVAT. Teaches CVAT to
accept large, georeferenced rasters by tiling them into a synthetic sequence of CVAT
"frames" that reuse CVAT's existing job/segment splitting logic unmodified, and to
convert annotations between tile-pixel space and real-world coordinates in both
directions.

Originally scaffolded against a design doc (`CVAT_GeoTIFF_ML_Integration_Design.md`)
and built/tested in a sandbox with no live CVAT stack. This README describes the
**current, live-verified state** after that work was actually merged into the real
source tree, run against a real Docker stack, and extended -- see
`GEOSPATIAL_INTEGRATION_SESSION_SUMMARY.md` at the repo root for the full history of
what that took.

## How it fits together

* **`ingestion.py`** — pure, Django-free tiling primitives: `build_tile_grid`,
  `read_tile_as_png_bytes`, `ensure_cog`, `is_georeferenced_raster`,
  `resolve_georeferencing`, `needs_rpc_georeferencing`, etc. Built on `rasterio` (GDAL)
  windowed reads specifically to avoid the whole-file PIL decode that fails on very
  large TIFFs (see [cvat-ai/cvat#531](https://github.com/cvat-ai/cvat/issues/531) and
  [#2205](https://github.com/cvat-ai/cvat/issues/2205)). A raster is tiled from its own
  native pixel grid regardless of which georeferencing model it uses below -- nothing
  in this app ever resamples/warps the source imagery.
* **`transforms.py`** — pure coordinate conversion between tile-pixel, raster-pixel,
  and geographic (CRS) space for **affine-georeferenced** rasters (a direct transform,
  or a GCP-fitted one -- both are a single linear mapping).
* **`rpc.py`** — pure RPC (Rational Polynomial Coefficients) math for
  **RPC-georeferenced** rasters (common raw satellite/aerial imagery): forward
  evaluation (ground -> image) and an iterative Newton-Raphson inverse (image ->
  ground), since RPC has no direct affine equivalent and no closed-form inverse. Getting
  the polynomial term order right needed real-data validation, not just a
  round-trip-through-itself check -- see the module docstring and `tests/test_rpc.py`.
* **`media_extractor.py`** — `GeoTiffTileReader`, a `cvat.apps.engine.media_extractors
  .ImageListReader` subclass that "unrolls" one GeoTIFF into N tile frames, exactly the
  way `ArchiveReader`/`DirectoryReader` already unroll one archive/directory into many
  frames. Registers itself as a new `"geotiff"` entry in
  `cvat.apps.engine.media_extractors.MEDIA_TYPES` (ordered *before* `"image"` so a
  georeferenced TIFF is claimed before the generic extension-based image check sees
  it) via `AppConfig.ready()`. Materializes each tile as a real PNG file on disk at
  ingestion time (not lazily), since CVAT's manifest generation opens frame paths
  directly rather than going through the reader's own `get_image()`.
* **`models.py`** — `RasterSource` (one row per ingested GeoTIFF: georeferencing_kind
  affine/rpc, plus either affine transform + CRS or RPC coefficients, band/dtype info),
  `RasterTile` (one row per tile: frame index -> pixel window), `RasterTaskConfig`
  (per-task tiling settings).
* **`services.py`** — Django-facing bridge: `persist_raster_metadata()` writes a
  `GeoTiffTileReader`'s already-computed tile grid into the DB models above (called
  from `cvat.apps.engine.task.initialize_task()`'s `media_type == "geotiff"` branch);
  `pixel_pairs_to_wgs84()`/`wgs84_pairs_to_tile_pixel()` are the shared dispatch points
  every consumer (GeoJSON export/import, the live cursor status bar, the ruler tool) goes
  through to convert coordinates without needing to know or care whether a given
  `RasterSource` is affine- or RPC-georeferenced.
* **`dataset_io.py`** — registers a **"GeoJSON" export and import format** in
  `cvat.apps.dataset_manager`'s format registry. Export produces a single WGS84
  FeatureCollection merging every job's annotations for a task (see the job-completion
  gate below); import reads one back, matching each feature to the tile(s) it falls
  within -- a feature that fits inside a single tile becomes one CVAT shape there, and a
  feature that spans multiple tiles (e.g. drawn against the whole raster's extent
  instead of one tile) is automatically clipped and split into one shape per tile it
  overlaps (see "Importing whole-raster annotations" below). Both directions are lossy
  in one specific, documented way: a CVAT rectangle becomes a 4-point GeoJSON `Polygon`
  (a rotated rectangle has no other GeoJSON representation), so anything imported back
  in is a CVAT `polygon`, never a `rectangle`.
* **`views.py`** — `GET /api/tasks/<id>/geospatial/frames/`, returning each frame's
  WGS84 corner coordinates. Consumed by the frontend's live cursor-position status bar
  and the click-two-points ruler/distance-measurement tool (both in `cvat-ui`, outside
  this app -- see `cvat-ui/src/components/annotation-page/canvas/views/canvas2d
  /canvas-wrapper.tsx`).

## Georeferencing models supported

A raster can be georeferenced in any of three ways GDAL recognizes, and this app
handles all three without ever resampling the source imagery:

1. **Direct affine transform + CRS** — the common case. `RasterSource.affine`.
2. **GCPs** (ground control points) — a handful of (pixel, line) <-> (x, y)
   correspondences instead of a transform, typical of raw aerial/satellite quicklooks.
   A single affine is *fit* from the GCPs at ingestion time
   (`rasterio.transform.from_gcps`) and stored the same as case 1 -- from that point on
   a GCP-georeferenced raster is indistinguishable from a directly-georeferenced one.
3. **RPCs** (Rational Polynomial Coefficients) — common for raw satellite/aerial
   imagery. Not a linear transform at all, so it's stored and evaluated separately
   (see `rpc.py`); everything downstream (export, import, the status bar, the ruler
   tool) dispatches on `RasterSource.georeferencing_kind` via `services.py`'s helpers
   without needing its own affine/RPC branching logic.

## Job-completion gate on export

A raster's tiles are typically split across multiple jobs/annotators (via CVAT's
ordinary `segment_size`), so "export the task" is meant to mean "the merged result is
done", not "here's whatever's there right now". GeoJSON export refuses (with a clear
error naming the unfinished job) until every annotation job for the task (excluding
ground-truth/consensus-replica jobs) is marked `completed`.

## Importing whole-raster annotations

A GeoJSON feature doesn't have to be drawn against a single tile's extent. On import,
`dataset_io._import` first tries the fast path -- does the whole feature fit inside one
tile's pixel window? -- and only if that fails does it fall back to clipping: the
feature's geometry is converted into raster-pixel space (via the new
`services.wgs84_pairs_to_raster_pixel`, the untiled counterpart of
`wgs84_pairs_to_tile_pixel`) and intersected (via `shapely`, already a transitive
dependency) against every tile's pixel window it overlaps. Each non-empty piece becomes
its own CVAT shape on that tile's frame:

* A `Polygon`/rectangle clipped by an axis-aligned tile window stays a single polygon
  per tile (or splits into more than one piece per tile for a concave input that a tile
  boundary disconnects -- each piece is its own shape, since one CVAT shape can't
  represent disjoint geometry).
* A `LineString` that crosses a tile boundary becomes one polyline per tile; a line that
  exits and re-enters the same tile becomes more than one polyline there.
* A `MultiPoint` is bucketed by which tile each point falls in, one `points` shape per
  tile with the points that landed there.

This means an annotation authored (or generated by an external tool) against the whole
raster's geographic extent, rather than tile-by-tile, imports cleanly without the
annotator having to pre-split it by hand.

## `tile_size`/`overlap`/`reencode_as_cog` are real API + UI fields

Settable through `POST /api/tasks/{id}/data` (validated: `overlap` must be smaller than
`tile_size`) and through the Create Task page's Advanced Configuration section in the
UI ("Tile size" / "Tile overlap"). Setting `tile_size` at or above the raster's own
largest dimension (with `overlap=0`) puts the whole raster in a single frame instead of
tiling it -- useful when a raster is small enough that CVAT's ordinary single-image
pan/zoom canvas can show it directly, capped by PIL's own ~179-megapixel
decompression-bomb guard (the exact failure mode this whole tiling system exists to
avoid for anything larger).

## How this was verified

Every claim above has been exercised against a real Docker Compose stack (Postgres,
Redis, RQ workers, a real annotator uploading real files through the actual HTTP API),
not just unit tests against a curated settings module -- see
`GEOSPATIAL_INTEGRATION_SESSION_SUMMARY.md` for the specifics of what was uploaded and
what was cross-validated against independently-known ground truth for each
georeferencing model. In brief:

* A direct-affine synthetic raster: tiling, single-frame mode, and a GeoJSON
  export/import round-trip matching exactly.
* A GCP-georeferenced real raster (four corner GCPs): the fitted affine reproduces all
  four GCPs to sub-meter accuracy; export/import round-trips exactly.
* An RPC-georeferenced real raster (same underlying scene as the GCP one, for
  cross-validation): tiles from its own native, unwarped grid; export/import
  round-trips exactly; a pixel's exported coordinate independently agrees with the
  GCP-based raster's ground truth for the same physical location to ~2e-6 degrees.
* The job-completion gate: confirmed blocked with an "in progress" job, confirmed
  succeeding once completed.
* `tile_size`=4000 with a non-default `overlap`: confirmed correct tile grid dimensions
  and edge-tile padding against a real 254MB raster.
* Whole-raster GeoJSON import clipping: verified the clip math against a synthetic
  two-tile scenario (a rectangle straddling the tile boundary correctly splits into two
  tile-local rectangles meeting exactly at the shared edge); the redeployed container
  was confirmed to be running the updated code before handing it back for real use.

## Known gaps / follow-ups for a real deployment

1. **No real IAM/OPA permissions.** `TaskGeospatialFramesView` (and the GeoJSON
   export/import format, which rides on `cvat.apps.dataset_manager`'s own permission
   checks) use a hand-rolled visibility check mirroring
   `cvat.apps.ml_processing.views._user_can_view_job`'s documented stand-in, not a real
   `OpenPolicyAgentPermission` + Rego policy.
2. **Cloud-storage-backed uploads (`remote_files`)**: `_is_geotiff()`'s georeferencing
   check needs the file to be locally readable at MIME-detection time; a GeoTIFF
   sitting only in cloud storage at that point falls back to ordinary "image" handling
   rather than being tiled. Known, documented limitation, not a crash.
3. **`RasterTaskConfig` isn't actually written to.** The model exists (per-task
   tiling settings meant to make re-tiling reproducible) but nothing currently
   populates it -- `tile_size`/`overlap` are read from the request and used, not
   persisted into this table.
4. **RPC height is always assumed flat (0), never draped onto a DEM.** A real accuracy
   limit on terrain with significant relief; acceptable for annotation purposes, not
   for survey-grade measurement -- see `rpc.py`'s module docstring.
5. **Multi-task splitting for extremely large scenes** (one raster split across
   several Tasks under one Project) is discussed in the original design doc as a
   policy option but isn't implemented -- today one GeoTIFF maps to exactly one Task
   (`GeoTiffTileReader` raises `ValueError` if given more than one source file, by
   design).
6. **No true multi-resolution/deep-zoom viewer.** A raster too large even for
   single-frame mode (over PIL's ~179-megapixel guard once padded to a square) has no
   option but ordinary tiling -- CVAT has no tile-pyramid/whole-slide-imaging canvas to
   build on top of (confirmed by research during this work; would be genuine
   greenfield work spanning both the backend and `cvat-canvas` itself).
