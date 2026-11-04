"""Persistent command vocabulary and cancellation fence shared by one worker.

Public canonical `succeeded` is represented by the compatible Job `completed`.
"""
from contextvars import ContextVar
from functools import wraps

active_command_id: ContextVar[str | None] = ContextVar("active_command_id", default=None)
price_research_budget: ContextVar[list[int] | None] = ContextVar("price_research_budget", default=None)


def bounded_price_research(function):
    """One optional extraction batch across all parallel candidates of a command."""
    @wraps(function)
    async def bounded(*args, **kwargs):
        token = price_research_budget.set([1])
        try:
            return await function(*args, **kwargs)
        finally:
            price_research_budget.reset(token)
    return bounded
TERMINAL_COMMAND_STATES = frozenset({"completed", "failed", "superseded", "canceled", "interrupted"})


class CommandError(ValueError):
    def __init__(self, code: str, status_code: int, *, retry_after: int | None = None):
        super().__init__(code)
        self.code, self.status_code, self.retry_after = code, status_code, retry_after


class StaleCommand(RuntimeError):
    """A canceled/interrupted/deleted command cannot publish late state."""
