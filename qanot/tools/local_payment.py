"""To'lov havolasi — Click va Payme."""

from __future__ import annotations

import json
import logging

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_payment_tools(registry: ToolRegistry) -> None:
    """Register payment link tools."""

    async def create_payment_link(params: dict) -> str:
        """Click yoki Payme to'lov havolasi yaratish."""
        system = params.get("system", "click").lower()
        amount = params.get("amount", 0)
        order_id = params.get("order_id", "1")
        description = params.get("description", "")

        if not amount or amount <= 0:
            return json.dumps({"error": "Summa kiritilishi shart (0 dan katta)"})

        if system == "click":
            merchant_id = params.get("merchant_id", "")
            service_id = params.get("service_id", "")
            if not merchant_id or not service_id:
                return json.dumps({"error": "Click uchun merchant_id va service_id kerak. Config da sozlang."})
            url = (
                f"https://my.click.uz/services/pay"
                f"?service_id={service_id}"
                f"&merchant_id={merchant_id}"
                f"&amount={amount}"
                f"&transaction_param={order_id}"
            )
            return json.dumps({
                "system": "Click",
                "url": url,
                "amount": amount,
                "amount_formatted": f"{amount:,.0f} so'm",
            }, ensure_ascii=False)

        elif system == "payme":
            merchant_id = params.get("merchant_id", "")
            if not merchant_id:
                return json.dumps({"error": "Payme uchun merchant_id kerak. Config da sozlang."})
            # Payme amount in tiyin (1 so'm = 100 tiyin)
            amount_tiyin = int(amount * 100)
            url = (
                f"https://checkout.paycom.uz/{merchant_id}"
                f"?a={amount_tiyin}"
                f"&ac.order_id={order_id}"
            )
            return json.dumps({
                "system": "Payme",
                "url": url,
                "amount": amount,
                "amount_formatted": f"{amount:,.0f} so'm",
            }, ensure_ascii=False)

        else:
            return json.dumps({"error": f"Noma'lum to'lov tizimi: {system}. click yoki payme tanlang."})

    registry.register(
        name="payment_link",
        description="Generate a Click or Payme payment link to send to customers.",
        parameters={
            "type": "object",
            "required": ["amount"],
            "properties": {
                "system": {
                    "type": "string",
                    "description": "To'lov tizimi: click yoki payme (default: click)",
                },
                "amount": {
                    "type": "number",
                    "description": "Summa (so'mda)",
                },
                "order_id": {
                    "type": "string",
                    "description": "Buyurtma ID (ixtiyoriy)",
                },
                "merchant_id": {
                    "type": "string",
                    "description": "Merchant ID (Click yoki Payme)",
                },
                "service_id": {
                    "type": "string",
                    "description": "Service ID (faqat Click uchun)",
                },
                "description": {
                    "type": "string",
                    "description": "To'lov tavsifi",
                },
            },
        },
        handler=create_payment_link,
    )
