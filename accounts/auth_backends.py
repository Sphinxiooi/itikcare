"""Login by full name, first name, email, mobile number or username (case-insensitive).

A name match is trusted only when it resolves to exactly one account. The backend class keeps
its original name because Django stores its dotted path in every session.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import CharField, Q, Value
from django.db.models.functions import Concat

from .contact import normalize_ph_mobile


def _clean_identifier(identifier):
    """Trim and collapse repeated spaces."""
    return " ".join((identifier or "").split())


def _unique_matches(identifier):
    """Match by a unique field (username, email or mobile number); returns the user or None."""
    UserModel = get_user_model()
    user = UserModel._default_manager.filter(
        Q(username__iexact=identifier) | Q(email__iexact=identifier)
    ).first()
    if user is not None:
        return user

    phone = normalize_ph_mobile(identifier)
    if phone:
        return UserModel._default_manager.filter(phone_number=phone).first()
    return None


def _name_matches(identifier):
    """Accounts matching the identifier by full name, else by first name; returns a list."""
    UserModel = get_user_model()
    candidates = UserModel._default_manager.annotate(
        full_name=Concat("first_name", Value(" "), "last_name", output_field=CharField())
    )

    full_name_matches = list(candidates.filter(full_name__iexact=identifier))
    if full_name_matches:
        return full_name_matches

    return list(
        candidates.filter(
            Q(first_name__iexact=identifier) | Q(full_name__istartswith=identifier + " ")
        )
    )


def find_user_by_login_identifier(identifier):
    """Resolve a login identifier to one User, or None (no match or ambiguous name)."""
    identifier = _clean_identifier(identifier)
    # A blank identifier would match every user without a name
    if not identifier:
        return None

    user = _unique_matches(identifier)
    if user is not None:
        return user

    name_matches = _name_matches(identifier)
    if len(name_matches) == 1:
        return name_matches[0]
    return None


def is_ambiguous_login_identifier(identifier):
    """True if the identifier matches several accounts by name only."""
    identifier = _clean_identifier(identifier)
    if not identifier:
        return False
    if _unique_matches(identifier) is not None:
        return False
    return len(_name_matches(identifier)) > 1


class UsernameEmailOrFullNameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        user = find_user_by_login_identifier(username)
        if user is None:
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
