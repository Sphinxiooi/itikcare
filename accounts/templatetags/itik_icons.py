"""Hand-inlined SVG icon set used across the app in place of emoji.

Drawn in the same 24x24 outline-stroke style already used for the sidebar
icons in templates/base.html (fill="none", stroke="currentColor",
stroke-width="2") -- visually the same family as shadcn/ui's default
(Lucide) icons, without pulling in an external icon library.

Usage in a template: {% load itik_icons %} then {% icon "wheat" %} or
{% icon "wheat" "w-4 h-4 text-white" %} to override the default size/color
classes. The icon name can also be a variable, e.g. {% icon block.meta.icon %}.
"""

from django.template import Library
from django.utils.html import format_html, mark_safe

register = Library()

# Each entry is the inner content of an <svg viewBox="0 0 24 24"> shell (see
# `icon` below) -- just the <path>/<circle>/<rect> shapes, no wrapping <svg>.
ICONS = {
    # Two overlapping figures -- generic "count of animals/flock" mark, used
    # for the Flock Size stat. (The brand logo is the raster
    # static/images/logo-mark.png, not an icon from this table.)
    "flock": (
        '<circle cx="8" cy="10" r="3"/><circle cx="16" cy="10" r="3"/>'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M3.5 19c.7-2.8 2.5-4.5 4.5-4.5s3.8 1.7 4.5 4.5M11.5 19c.7-2.8 '
        '2.5-4.5 4.5-4.5s3.8 1.7 4.5 4.5"/>'
    ),
    "map-pin": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M12 21s7-7.58 7-12a7 7 0 1 0-14 0c0 4.42 7 12 7 12Z"/>'
        '<circle cx="12" cy="9" r="2.5"/>'
    ),
    "calendar": (
        '<rect x="3.75" y="5.25" width="16.5" height="15" rx="2"/>'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M3.75 9.75h16.5M8 3v3.5M16 3v3.5"/>'
    ),
    "wheat": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M12 21V9M12 9c0-2 1.5-3 3-3M12 9c0-2-1.5-3-3-3M12 13c0-2 1.8-3.2 '
        '3.5-3.2M12 13c0-2-1.8-3.2-3.5-3.2M12 17c0-1.7 1.5-2.8 3-2.8M12 '
        '17c0-1.7-1.5-2.8-3-2.8"/><circle cx="12" cy="5.5" r="1.5"/>'
    ),
    "droplet": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M12 3.5s6 6.34 6 10.5a6 6 0 1 1-12 0c0-4.16 6-10.5 6-10.5Z"/>'
    ),
    "home": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M4 10.5 12 4l8 6.5M6 9.5V19a1 1 0 0 0 1 1h3.5v-5a1.5 1.5 0 0 1 '
        '1.5-1.5 1.5 1.5 0 0 1 1.5 1.5v5H17a1 1 0 0 0 1-1V9.5"/>'
    ),
    "sparkle": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 '
        '2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/>'
    ),
    "egg": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M12 21c4 0 6.5-3.5 6.5-8C18.5 8 15.5 3 12 3S5.5 8 5.5 13c0 4.5 '
        '2.5 8 6.5 8Z"/>'
    ),
    "trending-up": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="m3.75 16.5 5.69-5.69a1.5 1.5 0 0 1 2.12 0l2.38 2.38a1.5 1.5 0 0 '
        '0 2.12 0L20.25 9M20.25 9h-4.5M20.25 9v4.5"/>'
    ),
    "basket": (
        '<path stroke-linecap="round" stroke-linejoin="round" d="M8 10a4 4 '
        '0 0 1 8 0"/>'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M4 10h16l-1.5 9.5a1.5 1.5 0 0 1-1.48 1.25H6.98A1.5 1.5 0 0 1 5.5 '
        '19.5L4 10Z"/>'
    ),
    "thermometer": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M10 14.5V5.25a2 2 0 1 1 4 0V14.5"/><circle cx="12" cy="17" r="3"/>'
    ),
    "cloud": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M7 18a4 4 0 1 1 .8-7.92 5 5 0 0 1 9.6 1.67A3.5 3.5 0 0 1 17 '
        '18H7Z"/>'
    ),
    "wrench": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M14.5 6.5a4 4 0 0 0-5.4 4.9L4 16.5 7.5 20l5.1-5.1a4 4 0 0 0 '
        '4.9-5.4l-2.6 2.6-2-2 2.6-2.6Z"/>'
    ),
    "tag": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M11.5 3.75H6A2.25 2.25 0 0 0 3.75 6v5.5c0 .6.24 1.17.66 '
        '1.59l8.5 8.5a2.25 2.25 0 0 0 3.18 0l5.5-5.5a2.25 2.25 0 0 0 '
        '0-3.18l-8.5-8.5a2.25 2.25 0 0 0-1.59-.66Z"/>'
        '<circle cx="8.25" cy="8.25" r="1.25" fill="currentColor" stroke="none"/>'
    ),
    "lightbulb": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M9 18h6M10 21h4M8.5 14.5A5.25 5.25 0 1 1 15.5 14.5c-.7.85-1.5 '
        '1.55-1.5 2.75h-4c0-1.2-.8-1.9-1.5-2.75Z"/>'
    ),
    "check-circle": (
        '<circle cx="12" cy="12" r="8.25"/>'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="m8.5 12.5 2.5 2.5 5-5.5"/>'
    ),
    "info-circle": (
        '<circle cx="12" cy="12" r="8.25"/>'
        '<path stroke-linecap="round" stroke-linejoin="round" d="M12 11v5.25"/>'
        '<circle cx="12" cy="8" r="0.75" fill="currentColor" stroke="none"/>'
    ),
    "clock": (
        '<circle cx="12" cy="12" r="8.25"/>'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M12 7.5V12l3 2"/>'
    ),
    # Header notification bell (templates/includes/notification_bell.html).
    "bell": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M14.857 17.082a23.848 23.848 0 0 0 5.454-1.31A8.967 8.967 0 0 1 18 '
        '9.75V9A6 6 0 0 0 6 9v.75a8.967 8.967 0 0 1-2.312 6.022c1.733.64 3.56 '
        '1.085 5.455 1.31m5.714 0a24.255 24.255 0 0 1-5.714 0m5.714 0a3 3 0 1 '
        '1-5.714 0"/>'
    ),
    "x-mark": (
        '<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12"/>'
    ),
}


@register.simple_tag(name="icon")
def icon(name, css_class="w-5 h-5"):
    """Render one of the ICONS above as an inline <svg>.

    `name` may be a literal icon key or a template variable holding one
    (e.g. the category icon keys stored in forecasting.views). Unknown
    names render nothing rather than raising, so a stale/mistyped key
    degrades quietly instead of 500ing a page.
    """
    inner = ICONS.get(name)
    if inner is None:
        return ""
    return format_html(
        '<svg class="{}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" aria-hidden="true">{}</svg>',
        css_class,
        mark_safe(inner),
    )
