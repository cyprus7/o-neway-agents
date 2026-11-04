"""Optional source-reported event discovery; no runtime LLM or pricing dependency."""
from .domain import EventResult, EventSource, RegionalEvent, RegionalEventResult


def __getattr__(name):
    if name == "RegionalEventDiscovery":
        from .discovery import RegionalEventDiscovery
        return RegionalEventDiscovery
    raise AttributeError(name)

__all__ = ["EventResult", "EventSource", "RegionalEvent", "RegionalEventResult", "RegionalEventDiscovery"]
