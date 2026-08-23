# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from rest_framework import serializers

from .models import MLProcessingRequest, ProcessingEngineConfig


class ProcessingEngineConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingEngineConfig
        fields = (
            "id",
            "project",
            "task",
            "base_url",
            "secret",
            "timeout_seconds",
            "enabled",
            "created_date",
            "updated_date",
        )
        read_only_fields = ("id", "created_date", "updated_date")
        extra_kwargs = {
            "secret": {"write_only": True},
        }


class MLProcessingRequestSerializer(serializers.ModelSerializer):
    """Read-oriented serializer: this app never accepts arbitrary client-supplied
    request state through this serializer, only through the dedicated views (creation
    via `SendToProcessingEngineView`, updates via `MLProcessingCallbackView`).
    """

    class Meta:
        model = MLProcessingRequest
        fields = (
            "id",
            "job",
            "status",
            "engine_job_id",
            "error_message",
            "result_summary",
            "created_date",
            "updated_date",
        )
        read_only_fields = fields
