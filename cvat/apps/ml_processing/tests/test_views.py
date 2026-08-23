# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
View-level tests. This app's `urls.py` is written to be `include()`d by whoever wires
this app up centrally (see INTEGRATION.md) -- since this task must not touch
`cvat/urls.py`, tests invoke the views directly via `APIRequestFactory` rather than
through Django's URL resolver, which is equivalent for exercising view/permission/
serialization logic (it only skips URL-pattern matching itself).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from cvat.apps.ml_processing.models import (
    MLProcessingRequest,
    MLProcessingRequestStatus,
    ProcessingEngineConfig,
)
from cvat.apps.ml_processing.views import (
    MLProcessingCallbackView,
    MLProcessingRequestDetailView,
    SendToProcessingEngineView,
)

from .factories import make_job, make_label, make_task, make_user

factory = APIRequestFactory()


def _sign(secret: str, body_bytes: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


@pytest.mark.django_db
class TestSendToProcessingEngineView:
    def test_requires_authentication(self):
        task = make_task()
        job = make_job(task)
        request = factory.post(f"/api/jobs/{job.id}/ml-requests/")
        response = SendToProcessingEngineView.as_view()(request, job_id=job.id)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_400_when_no_config(self):
        task = make_task()
        job = make_job(task)
        user = make_user("annotator")

        request = factory.post(f"/api/jobs/{job.id}/ml-requests/")
        force_authenticate(request, user=user)
        response = SendToProcessingEngineView.as_view()(request, job_id=job.id)

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @patch("cvat.apps.ml_processing.views.django_rq.get_queue")
    def test_happy_path_returns_202_and_enqueues_job(self, mock_get_queue):
        task = make_task()
        job = make_job(task)
        user = make_user("annotator")
        ProcessingEngineConfig.objects.create(task=task, base_url="https://engine.example.com/run")

        request = factory.post(f"/api/jobs/{job.id}/ml-requests/")
        force_authenticate(request, user=user)
        response = SendToProcessingEngineView.as_view()(request, job_id=job.id)

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.data["status"] == MLProcessingRequestStatus.PENDING
        assert response.data["job"] == job.id

        mock_get_queue.assert_called_once_with("ml_processing")
        mock_get_queue.return_value.enqueue.assert_called_once()
        (_, enqueued_request_id), _ = mock_get_queue.return_value.enqueue.call_args
        assert str(enqueued_request_id) == response.data["id"]

        ml_request = MLProcessingRequest.objects.get(pk=response.data["id"])
        assert ml_request.submitted_by_id == user.id

    @patch("cvat.apps.ml_processing.views.django_rq.get_queue")
    def test_409_when_request_already_in_flight(self, mock_get_queue):
        task = make_task()
        job = make_job(task)
        user = make_user("annotator")
        ProcessingEngineConfig.objects.create(task=task, base_url="https://engine.example.com/run")

        request = factory.post(f"/api/jobs/{job.id}/ml-requests/")
        force_authenticate(request, user=user)
        first_response = SendToProcessingEngineView.as_view()(request, job_id=job.id)
        assert first_response.status_code == status.HTTP_202_ACCEPTED

        second_request = factory.post(f"/api/jobs/{job.id}/ml-requests/")
        force_authenticate(second_request, user=user)
        second_response = SendToProcessingEngineView.as_view()(second_request, job_id=job.id)
        assert second_response.status_code == status.HTTP_409_CONFLICT

    def test_404_for_missing_job(self):
        user = make_user("annotator")
        request = factory.post("/api/jobs/999999/ml-requests/")
        force_authenticate(request, user=user)
        response = SendToProcessingEngineView.as_view()(request, job_id=999999)
        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
class TestMLProcessingRequestDetailView:
    def test_visible_to_job_assignee(self):
        task = make_task()
        assignee = make_user("assignee")
        job = make_job(task, assignee=assignee)
        ml_request = MLProcessingRequest.objects.create(job=job)

        request = factory.get(f"/api/ml-requests/{ml_request.id}/")
        force_authenticate(request, user=assignee)
        response = MLProcessingRequestDetailView.as_view()(request, pk=ml_request.id)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == str(ml_request.id)

    def test_not_visible_to_unrelated_user(self):
        task = make_task()
        job = make_job(task)
        ml_request = MLProcessingRequest.objects.create(job=job)
        stranger = make_user("stranger")

        request = factory.get(f"/api/ml-requests/{ml_request.id}/")
        force_authenticate(request, user=stranger)
        response = MLProcessingRequestDetailView.as_view()(request, pk=ml_request.id)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_visible_to_task_owner(self):
        owner = make_user("task-owner")
        task = make_task(owner=owner)
        job = make_job(task)
        ml_request = MLProcessingRequest.objects.create(job=job)

        request = factory.get(f"/api/ml-requests/{ml_request.id}/")
        force_authenticate(request, user=owner)
        response = MLProcessingRequestDetailView.as_view()(request, pk=ml_request.id)

        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestMLProcessingCallbackView:
    def _make_request_with_config(self, secret="topsecret"):
        task = make_task()
        job = make_job(task)
        make_label(task, "car")
        config = ProcessingEngineConfig.objects.create(
            task=task, base_url="https://engine.example.com/run", secret=secret
        )
        ml_request = MLProcessingRequest.objects.create(job=job)
        return job, config, ml_request

    def test_rejects_missing_signature(self):
        _, _, ml_request = self._make_request_with_config()
        body = {"status": "failed", "error_message": "nope"}
        request = factory.post(
            f"/api/ml-requests/{ml_request.id}/callback/",
            data=json.dumps(body),
            content_type="application/json",
        )
        response = MLProcessingCallbackView.as_view()(request, pk=ml_request.id)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_rejects_tampered_signature(self):
        _, config, ml_request = self._make_request_with_config()
        body_bytes = json.dumps({"status": "failed", "error_message": "nope"}).encode("utf-8")
        signature = _sign(config.secret, body_bytes)

        # Tamper with the body after signing.
        tampered_body = body_bytes.replace(b"nope", b"yep!")
        request = factory.post(
            f"/api/ml-requests/{ml_request.id}/callback/",
            data=tampered_body,
            content_type="application/json",
            HTTP_X_SIGNATURE_256=signature,
        )
        response = MLProcessingCallbackView.as_view()(request, pk=ml_request.id)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_accepts_valid_signature_and_marks_failed(self):
        _, config, ml_request = self._make_request_with_config()
        body_bytes = json.dumps({"status": "failed", "error_message": "engine exploded"}).encode(
            "utf-8"
        )
        signature = _sign(config.secret, body_bytes)

        request = factory.post(
            f"/api/ml-requests/{ml_request.id}/callback/",
            data=body_bytes,
            content_type="application/json",
            HTTP_X_SIGNATURE_256=signature,
        )
        response = MLProcessingCallbackView.as_view()(request, pk=ml_request.id)

        assert response.status_code == status.HTTP_200_OK
        ml_request.refresh_from_db()
        assert ml_request.status == MLProcessingRequestStatus.FAILED
        assert ml_request.error_message == "engine exploded"

    def test_succeeded_callback_merges_annotations_and_is_idempotent(self):
        job, config, ml_request = self._make_request_with_config()
        payload = {
            "status": "succeeded",
            "annotations": {
                "shapes": [
                    {
                        "source": "engine",
                        "frame": 0,
                        "label": "car",
                        "type": "rectangle",
                        "points": [1, 2, 3, 4],
                        "occluded": False,
                        "outside": False,
                        "z_order": 0,
                        "rotation": 0,
                    }
                ]
            },
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = _sign(config.secret, body_bytes)

        request = factory.post(
            f"/api/ml-requests/{ml_request.id}/callback/",
            data=body_bytes,
            content_type="application/json",
            HTTP_X_SIGNATURE_256=signature,
        )
        response = MLProcessingCallbackView.as_view()(request, pk=ml_request.id)

        assert response.status_code == status.HTTP_200_OK
        ml_request.refresh_from_db()
        assert ml_request.status == MLProcessingRequestStatus.SUCCEEDED
        assert ml_request.result_summary == {"created": 1, "skipped": 0}
        assert job.labeledshape_set.count() == 1

        # Retry the exact same callback: must not re-merge / double-insert.
        retry_request = factory.post(
            f"/api/ml-requests/{ml_request.id}/callback/",
            data=body_bytes,
            content_type="application/json",
            HTTP_X_SIGNATURE_256=signature,
        )
        retry_response = MLProcessingCallbackView.as_view()(retry_request, pk=ml_request.id)
        assert retry_response.status_code == status.HTTP_200_OK
        assert job.labeledshape_set.count() == 1
