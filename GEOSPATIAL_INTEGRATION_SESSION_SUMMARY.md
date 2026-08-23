# GeoTIFF Geospatial Integration — Project Summary

_Archived from a Claude Code session, 2026-08-22/23._

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
