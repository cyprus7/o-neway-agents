"""Geographic negatives and partial journey evidence must stay conservative."""
import pytest

from backend.app.geography import find_place
from backend.app.models import TravelComponent, TravelIntent
from backend.app.planner import constraint_checks, excluded


@pytest.mark.parametrize(("constraint", "destination"), [
    ("не хочу в Турцию", "Antalya"),
    ("исключить Турцию", "Antalya"),
    ("Турцию не рассматривать", "Antalya"),
    ("кроме Кипра", "Larnaca"),
    ("не рассматриваем отдых в Египте", "Hurghada"),
    ("avoid Turkey", "Antalya"),
    ("except for Cyprus", "Larnaca"),
    ("do not want to go to Turkey", "Antalya"),
    ("exclude Turkey and Egypt", "Hurghada"),
    ("не хочу в Турцию и в Египет", "Hurghada"),
    ("не хочу в Барселону", "Barcelona"),
    ("exclude Larnaca", "Larnaca"),
])
def test_explicit_geographic_negation_handles_cases_and_lists(constraint, destination):
    assert excluded(find_place(destination), TravelIntent(hard_constraints=[constraint]))


@pytest.mark.parametrize("constraint", [
    "No more than 3 hours to Turkey",
    "без пересадок в Turkey",
    "без пересадок в Турцию",
    "без глютена в Турции",
    "не хочу жару в Турции",
    "no adults only hotels in Turkey",
    "avoid night flights to Turkey",
    "not only Turkey",
    "exclude Cyprus",
    "не хочу в Египет, Турция подходит",
])
def test_negation_of_another_object_does_not_exclude_requested_country(constraint):
    assert not excluded(find_place("Antalya"), TravelIntent(hard_constraints=[constraint]))


def test_city_exclusion_does_not_exclude_every_city_in_the_country():
    intent = TravelIntent(hard_constraints=["не хочу в Ларнаку"])
    assert excluded(find_place("Larnaca"), intent)
    assert not excluded(find_place("Paphos"), intent)


def test_structured_exclusions_remain_exact():
    assert excluded(find_place("Antalya"), TravelIntent(hard_constraints=["exclude_country:TR"]))
    assert not excluded(find_place("Larnaca"), TravelIntent(hard_constraints=["exclude_country:TR"]))
    assert excluded(find_place("Larnaca"), TravelIntent(hard_constraints=["exclude_destination:Larnaca"]))
    assert not excluded(find_place("Paphos"), TravelIntent(hard_constraints=["exclude_destination:Larnaca"]))


@pytest.mark.parametrize(("duration", "status"), [
    (120, "unknown"),
    (150, "unknown"),
    (151, "failed"),
    (None, "unknown"),
    ("120", "unknown"),
    (-1, "unknown"),
    (True, "unknown"),
    (float("inf"), "unknown"),
    (float("nan"), "unknown"),
])
def test_partial_transport_duration_cannot_pass_complete_journey_limit(duration, status):
    intent = TravelIntent(max_travel_minutes=150)
    components = (
        TravelComponent(title="Transport", description="", details={"duration_minutes": duration}),
        TravelComponent(title="Stay", description=""),
        TravelComponent(title="Unknown ground segments", description=""),
    )
    checks = constraint_checks(intent, find_place("Larnaca"), components)
    assert next(check for check in checks if check["constraint"] == "Время в пути")["status"] == status


def test_unusual_hard_hotel_wishes_are_preserved_without_claiming_verification():
    requirements = ["без ароматизаторов", "розетка для CPAP у кровати", "душ без порога 90 см"]
    intent = TravelIntent(hotel_requirements=requirements, hard_constraints=["нельзя лестницы"])
    components = tuple(TravelComponent(title="Unknown", description="No source") for _ in range(3))
    checks = constraint_checks(intent, find_place("Larnaca"), components)
    for requirement in requirements:
        assert next(check for check in checks if check["constraint"] == "Проживание: " + requirement)["status"] == "unknown"
    assert next(check for check in checks if check["constraint"] == "нельзя лестницы")["status"] == "unknown"
