"""
ingest_assets Lambda.
Triggered by EventBridge on a schedule (monthly - these sources move slowly).
Pulls energy asset locations from four public sources (Crown Estate x2,
NSTA, OSM Overpass), normalizes each to a common shape, and writes one
dated raw snapshot per source to S3 at raw/<source>/YYYY/MM/DD/snapshot.json.
"""

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from shared import s3_helpers, geo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET_RAW = os.environ["S3_BUCKET_RAW"]

CROWN_ESTATE_EWNI_URL = (
    "https://services2.arcgis.com/PZklK9Q45mfMFuZs/arcgis/rest/services/"
    "WindSite_EngWalNI_TheCrownEstate/FeatureServer/0/query"
)
CROWN_ESTATE_SCOTLAND_URL = (
    "https://services3.arcgis.com/nGV4jiurzcahJ9LV/arcgis/rest/services/"
    "Offshore_Wind_Crown_Estate_Scotland/FeatureServer/0/query"
)
NSTA_PLATFORMS_URL = (
    "https://services-eu1.arcgis.com/OZMfUznmLTnWccBc/arcgis/rest/services/"
    "UKCS%20offshore%20infrastructure%20surface%20points%20WGS84/FeatureServer/1/query"
)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACK_URL = "https://overpass.kumi.systems/api/interpreter"

# Great Britain mainland + North Sea / UKCS waters, south,west,north,east
GB_BBOX = (49.8, -8.7, 61.0, 2.0)

USER_AGENT = "DGEnergyAssetExposureMap/1.0 (ingest_assets)"


def _http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_arcgis_features(base_url, page_size=1000, timeout=30):
    """Page through an ArcGIS FeatureServer layer, returning all GeoJSON features."""
    features = []
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        page = _http_get_json(url, timeout=timeout)
        page_features = page.get("features", [])
        features.extend(page_features)
        if len(page_features) < page_size:
            break
        offset += page_size
    return features


def fetch_crown_estate_ewni():
    """Offshore wind site agreements - England, Wales & Northern Ireland."""
    raw_features = _fetch_arcgis_features(CROWN_ESTATE_EWNI_URL)
    assets = []
    for f in raw_features:
        lat, lon = geo.geometry_centroid(f["geometry"])
        props = f.get("properties", {})
        assets.append(
            {
                "id": f"crown_estate_ewni:{f.get('id')}",
                "source": "crown_estate_ewni",
                "type": "offshore_wind_lease",
                "name": props.get("SITE_NAME") or props.get("NAME") or "Unknown",
                "lat": lat,
                "lon": lon,
                "geometry": f["geometry"],
                "raw_properties": props,
            }
        )
    return assets


def fetch_crown_estate_scotland():
    """Offshore wind - Crown Estate Scotland."""
    raw_features = _fetch_arcgis_features(CROWN_ESTATE_SCOTLAND_URL)
    assets = []
    for f in raw_features:
        lat, lon = geo.geometry_centroid(f["geometry"])
        props = f.get("properties", {})
        assets.append(
            {
                "id": f"crown_estate_scotland:{f.get('id')}",
                "source": "crown_estate_scotland",
                "type": "offshore_wind_lease",
                "name": props.get("Tenant_Name") or props.get("Property_Description") or "Unknown",
                "lat": lat,
                "lon": lon,
                "geometry": f["geometry"],
                "raw_properties": props,
            }
        )
    return assets


def fetch_nsta_platforms():
    """Active oil & gas platforms / installations on the UK Continental Shelf."""
    raw_features = _fetch_arcgis_features(NSTA_PLATFORMS_URL)
    assets = []
    for f in raw_features:
        props = f.get("properties", {})
        if props.get("STATUS") != "ACTIVE":
            continue
        lat, lon = geo.geometry_centroid(f["geometry"])
        assets.append(
            {
                "id": f"nsta:{props.get('FEATURE_ID') or f.get('id')}",
                "source": "nsta",
                "type": props.get("INF_TYPE", "platform").lower(),
                "name": props.get("NAME") or "Unknown",
                "lat": lat,
                "lon": lon,
                "geometry": f["geometry"],
                "raw_properties": props,
            }
        )
    return assets


def _overpass_query():
    south, west, north, east = GB_BBOX
    return f"""[out:json][timeout:60];
(
  node["power"="plant"]({south},{west},{north},{east});
  node["power"="substation"]({south},{west},{north},{east});
  node["power"="generator"]["generator:source"~"wind|gas|oil"]({south},{west},{north},{east});
);
out body;"""


def fetch_osm_power_infrastructure():
    """Power plants, substations and generators from OSM via Overpass."""
    query = _overpass_query()
    data = urllib.parse.urlencode({"data": query}).encode()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    result = None
    for url in (OVERPASS_URL, OVERPASS_FALLBACK_URL, OVERPASS_URL, OVERPASS_FALLBACK_URL):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=75) as resp:
                candidate = json.loads(resp.read())
            if candidate.get("remark"):
                # The free public instance returns HTTP 200 with a "remark" and a
                # truncated/empty element list when it's too busy to fully answer -
                # that's a soft failure, not a real "no infrastructure found" result.
                logger.error(f"Overpass at {url} returned a remark, treating as failure: {candidate['remark']}")
                continue
            result = candidate
            break
        except Exception as e:
            logger.error(f"Overpass request to {url} failed: {e}", exc_info=True)

    if result is None:
        raise RuntimeError("All Overpass endpoints failed or returned no usable data after retries")

    assets = []
    for el in result.get("elements", []):
        tags = el.get("tags", {})
        assets.append(
            {
                "id": f"osm:{el.get('type')}/{el.get('id')}",
                "source": "osm_overpass",
                "type": tags.get("power", "unknown"),
                "name": tags.get("name", "Unknown"),
                "lat": el.get("lat"),
                "lon": el.get("lon"),
                "geometry": {"type": "Point", "coordinates": [el.get("lon"), el.get("lat")]},
                "raw_properties": tags,
            }
        )
    return assets


SOURCES = {
    "crown_estate_ewni": fetch_crown_estate_ewni,
    "crown_estate_scotland": fetch_crown_estate_scotland,
    "nsta": fetch_nsta_platforms,
    "osm_overpass": fetch_osm_power_infrastructure,
}


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y/%m/%d")
    results = []

    for source_name, fetch_fn in SOURCES.items():
        try:
            assets = fetch_fn()
            key = f"raw/{source_name}/{date_prefix}/snapshot.json"
            s3_helpers.write_json(
                S3_BUCKET_RAW,
                key,
                {
                    "source": source_name,
                    "fetched_at": now.isoformat(),
                    "count": len(assets),
                    "assets": assets,
                },
            )
            logger.info(f"Wrote {len(assets)} assets from {source_name} to {key}")
            results.append({"source": source_name, "count": len(assets)})
        except Exception as e:
            logger.error(f"Source {source_name} failed: {e}", exc_info=True)
            results.append({"source": source_name, "error": str(e)})

    return {"statusCode": 200, "body": {"results": results}}
