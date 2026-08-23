# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
This app's own URLconf, written as if it were going to be `include()`d from CVAT's
root URL configuration. This task's scope forbids editing `cvat/urls.py` directly --
see INTEGRATION.md in this app for the exact `path(...)` entries to splice in there
(or into `cvat/apps/engine/urls.py`) when this app is wired up centrally.
"""

from django.urls import path

from .views import (
    MLProcessingCallbackView,
    MLProcessingRequestDetailView,
    SendToProcessingEngineView,
)

app_name = "ml_processing"

urlpatterns = [
    path(
        "jobs/<int:job_id>/ml-requests/",
        SendToProcessingEngineView.as_view(),
        name="job-ml-request-create",
    ),
    path(
        "ml-requests/<uuid:pk>/",
        MLProcessingRequestDetailView.as_view(),
        name="ml-request-detail",
    ),
    path(
        "ml-requests/<uuid:pk>/callback/",
        MLProcessingCallbackView.as_view(),
        name="ml-request-callback",
    ),
]
