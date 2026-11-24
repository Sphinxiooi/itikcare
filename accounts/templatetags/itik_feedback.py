from django import template

register = template.Library()


@register.filter
def message_lines(value):
    """Split a message into lines: [title, step, step, ...] (see itikcare/feedback.py)."""
    return [line for line in str(value).split("\n") if line.strip()]
