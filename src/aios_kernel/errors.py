class AiosError(Exception):
    status = 400


class NotFound(AiosError):
    status = 404


class PermissionDenied(AiosError):
    status = 403


class Unauthenticated(AiosError):
    status = 401


class Conflict(AiosError):
    status = 409


class BudgetExceeded(AiosError):
    status = 402


class Halted(AiosError):
    """Raised inside a run when the emergency stop or a namespace pause is active."""
    status = 423
