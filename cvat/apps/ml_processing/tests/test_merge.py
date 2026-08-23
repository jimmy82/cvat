# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest

from cvat.apps.engine.models import LabeledShape, SourceType
from cvat.apps.ml_processing.merge import merge_engine_annotations, serialize_job_annotations

from .factories import make_job, make_label, make_task


@pytest.mark.django_db
class TestMergeEngineAnnotations:
    def test_inserts_only_engine_marked_shapes(self):
        task = make_task()
        job = make_job(task)
        make_label(task, "car")

        payload = {
            "shapes": [
                {
                    "source": "engine",
                    "frame": 0,
                    "label": "car",
                    "type": "rectangle",
                    "points": [0, 0, 10, 10],
                },
                {
                    # No "source": "engine" marker -- must be skipped entirely.
                    "frame": 1,
                    "label": "car",
                    "type": "rectangle",
                    "points": [1, 1, 2, 2],
                },
            ]
        }

        result = merge_engine_annotations(job, payload)

        assert result == {"created": 1, "skipped": 1}
        shapes = list(job.labeledshape_set.all())
        assert len(shapes) == 1
        assert shapes[0].frame == 0
        assert shapes[0].source == SourceType.AUTO

    def test_never_touches_existing_human_drawn_shapes(self):
        task = make_task()
        job = make_job(task)
        label = make_label(task, "car")

        human_shape = LabeledShape.objects.create(
            job=job,
            label=label,
            frame=2,
            type="rectangle",
            points=[5, 5, 6, 6],
            source=SourceType.MANUAL,
            group=0,
        )

        payload = {
            "shapes": [
                {
                    "source": "engine",
                    "frame": 3,
                    "label": "car",
                    "type": "rectangle",
                    "points": [0, 0, 1, 1],
                }
            ]
        }
        merge_engine_annotations(job, payload)

        human_shape.refresh_from_db()
        assert human_shape.points == [5, 5, 6, 6]
        assert human_shape.source == SourceType.MANUAL
        assert job.labeledshape_set.count() == 2

    def test_skips_unresolvable_label(self):
        task = make_task()
        job = make_job(task)
        # No labels created at all.

        payload = {
            "shapes": [
                {
                    "source": "engine",
                    "frame": 0,
                    "label": "does-not-exist",
                    "type": "rectangle",
                    "points": [0, 0, 1, 1],
                }
            ]
        }
        result = merge_engine_annotations(job, payload)
        assert result == {"created": 0, "skipped": 1}
        assert job.labeledshape_set.count() == 0

    def test_handles_empty_payload(self):
        task = make_task()
        job = make_job(task)
        assert merge_engine_annotations(job, None) == {"created": 0, "skipped": 0}
        assert merge_engine_annotations(job, {}) == {"created": 0, "skipped": 0}


@pytest.mark.django_db
class TestSerializeJobAnnotations:
    def test_exports_existing_shapes(self):
        task = make_task()
        job = make_job(task)
        label = make_label(task, "car")
        LabeledShape.objects.create(
            job=job,
            label=label,
            frame=0,
            type="rectangle",
            points=[1, 2, 3, 4],
            source=SourceType.MANUAL,
            group=0,
        )

        data = serialize_job_annotations(job)
        assert data["tags"] == []
        assert data["tracks"] == []
        assert len(data["shapes"]) == 1
        shape = data["shapes"][0]
        assert shape["label"] == "car"
        assert shape["points"] == [1, 2, 3, 4]
        assert shape["source"] == "manual"

    def test_empty_job_has_no_shapes(self):
        task = make_task()
        job = make_job(task)
        assert serialize_job_annotations(job) == {"shapes": [], "tags": [], "tracks": []}
