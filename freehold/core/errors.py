class FreeholdError(Exception):
    pass


class BackendUnavailable(FreeholdError):
    """Transient: offline, rate limited, auth expired. Engine degrades."""


class ChunkNotFound(FreeholdError):
    """Permanent for this location: the blob is gone."""


class QuotaExceeded(BackendUnavailable):
    """Backend is out of room or bandwidth. Engine stops writing to it."""


class IntegrityError(FreeholdError):
    pass


class ConfigError(FreeholdError):
    pass
