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
    predicted_daily_yield = models.DecimalField(max_digits=8, decimal_places=2)
    predicted_tri_day_yield = models.DecimalField(max_digits=8, decimal_places=2)
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
