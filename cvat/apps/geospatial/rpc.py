# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""Pure RPC (Rational Polynomial Coefficients) math: ground (lon, lat, height) <->
image (sample/col, line/row) for satellite/aerial imagery georeferenced via RPCs rather
than a direct affine transform or GCPs -- see `cvat.apps.geospatial.ingestion`'s
`needs_rpc_georeferencing` for why this raster kind needs its own transform path instead
of being handled by `transforms.py`'s affine-only helpers.

The RPC00B model (used by GDAL/rasterio, DigitalGlobe/Maxar, Pleiades, and the NITF/OGC
RPC spec generally) relates *normalized* ground coordinates
    X = (lon - long_off) / long_scale
    Y = (lat - lat_off) / lat_scale
    Z = (height - height_off) / height_scale
to normalized image coordinates via two ratios of the same fixed-form cubic polynomial
in (X, Y, Z), one for line/row and one for sample/col, each with 20 numerator and 20
denominator coefficients:
    line = line_off + line_scale * P(X, Y, Z; line_num_coeff) / P(X, Y, Z; line_den_coeff)
    samp = samp_off + samp_scale * P(X, Y, Z; samp_num_coeff) / P(X, Y, Z; samp_den_coeff)

This forward direction (ground -> image) is a direct polynomial evaluation. The inverse
(image -> ground), needed to convert a clicked pixel into a real-world coordinate, has
no closed form -- RPC provides no such thing by design -- so it's solved numerically via
Newton-Raphson, starting from the RPC's own reference point and using a finite-difference
Jacobian (avoids hand-deriving the polynomial's analytic partial derivatives, which would
be just as easy to get subtly wrong as the term order below was during development).

A flat height of 0 is assumed throughout (never draping onto a DEM) -- a real accuracy
limit on terrain with significant relief, acceptable for annotation purposes, not for
survey-grade measurement. This matches the working assumption already documented for the
GCP path in `transforms.py`.
"""

from __future__ import annotations

# The term order below is the standard RPC00B cubic basis, with X = normalized
# longitude, Y = normalized latitude, Z = normalized height -- in that variable order.
# This is the one detail in this module that's easy to get quietly wrong: swapping X
# and Y here still passes a "does it round-trip through its own reference point" sanity
# check (every term but the constant "1" vanishes there regardless of ordering, so that
# check can't catch it), but produces wildly wrong results everywhere else, since the
# asymmetric terms (X^2*Y vs X*Y^2, X^3 vs Y^3, ...) evaluate a genuinely different
# function once the meaning of X and Y is swapped. Validated against real GCP ground
# truth for this exact swap during development -- see
# cvat/apps/geospatial/tests/test_rpc.py.
_RPC_BASIS = (
    lambda x, y, z: 1.0,
    lambda x, y, z: x,
    lambda x, y, z: y,
    lambda x, y, z: z,
    lambda x, y, z: x * y,
    lambda x, y, z: x * z,
    lambda x, y, z: y * z,
    lambda x, y, z: x * x,
    lambda x, y, z: y * y,
    lambda x, y, z: z * z,
    lambda x, y, z: x * y * z,
    lambda x, y, z: x * x * x,
    lambda x, y, z: x * y * y,
    lambda x, y, z: x * z * z,
    lambda x, y, z: x * x * y,
    lambda x, y, z: y * y * y,
    lambda x, y, z: y * z * z,
    lambda x, y, z: x * x * z,
    lambda x, y, z: y * y * z,
    lambda x, y, z: z * z * z,
)

# Finite-difference step (in normalized-degree units) for the Newton-Raphson Jacobian,
# and iteration limits/convergence tolerance (in pixels) for the inverse solve. RPC
# models are smooth and well-conditioned in practice, so this converges in a handful of
# iterations for any point actually within (or near) the image's own footprint.
_JACOBIAN_EPS_DEGREES = 1e-6
_INVERSE_TOLERANCE_PIXELS = 1e-6
_INVERSE_MAX_ITERATIONS = 30


def _rational(num_coeff: list[float], den_coeff: list[float], x: float, y: float, z: float) -> float:
    num = sum(c * term(x, y, z) for c, term in zip(num_coeff, _RPC_BASIS))
    den = sum(c * term(x, y, z) for c, term in zip(den_coeff, _RPC_BASIS))
    return num / den


def rpc_forward(rpc, lon: float, lat: float, height: float = 0.0) -> tuple[float, float]:
    """Ground (lon, lat, height) -> image (col/sample, row/line), via direct evaluation
    of the RPC model. `rpc` is a `rasterio.rpc.RPC` (or anything exposing the same
    `*_off`/`*_scale`/`*_num_coeff`/`*_den_coeff` attributes)."""
    x = (lon - rpc.long_off) / rpc.long_scale
    y = (lat - rpc.lat_off) / rpc.lat_scale
    z = (height - rpc.height_off) / rpc.height_scale
    line = rpc.line_off + rpc.line_scale * _rational(rpc.line_num_coeff, rpc.line_den_coeff, x, y, z)
    samp = rpc.samp_off + rpc.samp_scale * _rational(rpc.samp_num_coeff, rpc.samp_den_coeff, x, y, z)
    return samp, line


def rpc_inverse(rpc, col: float, row: float, height: float = 0.0) -> tuple[float, float]:
    """Image (col/sample, row/line) -> ground (lon, lat), via Newton-Raphson inversion of
    `rpc_forward` (RPC has no closed-form inverse). Raises `ValueError` if it fails to
    converge -- e.g. for a pixel far outside the raster's own footprint, where the
    RPC model's polynomial approximation is no longer meaningful.
    """
    lon, lat = rpc.long_off, rpc.lat_off
    eps = _JACOBIAN_EPS_DEGREES

    for _ in range(_INVERSE_MAX_ITERATIONS):
        samp, line = rpc_forward(rpc, lon, lat, height)
        d_col, d_row = col - samp, row - line
        if abs(d_col) < _INVERSE_TOLERANCE_PIXELS and abs(d_row) < _INVERSE_TOLERANCE_PIXELS:
            return lon, lat

        samp_dlon, line_dlon = rpc_forward(rpc, lon + eps, lat, height)
        samp_dlat, line_dlat = rpc_forward(rpc, lon, lat + eps, height)
        j_col_lon = (samp_dlon - samp) / eps
        j_row_lon = (line_dlon - line) / eps
        j_col_lat = (samp_dlat - samp) / eps
        j_row_lat = (line_dlat - line) / eps

        determinant = (j_col_lon * j_row_lat) - (j_col_lat * j_row_lon)
        if determinant == 0:
            raise ValueError(f"RPC inverse solve is degenerate at (col={col}, row={row})")

        d_lon = ((j_row_lat * d_col) - (j_col_lat * d_row)) / determinant
        d_lat = ((-j_row_lon * d_col) + (j_col_lon * d_row)) / determinant
        lon += d_lon
        lat += d_lat

    raise ValueError(
        f"RPC inverse solve did not converge for (col={col}, row={row}) after "
        f"{_INVERSE_MAX_ITERATIONS} iterations"
    )
