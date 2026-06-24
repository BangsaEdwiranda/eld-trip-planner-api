"""Service layer for the trip planner.

- `routing`  — geocoding + route geometry/distance/duration via free map APIs.
- `hos`      — Hours-of-Service simulator producing a duty-status timeline.
- `eld`      — splits the timeline into per-day ELD log sheets.

See docs/hos-rules.md and docs/eld-log-format.md for the rules these implement.
"""
