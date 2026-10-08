"""The retail clients' catalogs (each client lists its items, Corp A stocks and ships them).

Corp B's pilot catalog has 1,360 items. Corp C, a home goods chain with a website and stores, has its
own 240 items, sold in cases to its stores. Deterministic from a seed so every process, test and
redeploy sees the same items and the same demand curve. Demand is long-tailed: a few items sell
daily, most sell a few units a month.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache

PILOT_SIZE = 1360
CORPC_SIZE = 240
REGIONS = ("west", "east")
CASE_PACKS = (4, 6, 8, 12, 24)

_ADJ = ["Classic", "Compact", "Deluxe", "Everyday", "Premium", "Travel", "Eco", "Pro", "Mini", "Family",
        "Smart", "Soft", "Heavy-Duty", "Rechargeable", "Organic", "Insulated", "Foldable", "Wireless"]
_NOUN = {
    "Home": ["Throw Blanket", "Storage Bin", "Desk Lamp", "Picture Frame", "Candle Set", "Shower Caddy"],
    "Kitchen": ["Chef Knife", "Mixing Bowls", "Water Bottle", "Cutting Board", "Coffee Grinder", "Spice Rack"],
    "Toys": ["Building Blocks", "Puzzle 500pc", "Plush Bear", "Card Game", "Race Car", "Art Kit"],
    "Beauty": ["Face Serum", "Hair Dryer", "Makeup Brushes", "Body Lotion", "Nail Kit", "Lip Balm 4pk"],
    "Electronics": ["USB-C Cable", "Phone Case", "Earbuds", "Power Bank", "Laptop Stand", "Smart Plug"],
    "Pet": ["Dog Leash", "Cat Toy Set", "Pet Bed", "Chew Bones", "Litter Mat", "Food Bowl"],
    "Outdoor": ["Camping Lantern", "Hiking Socks", "Picnic Blanket", "Garden Gloves", "Cooler Bag", "Bike Light"],
}
_PRICE = {"Home": 28, "Kitchen": 32, "Toys": 22, "Beauty": 18, "Electronics": 26, "Pet": 20, "Outdoor": 30}


@dataclass(frozen=True)
class Item:
    upc: str
    title: str
    category: str
    unit_cost: float
    weight_lb: float
    weekly_units: dict[str, float]   # expected demand per region (for Corp C: web and stores together)
    case_pack: int = 1               # units per case, for store orders


def upc_a(eleven: str) -> str:
    """Append the UPC-A check digit."""
    odd = sum(int(d) for d in eleven[0::2])
    even = sum(int(d) for d in eleven[1::2])
    return eleven + str((10 - (odd * 3 + even) % 10) % 10)


@lru_cache(maxsize=8)
def items(seed: int = 2026, size: int = PILOT_SIZE, total_weekly_units: float = 1400.0, *,
          upc_base: int = 64_000_000_000, categories: tuple[str, ...] | None = None) -> tuple[Item, ...]:
    rng = random.Random(seed)
    cats = list(categories or _NOUN)
    weights = [1 / (rank ** 0.85) for rank in range(1, size + 1)]
    rng.shuffle(weights)
    scale = total_weekly_units / sum(weights)
    out = []
    for i in range(size):
        cat = cats[i % len(cats)]
        title = f"{rng.choice(_ADJ)} {rng.choice(_NOUN[cat])}"
        cost = round(_PRICE[cat] * math.exp(rng.gauss(0, 0.45)), 2)
        weekly = weights[i] * scale
        west = rng.uniform(0.35, 0.55)
        out.append(Item(upc=upc_a(f"{upc_base + i * 37:011d}"), title=title, category=cat, unit_cost=cost,
                        weight_lb=round(rng.uniform(0.2, 6.0), 2),
                        weekly_units={"west": round(weekly * west, 4), "east": round(weekly * (1 - west), 4)},
                        case_pack=rng.choice(CASE_PACKS) if categories else 1))
    return tuple(out)


def corpc_items(size: int = CORPC_SIZE) -> tuple[Item, ...]:
    """Corp C's catalog: home, kitchen and outdoor goods. Its stores sell about twice what its website does."""
    return items(2027, size, 2400.0, upc_base=65_000_000_000, categories=("Home", "Kitchen", "Outdoor"))


@lru_cache(maxsize=4)
def by_upc(seed: int = 2026, size: int = PILOT_SIZE) -> dict[str, Item]:
    return {it.upc: it for it in items(seed, size)}


def every_client(seed: int = 2026, size: int = PILOT_SIZE, corpc_size: int = CORPC_SIZE) -> dict[str, Item]:
    """Everything any client may list with Corp A."""
    return {**by_upc(seed, size), **{it.upc: it for it in corpc_items(corpc_size)}}


def non_pilot_upcs(n: int = 40) -> list[str]:
    """Items Corp B sells that are not in the pilot; orders for them must be rejected."""
    return [upc_a(f"{71_500_000_000 + i * 53:011d}") for i in range(n)]
