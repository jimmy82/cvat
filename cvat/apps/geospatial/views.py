# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from cvat.apps.engine.models import Task
from cvat.apps.geospatial.models import RasterTile
from cvat.apps.geospatial.services import pixel_pairs_to_wgs84


def _user_can_view_task(user, task: Task) -> bool:
    """Deliberately simple stand-in for CVAT's full IAM/OPA permission system -- mirrors
    `cvat.apps.ml_processing.views._user_can_view_job`, see its docstring for why."""
    if user.is_superuser:
        return True

    project = task.project
    candidate_ids = {task.owner_id, task.assignee_id}
    if project is not None:
        candidate_ids.update({project.owner_id, project.assignee_id})

    candidate_ids.discard(None)
    return user.id in candidate_ids


class TaskGeospatialFramesView(APIView):
    """GET /api/tasks/<task_id>/geospatial/frames/

    For a GeoTIFF-backed task, returns each frame's tile-pixel valid extent (the
    padding-free width/height -- see `RasterTile`) plus the WGS84 lon/lat of its four
    corners, so the frontend can show a live cursor-position geocoordinate readout by
    bilinearly interpolating between them. That's an approximation -- a real
    reprojection isn't exactly affine -- but accurate enough for an interactive display
    over one ~1024px-scale tile, without needing a full CRS-reprojection library in the
    browser; the authoritative, exact conversion is what dataset export already does
    server-side (see `cvat.apps.geospatial.dataset_io`).

    404s for a task that isn't GeoTIFF-backed (no `RasterSource`) the same as for one
    that doesn't exist or isn't visible to the caller -- there's nothing useful to
    return in any of those cases, and not distinguishing them avoids leaking task
    existence to callers who can't otherwise see it.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = []  # see NOTE on ml_processing views' throttle_classes

    def get(self, request: Request, task_id: int) -> Response:
        task = get_object_or_404(Task, pk=task_id)
        if not _user_can_view_task(request.user, task):
            raise Http404

        tiles = list(
            RasterTile.objects.filter(raster_source__task=task).select_related("raster_source")
        )
        if not tiles:
            raise Http404

        frames = []
        for tile in tiles:
            tile_spec = tile.to_tile_spec()
            corners_px = [
                (0, 0),
                (tile.width, 0),
                (0, tile.height),
                (tile.width, tile.height),
            ]
            geo_corners = pixel_pairs_to_wgs84(tile.raster_source, tile_spec, corners_px)

            frames.append(
                {
                    "frame": tile.frame,
                    "width": tile.width,
                    "height": tile.height,
                    "corners": {
                        "top_left": list(geo_corners[0]),
                        "top_right": list(geo_corners[1]),
                        "bottom_left": list(geo_corners[2]),
                        "bottom_right": list(geo_corners[3]),
                    },
                }
            )

        return Response({"frames": frames})
