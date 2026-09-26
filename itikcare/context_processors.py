from django.conf import settings


def static_files(request):
    """Tells templates whether {% static %} URLs are content-hashed (see
    settings.USE_HASHED_STATIC). base.html only appends its "?v=<timestamp>" cache-buster
    when they aren't -- that buster makes the browser re-download the stylesheet on every
    page view, which is only worth paying in local dev (so a fresh Tailwind build shows
    up immediately). A hashed filename already changes whenever the file does."""
    return {"use_hashed_static": settings.USE_HASHED_STATIC}
