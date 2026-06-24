# ELD / Driver's Daily Log Sheet Format

> Reproduces the FMCSA **Driver's Daily Log (Record of Duty Status)** — the standard
> 24-hour grid drivers fill out. This describes what the backend emits so a frontend
> can **draw** the log: header + grid + per-status totals + remarks, one sheet per
> calendar day.

## The sheet (one per calendar day)

Header fields (operator metadata not produced by the API — render as blank/optional):

- Date (month / day / year)
- From / To (trip endpoints for the day)
- Total Miles Driving Today
- Carrier name, main office address, home terminal address
- Truck/tractor & trailer numbers

## The graph grid (the core drawing)

- A 24-hour grid: **Midnight → Noon → Midnight**, divided into 24 hour columns,
  each subdivided into **4 (15-minute)** ticks.
- **4 rows**, one per duty status, in this fixed order:
  1. Off Duty
  2. Sleeper Berth
  3. Driving
  4. On Duty (not driving)
- The duty status is drawn as a **horizontal line in the active row**, with
  **vertical transitions** when the status changes. The line is continuous across
  the full 24h.
- Right edge: **Total Hours** per row; the four totals sum to **24**.

## Remarks / annotations

- City + State where each duty-status **change** occurred (and the time).
- Shipping document / manifest numbers, shipper & commodity (operator-supplied).

## JSON the backend produces (per day)

```jsonc
{
  "date": "2026-06-23",
  "day_index": 0,
  "totals": { "OFF": 6.0, "SB": 0, "D": 11.0, "ON": 7.0 },   // sums to 24.0
  "segments": [
    { "status": "OFF", "start_minute": 0,   "end_minute": 360, "location": "",            "note": "Off duty" },
    { "status": "ON",  "start_minute": 360, "end_minute": 420, "location": "Chicago, IL", "note": "Pickup (loading)" },
    { "status": "D",   "start_minute": 420, "end_minute": 660, "location": "En route to dropoff", "note": "Driving" },
    { "status": "OFF", "start_minute": 660, "end_minute": 690, "location": "Rest area",   "note": "30-min break" }
    // ...
  ]
}
```

- `status` ∈ `OFF | SB | D | ON` (maps to grid rows 1–4).
- `start_minute` / `end_minute` are **minutes from local midnight (0–1440)**, in the
  trip's local time zone (the current location's zone, fixed for the whole trip — see
  [`hos-rules.md`](hos-rules.md)). A span crossing midnight is **split** across two sheets.
- `totals` per status sum to **24.0 on every sheet**: time before the driver goes on
  duty (and after the final duty status) is padded with **off-duty**, so even the
  first/last day is a full 24h, exactly like a real RODS page.
- Stop events (pickup, fuel, breaks, rests, dropoff) are also returned at the trip
  level with coordinates + a reverse-geocoded "City, ST", for plotting on a map.

## Map / routing & geocoding APIs (all free)

Routing (distance, duration, polyline) and geocoding are computed in
`services/routing.py`; map-tile rendering is the frontend's job.

| Service | Key needed | Role |
|---|---|---|
| **OpenRouteService** | free API key | Geocoding (Pelias) + `driving-hgv` truck routing. **Default.** |
| **Nominatim** (OpenStreetMap) | none | Geocoding fallback (no key). |
| **OSRM** (public demo) | none | Routing fallback (no key, rate-limited). |
| **Leaflet + OpenStreetMap tiles** | none | Frontend map rendering (no key). |

The backend prefers **OpenRouteService** (driven by `ORS_API_KEY`), with Nominatim
(geocoding) and OSRM (routing) as keyless fallbacks. Any upstream failure surfaces as
a clean 4xx from the API, never a 500.
