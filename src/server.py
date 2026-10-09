import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

from pathlib import Path

from mcp.server import MCPServer


# Find the project root
BASE_DIR = Path(__file__).resolve().parent.parent

# Path to the static JSON files
PANDAL_FILE = BASE_DIR / "data" / "static" / "pandals_2026.json"
METRO_FILE = BASE_DIR / "data" / "static" / "metro_stations.json"
RESTAURANT_FILE = BASE_DIR / "data" / "static" / "restaurants.json"

# Path to the dynamic Live Traffic file (read at query-time)
LIVE_TRAFFIC_FILE = BASE_DIR / "data" / "dynamic" / "live_traffic_2026.json"

# Static planned Puja routes sourced from the 2026 route dataset
PLANNED_ROUTES_FILE = BASE_DIR / "data" / "static" / "planned_puja_routes_2026.json"

PANDAL_UPDATES_FILE = (
    BASE_DIR / "data" / "dynamic" / "pandal_updates_2026.json"
)

PUJA_SCHEDULE_FILE = (
    BASE_DIR / "data" / "static" / "puja_schedule_2026.json"
)

OFFICIAL_PANDAL_PAGES_FILE = (
    BASE_DIR
    / "data"
    / "static"
    / "official_pandal_facebook_pages_2026.json"
)

# Production routing services.
# Point these to your own/private routing services in production.
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
VALHALLA_URL = os.environ.get("VALHALLA_URL", "https://valhalla1.openstreetmap.de").rstrip("/")

# Production safety/performance limits.
MAX_ROUTE_STOPS = int(os.environ.get("MAX_ROUTE_STOPS", "25"))
ROUTE_CACHE_TTL_SECONDS = int(os.environ.get("ROUTE_CACHE_TTL_SECONDS", "300"))
ROUTE_CACHE_MAX_ENTRIES = int(os.environ.get("ROUTE_CACHE_MAX_ENTRIES", "512"))
# Bump this when route-output semantics change so stale in-memory results are
# never reused after a server restart/hot reload cycle.
ROUTE_CACHE_VERSION = os.environ.get("ROUTE_CACHE_VERSION", "2026-10-07-optimization-comparison-v3")

# In-process cache. For multiple MCP instances, replace with shared Redis.
ROUTE_CACHE: dict[tuple, tuple[float, dict]] = {}

# Open-Meteo weather forecast configuration
OPEN_METEO_URL = os.environ.get(
    "OPEN_METEO_URL",
    "https://api.open-meteo.com/v1/forecast"
)

WEATHER_CACHE_TTL_SECONDS = int(
    os.environ.get("WEATHER_CACHE_TTL_SECONDS", "600")
)

WEATHER_CACHE = {}
WEATHER_BATCH_SIZE = 20

KOLKATA_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

# Load static data
with open(PANDAL_FILE, "r", encoding="utf-8") as f:
    pandals = json.load(f)

with open(METRO_FILE, "r", encoding="utf-8") as f:
    metro_stations = json.load(f)

with open(RESTAURANT_FILE, "r", encoding="utf-8") as f:
    restaurants = json.load(f)

with open(PLANNED_ROUTES_FILE, "r", encoding="utf-8") as f:
    planned_routes = json.load(f)

with open(PUJA_SCHEDULE_FILE, "r", encoding="utf-8") as f:
    puja_schedule_data = json.load(f)

with open(OFFICIAL_PANDAL_PAGES_FILE, "r", encoding="utf-8") as f:
    official_pandal_pages_data = json.load(f)

official_pandal_pages_by_id = {}

for record in official_pandal_pages_data["records"]:
    if not isinstance(record, dict):
        continue

    pandal_id = str(record.get("pandal_id", "")).strip()
    facebook_url = record.get("facebook_page")

    if (
        pandal_id
        and record.get("facebook_verified") is True
        and isinstance(facebook_url, str)
        and facebook_url.startswith("https://www.facebook.com/")
    ):
        official_pandal_pages_by_id[pandal_id.casefold()] = {
            "pandal_id": pandal_id,
            "pandal_name": record.get("pandal_name"),
            "facebook_page": facebook_url,
            "verified_at": record.get("verified_at"),
            "verification_source": record.get(
                "verification_source"
            ),
        }

def validate_records(records, record_type: str, required_fields=("id", "name", "latitude", "longitude")) -> None:
    """Validate dataset records before the MCP server starts.

    The server depends on stable unique IDs and valid geographic
    coordinates. Failing fast here prevents malformed data from
    producing incorrect tool results later.
    """

    if not isinstance(records, list):
        raise ValueError(f"{record_type} dataset must be a JSON list.")

    seen_ids = set()

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(
                f"{record_type} dataset record {index} must be a JSON object."
            )

        missing = [
            field for field in required_fields
            if field not in record or record[field] in (None, "")
        ]
        if missing:
            raise ValueError(
                f"{record_type} record {index} is missing required fields: "
                f"{', '.join(missing)}"
            )

        record_id = str(record["id"]).strip()
        normalized_id = record_id.lower()

        if normalized_id in seen_ids:
            raise ValueError(
                f"Duplicate {record_type} ID found: '{record_id}'. "
                "IDs must be unique (case-insensitive)."
            )
        seen_ids.add(normalized_id)

        try:
            latitude = float(record["latitude"])
            longitude = float(record["longitude"])
        except (TypeError, ValueError):
            raise ValueError(
                f"{record_type} record '{record_id}' has invalid coordinates."
            ) from None

        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise ValueError(
                f"{record_type} record '{record_id}' has non-finite coordinates."
            )

        if not -90 <= latitude <= 90:
            raise ValueError(
                f"{record_type} record '{record_id}' has invalid latitude: {latitude}."
            )

        if not -180 <= longitude <= 180:
            raise ValueError(
                f"{record_type} record '{record_id}' has invalid longitude: {longitude}."
            )


def validate_planned_routes(routes, pandal_records) -> None:
    """Validate planned routes against the master pandal dataset."""
    if not isinstance(routes, list):
        raise ValueError("Planned routes dataset must be a JSON list.")

    known_pandal_ids = {
        str(p["id"]).strip().lower()
        for p in pandal_records
    }

    seen_route_ids = set()

    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            raise ValueError(
                f"Planned route record {index} must be a JSON object."
            )

        route_id = str(route.get("route_id", "")).strip()
        route_name = str(route.get("route_name", "")).strip()
        route_pandal_ids = route.get("pandal_ids")

        if not route_id:
            raise ValueError(
                f"Planned route record {index} is missing route_id."
            )

        if route_id.casefold() in seen_route_ids:
            raise ValueError(
                f"Duplicate planned route ID found: '{route_id}'."
            )

        seen_route_ids.add(route_id.casefold())

        if not route_name:
            raise ValueError(
                f"Planned route '{route_id}' is missing route_name."
            )

        if not isinstance(route_pandal_ids, list) or not route_pandal_ids:
            raise ValueError(
                f"Planned route '{route_id}' must contain a non-empty "
                "pandal_ids list."
            )

        if len(route_pandal_ids) > MAX_ROUTE_STOPS:
            raise ValueError(
                f"Planned route '{route_id}' contains "
                f"{len(route_pandal_ids)} stops; maximum supported is "
                f"{MAX_ROUTE_STOPS}."
            )

        for pandal_id in route_pandal_ids:
            if str(pandal_id).strip().lower() not in known_pandal_ids:
                raise ValueError(
                    f"Planned route '{route_id}' references unknown "
                    f"pandal ID '{pandal_id}'."
                )


validate_records(pandals, "Pandal")
validate_records(metro_stations, "Metro station")
validate_records(restaurants, "Restaurant")
validate_planned_routes(planned_routes, pandals)


# Create the MCP server
mcp = MCPServer("Kolkata Puja Tourist MCP")

@mcp.resource("puja://2026/pandals")
def puja_pandals_resource() -> str:
    """
    Local 2026 Kolkata Durga Puja pandal dataset.

    Source provenance: PujoKolkata 2026 as recorded in the dataset.
    This resource exposes the project's collected records and should not
    be treated as a complete or official list of all Kolkata pandals.
    """
    return json.dumps(pandals, ensure_ascii=False, indent=2)

@mcp.resource("puja://transport")
def transport_resource() -> str:
    """
    Kolkata Metro station geographic data used by the tourist system.

    Source provenance: OpenStreetMap (ODbL), as recorded in the dataset.
    Distances calculated by this server are straight-line geographic
    distances from station coordinates, not walking distances.
    """
    return json.dumps(metro_stations, ensure_ascii=False, indent=2)

@mcp.resource("puja://restaurants")
def restaurants_resource() -> str:
    """
    Restaurant records and recorded opening hours.

    Source provenance: OpenStreetMap (ODbL), as recorded in the dataset.
    Opening hours are dataset values and are not live operational or
    reservation availability.
    """
    return json.dumps(restaurants, ensure_ascii=False, indent=2)

@mcp.resource("puja://2026/schedule")
def puja_schedule_resource() -> str:
    """Expose the local 2026 Puja ritual schedule and its sources."""
    return json.dumps(
        puja_schedule_data,
        ensure_ascii=False,
        indent=2,
    )

@mcp.tool()
def get_pandal_details(pandal_id: str) -> dict:
    """
    Return factual information about a Kolkata Durga Puja pandal from
    the local 2026 dataset.

    MUST be used when the user asks for information or details
    about a named pandal.

    Do not answer such questions from general knowledge or outside
    information. Use this tool for the named pandal's dataset record.

    Only report facts that are explicitly present in the returned dataset
    record. Do not supplement the answer with general knowledge, web knowledge,
    memory, assumptions, or outside information.

    Do not infer or invent:
    - popularity or fame
    - crowd levels
    - pandal themes
    - opening hours
    - historical significance
    - visitor recommendations
    - claims that a pandal is famous, prominent, popular, or well known

    If a requested fact is not present in the dataset, state that it is
    not available in the dataset.
    """

    for pandal in pandals:
        if pandal["id"].lower() == pandal_id.lower():
            return pandal

    return {
        "error": f"Pandal with ID '{pandal_id}' was not found."
    }

@mcp.tool()
def search_pandals(query: str = "") -> list[dict]:
    """
    Search Kolkata Durga Puja pandals by name, area, address, or zone.

    Use this tool when the user asks to:
    - find a pandal by name
    - find pandals in an area
    - search for pandals matching a place or location name

    Results contain only information from the local 2026 dataset.
    Do not infer popularity, themes, crowd levels, opening times,
    or other facts that are not present in the dataset.
    """

    query = query.strip().lower()

    # If no query is provided, return all pandals
    if not query:
        return pandals

    results = []

    for pandal in pandals:
        searchable_text = " ".join([
            str(pandal.get("name", "")),
            str(pandal.get("area", "")),
            str(pandal.get("address", "")),
            str(pandal.get("zone", ""))
        ]).lower()

        if query in searchable_text:
            results.append(pandal)

    return results

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)

    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c

@mcp.tool()
def find_nearby_pandals(
    latitude: float,
    longitude: float,
    radius_km: float = 3.0,
    limit: int = 10
) -> list[dict] | dict:
    """
    Find Durga Puja pandals within a specified radius of a geographic
    coordinate.

    MUST be used for requests such as:
    - pandals near a location
    - nearby pandals
    - pandals within X km
    - pandals around a landmark
    - pandals close to a given latitude/longitude

    Results are sorted by straight-line geographic distance from the
    supplied coordinates.

    For requests phrased as "near Deshapriya Park" or "within X km of
    a named pandal", use find_nearby_pandals_by_name instead of guessing
    coordinates.

    This tool does not determine popularity, crowd levels, themes,
    or other facts not contained in the dataset.

    The server currently supports driving routes only. Do not claim or
    estimate walking, cycling, or public-transit routes from this tool.
    """

    if not (-90 <= latitude <= 90):
        return {"error": "Latitude must be between -90 and 90."}

    if not (-180 <= longitude <= 180):
        return {"error": "Longitude must be between -180 and 180."}

    if radius_km <= 0:
        return {"error": "radius_km must be greater than 0."}

    if limit <= 0:
        return {"error": "limit must be greater than 0."}

    results = []

    for pandal in pandals:
        if not pandal.get("latitude") or not pandal.get("longitude"):
            continue

        distance = haversine_distance(
            latitude,
            longitude,
            float(pandal["latitude"]),
            float(pandal["longitude"])
        )

        if distance <= radius_km:
            result = dict(pandal)
            result["distance_km"] = round(distance, 2)
            results.append(result)

    results.sort(key=lambda x: x["distance_km"])

    return results[:limit]

@mcp.tool()
def find_nearby_pandals_by_name(
    pandal_name: str,
    radius_km: float = 2.0,
    limit: int = 10
) -> list[dict] | dict:
    """
    Find Durga Puja pandals within a specified radius of a named pandal.

    MUST be used when the user asks for pandals:
    - near a named pandal
    - within X km of a named pandal
    - around a specific pandal

    The reference pandal is resolved by exact case-insensitive name lookup
    in the local 2026 dataset. Its coordinates are taken directly from
    that dataset. Do NOT estimate or infer coordinates from general
    knowledge, web knowledge, memory, or assumptions.

    Results are sorted by straight-line geographic distance.
    The reference pandal itself is excluded from the results.
    """

    if not pandal_name or not pandal_name.strip():
        return {"error": "pandal_name must not be empty."}

    if radius_km <= 0:
        return {"error": "radius_km must be greater than 0."}

    if limit <= 0:
        return {"error": "limit must be greater than 0."}

    normalized_name = pandal_name.strip().casefold()
    reference_pandal = next(
        (p for p in pandals if str(p.get("name", "")).strip().casefold() == normalized_name),
        None
    )

    if reference_pandal is None:
        return {
            "error": f"Pandal named '{pandal_name}' was not found in the 2026 dataset."
        }

    reference_latitude = float(reference_pandal["latitude"])
    reference_longitude = float(reference_pandal["longitude"])

    results = []

    for pandal in pandals:
        if pandal["id"].lower() == reference_pandal["id"].lower():
            continue

        if not pandal.get("latitude") or not pandal.get("longitude"):
            continue

        distance = haversine_distance(
            reference_latitude,
            reference_longitude,
            float(pandal["latitude"]),
            float(pandal["longitude"])
        )

        if distance <= radius_km:
            result = dict(pandal)
            result["distance_km"] = round(distance, 2)
            results.append(result)

    results.sort(key=lambda x: x["distance_km"])

    return results[:limit]


@mcp.tool()
def get_nearest_metro(latitude: float, longitude: float) -> dict:
    """
    Find the nearest Kolkata Metro station to a geographic coordinate.

    Use this tool for requests such as:
    - nearest metro
    - closest metro station
    - metro station near a location or landmark

    Distance is straight-line geographic distance from the supplied
    coordinates. It is not walking distance.

    For requests about the nearest Metro station to a named pandal, use
    get_nearest_metro_by_pandal so the pandal coordinates come directly
    from the local dataset.
    """

    if not (-90 <= latitude <= 90):
        return {"error": "Latitude must be between -90 and 90."}

    if not (-180 <= longitude <= 180):
        return {"error": "Longitude must be between -180 and 180."}

    nearest_station = None
    nearest_distance = float("inf")

    for station in metro_stations:
        if (
            not station.get("latitude")
            or not station.get("longitude")
            or station["latitude"] == 0
            or station["longitude"] == 0
        ):
            continue

        distance = haversine_distance(
            latitude,
            longitude,
            float(station["latitude"]),
            float(station["longitude"])
        )

        if distance < nearest_distance:
            nearest_distance = distance
            nearest_station = station

    if nearest_station is None:
        return {
            "error": "No Metro station with valid coordinates was found."
        }

    result = dict(nearest_station)
    result["distance_km"] = round(nearest_distance, 2)

    return result

def _get_cached_route(cache_key: tuple) -> dict | None:
    entry = ROUTE_CACHE.get(cache_key)

    if entry is None:
        return None

    cached_at, data = entry

    if time.monotonic() - cached_at > ROUTE_CACHE_TTL_SECONDS:
        ROUTE_CACHE.pop(cache_key, None)
        return None

    return data


def _set_cached_route(cache_key: tuple, data: dict) -> None:
    now = time.monotonic()

    expired = [
        key
        for key, (created_at, _) in ROUTE_CACHE.items()
        if now - created_at > ROUTE_CACHE_TTL_SECONDS
    ]

    for key in expired:
        ROUTE_CACHE.pop(key, None)

    while len(ROUTE_CACHE) >= ROUTE_CACHE_MAX_ENTRIES:
        oldest_key = min(
            ROUTE_CACHE,
            key=lambda key: ROUTE_CACHE[key][0]
        )
        ROUTE_CACHE.pop(oldest_key, None)

    ROUTE_CACHE[cache_key] = (now, data)


def _validate_route_stop_count(pandal_ids: list[str]) -> dict | None:
    if len(pandal_ids) < 2:
        return {
            "status": "error",
            "error": "At least 2 pandal IDs are required."
        }

    if len(pandal_ids) > MAX_ROUTE_STOPS:
        return {
            "status": "error",
            "error": (
                f"A maximum of {MAX_ROUTE_STOPS} pandal stops is supported "
                "per route."
            )
        }

    return None


def build_route_stop_snapshot(selected_pandals: list[dict]) -> list[dict]:
    """Return a compact, deterministic representation of supplied stops."""
    return [
        {
            "stop_number": i + 1,
            "id": pandal["id"],
            "name": pandal["name"],
            "latitude": pandal["latitude"],
            "longitude": pandal["longitude"]
        }
        for i, pandal in enumerate(selected_pandals)
    ]


def build_straight_line_fallback_legs(selected_pandals: list[dict]) -> list[dict]:
    """
    Build explicitly labelled straight-line distances for a route fallback.

    These values must never be presented as driving or walking distances.
    """
    legs = []
    for i in range(len(selected_pandals) - 1):
        start = selected_pandals[i]
        end = selected_pandals[i + 1]
        legs.append({
            "from": start["name"],
            "to": end["name"],
            "distance_km": round(
                haversine_distance(
                    float(start["latitude"]),
                    float(start["longitude"]),
                    float(end["latitude"]),
                    float(end["longitude"])
                ),
                2
            ),
            "distance_type": "straight_line_geographic",
            "travel_time_available": False
        })
    return legs


def build_route_service_fallback(
    selected_pandals: list[dict],
    *,
    mode: str,
    routing_service: str,
    routing_error: dict
) -> dict:
    """
    Return a safe fallback when a routing backend is unavailable.

    The supplied sequence is retained for reference, but no route geometry,
    road distance, walking distance, or travel time is claimed.
    """
    error_message = routing_error.get("error", "Routing service unavailable.")
    return {
        "status": "routing_unavailable",
        "mode": mode,
        "routing_service": routing_service,
        "route_calculated": False,
        "route_strategy": "provided_order",
        "optimized": False,
        "stops": build_route_stop_snapshot(selected_pandals),
        "fallback": {
            "used": True,
            "type": "supplied_stop_order",
            "description": (
                "Routing was not calculated. The stop sequence shown is only "
                "the order supplied to the tool."
            )
        },
        "distance_information": {
            "driving_distance_available": False,
            "walking_distance_available": False,
            "straight_line_distance_available": True,
            "straight_line_distance_note": (
                "Any straight-line distances in fallback legs are geographic "
                "distances and must not be interpreted as road or walking distances."
            )
        },
        "travel_time_information": {
            "available": False,
            "note": "No driving/walking travel time was calculated."
        },
        "fallback_legs": build_straight_line_fallback_legs(selected_pandals),
        "routing_error": routing_error
    }


def check_routing_service_health() -> dict:
    """Check reachability of the configured private routing services."""
    health = {}

    try:
        url = (
            f"{OSRM_URL}/route/v1/driving/"
            "88.3639,22.5726;88.3649,22.5736"
            "?overview=false"
        )
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        health["osrm"] = {
            "status": "healthy" if data.get("code") == "Ok" else "unhealthy",
            "url": OSRM_URL
        }
    except Exception as exc:
        health["osrm"] = {
            "status": "unhealthy",
            "url": OSRM_URL,
            "error": str(exc)
        }

    try:
        url = f"{VALHALLA_URL}/status"
        with urllib.request.urlopen(url, timeout=5) as response:
            status_code = response.status

        health["valhalla"] = {
            "status": "healthy" if status_code == 200 else "unhealthy",
            "url": VALHALLA_URL
        }
    except Exception as exc:
        health["valhalla"] = {
            "status": "unhealthy",
            "url": VALHALLA_URL,
            "error": str(exc)
        }

    return health


@mcp.tool()
def get_routing_service_health() -> dict:
    """
    Check whether the configured private OSRM and Valhalla services
    are reachable.
    """
    return check_routing_service_health()


def request_osrm_route(url: str, purpose: str) -> dict:
    """Request and validate an OSRM route response.

    Returns either the parsed OSRM response or a user-friendly error object.
    """

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        return {
            "error": f"The routing service returned HTTP {e.code}.",
            "routing_service": "OSRM",
            "routing_error_type": "http_error",
            "details": str(e.reason),
        }

    except urllib.error.URLError as e:
        return {
            "error": f"Could not reach the routing service for {purpose}.",
            "routing_service": "OSRM",
            "routing_error_type": "network_error",
            "details": str(e.reason),
        }

    except TimeoutError:
        return {
            "error": f"The routing service timed out while calculating {purpose}.",
            "routing_service": "OSRM",
            "routing_error_type": "timeout",
        }

    except json.JSONDecodeError:
        return {
            "error": "The routing service returned an invalid response.",
            "routing_service": "OSRM",
            "routing_error_type": "invalid_response",
        }

    if not isinstance(data, dict):
        return {
            "error": "The routing service returned an unexpected response format.",
            "routing_service": "OSRM",
            "routing_error_type": "invalid_response",
        }

    if data.get("code") != "Ok":
        return {
            "error": data.get("message") or f"No {purpose} could be found.",
            "routing_service": "OSRM",
            "routing_error_type": "no_route",
        }

    routes = data.get("routes")
    if not routes:
        return {
            "error": f"The routing service returned no route for {purpose}.",
            "routing_service": "OSRM",
            "routing_error_type": "invalid_response",
        }

    return data

def request_valhalla_walking_route(payload: dict) -> dict:
    """
    Request a pedestrian route from the configured private/self-hosted
    Valhalla service.

    Valhalla supports pedestrian routing using the 'pedestrian' costing model.
    """
    url = f"{VALHALLA_URL}/route"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Kolkata-Puja-Tourist-MCP/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        return {
            "error": f"Walking routing service returned HTTP {e.code}.",
            "routing_service": "Valhalla",
            "routing_error_type": "http_error",
            "details": str(e.reason)
        }

    except urllib.error.URLError as e:
        return {
            "error": "Could not reach the walking routing service.",
            "routing_service": "Valhalla",
            "routing_error_type": "network_error",
            "details": str(e.reason)
        }

    except TimeoutError:
        return {
            "error": "Walking routing service timed out.",
            "routing_service": "Valhalla",
            "routing_error_type": "timeout"
        }

    except json.JSONDecodeError:
        return {
            "error": "Walking routing service returned invalid JSON.",
            "routing_service": "Valhalla",
            "routing_error_type": "invalid_response"
        }

    if not isinstance(data, dict):
        return {
            "error": "Walking routing service returned an unexpected response.",
            "routing_service": "Valhalla",
            "routing_error_type": "invalid_response"
        }

    trip = data.get("trip")

    if not isinstance(trip, dict):
        return {
            "error": "Walking routing service did not return a trip.",
            "routing_service": "Valhalla",
            "routing_error_type": "invalid_response"
        }

    if trip.get("status") != 0:
        return {
            "error": trip.get("status_message", "No walking route could be found."),
            "routing_service": "Valhalla",
            "routing_error_type": "no_route"
        }

    return data

@mcp.tool()
def get_nearest_metro_by_pandal(pandal_name: str) -> dict:
    """
    Find the nearest Kolkata Metro station to a named Durga Puja pandal.

    MUST be used when the user asks for the nearest or closest Metro
    station to a named pandal. The pandal coordinates are taken directly
    from the local 2026 dataset; do not estimate coordinates yourself.

    Distance is straight-line geographic distance, not walking distance.
    """

    if not pandal_name or not pandal_name.strip():
        return {"error": "pandal_name must not be empty."}

    normalized_name = pandal_name.strip().casefold()
    reference_pandal = next(
        (p for p in pandals if str(p.get("name", "")).strip().casefold() == normalized_name),
        None
    )

    if reference_pandal is None:
        return {
            "error": f"Pandal named '{pandal_name}' was not found in the 2026 dataset."
        }

    return get_nearest_metro(
        float(reference_pandal["latitude"]),
        float(reference_pandal["longitude"])
    )


@mcp.tool()
def get_route_between_pandals(
    start_pandal_id: str,
    end_pandal_id: str
) -> dict:
    """
    Get a driving route between two Kolkata Durga Puja pandals.

    Uses the configured private/self-hosted OSRM service.
    Distance and duration are driving-route estimates.
    """

    start_pandal = None
    end_pandal = None

    for pandal in pandals:
        if pandal["id"].lower() == start_pandal_id.lower():
            start_pandal = pandal
            break

    for pandal in pandals:
        if pandal["id"].lower() == end_pandal_id.lower():
            end_pandal = pandal
            break

    if start_pandal is None:
        return {
            "status": "error",
            "error": f"Starting pandal '{start_pandal_id}' was not found."
        }

    if end_pandal is None:
        return {
            "status": "error",
            "error": f"Destination pandal '{end_pandal_id}' was not found."
        }

    cache_key = (
        "driving",
        (start_pandal["id"].lower(), end_pandal["id"].lower())
    )

    cached = _get_cached_route(cache_key)
    if cached is not None:
        return cached

    coordinates = (
        f"{start_pandal['longitude']},{start_pandal['latitude']};"
        f"{end_pandal['longitude']},{end_pandal['latitude']}"
    )

    url = (
        f"{OSRM_URL}/route/v1/driving/"
        f"{coordinates}"
        f"?overview=false&steps=true"
    )

    data = request_osrm_route(url, "the driving route")

    if "error" in data:
        return data

    route = data["routes"][0]

    result = {
        "mode": "driving",
        "routing_service": "OSRM",
        "from": {
            "id": start_pandal["id"],
            "name": start_pandal["name"],
            "latitude": start_pandal["latitude"],
            "longitude": start_pandal["longitude"]
        },
        "to": {
            "id": end_pandal["id"],
            "name": end_pandal["name"],
            "latitude": end_pandal["latitude"],
            "longitude": end_pandal["longitude"]
        },
        "distance_km": round(route["distance"] / 1000, 2),
        "duration_minutes": round(route["duration"] / 60, 1),
        "steps": route["legs"][0]["steps"],
        "cache": {
            "enabled": True,
            "ttl_seconds": ROUTE_CACHE_TTL_SECONDS
        }
    }

    _set_cached_route(cache_key, result)
    return result

def _find_pandals_by_ids(pandal_ids: list[str]) -> list[dict] | dict:
    """Resolve and validate pandal IDs in the caller-supplied order."""
    selected_pandals = []

    for pandal_id in pandal_ids:
        found = next(
            (p for p in pandals if str(p["id"]).casefold() == str(pandal_id).casefold()),
            None
        )

        if found is None:
            return {
                "status": "error",
                "error": f"Pandal '{pandal_id}' was not found."
            }

        selected_pandals.append(found)

    return selected_pandals


def request_osrm_table(pandals_for_matrix: list[dict]) -> dict:
    """Request an OSRM distance/duration matrix for the supplied pandals."""
    coordinates = ";".join(
        f"{p['longitude']},{p['latitude']}"
        for p in pandals_for_matrix
    )
    url = (
        f"{OSRM_URL}/table/v1/driving/"
        f"{coordinates}"
        f"?annotations=distance,duration"
    )

    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {
            "error": f"OSRM optimization matrix returned HTTP {e.code}.",
            "routing_service": "OSRM",
            "routing_error_type": "http_error",
            "details": str(e.reason)
        }
    except urllib.error.URLError as e:
        return {
            "error": "Could not reach OSRM while building the optimization matrix.",
            "routing_service": "OSRM",
            "routing_error_type": "network_error",
            "details": str(e.reason)
        }
    except TimeoutError:
        return {
            "error": "OSRM timed out while building the optimization matrix.",
            "routing_service": "OSRM",
            "routing_error_type": "timeout"
        }
    except json.JSONDecodeError:
        return {
            "error": "OSRM returned invalid JSON for the optimization matrix.",
            "routing_service": "OSRM",
            "routing_error_type": "invalid_response"
        }

    if not isinstance(data, dict):
        return {
            "error": "OSRM returned an unexpected optimization response format.",
            "routing_service": "OSRM",
            "routing_error_type": "invalid_response"
        }

    if data.get("code") != "Ok":
        return {
            "error": data.get("message", "OSRM could not build the optimization matrix."),
            "routing_service": "OSRM",
            "routing_error_type": "no_matrix"
        }

    distances = data.get("distances")
    durations = data.get("durations")

    if not isinstance(distances, list) or not isinstance(durations, list):
        return {
            "error": "OSRM did not return both distance and duration matrices.",
            "routing_service": "OSRM",
            "routing_error_type": "invalid_response"
        }

    return {
        "distances": distances,
        "durations": durations
    }


def _held_karp_fixed_start(distances: list[list[float]]) -> list[int]:
    """Find an exact minimum-distance tour order with index 0 fixed as start."""
    n = len(distances)
    if n <= 2:
        return list(range(n))

    # State: (visited_mask, last_index) -> (cost, path_tuple).
    # The mask covers indices 1..n-1; index 0 is fixed as the start.
    dp: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {}

    for last in range(1, n):
        mask = 1 << (last - 1)
        cost = distances[0][last]
        if cost is not None:
            dp[(mask, last)] = (float(cost), (0, last))

    for mask in range(1, 1 << (n - 1)):
        for last in range(1, n):
            if not (mask & (1 << (last - 1))):
                continue
            state = dp.get((mask, last))
            if state is None:
                continue
            cost, path = state
            for nxt in range(1, n):
                bit = 1 << (nxt - 1)
                if mask & bit:
                    continue
                edge = distances[last][nxt]
                if edge is None:
                    continue
                new_mask = mask | bit
                new_cost = cost + float(edge)
                candidate = (new_cost, path + (nxt,))
                existing = dp.get((new_mask, nxt))
                if existing is None or candidate[0] < existing[0]:
                    dp[(new_mask, nxt)] = candidate

    full_mask = (1 << (n - 1)) - 1
    best = None
    for last in range(1, n):
        state = dp.get((full_mask, last))
        if state is None:
            continue
        if best is None or state[0] < best[0]:
            best = state

    if best is None:
        raise ValueError("No complete OSRM-connected route ordering was found.")

    return list(best[1])


def _nearest_neighbor_two_opt(distances: list[list[float]]) -> list[int]:
    """Heuristic optimization for larger stop sets with index 0 fixed."""
    n = len(distances)
    if n <= 2:
        return list(range(n))

    order = [0]
    remaining = set(range(1, n))
    current = 0

    while remaining:
        nxt = min(
            remaining,
            key=lambda idx: float("inf") if distances[current][idx] is None else distances[current][idx]
        )
        if distances[current][nxt] is None:
            raise ValueError("No complete OSRM-connected route ordering was found.")
        order.append(nxt)
        remaining.remove(nxt)
        current = nxt

    def path_cost(path: list[int]) -> float:
        total = 0.0
        for a, b in zip(path, path[1:]):
            edge = distances[a][b]
            if edge is None:
                return float("inf")
            total += float(edge)
        return total

    improved = True
    while improved:
        improved = False
        best_cost = path_cost(order)
        for i in range(1, n - 2):
            for j in range(i + 1, n):
                candidate = order[:i] + list(reversed(order[i:j])) + order[j:]
                candidate_cost = path_cost(candidate)
                if candidate_cost + 1e-9 < best_cost:
                    order = candidate
                    best_cost = candidate_cost
                    improved = True
    return order


def optimize_pandal_order_with_osrm(selected_pandals: list[dict]) -> dict:
    """Optimize supplied stops for minimum OSRM road-network driving distance.

    The first supplied pandal remains fixed as the start. The OSRM Table API
    supplies the directional road-distance matrix. The application, not OSRM,
    performs the TSP optimization using Held-Karp (exact) or nearest-neighbor
    + 2-opt (heuristic for larger inputs).
    """
    matrix_result = request_osrm_table(selected_pandals)
    if "error" in matrix_result:
        return matrix_result

    distances = matrix_result["distances"]
    durations = matrix_result["durations"]
    count = len(selected_pandals)

    if (
        len(distances) != count
        or len(durations) != count
        or any(
            not isinstance(row, list) or len(row) != count
            for row in distances
        )
        or any(
            not isinstance(row, list) or len(row) != count
            for row in durations
        )
    ):
        return {
            "error": "OSRM returned malformed distance/duration matrices.",
            "routing_service": "OSRM",
            "routing_error_type": "invalid_matrix"
        }

    try:
        if count <= 15:
            order = _held_karp_fixed_start(distances)
            method = "Exact Held-Karp TSP using OSRM driving-distance matrix"
            exact = True
        else:
            order = _nearest_neighbor_two_opt(distances)
            method = (
                "Nearest-neighbor + 2-opt heuristic using OSRM "
                "driving-distance matrix"
            )
            exact = False
    except ValueError as exc:
        return {
            "error": str(exc),
            "routing_service": "OSRM",
            "routing_error_type": "optimization_failed"
        }

    # The original order must have a valid matrix path if we are going to
    # compare optimization savings. Never silently skip unavailable legs.
    original_distance_m = 0.0
    for i in range(count - 1):
        edge = distances[i][i + 1]
        if edge is None:
            return {
                "error": (
                    "OSRM did not provide a distance for an original-order "
                    f"leg: {selected_pandals[i]['id']} -> "
                    f"{selected_pandals[i + 1]['id']}."
                ),
                "routing_service": "OSRM",
                "routing_error_type": "incomplete_matrix"
            }
        original_distance_m += float(edge)

    optimized_distance_m = 0.0
    for i in range(count - 1):
        edge = distances[order[i]][order[i + 1]]
        if edge is None:
            return {
                "error": "OSRM matrix contains an unavailable optimized leg.",
                "routing_service": "OSRM",
                "routing_error_type": "incomplete_matrix"
            }
        optimized_distance_m += float(edge)

    optimized_pandals = [selected_pandals[i] for i in order]
    original_ids = [p["id"] for p in selected_pandals]
    optimized_ids = [p["id"] for p in optimized_pandals]

    return {
        "optimized_pandals": optimized_pandals,
        "original_order": original_ids,
        "optimized_order": optimized_ids,
        "optimization_matrix_distance_km": [
            [None if value is None else value / 1000 for value in row]
            for row in distances
        ],
        "optimization_matrix_duration_minutes": [
            [None if value is None else value / 60 for value in row]
            for row in durations
        ],
        "optimization_method": method,
        "optimization_exact": exact,
        "optimization_objective": "minimum total OSRM road-network driving distance",
        "optimization_cost_source": "OSRM driving-distance matrix",
        "start_pandal_fixed": True
    }


def _calculate_osrm_route_for_order(route_pandals: list[dict]) -> dict:
    """Calculate one actual OSRM route for an explicit stop order."""
    coordinates = ";".join(
        f"{p['longitude']},{p['latitude']}"
        for p in route_pandals
    )
    url = (
        f"{OSRM_URL}/route/v1/driving/"
        f"{coordinates}"
        f"?overview=false&steps=false"
    )
    return request_osrm_route(url, "the driving route")


@mcp.tool()
def plan_puja_route(pandal_ids: list[str], optimize: bool = False) -> dict:
    """
    Calculate a driving route through multiple Kolkata Durga Puja pandals.

    By default, the exact supplied order is preserved.

    When optimize=True:
      1. OSRM Table provides a directional road-network distance matrix.
      2. This application optimizes that matrix with exact Held-Karp for
         <=15 stops, or nearest-neighbor + 2-opt for larger stop sets.
      3. The first supplied pandal remains fixed as the starting point.
      4. OSRM then calculates the final optimized route.
      5. OSRM also calculates the original supplied-order route so distance
         and time savings are based only on actual OSRM results.

    No Euclidean distance, average speed, or estimated driving time is used
    in optimization or savings calculations.
    """

    validation_error = _validate_route_stop_count(pandal_ids)
    if validation_error:
        return validation_error

    cache_key = (
        "driving",
        ROUTE_CACHE_VERSION,
        tuple(str(pid).strip().lower() for pid in pandal_ids),
        bool(optimize)
    )

    cached = _get_cached_route(cache_key)
    if cached is not None:
        return cached

    selected_result = _find_pandals_by_ids(pandal_ids)
    if isinstance(selected_result, dict):
        return selected_result
    selected_pandals = selected_result

    optimization_meta = {
        "requested": bool(optimize),
        "performed": False,
        "status": "not_requested" if not optimize else "pending",
        "method": None,
        "objective": None,
        "cost_source": None,
        "exact": None,
        "start_pandal_fixed": True,
        "starting_pandal_id": selected_pandals[0]["id"],
        "starting_pandal_name": selected_pandals[0]["name"],
        "original_order": [p["id"] for p in selected_pandals],
        "original_order_strategy": "supplied_order_with_first_stop_fixed_as_start",
        "optimized_order": None,
        "original_distance_km": None,
        "optimized_distance_km": None,
        "original_duration_minutes": None,
        "optimized_duration_minutes": None,
        "distance_saved_km": None,
        "duration_saved_minutes": None,
    }

    route_pandals = selected_pandals

    if optimize:
        optimization_result = optimize_pandal_order_with_osrm(selected_pandals)
        if "error" in optimization_result:
            optimization_meta.update({
                "performed": False,
                "status": "failed",
                "error": optimization_result.get("error"),
                "routing_error_type": optimization_result.get("routing_error_type")
            })
        else:
            route_pandals = optimization_result["optimized_pandals"]
            optimization_meta.update({
                "performed": True,
                "status": "success",
                "method": optimization_result["optimization_method"],
                "objective": optimization_result["optimization_objective"],
                "cost_source": optimization_result["optimization_cost_source"],
                "exact": optimization_result["optimization_exact"],
                "original_order": optimization_result["original_order"],
                "optimized_order": optimization_result["optimized_order"],
                "optimization_matrix_distance_km": optimization_result[
                    "optimization_matrix_distance_km"
                ],
                "optimization_matrix_duration_minutes": optimization_result[
                    "optimization_matrix_duration_minutes"
                ],
                "comparison_baseline_source": (
                    "separate OSRM route calculation for the original supplied order; "
                    "NOT the optimization matrix summary"
                )
            })

    # Calculate the actual route in the selected order.
    data = _calculate_osrm_route_for_order(route_pandals)

    if "error" in data:
        fallback = build_route_service_fallback(
            route_pandals,
            mode="driving",
            routing_service="OSRM",
            routing_error=data
        )
        fallback["optimization"] = optimization_meta
        return fallback

    route = data["routes"][0]

    legs = []
    for i, leg in enumerate(route["legs"]):
        legs.append({
            "from": route_pandals[i]["name"],
            "to": route_pandals[i + 1]["name"],
            "distance_km": round(leg["distance"] / 1000, 2),
            "duration_minutes": round(leg["duration"] / 60, 1)
        })

    optimized_distance_km = route["distance"] / 1000
    optimized_duration_minutes = route["duration"] / 60

    if optimize and optimization_meta.get("performed"):
        # Fetch the original supplied-order route separately. This makes all
        # savings values actual OSRM results rather than matrix/average-speed
        # estimates.
        original_data = _calculate_osrm_route_for_order(selected_pandals)

        if "error" in original_data:
            optimization_meta.update({
                "original_route_metrics_status": "unavailable",
                "original_route_metrics_error": original_data.get("error"),
                "original_distance_km": None,
                "original_duration_minutes": None,
                "distance_saved_km": None,
                "duration_saved_minutes": None,
            })
            route_note = (
                "OSRM optimized the stop order and calculated the final route. "
                "The original-order OSRM route could not be calculated, so "
                "savings are not reported."
            )
        else:
            original_route = original_data["routes"][0]

            # Keep the displayed metrics and the reported savings numerically
            # consistent. Both original and optimized durations are real OSRM
            # results; we round only for presentation, then subtract the
            # displayed values so the reported savings match what users see.
            original_distance_km = original_route["distance"] / 1000
            original_duration_minutes = original_route["duration"] / 60

            original_distance_display = round(original_distance_km, 2)
            optimized_distance_display = round(optimized_distance_km, 2)
            original_duration_display = round(original_duration_minutes, 1)
            optimized_duration_display = round(optimized_duration_minutes, 1)

            distance_saved_km = round(
                original_distance_display - optimized_distance_display, 2
            )
            duration_saved_minutes = round(
                original_duration_display - optimized_duration_display, 1
            )

            optimization_meta.update({
                "original_route_metrics_status": "calculated_by_osrm",
                "original_distance_km": original_distance_display,
                "original_duration_minutes": original_duration_display,
                "optimized_distance_km": optimized_distance_display,
                "optimized_duration_minutes": optimized_duration_display,
                "distance_saved_km": distance_saved_km,
                "duration_saved_minutes": duration_saved_minutes,
                "savings_basis": "rounded_display_values_from_actual_osrm_routes",
            })
            route_note = (
                "OSRM calculated both the original supplied-order route and the "
                "optimized route. P053 remained fixed as the starting pandal. "
                "Use the top-level comparison object for original-vs-optimized "
                "savings. The optimization matrix is for choosing the stop order "
                "and must not be used as the savings baseline."
            )

        route_strategy = "osrm_distance_optimized"
        optimized = True
    elif optimize:
        route_strategy = "provided_order_optimization_unavailable"
        optimized = False
        route_note = (
            "OSRM calculated the driving route in the supplied order, but the "
            "requested optimization could not be performed. No optimized order "
            "or optimization savings are claimed."
        )
    else:
        route_strategy = "provided_order"
        optimized = False
        route_note = (
            "OSRM calculated this driving route in the exact supplied order. "
            "Route optimization was not requested."
        )

    if optimize and optimization_meta.get("performed"):
        comparison = {
            "status": optimization_meta.get("original_route_metrics_status"),
            "source": "actual OSRM /route calculations",
            "original_order": optimization_meta.get("original_order"),
            "optimized_order": optimization_meta.get("optimized_order"),
            "original_distance_km": optimization_meta.get("original_distance_km"),
            "original_duration_minutes": optimization_meta.get(
                "original_duration_minutes"
            ),
            "optimized_distance_km": optimization_meta.get(
                "optimized_distance_km"
            ),
            "optimized_duration_minutes": optimization_meta.get(
                "optimized_duration_minutes"
            ),
            "distance_saved_km": optimization_meta.get("distance_saved_km"),
            "duration_saved_minutes": optimization_meta.get(
                "duration_saved_minutes"
            ),
            "calculation_rule": (
                "Savings are calculated only from the displayed original and "
                "optimized OSRM route results. No optimization-matrix baseline, "
                "average speed, interpolation, or estimation is used."
            )
        }
    else:
        comparison = {
            "status": "not_applicable",
            "source": "none",
            "original_order": optimization_meta.get("original_order"),
            "optimized_order": None,
            "original_distance_km": None,
            "original_duration_minutes": None,
            "optimized_distance_km": None,
            "optimized_duration_minutes": None,
            "distance_saved_km": None,
            "duration_saved_minutes": None,
            "calculation_rule": "No optimization comparison performed."
        }

    result = {
        "mode": "driving",
        "routing_service": "OSRM",
        "route_strategy": route_strategy,
        "optimized": optimized,
        "route_calculated": True,
        "stops": build_route_stop_snapshot(route_pandals),
        "total_distance_km": round(optimized_distance_km, 2),
        "total_duration_minutes": round(optimized_duration_minutes, 1),
        "legs": legs,
        "comparison": comparison,
        "optimization": optimization_meta,
        "route_note": route_note,
        "cache": {
            "enabled": True,
            "ttl_seconds": ROUTE_CACHE_TTL_SECONDS,
            "version": ROUTE_CACHE_VERSION
        }
    }

    _set_cached_route(cache_key, result)
    return result


@mcp.tool()
def plan_walking_puja_route(pandal_ids: list[str]) -> dict:
    """
    Calculate a walking route through multiple Kolkata Durga Puja pandals
    in the exact order provided.

    Uses the configured private/self-hosted Valhalla pedestrian service.

    The tool:
    - follows the supplied pandal order
    - calculates pedestrian distance and walking time when Valhalla succeeds
    - does not optimize or reorder the stops
    - returns an explicit non-calculated fallback when Valhalla is unavailable
    """

    validation_error = _validate_route_stop_count(pandal_ids)
    if validation_error:
        return validation_error

    cache_key = (
        "walking",
        tuple(str(pid).strip().lower() for pid in pandal_ids)
    )

    cached = _get_cached_route(cache_key)
    if cached is not None:
        return cached

    selected_pandals = []

    for pandal_id in pandal_ids:
        found = None

        for pandal in pandals:
            if str(pandal["id"]).lower() == str(pandal_id).lower():
                found = pandal
                break

        if found is None:
            return {
                "status": "error",
                "error": f"Pandal '{pandal_id}' was not found."
            }

        selected_pandals.append(found)

    locations = [
        {
            "lat": float(pandal["latitude"]),
            "lon": float(pandal["longitude"]),
            "type": "break"
        }
        for pandal in selected_pandals
    ]

    payload = {
        "locations": locations,
        "costing": "pedestrian",
        "units": "kilometers",
        "directions_options": {
            "units": "kilometers"
        }
    }

    data = request_valhalla_walking_route(payload)

    if "error" in data:
        return build_route_service_fallback(
            selected_pandals,
            mode="walking",
            routing_service="Valhalla",
            routing_error=data
        )

    trip = data["trip"]
    legs = trip.get("legs", [])

    leg_results = []

    for i, leg in enumerate(legs):
        summary = leg.get("summary", {})

        leg_results.append({
            "from": selected_pandals[i]["name"],
            "to": selected_pandals[i + 1]["name"],
            "distance_km": round(
                float(summary.get("length", 0)),
                2
            ),
            "duration_minutes": round(
                float(summary.get("time", 0)) / 60,
                1
            ),
            "walking_instructions": [
                maneuver.get("instruction")
                for maneuver in leg.get("maneuvers", [])
                if maneuver.get("instruction")
            ]
        })

    result = {
        "status": "success",
        "mode": "walking",
        "routing_service": "Valhalla",
        "route_strategy": "provided_order",
        "optimized": False,
        "route_calculated": True,
        "stops": build_route_stop_snapshot(selected_pandals),
        "total_stops": len(selected_pandals),
        "total_distance_km": round(
            float(trip["summary"]["length"]),
            2
        ),
        "total_duration_minutes": round(
            float(trip["summary"]["time"]) / 60,
            1
        ),
        "legs": leg_results,
        "route_note": (
            "This is a pedestrian route calculated by the configured "
            "Valhalla service. Walking distance and duration come from "
            "Valhalla. The supplied pandal order is preserved and the stops "
            "are not optimized."
        ),
        "cache": {
            "enabled": True,
            "ttl_seconds": ROUTE_CACHE_TTL_SECONDS
        }
    }

    _set_cached_route(cache_key, result)
    return result

@mcp.tool()
def search_restaurants(query: str) -> list[dict]:
    """
    Search restaurants in the local dataset by name, area, address,
    or cuisine.

    Use this tool for requests such as:
    - find a restaurant by name
    - search restaurants in an area
    - find restaurants serving a cuisine

    Only return information present in the restaurant dataset.
    Do not infer restaurant quality, popularity, current availability,
    or live operational status.

    For questions about whether a restaurant is listed as open at a
    specific date/time, use get_restaurant_availability.

    This server does not provide live restaurant availability,
    reservation availability, or real-time operational status.
    Do not claim or offer these capabilities.
    """

    query = query.strip().lower()

    if not query:
        return []

    results = []

    for restaurant in restaurants:
        searchable_text = " ".join([
            str(restaurant.get("name", "")),
            str(restaurant.get("area", "")),
            str(restaurant.get("address", "")),
            str(restaurant.get("cuisine", ""))
        ]).lower()

        if query in searchable_text:
            results.append({
                "id": restaurant["id"],
                "name": restaurant["name"],
                "latitude": restaurant["latitude"],
                "longitude": restaurant["longitude"],
                "area": restaurant.get("area"),
                "address": restaurant.get("address"),
                "cuisine": restaurant.get("cuisine"),
                "source": restaurant.get("source"),
                "check_date": restaurant.get("check_date")
    })

    return results

@mcp.tool()
def find_nearby_restaurants(
    latitude: float,
    longitude: float,
    radius_km: float = 2.0,
    limit: int = 10
) -> list[dict] | dict:
    """
    Find restaurants within a specified radius of a geographic coordinate.

    MUST be used for requests such as:
    - restaurants near a location
    - nearby restaurants
    - restaurants within X km
    - places to eat around a landmark

    Results are sorted by straight-line geographic distance.

    For requests phrased as "restaurants near Deshapriya Park" or
    "restaurants within X km of a named pandal", use
    find_nearby_restaurants_by_pandal so the pandal coordinates come
    directly from the local dataset.

    This tool does not provide live restaurant availability,
    reservation availability, or current operational status.

    Do not claim or offer live availability, reservation availability,
    or real-time operational-status checks.
    """

    if not (-90 <= latitude <= 90):
        return {"error": "Latitude must be between -90 and 90."}

    if not (-180 <= longitude <= 180):
        return {"error": "Longitude must be between -180 and 180."}

    if radius_km <= 0:
        return {"error": "radius_km must be greater than 0."}

    if limit <= 0:
        return {"error": "limit must be greater than 0."}

    results = []

    for restaurant in restaurants:
        if (
            "latitude" not in restaurant
            or "longitude" not in restaurant
        ):
            continue

        distance = haversine_distance(
            latitude,
            longitude,
            float(restaurant["latitude"]),
            float(restaurant["longitude"])
        )

        if distance <= radius_km:
            result = {
                "id": restaurant["id"],
                "name": restaurant["name"],
                "latitude": restaurant["latitude"],
                "longitude": restaurant["longitude"],
                "area": restaurant.get("area"),
                "address": restaurant.get("address"),
                "cuisine": restaurant.get("cuisine"),
                "source": restaurant.get("source"),
                "check_date": restaurant.get("check_date"),
                "distance_km": round(distance, 2)
            }
            results.append(result)

    results.sort(key=lambda x: x["distance_km"])

    return results[:limit]

@mcp.tool()
def find_nearby_restaurants_by_pandal(
    pandal_name: str,
    radius_km: float = 2.0,
    limit: int = 10
) -> list[dict] | dict:
    """
    Find restaurants within a specified radius of a named Durga Puja pandal.

    MUST be used when the user asks for restaurants:
    - near a named pandal
    - within X km of a named pandal
    - around a specific pandal

    The reference pandal is resolved by exact case-insensitive name lookup
    in the local 2026 dataset. Its coordinates are taken directly from
    that dataset. Do NOT estimate or infer coordinates from general
    knowledge, web knowledge, memory, or assumptions.

    Results are sorted by straight-line geographic distance.
    This tool does not provide live restaurant availability, reservations,
    or real-time operational status.
    """

    if not pandal_name or not pandal_name.strip():
        return {"error": "pandal_name must not be empty."}

    if radius_km <= 0:
        return {"error": "radius_km must be greater than 0."}

    if limit <= 0:
        return {"error": "limit must be greater than 0."}

    normalized_name = pandal_name.strip().casefold()
    reference_pandal = next(
        (p for p in pandals if str(p.get("name", "")).strip().casefold() == normalized_name),
        None
    )

    if reference_pandal is None:
        return {
            "error": f"Pandal named '{pandal_name}' was not found in the 2026 dataset."
        }

    reference_latitude = float(reference_pandal["latitude"])
    reference_longitude = float(reference_pandal["longitude"])

    results = []

    for restaurant in restaurants:
        if "latitude" not in restaurant or "longitude" not in restaurant:
            continue

        distance = haversine_distance(
            reference_latitude,
            reference_longitude,
            float(restaurant["latitude"]),
            float(restaurant["longitude"])
        )

        if distance <= radius_km:
            result = {
                "id": restaurant["id"],
                "name": restaurant["name"],
                "latitude": restaurant["latitude"],
                "longitude": restaurant["longitude"],
                "area": restaurant.get("area"),
                "address": restaurant.get("address"),
                "cuisine": restaurant.get("cuisine"),
                "source": restaurant.get("source"),
                "check_date": restaurant.get("check_date"),
                "distance_km": round(distance, 2)
            }

            results.append(result)

    results.sort(key=lambda x: x["distance_km"])

    return results[:limit]


def parse_time(time_text: str) -> int:
    """
    Convert HH:MM into minutes after midnight.
    Supports 24:00.
    """
    if time_text == "24:00":
        return 24 * 60

    hour, minute = map(int, time_text.split(":"))
    return hour * 60 + minute


def is_day_allowed(day_range: str, weekday: int) -> bool:
    """
    Check whether a weekday is included.

    Python weekday:
    Monday = 0
    Sunday = 6
    """

    day_numbers = {
        "Mo": 0,
        "Tu": 1,
        "We": 2,
        "Th": 3,
        "Fr": 4,
        "Sa": 5,
        "Su": 6
    }

    if day_range == "Mo-Su":
        return True

    if "-" in day_range:
        start, end = day_range.split("-")

        if start in day_numbers and end in day_numbers:
            start_num = day_numbers[start]
            end_num = day_numbers[end]

            if start_num <= end_num:
                return start_num <= weekday <= end_num

            return weekday >= start_num or weekday <= end_num

    if day_range in day_numbers:
        return weekday == day_numbers[day_range]

    return False


def restaurant_is_open(opening_hours: str, requested_dt: datetime):
    """
    Determine whether the restaurant is listed as open at the requested
    date/time according to the supported subset of OSM opening_hours.

    Returns:
        True  -> listed as open
        False -> listed as closed
        None  -> opening_hours format is unsupported or unavailable

    This is not live operational status.
    """

    if not opening_hours:
        return None

    text = opening_hours.strip()

    text = text.replace(",PH", "")

    weekday = requested_dt.weekday()
    current_minutes = requested_dt.hour * 60 + requested_dt.minute

    match = re.match(
        r"^(?:(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?)?\s*"
        r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$",
        text
    )

    if not match:
        return None

    day_start = match.group(1)
    day_end = match.group(2)
    start_time = match.group(3)
    end_time = match.group(4)

    if day_start:
        if day_end:
            day_range = f"{day_start}-{day_end}"
        else:
            day_range = day_start

        if not is_day_allowed(day_range, weekday):
            return False

    start_minutes = parse_time(start_time)
    end_minutes = parse_time(end_time)

    if end_minutes > start_minutes:
        return start_minutes <= current_minutes < end_minutes

    if end_minutes < start_minutes:
        return current_minutes >= start_minutes or current_minutes < end_minutes

    return False

@mcp.tool()
def get_restaurant_availability(
    restaurant_name: str,
    datetime_text: str
) -> dict:
    """
    Determine whether a named restaurant is listed as open at a
    specified date and time according to its recorded opening_hours.

    MUST be used when the user asks:
    - whether a named restaurant is open at a specified date/time
    - whether a named restaurant is listed as open at a specified timestamp
    - whether a named restaurant is listed as closed at a specified date/time

    Resolve the restaurant by exact case-insensitive name from the
    local restaurant dataset.

    Do not answer these date/time availability questions from general
    knowledge or from the restaurant search results alone. Use this
    tool to perform the opening-hours check.

    Datetime format:
    YYYY-MM-DD HH:MM

    This is NOT live operational status and does NOT provide live
    reservation availability.

    Possible statuses:
    - listed_as_open
    - listed_as_closed
    - unknown
    """

    if not restaurant_name or not restaurant_name.strip():
        return {
            "error": "restaurant_name must not be empty."
        }

    normalized_name = restaurant_name.strip().casefold()

    restaurant = next(
        (
            item for item in restaurants
            if str(item.get("name", "")).strip().casefold()
            == normalized_name
        ),
        None
    )

    if restaurant is None:
        return {
            "error": f"Restaurant named '{restaurant_name}' was not found."
        }

    try:
        requested_dt = datetime.strptime(
            datetime_text,
            "%Y-%m-%d %H:%M"
        )

    except ValueError:
        return {
            "error": "Invalid datetime format. Use YYYY-MM-DD HH:MM"
        }

    opening_hours = restaurant.get("opening_hours", "")

    is_open = restaurant_is_open(
        opening_hours,
        requested_dt
    )

    if is_open is True:
        status = "listed_as_open"
    elif is_open is False:
        status = "listed_as_closed"
    else:
        status = "unknown"

    return {
        "restaurant_id": restaurant["id"],
        "restaurant_name": restaurant["name"],
        "requested_datetime": datetime_text,
        "opening_hours": opening_hours,
        "status": status,
        "source": restaurant.get("source"),
        "check_date": restaurant.get("check_date"),
        "live_status_available": False
    }

@mcp.tool()
def get_live_traffic(location_query: str = "") -> str:
    """
    Retrieve recent Kolkata Police traffic advisories.

    IMPORTANT RESPONSE RULES
    - Return the prepared response text directly.
    - Do not add recommendations, introductions, conclusions, or
      follow-up offers outside the returned response.
    - Invoke this tool for traffic, congestion, road closure, and
      current road or bridge status questions.
    - An empty location_query means all active advisories.

    DATA FRESHNESS
    - Only use latest_state_per_location for current advisory results.
    - Never present historical records as current conditions.
    - An empty active dataset does not mean roads are clear, traffic
      is normal, bridges are open, or closures do not exist.
    - Do not claim no posts have been published.
    - If there is no fresh advisory, return the prepared message
      without additional explanation or recommendations.

    TRAFFIC STATUS
    - Report only facts supported by the active advisory records.
    - An existing advisory with status "unknown" is not the same as
      a missing advisory.
    - Describe normal, slow, or closed as the status indicated by the
      latest qualifying police advisory, not verified live conditions.
    - Never infer that a road is currently open or clear from an old
      announcement.
    - Never invent causes, directions, or traffic conditions.

    TIMESTAMPS
    - Distinguish advisory posting time from collection time.
    - "exact" means the source timestamp was extracted.
    - "relative" means the advisory timestamp is approximate.
    - "unknown" means the timestamp precision is not established.
    - Display collection times in UTC and IST (UTC+05:30).
    - Never describe a collection timestamp as a posting timestamp.

    SOURCES
    - Include the direct Facebook post URL when available.
    - Do not suggest alternative routes, websites, navigation apps,
      or other sources unless explicitly requested.

    Args:
        location_query: A road, bridge, or crossing, such as
            'Howrah Bridge', 'Bascule Bridge', or 'Red Road'.
            Use an empty string for all active advisories.
    """

    from datetime import datetime, timezone, timedelta

    # ---------------------------------------------------------
    # 1. Load the traffic dataset safely.
    # ---------------------------------------------------------
    if not LIVE_TRAFFIC_FILE.exists():
        return "Live traffic data is currently unavailable."

    try:
        with open(LIVE_TRAFFIC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

    except (OSError, json.JSONDecodeError):
        return "Live traffic data could not be read."

    if not isinstance(data, dict):
        return "The live traffic dataset has an invalid format."

    latest_states = data.get("latest_state_per_location")

    if not isinstance(latest_states, dict):
        return "The active traffic advisory data is unavailable or invalid."

    last_collected_at = data.get("last_collected_at")

    # ---------------------------------------------------------
    # 2. Format timestamps explicitly in UTC and IST.
    # ---------------------------------------------------------
    ist_timezone = timezone(timedelta(hours=5, minutes=30))

    def parse_timestamp(value):
        if not value:
            return None

        try:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except (ValueError, TypeError, OverflowError):
            return None

    def format_timestamp(value):
        parsed = parse_timestamp(value)

        if parsed is None:
            return None

        ist_time = parsed.astimezone(ist_timezone)

        utc_text = (
            f"{parsed.strftime('%B')} {parsed.day}, "
            f"{parsed.year}, {parsed.strftime('%H:%M')} UTC"
        )

        ist_text = ist_time.strftime("%I:%M %p IST")

        return f"{utc_text} ({ist_text})"

    collection_display = format_timestamp(last_collected_at)

    if collection_display:
        collection_line = f"Last collection: {collection_display}."
    else:
        collection_line = "Last collection time is unavailable."

    # ---------------------------------------------------------
    # 3. Prepare the no-advisory response.
    # ---------------------------------------------------------
    query = (location_query or "").strip().lower()

    if not query and not latest_states:
        return (
            "No sufficiently recent traffic advisories are available "
            "in the collected dataset.\n\n"
            f"{collection_line}\n\n"
            "The absence of recent advisories does not establish "
            "current road conditions."
        )

    # ---------------------------------------------------------
    # 4. Find the requested location, if one was specified.
    # ---------------------------------------------------------
    if query:
        matches = {
            location: state
            for location, state in latest_states.items()
            if query in location.lower()
        }

        if not matches:
            return (
                f"No fresh traffic advisory is currently available "
                f"for {location_query.strip()}.\n\n"
                f"{collection_line}\n\n"
                "The absence of a fresh advisory does not establish "
                "current road conditions."
            )
    else:
        matches = latest_states

    # ---------------------------------------------------------
    # 5. Format each active advisory into final response text.
    # ---------------------------------------------------------
    sections = []

    for location, state in matches.items():
        if not isinstance(state, dict):
            continue

        lines = [location]

        traffic_status = state.get("traffic_status", "unknown")

        if traffic_status == "unknown":
            lines.append(
                "Traffic status: Unknown; the available advisory "
                "does not establish a definite traffic condition."
            )
        else:
            lines.append(
                "Traffic status indicated by the latest qualifying "
                f"advisory: {traffic_status}."
            )

        cause = state.get("cause", "unknown")

        if cause and cause != "unknown":
            lines.append(f"Cause: {cause.replace('_', ' ')}.")

        directions = state.get("directions", {})

        if isinstance(directions, dict):
            for direction_name, direction_value in directions.items():
                if direction_value:
                    lines.append(
                        f"{direction_name.capitalize()}: "
                        f"{direction_value}."
                    )

        # Show posting time only with an honest precision label.
        posted_at = state.get("posted_at")
        precision = state.get("timestamp_precision", "unknown")
        posted_display = format_timestamp(posted_at)

        if precision == "exact" and posted_display:
            lines.append(f"Advisory posting time: {posted_display}.")
        elif precision == "relative" and posted_display:
            lines.append(
                f"Approximate advisory posting time: "
                f"{posted_display}. Reconstructed from a relative "
                "Facebook timestamp."
            )
        else:
            lines.append(
                "Advisory posting time: Precision is unknown."
            )

        advisory_text = (
            state.get("raw_clause")
            or state.get("original_message")
        )

        if advisory_text:
            lines.append(f"Police advisory: {advisory_text}")

        post_url = state.get("post_url")

        if post_url:
            lines.append(f"Official source: {post_url}")

        sections.append("\n".join(lines))

    if not sections:
        return (
            "No sufficiently recent traffic advisories are available "
            "in the collected dataset.\n\n"
            f"{collection_line}\n\n"
            "The absence of recent advisories does not establish "
            "current road conditions."
        )

    # ---------------------------------------------------------
    # 6. Return the final text, not a dictionary for Claude
    #    to interpret and expand.
    # ---------------------------------------------------------
    heading = (
        "Active Kolkata traffic advisories:"
        if not query
        else "Traffic advisory result:"
    )

    return (
        f"{heading}\n\n"
        + "\n\n".join(sections)
        + f"\n\n{collection_line}"
    )

@mcp.tool()
def get_planned_puja_routes() -> list[dict]:
    """
    Return the curated 2026 Puja routes from the planned route dataset.

    Use this tool when the user asks:
    - what planned Puja routes are available
    - what Puja circuits/routes are available
    - to see the available Kolkata Puja routes

    The routes are sourced from the project's static planned-route dataset.
    """
    return planned_routes

def _weather_location_key(latitude: float, longitude: float) -> str:
    return f"{float(latitude):.5f},{float(longitude):.5f}"


def fetch_weather_for_locations(
    locations: list[dict],
    forecast_hours: int = 8
) -> dict:
    """
    Fetch hourly rainfall forecasts for multiple pandal coordinates.

    Forecast assessment is based on the next forecast_hours at each
    pandal location, not on every road segment between pandals.
    """

    if not 1 <= forecast_hours <= 24:
        return {
            "status": "error",
            "error": "forecast_hours must be between 1 and 24."
        }

    now_ist = datetime.now(KOLKATA_TIMEZONE)
    now_local = now_ist.replace(tzinfo=None)

    window_start = now_local.replace(
        minute=0, second=0, microsecond=0
    )
    window_end = window_start + timedelta(hours=forecast_hours)

    def unavailable(message: str) -> dict:
        return {
            "status": "unavailable",
            "error": message,
            "source": "Open-Meteo"
        }

    # Deduplicate locations by coordinate.
    unique_locations = {}

    try:
        for location in locations:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])

            if (
                not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                return {
                    "status": "error",
                    "error": "A location has invalid coordinates."
                }

            key = _weather_location_key(latitude, longitude)
            unique_locations.setdefault(
                key, (latitude, longitude)
            )

    except (KeyError, TypeError, ValueError):
        return {
            "status": "error",
            "error": "Invalid location records."
        }

    if not unique_locations:
        return unavailable("No candidate pandal locations were supplied.")

    # Remove expired cache entries.
    monotonic_now = time.monotonic()

    for cache_key, (cached_at, _) in list(WEATHER_CACHE.items()):
        if monotonic_now - cached_at > WEATHER_CACHE_TTL_SECONDS:
            WEATHER_CACHE.pop(cache_key, None)

    forecasts_by_location = {}
    pending = []

    # Reuse fresh forecasts; fetch only uncached coordinates.
    for key, (latitude, longitude) in unique_locations.items():
        cache_key = (
            key,
            window_start.isoformat(),
            forecast_hours
        )

        cached = WEATHER_CACHE.get(cache_key)

        if cached is not None:
            forecasts_by_location[key] = cached[1]
        else:
            pending.append(
                (key, latitude, longitude, cache_key)
            )

    # Open-Meteo supports multiple coordinates per request.
    for offset in range(0, len(pending), WEATHER_BATCH_SIZE):
        batch = pending[offset:offset + WEATHER_BATCH_SIZE]

        params = {
            "latitude": ",".join(
                f"{item[1]:.5f}" for item in batch
            ),
            "longitude": ",".join(
                f"{item[2]:.5f}" for item in batch
            ),
            "hourly": (
                "precipitation_probability,precipitation,rain"
            ),
            "forecast_days": 2,
            "timezone": "Asia/Kolkata"
        }

        url = f"{OPEN_METEO_URL}?{urlencode(params)}"

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Kolkata-Puja-Tourist-MCP/1.0"
            }
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(
                    response.read().decode("utf-8")
                )

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            UnicodeDecodeError
        ) as exc:
            for key, _, _, _ in batch:
                forecasts_by_location[key] = unavailable(
                    f"Weather request failed: {exc}"
                )
            continue

        if isinstance(payload, list):
            response_items = payload
        elif isinstance(payload, dict) and len(batch) == 1:
            response_items = [payload]
        else:
            response_items = []

        # Multiple-coordinate responses must match the requested order.
        if len(response_items) != len(batch):
            for key, _, _, _ in batch:
                forecasts_by_location[key] = unavailable(
                    "Weather response did not match the requested locations."
                )
            continue

        for item, (key, latitude, longitude, cache_key) in zip(
            response_items, batch
        ):
            hourly = item.get("hourly", {})

            times = hourly.get("time", [])
            probabilities = hourly.get(
                "precipitation_probability", []
            )
            precipitation = hourly.get("precipitation", [])
            rain_values = hourly.get("rain", [])

            selected_hours = []

            for index, timestamp in enumerate(times):
                try:
                    forecast_time = datetime.fromisoformat(timestamp)

                    if forecast_time.tzinfo is not None:
                        forecast_time = (
                            forecast_time
                            .astimezone(KOLKATA_TIMEZONE)
                            .replace(tzinfo=None)
                        )

                except (TypeError, ValueError):
                    continue

                if not window_start <= forecast_time < window_end:
                    continue

                probability = (
                    probabilities[index]
                    if index < len(probabilities) else None
                )
                precipitation_mm = (
                    precipitation[index]
                    if index < len(precipitation) else None
                )
                rain_mm = (
                    rain_values[index]
                    if index < len(rain_values) else None
                )

                selected_hours.append({
                    "time": timestamp,
                    "probability": probability,
                    "precipitation_mm": precipitation_mm,
                    "rain_mm": rain_mm
                })

            if not selected_hours:
                forecasts_by_location[key] = unavailable(
                    "No hourly forecasts were returned for the requested window."
                )
                continue

            probability_values = [
                hour["probability"]
                for hour in selected_hours
                if isinstance(hour["probability"], (int, float))
            ]

            precipitation_values = [
                hour["precipitation_mm"]
                for hour in selected_hours
                if isinstance(hour["precipitation_mm"], (int, float))
            ]

            rain_hour_count = sum(
                1 for hour in selected_hours
                if (
                    (
                        isinstance(hour["probability"], (int, float))
                        and hour["probability"] >= 50
                    )
                    or (
                        isinstance(hour["precipitation_mm"], (int, float))
                        and hour["precipitation_mm"] >= 0.1
                    )
                    or (
                        isinstance(hour["rain_mm"], (int, float))
                        and hour["rain_mm"] >= 0.1
                    )
                )
            )

            if not probability_values and not precipitation_values:
                forecasts_by_location[key] = unavailable(
                    "Forecast contained no usable rain values."
                )
                continue

            result = {
                "status": "success",
                "source": "Open-Meteo",
                "latitude": latitude,
                "longitude": longitude,
                "forecast_hours": forecast_hours,
                "rain_expected": rain_hour_count > 0,
                "rain_signal_hours": rain_hour_count,
                "mean_precipitation_probability_percent": (
                    round(sum(probability_values) / len(probability_values), 1)
                    if probability_values else None
                ),
                "max_precipitation_probability_percent": (
                    max(probability_values)
                    if probability_values else None
                ),
                "total_forecast_precipitation_mm": round(
                    sum(precipitation_values), 2
                ),
                "forecast_window_start_local": window_start.isoformat(),
                "forecast_window_end_local": window_end.isoformat()
            }

            WEATHER_CACHE[cache_key] = (
                time.monotonic(),
                result
            )

            forecasts_by_location[key] = result

    statuses = [
        forecast.get("status")
        for forecast in forecasts_by_location.values()
    ]

    if statuses and all(status == "success" for status in statuses):
        overall_status = "success"
    elif any(status == "success" for status in statuses):
        overall_status = "partial"
    else:
        overall_status = "unavailable"

    return {
        "status": overall_status,
        "source": "Open-Meteo",
        "forecast_hours": forecast_hours,
        "forecast_window_start_local": window_start.isoformat(),
        "forecast_window_end_local": window_end.isoformat(),
        "locations": forecasts_by_location,
        "limitations": (
            "Forecasts describe weather at pandal coordinates, not every "
            "road segment. They do not establish flooding, road closures, "
            "or whether a route is safe."
        )
    }

@mcp.tool()
def recommend_puja_route(
    latitude: float,
    longitude: float
) -> dict:
    """
    Recommend a planned Kolkata Puja route using proximity and weather.

    Selection logic:
    - Find planned routes and rotate each route to start at its nearest
      pandal relative to the user's location.
    - Fetch hourly rain forecasts for pandal stops on candidate routes.
    - When rain is forecast and all candidate forecasts are complete,
      rank routes by rain exposure, precipitation probability, and
      forecast precipitation.
    - Use planned duration and proximity as tie-breakers.
    - If weather data is unavailable or incomplete, retain the original
      nearest-pandal selection rule.

    WEATHER INTERPRETATION
    - Base route selection explanations only on the returned
      weather_context, weather_assessment, and selection_reason.
    - Do not claim weather changed the recommendation unless
      weather_adjustment_applied is true.
    - A user's hypothetical assumption that it is raining does
      not override the actual forecast returned by the weather tool.
    - If the user requests a hypothetical rainy scenario, explain
      that the production recommendation uses forecast data.
    - Never claim a route is sheltered, flood-free, or safer
      unless reliable data supports that claim.    

    OSRM remains responsible for calculating actual driving routes.
    Weather forecasts do not establish flooding, road closures, or
    road safety. This tool does not optimize OSRM driving distances.
    """

    # ---------------------------------------------------------
    # 1. Validate the user's coordinates.
    # ---------------------------------------------------------
    if not (-90 <= latitude <= 90):
        return {
            "status": "error",
            "message": "Latitude must be between -90 and 90."
        }

    if not (-180 <= longitude <= 180):
        return {
            "status": "error",
            "message": "Longitude must be between -180 and 180."
        }

    # ---------------------------------------------------------
    # 2. Build a fast lookup of the pandal dataset.
    # ---------------------------------------------------------
    pandal_by_id = {
        str(p["id"]).strip().lower(): p
        for p in pandals
    }

    route_candidates = []

    # ---------------------------------------------------------
    # 3. Build each candidate route, preserving its planned order.
    # ---------------------------------------------------------
    for route in planned_routes:
        route_id = route.get("route_id")
        route_name = route.get("route_name")
        route_pandal_ids = route.get("pandal_ids", [])

        if not route_id or not route_name:
            continue

        valid_route_ids = [
            pandal_id
            for pandal_id in route_pandal_ids
            if str(pandal_id).strip().lower() in pandal_by_id
        ]

        if not valid_route_ids:
            continue

        nearest_index = None
        nearest_pandal = None
        nearest_distance = float("inf")

        for index, pandal_id in enumerate(valid_route_ids):
            pandal = pandal_by_id[
                str(pandal_id).strip().lower()
            ]

            distance = haversine_distance(
                latitude,
                longitude,
                float(pandal["latitude"]),
                float(pandal["longitude"])
            )

            if distance < nearest_distance:
                nearest_distance = distance
                nearest_pandal = pandal
                nearest_index = index

        if nearest_pandal is None or nearest_index is None:
            continue

        # Rotate the original route to start at its nearest pandal.
        recommended_pandal_ids = (
            valid_route_ids[nearest_index:]
            + valid_route_ids[:nearest_index]
        )

        recommended_stops = []

        for stop_number, pandal_id in enumerate(
            recommended_pandal_ids,
            start=1
        ):
            pandal = pandal_by_id[
                str(pandal_id).strip().lower()
            ]

            recommended_stops.append({
                "stop_number": stop_number,
                "pandal_id": pandal["id"],
                "pandal_name": pandal["name"],
                "latitude": float(pandal["latitude"]),
                "longitude": float(pandal["longitude"])
            })

        route_candidates.append({
            "route_id": route_id,
            "route_name": route_name,

            "nearest_pandal": {
                "pandal_id": nearest_pandal["id"],
                "pandal_name": nearest_pandal["name"],
                "latitude": nearest_pandal["latitude"],
                "longitude": nearest_pandal["longitude"]
            },

            "distance_to_nearest_pandal_km": round(
                nearest_distance, 2
            ),
            "distance_type": "straight_line_geographic",
            "distance_source": "Haversine",
            "route_calculated": False,

            "total_stops": len(recommended_stops),
            "must_see_count": route.get("must_see_count"),
            "estimated_duration_hours": route.get(
                "estimated_duration_hours"
            ),

            "recommended_stops": recommended_stops,
            "recommended_pandal_ids": recommended_pandal_ids,

            "source": route.get("source"),
            "source_url": route.get("source_url"),

            "route_order_strategy": (
                "Start at the pandal nearest to the user, then continue "
                "in the original planned-route order. This is a planned "
                "sequence, not a shortest-driving-route optimization."
            ),

            "route_optimization": {
                "optimized_for_shortest_driving_distance": False,
                "optimized_by_osrm": False,
                "original_planned_order_preserved": True
            },

            "walking_information": {
                "walking_distance_available": False,
                "walking_route_available": False
            },

            "visit_time_information": {
                "per_pandal_visit_time_provided": False,
                "duration_source": "planned_route_source",
                "duration_is_routing_time": False,
                "duration_note": (
                    "The estimated duration comes from the planned-route "
                    "dataset. It is not OSRM driving time or Valhalla "
                    "walking time."
                )
            },

            # Filled in by the weather-assessment step.
            "weather_assessment": {
                "status": "pending"
            }
        })

    if not route_candidates:
        return {
            "status": "error",
            "message": (
                "No valid planned Puja route could be matched "
                "to the pandal dataset."
            )
        }

    # ---------------------------------------------------------
    # 4. Fetch weather forecasts for all unique candidate stops.
    # ---------------------------------------------------------
    weather_locations = [
        stop
        for candidate in route_candidates
        for stop in candidate["recommended_stops"]
    ]

    weather_context = fetch_weather_for_locations(
        weather_locations,
        forecast_hours=8
    )

    forecasts_by_location = weather_context.get(
        "locations", {}
    )

    # ---------------------------------------------------------
    # 5. Calculate a weather assessment for each candidate route.
    # ---------------------------------------------------------
    for candidate in route_candidates:
        stops = candidate["recommended_stops"]
        stop_forecasts = []
        unavailable_stops = []

        for stop in stops:
            location_key = _weather_location_key(
                stop["latitude"],
                stop["longitude"]
            )

            forecast = forecasts_by_location.get(location_key)

            if (
                isinstance(forecast, dict)
                and forecast.get("status") == "success"
                and isinstance(
                    forecast.get("rain_expected"), bool
                )
                and isinstance(
                    forecast.get(
                        "total_forecast_precipitation_mm"
                    ),
                    (int, float)
                )
            ):
                stop_forecasts.append(forecast)
            else:
                unavailable_stops.append(stop["pandal_name"])

        # Do not compare incomplete route forecasts.
        if not stops or len(stop_forecasts) != len(stops):
            candidate["weather_assessment"] = {
                "status": "unavailable",
                "assessed_stops": len(stop_forecasts),
                "total_stops": len(stops),
                "unavailable_stops": unavailable_stops,
                "reason": (
                    "Weather data was not available for every "
                    "pandal on this route."
                )
            }
            continue

        rainy_stops = sum(
            1
            for forecast in stop_forecasts
            if forecast["rain_expected"]
        )

        probabilities = [
            forecast[
                "mean_precipitation_probability_percent"
            ]
            for forecast in stop_forecasts
            if isinstance(
                forecast.get(
                    "mean_precipitation_probability_percent"
                ),
                (int, float)
            )
        ]

        mean_probability = (
            round(
                sum(probabilities) / len(probabilities),
                1
            )
            if probabilities else None
        )

        mean_precipitation = round(
            sum(
                float(
                    forecast["total_forecast_precipitation_mm"]
                )
                for forecast in stop_forecasts
            ) / len(stop_forecasts),
            2
        )

        candidate["weather_assessment"] = {
            "status": "success",
            "source": "Open-Meteo",
            "forecast_hours": 8,
            "total_pandal_stops": len(stops),
            "rain_signal_stops": rainy_stops,
            "rain_signal_stop_fraction": round(
                rainy_stops / len(stops),
                3
            ),
            "mean_precipitation_probability_percent": (
                mean_probability
            ),
            "mean_precipitation_mm_per_stop": (
                mean_precipitation
            ),
            "assessment_basis": (
                "Forecast rainfall at pandal coordinates during "
                "the next eight hours. Weather between stops is "
                "not assessed."
            )
        }

    # ---------------------------------------------------------
    # 6. Decide whether weather can influence route selection.
    # ---------------------------------------------------------
    weather_complete = all(
        candidate["weather_assessment"].get("status") == "success"
        for candidate in route_candidates
    )

    rain_expected = (
        weather_complete
        and any(
            candidate["weather_assessment"]["rain_signal_stops"] > 0
            for candidate in route_candidates
        )
    )

    weather_adjustment_applied = False

    def planned_duration_key(route):
        """Return a numeric duration for tie-breaking."""
        duration = route.get("estimated_duration_hours")

        try:
            value = float(duration)
            if math.isfinite(value) and value >= 0:
                return value
        except (TypeError, ValueError):
            pass

        return float("inf")

    if rain_expected:
        # Rank by forecast rain exposure first. A lower score is preferred.
        # Planned duration and proximity only break ties.
        def weather_route_key(route):
            assessment = route["weather_assessment"]

            mean_probability = assessment[
                "mean_precipitation_probability_percent"
            ]

            probability_key = (
                float(mean_probability)
                if mean_probability is not None
                else float("inf")
            )

            return (
                assessment["rain_signal_stop_fraction"],
                probability_key,
                assessment["mean_precipitation_mm_per_stop"],
                planned_duration_key(route),
                route["distance_to_nearest_pandal_km"]
            )

        route_candidates.sort(key=weather_route_key)
        weather_adjustment_applied = True

        selection_reason = (
            "Rain is forecast at one or more candidate pandal stops. "
            "Routes were ranked by the proportion of stops with a rain "
            "signal, mean precipitation probability, and mean forecast "
            "precipitation per stop. Planned itinerary duration and "
            "proximity break ties. This does not establish that a route "
            "is sheltered, flood-free, or safer."
        )

    else:
        # Preserve existing behaviour if forecasts are unavailable,
        # incomplete, or show no configured rain signal.
        route_candidates.sort(
            key=lambda route: (
                route["distance_to_nearest_pandal_km"]
            )
        )

        if weather_complete:
            selection_reason = (
                "No configured rain signal was detected at the candidate "
                "pandal stops in the forecast window. The original "
                "nearest-pandal selection rule was retained."
            )
        else:
            selection_reason = (
                "Weather data was unavailable or incomplete for one or "
                "more candidate routes. The original nearest-pandal "
                "selection rule was retained without weather adjustment."
            )

    recommended = route_candidates[0]

    # ---------------------------------------------------------
    # 7. Return the recommendation, evidence, and limitations.
    # ---------------------------------------------------------
    return {
        "status": "success",

        "user_location": {
            "latitude": latitude,
            "longitude": longitude
        },

        "recommended_route": recommended,
        "other_route_options": route_candidates[1:],

        "weather_context": {
            "status": weather_context.get("status", "unavailable"),
            "source": "Open-Meteo",
            "forecast_hours": 8,
            "forecast_window_start_local": weather_context.get(
                "forecast_window_start_local"
            ),
            "forecast_window_end_local": weather_context.get(
                "forecast_window_end_local"
            ),
            "weather_adjustment_applied": (
                weather_adjustment_applied
            ),
            "limitations": (
                "Weather is assessed at planned pandal stops, not along "
                "every road segment. OSRM distances and durations are "
                "not weather-adjusted. Rain forecasts do not establish "
                "flooding, road closures, shelter availability, or safety."
            )
        },

        "selection_reason": selection_reason,

        "usage_note": (
            "Use plan_puja_route() with recommended_pandal_ids to "
            "calculate the actual OSRM driving route. Use "
            "plan_walking_puja_route() with the same IDs to calculate "
            "the actual Valhalla walking route. These routing services "
            "do not automatically account for weather."
        )
    }

@mcp.tool()
def get_latest_pandal_update(pandal_name: str) -> dict:
    """
    Return the latest verified Facebook update for a Kolkata Durga Puja pandal
    by its exact pandal name (case-insensitive).

    MUST be used when the user asks for:
    - latest pandal update
    - latest Facebook update
    - recent Facebook post
    - latest pandal announcement
    - latest pandal images
    - recent pandal photos
    - latest update for a named pandal

    IMPORTANT:
    - Use the pandal NAME, not the pandal ID.
    - Match the pandal name exactly, ignoring case and surrounding spaces.
    - Read only from the dynamic 2026 Facebook update dataset.
    - Do not search Facebook directly.
    - Do not use the static pandal dataset for latest-update questions.
    - Do not invent, infer, or supplement information.

    TRANSLATION RULE:
    - latest_update_original contains the original Facebook post text.
    - If that text is already in English, return it unchanged.
    - If it is in Bengali, Hindi, or another language, translate it
      faithfully into English.
    - Preserve the original meaning, names, hashtags, mentions, and important
      wording.
    - Do not summarize or add interpretation.
    - Do not translate proper names or hashtags unnecessarily.

    IMAGE RULE:
    - verified_images contains images that passed the configured
      vision-validation process.
    - These images belong to the selected current Facebook post.
    - Do NOT describe, interpret, or summarize image contents.
    - Do NOT infer that the image shows an idol, crowd, decorations,
      construction, preparation work, people, lighting, or any other
      visual detail.
    - "Vision validated" only means the image passed the pandal-image
      validation criteria.
    - In the final answer, state only that verified images are available
      and provide their URLs when appropriate.

    TIMESTAMP RULE:
    - latest_post_date = Facebook post publication time.
    - last_scraped_at = time the scraper processed the Facebook page.
    - data_last_run = time the dynamic dataset was last written.
    - Always prefer the exact timestamp.
    - Do NOT say "today", "earlier today", "yesterday", "recently",
      "posted earlier", or similar relative-time phrases unless the
      comparison with the actual current date/time has been verified.
    - Do NOT say that a post was newly published just because the scraper
      ran recently.

    FRESHNESS RULE:
    - The scraper uses a 1-day / 24-hour freshness window.
    - If there is no qualifying recent Facebook post, return
      status "no_recent_update".
    - Do not return old post information or old images as current.

    RESPONSE RULE:
    - Present the original Facebook text.
    - Provide an English translation only when the original is not already
      English.
    - Provide the exact Facebook post date/time.
    - Provide the Facebook post URL.
    - Report the number of verified images and their URLs when requested.
    - Never add an invented image description.
    - Never add unsupported claims about how recent the post is.

    STRICT NO-INFERENCE RULE:
    - Do not add a concluding sentence, interpretation, opinion, or summary
      after reporting the Facebook post.
    - Do not say that the pandal is "actively sharing updates".
    - Do not say that preparations are "underway", "ongoing", or similar
      unless those words or their direct meaning are explicitly present in
      latest_update_original and are being presented as part of the post
      content.
    - Do not add statements such as "the festival approaches", "the pandal
      is getting ready", "the organizers are excited", or any other contextual
      interpretation unless explicitly stated in the source.
    - Report only the Facebook post text, faithful English translation when
      needed, exact timestamp, post URL, and verified image availability.
    - End the answer after the supported facts. Do not add a general summary.

    Args:
        pandal_name: Exact pandal name, case-insensitive.
                     Example: "Dumdum Park Bharat Chakra"
    """

    if not PANDAL_UPDATES_FILE.exists():
        return {
            "status": "unavailable",
            "error": "Dynamic pandal update data is currently unavailable."
        }

    try:
        with open(PANDAL_UPDATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {
            "status": "unavailable",
            "error": f"Failed to read dynamic pandal update data: {e}"
        }

    records = data.get("records", [])

    requested_name = str(pandal_name).strip().casefold()

    if not requested_name:
        return {
            "status": "invalid_request",
            "error": "pandal_name must not be empty."
        }

    for record in records:
        record_name = str(record.get("name", "")).strip().casefold()

        if record_name != requested_name:
            continue

        scrape_status = record.get("scrape_status", "unknown")

        # No Facebook post within the configured 1-day freshness window.
        if scrape_status == "success_no_recent_post":
            return {
                "status": "no_recent_update",
                "pandal_name": record.get("name"),
                "latest_update_original": None,
                "latest_post_date": None,
                "latest_post_url": None,
                "verified_images": [],
                "last_scraped_at": record.get("last_scraped_at"),
                "data_last_run": data.get("last_run"),
                "freshness_window_days": record.get(
                    "freshness_window_days",
                    1
                )
            }

        return {
            "status": scrape_status,
            "pandal_name": record.get("name"),
            "pandal_id": record.get("pandal_id"),

            # Original Facebook post text.
            "latest_update_original": record.get("latest_update"),

            # Translation should be performed by the assistant only when
            # the original text is not already English.
            "translation_required": True,
            "translation_rule": (
                "If latest_update_original is already English, return it "
                "unchanged. Otherwise translate it faithfully into English. "
                "Do not summarize or add information."
            ),

            "latest_post_date": record.get("latest_post_date"),
            "latest_post_url": record.get("latest_post_url"),

            # Groq-approved images from the selected current Facebook post.
            "verified_images": record.get("images", []),

            "image_rule": (
                "These images passed vision validation. Do not describe or "
                "infer their visual contents."
            ),

            "last_scraped_at": record.get("last_scraped_at"),
            "data_last_run": data.get("last_run"),
            "freshness_window_days": record.get(
                "freshness_window_days",
                1
            )
        }

    return {
        "status": "not_found",
        "error": (
            f"No dynamic Facebook record found for pandal "
            f"named '{pandal_name}'. Use the exact pandal name."
        )
    }

@mcp.tool()
def get_puja_schedule(
    query: str = "",
    event_date: str = "",
) -> dict:
    """
    Search the Kolkata Durga Puja 2026 general ritual schedule.

    MUST be used for questions about ritual dates and timing windows,
    including Mahalaya, Shashthi, Saptami, Anjali, Pushpanjali,
    Kumari Puja, Sandhi Puja, Bhog, Aarti and Vijaya Dashami.

    Use query for a ritual or festival name.
    Use event_date in YYYY-MM-DD format when a date is specified.

    Preserve separate Panjika traditions and their different times.
    Return source information with matching records.
    Do not invent dates or exact timings.

    Individual pandal schedules must be verified separately.
    Do not present a general Panjika timing as a confirmed local
    pandal schedule.

    For questions about exact ritual timings at a nearby pandal,
    also use get_nearby_pandal_official_pages() to find its
    verified official Facebook page.

    Combine the general Panjika timing with the official page
    in the final answer. Clearly distinguish general timing
    from confirmed local timing.

    Never claim that a pandal will publish its schedule at a
    particular time unless a verified source supports that claim.
    """

    query_text = str(query or "").strip().casefold()
    date_text = str(event_date or "").strip()

    ignored_words = {
        "what", "when", "where", "is", "are", "the", "a", "an",
        "for", "to", "of", "at", "on", "in", "me", "please",
        "tell", "show", "give", "find", "get", "schedule",
        "timing", "timings", "time", "date", "dates", "about",
        "puja", "ritual", "2026", "during", "according", "each",
        "panjika", "window", "windows","maha", "this", "year", 
        "heard", "differs", "between", "usual", "usually", "kolkata",
    }

    query_terms = [
        word
        for word in re.findall(r"[a-z0-9]+", query_text)
        if word not in ignored_words
    ]

    def record_matches(record: dict) -> bool:
        record_text = json.dumps(
            record,
            ensure_ascii=False,
        ).casefold()

        if date_text and date_text not in record_text:
            return False

        return all(term in record_text for term in query_terms)

    def collect_source_ids(value) -> list[str]:
        found = []

        if isinstance(value, dict):
            source_id = value.get("source_id")
            if isinstance(source_id, str):
                found.append(source_id)

            source_ids = value.get("source_ids")
            if isinstance(source_ids, list):
                found.extend(
                    item for item in source_ids
                    if isinstance(item, str)
                )

            for child in value.values():
                found.extend(collect_source_ids(child))

        elif isinstance(value, list):
            for child in value:
                found.extend(collect_source_ids(child))

        return list(dict.fromkeys(found))

    sources_by_id = {
        source["source_id"]: source
        for source in puja_schedule_data.get("sources", [])
        if isinstance(source, dict) and source.get("source_id")
    }

    festival_results = [
        record
        for record in puja_schedule_data.get("festival_dates", [])
        if isinstance(record, dict) and record_matches(record)
    ]

    ritual_results = [
        record
        for record in puja_schedule_data.get("rituals", [])
        if isinstance(record, dict) and record_matches(record)
    ]

    matched_records = festival_results + ritual_results

    results = []
    for record in matched_records:
        source_ids = collect_source_ids(record)
        results.append({
            "record": record,
            "sources": [
                sources_by_id[source_id]
                for source_id in source_ids
                if source_id in sources_by_id
            ],
        })

    return {
        "dataset": puja_schedule_data.get("dataset_name"),
        "timezone": puja_schedule_data.get("timezone", "Asia/Kolkata"),
        "query": query,
        "event_date": event_date,
        "result_count": len(results),
        "results": results,
        "source_policy": puja_schedule_data.get("source_policy", {}),
        "message": (
            "Matching schedule records found."
            if results
            else (
                "No matching schedule was found in the dataset. "
                "Do not guess a date or time."
            )
        ),
    }

@mcp.tool()
def get_nearby_pandal_official_pages(
    latitude: float,
    longitude: float,
    limit: int = 5,
) -> dict:
    """
    Find nearby Kolkata Durga Puja pandals and their verified
    official Facebook pages.

    MUST be used when a user asks for the nearest pandal's
    official page or needs an official link for an exact local
    Puja schedule.

    Use the user's supplied coordinates or coordinates resolved
    from a matching record in the local pandal dataset.
    Never guess coordinates.

    Only return Facebook URLs marked facebook_verified=true
    in the official-page mapping.

    A verified page does not prove that the exact ritual timing
    has been published. Do not invent a local schedule.

    Distances are straight-line geographic distances, not
    walking or road distances.

    When the user asks about a ritual at a nearby pandal,
    also use get_puja_schedule() to retrieve the general
    Panjika timing.

    Return the complete verified Facebook URL when available.
    Do not invent exact local ritual times.
    """

    if not (
        math.isfinite(latitude)
        and -90 <= latitude <= 90
    ):
        return {"error": "Invalid latitude."}

    if not (
        math.isfinite(longitude)
        and -180 <= longitude <= 180
    ):
        return {"error": "Invalid longitude."}

    if not isinstance(limit, int) or not 1 <= limit <= 10:
        return {"error": "limit must be between 1 and 10."}

    results = []

    for pandal in pandals:
        try:
            pandal_lat = float(pandal["latitude"])
            pandal_lon = float(pandal["longitude"])
        except (KeyError, TypeError, ValueError):
            continue

        if (
            not math.isfinite(pandal_lat)
            or not math.isfinite(pandal_lon)
            or not -90 <= pandal_lat <= 90
            or not -180 <= pandal_lon <= 180
        ):
            continue

        distance_km = haversine_distance(
            latitude,
            longitude,
            pandal_lat,
            pandal_lon,
        )

        page = official_pandal_pages_by_id.get(
            str(pandal.get("id", "")).strip().casefold()
        )

        results.append({
            "pandal_id": pandal.get("id"),
            "pandal_name": pandal.get("name"),
            "area": pandal.get("area"),
            "address": pandal.get("address"),
            "distance_km": round(distance_km, 2),
            "distance_type": "straight_line_geographic",
            "official_facebook_page": (
                page["facebook_page"] if page else None
            ),
            "facebook_verified": page is not None,
            "page_verified_at": (
                page["verified_at"] if page else None
            ),
        })

    results.sort(key=lambda item: item["distance_km"])

    nearest = results[:limit]

    nearest_with_page = next(
        (
            item for item in results
            if item["facebook_verified"]
        ),
        None,
    )

    return {
        "status": "success",
        "user_location": {
            "latitude": latitude,
            "longitude": longitude,
        },
        "nearest_pandals": nearest,
        "nearest_pandal_with_verified_page": nearest_with_page,
        "message": (
            "These are the closest pandals by straight-line distance. "
            "Only verified Facebook URLs are provided. Check the page "
            "for a published ritual schedule; an exact local time is "
            "not guaranteed to be available."
        ),
    }