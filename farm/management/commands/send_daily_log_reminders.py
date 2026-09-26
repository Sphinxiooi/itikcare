"""Daily reminder: email any farmer with an active, caged flock who hasn't logged
today's data yet, and record a DailyLogReminder row so a rerun the same day doesn't
send a duplicate (see DailyLogReminder's docstring).

Meant to run once a day, unattended, via the systemd timer in deploy/
(itikcare-reminders.timer/.service) -- not invoked by any farmer-facing code path.
The in-app half of this reminder (a banner on the dashboard) needs no command at
all: it's computed live from the same DailyLog/Flock state every time the dashboard
is loaded, so it can never fall out of sync with what's actually true.

Run as:
    python manage.py send_daily_log_reminders
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
        # operational_today(), not the plain calendar date: this farm's logging day
        # rolls over at 6am, not midnight (see farm.services.operational_today). This
        # command only ever runs post-6am via the timer, so this is purely defensive —
        # it protects a future manual re-run before 6am from reminding a farmer about
        # a day that, operationally, hasn't started yet.
        today = operational_today()

        # Flock.owner has no direct is_active filter of its own -- get_active_flock
        # (farm/services.py) is the single source of truth for "which flock, if any,
        # is this owner's current one", same helper the dashboard/farm views already
        # use, so this command can't drift from what a farmer actually sees as active.
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
                # Already reminded today -- a rerun of this command shouldn't
                # double-email the same farmer.
                skipped_count += 1
                continue

            try:
                self._send_reminder_email(owner, active_flock)
            except Exception:
                # One bad address/SMTP hiccup shouldn't stop the run for every other
                # farmer -- same posture as accounts.views.request_reset_code's own
                # password-reset email send.
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
        # No request here (this runs from a timer), so the button's absolute URL is
        # built from settings.SITE_URL; if that's unset the email simply omits it.
        log_url = f"{settings.SITE_URL}{reverse('log_daily_data')}" if settings.SITE_URL else ""
        context = {"user": owner, "flock": flock, "log_url": log_url}
        send_mail(
            subject,
            render_to_string("farm/daily_log_reminder_email.txt", context),
            None,
            [owner.email],
            html_message=render_to_string("farm/daily_log_reminder_email.html", context),
        )
