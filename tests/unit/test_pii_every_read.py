"""No read the contract offers shows a declared PII value (#1113).

``test_pii_reads.py`` holds one negative test per way out that was known when it was
written. This one does not name the ways out: it calls every ``GET`` of the service
contract, taken from the generated operation table, for a run whose source declares a
personal column, and then the read operations that are a ``POST``. A route added to
the contract is swept the day it is added, and a path parameter this test cannot fill
fails it, so a new read cannot go unlooked at.

Every answer's status is asserted too: a read answers 200, or is one of the few listed
with the refusal it is expected to give and why. A new route that answers 400, 404 or
502 here fails the sweep instead of passing it with nothing looked at.

The same sweep over a run that opts the column out (``gold.publish_unmasked``) finds
the values, which is what shows the sweep can see them.

Bronze and Silver keep the original values on disk (#611) and are refused as
downloads; that the values exist nowhere else under the output and warehouse roots is
checked too, since a backup copies those roots.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service._contract_operations import OPERATIONS
from kpubdata_builder.service.responses import FileResponse, ServiceResponse, TextResponse
from kpubdata_builder.spec import JsonValue

from .test_gold_pii_masking import _PHONES, _SPEC, _Client

_RUN = "r1"
_DATASET = "permits.restaurant"
_SOURCE = "localdata.general_restaurant"
_OPT_OUT = "    gold:\n      publish_unmasked: [siteTel]\n"
_PARAMETER = re.compile(r"\{(\w+)\}")
_DOCUMENT = "doc1"

#: The read operations that are not a GET, by operation id, with the bodies to send.
#: ``{table}`` is the warehouse table the build committed.
_POST_READS: dict[str, list[dict[str, JsonValue]]] = {
    "previewBuild": [{"spec": _SPEC.format(gold=""), "limit": 5}],
    "queryBuiltDataset": [
        {"dataset_id": _DATASET, "run_id": _RUN, "stage": stage, "sql": sql}
        for stage in ("gold", "silver")
        for sql in (
            "SELECT * FROM dataset",
            "SELECT upper(siteTel) AS a, substr(siteTel, 1, 8) AS b FROM dataset",
            "SELECT min(siteTel) AS a, max(siteTel) AS b FROM dataset",
            "SELECT siteTel, count(*) AS n FROM dataset GROUP BY 1",
        )
    ],
    "queryWarehouseTable": [
        {"table": "{table}", "sql": "SELECT * FROM dataset"},
        {"table": "{table}", "sql": "SELECT min(siteTel) AS a, max(siteTel) AS b FROM dataset"},
    ],
    "readWarehouseRows": [{"table": "{table}"}],
    "aggregateWarehouseTable": [
        {
            "table": "{table}",
            "group_by": ["siteTel"],
            "measures": [{"fn": "count_rows", "as": "n"}],
        },
        {
            "table": "{table}",
            "measures": [
                {"fn": "min", "column": "siteTel", "as": "a"},
                {"fn": "max", "column": "siteTel", "as": "b"},
            ],
        },
    ],
}

#: The other POST operations: they write, or read nothing a build produced.
#: ``createAnalysis``, ``runAnalysis`` and ``createWarehouseExport`` are reads of a table:
#: the sweep makes one of each first, so that the GETs read real ids.
_POST_NOT_READS = frozenset(
    {
        "testProviderConnection",
        "probeProviderKey",
        "createUpload",
        "validateSpec",
        "createBuild",
        "submitBuild",
        "cancelBuildJob",
        "publishBuild",
        "reconcilePublish",
        "revertRevision",
        "adminApproveUser",
        "adminRejectUser",
    }
)
_POST_FOLLOWED = frozenset({"createAnalysis", "runAnalysis", "createWarehouseExport"})

#: The query string a GET needs to reach its answer, by operation id. A list sweeps each.
_GET_QUERIES: dict[str, list[str]] = {
    "getBuildStageDetail": [f"source={_SOURCE}"],
    "getPublishReadiness": ["target=huggingface", "target=kaggle", "target=local"],
    "getPublishReceipt": ["target=local&destination=published"],
}

#: The GETs that do not answer 200 in this sweep, with the status they answer and why
#: that is not a read left unlooked at. Anything else that is not 200 fails.
_EXPECTED_REFUSALS: dict[str, tuple[int, str]] = {
    # Not a read of the run: kpubdata's catalogue, which the fake client does not have.
    "getCatalog": (502, "catalog_unavailable"),
    "listProviders": (502, "catalog_unavailable"),
    "getProviderStatus": (502, "catalog_unavailable"),
    "getProviderCredential": (502, "catalog_unavailable"),
    # The run was not published — its spec declares no licence, so it cannot be — and a
    # receipt exists only for a publish. What a receipt records is the destination and
    # the files' digests (``test_publish_receipt*.py``), no rows.
    "getPublishReceipt": (404, "receipt_not_found"),
}
#: Bronze and Silver files of a source with a withheld column are refused (#900).
_RAW_STAGE_REFUSAL = (403, "declared_pii_withheld")


def _text(response: object) -> str:
    """Everything a client would receive, as text."""
    if isinstance(response, FileResponse):
        path = response.file_path
        if path.suffix == ".parquet":
            return json.dumps(pl.read_parquet(path).to_dicts(), ensure_ascii=False, default=str)
        return path.read_bytes().decode("utf-8", "replace")
    if isinstance(response, TextResponse):
        return response.text
    assert isinstance(response, ServiceResponse), response
    return json.dumps(response.body, ensure_ascii=False, default=str)


class _Sweep:
    """Every read of one built run, with what each answered."""

    def __init__(self, root: Path, gold: str = "") -> None:
        (root / "out").mkdir()
        (root / "wh").mkdir()
        self.root = root
        self.service = BuilderService(
            output_root=root / "out",
            client_factory=lambda **_: _Client(),
            warehouse_root=root / "wh",
        )
        built = self.service.build(_SPEC.format(gold=gold), run_id=_RUN)
        assert built.status_code == 200, built.body
        tables = cast(
            list[dict[str, JsonValue]],
            self._json("GET", "/warehouse/tables")["tables"],
        )
        (self.table,) = [cast(str, table["logical_name"]) for table in tables]
        files = cast(list[JsonValue], self._json("GET", f"/artifacts/{_RUN}")["files"])
        self.answers: dict[str, tuple[int, str]] = {}
        #: operation id of each GET answer, for the status expectations.
        self.operations: dict[str, str] = {}
        self._fills: dict[str, list[str]] = {
            "run_id": [_RUN],
            "dataset_id": [_DATASET],
            "stage": ["bronze", "silver", "gold"],
            "file_path": [str(f) for f in files],
            "name": [self.table],
            "provider": ["localdata"],
            "kind": ["spec", "annotation"],
            "doc_id": [_DOCUMENT],
        }

    def _made(self, label: str, response: object, key: str) -> str:
        """Record what creating something answered and return the id it gave."""
        self._record(label, response)
        assert isinstance(response, ServiceResponse) and response.status_code == 200, response
        body = cast(dict[str, JsonValue], response.body)
        return cast(str, cast(dict[str, JsonValue], body.get("analysis", body))[key])

    def _make_what_the_reads_need(self) -> None:
        """An upload, an export, a saved analysis and two documents, so no id is made up."""
        send = self.service
        query: dict[str, JsonValue] = {"table": self.table, "sql": "SELECT * FROM dataset"}
        self._fills["upload_id"] = [
            self._made(
                "POST /uploads",
                dispatch(send, "POST", "/uploads", None, "format=csv", raw_body=b"a,b\n1,2\n"),
                "upload_id",
            )
        ]
        export = dispatch(send, "POST", "/warehouse/exports", {**query, "format": "csv"})
        self._record("POST /warehouse/exports", export)
        # An export of a column the spec publishes unmasked is refused by the export's
        # own scan of the values (``export_blocked_pii``); there is then none to read.
        self._fills["export_id"] = (
            [cast(str, cast(dict[str, JsonValue], export.body)["export_id"])]
            if isinstance(export, ServiceResponse) and export.status_code == 200
            else []
        )
        self.analysis_id = self._made(
            "POST /analyses",
            dispatch(send, "POST", "/analyses", {"name": "all", **query}),
            "analysis_id",
        )
        self._fills["analysis_id"] = [self.analysis_id]
        documents: dict[str, JsonValue] = {
            "spec": {"yaml": _SPEC.format(gold="")},
            "annotation": {"text": "a note about the table"},
        }
        for kind, content in documents.items():
            self._made(
                f"PUT /revisions/{kind}/{_DOCUMENT}",
                dispatch(
                    send,
                    "PUT",
                    f"/revisions/{kind}/{_DOCUMENT}",
                    {"content": content, "expected_revision": 0},
                ),
                "revision",
            )

    def _json(self, method: str, path: str) -> dict[str, JsonValue]:
        response = dispatch(self.service, method, path, None)
        assert isinstance(response, ServiceResponse) and response.status_code == 200, response
        return cast(dict[str, JsonValue], response.body)

    def _paths(self, template: str) -> Iterator[str]:
        match = _PARAMETER.search(template)
        if match is None:
            yield template
            return
        assert match.group(1) in self._fills, (
            f"{template}: this sweep does not know how to fill {{{match.group(1)}}}; "
            "add it to _fills so the new read is looked at"
        )
        for value in self._fills[match.group(1)]:
            yield from self._paths(template.replace(match.group(0), value, 1))

    def _record(self, label: str, response: object) -> object:
        status = cast(int, getattr(response, "status_code"))  # noqa: B009
        self.answers[label] = (status, _text(response))
        return response

    def run(self) -> None:
        self._make_what_the_reads_need()
        for method, template, operation, *_ in OPERATIONS:
            if method == "GET":
                for path in self._paths(template):
                    for query in _GET_QUERIES.get(operation, [""]):
                        label = f"GET {path}" + (f"?{query}" if query else "")
                        self.operations[label] = operation
                        self._record(label, dispatch(self.service, "GET", path, None, query))
            elif method == "POST" and operation in _POST_READS:
                for index, body in enumerate(_POST_READS[operation]):
                    sent = cast(
                        dict[str, JsonValue],
                        json.loads(json.dumps(body).replace("{table}", self.table)),
                    )
                    self._record(
                        f"POST {template} #{index}",
                        dispatch(self.service, "POST", template, sent),
                    )
        path = f"/analyses/{self.analysis_id}/run"
        self._record(f"POST {path}", dispatch(self.service, "POST", path, {}))

    def unexpected_statuses(self) -> list[str]:
        """Every answer whose status is not the one this sweep expects of it."""
        wrong = []
        for label, (status, text) in self.answers.items():
            operation = self.operations.get(label)
            if operation is None:
                # A POST read, or something the sweep made: it worked, or was the
                # export's own refusal.
                expected, code = 200, ""
                if label == "POST /warehouse/exports" and status == 403:
                    expected, code = 403, "export_blocked_pii"
            elif operation == "getBuildArtifactFile" and (
                "/bronze/" in label or "/silver/" in label
            ):
                expected, code = _RAW_STAGE_REFUSAL
            else:
                expected, code = _EXPECTED_REFUSALS.get(operation, (200, ""))
            if status != expected or code not in text:
                wrong.append(f"{label}: {status}, expected {expected} {code}: {text[:200]}")
        return wrong

    def showing_a_phone(self) -> list[str]:
        return sorted(
            label
            for label, (_status, text) in self.answers.items()
            if any(phone in text for phone in _PHONES)
        )

    def files_holding_a_phone(self) -> list[str]:
        held = []
        for base in (self.root / "out", self.root / "wh"):
            for path in sorted(p for p in base.rglob("*") if p.is_file()):
                data = path.read_bytes()
                if any(phone.encode() in data for phone in _PHONES):
                    held.append(str(path.relative_to(self.root)))
        return held


def test_every_post_operation_is_classified() -> None:
    """A new POST is either swept as a read or listed as not one."""
    posts = {operation for method, _path, operation, *_ in OPERATIONS if method == "POST"}

    assert posts - set(_POST_READS) - _POST_NOT_READS - _POST_FOLLOWED == set()
    assert set(_POST_READS) | _POST_NOT_READS | _POST_FOLLOWED <= posts


def test_no_read_shows_a_declared_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    sweep = _Sweep(tmp_path)

    sweep.run()

    assert sweep.showing_a_phone() == []
    logged = [r.getMessage() for r in caplog.records if any(p in r.getMessage() for p in _PHONES)]
    assert logged == []


def test_every_read_answers_or_is_a_listed_refusal(tmp_path: Path) -> None:
    """A refusal for another reason would hide a leak behind it."""
    sweep = _Sweep(tmp_path)

    sweep.run()

    assert sweep.unexpected_statuses() == []
    # Every listed refusal is still met: an entry nothing answers is a stale excuse.
    met = {
        sweep.operations[label]
        for label, (status, _) in sweep.answers.items()
        if status != 200 and label in sweep.operations
    }
    assert met == {*_EXPECTED_REFUSALS, "getBuildArtifactFile"}
    # The made-up ids are gone: what was created is what was read.
    assert not any("/none" in label for label in sweep.answers)
    status = {label: answer[0] for label, answer in sweep.answers.items()}
    assert any(label.endswith("/download") and code == 200 for label, code in status.items())
    for kind in ("spec", "annotation"):
        assert status[f"GET /revisions/{kind}/{_DOCUMENT}"] == 200
        assert status[f"GET /revisions/{kind}/{_DOCUMENT}/history"] == 200


def test_the_values_are_on_disk_only_in_the_runs_raw_stages(tmp_path: Path) -> None:
    sweep = _Sweep(tmp_path)

    sweep.run()

    held = sweep.files_holding_a_phone()
    assert held, "Silver keeps the original values (#611); the sweep must be able to see them"
    for path in held:
        assert path.startswith((f"out/{_RUN}/bronze/", f"out/{_RUN}/silver/")), path


def test_the_sweep_sees_a_value_the_spec_publishes_unmasked(tmp_path: Path) -> None:
    """The control: the same sweep finds the column once it is opted out."""
    sweep = _Sweep(tmp_path, gold=_OPT_OUT)

    sweep.run()

    showing = sweep.showing_a_phone()
    assert f"GET /builds/{_RUN}/stages/silver?source={_SOURCE}" in showing
    assert f"GET /warehouse/tables/{sweep.table}/profile" in showing or any(
        label.startswith("POST /warehouse/rows") for label in showing
    )
    assert any(label.endswith("/data.csv") for label in showing)
