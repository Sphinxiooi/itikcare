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
    """The barangay dropdown shared by SignupForm and AccountSettingsForm below --
    every farmer in this single-farm-context app is in Libmanan, so restricting to a
    fixed choice list (rather than trusting typed text) guarantees every address is
    both in-bounds and spelled consistently. Pair with expand_barangay_address()/
    barangay_from_address() to convert to/from the full string actually stored on
    User.address.
    """
    return forms.ChoiceField(
        choices=[("", _("Select your barangay"))] + [(b, b) for b in BARANGAYS],
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
    identifier: accounts.auth_backends.UsernameEmailOrFullNameBackend accepts a full
    name, a first name, an email, a mobile number, or a username in the "username"
    field's value -- all case-insensitive. A name that matches more than one account
    can't be resolved there (names aren't unique) -- it's treated as a plain failed
    login by that backend. This override runs the same lookup first so that specific
    case gets its own, more useful error message instead of the generic "didn't
    match" one.

    Django's built-in LoginView doesn't add CSS classes to its fields, so this is a
    thin subclass rather than hand-rendering the whole form field by field.
    """

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
    """Step 1 of the password-reset flow (accounts/views.py's request_reset_code): the
    same "name, email, or phone number" the login form accepts, resolved through the
    same lookup (accounts.auth_backends.find_user_by_login_identifier). Signup no
    longer asks for a username (it's generated -- see SignupForm), so asking for one
    here would lock newer farmers out of self-service reset.

    The field is still named "username" so the reset form template and any bookmarked
    POSTs keep working unchanged."""

    username = forms.CharField(
        label=_("Name, email, or phone number"),
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES}),
    )


class VerifyResetCodeForm(SetPasswordForm):
    """Step 2 of the password-reset flow (accounts/views.py's verify_reset_code):
    SetPasswordForm's new_password1/new_password2 (with Django's usual password
    validators) plus the 6-digit code emailed in step 1. The code itself is checked
    against the stored PasswordResetCode by the view, not here — this form only
    validates that it looks like a 6-digit code and that the new password is valid."""

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
    """Shared by SignupForm and AccountSettingsForm: an email must belong to at most one
    account (it's a login identifier -- accounts/auth_backends.py -- and two accounts
    sharing one would make that login ambiguous), and its domain must be able to
    receive mail (accounts.contact.email_domain_accepts_mail), which catches typos like
    "gmial.com" before they're saved."""
    already_taken = User.objects.filter(email__iexact=email).exclude(pk=exclude_pk).exists()
    if already_taken:
        raise forms.ValidationError(_("That email is already in use by another account."))
    if not email_domain_accepts_mail(email):
        raise forms.ValidationError(
            _("This email address doesn't seem to exist — check the spelling "
            "(e.g. gmail.com).")
        )


def _check_phone_available(phone, exclude_pk=None):
    """Same idea as _check_email_available, for the mobile number (also a login
    identifier). The DB unique constraint on User.phone_number is the real guarantee;
    this just turns a clash into a readable form error instead of an IntegrityError."""
    if User.objects.filter(phone_number=phone).exclude(pk=exclude_pk).exists():
        raise forms.ValidationError(_("That mobile number is already in use by another account."))


class SignupForm(UserCreationForm):
    """Self-service farmer signup.

    Asks for a full name, not a username: farmers forget usernames, but not their own
    name. save() generates the username behind the scenes
    (accounts.contact.generate_unique_username), and login accepts the full name, first
    name, email, or phone number instead (accounts/auth_backends.py). The full name is
    split into first_name (first word) and last_name (the rest); a farmer can fix the
    split later in Account Settings, and login matching doesn't depend on it.

    "Email or phone number" is ONE field (`contact`) that accepts either, sorted into
    User.email or User.phone_number by accounts.contact.split_contact. It may be left
    blank -- signup keeps its low barrier, and User.missing_profile_items lets the
    notification bell remind the farmer to add one later. An email is checked for
    uniqueness and that its domain can receive mail (_check_email_available); there is
    no emailed code at signup -- the 6-digit code is only used for password reset.

    privacy_consent is required: the app collects personal information (name,
    contact, location, farm records), so the Data Privacy Act of 2012 (RA 10173) needs
    the farmer's informed consent. accounts.views.signup stamps
    User.privacy_consented_at when it saves the account.

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
    signup — it just leaves latitude/longitude unset.
    """

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
        # Collapse repeated spaces so "Juan  Dela Cruz" is stored the way it'll be typed.
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
        # Stashed for _post_clean() -- `contact` itself isn't a model field.
        self.contact_email, self.contact_phone = email, phone
        return email or phone or ""

    def clean_address(self):
        return expand_barangay_address(self.cleaned_data["address"])

    def _post_clean(self):
        # Copy the non-model fields onto the instance *before* UserCreationForm's own
        # _post_clean runs its password validation, so UserAttributeSimilarityValidator
        # ("password too similar to your name/email") actually sees them.
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
    """Lets a logged-in farmer edit their own personal data from Account Settings
    (accounts/views.py's account_settings) -- name, username, email, mobile number,
    farm address, and profile picture. Reused unchanged as the "finish setting up your
    account" form a brand-new Google sign-in is redirected to (accounts/views.py's
    google_callback), since a Google account is created with no name/address at all.

    Email and mobile number are separate fields here (signup has one combined field),
    so a farmer who gave only one at signup can add the other later. Both run the same
    checks as signup (_check_email_available / _check_phone_available); neither is
    required, matching signup.

    address works the same "dropdown in, full string out" way as SignupForm above
    (see barangay_choice_field's docstring) -- __init__ additionally reverses that on
    the way *in*, via barangay_from_address, so editing an existing user shows their
    current barangay selected instead of the placeholder every time.

    username keeps the model's own unique=True validation for free (ModelForm already
    excludes `instance` from that check).
    """

    class Meta:
        model = User
        fields = [
            "avatar", "first_name", "last_name", "username", "email", "phone_number", "address",
        ]

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
            # Only re-check an email that actually changed -- an existing address
            # shouldn't start failing the DNS check just because the farmer edited
            # their name.
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
    """Confirms self-service account deletion (accounts/views.py's delete_account)
    before it proceeds. Some accounts have no local password at all -- a Google-only
    sign-in gets User.set_unusable_password() (see google_callback) -- so "confirm with
    your password" isn't always possible. This form picks the strongest confirmation
    the account actually supports: the current password when there is one, otherwise
    typing the username, rather than offering both as if either always worked.

    confirmation_mode is exposed for the template to label the single field correctly
    ("Current password" vs "Type your username to confirm").
    """

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
