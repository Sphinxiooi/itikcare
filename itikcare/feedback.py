"""Helper for success messages that tell the farmer what happens next.

A plain `messages.success(request, "Saved.")` renders as a success card with just a
title. `success_with_next()` adds a short "What happens next" list under it. The
list travels inside the single message string (first line = title, remaining
lines = steps) so it works with any Django messages storage; the template filter
`message_lines` (accounts/templatetags/itik_feedback.py) splits it back apart.
"""
from django.contrib import messages


def success_with_next(request, title, next_steps=()):
    """Queue a success card: `title`, then one bullet per entry in `next_steps`."""
    steps = [str(step).strip() for step in next_steps if str(step).strip()]
    messages.success(request, "\n".join([str(title), *steps]))
