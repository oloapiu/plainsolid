"""The built-in material table: density in g/cm^3, so mass comes back in grams
for millimetre geometry. A part names its material in meta(); an instance can
override it or give a density outright."""
from __future__ import annotations

MATERIALS: dict[str, float] = {
    "al6061": 2.70, "aluminum": 2.70, "aluminium": 2.70,
    "steel": 7.85, "stainless": 8.00, "ss304": 8.00, "ss316": 8.00,
    "brass": 8.50, "copper": 8.96, "titanium": 4.43,
    "abs": 1.04, "pla": 1.24, "petg": 1.27, "nylon": 1.15, "pc": 1.20, "pom": 1.41,
    "glass": 2.50, "silicone": 1.10, "rubber": 1.20,
}


def density_of(name: str) -> float | None:
    """Density for a material name, tolerant of case, spaces and dashes; None if unknown."""
    key = str(name).lower().replace(" ", "").replace("-", "")
    return MATERIALS.get(key)
