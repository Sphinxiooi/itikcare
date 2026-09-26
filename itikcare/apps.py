from django.contrib.staticfiles.apps import StaticFilesConfig


class ItikCareStaticFilesConfig(StaticFilesConfig):
    """django.contrib.staticfiles, but `collectstatic` also skips static/src/.

    static/src/input.css is the Tailwind *source* file, compiled into static/dist/output.css
    (see README.md) -- it's never linked from a page, so there's no reason to publish it.
    It also can't be published with content-hashed filenames (settings.USE_HASHED_STATIC):
    that storage rewrites every url()/@import in a CSS file to its hashed name, and
    input.css's `@import "tailwindcss"` names the Tailwind package, not a real file, so
    collectstatic would fail -- and on Railway, a failed collectstatic means gunicorn never
    starts. This is Django's documented way to extend collectstatic's ignore list.
    """

    ignore_patterns = [*StaticFilesConfig.ignore_patterns, "src"]
