"""Export chosen farmers' data to a JSON fixture for `manage.py loaddata` on a fresh database.

Includes accounts, flocks, DailyLogs (+ edit history), reminders, Forecasts and Recommendations
with their primary keys. Users referenced by that data (e.g. the admin who recorded imports)
are included too and listed. Not exported: model files, sessions, reset codes, avatars.
The output contains password hashes and emails (gitignored); never commit or share it.

Usage:
    python manage.py export_farm_data --owner-ids 2 3 [--output path/to/file.json]
"""

import json
from itertools import chain
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError

from farm.models import DailyLog, DailyLogEdit, DailyLogReminder, Flock
from forecasting.models import Forecast
from recommendations.models import Recommendation

User = get_user_model()


class Command(BaseCommand):
    help = "Export the given owners' accounts, flocks, logs, forecasts and recommendations to a loaddata JSON fixture."

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-ids", type=int, nargs="+", required=True,
            help="Primary keys of the farmer accounts whose data to export.",
        )
        parser.add_argument(
            "--output", default=str(settings.BASE_DIR / "deploy" / "farm_data.json"),
            help="Where to write the fixture (default: deploy/farm_data.json, gitignored).",
        )

    def handle(self, *args, **options):
        owner_ids = sorted(set(options["owner_ids"]))
        owners = User.objects.filter(pk__in=owner_ids)
        missing = set(owner_ids) - set(owners.values_list("pk", flat=True))
        if missing:
            raise CommandError(f"No such user id(s): {sorted(missing)}")

        flocks = Flock.objects.filter(owner_id__in=owner_ids)
        logs = DailyLog.objects.filter(flock__in=flocks)
        edits = DailyLogEdit.objects.filter(daily_log__in=logs)
        reminders = DailyLogReminder.objects.filter(owner_id__in=owner_ids)
        forecasts = Forecast.objects.filter(flock__in=flocks)
        recommendations = Recommendation.objects.filter(forecast__in=forecasts)

        # Include every account the exported rows point at (PROTECT foreign keys)
        referenced_ids = (
            set(logs.values_list("recorded_by_id", flat=True))
            | set(edits.values_list("changed_by_id", flat=True))
        )
        extra_ids = sorted(referenced_ids - set(owner_ids))
        users = list(User.objects.filter(pk__in=set(owner_ids) | referenced_ids).order_by("pk"))
        for user in users:
            # In-memory only: the image file isn't exported
            user.avatar = ""

        # Parents before children, ordered by pk so re-runs give an identical file
        objects = chain(
            users,
            flocks.order_by("pk"),
            logs.order_by("pk"),
            edits.order_by("pk"),
            reminders.order_by("pk"),
            forecasts.order_by("pk"),
            recommendations.order_by("pk"),
        )
        payload = serializers.serialize("json", objects, indent=2)

        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(f"Wrote {output}"))
        counts = {}
        for row in json.loads(payload):
            counts[row["model"]] = counts.get(row["model"], 0) + 1
        for model_label, count in counts.items():
            self.stdout.write(f"  {model_label:38s} {count}")
        if extra_ids:
            names = ", ".join(f"{u.pk}:{u.username}" for u in users if u.pk in extra_ids)
            self.stdout.write(self.style.WARNING(
                f"Also included accounts referenced by the exported logs: {names}"
            ))
