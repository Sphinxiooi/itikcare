"""Daily reminder (email and phone notification) for farmers with an active, caged flock and no log today.

Records a DailyLogReminder so a rerun the same day doesn't send duplicates. Meant to run once a day
from a timer (deploy/). Email and phone notification are independent: if one fails, the other still
goes out, and a rerun retries only the one that failed.

Usage: python manage.py send_daily_log_reminders
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from farm.models import DailyLog, DailyLogReminder, Flock
from farm.services import get_active_flock, operational_today
from notifications.push import send_missing_log_push

logger = logging.getLogger(__name__)

User = get_user_model()


class Command(BaseCommand):
    help = "Email and notify every farmer with an active, caged flock who hasn't logged today's data yet."

    def handle(self, *args, **options):
        # Today's farm date
        today = operational_today()

        # Owners with an active flock (the active flock itself comes from get_active_flock)
        owner_ids = (
            Flock.objects.filter(is_active=True).values_list("owner_id", flat=True).distinct()
        )

        email_count = 0
        push_count = 0
        skipped_count = 0
        # Deleted (deactivated) accounts are never reminded
        for owner in User.objects.filter(pk__in=owner_ids, is_active=True):
            active_flock = get_active_flock(owner)
            if active_flock is None or not active_flock.is_caged:
                continue
            if DailyLog.objects.filter(flock=active_flock, date=today).exists():
                continue

            reminder = DailyLogReminder.objects.filter(owner=owner, reminder_date=today).first()
            need_email = bool(owner.email) and not (reminder and reminder.email_sent)
            need_push = not (reminder and reminder.push_sent_at)

            emailed = need_email and self._try_email(owner, active_flock)
            pushed = need_push and send_missing_log_push(owner, today) > 0

            if not (emailed or pushed):
                if reminder:
                    # Already reminded today
                    skipped_count += 1
                continue

            if reminder is None:
                reminder = DailyLogReminder(
                    owner=owner, flock=active_flock, reminder_date=today, email_sent=False
                )
            reminder.email_sent = reminder.email_sent or emailed
            if pushed:
                reminder.push_sent_at = timezone.now()
            reminder.save()
            email_count += emailed
            push_count += pushed

        self.stdout.write(self.style.SUCCESS(
            f"Sent {email_count} email reminder(s) and notified {push_count} farmer(s) by phone; "
            f"skipped {skipped_count} already reminded today."
        ))

    def _try_email(self, owner, flock):
        try:
            self._send_reminder_email(owner, flock)
        except Exception:
            # One failed send shouldn't stop the run
            logger.exception("Failed to send daily-log reminder to owner_id=%s", owner.pk)
            return False
        return True

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
