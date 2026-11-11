import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from requests import Timeout

from allocation import score_match
from database import SCHEMA, DatabaseConnection
from geo import Coordinates, MapServiceError, RoadRoute, geocode_address, get_road_route


class FakeResponse:
    def __init__(self, payload, status_error=None):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        return self.payload


class GeocodingTests(unittest.TestCase):
    @patch("geo.time.sleep")
    @patch("geo.requests.get")
    def test_geocodes_address_to_valid_coordinates(self, get, _sleep):
        get.return_value = FakeResponse([{"lat": "51.5", "lon": "-0.12"}])

        result = geocode_address("Public pickup point, London")

        self.assertEqual(result, Coordinates(51.5, -0.12))
        self.assertEqual(get.call_args.kwargs["params"]["q"], "Public pickup point, London")

    @patch("geo.time.sleep")
    @patch("geo.requests.get")
    def test_rejects_an_address_that_cannot_be_found(self, get, _sleep):
        get.return_value = FakeResponse([])

        with self.assertRaisesRegex(MapServiceError, "could not locate"):
            geocode_address("not a real place")

    @patch("geo.requests.get")
    def test_geocoding_provider_failure_is_actionable(self, get):
        get.side_effect = Timeout("network timeout")

        with self.assertRaisesRegex(MapServiceError, "temporarily unavailable"):
            geocode_address("Some address")

    @patch("geo.requests.get")
    def test_reads_driving_distance_and_duration(self, get):
        get.return_value = FakeResponse(
            {"routes": [{"distance": 12500, "duration": 1800}]}
        )

        route = get_road_route(Coordinates(51.5, -0.12), Coordinates(51.6, -0.1))

        self.assertEqual(route, RoadRoute(12500, 1800))
        self.assertIn("-0.120000,51.500000; -0.100000,51.600000".replace(" ", ""), get.call_args.args[0])


class DistanceMatchingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        now = datetime.now()
        self.donation = {
            "id": 1,
            "donor_id": 1,
            "food_name": "Fresh apples",
            "category": "fruit",
            "quantity": 20.0,
            "remaining_quantity": 20.0,
            "dietary_tags": "vegan",
            "allergens": "",
            "quantity_unit": "kg",
            "storage_method": "ambient",
            "packaging": "Crates",
            "prepared_at": None,
            "pickup_start": (now + timedelta(hours=1)).isoformat(timespec="minutes"),
            "pickup_end": (now + timedelta(hours=3)).isoformat(timespec="minutes"),
            "pickup_notes": "",
            "pickup_address": "Private food pickup point",
            "pickup_latitude": 51.5,
            "pickup_longitude": -0.12,
            "safety_confirmed": 1,
            "expires_at": (now + timedelta(days=1)).isoformat(timespec="minutes"),
            "status": "available",
        }
        self.food_request = {
            "id": 2,
            "recipient_id": 2,
            "category": "fruit",
            "quantity": 10.0,
            "remaining_quantity": 10.0,
            "dietary_requirements": "vegan",
            "excluded_allergens": "",
            "quantity_unit": "kg",
            "storage_preference": "any",
            "people_served": 15,
            "urgency": 3,
            "pickup_start": (now + timedelta(hours=1)).isoformat(timespec="minutes"),
            "pickup_end": (now + timedelta(hours=3)).isoformat(timespec="minutes"),
            "collection_deadline": (now + timedelta(hours=4)).isoformat(timespec="minutes"),
            "collection_notes": "",
            "collection_address": "Private recipient collection point",
            "collection_latitude": 51.55,
            "collection_longitude": -0.1,
            "max_distance_km": 25.0,
            "status": "open",
        }

    def tearDown(self):
        self.db.close()

    @patch("allocation.get_road_route", return_value=RoadRoute(12000, 1800))
    def test_scores_and_caches_road_route(self, get_route):
        result = score_match(self.db, self.donation, self.food_request)

        self.assertIsNotNone(result)
        self.assertEqual(result["distance_km"], 12.0)
        self.assertEqual(result["duration_minutes"], 30)
        self.assertEqual(result["components"]["proximity"], 52.0)
        self.assertIn("road route 12.0 km", result["explanation"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM route_distance_cache").fetchone()[0],
            1,
        )

        score_match(self.db, self.donation, self.food_request)
        get_route.assert_called_once()

    @patch(
        "allocation.get_road_route",
        side_effect=[RoadRoute(12000, 1800), RoadRoute(15000, 2400)],
    )
    def test_refreshes_route_cache_entries_older_than_30_days(self, get_route):
        score_match(self.db, self.donation, self.food_request)
        self.db.execute(
            "UPDATE route_distance_cache SET calculated_at = datetime('now', '-31 days')"
        )
        self.db.commit()

        result = score_match(self.db, self.donation, self.food_request)

        self.assertEqual(result["distance_km"], 15.0)
        self.assertEqual(get_route.call_count, 2)
        cached_distance = self.db.execute(
            "SELECT distance_meters FROM route_distance_cache"
        ).fetchone()[0]
        self.assertEqual(cached_distance, 15000)

    @patch("allocation.get_road_route", return_value=RoadRoute(26000, 3600))
    def test_excludes_routes_beyond_recipient_limit(self, _get_route):
        self.assertIsNone(score_match(self.db, self.donation, self.food_request))

    @patch("allocation.get_road_route")
    def test_excludes_records_without_both_geocoded_locations(self, get_route):
        self.donation["pickup_latitude"] = None

        self.assertIsNone(score_match(self.db, self.donation, self.food_request))
        get_route.assert_not_called()

    @patch("allocation.get_road_route", return_value=None)
    def test_excludes_locations_without_a_drivable_route(self, _get_route):
        self.assertIsNone(score_match(self.db, self.donation, self.food_request))

    @patch(
        "allocation.get_road_route",
        side_effect=MapServiceError("Routing is temporarily unavailable."),
    )
    def test_does_not_create_a_match_when_route_service_fails(self, _get_route):
        errors = []

        from allocation import get_recommendations

        recommendations = get_recommendations(
            self.db,
            donation=self.donation,
            food_request=self.food_request,
            route_errors=errors,
        )

        self.assertEqual(recommendations, [])
        self.assertEqual(len(errors), 1)


class DatabaseConnectionTests(unittest.TestCase):
    def test_converts_route_cache_timestamp_sql_for_postgres(self):
        class CaptureConnection:
            def execute(self, query, parameters):
                self.query = query
                self.parameters = parameters
                return None

        connection = CaptureConnection()
        db = DatabaseConnection(connection)
        db.execute(
            """UPDATE route_distance_cache
               SET calculated_at = CURRENT_TIMESTAMP
               WHERE calculated_at >= datetime('now', '-30 days')"""
        )

        self.assertIn("calculated_at = CURRENT_TIMESTAMP::text", connection.query)
        self.assertIn(
            "calculated_at::timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days'",
            connection.query,
        )


if __name__ == "__main__":
    unittest.main()
