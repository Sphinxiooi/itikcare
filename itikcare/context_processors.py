from django.conf import settings


def static_files(request):
    """Tell templates whether static URLs are content-hashed (base.html adds a cache-buster only when they aren't)."""
    return {"use_hashed_static": settings.USE_HASHED_STATIC}
