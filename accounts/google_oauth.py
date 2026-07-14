"""Google Sign-In (OAuth2 authorization code flow), hand-rolled with `requests` rather
than a library like django-allauth — same "talk to a third-party HTTP API with a plain
requests call, a timeout, and a try/except" pattern as farm/weather.py, kept small and
readable enough to walk through in a thesis defense.

Only two things ever happen here: build the URL that sends the farmer to Google's own
consent screen, and turn the authorization code Google hands back into that farmer's
Google account id/email. accounts/views.py owns everything after that (resolving/
creating the local User, logging them in) -- this module never touches the database.
"""

import logging
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
REQUEST_TIMEOUT_SECONDS = 5


def build_authorization_url(redirect_uri, state):
    """The URL to send the farmer's browser to so they can sign in with Google and
    approve sharing their basic profile (name/email) with this app.

    prompt=select_account forces Google's account chooser to show every time, instead
    of silently reusing whichever Google account is already signed in on this browser
    -- without it, a farm computer shared by multiple family members would keep signing
    everyone into whoever signed in with Google first.
    """
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return f"{AUTHORIZATION_URL}?{urlencode(params)}"


def fetch_google_account(code, redirect_uri):
    """Exchange the authorization code Google's callback handed us for that farmer's
    Google account id/email, or None on any failure (network error, Google rejected the
    code, malformed response). Never raises -- accounts.views.google_callback treats
    None as "something went wrong, send them back to the login page with a message,"
    the same "best effort" contract farm/weather.py's functions use.

    Returns {"sub": str, "email": str | None, "email_verified": bool, "name": str | None}.
    `sub` is Google's own stable per-account id (see User.google_sub's help_text) and is
    the only field guaranteed present.
    """
    try:
        token_response = requests.post(
            TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        userinfo_response = requests.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        userinfo_response.raise_for_status()
        userinfo = userinfo_response.json()
        sub = userinfo["sub"]
    except Exception:
        logger.warning("Google sign-in failed exchanging code for account info", exc_info=True)
        return None

    return {
        "sub": sub,
        "email": userinfo.get("email"),
        "email_verified": bool(userinfo.get("email_verified")),
        "name": userinfo.get("name"),
    }
