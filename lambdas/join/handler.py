"""
join Lambda.
Triggered by S3 ObjectCreated on raw/<source>/.../snapshot.json (any source -
the event just signals "something changed", the handler always recomputes
from whatever the latest snapshot of every source currently is, same
batch-recompute approach as the Global Shock transformer).

Reads the latest asset + hazard raw snapshots, joins them by proximity
(pure-Python haversine distance - see shared/geo.py for why this isn't a
true polygon intersection), and writes the result as GeoJSON to both a
dated snapshot and the always-overwritten processed/latest.json.
"""

import logging
import os
from datetime import datetime, timezone

from shared import s3_helpers, geo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET_RAW = os.environ["S3_BUCKET_RAW"]
S3_BUCKET_PROCESSED = os.environ["S3_BUCKET_PROCESSED"]

ASSET_SOURCES = ["crown_estate_ewni", "crown_estate_scotland", "nsta", "osm_overpass"]
HAZARD_SOURCES = ["flood_zones", "earthquakes", "storms"]

# Distance thresholds (km) for a "close" exposure flag, per hazard type.
FLOOD_ZONE_THRESHOLD_KM = 2.0
EARTHQUAKE_THRESHOLD_KM = 25.0
STORM_SEVERE_CATEGORIES = {"severe", "extreme"}


def _load_latest_or_fallback(bucket, prefix, source_name, existing_by_source, key_in_snapshot):
    """
    Read the latest raw snapshot for a source. If none exists (source down,
    or its raw snapshot already expired under the bucket's lifecycle
    policy), fall back to whatever was in the last published processed
    output for that source, so a single failing source doesn't blank out
    previously-known assets/hazards.
    """
    latest_key = s3_helpers.latest_snapshot_key(bucket, prefix)
    if latest_key:
        snapshot = s3_helpers.read_json(bucket, latest_key)
        if snapshot:
            return snapshot.get(key_in_snapshot, [])

    logger.error(f"No raw snapshot for {source_name}, falling back to last published output")
    return existing_by_source.get(source_name, [])


def _existing_assets_by_source(existing_processed):
    """
    Rebuild fallback asset dicts from previously-published GeoJSON. Assets
    can be Point (OSM, NSTA) or Polygon/MultiPolygon (Crown Estate lease
    sites), so lat/lon must go through geometry_centroid rather than
    assuming coordinates[0]/[1] - that would silently produce garbage for
    polygon assets falling back through this path.
    """
    by_source = {}
    for feature in existing_processed.get("features", []):
        source = feature.get("properties", {}).get("source")
        try:
            lat, lon = geo.geometry_centroid(feature["geometry"])
        except (KeyError, ValueError):
            continue
        by_source.setdefault(source, []).append(
            {
                "id": feature["properties"]["id"],
                "source": source,
                "type": feature["properties"].get("type"),
                "name": feature["properties"].get("name"),
                "lat": lat,
                "lon": lon,
                "geometry": feature["geometry"],
                "raw_properties": {},
            }
        )
    return by_source


def _hazard_centroids(features):
    out = []
    for f in features:
        try:
            lat, lon = geo.geometry_centroid(f["geometry"])
        except (KeyError, ValueError):
            continue
        out.append({"centroid": (lat, lon), "properties": f.get("properties", {})})
    return out


def build_exposure(assets, flood_features, earthquake_features, storm_catalogue):
    """Pure join logic - takes plain Python data, returns a GeoJSON FeatureCollection dict."""
    flood_hazards = _hazard_centroids(flood_features)
    quake_hazards = _hazard_centroids(earthquake_features)

    severe_storms = [s for s in storm_catalogue.get("storms", []) if s.get("category") in STORM_SEVERE_CATEGORIES]
    storm_summary = {
        "total_named_storms": len(storm_catalogue.get("storms", [])),
        "severe_or_extreme_count": len(severe_storms),
        "most_recent_severe": max((s["date"] for s in severe_storms), default=None),
    }

    out_features = []
    for asset in assets:
        lat, lon = asset["lat"], asset["lon"]

        flood_dist, _ = geo.nearest_feature(lat, lon, flood_hazards)
        quake_dist, quake_match = geo.nearest_feature(lat, lon, quake_hazards)

        hazards = {
            "flood": {
                "nearest_zone_km": round(flood_dist, 2) if flood_dist is not None else None,
                "exposed": flood_dist is not None and flood_dist <= FLOOD_ZONE_THRESHOLD_KM,
            },
            "earthquake": {
                "nearest_epicentre_km": round(quake_dist, 2) if quake_dist is not None else None,
                "nearest_magnitude_ml": (quake_match or {}).get("properties", {}).get("ml") if quake_match else None,
                "exposed": quake_dist is not None and quake_dist <= EARTHQUAKE_THRESHOLD_KM,
            },
            "storm": storm_summary,
        }

        out_features.append(
            {
                "type": "Feature",
                "geometry": asset["geometry"],
                "properties": {
                    "id": asset["id"],
                    "source": asset["source"],
                    "type": asset["type"],
                    "name": asset["name"],
                    "hazards": hazards,
                },
            }
        )

    return {"type": "FeatureCollection", "features": out_features}


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y/%m/%d")

    existing_processed = s3_helpers.load_existing_processed(S3_BUCKET_PROCESSED)
    existing_by_source = _existing_assets_by_source(existing_processed)

    assets = []
    for source in ASSET_SOURCES:
        assets.extend(
            _load_latest_or_fallback(S3_BUCKET_RAW, f"raw/{source}/", source, existing_by_source, "assets")
        )

    flood_features = _load_latest_or_fallback(S3_BUCKET_RAW, "raw/flood_zones/", "flood_zones", {}, "features")
    earthquake_features = _load_latest_or_fallback(S3_BUCKET_RAW, "raw/earthquakes/", "earthquakes", {}, "features")

    storm_key = s3_helpers.latest_snapshot_key(S3_BUCKET_RAW, "raw/storms/")
    storm_snapshot = (s3_helpers.read_json(S3_BUCKET_RAW, storm_key) if storm_key else None) or {"storms": []}

    exposure = build_exposure(assets, flood_features, earthquake_features, storm_snapshot)
    exposure["schema_version"] = "1.0"
    exposure["processed_at"] = now.isoformat()
    exposure["summary"] = {
        "asset_count": len(exposure["features"]),
        "by_source": {
            source: sum(1 for f in exposure["features"] if f["properties"]["source"] == source)
            for source in ASSET_SOURCES
        },
    }

    dated_key = f"processed/{date_prefix}/exposure.json"
    s3_helpers.write_json(S3_BUCKET_PROCESSED, dated_key, exposure)
    s3_helpers.write_json(S3_BUCKET_PROCESSED, "processed/latest.json", exposure)

    logger.info(f"Wrote exposure map with {len(exposure['features'])} assets to {dated_key} and processed/latest.json")
    return {"statusCode": 200, "body": {"asset_count": len(exposure["features"])}}
