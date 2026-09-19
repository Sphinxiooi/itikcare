from django.db import models

from forecasting.models import Forecast


class Recommendation(models.Model):
    """One piece of prescriptive advice generated from a Forecast.

    triggered_by names the specific variable/rule that fired (e.g. "temperature_c"),
    so every recommendation is traceable back to a rule and a feature importance
    value — required by CLAUDE.md for thesis-defense transparency, not just a
    black-box suggestion.
    """

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM_LOW = "medium_low", "Medium-Low"
        MEDIUM = "medium", "Medium/Normal"
        MEDIUM_HIGH = "medium_high", "Medium-High"
        HIGH = "high", "High"

    forecast = models.ForeignKey(Forecast, on_delete=models.CASCADE, related_name="recommendations")
    triggered_by = models.CharField(max_length=50, help_text="The input variable/rule that triggered this recommendation.")
    message = models.TextField(help_text="Plain-language, farmer-facing actionable advice.")
    status = models.CharField(
        max_length=30, blank=True, default="",
        help_text="Short status label shown before the message, e.g. 'Underfed', 'High'.",
    )
    learn_more = models.TextField(
        blank=True, default="",
        help_text="Rationale paragraph from Annex A's 'Learn More' column.",
    )
    reading_summary = models.CharField(
        max_length=100, blank=True, default="",
        help_text="The raw farm reading that triggered this rule, e.g. '33.0°C'.",
    )
    priority = models.CharField(max_length=11, choices=Priority.choices, default=Priority.MEDIUM)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.priority}] {self.triggered_by} — {self.forecast}"
