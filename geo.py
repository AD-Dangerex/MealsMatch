import os
import threading
import time
from dataclasses import dataclass

import requests


GEOCODER_URL = os.environ.get(
    "GEOCODER_URL", "https://nominatim.openstreetmap.org/search"
)
ROUTER_URL = os.environ.get(
    "ROUTER_URL", "https://router.project-osrm.org/route/v1/driving"
)
GEOCODER_INTERVAL_SECONDS = 1.0
_geocoder_lock = threading.Lock()
_last_geocoder_request = 0.0


class MapServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class RoadRoute:
    distance_meters: float
    duration_seconds: float


def _validate_coordinates(latitude, longitude):
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise MapServiceError("The map service returned invalid coordinates.")
    return Coordinates(latitude=latitude, longitude=longitude)


def geocode_address(address):
    global _last_geocoder_request
    headers = {
        "User-Agent": os.environ.get(
            "MAPS_USER_AGENT", "MealsMatch/1.0 (food-sharing community app)"
        )
    }

    with _geocoder_lock:
        wait_seconds = GEOCODER_INTERVAL_SECONDS - (
            time.monotonic() - _last_geocoder_request
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            response = requests.get(
                GEOCODER_URL,
                params={"q": address, "format": "jsonv2", "limit": 1},
                headers=headers,
                timeout=10,
            )
            _last_geocoder_request = time.monotonic()
            response.raise_for_status()
            results = response.json()
        except (requests.RequestException, ValueError) as error:
            raise MapServiceError(
                "The address lookup service is temporarily unavailable. "
                "Please try again shortly."
            ) from error

    if not results:
        raise MapServiceError(
            "We could not locate that address. Try a nearby street address "
            "or a public pickup point."
        )
    try:
        return _validate_coordinates(
            float(results[0]["lat"]), float(results[0]["lon"])
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MapServiceError(
            "The address lookup service returned an invalid location."
        ) from error


def get_road_route(origin, destination):
    origin = _validate_coordinates(origin.latitude, origin.longitude)
    destination = _validate_coordinates(destination.latitude, destination.longitude)
    url = (
        f"{ROUTER_URL}/"
        f"{origin.longitude:.6f},{origin.latitude:.6f};"
        f"{destination.longitude:.6f},{destination.latitude:.6f}"
    )
    try:
        response = requests.get(
            url,
            params={"overview": "false", "alternatives": "false", "steps": "false"},
            headers={
                "User-Agent": os.environ.get(
                    "MAPS_USER_AGENT", "MealsMatch/1.0 (food-sharing community app)"
                )
            },
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise MapServiceError(
            "The road-routing service is temporarily unavailable. "
            "Please try again shortly."
        ) from error

    routes = payload.get("routes")
    if not routes:
        return None
    try:
        distance_meters = float(routes[0]["distance"])
        duration_seconds = float(routes[0]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise MapServiceError(
            "The road-routing service returned an invalid route."
        ) from error
    if distance_meters < 0 or duration_seconds < 0:
        raise MapServiceError(
            "The road-routing service returned an invalid route."
        )
    return RoadRoute(distance_meters, duration_seconds)
