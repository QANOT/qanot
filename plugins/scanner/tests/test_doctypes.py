"""Doctype registry invariants and lookup behaviour."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from engine.doctypes import (  # noqa: E402
    DOCTYPES,
    as_dict_list,
    find_by_uzbek_name,
    get_doctype,
)


def test_all_keys_unique():
    keys = [dt.key for dt in DOCTYPES]
    assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"


def test_every_doctype_has_required_fields():
    # Every doctype must have a non-empty schema so Claude has something
    # concrete to extract into.
    for dt in DOCTYPES:
        assert dt.fields, f"{dt.key} has empty fields list"
        assert dt.uzbek_names, f"{dt.key} has no Uzbek names"
        assert dt.description, f"{dt.key} has no description"


def test_default_outputs_valid():
    allowed = {"sheet", "xlsx", "pdf", "docx", "crm_contact", "crm_deal"}
    for dt in DOCTYPES:
        assert dt.default_output in allowed, (
            f"{dt.key} has invalid default_output={dt.default_output!r}"
        )


def test_id_document_is_marked_sensitive():
    # ID docs hold PII; the sensitive flag must be set so SOUL enforces
    # the local-only save path.
    id_dt = get_doctype("id_document")
    assert id_dt is not None
    assert id_dt.sensitive is True
    assert id_dt.default_output == "docx", (
        "ID document default must stay DOCX (local) until user consents otherwise"
    )


def test_non_sensitive_doctypes_do_not_leak_pii_flag():
    # Everything else must NOT be marked sensitive (over-flagging hurts UX).
    for dt in DOCTYPES:
        if dt.key == "id_document":
            continue
        assert dt.sensitive is False, f"{dt.key} shouldn't be sensitive"


def test_find_by_uzbek_name_exact():
    assert find_by_uzbek_name("chek").key == "receipt"
    assert find_by_uzbek_name("vizitka").key == "business_card"
    assert find_by_uzbek_name("faktura").key == "invoice"
    assert find_by_uzbek_name("shartnoma").key == "contract"


def test_find_by_uzbek_name_case_insensitive():
    assert find_by_uzbek_name("CHEK").key == "receipt"
    assert find_by_uzbek_name("Vizitka").key == "business_card"


def test_find_by_uzbek_name_partial():
    # User typing "chek yozma" should still hit receipt
    assert find_by_uzbek_name("chek yozma").key == "receipt"


def test_find_by_uzbek_name_unknown():
    assert find_by_uzbek_name("") is None
    assert find_by_uzbek_name("nonsense-xyz") is None


def test_as_dict_list_is_json_serializable():
    import json

    data = as_dict_list()
    # Must round-trip through JSON without errors
    encoded = json.dumps(data, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert len(decoded) == len(DOCTYPES)
    for entry in decoded:
        assert "key" in entry
        assert "fields" in entry
        assert isinstance(entry["fields"], list)
