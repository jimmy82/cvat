# Spec: GeoTIFF Annotation + External ML Engine Integration

**Status:** Functional requirements, distilled from the original design
(`CVAT_GeoTIFF_ML_Integration_Design.md`) and everything learned building and running
it against a real stack (`GEOSPATIAL_INTEGRATION_SESSION_SUMMARY.md`).

**How to use this document:** this is the *what and why*, deliberately free of any
particular CVAT version's file layout, API paths, or internal class names. Anyone
re-implementing this — against a newer/older CVAT, a differently-forked CVAT, or a
different annotation tool entirely — should be able to build a conforming system from
this document plus [TECHSPEC.md](TECHSPEC.md) (the *how*, still tool-agnostic where
possible) without reading a single line of the current implementation. The current
implementation's specific file paths are recorded only in
[IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md), clearly marked as illustrative.

## 1. Problem statement

An annotation platform (CVAT, in the reference implementation) needs to let annotators
draw shapes on **large, georeferenced raster imagery** (GeoTIFFs, often hundreds of MB
to multi-GB, from satellite or aerial sensors) and needs a way to hand a job's
annotations to an **external Python ML engine** for automated processing, then bring
the engine's proposed changes back for human review — all without annotators losing
the geographic meaning of what they draw.

Two problems compound if solved naively:

1. Most annotation platforms' image pipelines decode a whole image into memory before
   display (commonly via Pillow/PIL). Georeferenced rasters at useful resolution are
   far larger than that pipeline was designed for, and PIL specifically refuses
   (decompression-bomb guard) or exhausts memory above roughly 179 million pixels.
2. Annotations are naturally drawn in pixel space on whatever's on screen. Without a
   deliberate coordinate-reconstitution step, that pixel-space data has no
   correspondence to the real world once tiling, cropping, or resampling has happened
   between the source raster and what the annotator actually saw.

## 2. Actors

- **Annotator** — draws/edits shapes on tiled raster imagery inside a Job.
- **Reviewer** — accepts or rejects shapes an external ML engine proposed.
- **Task/Project owner (or organization admin)** — uploads rasters, configures tiling
  parameters, configures which external engine a Task/Project talks to.
- **External ML processing engine** — an independently-operated HTTP service. Not
  assumed to be written or controlled by the same team; the contract with it must be
  self-describing (Section 5) rather than relying on shared code.
- **The platform itself** — the annotation tool being extended (CVAT in this instance),
  treated as a set of *integration seams* this system plugs into rather than something
  this system owns or may freely rewrite.

## 3. Functional requirements

### FR1 — Ingest and tile a large georeferenced raster

- **Given** a task is created with a GeoTIFF (or other GDAL-readable georeferenced
  raster) as its source data,
  **when** ingestion runs,
  **then** the raster must be split into a grid of fixed-size, uniformly-shaped tiles
  using **windowed reads** — at no point should the whole raster be decoded into memory
  at once.
- Tile size and inter-tile overlap must be **configurable per task**, with sensible
  defaults. Overlap must be strictly smaller than tile size.
- Edge tiles that would extend past the raster's bounds must be **padded**, not
  resized, so every tile has identical pixel dimensions.
- Each tile becomes one addressable unit of annotation work (a "frame", in whatever
  vocabulary the host platform uses for one displayable image in a task) laid out in a
  **deterministic, reconstructible order** (row-major is sufficient and is what the
  reference implementation uses).
- The platform's **existing work-distribution mechanism** (splitting a task's frames
  across multiple jobs/annotators) must be reusable **unmodified** on top of the tile
  sequence — this system supplies a new *source* of frames, not a new distribution
  scheme.
- A raster small enough not to need tiling should be usable as a single frame instead,
  still capped by whatever hard limit the host platform's own image pipeline imposes.
- **Must not resample or warp the source raster's pixel data** for any reason
  (including for georeferencing models — see FR2) unless the tile size legitimately
  requires re-encoding as a different container format for efficient windowed access
  (see "Cloud-Optimized" note below) — pixel *values* must never change as a side
  effect of ingestion.
- Acceptable ingestion-time cost: re-encoding an uploaded raster into a windowed-read-
  friendly layout (e.g. Cloud-Optimized GeoTIFF) is acceptable as a one-time cost at
  upload, since it doesn't change pixel values, band count, or dtype.

### FR2 — Reconstitute real-world coordinates for annotations

- Every annotation drawn on a tile must be convertible, on demand, to real-world
  geographic coordinates (and back), regardless of which of the raster's supported
  georeferencing models applies:
  1. A direct affine transform + CRS (the common case).
  2. Ground Control Points (GCPs) — a linear transform must be *fit* from them once, at
     ingestion time, and used identically to case 1 from then on.
  3. Rational Polynomial Coefficients (RPC) — a **nonlinear** model with no direct
     affine equivalent and, critically, **no closed-form inverse** (ground → image is
     direct evaluation; image → ground requires iterative solving).
- Whichever georeferencing model applies, **every consumer of coordinates** (export,
  import, any live geo-readout in the UI, any distance/measurement tool) must dispatch
  through one shared conversion boundary rather than each re-implementing
  affine-vs-RPC branching — a new georeferencing model added later should require
  touching that boundary alone.
- Export: annotations across an entire task must be exportable as a single
  geographic-coordinate feature collection (e.g. GeoJSON), merging every job's
  annotations, in the raster's real-world CRS (WGS84 lon/lat is an acceptable default
  and is what RPC's own spec fixes it to regardless of the raster's stated CRS tag).
- Import: a geographic-coordinate feature collection must be importable back into
  tile-pixel annotations, including:
  - A feature drawn against a **single tile's** extent (fast path).
  - A feature drawn against the **whole raster's** extent, spanning multiple tiles —
    it must be clipped and split into one shape per tile it overlaps, not rejected.
- One explicitly acceptable lossy conversion: a rotated rectangle has no rectangular
  representation in most geographic vector formats (GeoJSON polygons are axis-agnostic
  point lists) — round-tripping a rectangle through export/import may legitimately
  come back as a polygon, not a rectangle. This must be a **documented**, not silent,
  lossy conversion.

### FR3 — Multi-job export gating

- Because one raster's tiles are typically distributed across several jobs/annotators,
  "export the task's geographic annotations" must mean **the whole task's completed
  result**, not a partial in-progress snapshot.
- Export must **refuse with a clear, specific error** (naming the job(s) still
  outstanding) until every annotation job belonging to the task is marked complete.
  Non-annotation jobs that exist for QA/consensus purposes (ground truth, replica jobs)
  should be excluded from this gate.

### FR4 — Accept previously-unknown classes on import

- A geographic feature collection handed to the platform for import (e.g. produced by
  an external tool, or hand-authored by a GIS analyst) **must not be required to only
  use classes already declared on the task** — most annotation platforms reject an
  unrecognized class name outright, which defeats bulk import from any external
  source.
- An unrecognized class name must be **auto-registered** as a new class on the
  target task (or its project) the first time it's seen, then used for the rest of
  that import and by all subsequent draws in the same session, with no further
  intervention.
- This must be **idempotent**: importing the same previously-unknown class name twice
  must not create a duplicate class definition.
- Newly created classes must **not retroactively relabel** any existing annotation —
  they apply going forward only.

### FR5 — Toolbar action: send a job's annotations to an external ML engine

- From within a Job's annotation view, an authorized user must be able to trigger
  sending that job's **current annotations** to an external processing engine
  configured for that Task (or inherited from its Project).
- The action must be **asynchronous end to end**: triggering it must not block the UI
  waiting for the engine's actual processing to finish; the caller gets an immediate
  acknowledgment and a request identifier to poll or watch.
- Exactly one processing request may be in flight per Job at a time; a second attempt
  while one is outstanding must be rejected (not silently queued or duplicated).
- The submitting user must get visible, continuously-accurate status: pending,
  processing, succeeded, failed, or timed out — never a control that looks inert while
  work is actually happening, and never one that looks "in progress" forever after a
  failure.
- A configurable **timeout** must exist; a request that never gets a callback within
  it must eventually be marked timed-out rather than staying "pending"/"processing"
  indefinitely.

### FR6 — Engine round trip contract

- The payload sent to the engine must include, per frame in the job: enough for the
  engine to fetch the frame's image data, and (for georeferenced tasks) that frame's
  geographic metadata — so the engine may operate in pixel space, geographic space, or
  both, at its own discretion.
- The engine must be able to answer either **synchronously** (small/fast models, reply
  in the same HTTP response) or **asynchronously** (POST a signed callback later) —
  the contract must accommodate both without the caller needing to know in advance
  which the engine will do.
- The callback (or synchronous reply) must be **cryptographically signed** using a
  secret shared out-of-band per Task/Project engine configuration, and the receiving
  side must verify the signature before acting on the payload.
- The callback must be **idempotent**: a duplicate callback for a request already
  resolved must be acknowledged without applying a second time.

### FR7 — Reviewing engine-proposed changes

- Annotations the engine proposes must be merged into the job **without silently
  discarding concurrent human edits** made while the engine was processing:
  - Shapes the engine didn't touch must be left alone.
  - Shapes the engine explicitly revised must update in place.
  - Entirely new shapes the engine proposes must land as **visually distinguished,
    unconfirmed** annotations (not indistinguishable from human-confirmed ones) until
    a human explicitly accepts or discards each one.
- A reviewer must be able to see, per job, the history of processing requests and open
  a review view for a succeeded one.

### FR8 — Live class-selector correctness

- Whatever the platform's UI mechanism is for choosing which class to draw next (a
  class-selector control, one per shape-type tool), that control must always reflect
  the **current, live** set of classes available for that specific shape type — not a
  snapshot taken whenever that control first appeared on screen. If a class becomes
  usable partway through a session (imported, auto-registered per FR4, or added
  through ordinary class management), every shape-type tool's selector must pick it up
  without requiring a page reload, and consistently across every shape type — not just
  the one a given user happens to test.
- If a platform restricts certain classes to certain shape types (e.g. a class only
  usable as a polygon, not a rectangle), that restriction is intentional scoping, not a
  bug, and must be clearly distinguishable (in documentation and in how the UI
  communicates it) from a stale/not-yet-refreshed list.

## 4. Non-functional requirements

- **Memory bound on ingestion**: peak memory during ingestion must not scale with
  total raster size, only with tile size — this is the entire reason FR1 exists.
- **No pixel-value side effects**: nothing in this system may alter the radiometric
  content of the source raster (see FR1's resampling constraint) — a downstream
  consumer must be able to trust that a tile's pixel values are the original raster's,
  verbatim, windowed.
- **Security**: the outbound-to-engine and inbound-callback channels must both be
  signed (shared-secret HMAC or equivalent) and should additionally sit behind network-
  level trust (allowlist / private segment) between the platform and the engine — a
  leaked secret alone should not be sufficient to inject fabricated annotations if
  network access is also restricted. Frame image URLs handed to the engine should be
  short-lived and scoped to that specific request's frames, not general-purpose
  session credentials.
- **Authorization**: every new endpoint this system adds must respect the host
  platform's existing object-level permission model for who may view/edit a given
  job's annotations — a hand-rolled placeholder check is acceptable only as an
  explicitly-flagged interim step (see IMPLEMENTATION_GUIDE.md §7).
- **Reliability**: outbound calls to the engine should retry with backoff on
  connection-level failure only, never on an application-level failure the engine
  explicitly reported (that must surface to the human, not be silently retried away).
- **Portability**: nothing in the core tiling/coordinate-transform logic should require
  the host platform's web framework, ORM, or request/response cycle — it must be
  usable as a plain library, testable with no live stack at all (see
  IMPLEMENTATION_GUIDE.md §8 on testing strategy).

## 5. Explicitly out of scope (for this iteration)

- A true multi-resolution / deep-zoom ("whole-slide-imaging"-style) viewer for rasters
  too large even for single-frame mode. Ordinary fixed-size tiling is the only
  supported strategy; if the host platform has no tile-pyramid canvas, building one is
  its own separate project.
- Splitting one raster's tile grid across more than one Task (e.g. under a shared
  Project) for extremely large scenes — a real need for very large scenes, deferred as
  a policy decision rather than a hard architectural blocker.
- Draping RPC-derived ground coordinates onto a real digital elevation model (DEM) —
  RPC height is assumed flat (sea level / a fixed reference), which is a known,
  bounded accuracy limitation acceptable for annotation purposes on moderate terrain,
  not for survey-grade measurement.
- Detecting a georeferenced raster's type when it lives only in remote/cloud storage
  not yet locally readable at ingestion time (falls back to ordinary non-tiled image
  handling — a known limitation, not a crash).
- A formal policy-language (e.g. Rego/OPA) authorization ruleset — FR-level
  requirements for authorization (Section 4) must hold, but writing the actual policy
  in whatever policy engine the host platform uses is implementation work, not spec
  work, and is called out as a required follow-up in IMPLEMENTATION_GUIDE.md.
- A periodic sweep that transitions a stuck, never-called-back request to "timed out"
  automatically — FR5 requires the *state* to exist and be reachable, but the
  scheduled job that flips it is deferred, explicitly, as a known gap.

## 6. Glossary

- **Tile / frame** — one fixed-size rectangular window of the source raster's pixel
  grid, treated by the host annotation platform as one ordinary displayable image.
- **COG (Cloud-Optimized GeoTIFF)** — a GeoTIFF layout with internal tiling and an
  overview (thumbnail pyramid) that makes windowed reads and quick previews cheap.
  Preferred, not required, input layout.
- **GCP (Ground Control Point)** — a known correspondence between a raster's pixel
  location and a real-world coordinate, used (typically several at once) to fit a
  transform when the raster carries no direct affine georeferencing.
- **RPC (Rational Polynomial Coefficients)** — a nonlinear georeferencing model
  (ground ⟷ image via ratios of cubic polynomials) common in raw satellite/aerial
  products; per its governing spec (RPC00B), ground coordinates are always WGS84
  lon/lat regardless of any other CRS tag the file carries.
- **Job / segment / task** — used here in the generic sense any modern annotation
  platform uses: a Task holds a sequence of frames; a Job is a contiguous, assignable
  slice of that sequence; distribution of frames into jobs is a mechanism this system
  deliberately does not reinvent.

## 7. Acceptance summary (traceability)

| Requirement | How it's verified |
|---|---|
| FR1 tiling/memory bound | Ingest a real multi-hundred-MB raster; confirm tile count, dimensions, and edge padding; confirm process memory doesn't scale with file size. |
| FR2 coordinate round-trip | Export then re-import a known raster under each of the three georeferencing models; coordinates must match to a documented tolerance; cross-validate independently-derived ground-truth corners across georeferencing models for the *same physical scene*. |
| FR3 export gate | Attempt export with an in-progress job present (must be refused, naming the job); mark it complete; export must then succeed. |
| FR4 auto-registration | Import a feature naming an unknown class; confirm the class now exists and the shape carries it; re-import the same class name; confirm no duplicate class was created. |
| FR5/FR6 round trip | Submit a job to a stub/mock engine; confirm signed outbound payload; confirm signed callback is accepted and idempotent on retry; confirm timeout path when no callback arrives. |
| FR7 merge safety | Make a manual edit to a job while a (mock) engine request is outstanding; confirm the manual edit survives the merge and only engine-touched/engine-new shapes change. |
| FR8 live selector | Add a class mid-session (import or otherwise); confirm every already-open, shape-type-specific selector reflects it without a page reload. |
