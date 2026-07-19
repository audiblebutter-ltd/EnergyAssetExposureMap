"""
Local verification harness - no AWS required.

Calls each Lambda's fetch functions directly (bypassing S3/boto3 writes)
against the real public APIs, runs the join logic against the results, and
writes everything to ./local_dev_output/ for manual inspection. This is how
each source integration and the join logic get confirmed to actually work
before any AWS deployment.

Usage: python scripts/run_local.py [--skip-slow]
  --skip-slow  skip the OSM Overpass and full-GB flood/earthquake pulls,
               which can take a while against the public rate-limited APIs
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# The handler modules read these at import time; local runs never touch S3.
os.environ.setdefault("S3_BUCKET_RAW", "local-dev-unused")
os.environ.setdefault("S3_BUCKET_PROCESSED", "local-dev-unused")

from lambdas.ingest_assets import handler as ingest_assets  # noqa: E402
from lambdas.ingest_hazards import handler as ingest_hazards  # noqa: E402
from lambdas.join import handler as join  # noqa: E402

OUT_DIR = ROOT / "local_dev_output"


def _write(name, data):
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  wrote {path} ({len(json.dumps(data))} bytes)")


def main():
    skip_slow = "--skip-slow" in sys.argv

    print("Fetching assets...")
    all_assets = []
    sources = [
        ("crown_estate_ewni", ingest_assets.fetch_crown_estate_ewni),
        ("crown_estate_scotland", ingest_assets.fetch_crown_estate_scotland),
        ("nsta", ingest_assets.fetch_nsta_platforms),
    ]
    if not skip_slow:
        sources.append(("osm_overpass", ingest_assets.fetch_osm_power_infrastructure))

    for name, fetch_fn in sources:
        try:
            assets = fetch_fn()
            print(f"  {name}: {len(assets)} assets")
            _write(f"assets_{name}", assets)
            all_assets.extend(assets)
        except Exception as e:
            print(f"  {name}: FAILED ({e})")

    if skip_slow:
        print("  osm_overpass: skipped (--skip-slow)")

    print("Fetching hazards...")
    flood_features = [] if skip_slow else ingest_hazards.fetch_flood_zones()
    print(f"  flood_zones: {len(flood_features)} features" + (" (skipped)" if skip_slow else ""))
    _write("hazards_flood_zones", flood_features)

    quake_features = [] if skip_slow else ingest_hazards.fetch_earthquakes()
    print(f"  earthquakes: {len(quake_features)} features" + (" (skipped)" if skip_slow else ""))
    _write("hazards_earthquakes", quake_features)

    with open(ROOT / "data" / "storm_events.json", encoding="utf-8") as f:
        storm_catalogue = json.load(f)
    print(f"  storms: {len(storm_catalogue['storms'])} events (static catalogue)")
    _write("hazards_storms", storm_catalogue)

    print("Running join...")
    exposure = join.build_exposure(all_assets, flood_features, quake_features, storm_catalogue)
    exposure["summary"] = {
        "asset_count": len(exposure["features"]),
        "exposed_to_flood": sum(1 for f in exposure["features"] if f["properties"]["hazards"]["flood"]["exposed"]),
        "exposed_to_earthquake": sum(
            1 for f in exposure["features"] if f["properties"]["hazards"]["earthquake"]["exposed"]
        ),
    }
    print(f"  {exposure['summary']}")
    _write("exposure", exposure)

    print(f"\nDone. See {OUT_DIR}/")


if __name__ == "__main__":
    main()
