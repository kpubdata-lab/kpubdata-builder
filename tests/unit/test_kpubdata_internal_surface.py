"""Pin kpubdata **internal** symbols that ``verify/runner.py`` depends on.

builder's verify re-implements kpubdata's ``make verify`` and directly imports non-public
things — executor functions, spec model, transport, config. These
are not in ``kpubdata.__all__`` so name/location changes in minor releases are
not breaking changes on kpubdata side.

Removing that boundary is a cross-repo design decision not made here. Instead,
**we front-load when it breaks** — catch ImportError in CI with named symbols
instead of seeing it during verify execution after upgrade.

Add symbols to this list when additionally used. Dependencies not in this list
are not protected by this test.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib

import pytest

_RUNNER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "kpubdata_builder"
    / "verify"
    / "runner.py"
)

#: (module, name, expected signature or None)
_PINNED: tuple[tuple[str, str, str | None], ...] = (
    ("kpubdata.core.executor", "SpecExecutor", None),
    (
        "kpubdata.core.executor",
        "check_payload_error",
        "(spec: 'SpecDefinition', payload: 'dict[str, object]') -> 'None'",
    ),
    (
        "kpubdata.core.executor",
        "extract_items",
        "(spec: 'SpecDefinition', payload: 'dict[str, object]') -> 'list[dict[str, object]]'",
    ),
    (
        "kpubdata.core.executor",
        "extract_total_count",
        "(spec: 'SpecDefinition', payload: 'dict[str, object]') -> 'int | None'",
    ),
    ("kpubdata.core.spec", "SpecDefinition", None),
    ("kpubdata.core.spec", "ExampleSpec", None),
    ("kpubdata.transport.http", "HttpTransport", None),
    ("kpubdata.config", "KPubDataConfig", None),
)


class TestThePinnedSymbolsStillExist:
    @pytest.mark.parametrize(("module_name", "symbol", "signature"), _PINNED)
    def test_symbol_is_importable(
        self, module_name: str, symbol: str, signature: str | None
    ) -> None:
        module = importlib.import_module(module_name)

        assert hasattr(module, symbol), (
            f"{module_name}.{symbol} 이 사라졌다. builder 의 verify 가 이걸 직접 쓴다 — "
            "kpubdata 쪽에서는 공개 API 가 아니므로 파괴적 변경이 아니다."
        )

    @pytest.mark.parametrize(("module_name", "symbol", "signature"), _PINNED)
    def test_signature_is_unchanged(
        self, module_name: str, symbol: str, signature: str | None
    ) -> None:
        if signature is None:
            pytest.skip("클래스는 시그니처를 고정하지 않는다 — 존재만 확인한다")
        obj = getattr(importlib.import_module(module_name), symbol)

        assert str(inspect.signature(obj)) == signature


class TestTheListMatchesWhatTheRunnerActuallyImports:
    """If list grows stale, protected scope of this test silently shrinks."""

    def _imported_internals(self) -> set[tuple[str, str]]:
        tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
        found: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if not node.module.startswith("kpubdata."):
                continue
            # public exceptions/models in kpubdata.__all__ are not pinning targets.
            if node.module == "kpubdata.exceptions" or node.module == "kpubdata.core.models":
                continue
            for alias in node.names:
                found.add((node.module, alias.name))
        return found

    def test_every_internal_import_is_pinned(self) -> None:
        pinned = {(module, symbol) for module, symbol, _ in _PINNED}

        unpinned = self._imported_internals() - pinned

        assert not unpinned, (
            f"runner.py 가 고정되지 않은 kpubdata 내부 심볼을 쓴다: {sorted(unpinned)}. "
            "_PINNED 에 추가하라 — 그러지 않으면 업그레이드가 CI 가 아니라 실행 중에 깨진다."
        )
