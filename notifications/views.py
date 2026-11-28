from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from farm.services import operational_today

from .models import NotificationReceipt, PushSubscription
from .push import is_allowed_endpoint, missing_log_payload, push_enabled, send_push
from .services import get_notification_state


def _unread_count_response(user):
    _, unread_count = get_notification_state(user)
    return JsonResponse({"unread_count": unread_count})


@login_required
@require_POST
def mark_all_read(request):
    """Mark everything currently visible as read (called when the bell opens)."""
    visible, _ = get_notification_state(request.user)
    now = timezone.now()
    for notification in visible:
        if notification.is_unread:
            NotificationReceipt.objects.update_or_create(
                user=request.user, key=notification.key, defaults={"read_at": now}
            )
    return JsonResponse({"unread_count": 0})


@login_required
@require_POST
def dismiss(request):
    """Hide one notification (key from the POST body) for this farmer."""
    key = request.POST.get("key", "").strip()
    if not key or len(key) > NotificationReceipt._meta.get_field("key").max_length:
        return JsonResponse({"error": "invalid key"}, status=400)
    now = timezone.now()
    NotificationReceipt.objects.update_or_create(
        user=request.user, key=key, defaults={"dismissed_at": now, "read_at": now}
    )
    return _unread_count_response(request.user)


# ---------- phone notifications (Web Push) ----------
def _own_subscription(request):
    """The logged-in farmer's saved subscription for the endpoint in the POST body, or None."""
    endpoint = request.POST.get("endpoint", "")
    return PushSubscription.objects.filter(user=request.user, endpoint=endpoint).first()


@login_required
@require_POST
def push_status(request):
    """Is this phone turned on for this farmer? (A shared phone may belong to someone else.)"""
    return JsonResponse({"subscribed": _own_subscription(request) is not None})


@login_required
@require_POST
def push_subscribe(request):
    """Save the browser's push subscription (the settings switch turned on)."""
    if not push_enabled():
        return JsonResponse({"error": "push not configured"}, status=503)
    values = {name: request.POST.get(name, "").strip() for name in ("endpoint", "p256dh", "auth")}
    too_long = any(
        len(value) > PushSubscription._meta.get_field(name).max_length for name, value in values.items()
    )
    if not all(values.values()) or too_long or not is_allowed_endpoint(values["endpoint"]):
        return JsonResponse({"error": "invalid subscription"}, status=400)
    endpoint = values.pop("endpoint")
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "language": request.LANGUAGE_CODE, **values},
    )
    return JsonResponse({"subscribed": True})


@login_required
@require_POST
def push_unsubscribe(request):
    """Forget this phone (the settings switch turned off)."""
    subscription = _own_subscription(request)
    if subscription is not None:
        subscription.delete()
    return JsonResponse({"subscribed": False})


@login_required
@require_POST
@ratelimit(key="user", rate="5/m", block=True)
def push_test(request):
    """Send a sample reminder to this phone only."""
    subscription = _own_subscription(request)
    if subscription is None or not push_enabled():
        return JsonResponse({"error": "not subscribed"}, status=400)
    payload = missing_log_payload(subscription.language, operational_today())
    payload["title"] = f"{_('Test')}: {payload['title']}"
    payload["tag"] = "itikcare-test"
    if not send_push(subscription, payload):
        return JsonResponse({"error": "send failed"}, status=502)
    return JsonResponse({"sent": True})


# ---------- installable app (home-screen icon) ----------
@require_GET
def service_worker(request):
    """Served from the site root so the worker covers every page."""
    context = {"fallback_url": reverse("log_daily_data")}
    response = render(request, "notifications/sw.js", context, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def web_manifest(request):
    """Describes ItikCare to the browser so it can be installed with its own icon."""
    icons = [
        {"src": static(f"images/icon-{size}.png"), "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
        for size, purpose in ((192, "any"), (512, "any"), (512, "maskable"))
    ]
    manifest = {
        "id": "/",
        "name": "ItikCare",
        "short_name": "ItikCare",
        "description": _("Egg Yield Forecasting"),
        "start_url": reverse("dashboard"),
        "scope": "/",
        # Opens full screen like an app; "browser" would open an ordinary browser tab instead
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#047857",
        "icons": icons,
    }
    return JsonResponse(manifest, content_type="application/manifest+json")
