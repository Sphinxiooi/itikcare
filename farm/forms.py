from django import forms

from .models import DailyLog, Flock

INPUT_CLASSES = (
    "w-full rounded-md border border-gray-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-emerald-700 focus:border-emerald-700"
)


class DailyLogForm(forms.ModelForm):
    """Daily farm data entry form.

    flock_size is pre-filled by the view from the flock's most recent DailyLog (or
    left blank for a flock's very first-ever entry, which has no prior log to pull
    from) but is always editable here, so the farmer can adjust it up or down on any
    entry to reflect ducks lost/dead or ducks added that day.
    """

    class Meta:
        model = DailyLog
        fields = ["date", "flock_size", "egg_count", "feed_intake_kg", "flock_age_weeks", "temperature_c", "humidity_pct"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date", "class": INPUT_CLASSES}),
            "flock_size": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "100000"}),
            "egg_count": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "0", "max": "1000"}),
            "feed_intake_kg": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "150"}),
            "flock_age_weeks": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "150"}),
            "temperature_c": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "45"}),
            "humidity_pct": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "100"}),
        }
        labels = {
            "flock_size": "Flock Size (number of ducks)",
            "egg_count": "Today's Egg Count",
            "feed_intake_kg": "Feed Intake (kg)",
            "flock_age_weeks": "Average Flock Age (weeks)",
            "temperature_c": "Temperature (°C)",
            "humidity_pct": "Humidity (%)",
        }
        help_texts = {
            "flock_size": "Pre-filled from your last entry — adjust if ducks were lost or added.",
            "flock_age_weeks": "Pre-filled forward from your last entry based on today's date — adjust if needed.",
        }


class FlockForm(forms.ModelForm):
    """Collects a flock's start date.

    Reused for three lifecycle actions handled in views.flock_profile/flock_retire:
    correcting the active flock's started_on, starting a farm's very first flock
    (no active flock exists yet), and starting the new generation when retiring the
    current one. generation_number and is_active are never farmer-editable directly —
    the view logic sets those explicitly for each action instead.
    """

    class Meta:
        model = Flock
        fields = ["started_on"]
        widgets = {
            "started_on": forms.DateInput(attrs={"type": "date", "class": INPUT_CLASSES}),
        }
        labels = {
            "started_on": "Flock Start Date",
        }


class FlockResumeCagingForm(forms.Form):
    """Confirms the current duck count when resuming caging after a free-range period.

    A plain Form (not a ModelForm bound to Flock.pending_flock_size) so the field is
    always required here, regardless of that model field's null=True/blank=True (which
    only reflects that it's empty the rest of the time, not that it's optional at the
    moment of resuming).
    """

    flock_size = forms.IntegerField(
        label="Current Flock Size (ducks)",
        help_text="Adjust if ducks were added or lost while free-range.",
        min_value=1,
        max_value=100000,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "100000"}),
    )


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
            "flock_age_weeks": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "150"}),
            "egg_count": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "0", "max": "1000"}),
            "feed_intake_kg": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "150"}),
            "temperature_c": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "45"}),
            "humidity_pct": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "100"}),
        }
