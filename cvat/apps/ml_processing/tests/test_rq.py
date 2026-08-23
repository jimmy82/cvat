# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from cvat.apps.ml_processing.models import (
    MLProcessingRequest,
    MLProcessingRequestStatus,
    ProcessingEngineConfig,
)
from cvat.apps.ml_processing.rq import build_outbound_payload, send_to_engine
from cvat.apps.ml_processing.utils import SIGNATURE_HEADER, verify_signature

from .factories import make_job, make_label, make_task


@pytest.mark.django_db
class TestBuildOutboundPayload:
    def test_shape_matches_documented_contract(self):
        task = make_task()
        job = make_job(task)
        make_label(task, "car")
        ml_request = MLProcessingRequest.objects.create(job=job)

        payload = build_outbound_payload(ml_request)

        assert payload["request_id"] == str(ml_request.id)
        assert payload["job_id"] == job.id
        assert payload["task_id"] == task.id
        assert payload["callback_url"] == f"/api/ml-requests/{ml_request.id}/callback/"
        assert payload["annotations"] == {"shapes": [], "tags": [], "tracks": []}

        # segment is frames 0..4 inclusive -> 5 frame entries
        assert [f["frame"] for f in payload["frames"]] == [0, 1, 2, 3, 4]
        for frame_entry in payload["frames"]:
            assert frame_entry["image_url"].startswith(f"/api/jobs/{job.id}/data?type=frame")
            # no geospatial.RasterTile rows exist for this plain task -> all None
            assert frame_entry["pixel_window"] is None
            assert frame_entry["geotransform"] is None
            assert frame_entry["crs"] is None

    def test_json_serializable(self):
        task = make_task()
        job = make_job(task)
        ml_request = MLProcessingRequest.objects.create(job=job)

        payload = build_outbound_payload(ml_request)
        # Must round-trip through json.dumps exactly as send_to_engine does.
        json.dumps(payload)


@pytest.mark.django_db
class TestSendToEngine:
    def test_marks_failed_when_no_config(self):
        task = make_task()
        job = make_job(task)
        ml_request = MLProcessingRequest.objects.create(job=job)

        send_to_engine(str(ml_request.id))

        ml_request.refresh_from_db()
        assert ml_request.status == MLProcessingRequestStatus.FAILED
        assert "No enabled ML processing engine" in ml_request.error_message

    @patch("cvat.apps.ml_processing.rq.requests.post")
    def test_signs_request_and_marks_processing(self, mock_post):
        task = make_task()
        job = make_job(task)
        config = ProcessingEngineConfig.objects.create(
            task=task, base_url="https://engine.example.com/run", secret="s3cr3t"
        )
        ml_request = MLProcessingRequest.objects.create(job=job)

        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_post.return_value = mock_response

        send_to_engine(str(ml_request.id))

        mock_post.assert_called_once()
        call_args, call_kwargs = mock_post.call_args
        assert call_args[0] == config.base_url
        sent_body = call_kwargs["data"]
        assert call_kwargs["timeout"] == config.timeout_seconds

        signature_header = call_kwargs["headers"][SIGNATURE_HEADER]
        assert verify_signature(config.secret, sent_body, signature_header)

        ml_request.refresh_from_db()
        # Not resolved to a terminal state yet (202 ack, no callback-shaped body) --
        # stays PROCESSING until the callback endpoint is hit.
        assert ml_request.status == MLProcessingRequestStatus.PROCESSING

    @patch("cvat.apps.ml_processing.rq.requests.post")
    def test_synchronous_success_response_is_merged_immediately(self, mock_post):
        task = make_task()
        job = make_job(task)
        make_label(task, "car")
        ProcessingEngineConfig.objects.create(
            task=task, base_url="https://engine.example.com/run", secret="s3cr3t"
        )
        ml_request = MLProcessingRequest.objects.create(job=job)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "succeeded",
            "annotations": {
                "shapes": [
                    {
                        "source": "engine",
                        "frame": 1,
                        "label": "car",
                        "type": "rectangle",
                        "points": [0, 0, 1, 1],
                    }
                ]
            },
        }
        mock_post.return_value = mock_response

        send_to_engine(str(ml_request.id))

        ml_request.refresh_from_db()
        assert ml_request.status == MLProcessingRequestStatus.SUCCEEDED
        assert ml_request.result_summary == {"created": 1, "skipped": 0}
        assert job.labeledshape_set.count() == 1

    @patch("cvat.apps.ml_processing.rq.requests.post")
    def test_request_exception_marks_failed_without_raising(self, mock_post):
        task = make_task()
        job = make_job(task)
        ProcessingEngineConfig.objects.create(
            task=task, base_url="https://engine.example.com/run"
        )
        ml_request = MLProcessingRequest.objects.create(job=job)

        mock_post.side_effect = requests.exceptions.ConnectionError("connection refused")

        send_to_engine(str(ml_request.id))  # must not raise

        ml_request.refresh_from_db()
        assert ml_request.status == MLProcessingRequestStatus.FAILED
        assert "connection refused" in ml_request.error_message
