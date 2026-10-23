from unittest.mock import patch

from django.test import TestCase

from .services import (
    FuelStation,
    NoFuelStationsError,
    find_route_stations,
    optimize_fuel_stops,
)


def station(name: str, mile: float, price: float) -> FuelStation:
    return FuelStation(
        truckstop_id=name,
        name=name,
        city="Test City",
        state="CA",
        price_per_gallon=price,
        latitude=0,
        longitude=0,
        route_mile=mile,
    )


class FuelPlanningTests(TestCase):
    def test_finds_fuel_stations_using_bundled_place_coordinates(self):
        stations = find_route_stations(
            [[-122.33, 47.60], [-122.70, 46.00], [-122.68, 45.52]],
            180,
        )

        self.assertTrue(stations)
        self.assertTrue(any(item.city == "Tacoma" for item in stations))

    def test_chooses_stations_with_lowest_feasible_total_cost(self):
        stops, total = optimize_fuel_stops(
            1000,
            [
                station("start", 0, 4),
                station("cheap", 400, 2),
                station("last", 800, 3),
            ],
        )

        self.assertEqual([item.name for item in stops], ["cheap", "last"])
        self.assertEqual(total, 220)

    def test_respects_the_500_mile_maximum_range(self):
        stops, _ = optimize_fuel_stops(
            1200,
            [
                station("first", 0, 3),
                station("middle", 500, 3),
                station("last", 1000, 3),
            ],
        )

        self.assertEqual([item.name for item in stops], ["middle", "last"])
        self.assertEqual(stops[0].route_mile, 500)

    def test_fails_when_no_stations_make_the_route_reachable(self):
        with self.assertRaises(NoFuelStationsError):
            optimize_fuel_stops(
                1200,
                [station("first", 0, 3), station("too-far", 800, 2)],
            )


class RouteApiTests(TestCase):
    def test_returns_route_plan_for_valid_request(self):
        plan = {
            "distance_miles": 10,
            "total_fuel_cost_usd": 3,
            "fuel_stops": [],
            "route": {"type": "LineString", "coordinates": [[-122, 47], [-123, 45]]},
        }
        with patch("core.views.plan_route", return_value=plan) as plan_route:
            response = self.client.post(
                "/api/route/",
                data={"start": "Seattle, WA", "finish": "Portland, OR"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), plan)
        plan_route.assert_called_once_with("Seattle, WA", "Portland, OR")

    def test_rejects_invalid_json_and_missing_locations(self):
        invalid_json = self.client.post(
            "/api/route/", data="{", content_type="application/json"
        )
        missing_location = self.client.post(
            "/api/route/",
            data={"start": "Seattle, WA"},
            content_type="application/json",
        )

        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(missing_location.status_code, 400)
