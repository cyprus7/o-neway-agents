"""Safe application errors shared by adapters and command orchestration."""


class PlannerUnavailable(Exception):
    """Public-safe failure; never includes credentials or raw upstream content."""


class BusySession(Exception):
    def __init__(self, job_id=None):
        super().__init__("Session already has an active job")
        self.job_id = job_id


class ApplicationError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **details):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **details}
