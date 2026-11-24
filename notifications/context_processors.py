from functools import cached_property

from .services import get_notification_state


class LazyNotificationState:
    """Runs the notification queries only when a template reads them."""

    def __init__(self, user):
        self._user = user

    @cached_property
    def _state(self):
        return get_notification_state(self._user)

    @property
    def items(self):
        return self._state[0]

    @property
    def unread_count(self):
        return self._state[1]


def notifications(request):
    """Expose `notification_state` to the bell template."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"notification_state": LazyNotificationState(user)}
