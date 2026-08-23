# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Applying annotations coming back from the external ML engine to a Job, and
serializing a Job's current annotations to send out to the engine.

*** HONESTY NOTE ABOUT WHAT'S IMPLEMENTED HERE VS. WHAT PRODUCTION NEEDS ***

CVAT's real partial-annotation-update path is `cvat.apps.dataset_manager.task`:
`JobAnnotation.put()/.update()/.create()` (driven by `patch_task_data()`, the same
function `JobViewSet.annotations()` / `TaskViewSet.annotations()` call for
PUT/PATCH /api/.../annotations). That path was inspected for this app (see
`cvat/apps/dataset_manager/task.py`), but wiring `merge_engine_annotations` through it
for real was judged too risky to land untested in this sandbox:

  - `JobAnnotation.update()`/`.create()` call
    `cvat.apps.events.handlers.handle_annotations_change()`, which records analytics
    events and expects the `cvat.apps.events` app (and its downstream vector/analytics
    client wiring) to be installed and configured -- machinery this self-contained app
    has no business standing up, and that isn't present in this task's minimal test
    settings (see /tmp/pytest_django_settings.py).
  - Fully exercising that path here would mean either mocking large parts of
    `dataset_manager`/`events` (making the test dishonestly "green" without actually
    proving the integration works) or pulling in ~40 apps' worth of dependencies
    (including the pinned `datumaro` fork) that this network-restricted sandbox can't
    install.

So what's implemented instead, and clearly scoped:

  - `serialize_job_annotations()` exports only `LabeledShape` rows (no tags, no
    tracks, no track-interpolation) into a small documented dict shape -- NOT the
    real CVAT annotation export format used by `dataset_manager`/datumaro.
  - `merge_engine_annotations()` only *inserts* new `LabeledShape` rows for engine
    payload entries carrying `"source": "engine"`; it never touches existing shapes
    (human-drawn or otherwise), and marks inserted shapes with
    `source=SourceType.AUTO` so they read as unconfirmed/machine-generated in the UI
    review flow, since CVAT annotations have no separate "pending" flag.

See INTEGRATION.md in this app for exactly what a production wire-up should call
in `dataset_manager` instead, and what would need to be true of the environment to
verify it for real.
"""

from __future__ import annotations

from django.db import transaction

from cvat.apps.engine.models import Job, Label, LabeledShape, SourceType


def _resolve_label(job: Job, label_name: str | None) -> Label | None:
    if not label_name:
        return None

    task = job.segment.task
    qs = Label.objects.filter(name=label_name)

    label = qs.filter(task=task).first()
    if label is None and task.project_id:
        label = qs.filter(project_id=task.project_id).first()

    return label


def serialize_job_annotations(job: Job) -> dict:
    """Simplified, `LabeledShape`-only export of a Job's current annotations.

    NOT the real CVAT annotation export format (see module docstring) -- omits tags,
    tracks/interpolation, and skeleton element structure. Good enough to give the
    external engine a "what's already here" snapshot for shapes; a production version
    should instead reuse `cvat.apps.dataset_manager.bindings.JobData` (the same class
    backing `GET /api/jobs/{id}/annotations`) so the wire format matches exactly.
    """
    shapes = []
    queryset = job.labeledshape_set.select_related("label").prefetch_related(
        "attributes__spec"
    )
    for shape in queryset.iterator(chunk_size=2000):
        shapes.append(
            {
                "id": shape.id,
                "frame": shape.frame,
                "label": shape.label.name,
                "type": shape.type,
                "points": list(shape.points),
                "occluded": shape.occluded,
                "outside": shape.outside,
                "z_order": shape.z_order,
                "rotation": shape.rotation,
                "source": str(shape.source),
                "attributes": [
                    {"name": attr.spec.name, "value": attr.value}
                    for attr in shape.attributes.all()
                ],
            }
        )

    return {"shapes": shapes, "tags": [], "tracks": []}


def merge_engine_annotations(job: Job, annotations_payload: dict | None) -> dict:
    """Apply annotations returned by the external engine to `job`.

    Only entries in `annotations_payload["shapes"]` carrying `"source": "engine"` are
    applied, and they are always *inserted* as new shapes (never matched against /
    overwriting existing rows) -- human-drawn shapes without that marker are left
    completely untouched. Inserted shapes are stored with `source=SourceType.AUTO` so
    they're visually distinguishable as machine-generated/unconfirmed.

    Returns a result_summary dict, e.g. `{"created": 3, "skipped": 1}`, suitable for
    `MLProcessingRequest.mark_succeeded()`.
    """
    created = 0
    skipped = 0

    shapes_payload = (annotations_payload or {}).get("shapes") or []

    with transaction.atomic():
        for shape_data in shapes_payload:
            if shape_data.get("source") != "engine":
                skipped += 1
                continue

            label = _resolve_label(job, shape_data.get("label"))
            if label is None:
                skipped += 1
                continue

            try:
                LabeledShape.objects.create(
                    job=job,
                    label=label,
                    frame=shape_data["frame"],
                    type=shape_data["type"],
                    points=shape_data.get("points") or [],
                    occluded=shape_data.get("occluded", False),
                    outside=shape_data.get("outside", False),
                    z_order=shape_data.get("z_order", 0),
                    rotation=shape_data.get("rotation", 0),
                    group=shape_data.get("group") or 0,
                    source=SourceType.AUTO,
                )
            except (KeyError, TypeError):
                skipped += 1
                continue

            created += 1

    return {"created": created, "skipped": skipped}
