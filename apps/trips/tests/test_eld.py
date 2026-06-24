"""ELD daily-log-sheet building: per-day split, off-duty padding, 24h totals."""

from datetime import datetime, timezone

from django.test import TestCase

from ..services import eld, hos
from ._helpers import HOSComplianceMixin, _legs


class ELDSheetTests(HOSComplianceMixin, TestCase):
    start = datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)

    def test_multi_day_trip_produces_multiple_sheets_summing_to_24(self):
        timeline = hos.plan_timeline(
            legs=_legs([400, 1600]),  # 2000 mi
            start_time=self.start,
            current_cycle_used_hours=0,
        )
        self.assert_hos_compliant(timeline)
        sheets = eld.build_log_sheets(timeline)
        self.assertGreater(len(sheets), 1)
        # Every sheet — including the padded first/last day — totals EXACTLY 24h
        # (totals are rounded so they sum without drift; no 24.01 artifacts).
        for sheet in sheets:
            self.assertEqual(
                round(sum(sheet["totals"].values()), 2), 24.0,
                msg="every sheet's status totals must sum to exactly 24.00h",
            )

    def test_status_totals_round_without_drift(self):
        # A day split into thirds that don't divide evenly into 0.01h must still
        # sum to exactly 24.00 (largest-remainder rounding, not naive per-status).
        timeline = [
            {"status": "OFF", "start": "2026-06-23T00:00:00+00:00", "end": "2026-06-23T08:00:00+00:00", "location": "", "note": ""},
            {"status": "D",   "start": "2026-06-23T08:00:00+00:00", "end": "2026-06-23T16:20:00+00:00", "location": "", "note": ""},
            {"status": "ON",  "start": "2026-06-23T16:20:00+00:00", "end": "2026-06-24T00:00:00+00:00", "location": "", "note": ""},
        ]
        totals = eld.build_log_sheets(timeline)[0]["totals"]
        self.assertEqual(round(sum(totals.values()), 2), 24.0)

    def test_first_and_last_day_padded_with_off_duty(self):
        # Start at 08:00 -> day 0 must open with OFF from midnight (0) to 08:00 (480);
        # the last day must close with OFF running to midnight (1440).
        timeline = hos.plan_timeline(
            legs=_legs([400, 1600]),
            start_time=self.start,  # 08:00
            current_cycle_used_hours=0,
        )
        sheets = eld.build_log_sheets(timeline)
        first = sheets[0]["segments"][0]
        self.assertEqual((first["status"], first["start_minute"], first["end_minute"]), ("OFF", 0, 480))
        last = sheets[-1]["segments"][-1]
        self.assertEqual((last["status"], last["end_minute"]), ("OFF", 1440))
        # Contiguity preserved: every day covers 0..1440 with no gaps.
        for sheet in sheets:
            segs = sheet["segments"]
            self.assertEqual(segs[0]["start_minute"], 0)
            self.assertEqual(segs[-1]["end_minute"], 1440)
            for a, b in zip(segs, segs[1:]):
                self.assertEqual(a["end_minute"], b["start_minute"])

    def test_segments_never_cross_midnight(self):
        timeline = hos.plan_timeline(
            legs=_legs([400, 1600]),
            start_time=self.start,
            current_cycle_used_hours=0,
        )
        for sheet in eld.build_log_sheets(timeline):
            for seg in sheet["segments"]:
                self.assertGreaterEqual(seg["start_minute"], 0)
                self.assertLessEqual(seg["end_minute"], 24 * 60)
                self.assertLess(seg["start_minute"], seg["end_minute"])

    def test_empty_timeline_returns_no_sheets(self):
        self.assertEqual(eld.build_log_sheets([]), [])

    def test_off_block_spanning_a_whole_day_yields_a_full_off_sheet(self):
        # A long off-duty block (e.g. a 34h restart) that fully covers a calendar
        # day produces a middle sheet that is a single OFF segment, 00:00–24:00.
        timeline = [
            {"status": "D",   "start": "2026-06-23T08:00:00+00:00", "end": "2026-06-23T12:00:00+00:00", "location": "", "note": "Driving"},
            {"status": "OFF", "start": "2026-06-23T12:00:00+00:00", "end": "2026-06-25T14:00:00+00:00", "location": "", "note": "34-hour restart"},
            {"status": "D",   "start": "2026-06-25T14:00:00+00:00", "end": "2026-06-25T16:00:00+00:00", "location": "", "note": "Driving"},
        ]
        sheets = eld.build_log_sheets(timeline)
        self.assertEqual(len(sheets), 3)              # 23rd, 24th, 25th
        middle = sheets[1]                            # the 24th is wholly off duty
        self.assertEqual(middle["totals"]["OFF"], 24.0)
        self.assertEqual(len(middle["segments"]), 1)
        seg = middle["segments"][0]
        self.assertEqual((seg["status"], seg["start_minute"], seg["end_minute"]), ("OFF", 0, 1440))
