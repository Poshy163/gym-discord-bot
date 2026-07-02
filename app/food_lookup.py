"""Open Food Facts lookup — per-100g energy/protein for packaged foods.

Backs ``/calories food_lookup``: search by product name or barcode and get
the label values (kJ, kcal, protein per 100 g) without typing them in. Pairs
with the multiplier logging syntax — a 1640 kJ/100g product eaten as 70 g is
logged as ``0.7x1640kj``.

Open Food Facts (https://openfoodfacts.org) is a free, open database; no API
key needed. Their API policy asks for a descriptive User-Agent, which we set.
Kept import-safe without ``requests`` (mirrors :mod:`app.gemini_client`) —
callers should check :func:`available` first. :func:`parse_product` is pure
so response parsing can be unit-tested from fixture dicts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

LOG = logging.getLogger("gymbot.foodlookup")

try:  # pragma: no cover - trivial import guard
    import requests  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
_USER_AGENT = "GymDiscordBot/1.0 (github.com Gym-Discord-Bot; contact via repo)"
_TIMEOUT = 10
# Only ask for the fields we read — keeps responses small.
_FIELDS = "code,product_name,brands,nutriments,serving_quantity"


class FoodLookupError(RuntimeError):
    """Raised when Open Food Facts can't be reached or answers strangely."""


@dataclass
class FoodInfo:
    """Per-100g label values for one product. Any nutrient may be None —
    Open Food Facts data is crowdsourced and often partial."""

    name: str
    brand: str | None
    barcode: str | None
    kj_per_100g: float | None
    kcal_per_100g: float | None
    protein_per_100g: float | None
    serving_g: float | None

    @property
    def has_energy(self) -> bool:
        return self.kj_per_100g is not None or self.kcal_per_100g is not None


def available() -> bool:
    """True when the HTTP dependency is installed."""
    return requests is not None


def _num(v: object) -> float | None:
    """Coerce an OFF nutriment value (may be str/int/float/None) to float."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_product(p: dict) -> FoodInfo | None:
    """Build a :class:`FoodInfo` from one OFF product dict, or None if the
    entry is unusable (no name, or no nutrient we care about)."""
    if not isinstance(p, dict):
        return None
    name = str(p.get("product_name") or "").strip()
    if not name:
        return None
    nutr = p.get("nutriments") or {}
    kj = _num(nutr.get("energy-kj_100g"))
    kcal = _num(nutr.get("energy-kcal_100g"))
    if kj is None:
        # OFF's unit-less "energy_100g" is kilojoules.
        kj = _num(nutr.get("energy_100g"))
    protein = _num(nutr.get("proteins_100g"))
    if kj is None and kcal is None and protein is None:
        return None
    brand = str(p.get("brands") or "").strip() or None
    if brand and "," in brand:
        brand = brand.split(",")[0].strip()
    return FoodInfo(
        name=name,
        brand=brand,
        barcode=str(p.get("code") or "").strip() or None,
        kj_per_100g=kj,
        kcal_per_100g=kcal,
        protein_per_100g=protein,
        serving_g=_num(p.get("serving_quantity")),
    )


def _get(url: str, params: dict) -> dict:
    if requests is None:
        raise FoodLookupError("The 'requests' package isn't installed.")
    try:
        resp = requests.get(
            url, params=params, timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
    except requests.RequestException as exc:  # type: ignore[union-attr]
        raise FoodLookupError(f"Couldn't reach Open Food Facts: {exc}") from exc
    if resp.status_code != 200:
        raise FoodLookupError(
            f"Open Food Facts returned HTTP {resp.status_code}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise FoodLookupError("Open Food Facts returned invalid JSON") from exc


def by_barcode(code: str) -> FoodInfo | None:
    """Fetch one product by barcode. None when the code is unknown."""
    data = _get(_PRODUCT_URL.format(code=code), {"fields": _FIELDS})
    if data.get("status") != 1:
        return None
    return parse_product(data.get("product") or {})


def search(query: str, *, limit: int = 5) -> list[FoodInfo]:
    """Name search, best matches first, entries without usable data dropped."""
    data = _get(_SEARCH_URL, {
        "search_terms": query,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": max(1, min(limit * 4, 24)),  # headroom for unusable rows
        "fields": _FIELDS,
    })
    out: list[FoodInfo] = []
    for p in data.get("products") or []:
        info = parse_product(p)
        # Search hits without energy are useless for calorie logging.
        if info is not None and info.has_energy:
            out.append(info)
        if len(out) >= limit:
            break
    return out


def lookup(query: str, *, limit: int = 5) -> list[FoodInfo]:
    """Barcode when the query is all digits (8+ chars), else a name search."""
    q = query.strip()
    if q.isdigit() and len(q) >= 8:
        info = by_barcode(q)
        return [info] if info is not None else []
    return search(q, limit=limit)
