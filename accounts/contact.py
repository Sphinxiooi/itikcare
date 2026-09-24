"""Helpers for a farmer's contact details: the single "Email or phone number" signup
field (accounts/forms.py's SignupForm), the separate email/mobile fields in Account
Settings, and the login lookup in accounts/auth_backends.py.

Kept in one module so every place that reads a phone number or an email normalizes it
the exact same way -- a number typed as "0917 123 4567" at signup must still match
"+639171234567" typed at login.
"""

import re

import dns.exception
import dns.resolver
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

# Philippine mobile numbers in their local 11-digit form: "09" + 9 digits.
PH_MOBILE_PATTERN = re.compile(r"^09\d{9}$")

# How long the email-domain DNS lookup may take before we give up and let the email
# through (see email_domain_accepts_mail). Short, because the farmer is waiting on the
# signup form while it runs.
DNS_TIMEOUT_SECONDS = 3


def normalize_ph_mobile(raw):
    """Turn a PH mobile number typed in any common format into its 11-digit local form
    ("09XXXXXXXXX"), or None if it isn't one.

    Accepted: "09171234567", "0917 123 4567", "0917-123-4567", "+639171234567",
    "639171234567". Anything else (wrong length, landline, letters) returns None.
    """
    if not raw:
        return None
    digits = re.sub(r"[\s\-()]", "", raw.strip())
    if digits.startswith("+63"):
        digits = "0" + digits[3:]
    elif digits.startswith("63") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits if PH_MOBILE_PATTERN.match(digits) else None


def email_domain_accepts_mail(email):
    """True if the email's domain is set up to receive mail (has an MX record, or at
    least an A record -- the RFC 5321 fallback when a domain has no MX -- and doesn't
    publish a "null MX" saying it accepts no mail).

    This is the "does this email exist?" check at signup. It can't prove the mailbox
    itself exists (no mail server answers that reliably without actually sending a
    message), but it does catch the most common farmer mistakes on the spot: a
    misspelled domain like "gmial.com" or "yahoo.con", or a made-up one.

    If DNS itself fails (timeout, no network), this returns True rather than blocking
    signup -- an outage on our side must never stop a farmer from registering.
    """
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return False
    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    for record_type in ("MX", "A"):
        try:
            answer = resolver.resolve(domain, record_type)
            if record_type == "MX" and all(str(r.exchange) == "." for r in answer):
                # A "null MX" (RFC 7505) is the domain explicitly saying it accepts no
                # mail at all -- e.g. example.com.
                return False
            return True
        except dns.resolver.NXDOMAIN:
            # The domain doesn't exist at all -- no point checking the A record.
            return False
        except dns.resolver.NoAnswer:
            continue  # domain exists but has no record of this type; try the next
        except (dns.exception.Timeout, dns.resolver.NoNameservers):
            return True
    return False


def split_contact(raw):
    """Split the single "Email or phone number" signup field into (email, phone).

    Returns ("", None) for a blank field -- signup allows leaving it empty (the
    notification bell reminds the farmer later, see User.missing_profile_items).
    Raises ValidationError when the input is neither a valid email nor a PH mobile
    number. Uniqueness and the domain check are the form's job, not this function's.
    """
    value = (raw or "").strip()
    if not value:
        return "", None

    phone = normalize_ph_mobile(value)
    if phone:
        return "", phone

    if "@" in value:
        try:
            validate_email(value)
        except ValidationError:
            raise ValidationError(_("Enter a valid email address."))
        return value.lower(), None

    raise ValidationError(
        _("Enter a valid email or an 11-digit mobile number (09XXXXXXXXX).")
    )


def _username_safe(text):
    """Lowercase ASCII letters/digits/dots only: "Peñaredondo" -> "penaredondo",
    "juan.delacruz" (a Google email's local part) keeps its dot."""
    pieces = [slugify(piece).replace("-", "") for piece in text.split(".")]
    return ".".join(piece for piece in pieces if piece)


def generate_unique_username(base):
    """Turn a name (or an email's local part) into a free username, appending a
    numeric suffix if it's already taken -- e.g. "Juan Dela Cruz" -> "juan.delacruz",
    then "juan.delacruz2" for the next Juan Dela Cruz.

    Farmers never have to remember this username: signup asks for a full name instead,
    and login accepts name, email, or phone (accounts/auth_backends.py). It exists only
    because Django's User model needs a unique username.
    """
    # Local import: accounts.models imports nothing from here, but keeping the model
    # import out of module scope lets this module stay importable from anywhere.
    from .models import User

    parts = [_username_safe(part) for part in (base or "").split()]
    parts = [part for part in parts if part]
    if len(parts) >= 2:
        # "Juan Dela Cruz" -> "juan.delacruz": first name, then the rest joined.
        username = f"{parts[0]}.{''.join(parts[1:])}"
    elif parts:
        username = parts[0]
    else:
        username = "farmer"
    username = username[:140]  # leave room for a suffix under the 150-char limit

    candidate = username
    suffix = 1
    while User.objects.filter(username__iexact=candidate).exists():
        suffix += 1
        candidate = f"{username}{suffix}"
    return candidate
