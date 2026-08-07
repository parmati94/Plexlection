"""The fact provider interface.

Async-only at the interface. Providers whose libraries are blocking (plexapi,
requests) wrap their calls in `asyncio.to_thread` internally — four extra
characters, and it keeps one execution model in the scan engine.

`enrich` is an async *generator* rather than returning a list, which buys three
things: results persist incrementally so a cancel or crash keeps completed work,
progress is just "count the yields", and a provider can fan out internally and
stream results out of order.
"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, ClassVar, NamedTuple

from backend.facts.spec import CostTier, FactSpec

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"


class Eligibility(NamedTuple):
    ok: bool
    reason: str | None = None

    @staticmethod
    def yes() -> "Eligibility":
        return Eligibility(True)

    @staticmethod
    def skip(reason: str) -> "Eligibility":
        return Eligibility(False, reason)


@dataclass
class ItemRow:
    """The subset of an item a provider is allowed to see."""
    id: int
    rating_key: str
    library_key: str
    item_type: str
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    guid: str | None = None
    plex_path: str | None = None
    local_path: str | None = None
    path_status: str = "unknown"
    file_size: int | None = None
    file_mtime: int | None = None
    file_fp: str | None = None
    plex_added_at: int | None = None
    plex_updated_at: int | None = None
    plex_duration_ms: int | None = None
    facts: dict = field(default_factory=dict)


@dataclass
class FactResult:
    item_id: int
    status: str = STATUS_OK
    # Flat dotted keys -> values. The engine nests them before json_patch.
    facts: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    input_fp: str | None = None
    duration_ms: int = 0


@dataclass
class EnrichContext:
    settings: Any
    cancel: asyncio.Event
    semaphore: asyncio.Semaphore
    # Report the item currently being worked on, for the progress display.
    progress: Any = None

    def cancelled(self) -> bool:
        return self.cancel.is_set()


class FactProvider(ABC):
    # ── declaration ───────────────────────────────────────────────────────
    id: ClassVar[str]
    label: ClassVar[str]
    cost: ClassVar[CostTier]
    # Bump when the provider's output changes meaning. Every stored provenance
    # row for this provider becomes stale immediately.
    schema_version: ClassVar[int] = 1
    facts: ClassVar[tuple[FactSpec, ...]] = ()
    depends_on: ClassVar[tuple[str, ...]] = ()
    # 1 = per item (isolates failures, best resumability).
    # 0 = hand me every eligible item at once (Tautulli's history pager).
    batch_size: ClassVar[int] = 1
    # None = immutable until the fingerprint changes. Set for time-varying data.
    max_age_s: ClassVar[int | None] = None
    default_concurrency: ClassVar[int] = 4

    def __init__(self, settings) -> None:
        self.settings = settings

    # ── configuration ─────────────────────────────────────────────────────
    def is_configured(self) -> bool:
        return True

    def not_configured_reason(self) -> str:
        return "not configured"

    # ── item selection ────────────────────────────────────────────────────
    def selector(self) -> tuple[str, list]:
        """Coarse SQL prefilter, so planning never loads irrelevant rows."""
        return "1=1", []

    def can_enrich(self, item: ItemRow) -> Eligibility:
        """Fine per-item check. A skip is recorded with its reason rather than
        retried every scan."""
        return Eligibility.yes()

    def fingerprint(self, item: ItemRow) -> str | None:
        """Identity of *this provider's* inputs for `item`.

        None means uncacheable — freshness then depends solely on max_age_s.
        """
        return None

    # ── the work ──────────────────────────────────────────────────────────
    @abstractmethod
    def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        """Yield one FactResult per item, as each completes."""
        raise NotImplementedError

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(spec.key for spec in self.facts)

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "cost": self.cost.value,
            "schema_version": self.schema_version,
            "depends_on": list(self.depends_on),
            "fact_count": len(self.facts),
            "configured": self.is_configured(),
            "reason": None if self.is_configured() else self.not_configured_reason(),
        }
