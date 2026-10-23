"""Small geodesy helpers."""

import math

EARTH_RADIUS_M = 6371000.0
_COMPASS = ["north", "north-east", "east", "south-east",
            "south", "south-west", "west", "north-west"]


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two coordinates."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compass_direction(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Eight-point compass direction from point 1 toward point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    return _COMPASS[int((bearing + 22.5) // 45) % 8]


def describe_distance(meters: float) -> str:
    """Human distance in US units, e.g. '450 feet' or '1.2 miles'."""
    feet = meters * 3.28084
    if feet < 1000:
        return f"{int(round(feet, -1))} feet"
    miles = meters / 1609.344
    return f"{miles:.1f} miles"
