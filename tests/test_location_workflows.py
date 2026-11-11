import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from geo import Coordinates, RoadRoute


_test_storage = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_test_storage.name, "import.db")
os.environ["SESSION_SECRET"] = "test-session-secret-with-more-than-32-characters"
os.environ.pop("RAILWAY_ENVIRONMENT", None)

import app as app_module


def future_time(hours):
    return (datetime.now() + timedelta(hours=hours)).isoformat(timespec="minutes")


class LocationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(
            self.temporary_directory.name, "mealsmatch.db"
        )
        os.environ["DATABASE_PATH"] = self.database_path
        with app_module.app.app_context():
            from database import initialize_database

            initialize_database()
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.donor = app_module.app.test_client()
        self.recipient = app_module.app.test_client()
        self._register(self.donor, "donor", "donor@example.invalid")
        self._register(self.recipient, "recipient", "recipient@example.invalid")

    def tearDown(self):
        self.donor = None
        self.recipient = None
        self.temporary_directory.cleanup()

    def test_google_callback_uses_https_origin_on_railway(self):
        class FakeGoogleClient:
            callback_url = None

            def authorize_redirect(self, callback_url):
                self.callback_url = callback_url
                return app_module.redirect("/login")

        fake_google = FakeGoogleClient()
        with patch.object(app_module, "google_client", fake_google):
            with patch.dict(
                os.environ,
                {"RAILWAY_ENVIRONMENT": "production", "PUBLIC_BASE_URL": ""},
            ):
                response = self.donor.get(
                    "/auth/google",
                    base_url="http://cs-ia-website-production.up.railway.app",
                )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            fake_google.callback_url,
            "https://cs-ia-website-production.up.railway.app/auth/google/callback",
        )

    def test_google_redirect_uri_is_canonical_railway_https_url(self):
        self.assertEqual(
            app_module.google_redirect_uri(),
            "https://cs-ia-website-production.up.railway.app/auth/google/callback",
        )

    def test_privacy_policy_is_public_and_linked_in_footer(self):
        response = self.donor.get("/privacy-policy")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Information we collect", response.data)
        self.assertIn(b"Google sign-in", response.data)
        self.assertIn(b"OpenStreetMap", response.data)
        self.assertIn(b'href="/privacy-policy"', response.data)

    def test_terms_are_public_and_linked_in_footer(self):
        response = self.donor.get("/terms-of-service")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Terms of service", response.data)
        self.assertIn(b"food safety", response.data.lower())
        self.assertIn(b'href="/terms-of-service"', response.data)

    def _csrf(self, client, path):
        response = client.get(path)
        match = re.search(
            rb'name="csrf_token" value="([^"]+)"',
            response.data,
        )
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def _register(self, client, role, email):
        token = self._csrf(client, f"/register?role={role}")
        response = client.post(
            "/register",
            data={
                "csrf_token": token,
                "name": role.title() + " User",
                "organization": role.title() + " Community",
                "organization_type": "Individual",
                "role": role,
                "city": "London",
                "email": email,
                "password": "valid-test-password",
            },
        )
        self.assertEqual(response.status_code, 302)

    def _create_donation(self, address, location_consent="yes"):
        token = self._csrf(self.donor, "/donations/new")
        return self.donor.post(
            "/donations/new",
            data={
                "csrf_token": token,
                "food_name": "Fresh apples",
                "category": "fruit",
                "quantity": "20",
                "quantity_unit": "kg",
                "dietary_tags": "vegan",
                "allergens": "",
                "storage_method": "ambient",
                "packaging": "Crates",
                "pickup_address": address,
                "pickup_start": future_time(1),
                "pickup_end": future_time(3),
                "expires_at": future_time(6),
                "pickup_notes": "Use the side entrance",
                "safety_confirmed": "yes",
                "location_consent": location_consent,
            },
        )

    def _create_request(self, address):
        token = self._csrf(self.recipient, "/requests/new")
        return self.recipient.post(
            "/requests/new",
            data={
                "csrf_token": token,
                "category": "fruit",
                "quantity": "10",
                "quantity_unit": "kg",
                "people_served": "12",
                "dietary_requirements": "vegan",
                "excluded_allergens": "",
                "storage_preference": "any",
                "collection_address": address,
                "max_distance_km": "25",
                "location_consent": "yes",
                "urgency": "3",
                "pickup_start": future_time(1),
                "pickup_end": future_time(3),
                "collection_deadline": future_time(4),
                "collection_notes": "Step-free access",
            },
        )

    def _read_rows(self):
        db = sqlite3.connect(self.database_path)
        try:
            db.row_factory = sqlite3.Row
            donation = db.execute("SELECT * FROM donations").fetchone()
            food_request = db.execute("SELECT * FROM requests").fetchone()
            return donation, food_request
        finally:
            db.close()

    @patch("app.geocode_address", return_value=Coordinates(51.5, -0.12))
    def test_publishing_requires_location_consent(self, geocode):
        response = self._create_donation("Private donor address", location_consent="")

        self.assertEqual(response.status_code, 200)
        geocode.assert_not_called()
        db = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM donations").fetchone()[0], 0)
        finally:
            db.close()

    @patch(
        "app.geocode_address",
        side_effect=[
            Coordinates(51.5, -0.12),
            Coordinates(51.55, -0.1),
        ],
    )
    @patch("allocation.get_road_route", return_value=RoadRoute(12000, 1800))
    def test_addresses_are_private_until_allocation(self, _route, _geocode):
        donor_response = self._create_donation("Private donor pickup")
        recipient_response = self._create_request("Private recipient collection")
        self.assertEqual(donor_response.status_code, 302)
        self.assertEqual(recipient_response.status_code, 302)

        donation, food_request = self._read_rows()
        self.assertEqual(donation["pickup_address"], "Private donor pickup")
        self.assertEqual(food_request["collection_address"], "Private recipient collection")
        self.assertEqual(food_request["max_distance_km"], 25)

        recipient_matches = self.recipient.get("/dashboard")
        donor_matches = self.donor.get("/dashboard")
        for response in (recipient_matches, donor_matches):
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"12.0 km road distance", response.data)
        self.assertNotIn(b"Private donor pickup", recipient_matches.data)
        self.assertNotIn(b"Private recipient collection", donor_matches.data)

        donor_token = self._csrf(self.donor, "/dashboard")
        allocated = self.donor.post(
            "/allocations",
            data={
                "csrf_token": donor_token,
                "donation_id": donation["id"],
                "request_id": food_request["id"],
                "quantity": "5",
            },
        )
        self.assertEqual(allocated.status_code, 302)

        donor_allocations = self.donor.get("/dashboard")
        recipient_allocations = self.recipient.get("/dashboard")
        for response in (donor_allocations, recipient_allocations):
            self.assertIn(b"Private donor pickup", response.data)
            self.assertIn(b"Private recipient collection", response.data)

    def test_request_rejects_distance_outside_supported_range(self):
        token = self._csrf(self.recipient, "/requests/new")
        with patch("app.geocode_address") as geocode:
            response = self.recipient.post(
                "/requests/new",
                data={
                    "csrf_token": token,
                    "category": "fruit",
                    "quantity": "10",
                    "quantity_unit": "kg",
                    "people_served": "12",
                    "dietary_requirements": "",
                    "excluded_allergens": "",
                    "storage_preference": "any",
                    "collection_address": "A valid address",
                    "max_distance_km": "251",
                    "location_consent": "yes",
                    "urgency": "3",
                    "pickup_start": future_time(1),
                    "pickup_end": future_time(3),
                    "collection_deadline": future_time(4),
                },
            )

        self.assertEqual(response.status_code, 200)
        geocode.assert_not_called()
        db = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
