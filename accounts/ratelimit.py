"""Client-IP lookup for django-ratelimit when the app sits behind a reverse proxy.

Wired in through settings.RATELIMIT_IP_META_KEY (only when DJANGO_BEHIND_PROXY=True), so
every existing ``@ratelimit(key="ip", ...)`` in accounts/views.py keeps working unchanged.
"""


def client_ip(request):
    """The real client address, as reported by the trusted proxy in front of the app.

    The proxy (Railway's edge, nginx) appends the address it actually saw to
    X-Forwarded-For. Only that last entry is trustworthy: anything to its left was
    supplied by the client (or an earlier hop) and can be spoofed to dodge a rate limit.
    Falls back to REMOTE_ADDR when the header is absent (e.g. a direct connection).
    """
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        last_hop = forwarded_for.split(",")[-1].strip()
        if last_hop:
            return last_hop
    return request.META["REMOTE_ADDR"]
