"""Tests for the sync_dataset command (replaying a newer ItikCareDataSet.csv onto existing logs)."""

import tempfile
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from .models import DailyLog, DailyLogEdit, Flock

HEADER = (
    "Date,Caging_Period,Flock_Generation,Number of Flocks,Average Age of Flock (weeks),"
    "Feed Intake (kgs per day),Egg Yield (per day),Yield_Per_Bird,Temperature,Humidity\n"
)
OLD_ROWS = "2024-02-02,1,1,250,69,38.5,196,0.784,25.6,82\n2024-02-03,1,1,250,69,38.5,203,0.812,25.7,84\n"
# Newer copy: 02-03's egg count corrected (203 -> 210), a new 02-04 row, formatting-only
# difference on feed intake (38.5 stays 38.5), and a UTF-8 BOM like a spreadsheet export.
NEW_ROWS = (
    "2024-02-02,1,1,250,69,38.5,196,0.784,25.6,82\n"
    "2024-02-03,1,1,250,69,38.5,210,0.84,25.7,84\n"
    "2024-02-04,1,1,250,69,38.5,212,0.848,26.7,81\n"
)


class SyncDatasetTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="mario", email="m@example.com", password="pw", is_foundation_farmer=True,
        )
        self.dir = Path(tempfile.mkdtemp())
        old = self.dir / "old.csv"
        old.write_text(HEADER + OLD_ROWS, encoding="utf-8")
        call_command("import_daily_logs", str(old), recorded_by="mario", stdout=StringIO())
        self.new = self.dir / "new.csv"
        self.new.write_text("﻿" + HEADER + NEW_ROWS, encoding="utf-8")

    def _sync(self, *args):
        out = StringIO()
        call_command("sync_dataset", str(self.new), *args, stdout=out)
        return out.getvalue()

    def test_dry_run_persists_nothing(self):
        self._sync("--dry-run")
        self.assertEqual(DailyLog.objects.count(), 2)
        self.assertEqual(DailyLogEdit.objects.count(), 0)

    def test_creates_new_rows_and_audits_changed_ones(self):
        # Locked rows (already learned by a retrain) must still be correctable, audited.
        DailyLog.objects.update(is_locked=True)
        self._sync()

        self.assertEqual(DailyLog.objects.count(), 3)
        corrected = DailyLog.objects.get(date=date(2024, 2, 3))
        self.assertEqual(corrected.egg_count, 210)
        edit = DailyLogEdit.objects.get()
        self.assertEqual((edit.daily_log, edit.field_name, edit.old_value, edit.new_value),
                         (corrected, "egg_count", "203", "210"))
        self.assertEqual(edit.changed_by, self.user)
        # Formatting-only differences are not "changes".
        self.assertEqual(DailyLog.objects.get(date=date(2024, 2, 2)).feed_intake_kg, Decimal("38.50"))

    def test_second_run_is_a_no_op(self):
        self._sync()
        self._sync()
        self.assertEqual(DailyLog.objects.count(), 3)
        self.assertEqual(DailyLogEdit.objects.count(), 1)

    def test_logs_missing_from_csv_are_kept(self):
        flock = Flock.objects.get()
        DailyLog.objects.create(
            flock=flock, date=date(2024, 3, 1), caging_period=1, flock_size=250, flock_age_weeks=73,
            egg_count=180, feed_intake_kg=Decimal("38.5"), temperature_c=Decimal("26"),
            humidity_pct=Decimal("80"), recorded_by=self.user,
        )
        self.assertIn("in database but not in CSV (left alone): 1", self._sync())
        self.assertTrue(DailyLog.objects.filter(date=date(2024, 3, 1)).exists())
