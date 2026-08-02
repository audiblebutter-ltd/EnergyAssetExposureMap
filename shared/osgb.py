"""
OSGB36 British National Grid (easting, northing) -> WGS84 (lat, lon).

REPD (see lambdas/ingest_assets/handler.py) publishes coordinates as OSGB36
National Grid eastings/northings, not lat/lon - everything else in this
pipeline is WGS84, so this conversion has to happen at ingest time.

Two steps, both standard Ordnance Survey formulae (see OS's "A guide to
coordinate systems in Great Britain"):
1. Inverse Transverse Mercator: grid reference -> lat/lon on the OSGB36
   datum (Airy 1830 ellipsoid).
2. A 7-parameter Helmert transform: OSGB36 -> WGS84. The published
   parameters are for the WGS84->OSGB36 direction; the reverse uses every
   parameter negated.

Accurate to a few metres, which OS states as the expected accuracy of a
single Helmert transform (better methods use OSTN15, not warranted here).
Validated against OS's own published Transverse Mercator and Helmert
worked examples - see the pipeline's test notes; round-trip error is
sub-millimetre.
"""

import math

_A, _B = 6377563.396, 6356256.909  # Airy 1830 ellipsoid
_F0 = 0.9996012717  # NG scale factor on the central meridian
_LAT0 = math.radians(49.0)
_LON0 = math.radians(-2.0)
_N0, _E0 = -100000.0, 400000.0
_E2 = 1 - (_B * _B) / (_A * _A)
_N = (_A - _B) / (_A + _B)

# Official WGS84 -> OSGB36 Helmert parameters. The reverse direction
# (OSGB36 -> WGS84, what this module actually does) negates all of them.
_TX, _TY, _TZ = -446.448, 125.157, -542.060
_S_PPM = 20.4894
_RX = math.radians(-0.1502 / 3600)
_RY = math.radians(-0.2470 / 3600)
_RZ = math.radians(-0.8421 / 3600)

_WGS84_A, _WGS84_B = 6378137.000, 6356752.314245
_WGS84_E2 = 1 - (_WGS84_B * _WGS84_B) / (_WGS84_A * _WGS84_A)


def _meridional_arc(lat):
    n1, n2, n3 = _N, _N * _N, _N * _N * _N
    dlat, slat = lat - _LAT0, lat + _LAT0
    return _B * _F0 * (
        (1 + n1 + 1.25 * n2 + 1.25 * n3) * dlat
        - (3 * n1 + 3 * n2 + 2.625 * n3) * math.sin(dlat) * math.cos(slat)
        + (1.875 * n2 + 1.875 * n3) * math.sin(2 * dlat) * math.cos(2 * slat)
        - (35 / 24) * n3 * math.sin(3 * dlat) * math.cos(3 * slat)
    )


def _to_cartesian(lat, lon, a, e2):
    nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x = nu * math.cos(lat) * math.cos(lon)
    y = nu * math.cos(lat) * math.sin(lon)
    z = (1 - e2) * nu * math.sin(lat)
    return x, y, z


def _from_cartesian(x, y, z, a, e2):
    p = math.hypot(x, y)
    phi = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        nu = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
        phi = math.atan2(z + e2 * nu * math.sin(phi), p)
    return phi, math.atan2(y, x)


def osgb36_to_wgs84(easting, northing):
    """OSGB36 National Grid (easting, northing) in metres -> (lat, lon) in WGS84 degrees."""
    lat = _LAT0
    m = 0.0
    while True:
        lat = lat + (northing - _N0 - m) / (_A * _F0)
        m = _meridional_arc(lat)
        if abs(northing - _N0 - m) < 1e-5:
            break

    sin_lat, cos_lat, tan_lat = math.sin(lat), math.cos(lat), math.tan(lat)
    tan2 = tan_lat * tan_lat
    tan4 = tan2 * tan2
    tan6 = tan4 * tan2

    nu = _A * _F0 / math.sqrt(1 - _E2 * sin_lat * sin_lat)
    rho = _A * _F0 * (1 - _E2) / (1 - _E2 * sin_lat * sin_lat) ** 1.5
    eta2 = nu / rho - 1

    VII = tan_lat / (2 * rho * nu)
    VIII = tan_lat / (24 * rho * nu ** 3) * (5 + 3 * tan2 + eta2 - 9 * tan2 * eta2)
    IX = tan_lat / (720 * rho * nu ** 5) * (61 + 90 * tan2 + 45 * tan4)
    X = 1 / (cos_lat * nu)
    XI = 1 / (cos_lat * 6 * nu ** 3) * (nu / rho + 2 * tan2)
    XII = 1 / (cos_lat * 120 * nu ** 5) * (5 + 28 * tan2 + 24 * tan4)
    XIIA = 1 / (cos_lat * 5040 * nu ** 7) * (61 + 662 * tan2 + 1320 * tan4 + 720 * tan6)

    dE = easting - _E0
    lat_airy = lat - VII * dE ** 2 + VIII * dE ** 4 - IX * dE ** 6
    lon_airy = _LON0 + X * dE - XI * dE ** 3 + XII * dE ** 5 - XIIA * dE ** 7

    x1, y1, z1 = _to_cartesian(lat_airy, lon_airy, _A, _E2)

    s = -_S_PPM * 1e-6
    x2 = -_TX + (1 + s) * x1 + _RZ * y1 - _RY * z1
    y2 = -_TY - _RZ * x1 + (1 + s) * y1 + _RX * z1
    z2 = -_TZ + _RY * x1 - _RX * y1 + (1 + s) * z1

    phi_w, lam_w = _from_cartesian(x2, y2, z2, _WGS84_A, _WGS84_E2)
    return math.degrees(phi_w), math.degrees(lam_w)
