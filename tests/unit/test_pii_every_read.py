"""No read the contract offers shows a declared PII value (#1113).

``test_pii_reads.py`` holds one negative test per way out that was known when it was
written. This one does not name the ways out: it calls every ``GET`` of the service
contract, taken from the generated operation table, for a run whose source declares a
personal column, and then the read operations that are a ``POST``. A route added to
the contract is swept the day it is added, and a path parameter this test cannot fill
fails it, so a new read cannot go unlooked at.

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
    "createWarehouseExport": [
        {"table": "{table}", "sql": "SELECT * FROM dataset", "format": "csv"},
    ],
}

#: The other POST operations: they write, or read nothing a build produced.
#: ``createAnalysis`` and ``runAnalysis`` are reads of a table and are followed below.
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
_POST_FOLLOWED = frozenset({"createAnalysis", "runAnalysis"})

#: These GETs cannot return this run's rows with the fixture's client or absent ID.
#: Pin their refusals so a new route cannot pass the sweep by returning an error.
_GET_REFUSALS = {
    "readyz": 503,  # The fixture has no live provider configuration.
    "getCatalog": 502,
    "listProviders": 502,
    "getProviderStatus": 502,
    "getProviderCredential": 502,
    "getUpload": 404,  # This run was built without an upload.
    "getPublishReceipt": 404,  # No remote publish is attempted by this test.
    "getWarehouseExport": 404,  # The placeholder precedes the real export below.
    "downloadWarehouseExport": 404,
    "getAnalysis": 404,  # The placeholder precedes the saved analysis below.
}


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
        self.publish_unmasked = bool(gold)
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
        revision = dispatch(
            self.service,
            "PUT",
            "/revisions/spec/pii-sweep",
            {"content": {"yaml": _SPEC.format(gold=gold)}, "expected_revision": 0},
        )
        assert isinstance(revision, ServiceResponse) and revision.status_code == 200, revision
        tables = cast(
            list[dict[str, JsonValue]],
            self._json("GET", "/warehouse/tables")["tables"],
        )
        (self.table,) = [cast(str, table["logical_name"]) for table in tables]
        files = cast(list[JsonValue], self._json("GET", f"/artifacts/{_RUN}")["files"])
        self._fills: dict[str, list[str]] = {
            "run_id": [_RUN],
            "dataset_id": [_DATASET],
            "stage": ["bronze", "silver", "gold"],
            "file_path": [str(f) for f in files],
            "name": [self.table],
            "provider": ["localdata"],
            # Nothing of these exists for the run until this sweep makes one.
            "upload_id": ["none"],
            "export_id": ["none"],
            "analysis_id": ["none"],
            "kind": ["spec"],
            "doc_id": ["pii-sweep"],
        }
        self.answers: dict[str, tuple[int, str]] = {}

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
        for method, template, operation, *_ in OPERATIONS:
            if method == "GET":
                for path in self._paths(template):
                    query = (
                        f"source={_SOURCE}"
                        if operation == "getBuildStageDetail"
                        else "target=huggingface&destination=pii-sweep"
                        if operation in {"getPublishReadiness", "getPublishReceipt"}
                        else ""
                    )
                    response = self._record(
                        f"GET {path}", dispatch(self.service, "GET", path, None, query)
                    )
                    assert isinstance(response, (FileResponse, ServiceResponse, TextResponse))
                    expected = _GET_REFUSALS.get(operation, 200)
                    if (
                        not self.publish_unmasked
                        and operation == "getBuildArtifactFile"
                        and ("/bronze/" in path or "/silver/" in path)
                    ):
                        expected = 403
                    assert response.status_code == expected, (operation, path, response)
            elif method == "POST" and operation in _POST_READS:
                for index, body in enumerate(_POST_READS[operation]):
                    sent = cast(
                        dict[str, JsonValue],
                        json.loads(json.dumps(body).replace("{table}", self.table)),
                    )
                    response = self._record(
                        f"POST {template} #{index}",
                        dispatch(self.service, "POST", template, sent),
                    )
                    if operation == "createWarehouseExport":
                        self._follow_export(response)
        self._saved_analysis()

    def _saved_analysis(self) -> None:
        """Save a query over the table, then read it back and run it."""
        saved = self._record(
            "POST /analyses",
            dispatch(
                self.service,
                "POST",
                "/analyses",
                {"name": "all", "table": self.table, "sql": "SELECT * FROM dataset"},
            ),
        )
        assert isinstance(saved, ServiceResponse) and saved.status_code < 300, saved
        analysis = cast(dict[str, JsonValue], cast(dict[str, JsonValue], saved.body)["analysis"])
        analysis_id = analysis["analysis_id"]
        path = f"/analyses/{analysis_id}"
        self._record(f"GET {path}", dispatch(self.service, "GET", path, None))
        self._record(f"POST {path}/run", dispatch(self.service, "POST", f"{path}/run", {}))
        self._record("GET /analyses after", dispatch(self.service, "GET", "/analyses", None))

    def _follow_export(self, response: object) -> None:
        """Read back an export that was made; a refused one has nothing to read."""
        assert isinstance(response, ServiceResponse), response
        if response.status_code >= 300:
            return
        export_id = cast(dict[str, JsonValue], response.body)["export_id"]
        for suffix in ("", "/download"):
            path = f"/warehouse/exports/{export_id}{suffix}"
            self._record(f"GET {path}", dispatch(self.service, "GET", path, None))

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


def test_the_sweep_reaches_the_reads_that_carry_rows(tmp_path: Path) -> None:
    """An answer that is a refusal for another reason would hide a leak behind it."""
    sweep = _Sweep(tmp_path)

    sweep.run()

    status = {label: answer[0] for label, answer in sweep.answers.items()}
    gold = f"/artifacts/{_RUN}/gold/{_SOURCE}"
    for label in (
        f"GET /builds/{_RUN}/stages/silver",
        f"GET /builds/{_RUN}/stages/gold",
        f"GET /builds/{_RUN}/manifest",
        f"GET /builds/{_RUN}/quality",
        f"GET /datasets/{_DATASET}",
        f"GET /warehouse/tables/{sweep.table}",
        f"GET /warehouse/tables/{sweep.table}/profile",
        f"GET {gold}/data.csv",
        f"GET {gold}/data.jsonl",
        f"GET {gold}/table.parquet",
        "POST /preview #0",
        "POST /warehouse/rows #0",
    ):
        assert status[label] == 200, (label, sweep.answers[label])
    for label, code in status.items():
        if label.startswith("POST "):
            assert code < 300, (label, sweep.answers[label])
    assert any(label.endswith("/download") and code == 200 for label, code in status.items())
    # The raw stages are refused, not served masked.
    refused = [label for label, code in status.items() if code == 403]
    assert refused and all("/bronze/" in label or "/silver/" in label for label in refused)


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
    assert f"GET /builds/{_RUN}/stages/silver" in showing
    assert f"GET /warehouse/tables/{sweep.table}/profile" in showing or any(
        label.startswith("POST /warehouse/rows") for label in showing
    )
    assert any(label.endswith("/data.csv") for label in showing)
