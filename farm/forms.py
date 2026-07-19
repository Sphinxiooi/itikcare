from django import forms
from django.utils import timezone

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

    def __init__(self, *args, active_flock=None, **kwargs):
        # Caps the browser's native date picker at today (and, if we know which flock
        # this entry is for, at the flock's start date on the other end) so the
        # farmer can't even pick an out-of-range date — clean_date() below is the
        # real (server-side) guard either way.
        super().__init__(*args, **kwargs)
        self.active_flock = active_flock
        self.fields["date"].widget.attrs["max"] = timezone.localdate().isoformat()
        if active_flock is not None:
            self.fields["date"].widget.attrs["min"] = active_flock.started_on.isoformat()

    def clean_date(self):
        """Keep the date within this flock's normal range: not in the future (a farmer
        can log today or backfill a missed past day, but not log ahead of time for a
        day that hasn't happened yet), and not before this flock even started."""
        entered_date = self.cleaned_data["date"]
        if entered_date > timezone.localdate():
            raise forms.ValidationError("You can't log data for a future date.")
        if self.active_flock is not None and entered_date < self.active_flock.started_on:
            raise forms.ValidationError(
                f"This flock started on {self.active_flock.started_on:%b %d, %Y} — "
                "you can't log data from before then."
            )
        return entered_date


class FlockRegisterForm(forms.Form):
    """Collects a new flock's starting details: size, age, and feed intake.

    A plain Form (not a ModelForm) because these values aren't stored directly on
    Flock — they're staged on the pending_flock_size/pending_flock_age_weeks/
    pending_feed_intake_kg fields (same pattern as FlockResumeCagingForm above) and
    consumed as the prefill for the flock's very first DailyLog in views.log_daily_data.
    Used both for a farm's first-ever flock and for registering the next generation
    after retiring the current one — both go through views.flock_profile, since
    retiring (views.flock_retire) no longer creates a replacement flock itself.
    Flock.started_on is set to today's date by the view, not farmer-entered.
    """

    flock_size = forms.IntegerField(
        label="Flock Size (number of ducks)",
        min_value=1,
        max_value=100000,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "100000"}),
    )
    flock_age_weeks = forms.IntegerField(
        label="Flock Age (weeks)",
        min_value=1,
        max_value=150,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "150"}),
    )
    feed_intake_kg = forms.DecimalField(
        label="Feed Intake (kg/day)",
        min_value=0,
        max_value=150,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "150"}),
    )


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

    def __init__(self, *args, **kwargs):
        # Caps the browser's native date picker at today, and at this record's own
        # flock's start date on the other end, so a date can't be edited outside this
        # flock's normal range — clean_date() below is the real (server-side) guard.
        super().__init__(*args, **kwargs)
        self.fields["date"].widget.attrs["max"] = timezone.localdate().isoformat()
        self.fields["date"].widget.attrs["min"] = self.instance.flock.started_on.isoformat()

    def clean_date(self):
        """Same date-range rules as DailyLogForm — an edit can't move a record's date
        ahead of today, or back before its flock even started."""
        entered_date = self.cleaned_data["date"]
        if entered_date > timezone.localdate():
            raise forms.ValidationError("You can't log data for a future date.")
        if entered_date < self.instance.flock.started_on:
            raise forms.ValidationError(
                f"This flock started on {self.instance.flock.started_on:%b %d, %Y} — "
                "you can't log data from before then."
            )
        return entered_date
