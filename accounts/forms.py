from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    SetPasswordForm,
    UserCreationForm,
)

from .auth_backends import is_ambiguous_login_identifier
from .contact import (
    email_domain_accepts_mail,
    generate_unique_username,
    normalize_ph_mobile,
    split_contact,
)
from .libmanan_barangays import BARANGAYS
from .models import User
from django.utils.translation import gettext_lazy as _

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


def barangay_choice_field(label=_("Farm address")):
    """Barangay dropdown shared by signup and account settings (Libmanan only)."""
    return forms.ChoiceField(
        choices=[("", _("Select your barangay"))] + [(b, b) for b in BARANGAYS],
        label=label,
    )


def expand_barangay_address(barangay):
    """Turn a picked barangay into the full address stored on User.address."""
    return f"{barangay}{LIBMANAN_SUFFIX}"


def barangay_from_address(address):
    """Recover the barangay name from a stored address (empty if it doesn't match)."""
    if address and address.endswith(LIBMANAN_SUFFIX):
        return address[: -len(LIBMANAN_SUFFIX)]
    return ""


class StyledAuthenticationForm(AuthenticationForm):
    """Login form with Tailwind classes and a clear error for names that match several accounts."""

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _(
            "Please enter a correct name, email, or phone number, and the correct "
            "password (passwords are case-sensitive)."
        ),
        "ambiguous_name": _(
            "That name matches more than one account — log in with your full name, "
            "email, or phone number instead."
        ),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("Name, email, or phone number")
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


class AccountLookupForm(forms.Form):
    """Password reset step 1: name, email or phone number (field kept named "username")."""

    username = forms.CharField(
        label=_("Name, email, or phone number"),
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES}),
    )


class VerifyResetCodeForm(SetPasswordForm):
    """Password reset step 2: new password plus the emailed 6-digit code (code is checked in the view)."""

    code = forms.CharField(
        label=_("Verification code"),
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
            raise forms.ValidationError(_("Enter the 6-digit code exactly as emailed to you."))
        return code

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["new_password2"].widget.attrs.update({"class": INPUT_CLASSES})


def _check_email_available(email, exclude_pk=None):
    """Email must be unused and its domain able to receive mail."""
    already_taken = User.objects.filter(email__iexact=email).exclude(pk=exclude_pk).exists()
    if already_taken:
        raise forms.ValidationError(_("That email is already in use by another account."))
    if not email_domain_accepts_mail(email):
        raise forms.ValidationError(
            _("This email address doesn't seem to exist — check the spelling "
            "(e.g. gmail.com).")
        )


def _check_phone_available(phone, exclude_pk=None):
    """Mobile number must not belong to another account."""
    if User.objects.filter(phone_number=phone).exclude(pk=exclude_pk).exists():
        raise forms.ValidationError(_("That mobile number is already in use by another account."))


class SignupForm(UserCreationForm):
    """Farmer signup: full name, optional email/phone, barangay, password and privacy consent."""

    full_name = forms.CharField(label=_("Full name"), max_length=150)
    contact = forms.CharField(label=_("Email or phone number"), max_length=254, required=False)
    privacy_consent = forms.BooleanField(
        required=True,
        error_messages={"required": _("Please agree to the Privacy Notice to create your account.")},
    )

    field_order = ["full_name", "contact", "address", "password1", "password2", "privacy_consent"]

    class Meta:
        model = User
        fields = ["address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["address"] = barangay_choice_field()
        self.order_fields(self.field_order)
        for field_name in ("full_name", "contact", "address"):
            self.fields[field_name].widget.attrs.update({"class": INPUT_CLASSES})
        for field_name in ("password1", "password2"):
            self.fields[field_name].widget.attrs.update({"class": INPUT_CLASSES + " pr-10"})
        self.fields["full_name"].widget.attrs.update({"autocomplete": "name"})
        self.fields["contact"].widget.attrs.update(
            {"placeholder": "you@gmail.com or 09XXXXXXXXX", "autocomplete": "username"}
        )
        self.fields["privacy_consent"].widget.attrs.update(
            {"class": "h-4 w-4 rounded border-gray-300 text-emerald-700 focus:ring-emerald-700"}
        )

    def clean_full_name(self):
        # Collapse repeated spaces
        full_name = " ".join(self.cleaned_data["full_name"].split())
        if not full_name:
            raise forms.ValidationError(_("Enter your full name."))
        return full_name

    def clean_contact(self):
        email, phone = split_contact(self.cleaned_data.get("contact"))
        if email:
            _check_email_available(email)
        if phone:
            _check_phone_available(phone)
        # Stashed for _post_clean (contact is not a model field)
        self.contact_email, self.contact_phone = email, phone
        return email or phone or ""

    def clean_address(self):
        return expand_barangay_address(self.cleaned_data["address"])

    def _post_clean(self):
        # Copy name/contact onto the instance before password validation runs
        first_name, _, last_name = self.cleaned_data.get("full_name", "").partition(" ")
        self.instance.first_name = first_name
        self.instance.last_name = last_name
        self.instance.email = getattr(self, "contact_email", "")
        self.instance.phone_number = getattr(self, "contact_phone", None)
        super()._post_clean()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = generate_unique_username(self.cleaned_data["full_name"])
        if commit:
            user.save()
        return user


class AccountSettingsForm(forms.ModelForm):
    """Personal-data settings form (also used to finish setting up a new Google account)."""

    class Meta:
        model = User
        fields = [
            "avatar", "first_name", "last_name", "username", "email", "phone_number", "address",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["address"] = barangay_choice_field()
        # Pre-select the saved barangay in the dropdown
        self.initial["address"] = barangay_from_address(self.instance.address)
        self.fields["address"].required = False
        self.fields["first_name"].required = False
        self.fields["last_name"].required = False
        self.fields["email"].required = False
        self.fields["phone_number"] = forms.CharField(
            label=_("Mobile number"),
            required=False,
            max_length=16,  # room for "+63 917 123 4567" before normalizing
            widget=forms.TextInput(attrs={"placeholder": "09XXXXXXXXX", "inputmode": "tel"}),
        )
        for field_name in (
            "first_name", "last_name", "username", "email", "phone_number", "address",
        ):
            self.fields[field_name].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["avatar"].widget.attrs.update({"class": FILE_INPUT_CLASSES})

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if email and email != (self.instance.email or "").lower():
            # Re-check the email only if it changed
            _check_email_available(email, exclude_pk=self.instance.pk)
        return email

    def clean_phone_number(self):
        raw = self.cleaned_data.get("phone_number")
        if not raw:
            return None  # null, not "" -- see User.phone_number's help_text
        phone = normalize_ph_mobile(raw)
        if phone is None:
            raise forms.ValidationError(_("Enter an 11-digit mobile number (09XXXXXXXXX)."))
        _check_phone_available(phone, exclude_pk=self.instance.pk)
        return phone

    def clean_address(self):
        barangay = self.cleaned_data["address"]
        return expand_barangay_address(barangay) if barangay else ""


class AccountDeletionForm(forms.Form):
    """Confirms account deletion by password, or by typing the username when no password is set."""

    confirmation = forms.CharField(label=_("Confirm"), widget=forms.PasswordInput)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user.has_usable_password():
            self.confirmation_mode = "password"
            self.fields["confirmation"].label = _("Current password")
        else:
            self.confirmation_mode = "username"
            self.fields["confirmation"].label = _('Type your username ("%(username)s") to confirm') % {
                "username": user.username
            }
            self.fields["confirmation"].widget = forms.TextInput()
        self.fields["confirmation"].widget.attrs.update({"class": INPUT_CLASSES})

    def clean_confirmation(self):
        value = self.cleaned_data["confirmation"]
        if self.confirmation_mode == "password":
            if not self.user.check_password(value):
                raise forms.ValidationError(_("Incorrect password."))
        else:
            if value != self.user.username:
                raise forms.ValidationError(_("Type your username exactly to confirm."))
        return value
