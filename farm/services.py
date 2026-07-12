"""Small ORM-facing helpers shared across farm/dashboard/forecasting views.

Kept here (rather than duplicated per-view) because "the active flock" is looked up
identically in many places, and every one of those lookups must be scoped to the
requesting farmer now that more than one farm's data lives in the same tables.
"""

from datetime import date

from django.db.models import Max

from .models import DailyLog, Flock

# A live entry more than this many days after the flock's previous log is treated as
# the start of a new caging period (i.e. the flock was free-ranged in between and has
# just been re-caged — itikcare-spec.md section 10). Chosen from the historical CSV
# import: every gap that stayed within one caging_period was <= 5 days, and every real
# caging-period boundary was >= 43 days, so 14 sits safely in between either reading.
CAGING_PERIOD_GAP_DAYS = 14


def get_active_flock(owner):
    """The requesting farmer's own active Flock, or None if they don't have one yet."""
    return Flock.objects.filter(owner=owner, is_active=True).order_by("-generation_number").first()


def get_effective_coordinates(owner):
    """(latitude, longitude) to use for owner's weather lookups: their own farm
    location if they've set one (accounts.User.latitude/longitude, captured at signup),
    else (None, None) so farm.weather's own settings.FARM_LATITUDE/FARM_LONGITUDE
    fallback applies (the foundation farmer's location) — covers every farmer who
    signed up without sharing a location, including all pre-existing users.
    """
    if owner.latitude is not None and owner.longitude is not None:
        return owner.latitude, owner.longitude
    return None, None


def current_flock_age_weeks(daily_log):
    """Project a DailyLog's flock_age_weeks forward to today's calendar date.

    daily_log.flock_age_weeks is a snapshot as of daily_log.date, not a live value —
    it goes stale as soon as a day passes without a new log (e.g. a free-range gap,
    itikcare-spec.md section 10), so anywhere the UI displays "current" flock age must
    add the calendar weeks elapsed since that snapshot rather than showing it as-is.
    Mirrors the prefill math in views.log_daily_data. Returns None if daily_log is None.
    """
    if daily_log is None:
        return None
    weeks_elapsed = (date.today() - daily_log.date).days // 7
    return daily_log.flock_age_weeks + weeks_elapsed


def assign_caging_periods(active_flock, owner, new_dates_sorted):
    """Caging-period numbers for a batch of new DailyLog dates for active_flock.

    ``new_dates_sorted`` must already be sorted ascending. Returns a list of ints, one
    per date, in the same order — the identical rule views.log_daily_data applies to a
    single new entry, just walked across a whole batch (so a CSV bulk import and a
    farmer typing rows in one at a time can never disagree on where a caging period
    boundary falls).

    Bridges from whatever history already exists for this flock: if the flock has no
    DailyLog yet, the first date in the batch continues this *owner's* overall
    caging_period counter (Max across all their flocks — see log_daily_data's docstring
    on why a flock-retirement reset must continue the counter, not restart at 1)
    unconditionally, with no gap check possible since there's no prior date to compare
    against. Otherwise it bridges from the flock's own most recent existing log. Every
    date after the first in the batch is compared to the *previous date in the batch*
    (not always the pre-existing history), so an internal gap partway through an
    imported file is detected exactly like a live gap would be.
    """
    previous_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    if previous_log is None:
        max_caging_period = DailyLog.objects.filter(flock__owner=owner).aggregate(
            Max("caging_period")
        )["caging_period__max"]
        prev_date, prev_period = None, max_caging_period or 0
    else:
        prev_date, prev_period = previous_log.date, previous_log.caging_period

    periods = []
    for new_date in new_dates_sorted:
        if prev_date is None:
            new_period = prev_period + 1
        else:
            gap_days = (new_date - prev_date).days
            new_period = prev_period + 1 if gap_days > CAGING_PERIOD_GAP_DAYS else prev_period
        periods.append(new_period)
        prev_date, prev_period = new_date, new_period
    return periods
