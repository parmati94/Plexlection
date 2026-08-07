"""The fact registry.

Collects specs from every registered provider, enforces global key uniqueness,
and serves the JSON the rule builder is generated from.

Uniqueness matters beyond hygiene: exactly one provider owns each key, which is
what makes "purge everything this provider produced" a safe operation.
"""
from backend.common.errors import RuleError
from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier, FactSpec, FactType
from backend.rules.operators import aggregates_for, operators_for

logger = get_logger(__name__)


class FactRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, FactSpec] = {}
        self._by_provider: dict[str, list[FactSpec]] = {}

    def register(self, provider) -> None:
        for spec in provider.facts:
            if spec.key in self._specs:
                owner = self._specs[spec.key].provider
                raise ValueError(
                    f"Fact key {spec.key!r} is declared by both {owner!r} and "
                    f"{provider.id!r}. Keys must have exactly one owner."
                )
            # FactSpec is frozen with slots, so stamp ownership by rebuilding.
            stamped = _restamp(spec, provider.id, provider.default_applies_to)
            self._specs[stamped.key] = stamped
            self._by_provider.setdefault(provider.id, []).append(stamped)

    # ── lookup ────────────────────────────────────────────────────────────
    def get(self, key: str) -> FactSpec | None:
        return self._specs.get(key)

    def require(self, key: str) -> FactSpec:
        spec = self._specs.get(key)
        if spec is None:
            raise RuleError(f"Unknown fact key: {key!r}")
        return spec

    def all(self) -> list[FactSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.group, s.key))

    def indexed_specs(self) -> list[FactSpec]:
        return [s for s in self._specs.values() if s.indexed]

    def for_provider(self, provider_id: str) -> list[FactSpec]:
        return self._by_provider.get(provider_id, [])

    def groups(self) -> list[str]:
        seen: list[str] = []
        for spec in self.all():
            if spec.group not in seen:
                seen.append(spec.group)
        return seen

    # ── wire format ───────────────────────────────────────────────────────
    def to_wire(self, coverage: dict[str, dict] | None = None) -> list[dict]:
        """The payload the rule builder renders itself from.

        Operators, value editors and help text all come from here, so a new
        provider changes the UI without the UI changing.
        """
        coverage = coverage or {}
        out = []
        for spec in self.all():
            out.append({
                "key": spec.key,
                "label": spec.label,
                "type": spec.type.value,
                "group": spec.group,
                "description": spec.description,
                "provider": spec.provider,
                "unit": spec.unit,
                "format": spec.format,
                "enum_values": list(spec.enum_values) if spec.enum_values else None,
                "element_type": spec.element_type.value if spec.element_type else None,
                "indexed": spec.indexed,
                "aggregatable": spec.aggregatable,
                "applies_to": list(spec.applies_to),
                "example": spec.example,
                "operators": operators_for(spec),
                "aggregates": aggregates_for(spec),
                "coverage": coverage.get(spec.provider, {}),
            })
        return out


def _restamp(spec: FactSpec, provider_id: str, default_applies_to: tuple) -> FactSpec:
    """FactSpec is frozen and uses slots, so build a new one with the owner set.

    An empty applies_to inherits the provider's scope.
    """
    return FactSpec(
        key=spec.key, label=spec.label, type=spec.type, description=spec.description,
        group=spec.group, unit=spec.unit, format=spec.format,
        enum_values=spec.enum_values, element_type=spec.element_type,
        indexed=spec.indexed, aggregatable=spec.aggregatable, example=spec.example,
        provider=provider_id,
        applies_to=spec.applies_to or default_applies_to,
    )


def build_registry(providers) -> FactRegistry:
    registry = FactRegistry()
    for provider in providers:
        registry.register(provider)
    logger.info(
        "📖 Fact registry: %d keys from %d providers",
        len(registry.all()), len(providers),
    )
    return registry
