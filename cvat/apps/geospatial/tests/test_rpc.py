# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest
from rasterio.rpc import RPC

from cvat.apps.geospatial.rpc import rpc_forward, rpc_inverse


@pytest.fixture
def rpc() -> RPC:
    # Real RPC coefficients extracted from an actual test raster during development
    # (see the module docstring in cvat.apps.geospatial.rpc for why the term order
    # needs this level of real-data validation rather than a synthetic toy case).
    return RPC(
        height_off=4.99453663841698e-07,
        height_scale=4.9841814710086e-07,
        lat_off=1.27874544125422,
        lat_scale=0.0313657305943377,
        long_off=103.700231628803,
        long_scale=0.0311602779357258,
        line_off=5876.0,
        line_scale=5876.0,
        samp_off=5876.0,
        samp_scale=5876.0,
        line_den_coeff=[
            1.0, -5.79196377315231e-11, -5.92610266444257e-11, -1.51440683613753e-10,
            -1.72604954387866e-10, -5.20465260173656e-11, -5.26971555385119e-11,
            -7.90173582601701e-10, -7.80232110239572e-10, -9.66893238094063e-10,
            -2.42895981096482e-14, -1.8506030043109e-14, -2.47024622897139e-14,
            -8.85402862216524e-15, -6.96664947879487e-15, -4.06974801121679e-14,
            -5.80091530655333e-15, 1.77774461771724e-14, 3.01841884783441e-15,
            -4.99600360588141e-15,
        ],
        line_num_coeff=[
            1.71113123671933e-14, -0.235660424470987, 1.15557850055356, -6.49480469320101e-15,
            -5.29801624646486e-11, 3.56683252847413e-11, -1.75000418468736e-10,
            1.36538082993531e-11, -6.85149421921328e-11, 3.63598040492783e-15,
            -4.77273443482196e-11, 1.86211679570268e-10, -1.55744191648993e-11,
            2.27851587137063e-10, -8.72430847853791e-10, -9.01640434371812e-10,
            -1.1173223757901e-09, 1.22655003441321e-11, -6.08789186866504e-11,
            5.52335954700714e-15,
        ],
        samp_den_coeff=[
            1.0, 1.58929373134109e-10, -1.82704751501561e-09, -3.84743428782919e-09,
            3.75714503779434e-09, -5.60772650537444e-10, -1.12605945401523e-09,
            -1.80380948580638e-08, -1.79954846163399e-08, -2.22150957351896e-08,
            -7.97070742741823e-14, -5.29264132520524e-14, -6.24812701577326e-14,
            8.9303564543286e-15, 6.59194920871187e-14, 2.21826029767058e-13,
            5.30339661075629e-14, 4.14113188185183e-14, 1.01224584270199e-13,
            4.01761957036229e-14,
        ],
        samp_num_coeff=[
            3.9267200602211e-14, -1.15645116019775, -0.236533084115114, -3.84137166520304e-14,
            2.07517390944656e-09, 4.44923664577601e-09, 9.10029829270798e-10,
            -1.83750134899463e-10, 4.32121158300269e-10, 1.20528587110869e-14,
            1.43485699363732e-09, 2.08601695222965e-08, 1.99222819213807e-08,
            2.56906343976127e-08, -7.83457587000269e-11, 4.25643484347482e-09,
            5.25459870059919e-09, 6.48499365318855e-10, 2.66431321449545e-10,
            3.11001224773122e-14,
        ],
    )


# Independently-known (pixel <-> ground) correspondences for the *same* scene, taken
# from a separately-created GCP-georeferenced test raster of identical dimensions
# (11752x10797) -- not derived from the RPC model at all, so this validates the term
# order/implementation against real ground truth rather than only round-tripping
# through itself (see the module docstring's warning about the reference-point
# self-test being unable to catch an X/Y term swap).
KNOWN_CORNERS = [
    (0, 0, 103.73139356531533, 1.2579991020848331),
    (0, 10796, 103.72166647567185, 1.3058719989667509),
    (11751, 0, 103.67966580125598, 1.2473806534426675),
    (11751, 10796, 103.66993857850792, 1.2952510979685707),
]


@pytest.mark.parametrize("col,row,lon,lat", KNOWN_CORNERS)
def test_rpc_forward_matches_known_ground_truth(rpc, col, row, lon, lat):
    got_col, got_row = rpc_forward(rpc, lon, lat, height=0.0)
    assert got_col == pytest.approx(col, abs=1.0)
    assert got_row == pytest.approx(row, abs=1.0)


@pytest.mark.parametrize("col,row,lon,lat", KNOWN_CORNERS)
def test_rpc_inverse_matches_known_ground_truth(rpc, col, row, lon, lat):
    got_lon, got_lat = rpc_inverse(rpc, col, row, height=0.0)
    assert got_lon == pytest.approx(lon, abs=1e-4)
    assert got_lat == pytest.approx(lat, abs=1e-4)


def test_rpc_inverse_is_a_left_inverse_of_forward(rpc):
    # Round-trips at an arbitrary interior point too, not just the corners.
    lon, lat = 103.7, 1.28
    col, row = rpc_forward(rpc, lon, lat)
    got_lon, got_lat = rpc_inverse(rpc, col, row)
    assert got_lon == pytest.approx(lon, abs=1e-9)
    assert got_lat == pytest.approx(lat, abs=1e-9)
