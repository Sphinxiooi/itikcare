from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class SignupTests(TestCase):
    """Covers the farm-address question added to self-service signup — must stay
    optional, and an unresolvable address must never block account creation (see
    accounts/forms.py's SignupForm docstring)."""

    def _post(self, **overrides):
        data = {
            "username": "newfarmer",
            "password1": "a-strong-passw0rd",
            "password2": "a-strong-passw0rd",
        }
        data.update(overrides)
        return self.client.post("/accounts/signup/", data)

    @patch("accounts.views.geocode_address", return_value=(13.5, 123.2))
    def test_signup_with_resolvable_address_saves_latitude_and_longitude(self, mock_geocode):
        self._post(address="Libmanan, Camarines Sur")
        user = User.objects.get(username="newfarmer")
        mock_geocode.assert_called_once_with("Libmanan, Camarines Sur")
        self.assertEqual(user.address, "Libmanan, Camarines Sur")
        self.assertAlmostEqual(float(user.latitude), 13.5)
        self.assertAlmostEqual(float(user.longitude), 123.2)

    @patch("accounts.views.geocode_address", return_value=None)
    def test_signup_with_unresolvable_address_succeeds_with_null_coordinates(self, mock_geocode):
        response = self._post(address="Nowhere Land")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        user = User.objects.get(username="newfarmer")
        self.assertEqual(user.address, "Nowhere Land")
        self.assertIsNone(user.latitude)
        self.assertIsNone(user.longitude)

    @patch("accounts.views.geocode_address")
    def test_signup_without_address_succeeds_with_null_coordinates(self, mock_geocode):
        response = self._post()
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        mock_geocode.assert_not_called()
        user = User.objects.get(username="newfarmer")
        self.assertEqual(user.address, "")
        self.assertIsNone(user.latitude)
        self.assertIsNone(user.longitude)
