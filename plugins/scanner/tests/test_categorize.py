"""Vendor → category keyword matching for Uzbek SMB expense flows."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from engine.categorize import CATEGORIES, categorize, is_valid_category  # noqa: E402


# ── Category enum stability ─────────────────────────────────────


def test_14_canonical_categories():
    assert len(CATEGORIES) == 14
    # Boshqa must always be the fallback
    assert "Boshqa" in CATEGORIES


def test_is_valid_category_checks_exact_match():
    assert is_valid_category("Oziq-ovqat") is True
    assert is_valid_category("oziq-ovqat") is False  # case-sensitive
    assert is_valid_category("Food") is False


# ── Grocery chains ───────────────────────────────────────────────


def test_korzinka_is_groceries():
    assert categorize("Korzinka") == "Oziq-ovqat"
    assert categorize("KORZINKA SUPERMARKET") == "Oziq-ovqat"
    assert categorize("ООО Korzinka") == "Oziq-ovqat"


def test_common_grocery_names():
    for vendor in ("Havas", "Makro", "Magnum", "Chekanto", "Baraka", "Caprice"):
        assert categorize(vendor) == "Oziq-ovqat", f"failed on {vendor!r}"


# ── Taxi / transport ─────────────────────────────────────────────


def test_yandex_taxi_variants():
    assert categorize("Yandex Go") == "Transport"
    assert categorize("Yandex Taxi") == "Transport"
    assert categorize("YANDEX PRO") == "Transport"


def test_other_ride_share():
    assert categorize("Uklon") == "Transport"
    assert categorize("MyTaxi") == "Transport"
    assert categorize("inDrive") == "Transport"


# ── Fuel ─────────────────────────────────────────────────────────


def test_fuel_stations():
    assert categorize("UzNefteProdukt") == "Yoqilg'i"
    assert categorize("AGZS №12") == "Yoqilg'i"
    assert categorize("Lukoil Tashkent") == "Yoqilg'i"


def test_fuel_via_items_even_if_vendor_unknown():
    # A generic vendor name but items say "Benzin A95" → fuel
    result = categorize("Avtozapravka 7", items=["Benzin A95", "20 litr"])
    assert result == "Yoqilg'i"


# ── Telecom / utilities ──────────────────────────────────────────


def test_telecom():
    assert categorize("Beeline") == "Kommunal"
    assert categorize("UZTELECOM") == "Kommunal"
    assert categorize("Ucell balans") == "Kommunal"


def test_utility_companies():
    assert categorize("Hududgaz") == "Kommunal"
    assert categorize("Suvoqova") == "Kommunal"


# ── Fast food / restaurants ──────────────────────────────────────


def test_restaurants():
    assert categorize("Evos") == "Restoran"
    assert categorize("MAX WAY") == "Restoran"
    assert categorize("KFC Tashkent") == "Restoran"
    assert categorize("Les Ailes") == "Restoran"


def test_delivery_apps_are_restaurant():
    assert categorize("Glovo") == "Restoran"
    assert categorize("Express-24") == "Restoran"


# ── E-commerce / marketplaces ────────────────────────────────────


def test_marketplaces_are_goods():
    assert categorize("Uzum Market") == "Tovar"
    assert categorize("OLCHA") == "Tovar"
    assert categorize("Wildberries") == "Tovar"


# ── Pharmacy / medical ───────────────────────────────────────────


def test_pharmacy():
    assert categorize("Dori-Darmon Apteka") == "Tibbiyot"
    assert categorize("Pharmacy Plus") == "Tibbiyot"


def test_clinic():
    assert categorize("Akfa Medline klinikasi") == "Tibbiyot"


# ── Education ────────────────────────────────────────────────────


def test_education():
    assert categorize("Najot Ta'lim") == "Ta'lim"
    assert categorize("Udemy") == "Ta'lim"


# ── Unknown vendor → None, not fallback ──────────────────────────


def test_unknown_vendor_returns_none():
    # Keyword match MUST return None when no confident match exists, so
    # the agent can LLM-classify or ask the user. Returning 'Boshqa'
    # silently would bucket real categories as 'Other'.
    assert categorize("ZZZ Random LLC") is None
    assert categorize("") is None
    assert categorize(None) is None


def test_empty_inputs_dont_crash():
    assert categorize(None, None) is None
    assert categorize("", []) is None
    assert categorize(None, ["some item"]) is None or isinstance(
        categorize(None, ["some item"]), str
    )
