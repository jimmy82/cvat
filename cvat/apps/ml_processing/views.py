# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import django_rq
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from cvat.apps.engine.models import Job

from .merge import merge_engine_annotations
from .models import MLProcessingRequest, MLProcessingRequestStatus, ProcessingEngineConfig
from .rq import ML_PROCESSING_QUEUE_NAME, send_to_engine
from .serializers import MLProcessingRequestSerializer
from .utils import SIGNATURE_HEADER, verify_signature


def _user_can_view_job(user, job: Job) -> bool:
    """Simple visibility check: superusers, and anyone who is the job's assignee, the
    job's task's owner/assignee, or (if the task belongs to a project) the project's
    owner/assignee.

    NOTE: production CVAT would use its full IAM/OPA permission system here (as the
    `iam_permission_class` TODO on `SendToProcessingEngineView` below describes) --
    this is a deliberately simple stand-in, not a full reimplementation of CVAT's
    object permissions.
    """
    if user.is_superuser:
        return True

    task = job.segment.task
    project = task.project

    candidate_ids = {job.assignee_id, task.owner_id, task.assignee_id}
    if project is not None:
        candidate_ids.update({project.owner_id, project.assignee_id})

    candidate_ids.discard(None)
    return user.id in candidate_ids


class SendToProcessingEngineView(APIView):
    """POST /api/jobs/<job_id>/ml-requests/

    Kicks off an asynchronous round trip to the external ML engine for a job's
    current annotations.
    """

    # TODO: production CVAT would use an `iam_permission_class` here, following the
    # pattern in cvat.apps.webhooks.permissions.WebhookPermission (an
    # OpenPolicyAgentPermission backed by real OPA rule data). Fabricating actual
    # IAM/OPA rule files is out of scope for this self-contained app -- a plain
    # `IsAuthenticated` check is used instead, plus the simple visibility check in
    # `_user_can_view_job` for the read-side detail view below.
    permission_classes = [IsAuthenticated]
    # NOTE: CVAT's DRF-wide DEFAULT_THROTTLE_CLASSES require a real Redis-backed
    # cache (see cvat.settings.base CACHES). Rate-limiting this endpoint isn't part
    # of this app's scope, so it's explicitly disabled here rather than depending on
    # that shared infrastructure being reachable.
    throttle_classes = []

    def post(self, request: Request, job_id: int) -> Response:
        job = get_object_or_404(Job, pk=job_id)

        config = ProcessingEngineConfig.resolve_for_task(job.segment.task)
        if config is None:
            return Response(
                {
                    "detail": (
                        "No enabled ML processing engine is configured for this "
                        "job's task or project."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if MLProcessingRequest.objects.has_unfinished_request_for_job(job):
            return Response(
                {"detail": "An ML processing request is already in flight for this job."},
                status=status.HTTP_409_CONFLICT,
            )

        ml_request = MLProcessingRequest.objects.create(
            job=job,
            status=MLProcessingRequestStatus.PENDING,
            submitted_by=request.user,
        )

        queue = django_rq.get_queue(ML_PROCESSING_QUEUE_NAME)
        queue.enqueue(send_to_engine, str(ml_request.id))

        serializer = MLProcessingRequestSerializer(ml_request)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)


class MLProcessingRequestDetailView(APIView):
    """GET /api/ml-requests/<uuid:pk>/"""

    permission_classes = [IsAuthenticated]
    throttle_classes = []  # see NOTE on SendToProcessingEngineView.throttle_classes

    def get(self, request: Request, pk) -> Response:
        ml_request = get_object_or_404(
            MLProcessingRequest.objects.select_related(
                "job__segment__task__project", "job__assignee"
            ),
            pk=pk,
        )

        if not _user_can_view_job(request.user, ml_request.job):
            # Deliberately indistinguishable from "doesn't exist" to avoid leaking
            # existence of requests the user can't see.
            raise Http404

        serializer = MLProcessingRequestSerializer(ml_request)
        return Response(serializer.data)


class MLProcessingCallbackView(APIView):
    """POST /api/ml-requests/<uuid:pk>/callback/

    Called by the external engine (not a logged-in CVAT user), so no
    `IsAuthenticated` here -- authenticity is instead established via the
    `X-Signature-256` HMAC signature, verified against the resolved
    `ProcessingEngineConfig.secret` for the request's job.
    """

    permission_classes = []
    authentication_classes = []
    throttle_classes = []  # see NOTE on SendToProcessingEngineView.throttle_classes

    def post(self, request: Request, pk) -> Response:
        ml_request = get_object_or_404(
            MLProcessingRequest.objects.select_related("job__segment__task"), pk=pk
        )

        config = ProcessingEngineConfig.resolve_for_task(ml_request.job.segment.task)
        if config is None or not config.secret:
            return Response(
                {"detail": "No signing secret is configured for this job's ML engine."},
                status=status.HTTP_403_FORBIDDEN,
            )

        signature_header = request.headers.get(SIGNATURE_HEADER)
        if not verify_signature(config.secret, request.body, signature_header):
            return Response(
                {"detail": "Missing or invalid X-Signature-256 signature."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Idempotent retry: if the request has already reached a terminal state,
        # acknowledge without re-merging (avoids double-inserting shapes if the
        # engine retries a callback that already succeeded).
        if ml_request.is_finished:
            return Response(status=status.HTTP_200_OK)

        payload = request.data
        engine_status = payload.get("status")

        if engine_status == "failed":
            ml_request.mark_failed(payload.get("error_message", ""))
        elif engine_status == "succeeded":
            result_summary = merge_engine_annotations(ml_request.job, payload.get("annotations"))
            ml_request.mark_succeeded(result_summary)
        else:
            return Response(
                {"detail": "`status` must be one of 'succeeded' or 'failed'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(status=status.HTTP_200_OK)
