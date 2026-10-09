from unittest.mock import patch

from src import server as app


TEST_PANDALS = [
    {
        "id": "P1",
        "name": "Nearby Pandal 1",
        "latitude": 22.5726,
        "longitude": 88.3639,
    },
    {
        "id": "P2",
        "name": "Nearby Pandal 2",
        "latitude": 22.5826,
        "longitude": 88.3639,
    },
    {
        "id": "P3",
        "name": "Alternative Pandal 1",
        "latitude": 22.5776,
        "longitude": 88.3639,
    },
    {
        "id": "P4",
        "name": "Alternative Pandal 2",
        "latitude": 22.5876,
        "longitude": 88.3639,
    },
]

TEST_ROUTES = [
    {
        "route_id": "R_NEAR",
        "route_name": "Nearest Route",
        "pandal_ids": ["P1", "P2"],
        "estimated_duration_hours": 4,
    },
    {
        "route_id": "R_ALT",
        "route_name": "Alternative Route",
        "pandal_ids": ["P3", "P4"],
        "estimated_duration_hours": 4,
    },
]


def run_test(scenario, rainy_pandals=None, unavailable=False):
    rainy_pandals = rainy_pandals or set()

    # Match rain to coordinates because the weather helper uses
    # coordinate keys, not pandal IDs.
    rainy_coordinates = {
        app._weather_location_key(
            p["latitude"], p["longitude"]
        )
        for p in TEST_PANDALS
        if p["id"] in rainy_pandals
    }

    def mock_weather(locations, forecast_hours=8):
        forecasts = {}

        for location in locations:
            key = app._weather_location_key(
                location["latitude"],
                location["longitude"],
            )
            rainy = key in rainy_coordinates

            forecasts[key] = {
                "status": "success",
                "rain_expected": rainy,
                "rain_signal_hours": 2 if rainy else 0,
                "mean_precipitation_probability_percent": (
                    80.0 if rainy else 0.0
                ),
                "total_forecast_precipitation_mm": (
                    2.0 if rainy else 0.0
                ),
            }

        return {
            "status": "success",
            "source": "MOCK TEST — NOT LIVE WEATHER",
            "forecast_hours": forecast_hours,
            "locations": forecasts,
        }

    weather_result = (
        {
            "status": "unavailable",
            "error": "Simulated API failure",
        }
        if unavailable
        else None
    )

    with (
        patch.object(app, "pandals", TEST_PANDALS),
        patch.object(app, "planned_routes", TEST_ROUTES),
    ):
        if unavailable:
            with patch.object(
                app,
                "fetch_weather_for_locations",
                return_value=weather_result,
            ):
                result = app.recommend_puja_route(
                    22.5726, 88.3639
                )
        else:
            with patch.object(
                app,
                "fetch_weather_for_locations",
                side_effect=mock_weather,
            ):
                result = app.recommend_puja_route(
                    22.5726, 88.3639
                )

    print(f"\n--- {scenario} ---")
    print("Route:", result["recommended_route"]["route_id"])
    print(
        "Weather adjustment:",
        result["weather_context"]["weather_adjustment_applied"],
    )
    print("Reason:", result["selection_reason"])

    return result


if __name__ == "__main__":
    # Test 1: All forecast locations are dry.
    dry = run_test("DRY WEATHER")

    assert dry["recommended_route"]["route_id"] == "R_NEAR"
    assert not dry["weather_context"]["weather_adjustment_applied"]

    # Test 2: Rain at both stops of the nearest route.
    rainy = run_test(
        "SIMULATED RAIN",
        rainy_pandals={"P1", "P2"},
    )

    assert rainy["recommended_route"]["route_id"] == "R_ALT"
    assert rainy["weather_context"]["weather_adjustment_applied"]

    # Test 3: Forecast API unavailable.
    fallback = run_test(
        "WEATHER UNAVAILABLE",
        unavailable=True,
    )

    assert fallback["recommended_route"]["route_id"] == "R_NEAR"
    assert not fallback["weather_context"]["weather_adjustment_applied"]

    print("\nPASS: All three scenarios passed.")