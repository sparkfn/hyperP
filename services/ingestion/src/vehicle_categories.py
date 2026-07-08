"""Per-source vehicle-category allowlists (by category name)."""

from __future__ import annotations

EKO_VEHICLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "Bicycles",
        "Foldable Bicycles",
        "Bi-fold Bicycles",
        "Tri-fold Bicycles",
        "Brompton Alternatives",
        "Hybrid Bicycles",
        "Mountain Bikes",
        "Cross Bikes",
        "Fixed Gear Bikes",
        "Gravel Bikes",
        "Road Bikes",
        "Mini Velos",
        "Power Assisted Bicycles",
        "E-Bikes",
        "Electric Bicycles",
        "Personal Mobility Aids",
        "Electric Scooters",
        "Mobility Scooters",
        "Electric Wheelchairs",
        "Motorised Wheelchairs",
    }
)

SPEEDZONE_VEHICLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "New Motorbike",
        "Used Motorbike",
        "Road Bike",
        "Scooter",
        "Sport Bike",
        "Scrambler",
        "Cafe Racer",
        "Used Road Bike",
        "Used Scooter",
        "Used Scrambler",
        "Used Cafe Racer",
        "Tourer",
        "Used Tourer",
        "Used Sport Bike",
        "Cruiser",
        "Used Cruiser",
        "Motorbike",
    }
)

FUNDBOX_VEHICLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "Electric Scooters",
        "Personal Mobility Aids",
        "Electric Wheelchairs",
        "Motorised Wheelchairs",
        "Mobility Scooters",
        "Power Assisted Bicycles",
        "Electric Bicycles",
        "Bicycles",
    }
)

_VEHICLE_CATEGORIES: dict[str, frozenset[str]] = {
    "eko_phppos": EKO_VEHICLE_CATEGORIES,
    "speedzone_phppos": SPEEDZONE_VEHICLE_CATEGORIES,
    "fundbox_consumer_backend": FUNDBOX_VEHICLE_CATEGORIES,
}


def base_source_key(source_system_key: str) -> str:
    """Strip a ``:sales``/``:contacts`` style suffix to get the base source key.

    ``eko_phppos:sales`` → ``eko_phppos``; ``fundbox_consumer_backend`` → itself.
    The base key is the lookup key into ``_VEHICLE_CATEGORIES`` so per-source
    category allowlists apply uniformly to sales + contacts sources.
    """
    return source_system_key.split(":", 1)[0]


def vehicle_category_allowlist(source_system_key: str) -> frozenset[str] | None:
    """Return the vehicle-category allowlist for ``source_system_key``, or None."""
    return _VEHICLE_CATEGORIES.get(base_source_key(source_system_key))


def category_is_vehicle(source_system_key: str, category_name: str | None) -> bool:
    """True if the product category is a vehicle category for the source."""
    if not category_name:
        return False
    allow = vehicle_category_allowlist(source_system_key)
    return bool(allow and category_name in allow)
