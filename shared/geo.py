"""
Minimal pure-Python geospatial helpers for the join Lambda.

v1 of the join is point-to-point proximity, not true polygon-boundary
intersection, so no shapely/geopandas Lambda Layer is needed here -
matches the Global Shock pipeline's minimal-dependency convention
(stdlib + boto3 only). Polygon hazards/assets are reduced to their
centroid before distance is measured. If point-based proximity proves
too coarse later, swapping in a shapely layer for real intersection is
the documented upgrade path - this module's public functions
(nearest_feature) are the seam to change.
"""

import math


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _ring_centroid(ring):
    lons = [c[0] for c in ring]
    lats = [c[1] for c in ring]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def geometry_centroid(geometry):
    """Return (lat, lon) for a GeoJSON Point/Polygon/MultiPolygon geometry."""
    gtype = geometry["type"]
    coords = geometry["coordinates"]

    if gtype == "Point":
        lon, lat = coords
        return lat, lon

    if gtype == "Polygon":
        return _ring_centroid(coords[0])

    if gtype == "MultiPolygon":
        lats, lons = [], []
        for polygon in coords:
            lat, lon = _ring_centroid(polygon[0])
            lats.append(lat)
            lons.append(lon)
        return sum(lats) / len(lats), sum(lons) / len(lons)

    raise ValueError(f"Unsupported geometry type for centroid: {gtype}")


def nearest_feature(lat, lon, features, centroid_key="centroid"):
    """
    features: list of dicts each carrying a precomputed [lat, lon] under
    centroid_key. Returns (distance_km, feature) for the closest one, or
    (None, None) if features is empty.
    """
    if not features:
        return None, None

    best_dist, best_feature = None, None
    for feature in features:
        f_lat, f_lon = feature[centroid_key]
        dist = haversine_km(lat, lon, f_lat, f_lon)
        if best_dist is None or dist < best_dist:
            best_dist, best_feature = dist, feature
    return best_dist, best_feature
