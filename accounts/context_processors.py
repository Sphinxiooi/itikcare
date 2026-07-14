from django.conf import settings


def google_oauth_enabled(request):
    """Lets templates conditionally show the "Sign in with Google" button (see
    templates/registration/login.html, signup.html) without every view that renders
    those templates remembering to pass this in its own context."""
    return {"google_oauth_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID)}
