# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("geospatial", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="rastersource",
            name="georeferencing_kind",
            field=models.CharField(
                choices=[("affine", "Affine"), ("rpc", "Rpc")], default="affine", max_length=16
            ),
        ),
        migrations.AddField(
            model_name="rastersource",
            name="rpc_coefficients",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rastersource",
            name="transform_a",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rastersource",
            name="transform_b",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rastersource",
            name="transform_c",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rastersource",
            name="transform_d",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rastersource",
            name="transform_e",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rastersource",
            name="transform_f",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rastersource",
            name="crs_wkt",
            field=models.TextField(
                help_text=(
                    "Well-known text representation of the raster's CRS. Only "
                    "meaningful when georeferencing_kind == AFFINE -- RPC ground "
                    "coordinates are always WGS84 lon/lat by the RPC00B "
                    "specification, independent of whatever placeholder CRS the "
                    "raster's own dataset-level tags might carry."
                )
            ),
        ),
    ]
