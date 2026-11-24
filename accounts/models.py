from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

AVATAR_MAX_SIZE_BYTES = 5 * 1024 * 1024


def validate_avatar_size(file):
    if file.size > AVATAR_MAX_SIZE_BYTES:
        raise ValidationError(_("Image must be 5MB or smaller."))


class User(AbstractUser):
    """Custom user model with a farm role (must be AUTH_USER_MODEL from the first migration)."""

    class Role(models.TextChoices):
        FARMER = "farmer", "Farmer"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.FARMER)
    is_foundation_farmer = models.BooleanField(
        default=False,
        help_text="The one farmer whose historical DailyLog data seeds every new "
        "farmer's bootstrap forecasting model (see forecasting/management/commands/"
        "train_forecast_model.py). At most one user may ever have this set — enforced "
        "in save() below, not a DB constraint. Left over from when MySQL was the "
        "target (it doesn't support conditional/partial unique constraints, Django "
        "system check W036); Postgres does, so a partial UniqueConstraint here "
        "(like unique_generation_per_owner on Flock) is a viable follow-up, not "
        "attempted yet.",
    )
    address = models.CharField(
        max_length=255,
        blank=True,
        help_text="Farm address as typed by the farmer at signup, geocoded into "
        "latitude/longitude below (see accounts.views.signup and "
        "farm.weather.geocode_address). Kept even if geocoding fails so the farmer's "
        "input isn't silently lost.",
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
        help_text="Farm's GPS latitude, geocoded from `address` at signup. Optional: "
        "used to personalize weather-based prefill in farm/weather.py — if unset, "
        "weather fetching falls back to the global FARM_LATITUDE/FARM_LONGITUDE "
        "settings (the foundation farmer's location).",
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
        help_text="Farm's GPS longitude — see latitude's help_text.",
    )
    google_sub = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text="Google's stable per-account ID ('sub' claim), set the first time this "
        "user signs in with Google (see accounts/google_oauth.py, accounts/views.py's "
        "google_callback). Stable for the life of the Google account even if its email "
        "changes later, unlike matching on email alone. null=True (not blank default) so "
        "MySQL's unique index only enforces uniqueness among accounts that actually have "
        "one linked -- most accounts never will.",
    )
    avatar = models.ImageField(
        upload_to="avatars/",
        blank=True,
        null=True,
        validators=[validate_avatar_size],
        help_text="Optional profile picture, set by the farmer from Account Settings "
        "(accounts/views.py's account_settings). Falls back to an initial-letter avatar "
        "in the UI (templates/base.html) when unset.",
    )

    phone_number = models.CharField(
        max_length=11,
        unique=True,
        null=True,
        blank=True,
        validators=[
            RegexValidator(r"^09\d{9}$", "Enter an 11-digit mobile number (09XXXXXXXXX).")
        ],
        help_text=("Optional PH mobile number in 11-digit local form (09XXXXXXXXX), "
        "normalized by accounts/contact.py's normalize_ph_mobile. Login-only: farmers "
        "can type it into the login form instead of their name or email "
        "(accounts/auth_backends.py). No SMS is ever sent to it. null (not \"\") when "
        "unset so the unique index only covers accounts that actually entered one -- "
        "same pattern as google_sub."),
    )
    privacy_consented_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the farmer agreed to the Privacy Notice (templates/privacy.html) "
        "-- the signup consent checkbox, or creating an account via Google sign-in. "
        "Required under the Data Privacy Act of 2012 (RA 10173) since the app collects "
        "personal information. Null for accounts created before the notice existed.",
    )

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    @property
    def missing_profile_items(self):
        """Profile details the farmer hasn't filled in yet, as short keys for reminders."""
        missing = []
        if not self.email and not self.phone_number:
            missing.append("email_or_phone")
        elif not self.email:
            missing.append("email")
        if not self.first_name:
            missing.append("full_name")
        if not self.address:
            missing.append("address")
        if self.privacy_consented_at is None:
            missing.append("privacy_consent")
        return missing

    def save(self, *args, **kwargs):
        # Superusers always get the admin role
        if self.is_superuser:
            self.role = self.Role.ADMIN
        if self.is_foundation_farmer:
            already_exists = (
                User.objects.filter(is_foundation_farmer=True).exclude(pk=self.pk).exists()
            )
            if already_exists:
                raise ValueError(
                    "Another user is already the foundation farmer — at most one is allowed."
                )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.username

    @classmethod
    def get_foundation_farmer(cls) -> "User":
        return cls.objects.get(is_foundation_farmer=True)


class PasswordResetCode(models.Model):
    """One-time 6-digit password-reset code (stored hashed, wrong guesses are capped in the view)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="password_reset_codes"
    )
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
