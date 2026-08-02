# Data Sources

Every endpoint below was live-tested during research (2026-07-19). All are free, public, no API key required, Open Government Licence v3.0 or equivalent open terms — attribution required, no redistribution restrictions beyond that.

## Assets

### Crown Estate — offshore wind (England, Wales & NI)
- **Endpoint**: `https://services2.arcgis.com/PZklK9Q45mfMFuZs/arcgis/rest/services/WindSite_EngWalNI_TheCrownEstate/FeatureServer/0/query`
- **Query**: `?where=1=1&outFields=*&f=geojson` (add `&resultOffset=N&resultRecordCount=1000` to page — a single request hits `exceededTransferLimit: true` on the full dataset)
- **Format**: GeoJSON `FeatureCollection`, `Polygon` geometries (site boundaries, not points — centroid needed for point-based join)
- **License**: The Crown Estate Open Data Licence (GIS) v1.1 — free use with attribution
- **Notes**: Sibling layers exist on the same service root for cable agreements, natural gas storage, tidal stream, wave, mining, aggregates, CCS — same query pattern if scope expands later.

### Crown Estate Scotland — offshore wind
- **Endpoint**: `https://services3.arcgis.com/nGV4jiurzcahJ9LV/arcgis/rest/services/Offshore_Wind_Crown_Estate_Scotland/FeatureServer/0/query`
- **Query**: same pattern as above
- **Format**: GeoJSON, `Polygon` geometries. Fields include `Property_Description`, `Tenant_Name`, `Lease_Type_Description`, `Project_Phase`, `Capacity_MW`.
- **License**: Crown Estate Scotland open data terms (OGL-equivalent)
- **Notes**: Required separately from the England/Wales/NI portal — Scottish waters aren't covered by that one. A `ScotWind_Offers_Crown_Estate_Scotland` layer also exists on the same service root if lease-round detail is wanted later.

### NSTA — oil & gas platforms / installations
- **Endpoint**: `https://services-eu1.arcgis.com/OZMfUznmLTnWccBc/arcgis/rest/services/UKCS%20offshore%20infrastructure%20surface%20points%20WGS84/FeatureServer/1/query`
- **Query**: same `where=1=1&outFields=*&f=geojson` pattern
- **Format**: GeoJSON, `Point` geometries. Key fields: `NAME`, `INF_TYPE` (`PLATFORM`, `FPSO`, etc.), `STATUS` (`ACTIVE`/etc.), `REP_GROUP` (operator).
- **License**: NSTA Open Data (OGL-equivalent)
- **Notes**: This dataset is one of 700+ layers on the NSTA open data catalogue (`open-data-ukcs-transition.hub.arcgis.com`) — most are pipeline/subsea infrastructure in ED50/ETRS89/WGS84 variants; we only need the WGS84 surface-points layer for asset locations. Filter `STATUS = 'ACTIVE'` to exclude decommissioned platforms.

### OpenStreetMap Overpass — power infrastructure
- **Endpoint**: `https://overpass-api.de/api/interpreter` (POST)
- **Query pattern**:
  ```
  [out:json][timeout:60];
  (
    node["power"="plant"](bbox);
    node["power"="substation"](bbox);
    node["power"="generator"]["generator:source"~"wind|gas|oil"](bbox);
  );
  out body;
  ```
- **Format**: JSON, `elements[]` with `lat`/`lon`/`tags`
- **License**: ODbL — attribution required ("© OpenStreetMap contributors")
- **Notes**: Confirmed reachable and query syntax valid, but the public instance is frequently slow/busy (hit a 504 "server too busy" during testing) and returns **406** if the `Accept`/`Content-Type` headers aren't set explicitly — must set both. A whole-country `area["ISO3166-1"="GB"]` query is heavy; scope by **bounding box** instead (e.g. GB mainland + North Sea: roughly `49.8,-8.7,61.0,2.0`), and build in a fallback mirror (`overpass.kumi.systems`) plus retry-with-backoff since the public server has no SLA.

### DESNZ — Renewable Energy Planning Database (REPD)
- **Endpoint**: quarterly CSV/XLSX extract, published at `gov.uk/government/publications/renewable-energy-planning-database-monthly-extract`. Current extract as of 2026-08-02: `https://assets.publishing.service.gov.uk/media/69fc56908cc72d2f863ea58d/REPD_publication_Q1_2026.csv`. **No stable "latest" alias exists** — every quarter gets a new dated URL, so this needs a manual bump each quarter.
- **Format**: CSV, ~14,300 rows covering every UK renewable electricity project above 150kW tracked through planning/construction/operation/decommissioning. Filter `Development Status (short) == "Operational"` for currently-generating sites (3,100 of the total; 3,050 once `Technology Type == "Wind Offshore"` is excluded to avoid double-counting Crown Estate leases).
- **License**: OGL v3.0
- **Notes**: Coordinates are given as `X-coordinate`/`Y-coordinate` — **OSGB36 British National Grid eastings/northings, not lat/lon**, unlike every other source in this pipeline. Converting requires a proper geodetic transform (inverse Transverse Mercator to the Airy 1830/OSGB36 datum, then a 7-parameter Helmert transform to WGS84) — `shared/osgb.py` implements this, validated against Ordnance Survey's own published Transverse Mercator worked example (`docs.os.uk`, "A guide to coordinate systems in Great Britain") and Helmert worked example (round-trip error sub-millimetre on both). 2 of 3,052 operational-onshore rows have blank coordinates and are skipped. Technology types among the operational set: Solar Photovoltaics (1,393), Wind Onshore (778), Landfill Gas (270), Battery (171), Anaerobic Digestion (151), Biomass dedicated/co-firing (83), Small/Large Hydro (97), EfW Incineration (60), Wind Offshore (48, excluded), plus smaller categories (tidal stream, pumped storage, geothermal, hydrogen, flywheels, etc.).

### Nuclear power stations — hand-curated (not a live API)
- **Source**: EDF's own "nuclear power stations" page (`edfenergy.com/about/nuclear/power-stations`) for which stations are currently generating (checked 2026-08-02); coordinates from each station's Wikipedia infobox (checked same date).
- **Format**: static `data/nuclear_sites.json` — `{name, county, lat, lon, reactor_type}[]`, committed to the repo, not re-fetched on a schedule.
- **Rationale**: only 5 UK stations are currently operating (Sizewell B, Heysham 1, Heysham 2, Hartlepool, Torness) — Dungeness B, Hunterston B and Hinkley Point B are decommissioning; Hinkley Point C and Sizewell C aren't generating yet. A list this small and this stable doesn't need a live feed pretending otherwise, matching the same reasoning already applied to the storm catalogue. Will need manual review if a station's status changes (life extension, closure, or a new build coming online).

## Hazards

### Environment Agency — flood zones
- **Endpoint**: `https://environment.data.gov.uk/spatialdata/flood-map-for-planning-flood-zones/wfs`
- **Query**: `?service=WFS&version=2.0.0&request=GetFeature&typeNames=dataset-04532375-a198-476e-985e-0579a0a11b47:Flood_Zones_2_3_Rivers_and_Sea&outputFormat=json&bbox=<minLon>,<minLat>,<maxLon>,<maxLat>,urn:ogc:def:crs:EPSG::4326`
- **Format**: GeoJSON `FeatureCollection`, polygons for combined Flood Zone 2+3
- **License**: OGL v3.0 — "© Environment Agency copyright and/or database right 2026"
- **Notes**: `outputFormat=geojson` is rejected (`InvalidParameterValue`) — must be exactly `json`. The dataset previously listed at `data.gov.uk` under a different slug appears retired; the current live typeName was found via the WFS `GetCapabilities` document, not the catalogue page — worth re-checking `GetCapabilities` if this breaks in future, since EA has migrated endpoints before. Two sharper gotchas found only by test-querying with a real bbox (both silently return zero matches rather than erroring, so they're easy to miss): (1) the dataset's native CRS is **EPSG:27700 (British National Grid)**, not WGS84 — `srsName=urn:ogc:def:crs:EPSG::4326` must be passed explicitly to get lon/lat geometry back; (2) the `urn:ogc:def:crs:EPSG::4326` bbox filter uses EPSG-registry axis order, which for 4326 is **(lat, lon)**, not the GeoJSON-conventional (lon, lat) — passing lon,lat matches nothing. Confirmed via `docs`-stage testing: 813,627 total features nationwide; a GB-wide bbox query with both fixes applied returns real polygons immediately. **Coverage caveat**: `lambdas/ingest_hazards/handler.py`'s pagination safety cap (25,000 features) only captures a small, arbitrarily-ordered slice of the 813,627 total - a production run needs either a much higher cap or (better) the GB bbox subdivided into smaller regional tiles so coverage is geographically complete rather than just "however many records the API returns first." The initial local verification run found zero energy assets within the flood-exposure threshold, which may be a genuine result (most assets here are well offshore, and EA's "rivers and sea" zones are largely land/coastal) or may just reflect this coverage gap - not resolved yet, flag before trusting the flood numbers.

**Deployed-Lambda finding (not visible from local testing)**: the same 26-page pagination that took ~90 seconds from a home/office network took long enough from an AWS Lambda in eu-west-2 that it hit both a 300s and then a 600s Lambda timeout with zero completed pages logged. Individual requests to `environment.data.gov.uk` appear to be markedly slower (or throttled) from AWS IP ranges than from residential/office IPs - a real, if unconfirmed-in-detail, characteristic of this API worth designing around rather than fixing by raising the timeout indefinitely. `ingest_hazards/handler.py` now caps total pagination time per source (`PAGINATION_TIME_BUDGET_SECONDS`, currently 120s) so the Lambda returns whatever partial data it managed to fetch instead of blocking until killed.

### BGS — earthquake catalogues
- **Endpoint**: `https://ogcapi.bgs.ac.uk/collections/recentearthquakes/items` (1970–present) and `https://ogcapi.bgs.ac.uk/collections/historicalearthquakes/items` (pre-1970)
- **Query**: `?f=json&limit=1000&bbox=<minLon>,<minLat>,<maxLon>,<maxLat>` (standard OGC API - Features paging via `limit`/`offset`)
- **Format**: GeoJSON `FeatureCollection`, `Point` geometries. Fields: `datetime`, `depth`, `ml` (magnitude, local scale), `intensity`.
- **License**: BGS Open Government Licence
- **Notes**: Both collections confirmed live. Use `ml` (magnitude) to threshold out the large number of very minor events — UK seismicity is mostly sub-magnitude-2 and not underwriting-relevant.

### Storm events — hand-curated (not a live API)
- **Source**: manually compiled from the Met Office's public "named storms" season pages (`metoffice.gov.uk/weather/warnings-and-advice/uk-storm-centre`) — no clean single machine-readable feed exists; IBTrACS was investigated and rejected (tropical-cyclone-only, doesn't cover UK/North Sea extratropical storms).
- **Format**: static `data/storm_events.json` — `{name, season, date, max_gust_mph?, category}[]`, committed to the repo and uploaded once as a static file, not re-fetched on a schedule.
- **Rationale**: matches the existing precedent in the sibling Global Shock pipeline, where `global_shock_events.json` is likewise a hand-curated catalogue rather than a live-fetched source.

## Dropped from scope
- **EIA** (US Energy Information Administration) — US-focused, not meaningfully applicable to a Britain-only exposure map. Dropped per project decision.
