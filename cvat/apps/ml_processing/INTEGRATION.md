# Wiring `ml_processing` into the rest of CVAT

This app is self-contained by design (per the task that produced it, nothing outside
`cvat/apps/ml_processing/` was touched). This document lists every change someone
needs to make elsewhere in the CVAT codebase to actually wire it up.

## 1. `INSTALLED_APPS`

Add `"cvat.apps.ml_processing"` to `INSTALLED_APPS` in `cvat/settings/base.py`
(alongside the other first-party apps, e.g. right after `"cvat.apps.webhooks"`).

## 2. RQ queue: `cvat/settings/base.py`

Add a new queue name to the `CVAT_QUEUES` enum and a corresponding entry to
`RQ_QUEUES`, following the exact pattern of the existing `WEBHOOKS` entry:

```python
class CVAT_QUEUES(Enum):
    ...
    WEBHOOKS = "webhooks"
    ML_PROCESSING = "ml_processing"  # <-- add this
    ...

RQ_QUEUES = {
    ...
    CVAT_QUEUES.WEBHOOKS.value: {
        **REDIS_INMEM_SETTINGS,
        "DEFAULT_TIMEOUT": "25s",
    },
    CVAT_QUEUES.ML_PROCESSING.value: {  # <-- add this
        **REDIS_INMEM_SETTINGS,
        "DEFAULT_TIMEOUT": "4h",  # external engine jobs can run long
    },
    ...
}
```

This app currently hardcodes the literal queue name `"ml_processing"` in
`cvat/apps/ml_processing/rq.py` (`ML_PROCESSING_QUEUE_NAME`) since it has no access to
`cvat.settings.base.CVAT_QUEUES` without importing across the boundary this task
draws. Once the queue is added to `CVAT_QUEUES`, feel free to change
`ML_PROCESSING_QUEUE_NAME` to `settings.CVAT_QUEUES.ML_PROCESSING.value` for
consistency with the rest of the codebase.

## 3. URL routing: `cvat/urls.py`

Add, alongside the other conditionally-appended app URLconfs (e.g. right after the
`cvat.apps.webhooks.urls` include):

```python
urlpatterns.append(path("api/", include("cvat.apps.ml_processing.urls")))
```

This app's own `cvat/apps/ml_processing/urls.py` already defines the three routes at
paths relative to that include, i.e. the effective URLs become:

- `POST /api/jobs/<job_id>/ml-requests/` -> `SendToProcessingEngineView`
- `GET  /api/ml-requests/<uuid:pk>/` -> `MLProcessingRequestDetailView`
- `POST /api/ml-requests/<uuid:pk>/callback/` -> `MLProcessingCallbackView`

If preferred, these could instead be nested under `cvat/apps/engine/urls.py` as
sub-resources of the existing `jobs/<job_id>/...` routes -- the view code doesn't
care which URLconf includes it.

## 4. IAM / OPA permissions

`SendToProcessingEngineView` and `MLProcessingRequestDetailView` currently just use
DRF's `permission_classes = [IsAuthenticated]`, with a hand-rolled, deliberately
simple visibility check (`_user_can_view_job` in `views.py`) standing in for CVAT's
real object-level permissions. Production CVAT should instead define an
`OpenPolicyAgentPermission` subclass (e.g. `MLProcessingPermission`) following the
exact pattern in `cvat/apps/webhooks/permissions.py`'s `WebhookPermission`:

- A `Scopes` `StrEnum` (e.g. `CREATE`, `VIEW`).
- A `_get_scopes` classmethod mapping `(view.action, request.method)` to a scope.
- `self.url = settings.IAM_OPA_DATA_URL + "/ml_processing/allow"` in `__init__`.
- A matching Rego policy file under CVAT's `cvat/apps/iam/rules/` (mirroring
  `webhooks.rego`) that actually encodes the "job assignee, task owner/assignee, or
  project owner/assignee can view; job assignee or task/project owner can create" rule
  this app currently approximates with `_user_can_view_job`.

Writing real Rego rule files was explicitly out of scope for this task.

## 5. `merge.py` / `serialize_job_annotations`: production annotation integration

See the detailed docstring in `cvat/apps/ml_processing/merge.py`, but in short:

- **What's implemented and tested here:** a documented, simplified path that only
  ever *inserts* new `LabeledShape` rows carrying a `"source": "engine"` marker in
  the callback payload (marked `source=SourceType.AUTO` in the DB so they read as
  machine-generated/unconfirmed), and only exports `LabeledShape` rows (no tags,
  tracks, or attributes-format matching) as the outbound "current annotations"
  snapshot.
- **What production needs instead:** wire `merge_engine_annotations` through
  `cvat.apps.dataset_manager.task.JobAnnotation` (via `patch_task_data(job_id, data,
  PatchAction.CREATE)` for pure additions, or `.UPDATE` if the engine also revises
  existing engine-authored shapes) so annotation changes go through CVAT's real
  validation, undo/redo history, and change-tracking. Likewise, replace
  `serialize_job_annotations` with `cvat.apps.dataset_manager.bindings.JobData` (the
  class backing `GET /api/jobs/{id}/annotations`) so the wire format the external
  engine sees matches CVAT's real annotation schema exactly (attributes, tracks,
  skeletons, etc.)
- **Why this task didn't do that:** `JobAnnotation.create()/.update()` call
  `cvat.apps.events.handlers.handle_annotations_change()`, which needs the
  `cvat.apps.events` app (and its downstream analytics/vector client wiring)
  installed and configured -- infrastructure this self-contained app has no business
  standing up, and that isn't present in this task's sandboxed test settings (see
  `dev/sandbox_verification_settings/`, whose minimal `INSTALLED_APPS` excludes
  `cvat.apps.events`, `cvat.apps.quality_control`, and `cvat.apps.consensus`, several
  of which transitively require an exact pinned fork of `datumaro` not installable in
  this network-restricted sandbox). Wiring through the real path here would have
  meant either faking a large slice of `dataset_manager`/`events` behavior (making
  tests dishonestly "green") or pulling in ~40 apps' worth of dependencies this
  sandbox can't install -- so a smaller, honestly-scoped, actually-tested path was
  built instead.

## 6. Timeout sweep (not implemented)

`MLProcessingRequestStatus.TIMED_OUT` exists on the model and
`MLProcessingRequest.mark_timed_out()` is implemented, but nothing currently
transitions a stuck `PENDING`/`PROCESSING` request to `TIMED_OUT` after
`ProcessingEngineConfig.timeout_seconds` elapses with no callback. A production
deployment should add a periodic task (e.g. an RQ scheduler job, or a Django
management command run via cron) that scans for
`MLProcessingRequest.objects.unfinished()` rows older than their config's timeout and
calls `.mark_timed_out(...)` on them.

## 7. Absolute callback/image URLs

Both `build_outbound_payload`'s `callback_url` and `_build_frame_entries`'s
`image_url` are currently relative paths (`/api/ml-requests/.../callback/`,
`/api/jobs/{id}/data?...`), since `rq.py` runs in a worker process with no access to
the original request. Production code should instead capture the request's host
(e.g. via a `CVAT_BASE_URL` setting, or by threading `request.build_absolute_uri()`
through from `SendToProcessingEngineView.post()` into the enqueued job's arguments)
so the external engine gets fully-qualified URLs it can actually call back to.
