"""Vendor → expense category (Uzbek enum).

Deterministic keyword-based match over common Uzbek vendors. Covers the
names that account for ~80% of Tashkent SMB traffic: grocery chains, taxi
apps, telecom operators, fast food, fuel stations, marketplaces.

When no keyword matches, return None — the agent then LLM-classifies
using the category list below (which is also used for validation).

Categories are stable IDs. The user-facing Uzbek display names match 1:1
in v1; future-proof for translation by keeping the ID in English in
sheet data if needed. We keep Uzbek throughout for simplicity.
"""

from __future__ import annotations

import re

# 14 categories — keep in sync with SOUL_APPEND.md and doctypes.py receipt notes.
CATEGORIES: tuple[str, ...] = (
    "Oziq-ovqat",   # groceries, food supplies
    "Restoran",     # dining out, delivery food
    "Transport",    # taxi, metro, bus, ride-share
    "Yoqilg'i",     # fuel / gas stations
    "Kommunal",     # utilities (gas, water, electric, internet, telecom)
    "Ijara",        # rent (office, apartment, warehouse)
    "Maosh",        # payroll / salary
    "Tovar",        # inventory / goods for resale
    "Tibbiyot",     # medical, pharmacy
    "Ta'lim",       # education, courses, books
    "Reklama",      # advertising, marketing
    "Ofis",         # office supplies, stationery, cleaning
    "Texnika",      # equipment, tools, tech purchases, software
    "Boshqa",       # fallback / other
)


# Regex patterns → category. Case-insensitive. Order matters (first match wins).
# Keep the common cases at the top so Uzbek SMB hit-rate is high.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Grocery chains (most frequent SMB expense type)
    (
        re.compile(
            r"\b(korzinka|havas|makro|magnum|chekanto|anhor|caprice|baraka|"
            r"good\s*mart|asia\s*market|bi[- ]?mart|supermarket|oziq[- ]?ovqat)\b",
            re.I,
        ),
        "Oziq-ovqat",
    ),
    # Pharmacies
    (re.compile(r"\b(dori|apteka|pharmacy|pharmac|farmatsiya)\b", re.I), "Tibbiyot"),
    # Clinics / hospitals.
    # Stem-based: Uzbek suffixes (klinika+si, shifoxona+ga) would break
    # \bword\b boundaries, so we allow trailing word chars after each stem.
    (
        re.compile(
            r"\b(klinik|hospital|shifoxona|poliklinika|stomatologiya|"
            r"laboratoriya|akinshin|tibbiyot|doctor|doktor|medical)\w*",
            re.I,
        ),
        "Tibbiyot",
    ),
    # Taxi / ride-share
    (
        re.compile(
            r"\b(yandex\s*(go|taxi|pro)|uklon|mytaxi|indriver|indrive|taksi|"
            r"olcha\s*taxi|call\s*taxi|yandex\s*kliring)\b",
            re.I,
        ),
        "Transport",
    ),
    # Fuel
    (
        re.compile(
            r"\b(uznefteprodukt|agzs|gas\s*station|petrol|benzin|"
            r"neftebazasi|gaz\s*ompozit|avtogaz|texaco|lukoil)\b",
            re.I,
        ),
        "Yoqilg'i",
    ),
    # Telecom / internet
    (
        re.compile(
            r"\b(beeline|ucell|mobiuz|perfectum|uzbektelekom|uztelecom|"
            r"sharq\s*telecom|comnet|tps|east\s*telecom|oltin[- ]yo.l)\b",
            re.I,
        ),
        "Kommunal",
    ),
    # Utilities (non-telecom)
    (
        re.compile(
            r"\b(hududgaz|hududiy\s*gaz|regional\s*gaz|suvoqova|suv\s*kanal|"
            r"elektr\s*ta.minot|energo|gas\s*supply|vodokanal)\b",
            re.I,
        ),
        "Kommunal",
    ),
    # Fast food / cafes / restaurants
    (
        re.compile(
            r"\b(evos|max\s*way|kfc|burger\s*king|les\s*ailes|cafeteria|"
            r"osh\s*markaz|choyxona|pizza|pekarnya|nonvoy|glovo|express[- ]?24)\b",
            re.I,
        ),
        "Restoran",
    ),
    # E-commerce / marketplaces (purchase of goods)
    (
        re.compile(
            r"\b(uzum|olcha|yandex\s*market|alibaba|aliexpress|ozon\.ru|"
            r"wildberries|ozone|lavka)\b",
            re.I,
        ),
        "Tovar",
    ),
    # Education / courses
    (
        re.compile(
            r"\b(najot\s*ta.lim|it\s*park|webster|inha|mit|udemy|coursera|"
            r"ziyonet|maktab|universitet|akademiya|o.quv\s*markaz)\b",
            re.I,
        ),
        "Ta'lim",
    ),
    # Office supplies
    (
        re.compile(
            r"\b(kantselyariya|ofis\s*mebel|office\s*depot|shtrix|kanctovar|"
            r"idoraviy\s*buyum|bilgisayar)\b",
            re.I,
        ),
        "Ofis",
    ),
    # Tech / equipment
    (
        re.compile(
            r"\b(technomart|mediapark|texnomart|elmakon|malika|idea|"
            r"mi\s*store|samsung|apple\s*store|dns)\b",
            re.I,
        ),
        "Texnika",
    ),
    # Rent mentions
    (
        re.compile(r"\b(ijara|arenda|rent|biznes[- ]?markaz|business[- ]?center)\b", re.I),
        "Ijara",
    ),
    # Advertising / marketing
    (
        re.compile(
            r"\b(facebook|fb|instagram|meta|google\s*ads|yandex\s*direct|"
            r"billboard|reklama\s*agentligi)\b",
            re.I,
        ),
        "Reklama",
    ),
    # Payroll hints
    (re.compile(r"\b(maosh|ish\s*haqi|salary|payroll|oklad)\b", re.I), "Maosh"),
)


def categorize(vendor: str | None, items: list[str] | None = None) -> str | None:
    """Return one of CATEGORIES, or None if no keyword matched.

    `vendor` — the shop/vendor name as extracted from the receipt.
    `items` — optional list of item description strings; some receipts
    don't name the vendor but show "Benzin A95" in the item line, which
    lets us infer the category.

    Returns None when nothing matched so the caller knows to ask the LLM
    (or user) for a category.
    """
    haystacks: list[str] = []
    if vendor:
        haystacks.append(vendor)
    if items:
        haystacks.extend(str(i) for i in items if i)
    if not haystacks:
        return None

    combined = " | ".join(haystacks)
    for pattern, category in _PATTERNS:
        if pattern.search(combined):
            return category
    return None


def is_valid_category(value: str) -> bool:
    """Check if a category string (after user/LLM input) is canonical."""
    return value in CATEGORIES
