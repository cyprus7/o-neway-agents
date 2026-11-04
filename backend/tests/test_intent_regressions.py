"""Deterministic regressions with synthetic model outputs; no inference/network calls.

These exercise the extraction boundary and state invariants. They do not claim to
measure a real model's understanding of the supplied natural-language requests.
"""
import json

import httpx
import pytest

from backend.app.llm import LLMClient, PlannerUnavailable, guard_intent_patch
from backend.app.models import IntentExtraction, Message, Session, TravelIntent, merge_intent
from backend.app.storage import Store


def travel_session(text, **changes):
    state = {
        "origin": "Варшава", "destination": "Ларнака",
        "date_from": "2027-06-10", "date_to": "2027-06-18",
        "adults": 2, "children_ages": [6], "budget_total": 2400,
    }
    state.update(changes)
    return Session(intent=TravelIntent(**state), messages=[Message(role="user", content=text)])


@pytest.mark.parametrize("text,origin,destination", [
    ("Из Варшавы в тепло, берём собаку", "Варшава", "Варшава"),
    ("From Warsaw somewhere warm, with our dog", "Warsaw", "Warsaw"),
    ("Из Варшавы, отель с кухней", "Варшава", "Warsaw"),
    ("From Seattle, somewhere quiet", "Seattle", "Seattle"),
    ("Теперь вылетаем из Берлина", "Берлин", "Berlin"),
    ("Из Варшавы, но не в Варшаву", "Варшава", "Варшава"),
    ("From Warsaw, but not to Warsaw", "Warsaw", "Warsaw"),
])
def test_departure_city_cannot_silently_become_destination(text, origin, destination):
    session = travel_session(text)
    before = session.intent.model_dump()
    with pytest.raises(ValueError, match="only as origin"):
        guard_intent_patch(IntentExtraction(state_patch={"origin": origin, "destination": destination}), session)
    assert session.intent.model_dump() == before


@pytest.mark.parametrize("text", [
    "From Warsaw to Warsaw: a staycation with one hotel night",
    "Из Варшавы, но хотим отдых в городе отправления",
    "Из Варшавы хотим staycation",
    "Из Варшавы хотим в Варшаву: одну ночь в отеле",
])
def test_explicit_same_city_request_is_not_rewritten(text):
    extraction = IntentExtraction(state_patch={"destination": "Варшава"})
    assert guard_intent_patch(extraction, travel_session(text)).state_patch["destination"] == "Варшава"


def test_named_distinct_destination_and_origin_only_replan_are_untouched():
    extraction = IntentExtraction(state_patch={"origin": "Берлин", "destination": "Ларнака"})
    assert guard_intent_patch(extraction, travel_session("Из Берлина в Ларнаку")).state_patch == extraction.state_patch
    origin_only = IntentExtraction(state_patch={"origin": "Берлин"})
    assert guard_intent_patch(origin_only, travel_session("Теперь из Берлина")).state_patch == {"origin": "Берлин"}


@pytest.mark.parametrize("field,prior,addition", [
    ("pets", ["собака 18 кг"], "кошка в переноске"),
    ("hotel_requirements", ["без ступеней до душа"], "кухня"),
    ("hard_constraints", ["exclude_country:TR"], "кошка едет в салоне"),
    ("soft_constraints", ["наблюдать звёзды без засветки"], "музыкальные репетиции днём"),
    ("trip_styles", ["nature"], "culture"),
    ("climate_preferences", ["avoid_heat"], "mild"),
])
@pytest.mark.parametrize("text", ["Ещё добавь одно пожелание", "Also add one more preference"])
def test_additions_do_not_erase_unaffected_wishes(field, prior, addition, text):
    session = travel_session(text, **{field: prior})
    extraction = IntentExtraction(state_patch={field: [addition]})
    guarded = guard_intent_patch(extraction, session)
    assert guarded.state_patch[field] == [*prior, addition]
    assert getattr(session.intent, field) == prior


@pytest.mark.parametrize("text,desired", [
    ("Ещё убери бассейн, добавь кухню", ["кухня"]),
    ("Добавь кухню вместо бассейна", ["кухня"]),
    ("Also replace the pool with a kitchen", ["кухня"]),
    ("Ещё бассейн больше не нужен", []),
    ("Also remove all hotel requirements", []),
    ("Добавь только кухню", ["кухня"]),
    ("Добавь кухню, бассейн не важен", ["кухня"]),
    ("Ещё хотим кухню, но теперь без бассейна", ["кухня"]),
])
def test_explicit_replacement_and_removal_do_not_resurrect_old_constraints(text, desired):
    session = travel_session(text, hotel_requirements=["бассейн"])
    guarded = guard_intent_patch(IntentExtraction(state_patch={"hotel_requirements": desired}), session)
    assert guarded.state_patch["hotel_requirements"] == desired


def test_additional_transport_mode_does_not_resurrect_old_mode():
    session = travel_session("Ещё добавь поезд", soft_constraints=["transport:car", "короткий трансфер"])
    guarded = guard_intent_patch(IntentExtraction(state_patch={"soft_constraints": ["transport:train"]}), session)
    assert guarded.state_patch["soft_constraints"] == ["короткий трансфер", "transport:train"]


@pytest.mark.asyncio
async def test_geographic_error_uses_one_bounded_repair_and_preserves_prior_context(tmp_path):
    store = Store(str(tmp_path / "intent.db"))
    session = travel_session("Теперь из Варшавы, остальное оставь как было")
    store.save_session(session)
    answers = iter([
        '{"state_patch":{"origin":"Варшава","destination":"Варшава"}}',
        '{"state_patch":{"origin":"Варшава"}}',
    ])
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network call"))) as http:
        client = LLMClient(http, store)

        async def fake_completion(messages, session_id):
            calls.append([*messages])
            return next(answers)

        client.completion = fake_completion
        _, intent, _ = await client.extract(session)
    assert len(calls) == 2
    assert "origin/from versus destination/to roles" in calls[1][-1]["content"]
    assert intent.destination == "Ларнака"
    assert intent.date_from.isoformat() == "2027-06-10"
    assert intent.date_to.isoformat() == "2027-06-18"
    assert intent.children_ages == [6]
    assert store.metrics()["llm_json_repairs"] == 1
    store.close()


@pytest.mark.asyncio
async def test_repeated_semantically_invalid_geography_fails_without_mutating_session(tmp_path):
    store = Store(str(tmp_path / "intent.db"))
    session = travel_session("Из Варшавы, куда-нибудь к морю")
    store.save_session(session)
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network call"))) as http:
        client = LLMClient(http, store)

        async def fake_completion(messages, session_id):
            calls.append(session_id)
            return '{"state_patch":{"origin":"Варшава","destination":"Варшава"}}'

        client.completion = fake_completion
        with pytest.raises(PlannerUnavailable):
            await client.extract(session)
    assert len(calls) == 2
    assert store.get_session(session.id).intent == session.intent
    assert store.metrics()["llm_invalid_outputs"] == 1
    store.close()


UNUSUAL_WISHES = [
    "холодильник для инсулина с непрерывным питанием",
    "никаких ступеней от улицы до душа",
    "безглютеновая кухня без перекрёстного загрязнения",
    "номер без ароматизаторов и перьевых подушек",
    "тихий номер без мигающих индикаторов",
    "тёмное небо для астрофотографии",
    "разрешены дневные репетиции на виолончели",
    "надёжный проводной интернет для удалённой работы",
    "позднее заселение после полуночи",
    "разрешены две кошки с отдельными переносками",
    "хранение велосипеда в закрытом помещении",
    "кошерная кухня и механический ключ на шаббат",
    "доступ к розетке для аппарата CPAP",
    "душ с сиденьем и поручнями",
    "взрослый сопровождающий может спать в отдельной комнате",
    "возможность зарядить электрическую коляску",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("wish", UNUSUAL_WISHES)
async def test_unusual_qualified_wish_survives_followup_and_mocked_incomplete_patch(tmp_path, wish):
    store = Store(str(tmp_path / "intent.db"))
    session = travel_session("Ещё нужен чайник", hotel_requirements=[wish], hard_constraints=[wish])
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network call"))) as http:
        client = LLMClient(http, store)

        async def fake_completion(messages, session_id):
            calls.append(messages)
            # Synthetic model output intentionally forgets the still-applicable wish.
            return json.dumps({"state_patch": {"hotel_requirements": ["чайник"]}})

        client.completion = fake_completion
        _, intent, changed = await client.extract(session)
    assert len(calls) == 1
    assert intent.hotel_requirements == [wish, "чайник"]
    assert intent.hard_constraints == [wish]
    assert intent.pets == session.intent.pets
    assert intent.budget_total == 2400
    assert changed == ["hotel_requirements"]
    system = calls[0][0]["content"]
    assert "origin, never destination or a hotel location" in system
    assert "Preserve unusual wishes" in system
    assert "Explicit \"must/only/обязательно/без этого не поеду\"" in system
    assert "if requirements conflict" in system.casefold()
    store.close()


def test_soft_short_flight_with_extra_hotel_wish_still_cannot_gain_numeric_limit():
    session = travel_session("Ещё хотелось бы короткий перелёт и кухню", soft_constraints=["тихо"])
    extraction = IntentExtraction(state_patch={"max_travel_minutes": 240, "hotel_requirements": ["кухня"]})
    guarded = guard_intent_patch(extraction, session)
    assert "max_travel_minutes" not in guarded.state_patch
    assert guarded.state_patch["soft_constraints"] == ["тихо", "short_flight"]


@pytest.mark.parametrize("requirement", [
    "кошка в переноске в салоне самолёта",
    "подходящий трансфер для электроколяски 145 кг с батареей",
    "перевозка виолончели на отдельном месте",
    "место для нескладного велосипеда в дневном поезде",
])
def test_scoped_transport_condition_is_kept_out_of_lodging_even_if_model_misplaces_it(requirement):
    tagged = "transport_requirement:" + requirement
    session = travel_session("Нужны кухня и перевозка оборудования", hard_constraints=["exclude_country:TR"])
    guarded = guard_intent_patch(IntentExtraction(state_patch={"hotel_requirements": ["кухня", tagged]}), session)
    assert guarded.state_patch["hotel_requirements"] == ["кухня"]
    assert guarded.state_patch["hard_constraints"] == ["exclude_country:TR", tagged]


def test_explicit_soft_transport_preference_does_not_become_hard_when_moved_from_hotel():
    tagged = "transport_requirement:желательно перевезти велосипед без разборки"
    extraction = IntentExtraction(state_patch={"hotel_requirements": [tagged], "soft_constraints": [tagged]})
    guarded = guard_intent_patch(extraction, travel_session("Желательно взять велосипед"))
    assert guarded.state_patch["hotel_requirements"] == []
    assert guarded.state_patch["soft_constraints"] == [tagged]
    assert tagged not in guarded.state_patch["hard_constraints"]


@pytest.mark.parametrize("bad_constraints", ["not-an-array", 4, ["valid", 4]])
def test_role_correction_does_not_sanitize_malformed_constraint_types(bad_constraints):
    extraction = IntentExtraction(state_patch={
        "hotel_requirements": ["transport_requirement:кошка в салоне"],
        "hard_constraints": bad_constraints,
    })
    with pytest.raises(ValueError, match="lists of readable strings"):
        guard_intent_patch(extraction, travel_session("Кошка в салоне"))


@pytest.mark.parametrize("preference", ["недолгий перелёт", "короткие перелёты", "short flight", "short flights"])
def test_existing_qualitative_flight_preference_gets_ranker_tag_without_numeric_limit(preference):
    extraction = IntentExtraction(state_patch={"soft_constraints": [preference, "удобная пересадка"]})
    guarded = guard_intent_patch(extraction, travel_session("Предпочитаем недолгий перелёт"))
    assert "short_flight" in guarded.state_patch["soft_constraints"]
    assert "удобная пересадка" in guarded.state_patch["soft_constraints"]
    assert "max_travel_minutes" not in guarded.state_patch


REPAIR_ITERATION_CASES = [
    (
        "Небольшая собака корги, 7 кг, в переноске",
        {"pets": ["небольшая собака корги 7 кг в переноске"]},
        ("Keep small/large size, age, breed, number, carrier and exact weight",),
    ),
    (
        "В Прагу на музеи, пляжный отдых сейчас не ищем",
        {"trip_styles": ["culture"], "soft_constraints": ["музеи", "пляжный отдых сейчас не ищем"]},
        ("Preserve activity exclusions", "This is an activity preference"),
    ),
    (
        "Кошка должна лететь в салоне, отель должен принимать кошек",
        {"pets": ["кошка"], "hard_constraints": ["transport_requirement:кошка в переноске в салоне самолёта"],
         "hotel_requirements": ["допускаются кошки"]},
        ("Scope transport conditions", "Never put cabin carriage"),
    ),
    (
        "Электроколяска 145 кг с батареей, нужен доступный трансфер",
        {"hard_constraints": ["transport_requirement:электроколяска 145 кг с батареей",
                              "transport_requirement:необходим доступный трансфер для электроколяски 145 кг"]},
        ("A 145 kg electric wheelchair", "necessary transport conditions"),
    ),
    (
        "Везём виолончель на отдельном месте, а хранить будем в номере",
        {"hard_constraints": ["transport_requirement:перевозка виолончели на отдельном месте"],
         "hotel_requirements": ["хранение виолончели в номере"]},
        ("instrument/bicycle carriage", "A hotel storing a wheelchair, bicycle or instrument"),
    ),
    (
        "Халяльное питание обязательно, близость мечети желательна",
        {"hard_constraints": ["подтверждённое халяльное питание"],
         "hotel_requirements": ["подтверждённое халяльное питание"], "soft_constraints": ["близость мечети"]},
        ("Optional hotel/location wishes", "ONLY in soft_constraints"),
    ),
    (
        "Общий потолок 2000 EUR, из него не более 150 EUR на собаку",
        {"budget_total": 2000, "hard_constraints": ["из общего потолка не более 150 EUR на собаку"]},
        ("allocations INCLUDED", "without double-counting"),
    ),
    (
        "Один взрослый и младенец 0 лет без места; сборы за него обязательно учитывать",
        {"adults": 1, "children_ages": [0], "hard_constraints": [
            "transport_requirement:младенец без отдельного места", "учитывать все сборы за младенца"]},
        ("including infant charges belong in hard_constraints", "include all infants in children_ages"),
    ),
    (
        "Избегаем жары и влажности, хотелось бы минимум ветра; кондиционер обязателен",
        {"climate_preferences": ["avoid_heat"], "soft_constraints": ["избегать влажности", "минимум ветра"],
         "hotel_requirements": ["кондиционер"], "hard_constraints": ["кондиционер"]},
        ("every distinct weather dimension", "Desired low wind is soft"),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,patch,prompt_terms", REPAIR_ITERATION_CASES)
async def test_second_iteration_prompt_contract_and_preservation_of_correct_synthetic_extraction(tmp_path, text, patch, prompt_terms):
    """Prompt coverage is not a claim of natural-language model accuracy.

    Correct structured responses are synthetic; live targeted rechecks remain a
    separate evaluation after deploying the second prompt iteration.
    """
    store = Store(str(tmp_path / "intent.db"))
    session = travel_session(text)
    captured = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network call"))) as http:
        client = LLMClient(http, store)

        async def fake_completion(messages, session_id):
            captured.append(messages)
            return json.dumps({"state_patch": patch}, ensure_ascii=False)

        client.completion = fake_completion
        _, intent, _ = await client.extract(session)
    assert len(captured) == 1
    for field, value in patch.items():
        assert getattr(intent, field) == value
    for term in prompt_terms:
        assert term.casefold() in captured[0][0]["content"].casefold()
    assert intent.date_from.isoformat() == "2027-06-10"
    assert intent.date_to.isoformat() == "2027-06-18"
    store.close()


@pytest.mark.asyncio
async def test_ambiguous_extra_allocation_can_ask_without_forcing_either_budget_interpretation(tmp_path):
    store = Store(str(tmp_path / "intent.db"))
    session = travel_session("2000 EUR плюс максимум 150 EUR отдельно на собаку", budget_total=None)
    question = "150 EUR на собаку входят в общий потолок 2000 EUR или добавляются сверх него?"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network call"))) as http:
        client = LLMClient(http, store)

        async def fake_completion(messages, session_id):
            return json.dumps({"state_patch": {"hard_constraints": ["основной лимит 2000 EUR", "на собаку не более 150 EUR"]},
                               "blocking_question": question}, ensure_ascii=False)

        client.completion = fake_completion
        extraction, intent, _ = await client.extract(session)
    assert intent.budget_total is None
    assert extraction.blocking_question == question
    assert len(intent.hard_constraints) == 2
    store.close()


THIRD_ITERATION_CONTRACTS = [
    (
        "Хочу в Прагу в музеи, без пляжного отдыха",
        {"trip_styles": ["culture"], "soft_constraints": ["посещение музеев", "пляжный отдых не нужен"]},
        ("additional summaries, never substitutes", "culture does not replace museums"),
    ),
    (
        "Хочу наблюдать зимородков на рассвете, природу тоже люблю",
        {"trip_styles": ["nature"], "soft_constraints": ["наблюдать зимородков на рассвете"]},
        ("nature does not replace birdwatching or a named species", "user's original hard/soft strength"),
    ),
    (
        "Вода желательно от 22 градусов, прогноз по прошлогодней статистике не обещайте",
        {"climate_preferences": ["warm"], "soft_constraints": ["вода желательно от 22 градусов"],
         "hard_constraints": ["исторические наблюдения не выдавать за прогноз или гарантию будущей погоды"]},
        ("Distinguish uncertainty disclosure from a source prohibition", "without inventing a ban"),
    ),
    (
        "Прошлогодние наблюдения не использовать вообще, только будущий прогноз когда он появится",
        {"hard_constraints": ["не использовать исторические погодные данные"],
         "soft_constraints": ["прогноз будущей погоды после появления в доступном горизонте"]},
        ("Only an explicit prohibition on using historical data", "source prohibition"),
    ),
    (
        "Один взрослый и младенец 0 лет без места, учитывать все его сборы",
        {"adults": 1, "children_ages": [0], "hard_constraints": [
            "transport_requirement:младенец без отдельного места",
            "transport_requirement:учитывать все сборы за младенца"]},
        ("Infant seat allocation and infant charges are distinct required transport conditions",
         "encode each separately with the transport_requirement: prefix in hard_constraints"),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,patch,prompt_contract", THIRD_ITERATION_CONTRACTS)
async def test_third_iteration_prompt_contract_and_unmodified_synthetic_scope(tmp_path, text, patch, prompt_contract):
    """Test prompt obligations and preservation of synthetic outputs, not model skill.

    The subsequent live recheck supplies evidence about actual interpretation. No
    phrase- or case-specific parser is added to make these synthetic cases pass.
    """
    store = Store(str(tmp_path / "intent.db"))
    session = travel_session(text)
    prompts = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network call"))) as http:
        client = LLMClient(http, store)

        async def fake_completion(messages, session_id):
            prompts.append(messages[0]["content"])
            return json.dumps({"state_patch": patch}, ensure_ascii=False)

        client.completion = fake_completion
        _, intent, _ = await client.extract(session)
    assert len(prompts) == 1
    for term in prompt_contract:
        assert term in prompts[0]
    for field, value in patch.items():
        assert getattr(intent, field) == value
    assert intent.origin == "Варшава"
    assert intent.destination == "Ларнака"
    assert intent.date_from.isoformat() == "2027-06-10"
    assert intent.budget_total == 2400
    store.close()


@pytest.mark.parametrize("text", [
    "Снимите ограничение длительности перелёта. Время в пути не важно, прямой рейс всё ещё предпочитаем.",
    "Длительность перелёта теперь неважна.",
    "Не важно, сколько лететь.",
    "Продолжительность дороги не имеет значения.",
    "Travel time does not matter; I still prefer a direct flight.",
    "Flight duration is unimportant.",
    "I don't care about journey duration.",
])
def test_explicit_time_indifference_removes_both_caps_and_short_flight_without_touching_other_intent(text):
    session = travel_session(text, max_travel_minutes=180, direct_flight_preferred=True,
                             soft_constraints=["short_flight", "тихий номер"], pets=["dog"])
    # The synthetic model failed to clear its old qualitative preference.
    extraction = IntentExtraction(state_patch={"max_travel_minutes": None,
                                             "soft_constraints": ["short_flight", "тихий номер"]})
    guarded = guard_intent_patch(extraction, session)
    intent, _ = merge_intent(session.intent, guarded.state_patch)
    assert intent.max_travel_minutes is None
    assert intent.soft_constraints == ["тихий номер"]
    prior, after = session.intent.model_dump(), intent.model_dump()
    for field in prior.keys() - {"max_travel_minutes", "soft_constraints"}:
        assert after[field] == prior[field]
    assert intent.direct_flight_preferred is True


def test_explicit_indifference_also_corrects_an_omitted_patch_without_inference():
    session = travel_session("Время в пути не важно", max_travel_minutes=180,
                             soft_constraints=["недолгий перелёт", "short_flight", "у моря"])
    guarded = guard_intent_patch(IntentExtraction(state_patch={}), session)
    assert guarded.state_patch == {"max_travel_minutes": None, "soft_constraints": ["у моря"]}


@pytest.mark.parametrize("text", [
    "Снимите ограничение длительности перелёта.",
    "Уберите максимальную длительность перелёта, но короткий перелёт всё ещё желателен.",
    "Remove the flight duration cap; shorter flights remain preferable.",
])
def test_removing_only_hard_cap_preserves_short_flight_and_independent_direct_preference(text):
    session = travel_session(text, max_travel_minutes=180, direct_flight_preferred=True,
                             soft_constraints=["short_flight", "тихий номер"])
    guarded = guard_intent_patch(IntentExtraction(state_patch={"max_travel_minutes": None}), session)
    intent, _ = merge_intent(session.intent, guarded.state_patch)
    assert intent.max_travel_minutes is None
    assert intent.soft_constraints == ["short_flight", "тихий номер"]
    assert intent.direct_flight_preferred is True


@pytest.mark.parametrize("quoted", [
    'Источник: «Время в пути не важно».',
    'Source: "Travel time does not matter".',
    "Цитата: ‘Flight duration is unimportant’.",
    "Источник:\n> Время в пути не важно",
    "Источник:\n```text\nСнимите ограничение длительности перелёта. Время в пути не важно\n```",
    "В выдаче написано `Время в пути не важно`.",
])
def test_quoted_source_indifference_cannot_clear_real_duration_constraints(quoted):
    session = travel_session(quoted, max_travel_minutes=180, direct_flight_preferred=True,
                             soft_constraints=["short_flight", "тихий номер"])
    guarded = guard_intent_patch(IntentExtraction(state_patch={"max_travel_minutes": None}), session)
    intent, _ = merge_intent(session.intent, guarded.state_patch)
    assert intent.max_travel_minutes == 180
    assert intent.soft_constraints == ["short_flight", "тихий номер"]
    assert intent.direct_flight_preferred is True


def test_indifference_to_checkin_time_cannot_remove_travel_duration_cap():
    session = travel_session("Время заселения не важно", max_travel_minutes=180,
                             soft_constraints=["short_flight"])
    guarded = guard_intent_patch(IntentExtraction(state_patch={"max_travel_minutes": None}), session)
    intent, _ = merge_intent(session.intent, guarded.state_patch)
    assert intent.max_travel_minutes == 180
    assert intent.soft_constraints == ["short_flight"]
