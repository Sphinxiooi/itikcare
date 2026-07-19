"""One-time backfill: retroactively lock DailyLog rows for owners who already have a
persisted model artifact from before DailyLog.is_locked existed.

Run as:
    python manage.py backfill_locked_daily_logs [--dry-run]

A trained model artifact (models/forecast_model_<owner_id>.joblib) is proof that
train_forecast_model has already done a full refit over that owner's entire DailyLog
history — see DailyLog.is_locked's help_text and train_forecast_model.py's own
docstring on why every retrain is a fresh fit, never warm_start. Going forward, new
successful training runs lock records themselves (train_forecast_model.Command._lock_records);
this command only exists to catch up rows that predate that behaviour.
"""

import re

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from farm.models import DailyLog
from forecasting.services import MODEL_DIR

User = get_user_model()

# Matches forecast_model_<owner_id>.joblib, deliberately not the legacy unsuffixed
# forecast_model.joblib from before multi-tenancy — that filename doesn't identify
# which current owner it belongs to, and it's superseded the moment that owner's next
# retrain writes a properly-suffixed artifact.
MODEL_FILENAME_RE = re.compile(r"^forecast_model_(\d+)\.joblib$")


class Command(BaseCommand):
    help = "One-time backfill locking DailyLogs for owners with an existing trained model artifact."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows per owner would be locked, without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if not MODEL_DIR.exists():
            self.stdout.write(self.style.WARNING(f"No models directory at {MODEL_DIR}; nothing to do."))
            return

        owner_ids = sorted(
            int(match.group(1))
            for path in MODEL_DIR.iterdir()
            if (match := MODEL_FILENAME_RE.match(path.name))
        )

        total_locked = 0
        for owner_id in owner_ids:
            if not User.objects.filter(pk=owner_id).exists():
                self.stdout.write(self.style.WARNING(f"Skipping owner_id={owner_id}: no such user."))
                continue

            queryset = DailyLog.objects.filter(flock__owner_id=owner_id, is_locked=False)
            count = queryset.count()
            if not dry_run:
                queryset.update(is_locked=True)
            total_locked += count
            self.stdout.write(
                f"{'[DRY RUN] Would lock' if dry_run else 'Locked'} {count} record(s) for owner_id={owner_id}."
            )

        self.stdout.write(self.style.SUCCESS(
            f"{'[DRY RUN] ' if dry_run else ''}Done. "
            f"{total_locked} record(s) across {len(owner_ids)} owner(s) with a trained model."
        ))
