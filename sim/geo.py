"""US destinations, regions, node locations and carrier transit times."""
from __future__ import annotations

import math

# city, state, zip, lat, lon, weight (roughly metro population, millions)
CITIES = [
    ("New York", "NY", "10001", 40.75, -73.99, 19.5), ("Brooklyn", "NY", "11215", 40.67, -73.98, 2.5),
    ("Los Angeles", "CA", "90012", 34.05, -118.24, 13.0), ("San Diego", "CA", "92101", 32.72, -117.16, 3.3),
    ("San Jose", "CA", "95113", 37.34, -121.89, 2.0), ("San Francisco", "CA", "94103", 37.77, -122.41, 4.7),
    ("Sacramento", "CA", "95814", 38.58, -121.49, 2.4), ("Fresno", "CA", "93721", 36.74, -119.78, 1.0),
    ("Chicago", "IL", "60601", 41.88, -87.62, 9.4), ("Houston", "TX", "77002", 29.76, -95.37, 7.1),
    ("Dallas", "TX", "75201", 32.78, -96.80, 7.6), ("Austin", "TX", "78701", 30.27, -97.74, 2.3),
    ("San Antonio", "TX", "78205", 29.42, -98.49, 2.6), ("El Paso", "TX", "79901", 31.76, -106.49, 0.9),
    ("Phoenix", "AZ", "85004", 33.45, -112.07, 4.9), ("Tucson", "AZ", "85701", 32.22, -110.97, 1.0),
    ("Philadelphia", "PA", "19107", 39.95, -75.16, 6.2), ("Pittsburgh", "PA", "15222", 40.44, -79.99, 2.4),
    ("Jacksonville", "FL", "32202", 30.33, -81.66, 1.6), ("Miami", "FL", "33130", 25.77, -80.20, 6.1),
    ("Orlando", "FL", "32801", 28.54, -81.38, 2.7), ("Tampa", "FL", "33602", 27.95, -82.46, 3.2),
    ("Columbus", "OH", "43215", 39.96, -83.00, 2.1), ("Cleveland", "OH", "44113", 41.50, -81.69, 2.1),
    ("Cincinnati", "OH", "45202", 39.10, -84.51, 2.2), ("Charlotte", "NC", "28202", 35.23, -80.84, 2.7),
    ("Raleigh", "NC", "27601", 35.78, -78.64, 1.4), ("Indianapolis", "IN", "46204", 39.77, -86.16, 2.1),
    ("Seattle", "WA", "98101", 47.61, -122.33, 4.0), ("Spokane", "WA", "99201", 47.66, -117.43, 0.6),
    ("Denver", "CO", "80202", 39.75, -104.99, 3.0), ("Colorado Springs", "CO", "80903", 38.83, -104.82, 0.8),
    ("Washington", "DC", "20001", 38.91, -77.02, 6.3), ("Boston", "MA", "02108", 42.36, -71.06, 4.9),
    ("Nashville", "TN", "37203", 36.15, -86.78, 2.0), ("Memphis", "TN", "38103", 35.15, -90.05, 1.3),
    ("Detroit", "MI", "48226", 42.33, -83.05, 4.4), ("Grand Rapids", "MI", "49503", 42.96, -85.66, 1.1),
    ("Portland", "OR", "97204", 45.52, -122.68, 2.5), ("Eugene", "OR", "97401", 44.05, -123.09, 0.4),
    ("Las Vegas", "NV", "89101", 36.17, -115.14, 2.3), ("Reno", "NV", "89501", 39.53, -119.81, 0.5),
    ("Louisville", "KY", "40202", 38.25, -85.76, 1.3), ("Baltimore", "MD", "21202", 39.29, -76.61, 2.8),
    ("Milwaukee", "WI", "53202", 43.04, -87.91, 1.6), ("Madison", "WI", "53703", 43.07, -89.38, 0.7),
    ("Albuquerque", "NM", "87102", 35.08, -106.65, 0.9), ("Oklahoma City", "OK", "73102", 35.47, -97.52, 1.4),
    ("Tulsa", "OK", "74103", 36.15, -95.99, 1.0), ("Kansas City", "MO", "64106", 39.10, -94.58, 2.2),
    ("St. Louis", "MO", "63101", 38.63, -90.20, 2.8), ("Atlanta", "GA", "30303", 33.75, -84.39, 6.1),
    ("Savannah", "GA", "31401", 32.08, -81.09, 0.4), ("Minneapolis", "MN", "55401", 44.98, -93.27, 3.7),
    ("Omaha", "NE", "68102", 41.26, -95.94, 1.0), ("New Orleans", "LA", "70112", 29.95, -90.07, 1.3),
    ("Salt Lake City", "UT", "84101", 40.76, -111.89, 1.3), ("Boise", "ID", "83702", 43.62, -116.20, 0.8),
    ("Birmingham", "AL", "35203", 33.52, -86.81, 1.1), ("Richmond", "VA", "23219", 37.54, -77.44, 1.3),
    ("Virginia Beach", "VA", "23451", 36.85, -75.98, 1.8), ("Newark", "NJ", "07102", 40.74, -74.17, 2.0),
    ("Hartford", "CT", "06103", 41.77, -72.67, 1.2), ("Providence", "RI", "02903", 41.82, -71.41, 1.7),
    ("Des Moines", "IA", "50309", 41.59, -93.62, 0.7), ("Little Rock", "AR", "72201", 34.75, -92.29, 0.8),
    ("Jackson", "MS", "39201", 32.30, -90.18, 0.6), ("Columbia", "SC", "29201", 34.00, -81.03, 0.8),
    ("Wichita", "KS", "67202", 37.69, -97.34, 0.6), ("Billings", "MT", "59101", 45.78, -108.50, 0.2),
    ("Cheyenne", "WY", "82001", 41.14, -104.82, 0.1), ("Fargo", "ND", "58102", 46.88, -96.79, 0.25),
    ("Sioux Falls", "SD", "57104", 43.55, -96.73, 0.3), ("Portland", "ME", "04101", 43.66, -70.26, 0.5),
    ("Manchester", "NH", "03101", 42.99, -71.46, 0.4), ("Burlington", "VT", "05401", 44.48, -73.21, 0.2),
    ("Wilmington", "DE", "19801", 39.74, -75.55, 0.7), ("Charleston", "WV", "25301", 38.35, -81.63, 0.2),
    # Outside the US 48: Corp B sells there, the pilot does not ship there.
    ("Anchorage", "AK", "99501", 61.22, -149.90, 0.4), ("Honolulu", "HI", "96813", 21.31, -157.86, 1.0),
    ("San Juan", "PR", "00901", 18.47, -66.11, 0.3),
]

WEST_STATES = {"WA", "OR", "CA", "NV", "ID", "MT", "WY", "UT", "CO", "AZ", "NM", "AK", "HI"}

# Where Corp A's pilot nodes are (FCs serve a region; ISCs hold bulk stock; IXD cross-docks).
NODES = {
    "FC-WEST": (39.53, -119.81),   # Reno, NV
    "FC-EAST": (36.21, -86.29),    # Lebanon, TN
    "ISC-X": (34.06, -117.65),     # Ontario, CA
    "ISC-Y": (40.52, -74.41),      # Edison, NJ
    "IXD-1": (39.10, -94.58),      # Kansas City, MO
}
REGION_FC = {"west": "FC-WEST", "east": "FC-EAST"}
REGION_ISC = {"west": "ISC-X", "east": "ISC-Y"}


def region_of(state: str) -> str:
    return "west" if state.upper() in WEST_STATES else "east"


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def ground_days(distance: float) -> int:
    return 1 if distance < 250 else 2 if distance < 700 else 3 if distance < 1400 else 4 if distance < 2200 else 5


def node_transfer_days(src: str, dst: str) -> int:
    return max(1, ground_days(miles(NODES[src], NODES[dst])))


def zip_location(postal_code: str, state: str) -> tuple[float, float]:
    """Best-known coordinates for a destination: the city with that ZIP, else the state's first city."""
    for c in CITIES:
        if c[2] == postal_code:
            return c[3], c[4]
    for c in CITIES:
        if c[1] == state.upper():
            return c[3], c[4]
    return 39.8, -98.6  # geographic center of the US 48
