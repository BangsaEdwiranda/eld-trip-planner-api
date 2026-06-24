# Hours of Service (HOS) Rules — Property-Carrying Drivers

> Based on the FMCSA Hours-of-Service regulations (**49 CFR Part 395**), distilled
> to what this planner enforces. Modeling assumptions: 70-hour/8-day cycle, no
> adverse-driving exception, fueling at least every 1,000 miles, and 1 hour each for
> pickup and dropoff (on-duty, not driving).

## The four duty statuses (the ELD grid rows)

1. **Off Duty** (OFF)
2. **Sleeper Berth** (SB)
3. **Driving** (D)
4. **On Duty (not driving)** (ON) — fueling, pickup/dropoff, inspections, paperwork.

## The limits the simulator respects

### 1. 14-hour driving window — §395.3(a)(2)
- After 10+ consecutive hours off duty, the driver has a **14 consecutive-hour
  window** in which driving is allowed.
- The window **starts when any work starts** and runs on the wall clock — breaks
  and on-duty-not-driving time do **not** pause it.
- Once 14 hours elapse, **no more driving** until another 10 consecutive hours off.

### 2. 11-hour driving limit — §395.3(a)(3)
- Within the 14h window, **max 11 hours of actual driving**.
- After 11 hours driving, must take 10 consecutive hours off before driving again.

### 3. 30-minute break — §395.3(a)(3)(ii)
- A **30-minute consecutive break from driving** is required after **8 cumulative
  hours of driving** (cumulative, not consecutive).
- The break may be OFF, SB, or ON (non-driving) — any non-driving status counts, as
  long as it is 30 consecutive minutes.
- It does **not** extend the 11h or 14h limits.

### 4. 70-hour / 8-day on-duty limit — §395.3(b)
- May **not drive** after **70 on-duty hours in 8 consecutive days**.
- It is total **on-duty** time (Driving + On Duty), not just driving.
- The "cycle hours already used" input seeds this counter at trip start.

### 34-hour restart — §395.3(c)
- **34+ consecutive hours off duty** resets the 70hr/8day cycle to zero. Relevant
  for long multi-day trips where the cycle is exhausted.

### 10-hour reset
- **10 consecutive hours off duty** resets both the 11-hour driving limit and the
  14-hour window (the daily clocks).

## Not modeled (out of scope)

- **Sleeper-berth split** (§395.1(g)) — rests are modeled as a single 10h off-duty
  block rather than a 7/3 or 8/2 split.
- **Adverse driving conditions** (+2h, §395.1(b)) — assumed none.
- **Short-haul exceptions** (§395.1(e)).
- The true rolling 8-day drop-off — the 34-hour restart stands in for cycle
  exhaustion instead of aging off the oldest day.

## Simulation algorithm (`services/hos.py`)

State carried while stepping along the route:

- `driving_since_break` — driving hours since the last 30-min break (limit 8).
- `driving_today` — driving hours in the current window (limit 11).
- `window_elapsed` — wall-clock hours since the window opened (limit 14).
- `cycle_hours` — on-duty hours in the 70/8 cycle (limit 70), seeded from input.
- `clock` — current timestamp.

The timeline starts at **"now" in the local time zone of the current location**
(resolved from coordinates in `services/timezone.py`, held constant for the whole
trip — mirroring a driver's fixed home-terminal time base per §395.8). FMCSA defines
no fixed shift start, so the driver simply goes on duty at the current local time.

> **"Depart now" projection.** This is a trip *planner*, not a live ELD recorder. It
> assumes the driver **departs at the planning moment and the first activity is
> driving** (current → pickup). It does not model a chosen departure time, the
> driver's real current duty status, or a pre-trip on-duty inspection. Time before
> the start is shown as off-duty padding; the simplification is that planning-time =
> departure-time. An optional departure-time / pre-trip input would close this.

The route is modeled as ordered **legs** — leg 1 = current→pickup, leg 2 =
pickup→dropoff — each a drive followed by an on-duty stop on arrival. Counters
persist across legs (fueling is cumulative; the driver drives to the pickup *before*
loading). For each leg, consume its distance in increments:

1. **Drive the leg.** While distance remains, at the top of each step resolve any
   reached limit, then drive:
   - If `cycle_hours >= 70` → append a 34h restart (OFF), reset cycle + daily clocks.
     (Driving increments stop *at* the 70h line below, so this fires when an on-duty
     stop — fuel/pickup/dropoff, not driving — is what tips the cycle over.)
   - If miles since last fuel `>= 1000` → append a fuel stop (0.5h ON); a ≥30-min
     non-driving stop also satisfies the break, so reset `driving_since_break`.
   - If `driving_since_break >= 8` → append a 30-min break (OFF), reset to 0.
   - If `driving_today >= 11` **or** `window_elapsed >= 14` → append a 10h OFF reset,
     reset `driving_today`, `window_start`, `driving_since_break`.
   - Otherwise append a **driving** increment capped by the nearest limit — the
     8h-before-break, 11h-driving, 14h-window, **and 70h-cycle** boundaries — so a
     driving step never crosses any of them (§395.3(b): may not *drive* past 70
     on-duty hours); accumulate driving counters, miles, and cycle.
2. **Arrival stop.** Append the leg's on-duty stop (1h pickup / 1h dropoff). It
   counts toward the window + cycle and, being ≥30 min, also satisfies the break clock.
3. Emit the full ordered list of duty-status segments.

> Any ≥30-min non-driving period (off duty, sleeper, **or** on-duty-not-driving such
> as fuel/pickup) satisfies the 30-min break rule — so the simulator doesn't add a
> redundant break right after a fuel or loading stop.

The timeline is then split by **calendar day** (in the trip's local zone) into one
ELD log sheet per day, each **padded with off-duty at the edges to total a full 24h**
like a real Record of Duty Status page (see [`eld-log-format.md`](eld-log-format.md)).
