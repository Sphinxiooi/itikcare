from django.contrib.auth.forms import AuthenticationForm

INPUT_CLASSES = (
    "w-full rounded-md border border-gray-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-emerald-700 focus:border-emerald-700"
)


class StyledAuthenticationForm(AuthenticationForm):
    """AuthenticationForm with Tailwind classes on its widgets.

    Django's built-in LoginView doesn't add CSS classes to its fields, so this is a
    thin subclass rather than hand-rendering the whole form field by field.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": INPUT_CLASSES})
        self.fields["password"].widget.attrs.update({"class": INPUT_CLASSES})
