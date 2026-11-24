"""Tiny JSON endpoints behind the "your session is about to expire" flow.

static/js/itik-feedback.js polls `session_status` so it can (a) warn a farmer a few
minutes before the session ends, (b) save their half-typed form to the device and
show a friendly "log in again" dialog the moment it has ended, instead of letting a
form submit bounce them to the login page with everything they typed gone.

Neither endpoint redirects: an expired session answers with a normal JSON body
(`authenticated: false`), because the caller is a script, not a browser navigation.
"""
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST


def _no_session_response():
    # 200 on purpose for status; the extend endpoint overrides with 401 below.
    return JsonResponse({"authenticated": False, "seconds_left": 0})


@never_cache
@require_GET
def session_status(request):
    """Report whether the session is still valid and how long it has left.

    Read-only: it never extends the session, so polling can't keep an idle
    farmer signed in forever.
    """
    if not request.user.is_authenticated:
        return _no_session_response()
    return JsonResponse(
        {"authenticated": True, "seconds_left": int(request.session.get_expiry_age())}
    )


@never_cache
@require_POST
def session_extend(request):
    """"Stay signed in": restart the session clock from now (CSRF-protected POST)."""
    if not request.user.is_authenticated:
        response = _no_session_response()
        response.status_code = 401
        return response
    request.session.set_expiry(settings.SESSION_COOKIE_AGE)
    return JsonResponse(
        {"authenticated": True, "seconds_left": int(request.session.get_expiry_age())}
    )
