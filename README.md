# EnergyAssetExposureMap

Maps Britain's energy infrastructure (offshore wind, oil & gas platforms, power infrastructure) against natural hazard data (storm tracks, flood zones, seismic activity) to answer a simple question: what assets are on risk, and where. Built entirely from public data sources, with the pipeline and output left open for anyone to explore.

## Architecture

The pipeline follows the same shape as the [Global Shock oil price pipeline](https://dataandgrit.energy): one CloudFormation stack, S3 for raw/processed data, Lambda for compute, EventBridge for scheduling.

**Ingestion**
- One Lambda per data type, each pulling from several public APIs and writing one raw snapshot per source to S3:
  - Assets (`lambdas/ingest_assets`): Crown Estate offshore wind leases (England/Wales/NI and Scotland, separate portals), NSTA active oil & gas platforms, OpenStreetMap power infrastructure via Overpass. EIA was considered and dropped - it's US-focused and doesn't cover UK assets.
  - Hazards (`lambdas/ingest_hazards`): Environment Agency flood zones (WFS), BGS earthquake catalogues (modern + historical, magnitude-filtered), and a hand-curated storm catalogue (`data/storm_events.json`) - there's no clean live API for UK/North Sea storm tracks, so this one is compiled once rather than fetched, same approach the Global Shock pipeline uses for its event catalogue.
- Raw bucket keeps one dated snapshot per source: `raw/<source>/YYYY/MM/DD/snapshot.json`
- Exact endpoints, formats, licenses and quirks found while building this are documented in [`docs/data-sources.md`](docs/data-sources.md).

**Join**
- A join Lambda, triggered by S3 `ObjectCreated` on `raw/`, reads the latest asset and hazard snapshots and joins them by proximity (haversine distance to the nearest hazard, polygons reduced to a centroid - see `shared/geo.py` for why this is v1 and not true polygon intersection). Storms are a national-scale hazard (no per-storm geometry), so they're summarized per asset rather than distance-joined.
- Writes both a dated snapshot (`processed/YYYY/MM/DD/exposure.json`) and an overwritten `processed/latest.json` for the frontend to read. If a source's raw snapshot is missing (expired, or that ingestion run failed), the join falls back to whatever was in the last published output for that source rather than dropping it.

**Scheduling**
- EventBridge triggers both ingestion Lambdas monthly (these sources don't move daily like oil prices)

**Frontend**
- Live at [dataandgrit.energy/energy-asset-exposure-map](https://dataandgrit.energy/energy-asset-exposure-map) - `processed/latest.json` is fetched directly from S3 client-side (no API Gateway layer), same pattern as the oil price pipeline. The processed bucket allows public `s3:GetObject` scoped to `processed/*` only via a bucket policy (`infra/cloudformation.yaml`'s `ProcessedBucketPolicy`); the raw bucket stays fully private throughout.

## Repo structure

```text
EnergyAssetExposureMap/
├── docs/
│   └── data-sources.md      # endpoint/license/quirk research for every source
├── data/
│   └── storm_events.json    # hand-curated storm catalogue
├── lambdas/
│   ├── ingest_assets/handler.py
│   ├── ingest_hazards/handler.py
│   └── join/handler.py
├── shared/
│   ├── s3_helpers.py        # S3 read/write/list, seed-from-published resilience
│   └── geo.py                # haversine distance, GeoJSON centroid, nearest-feature
├── scripts/
│   └── run_local.py         # runs every fetch + the join locally, no AWS needed
├── infra/
│   └── cloudformation.yaml
├── deploy.sh
├── README.md
└── .gitignore
```

## Status

Data engineering is built, **deployed, and verified end-to-end in AWS**: stack `energy-asset-exposure-map-dev` in `eu-west-2`. `processed/latest.json` currently holds 395 real energy assets (72 Crown Estate England/Wales/NI leases, 58 Crown Estate Scotland leases, 265 NSTA platforms), each joined against flood/earthquake/storm hazard data. The processed bucket is public-read (scoped to `processed/*`), and the interactive map frontend and write-up are live at [dataandgrit.energy/energy-asset-exposure-map](https://dataandgrit.energy/energy-asset-exposure-map).

Deploying against real AWS - not just local testing - surfaced three problems worth knowing about if this pipeline gets touched again:

1. **Two circular CloudFormation dependencies.** `JoinFunction`'s env vars and the shared IAM role's inline policy both referenced the bucket via `!Ref`/`!GetAtt`, which loops back since the raw bucket's S3 notification depends on `JoinFunction`. Fixed by using deterministic `!Sub` ARN strings (bucket names have no random suffix, so this is safe) instead of resource references, for every edge that would otherwise close the loop. Caught by `aws cloudformation validate-template`, not by eyeballing the YAML.
2. **A hang specific to AWS's network path.** The EA flood-zone WFS pagination that took ~90s from a home/office network never completed inside a 600s Lambda invocation from `eu-west-2` - `urllib`'s `timeout` parameter is an inactivity timeout (resets on any received byte), not a wall-clock one, so a server trickling data slowly can dodge it indefinitely. Fixed with a `SIGALRM`-based hard timeout per request in `lambdas/ingest_hazards/handler.py`, plus an overall pagination time budget so a slow source degrades to partial data instead of blocking until killed.
3. **CPU starvation at low memory.** Once fetching was fixed, serializing and uploading the ~26,000-feature flood zone payload silently ate the rest of the timeout budget at 512MB. Lambda allocates CPU proportional to memory, so bumping `ingest_hazards` to 2048MB fixed it - confirmed by CloudWatch logs showing all pages fetched quickly, then nothing until the timeout kill, which is what pointed at post-fetch processing rather than the network.

**Known limitations, left as-is rather than hidden:**
- `osm_overpass` (power infrastructure via OSM) is at 0 assets - the public Overpass API was down/overloaded for this entire build session (confirmed via repeated retries against both the primary and a fallback mirror). The code correctly reports this as a per-source failure rather than faking an empty-but-successful result, and the join Lambda falls back to the last published output for that source rather than dropping its assets. It should pick up real data automatically on the next scheduled run once the public API recovers.
- Flood zone coverage is capped at 26,000 of 813,627 total nationwide features (`MAX_PAGES` safety cap in `lambdas/ingest_hazards/handler.py`) - full national coverage needs the GB bbox subdivided into smaller regional tiles rather than one large paged pull.
- All 395 assets currently in the dataset are offshore or coastal (Crown Estate leases, NSTA platforms) - there's no onshore energy infrastructure (nuclear, solar, onshore wind, battery storage, refineries) in scope yet. That's why flood exposure sits at 0% across the board: the current assets simply don't overlap with an inland hazard layer, which is a real gap in coverage, not a join bug. Onshore coverage needs a proper authoritative source (e.g. the DESNZ Renewable Energy Planning Database or National Grid ESO's Embedded Capacity Register) - not a hand-typed list, given how easily an unverified one drifts from what's actually operating.

## Local verification

```
python scripts/run_local.py            # full run - hits every real API
python scripts/run_local.py --skip-slow  # skips OSM Overpass + full-GB flood/earthquake pulls
```
Writes each source's output plus the final joined `exposure.json` to `local_dev_output/` (gitignored) for inspection. No AWS credentials needed - nothing touches S3.
