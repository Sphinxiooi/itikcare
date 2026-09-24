"""Custom authentication backend letting a farmer log in with whatever they remember:
their full name, just their first name, their email, their mobile number, or (for
older accounts) their username. accounts/forms.py's StyledAuthenticationForm still has
one "username" field on the login form; whichever of these the farmer types goes
through this same backend. Every match is case-insensitive.

Names aren't unique the way username/email/phone are (two Libmanan farmers can share
a common name, and many more share a first name), so a name match is only ever trusted
when it resolves to exactly one account. An ambiguous name match is treated as "no
match" here -- the same generic failed-login outcome as a wrong password -- because
StyledAuthenticationForm's own clean() already runs the same lookup first and raises a
specific "log in with your full name, email, or phone number instead" error before
authenticate() is ever called in that case.

The class keeps its original name (UsernameEmailOrFullNameBackend) on purpose: Django
stores the backend's dotted path in every logged-in session, so renaming it would log
out every farmer on the deployed site.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import CharField, Q, Value
from django.db.models.functions import Concat

from .contact import normalize_ph_mobile


def _clean_identifier(identifier):
    """Trim and collapse repeated spaces, so "  Juan   Dela Cruz " still matches."""
    return " ".join((identifier or "").split())


def _unique_matches(identifier):
    """Look up accounts that match `identifier` by something unique -- username, email,
    or mobile number. Returns the single match, or None. Any of these can only ever
    match one account, so no ambiguity handling is needed at this step."""
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
    """Every account whose name matches `identifier`, tried in two steps:

    1. Full name ("Juan Dela Cruz"), case-insensitive.
    2. Only if step 1 found nobody: first name alone ("juan"). Also matches the start of
       the full name up to a space, so "Maria" finds "Maria Clara Santos" whether signup
       stored her first name as "Maria" or "Maria Clara" (see SignupForm's full_name).

    Returns a list -- the caller decides what to do with zero, one, or several.
    """
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
    """Resolve `identifier` (as typed into the login form) to at most one User, or
    None if it matches nobody, or matches more than one account by name.
    Shared by this backend, StyledAuthenticationForm's ambiguity check, and the
    password-reset lookup (accounts/views.py's request_reset_code), so all three
    resolve a farmer the exact same way.
    """
    identifier = _clean_identifier(identifier)
    # first_name/last_name default to "" (never null), so a blank identifier would
    # otherwise spuriously match every user who hasn't set a name yet.
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
    """True when `identifier` doesn't match a username/email/phone but matches more than
    one account by name -- accounts/forms.py's StyledAuthenticationForm uses this to
    raise a specific error before authenticate() would otherwise just treat it as a
    generic failed login (see find_user_by_login_identifier's docstring)."""
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
