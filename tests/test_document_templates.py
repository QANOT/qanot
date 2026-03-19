"""Tests for document_templates — TIER 1 Uzbek business documents."""

import json
import pytest
from qanot.tools.document_templates import (
    TIER1_GENERATORS,
    generate_oldi_sotdi,
    generate_yetkazib_berish,
    generate_ijara,
    generate_mehnat,
    generate_solishtirma,
    generate_tijorat_taklifi,
    generate_xizmat,
    generate_qabul_topshirish,
    generate_buyruq_t1,
    generate_buyruq_t6,
    generate_buyruq_t8,
    generate_pudrat,
    generate_nda,
    generate_tushuntirish_xati,
    generate_ariza,
    _rekvizit,
    _amount_str,
    _common_fields,
)


# ── Helpers ──

SAMPLE_PARAMS = {
    "company": "Sirli AI MCHJ",
    "inn": "123456789",
    "director": "Sirliboyev Umurzoq",
    "address": "Toshkent sh., Mirzo Ulug'bek tumani",
    "bank": "Ipak Yo'li bank",
    "account": "20208000123456789012",
    "mfo": "01033",
    "counterparty": "TechnoPlus MCHJ",
    "counterparty_inn": "987654321",
    "counterparty_director": "Karimov Sardor",
    "counterparty_address": "Toshkent sh., Yunusobod tumani",
    "counterparty_bank": "Asaka bank",
    "counterparty_account": "20208000987654321012",
    "counterparty_mfo": "00873",
    "amount": 50_000_000,
    "description": "Ofis jihozlari yetkazib berish",
    "number": "25",
    "date": "20.03.2026",
    "city": "Toshkent",
}


class TestHelpers:
    def test_rekvizit_filled(self):
        result = _rekvizit("Test MCHJ", "123", "Toshkent", "Bank", "2020", "01033", "Direktor")
        assert "Test MCHJ" in result
        assert "123" in result
        assert "M.O." in result

    def test_rekvizit_blanks(self):
        result = _rekvizit("Test", "", "", "", "", "", "")
        assert "_______________" in result

    def test_amount_str_with_value(self):
        assert _amount_str(1_000_000) == "1,000,000"

    def test_amount_str_zero(self):
        assert _amount_str(0) == "_______________"

    def test_common_fields_defaults(self):
        fields = _common_fields({"company": "Test", "counterparty": "Test2"})
        assert fields["company"] == "Test"
        assert fields["city"] == "Toshkent"
        assert fields["number"] == "1"

    def test_tier1_generators_registry(self):
        assert len(TIER1_GENERATORS) == 15  # 6 TIER1 + 9 TIER2
        assert "oldi_sotdi" in TIER1_GENERATORS
        assert "yetkazib_berish" in TIER1_GENERATORS
        assert "ijara" in TIER1_GENERATORS
        assert "mehnat" in TIER1_GENERATORS
        assert "solishtirma" in TIER1_GENERATORS
        assert "tijorat_taklifi" in TIER1_GENERATORS
        # TIER 2
        assert "xizmat" in TIER1_GENERATORS
        assert "qabul_topshirish" in TIER1_GENERATORS
        assert "buyruq_t1" in TIER1_GENERATORS
        assert "buyruq_t6" in TIER1_GENERATORS
        assert "buyruq_t8" in TIER1_GENERATORS
        assert "pudrat" in TIER1_GENERATORS
        assert "nda" in TIER1_GENERATORS
        assert "tushuntirish_xati" in TIER1_GENERATORS
        assert "ariza" in TIER1_GENERATORS


class TestOldiSotdi:
    def test_basic_generation(self):
        content = generate_oldi_sotdi(SAMPLE_PARAMS)
        assert "OLDI-SOTDI SHARTNOMASI" in content
        assert "386-432" in content  # legal reference
        assert "Sotuvchi" in content
        assert "Xaridor" in content
        assert "Sirli AI MCHJ" in content
        assert "TechnoPlus MCHJ" in content

    def test_legal_sections(self):
        content = generate_oldi_sotdi(SAMPLE_PARAMS)
        assert "SHARTNOMA PREDMETI" in content
        assert "TOVAR SIFATI VA MIQDORI" in content
        assert "MULK HUQUQI O'TISHI" in content
        assert "FK 392" in content
        assert "FK 396-404" in content
        assert "FK 402" in content
        assert "FORS-MAJOR" in content
        assert "NIZOLARNI HAL QILISH" in content

    def test_with_items(self):
        params = {**SAMPLE_PARAMS, "items": [
            {"name": "Stol", "quantity": 10, "unit": "dona", "price": 1_000_000},
            {"name": "Stul", "quantity": 20, "unit": "dona", "price": 500_000},
        ]}
        content = generate_oldi_sotdi(params)
        assert "Stol" in content
        assert "Stul" in content
        assert "10,000,000" in content  # 10 * 1M
        assert "10,000,000" in content

    def test_warranty(self):
        params = {**SAMPLE_PARAMS, "warranty_months": 12}
        content = generate_oldi_sotdi(params)
        assert "KAFOLAT" in content
        assert "12 oy" in content
        assert "FK 405-416" in content

    def test_prepayment(self):
        params = {**SAMPLE_PARAMS, "prepay_pct": 50}
        content = generate_oldi_sotdi(params)
        assert "50%" in content
        assert "oldindan to'lov" in content

    def test_cash_payment(self):
        params = {**SAMPLE_PARAMS, "payment_type": "cash"}
        content = generate_oldi_sotdi(params)
        assert "naqd pul" in content

    def test_mixed_payment(self):
        params = {**SAMPLE_PARAMS, "payment_type": "mixed", "prepay_pct": 30}
        content = generate_oldi_sotdi(params)
        assert "aralash" in content
        assert "30%" in content


class TestYetkazibBerish:
    def test_basic_generation(self):
        content = generate_yetkazib_berish(SAMPLE_PARAMS)
        assert "YETKAZIB BERISH SHARTNOMASI" in content
        assert "437-462" in content
        assert "Yetkazib beruvchi" in content
        assert "Buyurtmachi" in content

    def test_legal_sections(self):
        content = generate_yetkazib_berish(SAMPLE_PARAMS)
        assert "FK 441-442" in content  # delivery order
        assert "FK 443-445" in content  # acceptance
        assert "FK 449" in content  # defects notification
        assert "FK 450" in content  # shortage
        assert "FK 460" in content  # penalty
        assert "tadbirkorlik faoliyati" in content  # FK 437

    def test_delivery_schedule(self):
        params = {**SAMPLE_PARAMS, "delivery_schedule": "har hafta", "delivery_place": "Toshkent ombor"}
        content = generate_yetkazib_berish(params)
        assert "har hafta" in content
        assert "Toshkent ombor" in content

    def test_acceptance_days(self):
        params = {**SAMPLE_PARAMS, "acceptance_days": 5}
        content = generate_yetkazib_berish(params)
        assert "5 ish kuni" in content

    def test_with_items(self):
        params = {**SAMPLE_PARAMS, "items": [
            {"name": "Un", "quantity": 1000, "unit": "kg", "price": 5000},
        ]}
        content = generate_yetkazib_berish(params)
        assert "Un" in content
        assert "5,000,000" in content  # 1000 * 5000


class TestIjara:
    def test_basic_generation(self):
        content = generate_ijara(SAMPLE_PARAMS)
        assert "IJARA SHARTNOMASI" in content
        assert "535-570" in content
        assert "Ijara beruvchi" in content
        assert "Ijarachi" in content

    def test_legal_sections(self):
        content = generate_ijara(SAMPLE_PARAMS)
        assert "FK 537" in content  # subject
        assert "FK 540-541" in content  # term
        assert "FK 544" in content  # rent
        assert "FK 539" in content  # condition
        assert "FK 545" in content  # capital repair
        assert "FK 546" in content  # current repair
        assert "FK 548" in content  # priority right
        assert "FK 554" in content  # termination for non-payment
        assert "FK 556" in content  # return

    def test_registration_warning(self):
        params = {**SAMPLE_PARAMS, "valid_until": "31.12.2027"}
        content = generate_ijara(params)
        assert "davlat ro'yxat" in content
        assert "FK 541" in content

    def test_object_details(self):
        params = {
            **SAMPLE_PARAMS,
            "object_type": "ofis binosi",
            "object_area": "150",
            "object_address": "Toshkent, Amir Temur ko'chasi 5",
            "cadastral_number": "10:03:05:12:001",
        }
        content = generate_ijara(params)
        assert "ofis binosi" in content
        assert "150 kv.m" in content
        assert "10:03:05:12:001" in content

    def test_utilities_included(self):
        params = {**SAMPLE_PARAMS, "utilities_included": True}
        content = generate_ijara(params)
        assert "kommunal xizmatlar" in content.lower()
        assert "bilan birga" in content

    def test_utilities_separate(self):
        params = {**SAMPLE_PARAMS, "utilities_included": False}
        content = generate_ijara(params)
        assert "alohida" in content


class TestMehnat:
    def test_basic_generation(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Rahimov Jasur Bahodirovich",
            "passport": "AB 1234567",
            "position": "Dasturchi",
            "salary": 15_000_000,
        }
        content = generate_mehnat(params)
        assert "MEHNAT SHARTNOMASI" in content
        assert "103-132" in content
        assert "my.mehnat.uz" in content
        assert "mehnat.uz" in content
        assert "Ish beruvchi" in content
        assert "Xodim" in content
        assert "Rahimov Jasur" in content
        assert "Dasturchi" in content

    def test_legal_sections(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Test",
            "position": "Test",
            "salary": 5_000_000,
        }
        content = generate_mehnat(params)
        assert "MK 103" in content  # basis
        assert "MK 104" in content  # duties
        assert "MK 115" in content  # work time
        assert "MK 134" in content  # vacation
        assert "MK 153" in content  # salary
        assert "MK 183" in content  # material liability
        assert "MK 97" in content  # termination

    def test_probation_max_3_months(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Test",
            "position": "Test",
            "probation_months": 6,  # should be capped to 3
        }
        content = generate_mehnat(params)
        assert "SINOV MUDDATI" in content
        assert "3 oy" in content
        assert "MK 114" in content

    def test_no_probation(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Test",
            "position": "Test",
            "probation_months": 0,
        }
        content = generate_mehnat(params)
        assert "SINOV MUDDATI" not in content

    def test_fixed_term_contract(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Test",
            "position": "Test",
            "contract_type": "muddatli",
            "valid_until": "31.12.2026",
        }
        content = generate_mehnat(params)
        assert "Muddatli mehnat shartnomasi" in content
        assert "MK 106" in content

    def test_tax_deductions(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Test",
            "position": "Test",
            "salary": 10_000_000,
        }
        content = generate_mehnat(params)
        assert "12%" in content  # JSHDS
        assert "INPS" in content
        assert "1%" in content  # INPS rate

    def test_department(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Test",
            "position": "Test",
            "department": "IT bo'limi",
        }
        content = generate_mehnat(params)
        assert "IT bo'limi" in content


class TestSolishtirma:
    def test_basic_generation(self):
        params = {
            **SAMPLE_PARAMS,
            "period_from": "01.01.2026",
            "period_to": "31.03.2026",
        }
        content = generate_solishtirma(params)
        assert "SOLISHTIRISH DALOLATNOMASI" in content
        assert "01.01.2026" in content
        assert "31.03.2026" in content

    def test_legal_reference(self):
        content = generate_solishtirma(SAMPLE_PARAMS)
        assert "FK 159" in content  # statute of limitations restart

    def test_with_operations(self):
        params = {
            **SAMPLE_PARAMS,
            "period_from": "01.01.2026",
            "period_to": "31.03.2026",
            "operations": [
                {"date": "15.01.2026", "description": "Faktura No 1", "debit": 10_000_000, "credit": 0},
                {"date": "20.01.2026", "description": "To'lov platejka", "debit": 0, "credit": 5_000_000},
            ],
        }
        content = generate_solishtirma(params)
        assert "Faktura No 1" in content
        assert "10,000,000" in content
        assert "5,000,000" in content
        assert "qarzdor" in content  # debit > credit = counterparty owes

    def test_zero_balance(self):
        params = {
            **SAMPLE_PARAMS,
            "period_from": "01.01.2026",
            "period_to": "31.03.2026",
            "operations": [
                {"date": "15.01.2026", "description": "Faktura", "debit": 5_000_000, "credit": 0},
                {"date": "20.01.2026", "description": "To'lov", "debit": 0, "credit": 5_000_000},
            ],
        }
        content = generate_solishtirma(params)
        assert "qarz yo'q" in content

    def test_company_owes(self):
        params = {
            **SAMPLE_PARAMS,
            "period_from": "01.01.2026",
            "period_to": "31.03.2026",
            "operations": [
                {"date": "15.01.2026", "description": "Oldindan to'lov", "debit": 0, "credit": 10_000_000},
            ],
        }
        content = generate_solishtirma(params)
        assert "Sirli AI MCHJ" in content
        assert "qarzdor" in content

    def test_opening_balance(self):
        params = {
            **SAMPLE_PARAMS,
            "period_from": "01.01.2026",
            "period_to": "31.03.2026",
            "opening_balance": 3_000_000,
            "opening_balance_side": "debit",
        }
        content = generate_solishtirma(params)
        assert "3,000,000" in content
        assert "Boshlang'ich qoldiq" in content

    def test_objection_deadline(self):
        content = generate_solishtirma(SAMPLE_PARAMS)
        assert "10 kun" in content
        assert "e'tiroz" in content

    def test_bosh_hisobchi_signature(self):
        content = generate_solishtirma(SAMPLE_PARAMS)
        assert "Bosh hisobchi" in content


class TestTijoratTaklifi:
    def test_basic_generation(self):
        content = generate_tijorat_taklifi(SAMPLE_PARAMS)
        assert "TIJORAT TAKLIFI" in content
        assert "OFERTA" in content
        assert "Sirli AI MCHJ" in content

    def test_legal_reference(self):
        content = generate_tijorat_taklifi(SAMPLE_PARAMS)
        assert "FK 365" in content  # offer
        assert "FK 369" in content  # offer validity

    def test_validity_period(self):
        params = {**SAMPLE_PARAMS, "valid_days": 15}
        content = generate_tijorat_taklifi(params)
        assert "15 kalendar kun" in content

    def test_default_validity(self):
        content = generate_tijorat_taklifi(SAMPLE_PARAMS)
        assert "30 kalendar kun" in content

    def test_with_items(self):
        params = {**SAMPLE_PARAMS, "items": [
            {"name": "Laptop", "quantity": 5, "unit": "dona", "price": 10_000_000},
        ]}
        content = generate_tijorat_taklifi(params)
        assert "Laptop" in content
        assert "50,000,000" in content  # 5 * 10M
        assert "QQS" in content

    def test_contact_info(self):
        params = {
            **SAMPLE_PARAMS,
            "contact_person": "Aliyev Bobur",
            "contact_phone": "+998901234567",
            "contact_email": "bobur@test.uz",
        }
        content = generate_tijorat_taklifi(params)
        assert "Aliyev Bobur" in content
        assert "+998901234567" in content
        assert "bobur@test.uz" in content

    def test_custom_terms(self):
        params = {
            **SAMPLE_PARAMS,
            "delivery_terms": "2 hafta ichida",
            "payment_terms": "50% oldindan",
            "special_conditions": "Faqat optom savdo",
        }
        content = generate_tijorat_taklifi(params)
        assert "2 hafta ichida" in content
        assert "50% oldindan" in content
        assert "Faqat optom savdo" in content

    def test_aksept_reference(self):
        content = generate_tijorat_taklifi(SAMPLE_PARAMS)
        assert "aksept" in content
        assert "shartnoma tuzishga asos" in content


class TestIntegrationWithLocalTool:
    """Test that TIER1_GENERATORS integrates properly with generate_document."""

    @pytest.mark.asyncio
    async def test_generate_document_dispatches_to_tier1(self):
        """Verify generate_document routes to TIER1 generators."""
        from qanot.registry import ToolRegistry

        registry = ToolRegistry()

        # Import and register
        from qanot.tools.local import register_local_tools
        register_local_tools(registry)

        handler = registry.get_handler("generate_document")
        assert handler is not None

        result = await handler({
            "type": "oldi_sotdi",
            "company": "Test MCHJ",
            "counterparty": "Test2 MCHJ",
            "amount": 1_000_000,
        })

        data = json.loads(result)
        assert data["type"] == "oldi_sotdi"
        assert "OLDI-SOTDI SHARTNOMASI" in data["content"]
        assert "filename" in data

    @pytest.mark.asyncio
    async def test_all_types_work(self):
        """Verify all 15 types generate without errors."""
        for doc_type, generator in TIER1_GENERATORS.items():
            params = {
                "company": "Test MCHJ",
                "counterparty": "Test2 MCHJ",
                "amount": 1_000_000,
                "description": "Test",
            }
            # Add type-specific required params
            if doc_type in ("mehnat", "buyruq_t1", "buyruq_t6", "buyruq_t8", "tushuntirish_xati", "ariza"):
                params["employee_name"] = "Test Xodim"
                params["position"] = "Dasturchi"
            if doc_type == "mehnat":
                params["salary"] = 5_000_000
            elif doc_type == "solishtirma":
                params["period_from"] = "01.01.2026"
                params["period_to"] = "31.03.2026"

            content = generator(params)
            assert len(content) > 50, f"{doc_type} generated too short content"
            assert "Imzo" in content or "M.O." in content or "Rahbar" in content, f"{doc_type} missing signature block"

    @pytest.mark.asyncio
    async def test_unknown_type_error(self):
        """Verify unknown type returns error."""
        from qanot.registry import ToolRegistry
        from qanot.tools.local import register_local_tools

        registry = ToolRegistry()
        register_local_tools(registry)

        handler = registry.get_handler("generate_document")
        result = await handler({
            "type": "nonexistent",
            "company": "Test",
            "counterparty": "Test2",
        })

        data = json.loads(result)
        assert "error" in data
        assert "oldi_sotdi" in data["error"]  # should list available types


# ═══════════════════════════════════════════════════════════
# TIER 2 Tests
# ═══════════════════════════════════════════════════════════


class TestXizmat:
    def test_basic_generation(self):
        content = generate_xizmat(SAMPLE_PARAMS)
        assert "XIZMAT KO'RSATISH SHARTNOMASI" in content
        assert "703-714" in content
        assert "Ijrochi" in content
        assert "Buyurtmachi" in content

    def test_report_section(self):
        content = generate_xizmat(SAMPLE_PARAMS)
        assert "HISOBOT" in content
        assert "dalolatnoma" in content

    def test_no_report(self):
        params = {**SAMPLE_PARAMS, "report_required": False}
        content = generate_xizmat(params)
        assert "HISOBOT" not in content

    def test_termination_right(self):
        content = generate_xizmat(SAMPLE_PARAMS)
        assert "FK 709" in content
        assert "istalgan vaqt" in content


class TestQabulTopshirish:
    def test_basic_generation(self):
        content = generate_qabul_topshirish(SAMPLE_PARAMS)
        assert "QABUL-TOPSHIRISH DALOLATNOMASI" in content
        assert "Topshiruvchi" in content
        assert "Qabul qiluvchi" in content

    def test_with_items(self):
        params = {**SAMPLE_PARAMS, "items": [
            {"name": "Printer", "quantity": 3, "unit": "dona", "price": 2_000_000},
        ]}
        content = generate_qabul_topshirish(params)
        assert "Printer" in content
        assert "6,000,000" in content

    def test_quality_ok(self):
        content = generate_qabul_topshirish(SAMPLE_PARAMS)
        assert "da'volar: YO'Q" in content

    def test_quality_issues(self):
        params = {**SAMPLE_PARAMS, "quality_ok": False, "remarks": "2 ta shikastlangan"}
        content = generate_qabul_topshirish(params)
        assert "da'volar: BOR" in content
        assert "2 ta shikastlangan" in content


class TestBuyruqT1:
    def test_basic_generation(self):
        params = {**SAMPLE_PARAMS, "employee_name": "Karimov Ali", "position": "Hisobchi", "salary": 8_000_000}
        content = generate_buyruq_t1(params)
        assert "BUYRUQ" in content
        assert "Ishga qabul qilish" in content
        assert "T-1" in content
        assert "VMQ 1297" in content
        assert "Karimov Ali" in content
        assert "Hisobchi" in content

    def test_with_probation(self):
        params = {**SAMPLE_PARAMS, "employee_name": "Test", "position": "Test", "probation_months": 2}
        content = generate_buyruq_t1(params)
        assert "Sinov muddati: 2 oy" in content


class TestBuyruqT6:
    def test_basic_generation(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Karimov Ali",
            "position": "Hisobchi",
            "leave_from": "01.04.2026",
            "leave_to": "15.04.2026",
            "leave_days": 15,
        }
        content = generate_buyruq_t6(params)
        assert "BUYRUQ" in content
        assert "ta'til" in content.lower()
        assert "T-6" in content
        assert "Karimov Ali" in content
        assert "15 kalendar kun" in content

    def test_leave_types(self):
        for lt in ("yillik", "qoshimcha", "haqi_saqlanmaydigan", "oquv"):
            params = {**SAMPLE_PARAMS, "employee_name": "T", "position": "T", "leave_type": lt}
            content = generate_buyruq_t6(params)
            assert "Ta'til turi:" in content


class TestBuyruqT8:
    def test_basic_generation(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Karimov Ali",
            "position": "Hisobchi",
            "dismissal_reason": "oz_xohishi",
        }
        content = generate_buyruq_t8(params)
        assert "BUYRUQ" in content
        assert "bekor qilish" in content
        assert "T-8" in content
        assert "MK 99" in content

    def test_standard_reasons(self):
        reasons = ["oz_xohishi", "kelishuv", "muddat", "qisqartirish"]
        for r in reasons:
            params = {**SAMPLE_PARAMS, "employee_name": "T", "position": "T", "dismissal_reason": r}
            content = generate_buyruq_t8(params)
            assert "MK" in content  # each reason references MK article

    def test_accounting_instruction(self):
        params = {**SAMPLE_PARAMS, "employee_name": "T", "position": "T"}
        content = generate_buyruq_t8(params)
        assert "Bosh hisobchi" in content
        assert "mehnat daftarcha" in content


class TestPudrat:
    def test_basic_generation(self):
        content = generate_pudrat(SAMPLE_PARAMS)
        assert "PUDRAT SHARTNOMASI" in content
        assert "631-670" in content
        assert "Pudratchi" in content
        assert "Buyurtmachi" in content

    def test_legal_sections(self):
        content = generate_pudrat(SAMPLE_PARAMS)
        assert "FK 631" in content
        assert "FK 635" in content or "FK 636" in content  # materials
        assert "FK 647" in content or "FK 650" in content  # acceptance
        assert "FK 652" in content  # warranty
        assert "FK 653" in content  # liability

    def test_materials_options(self):
        for mat in ("pudratchi", "buyurtmachi", "aralash"):
            params = {**SAMPLE_PARAMS, "materials_by": mat}
            content = generate_pudrat(params)
            assert "MATERIALLAR" in content

    def test_warranty(self):
        params = {**SAMPLE_PARAMS, "warranty_months": 24}
        content = generate_pudrat(params)
        assert "24 oy" in content

    def test_prepayment(self):
        params = {**SAMPLE_PARAMS, "prepay_pct": 30}
        content = generate_pudrat(params)
        assert "30%" in content
        assert "Oldindan to'lov" in content


class TestNDA:
    def test_basic_generation(self):
        content = generate_nda(SAMPLE_PARAMS)
        assert "MAXFIYLIK SHARTNOMASI" in content
        assert "NDA" in content
        assert "O'RQ-370" in content

    def test_bilateral(self):
        params = {**SAMPLE_PARAMS, "nda_type": "ikki_tomonlama"}
        content = generate_nda(params)
        assert "Tomonlar" in content

    def test_unilateral(self):
        params = {**SAMPLE_PARAMS, "nda_type": "bir_tomonlama"}
        content = generate_nda(params)
        assert "Oshkor qiluvchi tomon" in content
        assert "Qabul qiluvchi tomon" in content

    def test_confidential_info_default(self):
        content = generate_nda(SAMPLE_PARAMS)
        assert "moliyaviy" in content
        assert "texnologik" in content
        assert "tijorat" in content

    def test_custom_confidential_info(self):
        params = {**SAMPLE_PARAMS, "confidential_info": "Dasturiy ta'minot kodi"}
        content = generate_nda(params)
        assert "Dasturiy ta'minot kodi" in content

    def test_validity_period(self):
        params = {**SAMPLE_PARAMS, "valid_months": 36}
        content = generate_nda(params)
        assert "36 oy" in content

    def test_criminal_liability(self):
        content = generate_nda(SAMPLE_PARAMS)
        assert "jinoiy javobgarlik" in content

    def test_return_obligation(self):
        content = generate_nda(SAMPLE_PARAMS)
        assert "qaytariladi" in content or "yo'q qilinadi" in content


class TestTushuntirishXati:
    def test_basic_generation(self):
        params = {
            **SAMPLE_PARAMS,
            "employee_name": "Rahimov Jasur",
            "position": "Hisobchi",
            "incident_date": "18.03.2026",
            "incident_description": "Ishga 2 soat kech keldim",
        }
        content = generate_tushuntirish_xati(params)
        assert "TUSHUNTIRISH XATI" in content
        assert "Rahimov Jasur" in content
        assert "18.03.2026" in content
        assert "2 soat kech" in content

    def test_mk_reference(self):
        params = {**SAMPLE_PARAMS, "employee_name": "T", "position": "T"}
        content = generate_tushuntirish_xati(params)
        assert "MK 181" in content
        assert "2 ish kuni" in content


class TestAriza:
    def test_ishga_ariza(self):
        params = {
            **SAMPLE_PARAMS,
            "ariza_type": "ishga",
            "employee_name": "Karimov Ali",
            "position": "Dasturchi",
            "desired_date": "01.04.2026",
        }
        content = generate_ariza(params)
        assert "ARIZA" in content
        assert "ishga qabul" in content
        assert "Karimov Ali" in content
        assert "Pasport nusxasi" in content

    def test_tatilga_ariza(self):
        params = {
            **SAMPLE_PARAMS,
            "ariza_type": "tatilga",
            "employee_name": "Karimov Ali",
            "position": "Dasturchi",
            "leave_type": "yillik",
            "leave_days": 15,
            "leave_from": "01.07.2026",
        }
        content = generate_ariza(params)
        assert "ARIZA" in content
        assert "ta'til" in content
        assert "15 kalendar kun" in content

    def test_boshatish_ariza(self):
        params = {
            **SAMPLE_PARAMS,
            "ariza_type": "boshatish",
            "employee_name": "Karimov Ali",
            "position": "Dasturchi",
            "desired_date": "01.04.2026",
        }
        content = generate_ariza(params)
        assert "ARIZA" in content
        assert "bo'shatish" in content
        assert "MK 99" in content
        assert "2 hafta" in content

    def test_with_reason(self):
        params = {
            **SAMPLE_PARAMS,
            "ariza_type": "boshatish",
            "employee_name": "T",
            "position": "T",
            "reason": "Oilaviy sharoit",
        }
        content = generate_ariza(params)
        assert "Oilaviy sharoit" in content
