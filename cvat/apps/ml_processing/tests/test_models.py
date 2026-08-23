# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from cvat.apps.ml_processing.models import (
    MLProcessingRequest,
    MLProcessingRequestStatus,
    ProcessingEngineConfig,
)

from .factories import make_job, make_project, make_task, make_user


@pytest.mark.django_db
class TestProcessingEngineConfig:
    def test_rejects_both_project_and_task(self):
        project = make_project()
        task = make_task(project=project)

        config = ProcessingEngineConfig(
            project=project, task=task, base_url="https://engine.example.com/run"
        )
        with pytest.raises(ValidationError):
            config.save()

    def test_rejects_neither_project_nor_task(self):
        config = ProcessingEngineConfig(base_url="https://engine.example.com/run")
        with pytest.raises(ValidationError):
            config.save()

    def test_task_level_config_wins_over_project_level(self):
        project = make_project()
        task = make_task(project=project)

        ProcessingEngineConfig.objects.create(
            project=project, base_url="https://project-engine.example.com/run"
        )
        task_config = ProcessingEngineConfig.objects.create(
            task=task, base_url="https://task-engine.example.com/run"
        )

        resolved = ProcessingEngineConfig.resolve_for_task(task)
        assert resolved is not None
        assert resolved.pk == task_config.pk

    def test_falls_back_to_project_level_config(self):
        project = make_project()
        task = make_task(project=project)

        project_config = ProcessingEngineConfig.objects.create(
            project=project, base_url="https://project-engine.example.com/run"
        )

        resolved = ProcessingEngineConfig.resolve_for_task(task)
        assert resolved is not None
        assert resolved.pk == project_config.pk

    def test_disabled_task_config_falls_back_to_project_config(self):
        project = make_project()
        task = make_task(project=project)

        ProcessingEngineConfig.objects.create(
            task=task, base_url="https://task-engine.example.com/run", enabled=False
        )
        project_config = ProcessingEngineConfig.objects.create(
            project=project, base_url="https://project-engine.example.com/run"
        )

        resolved = ProcessingEngineConfig.resolve_for_task(task)
        assert resolved is not None
        assert resolved.pk == project_config.pk

    def test_returns_none_when_nothing_configured(self):
        task = make_task()
        assert ProcessingEngineConfig.resolve_for_task(task) is None

    def test_returns_none_when_only_disabled_configs_exist(self):
        project = make_project()
        task = make_task(project=project)
        ProcessingEngineConfig.objects.create(
            project=project, base_url="https://project-engine.example.com/run", enabled=False
        )
        assert ProcessingEngineConfig.resolve_for_task(task) is None


@pytest.mark.django_db
class TestMLProcessingRequest:
    def test_has_unfinished_request_for_job(self):
        task = make_task()
        job = make_job(task)
        user = make_user("submitter")

        assert MLProcessingRequest.objects.has_unfinished_request_for_job(job) is False

        request = MLProcessingRequest.objects.create(job=job, submitted_by=user)
        assert MLProcessingRequest.objects.has_unfinished_request_for_job(job) is True

        request.mark_succeeded({"created": 1})
        assert MLProcessingRequest.objects.has_unfinished_request_for_job(job) is False

    def test_mark_processing_succeeded_failed(self):
        task = make_task()
        job = make_job(task)
        request = MLProcessingRequest.objects.create(job=job)

        request.mark_processing()
        request.refresh_from_db()
        assert request.status == MLProcessingRequestStatus.PROCESSING

        request.mark_succeeded({"created": 2, "updated": 1})
        request.refresh_from_db()
        assert request.status == MLProcessingRequestStatus.SUCCEEDED
        assert request.result_summary == {"created": 2, "updated": 1}

        other_request = MLProcessingRequest.objects.create(job=job)
        other_request.mark_failed("boom")
        other_request.refresh_from_db()
        assert other_request.status == MLProcessingRequestStatus.FAILED
        assert other_request.error_message == "boom"
