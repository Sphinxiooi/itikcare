"""Contact-detail helpers (email/phone normalizing and checks, username generation) shared by signup, settings and login."""

import re

import dns.exception
import dns.resolver
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

# Local 11-digit PH mobile format: "09" + 9 digits
PH_MOBILE_PATTERN = re.compile(r"^09\d{9}$")

# DNS lookup timeout (the farmer is waiting on the form)
DNS_TIMEOUT_SECONDS = 3


def normalize_ph_mobile(raw):
    """Turn a PH mobile number in any common format into 09XXXXXXXXX, or None."""
    if not raw:
        return None
    digits = re.sub(r"[\s\-()]", "", raw.strip())
    if digits.startswith("+63"):
        digits = "0" + digits[3:]
    elif digits.startswith("63") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits if PH_MOBILE_PATTERN.match(digits) else None


def email_domain_accepts_mail(email):
    """True if the email's domain can receive mail (MX or A record, not a null MX); True if DNS itself fails."""
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return False
    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    for record_type in ("MX", "A"):
        try:
            answer = resolver.resolve(domain, record_type)
            if record_type == "MX" and all(str(r.exchange) == "." for r in answer):
                # Null MX: the domain accepts no mail
                return False
            return True
        except dns.resolver.NXDOMAIN:
            # Domain doesn't exist
            return False
        except dns.resolver.NoAnswer:
            continue  # domain exists but has no record of this type; try the next
        except (dns.exception.Timeout, dns.resolver.NoNameservers):
            return True
    return False


def split_contact(raw):
    """Split the combined signup field into (email, phone); raises ValidationError if it is neither."""
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
    """Lowercase ASCII letters/digits/dots only ("Peñaredondo" -> "penaredondo")."""
    pieces = [slugify(piece).replace("-", "") for piece in text.split(".")]
    return ".".join(piece for piece in pieces if piece)


def generate_unique_username(base):
    """Make a free username from a name, adding a number suffix if taken ("juan.delacruz2")."""
    # Local import keeps this module importable from anywhere
    from .models import User

    parts = [_username_safe(part) for part in (base or "").split()]
    parts = [part for part in parts if part]
    if len(parts) >= 2:
        # First name, then the rest joined
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
