"""Rule CRUD models.

The rule *tree* itself is validated by backend/rules/validate.py rather than by
pydantic. A recursive discriminated union is expressible in pydantic v2, but the
errors it produces ("no match for any member of the union at $.root.children[2]")
are far worse than a hand-written validator can give, and this is a structure the
user edits directly in a builder — good messages matter more than terseness here.
"""
from typing import Any, Literal

from pydantic import BaseModel, Field


class RuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    rule: dict[str, Any]                       # {"version": 1, "root": {...}}
    library_keys: list[str] = Field(default_factory=list)
    item_types: list[str] = Field(default_factory=lambda: ["movie"])
    order_by_key: str | None = None
    order_dir: Literal["asc", "desc"] = "desc"
    limit_n: int | None = Field(default=None, ge=1, le=10000)
    enabled: bool = True
    sync_mode: Literal["label", "static", "none"] = "label"
    collection_title: str | None = None
    collection_sort_title: str | None = None
    collection_summary: str | None = None


class RuleCreate(RuleBase):
    # Derived from the name when absent. Becomes the label suffix, so it has to
    # be stable and URL/label-safe.
    slug: str | None = None


class RuleUpdate(BaseModel):
    """Every field optional — the UI saves partial edits."""
    name: str | None = None
    description: str | None = None
    rule: dict[str, Any] | None = None
    library_keys: list[str] | None = None
    item_types: list[str] | None = None
    order_by_key: str | None = None
    order_dir: Literal["asc", "desc"] | None = None
    limit_n: int | None = Field(default=None, ge=1, le=10000)
    enabled: bool | None = None
    sync_mode: Literal["label", "static", "none"] | None = None
    collection_title: str | None = None
    collection_sort_title: str | None = None
    collection_summary: str | None = None


class RulePreview(BaseModel):
    """Preview an unsaved tree.

    Sent on every edit in the builder, so it has to be cheap — which it is, being
    one compiled SQL query.
    """
    rule: dict[str, Any]
    library_keys: list[str] = Field(default_factory=list)
    item_types: list[str] = Field(default_factory=lambda: ["movie"])
    order_by_key: str | None = None
    order_dir: Literal["asc", "desc"] = "desc"
    limit_n: int | None = Field(default=None, ge=1, le=10000)
    sample_size: int = Field(default=20, ge=0, le=100)
