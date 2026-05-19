"""Dreams — deterministic pre/post passes around the consolidation LLM.

``digest``  builds the bounded session-transcript input the
            consolidation agent reads (the *before* pass).
``verify``  runs the 5 mechanical probes over the freshly-built memory
            tree before the atomic swap (the *after* pass).

Both are LLM-free by design: cheap, deterministic, explainable.
"""

from __future__ import annotations

from .digest import build_session_digest, write_session_digest
from .verify import VerifyResult, verify_tree

__all__ = [
    "build_session_digest",
    "write_session_digest",
    "verify_tree",
    "VerifyResult",
]
