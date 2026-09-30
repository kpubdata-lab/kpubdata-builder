"""Redistribution gate for the legacy publish path (#688, owner decision D2).

ADR 0018 keeps two publish paths while configs move to BuildSpec one at a time, and every
dataset actually published still goes through this one — so the gate must be here too,
not only on the BuildSpec path. It reads the terms a config states for its source
(``card.license`` / ``card.license_name``, the values ``check_config_licence.py`` reads)
and answers one of:

- ``allowed`` — the source's terms allow redistributing a transformed dataset.
- ``non_commercial`` — only for non-commercial use (KOGL type 2). Published only as a
  private Kaggle dataset, and only when the operator confirms it explicitly.
- ``forbidden`` — the terms forbid it. KOGL types 3 and 4 forbid derivative works, and
  this pipeline publishes transformed data.
- ``unknown`` — the config states no recognised terms. **Not permission**: blocked.

The gate goes away with the legacy code when the last config has moved (D2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Verdict = Literal["allowed", "non_commercial", "forbidden", "unknown"]

#: Source terms by ``license_name``, as the configs record them.
_TERMS: dict[str, tuple[Verdict, str]] = {
    "korea-public-data-unrestricted": ("allowed", "public data without restrictions"),
    "kogl-type-1": ("allowed", "KOGL type 1: attribution"),
    "kogl-type-2": ("non_commercial", "KOGL type 2: attribution, no commercial use"),
    "kogl-type-3": (
        "forbidden",
        "KOGL type 3: no derivative works — this publishes transformed data",
    ),
    "kogl-type-4": ("forbidden", "KOGL type 4: no commercial use and no derivative works"),
}


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    reason: str


def redistribution_verdict(config: dict[str, Any]) -> GateDecision:
    """What the config's stated terms allow."""
    card = config.get("card") or {}
    licence = card.get("license")
    name = card.get("license_name")
    if licence != "other" or not isinstance(name, str):
        return GateDecision(
            "unknown",
            f"the config states no recognised source terms (license={licence!r}); "
            "record them as license: other with license_name",
        )
    verdict, reason = _TERMS.get(name, ("unknown", f"license_name {name!r} is not a recorded term"))
    return GateDecision(verdict, reason)


def publish_refusal(
    config: dict[str, Any],
    *,
    targets: tuple[str, ...],
    kaggle_public: bool,
    confirm_non_commercial: bool,
) -> str | None:
    """Why publishing ``config`` to ``targets`` is refused, or None when it may go ahead.

    The Hugging Face upload is always public, so only ``allowed`` terms reach it. A
    ``non_commercial`` config may go only to a private Kaggle dataset, with confirmation.
    """
    decision = redistribution_verdict(config)
    if decision.verdict == "allowed":
        return None
    if decision.verdict == "non_commercial":
        if "hf" in targets:
            return f"{decision.reason}; the Hugging Face upload is public, so it is refused"
        if kaggle_public:
            return f"{decision.reason}; publish it privately (without --public)"
        if not confirm_non_commercial:
            return f"{decision.reason}; pass --confirm-non-commercial to publish it privately"
        return None
    return f"redistribution is {decision.verdict}: {decision.reason}"


__all__ = ["GateDecision", "Verdict", "publish_refusal", "redistribution_verdict"]
