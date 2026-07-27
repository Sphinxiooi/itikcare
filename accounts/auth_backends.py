"""Custom authentication backend letting a farmer log in with their username, email,
or full name -- accounts/forms.py's StyledAuthenticationForm still only has one
"username" field on the login form; whichever of the three the farmer types goes
through this same backend.

Full names aren't unique the way username/email are (two Libmanan farmers can share a
common name), so a full-name match is only ever trusted when it resolves to exactly one
account. An ambiguous full-name match is treated as "no match" here -- the same
generic failed-login outcome as a wrong password -- because StyledAuthenticationForm's
own clean() already runs the same lookup first and raises a specific "log in with your
username or email instead" error before authenticate() is ever called in that case.
"""

from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import CharField, Q, Value
from django.db.models.functions import Concat


def find_user_by_login_identifier(identifier):
    """Resolve `identifier` (as typed into the login form) to at most one User, or
    None if it matches zero users or ambiguously matches more than one by full name.
    Shared by this backend and StyledAuthenticationForm's ambiguity check so both run
    the exact same lookup.
    """
    UserModel = get_user_model()
    candidates = UserModel._default_manager.annotate(
        full_name=Concat("first_name", Value(" "), "last_name", output_field=CharField())
    )

    exact = candidates.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier))
    user = exact.first()
    if user is not None:
        return user

    # first_name/last_name default to "" (never null), so a blank identifier would
    # otherwise spuriously match every user who hasn't set a name yet.
    if not identifier.strip():
        return None
    name_matches = list(candidates.filter(full_name__iexact=identifier))
    if len(name_matches) == 1:
        return name_matches[0]
    return None


def is_ambiguous_login_identifier(identifier):
    """True when `identifier` doesn't uniquely match a username/email but matches more
    than one account by full name -- accounts/forms.py's StyledAuthenticationForm uses
    this to raise a specific error before authenticate() would otherwise just treat it
    as a generic failed login (see find_user_by_login_identifier's docstring)."""
    if not identifier or not identifier.strip():
        return False
    UserModel = get_user_model()
    exact_match = UserModel._default_manager.filter(
        Q(username__iexact=identifier) | Q(email__iexact=identifier)
    ).exists()
    if exact_match:
        return False
    name_match_count = UserModel._default_manager.annotate(
        full_name=Concat("first_name", Value(" "), "last_name", output_field=CharField())
    ).filter(full_name__iexact=identifier).count()
    return name_match_count > 1


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
