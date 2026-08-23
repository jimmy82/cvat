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

from rasterio.crs import CRS
from rasterio.warp import transform as warp_transform
from rest_framework.serializers import ValidationError

from cvat.apps.dataset_manager.bindings import CommonData
from cvat.apps.dataset_manager.formats.registry import exporter, importer
from cvat.apps.engine.models import ShapeType
from cvat.apps.geospatial.models import RasterTile
from cvat.apps.geospatial.transforms import geo_to_tile_pixel, tile_pixel_to_geo

WGS84 = CRS.from_epsg(4326)

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
    src_crs = CRS.from_wkt(next(iter(tiles_by_frame.values())).raster_source.crs_wkt)

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
        transform = tile.raster_source.affine

        for shape in frame.labeled_shapes:
            geometry_spec = _shape_pixel_rings(shape.type, shape.points)
            if geometry_spec is None:
                continue
            geojson_type, pixel_pairs = geometry_spec

            native_pairs = [tile_pixel_to_geo(transform, tile_spec, x, y) for x, y in pixel_pairs]
            xs, ys = zip(*native_pairs)
            lons, lats = warp_transform(src_crs, WGS84, xs, ys)
            geo_pairs = list(zip(lons, lats))

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


@exporter(name="GeoJSON", version="1.0", ext="GEOJSON")
def _export(dst_file, temp_dir, instance_data: CommonData, save_images=False, **options):
    if save_images:
        raise ValidationError("Media export as a dataset is not supported for GeoJSON export")

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


@importer(name="GeoJSON", version="1.0", ext="GEOJSON")
def _import(src_file, temp_dir, instance_data: CommonData, load_data_callback=None, **kwargs):
    if load_data_callback is not None:
        raise ValidationError("Media import from a dataset is not supported for GeoJSON import")

    db_task = instance_data._db_task
    tiles = list(_tiles_for_task(db_task, action="import").values())
    src_crs = CRS.from_wkt(tiles[0].raster_source.crs_wkt)

    feature_collection = json.load(io.TextIOWrapper(src_file, encoding="utf-8"))

    for feature in feature_collection.get("features", []):
        geometry = feature["geometry"]
        properties = feature.get("properties") or {}
        label_name = properties.get("label")
        if not label_name:
            raise ValidationError(f"A GeoJSON feature is missing a 'label' property: {feature}")

        geo_pairs = _feature_geo_pairs(geometry)
        lons, lats = zip(*geo_pairs)
        # RFC 7946: input is always WGS84 lon/lat, regardless of any legacy `crs`
        # member the file might carry -- see the module docstring.
        xs, ys = warp_transform(WGS84, src_crs, lons, lats)
        native_pairs = list(zip(xs, ys))

        # A shape must land entirely within one tile's frame to become one CVAT shape;
        # try every tile and use the first whose window contains every point. Tiles can
        # overlap (see `RasterSource.overlap`), so a shape near a shared edge may
        # legitimately fit more than one -- which tile "wins" in that case is
        # unspecified, same as it would be for an annotator drawing directly on the
        # overlap region.
        for tile in tiles:
            transform = tile.raster_source.affine
            tile_spec = tile.to_tile_spec()
            tile_pixel_pairs = [geo_to_tile_pixel(transform, tile_spec, x, y) for x, y in native_pairs]
            if all(pair is not None for pair in tile_pixel_pairs):
                break
        else:
            raise ValidationError(
                "A GeoJSON feature doesn't fit within any single tile's frame -- it may "
                f"straddle a tile boundary, or fall outside the raster entirely: {feature}"
            )

        instance_data.add_shape(
            instance_data.LabeledShape(
                type=_GEOJSON_TYPE_TO_SHAPE_TYPE[geometry["type"]],
                frame=tile.frame,
                points=[coord for pair in tile_pixel_pairs for coord in pair],
                label=label_name,
                occluded=bool(properties.get("occluded", False)),
                attributes=[],
                source="file",
            )
        )
