"""A small attributed destination catalogue, augmented by real geocoding.

Tags describe broad destination character, not live weather, inventory or visas.
"""
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Literal


@dataclass(frozen=True)
class Place:
    name: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    airport: str | None
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    wiki: str
    subtitle: str
    source_provider: str = "Wikivoyage / кураторский географический справочник"
    retrieved_at: str = "2026-10-23T00:00:00+00:00"
    freshness: str = "snapshot"
    scope: Literal["locality", "country"] = "locality"


PLACES = [
    Place("Ларнака", "Кипр", "CY", 34.92, 33.63, "LCA", ("beach", "family", "culture", "mild"), ("larnaca", "larnaka", "ларнака", "λάρνακα", "λαρνακα", "кипр", "cyprus"), "https://en.wikivoyage.org/wiki/Larnaca", "Море, прогулки и спокойный городской ритм"),
    Place("Пафос", "Кипр", "CY", 34.77, 32.42, "PFO", ("beach", "family", "culture", "nature", "mild"), ("paphos", "pafos", "пафос", "πάφος", "παφος", "кипр", "cyprus"), "https://en.wikivoyage.org/wiki/Paphos", "Античные места и прибрежные тропы"),
    Place("Лимасол", "Кипр", "CY", 34.68, 33.04, None, ("beach", "city", "culture", "mild"), ("limassol", "lemesos", "лимасол", "лимассол", "лимасола", "λεμεσός", "λεμεσος"), "https://en.wikivoyage.org/wiki/Limassol", "Городские прогулки и побережье"),
    Place("Никосия", "Кипр", "CY", 35.17, 33.36, None, ("city", "culture", "mild"), ("nicosia", "lefkosia", "никосия", "левкосия", "λευκωσία", "λευκωσια"), "https://en.wikivoyage.org/wiki/Nicosia", "Исторические кварталы и городская культура"),
    Place("Айя-Напа", "Кипр", "CY", 34.99, 34.00, None, ("beach", "family", "mild"), ("ayia napa", "agia napa", "айя-напа", "айя напа", "агия напа", "αγία νάπα", "αγια ναπα"), "https://en.wikivoyage.org/wiki/Ayia_Napa", "Побережье и курортные прогулки"),
    Place("Протарас", "Кипр", "CY", 35.01, 34.05, None, ("beach", "family", "nature", "mild"), ("protaras", "протарас", "πρωταράς", "πρωταρας"), "https://en.wikivoyage.org/wiki/Protaras", "Пляжи и прибрежные маршруты"),
    Place("Полис", "Кипр", "CY", 35.03, 32.43, None, ("beach", "nature", "mild"), ("polis", "polis chrysochous", "полис", "πόλις", "πολις"), "https://en.wikivoyage.org/wiki/Polis", "Небольшой город и природные маршруты"),
    Place("Троодос", "Кипр", "CY", 34.92, 32.88, None, ("nature", "culture", "mild"), ("troodos", "троодос", "τρόοδος", "τροοδος"), "https://en.wikivoyage.org/wiki/Troodos", "Горные деревни и пешие маршруты"),
    Place("Анталья", "Турция", "TR", 36.90, 30.70, "AYT", ("beach", "family", "nature", "mild"), ("antalya", "анталья", "анталия", "турция", "turkey", "türkiye"), "https://en.wikivoyage.org/wiki/Antalya", "Старый город и побережье Средиземного моря"),
    Place("Тенерифе", "Испания", "ES", 28.29, -16.63, "TFS", ("beach", "nature", "family", "warm"), ("tenerife", "тенерифе", "канары", "canary islands"), "https://en.wikivoyage.org/wiki/Tenerife", "Океан и вулканические пейзажи"),
    Place("Хургада", "Египет", "EG", 27.26, 33.81, "HRG", ("beach", "family", "warm"), ("hurghada", "хургада", "египет", "egypt"), "https://en.wikivoyage.org/wiki/Hurghada", "Красное море и курортный отдых"),
    Place("Шарм-эш-Шейх", "Египет", "EG", 27.92, 34.33, "SSH", ("beach", "nature", "warm"), ("sharm el sheikh", "шарм", "египет", "egypt"), "https://en.wikivoyage.org/wiki/Sharm_el-Sheikh", "Рифы Красного моря и пустынные пейзажи"),
    Place("Дубай", "ОАЭ", "AE", 25.20, 55.27, "DXB", ("beach", "city", "family", "food", "warm"), ("dubai", "дубай", "оаэ", "uae"), "https://en.wikivoyage.org/wiki/Dubai", "Большой город и пляжи Персидского залива"),
    Place("Лиссабон", "Португалия", "PT", 38.72, -9.14, "LIS", ("city", "culture", "food", "mild"), ("lisbon", "lisboa", "лиссабон", "португалия", "portugal"), "https://en.wikivoyage.org/wiki/Lisbon", "Холмы, океанский воздух и городская культура"),
    Place("Малага", "Испания", "ES", 36.72, -4.42, "AGP", ("beach", "city", "culture", "food", "mild"), ("malaga", "малага", "испания", "spain"), "https://en.wikivoyage.org/wiki/M%C3%A1laga", "Андалусия, музеи и набережные"),
    Place("Ницца", "Франция", "FR", 43.70, 7.27, "NCE", ("beach", "city", "culture", "food", "mild"), ("nice", "ницца", "франция", "france"), "https://en.wikivoyage.org/wiki/Nice", "Лазурный берег и прогулки по старому городу"),
    Place("Рим", "Италия", "IT", 41.90, 12.50, "FCO", ("city", "culture", "food", "mild"), ("rome", "roma", "рим", "италия", "italy"), "https://en.wikivoyage.org/wiki/Rome", "История, архитектура и итальянская кухня"),
    Place("Стамбул", "Турция", "TR", 41.01, 28.98, "IST", ("city", "culture", "food", "mild"), ("istanbul", "стамбул", "турция", "turkey", "türkiye"), "https://en.wikivoyage.org/wiki/Istanbul", "Босфор, история и гастрономия"),
    Place("Тбилиси", "Грузия", "GE", 41.72, 44.79, "TBS", ("city", "culture", "food", "nature", "mild"), ("tbilisi", "тбилиси", "грузия", "georgia"), "https://en.wikivoyage.org/wiki/Tbilisi", "Старый город и поездки к горам"),
    Place("Будапешт", "Венгрия", "HU", 47.50, 19.04, "BUD", ("city", "culture", "wellness", "food", "cold"), ("budapest", "будапешт", "венгрия", "hungary"), "https://en.wikivoyage.org/wiki/Budapest", "Дунай, термальные купальни и архитектура"),
    Place("Прага", "Чехия", "CZ", 50.08, 14.44, "PRG", ("city", "culture", "food", "cold"), ("prague", "praha", "прага", "чехия", "czechia"), "https://en.wikivoyage.org/wiki/Prague", "Исторические кварталы и прогулки по городу"),
    Place("Вена", "Австрия", "AT", 48.21, 16.37, "VIE", ("city", "culture", "food", "cold"), ("vienna", "wien", "вена", "австрия", "austria"), "https://en.wikivoyage.org/wiki/Vienna", "Музеи, музыка и городские парки"),
    Place("Афины", "Греция", "GR", 37.98, 23.73, "ATH", ("city", "culture", "food", "mild"), ("athens", "афины", "греция", "greece"), "https://en.wikivoyage.org/wiki/Athens", "Античная история и средиземноморская кухня"),
    Place("Рейкьявик", "Исландия", "IS", 64.15, -21.94, "KEF", ("nature", "city", "wellness", "cold", "snow"), ("reykjavik", "рейкьявик", "исландия", "iceland"), "https://en.wikivoyage.org/wiki/Reykjavik", "Северные пейзажи и геотермальные источники"),
    Place("Барселона", "Испания", "ES", 41.39, 2.17, "BCN", ("beach", "city", "culture", "food", "mild"), ("barcelona", "барселона", "испания", "spain"), "https://en.wikivoyage.org/wiki/Barcelona", "Архитектура, городская жизнь и море"),
    Place("Мадейра", "Португалия", "PT", 32.65, -16.91, "FNC", ("nature", "wellness", "mild"), ("madeira", "мадейра", "фуншал", "funchal"), "https://en.wikivoyage.org/wiki/Madeira", "Океанский остров, горы и пешие маршруты"),
    Place("Банско", "Болгария", "BG", 41.84, 23.49, "SOF", ("ski", "nature", "family", "snow", "cold"), ("bansko", "банско", "болгария", "bulgaria"), "https://en.wikivoyage.org/wiki/Bansko", "Горы Пирин и зимний курорт"),
    Place("Любляна", "Словения", "SI", 46.06, 14.51, "LJU", ("nature", "city", "culture", "food", "cold"), ("ljubljana", "любляна", "словения", "slovenia"), "https://en.wikivoyage.org/wiki/Ljubljana", "Зелёный город и доступ к альпийским маршрутам"),
    Place("Хельсинки", "Финляндия", "FI", 60.17, 24.94, "HEL", ("city", "culture", "nature", "cold", "snow"), ("helsinki", "хельсинки", "финляндия", "finland"), "https://en.wikivoyage.org/wiki/Helsinki", "Балтийский берег, дизайн и острова"),
    Place("Бали", "Индонезия", "ID", -8.65, 115.22, "DPS", ("beach", "nature", "wellness", "warm"), ("bali", "бали", "индонезия", "indonesia"), "https://en.wikivoyage.org/wiki/Bali", "Тропический остров, побережье и природные маршруты"),
    Place("Бора-Бора", "Французская Полинезия", "PF", -16.4944, -151.7364, "BOB", ("beach", "nature", "warm"), ("bora bora", "bora-bora", "бора-бора", "бора бора", "боро-боро", "боро боро", "borabora"), "https://en.wikivoyage.org/wiki/Bora_Bora", "Остров и лагуна Французской Полинезии", "Wikivoyage; аэропорт BOB — Air Tahiti (airtahiti.com/en/bora-bora)", "2026-10-28T21:39:10+00:00"),
]

ORIGINS = PLACES + [
    Place("Казань", "Россия", "RU", 55.79, 49.12, "KZN", (), ("kazan", "казань", "казани", "kzn"), "https://sales.kazan.aero/", "", "Официальный сайт аэропорта Казань; округлённые координаты города", "2026-10-28T21:39:10+00:00"),
    Place("Папеэте", "Французская Полинезия", "PF", -17.54, -149.57, "PPT", (), ("papeete", "tahiti", "папеэте", "папеете", "таити", "ppt"), "https://www.airtahiti.com/en/bora-bora", "", "Air Tahiti; округлённые координаты Папеэте", "2026-10-28T21:39:10+00:00"),
    Place("Варшава", "Польша", "PL", 52.23, 21.01, "WAW", (), ("warsaw", "warszawa", "варшава", "варшавы", "waw"), "https://en.wikivoyage.org/wiki/Warsaw", ""),
    Place("Москва", "Россия", "RU", 55.75, 37.62, "MOW", (), ("moscow", "москва", "москвы", "mow"), "https://en.wikivoyage.org/wiki/Moscow", ""),
    Place("Санкт-Петербург", "Россия", "RU", 59.93, 30.34, "LED", (), ("saint petersburg", "санкт-петербург", "петербург", "спб", "led"), "https://en.wikivoyage.org/wiki/Saint_Petersburg", ""),
    Place("Берлин", "Германия", "DE", 52.52, 13.40, "BER", (), ("berlin", "берлин", "берлина", "ber"), "https://en.wikivoyage.org/wiki/Berlin", ""),
    Place("Лондон", "Великобритания", "GB", 51.51, -0.13, "LON", (), ("london", "лондон", "лондона", "lon"), "https://en.wikivoyage.org/wiki/London", ""),
    Place("Алматы", "Казахстан", "KZ", 43.24, 76.95, "ALA", (), ("almaty", "алматы", "алма-ата", "ala"), "https://en.wikivoyage.org/wiki/Almaty", ""),
]


def normalized(value: str) -> str:
    return value.casefold().replace("ё", "е").strip()


COUNTRY_QUALIFIERS = {
    "CY": ("cyprus",), "PL": ("poland", "polska"), "RU": ("russia", "russian federation"),
    "DE": ("germany", "deutschland"), "GB": ("united kingdom", "uk", "great britain"),
    "KZ": ("kazakhstan",), "TR": ("turkey", "türkiye"), "ES": ("spain",),
    "EG": ("egypt",), "AE": ("uae", "united arab emirates"), "PT": ("portugal",),
    "FR": ("france",), "IT": ("italy",), "GE": ("georgia",), "HU": ("hungary",),
    "CZ": ("czechia", "czech republic"), "AT": ("austria",), "BG": ("bulgaria",),
    "SI": ("slovenia",), "IS": ("iceland",), "FI": ("finland",), "ID": ("indonesia",),
    "GR": ("greece",), "MV": ("maldives",), "PF": ("french polynesia", "polynésie française"),
}


def find_place(value: str, places: list[Place] = ORIGINS) -> Place | None:
    value = normalized(value)
    if not value:
        return None
    if "," in value:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 2 or not all(parts):
            return None
        city, country = parts
        # Accept a model's explicit city/country format only when BOTH agree.
        # Never strip an unknown/conflicting qualifier or match a country alias as a city.
        for place in places:
            city_names = (place.name, place.airport or "", *place.aliases[:2])
            countries = (place.country, place.country_code, *COUNTRY_QUALIFIERS.get(place.country_code, ()))
            if city in map(normalized, city_names) and country in map(normalized, countries):
                return place
        return None
    # Prefer exact city/IATA match over country aliases.
    for place in places:
        if value in (normalized(place.name), normalized(place.airport or "")):
            return place
    for place in places:
        if value in tuple(normalized(alias) for alias in place.aliases):
            return place
    return None


def distance_km(origin: Place, destination: Place) -> float:
    lat1, lat2 = radians(origin.latitude), radians(destination.latitude)
    dlat = lat2 - lat1
    dlon = radians(destination.longitude - origin.longitude)
    return 6371 * 2 * asin(min(1, sqrt(sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2)))
