import asyncio
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from backend.app.geography import PLACES, find_place
from backend.app.llm import SlidingWindowLimiter, guard_hard_duration, parse_extraction, retry_after_seconds
from backend.app.models import IntentExtraction, Message, Session, TravelComponent, TravelIntent, local_today, merge_intent
from backend.app.planner import blocking_question, constraint_checks, excluded, simulate_checkout, temporal_connection_check, validate_option
from backend.app.storage import BusySession, Store


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_limiter_enforces_real_sliding_window_and_global_cooldown():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(10, clock=clock.clock, sleep=clock.sleep)
    for _ in range(10):
        assert await limiter.acquire() == 0
    assert len(limiter.timestamps) == 10
    assert await limiter.acquire() >= 60
    assert len(limiter.timestamps) == 1
    limiter.defer(20)
    assert await limiter.acquire() == pytest.approx(20)
    assert clock.now >= 80


@pytest.mark.asyncio
async def test_limiter_cannot_be_configured_above_provider_limit():
    assert SlidingWindowLimiter(900).limit == 10
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, clock=clock.clock, sleep=clock.sleep)
    await asyncio.gather(*(limiter.acquire() for _ in range(7)))
    assert clock.now >= 180
    assert len(limiter.timestamps) <= 2


def test_state_patch_preserves_original_family_dates_and_origin():
    before = TravelIntent(origin="Варшава", date_from="2027-11-23", date_to="2027-12-01", adults=2, children_ages=[6], budget_total=2500, climate_preferences=["warm"], trip_styles=["beach"])
    after, changed = merge_intent(before, {"pets": ["dog"], "budget_total": 2000})
    assert after.origin == "Варшава" and after.children_ages == [6]
    assert after.date_from == before.date_from and after.date_to == before.date_to
    assert after.climate_preferences == ["warm"]
    assert set(changed) == {"pets", "budget_total"}


def test_explicit_removal_and_duration_replanning():
    before = TravelIntent(origin="Берлин", date_from="2027-06-10", date_to="2027-06-18", nights_min=8, nights_max=8, pets=["dog"])
    after, changed = merge_intent(before, {"pets": [], "nights_min": 5, "nights_max": 5})
    assert after.date_to == date(2027, 6, 15)
    assert after.pets == []
    assert "date_to" in changed
    with pytest.raises(ValueError):
        merge_intent(before, {"fake_price": 123})
    with pytest.raises(ValidationError):
        merge_intent(before, {"date_to": "2027-06-01"})


def test_blocking_clarification_is_only_for_required_missing_fields():
    intent = TravelIntent(origin="Берлин", date_from=local_today() + timedelta(days=30), date_to=local_today() + timedelta(days=35), budget_total=1000)
    assert blocking_question(intent) is None
    missing = blocking_question(TravelIntent(trip_styles=["beach"]))
    assert "города" in missing and "даты" in missing and "бюджет" in missing
    unlimited = intent.model_copy(update={"budget_total": None, "soft_constraints": ["budget_unlimited"]})
    assert blocking_question(unlimited) is None


def test_bounded_schema_accepts_json_and_rejects_prose_and_impossible_ages():
    extraction = parse_extraction('```json\n{"state_patch":{"origin":"Рига"},"blocking_question":null}\n```')
    assert extraction.state_patch == {"origin": "Рига"}
    with pytest.raises(ValueError):
        parse_extraction('{"state_patch":{}} and an explanation')
    with pytest.raises(ValidationError):
        TravelIntent(children_ages=[18])
    assert retry_after_seconds("120") == 120
    assert retry_after_seconds("bad") == 60


def test_sqlite_persists_sessions_preferences_jobs_and_recovers_restart(tmp_path):
    path = str(tmp_path / "travel.db")
    store = Store(path)
    session = store.create_session()
    session.intent = TravelIntent(origin="Варшава", pets=["dog"])
    store.save_preferences(session)
    store.save_session(session)
    job = store.start_message(session.id, "Хочу в отпуск")
    with pytest.raises(BusySession):
        store.start_message(session.id, "Ещё запрос")
    store.close()
    restored = Store(path)
    assert restored.get_session(session.id).messages[0].content == "Хочу в отпуск"
    assert restored.get_session(session.id).intent.pets == ["dog"]
    restored.recover_interrupted()
    assert restored.get_job(job.id).status == "failed"
    suggestions = restored.create_session(session.id)
    assert suggestions.preferences["origin"] == "Варшава"
    assert suggestions.intent.origin is None  # suggestions never silently change a new trip
    restored.close()


def test_unknown_prices_never_pass_budget_or_pet_constraints():
    intent = TravelIntent(budget_total=1500, pets=["dog"], hotel_requirements=["доступный номер"], max_travel_minutes=180)
    components = tuple(TravelComponent(title="Unknown", description="No inventory") for _ in range(3))
    checks = constraint_checks(intent, PLACES[0], components)
    assert next(check for check in checks if check["constraint"] == "Общий бюджет")["status"] == "unknown"
    assert next(check for check in checks if check["constraint"] == "Поездка с питомцем")["status"] == "unknown"
    assert next(check for check in checks if check["constraint"] == "Время в пути")["status"] == "unknown"
    assert next(check for check in checks if check["constraint"] == "Прибытие → трансфер → заселение")["status"] == "unknown"
    assert all(component.price is None for component in components)


def test_known_partial_over_budget_is_rejected_and_exclusions_apply():
    intent = TravelIntent(budget_total=100, hard_constraints=["exclude_country:CY"])
    components = tuple(TravelComponent(title="Observed", description="Source", price=101 if i == 0 else None) for i in range(3))
    checks = constraint_checks(intent, PLACES[0], components)
    assert next(check for check in checks if check["constraint"] == "Общий бюджет")["status"] == "failed"
    assert excluded(PLACES[0], intent)
    assert not excluded(find_place("Анталья"), intent)


def test_temporal_connection_checks_real_aware_times_and_rejects_impossible():
    flight = TravelComponent(title="Рейс", description="", details={"arrival_at": "2027-06-10T22:30:00+03:00"})
    stay = TravelComponent(title="Отель", description="", details={"check_in_until": "2027-06-10T23:00:00+03:00"})
    transfer = TravelComponent(title="Трансфер", description="", details={"duration_minutes": 45})
    assert temporal_connection_check(flight, stay, transfer)["status"] == "failed"
    transfer.details["duration_minutes"] = 20
    assert temporal_connection_check(flight, stay, transfer)["status"] == "passed"
    flight.details["arrival_at"] = "2027-06-10T22:30:00"
    assert temporal_connection_check(flight, stay, transfer)["status"] == "unknown"


def test_job_success_is_distinct_from_completed_trip(tmp_path):
    store = Store(str(tmp_path / "metrics.db"))
    store.create_session()
    store.increment("jobs_completed")
    assert store.metrics()["job_success_rate"] == 1
    assert store.metrics()["scenario_completion_rate"] == 0
    assert store.metrics()["completion_rate"] == 0
    store.record_first_recommendation("first", 12)
    store.record_first_recommendation("first", 40)
    assert store.metrics()["first_recommendations"] == 1
    assert store.metrics()["mean_first_recommendation_seconds"] == 12
    store.close()


def test_short_flight_is_soft_and_numeric_hard_limit_requires_explicit_user_limit():
    session = Session(messages=[Message(role="user", content="Предпочитаем короткий перелёт")])
    invented = IntentExtraction(state_patch={"max_travel_minutes": 240, "assumptions": ["Короткий перелет принят не более 4 часов"]})
    guarded = guard_hard_duration(invented, session)
    assert "max_travel_minutes" not in guarded.state_patch
    assert guarded.state_patch["assumptions"] == []
    assert "short_flight" in guarded.state_patch["soft_constraints"]
    session.messages.append(Message(role="user", content="Перелёт не дольше 3 часов"))
    explicit = IntentExtraction(state_patch={"max_travel_minutes": 180})
    assert guard_hard_duration(explicit, session).state_patch["max_travel_minutes"] == 180
    session.messages.append(Message(role="user", content="А теперь берём собаку"))
    assert "max_travel_minutes" not in guard_hard_duration(explicit, session).state_patch
