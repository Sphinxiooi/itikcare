"""Print a new VAPID key pair for phone notifications, ready to paste into .env or the host's variables.

Run once per deployment. Changing the pair later means every phone must turn notifications on again.

Usage: python manage.py generate_vapid_keys
"""

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid02, b64urlencode


def new_key_pair():
    """(public key, private key) as the URL-safe base64 strings the browser and pywebpush expect."""
    vapid = Vapid02()
    vapid.generate_keys()
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return b64urlencode(public), b64urlencode(private)


class Command(BaseCommand):
    help = "Print a new VAPID key pair for phone notifications."

    def handle(self, *args, **options):
        public, private = new_key_pair()
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
