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
  overlaps (see "Importing whole-raster annotations" below). Unlike most CVAT
  importers, a feature's `label` naming a class that doesn't already exist on the task
  isn't rejected -- it's registered as a new label the first time it's seen (see
  "Unrecognized classes are auto-registered on import" below). Both directions are
  lossy in one specific, documented way: a CVAT rectangle becomes a 4-point GeoJSON
  `Polygon` (a rotated rectangle has no other GeoJSON representation), so anything
  imported back in is a CVAT `polygon`, never a `rectangle`.
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

## Unrecognized classes are auto-registered on import

Every other CVAT importer requires a `label` to name a class that's already been
declared on the target task (or project) beforehand -- an unrecognized name fails the
whole import with `Label '...' is not registered for this task`
(`InstanceLabelData._get_label_id`, `cvat/apps/dataset_manager/bindings.py`). GeoJSON
import doesn't: `dataset_io._ensure_label_registered` checks each feature's `label`
against the task's (or its project's, if it belongs to one) already-known labels the
first time it's seen, and if it's new, creates it on the fly --
`type="any"`, an auto-assigned color (`cvat.apps.dataset_manager.formats.utils
.get_label_color`, the same helper CVAT's own label-creation API uses) -- then keeps
going with the rest of the import.

This exists because a GeoJSON file handed to CVAT for import (e.g. produced by an
external ML pipeline, per the original design's Phase 0 item 3/4, or hand-authored by a
GIS analyst) may name classes CVAT has never heard of; requiring every class to be
pre-declared through the UI first would defeat the point of a bulk import. Newly
created labels are always top-level (no `parent`/skeleton nesting) and, as with any
newly added label, apply going forward only -- they don't retroactively affect
annotations already made under a different name for what a human would consider "the
same" class.

**This required a small core `cvat.apps.dataset_manager` fix**, not just a change in
this app, and took two attempts to get right. `JobAnnotation` (`dataset_manager/task.py`)
validates every imported shape's `label_id` against `self.db_labels`
(`_validate_label_for_existence`) -- a dict built from `self.db_job`'s *prefetched*
`label_set`/`project.label_set` (see `add_prefetch_info`). A **prefetched** relation
manager's `.all()` keeps serving the snapshot taken when the prefetch query ran,
forever, regardless of what's since been written to the database -- so a label created
mid-import (by us, or by any future importer that does the same thing) looked "not
registered" even though it genuinely existed by validation time. This bites in two
different spots, not just one:

1. *Job*-level import: `JobAnnotation.__init__` snapshots labels *before*
   `import_annotations` runs the importer. Fixed by re-loading right after the importer
   returns and before validation/save.
2. *Task*-level import (`TaskAnnotation._patch_data`, used by the ordinary "Upload
   annotations" task-level endpoint): the importer runs first, but `_patch_data` then
   reuses one `db_job` object *fetched before that* (once, up front, for the whole
   task) for every job's `JobAnnotation(..., db_job=db_job)` construction --so even
   though each `JobAnnotation` is a fresh object built *after* the importer ran, it's
   built from an already-stale prefetched `db_job`. The first fix (reload only in
   job-level `import_annotations`) missed this path entirely, and a real import through
   it still failed the same way.

Both are fixed by the same underlying change: `JobAnnotation`'s label loading always
re-queries `models.Label.objects` directly (filtered by task/project id), never through
`db_job`'s prefetched relation, regardless of whether it's the initial load in
`__init__` or the explicit post-import reload in `import_annotations`. Verified against
the real production entrypoint (`dm.task.import_task_annotations`, not just internal
methods): imported a real GeoJSON file naming a brand-new class through the actual
task-level path, confirmed the shape landed with the correct label and points, and
confirmed (via the task's event log) the test left no other trace behind.

**A second, frontend-only gap remained even after the backend fix above**: a label
CVAT's backend genuinely created mid-import never appeared in the annotation page's
own "new shape" class selector until a full page reload. `cvat-core`'s `Job.labels`
(`cvat-core/src/session.ts`) is populated once, when the `Job` instance is constructed
(`Job.reinit`'s own comment flags this: "labels also may get changed, but ... need to
think on this additionally"), and `cvat-ui`'s import-completion handler
(`actions/import-actions.ts`'s `importDatasetAsync`) only ever re-fetched the
annotations themselves afterward, never the job's labels.

Fixed by adding `Job.fetchLabels()` (`session.ts` + its implementation in
`session-implementation.ts`, following the exact same `PluginRegistry.apiWrapper.call`
/ `Object.defineProperty(..., 'implementation', ...)` pattern every other `Job` method
already uses) -- it re-queries `serverProxy.labels.get({ job_id })` and replaces
`this.labels` via a new setter. `import-actions.ts` now awaits a new
`refreshJobLabelsAsync(jobInstance)` thunk (`actions/annotation-actions.ts`) right
after a job-level import succeeds and *before* re-fetching annotations -- ordering
matters here: an annotation for the brand-new label must not be converted into an
`ObjectState` before that label exists in `jobInstance.labels`, or resolving the shape's
label crashes downstream UI code that assumes every shape's label is always defined.
A new `UPDATE_JOB_LABELS_SUCCESS` reducer case (`reducers/annotation-reducer.ts`)
updates `state.job.labels`/`state.job.attributes` the same way `GET_JOB_SUCCESS`
already does for the initial load.

Verified directly against the real running bundle from the browser console (not just
code review): held a reference to an already-constructed `Job` instance, created a new
label on the task server-side (bypassing the UI entirely, the same way an import would),
then called `.fetchLabels()` on that stale instance and confirmed it picked up the new
label in place. Driving the actual upload modal end-to-end through browser automation
proved unreliable in this environment (AntD's controlled Select/Upload form components
don't reliably respond to synthetic DOM events), so this direct-mechanism test is the
verification of record for the frontend half of the fix; the backend half (label
creation, persistence, and the stale-snapshot fix above) was verified through the real
HTTP import path as noted above.

**A third gap, pre-existing in core CVAT and unrelated to this app, surfaced once the
fix above started actually changing `state.annotation.job.labels` during a session**:
a real user reported that after importing, the "draw new polygon" tool's class
selector correctly showed the new class, but "draw new rectangle" still didn't --
inconsistent behavior between two tools reading the exact same Redux state.
`DrawShapePopoverContainer` (`cvat-ui/src/containers/annotation-page/standard-workspace
/controls-side-bar/draw-shape-popover.tsx`) is mounted once per shape-type button (a
separate instance for rectangle, polygon, polyline, etc., each wrapped in its own antd
`Popover` that mounts its content on first open and never unmounts it again), and its
label list (`this.satisfiedLabels`, filtered from `props.labels` by shape type) was
computed **only in the constructor** -- correct for whichever instance hadn't been
opened yet before the label refresh (fresh constructor run, sees the update), stale
forever for whichever had already been opened before it (no `componentDidUpdate`/
`getDerivedStateFromProps` ever revisited it). This bug existed before our work here
too; nothing about `state.annotation.job.labels` ever changing mid-session, for any
reason, was needed to trigger it -- it just never had a code path capable of triggering
it until this app added one.

Fixed by recomputing `satisfiedLabels` (and the selected label, falling back to the
first satisfied label if the previous selection is no longer valid) in a
`componentDidUpdate` that compares `prevProps.labels` against the current value, using
a small extracted `computeSatisfiedLabels()` helper shared with the constructor.
Verified directly: opened the rectangle popover with 1 label already mounted, created
a new label server-side and dispatched the same `UPDATE_JOB_LABELS_SUCCESS` action
`refreshJobLabelsAsync` dispatches, and confirmed the already-open popover's dropdown
updated to show both classes without being closed and reopened.

**A fourth gap surfaced once the fix above was exercised through the real upload
modal end-to-end** (rather than the synthetic Redux dispatch used to verify it, which
happened not to trigger this): `importDatasetAsync`'s job branch called
`refreshJobLabelsAsync` *after* `(instance as Job).annotations.clear({ reload: true
})`. That call rebuilds cvat-core's internal annotation collection straight from the
server's response using whatever label list the job instance had *at that moment* --
if the import had just created a new label and a shape uses it, the label wasn't
loaded yet, and building that shape's `ObjectState` threw (`Cannot read properties of
undefined (reading 'attributes')` in `AnnotationBase.appendDefaultAttributes`, since
`this.labels[label_id]` came up empty) instead of just not showing the label yet.
Fixed by moving the `refreshJobLabelsAsync` call to run *before*
`annotations.clear({ reload: true })`, so the label list is already current by the
time that rebuild happens. Verified against the real upload modal end-to-end (format
selection, file attachment, and submission all driven programmatically against the
real DOM, not simulated): reproduced the crash against the unfixed build, confirmed
it's gone against the fixed one, and confirmed the already-open rectangle popover
picks up each newly imported class live, repeated across four consecutive imports.

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
* Auto-registering unrecognized labels on import: verified live against a real
  GeoTIFF-backed task (task id 4, one pre-existing label `building`) --
  `_ensure_label_registered` created a new `road` label on the task, was confirmed
  idempotent on a second call with the same name (no duplicate), and the test label was
  removed afterward.
* `JobAnnotation`'s stale-label-snapshot fix: reproduced the exact failure a real user
  hit (`label_id \`N\` is invalid` right after a new label was created mid-import)
  against a real task/job in isolation first, then -- after finding the first attempt
  only covered the job-level path -- imported a real GeoJSON file naming a new class
  through the actual `dm.task.import_task_annotations` production entrypoint
  (task-level, the path that was still failing), confirmed the shape landed correctly,
  and confirmed via the task's ClickHouse event log that the test added and removed
  exactly one label and one shape, nothing else.

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
