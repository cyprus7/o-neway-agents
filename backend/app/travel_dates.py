"""Bounded departure choices; hotel dates never move when a flight date changes."""
from datetime import date, timedelta


def departure_dates(intent) -> tuple[date, ...]:
    start = getattr(intent, "flight_departure_from", None)
    end = getattr(intent, "flight_departure_to", None)
    if start is None or end is None:
        return (intent.date_from,) if intent.date_from else ()
    return tuple(end - timedelta(days=offset) for offset in range((end - start).days + 1))


def flight_departure_date(intent, observed=None):
    dates = departure_dates(intent)
    if isinstance(observed, str):
        try:
            observed = date.fromisoformat(observed)
        except ValueError:
            observed = None
    return observed if observed in dates else (dates[0] if dates else None)


def arrival_date_required(intent):
    return intent.date_from if getattr(intent, "flight_departure_from", None) is not None else None


def selected_departure_date(intent, component):
    """Use a permitted stored flight date, otherwise the latest allowed date.

    The observed route has priority over a quote scope. Neither may enlarge the
    current window; an out-of-window route may only fall through to a still-valid
    quote date, never authorize retention of its former context hash.
    """
    route = component.details.get("flight_route")
    observed_route = route.get("date_from") if isinstance(route, dict) else None
    quote = component.quote
    observed_quote = quote.scope.date_from if quote and quote.scope else None
    allowed = departure_dates(intent)
    for observed in (observed_route, observed_quote):
        if isinstance(observed, str):
            try:
                observed = date.fromisoformat(observed)
            except ValueError:
                continue
        if observed in allowed:
            return flight_departure_date(intent, observed)
    return flight_departure_date(intent)
