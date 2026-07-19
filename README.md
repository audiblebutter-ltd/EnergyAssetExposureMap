# EnergyAssetExposureMap

Maps Britain's energy infrastructure (offshore wind, oil & gas platforms, power infrastructure) against natural hazard data (storm tracks, flood zones, seismic activity) to answer a simple question: what assets are on risk, and where. Built entirely from public data sources, with the pipeline and output left open for anyone to explore.

## Architecture

The pipeline follows the same shape as the [Global Shock oil price pipeline](https://dataandgrit.energy): one CloudFormation stack, S3 for raw/processed data, Lambda for compute, EventBridge for scheduling.

**Ingestion**
- One Lambda per data source, each pulling from a public API/dataset and writing a raw snapshot to S3:
  - Assets: Crown Estate (offshore wind leases), NSTA (oil & gas platforms), EIA, OpenStreetMap Overpass API (power infrastructure)
  - Hazards: storm track archives, flood risk zones, seismic data
- Raw bucket keeps one dated snapshot per source, same partition style as `raw/<source>/YYYY/MM/DD/`

**Join**
- A transformer Lambda reads the latest asset and hazard snapshots and performs the geospatial join (asset location against hazard zone), producing a single GeoJSON output
- Writes both a dated snapshot and an overwritten `processed/latest.json` for the frontend to read

**Scheduling**
- EventBridge triggers ingestion on a fixed schedule (these sources don't move daily like oil prices, so this runs weekly/monthly rather than daily)

**Frontend**
- The site fetches `processed/latest.json` directly from S3 (no API Gateway layer, same as the oil price pipeline's simplest option) and renders it as an interactive map using a lightweight client-side map library

## Repo structure

```text
EnergyAssetExposureMap/
├── lambdas/
│   ├── ingest_assets/
│   ├── ingest_hazards/
│   └── join/
├── infra/
│   └── cloudformation.yaml
├── deploy.sh
├── README.md
└── .gitignore
```
