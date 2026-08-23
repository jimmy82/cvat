# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from django.db import models

from cvat.apps.engine.models import Task, TimestampedModel


class RasterSource(TimestampedModel):
    """One row per GeoTIFF ingested into a Task. Keeps the raster's georeferencing and
    band information so tile-pixel annotations can later be converted back to
    real-world coordinates (see `cvat.apps.geospatial.transforms`), and so the Python
    processing engine can be told, per frame, which raster/window it came from.
    """

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="raster_sources")

    # Path is relative to the task's data root, mirroring how CVAT already stores
    # other media paths -- keeps this model portable across storage backends.
    source_path = models.CharField(max_length=1024)

    crs_wkt = models.TextField(help_text="Well-known text representation of the raster's CRS")
    # Affine geotransform coefficients (a, b, c, d, e, f), i.e. rasterio/GDAL order,
    # such that (x, y) = (a*col + b*row + c, d*col + e*row + f).
    transform_a = models.FloatField()
    transform_b = models.FloatField()
    transform_c = models.FloatField()
    transform_d = models.FloatField()
    transform_e = models.FloatField()
    transform_f = models.FloatField()

    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    band_count = models.PositiveSmallIntegerField()
    dtype = models.CharField(max_length=32)
    nodata_value = models.FloatField(null=True, blank=True)

    tile_size = models.PositiveIntegerField()
    overlap = models.PositiveIntegerField()
    was_reencoded_as_cog = models.BooleanField(default=False)
    display_bands = models.JSONField(
        default=list, help_text="1-indexed band numbers used for the browser-facing PNG tiles"
    )

    class Meta:
        default_related_name = "raster_sources"

    def __str__(self) -> str:
        return f"RasterSource(task={self.task_id}, source_path={self.source_path!r})"

    @property
    def affine(self):
        from affine import Affine

        return Affine(
            self.transform_a,
            self.transform_b,
            self.transform_c,
            self.transform_d,
            self.transform_e,
            self.transform_f,
        )


class RasterTile(models.Model):
    """One row per tile, mapping a CVAT frame index back to its pixel window within a
    `RasterSource`. This is the join CVAT's `ml_processing` app uses to tell the Python
    engine (and any GeoJSON/shapefile export) how each frame relates to the source
    raster and its CRS.
    """

    raster_source = models.ForeignKey(RasterSource, on_delete=models.CASCADE, related_name="tiles")

    # Index of this tile within the Task's frame sequence (i.e. CVAT's own frame
    # numbering) -- see `build_tile_grid`'s row-major indexing.
    frame = models.PositiveIntegerField()

    row = models.PositiveIntegerField()
    col = models.PositiveIntegerField()
    col_off = models.PositiveIntegerField()
    row_off = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    pad_right = models.PositiveIntegerField(default=0)
    pad_bottom = models.PositiveIntegerField(default=0)

    class Meta:
        default_related_name = "tiles"
        constraints = [
            models.UniqueConstraint(
                fields=["raster_source", "frame"], name="unique_frame_per_raster_source"
            )
        ]
        ordering = ["frame"]

    def __str__(self) -> str:
        return f"RasterTile(raster_source={self.raster_source_id}, frame={self.frame})"

    def to_tile_spec(self):
        from cvat.apps.geospatial.ingestion import TileSpec

        return TileSpec(
            index=self.frame,
            row=self.row,
            col=self.col,
            col_off=self.col_off,
            row_off=self.row_off,
            width=self.width,
            height=self.height,
            pad_right=self.pad_right,
            pad_bottom=self.pad_bottom,
            tile_size=self.raster_source.tile_size,
        )


class RasterTaskConfig(TimestampedModel):
    """Per-task settings captured at ingestion time so re-tiling (or debugging why a
    given tile grid looks the way it does) is reproducible without re-reading them off
    the upload request."""

    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name="raster_config")
    tile_size = models.PositiveIntegerField()
    overlap = models.PositiveIntegerField()
    reencode_as_cog = models.BooleanField(default=True)

    class Meta:
        default_related_name = "raster_config"

    def __str__(self) -> str:
        return f"RasterTaskConfig(task={self.task_id}, tile_size={self.tile_size}, overlap={self.overlap})"
