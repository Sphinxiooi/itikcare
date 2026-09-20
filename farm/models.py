from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

# Fields a DailyLog edit is audited against, and how to render each value as text
# for the DailyLogEdit.old_value/new_value CharFields. Shared by farm/views.py
# (farmer-facing edit) and farm/admin.py (staff edit) so neither path can silently
# apply a change without a paired audit entry.
AUDITED_FIELDS = ["date", "flock_size", "flock_age_weeks", "egg_count", "feed_intake_kg", "temperature_c", "humidity_pct"]


class Flock(models.Model):
    """A generation/batch of ducks.

    Flock size and age change day to day, so they are NOT stored here as mutable
    "current" fields — DailyLog snapshots its own flock_size/flock_age_weeks for the
    day it was recorded, which preserves accurate history for model retraining.
    A new Flock row marks a generation reset (old flock retired, younger flock
    brought in) per itikcare-spec.md section 10 — it is not a data-entry error when
    flock age drops sharply between generations.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="flocks",
        help_text="The farmer this flock (and its farm) belongs to.",
    )
    generation_number = models.PositiveIntegerField()
    started_on = models.DateField()
    is_active = models.BooleanField(default=True)
    is_caged = models.BooleanField(
        default=True,
        help_text="False while ducks are free-range in the field; daily logging and forecasts pause until it's caged again.",
    )
    pending_flock_size = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duck count confirmed when resuming caging after a free-range period; "
        "consumed as the next DailyLog's flock_size prefill, then cleared.",
    )
    pending_flock_age_weeks = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(150)],
        help_text="Flock age confirmed at registration; consumed as the next DailyLog's "
        "flock_age_weeks prefill on a flock's very first entry, then cleared.",
    )
    pending_feed_intake_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(150)],
        help_text="Feed intake confirmed at registration; consumed as the next DailyLog's "
        "feed_intake_kg prefill on a flock's very first entry, then cleared.",
    )

    class Meta:
        ordering = ["generation_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "generation_number"], name="unique_generation_per_owner"
            )
        ]

    def __str__(self):
        return f"Generation {self.generation_number} (started {self.started_on})"


class DailyLog(models.Model):
    """One farmer-entered daily record for a flock.

    flock_size and flock_age_weeks are snapshots as of `date`, mirroring the source
    CSV's per-day columns rather than deriving from Flock, since both values genuinely
    vary day to day within a single generation.
    """

    flock = models.ForeignKey(Flock, on_delete=models.PROTECT, related_name="daily_logs")
    date = models.DateField()
    flock_size = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(100000)],
        help_text="Total duck count that day (includes males).",
    )
    caging_period = models.PositiveIntegerField(
        help_text="Caging-period marker from the historical CSV (itikcare-spec.md section 10). "
        "Used only to segment training data around free-range gaps — never fed to the "
        "RF model as a raw feature.",
    )
    flock_age_weeks = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(150)],
        help_text="Reasonable range 1-150 weeks, padded from the historical dataset's observed 23-107.",
    )
    egg_count = models.PositiveIntegerField(
        validators=[MaxValueValidator(1000)],
        help_text="Reasonable range up to 1000/day, padded from the historical dataset's observed 112-487.",
    )
    feed_intake_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(150)],
        help_text="Reasonable range 0-150 kg/day, padded from the historical dataset's observed 35.5-100.0.",
    )
    temperature_c = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        validators=[MinValueValidator(0), MaxValueValidator(45)],
        help_text="Reasonable range 0-45°C, padded from the historical dataset's observed 24.0-34.4.",
    )
    humidity_pct = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="0-100%, a hard physical ceiling rather than just a historical range.",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="daily_logs"
    )
    is_locked = models.BooleanField(
        default=False,
        help_text="True once this row existed at the time train_forecast_model last "
        "successfully persisted a model for its owner. Every retrain is a full refit "
        "over the owner's entire DailyLog history (never warm_start — see that "
        "command's module docstring), so a persisted model has already learned from "
        "whatever values this row held at that time. Locked rows can never be edited "
        "or deleted again (farm/views.py::farm_record_edit/farm_record_delete).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["flock", "date"], name="unique_daily_log_per_flock_date")
        ]

    def __str__(self):
        return f"{self.date} — {self.flock}"

    def clean(self):
        """Model-level backstop for the rules the farmer-facing layer already enforces
        elsewhere (DailyLogForm/DailyLogEditForm.clean_date for the date range,
        farm/views.py::log_daily_data for "active flock only"). Those forms/views only
        guard the farmer path — this makes the same rules hold for full_clean() callers
        that bypass them entirely, like the Django admin and import_daily_logs."""
        super().clean()
        # Local import to avoid a models<->services import cycle (farm.services already
        # imports DailyLog/Flock from this module). operational_today() rolls the
        # logging day over at 6am rather than midnight — see its docstring.
        from .services import operational_today

        if self.date and self.date > operational_today():
            raise ValidationError({"date": "You can't log data for a future date."})
        if self.date and self.flock_id and self.date < self.flock.started_on:
            raise ValidationError({
                "date": f"This flock started on {self.flock.started_on:%b %d, %Y} — "
                "you can't log data from before then."
            })
        # A retired flock's history is closed — no new entries for it. This only blocks
        # *creating* a log (self._state.adding); existing rows can still be re-cleaned,
        # since edits go through farm_record_edit, which has its own is_active guard.
        # The historical CSV import legitimately backfills already-retired generations,
        # so import_daily_logs opts out via _allow_inactive_flock.
        if (
            self._state.adding
            and self.flock_id
            and not self.flock.is_active
            and not getattr(self, "_allow_inactive_flock", False)
        ):
            raise ValidationError(
                {"flock": "This flock has been retired — you can't log new data for it."}
            )


class DailyLogReminder(models.Model):
    """Audit trail for the "log today's data" reminder email (farm/management/commands/
    send_daily_log_reminders.py) -- one row per owner per day it was sent.

    This is purely a dedup/audit record: the in-app reminder banner (dashboard/
    templates/dashboard/index.html) is computed live from DailyLog/Flock state and
    needs no row here at all. The unique constraint below is what makes the daily
    command idempotent if it's ever run twice on the same day.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="daily_log_reminders"
    )
    flock = models.ForeignKey(Flock, on_delete=models.PROTECT, related_name="daily_log_reminders")
    reminder_date = models.DateField(help_text="The date this reminder was for (owner had no DailyLog for it yet).")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-reminder_date"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "reminder_date"], name="unique_reminder_per_owner_per_day")
        ]

    def __str__(self):
        return f"{self.owner} — reminded for {self.reminder_date}"


class DailyLogEdit(models.Model):
    """Audit trail for DailyLog edits.

    CLAUDE.md requires historical farm data edits to be tracked, never silently
    overwritten — this records the old/new value of a single field on a single edit,
    so a DailyLog change with N edited fields produces N rows here.
    """

    daily_log = models.ForeignKey(DailyLog, on_delete=models.CASCADE, related_name="edits")
    field_name = models.CharField(max_length=50)
    old_value = models.CharField(max_length=255)
    new_value = models.CharField(max_length=255)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.daily_log} — {self.field_name}: {self.old_value} -> {self.new_value}"
