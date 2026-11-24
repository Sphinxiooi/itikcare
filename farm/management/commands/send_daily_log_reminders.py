"""Daily reminder email for farmers with an active, caged flock and no log today.

Records a DailyLogReminder so a rerun the same day doesn't send duplicates. Meant to run once a day
from a timer (deploy/).

Usage: python manage.py send_daily_log_reminders
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.urls import reverse

from farm.models import DailyLog, DailyLogReminder, Flock
from farm.services import get_active_flock, operational_today

logger = logging.getLogger(__name__)

User = get_user_model()


class Command(BaseCommand):
    help = "Email + record a reminder for every farmer with an active, caged flock who hasn't logged today's data yet."

    def handle(self, *args, **options):
        # Today's farm date
        today = operational_today()

        # Owners with an active flock (the active flock itself comes from get_active_flock)
        owner_ids = (
            Flock.objects.filter(is_active=True).values_list("owner_id", flat=True).distinct()
        )

        sent_count = 0
        skipped_count = 0
        for owner in User.objects.filter(pk__in=owner_ids):
            active_flock = get_active_flock(owner)
            if active_flock is None or not active_flock.is_caged:
                continue
            if DailyLog.objects.filter(flock=active_flock, date=today).exists():
                continue
            if DailyLogReminder.objects.filter(owner=owner, reminder_date=today).exists():
                # Already reminded today
                skipped_count += 1
                continue

            try:
                self._send_reminder_email(owner, active_flock)
            except Exception:
                # One failed send shouldn't stop the run
                logger.exception("Failed to send daily-log reminder to owner_id=%s", owner.pk)
                continue

            DailyLogReminder.objects.create(owner=owner, flock=active_flock, reminder_date=today)
            sent_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Sent {sent_count} reminder(s); skipped {skipped_count} already reminded today."
        ))

    def _send_reminder_email(self, owner, flock):
        subject = "".join(
            render_to_string("farm/daily_log_reminder_subject.txt").splitlines()
        )
        # No request here, so the link comes from settings.SITE_URL (omitted if unset)
        log_url = f"{settings.SITE_URL}{reverse('log_daily_data')}" if settings.SITE_URL else ""
        context = {"user": owner, "flock": flock, "log_url": log_url}
        send_mail(
            subject,
            render_to_string("farm/daily_log_reminder_email.txt", context),
            None,
            [owner.email],
            html_message=render_to_string("farm/daily_log_reminder_email.html", context),
        )
