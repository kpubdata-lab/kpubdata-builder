"""The settings catalog is what the code reads and what the document says (#1108).

`docs/deployment.md` listed the environment variables in two hand-written tables that had
drifted apart, and eight variables the code reads were in neither. The tables are now
generated from one list. A list nobody checks drifts the same way, so this holds it on
both sides: to the code that reads the environment, and to the document.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from kpubdata_builder import settings_catalog as catalog

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "kpubdata_builder"
_ENTRYPOINT = _ROOT / "docker-entrypoint.sh"

#: A name that is all capitals, digits and underscores — what an environment variable
#: looks like.
_NAME = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")
#: Prefixes that belong to this product: any such literal in the source is a variable.
_OWN_PREFIXES = ("KPUBDATA_", "OIDC_")

#: Literals with an own prefix that are not environment variables.
_NOT_VARIABLES: frozenset[str] = frozenset()


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_generate_settings_doc", _ROOT / "scripts" / "generate_settings_doc.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_environ(node: ast.AST) -> bool:
    """`os.environ` (or a bare `environ`)."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def variables_in_source(root: Path = _SRC) -> dict[str, set[str]]:
    """Each environment variable the Python source names, with the files that name it.

    Three shapes are read: a string handed to `os.environ[...]`, `os.environ.get(...)`,
    `os.getenv(...)` and their like; a string assigned to a name that says it is an
    environment variable (`..._ENV`, `..._ENV_VAR`); and any string with one of this
    product's prefixes, wherever it stands.
    """
    found: dict[str, set[str]] = {}

    def note(name: str | None, path: Path) -> None:
        if name and _NAME.match(name) and name not in _NOT_VARIABLES:
            found.setdefault(name, set()).add(str(path.relative_to(root)))

    for path in sorted(root.rglob("*.py")):
        if path.name == "settings_catalog.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_environ(node.value):
                note(_literal(node.slice), path)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = node.func
                reads_environ = _is_environ(target.value) and target.attr in (
                    "get",
                    "pop",
                    "setdefault",
                )
                if reads_environ or target.attr == "getenv":
                    note(_literal(node.args[0]) if node.args else None, path)
            elif isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
                note(_literal(node.left), path)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if any(n.endswith(("_ENV", "_ENV_VAR")) or "_ENV_" in n for n in names):
                    note(_literal(node.value), path)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(_OWN_PREFIXES)
            ):
                note(node.value, path)
    return found


def variables_in_entrypoint() -> set[str]:
    """The variables the container entrypoint expands (`${NAME...}`)."""
    text = _ENTRYPOINT.read_text(encoding="utf-8")
    return {name for name in re.findall(r"\$\{([A-Z][A-Z0-9_]+)", text) if _NAME.match(name)}


# ------------------------------------------------------------ the list and the code


def test_every_variable_the_code_reads_is_in_the_catalog() -> None:
    """Read a new variable and this fails until it is described."""
    read = variables_in_source()
    known = catalog.setting_names() | catalog.internal_names()

    assert {name: sorted(files) for name, files in read.items() if name not in known} == {}
    assert sorted(variables_in_entrypoint() - known) == []
    # The scan is not vacuous: it finds the variables it is there for.
    assert len(read) >= 45
    for name in ("ENFORCE_OWNERSHIP", "HF_TOKEN", "OIDC_ISSUER", "KPUBDATA_QUERY_TEMP_DIR"):
        assert name in read, name


def test_every_entry_of_the_catalog_is_read_by_something() -> None:
    """Stop reading a variable and this fails until its entry is removed."""
    read = set(variables_in_source()) | variables_in_entrypoint()

    assert sorted((catalog.setting_names() | catalog.internal_names()) - read) == []


def test_the_scanner_sees_each_way_the_code_names_a_variable(tmp_path: Path) -> None:
    """A gate nobody has watched catch something is a gate nobody knows works."""
    (tmp_path / "module.py").write_text(
        "import os\n"
        'A = os.environ.get("PLAIN_GET", "")\n'
        'B = os.environ["PLAIN_INDEX"]\n'
        'C = os.getenv("PLAIN_GETENV")\n'
        'D = "PLAIN_IN" in os.environ\n'
        '_SOMETHING_ENV = "NAMED_BY_CONSTANT"\n'
        'E = "KPUBDATA_ANYWHERE_AT_ALL"\n'
        'F = os.environ.pop("PLAIN_POP", None)\n'
        'G = "NOT_A_VARIABLE_JUST_TEXT"\n'
        'H = os.environ.get("lowercase_is_not_one")\n',
        encoding="utf-8",
    )

    assert set(variables_in_source(tmp_path)) == {
        "PLAIN_GET",
        "PLAIN_INDEX",
        "PLAIN_GETENV",
        "PLAIN_IN",
        "NAMED_BY_CONSTANT",
        "KPUBDATA_ANYWHERE_AT_ALL",
        "PLAIN_POP",
    }


def test_names_are_unique_and_no_variable_is_both_a_setting_and_internal() -> None:
    names = [setting.name for setting in catalog.SETTINGS]
    internal = [variable.name for variable in catalog.INTERNAL_VARIABLES]

    assert len(names) == len(set(names))
    assert len(internal) == len(set(internal))
    assert set(names) & set(internal) == set()


def test_every_setting_has_a_group_the_document_has_a_heading_for() -> None:
    for setting in catalog.SETTINGS:
        assert setting.group in catalog.GROUPS, setting.name
        assert setting.description.strip() and setting.default.strip() and setting.required.strip()
    assert {setting.group for setting in catalog.SETTINGS} == set(catalog.GROUPS)


def test_no_description_breaks_the_table_it_is_written_into() -> None:
    """A newline or a bare pipe in a cell ends the row."""
    for entry in (*catalog.SETTINGS, *catalog.INTERNAL_VARIABLES):
        assert "\n" not in entry.description, entry.name
        assert not re.search(r"(?<!\\)\|", entry.description), entry.name


def test_the_variable_the_issue_named_is_documented() -> None:
    assert "KPUBDATA_QUERY_TEMP_DIR" in catalog.internal_names()
    assert "| `KPUBDATA_QUERY_TEMP_DIR` |" in (_ROOT / "docs" / "deployment.md").read_text("utf-8")


# -------------------------------------------------------- the list and the document


def test_the_document_is_not_stale() -> None:
    assert _generator().main(["--check"]) == 0


def test_the_document_lists_each_variable_once_and_only_in_the_generated_block() -> None:
    generator = _generator()
    document = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    block = document[document.index(generator.BEGIN) : document.index(generator.END)]
    outside = document.replace(block, "")

    for name in sorted(catalog.setting_names() | catalog.internal_names()):
        assert block.count(f"| `{name}` |") == 1, name
        # Prose may mention a variable; a second table row for it is how the two drifted.
        assert f"| `{name}` |" not in outside, name


def test_a_stale_document_fails_the_check_and_regenerating_fixes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _generator()
    copy = tmp_path / "deployment.md"
    original = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    copy.write_text(original.replace("| `OIDC_ISSUER` |", "| `OIDC_ISSUERS` |"), encoding="utf-8")
    monkeypatch.setattr(generator, "DOCUMENT", copy)
    monkeypatch.setattr(generator, "ROOT", tmp_path)

    assert generator.main(["--check"]) == 1

    assert generator.main([]) == 0
    assert generator.main(["--check"]) == 0
    assert copy.read_text(encoding="utf-8") == original


def test_what_is_written_by_hand_outside_the_markers_is_left_alone() -> None:
    generator = _generator()
    document = f"before\n{generator.BEGIN}\nold\n{generator.END}\nafter\n"

    assert generator.replace_block(document, "NEW") == "before\nNEW\nafter\n"


@pytest.mark.parametrize(
    "document",
    [
        "no markers at all",
        "<!-- settings:end -->",
        "{begin}\n{begin}\n{end}",
        "{end}\n{begin}",
    ],
)
def test_a_document_without_one_pair_of_markers_is_refused(
    document: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _generator()
    text = document.format(begin=generator.BEGIN, end=generator.END)
    copy = tmp_path / "deployment.md"
    copy.write_text(text, encoding="utf-8")
    monkeypatch.setattr(generator, "DOCUMENT", copy)
    monkeypatch.setattr(generator, "ROOT", tmp_path)

    assert generator.main(["--check"]) == 1
    assert generator.main([]) == 1
    assert copy.read_text(encoding="utf-8") == text
