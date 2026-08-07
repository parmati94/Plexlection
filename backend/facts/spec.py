"""Fact specifications — the contract between providers and everything else.

A FactSpec is declared once by the provider that owns a key, and drives:
  * the operators offered for it in the rule builder,
  * the value editor rendered next to those operators,
  * whether an expression index is created for it,
  * whether it can be compared against a library aggregate,
  * the help text the user reads.

Key names are semantic (`video.dar`), not per-provider, so two providers can
both write into `video.*` if they own different keys. The registry enforces
global key uniqueness, which is what makes "purge everything this provider
owns" safe.
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Dotted lowercase. This regex is the allowlist that makes it safe to
# interpolate a key into a JSON path in generated SQL.
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class FactType(str, Enum):
    NUMBER = "number"
    STRING = "string"
    ENUM = "enum"
    BOOL = "bool"
    DATE = "date"
    LIST = "list"


class CostTier(str, Enum):
    FREE = "free"            # already in the database; no IO
    CHEAP = "cheap"          # local disk read, sub-second
    NETWORK = "network"      # external API, rate-limited
    EXPENSIVE = "expensive"  # decodes frames; seconds to minutes per item


@dataclass(frozen=True, slots=True)
class FactSpec:
    key: str
    label: str
    type: FactType
    description: str
    group: str = "Other"
    unit: str | None = None
    # Presentation hint for the UI: bytes | kbps | duration_s | ratio | date | percent
    format: str | None = None
    enum_values: tuple[str, ...] | None = None
    element_type: FactType | None = None   # LIST only
    indexed: bool = False
    aggregatable: bool = False             # eligible for percentile/median predicates
    example: Any = None
    provider: str = ""                     # stamped by the registry
    # Which item types this fact can ever be set on. Without it the rule builder
    # offers "Aspect ratio" on a rule targeting shows — a condition that can
    # never match, because shows have no file.
    #
    # Empty means "inherit from the provider", which is the usual case: a
    # provider's facts almost always share its scope, and stating it 23 times
    # in ffprobe would be noise.
    applies_to: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not KEY_RE.fullmatch(self.key):
            raise ValueError(
                f"Invalid fact key {self.key!r}: must be dotted lowercase, "
                f"e.g. 'video.dar'"
            )
        if self.type is FactType.ENUM and not self.enum_values:
            raise ValueError(f"{self.key}: ENUM facts must declare enum_values")
        if self.type is FactType.LIST and self.element_type is None:
            raise ValueError(f"{self.key}: LIST facts must declare element_type")
        if self.aggregatable and self.type not in (FactType.NUMBER, FactType.DATE):
            raise ValueError(f"{self.key}: only NUMBER and DATE facts can be aggregated")

    @property
    def namespace(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def json_path(self) -> str:
        return "$." + self.key
