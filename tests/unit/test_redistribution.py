"""The source terms decide what may leave Builder (#688)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service import publish_api as publish_api_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.redistribution import (
    build_verdict,
    has_non_commercial_marker,
    is_public,
    kpubdata_terms,
    kpubdata_version,
    publish_issues,
)
from kpubdata_builder.service.responses import FileResponse, ServiceResponse
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef

from .test_service import _FakeClient
from .test_service_publish import (
    LICENSED_SPEC_YAML,
    _blocker_codes,
    _build,
    _publish,
    _readiness,
    _service,
    _SpyPublisher,
    _with_credentials,
    dispatch,
)

_DEV = Principal("dev")


def _spec(*sources: SourceRef, license: str | None = "CC-BY-4.0") -> BuildSpec:  # noqa: A002
    return BuildSpec(
        dataset_id="terms.test",
        title="t",
        description="d",
        sources=sources,
        exports=(ExportTarget(kind="jsonl", output_path="d.jsonl"),),
        license=license,
    )


def _terms(**by_dataset: str | None) -> object:
    def lookup(dataset_id: str) -> str | None:
        if dataset_id not in by_dataset:
            raise LookupError(dataset_id)
        return by_dataset[dataset_id]

    return lookup


# ------------------------------------------------------------------ verdicts


def test_a_build_takes_its_most_restricted_source() -> None:
    lookup = _terms(**{"p.a": "allowed", "p.b": "non_commercial", "p.c": "forbidden"})
    a, b, c = (SourceRef(provider="p", dataset=d, alias=d) for d in "abc")

    assert build_verdict(_spec(a), lookup).verdict == "allowed"  # type: ignore[arg-type]
    assert build_verdict(_spec(a, b), lookup).verdict == "non_commercial"  # type: ignore[arg-type]
    assert build_verdict(_spec(a, b, c), lookup).verdict == "forbidden"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("declared", "source"),
    [
        (None, SourceRef(provider="p", dataset="a")),
        ("bogus", SourceRef(provider="p", dataset="a")),
        ("allowed", SourceRef(provider="p", dataset="not_in_catalog")),
        ("allowed", SourceRef(kind="url", endpoint="https://example.org/data.json")),
    ],
    ids=["declares nothing", "unknown word", "not in the catalog", "url source"],
)
def test_not_knowing_is_never_permission(declared: str | None, source: SourceRef) -> None:
    """Negative: every way of not knowing reads as unknown."""
    verdict = build_verdict(_spec(source), _terms(**{"p.a": declared}))  # type: ignore[arg-type]

    assert verdict.verdict == "unknown"
    assert verdict.sources[0].reason


def test_a_missing_spec_is_unknown() -> None:
    assert build_verdict(None).verdict == "unknown"


@pytest.mark.real_catalog_terms
def test_the_real_catalog_is_read() -> None:
    """kpubdata 0.8 carries the field; kpubdata#617 starts filling it."""
    assert kpubdata_terms("datago.air_quality") in (
        None,
        "allowed",
        "non_commercial",
        "forbidden",
        "unknown",
    )
    with pytest.raises(LookupError):
        kpubdata_terms("datago.no_such_dataset")


@pytest.mark.parametrize(
    ("verdict", "public", "confirmed", "license", "codes"),
    [
        ("allowed", True, False, "CC-BY-4.0", []),
        ("unknown", False, False, "CC-BY-4.0", []),
        ("unknown", True, False, "CC-BY-4.0", ["redistribution_unknown"]),
        ("forbidden", False, True, "CC-BY-4.0", ["redistribution_forbidden"]),
        ("non_commercial", False, False, "CC-BY-4.0", ["non_commercial_unconfirmed"]),
        ("non_commercial", False, True, "CC-BY-4.0", []),
        ("non_commercial", True, True, "CC-BY-4.0", ["non_commercial_marker_missing"]),
        ("non_commercial", True, True, "cc-by-nc-4.0", []),
        (
            "non_commercial",
            True,
            False,
            "CC-BY-4.0",
            ["non_commercial_unconfirmed", "non_commercial_marker_missing"],
        ),
    ],
)
def test_publish_rules(
    verdict: str,
    public: bool,
    confirmed: bool,
    license: str,
    codes: list[str],  # noqa: A002
) -> None:
    spec = _spec(SourceRef(provider="p", dataset="a"), license=license)
    build = build_verdict(spec, _terms(**{"p.a": verdict}))  # type: ignore[arg-type]

    issues = publish_issues(build, public=public, confirmed_non_commercial=confirmed, spec=spec)

    assert [i.code for i in issues] == codes


@pytest.mark.parametrize(
    ("license", "marked"),
    [
        ("cc-by-nc-4.0", True),
        ("CC BY-NC-SA 2.0 KR", True),
        ("non-commercial", True),
        ("CC-BY-4.0", False),
        ("kogl-type-2", False),
        (None, False),
    ],
)
def test_non_commercial_marker(license: str | None, marked: bool) -> None:  # noqa: A002
    assert (
        has_non_commercial_marker(_spec(SourceRef(provider="p", dataset="a"), license=license))
        is marked
    )


def test_what_counts_as_public() -> None:
    assert is_public("huggingface", {"private": False})
    assert not is_public("huggingface", {"private": True})
    assert is_public("kaggle", {"public": True})
    assert not is_public("kaggle", {"public": False})
    assert not is_public("local", {})


# ------------------------------------------------------------------ publishing


def test_readiness_reports_the_verdict(tmp_path: Path) -> None:
    service = _service(tmp_path, terms="non_commercial")
    _build(service, "r1", LICENSED_SPEC_YAML)

    body = _readiness(service, "r1").body

    redistribution = cast(dict[str, JsonValue], body["redistribution"])
    assert redistribution["verdict"] == "non_commercial"
    assert "non_commercial_unconfirmed" in _blocker_codes(_readiness(service, "r1"))


def test_unknown_terms_block_a_public_publish_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: terms nobody declared (kpubdata 0.8's catalog) — public is refused,
    private goes through."""
    _with_credentials(monkeypatch, "huggingface")
    spy = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)
    service = _service(tmp_path)
    _build(service, "r1", LICENSED_SPEC_YAML)

    public = _publish(service, "r1", options={"private": False})
    private = _publish(service, "r1", options={"private": True})

    assert public.status_code == 409
    assert "redistribution_unknown" in _blocker_codes(public)
    assert private.status_code == 200
    assert len(spy.calls) == 1


def test_forbidden_terms_block_every_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: KRX-, KIPRIS- or KOGL 3/4-like terms stop even a private publish."""
    _with_credentials(monkeypatch, "huggingface")
    spy = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)
    service = _service(tmp_path, terms="forbidden")
    _build(service, "r1", LICENSED_SPEC_YAML)

    response = _publish(service, "r1", options={"private": True})

    assert response.status_code == 409
    assert "redistribution_forbidden" in _blocker_codes(response)
    assert spy.calls == []


def test_non_commercial_publishes_privately_once_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_credentials(monkeypatch, "huggingface")
    spy = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)
    service = _service(tmp_path, terms="non_commercial")
    _build(service, "r1", LICENSED_SPEC_YAML)

    unconfirmed = _publish(service, "r1", options={"private": True})
    confirmed = _publish(service, "r1", options={"private": True, "confirm_non_commercial": True})

    assert unconfirmed.status_code == 409
    assert confirmed.status_code == 200


@pytest.mark.parametrize(
    ("terms", "options"),
    [
        ("unknown", {"private": True}),
        ("non_commercial", {"private": True, "confirm_non_commercial": True}),
    ],
)
@pytest.mark.parametrize(
    ("visibility", "code"),
    [("public", "destination_public"), ("unreadable", "destination_visibility_unknown")],
)
def test_a_private_only_publish_to_a_public_destination_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terms: str,
    options: dict[str, object],
    visibility: str,
    code: str,
) -> None:
    """Negative: publishing to an existing repo never changes its visibility, so a
    private-only publish to a public one — or one whose visibility cannot be read —
    would be public."""
    _with_credentials(monkeypatch, "huggingface")
    spy = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)
    service = _service(tmp_path, terms=terms, visibility=visibility)
    _build(service, "r1", LICENSED_SPEC_YAML)

    response = _publish(service, "r1", options=options)

    assert response.status_code == 409
    assert _blocker_codes(response) == [code]
    redistribution = cast(dict[str, JsonValue], response.body["redistribution"])
    assert redistribution["verdict"] == terms
    assert spy.calls == []


@pytest.mark.parametrize("visibility", ["private", "absent"])
def test_a_private_or_new_destination_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, visibility: str
) -> None:
    _with_credentials(monkeypatch, "huggingface")
    spy = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)
    service = _service(tmp_path, visibility=visibility)
    _build(service, "r1", LICENSED_SPEC_YAML)

    assert _publish(service, "r1", options={"private": True}).status_code == 200


def test_a_probe_that_fails_is_not_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: an error reading the visibility refuses, it does not let through."""
    _with_credentials(monkeypatch, "huggingface")
    spy = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)

    def probe(*_: object) -> str:
        raise ConnectionError("hub unreachable")

    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        publish_visibility_probe=probe,
    )
    _build(service, "r1", LICENSED_SPEC_YAML)

    response = _publish(service, "r1", options={"private": True})

    assert response.status_code == 409
    assert _blocker_codes(response) == ["destination_visibility_unknown"]
    assert spy.calls == []


def test_a_publish_records_the_terms_it_went_out_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict, the kpubdata release it came from and the confirmation are kept in
    the response and the receipt."""
    _with_credentials(monkeypatch, "huggingface")
    spy = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)
    service = _service(tmp_path, terms="non_commercial")
    _build(service, "r1", LICENSED_SPEC_YAML)

    response = _publish(service, "r1", options={"private": True, "confirm_non_commercial": True})
    receipt = dispatch(
        service,
        "GET",
        "/builds/r1/publish/receipt",
        None,
        query="target=huggingface&destination=kpubdata%2Fair-quality",
    )

    assert response.status_code == 200
    record = cast(dict[str, JsonValue], response.body["redistribution"])
    assert record["verdict"] == "non_commercial"
    assert record["confirm_non_commercial"] is True
    assert record["kpubdata_version"] == kpubdata_version()
    assert cast(dict[str, JsonValue], receipt.body["result"])["redistribution"] == record
    # The confirmation is Builder's, not an option the publisher is handed.
    assert "confirm_non_commercial" not in spy.calls[0][1]


# ------------------------------------------------------------------ other ways out


def _warehouse_service(tmp_path: Path, terms: str) -> tuple[BuilderService, str]:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        warehouse_root=tmp_path / "wh",
        terms_lookup=lambda _id: terms,
    )
    response = service.build(LICENSED_SPEC_YAML, run_id="r1")
    assert response.status_code == 200
    materialized = cast(dict[str, dict[str, JsonValue]], response.body["materialized"])
    return service, cast(str, next(iter(materialized.values()))["logical_name"])


def _code(response: ServiceResponse | FileResponse) -> tuple[int, object]:
    if isinstance(response, FileResponse):
        return response.status_code, None
    return response.status_code, response.body.get("code")


def _ways_out(service: BuilderService, table: str) -> dict[str, tuple[int, object]]:
    query = {"table": table, "sql": "SELECT * FROM dataset"}
    return {
        "query": _code(
            service.query(
                {
                    "dataset_id": "dataset.publish",
                    "run_id": "r1",
                    "stage": "silver",
                    "sql": "SELECT * FROM dataset",
                },
                principal=_DEV,
            )
        ),
        "preview": _code(service.preview(LICENSED_SPEC_YAML, principal=_DEV)),
        "warehouse_query": _code(service.query_warehouse(query, principal=_DEV)),
        "warehouse_rows": _code(service.read_warehouse_rows({"table": table}, principal=_DEV)),
        "warehouse_aggregate": _code(
            service.aggregate_warehouse(
                {"table": table, "measures": [{"fn": "count_rows", "as": "n"}]}, principal=_DEV
            )
        ),
        "export": _code(service.create_warehouse_export(query, principal=_DEV)),
        "artifact": _code(
            service.serve_artifact_file("r1", "gold/datago.air_quality/out/data.jsonl")
        ),
        "profile": _code(service.get_warehouse_profile(table, "current", principal=_DEV)),
        "analysis": _code(service.create_analysis({"name": "a", **query}, principal=_DEV)),
    }


def test_forbidden_data_leaves_by_no_way(tmp_path: Path) -> None:
    """Negative: /query, /preview, warehouse reads, exports and downloads all refuse."""
    service, table = _warehouse_service(tmp_path, "forbidden")

    outcomes = _ways_out(service, table)

    assert outcomes == dict.fromkeys(outcomes, (403, "redistribution_forbidden")), outcomes
    stage = service.get_run_stage_detail("r1", "silver", "datago.air_quality", limit=5)
    assert stage.status_code == 200
    assert stage.body["sample"] == []
    assert stage.body["sample_withheld"] == "redistribution_forbidden"


def test_the_refusal_matches_the_contract(tmp_path: Path) -> None:
    """The 403 body is the declared ``RedistributionForbidden`` response."""
    import yaml

    from ._openapi import validate

    contract = yaml.safe_load(
        (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
    )
    schema = {"$ref": "#/components/schemas/RedistributionForbiddenError"}
    service, table = _warehouse_service(tmp_path, "forbidden")

    response = service.read_warehouse_rows({"table": table}, principal=_DEV)

    assert response.status_code == 403
    assert validate(cast(JsonValue, response.body), schema, contract) == []
    assert (
        validate(cast(JsonValue, response.body), {"$ref": "#/components/schemas/Error"}, contract)
        == []
    )


@pytest.mark.parametrize("terms", ["allowed", "unknown", "non_commercial"])
def test_other_terms_keep_every_way_working(tmp_path: Path, terms: str) -> None:
    """What the gate protects is still usable: reading is not publishing."""
    service, table = _warehouse_service(tmp_path, terms)

    outcomes = _ways_out(service, table)

    assert all(status == 200 for status, _ in outcomes.values()), outcomes
    stage = service.get_run_stage_detail("r1", "silver", "datago.air_quality", limit=5)
    assert stage.body["sample"]
    assert "sample_withheld" not in stage.body


def test_an_export_made_before_the_terms_were_declared_is_not_served(tmp_path: Path) -> None:
    """Downloads are checked now, not when the export was made."""
    terms = {"value": "allowed"}
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        warehouse_root=tmp_path / "wh",
        terms_lookup=lambda _id: terms["value"],
    )
    built = service.build(LICENSED_SPEC_YAML, run_id="r1")
    materialized = cast(dict[str, dict[str, JsonValue]], built.body["materialized"])
    table = cast(str, next(iter(materialized.values()))["logical_name"])
    created = service.create_warehouse_export(
        {"table": table, "sql": "SELECT * FROM dataset"}, principal=_DEV
    )
    export_id = cast(str, created.body["export_id"])

    terms["value"] = "forbidden"
    response = service.download_warehouse_export(export_id, principal=_DEV)

    assert _code(response) == (403, "redistribution_forbidden")


def test_a_saved_analysis_does_not_run_once_the_terms_forbid(tmp_path: Path) -> None:
    """An analysis saved while the terms allowed it is refused when it runs again."""
    terms = {"value": "allowed"}
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        warehouse_root=tmp_path / "wh",
        terms_lookup=lambda _id: terms["value"],
    )
    built = service.build(LICENSED_SPEC_YAML, run_id="r1")
    materialized = cast(dict[str, dict[str, JsonValue]], built.body["materialized"])
    table = cast(str, next(iter(materialized.values()))["logical_name"])
    created = service.create_analysis(
        {"name": "a", "table": table, "sql": "SELECT * FROM dataset"}, principal=_DEV
    )
    assert created.status_code == 200, created.body
    analysis = cast(dict[str, JsonValue], created.body["analysis"])

    terms["value"] = "forbidden"
    response = service.run_analysis(cast(str, analysis["analysis_id"]), principal=_DEV)

    assert _code(response) == (403, "redistribution_forbidden")


def test_a_refused_query_on_someone_elses_run_says_nothing_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes after the run is resolved as the caller's (#796)."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service, _ = _warehouse_service(tmp_path, "forbidden")
    other = Principal("oidc", "bob", "oidc:bob")

    response = service.query(
        {
            "dataset_id": "dataset.publish",
            "run_id": "r1",
            "stage": "silver",
            "sql": "SELECT * FROM dataset",
        },
        principal=other,
    )

    assert response.body.get("code") != "redistribution_forbidden"


# ------------------------------------------------------------------ CLI publish


@pytest.mark.parametrize(
    ("terms", "confirm", "exit_code"),
    [
        ("forbidden", True, 2),
        ("non_commercial", False, 2),
        ("non_commercial", True, 0),
        ("unknown", False, 0),
        ("allowed", False, 0),
    ],
)
def test_the_cli_publish_has_the_same_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    terms: str,
    confirm: bool,
    exit_code: int,
) -> None:
    """The CLI is no side door (a local publish is private)."""
    from kpubdata_builder.cli import _run_publish

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(LICENSED_SPEC_YAML, encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "data.jsonl").write_text('{"a": 1}\n', encoding="utf-8")

    code = _run_publish(
        str(spec_path),
        target="local",
        destination=str(tmp_path / "dest"),
        artifacts_dir=str(artifacts),
        confirm_non_commercial=confirm,
        terms_lookup=lambda _id: terms,
    )

    assert code == exit_code
    if exit_code:
        assert "source terms do not allow" in capsys.readouterr().err
        assert not (tmp_path / "dest").exists()


class _VisibleSpy(_SpyPublisher):
    def __init__(self, visibility: str | None) -> None:
        super().__init__("huggingface")
        self._visibility = visibility

    def destination_visibility(
        self, destination: str, *, credentials: Mapping[str, str] | None = None
    ) -> str:
        if self._visibility is None:
            raise ConnectionError("hub unreachable")
        return self._visibility


@pytest.mark.parametrize(
    ("visibility", "code"),
    [("public", "destination_public"), (None, "destination_visibility_unknown")],
)
def test_the_cli_reads_the_destination_visibility_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    visibility: str | None,
    code: str,
) -> None:
    """Negative: a private-only CLI publish to a public (or unreadable) destination."""
    from kpubdata_builder.cli import _run_publish
    from kpubdata_builder.publishers import PUBLISHER_REGISTRY

    spy = _VisibleSpy(visibility)
    monkeypatch.setitem(PUBLISHER_REGISTRY, "huggingface", spy)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(LICENSED_SPEC_YAML, encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "data.jsonl").write_text('{"a": 1}\n', encoding="utf-8")

    exit_code = _run_publish(
        str(spec_path),
        target="huggingface",
        destination="kpubdata/air-quality",
        artifacts_dir=str(artifacts),
        terms_lookup=lambda _id: "unknown",
    )

    assert exit_code == 2
    assert code in capsys.readouterr().err
    assert spy.calls == []
