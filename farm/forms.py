from django import forms
from django.utils import timezone

from .models import DailyLog
from .services import operational_today
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _

INPUT_CLASSES = (
    "w-full rounded-md border border-gray-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-emerald-700 focus:border-emerald-700"
)


def number_error_messages(what, example, decimals=False):
    """Field error messages that name the field AND say how to fix it.

    `what` is the field's plain-language name ("Egg count"), `example` a typical
    value shown in the hint. Django fills %(limit_value)s with the field's min/max.
    The same wording is produced client-side (static/js/itik-feedback.js), so a
    farmer sees one voice whether the browser or the server caught the mistake.
    """
    shape = _("digits only (a decimal point is fine)") if decimals else _("whole digits only")
    # format_lazy (not `%`) keeps the text translatable per request instead of freezing
    # it in whatever language was active at import time. Django's own
    # %(limit_value)s placeholder has no braces, so it passes through untouched.
    return {
        "required": format_lazy(
            _("Enter {what} — it can't be left empty. Type a number like {example}."),
            what=what, example=example,
        ),
        "invalid": format_lazy(
            _("{what} must be a number, {shape} — for example {example}."),
            what=what, shape=shape, example=example,
        ),
        "min_value": format_lazy(
            _("{what} can't be lower than %(limit_value)s. Raise it to %(limit_value)s or more."),
            what=what,
        ),
        "max_value": format_lazy(
            _("{what} can't be higher than %(limit_value)s. Check for an extra digit."),
            what=what,
        ),
        "max_digits": format_lazy(_("{what} has too many digits. Remove the extras."), what=what),
        "max_decimal_places": format_lazy(
            _("{what} allows only 1 decimal place — for example {example}."),
            what=what, example=example,
        ),
        "max_whole_digits": format_lazy(
            _("{what} has too many digits before the decimal point. Check for an extra digit."),
            what=what,
        ),
    }


def _date_error_messages():
    return {
        "required": _("Pick a date — it can't be left empty."),
        "invalid": _("That isn't a date we can read. Use the calendar picker, or type it as YYYY-MM-DD."),
    }


# Shared by the log form and the record-edit form so both give identical guidance.
DAILY_LOG_ERROR_MESSAGES = {
    "date": _date_error_messages(),
    "flock_size": number_error_messages(_("the flock size"), "120"),
    "egg_count": number_error_messages(_("today's egg count"), "85"),
    "feed_intake_kg": number_error_messages(_("the feed intake"), "9.5", decimals=True),
    "flock_age_weeks": number_error_messages(_("the flock's average age in weeks"), "40"),
    "temperature_c": number_error_messages(_("the temperature"), "29.5", decimals=True),
    "humidity_pct": number_error_messages(_("the humidity"), "72", decimals=True),
}


class DailyLogForm(forms.ModelForm):
    """Daily farm data entry form (flock size is pre-filled but editable)."""

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
            "date": _("Date"),
            "flock_size": _("Flock Size (number of ducks)"),
            "egg_count": _("Today's Egg Count"),
            "feed_intake_kg": _("Feed Intake (kg)"),
            "flock_age_weeks": _("Average Flock Age (weeks)"),
            "temperature_c": _("Temperature (°C)"),
            "humidity_pct": _("Humidity (%)"),
        }
        error_messages = DAILY_LOG_ERROR_MESSAGES
        help_texts = {
            "flock_size": _("Pre-filled from your last entry — adjust if ducks were lost or added."),
            "flock_age_weeks": _("Pre-filled forward from your last entry based on today's date — adjust if needed."),
        }

    def __init__(self, *args, active_flock=None, **kwargs):
        # Limit the date picker (clean_date is the real check)
        super().__init__(*args, **kwargs)
        self.active_flock = active_flock
        self.fields["date"].widget.attrs["max"] = operational_today().isoformat()
        if active_flock is not None:
            self.fields["date"].widget.attrs["min"] = active_flock.started_on.isoformat()

    def clean_date(self):
        """Date must not be in the future or before the flock started."""
        entered_date = self.cleaned_data["date"]
        if entered_date > operational_today():
            raise forms.ValidationError(_("You can't log data for a future date."))
        if self.active_flock is not None and entered_date < self.active_flock.started_on:
            raise forms.ValidationError(
                _("This flock started on %(date)s — you can't log data from before then.")
                % {"date": f"{self.active_flock.started_on:%b %d, %Y}"}
            )
        return entered_date


class FlockRegisterForm(forms.Form):
    """New flock's starting details (staged on the flock as pending_* values for its first DailyLog)."""

    started_on = forms.DateField(
        label=_("Flock Start Date"),
        help_text=_("When this flock began. Use today's date if it's brand new, or a past "
        "date if you've been raising it for a while and are only just starting to log it here."),
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT_CLASSES}),
        error_messages=_date_error_messages(),
    )
    flock_size = forms.IntegerField(
        label=_("Flock Size (number of ducks)"),
        min_value=1,
        max_value=100000,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "100000"}),
        error_messages=DAILY_LOG_ERROR_MESSAGES["flock_size"],
    )
    flock_age_weeks = forms.IntegerField(
        label=_("Flock Age (weeks)"),
        min_value=1,
        max_value=150,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "150"}),
        error_messages=DAILY_LOG_ERROR_MESSAGES["flock_age_weeks"],
    )
    feed_intake_kg = forms.DecimalField(
        label=_("Feed Intake (kg/day)"),
        min_value=0,
        max_value=150,
        error_messages=DAILY_LOG_ERROR_MESSAGES["feed_intake_kg"],
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "150"}),
    )

    def __init__(self, *args, **kwargs):
        # Limit the date picker to today and default to today (clean_started_on is the real check)
        super().__init__(*args, **kwargs)
        self.fields["started_on"].widget.attrs["max"] = timezone.localdate().isoformat()
        self.fields["started_on"].initial = timezone.localdate()

    def clean_started_on(self):
        started_on = self.cleaned_data["started_on"]
        if started_on > timezone.localdate():
            raise forms.ValidationError(_("Flock start date can't be in the future."))
        return started_on


class FlockResumeCagingForm(forms.Form):
    """Confirms the current duck count when resuming caging after free-range."""

    flock_size = forms.IntegerField(
        label=_("Current Flock Size (ducks)"),
        help_text=_("Adjust if ducks were added or lost while free-range."),
        min_value=1,
        max_value=100000,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "100000"}),
        error_messages=DAILY_LOG_ERROR_MESSAGES["flock_size"],
    )


class DailyLogEditForm(forms.ModelForm):
    """Edit form for an existing DailyLog (the view writes an audit row per changed field)."""

    class Meta:
        model = DailyLog
        fields = ["date", "flock_size", "flock_age_weeks", "egg_count", "feed_intake_kg", "temperature_c", "humidity_pct"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date", "class": INPUT_CLASSES}),
            "flock_size": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "100000"}),
            "flock_age_weeks": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "1", "max": "150"}),
            "egg_count": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": "0", "max": "1000"}),
            "feed_intake_kg": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "150"}),
            "temperature_c": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "45"}),
            "humidity_pct": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1", "min": "0", "max": "100"}),
        }
        # Reuse the daily-log form labels and error guidance
        labels = DailyLogForm.Meta.labels
        error_messages = DAILY_LOG_ERROR_MESSAGES

    def __init__(self, *args, **kwargs):
        # Limit the date picker to the flock's range (clean_date is the real check)
        super().__init__(*args, **kwargs)
        self.fields["date"].widget.attrs["max"] = operational_today().isoformat()
        self.fields["date"].widget.attrs["min"] = self.instance.flock.started_on.isoformat()

    def clean_date(self):
        """Same date rules as DailyLogForm."""
        entered_date = self.cleaned_data["date"]
        if entered_date > operational_today():
            raise forms.ValidationError(_("You can't log data for a future date."))
        if entered_date < self.instance.flock.started_on:
            raise forms.ValidationError(
                _("This flock started on %(date)s — you can't log data from before then.")
                % {"date": f"{self.instance.flock.started_on:%b %d, %Y}"}
            )
        return entered_date
