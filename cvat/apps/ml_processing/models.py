# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from cvat.apps.engine.models import Job, Project, Task, TimestampedModel


class ProcessingEngineConfig(TimestampedModel):
    """Points a Task, or all Tasks within a Project, at an external ML processing
    engine HTTP endpoint. Mirrors the nullable project/task "which scope does this
    apply to" pattern used by `cvat.apps.webhooks.models.Webhook`, except here scoping
    is project-or-task (not project-or-organization) since processing is always
    triggered per-Job and needs to resolve to a single Task's engine.
    """

    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    task = models.ForeignKey(
        Task, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )

    base_url = models.URLField()
    secret = models.CharField(max_length=64, blank=True, default="")
    timeout_seconds = models.PositiveIntegerField(default=60)
    enabled = models.BooleanField(default=True)

    def __str__(self) -> str:
        scope = f"task={self.task_id}" if self.task_id else f"project={self.project_id}"
        return f"ProcessingEngineConfig({scope}, base_url={self.base_url!r})"

    def clean(self) -> None:
        super().clean()
        if bool(self.project_id) == bool(self.task_id):
            raise ValidationError(
                "Exactly one of `project` or `task` must be set on a ProcessingEngineConfig."
            )

    def save(self, *args, **kwargs) -> None:
        # NOTE: we deliberately call self.clean() here rather than self.full_clean(),
        # since full_clean() would also re-validate ordinary field constraints (e.g.
        # base_url format) on every save, which is redundant with form/serializer
        # validation and would trip up partial in-place updates (e.g. toggling
        # `enabled`) that don't touch project/task. We only need to guard the
        # project-xor-task invariant here.
        self.clean()
        super().save(*args, **kwargs)

    @classmethod
    def resolve_for_task(cls, task: Task) -> ProcessingEngineConfig | None:
        """Return the effective enabled config for a Task: a Task-level config wins
        if one exists and is enabled, otherwise fall back to the Task's Project-level
        config if enabled, otherwise None.
        """
        task_config = cls.objects.filter(task=task, enabled=True).first()
        if task_config is not None:
            return task_config

        if task.project_id:
            project_config = cls.objects.filter(project_id=task.project_id, enabled=True).first()
            if project_config is not None:
                return project_config

        return None


class MLProcessingRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    TIMED_OUT = "timed_out", "Timed out"


UNFINISHED_STATUSES = (MLProcessingRequestStatus.PENDING, MLProcessingRequestStatus.PROCESSING)
FINISHED_STATUSES = (
    MLProcessingRequestStatus.SUCCEEDED,
    MLProcessingRequestStatus.FAILED,
    MLProcessingRequestStatus.TIMED_OUT,
)


class MLProcessingRequestQuerySet(models.QuerySet):
    def unfinished(self) -> MLProcessingRequestQuerySet:
        return self.filter(status__in=UNFINISHED_STATUSES)

    def has_unfinished_request_for_job(self, job: Job) -> bool:
        return self.filter(job=job).unfinished().exists()


class MLProcessingRequest(TimestampedModel):
    """One round trip of "send this Job's annotations to the external engine, wait for
    it to post updated annotations back". Analogous in spirit to CVAT's built-in
    Request objects (see `cvat.apps.redis_handler`), but kept as our own lightweight
    model rather than plugging into that framework, since it is tightly coupled to
    CVAT's global Requests API machinery.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="ml_processing_requests")

    status = models.CharField(
        max_length=16,
        choices=MLProcessingRequestStatus.choices,
        default=MLProcessingRequestStatus.PENDING,
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    engine_job_id = models.CharField(max_length=255, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    result_summary = models.JSONField(null=True, blank=True)

    objects = MLProcessingRequestQuerySet.as_manager()

    def __str__(self) -> str:
        return f"MLProcessingRequest(id={self.id}, job={self.job_id}, status={self.status})"

    def mark_processing(self) -> None:
        self.status = MLProcessingRequestStatus.PROCESSING
        self.save(update_fields=["status", "updated_date"])

    def mark_succeeded(self, result_summary: dict | None = None) -> None:
        self.status = MLProcessingRequestStatus.SUCCEEDED
        self.result_summary = result_summary
        self.save(update_fields=["status", "result_summary", "updated_date"])

    def mark_failed(self, error_message: str = "") -> None:
        self.status = MLProcessingRequestStatus.FAILED
        self.error_message = error_message or ""
        self.save(update_fields=["status", "error_message", "updated_date"])

    def mark_timed_out(self, error_message: str = "") -> None:
        self.status = MLProcessingRequestStatus.TIMED_OUT
        self.error_message = error_message or ""
        self.save(update_fields=["status", "error_message", "updated_date"])

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED_STATUSES
