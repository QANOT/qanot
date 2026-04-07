"""Biznes hujjat shablonlari — O'zR qonunchiligiga mos.

Split into doc_templates/ package. This module re-exports for backward compat.
"""

from qanot.tools.doc_templates import *  # noqa: F401,F403
from qanot.tools.doc_templates import (  # explicit re-exports for type checkers
    BLANK,
    TIER1_GENERATORS,
    TIER1_EXTRA_PARAMS,
    _rekvizit,
    _amount_str,
    _common_fields,
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
)
