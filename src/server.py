import json
import heapq
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

# Official Metro Railway Kolkata special-service schedule for Durga Puja 2026.
# This is a date-specific notice, not a complete regular timetable.
METRO_PUJA_SPECIAL_SERVICES_FILE = (
    BASE_DIR / "data" / "static" / "metro_puja_special_services_2026.json"
)

# Station graph used by the date-aware Metro-only journey planner.
# This file is read at query time so network updates do not require code edits.
METRO_NETWORK_FILE = BASE_DIR / "data" / "static" / "metro_network_2026.json"

OFFICIAL_PANDAL_PAGES_FILE = (
    BASE_DIR
    / "data"
    / "static"
    / "official_pandal_facebook_pages_2026.json"
)

# Dynamic road-traffic-derived area-busyness snapshots from the TomTom collector.
# These files are read at query time, so a successful collector refresh is
# visible without restarting the MCP server.
CROWD_PANDALS_FILE = BASE_DIR / "data" / "dynamic" / "enriched_pandals.json"
CROWD_STATIONS_FILE = BASE_DIR / "data" / "dynamic" / "enriched_stations.json"
CROWD_REFRESH_STATUS_FILE = BASE_DIR / "data" / "dynamic" / "crowd_refresh_status.json"
CROWD_DATA_MAX_AGE_MINUTES = int(
    os.environ.get("CROWD_DATA_MAX_AGE_MINUTES", "90")
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
ROUTE_CACHE_VERSION = os.environ.get("ROUTE_CACHE_VERSION", "2026-10-10-crowd-context-v1")

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

with open(METRO_PUJA_SPECIAL_SERVICES_FILE, "r", encoding="utf-8") as f:
    metro_puja_special_services_data = json.load(f)

if not isinstance(metro_puja_special_services_data, dict) or not isinstance(
    metro_puja_special_services_data.get("lines"), list
):
    raise ValueError(
        "Metro Puja special-services JSON must be an object containing a 'lines' list."
    )

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


# ---------------------------------------------------------------------------
# Dynamic road-traffic-derived area busyness
# ---------------------------------------------------------------------------
def _read_enriched_snapshot(path: Path) -> list[dict] | dict:
    """Read a current enriched snapshot at query time (no restart required)."""
    try:
        with path.open("r", encoding="utf-8") as f:
            records = json.load(f)
    except FileNotFoundError:
        return {
            "status": "unavailable",
            "error": f"Dynamic busyness data has not been generated yet: {path.name}",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "error": f"Could not read dynamic busyness data from {path.name}: {exc}",
        }

    if not isinstance(records, list):
        return {
            "status": "unavailable",
            "error": f"Unexpected format in {path.name}; expected a JSON list.",
        }
    return records


def _lookup_enriched_record(records: list[dict], query: str) -> dict:
    """Resolve by exact ID/name first, then by an unambiguous substring."""
    needle = str(query or "").strip().casefold()
    if not needle:
        return {
            "status": "error",
            "error": "A pandal or Metro station name/ID is required.",
        }

    valid_records = [row for row in records if isinstance(row, dict)]
    exact = [
        row for row in valid_records
        if str(row.get("id", "")).strip().casefold() == needle
        or str(row.get("name", "")).strip().casefold() == needle
    ]
    if len(exact) == 1:
        return {"status": "found", "record": exact[0]}
    if len(exact) > 1:
        return {
            "status": "ambiguous",
            "query": query,
            "matches": [{"id": row.get("id"), "name": row.get("name")} for row in exact[:10]],
            "message": "The exact query matches multiple records; specify an ID.",
        }

    partial = [
        row for row in valid_records
        if needle in str(row.get("id", "")).casefold()
        or needle in str(row.get("name", "")).casefold()
    ]
    if len(partial) == 1:
        return {"status": "found", "record": partial[0]}
    if len(partial) > 1:
        return {
            "status": "ambiguous",
            "query": query,
            "matches": [{"id": row.get("id"), "name": row.get("name")} for row in partial[:10]],
            "message": "Several records match; specify the exact name or ID.",
        }
    return {"status": "not_found", "query": query}


def _make_area_busyness_response(record: dict, entity_type: str, source_path: Path) -> dict:
    """Format data and explicitly label its proxy nature and freshness."""
    detail = record.get("area_busyness_detail")
    if not isinstance(detail, dict):
        detail = {}

    measured_at = detail.get("measured_at")
    age_minutes = None
    freshness = "unknown"
    if isinstance(measured_at, str) and measured_at.strip():
        try:
            parsed = datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Collector timestamps are normally timezone-aware. For old
                # timestamps without an offset, assume Kolkata local time.
                parsed = parsed.replace(tzinfo=KOLKATA_TIMEZONE)
            age_minutes = max(
                0.0,
                (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 60,
            )
            freshness = "fresh" if age_minutes <= CROWD_DATA_MAX_AGE_MINUTES else "stale"
        except (TypeError, ValueError, OverflowError):
            freshness = "unknown"

    label = record.get("area_busyness")
    if label in (None, "", "Unknown", "Not measured"):
        data_status = "measurement_unavailable"
    elif freshness == "stale":
        data_status = "stale"
    elif freshness == "fresh":
        data_status = "success"
    else:
        data_status = "freshness_unknown"

    radius_m = detail.get("radius_m")
    if entity_type == "pandal":
        limitation = (
            f"This estimate is derived from TomTom vehicle speeds/congestion on sampled roads "
            f"within approximately {radius_m if radius_m is not None else 500} m of the pandal. "
            "It is not a direct count of visitors, pedestrian density, or queue length."
        )
    else:
        limitation = (
            f"This estimate is derived from TomTom vehicle speeds/congestion on sampled roads "
            f"within approximately {radius_m if radius_m is not None else 100} m of the station. "
            "It is not a direct count of passengers, platform crowding, or train occupancy."
        )

    return {
        "status": data_status,
        "entity_type": entity_type,
        "id": record.get("id"),
        "name": record.get("name"),
        "area": record.get("area"),
        "address": record.get("address"),
        "area_busyness": label,
        "measurement_type": "road_traffic_derived_area_busyness_proxy",
        "area_busyness_detail": detail,
        "measured_at": measured_at,
        "data_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "freshness": freshness,
        "freshness_threshold_minutes": CROWD_DATA_MAX_AGE_MINUTES,
        "source": "TomTom Traffic Flow Segment Data API via the project collector",
        "source_snapshot": source_path.name,
        "interpretation_and_limitations": limitation,
        "stale_data_note": (
            "This snapshot exceeds the configured freshness threshold; treat it as historical context, not current conditions."
            if freshness == "stale" else None
        ),
    }


def _compact_area_busyness_context(record: dict | None, entity_type: str, source_path: Path) -> dict:
    """Compact per-stop context for route responses."""
    if not isinstance(record, dict):
        return {
            "status": "unavailable",
            "area_busyness": None,
            "freshness": "unknown",
            "measurement_type": "road_traffic_derived_area_busyness_proxy",
            "note": f"No matching enriched record was found in {source_path.name}.",
        }

    full = _make_area_busyness_response(record, entity_type, source_path)
    detail = full.get("area_busyness_detail") or {}
    return {
        "status": full.get("status"),
        "area_busyness": full.get("area_busyness"),
        "measured_at": full.get("measured_at"),
        "data_age_minutes": full.get("data_age_minutes"),
        "freshness": full.get("freshness"),
        "avg_congestion_pct": detail.get("avg_congestion_pct"),
        "peak_congestion_pct": detail.get("peak_congestion_pct"),
        "avg_speed_kmph": detail.get("avg_speed_kmph"),
        "road_segments": detail.get("road_segments"),
        "radius_m": detail.get("radius_m"),
        "measurement_type": full.get("measurement_type"),
        "note": full.get("interpretation_and_limitations"),
        "stale_data_note": full.get("stale_data_note"),
    }


def _crowd_record_map(path: Path) -> dict | list[dict]:
    """Load an enriched snapshot into an ID-keyed map for one operation."""
    records = _read_enriched_snapshot(path)
    if isinstance(records, dict):
        return records
    return {
        str(row.get("id", "")).strip().casefold(): row
        for row in records
        if isinstance(row, dict) and str(row.get("id", "")).strip()
    }


def _crowd_for_id(path: Path, entity_type: str, record_id: str) -> dict:
    records = _read_enriched_snapshot(path)
    if isinstance(records, dict):
        return records
    match = _lookup_enriched_record(records, record_id)
    if match.get("status") != "found":
        return match
    return _make_area_busyness_response(match["record"], entity_type, path)


def _summarize_route_busyness(stops: list[dict]) -> dict:
    label_counts: dict[str, int] = {}
    stale_stops = []
    unavailable_stops = []
    freshness_unknown_stops = []
    fresh_count = 0

    for stop in stops:
        context = stop.get("road_traffic_busyness") or {}
        status = context.get("status")
        name = stop.get("pandal_name") or stop.get("name") or stop.get("pandal_id")
        if status == "success":
            fresh_count += 1
            label = str(context.get("area_busyness") or "Unknown")
            label_counts[label] = label_counts.get(label, 0) + 1
        elif status == "stale":
            stale_stops.append(name)
        elif status == "freshness_unknown":
            freshness_unknown_stops.append(name)
        else:
            unavailable_stops.append(name)

    if fresh_count == len(stops) and stops:
        summary_status = "success"
    elif fresh_count or stale_stops or freshness_unknown_stops:
        summary_status = "partial"
    else:
        summary_status = "unavailable"

    return {
        "status": summary_status,
        "fresh_estimates_count": fresh_count,
        "total_stops": len(stops),
        "fresh_label_counts": label_counts,
        "stale_stops": stale_stops,
        "freshness_unknown_stops": freshness_unknown_stops,
        "unavailable_stops": unavailable_stops,
        "selection_effect": (
            "Context only. This estimate is not used to rank route candidates because it measures nearby road traffic, not pedestrian crowd levels."
        ),
    }


@mcp.tool()
def get_pandal_crowding(pandal_name_or_id: str) -> dict:
    """Get the latest road-traffic-derived area-busyness estimate near a Puja pandal.

    MUST be used when the user asks how busy or crowded a named pandal area is.
    Match by exact pandal ID/name where possible. Return measurement time and
    freshness. This is a road-traffic proxy, not a direct count of people,
    visitors, queues, or pedestrian density.
    """
    records = _read_enriched_snapshot(CROWD_PANDALS_FILE)
    if isinstance(records, dict):
        return records
    match = _lookup_enriched_record(records, pandal_name_or_id)
    if match.get("status") != "found":
        return match
    return _make_area_busyness_response(match["record"], "pandal", CROWD_PANDALS_FILE)


@mcp.tool()
def get_metro_station_crowding(station_name_or_id: str) -> dict:
    """Get the latest road-traffic-derived area-busyness estimate near a Metro station.

    MUST be used when the user asks how busy or crowded the area around a
    named Metro station is. This is not a direct measure of passengers,
    platform density, or train occupancy.
    """
    records = _read_enriched_snapshot(CROWD_STATIONS_FILE)
    if isinstance(records, dict):
        return records
    match = _lookup_enriched_record(records, station_name_or_id)
    if match.get("status") != "found":
        return match
    return _make_area_busyness_response(match["record"], "metro_station", CROWD_STATIONS_FILE)


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
    result["distance_type"] = "straight_line_geographic"
    result["road_traffic_busyness"] = _crowd_for_id(
        CROWD_STATIONS_FILE,
        "metro_station",
        str(nearest_station.get("id", "")),
    )

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
    """Return route stops with current road-traffic-derived busyness context."""
    crowd_map = _crowd_record_map(CROWD_PANDALS_FILE)
    snapshot = []
    for i, pandal in enumerate(selected_pandals):
        crowd_record = (
            crowd_map.get(str(pandal.get("id", "")).strip().casefold())
            if isinstance(crowd_map, dict) else None
        )
        snapshot.append({
            "stop_number": i + 1,
            "id": pandal["id"],
            "name": pandal["name"],
            "latitude": pandal["latitude"],
            "longitude": pandal["longitude"],
            "road_traffic_busyness": _compact_area_busyness_context(
                crowd_record, "pandal", CROWD_PANDALS_FILE
            ),
        })
    return snapshot


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
                        and hour["precipitation_mm"] >= 0.5
                    )
                    or (
                        isinstance(hour["rain_mm"], (int, float))
                        and hour["rain_mm"] >= 0.5
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
                # This endpoint's values used here do not classify precipitation
                # type (e.g. drizzle) or intensity. Do not infer either from
                # probability or accumulation alone.
                "precipitation_type_available": False,
                "precipitation_type": None,
                "precipitation_type_note": (
                    "Precipitation type/intensity is not available in this "
                    "result. Do not infer drizzle, showers, or rain intensity "
                    "from probability or amount alone."
                ),
                # Trace-level precipitation is retained in the metrics but does
                # not trigger a route-level rain signal on its own. A rain signal
                # requires >=50% hourly probability or >=0.5 mm in an hour.
                "rain_expected": rain_hour_count > 0,
                "rain_signal_detected": rain_hour_count > 0,
                "rain_signal_thresholds": {
                    "precipitation_probability_percent": 50,
                    "precipitation_mm_per_hour": 0.5,
                },
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
    latitude: float | None = None,
    longitude: float | None = None
) -> dict:
    """
    Recommend a planned Kolkata Puja route using weather and, when supplied,
    a known starting coordinate.

    LOCATION RULES
    - Only pass latitude and longitude when the user explicitly supplied them
      or a trusted location source returned them in this conversation.
    - Never guess, infer, or silently substitute the user's current location.
    - If no reliable starting coordinates are available, call this tool with
      no arguments. The original planned route order is retained and no
      distance-to-user or nearest-to-user claim is returned.
    - If coordinates are supplied, distances are straight-line distances,
      not walking or driving distances.

    Selection logic:
    - With valid start coordinates, rotate each route to start at its nearest
      pandal relative to those coordinates.
    - Without start coordinates, preserve each route's original planned order.
    - Fetch hourly rain forecasts for pandal stops on candidate routes.
    - When rain is forecast and all candidate forecasts are complete,
      rank routes by rain exposure, precipitation probability, and
      forecast precipitation.
    - Use planned duration and proximity as tie-breakers.
    - If weather data is unavailable or incomplete, retain the original
      nearest-pandal selection rule.
    - Include each stop's latest road-traffic-derived busyness estimate and
      route-level data freshness summary when available. Do not use these
      estimates to rank routes: they are not direct pedestrian crowd data.

    WEATHER INTERPRETATION AND USER-FACING STYLE
    - Use the returned forecast measurements and weather_summary as the source
      of truth. Explain weather in concise, natural language for a tourist.
    - Keep weather copy to 1-3 short sentences unless the user asks for details.
      Include the maximum hourly precipitation probability and, when useful, the
      average forecast precipitation. Avoid unnecessary technical explanation.
    - Do not expose implementation details such as configured trigger thresholds,
      internal signal flags, field names, or how the collector decides a signal.
      Do not say "rain signal met the configured threshold" or list the thresholds
      unless the user specifically asks how the system works.
    - If there is no notable forecast indication, say the chance looks very low
      (below 10%) or low but not zero (10% to below 20%), and give the percentage.
      For 20% or higher, report the exact percentage without overstating certainty.
    - Do not say "rain expected" when rain_signal_detected is false. A probability
      describes a chance, not a guarantee that precipitation will occur.
    - Do not infer or name precipitation type/intensity (such as drizzle, showers,
      light rain, or heavy rain) from probability or a small precipitation amount.
      Only mention type/intensity if an explicit forecast field supports it.
    - Use weather_assessment.visitor_preparation_guidance as the basis for any
      advice about preparing for wet weather. Do not replace it with a definitive
      statement that an umbrella/raincoat is or is not needed.
    - Keep this advice practical and user-friendly: state the chance plainly,
      recommend checking the forecast before leaving, and describe a compact
      umbrella as an optional precaution when the chance is low but non-zero.
      Do not infer drizzle, showers, rain intensity, or certainty from probability.
    - For road-traffic busyness, use area_busyness_context.user_facing_summary.
      Never infer that pandals are uncrowded, that queues are short, or that the
      visit will be smooth/easy just because nearby road traffic is labelled Light.
      Light traffic describes sampled roads and is not a pedestrian-crowd measure.
    - Never claim that a route is sheltered, flood-free, or safer unless reliable
      data supports that claim. The forecast applies to planned stops, not every
      road segment, and does not establish road safety.
    - Never invent a starting point or distance. If no explicit or trusted
      coordinates were supplied, call without latitude/longitude and do not say a
      pandal is a certain distance from the user.
    - Do not claim weather changed the route recommendation unless
      weather_adjustment_applied is true. Without reliable starting coordinates,
      preserve the original planned order and make no distance-to-user claim.

    OSRM remains responsible for calculating actual driving routes.
    Weather forecasts do not establish flooding, road closures, or
    road safety. This tool does not optimize OSRM driving distances.
    """

    # ---------------------------------------------------------
    # 1. Validate an optional starting location.
    # ---------------------------------------------------------
    if (latitude is None) != (longitude is None):
        return {
            "status": "error",
            "message": (
                "Provide both latitude and longitude, or omit both when "
                "no reliable starting coordinates are available."
            ),
        }

    has_start_coordinates = latitude is not None and longitude is not None

    if has_start_coordinates:
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            return {
                "status": "error",
                "message": "Latitude and longitude must be numeric values.",
            }

        if not (math.isfinite(latitude) and -90 <= latitude <= 90):
            return {
                "status": "error",
                "message": "Latitude must be a finite number between -90 and 90.",
            }

        if not (math.isfinite(longitude) and -180 <= longitude <= 180):
            return {
                "status": "error",
                "message": "Longitude must be a finite number between -180 and 180.",
            }

    # ---------------------------------------------------------
    # 2. Build a fast lookup of the pandal dataset.
    # ---------------------------------------------------------
    pandal_by_id = {
        str(p["id"]).strip().lower(): p
        for p in pandals
    }
    # Read the most recent successful crowd snapshot once for this recommendation.
    crowd_map_result = _crowd_record_map(CROWD_PANDALS_FILE)
    crowd_by_pandal_id = crowd_map_result if isinstance(crowd_map_result, dict) else {}

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

        nearest_index = 0
        nearest_pandal = pandal_by_id[
            str(valid_route_ids[0]).strip().lower()
        ]
        nearest_distance = None

        if has_start_coordinates:
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

        # Rotate only when a reliable start coordinate was supplied;
        # otherwise preserve the original route order.
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

            crowd_record = crowd_by_pandal_id.get(
                str(pandal.get("id", "")).strip().casefold()
            )
            recommended_stops.append({
                "stop_number": stop_number,
                "pandal_id": pandal["id"],
                "pandal_name": pandal["name"],
                "latitude": float(pandal["latitude"]),
                "longitude": float(pandal["longitude"]),
                "road_traffic_busyness": _compact_area_busyness_context(
                    crowd_record, "pandal", CROWD_PANDALS_FILE
                ),
            })

        route_candidates.append({
            "route_id": route_id,
            "route_name": route_name,

            "nearest_pandal": ({
                "pandal_id": nearest_pandal["id"],
                "pandal_name": nearest_pandal["name"],
                "latitude": nearest_pandal["latitude"],
                "longitude": nearest_pandal["longitude"]
            } if has_start_coordinates else None),

            "distance_to_nearest_pandal_km": (
                round(nearest_distance, 2)
                if has_start_coordinates else None
            ),
            "distance_type": (
                "straight_line_geographic" if has_start_coordinates else None
            ),
            "distance_source": "Haversine" if has_start_coordinates else None,
            "start_location_provided": has_start_coordinates,
            "route_calculated": False,

            "total_stops": len(recommended_stops),
            "must_see_count": route.get("must_see_count"),
            "estimated_duration_hours": route.get(
                "estimated_duration_hours"
            ),

            "recommended_stops": recommended_stops,
            "recommended_pandal_ids": recommended_pandal_ids,
            "area_busyness_assessment": _summarize_route_busyness(
                recommended_stops
            ),

            "source": route.get("source"),
            "source_url": route.get("source_url"),

            "route_order_strategy": (
                (
                    "Start at the pandal nearest to the supplied starting "
                    "coordinates, then continue in the original planned-route "
                    "order. Distance is straight-line, not road or walking distance. "
                    "This is not a shortest-driving-route optimization."
                )
                if has_start_coordinates else
                "No starting coordinates were supplied. Preserve the original "
                "planned-route order; no distance-to-user or nearest-to-user "
                "claim is available. This is not a shortest-driving-route optimization."
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

        maximum_probabilities = [
            forecast["max_precipitation_probability_percent"]
            for forecast in stop_forecasts
            if isinstance(
                forecast.get("max_precipitation_probability_percent"),
                (int, float)
            )
        ]
        maximum_probability = (
            max(maximum_probabilities)
            if maximum_probabilities else None
        )

        rain_signal_detected = rainy_stops > 0
        if rain_signal_detected:
            if maximum_probability is not None:
                weather_summary = (
                    f"The forecast indicates a possibility of precipitation at "
                    f"some stops during the next eight hours. The highest hourly "
                    f"chance is {maximum_probability}%. Check the forecast again "
                    "before setting out; it does not specify precipitation type or "
                    "guarantee road conditions."
                )
            else:
                weather_summary = (
                    "The forecast indicates a possibility of precipitation at "
                    "some stops during the next eight hours. Check the latest "
                    "forecast before setting out."
                )
        elif maximum_probability is not None and maximum_probability < 10:
            weather_summary = (
                f"The chance of precipitation looks very low, with a maximum "
                f"hourly probability of {maximum_probability}%. Forecasts can "
                "change, so check again before leaving."
            )
        elif maximum_probability is not None and maximum_probability < 20:
            weather_summary = (
                f"The chance of precipitation is low but not zero. The maximum "
                f"hourly probability is {maximum_probability}%. Check again "
                "before leaving, as conditions can change."
            )
        elif maximum_probability is not None:
            weather_summary = (
                f"The highest hourly chance of precipitation is "
                f"{maximum_probability}%. This is a possibility, not a guarantee "
                "that it will rain. Check the latest forecast before leaving."
            )
        else:
            weather_summary = (
                "A reliable precipitation probability is not available for this "
                "forecast window. Check a current forecast before heading out."
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

        # Keep the user-facing preparation advice explicit in the tool result,
        # so the model does not improvise a definitive "no umbrella needed" claim.
        if rain_signal_detected:
            visitor_preparation_guidance = (
                "The forecast indicates a possibility of precipitation at some stops. "
                "Check the latest forecast before leaving; consider carrying an umbrella. "
                "The available values do not identify precipitation type or intensity."
            )
        elif maximum_probability is None:
            visitor_preparation_guidance = (
                "A reliable precipitation probability is unavailable for this route. "
                "Check a current forecast before leaving; the available data is not "
                "enough to make a confident recommendation about rain gear."
            )
        elif maximum_probability < 10:
            visitor_preparation_guidance = (
                f"The chance of precipitation is very low, with a maximum hourly "
                f"probability of {maximum_probability}%. Check again before leaving. "
                "Carrying a compact umbrella is an optional precaution if you prefer."
            )
        elif maximum_probability < 20:
            visitor_preparation_guidance = (
                f"The chance of precipitation is low but not zero, with a maximum "
                f"hourly probability of {maximum_probability}%. Check the forecast "
                "before leaving; if you will be out for several hours, carrying a "
                "compact umbrella is an optional precaution."
            )
        elif maximum_probability < 50:
            visitor_preparation_guidance = (
                f"There is a chance of precipitation, with a maximum hourly "
                f"probability of {maximum_probability}%. Check the forecast before "
                "leaving and consider bringing an umbrella."
            )
        else:
            visitor_preparation_guidance = (
                f"The forecast shows a higher chance of precipitation, with a maximum "
                f"hourly probability of {maximum_probability}%. Check the latest "
                "forecast and consider bringing an umbrella."
            )

        candidate["_weather_selection_metrics"] = {
            "rain_signal_stops": rainy_stops,
            "rain_signal_stop_fraction": round(rainy_stops / len(stops), 3),
            "rain_signal_detected": rain_signal_detected,
        }
        candidate["weather_assessment"] = {
            "status": "success",
            "source": "Open-Meteo",
            "forecast_hours": 8,
            "total_pandal_stops": len(stops),
            # These metrics help Claude give a concise, grounded forecast summary.
            # Route-selection flags and trigger thresholds are intentionally not
            # returned as user-facing output.
            "mean_precipitation_probability_percent": mean_probability,
            "max_hourly_precipitation_probability_percent": maximum_probability,
            "mean_precipitation_mm_per_stop": mean_precipitation,
            "weather_summary": weather_summary,
            "visitor_preparation_guidance": visitor_preparation_guidance,
            "assessment_basis": (
                "Forecast values are for planned pandal stops during the next "
                "eight hours; weather between stops is not assessed."
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
            candidate.get("_weather_selection_metrics", {}).get("rain_signal_stops", 0) > 0
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

            selection_metrics = route.get("_weather_selection_metrics", {})
            return (
                selection_metrics.get("rain_signal_stop_fraction", 1.0),
                probability_key,
                assessment["mean_precipitation_mm_per_stop"],
                planned_duration_key(route),
                (
                    route["distance_to_nearest_pandal_km"]
                    if isinstance(route.get("distance_to_nearest_pandal_km"), (int, float))
                    else float("inf")
                )
            )

        route_candidates.sort(key=weather_route_key)
        weather_adjustment_applied = True

        selection_reason = (
            "The route order was chosen after comparing the available "
            "precipitation forecasts at planned stops. This does not mean "
            "the route is sheltered or that road conditions are guaranteed."
        )

    else:
        # Preserve existing behaviour if forecasts are unavailable,
        # incomplete, or show no configured rain signal.
        if has_start_coordinates:
            route_candidates.sort(
                key=lambda route: route["distance_to_nearest_pandal_km"]
            )

        if weather_complete:
            if has_start_coordinates:
                selection_reason = (
                    "The available forecast did not indicate a notable "
                    "precipitation concern at the planned stops, so the "
                    "location-based route choice was retained."
                )
            else:
                selection_reason = (
                    "The available forecast did not indicate a notable "
                    "precipitation concern at the planned stops, so the original "
                    "planned route order was retained. No starting location was "
                    "provided, so distances from you are not shown."
                )
        elif has_start_coordinates:
            selection_reason = (
                "Weather data was unavailable or incomplete for one or "
                "more candidate routes. The original nearest-pandal "
                "selection rule was retained without weather adjustment."
            )
        else:
            selection_reason = (
                "Weather data was unavailable or incomplete for one or "
                "more candidate routes. With no reliable starting coordinates, "
                "the original planned-route order was retained; no distance-to-user "
                "claim is made."
            )

    recommended = route_candidates[0]

    # Remove internal route-ranking flags before returning results to the LLM.
    # Return only concise, user-facing weather fields for each route option.
    def make_user_facing_route(route: dict) -> dict:
        public_route = dict(route)
        public_route.pop("_weather_selection_metrics", None)
        assessment = public_route.get("weather_assessment")
        if isinstance(assessment, dict):
            allowed_weather_fields = {
                "status",
                "source",
                "forecast_hours",
                "total_pandal_stops",
                "mean_precipitation_probability_percent",
                "max_hourly_precipitation_probability_percent",
                "mean_precipitation_mm_per_stop",
                "weather_summary",
                "visitor_preparation_guidance",
                "assessment_basis",
            }
            public_route["weather_assessment"] = {
                key: value for key, value in assessment.items()
                if key in allowed_weather_fields
            }
        return public_route

    public_recommended = make_user_facing_route(recommended)
    public_alternatives = [
        make_user_facing_route(route) for route in route_candidates[1:]
    ]

    # ---------------------------------------------------------
    # 7. Return the recommendation, evidence, and limitations.
    # ---------------------------------------------------------
    return {
        "status": "success",

        "user_location": (
            {
                "latitude": latitude,
                "longitude": longitude,
                "coordinate_source": "coordinates_supplied_to_tool",
            }
            if has_start_coordinates else None
        ),
        "start_location_status": (
            "coordinates_supplied"
            if has_start_coordinates else "not_provided"
        ),
        "distance_to_user_available": has_start_coordinates,

        "recommended_route": public_recommended,
        "other_route_options": public_alternatives,

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
            "weather_adjustment_applied": weather_adjustment_applied,
            "recommended_route_weather_summary": public_recommended.get(
                "weather_assessment", {}
            ).get("weather_summary"),
            "limitations": (
                "Forecasts apply to planned pandal stops, not every road segment. "
                "They do not specify precipitation type unless explicitly provided, "
                "and they do not establish flooding, road closures, shelter, or safety."
            )
        },

        "selection_reason": selection_reason,

        "area_busyness_context": {
            "included": True,
            "source": "TomTom Traffic Flow Segment Data API via the project collector",
            "interpretation": (
                "Road-traffic-derived proxy at sampled roads near pandals; it is not direct pedestrian crowd, queue, or visitor-count data."
            ),
            "user_facing_summary": (
                "The road-traffic readings can describe vehicle congestion on sampled roads near the pandals. "
                "They do not show how crowded the pandals or queues are and cannot guarantee a smooth visit."
            ),
            "selection_effect": (
                "Used as informational context only; route ranking remains based on the existing weather and proximity logic."
            ),
            "freshness_threshold_minutes": CROWD_DATA_MAX_AGE_MINUTES,
        },

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

@mcp.resource("metro://2026/puja-special-services")
def metro_puja_special_services_resource() -> str:
    """Expose the official, date-specific Metro Puja special-services notice."""
    return json.dumps(
        metro_puja_special_services_data,
        ensure_ascii=False,
        indent=2,
    )


def _metro_period_user_summary(line_name: str, event_date: str, period: dict) -> str:
    """Create a concise, tourist-friendly summary for one line/date period."""
    try:
        date_label = datetime.strptime(event_date, "%Y-%m-%d").strftime("%d %b %Y")
    except (TypeError, ValueError):
        date_label = event_date

    if period.get("service_status") == "suspended":
        return (
            f"{line_name}: no services are scheduled for {date_label} under "
            "the supplied Metro Railway special-services notice."
        )

    if period.get("service_status") != "scheduled":
        return f"{line_name}: service status is not specified for {date_label}."

    start = period.get("service_window_start") or "time unavailable"
    end = period.get("service_window_end") or "time unavailable"
    end_offset = period.get("service_window_end_day_offset", 0)
    end_phrase = f"{end} the following morning" if end_offset == 1 else str(end)
    parts = [
        f"{line_name} special services on {date_label} are scheduled from "
        f"{start} to {end_phrase} IST"
    ]

    count = period.get("daily_services_total")
    if count is not None:
        parts.append(f"{count} services are scheduled that day")

    frequency = period.get("peak_frequency_minutes")
    if frequency is not None:
        parts.append(f"peak-hour intervals are about {frequency} minutes")

    return "; ".join(parts) + "."


def _metro_overnight_user_summary(
    line_name: str, service_start_date: str, requested_date: str, period: dict
) -> str:
    """Explain prior-date service that continues into the requested date."""
    try:
        start_label = datetime.strptime(service_start_date, "%Y-%m-%d").strftime("%d %b %Y")
    except (TypeError, ValueError):
        start_label = service_start_date
    try:
        requested_label = datetime.strptime(requested_date, "%Y-%m-%d").strftime("%d %b %Y")
    except (TypeError, ValueError):
        requested_label = requested_date

    return (
        f"{line_name}: overnight service that began at "
        f"{period.get('service_window_start')} IST on {start_label} continues "
        f"until {period.get('service_window_end')} IST on {requested_label}."
    )


def _metro_last_services_user_summary(line_name: str, service_date: str, period: dict) -> str | None:
    """Format each published last departure separately without merging routes."""
    last_services = period.get("last_services")
    if not isinstance(last_services, list) or not last_services:
        return None

    try:
        start_date = datetime.strptime(service_date, "%Y-%m-%d").date()
        if period.get("service_window_end_day_offset", 0) == 1:
            last_service_date = (start_date + timedelta(days=1)).strftime("%d %b %Y")
        else:
            last_service_date = start_date.strftime("%d %b %Y")
    except (TypeError, ValueError):
        last_service_date = service_date

    departures = []
    for item in last_services:
        if not isinstance(item, dict):
            continue
        time_text = str(item.get("time", "")).strip()
        origin = str(item.get("from", "")).strip()
        destination = str(item.get("to", "")).strip()
        if time_text and origin and destination:
            departures.append(
                f"{time_text} IST {last_service_date}: {origin} to {destination}"
            )

    if not departures:
        return None

    return (
        f"Last listed {line_name} departures (each is a separate origin/destination trip): "
        + "; ".join(departures)
        + "."
    )


def _metro_user_facing_table_row(line_name: str, period: dict) -> dict:
    """Return an explicit, UI-ready status row for line/date summaries."""
    status = str(period.get("service_status", "unknown")).casefold()
    if status == "suspended":
        return {
            "line": line_name,
            "status": "Suspended",
            "operating_hours": "—",
            "services_per_day": "—",
            "peak_hour_frequency": "—",
        }

    start = period.get("service_window_start")
    end = period.get("service_window_end")
    end_offset = period.get("service_window_end_day_offset", 0)
    if start and end:
        operating_hours = f"{start}–{end}"
        if end_offset == 1:
            operating_hours += " (next morning)"
    else:
        operating_hours = "Not specified"

    count = period.get("daily_services_total")
    frequency = period.get("peak_frequency_minutes")
    return {
        "line": line_name,
        "status": "Operating" if status == "scheduled" else "Not specified",
        "operating_hours": operating_hours,
        "services_per_day": count if count is not None else "—",
        "peak_hour_frequency": (
            f"About {frequency} minutes" if frequency is not None else "—"
        ),
    }


@mcp.tool()
def get_metro_puja_special_services(
    event_date: str,
    line_name: str = "",
) -> dict:
    """Look up Kolkata Metro's published Durga Puja 2026 special services by date.

    MUST be used for questions about Metro service timings, first/last services,
    frequency, number of services, or line suspensions for 15–21 October 2026.

    Required event_date format: YYYY-MM-DD. Optionally set line_name to Blue,
    Green, Yellow, Purple, or Orange (with or without the word "Line").

    This dataset reproduces the official press-release text supplied to the
    project, dated 8 October 2026. It is a special-service notice only, not a
    complete regular timetable. Do not infer schedules outside its coverage.
    If a later official amendment is available, it must take precedence.

    For Blue and Green Line services that run past midnight, the result may
    include a continuation from the previous service date. Treat the service
    start date and next-day end time explicitly; do not mistake the ending time
    as a new day's first service.

    This tool reports published service information; it does not calculate a
    station-to-station journey or guarantee live operational status.

    For Yellow and Purple Lines, never describe the schedule as
    "evening-only". Services begin in the afternoon and continue
    into the evening. Use this wording:
    "The Yellow and Purple Lines operate from the afternoon
    into the evening."

    USER-FACING RESPONSE GUIDANCE:
    - Use plain language and Kolkata local time (IST). Do not dump raw JSON.
    - If a line is specified, say first whether it is scheduled or suspended,
      then provide the operating window and relevant first/last services.
    - If all lines are requested, use a compact table with exactly these
      column labels: "Line", "Status", "Operating Hours", "Services per Day",
      and "Peak-hour Frequency". The Status column must explicitly say
      "Operating" or "Suspended" based on service_status.
    - Use daily_services_total for the daily count and peak_frequency_minutes
      for peak-hour frequency. Do not imply the peak interval applies throughout
      the day.
    - For a suspended line, show "Suspended" in the Status column and an em dash
      for Operating Hours, Services per Day, and Peak-hour Frequency. Do not put
      the word "Suspended" in the frequency column.
    - For overnight services, explicitly say that the end time is the next
      morning and state the calendar date it ends on.
    - For Blue Line overnight schedules, use the individual last_services records
      as separate departures with their exact time, origin, and destination. Do
      not summarise all Blue Line services as one final 04:00 service. If the
      notice lists 04:00 departures, explain that these listed trips terminate at
      Dum Dum and Mahanayak Uttam Kumar respectively; do not imply that either is
      a full end-to-end trip.
    - Do not describe Yellow or Purple Line schedules as "evening service only".
      When the schedule starts in the afternoon, say that services begin in the
      afternoon and continue into the evening, using the actual operating times.
    - Label service counts as counts scheduled that day, not trains per hour.
    - Say this is the supplied special-service notice, not live running status,
      and recommend checking for later official amendments before travel.
    - Do not infer regular timetables outside the notice's coverage dates.
    """
    raw_date = str(event_date or "").strip()
    try:
        requested_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {
            "status": "error",
            "error": "event_date is required in YYYY-MM-DD format, for example 2026-10-18.",
        }

    coverage = metro_puja_special_services_data.get("coverage", {})
    coverage_start_text = str(coverage.get("start_date", ""))
    coverage_end_text = str(coverage.get("end_date", ""))
    try:
        coverage_start = datetime.strptime(coverage_start_text, "%Y-%m-%d").date()
        coverage_end = datetime.strptime(coverage_end_text, "%Y-%m-%d").date()
    except ValueError:
        return {
            "status": "unavailable",
            "error": "The Metro special-services dataset has invalid coverage dates.",
        }

    source = metro_puja_special_services_data.get("source", {})
    base_response = {
        "dataset": metro_puja_special_services_data.get("dataset_name"),
        "requested_date": raw_date,
        "timezone": metro_puja_special_services_data.get("timezone", "Asia/Kolkata"),
        "line_filter": line_name or None,
        "coverage": coverage,
        "source": source,
    }

    if requested_date < coverage_start or requested_date > coverage_end:
        return {
            **base_response,
            "status": "outside_coverage",
            "result_count": 0,
            "results": [],
            "message": (
                f"This special-services notice covers {coverage_start_text} through "
                f"{coverage_end_text} only. No Metro timetable for {raw_date} is "
                "provided by this dataset; do not infer regular service hours."
            ),
        }

    all_lines = [
        item for item in metro_puja_special_services_data.get("lines", [])
        if isinstance(item, dict) and isinstance(item.get("line_name"), str)
    ]

    if line_name and line_name.strip():
        query = re.sub(r"\s+line$", "", line_name.strip().casefold()).strip()
        exact = [
            item for item in all_lines
            if re.sub(r"\s+line$", "", item["line_name"].strip().casefold()).strip() == query
        ]
        if not exact:
            partial = [
                item for item in all_lines
                if query in item["line_name"].strip().casefold()
            ]
            exact = partial if len(partial) == 1 else []
        if not exact:
            return {
                **base_response,
                "status": "line_not_found",
                "available_lines": [item["line_name"] for item in all_lines],
                "result_count": 0,
                "results": [],
                "message": "Specify one of the available Metro line names.",
            }
        selected_lines = exact
    else:
        selected_lines = all_lines

    requested_text = requested_date.isoformat()
    previous_text = (requested_date - timedelta(days=1)).isoformat()
    results = []
    overnight_continuations = []

    for line in selected_lines:
        for period in line.get("date_periods", []):
            if not isinstance(period, dict):
                continue
            service_dates = period.get("dates", [])
            if not isinstance(service_dates, list):
                continue

            if requested_text in service_dates:
                results.append({
                    "line_name": line["line_name"],
                    "schedule_relation": "service_scheduled_for_requested_date",
                    "service_date": requested_text,
                    "user_facing_summary": _metro_period_user_summary(
                        line["line_name"], requested_text, period
                    ),
                    "user_facing_table_row": _metro_user_facing_table_row(
                        line["line_name"], period
                    ),
                    "last_services_summary": _metro_last_services_user_summary(
                        line["line_name"], requested_text, period
                    ),
                    "service_period": period,
                })

            # Include late-night service from the prior date when it runs into
            # the calendar day the user asked about.
            if (
                previous_text in service_dates
                and period.get("service_status") == "scheduled"
                and period.get("service_window_end_day_offset") == 1
            ):
                overnight_continuations.append({
                    "line_name": line["line_name"],
                    "schedule_relation": "overnight_service_continuing_from_previous_date",
                    "service_start_date": previous_text,
                    "user_facing_summary": _metro_overnight_user_summary(
                        line["line_name"], previous_text, requested_text, period
                    ),
                    "service_period": period,
                    "note": (
                        f"This service period starts on {previous_text} and continues "
                        f"into {requested_text}; confirm the listed last-service times "
                        "before travelling."
                    ),
                })

    return {
        **base_response,
        "status": "success" if results or overnight_continuations else "no_schedule_found",
        "result_count": len(results),
        "results": results,
        "user_facing_table_rows": [
            item["user_facing_table_row"]
            for item in results
            if isinstance(item.get("user_facing_table_row"), dict)
        ],
        "overnight_continuations_from_previous_date": overnight_continuations,
        "result_count_including_overnight_continuations": len(results) + len(overnight_continuations),
        "message": (
            "Published special-service information found. Check for later official amendments before travelling."
            if results or overnight_continuations
            else "No schedule entry was found for this date in the supplied special-services notice. Do not guess service times."
        ),
        "limitations": [
            "This is a date-specific special-service notice, not a full regular timetable.",
            "This lookup provides service information only; use the Metro journey-planning tool for station sequences and line changes. It does not provide live disruption status.",
            "Service details reproduce the press-release text supplied to the project; check for later official amendments.",
        ],
    }


# ---------------------------------------------------------------------------
# Date-aware Metro-only journey planning
# ---------------------------------------------------------------------------
def _metro_normalize_name(value: str) -> str:
    """Normalize a station/line label for forgiving name matching."""
    value = str(value or "").casefold().strip()
    value = re.sub(r"\bmetro\b", " ", value)
    value = re.sub(r"\bstation\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _load_metro_network_for_route_planner() -> dict:
    """Load the station graph at query time so replacing the JSON needs no code edit."""
    try:
        with METRO_NETWORK_FILE.open("r", encoding="utf-8") as stream:
            network = json.load(stream)
    except FileNotFoundError:
        return {
            "_error": (
                "Metro network data is missing. Save the reviewed network as "
                "data/static/metro_network_2026.json before using Metro journey planning."
            )
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {"_error": f"Could not read metro_network_2026.json: {exc}"}

    if not isinstance(network, dict):
        return {"_error": "Metro network data must be a JSON object."}
    for required in ("lines", "stations", "connections"):
        if not isinstance(network.get(required), list):
            return {"_error": f"Metro network data must contain a '{required}' list."}
    return network


def _metro_line_name_map(network: dict) -> dict[str, str]:
    return {
        str(item.get("line_id", "")).strip().upper(): str(item.get("line_name", "")).strip()
        for item in network.get("lines", [])
        if isinstance(item, dict) and item.get("line_id") and item.get("line_name")
    }


def _metro_schedule_state_for_date(line_name: str, event_date: str, network: dict) -> dict:
    """Return date-specific schedule state; unknown dates are never guessed."""
    wanted = _metro_normalize_name(line_name)
    schedule_lines = metro_puja_special_services_data.get("lines", [])
    matched_line = next(
        (
            row for row in schedule_lines
            if isinstance(row, dict)
            and _metro_normalize_name(row.get("line_name", "")) == wanted
        ),
        None,
    )
    if matched_line:
        for period in matched_line.get("date_periods", []):
            if isinstance(period, dict) and event_date in period.get("dates", []):
                return {
                    "status": str(period.get("service_status", "unknown")).casefold(),
                    "period": period,
                    "source": "metro_puja_special_services_2026.json",
                }

    # Also honor date-ranged network advisories when no published schedule entry
    # exists for the requested date. A later explicit scheduled entry above takes
    # precedence over older advisory metadata.
    line_id = next(
        (
            str(item.get("line_id", "")).upper()
            for item in network.get("lines", [])
            if isinstance(item, dict)
            and _metro_normalize_name(item.get("line_name", "")) == wanted
        ),
        "",
    )
    for advisory in network.get("temporary_service_advisories", []):
        if not isinstance(advisory, dict):
            continue
        if str(advisory.get("line_id", "")).upper() != line_id:
            continue
        if advisory.get("type") == "date_range_line_suspension":
            start = str(advisory.get("start_date", ""))
            end = str(advisory.get("end_date", ""))
            if start and end and start <= event_date <= end:
                return {
                    "status": "suspended",
                    "period": None,
                    "source": "metro_network_2026.json advisory",
                    "advisory": advisory,
                }

    coverage = metro_puja_special_services_data.get("coverage", {})
    start = str(coverage.get("start_date", ""))
    end = str(coverage.get("end_date", ""))
    if start and end and start <= event_date <= end:
        return {
            "status": "unknown",
            "period": None,
            "source": "no matching date period",
        }
    return {
        "status": "unknown",
        "period": None,
        "source": "outside special-services notice coverage",
    }


def _metro_resolve_station(query: str, stations: list[dict]) -> dict:
    """Resolve station by ID or name; return candidates for ambiguous queries."""
    needle = _metro_normalize_name(query)
    if not needle:
        return {"status": "invalid", "message": "Please provide a station name or ID."}

    aliases = {
        "m052": {"jai hind bimanbandar", "jai hind airport", "biman bandar", "bimanbandar"},
        "m016": {"dharmatala", "esplanade metro"},
        "m008": {"dum dum metro"},
    }
    exact = []
    for station in stations:
        sid = str(station.get("id", "")).strip()
        name = _metro_normalize_name(station.get("name", ""))
        if needle == sid.casefold() or needle == name or needle in aliases.get(sid.casefold(), set()):
            exact.append(station)

    if len(exact) == 1:
        return {"status": "found", "station": exact[0]}
    if len(exact) > 1:
        return {
            "status": "ambiguous",
            "message": "That station name matches multiple records. Please specify the station ID.",
            "candidates": [{"id": s.get("id"), "name": s.get("name")} for s in exact[:10]],
        }

    partial = []
    for station in stations:
        sid = str(station.get("id", "")).strip().casefold()
        name = _metro_normalize_name(station.get("name", ""))
        if needle in name or (sid and needle in sid):
            partial.append(station)
    if len(partial) == 1:
        return {"status": "found", "station": partial[0]}
    if partial:
        return {
            "status": "ambiguous",
            "message": "Several Metro stations match. Please select one of these names.",
            "candidates": [{"id": s.get("id"), "name": s.get("name")} for s in partial[:10]],
        }
    return {"status": "not_found"}


def _metro_resolve_endpoint(query: str, network_stations: list[dict], usable_station_ids: set[str]) -> dict:
    """Accept a Metro station or a pandal; pandals connect by nearest Metro station."""
    station_result = _metro_resolve_station(query, network_stations)
    if station_result.get("status") in {"found", "ambiguous", "invalid"}:
        if station_result.get("status") == "found":
            station = station_result["station"]
            return {
                "status": "found",
                "type": "metro_station",
                "station": station,
                "station_id": str(station.get("id", "")),
                "name": station.get("name"),
            }
        return station_result

    needle = _metro_normalize_name(query)
    exact_pandals = [
        p for p in pandals
        if _metro_normalize_name(p.get("name", "")) == needle
        or str(p.get("id", "")).strip().casefold() == str(query).strip().casefold()
    ]
    if len(exact_pandals) == 1:
        pandal = exact_pandals[0]
    elif len(exact_pandals) > 1:
        return {
            "status": "ambiguous",
            "message": "That pandal name matches multiple records; please specify the pandal ID.",
            "candidates": [{"id": p.get("id"), "name": p.get("name")} for p in exact_pandals[:10]],
        }
    else:
        partial_pandals = [
            p for p in pandals
            if needle and (
                needle in _metro_normalize_name(p.get("name", ""))
                or needle in _metro_normalize_name(p.get("area", ""))
            )
        ]
        if len(partial_pandals) == 1:
            pandal = partial_pandals[0]
        elif partial_pandals:
            return {
                "status": "ambiguous",
                "message": "Several pandals match. Please specify a more exact name or pandal ID.",
                "candidates": [{"id": p.get("id"), "name": p.get("name"), "area": p.get("area")} for p in partial_pandals[:10]],
            }
        else:
            return {
                "status": "not_found",
                "message": f"Could not find a Metro station or pandal matching '{query}'.",
                "suggestion": "Use a station name from metro_stations.json or a pandal name from pandals_2026.json.",
            }

    try:
        lat = float(pandal["latitude"])
        lon = float(pandal["longitude"])
    except (KeyError, TypeError, ValueError):
        return {"status": "error", "message": f"Pandal '{pandal.get('name')}' has no valid coordinates."}

    candidates = []
    for station in network_stations:
        sid = str(station.get("id", ""))
        if sid not in usable_station_ids:
            continue
        try:
            slat, slon = float(station["latitude"]), float(station["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = haversine_distance(lat, lon, slat, slon)
        candidates.append((distance, station))
    candidates.sort(key=lambda pair: pair[0])
    if not candidates:
        return {"status": "unavailable", "message": "No Metro stations are available in the route graph for this date."}
    distance, station = candidates[0]
    return {
        "status": "found",
        "type": "pandal",
        "pandal_id": pandal.get("id"),
        "name": pandal.get("name"),
        "station": station,
        "station_id": str(station.get("id", "")),
        "nearest_metro_station": station.get("name"),
        "access_distance_km_straight_line": round(distance, 2),
        "access_distance_note": "Straight-line geographic distance only; this is not a walking route or walking distance.",
    }


def _metro_user_facing_service_note(line_service: dict, travel_date: str) -> str:
    """Format a short line-service note without implying live running status."""
    line_name = str(line_service.get("line_name") or "Metro line")
    status = str(line_service.get("service_status_on_date") or "unknown")
    if status == "scheduled":
        start = line_service.get("service_window_start")
        end = line_service.get("service_window_end")
        end_offset = line_service.get("service_window_end_day_offset", 0)
        if start and end:
            window = f"{start}–{end}" + (" the following morning" if end_offset == 1 else "")
        else:
            window = "not specified"
        frequency = line_service.get("peak_frequency_minutes")
        frequency_text = (
            f" Peak-hour intervals are about {frequency} minutes."
            if frequency is not None else ""
        )
        return (
            f"{line_name}: the supplied special-service notice lists {window} for "
            f"{travel_date} in Kolkata time." + frequency_text +
            " This is the published schedule, not live train status."
        )
    return (
        f"{line_name}: the supplied special-service notice does not confirm operating "
        f"hours for {travel_date}. Check Metro Railway's latest notice before travelling."
    )


@mcp.tool()
def plan_metro_journey(
    origin: str,
    destination: str,
    travel_date: str = "",
) -> dict:
    """Plan a Metro-only station route for a Kolkata journey on a specified date.

    MUST be used when the user asks for a Metro route between stations, asks
    whether a Metro journey is possible on a date, or asks how to use Metro to
    reach a pandal. Origin and destination may be Metro station names/IDs or
    pandal names/IDs. Pandal endpoints are connected to their nearest station
    by straight-line geographic proximity only; the tool does not calculate
    the pedestrian access route.

    travel_date is YYYY-MM-DD. If omitted, use today's date in Kolkata time and
    report that the date was defaulted. During 15–21 October 2026, filter lines
    using the project's published special-service notice. Never include a line
    marked suspended on the requested date. For other dates, use the static
    network only and warn that no timetable for that date is supplied.

    The result is a station-to-station network path with line changes. It is
    NOT a live train tracker, exact departure itinerary, fare calculator, or
    travel-time estimate. Do not invent journey duration, fares, live status,
    walking distance, or service outside the provided sources.

    Prefer fewer line changes, then fewer station-to-station segments. If no
    path is available because a needed line is suspended, say so clearly rather
    than suggesting the suspended line.

    USER-FACING RESPONSE GUIDANCE:
    - Lead with a one-sentence answer saying whether a Metro-only route is available.
    - If available, present the route as short numbered steps using user_facing_steps.
      Clearly name the line, boarding station, alighting station, and each interchange.
    - Show the requested travel date. If omitted, say that today's Kolkata date was assumed.
    - Do not dump the raw JSON, station IDs, graph states, or internal optimization details.
    - Do not call the route the fastest or estimate journey duration; the route is chosen
      by fewest line changes, then fewest station hops, not by elapsed time.
    - State the network-draft warning in one brief sentence in the actual answer; do not omit it.
    - Include relevant user_facing_warnings in the natural-language answer, especially station-specific restrictions and the draft-network caveat.
    - If either endpoint is Kavi Subhash, state both restrictions distinctly when applicable:
      (1) the supplied network lists the Kavi Subhash–Beleghata Orange Line section, which may
      be suspended for the requested date; and (2) Blue Line passenger service at Kavi Subhash
      is separately listed as suspended, with Shahid Khudiram as the Blue Line passenger terminal.
      Never claim that Kavi Subhash is only on the Orange Line, and never conflate these restrictions.
      When returned, preserve the explicit Blue Line restriction sentence in user_facing_summary.
    - For pandal endpoints, explain that the nearest-station link uses straight-line distance;
      walking directions and real walking distance are not provided.
    - If a line is suspended or its schedule is unconfirmed, put that limitation near the top.
    - End with a brief note to check Metro Railway for amendments or live disruptions.
    - Prefer user_facing_summary, user_facing_steps, and user_facing_warnings over technical fields.
    """
    raw_origin = str(origin or "").strip()
    raw_destination = str(destination or "").strip()
    if not raw_origin or not raw_destination:
        return {"status": "error", "error": "Both origin and destination are required."}

    date_was_defaulted = not str(travel_date or "").strip()
    raw_date = str(travel_date or "").strip()
    if date_was_defaulted:
        raw_date = datetime.now(KOLKATA_TIMEZONE).date().isoformat()
    try:
        requested_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        if requested_date.isoformat() != raw_date:
            raise ValueError
    except (TypeError, ValueError):
        return {"status": "error", "error": "travel_date must use YYYY-MM-DD format, for example 2026-10-18."}

    friendly_travel_date = requested_date.strftime("%d %B %Y").lstrip("0")

    network = _load_metro_network_for_route_planner()
    if "_error" in network:
        return {"status": "unavailable", "error": network["_error"]}

    network_stations = [s for s in network.get("stations", []) if isinstance(s, dict) and s.get("id") and s.get("name")]
    line_records = [l for l in network.get("lines", []) if isinstance(l, dict) and l.get("line_id") and l.get("line_name")]
    if not network_stations or not line_records:
        return {"status": "unavailable", "error": "Metro network file contains no usable stations or lines."}

    station_by_id = {str(s["id"]).strip(): s for s in network_stations}
    line_name_by_id = _metro_line_name_map(network)
    line_record_by_id = {str(l["line_id"]).strip().upper(): l for l in line_records}
    line_state_by_id = {}
    route_warnings = []

    network_status = str(network.get("status", "unknown"))
    network_draft_notice = ""
    if network_status.upper() in {"DRAFT_FOR_REVIEW", "DRAFT", "UNVERIFIED"}:
        network_draft_notice = (
            "This route uses a draft Metro station network that has not been fully "
            "verified; confirm the station sequence and interchange with Metro Railway "
            "before travelling."
        )
        route_warnings.append(network_draft_notice)

    special_coverage = metro_puja_special_services_data.get("coverage", {})
    coverage_start = str(special_coverage.get("start_date", ""))
    coverage_end = str(special_coverage.get("end_date", ""))
    in_special_coverage = bool(coverage_start and coverage_end and coverage_start <= raw_date <= coverage_end)

    blocked_base_statuses = {"closed", "suspended", "not_operational", "under_construction", "not_commissioned"}
    active_line_ids = set()
    line_service_details = {}
    for line_id, line_record in line_record_by_id.items():
        base_status = str(line_record.get("status", "operational")).casefold()
        state = _metro_schedule_state_for_date(line_record.get("line_name", ""), raw_date, network)
        status = str(state.get("status", "unknown")).casefold()
        line_state_by_id[line_id] = state
        if status == "suspended":
            continue
        if base_status in blocked_base_statuses:
            continue
        active_line_ids.add(line_id)
        period = state.get("period") if isinstance(state.get("period"), dict) else {}
        line_service_details[line_id] = {
            "line_id": line_id,
            "line_name": line_record.get("line_name"),
            "service_status_on_date": "scheduled" if status == "scheduled" else "not_confirmed_by_special_schedule",
            "service_window_start": period.get("service_window_start"),
            "service_window_end": period.get("service_window_end"),
            "service_window_end_day_offset": period.get("service_window_end_day_offset"),
            "peak_frequency_minutes": period.get("peak_frequency_minutes"),
            "daily_services_total": period.get("daily_services_total"),
            "schedule_source": state.get("source"),
        }
        if status != "scheduled":
            route_warnings.append(
                f"No matching special-service schedule confirms {line_record.get('line_name')} for {raw_date}; the route uses the static network layout only."
            )

    if not in_special_coverage:
        route_warnings.append(
            f"The supplied Puja special-service notice covers {coverage_start or 'an unspecified start date'} to {coverage_end or 'an unspecified end date'} only; normal operating hours for {raw_date} are not provided."
        )

    # Exclude station/line pairs affected by permanent or long-running advisories,
    # such as Blue Line passenger service at Kavi Subhash.
    blocked_station_line_pairs = set()
    effective_station_line_advisories = []
    for advisory in network.get("temporary_service_advisories", []):
        if not isinstance(advisory, dict):
            continue
        if advisory.get("type") == "station_line_closure":
            start = str(advisory.get("effective_from", ""))
            end = str(advisory.get("effective_to", "9999-12-31"))
            if not start or start <= raw_date <= end:
                station_id = str(advisory.get("station_id", "")).strip()
                line_id = str(advisory.get("line_id", "")).strip().upper()
                blocked_station_line_pairs.add((station_id, line_id))
                effective_station_line_advisories.append(advisory)

    # Compatibility fallback for the currently supplied draft network. That JSON
    # has no temporary_service_advisories entry, although its Blue Line special-
    # service records list Shahid Khudiram as a terminal and omit Kavi Subhash.
    # Only infer a station/line closure when all the evidence below agrees:
    #   * the requested date has an explicitly scheduled Blue Line period;
    #   * the period's first/last service records include Shahid Khudiram;
    #   * those terminal records do not include Kavi Subhash; and
    #   * Kavi Subhash appears after Shahid Khudiram in the draft Blue Line order.
    # An explicit, date-effective network advisory always takes precedence.
    has_explicit_kavi_blue_advisory = any(
        str(item.get("line_id", "")).strip().upper() == "BLUE"
        and _metro_normalize_name(item.get("station_name", "")) == "kavi subhash"
        for item in effective_station_line_advisories
    )
    if not has_explicit_kavi_blue_advisory:
        blue_state = line_state_by_id.get("BLUE", {})
        blue_period = blue_state.get("period")
        blue_line_record = line_record_by_id.get("BLUE", {})
        blue_station_order = [str(value).strip() for value in blue_line_record.get("station_ids", [])]
        kavi_station = next(
            (
                station for station in network_stations
                if _metro_normalize_name(station.get("name", "")) == "kavi subhash"
                and "BLUE" in {
                    str(value).strip().upper()
                    for value in station.get("line_ids", station.get("operational_line_ids", []))
                }
            ),
            None,
        )
        shahid_station = next(
            (
                station for station in network_stations
                if _metro_normalize_name(station.get("name", "")) == "shahid khudiram"
            ),
            None,
        )
        if (
            str(blue_state.get("status", "")).casefold() == "scheduled"
            and isinstance(blue_period, dict)
            and isinstance(kavi_station, dict)
            and isinstance(shahid_station, dict)
        ):
            terminal_records = []
            for key in ("first_services", "last_services"):
                rows = blue_period.get(key, [])
                if isinstance(rows, list):
                    terminal_records.extend(row for row in rows if isinstance(row, dict))
            terminal_names = {
                _metro_normalize_name(row.get(field, ""))
                for row in terminal_records
                for field in ("from", "to")
                if row.get(field)
            }
            kavi_id = str(kavi_station.get("id", "")).strip()
            shahid_id = str(shahid_station.get("id", "")).strip()
            try:
                kavi_position = blue_station_order.index(kavi_id)
                shahid_position = blue_station_order.index(shahid_id)
            except ValueError:
                kavi_position = shahid_position = -1
            if (
                "shahid khudiram" in terminal_names
                and "kavi subhash" not in terminal_names
                and shahid_position >= 0
                and kavi_position > shahid_position
                and (kavi_id, "BLUE") not in blocked_station_line_pairs
            ):
                inferred_advisory = {
                    "type": "station_line_closure",
                    "station_id": kavi_id,
                    "station_name": "Kavi Subhash",
                    "line_id": "BLUE",
                    "status": "passenger_service_suspended",
                    "effective_from": raw_date,
                    "effective_to": raw_date,
                    "source": "metro_puja_special_services_2026.json",
                    "passenger_terminal_station_name": str(shahid_station.get("name", "Shahid Khudiram")),
                    "reason": (
                        "The scheduled Blue Line first/last service records list Shahid Khudiram "
                        "as a terminal and do not list Kavi Subhash as a Blue Line terminal."
                    ),
                }
                blocked_station_line_pairs.add((kavi_id, "BLUE"))
                effective_station_line_advisories.append(inferred_advisory)

    # Construct a graph whose nodes are (station, line), so transfers occur only
    # at explicit interchange stations listed by the reviewed network dataset.
    adjacency = {}
    usable_station_ids = set()
    usable_connections = 0
    for connection in network.get("connections", []):
        if not isinstance(connection, dict):
            continue
        line_id = str(connection.get("line_id", "")).strip().upper()
        start_id = str(connection.get("from_station_id", "")).strip()
        end_id = str(connection.get("to_station_id", "")).strip()
        if line_id not in active_line_ids or start_id not in station_by_id or end_id not in station_by_id:
            continue
        if connection.get("operational_as_of_map") is False:
            continue
        if (start_id, line_id) in blocked_station_line_pairs or (end_id, line_id) in blocked_station_line_pairs:
            continue
        direction = str(connection.get("travel_direction", "both")).casefold()
        a, b = (start_id, line_id), (end_id, line_id)
        adjacency.setdefault(a, [])
        adjacency.setdefault(b, [])
        adjacency[a].append((b, "ride"))
        if direction in {"both", "reverse", "backward", "two_way", "two-way"}:
            adjacency[b].append((a, "ride"))
        usable_station_ids.update((start_id, end_id))
        usable_connections += 1

    # Add transfer edges only at explicit interchanges, and only for line states
    # that have actual available ride edges for the requested date.
    for interchange in network.get("interchanges", []):
        if not isinstance(interchange, dict):
            continue
        station_id = str(interchange.get("station_id", "")).strip()
        operational_line_ids = [str(v).strip().upper() for v in interchange.get("operational_line_ids", [])]
        states = [
            (station_id, lid) for lid in operational_line_ids
            if lid in active_line_ids and (station_id, lid) in adjacency
            and (station_id, lid) not in {pair for pair in blocked_station_line_pairs}
        ]
        for i, state_a in enumerate(states):
            for state_b in states[i + 1:]:
                adjacency[state_a].append((state_b, "transfer"))
                adjacency[state_b].append((state_a, "transfer"))

    start_resolved = _metro_resolve_endpoint(raw_origin, network_stations, usable_station_ids)
    if start_resolved.get("status") != "found":
        return {"status": start_resolved.get("status", "not_found"), "error": start_resolved.get("message", f"Could not resolve origin '{raw_origin}'."), "candidates": start_resolved.get("candidates", []), "travel_date": raw_date}
    end_resolved = _metro_resolve_endpoint(raw_destination, network_stations, usable_station_ids)
    if end_resolved.get("status") != "found":
        return {"status": end_resolved.get("status", "not_found"), "error": end_resolved.get("message", f"Could not resolve destination '{raw_destination}'."), "candidates": end_resolved.get("candidates", []), "travel_date": raw_date}

    start_station_id = str(start_resolved.get("station_id", ""))
    end_station_id = str(end_resolved.get("station_id", ""))

    # Convert relevant station/line advisories into clear user-facing notices.
    # Keep station closures distinct from whole-line suspensions.
    endpoint_station_ids = {start_station_id, end_station_id}
    user_facing_station_restrictions = []
    for advisory in effective_station_line_advisories:
        advisory_station_id = str(advisory.get("station_id", "")).strip()
        line_id = str(advisory.get("line_id", "")).strip().upper()
        if advisory_station_id not in endpoint_station_ids:
            continue
        station_name = (
            station_by_id.get(advisory_station_id, {}).get("name")
            or advisory.get("station_name")
            or advisory_station_id
        )
        line_name = line_name_by_id.get(line_id, line_id or "Metro line")
        status_text = str(advisory.get("status", "")).casefold()
        if status_text in {"passenger_service_suspended", "suspended", "closed"}:
            note = f"{line_name} passenger service is listed as suspended at {station_name}."
        else:
            note = f"The supplied network marks {line_name} service at {station_name} as restricted."

        # Prefer a date-specific terminal inferred from the special-service notice.
        # The draft network's Blue Line station_ids currently ends at Kavi Subhash,
        # so blindly using its final station would contradict the passenger-service
        # restriction derived above. Only fall back to station_ids for advisories
        # that do not carry an explicit terminal name.
        if line_id == "BLUE":
            terminal_name = str(advisory.get("passenger_terminal_station_name") or "").strip()
            if not terminal_name:
                blue_record = line_record_by_id.get("BLUE", {})
                blue_station_ids = [str(x) for x in blue_record.get("station_ids", [])]
                terminal_id = blue_station_ids[-1] if blue_station_ids else ""
                terminal_name = station_by_id.get(terminal_id, {}).get("name")
            if terminal_name:
                note += f" {terminal_name} is listed as the Blue Line passenger terminal."
                note += " This Blue Line restriction is separate from Orange Line service status."
        if note not in user_facing_station_restrictions:
            user_facing_station_restrictions.append(note)

    user_facing_common_warnings = []
    if network_draft_notice:
        user_facing_common_warnings.append(network_draft_notice)
    user_facing_common_warnings.extend(user_facing_station_restrictions)

    start_states = [state for state in adjacency if state[0] == start_station_id]
    end_states = {state for state in adjacency if state[0] == end_station_id}

    # If the endpoint's only lines are suspended, return the reason explicitly.
    def suspended_lines_at_station(station_id: str) -> list[str]:
        line_ids = set()
        station = station_by_id.get(station_id, {})
        for lid in station.get("line_ids", station.get("operational_line_ids", [])):
            state = _metro_schedule_state_for_date(line_name_by_id.get(str(lid).upper(), str(lid)), raw_date, network)
            if str(state.get("status", "")).casefold() == "suspended":
                line_ids.add(line_name_by_id.get(str(lid).upper(), str(lid)))
        for line_id, line in line_record_by_id.items():
            if station_id in [str(x) for x in line.get("station_ids", [])]:
                state = line_state_by_id.get(line_id, {})
                if str(state.get("status", "")).casefold() == "suspended":
                    line_ids.add(line.get("line_name", line_id))
        return sorted(line_ids)

    if not start_states or not end_states:
        unavailable_lines = sorted(set(suspended_lines_at_station(start_station_id) + suspended_lines_at_station(end_station_id)))
        restriction_reasons = list(user_facing_station_restrictions)

        # Explain any suspended line that serves one of the endpoints separately
        # from a station-specific closure (for example, Orange suspension vs the
        # Blue Line passenger-service closure at Kavi Subhash).
        endpoint_line_suspensions = []
        endpoint_ids_and_names = (
            (start_station_id, str(start_resolved.get("name") or "origin")),
            (end_station_id, str(end_resolved.get("name") or "destination")),
        )
        for line_name in unavailable_lines:
            line_id = next(
                (lid for lid, name in line_name_by_id.items() if _metro_normalize_name(name) == _metro_normalize_name(line_name)),
                "",
            )
            line_record = line_record_by_id.get(line_id, {})
            line_station_ids = {str(x) for x in line_record.get("station_ids", [])}
            connected_endpoint_names = []
            # Include stations declared by the station record as well, because
            # endpoint stations may not be present on the active ride graph.
            for sid, endpoint_name in endpoint_ids_and_names:
                station = station_by_id.get(sid, {})
                endpoint_line_ids = {
                    str(x).strip().upper()
                    for x in station.get("line_ids", station.get("operational_line_ids", []))
                }
                if (sid in line_station_ids or line_id in endpoint_line_ids) and endpoint_name not in connected_endpoint_names:
                    connected_endpoint_names.append(endpoint_name)
            if connected_endpoint_names:
                if len(connected_endpoint_names) == 2:
                    endpoint_list = f"both {connected_endpoint_names[0]} and {connected_endpoint_names[1]}"
                else:
                    endpoint_list = connected_endpoint_names[0]
                endpoint_line_suspensions.append(
                    f"Separately, the {line_name} is listed as suspended on {friendly_travel_date}. "
                    f"The supplied network shows {endpoint_list} on that line."
                )
        restriction_reasons.extend(endpoint_line_suspensions)

        # Keep the Kavi Subhash restrictions explicit in the primary message.
        # Kavi Subhash is the Orange Line endpoint in this network, while its
        # Blue Line passenger service is separately listed as suspended.
        resolved_endpoint_names = {
            _metro_normalize_name(start_resolved.get("name", "")),
            _metro_normalize_name(end_resolved.get("name", "")),
        }
        kavi_subhash_is_endpoint = "kavi subhash" in resolved_endpoint_names
        blue_kavi_restriction_note = ""
        if kavi_subhash_is_endpoint:
            has_kavi_blue_advisory = any(
                str(advisory.get("station_id", "")).strip() == start_station_id
                or str(advisory.get("station_id", "")).strip() == end_station_id
                for advisory in effective_station_line_advisories
                if str(advisory.get("line_id", "")).strip().upper() == "BLUE"
                and str(advisory.get("station_name", "")).strip().casefold() == "kavi subhash"
            )
            if has_kavi_blue_advisory:
                blue_kavi_restriction_note = (
                    "Separately, Blue Line passenger service at Kavi Subhash is listed as suspended; "
                    "the supplied draft network lists Shahid Khudiram as the Blue Line passenger terminal. "
                    "This Blue Line restriction is distinct from the Orange Line suspension."
                )
                if not any(
                    "blue line" in reason.casefold()
                    and "kavi subhash" in reason.casefold()
                    and "suspend" in reason.casefold()
                    for reason in restriction_reasons
                ):
                    restriction_reasons.append(blue_kavi_restriction_note)

        if unavailable_lines:
            message = (
                "No Metro-only route is available for "
                f"{start_resolved.get('name')} to {end_resolved.get('name')} on {friendly_travel_date}. "
                "The required line is listed as suspended: " + ", ".join(unavailable_lines) + "."
            )
        else:
            message = (
                "No Metro-only route is available between these endpoints on "
                f"{friendly_travel_date} in the supplied station network."
            )

        # Make the distinction visible even if a client displays `message`
        # instead of the richer `user_facing_summary` field.
        if blue_kavi_restriction_note:
            message += " " + blue_kavi_restriction_note

        user_facing_summary = (
            f"No Metro-only route is available from {start_resolved.get('name')} "
            f"to {end_resolved.get('name')} on {friendly_travel_date}."
        )
        if restriction_reasons:
            user_facing_summary += " " + " ".join(restriction_reasons)
        elif unavailable_lines:
            user_facing_summary += (
                " The supplied service data lists these required lines as suspended: "
                + ", ".join(unavailable_lines) + "."
            )
        if network_draft_notice:
            user_facing_summary += " " + network_draft_notice
        user_facing_summary += " Check Metro Railway for any newer official notice before travelling."
        return {
            "status": "no_route_for_date",
            "origin": {"name": start_resolved.get("name"), "station_id": start_station_id, "type": start_resolved.get("type")},
            "destination": {"name": end_resolved.get("name"), "station_id": end_station_id, "type": end_resolved.get("type")},
            "travel_date": raw_date,
            "unavailable_lines": unavailable_lines,
            "message": message,
            "user_facing_summary": user_facing_summary,
            "user_facing_restrictions": restriction_reasons,
            "user_facing_warnings": user_facing_common_warnings + endpoint_line_suspensions,
            "user_facing_next_step": (
                "Check Metro Railway's latest notice and use a non-Metro option such as a bus, auto, or taxi if the suspension remains in effect. "
                "This tool does not calculate bus or walking connections."
            ),
            "warnings": route_warnings,
            "recommendation": "Use another available transport option or check for a later official service notice; the planner will not recommend a suspended line.",
        }

    if start_station_id == end_station_id:
        same_station_access = {}
        for endpoint_key, resolved in (("origin_access", start_resolved), ("destination_access", end_resolved)):
            if resolved.get("type") == "pandal":
                same_station_access[endpoint_key] = {
                    "pandal_name": resolved.get("name"),
                    "nearest_metro_station": resolved.get("nearest_metro_station"),
                    "distance_km_straight_line": resolved.get("access_distance_km_straight_line"),
                    "note": resolved.get("access_distance_note"),
                }
        return {
            "status": "success",
            "travel_date": raw_date,
            "travel_date_defaulted": date_was_defaulted,
            "timezone": "Asia/Kolkata",
            "origin": {"input": raw_origin, "name": start_resolved.get("name"), "station_id": start_station_id, "type": start_resolved.get("type")},
            "destination": {"input": raw_destination, "name": end_resolved.get("name"), "station_id": end_station_id, "type": end_resolved.get("type")},
            "route_summary": "Origin and destination resolve to the same Metro station; no Metro ride is needed.",
            "user_facing_summary": (
                f"Both endpoints connect to {station_by_id[start_station_id].get('name', start_station_id)}. "
                "No Metro ride or line change is needed between them."
            ),
            "user_facing_steps": [
                "Use the same Metro station for both endpoints; no train journey is needed."
            ],
            "segments": [], "transfers": [], "station_sequence": [{"id": start_station_id, "name": station_by_id[start_station_id].get("name")}],
            "warnings": route_warnings,
            "user_facing_warnings": user_facing_common_warnings,
            **same_station_access,
        }

    # Dijkstra with lexicographic cost: minimize interchanges first, then rail hops.
    best_cost = {}
    previous = {}
    heap = []
    for state in start_states:
        best_cost[state] = (0, 0)
        heapq.heappush(heap, (0, 0, state[0], state[1]))
    end_state = None
    while heap:
        transfers, stops, station_id, line_id = heapq.heappop(heap)
        state = (station_id, line_id)
        if best_cost.get(state) != (transfers, stops):
            continue
        if state in end_states:
            end_state = state
            break
        for neighbor, edge_type in adjacency.get(state, []):
            extra_transfer = 1 if edge_type == "transfer" else 0
            extra_stop = 1 if edge_type == "ride" else 0
            candidate = (transfers + extra_transfer, stops + extra_stop)
            if candidate < best_cost.get(neighbor, (10**9, 10**9)):
                best_cost[neighbor] = candidate
                previous[neighbor] = (state, edge_type)
                heapq.heappush(heap, (candidate[0], candidate[1], neighbor[0], neighbor[1]))

    if end_state is None:
        unavailable_lines = sorted(set(suspended_lines_at_station(start_station_id) + suspended_lines_at_station(end_station_id)))
        possible_lines = sorted({str(x.get("line_name", "")) for x in line_records if str(x.get("line_id", "")).upper() in active_line_ids})
        return {
            "status": "no_metro_only_route",
            "travel_date": raw_date,
            "origin": {"name": start_resolved.get("name"), "station_id": start_station_id, "type": start_resolved.get("type")},
            "destination": {"name": end_resolved.get("name"), "station_id": end_station_id, "type": end_resolved.get("type")},
            "unavailable_lines_at_endpoints": unavailable_lines,
            "message": "No Metro-only path connects these endpoints in the supplied network for this date. A bus or walking connection may be needed, but is not calculated by this tool.",
            "user_facing_summary": (
                "I couldn't find a connected Metro-only route between these endpoints "
                "for the selected date in the current station network. A bus or walking "
                "connection may be needed; this tool does not calculate those connections."
            ),
            "user_facing_next_step": "Try another pair of stations or ask for a separate walking/driving route.",
            "available_line_names_in_graph": possible_lines,
            "warnings": route_warnings,
        }

    path_states = [end_state]
    while path_states[-1] not in start_states:
        prev = previous.get(path_states[-1])
        if prev is None:
            break
        path_states.append(prev[0])
    path_states.reverse()

    # Convert state path into ride segments, with transfers at explicit interchanges.
    segments = []
    current_line = path_states[0][1]
    current_ids = [path_states[0][0]]
    for station_id, next_line in path_states[1:]:
        if next_line == current_line:
            if station_id != current_ids[-1]:
                current_ids.append(station_id)
        else:
            segments.append({
                "line_id": current_line,
                "line_name": line_name_by_id.get(current_line, current_line),
                "station_ids": current_ids[:],
                "stations": [station_by_id[sid].get("name", sid) for sid in current_ids],
                "from_station": station_by_id[current_ids[0]].get("name", current_ids[0]),
                "to_station": station_by_id[current_ids[-1]].get("name", current_ids[-1]),
                "station_to_station_hops": max(0, len(current_ids) - 1),
                "service": line_service_details.get(current_line, {
                    "line_id": current_line,
                    "line_name": line_name_by_id.get(current_line, current_line),
                    "service_status_on_date": "not_confirmed_by_special_schedule",
                    "operating_window_start": None,
                    "operating_window_end": None,
                    "schedule_source": "static network only",
                }),
            })
            current_line = next_line
            current_ids = [station_id]
    segments.append({
        "line_id": current_line,
        "line_name": line_name_by_id.get(current_line, current_line),
        "station_ids": current_ids[:],
        "stations": [station_by_id[sid].get("name", sid) for sid in current_ids],
        "from_station": station_by_id[current_ids[0]].get("name", current_ids[0]),
        "to_station": station_by_id[current_ids[-1]].get("name", current_ids[-1]),
        "station_to_station_hops": max(0, len(current_ids) - 1),
        "service": line_service_details.get(current_line, {
            "line_id": current_line,
            "line_name": line_name_by_id.get(current_line, current_line),
            "service_status_on_date": "not_confirmed_by_special_schedule",
            "operating_window_start": None,
            "operating_window_end": None,
            "schedule_source": "static network only",
        }),
    })

    station_sequence_ids = []
    for station_id, _line_id in path_states:
        if not station_sequence_ids or station_sequence_ids[-1] != station_id:
            station_sequence_ids.append(station_id)
    station_sequence = [{"id": sid, "name": station_by_id[sid].get("name", sid)} for sid in station_sequence_ids]

    transfers = []
    for previous_segment, next_segment in zip(segments, segments[1:]):
        transfer_station_id = previous_segment["station_ids"][-1]
        if transfer_station_id == next_segment["station_ids"][0]:
            transfers.append({
                "station_id": transfer_station_id,
                "station_name": station_by_id[transfer_station_id].get("name", transfer_station_id),
                "from_line": previous_segment["line_name"],
                "to_line": next_segment["line_name"],
            })

    endpoint_access = {}
    for endpoint_key, resolved in (("origin_access", start_resolved), ("destination_access", end_resolved)):
        if resolved.get("type") == "pandal":
            endpoint_access[endpoint_key] = {
                "pandal_name": resolved.get("name"),
                "nearest_metro_station": resolved.get("nearest_metro_station"),
                "distance_km_straight_line": resolved.get("access_distance_km_straight_line"),
                "note": resolved.get("access_distance_note"),
            }

    route_line_names = [segment["line_name"] for segment in segments]
    if len(segments) == 1:
        route_summary = f"Take the {segments[0]['line_name']} from {segments[0]['from_station']} to {segments[0]['to_station']}."
    else:
        route_summary = "Take " + " then ".join(route_line_names) + "; change lines at " + ", ".join(t["station_name"] for t in transfers) + "."

    # Build concise, readable route steps for the model to present directly.
    user_facing_steps = []
    if start_resolved.get("type") == "pandal":
        user_facing_steps.append(
            f"From {start_resolved.get('name')}, reach {start_resolved.get('nearest_metro_station')} Metro station. "
            f"The approximate straight-line distance is {start_resolved.get('access_distance_km_straight_line')} km; "
            "actual walking directions and walking distance are not available here."
        )
    for index, segment in enumerate(segments):
        stations_on_segment = segment.get("stations", [])
        step = (
            f"Take the {segment['line_name']} from {segment['from_station']} "
            f"to {segment['to_station']}."
        )
        if len(stations_on_segment) > 2:
            via = stations_on_segment[1:-1]
            intermediate_count = len(via)
            noun = "station" if intermediate_count == 1 else "stations"
            step += (
                f" Pass {intermediate_count} intermediate {noun}: "
                + ", ".join(via)
                + f"; then arrive at {segment['to_station']} after "
                f"{segment['station_to_station_hops']} station-to-station hops."
            )
        elif segment.get("station_to_station_hops", 0) > 0:
            step += f" This is {segment['station_to_station_hops']} station-to-station hop."
        user_facing_steps.append(step)
        if index < len(transfers):
            transfer = transfers[index]
            user_facing_steps.append(
                f"At {transfer['station_name']}, change from the {transfer['from_line']} "
                f"to the {transfer['to_line']}."
            )
    if end_resolved.get("type") == "pandal":
        user_facing_steps.append(
            f"From {end_resolved.get('nearest_metro_station')} Metro station to {end_resolved.get('name')}, "
            f"the straight-line distance is about {end_resolved.get('access_distance_km_straight_line')} km. "
            "Walking directions are not included."
        )

    schedule_known = all(segment.get("service", {}).get("service_status_on_date") == "scheduled" for segment in segments)
    unique_line_services = {}
    for segment in segments:
        service = segment.get("service", {})
        line_id = str(service.get("line_id") or segment.get("line_id") or "").upper()
        if line_id and line_id not in unique_line_services:
            unique_line_services[line_id] = service
    user_facing_service_notes = [
        _metro_user_facing_service_note(service, raw_date)
        for service in unique_line_services.values()
    ]
    if not schedule_known:
        route_warnings.append("A station path was found, but the supplied special-service notice does not confirm every line for this date. Check the published timetable before travelling.")
    route_warnings.append("This is a station-to-station route suggestion, not live train status. Exact departure times, fares, and total journey duration are not available here.")

    return {
        "status": "success",
        "tool": "plan_metro_journey",
        "travel_date": raw_date,
        "travel_date_defaulted": date_was_defaulted,
        "timezone": "Asia/Kolkata",
        "origin": {"input": raw_origin, "name": start_resolved.get("name"), "station_id": start_station_id, "type": start_resolved.get("type")},
        "destination": {"input": raw_destination, "name": end_resolved.get("name"), "station_id": end_station_id, "type": end_resolved.get("type")},
        "route_summary": route_summary,
        "user_facing_summary": (
            f"A Metro-only route is available from {start_resolved.get('name')} to {end_resolved.get('name')} on {friendly_travel_date}: {route_summary}"
            + (
                f" I assumed the travel date is {raw_date} in Kolkata time because no date was provided."
                if date_was_defaulted else ""
            )
            + (" " + network_draft_notice if network_draft_notice else "")
            + (" " + " ".join(user_facing_station_restrictions) if user_facing_station_restrictions else "")
            + " Check Metro Railway for any later official amendments or live disruptions."
        ),
        "user_facing_steps": user_facing_steps,
        "user_facing_warnings": user_facing_common_warnings + [
            "This is a station-to-station route suggestion, not live train status. Exact departure times, fares, and total journey duration are not available here."
        ],
        "user_facing_service_notes": user_facing_service_notes,
        "user_facing_transfer_summary": (
            "No line changes are needed." if not transfers else
            "Line changes: " + "; ".join(
                f"change from {t['from_line']} to {t['to_line']} at {t['station_name']}"
                for t in transfers
            ) + "."
        ),
        "route_selection": "Fewest line changes, then fewest station-to-station hops; not optimized for elapsed travel time.",
        "station_count_including_endpoints": len(station_sequence),
        "station_to_station_hops": max(0, len(station_sequence) - 1),
        "transfers_count": len(transfers),
        "transfers": transfers,
        "segments": segments,
        "station_sequence": station_sequence,
        "line_service_check": [line_service_details.get(seg["line_id"], seg["service"]) for seg in segments],
        "network_source": network.get("primary_source", {}),
        "network_status": network_status,
        "service_schedule_source": metro_puja_special_services_data.get("source", {}),
        "warnings": list(dict.fromkeys(route_warnings)),
        "limitations": [
            "No exact train-by-train timetable, fare, live delay, or journey duration is available in these files.",
            "Special-service information is date-specific and may be amended by Metro Railway.",
            "The Purple Line is isolated in this Metro-only graph; no bus, rail, or walking transfer connection to other lines is defined.",
            "For pandal endpoints, nearest-station access is a straight-line distance, not a walking route.",
        ],
        **endpoint_access,
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