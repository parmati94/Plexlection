"""Domain exceptions.

Each maps to a specific HTTP status in the routers, so callers can distinguish
"you asked for something invalid" from "I refused on safety grounds".
"""


class PlexlectionError(Exception):
    """Base for all domain errors."""


class RuleError(PlexlectionError):
    """A rule tree is malformed, references an unknown fact, or uses an
    operator that isn't valid for its fact type. -> 400"""


class SyncGuardError(PlexlectionError):
    """A sync was refused by a safety guard (too many removals, stale facts,
    empty match set). Carries the diff so the UI can offer an override. -> 409"""

    def __init__(self, message: str, diff=None):
        super().__init__(message)
        self.diff = diff


class ScanBusyError(PlexlectionError):
    """A scan is already running. Carries the active run so the UI can say
    which one and offer to cancel it. -> 409"""

    def __init__(self, message: str, run=None):
        super().__init__(message)
        self.run = run


class ProviderError(PlexlectionError):
    """A fact provider failed for an item. Recorded in fact_provenance rather
    than aborting the scan."""


class NotConfiguredError(PlexlectionError):
    """A required service (Plex, TMDB, Tautulli) has no credentials. -> 503"""
