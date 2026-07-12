"""One-time historical backfill: import ItikCare_Cleaned_Dataset.csv into Flock/DailyLog.

Run as:
    python manage.py import_daily_logs --recorded-by <username> [csv_path] [--dry-run]

Per itikcare-spec.md section 10, the CSV's Caging_Period and Flock_Generation columns
mark real operational boundaries (free-range/caging cycles and flock-generation
resets). This command preserves both as-is (Caging_Period on DailyLog, Flock_Generation
as Flock.generation_number) so later training-time segmentation can use them — neither
is fed to the RF model as a raw feature, that happens in the forecasting pipeline, not
here.
"""

import csv
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from farm.models import DailyLog, Flock

User = get_user_model()

# Historical CSV -> DailyLog field, with a parser for each. Yield_Per_Bird is
# deliberately omitted: it's exactly egg_count / flock_size, a derived value with no
# new information, so it isn't stored.
FIELD_PARSERS = {
    "caging_period": ("Caging_Period", int),
    "flock_size": ("Number of Flocks", int),
    "flock_age_weeks": ("Average Age of Flock (weeks)", int),
    "feed_intake_kg": ("Feed Intake (kgs per day)", Decimal),
    "egg_count": ("Egg Yield (per day)", int),
    "temperature_c": ("Temperature", Decimal),
    "humidity_pct": ("Humidity", Decimal),
}


class Command(BaseCommand):
    help = "One-time import of the historical CSV dataset into Flock/DailyLog."

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            nargs="?",
            default=str(settings.BASE_DIR / "ItikCare_Cleaned_Dataset.csv"),
            help="Path to the historical CSV. Defaults to the project root's ItikCare_Cleaned_Dataset.csv.",
        )
        parser.add_argument(
            "--recorded-by",
            required=True,
            help="Username of the existing account historical DailyLog rows should be attributed to.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the full import and report counts, then roll back — nothing is persisted.",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        dry_run = options["dry_run"]

        try:
            recorded_by = User.objects.get(username=options["recorded_by"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user found with username {options['recorded_by']!r}.") from exc

        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                rows = sorted(csv.DictReader(f), key=lambda row: row["Date"])
        except FileNotFoundError as exc:
            raise CommandError(f"CSV file not found: {csv_path}") from exc

        with transaction.atomic():
            summary = self._import_rows(rows, recorded_by)
            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f"{'[DRY RUN] ' if dry_run else ''}"
            f"Flocks created: {summary['flocks_created']}. "
            f"DailyLogs created: {summary['logs_created']}, "
            f"skipped (already existed): {summary['logs_skipped']}, "
            f"failed validation: {summary['logs_failed']}."
        ))

    def _import_rows(self, rows, recorded_by):
        flocks_by_generation = {}
        summary = {"flocks_created": 0, "logs_created": 0, "logs_skipped": 0, "logs_failed": 0}

        for row in rows:
            row_date = date.fromisoformat(row["Date"])
            generation_number = int(row["Flock_Generation"])

            flock = flocks_by_generation.get(generation_number)
            if flock is None:
                flock, created = Flock.objects.get_or_create(
                    owner=recorded_by,
                    generation_number=generation_number,
                    defaults={"started_on": row_date, "is_active": False},
                )
                flocks_by_generation[generation_number] = flock
                if created:
                    summary["flocks_created"] += 1

            if DailyLog.objects.filter(flock=flock, date=row_date).exists():
                summary["logs_skipped"] += 1
                continue

            daily_log = DailyLog(
                flock=flock,
                date=row_date,
                recorded_by=recorded_by,
                **{field: parser(row[column]) for field, (column, parser) in FIELD_PARSERS.items()},
            )
            try:
                daily_log.full_clean(exclude=["flock", "recorded_by"])
            except Exception as exc:
                summary["logs_failed"] += 1
                self.stderr.write(self.style.WARNING(f"Skipping {row_date} (validation failed): {exc}"))
                continue

            daily_log.save()
            summary["logs_created"] += 1

        self._set_active_flock(recorded_by)
        return summary

    def _set_active_flock(self, owner):
        """Mark this owner's highest-generation Flock as active, matching
        farm/services.py's get_active_flock single-active-flock-per-owner assumption."""

        latest_flock = Flock.objects.filter(owner=owner).order_by("-generation_number").first()
        if latest_flock is None:
            return
        Flock.objects.filter(owner=owner).exclude(pk=latest_flock.pk).update(is_active=False)
        if not latest_flock.is_active:
            latest_flock.is_active = True
            latest_flock.save(update_fields=["is_active"])
