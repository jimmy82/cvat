# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from django.urls import path

from cvat.apps.geospatial.views import TaskGeospatialFramesView

urlpatterns = [
    path(
        "tasks/<int:task_id>/geospatial/frames/",
        TaskGeospatialFramesView.as_view(),
        name="task-geospatial-frames",
    ),
]
