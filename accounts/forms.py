from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)

from .models import User

INPUT_CLASSES = (
    "w-full rounded-md border border-gray-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-emerald-700 focus:border-emerald-700"
)


class StyledAuthenticationForm(AuthenticationForm):
    """AuthenticationForm with Tailwind classes on its widgets.

    Django's built-in LoginView doesn't add CSS classes to its fields, so this is a
    thin subclass rather than hand-rendering the whole form field by field.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["password"].widget.attrs.update({"class": INPUT_CLASSES})


class StyledPasswordResetForm(PasswordResetForm):
    """PasswordResetForm with Tailwind classes — same rationale as
    StyledAuthenticationForm above."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs.update({"class": INPUT_CLASSES})


class StyledSetPasswordForm(SetPasswordForm):
    """SetPasswordForm (the "choose a new password" step of the reset flow) with
    Tailwind classes — same rationale as StyledAuthenticationForm above."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["new_password2"].widget.attrs.update({"class": INPUT_CLASSES})


class SignupForm(UserCreationForm):
    """Self-service farmer signup.

    role/is_foundation_farmer are both deliberately left untouched here: User.role
    already defaults to Role.FARMER, and this form never sets is_superuser, so
    User.save()'s auto-promote-to-admin branch never fires for a self-registered
    account. There is exactly one foundation farmer (accounts.User.is_foundation_farmer,
    a one-time data migration), never assigned through signup.

    address is optional free text (e.g. "Libmanan, Camarines Sur") — accounts.views.
    signup geocodes it into User.latitude/longitude via farm.weather.geocode_address so
    weather prefill in farm/weather.py can use the farmer's own coordinates instead of
    always falling back to the global FARM_LATITUDE/FARM_LONGITUDE settings. Signup must
    never block on this (an unresolvable address just leaves latitude/longitude unset),
    so address is never made required, and latitude/longitude aren't form fields at all
    — they're only ever set from the geocoding result, never typed in directly.

    email is optional, same reasoning as address: it's the only channel the built-in
    password-reset flow (itikcare/urls.py) can send a reset link to, so a farmer who
    skips it simply can't self-service a forgotten password later — an admin resets it
    manually via /admin/ instead. Never made required so signup keeps its low barrier.
    """

    class Meta:
        model = User
        fields = ["username", "email", "address"]
        widgets = {
            "address": forms.TextInput(attrs={"placeholder": "e.g. Libmanan, Camarines Sur"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("username", "email", "password1", "password2", "address"):
            self.fields[field_name].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["email"].required = False
        self.fields["address"].required = False
