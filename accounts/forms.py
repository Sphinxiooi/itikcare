from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    SetPasswordForm,
    UserCreationForm,
)

from .auth_backends import is_ambiguous_login_identifier
from .libmanan_barangays import BARANGAYS
from .models import User

INPUT_CLASSES = (
    "w-full rounded-md border border-gray-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-emerald-700 focus:border-emerald-700"
)

FILE_INPUT_CLASSES = (
    "block w-full text-sm text-gray-500 "
    "file:mr-4 file:rounded-md file:border-0 file:bg-emerald-50 file:px-4 file:py-2 "
    "file:text-sm file:font-semibold file:text-emerald-700 hover:file:bg-emerald-100 "
    "file:transition-colors file:cursor-pointer cursor-pointer "
    "focus:outline-none focus:ring-2 focus:ring-emerald-700 focus:ring-offset-2 rounded-md"
)

LIBMANAN_SUFFIX = ", Libmanan, Camarines Sur, Philippines"


def barangay_choice_field(label="Farm address"):
    """The barangay dropdown shared by SignupForm and AccountSettingsForm below --
    every farmer in this single-farm-context app is in Libmanan, so restricting to a
    fixed choice list (rather than trusting typed text) guarantees every address is
    both in-bounds and spelled consistently. Pair with expand_barangay_address()/
    barangay_from_address() to convert to/from the full string actually stored on
    User.address.
    """
    return forms.ChoiceField(
        choices=[("", "Select your barangay")] + [(b, b) for b in BARANGAYS],
        label=label,
    )


def expand_barangay_address(barangay):
    """Turn a picked barangay into the full string actually geocoded and stored on
    User.address -- see barangay_choice_field's docstring."""
    return f"{barangay}{LIBMANAN_SUFFIX}"


def barangay_from_address(address):
    """Inverse of expand_barangay_address: recover the bare barangay name from a
    stored User.address string, so a form editing an existing user can pre-select the
    right dropdown option instead of showing "Select your barangay" every time."""
    if address and address.endswith(LIBMANAN_SUFFIX):
        return address[: -len(LIBMANAN_SUFFIX)]
    return ""


class StyledAuthenticationForm(AuthenticationForm):
    """AuthenticationForm with Tailwind classes on its widgets, plus a relaxed login
    identifier: accounts.auth_backends.UsernameEmailOrFullNameBackend already accepts
    username, email, or full name in the "username" field's value, but a full name
    that matches more than one account can't be resolved there (full names aren't
    unique) -- it's treated as a plain failed login by that backend. This override
    runs the same lookup first so that specific case gets its own, more useful error
    message instead of the generic "didn't match" one.

    Django's built-in LoginView doesn't add CSS classes to its fields, so this is a
    thin subclass rather than hand-rendering the whole form field by field.
    """

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": (
            "Please enter a correct username, email, or full name, and the correct "
            "password (passwords are case-sensitive)."
        ),
        "ambiguous_name": (
            "That name matches more than one account — log in with your username or "
            "email instead."
        ),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Username, email, or full name"
        self.fields["username"].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["password"].widget.attrs.update({"class": INPUT_CLASSES + " pr-10"})

    def clean(self):
        identifier = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        if identifier and password and is_ambiguous_login_identifier(identifier):
            raise forms.ValidationError(
                self.error_messages["ambiguous_name"], code="ambiguous_name"
            )
        return super().clean()


class UsernameLookupForm(forms.Form):
    """Step 1 of the password-reset flow (accounts/views.py's request_reset_code):
    just the username, so a farmer who forgot which email they signed up with (email
    is optional at signup — see SignupForm's docstring below) can still self-serve."""

    username = forms.CharField(
        label="Username", widget=forms.TextInput(attrs={"class": INPUT_CLASSES})
    )


class VerifyResetCodeForm(SetPasswordForm):
    """Step 2 of the password-reset flow (accounts/views.py's verify_reset_code):
    SetPasswordForm's new_password1/new_password2 (with Django's usual password
    validators) plus the 6-digit code emailed in step 1. The code itself is checked
    against the stored PasswordResetCode by the view, not here — this form only
    validates that it looks like a 6-digit code and that the new password is valid."""

    code = forms.CharField(
        label="Verification code",
        min_length=6,
        max_length=6,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "inputmode": "numeric", "autocomplete": "one-time-code"}
        ),
    )

    field_order = ["code", "new_password1", "new_password2"]

    def clean_code(self):
        code = self.cleaned_data["code"]
        if not code.isdigit():
            raise forms.ValidationError("Enter the 6-digit code exactly as emailed to you.")
        return code

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

    address is a required dropdown of Libmanan barangays (accounts.libmanan_barangays.
    BARANGAYS), not free text — every farmer in this single-farm-context app is in
    Libmanan, so restricting to a fixed choice list (rather than trusting typed text)
    guarantees every address is both in-bounds and spelled consistently. clean_address
    below expands the picked barangay into a full "<Barangay>, Libmanan, Camarines Sur,
    Philippines" string before it's saved, since that's what actually gets geocoded.
    accounts.views.signup geocodes it into User.latitude/longitude via
    farm.weather.geocode_address so weather prefill in farm/weather.py can use the
    farmer's own coordinates instead of always falling back to the global
    FARM_LATITUDE/FARM_LONGITUDE settings. An unresolvable address still never blocks
    signup — it just leaves latitude/longitude unset — but the field itself is required,
    since every farmer has a real barangay to pick. latitude/longitude aren't form
    fields at all — they're only ever set from the geocoding result, never typed in
    directly.

    email is optional: it's the only channel the built-in password-reset flow
    (itikcare/urls.py) can send a reset link to, so a farmer who skips it simply can't
    self-service a forgotten password later — an admin resets it manually via /admin/
    instead. Never made required so signup keeps its low barrier.
    """

    class Meta:
        model = User
        fields = ["username", "email", "address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["address"] = barangay_choice_field()
        for field_name in ("username", "email", "password1", "password2", "address"):
            self.fields[field_name].widget.attrs.update({"class": INPUT_CLASSES})
        for field_name in ("password1", "password2"):
            self.fields[field_name].widget.attrs.update({"class": INPUT_CLASSES + " pr-10"})
        self.fields["email"].required = False

    def clean_address(self):
        return expand_barangay_address(self.cleaned_data["address"])


class AccountSettingsForm(forms.ModelForm):
    """Lets a logged-in farmer edit their own personal data from Account Settings
    (accounts/views.py's account_settings) -- name, username, email, farm address, and
    profile picture. Reused unchanged as the "finish setting up your account" form a
    brand-new Google sign-in is redirected to (accounts/views.py's google_callback),
    since a Google account is created with no name/address at all.

    address works the same "dropdown in, full string out" way as SignupForm above
    (see barangay_choice_field's docstring) -- __init__ additionally reverses that on
    the way *in*, via barangay_from_address, so editing an existing user shows their
    current barangay selected instead of the placeholder every time.

    username keeps the model's own unique=True validation for free (ModelForm already
    excludes `instance` from that check). email doesn't have a matching DB constraint
    -- clean_email below enforces it here instead, now that email is also a valid login
    identifier (accounts/auth_backends.py) and two accounts sharing one would make that
    login path ambiguous.
    """

    class Meta:
        model = User
        fields = ["avatar", "first_name", "last_name", "username", "email", "address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["address"] = barangay_choice_field()
        # ModelForm.__init__ already populated self.initial["address"] from the
        # instance's full stored address string (via model_to_dict) -- that dict entry
        # takes priority over field.initial when the form is unbound, so the override
        # has to land there too, not just on the field.
        self.initial["address"] = barangay_from_address(self.instance.address)
        self.fields["address"].required = False
        self.fields["first_name"].required = False
        self.fields["last_name"].required = False
        self.fields["email"].required = False
        for field_name in ("first_name", "last_name", "username", "email", "address"):
            self.fields[field_name].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["avatar"].widget.attrs.update({"class": FILE_INPUT_CLASSES})

    def clean_email(self):
        email = self.cleaned_data["email"]
        if email:
            already_taken = (
                User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists()
            )
            if already_taken:
                raise forms.ValidationError("That email is already in use by another account.")
        return email

    def clean_address(self):
        barangay = self.cleaned_data["address"]
        return expand_barangay_address(barangay) if barangay else ""


class AccountDeletionForm(forms.Form):
    """Confirms self-service account deletion (accounts/views.py's delete_account)
    before it proceeds. Some accounts have no local password at all -- a Google-only
    sign-in gets User.set_unusable_password() (see google_callback) -- so "confirm with
    your password" isn't always possible. This form picks the strongest confirmation
    the account actually supports: the current password when there is one, otherwise
    typing the username, rather than offering both as if either always worked.

    confirmation_mode is exposed for the template to label the single field correctly
    ("Current password" vs "Type your username to confirm").
    """

    confirmation = forms.CharField(label="Confirm", widget=forms.PasswordInput)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user.has_usable_password():
            self.confirmation_mode = "password"
            self.fields["confirmation"].label = "Current password"
        else:
            self.confirmation_mode = "username"
            self.fields["confirmation"].label = f'Type your username ("{user.username}") to confirm'
            self.fields["confirmation"].widget = forms.TextInput()
        self.fields["confirmation"].widget.attrs.update({"class": INPUT_CLASSES})

    def clean_confirmation(self):
        value = self.cleaned_data["confirmation"]
        if self.confirmation_mode == "password":
            if not self.user.check_password(value):
                raise forms.ValidationError("Incorrect password.")
        else:
            if value != self.user.username:
                raise forms.ValidationError("Type your username exactly to confirm.")
        return value
