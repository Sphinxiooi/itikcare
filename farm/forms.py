from django import forms

from .models import DailyLog

INPUT_CLASSES = (
    "w-full rounded-md border border-gray-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-emerald-700 focus:border-emerald-700"
)


class DailyLogForm(forms.ModelForm):
    """Daily farm data entry form.

    Matches the Figma "Log Today's Farm Data" screen, which does not collect
    flock_size directly — the view carries it forward from the flock's most recent
    DailyLog instead, since day-to-day flock size rarely changes and re-typing it
    every day isn't what the design asks for.
    """

    class Meta:
        model = DailyLog
        fields = ["date", "egg_count", "feed_intake_kg", "flock_age_weeks", "temperature_c", "humidity_pct"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date", "class": INPUT_CLASSES}),
            "egg_count": forms.NumberInput(attrs={"class": INPUT_CLASSES}),
            "feed_intake_kg": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1"}),
            "flock_age_weeks": forms.NumberInput(attrs={"class": INPUT_CLASSES}),
            "temperature_c": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1"}),
            "humidity_pct": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1"}),
        }
        labels = {
            "egg_count": "Today's Egg Count",
            "feed_intake_kg": "Feed Intake (kg)",
            "flock_age_weeks": "Average Flock Age (weeks)",
            "temperature_c": "Temperature (°C)",
            "humidity_pct": "Humidity (%)",
        }


class DailyLogEditForm(forms.ModelForm):
    """Edit form for an existing DailyLog.

    Deliberately excludes `flock` and `recorded_by` (identity/ownership shouldn't
    change via an edit) — the view diffs the remaining fields against their prior
    values and writes a DailyLogEdit audit row for each one that changed, per
    CLAUDE.md's requirement that historical data edits are never silently applied.
    """

    class Meta:
        model = DailyLog
        fields = ["date", "flock_size", "flock_age_weeks", "egg_count", "feed_intake_kg", "temperature_c", "humidity_pct"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date", "class": INPUT_CLASSES}),
            "flock_size": forms.NumberInput(attrs={"class": INPUT_CLASSES}),
            "flock_age_weeks": forms.NumberInput(attrs={"class": INPUT_CLASSES}),
            "egg_count": forms.NumberInput(attrs={"class": INPUT_CLASSES}),
            "feed_intake_kg": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1"}),
            "temperature_c": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1"}),
            "humidity_pct": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1"}),
        }
