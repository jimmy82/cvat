# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
Background-job entry point enqueued onto the (documented-but-not-wired-here)
`ml_processing` RQ queue by `SendToProcessingEngineView`. Kept as a plain, importable
module-level function (not a class/method) so it's trivially picklable by RQ.
"""

from __future__ import annotations

import json

import requests

from cvat.apps.engine.models import Job

from .merge import merge_engine_annotations, serialize_job_annotations
from .models import MLProcessingRequest, ProcessingEngineConfig
from .utils import SIGNATURE_HEADER, sign_payload

ML_PROCESSING_QUEUE_NAME = "ml_processing"


def _build_frame_entries(job: Job) -> list[dict]:
    """One entry per frame in the job's segment, with optional geospatial metadata.

    `RasterTile` is imported lazily and defensively: ordinary (non-GeoTIFF) tasks have
    no geospatial data at all, and `cvat.apps.geospatial` may not even be installed in
    every deployment of this app, so a missing import (or missing row) must not break
    plain tasks -- it just leaves `pixel_window`/`geotransform`/`crs` as `None`.
    """
    segment = job.segment

    try:
        from cvat.apps.geospatial.models import RasterTile
    except ImportError:
        RasterTile = None  # noqa: N806

    tiles_by_frame = {}
    if RasterTile is not None:
        tiles_by_frame = {
            tile.frame: tile
            for tile in RasterTile.objects.filter(raster_source__task=segment.task)
            .select_related("raster_source")
            .iterator()
        }

    frames = []
    for frame in range(segment.start_frame, segment.stop_frame + 1):
        entry = {
            # TODO: the real endpoint should build an absolute URI via
            # `request.build_absolute_uri()` from the originating view, threading the
            # base URL through as a parameter. A relative path is used here since this
            # module has no access to the request that triggered it (it may run much
            # later, in a worker process).
            "frame": frame,
            "image_url": f"/api/jobs/{job.id}/data?type=frame&number={frame}",
            "pixel_window": None,
            "geotransform": None,
            "crs": None,
        }

        tile = tiles_by_frame.get(frame)
        if tile is not None:
            source = tile.raster_source
            entry["pixel_window"] = {
                "col_off": tile.col_off,
                "row_off": tile.row_off,
                "width": tile.width,
                "height": tile.height,
            }
            entry["geotransform"] = [
                source.transform_a,
                source.transform_b,
                source.transform_c,
                source.transform_d,
                source.transform_e,
                source.transform_f,
            ]
            entry["crs"] = source.crs_wkt

        frames.append(entry)

    return frames


def build_outbound_payload(ml_request: MLProcessingRequest) -> dict:
    """Build the JSON payload sent to the external engine. Split out from
    `send_to_engine` so tests can assert on the exact documented shape without also
    mocking `requests.post`.
    """
    job = ml_request.job
    return {
        "request_id": str(ml_request.id),
        "job_id": job.id,
        "task_id": job.segment.task_id,
        "callback_url": f"/api/ml-requests/{ml_request.id}/callback/",
        "frames": _build_frame_entries(job),
        "annotations": serialize_job_annotations(job),
    }


def send_to_engine(request_id: str) -> None:
    """Fetch the `MLProcessingRequest`, mark it PROCESSING, and POST its payload to the
    resolved `ProcessingEngineConfig`'s `base_url`. Never raises: any transport error
    or unresolvable config is captured onto the request via `mark_failed()` so a
    failing external engine can't crash the RQ worker.
    """
    ml_request = MLProcessingRequest.objects.select_related("job__segment__task").get(
        pk=request_id
    )
    job = ml_request.job

    config = ProcessingEngineConfig.resolve_for_task(job.segment.task)
    if config is None:
        ml_request.mark_failed(
            "No enabled ML processing engine configuration was found for this job "
            "(it may have been disabled/deleted after the request was created)."
        )
        return

    ml_request.mark_processing()

    payload = build_outbound_payload(ml_request)
    body_bytes = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if config.secret:
        # Sign the exact bytes we send, matching the webhooks app's HMAC-SHA256
        # scheme (see cvat.apps.webhooks.utils.perform_webhook_request).
        headers[SIGNATURE_HEADER] = sign_payload(config.secret, body_bytes)

    try:
        response = requests.post(
            config.base_url,
            data=body_bytes,
            headers=headers,
            timeout=config.timeout_seconds,
        )
    except requests.exceptions.RequestException as exc:
        ml_request.mark_failed(str(exc))
        return

    if response.status_code != 200:
        # A non-200 synchronous response doesn't necessarily mean failure -- many
        # engines simply 202/204-ack receipt and call the callback URL later. Leave
        # the request PROCESSING; it will be resolved by a later callback (or, in a
        # production deployment, by a timeout sweep -- not implemented in this app,
        # see INTEGRATION.md).
        return

    try:
        response_body = response.json()
    except ValueError:
        response_body = None

    if not isinstance(response_body, dict) or "status" not in response_body:
        # Plain 200 ack with no callback-shaped body: still async, wait for callback.
        return

    # The engine responded synchronously with a callback-shaped body -- treat it the
    # same way the callback endpoint would.
    engine_status = response_body.get("status")
    if engine_status == "failed":
        ml_request.mark_failed(response_body.get("error_message", ""))
    elif engine_status == "succeeded":
        result_summary = merge_engine_annotations(job, response_body.get("annotations"))
        ml_request.mark_succeeded(result_summary)
    # else: unrecognized synchronous shape -- ignore and keep waiting for the callback.
