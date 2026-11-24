"""Header bell notifications, computed live from current farm data on every page load.

Only the farmer's read/dismiss state is stored (NotificationReceipt). Each source below is
one small function returning a Notification or None.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from farm.models import DailyLog
from farm.services import get_active_flock, operational_today
from forecasting.models import Forecast
from forecasting.services import current_forecast_warmup
from recommendations.services import ACTIONABLE_PRIORITIES, most_urgent_recommendation

from .models import NotificationReceipt

# How many recent days the "missed days" check looks back
MISSED_DAYS_LOOKBACK = 7

# How long the dismiss (x) button hides a notification whose issue is still unfixed
DISMISS_SNOOZE = timedelta(minutes=30)

# Dropdown order: act now, then warnings, then info
LEVEL_ORDER = {"action": 0, "warning": 1, "info": 2}


@dataclass
class Notification:
    key: str  # stable identity for NotificationReceipt; embeds the date/object it's about
    level: str  # "action" | "warning" | "info" -- drives color and sort order
    icon: str  # an accounts.templatetags.itik_icons ICONS key
    title: str
    message: str
    url: str
    is_unread: bool = True


# ---------- notification sources ----------
def _no_flock(user, flock):
    """No active flock registered."""
    if flock is not None:
        return None
    return Notification(
        key="no_flock",
        level="action",
        icon="flock",
        title=_("Set up your flock"),
        message=_("Register your flock so you can start logging data and getting forecasts."),
        url=reverse("flock_profile"),
    )


def _log_today_missing(user, flock, today):
    """Caged flock with no DailyLog for today."""
    if flock is None or not flock.is_caged:
        return None
    if DailyLog.objects.filter(flock=flock, date=today).exists():
        return None
    return Notification(
        key=f"log_today_missing:{today.isoformat()}",
        level="action",
        icon="clock",
        title=_("Today's log is missing"),
        message=_("Record today's egg count, feed, and weather so your forecast stays up to date."),
        url=reverse("log_daily_data"),
    )


def _missed_days(user, flock, today):
    """Recent unlogged days inside the current caging stretch (free-range gaps don't count)."""
    # Nothing is flagged right after a free-range return (before the first new log)
    if flock is None or not flock.is_caged or flock.starts_new_caging_period:
        return None
    latest_log = DailyLog.objects.filter(flock=flock).order_by("-date").first()
    if latest_log is None:
        return None
    stretch_start = (
        DailyLog.objects.filter(flock=flock, caging_period=latest_log.caging_period)
        .order_by("date")
        .values_list("date", flat=True)
        .first()
    )
    window_start = max(today - timedelta(days=MISSED_DAYS_LOOKBACK), stretch_start, flock.started_on)
    logged_dates = set(
        DailyLog.objects.filter(flock=flock, date__gte=window_start, date__lt=today).values_list("date", flat=True)
    )
    missing = [
        window_start + timedelta(days=offset)
        for offset in range((today - window_start).days)
        if window_start + timedelta(days=offset) not in logged_dates
    ]
    if not missing:
        return None
    return Notification(
        key=f"missed_days:{today.isoformat()}",
        level="warning",
        icon="calendar",
        title=ngettext(
            "%(count)d recent day has no record",
            "%(count)d recent days have no record",
            len(missing),
        ) % {"count": len(missing)},
        message=_("Missing: %(dates)s. Filling these in keeps your forecast accurate.")
        % {"dates": ", ".join(date_format(d, "M d") for d in missing)},
        url=reverse("log_daily_data"),
    )


def _profile_incomplete(user):
    """Account Settings fields left blank (farm location matters most for weather)."""
    missing = []
    if not user.first_name or not user.last_name:
        missing.append("full name")
    if not user.email:
        missing.append("email")
    if not user.address or user.latitude is None or user.longitude is None:
        missing.append("farm location")
    if not user.avatar:
        missing.append("profile photo")
    if not missing:
        return None
    return Notification(
        # Missing fields are part of the key, so dismissing one doesn't hide another
        key=f"profile_incomplete:{','.join(sorted(missing))}",
        level="info",
        icon="info-circle",
        title=_("Complete your profile"),
        # Key keeps English names; only the message is translated
        message=_("Add your %(fields)s in Account Settings.") % {"fields": ", ".join(_(m) for m in missing)},
        url=reverse("account_settings"),
    )


def _todays_forecast(flock, today):
    """The soonest forecast dated today or later (same as the dashboard)."""
    if flock is None or not flock.is_caged:
        return None
    return Forecast.objects.filter(flock=flock, forecast_date__gte=today).order_by("forecast_date").first()


def _urgent_recommendation(forecast):
    """The forecast's most urgent recommendation, if it is at an actionable priority."""
    rec = most_urgent_recommendation(forecast)
    if rec is None or rec.priority not in ACTIONABLE_PRIORITIES:
        return None
    variable = rec.triggered_by.replace("_", " ")
    # Stored rule texts are English; translate at display time
    status = f"{_(rec.status)}: " if rec.status else ""
    return Notification(
        key=f"urgent_rec:{rec.pk}",
        level="warning",
        icon="lightbulb",
        title=_("Recommendation needs attention (%(variable)s)") % {"variable": _(variable)},
        message=f"{status}{_(rec.message)}",
        url=reverse("forecast_recommendations"),
    )


def _forecast_warming_up(flock):
    """Yield prediction is withheld until enough of the farm's own data exists."""
    warmup = current_forecast_warmup(flock)
    if warmup is None:
        return None
    return Notification(
        key=f"warmup:{warmup.reason}:{flock.pk}:{warmup.logs_so_far}",
        level="info",
        icon="trending-up",
        title=_("Forecast is warming up"),
        message=_(
            "%(so_far)s/%(needed)s days logged — egg yield predictions start "
            "once there's enough of your own data. Recommendations are already available."
        ) % {"so_far": warmup.logs_so_far, "needed": warmup.logs_needed},
        url=reverse("forecast_recommendations"),
    )


def _flock_free_range(flock):
    """Flock is out in the field; logging and forecasts are paused."""
    if flock is None or flock.is_caged:
        return None
    return Notification(
        key=f"free_range:{flock.pk}",
        level="info",
        icon="map-pin",
        title=_("Flock is free-range"),
        message=_("Logging and forecasts are paused. Mark the flock as caged when the ducks are back."),
        url=reverse("flock_profile"),
    )


# ---------- combining & read/dismiss state ----------
def build_notifications(user):
    """Every notification that currently applies to the user, most urgent first."""
    flock = get_active_flock(user)
    today = operational_today()
    forecast = _todays_forecast(flock, today)

    candidates = [
        _no_flock(user, flock),
        _log_today_missing(user, flock, today),
        _missed_days(user, flock, today),
        _urgent_recommendation(forecast),
        _forecast_warming_up(flock),
        _flock_free_range(flock),
        _profile_incomplete(user),
    ]
    notifications = [n for n in candidates if n is not None]
    # Stable sort: ties keep the order above
    return sorted(notifications, key=lambda n: LEVEL_ORDER[n.level])


def _dismissal_active(receipt, user, now):
    """True while a dismissal still hides its notification (snoozed for DISMISS_SNOOZE or until next login)."""
    if receipt is None or receipt.dismissed_at is None:
        return False
    if now - receipt.dismissed_at >= DISMISS_SNOOZE:
        return False
    if user.last_login and user.last_login > receipt.dismissed_at:
        return False
    return True


def get_notification_state(user):
    """(visible notifications, unread count) with snoozed dismissals dropped and read ones marked."""
    notifications = build_notifications(user)
    receipts = {
        r.key: r
        for r in NotificationReceipt.objects.filter(user=user, key__in=[n.key for n in notifications])
    }
    now = timezone.now()
    visible = []
    for notification in notifications:
        receipt = receipts.get(notification.key)
        if _dismissal_active(receipt, user, now):
            continue
        read_at = receipt.read_at if receipt else None
        # Returning from a dismissal counts as unread again
        if read_at and receipt.dismissed_at and read_at <= receipt.dismissed_at:
            read_at = None
        notification.is_unread = read_at is None
        visible.append(notification)
    unread_count = sum(1 for n in visible if n.is_unread)
    return visible, unread_count
