"""Google Sign-In (OAuth2 authorization code flow) using plain `requests`.

Builds the Google consent URL and exchanges the returned code for the account's id/email.
Never touches the database (accounts/views.py handles users and login).
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
    """URL of Google's consent screen (prompt=select_account always shows the account chooser)."""
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
    """Exchange the code for {"sub", "email", "email_verified", "name"}, or None on any failure (never raises)."""
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
