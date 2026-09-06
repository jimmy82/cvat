# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""GeoJSON export/import for GeoTIFF-backed tasks.

Registers a "GeoJSON" entry in `cvat.apps.dataset_manager`'s export/import format
registry (see `cvat/apps/dataset_manager/formats/registry.py`, which imports this module
for its side effect of registering `_export`/`_import` below). Unlike CVAT's other
formats, this one converts annotation points between tile-pixel space and real-world
coordinates, using the per-frame tile/raster metadata that
`cvat.apps.geospatial.services.persist_raster_metadata` recorded when the task's GeoTIFF
was ingested (see `cvat/apps/geospatial/README.md`, "Known integration gaps" #3).

Export produces a single WGS84 (EPSG:4326) `FeatureCollection`, one `Feature` per CVAT
shape, with the label name and CVAT shape id carried over as feature properties. Import
reads that same shape back (RFC 7946: input is always assumed WGS84 lon/lat, regardless
of any legacy `crs` member the file might carry -- a deliberate scope limit, not an
oversight) and only requires each feature to carry a `label` property naming an existing
label on the target task; other properties are ignored.

Both directions share the same shape-type mapping, and it's lossy in one specific way:
CVAT rectangles are exported as 4-point `Polygon`s (a rectangle drawn at an angle isn't
representable as a GeoJSON `Polygon` any other way), so anything imported back in comes
in as a CVAT `polygon`, never a `rectangle` -- there's no reliable way to tell "this
Polygon was originally an axis-aligned rectangle" from "this Polygon happens to have 4
corners" after a real-world reprojection.
"""

from __future__ import annotations

import io
import json

import shapely.geometry as shapely_geom
from rest_framework.serializers import ValidationError

from cvat.apps.dataset_manager.bindings import CommonData, JobData
from cvat.apps.dataset_manager.formats.registry import exporter, importer
from cvat.apps.engine.models import Job, JobType, ShapeType, StateChoice
from cvat.apps.geospatial.models import RasterTile
from cvat.apps.geospatial.services import (
    pixel_pairs_to_wgs84,
    wgs84_pairs_to_raster_pixel,
    wgs84_pairs_to_tile_pixel,
)

# Maps a CVAT shape type to the flat tile-pixel `points` it carries into the closed-ring
# (or open, for polylines/points) list of (x, y) pairs that make up its geometry, in the
# same tile-pixel space the annotation was actually drawn in. Ellipses, cuboids,
# skeletons, and masks aren't representable as a single simple polygon/line/point
# geometry without further decisions about resolution/approximation, so they're skipped
# rather than silently exported wrong -- see the module docstring's linked gap.
def _shape_pixel_rings(shape_type: str, points: list) -> tuple[str, list[tuple[float, float]]] | None:
    pairs = [(points[i], points[i + 1]) for i in range(0, len(points), 2)]

    if shape_type == ShapeType.RECTANGLE:
        (x0, y0), (x1, y1) = pairs
        ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        return ("Polygon", ring)
    if shape_type == ShapeType.POLYGON:
        return ("Polygon", [*pairs, pairs[0]])
    if shape_type == ShapeType.POLYLINE:
        return ("LineString", pairs)
    if shape_type == ShapeType.POINTS:
        return ("MultiPoint", pairs)
    return None


def _to_geojson_geometry(geojson_type: str, geo_pairs: list[tuple[float, float]]) -> dict:
    if geojson_type == "Polygon":
        return {"type": "Polygon", "coordinates": [geo_pairs]}
    return {"type": geojson_type, "coordinates": geo_pairs}




def _tiles_for_task(db_task, *, action: str) -> dict:
    """RasterTile rows for `db_task`, keyed by frame. Raises `ValidationError` if the
    task wasn't ingested from a GeoTIFF (i.e. has no `RasterSource`), since tile-pixel
    coordinates are meaningless without one.
    """
    tiles_by_frame = {
        tile.frame: tile
        for tile in RasterTile.objects.filter(raster_source__task=db_task).select_related(
            "raster_source"
        )
    }
    if not tiles_by_frame:
        raise ValidationError(
            f"GeoJSON {action} is only available for tasks created from a GeoTIFF "
            f"raster (task {db_task.id} has no associated RasterSource)."
        )
    return tiles_by_frame


def build_feature_collection(instance_data: CommonData) -> dict:
    """Convert every shape in `instance_data` (a `TaskData` or `JobData`) into a GeoJSON
    `Feature`, reprojected from the source raster's native CRS into WGS84 lon/lat.
    """

    db_task = instance_data._db_task
    tiles_by_frame = _tiles_for_task(db_task, action="export")

    features = []
    # `include_empty=True`: `group_by_frame()` asserts on streamed annotation IRs
    # otherwise (see `cvat.apps.dataset_manager.formats.cvat` for the same convention);
    # frames with no shapes are simply skipped below once `labeled_shapes` is empty.
    for frame in instance_data.group_by_frame(include_empty=True):
        if not frame.labeled_shapes:
            continue

        tile = tiles_by_frame.get(frame.frame)
        if tile is None:
            # Frame isn't part of the ingested tile grid (shouldn't normally happen for
            # a geotiff task, but fail per-shape rather than aborting the whole export).
            continue

        tile_spec = tile.to_tile_spec()

        for shape in frame.labeled_shapes:
            geometry_spec = _shape_pixel_rings(shape.type, shape.points)
            if geometry_spec is None:
                continue
            geojson_type, pixel_pairs = geometry_spec

            geo_pairs = pixel_pairs_to_wgs84(tile.raster_source, tile_spec, pixel_pairs)

            features.append(
                {
                    "type": "Feature",
                    "geometry": _to_geojson_geometry(geojson_type, geo_pairs),
                    "properties": {
                        "cvat_shape_id": shape.id,
                        "cvat_frame": frame.frame,
                        # `shape.label` is already the resolved label *name* by the
                        # time `group_by_frame()` hands it to us (see
                        # `CommonData._export_labeled_shape`), despite the `label: int`
                        # type hint on `CommonData.LabeledShape`.
                        "label": shape.label,
                        "occluded": shape.occluded,
                        "source": shape.source,
                    },
                }
            )

    return {
        "type": "FeatureCollection",
        # RFC 7946 mandates WGS84 lon/lat unless stated otherwise; called out explicitly
        # here (via the legacy GeoJSON "crs" member) since downstream GIS tooling
        # otherwise has no way to tell this apart from a plain-pixel export.
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def _require_jobs_completed(instance_data: CommonData) -> None:
    """Refuse to export until the relevant job(s) are actually marked 'completed'.

    Without this, a task-level export would silently merge in whatever partial
    annotation work exists so far and call it a finished result -- the whole point of
    tiling one raster across multiple jobs is that several annotators split the work,
    so "export the task" should mean "everyone is done", not "here's whatever's there
    right now". Ground-truth and consensus-replica jobs are excluded since they're not
    part of the normal annotation workflow this check is guarding.
    """
    if isinstance(instance_data, JobData):
        jobs = list(Job.objects.filter(pk=instance_data._db_job.id))
    else:
        jobs = list(
            Job.objects.filter(segment__task=instance_data._db_task, type=JobType.ANNOTATION)
        )

    unfinished = [job for job in jobs if job.state != StateChoice.COMPLETED]
    if unfinished:
        job_ids = ", ".join(str(job.id) for job in unfinished)
        raise ValidationError(
            "GeoJSON export requires every annotation job to be marked 'completed' "
            f"first (not yet completed: job id(s) {job_ids})."
        )


@exporter(name="GeoJSON", version="1.0", ext="GEOJSON")
def _export(dst_file, temp_dir, instance_data: CommonData, save_images=False, **options):
    if save_images:
        raise ValidationError("Media export as a dataset is not supported for GeoJSON export")

    _require_jobs_completed(instance_data)
    feature_collection = build_feature_collection(instance_data)

    file_writer = io.TextIOWrapper(dst_file, encoding="utf-8")
    with file_writer:
        json.dump(feature_collection, file_writer, indent=2)


_GEOJSON_TYPE_TO_SHAPE_TYPE = {
    "Polygon": ShapeType.POLYGON,
    "LineString": ShapeType.POLYLINE,
    "MultiPoint": ShapeType.POINTS,
}


def _feature_geo_pairs(geometry: dict) -> list[tuple[float, float]]:
    """Flatten a GeoJSON geometry's coordinates into a list of (lon, lat) pairs, in
    drawing order, with a closed `Polygon` ring's duplicated closing point dropped (CVAT
    polygons aren't closed the way a GeoJSON ring is)."""
    geojson_type = geometry["type"]
    if geojson_type == "Polygon":
        ring = geometry["coordinates"][0]
        if len(ring) > 1 and tuple(ring[0]) == tuple(ring[-1]):
            ring = ring[:-1]
        return [(pt[0], pt[1]) for pt in ring]
    if geojson_type in ("LineString", "MultiPoint"):
        return [(pt[0], pt[1]) for pt in geometry["coordinates"]]
    raise ValidationError(
        f"Unsupported GeoJSON geometry type {geojson_type!r} "
        "(only Polygon, LineString, and MultiPoint can be imported)"
    )


def _tile_raster_box(tile: RasterTile) -> shapely_geom.base.BaseGeometry:
    """The tile's full pixel window (including its blank padding area, out to
    `tile_size` on every edge) in raster-pixel space, matching the same clamp
    `raster_pixel_to_tile_pixel` already applies for the single-tile fast path."""
    tile_spec = tile.to_tile_spec()
    return shapely_geom.box(
        tile.col_off, tile.row_off, tile.col_off + tile_spec.tile_size, tile.row_off + tile_spec.tile_size
    )


def _iter_geoms(geometry: shapely_geom.base.BaseGeometry, of_type: type):
    """Flatten a Polygon/MultiPolygon/LineString/MultiLineString/GeometryCollection (the
    possible results of clipping a subject geometry against an axis-aligned box) down to
    its individual `of_type` parts, dropping empty/degenerate slivers."""
    if geometry.is_empty:
        return
    if isinstance(geometry, of_type):
        measure = geometry.area if isinstance(geometry, shapely_geom.Polygon) else geometry.length
        if measure > 0:
            yield geometry
    elif isinstance(geometry, (shapely_geom.MultiPolygon, shapely_geom.MultiLineString, shapely_geom.GeometryCollection)):
        for part in geometry.geoms:
            yield from _iter_geoms(part, of_type)


def _to_tile_local_flat_points(coords, tile: RasterTile) -> list[float]:
    flat: list[float] = []
    for x, y in coords:
        flat.append(x - tile.col_off)
        flat.append(y - tile.row_off)
    return flat


def _clip_feature_to_tiles(geojson_type: str, raster_pixel_pairs: list[tuple[float, float]], tiles: list[RasterTile]):
    """A feature that doesn't fit entirely within any single tile (e.g. one drawn against
    the whole raster's extent rather than a specific tile) is split across every tile it
    overlaps instead of being rejected outright: the geometry is clipped to each tile's
    pixel window, and one CVAT shape is yielded per tile per resulting piece.

    Yields `(tile, ShapeType, flat_tile_local_points)`. A concave polygon or a line that
    exits and re-enters a tile can produce more than one piece for the same tile -- each
    becomes its own CVAT shape, since a single shape can't represent disjoint geometry.
    """
    if geojson_type == "Polygon":
        subject = shapely_geom.Polygon(raster_pixel_pairs)
    elif geojson_type == "LineString":
        subject = shapely_geom.LineString(raster_pixel_pairs)
    else:
        subject = shapely_geom.MultiPoint(raster_pixel_pairs)

    for tile in tiles:
        box = _tile_raster_box(tile)
        if not subject.intersects(box):
            continue
        clipped = subject.intersection(box)

        if geojson_type == "Polygon":
            for part in _iter_geoms(clipped, shapely_geom.Polygon):
                coords = list(part.exterior.coords)[:-1]  # drop the closing duplicate point
                yield tile, ShapeType.POLYGON, _to_tile_local_flat_points(coords, tile)
        elif geojson_type == "LineString":
            for part in _iter_geoms(clipped, shapely_geom.LineString):
                yield tile, ShapeType.POLYLINE, _to_tile_local_flat_points(part.coords, tile)
        else:
            points = (
                [clipped]
                if isinstance(clipped, shapely_geom.Point)
                else list(clipped.geoms)
                if hasattr(clipped, "geoms")
                else []
            )
            coords = [(p.x, p.y) for p in points if isinstance(p, shapely_geom.Point)]
            if coords:
                yield tile, ShapeType.POINTS, _to_tile_local_flat_points(coords, tile)


@importer(name="GeoJSON", version="1.0", ext="GEOJSON")
def _import(src_file, temp_dir, instance_data: CommonData, load_data_callback=None, **kwargs):
    if load_data_callback is not None:
        raise ValidationError("Media import from a dataset is not supported for GeoJSON import")

    db_task = instance_data._db_task
    tiles = list(_tiles_for_task(db_task, action="import").values())

    feature_collection = json.load(io.TextIOWrapper(src_file, encoding="utf-8"))

    for feature in feature_collection.get("features", []):
        geometry = feature["geometry"]
        geojson_type = geometry["type"]
        properties = feature.get("properties") or {}
        label_name = properties.get("label")
        if not label_name:
            raise ValidationError(f"A GeoJSON feature is missing a 'label' property: {feature}")
        occluded = bool(properties.get("occluded", False))

        # RFC 7946: input is always WGS84 lon/lat, regardless of any legacy `crs`
        # member the file might carry -- see the module docstring.
        geo_pairs = _feature_geo_pairs(geometry)

        # Fast path: a shape that lands entirely within one tile's frame becomes one
        # CVAT shape, unfragmented -- true for any shape actually drawn against a single
        # tile (including everything a round-trip export produces). Tiles can overlap
        # (see `RasterSource.overlap`), so a shape near a shared edge may legitimately
        # fit more than one -- which tile "wins" in that case is unspecified, same as it
        # would be for an annotator drawing directly on the overlap region.
        placed = False
        for tile in tiles:
            tile_pixel_pairs = wgs84_pairs_to_tile_pixel(tile.raster_source, tile.to_tile_spec(), geo_pairs)
            if tile_pixel_pairs is not None:
                instance_data.add_shape(
                    instance_data.LabeledShape(
                        type=_GEOJSON_TYPE_TO_SHAPE_TYPE[geojson_type],
                        frame=tile.frame,
                        points=[coord for pair in tile_pixel_pairs for coord in pair],
                        label=label_name,
                        occluded=occluded,
                        attributes=[],
                        source="file",
                    )
                )
                placed = True
                break

        if placed:
            continue

        # Doesn't fit in any single tile -- e.g. drawn against the whole raster's
        # extent rather than one tile. Split it across every tile it overlaps instead
        # of rejecting it, clipping the geometry to each tile's pixel window.
        raster_pixel_pairs = wgs84_pairs_to_raster_pixel(tiles[0].raster_source, geo_pairs)
        split_into_any = False
        for tile, shape_type, flat_points in _clip_feature_to_tiles(geojson_type, raster_pixel_pairs, tiles):
            split_into_any = True
            instance_data.add_shape(
                instance_data.LabeledShape(
                    type=shape_type,
                    frame=tile.frame,
                    points=flat_points,
                    label=label_name,
                    occluded=occluded,
                    attributes=[],
                    source="file",
                )
            )

        if not split_into_any:
            raise ValidationError(
                "A GeoJSON feature doesn't overlap any tile of this task's raster at "
                f"all: {feature}"
            )
