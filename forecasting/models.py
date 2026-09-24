from django.db import models

from farm.models import DailyLog, Flock


class Forecast(models.Model):
    """One Random Forest prediction run for a flock.

    source_logs links back to the exact DailyLog rows the prediction was generated
    from, so a forecast can always be traced back to its input data — required for
    the prescriptive module's explainability and for the thesis defense.
    """

    flock = models.ForeignKey(Flock, on_delete=models.PROTECT, related_name="forecasts")
    source_logs = models.ManyToManyField(DailyLog, related_name="forecasts")
    forecast_date = models.DateField(help_text="The date this forecast is predicting for.")
    # Both null while the forecast is withheld during a warm-up period (a brand-new farm's
    # first week, or the first days back after free-range) -- see
    # forecasting/services.py's forecast_readiness. The Forecast row itself still exists
    # then, because Recommendations hang off it and keep working from the logged data.
    predicted_daily_yield = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Null = prediction withheld during the warm-up period (see forecast_readiness).",
    )
    predicted_tri_day_yield = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Null = prediction withheld during the warm-up period (see forecast_readiness).",
    )
    predicted_next_day1_yield = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Best-effort forecast for forecast_date + 1 day (tomorrow). Derived by "
        "recursively re-applying the same daily_pipeline used for predicted_daily_yield, "
        "not a separately trained model — see forecasting/services.py's "
        "_predict_next_days. Null until a forecast generated after this field existed.",
    )
    predicted_next_day2_yield = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Best-effort forecast for forecast_date + 2 days. See "
        "predicted_next_day1_yield — this step's lag1/roll3 inputs partly depend on that "
        "day's own prediction, so error compounds further here.",
    )
    predicted_next_day3_yield = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Best-effort forecast for forecast_date + 3 days. See "
        "predicted_next_day1_yield — the most compounded of the three recursive steps.",
    )
    feature_importances = models.JSONField(
        help_text="RF feature importance scores at prediction time, e.g. "
        '{"temperature_c": 0.31, "feed_intake_kg": 0.22, ...}. '
        "Feeds the prescriptive module's rule prioritization."
    )
    model_version = models.CharField(max_length=50, help_text="Identifies which trained model artifact produced this forecast.")
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-forecast_date"]
        constraints = [
            models.UniqueConstraint(fields=["flock", "forecast_date"], name="unique_forecast_per_flock_date")
        ]

    def __str__(self):
        return f"Forecast for {self.forecast_date} ({self.flock})"

    @property
    def has_prediction(self) -> bool:
        """False while the yield prediction is withheld for warm-up (see
        forecasting/services.py's forecast_readiness) -- recommendations still exist."""
        return self.predicted_daily_yield is not None
