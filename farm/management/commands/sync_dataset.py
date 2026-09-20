"""Reconcile the foundation farmer's DailyLogs with the current ItikCareDataSet.csv.

Run as:
    python manage.py sync_dataset [csv_path] [--recorded-by <username>] [--dry-run]

import_daily_logs is a one-time backfill that skips any date already on file, so it
can't pick up a *newer* copy of the dataset. This command replays the CSV onto a
database that already holds an older copy:

  * a CSV date with no DailyLog -> created (same rules as import_daily_logs);
  * a CSV date whose stored values differ -> updated, with one DailyLogEdit audit row
    per changed field (old value, new value, who, when), so historical farm data is
    never silently overwritten (CLAUDE.md). This includes rows already locked by a
    model retrain (DailyLog.is_locked): the lock stops farmers editing rows a model
    has learned from, but a deliberate dataset correction has to be able to fix them --
    the audit trail records it, and the model should be retrained afterwards;
  * a DailyLog with no CSV row -> left alone and only reported, never deleted.

Re-running it after a successful sync changes nothing. --dry-run does the whole thing
inside a transaction and rolls it back, so you can see the exact diff first.
"""

import csv
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import transaction

from farm.models import DailyLog, DailyLogEdit, Flock

from .import_daily_logs import FIELD_PARSERS, Command as ImportCommand

User = get_user_model()


class Command(ImportCommand):
    help = "Replay ItikCareDataSet.csv onto existing DailyLogs (insert new dates, audited updates for changed ones)."

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            nargs="?",
            default=str(settings.BASE_DIR / "ItikCareDataSet.csv"),
            help="Path to the dataset CSV. Defaults to the project root's ItikCareDataSet.csv.",
        )
        parser.add_argument(
            "--recorded-by",
            help="Username that owns the data. Defaults to the foundation farmer.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the full sync and report the diff, then roll back — nothing is persisted.",
        )

    def handle(self, *args, **options):
        owner = self._resolve_owner(options["recorded_by"])
        rows = self._read_rows(options["csv_path"])

        with transaction.atomic():
            summary = self._sync_rows(rows, owner)
            if options["dry_run"]:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f"{'[DRY RUN] ' if options['dry_run'] else ''}"
            f"Owner: {owner.username}. CSV rows: {len(rows)}. "
            f"Created: {summary['created']}, updated: {summary['updated']} "
            f"({summary['edits']} audited field changes, {summary['locked_updated']} on locked rows), "
            f"unchanged: {summary['unchanged']}, failed validation: {summary['failed']}, "
            f"in database but not in CSV (left alone): {summary['db_only']}."
        ))

    def _read_rows(self, csv_path):
        try:
            # utf-8-sig: spreadsheet exports prepend a BOM that would corrupt the "Date" key.
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                return sorted(csv.DictReader(f), key=lambda row: row["Date"])
        except FileNotFoundError as exc:
            raise CommandError(f"CSV file not found: {csv_path}") from exc

    def _resolve_owner(self, username):
        try:
            if username:
                return User.objects.get(username=username)
            return User.get_foundation_farmer()
        except User.DoesNotExist as exc:
            raise CommandError(
                f"No user found ({username!r}). Pass --recorded-by, or mark a foundation farmer."
            ) from exc

    def _sync_rows(self, rows, owner):
        summary = {"created": 0, "updated": 0, "edits": 0, "locked_updated": 0,
                   "unchanged": 0, "failed": 0, "db_only": 0}
        flocks_by_generation = {}
        csv_keys = set()

        for row in rows:
            row_date = date.fromisoformat(row["Date"])
            generation_number = int(row["Flock_Generation"])

            flock = flocks_by_generation.get(generation_number)
            if flock is None:
                flock, _ = Flock.objects.get_or_create(
                    owner=owner,
                    generation_number=generation_number,
                    defaults={"started_on": row_date, "is_active": False},
                )
                flocks_by_generation[generation_number] = flock
            csv_keys.add((flock.pk, row_date))

            new_values = {field: parser(row[column]) for field, (column, parser) in FIELD_PARSERS.items()}
            existing = DailyLog.objects.filter(flock=flock, date=row_date).first()

            if existing is None:
                self._create(flock, row_date, owner, new_values, summary)
            else:
                self._update(existing, new_values, owner, summary)

        # Report (never delete) logs the CSV doesn't know about, e.g. entered live in the app.
        stored = DailyLog.objects.filter(flock__owner=owner).values_list("flock_id", "date")
        extras = sorted(d for f, d in stored if (f, d) not in csv_keys)
        summary["db_only"] = len(extras)
        if extras:
            self.stdout.write(self.style.WARNING(
                f"Not in CSV, left untouched: {', '.join(d.isoformat() for d in extras[:10])}"
                f"{' ...' if len(extras) > 10 else ''}"
            ))
        self._set_active_flock(owner)
        return summary

    def _create(self, flock, row_date, owner, values, summary):
        log = DailyLog(flock=flock, date=row_date, recorded_by=owner, **values)
        # Same opt-out as import_daily_logs: the dataset backfills a retired generation.
        log._allow_inactive_flock = True
        try:
            log.full_clean(exclude=["flock", "recorded_by"])
        except Exception as exc:
            summary["failed"] += 1
            self.stderr.write(self.style.WARNING(f"Skipping {row_date} (validation failed): {exc}"))
            return
        log.save()
        summary["created"] += 1

    def _update(self, log, values, owner, summary):
        # Decimal("38.5") == Decimal("38.50"), so a formatting-only difference in the CSV
        # (38 vs 38.0) is correctly not treated as a change.
        changed = {
            field: (getattr(log, field), new)
            for field, new in values.items()
            if getattr(log, field) != new
        }
        if not changed:
            summary["unchanged"] += 1
            return

        for field, (_, new) in changed.items():
            setattr(log, field, new)
        try:
            log.full_clean(exclude=["flock", "recorded_by"])
        except Exception as exc:
            summary["failed"] += 1
            self.stderr.write(self.style.WARNING(f"Skipping {log.date} update (validation failed): {exc}"))
            return
        log.save()
        for field, (old, new) in changed.items():
            DailyLogEdit.objects.create(
                daily_log=log, field_name=field,
                old_value=str(old), new_value=str(new), changed_by=owner,
            )
        summary["updated"] += 1
        summary["edits"] += len(changed)
        summary["locked_updated"] += int(log.is_locked)
        self.stdout.write(
            f"  {log.date}: " + ", ".join(f"{f} {o} -> {n}" for f, (o, n) in changed.items())
            + (" [locked row]" if log.is_locked else "")
        )
