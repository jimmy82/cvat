# GeoTIFF Geospatial Integration — Project Summary

_Archived from a Claude Code session, 2026-08-22 through 2026-09-06._

## Phase 0 — The original design (18 August 2026)

A design doc (`CVAT_GeoTIFF_ML_Integration_Design.md`, kept alongside this repo checkout)
specified four capabilities for CVAT:

1. **Ingest large GeoTIFFs** by tiling them into CVAT "frames" via `rasterio` windowed
   reads (avoiding PIL's whole-file decode, which upstream CVAT issues #531/#2205 report
   failing on big rasters), reusing CVAT's existing job/segment splitting unmodified.
2. **Reconstitute real-world coordinates** — convert tile-pixel annotations back to
   geographic coordinates via the raster's affine transform, exposed as a
   GeoJSON/shapefile export.
3. **A "Processing" toolbar button** to send a job's annotations to an external Python
   ML engine over HTTP (async, HMAC-signed, mirroring CVAT's webhook pattern).
4. **A signed callback endpoint** for the engine to return updated/proposed
   annotations, merged back into the job.

## Phase 1 — The prior sandbox build

A separate earlier effort (documented in `GEOTIFF_ML_INTEGRATION.md`) implemented most
of this — the `geospatial` and `ml_processing` Django apps, a mock Python engine, and
frontend toolbar — built and unit-tested in a sandboxed environment with **no live CVAT
stack, no Docker, no real upload ever exercised**. That report was explicit about the
gap: detection logic, manifest generation, and the real upload→ingest path were "not
exercised via a live HTTP upload," and the whole diff was left as **loose, uncommitted
patch files** rather than merged into the actual source tree, because the sandbox
couldn't fetch the pinned Yarn version needed for the pre-commit hook.

## Phase 2 — Making it real

Starting from a broken `docker compose ... up -d --build` command, the entire feature
was found to have never been applied to the CVAT tree being run. From there:

- **Merged** the loose patch into the real `cvat/apps/...` source tree.
- **Found and fixed every bug the sandbox couldn't catch** by actually running it:
  `AUTH_USER_MODEL` incompatibility, GeoTIFF detection failing on real browser uploads
  (bare filename vs. real path in `_count_files`), tile PNGs never being written to disk
  (breaking manifest generation), a `group_by_frame()` streaming-assertion crash, a
  label name/id mixup, and an `overlap=0`-treated-as-unset bug (`or` vs. `dict.get`
  default).
- **Added GCP-based georeferencing support** — the real test raster used ground control
  points, not a direct affine+CRS, which the original design never anticipated. Added
  `rasterio.transform.from_gcps()` fitting, and fixed a related bug where COG
  re-encoding's fallback path could silently drop GCPs.
- **Closed the design's stated export gap**: built the actual GeoJSON export *and*
  import (the design only asked for export), verified with exact round-trip coordinate
  matches against independently-computed ground truth.
- **Solved "why can I only see one tile"**: made `tile_size`/`overlap` real, working API
  and UI fields — specified in the original design as a task-level setting but never
  actually wired to the API (`DataSerializer` never declared them).
- **Added a feature beyond the original design**: a live geocoordinate readout under the
  mouse cursor on the annotation canvas, backed by a new
  `GET /api/tasks/{id}/geospatial/frames/` endpoint.
- Did **not** touch the ML-processing engine round-trip (Phase 0 items 3–4) — that was
  already functionally present from the sandbox build and out of scope for the reported
  problems, which were all on the ingestion/coordinate side. It still carries its own
  documented gaps (real IAM/OPA permissions, `dataset_manager`-integrated annotation
  merging, timeout sweeps) — see `cvat/apps/ml_processing/INTEGRATION.md`.

## Phase 3 — Shipping it

Committed to branch `feature/geotiff-geospatial-integration` and pushed to
[github.com/jimmy82/cvat](https://github.com/jimmy82/cvat/tree/feature/geotiff-geospatial-integration).

## Phase 4 — Continued improvements after the initial ship

More real usage surfaced more gaps, each fixed and re-verified against the live stack:

- **`s` as a plain save-annotations shortcut**, alongside the existing `ctrl+s`
  (`save-annotations-button.tsx`'s `SAVE_JOB.sequences`).
- **Multi-job merge-on-export was already the design's intent but not yet enforced.**
  The user pointed out that since one raster is split into tiles across separate jobs,
  "export" should mean the whole task's merged result, not partial in-progress work.
  Added `_require_jobs_completed()` in `dataset_io.py`: export now refuses with a
  clear "not yet completed: job id(s) ..." error until every annotation job for the
  task is marked `completed` (ground-truth/consensus-replica jobs excluded).
  Verified blocked with an in-progress job, then verified it succeeds once completed.
- **Tested `tile_size=4000`** (roughly 2km per tile on the real raster's ground
  sampling distance) end-to-end against a real 254MB GeoTIFF with a non-default
  `overlap`, confirming correct tile-grid dimensions and correct edge-tile padding.
  Along the way, hit and explained (not a bug) PIL's `DecompressionBombError` when an
  overly large `tile_size` was tried for single-frame mode — PIL's own ~179-megapixel
  safety guard, the exact failure this tiling system exists to route around; fixed by
  choosing a `tile_size` sized to the raster's actual dimensions instead.
- **Added a full ruler / distance-measurement tool** to the annotation canvas (user's
  explicit choice over a lighter "mark a point" alternative, since the task is already
  geocoordinate-aware): click two points, draw a line, label it with the great-circle
  (haversine) distance. Implemented entirely in
  `canvas-wrapper.tsx` as imperative SVG manipulation (append `<line>`/`<text>` directly
  into cvat-canvas's stable `#cvat_canvas_content`/`#cvat_canvas_text_content` DOM nodes)
  rather than React state, to match the existing status-bar approach and avoid
  re-render overhead on mousemove.
  - Hit a method-name collision with a pre-existing canvas click handler; renamed the
    new one to `onMeasureCanvasClick`.
  - Hit a coordinate-space bug: the click handler's raw SVG point needed the same
    `geometry.offset` subtraction that cvat-canvas's own `canvas.moved` event already
    applies, or the line would draw offset from the actual click.
  - **Self-correction on verification honesty**: at one point stated the ruler tool was
    "verified working" based on an earlier, incomplete debug session — this was false,
    and was retracted explicitly to the user as soon as noticed. On actually re-testing
    with a real hover-then-two-click sequence, the tool *did* work correctly (the
    earlier debug attempt's test methodology had been the flawed part, not the
    underlying code) — confirmed live and reported plainly, without over-correcting
    into a new unverified claim in the other direction.
- **RPC (Rational Polynomial Coefficient) georeferencing.** The user uploaded a real
  raster and reported "i do not see tiling happens for the raster" — root cause: the
  raster used RPCs, a georeferencing model the integration didn't recognize at all
  (only direct-affine and GCP-fitted-affine were handled), so it silently fell through
  to CVAT's ordinary single-image path instead of being tiled.
  - **First attempt**: warp the raster onto a new WGS84-aligned grid at ingestion time
    (`rasterio.warp.reproject`), so downstream code could keep treating everything as a
    simple affine transform. Implemented and verified working end-to-end.
  - **User explicitly rejected this approach**: *"i do not want to rewrap the image."*
    Warping resamples every pixel onto a new grid — changing pixel values and
    dimensions from the original source raster — which the user did not want under any
    circumstances, even though it "worked."
  - **Reworked from scratch to avoid all resampling**: raster tiles are read from their
    own native, unwarped pixel grid regardless of georeferencing model; RPC ground
    coordinates are computed directly, per-point, from the RPC polynomial model itself.
    - Ruled out GDAL's own native point-wise RPC transformer since `osgeo` (GDAL's
      Python bindings) isn't available in this stack (confirmed via
      `ModuleNotFoundError`), requiring a pure-Python implementation.
    - New `cvat/apps/geospatial/rpc.py`: `rpc_forward()` (ground → image, direct
      cubic-rational-polynomial evaluation per the RPC00B spec) and `rpc_inverse()`
      (image → ground, since RPC has no closed-form inverse — solved via
      Newton-Raphson iteration with a finite-difference Jacobian).
    - **Caught a real correctness bug via cross-validation, not by unit-testing in
      isolation**: the first implementation normalized longitude into the model's "Y"
      term and latitude into "X" (backwards from the RPC00B convention). This passed a
      trivial self-test (evaluating at the reference point, where the swap is
      invisible because all the non-constant polynomial terms vanish to zero there) but
      produced ~500–1000m of error everywhere else. Caught by cross-checking against
      four known ground-truth corner coordinates independently derived from an earlier
      GCP-georeferenced test file of the *same physical scene* — a real check the
      trivial self-test could never have caught. Fixed by swapping the term order;
      re-verified all four corners matched to ~0.3–0.7 pixels (forward) and ~1e-6
      degrees (inverse).
    - Added `GeoreferencingKind` (`affine`/`rpc`) to `RasterSource`, made the affine
      transform fields nullable, added an `rpc_coefficients` JSON field, and wrote a
      proper new migration (`0002_add_rpc_support.py`) rather than editing the
      already-applied `0001_initial.py` in place — the live dev database already had
      real data under the old schema, and editing an applied migration wouldn't
      retroactively change it.
    - `services.py`'s `pixel_pairs_to_wgs84()`/`wgs84_pairs_to_tile_pixel()` now dispatch
      on `georeferencing_kind`, so every consumer (GeoJSON export/import, the live
      cursor status bar, the ruler tool) handles RPC rasters automatically with no
      changes of their own.
    - Fixed a related latent bug this surfaced: the manual COG-re-encoding fallback
      path (used only when the installed GDAL build lacks a native COG driver) was
      silently dropping RPC (and, earlier, GCP) tags when copying a dataset — fixed by
      explicitly re-attaching them on the re-encoded output.
    - Wrote `cvat/apps/geospatial/tests/test_rpc.py` (real GCP-derived ground-truth
      corners, parametrized forward/inverse/round-trip cases) for future use in an
      environment with `pytest` installed — the running production container has `pip`
      deliberately uninstalled for hardening, so the same logic was instead validated
      via inline `python -c` scripts against the container's actual Python/rasterio.
- **Git housekeeping**: a later push to the fork was rejected because the remote had
  moved on (a `Merge branch 'cvat-ai:develop' into feature/...` commit from the fork's
  own sync, not from this session) — resolved with an ordinary `git fetch` +
  `git merge` (clean, no conflicts) before re-pushing, never force-pushing.
- **Documentation pass**: rewrote `cvat/apps/geospatial/README.md` from scratch (it
  had gone stale describing only the original sandbox-era state) and extended this
  summary document, per the user's explicit request to capture the full history for
  anyone else looking at the pushed branch.

## Key files touched

- `cvat/apps/geospatial/` — GeoTIFF ingestion/tiling, coordinate transforms, GeoJSON
  export/import (`dataset_io.py`), live-cursor endpoint (`views.py`).
- `cvat/apps/ml_processing/` — external ML engine round-trip (merged in, not modified).
- `cvat/apps/engine/task.py`, `serializers.py`, `views.py` — upload path fixes,
  `tile_size`/`overlap` API fields.
- `cvat/apps/dataset_manager/formats/registry.py` — registers the GeoJSON format.
- `cvat-core/src/annotations.ts`, `session-implementation.ts` — GeoJSON upload
  MIME/extension allowlist, `tile_size`/`overlap` request wiring.
- `cvat-ui/src/components/create-task-page/advanced-configuration-form.tsx` —
  Tile size / Tile overlap fields in the Create Task UI.
- `cvat-ui/src/components/annotation-page/canvas/views/canvas2d/canvas-wrapper.tsx` —
  live geocoordinate status bar.
