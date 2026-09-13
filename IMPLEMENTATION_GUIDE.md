# Implementation Guide: GeoTIFF Annotation + External ML Engine Integration

Implements [TECHSPEC.md](TECHSPEC.md) / [SPEC.md](SPEC.md). This document is the
*build plan*: recommended order, what to verify at each step, and every real gotcha
hit while actually building and running this once, so the next person (on this CVAT
version or any other) doesn't rediscover them the hard way.

**A note on version-independence:** Section 9's file/path mapping is a snapshot of
where things live in *one specific* CVAT fork
(`github.com/jimmy82/cvat`, branch `feature/geotiff-geospatial-integration`, as of
this writing). It is reference material for *that* checkout only — treat it as
"here's where this landed last time," not as a stable contract. Everywhere else in
this document, guidance is phrased in terms of the capability needed (per
TECHSPEC.md §7), not a file path, precisely so it holds up against a different
version.

## 0. Before writing any platform-integration code

Build and fully unit-test the two portable libraries from TECHSPEC.md §1
(components 1–2: tiling, coordinate transforms) with **zero** framework/DB/HTTP
dependency. This is the highest-leverage step available: it's the part of the system
cheapest to get real, fast, CI-friendly test coverage on, and the part most likely to
be reused unchanged if the host platform ever changes. Concretely:

- Tile grid construction (TECHSPEC.md §3.1) against synthetic dimensions — verify
  frame count, edge padding, and overlap math with plain arithmetic assertions, no
  raster I/O needed.
- Windowed reads against small **real** synthetic GeoTIFFs (a real raster library
  writing a tiny test file is more honest than mocking the library away entirely).
- Affine and GCP-fit transforms, round-tripped against known coordinates.
- **RPC forward/inverse, validated against independently-known ground truth away from
  the model's reference point** — not just a self-consistency round-trip (see
  TECHSPEC.md §3.3's term-order trap; a naive test here will pass with a real bug
  still in the code).
- Whole-raster clipping (TECHSPEC.md §3.4) against a synthetic two-tile scenario with
  a shape deliberately straddling the boundary.

Do not proceed to Phase A until this layer has real, passing, non-trivial tests. If
you can't install the host platform's full dependency stack in your build
environment, that's fine here — this layer needs no host-platform runtime at all.

## Phase A — Ingestion: from raster to tiled frames

**Goal:** a Task created from a large georeferenced raster produces the right number
of correctly-shaped, correctly-positioned frames, viewable in the ordinary annotation
canvas, with peak memory independent of file size.

**Build:**
1. Wire the tiling library into the platform's pluggable-frame-source seam
   (TECHSPEC.md §7.1). Detect a georeferenced raster (extension + a real
   "is this actually georeferenced" check — don't rely on extension alone) and claim
   it *before* generic image handling would otherwise see it.
2. Persist `RasterSource` / `RasterTile` (TECHSPEC.md §2.1–2.2) once tiling completes.
3. Materialize each tile's display image to disk eagerly if the platform's own
   manifest/thumbnail step needs to open frame files directly (§7.1's gotcha) —
   confirm this by actually creating a task and checking whether thumbnails/manifest
   generation succeeds, not by reading the platform's source and assuming.
4. Confirm the platform's existing job/segment-splitting logic works over the new
   frame sequence completely unmodified — if it doesn't, that's a sign of unexpected
   coupling to fix here, not a reason to add new distribution logic.

**Verify against a real deployment, not just unit tests:**
- Upload a real, sizeable (hundreds of MB) GeoTIFF through the actual upload path,
  not a synthetic tiny fixture — several real bugs in the reference build only
  surfaced this way (a media-type check that worked on a synthetic fixture's bare
  filename but failed on a real browser upload's actual path; tile PNGs computed
  correctly in memory but never actually written to disk, breaking manifest
  generation only in the real pipeline).
- Confirm tiles render, pan, and zoom correctly in the browser.
- Test at least one non-default `tile_size`/`overlap` combination against a real
  multi-hundred-MB raster and manually confirm tile-grid dimensions and edge padding.
- If your test raster happens to use GCPs or RPC rather than a direct affine
  transform, don't assume the "happy path" direct-affine case is representative —
  budget real time for whichever georeferencing model(s) your actual source imagery
  uses (this alone was a multi-day effort in the reference build, including a full
  redesign after an initial resample-based approach was explicitly rejected — see
  TECHSPEC.md §3.3 and this file's Phase A retrospective note below).

**Retrospective note — resampling is not a shortcut:** it may be tempting to solve
"support georeferencing model X" by warping/reprojecting the raster onto a simple
affine grid at ingestion time, so all downstream code can stay affine-only. This
works, and is *faster to build* — but it resamples every pixel, changing the source
raster's own pixel values and dimensions. If SPEC.md's "no pixel-value side effects"
requirement is real for your use case (it was, here, non-negotiably, per explicit user
rejection of a working-but-resampling implementation), don't take this shortcut even
provisionally — the correct design tiles from the native, unwarped grid regardless of
georeferencing model, computing ground coordinates directly from whatever model
applies (TECHSPEC.md §3.3).

## Phase B — Coordinate reconstitution: export

**Goal:** a task's annotations are exportable as a single geographic feature
collection in the raster's real-world CRS, correct under every georeferencing model
you support.

**Build:**
1. Register the export format into the platform's dataset format registry
   (§7.2).
2. Route every coordinate conversion through the shared `pixel↔geo` dispatch
   boundary (TECHSPEC.md §3.3) — never branch on georeferencing model outside that
   boundary.
3. Add the multi-job completion gate (SPEC.md FR3): refuse export with a specific,
   actionable error until every real annotation job for the task is complete.

**Verify:**
- Round-trip a known raster's annotations through export and confirm exact
  coordinate matches against independently-computed values (not just "it didn't
  crash").
- If you support more than one georeferencing model, verify **cross-model**
  agreement for the same physical scene where possible (e.g. a GCP-fitted raster and
  an RPC raster covering the same real location should export the same point to a
  documented tolerance) — this is a substantially stronger check than either model's
  internal self-consistency.
- Confirm the completion gate actually blocks with an in-progress job present, then
  actually succeeds once every job is marked complete.

## Phase C — Coordinate reconstitution: import

**Goal:** a geographic feature collection imports back into correct tile-pixel
annotations, whether drawn tile-by-tile or against the whole raster's extent, and
accepts classes the target task hasn't seen before.

**Build:**
1. Fast-path import (feature fits one tile) first; add the whole-raster clipping
   fallback (TECHSPEC.md §3.4) once that works.
2. Add auto-registration of unrecognized classes (TECHSPEC.md §3.5), being explicit
   in your own code's documentation that this is a deliberate departure from however
   the platform's *other* built-in importers behave (they typically reject an
   unknown class outright) — a future maintainer should not "fix" this back to
   strict validation without knowing it was intentional.
3. **Budget real time for the prefetch-cache gotcha (§7.2).** In the reference build
   this specific gap took two separate fix attempts: the first covered only one
   import code path (job-level), and a second, independent report of the *same*
   underlying symptom through a *different* code path (task-level import) revealed
   the first fix was incomplete, not wrong. The durable fix was to make the
   underlying class-lookup mechanism always bypass the prefetched relation
   everywhere it's constructed, not to patch call sites one at a time as they're
   reported. If your platform has more than one import entrypoint (job-level vs.
   task-level, bulk vs. single, etc.), test *each one independently* against this
   exact scenario — passing on one path is not evidence the others are fine.

**Verify:**
- Import a feature that fits one tile; import one that spans several; confirm a
  concave shape split by a tile boundary produces the right number of disjoint
  pieces.
- Import a feature naming a class the task has never seen; confirm it's created;
  import the same class name again; confirm no duplicate.
- Drive this through **every** import entrypoint your platform exposes (not just the
  one you tested first), specifically re-testing the prefetch-cache scenario on
  each.
- Confirm via whatever audit/event log the platform keeps that a test import created
  and removed exactly what you expect, nothing else — cheap insurance against a
  subtle over-broad side effect.

## Phase D — Toolbar: send to processing engine

**Goal:** an authorized user can send a job's current annotations to the task's
configured engine and see accurate live status.

**Build:**
1. `ProcessingEngineConfig` (TECHSPEC.md §2.4) with task-overrides-project
   resolution.
2. The send endpoint (§4.3): create the `MLProcessingRequest` row, return `202`
   immediately, enqueue the actual outbound call on a background worker — reject a
   second send while one is unfinished for the same job.
3. The outbound call itself (§4.4): build the payload, sign it, POST to the
   engine, handle either an immediate synchronous result or a `202`-plus-later-
   callback.
4. Frontend: a toolbar action, disabled/spinner while a request is unfinished for
   the open job, a status panel listing past/current requests, and a poll against
   the status endpoint (§4.6).

**Verify against a real (or realistic stub) engine, not just a synthetic HTTP mock:**
- Confirm the outbound payload is correctly signed and well-formed.
- Confirm a second send attempt is rejected while one is unfinished.
- Confirm the frontend's disabled/spinner state and eventual status transition
  actually reflect the request's real state, including the failure and timeout
  cases (don't only test the happy path — a control stuck permanently disabled
  after a failure is a real, user-visible bug class).

## Phase E — Callback + merge

**Goal:** the engine's response (sync or async) merges into the job's annotations
without clobbering concurrent human edits.

**Build:**
1. The callback endpoint (§4.5): verify the signature *before* touching anything
   else; make it idempotent on `request_id`.
2. Merge logic per SPEC.md FR7: leave untouched shapes alone, update engine-revised
   shapes in place, insert new engine-proposed shapes as visually-distinguished,
   unconfirmed.
3. **Route the merge through the platform's real annotation-write path** (its own
   validation, undo/redo history, change-tracking), not a simplified direct-write
   fallback, if at all possible in your environment. The reference build explicitly
   used a smaller, honestly-documented fallback here (see this file's §7 item 5) only
   because the real path pulled in dependencies unavailable in its sandbox — if
   your environment has no such constraint, don't reproduce that shortcut; wire
   through the real path from the start.

**Verify:**
- A duplicate callback for an already-resolved request is a no-op, confirmed by
  checking nothing changed a second time (not just that it returned `200`).
- Make a manual edit to a job while a mock engine's callback is pending; confirm
  the manual edit survives the merge.

## Phase F — Frontend correctness: live class-selector state

**Goal:** every shape-type-specific class selector reflects the live class list at
all times, including for a selector opened *before* a class was added.

**Build, in this order, verifying each before moving to the next** (the reference
build found each of these only by testing the *previous* fix through a more
realistic path than the one used to verify it, three times in a row — plan test
passes accordingly rather than assuming one test methodology covers every gap):

1. Whatever client-side object represents "the job" needs a way to re-fetch its own
   class list on demand, not just at construction (§7.5).
2. Whatever code path runs after an import completes must call that re-fetch, and
   must do so **before** any step that rebuilds annotation objects from a fresh
   server response using the current class list (§7.5's ordering gotcha) — get this
   order right the first time rather than discovering the crash it causes later.
3. Every shape-type-specific selector component must recompute its filtered class
   list whenever the underlying class list changes, not only when the component is
   first constructed (§7.5's main gotcha) — and this must be true for *every* shape
   type's selector instance, not just the one you happen to test with.

**Verify, escalating realism at each step:**
- First, a direct/synthetic test: hold a reference to an already-constructed
  client-side job object, create a class server-side directly (bypassing the UI),
  call the re-fetch method, confirm the object picks it up.
- Then, a synthetic-but-in-app test: dispatch the same client-side state-update
  action your real code path dispatches, with a selector already open, and confirm
  it updates live.
- Finally, the **actual** end-to-end path a real user would take (the real upload
  modal, real clicks, real file attachment) — the reference build's synthetic tests
  at the prior two steps both passed while a real crash still existed on this last,
  most-realistic path, caused by the exact ordering issue in step 2 above. Don't
  consider this phase done until you've tested the real path, not a stand-in for it.
- If UI test automation in your environment proves unreliable against the specific
  form components involved (a real issue encountered here with a popular admin-UI
  component library's controlled Select/Upload inputs not reliably responding to
  synthetic DOM events), don't keep fighting it indefinitely — a direct-mechanism
  test against the real running bundle (as in the "first" bullet above) is legitimate
  verification of record for the parts automation can't reach, *as long as* the
  higher-realism passes above still happen for the parts it can.

## Phase G — Hardening (do before any production rollout)

In priority order:

1. **Real object-level permissions.** Replace any interim "logged in + a hand-rolled
   visibility check" with the platform's actual permission/policy framework
   (TECHSPEC.md §7.4) — encode the real rule ("job assignee, task owner/assignee, or
   project owner/assignee may view; job assignee or task/project owner may create",
   or whatever your organization's actual rule is) in whatever policy mechanism the
   platform's *other* first-party apps already use, for consistency and so a future
   platform-wide permission change doesn't silently miss this feature.
2. **Route the engine-merge through the platform's real annotation-write path** if
   Phase E used a fallback (see Phase E's build note).
3. **Timeout sweep.** Add whatever periodic-execution mechanism the platform
   supports (TECHSPEC.md §7.6) to transition stuck `pending`/`processing` requests to
   `timed_out` once their configured timeout elapses — without this, a request whose
   callback never arrives stays "processing" forever from the UI's perspective.
4. **Absolute callback/image URLs.** If your outbound call runs in a background
   worker with no access to the original HTTP request (a common setup), don't emit
   relative URLs for `callback_url`/`image_url` — an external engine can't resolve
   them. Capture the server's own base URL via configuration, or thread the real
   request's host through into the enqueued job's arguments.
5. **`RasterTaskConfig` (or equivalent) actually populated**, if you deferred this
   during Phase A, so re-tiling/debugging a tile grid's shape is reproducible from
   stored settings rather than needing to be re-derived.
6. **Cloud-storage-backed raster detection**, if your uploads can come from remote
   storage not locally readable at ingestion time — decide explicitly whether to
   support tiling in that case or to document the fallback-to-ordinary-image
   behavior, rather than leaving it an accidental gap.
7. **Multi-task splitting** for scenes too large for one task's tile grid to be a
   reasonable unit of work, if your real data needs it (SPEC.md explicitly defers
   this as a policy decision, not a hard blocker).
8. **DEM draping for RPC height**, if survey-grade (not just annotation-grade)
   accuracy is ever required on terrain with real relief.

## Testing strategy summary

- **Phase 0's two libraries**: pure unit tests, no live stack, fast, run in CI on
  every change. This is your highest-value, cheapest-to-maintain coverage — keep it
  that way; don't let it grow a framework dependency by accident.
- **Everything touching the host platform's real ORM/permission/import machinery**:
  integration-test against a real instance of the platform (real DB, real queue,
  real HTTP), not a curated/minimal settings module standing in for it — several
  real bugs in the reference build (the AUTH_USER_MODEL mismatch, the stale-prefetch
  bugs, the manifest-generation gap, the frontend ordering crash) were specifically
  the kind that a narrower test harness could not have caught, because they only
  exist in the interaction between this code and the platform's actual, full
  configuration.
- **Nonlinear/nonlocal correctness (e.g. RPC)**: validate away from the model's own
  reference point, and cross-validate against an independent source of ground truth
  where one exists (TECHSPEC.md §3.3) — a self-consistency round-trip test is not
  sufficient on its own.
- **Frontend live-update correctness**: test with the relevant UI element already
  open/mounted *before* triggering the underlying state change, not only freshly
  opened afterward — the "already open" case is where staleness bugs hide.
- **Don't trust a build/deploy notification's "completed" framing** as proof the
  build actually succeeded — confirm via the artifact itself (e.g. the served
  bundle's timestamp, or grepping it for your actual change) before re-testing
  against a redeployed instance. A background build can report success while an
  underlying step (e.g. a package-manager network timeout) actually failed.

## Known gaps checklist (carry into your own backlog)

- [ ] Real IAM/OPA (or platform-equivalent) permission policy, replacing any interim
      hand-rolled check (Phase G item 1).
- [ ] Engine-merge routed through the platform's real annotation-write path, if not
      done from the start (Phase G item 2).
- [ ] Timeout sweep implemented (Phase G item 3).
- [ ] Absolute (not relative) callback/image URLs from the background worker
      (Phase G item 4).
- [ ] Per-task tiling-config table actually populated, if deferred (Phase G item 5).
- [ ] Explicit decision + documented behavior for cloud-storage-backed raster
      detection (Phase G item 6).
- [ ] Multi-task splitting for oversized scenes, if your data needs it
      (Phase G item 7).
- [ ] DEM draping for RPC height, if survey-grade accuracy is ever required
      (Phase G item 8).
- [ ] No true multi-resolution/deep-zoom viewer exists for rasters too large even for
      single-frame mode — out of scope per SPEC.md §5, but worth flagging to
      stakeholders explicitly rather than letting it be discovered as a surprise
      limitation.

## 9. Reference mapping (this specific build only — not a stable contract)

Where each capability landed in the one fork this was actually built against
(`jimmy82/cvat`, branch `feature/geotiff-geospatial-integration`). Useful as a
starting point for reading real code; **do not** assume these paths exist in a
different CVAT version — re-locate each by capability (TECHSPEC.md §7), not by path.

| Capability | Where it lived here |
|---|---|
| Tiling/coordinate-transform libraries | `cvat/apps/geospatial/ingestion.py`, `transforms.py`, `rpc.py` |
| Frame-source integration | `cvat/apps/geospatial/media_extractor.py` (`GeoTiffTileReader`), registered into `cvat.apps.engine.media_extractors.MEDIA_TYPES` |
| Persistence models | `cvat/apps/geospatial/models.py` (`RasterSource`, `RasterTile`, `RasterTaskConfig`) |
| Django-facing bridge / dispatch boundary | `cvat/apps/geospatial/services.py` |
| Dataset format registration + import/export | `cvat/apps/geospatial/dataset_io.py`, registered in `cvat.apps.dataset_manager`'s format registry |
| Geo-frame lookup endpoint | `cvat/apps/geospatial/views.py` (`GET /api/tasks/<id>/geospatial/frames/`) |
| Prefetch-cache fix | `cvat.apps.dataset_manager.task.JobAnnotation` (core CVAT file, not this app) |
| Engine config + request models | `cvat/apps/ml_processing/models.py` |
| Send/status/callback endpoints | `cvat/apps/ml_processing/views.py`, `urls.py` |
| Outbound-call worker | `cvat/apps/ml_processing/rq.py` |
| Merge logic (with documented fallback) | `cvat/apps/ml_processing/merge.py` |
| Wiring checklist for this app | `cvat/apps/ml_processing/INTEGRATION.md` |
| Toolbar / status panel | `cvat-ui/src/components/annotation-page/top-bar/processing-*` |
| Live class-list re-fetch | `cvat-core/src/session.ts` + `session-implementation.ts` (`Job.fetchLabels()`) |
| Import-completion ordering fix | `cvat-ui/src/actions/import-actions.ts`, `actions/annotation-actions.ts` (`refreshJobLabelsAsync`) |
| Per-shape-type selector live update | `cvat-ui/src/containers/annotation-page/standard-workspace/controls-side-bar/draw-shape-popover.tsx` |
| Live geocoordinate status bar / ruler tool | `cvat-ui/src/components/annotation-page/canvas/views/canvas2d/canvas-wrapper.tsx` |
| Full narrative history, bug-by-bug | `GEOSPATIAL_INTEGRATION_SESSION_SUMMARY.md` (repo root of the `cvat` checkout) |
