from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import NotificationReceipt
from .services import get_notification_state


def _unread_count_response(user):
    _, unread_count = get_notification_state(user)
    return JsonResponse({"unread_count": unread_count})


@login_required
@require_POST
def mark_all_read(request):
    """Called when the bell's dropdown is opened: everything currently visible counts
    as seen. Only keys that currently apply are written, so a notification that shows
    up later (e.g. tomorrow's log reminder) still arrives unread."""
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
    """Hides one notification (by key) for this farmer. The key comes from the POST
    body rather than the URL since keys contain ':' and ',' characters."""
    key = request.POST.get("key", "").strip()
    if not key or len(key) > NotificationReceipt._meta.get_field("key").max_length:
        return JsonResponse({"error": "invalid key"}, status=400)
    now = timezone.now()
    NotificationReceipt.objects.update_or_create(
        user=request.user, key=key, defaults={"dismissed_at": now, "read_at": now}
    )
    return _unread_count_response(request.user)
