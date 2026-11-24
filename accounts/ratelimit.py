"""Client-IP lookup for django-ratelimit behind a reverse proxy (set via RATELIMIT_IP_META_KEY)."""


def client_ip(request):
    """Real client address: the last X-Forwarded-For entry (added by the trusted proxy), else REMOTE_ADDR."""
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        last_hop = forwarded_for.split(",")[-1].strip()
        if last_hop:
            return last_hop
    return request.META["REMOTE_ADDR"]
