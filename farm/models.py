from django.conf import settings
from django.db import models


class Flock(models.Model):
    """A generation/batch of ducks.

    Flock size and age change day to day, so they are NOT stored here as mutable
    "current" fields — DailyLog snapshots its own flock_size/flock_age_weeks for the
    day it was recorded, which preserves accurate history for model retraining.
    A new Flock row marks a generation reset (old flock retired, younger flock
    brought in) per itikcare-spec.md section 10 — it is not a data-entry error when
    flock age drops sharply between generations.
    """

    generation_number = models.PositiveIntegerField(unique=True)
    started_on = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["generation_number"]

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
    flock_size = models.PositiveIntegerField(help_text="Total duck count that day (includes males).")
    flock_age_weeks = models.PositiveIntegerField()
    egg_count = models.PositiveIntegerField()
    feed_intake_kg = models.DecimalField(max_digits=6, decimal_places=2)
    temperature_c = models.DecimalField(max_digits=4, decimal_places=1)
    humidity_pct = models.DecimalField(max_digits=4, decimal_places=1)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="daily_logs"
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
