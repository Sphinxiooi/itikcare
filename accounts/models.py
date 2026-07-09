from django.contrib.auth.models import AbstractUser
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

    def save(self, *args, **kwargs):
        # Keep createsuperuser accounts consistent: an admin flag should imply the admin role.
        if self.is_superuser:
            self.role = self.Role.ADMIN
        super().save(*args, **kwargs)

    def __str__(self):
        return self.username
