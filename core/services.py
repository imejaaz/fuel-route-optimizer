import csv
import io
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

import geonamescache
from django.conf import settings


MILES_PER_METER = 1 / 1609.344
MILES_PER_GALLON = 10
MAX_RANGE_MILES = 500
STATION_CORRIDOR_MILES = 30
EARTH_RADIUS_MILES = 3958.8
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
_nominatim_lock = threading.Lock()
_last_nominatim_request = 0.0
CENSUS_PLACE_ARCHIVE = "core/data/2025_Gaz_place_national.zip"
CENSUS_PLACE_FILE = "2025_Gaz_place_national.txt"
SAMPLE_GRID_DEGREES = 0.25
PLACE_TYPE_SUFFIX = re.compile(
    r" (?:city|town|village|borough|CDP|municipality|plantation|balance)$",
    re.IGNORECASE,
)
STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


class RouteServiceError(Exception):
    """An upstream routing or geocoding service failed."""


class LocationNotFoundError(ValueError):
    """A supplied location could not be resolved to a US location."""


class NoFuelStationsError(ValueError):
    """The supplied data cannot support a complete fuel plan for this route."""


@dataclass(frozen=True)
class FuelStation:
    truckstop_id: str
    name: str
    city: str
    state: str
    price_per_gallon: float
    latitude: float
    longitude: float
    route_mile: float


def _fetch_json(url: str) -> Any:
    request = Request(
        url,
        headers={"User-Agent": "FuelRouteOptimizer/1.0 (route planning API)"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except (
        HTTPError,
        URLError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise RouteServiceError("A map or routing service could not be reached.") from exc


def _fetch_nominatim_json(url: str) -> Any:
    global _last_nominatim_request
    with _nominatim_lock:
        delay = NOMINATIM_MIN_INTERVAL_SECONDS - (
            time.monotonic() - _last_nominatim_request
        )
        if delay > 0:
            time.sleep(delay)
        _last_nominatim_request = time.monotonic()
        return _fetch_json(url)


@lru_cache(maxsize=256)
def geocode_location(query: str) -> tuple[float, float]:
    params = urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "countrycodes": "us",
            "limit": 1,
        }
    )
    url = f"{NOMINATIM_URL}?{params}"
    results = _fetch_nominatim_json(url)
    if (
        not isinstance(results, list)
        or not results
        or not isinstance(results[0], dict)
        or results[0].get("address", {}).get("country_code") != "us"
    ):
        raise LocationNotFoundError(f"Could not find a US location for '{query}'.")
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RouteServiceError("The geocoding service returned an invalid location.") from exc


def get_driving_route(
    start: tuple[float, float], finish: tuple[float, float]
) -> tuple[list[list[float]], float]:
    start_lat, start_lon = start
    finish_lat, finish_lon = finish
    coordinates = f"{start_lon},{start_lat};{finish_lon},{finish_lat}"
    params = urlencode({"overview": "full", "geometries": "geojson", "steps": "false"})
    result = _fetch_json(f"{OSRM_URL}/{coordinates}?{params}")
    if (
        not isinstance(result, dict)
        or result.get("code") != "Ok"
        or not isinstance(result.get("routes"), list)
        or not result["routes"]
        or not isinstance(result["routes"][0], dict)
    ):
        raise RouteServiceError("The routing service could not find a driving route.")
    route = result["routes"][0]
    try:
        coordinates = route["geometry"]["coordinates"]
        geometry = [
            [float(point[0]), float(point[1])] for point in coordinates
        ]
        distance_miles = float(route["distance"]) * MILES_PER_METER
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RouteServiceError("The routing service returned an invalid route.") from exc
    if (
        len(geometry) < 2
        or not math.isfinite(distance_miles)
        or distance_miles <= 0
        or any(
            not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
            for longitude, latitude in geometry
        )
    ):
        raise RouteServiceError("The routing service returned an empty route.")
    return geometry, distance_miles


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


@lru_cache(maxsize=1)
def _city_coordinates() -> dict[tuple[str, str], tuple[float, float]]:
    path = Path(settings.BASE_DIR) / CENSUS_PLACE_ARCHIVE
    try:
        with ZipFile(path) as archive:
            with archive.open(CENSUS_PLACE_FILE) as data_file:
                records = csv.DictReader(
                    io.TextIOWrapper(data_file, encoding="utf-8"),
                    delimiter="|",
                )
                return {
                    (
                        _normalize(PLACE_TYPE_SUFFIX.sub("", record["NAME"])),
                        record["USPS"],
                    ): (float(record["INTPTLAT"]), float(record["INTPTLONG"]))
                    for record in records
                }
    except (OSError, BadZipFile, KeyError, ValueError) as exc:
        raise RouteServiceError(
            "The offline US place coordinate dataset is unavailable or invalid."
        ) from exc


@lru_cache(maxsize=1)
def _fuel_stations() -> tuple[dict[str, Any], ...]:
    path = Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv"
    try:
        with path.open(encoding="utf-8-sig", newline="") as csv_file:
            return tuple(csv.DictReader(csv_file))
    except OSError as exc:
        raise RouteServiceError("The fuel price dataset is unavailable.") from exc


def _haversine_miles(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    lat_a, lat_b = math.radians(latitude_a), math.radians(latitude_b)
    lat_delta = lat_b - lat_a
    lon_delta = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(lon_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, value)))


def _route_samples(
    geometry: list[list[float]], route_miles: float
) -> list[tuple[float, float, float]]:
    cumulative = [0.0]
    for previous, current in zip(geometry, geometry[1:]):
        cumulative.append(
            cumulative[-1]
            + _haversine_miles(previous[1], previous[0], current[1], current[0])
        )
    geometry_miles = cumulative[-1]
    if geometry_miles <= 0:
        raise RouteServiceError("The routing service returned an invalid route geometry.")
    stride = max(1, len(geometry) // 1000)
    indices = set(range(0, len(geometry), stride))
    indices.add(len(geometry) - 1)
    return [
        (
            geometry[index][1],
            geometry[index][0],
            cumulative[index] * route_miles / geometry_miles,
        )
        for index in sorted(indices)
    ]


def find_route_stations(
    geometry: list[list[float]], route_miles: float
) -> list[FuelStation]:
    city_coordinates = _city_coordinates()
    samples = _route_samples(geometry, route_miles)
    samples_by_cell: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for sample in samples:
        cell = (
            math.floor(sample[0] / SAMPLE_GRID_DEGREES),
            math.floor(sample[1] / SAMPLE_GRID_DEGREES),
        )
        samples_by_cell.setdefault(cell, []).append(sample)
    best_by_city: dict[tuple[str, str], dict[str, Any]] = {}
    for record in _fuel_stations():
        city = record.get("City", "").strip()
        state = record.get("State", "").strip().upper()
        if not city or state not in STATE_CODES:
            continue
        try:
            price = float(record["Retail Price"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        key = (_normalize(city), state)
        existing = best_by_city.get(key)
        if existing is None or price < existing["price"]:
            best_by_city[key] = {"record": record, "price": price}

    stations: list[FuelStation] = []
    for key, item in best_by_city.items():
        location = city_coordinates.get(key)
        if location is None:
            continue
        latitude, longitude = location
        lat_radius = (STATION_CORRIDOR_MILES + 5) / 68.7
        lon_radius = (STATION_CORRIDOR_MILES + 5) / (
            69.172 * max(0.1, math.cos(math.radians(latitude)))
        )
        nearby = []
        for lat_cell in range(
            math.floor((latitude - lat_radius) / SAMPLE_GRID_DEGREES),
            math.floor((latitude + lat_radius) / SAMPLE_GRID_DEGREES) + 1,
        ):
            for lon_cell in range(
                math.floor((longitude - lon_radius) / SAMPLE_GRID_DEGREES),
                math.floor((longitude + lon_radius) / SAMPLE_GRID_DEGREES) + 1,
            ):
                nearby.extend(samples_by_cell.get((lat_cell, lon_cell), ()))
        if not nearby:
            continue
        nearest = min(
            nearby,
            key=lambda sample: _haversine_miles(
                latitude, longitude, sample[0], sample[1]
            ),
        )
        detour = _haversine_miles(latitude, longitude, nearest[0], nearest[1])
        if detour > STATION_CORRIDOR_MILES:
            continue
        record = item["record"]
        stations.append(
            FuelStation(
                truckstop_id=record.get("OPIS Truckstop ID", ""),
                name=record.get("Truckstop Name", ""),
                city=record.get("City", ""),
                state=record.get("State", ""),
                price_per_gallon=item["price"],
                latitude=latitude,
                longitude=longitude,
                route_mile=nearest[2],
            )
        )
    stations.sort(key=lambda station: station.route_mile)
    return stations


def optimize_fuel_stops(
    route_miles: float, stations: list[FuelStation]
) -> tuple[list[FuelStation], float]:
    candidates = sorted(stations, key=lambda station: station.route_mile)
    costs: list[float] = []
    previous: list[int | None] = []
    for index, station in enumerate(candidates):
        position = station.route_mile
        cost = position * station.price_per_gallon / MILES_PER_GALLON
        predecessor = None
        if position > MAX_RANGE_MILES:
            cost = math.inf
        for earlier in range(index):
            gap = position - candidates[earlier].route_mile
            if gap > MAX_RANGE_MILES:
                continue
            candidate_cost = (
                costs[earlier]
                + gap * candidates[earlier].price_per_gallon / MILES_PER_GALLON
            )
            if candidate_cost < cost:
                cost = candidate_cost
                predecessor = earlier
        costs.append(cost)
        previous.append(predecessor)

    best_cost = math.inf
    last_stop: int | None = None
    for index, station in enumerate(candidates):
        remaining = route_miles - station.route_mile
        if remaining < 0 or remaining > MAX_RANGE_MILES or math.isinf(costs[index]):
            continue
        total = costs[index] + remaining * station.price_per_gallon / MILES_PER_GALLON
        if total < best_cost:
            best_cost = total
            last_stop = index
    if last_stop is None:
        raise NoFuelStationsError(
            "The fuel price data does not provide stations within 500 miles "
            "of each part of this route."
        )
    stop_indices = []
    current = last_stop
    while current is not None:
        stop_indices.append(current)
        current = previous[current]
    stop_indices.reverse()
    return [candidates[index] for index in stop_indices], round(best_cost, 2)


def plan_route(start_query: str, finish_query: str) -> dict[str, Any]:
    start = geocode_location(start_query)
    finish = geocode_location(finish_query)
    geometry, route_miles = get_driving_route(start, finish)
    stations = find_route_stations(geometry, route_miles)
    stops, total_cost = optimize_fuel_stops(route_miles, stations)
    return {
        "start": {"query": start_query, "latitude": start[0], "longitude": start[1]},
        "finish": {
            "query": finish_query,
            "latitude": finish[0],
            "longitude": finish[1],
        },
        "distance_miles": round(route_miles, 2),
        "estimated_fuel_gallons": round(route_miles / MILES_PER_GALLON, 2),
        "fuel_efficiency_mpg": MILES_PER_GALLON,
        "maximum_range_miles": MAX_RANGE_MILES,
        "total_fuel_cost_usd": total_cost,
        "fuel_stops": [
            {
                "truckstop_id": station.truckstop_id,
                "name": station.name,
                "city": station.city,
                "state": station.state,
                "price_per_gallon": station.price_per_gallon,
                "distance_from_start_miles": round(station.route_mile, 2),
                "latitude": station.latitude,
                "longitude": station.longitude,
                "location_is_approximate": True,
            }
            for station in stops
        ],
        "route": {"type": "LineString", "coordinates": geometry},
    }
