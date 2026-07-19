from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class User(AbstractUser):
    """Custom user model so we can attach a farm role without a separate profile table.

    Must stay wired as AUTH_USER_MODEL from this project's very first migration —
    switching the user model after tables exist is not a supported Django operation.
    """

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

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def save(self, *args, **kwargs):
        # Keep createsuperuser accounts consistent: an admin flag should imply the admin role.
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
