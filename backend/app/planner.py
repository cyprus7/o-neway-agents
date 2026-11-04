"""Deterministic tool orchestration, constraint accounting and explainable ranking."""
import asyncio
import hashlib
import math
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from one_request_telemetry import set_attributes, traced

from .geography import PLACES, Place, distance_km, find_place, normalized
from .application.errors import PlannerUnavailable
from .application.ports import IntentModelPort, PlanningRepository
from .application.commands import StaleCommand, bounded_price_research
from .application.explanations import saved_plan_answer
from .models import Evidence, IntentExtraction, Job, Message, PlanSnapshot, Session, TravelIntent, TripOption, local_today, uid, utcnow
from .domain.travel_data import ComponentBundle, TravelDataPort, transport_mode
from .pricing.aggregation import summarize_prices
from .pricing.demo import build_demo_pricing
from .pricing.models import PriceObservation, PricingSummary
from .regional_events import EventResult
from .travel_dates import departure_dates


COUNTRY_ALIASES = {
    "CY": ("кипр", "cyprus"), "TR": ("турция", "turkey", "türkiye"), "EG": ("египет", "egypt"),
    "ES": ("испания", "spain"), "PT": ("португалия", "portugal"), "FR": ("франция", "france"),
    "AE": ("оаэ", "uae", "united arab emirates"), "GR": ("греция", "greece"), "IT": ("италия", "italy"),
    "GE": ("грузия", "georgia"), "HU": ("венгрия", "hungary"), "CZ": ("чехия", "czechia"),
    "AT": ("австрия", "austria"), "BG": ("болгария", "bulgaria"), "SI": ("словения", "slovenia"),
    "IS": ("исландия", "iceland"), "FI": ("финляндия", "finland"), "ID": ("индонезия", "indonesia"),
}

STYLE_LABELS = {"beach": "море и побережье", "nature": "природа и прогулки", "culture": "культурная программа", "city": "городской отдых", "food": "гастрономия", "family": "семейный формат", "ski": "горный курорт", "wellness": "спокойный отдых", "nightlife": "вечерняя жизнь"}
FIELD_LABELS = {"origin": "город отправления", "destination": "направление", "date_from": "дата выезда", "date_to": "дата возвращения", "nights_min": "длительность", "nights_max": "диапазон ночей", "adults": "число взрослых", "children_ages": "дети", "rooms": "количество номеров", "room_allocation": "распределение по номерам", "pets": "питомцы", "budget_total": "общий бюджет", "currency": "валюта", "climate_preferences": "пожелания к погоде", "trip_styles": "формат отдыха", "max_travel_minutes": "ограничение времени в пути", "direct_flight_preferred": "предпочтение прямого рейса", "hotel_requirements": "требования к проживанию", "hard_constraints": "обязательные условия", "soft_constraints": "предпочтения", "assumptions": "допущения"}
FIELD_LABELS.update(flight_departure_from="начало окна вылета", flight_departure_to="конец окна вылета")


def pricing_summary(intent: TravelIntent, components: tuple[Any, Any, Any], *, now: datetime | None = None) -> PricingSummary | None:
    quotes = {name: getattr(component, "quote", None) for name, component in zip(("flight", "stay", "transfer"), components)}
    if not any(quotes.values()):
        return None
    return summarize_prices(quotes, currency=intent.currency, budget=str(intent.budget_total) if intent.budget_total is not None else None, now=now)


def apply_option_pricing(option: TripOption, intent: TravelIntent, *, now: datetime | None = None) -> None:
    """Keep compatibility numbers only for fresh, scoped point observations."""
    components = (option.flight, option.stay, option.transfer)
    summary = pricing_summary(intent, components, now=now)
    option.pricing_summary = summary
    option.demo_pricing = build_demo_pricing(intent, option)
    option.tradeoffs = [
        "Стоимость рассчитана ориентировочно для демо; состав оценки показан в маршруте."
        if value == "Полная стоимость неизвестна: отсутствующие цены не считаются нулевыми." else value
        for value in option.tradeoffs
    ]
    if summary is None:
        return
    for component in components:
        quote = component.quote
        if quote is not None:
            current = quote.normalized_group
            component.price = float(current.amount) if quote.outcome == "quoted" and quote.freshness(now) != "stale" and current is not None and current.kind == "point" else None
            component.status = "observed" if component.price is not None else "estimated" if quote.outcome == "quoted" and current is not None else "unavailable"
    option.estimated_total = float(summary.full_trip.amount) if summary.full_trip is not None and summary.full_trip.kind == "point" else None
    option.price_status = "observed" if option.estimated_total is not None else "partial" if summary.known_partial is not None else "unavailable"
    for check in option.constraint_checks:
        if check.get("constraint") == "Общий бюджет":
            check.update(status=summary.hard_constraint_status, detail=summary.budget_explanation,
                         assessment=summary.budget_assessment,
                         known_partial_total=summary.known_partial.model_dump(mode="json") if summary.known_partial else None)


def option_place(option: TripOption) -> Place:
    known = find_place(option.destination)
    return known or Place(option.destination, option.country, option.country_code or "", option.latitude, option.longitude,
                          option.flight.details.get("airport"), (), option.destination_aliases, "", option.subtitle,
                          scope=option.destination_scope)


def refresh_option_checks(option: TripOption, intent: TravelIntent) -> None:
    place = option_place(option)
    old_failed = any(check.get("status") == "failed" for check in option.constraint_checks)
    option.constraint_checks = constraint_checks(intent, place, (option.flight, option.stay, option.transfer))
    apply_option_pricing(option, intent)
    failed = any(check.get("status") == "failed" for check in option.constraint_checks)
    origin = find_place(intent.origin or "")
    if origin:
        option.score = candidate_score(place, origin, intent, option.weather)[0]
        if failed:
            option.score = max(0, option.score - 50)
    elif failed and not old_failed:
        option.score = max(0, option.score - 50)
    option.risks = [risk for risk in option.risks if risk != "Есть нарушение обязательного ограничения; выбор и симуляция заблокированы."]
    if failed:
        option.risks.insert(0, "Есть нарушение обязательного ограничения; выбор и симуляция заблокированы.")


def blocking_question(intent: TravelIntent) -> str | None:
    missing = []
    if not intent.origin:
        missing.append("из какого города отправляетесь")
    if not intent.date_from or not intent.date_to:
        missing.append("даты поездки или месяц и число ночей")
    elif intent.date_from < local_today():
        missing.append("будущие даты поездки — указанная дата выезда уже прошла")
    elif intent.flight_departure_from is not None and intent.flight_departure_from < local_today():
        missing.append("будущее окно вылета — начало указанного окна уже прошло")
    if intent.budget_total is None and "budget_unlimited" not in intent.soft_constraints:
        missing.append("общий бюджет и валюту на всех путешественников (или напишите «без ограничения бюджета»)")
    if transport_mode(intent) == 'unsupported':
        missing.append('способ передвижения: для запрошенного вида транспорта пока нет адаптера; уточните допустимую альтернативу или сохраните его для отдельной проверки')
    if missing:
        return "Чтобы собрать сравнимые варианты, уточните: " + "; ".join(missing) + ". Остальные пожелания я сохраню."
    return None


def geographic_name_pattern(value: str) -> str:
    """Recognize common Russian case endings without loose substring matches."""
    value = normalized(value)
    if not re.search(r"[а-я]", value):
        return re.escape(value)
    if value == "египет":
        return r"егип(?:ет|та|те|ту|том)"
    if value.endswith("ия"):
        return re.escape(value[:-1]) + r"(?:я|и|е|ю|ей|ею)"
    if value.endswith("а"):
        return re.escape(value[:-1]) + r"(?:а|ы|е|у|ой|ою)"
    if value.endswith("я"):
        return re.escape(value[:-1]) + r"(?:я|и|е|ю|ей|ею)"
    if value.endswith("й"):
        return re.escape(value[:-1]) + r"(?:й|я|ю|ем|е)"
    if value.endswith("ь"):
        return re.escape(value[:-1]) + r"(?:ь|и|ью)"
    if re.search(r"[бвгджзклмнпрстфхцчшщ]$", value):
        return re.escape(value) + r"(?:а|е|у|ом)?"
    return re.escape(value)


def geography_negated(value: str, names: tuple[str, ...]) -> bool:
    """A negation must address the place, not a flight/hotel condition near it.

    Broad matching such as ``no ... Turkey`` mistakes ``no more than 3 hours
    to Turkey`` for a country exclusion. Only geographic verbs/prepositions
    and explicit geographic lists can connect a negation to its object.
    Unknown wording remains an unverified hard constraint instead.
    """
    target = r"(?<!\w)(?:" + "|".join(geographic_name_pattern(name) for name in names if name) + r")(?!\w)"
    all_names = {alias for aliases in COUNTRY_ALIASES.values() for alias in aliases}
    all_names.update(candidate.name for candidate in PLACES)
    any_place = r"(?<!\w)(?:" + "|".join(geographic_name_pattern(name) for name in sorted(all_names)) + r")(?!\w)"
    negative = (r"(?<!\w)(?:exclude|excluding|avoid|except(?:\s+for)?|no|not|"
                r"(?:do\s+not|don't)\s+want|без|кроме|исключ(?:ить|и|ите|аю|аем)|"
                r"избега(?:ть|ю|ем|йте)|не\s+(?:хочу|хотим|рассматрива(?:ю|ем)|предлаг(?:ай|айте)|надо)|не)(?!\w)")
    qualifier = r"(?:(?:to|in|visit|visiting|go|travel|trips?|holidays?|в|на|лететь|ехать|ездить|поехать|поездку|поездок|поездки|отдых|отдыха|путешествовать)\s+){0,5}"
    geographic_list = rf"(?:{any_place}\s*(?:,|\band\b|\bor\b|\bи\b|\bили\b)\s*(?:(?:в|на|to|in)\s+)?)*"
    # A comma can also start a new positive clause ("не хочу в Египет,
    # Турция подходит"), rather than continue the exclusion list.
    positive_clause = r"(?!\s+(?:подходит|подойдут|можно|хочу|рассматрива\w*|предпочита\w*|allowed|works|fine|is\s+(?:fine|okay|ok))(?!\w))"
    if re.search(rf"{negative}\s+{qualifier}{geographic_list}{target}{positive_clause}", value):
        return True
    return bool(re.search(rf"{target}\s+(?:исключить|не\s+(?:рассматривать|предлагать)|excluded)(?!\w)", value))


def excluded(place: Place, intent: TravelIntent) -> bool:
    for constraint in intent.hard_constraints:
        value = normalized(constraint)
        if value.startswith("exclude_country:") and value.split(":", 1)[1].strip().upper() == place.country_code:
            return True
        if value.startswith("exclude_destination:"):
            banned = value.split(":", 1)[1].strip()
            if banned and (banned == normalized(place.name) or banned in tuple(normalized(alias) for alias in place.aliases)):
                return True
        # Readable exclusions remain enforceable if the model did not normalize them.
        # Hotel/transport negatives must never become geographic exclusions.
        names = (place.name,) + place.aliases + COUNTRY_ALIASES.get(place.country_code, ())
        if geography_negated(value, names):
            return True
    return False


def candidate_score(place: Place, origin: Place, intent: TravelIntent, weather: dict[str, Any] | None = None) -> tuple[float, list[str]]:
    score = 48.0
    reasons: list[str] = []
    styles = [style for style in intent.trip_styles if style in place.tags]
    if styles:
        score += min(24, 12 * len(styles))
        reasons.append("По справочнику подходит под запрос: " + ", ".join(STYLE_LABELS.get(style, style) for style in styles) + ".")
    if intent.children_ages and "family" in place.tags:
        score += 6
        reasons.append("Курортный формат подходит для семейного поиска; конкретное размещение с детьми ещё не подтверждено.")
    high = (weather or {}).get("temperature_max")
    climate = intent.climate_preferences
    if isinstance(high, (float, int)):
        basis = "По прогнозу" if weather.get("kind") == "forecast" else "По историческим данным за один прошлый год"
        if "warm" in climate:
            score += max(-16, min(20, (high - 15) * 1.6))
            reasons.append(f"{basis}: средняя дневная температура {high:g} °C; это ориентир для сравнения.")
        if "avoid_heat" in climate:
            score += 10 if 12 <= high <= 27 else -10
        if "cold" in climate or "snow" in climate:
            score += 12 if high < 8 else -8
        if "mild" in climate:
            score += 12 if 14 <= high <= 25 else -6
    elif any(tag in place.tags for tag in climate):
        score += 8
        reasons.append("Общий характер направления соответствует пожеланиям к климату; погода на даты неизвестна.")
    # Distance is used only as an explicitly described soft proxy, never as a verified flight duration.
    km = distance_km(origin, place)
    score += max(0, 8 - km / 600)
    if intent.direct_flight_preferred or intent.max_travel_minutes or "short_flight" in intent.soft_constraints:
        score += max(0, 7 - km / 500)
        reasons.append(f"Географическая близость: примерно {round(km):,} км по прямой. Реальное время и прямой рейс требуют проверки.".replace(",", " "))
    if intent.destination:
        score += 8
        reasons.append("Соответствует указанному направлению.")
    if not reasons:
        reasons.append("Направление из географического справочника; сравните источники и уточните предпочтения.")
    return round(max(0, min(98, score)), 1), reasons


def constraint_checks(intent: TravelIntent, place: Place, components: tuple[Any, Any, Any]) -> list[dict[str, Any]]:
    flight, stay, transfer = components
    checks: list[dict[str, Any]] = [{"constraint": "Направление не исключено", "status": "failed" if excluded(place, intent) else "passed", "detail": "Проверено по обязательным ограничениям запроса"}]
    prices = [component.price for component in components]
    known_total = sum(price for price in prices if price is not None)
    if intent.budget_total is not None:
        summary = pricing_summary(intent, components)
        if summary is not None:
            checks.append({"constraint": "Общий бюджет", "status": summary.hard_constraint_status,
                           "detail": f"Лимит {intent.budget_total:g} {intent.currency}. " + summary.budget_explanation,
                           "limit": intent.budget_total, "assessment": summary.budget_assessment,
                           "known_partial_total": summary.known_partial.model_dump(mode="json") if summary.known_partial else None})
        else:
            status = "failed" if known_total > intent.budget_total else "unknown" if any(price is None for price in prices) else "passed"
            checks.append({"constraint": "Общий бюджет", "status": status, "detail": f"Лимит {intent.budget_total:g} {intent.currency}. " + ("Известная часть расходов уже превышает лимит." if status == "failed" else "Соответствие не подтверждено: отсутствуют проверенные цены всех компонентов." if status == "unknown" else "Сумма наблюдаемых цен не превышает лимит; цену у продавца нужно перепроверить."), "limit": intent.budget_total, "known_partial_total": known_total if any(price is not None for price in prices) else None})
    if intent.pets:
        checks.append({"constraint": "Поездка с питомцем", "status": "unknown", "detail": "Нет подтверждения перевозки и правил конкретного отеля. Порода, вес, доплаты и документы требуют согласования с продавцами."})
    if intent.max_travel_minutes:
        duration = flight.details.get("duration_minutes")
        valid_duration = isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0
        # A flight duration is only a lower bound on the complete journey.
        # Unknown ground segments cannot turn that bound into a passed limit.
        exceeds = valid_duration and duration > intent.max_travel_minutes
        detail = "длительность транспорта неизвестна."
        if valid_duration:
            detail = f"наблюдаемая длительность транспорта {duration:g} мин.; " + ("уже превышает общий лимит даже без наземных участков." if exceeds else "общее время с наземными участками не подтверждено.")
        checks.append({"constraint": "Время в пути", "status": "failed" if exceeds else "unknown", "detail": f"Лимит {intent.max_travel_minutes} мин.; " + detail})
    if intent.direct_flight_preferred or "direct_flight" in intent.hard_constraints:
        verified = flight.details.get("direct_flight_verified")
        checks.append({"constraint": "Прямой рейс" + (" (обязательно)" if "direct_flight" in intent.hard_constraints else " (предпочтение)"), "status": "passed" if verified else "unknown", "detail": "Подтверждён поисковым результатом; окончательно проверьте расписание." if verified else "Прямой рейс не подтверждён. Географическая близость не означает наличие рейса."})
    for requirement in intent.hotel_requirements:
        checks.append({"constraint": "Проживание: " + requirement, "status": "unknown", "detail": "Правила и удобства конкретного объекта не проверены."})
    for requirement in intent.hard_constraints:
        if requirement.startswith(("exclude_country:", "exclude_destination:")) or requirement == "direct_flight":
            continue
        if requirement in ('no_purchases', 'no_bookings_without_consent', 'simulation_only'):
            # This is an application invariant, not a fact to verify with a hotel.
            continue
        label = requirement.split(':', 1)[1].strip() if requirement.startswith('transport_requirement:') else requirement
        checks.append({"constraint": label, "status": "unknown", "detail": "Обязательное условие сохранено; подтверждающего источника пока нет."})
    if any(value in ('no_purchases', 'no_bookings_without_consent', 'simulation_only') for value in intent.hard_constraints):
        checks.append({'constraint': 'Без реальной покупки', 'status': 'passed',
            'detail': 'Демо-оплата — симуляция: деньги не списываются, реальная бронь не создаётся.'})
    checks.append({"constraint": "Наличие билетов и размещения", "status": "unknown", "detail": "Поисковая выдача и ссылки не подтверждают бронируемое наличие. Реальной брони нет."})
    checks.append(temporal_connection_check(flight, stay, transfer))
    return checks


def temporal_connection_check(flight: Any, stay: Any, transfer: Any) -> dict[str, Any]:
    check = {"constraint": "Прибытие → трансфер → заселение", "status": "unknown", "detail": "Нет подтверждённых времени прибытия, длительности трансфера и крайнего времени заселения с часовыми поясами. Стыковка не проверена."}
    try:
        arrival = datetime.fromisoformat(flight.details["arrival_at"].replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(stay.details["check_in_until"].replace("Z", "+00:00"))
        duration = float(transfer.details["duration_minutes"])
        if arrival.tzinfo is None or deadline.tzinfo is None or duration < 0:
            return check
        at_hotel = arrival + timedelta(minutes=duration)
        if at_hotel > deadline:
            return {**check, "status": "failed", "detail": "Расчётное прибытие после трансфера позже крайнего времени заселения. Выбор заблокирован.", "hotel_arrival_at": at_hotel.isoformat(), "check_in_until": deadline.isoformat()}
        return {**check, "status": "passed", "detail": "Прибытие плюс известная длительность трансфера укладываются в крайнее время заселения; задержки не учитываются.", "hotel_arrival_at": at_hotel.isoformat(), "check_in_until": deadline.isoformat()}
    except (KeyError, TypeError, ValueError, OverflowError):
        return check


class Planner:
    def __init__(self, store: PlanningRepository, llm: IntentModelPort | None, providers: TravelDataPort):
        self.store, self.llm, self.providers = store, llm, providers

    @traced("planner.candidates")
    async def candidates(self, intent: TravelIntent, origin: Place) -> list[Place]:
        if intent.destination:
            requested = normalized(intent.destination)
            country = next((code for code, aliases in COUNTRY_ALIASES.items() if requested in aliases), None)
            if country:
                candidates = [place for place in PLACES if place.country_code == country]
            else:
                place = await self.providers.resolve_place(intent.destination)
                candidates = [place] if place else []
        else:
            candidates = list(PLACES)
        candidates = [place for place in candidates if not excluded(place, intent) and distance_km(origin, place) > 25]
        candidates.sort(key=lambda place: (-candidate_score(place, origin, intent)[0], place.name))
        # Diverse first pass; allow repeated countries when user requests that country.
        if not intent.destination:
            diverse: list[Place] = []
            seen: set[str] = set()
            for place in candidates:
                if place.country_code not in seen:
                    diverse.append(place)
                    seen.add(place.country_code)
            candidates = diverse + [place for place in candidates if place not in diverse]
        return candidates[:6]

    @traced("planner.build_options")
    async def build_options(self, session: Session, origin: Place, candidates: list[Place]) -> list[TripOption]:
        intent = session.intent
        self.store.add_event(session.id, "weather", "running", "Запрашиваю погодный контекст через порт данных: прогноз и прошлые наблюдения будут отмечены раздельно.")
        weather_results = await asyncio.gather(*(self.providers.weather(place, intent) for place in candidates))
        ranked = [(place, weather, *candidate_score(place, origin, intent, weather)) for place, weather in zip(candidates, weather_results)]
        ranked.sort(key=lambda row: (-row[2], row[0].name))
        amendment = session.preferences.get("aftercare_amendment", {})
        fixed = session.previous_plan.options[0] if (amendment.get("fixed_hotel")
            and session.previous_plan and len(session.previous_plan.options) == 1) else None
        if fixed is not None:
            ranked = [row for row in ranked if same_city(row[0].name, fixed.destination)]
        if not intent.destination:
            unique: list[Any] = []
            seen: set[str] = set()
            for row in ranked:
                if row[0].country_code not in seen:
                    unique.append(row)
                    seen.add(row[0].country_code)
            ranked = unique + [row for row in ranked if row not in unique]
        top = ranked[:3]
        self.store.add_event(session.id, "weather", "completed", "Погодные данные сопоставлены с датами; недоступные значения остаются неизвестными.", types=[row[1].get("kind") for row in top])
        self.store.add_event(session.id, "search", "running", "Проверяю локальные данные направления; недостающие сведения ищу в разрешённых резервных источниках.", destinations=[row[0].name for row in top])

        async def build(row: Any) -> list[TripOption]:
            place, weather, score, reasons = row
            async def events() -> EventResult:
                discovery = getattr(self.providers, "regional_events", None)
                if discovery is None:
                    return EventResult()
                try:
                    # An optional source must never fail the core trip or outlive its budget.
                    return await asyncio.wait_for(discovery(place, intent), timeout=12)
                except Exception:
                    return EventResult(status="unavailable", checked_at=utcnow())

            async def inventory():
                if fixed is not None:
                    previous = ComponentBundle.from_components(
                        [getattr(fixed, name).model_copy(deep=True) for name in ("flight", "stay", "transfer")])
                    # The source property is fixed; an amended scope cannot inherit
                    # old rates or a verified itinerary. Refresh owns identity checks.
                    if session.previous_plan.intent != intent:
                        for component in previous:
                            component.price, component.quote, component.status = None, None, "unavailable"
                    def identity(stay):
                        return (stay.details.get("catalog_property_id") or stay.details.get("pricing_entity_id"),
                                stay.title, stay.source_url)
                    original_identity = identity(previous.stay)
                    refreshed = await self.providers.refresh_inventory(place, origin, intent, previous)
                    if not isinstance(refreshed, ComponentBundle):
                        refreshed = ComponentBundle.from_components(refreshed)
                    if identity(refreshed.stay) != original_identity:
                        raise PlannerUnavailable("Источник не сохранил выбранное размещение. Уточните контекст поездки; замена отеля не выполнена.")
                    return [refreshed]
                discover = getattr(self.providers, "inventory_candidates", None)
                if discover is None:
                    return [await self.providers.inventory(place, origin, intent)]
                return await discover(place, origin, intent, limit=3)

            evidence, bundles, regional_events = await asyncio.gather(
                self.providers.search(place, intent), inventory(), events())

            def option_for(components):
                score, reasons = row[2], list(row[3])
                checks = constraint_checks(intent, place, components)
                if any(check["constraint"] == "Прибытие → трансфер → заселение" and check["status"] == "failed" for check in checks):
                    self.store.increment("itinerary_validation_errors")
                flight, stay, transfer = components
                known = [component.price for component in components if component.price is not None]
                total = round(sum(known), 2) if len(known) == 3 else None
                price_status = "observed" if len(known) == 3 else "partial" if known else "unavailable"
                geo_evidence = Evidence(provider=place.source_provider, source_url=place.wiki, retrieved_at=place.retrieved_at, freshness=place.freshness, confidence="География и общий характер направления; не инвентарь", label="Справочник направления и округлённые координаты; не расписание, цены или наличие.")
                all_evidence = [geo_evidence] + evidence + [Evidence.model_validate(item) for item in weather.get("evidence", [])]
                for component in components:
                    all_evidence.extend(component.evidence)
                risks = ["Нет подтверждения наличия билетов и размещения; эта карточка — исследованный план, не предложение бронирования.", "Визы, въездные правила, страховку и документы проверьте по гражданству путешественников."]
                tradeoffs = ["Стоимость рассчитана ориентировочно для демо; состав оценки показан в маршруте."] if total is None else ["Наблюдаемые цены могут измениться; перед оплатой требуется проверка у продавцов."]
                if weather.get("kind") == "historical":
                    tradeoffs.append("Погода показана по одному прошлому году: это не прогноз и не гарантия условий на поездку.")
                if weather.get("kind") == "unknown":
                    tradeoffs.append("Погодный источник недоступен, температура неизвестна.")
                if intent.pets:
                    risks.append("Питомец: перевозка и pet-friendly размещение не подтверждены; до проверки вариант нельзя считать выполнимым.")
                if intent.hotel_requirements:
                    risks.append("Обязательные условия проживания сохранены, но конкретным объектом ещё не подтверждены.")
                if intent.max_travel_minutes:
                    risks.append("Максимальное время в пути требует подтверждения расписанием и наземными участками.")
                if any(check["status"] == "failed" for check in checks):
                    score = max(0, score - 50)
                    risks.insert(0, "Есть нарушение обязательного ограничения; выбор и симуляция заблокированы.")
                option_id = f"r{session.revision}-" + (components.candidate_id if isinstance(components, ComponentBundle) and components.candidate_id else hashlib.sha256(f"{place.name}:{place.latitude}:{place.longitude}:{stay.title}:{stay.source_url}".encode()).hexdigest()[:12])
                option = TripOption(id=option_id, destination=place.name, country=place.country, subtitle=stay.title if len(bundles) > 1 else place.subtitle, latitude=place.latitude, longitude=place.longitude, score=score, estimated_total=total,
                                    country_code=place.country_code, destination_scope=place.scope, destination_aliases=place.aliases,
                                    currency=intent.currency, price_status=price_status, flight=flight, stay=stay, transfer=transfer, weather=weather, reasons=reasons, tradeoffs=tradeoffs, risks=risks,
                                    evidence=all_evidence, constraint_checks=checks, booking_url=stay.details.get("booking_search_url") or stay.source_url,
                                    regional_events=regional_events)
                apply_option_pricing(option, intent)
                return option

            return [option_for(bundle) for bundle in bundles[:3]]

        groups = await asyncio.gather(*(build(row) for row in top))
        options = [option for group in groups for option in group]
        options.sort(key=lambda option: (-option.score, option.destination, option.stay.title, option.id))
        # Different destinations first for inspiration, genuine properties for a fixed city.
        preferred, deferred, seen, identities = [], [], set(), set()
        for option in options:
            identity = (option.destination, option.stay.title, option.stay.source_url)
            if identity in identities:
                continue
            identities.add(identity)
            if option.destination in seen:
                deferred.append(option)
            else:
                preferred.append(option)
                seen.add(option.destination)
        options = (preferred + deferred)[:3]
        observed = sum(option.price_status != "unavailable" for option in options)
        message = ("Варианты собраны с ориентировочными демо-ценами. Основания расчёта доступны в маршруте."
                   if options else "Источники не вернули подходящих карточек отелей для этого запроса.")
        self.store.add_event(session.id, "search", "completed" if options else "warning", message, options=len(options), options_with_observed_prices=observed)
        return options

    def _complete(self, job, session, message, *, step="ranking", status="completed", accepted=False, action=None):
        """Publish the final result and terminal command in one committed snapshot."""
        with self.store.transaction():
            if accepted:
                session.accepted_plan_revision = session.revision
                session.previous_plan = None
                self.store.save_preferences(session)
            session.messages.append(Message(role="assistant", content=message))
            self.store.save_session(session)
            self.store.add_event(session.id, step, status, message)
            job.status, job.result_revision = "completed", session.revision
            job.result_option_ids = [option.id for option in session.options]
            self.store.save_job(job)
            self.store.increment("jobs_completed")
            if action in ("compare", "explain"):
                self.store.increment("actions_" + action)

    @traced("planner.run")
    @bounded_price_research
    async def run(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job or job.status not in ("queued", "running"):
            return
        started = time.monotonic()
        job.status = "running"
        self.store.save_job(job)
        try:
            self.store.add_event(job.session_id, "intent", "running", "Разбираю запрос и обновляю сохранённое состояние поездки.")
            session = self.store.get_session(job.session_id)
            if session is None:
                raise StaleCommand("missing_session")
            explicit_action = job.payload.get("action")
            if explicit_action in ("compare", "explain"):
                extraction = IntentExtraction(state_patch={}, action=explicit_action,
                                              option_ids=job.payload.get("option_ids", []))
                intent, changes = session.intent, []
            else:
                extraction, intent, changes = await self.llm.extract(session)
            # Reload because quota/repair events were appended during the model call.
            session = self.store.get_session(job.session_id)
            if session is None:
                raise StaleCommand("missing_session")
            if extraction.action in ("compare", "explain"):
                answer = ("Уточните: изменить условия поездки или объяснить сохранённые варианты?"
                          if extraction.state_patch or changes else
                          saved_plan_answer(session, extraction.action, extraction.option_ids))
                self._complete(job, session, answer, step="explanation", action=extraction.action)
                return
            with self.store.transaction():
                if session.options:
                    session.previous_plan = PlanSnapshot(revision=session.revision,
                        intent=session.intent.model_copy(deep=True),
                        options=[option.model_copy(deep=True) for option in session.options])
                session.intent = intent
                amendment = session.preferences.get("aftercare_amendment", {})
                if (amendment.get("fixed_hotel") and session.previous_plan and session.previous_plan.options
                        and intent.destination and not same_city(intent.destination, session.previous_plan.options[0].destination)):
                    amendment["fixed_hotel"] = False
                session.revision += 1
                session.last_changes = [FIELD_LABELS.get(field, field) for field in changes]
                session.assumptions = list(intent.assumptions)
                session.options = []
                session.selected_option_id = None
                session.clarification = extraction.blocking_question or blocking_question(intent)
                if extraction.action == "clarify" and not session.clarification:
                    session.clarification = "Уточните, какое условие поездки вы хотите изменить."
                self.store.save_session(session)
                self.store.add_event(session.id, "intent", "completed", "Параметры проверены; предыдущие условия сохранены.", changed_fields=changes, revision=session.revision)
            if session.clarification:
                session = self.store.get_session(session.id)
                self._complete(job, session, session.clarification, step="clarification", status="warning")
                self.store.increment("clarifications")
                return
            if changes and set(changes) <= {"budget_total"} and job.payload.get("previous_options"):
                previous_intent = TravelIntent.model_validate(job.payload["previous_intent"])
                if previous_intent.model_dump(exclude={"budget_total"}) != intent.model_dump(exclude={"budget_total"}):
                    raise PlannerUnavailable("Не удалось безопасно переиспользовать параметры цен. Повторите поиск.")
                session = self.store.get_session(session.id)
                session.options = [TripOption.model_validate(item) for item in job.payload["previous_options"]]
                for option in session.options:
                    option.id = f"r{session.revision}-" + option.id.split("-", 1)[-1]
                    refresh_option_checks(option, intent)
                session.options.sort(key=lambda option: (-option.score, option.destination))
                self._complete(job, session,
                    "Бюджет обновлен. Сохранил совместимые наблюдения цен без повторного обращения к поставщикам и пересчитал соответствие новому лимиту. Сроки и область применения источников не изменились; оформление остается симуляцией.",
                    step="prices", accepted=True)
                self.store.increment("pricing_budget_reuses")
                return
            self.store.add_event(session.id, "destinations", "running", "Разрешаю географию и применяю обязательные исключения направлений.")
            origin = await self.providers.resolve_place(intent.origin)
            if not origin:
                session = self.store.get_session(session.id)
                session.clarification = "Не удалось однозначно найти город отправления. Укажите название города и страну; остальные параметры сохранены."
                self._complete(job, session, session.clarification, step="destinations", status="warning")
                self.store.increment("clarifications")
                return
            candidates = await self.candidates(intent, origin)
            self.store.add_event(session.id, "destinations", "completed", "Кандидаты отобраны по географии и предпочтениям.", candidate_count=len(candidates))
            if not candidates:
                session = self.store.get_session(session.id)
                session.clarification = "Не нашёл направлений после обязательных исключений или не удалось определить географию. Уточните направление или ослабьте ограничение."
                self._complete(job, session, session.clarification, status="warning")
                self.store.increment("clarifications")
                return
            options = await self.build_options(session, origin, candidates)
            session = self.store.get_session(session.id)
            if not options:
                session.clarification = "Источники не вернули подходящих вариантов. Уточните направление или повторите поиск позже."
                self._complete(job, session, session.clarification, status="warning")
                return
            session.options = options
            changed = " Обновлены: " + ", ".join(session.last_changes) + "." if session.last_changes else ""
            answer = (f"Подготовил вариант по вашему запросу.{changed} " if len(options) == 1 else f"Сравнил {len(options)} варианта по вашему запросу.{changed} ")
            answer += "Порядок — объяснимый рейтинг предпочтений, а не вероятность успешной брони. "
            if all(option.estimated_total is None for option in options):
                answer += "Для каждого варианта рассчитан ориентировочный демо-бюджет: транспорт, проживание и трансфер. "
            if intent.pets:
                answer += "Условия для питомца отмечены как обязательные и непроверенные. "
            answer += "Цены и их источники показаны в маршруте. Выберите вариант и проверьте состав поездки."
            self._complete(job, session, answer, accepted=True)
            self.store.increment("recommendations_completed")
            self.store.increment("recommendation_seconds_total", time.monotonic() - started)
            self.store.record_first_recommendation(session.id, time.monotonic() - started)
        except StaleCommand:
            raise
        except asyncio.CancelledError:
            job.status = "interrupted"
            job.error = "Обработка прервана остановкой приложения. Повторите сообщение с новым ключом."
            self.store.save_job(job)
            raise
        except Exception as exc:
            set_attributes(outcome="unavailable")
            job.status = "failed"
            job.error = str(exc) if isinstance(exc, PlannerUnavailable) else "Не удалось завершить поиск. Состояние сохранено; повторите сообщение или уточните параметры."
            with self.store.transaction():
                self.store.add_event(job.session_id, "planner", "failed", job.error)
                session = self.store.get_session(job.session_id)
                if session:
                    session.messages.append(Message(role="assistant", content=job.error))
                    self.store.save_session(session)
                self.store.save_job(job)
                self.store.increment("jobs_failed")

    @traced("planner.refresh_prices")
    @bounded_price_research
    async def refresh_prices(self, job_id: str) -> None:
        """Refresh data through the port; optional price extraction shares inference quota."""
        job = self.store.get_job(job_id)
        if not job or job.kind != "prices_refresh" or job.status not in ("queued", "running"):
            return
        started = time.monotonic()
        job.status, job.started_at = "running", utcnow()
        job.progress = "Проверяю открытые источники цен"
        self.store.save_job(job)
        self.store.add_event(job.session_id, "prices", "running", job.progress)
        try:
            session = self.store.get_session(job.session_id)
            if session is None:
                raise PlannerUnavailable("Поездка не найдена.")
            if session.revision != job.captured_revision:
                self.store.apply_refresh(job, [], [])
                return
            deadline = datetime.fromisoformat(job.deadline_at) if job.deadline_at else datetime.now(timezone.utc) + timedelta(seconds=60)
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds() - 5
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(min(235, remaining)):
                origin = await self.providers.resolve_place(session.intent.origin)
                if origin is None:
                    raise PlannerUnavailable("Не удалось определить город отправления; уточните маршрут.")
                targets = [option.model_copy(deep=True) for option in session.options if option.id in job.payload["option_ids"]]

                async def update(option: TripOption) -> TripOption:
                    place = option_place(option)
                    components = (option.flight, option.stay, option.transfer)
                    prior_evidence = {item.model_dump_json() for component in components for item in component.evidence}
                    try:
                        if not place.country_code:
                            # Older non-catalogue plans did not persist the country code.
                            resolved = await self.providers.resolve_place(option.destination)
                            if resolved is not None:
                                place = resolved
                                option.country_code, option.destination_scope = place.country_code, place.scope
                                option.destination_aliases = place.aliases
                        updated = await self.providers.refresh_inventory(place, origin, session.intent,
                            ComponentBundle(flight=components[0], stay=components[1], transfer=components[2]))
                        option.flight, option.stay, option.transfer = updated.flight, updated.stay, updated.transfer
                        components = (option.flight, option.stay, option.transfer)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Keep provider exceptions and browser bodies out of public data.
                        for component in components:
                            component.quote = PriceObservation(provider_id="keyless-pricing", outcome="unavailable", reason="Источник не завершил проверку; попробуйте повторить")
                            component.price, component.status = None, "unavailable"
                        self.store.increment("pricing_provider_errors")
                    base = [item for item in option.evidence if item.model_dump_json() not in prior_evidence]
                    seen = {item.model_dump_json() for item in base}
                    for component in components:
                        for item in component.evidence:
                            if item.model_dump_json() not in seen:
                                base.append(item)
                                seen.add(item.model_dump_json())
                    option.evidence = base
                    option.booking_url = option.stay.details.get("booking_search_url") or option.stay.source_url
                    refresh_option_checks(option, session.intent)
                    return option

                options = await asyncio.gather(*(update(option) for option in targets))
            outcomes = []
            for option in options:
                for name in ("flight", "stay", "transfer"):
                    quote = getattr(option, name).quote
                    outcomes.append({"destination": option.destination, "component": name, "provider": quote.provider_id if quote else None,
                                     "outcome": quote.outcome if quote else "unavailable"})
                    self.store.increment("pricing_" + ("observed_components" if quote and quote.outcome == "quoted" and quote.amount_kind == "observed" else "estimated_components" if quote and quote.basis else "reference_components" if quote and quote.outcome == "reference" else "unknown_components"))
            if self.store.apply_refresh(job, options, outcomes):
                self.store.increment("pricing_jobs_completed")
            else:
                self.store.increment("pricing_deadline_exceeded" if job.status == "failed" else "pricing_jobs_superseded")
        except asyncio.CancelledError:
            job.status, job.error = "failed", "Проверка прервана перезапуском приложения. Повторите проверку цен."
            self.store.save_job(job)
            raise
        except TimeoutError:
            set_attributes(outcome="timeout")
            job.status, job.error = "failed", "Проверка цен не завершилась за отведенное время. Сохраненные результаты остаются с исходными сроками."
            self.store.increment("pricing_deadline_exceeded")
            self.store.save_job(job)
            self.store.add_event(job.session_id, "prices", "warning", job.error)
        except Exception as exc:
            set_attributes(outcome="unavailable")
            job.status = "failed"
            job.error = str(exc) if isinstance(exc, PlannerUnavailable) else "Не удалось завершить проверку цен. Повторите запрос."
            self.store.save_job(job)
            self.store.add_event(job.session_id, "prices", "failed", job.error)
        finally:
            self.store.increment("pricing_seconds", time.monotonic() - started)


class PriceValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def same_city(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    if normalized(first) == normalized(second):
        return True
    countries = {alias for aliases in COUNTRY_ALIASES.values() for alias in aliases}
    if normalized(first) in countries or normalized(second) in countries:
        return False
    first_place, second_place = find_place(first), find_place(second)
    return first_place is not None and second_place is not None and first_place.name == second_place.name


def validate_quote_context(session: Session, option: TripOption) -> None:
    intent = session.intent
    for name in ("flight", "stay", "transfer"):
        component = getattr(option, name)
        quote = component.quote
        # Published references are visible evidence, not a selected inventory quote.
        if quote is None or quote.outcome != "quoted":
            continue
        if quote.freshness() == "stale":
            raise PriceValidationError("price_expired", "Срок использованной цены истек. Запустите проверку цен и выберите обновленный вариант.")
        scope = quote.scope
        compatible = (scope is not None and scope.context_verified and scope.component == name
                      and (scope.date_from in departure_dates(intent) if name == "flight" else scope.date_from == intent.date_from)
                      and scope.date_to == intent.date_to
                      and scope.adults == intent.adults and sorted(scope.children_ages or ()) == sorted(intent.children_ages)
                      and scope.currency == intent.currency and same_city(scope.destination_city or scope.destination, option.destination))
        if compatible and name == "flight":
            compatible = scope.trip_type == "roundtrip" and same_city(scope.origin, intent.origin)
        if compatible and name == "stay":
            compatible = scope.rooms == intent.rooms
            if compatible and intent.room_allocation is not None:
                expected = sorted((room.adults, tuple(sorted(room.children_ages))) for room in intent.room_allocation)
                actual = sorted((room.adults, tuple(sorted(room.children_ages))) for room in scope.room_allocation or ())
                compatible = expected == actual
            property_id = component.details.get("pricing_entity_id") or component.details.get("property_id")
            if compatible and property_id is not None and scope.entity_id is not None:
                compatible = str(property_id) == scope.entity_id
        if not compatible:
            raise PriceValidationError("price_context_mismatch", "Цена относится к другим датам, группе или маршруту. Перепроверьте стоимость для текущей поездки.")


def validate_option(session: Session, option_id: str) -> TripOption:
    option = next((option for option in session.options if option.id == option_id), None)
    if not option or not option.id.startswith(f"r{session.revision}-"):
        raise ValueError("Вариант устарел или не найден. Выберите вариант из текущего результата.")
    if session.clarification or not session.intent.date_from or session.intent.date_from < local_today():
        raise ValueError("Даты или параметры поездки требуют уточнения.")
    validate_quote_context(session, option)
    # Budget can become unknown after expiry even without a new session revision.
    apply_option_pricing(option, session.intent)
    if any(check.get("status") == "failed" for check in option.constraint_checks):
        raise ValueError("Вариант нарушает обязательное ограничение. Измените параметры или выберите другой вариант.")
    return option


def simulate_checkout(session: Session, option: TripOption, demo_invoice: dict[str, Any] | None = None) -> dict[str, Any]:
    warnings = [check["constraint"] + ": " + check.get("detail", "Не подтверждено") for check in option.constraint_checks if check.get("status") == "unknown"]
    for component in (option.flight, option.stay, option.transfer):
        if component.quote and component.quote.outcome == "reference":
            warnings.append("Опубликованный ориентир: " + (component.quote.unit_label or component.title) + "; цена не подтверждена для текущих дат и группы")
    result = {"id": uid(), "session_id": session.id, "option_id": option.id, "status": "simulated", "created_at": utcnow(), "revision": session.revision,
            "message": "Симуляция завершена. Билеты, отель и трансфер НЕ забронированы. Оплата не выполнялась.", "ready_to_book": False,
            "estimated_total": option.estimated_total, "currency": option.currency, "quote_ids": list(option.pricing_summary.quote_ids) if option.pricing_summary else [], "warnings": warnings, "next_steps": ["Проверить цены и наличие по ссылкам поставщиков", "Подтвердить обязательные условия, правила перевозки и документы", "Оформить реальную поездку у выбранных продавцов самостоятельно"]}
    # Immutable simulation context, not evidence that any booking occurred.
    # Older receipts without this field stay unknown rather than borrowing a later plan.
    result["itinerary"] = {"intent": session.intent.model_dump(mode="json"),
                           "option": option.model_dump(mode="json")}
    if demo_invoice is not None:
        result["demo_payment"] = {"status": "simulated_paid", "invoice": demo_invoice,
                                  "paid_amount_minor": demo_invoice["total_minor"], "currency": demo_invoice["currency"],
                                  "card_label": "Вымышленная демо-карта"}
        result["message"] = "Демо-оплата прошла. Деньги не списаны, билеты, отель и трансфер не забронированы."
    return result
