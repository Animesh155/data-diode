"""Configurable filter chain for tag-level ingress/egress filtering.

Provides an abstract :class:`Filter` base and concrete implementations for
range checks, keyword blocking, and whitelist-based access control.  Filters
are composed into a :class:`FilterChain` that applies them in order; a tag
is rejected (returns ``None``) as soon as any filter denies it.
"""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from src.gateway.core.tag_manager import TagValue

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------------
# Abstract base
# ------------------------------------------------------------------


class Filter(ABC):
    """Base class for all tag filters.

    Subclasses implement :meth:`apply` which returns the tag (possibly
    transformed) or ``None`` to reject it.
    """

    def __init__(self, name: str | None = None) -> None:
        self._name = name or self.__class__.__name__
        self._accepted: int = 0
        self._rejected: int = 0

    @property
    def name(self) -> str:
        """Human-readable filter name."""
        return self._name

    @abstractmethod
    def apply(self, tag: TagValue) -> Optional[TagValue]:
        """Evaluate *tag* against this filter.

        Args:
            tag: Incoming tag snapshot.

        Returns:
            The (possibly mutated) tag on acceptance, or ``None`` on rejection.
        """

    @property
    def stats(self) -> dict[str, Any]:
        """Per-filter acceptance / rejection counters."""
        return {
            "name": self._name,
            "accepted_count": self._accepted,
            "rejected_count": self._rejected,
        }

    def _accept(self, tag: TagValue) -> TagValue:
        self._accepted += 1
        return tag

    def _reject(self, tag: TagValue, reason: str = "") -> None:
        self._rejected += 1
        logger.debug(
            "filter.rejected",
            filter=self._name,
            tag=tag.name,
            reason=reason,
        )


# ------------------------------------------------------------------
# Concrete filters
# ------------------------------------------------------------------


class RangeFilter(Filter):
    """Reject tags whose numeric value falls outside ``[min_val, max_val]``.

    Tag names are matched using :func:`fnmatch.fnmatch` against the
    configured *pattern*, so ``"PLC1.*"`` matches all tags under ``PLC1``.

    Args:
        pattern: Glob pattern applied to :attr:`TagValue.name`.
        min_val: Minimum allowed value (inclusive).
        max_val: Maximum allowed value (inclusive).
        name: Optional human-readable filter name.
    """

    def __init__(
        self,
        pattern: str,
        min_val: float,
        max_val: float,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name or f"RangeFilter({pattern})")
        self._pattern = pattern
        self._min = min_val
        self._max = max_val

    def apply(self, tag: TagValue) -> Optional[TagValue]:
        """Return *tag* if its value is within the configured range."""
        if not fnmatch.fnmatch(tag.name, self._pattern):
            # Pattern doesn't match — filter is not applicable; pass through.
            return self._accept(tag)

        try:
            numeric = float(tag.value)
        except (TypeError, ValueError):
            # Non-numeric value for a matched tag — reject.
            self._reject(tag, reason="non-numeric value for range check")
            return None

        if self._min <= numeric <= self._max:
            return self._accept(tag)

        self._reject(tag, reason=f"value {numeric} outside [{self._min}, {self._max}]")
        return None


class KeywordFilter(Filter):
    """Reject tags whose **string** value contains any blocked keyword.

    Comparison is case-insensitive.

    Args:
        blocked_keywords: Iterable of keywords to block.
        name: Optional human-readable filter name.
    """

    def __init__(
        self,
        blocked_keywords: list[str],
        name: str | None = None,
    ) -> None:
        super().__init__(name=name or "KeywordFilter")
        self._blocked = [kw.lower() for kw in blocked_keywords]

    def apply(self, tag: TagValue) -> Optional[TagValue]:
        """Return *tag* if its string value contains no blocked keywords."""
        if not isinstance(tag.value, str):
            return self._accept(tag)

        lower_val = tag.value.lower()
        for kw in self._blocked:
            if kw in lower_val:
                self._reject(tag, reason=f"blocked keyword '{kw}' found")
                return None
        return self._accept(tag)


class WhitelistFilter(Filter):
    """Only allow tags whose names match at least one of the given glob patterns.

    Args:
        patterns: Glob patterns (e.g. ``["PLC1.*", "Sensor.Temperature"]``).
        name: Optional human-readable filter name.
    """

    def __init__(
        self,
        patterns: list[str],
        name: str | None = None,
    ) -> None:
        super().__init__(name=name or "WhitelistFilter")
        self._patterns = list(patterns)

    def apply(self, tag: TagValue) -> Optional[TagValue]:
        """Return *tag* if its name matches any whitelisted pattern."""
        for pat in self._patterns:
            if fnmatch.fnmatch(tag.name, pat):
                return self._accept(tag)
        self._reject(tag, reason="not in whitelist")
        return None


# ------------------------------------------------------------------
# Filter chain
# ------------------------------------------------------------------


class FilterChain:
    """Ordered pipeline of :class:`Filter` instances.

    Tags pass through every filter in sequence.  The first ``None`` return
    short-circuits the chain and the tag is discarded.

    Example::

        chain = FilterChain([
            WhitelistFilter(["PLC1.*"]),
            RangeFilter("PLC1.Temp*", 0, 200),
        ])
        result = chain.apply(tag)
    """

    def __init__(self, filters: list[Filter] | None = None) -> None:
        self._filters: list[Filter] = list(filters) if filters else []
        logger.info(
            "filter_chain.init",
            filter_count=len(self._filters),
            filters=[f.name for f in self._filters],
        )

    def add(self, flt: Filter) -> None:
        """Append a filter to the chain.

        Args:
            flt: Filter instance to add.
        """
        self._filters.append(flt)

    def apply(self, tag: TagValue) -> Optional[TagValue]:
        """Run *tag* through the chain.

        Args:
            tag: Incoming tag snapshot.

        Returns:
            The surviving tag, or ``None`` if any filter rejected it.
        """
        current: Optional[TagValue] = tag
        for flt in self._filters:
            if current is None:
                break
            current = flt.apply(current)
        return current

    @property
    def stats(self) -> list[dict[str, Any]]:
        """Aggregate stats from every filter in the chain."""
        return [f.stats for f in self._filters]

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> FilterChain:
        """Build a :class:`FilterChain` from a configuration dictionary.

        Expected schema::

            {
                "filters": [
                    {
                        "type": "range",
                        "pattern": "PLC1.*",
                        "min": 0,
                        "max": 200
                    },
                    {
                        "type": "keyword",
                        "blocked": ["password", "secret"]
                    },
                    {
                        "type": "whitelist",
                        "patterns": ["PLC1.*", "Sensor.*"]
                    }
                ]
            }

        Args:
            config: Dictionary matching the schema above.

        Returns:
            Configured :class:`FilterChain`.

        Raises:
            ValueError: On unknown filter type.
        """
        filters: list[Filter] = []
        for entry in config.get("filters", []):
            ftype = entry.get("type", "").lower()
            if ftype == "range":
                filters.append(
                    RangeFilter(
                        pattern=entry["pattern"],
                        min_val=float(entry["min"]),
                        max_val=float(entry["max"]),
                        name=entry.get("name"),
                    )
                )
            elif ftype == "keyword":
                filters.append(
                    KeywordFilter(
                        blocked_keywords=entry["blocked"],
                        name=entry.get("name"),
                    )
                )
            elif ftype == "whitelist":
                filters.append(
                    WhitelistFilter(
                        patterns=entry["patterns"],
                        name=entry.get("name"),
                    )
                )
            else:
                raise ValueError(f"Unknown filter type: '{ftype}'")

        chain = cls(filters)
        logger.info("filter_chain.from_config", filter_count=len(filters))
        return chain
