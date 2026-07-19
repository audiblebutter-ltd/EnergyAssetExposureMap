"""
ingest_hazards Lambda.
Triggered by EventBridge on a schedule (monthly - these sources move slowly).
Pulls flood zone and earthquake hazard data from public sources, writes one
dated raw snapshot per source to S3, and copies the hand-curated storm
catalogue (data/storm_events.json, bundled into the deployment zip) into the
same raw prefix so the join Lambda can read all three hazards the same way.
"""

import json
import logging
import os
import signal
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from shared import s3_helpers

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET_RAW = os.environ["S3_BUCKET_RAW"]

EA_FLOOD_ZONES_WFS_URL = "https://environment.data.gov.uk/spatialdata/flood-map-for-planning-flood-zones/wfs"
EA_FLOOD_ZONES_TYPE_NAME = "dataset-04532375-a198-476e-985e-0579a0a11b47:Flood_Zones_2_3_Rivers_and_Sea"

BGS_COLLECTIONS = ["recentearthquakes", "historicalearthquakes"]
BGS_MIN_MAGNITUDE = 2.0  # UK seismicity below ML 2 is not underwriting-relevant

# south, west, north, east - Great Britain mainland + North Sea / UKCS waters
GB_BBOX = (49.8, -8.7, 61.0, 2.0)
GB_BBOX_LONLAT = (-8.7, 49.8, 2.0, 61.0)  # minLon,minLat,maxLon,maxLat order, EA WFS convention

USER_AGENT = "DGEnergyAssetExposureMap/1.0 (ingest_hazards)"
MAX_PAGES = 25  # safety cap against runaway pagination
# Observed running this from a Lambda in eu-west-2: individual requests to
# environment.data.gov.uk take far longer (and vary far more) than the same
# requests from a home/office network - each page took ~2-3s locally but a
# 26-page run didn't finish inside a 600s Lambda invocation from AWS. Rather
# than keep raising the Lambda timeout and hoping, cap total pagination time
# so this degrades to partial data instead of blocking until the function
# is killed. A slow/rate-limiting public API from cloud IP ranges is a real
# characteristic to design around, not just a number to increase.
PAGINATION_TIME_BUDGET_SECONDS = 120


class RequestHardTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise RequestHardTimeout()


def _http_get_json(url, timeout=20):
    """
    urllib's own `timeout` is an inactivity timeout (per connect/recv call),
    not a wall-clock request timeout - a server that trickles bytes slowly
    enough to keep resetting that clock can still make a single call take
    minutes. Observed exactly this against environment.data.gov.uk from a
    Lambda in eu-west-2 (a 26-page pull that took ~90s locally never
    completed a single page inside a 600s Lambda invocation). A SIGALRM
    forces a real wall-clock ceiling regardless of how the server behaves.
    """
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def fetch_flood_zones():
    """
    Flood Zone 2/3 (rivers and sea) polygons for Great Britain, paged.

    Two gotchas confirmed against the live service: the dataset's native CRS
    is EPSG:27700 (British National Grid eastings/northings), so srsName
    must be set explicitly to get WGS84 lon/lat geometry out; and the
    "urn:ogc:def:crs:EPSG::4326" bbox filter uses EPSG-registry axis order,
    which for 4326 is (lat, lon), not (lon, lat) - passing lon,lat silently
    matches zero features rather than erroring.

    Coverage caveat found during local verification: this dataset has
    813,627 features nationwide, so the MAX_PAGES safety cap (25,000
    features) captures only a small, arbitrarily-ordered slice of it, not
    full GB coverage - a production run either needs a much higher cap or
    (better) to subdivide the GB bbox into smaller regional tiles so what's
    fetched is geographically complete rather than just the first N records
    returned. In this pipeline's initial verification run zero assets came
    back "flood exposed", which may be a real result (these EA "rivers and
    sea" zones are largely land-based/coastal and most of our assets are
    well offshore) rather than a coverage gap, but that hasn't been proven
    either way yet - flag before relying on the flood numbers.
    """
    features = []
    start_index = 0
    page_size = 1000
    min_lon, min_lat, max_lon, max_lat = GB_BBOX_LONLAT
    deadline = time.monotonic() + PAGINATION_TIME_BUDGET_SECONDS

    while True:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": EA_FLOOD_ZONES_TYPE_NAME,
            "outputFormat": "json",
            "srsName": "urn:ogc:def:crs:EPSG::4326",
            "count": page_size,
            "startIndex": start_index,
            "bbox": f"{min_lat},{min_lon},{max_lat},{max_lon},urn:ogc:def:crs:EPSG::4326",
        }
        url = f"{EA_FLOOD_ZONES_WFS_URL}?{urllib.parse.urlencode(params)}"
        try:
            page = _http_get_json(url, timeout=20)
        except RequestHardTimeout:
            logger.error(
                f"flood_zones request at startIndex={start_index} hard-timed-out - "
                f"returning {len(features)} features fetched so far"
            )
            break
        page_features = page.get("features", [])
        features.extend(page_features)
        logger.info(f"flood_zones page at startIndex={start_index}: +{len(page_features)} (total {len(features)})")

        if len(page_features) < page_size or start_index // page_size >= MAX_PAGES:
            break
        if time.monotonic() >= deadline:
            logger.error(
                f"flood_zones pagination hit its {PAGINATION_TIME_BUDGET_SECONDS}s time budget "
                f"after {len(features)} features - returning partial data rather than blocking further"
            )
            break
        start_index += page_size

    return features


def _bgs_collection_features(collection):
    features = []
    min_lon, min_lat, max_lon, max_lat = GB_BBOX_LONLAT
    url = (
        f"https://ogcapi.bgs.ac.uk/collections/{collection}/items"
        f"?f=json&limit=1000&bbox={min_lon},{min_lat},{max_lon},{max_lat}"
    )
    pages = 0
    deadline = time.monotonic() + PAGINATION_TIME_BUDGET_SECONDS

    while url and pages < MAX_PAGES and time.monotonic() < deadline:
        try:
            page = _http_get_json(url, timeout=20)
        except RequestHardTimeout:
            logger.error(f"{collection} request hard-timed-out on page {pages} - returning {len(features)} kept so far")
            break
        page_features = page.get("features", [])
        for f in page_features:
            if (f.get("properties", {}).get("ml") or 0) >= BGS_MIN_MAGNITUDE:
                f["properties"]["_collection"] = collection
                features.append(f)

        logger.info(f"{collection} page {pages}: {len(page_features)} raw, {len(features)} kept so far")

        next_link = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)
        url = next_link
        pages += 1

    return features


def fetch_earthquakes():
    """UK earthquakes above BGS_MIN_MAGNITUDE, modern + historical catalogues."""
    features = []
    for collection in BGS_COLLECTIONS:
        features.extend(_bgs_collection_features(collection))
    return features


def load_storm_catalogue():
    """Read the hand-curated storm catalogue bundled alongside this handler."""
    path = Path(__file__).parent / "data" / "storm_events.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y/%m/%d")
    results = []

    try:
        flood_features = fetch_flood_zones()
        key = f"raw/flood_zones/{date_prefix}/snapshot.json"
        s3_helpers.write_json(
            S3_BUCKET_RAW,
            key,
            {"source": "flood_zones", "fetched_at": now.isoformat(), "count": len(flood_features), "features": flood_features},
        )
        logger.info(f"Wrote {len(flood_features)} flood zone features to {key}")
        results.append({"source": "flood_zones", "count": len(flood_features)})
    except Exception as e:
        logger.error(f"flood_zones failed: {e}", exc_info=True)
        results.append({"source": "flood_zones", "error": str(e)})

    try:
        quake_features = fetch_earthquakes()
        key = f"raw/earthquakes/{date_prefix}/snapshot.json"
        s3_helpers.write_json(
            S3_BUCKET_RAW,
            key,
            {"source": "earthquakes", "fetched_at": now.isoformat(), "count": len(quake_features), "features": quake_features},
        )
        logger.info(f"Wrote {len(quake_features)} earthquake features to {key}")
        results.append({"source": "earthquakes", "count": len(quake_features)})
    except Exception as e:
        logger.error(f"earthquakes failed: {e}", exc_info=True)
        results.append({"source": "earthquakes", "error": str(e)})

    try:
        storms = load_storm_catalogue()
        key = f"raw/storms/{date_prefix}/snapshot.json"
        s3_helpers.write_json(
            S3_BUCKET_RAW,
            key,
            {"source": "storms", "fetched_at": now.isoformat(), "count": len(storms["storms"]), **storms},
        )
        logger.info(f"Wrote {len(storms['storms'])} storm events to {key}")
        results.append({"source": "storms", "count": len(storms["storms"])})
    except Exception as e:
        logger.error(f"storms failed: {e}", exc_info=True)
        results.append({"source": "storms", "error": str(e)})

    return {"statusCode": 200, "body": {"results": results}}
