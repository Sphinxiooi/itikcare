from functools import cached_property

from .services import get_notification_state


class LazyNotificationState:
    """Defers the notification queries until a template actually reads them.

    Context processors run for every rendered template, including ones that never
    show the header bell (the landing page, ?partial=1 fragments fetched into
    modals) -- those pay nothing, since nothing here runs until .items or
    .unread_count is first accessed.
    """

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
    """Exposes `notification_state` to templates/includes/notification_bell.html."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"notification_state": LazyNotificationState(user)}
