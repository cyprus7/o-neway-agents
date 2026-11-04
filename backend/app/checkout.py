"""Fictional payment invoices. Never an inventory quote or payment gateway."""
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import Field, model_validator

from .models import OptionInput, Session, TripOption
from .pricing.demo import build_demo_pricing
from .pricing.demo_models import DemoPricing


_SIGNING_KEY = secrets.token_bytes(32)
QUOTE_LIFETIME_SECONDS = 15 * 60
DISCLAIMER = "Все суммы в этом счёте вымышленные и нужны только для демонстрации оплаты. Это не цена поездки, не подтверждение наличия и не проверка бюджета. Деньги не списываются, бронь не создаётся."


class QuoteInput(OptionInput):
    expected_revision: int = Field(ge=0)


class CheckoutInput(OptionInput):
    expected_revision: int | None = Field(default=None, ge=0)
    quote_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def quoted_revision(self):
        if self.quote_id is not None and self.expected_revision is None:
            raise ValueError("Quoted demo payment requires expected_revision")
        return self


class DemoQuoteError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _clock() -> float:
    return time.time()


def _context(session: Session, option: TripOption, pricing: DemoPricing) -> str:
    # Include actual source payloads, not only revision: unsupported same-revision
    # mutations must not authorize a previously reviewed payment.
    data = {"tariff_version": 2, "session_id": session.id, "revision": session.revision,
            "selected_option_id": session.selected_option_id,
            "intent": session.intent.model_dump(mode="json"),
            "option": option.model_dump(mode="json", exclude={"demo_pricing"}),
            "demo_pricing": pricing.model_dump(mode="json")}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _signature(payload: str) -> str:
    return hmac.new(_SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()


def demo_invoice(session: Session, option: TripOption, quote_id: str | None = None) -> dict[str, Any]:
    pricing = build_demo_pricing(session.intent, option)
    context = _context(session, option, pricing)
    if quote_id is None:
        expiry = int(_clock()) + QUOTE_LIFETIME_SECONDS
        payload = f"{expiry}.{context}"
        quote_id = f"{payload}.{_signature(payload)}"
    else:
        if not re.fullmatch(r"[0-9]{1,16}\.[0-9a-f]{64}\.[0-9a-f]{64}", quote_id):
            raise DemoQuoteError("demo_quote_invalid", "Счёт демо-оплаты недействителен. Обновите его перед оплатой.")
        try:
            expiry_text, issued_context, signature = quote_id.split(".")
            expiry = int(expiry_text)
        except (ValueError, TypeError):
            raise DemoQuoteError("demo_quote_invalid", "Счёт демо-оплаты недействителен. Обновите его перед оплатой.") from None
        if not hmac.compare_digest(signature, _signature(f"{expiry_text}.{issued_context}")):
            raise DemoQuoteError("demo_quote_invalid", "Счёт демо-оплаты недействителен. Обновите его перед оплатой.")
        if _clock() >= expiry:
            raise DemoQuoteError("demo_quote_expired", "Срок демо-счёта истёк. Обновите счёт и проверьте итог ещё раз.")
        if not hmac.compare_digest(context, issued_context):
            raise DemoQuoteError("demo_quote_changed", "Поездка или данные источников изменились. Проверьте новый демо-счёт перед оплатой.")

    labels = {"flight": "Транспорт", "stay": "Проживание", "transfer": "Трансфер"}
    line_items = [{"key": component.kind, "label": labels[component.kind],
                   "detail": "Пример внутри демо-диапазона · " + pricing.scope_label,
                   "amount_minor": component.example_amount_minor}
                  for component in pricing.components]
    subtotal = sum(item["amount_minor"] for item in line_items)
    service_fee = pricing.example_service_fee_minor
    summary = option.pricing_summary
    unknown = ["Реальные наличие, итоговые цены, налоги и комиссии поставщиков не подтверждены демо-оплатой."]
    if summary:
        unknown.extend(summary.unknown_charges)
        unknown.extend("Оплачивается отдельно у источника: " + charge for charge in summary.excluded_charges)
    return {"quote_id": quote_id, "context_id": context, "session_id": session.id, "option_id": option.id,
            "revision": session.revision, "destination": option.destination,
            "scope_label": pricing.scope_label,
            "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
            "pricing_basis": "synthetic_demo", "currency": pricing.currency, "line_items": line_items,
            "subtotal_minor": subtotal, "service_fee_minor": service_fee, "service_fee_label": "Демо-сервисный сбор · 2,5%",
            "tax_label": "Демо-налоги включены в вымышленные тарифы", "total_minor": pricing.example_total_minor,
            "disclaimer": DISCLAIMER, "unknown_real_conditions": list(dict.fromkeys(unknown)),
            "source_total": summary.full_trip.model_dump(mode="json") if summary and summary.full_trip else None}
