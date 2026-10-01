"""Hugging Face Hub layout exporter (#9).

This module exports ArtifactDataset to Hugging Face Hub upload specification directory
layout. Generates data files (data/), dataset card (README.md with YAML front matter),
and metadata (dataset_infos.json). Does not perform actual upload.

Layout::

    {output_path}/
    ├── data/
    │   └── train-00000-of-00001.{parquet|jsonl}
    ├── README.md
    └── dataset_infos.json
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import yaml

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ..stages._atomic import atomic_replace_dir
from ..stages._path_safety import safe_output_path
from ..tabular.convert import records_to_dataframe
from .base import BaseExporter, ExportResult
from .jsonl import write_jsonl

_SUPPORTED_FORMATS = ("parquet", "jsonl")


def _resolve_format(target: ExportTarget) -> str:
    """Determine data file format from target.options (default parquet)."""
    raw = target.options.get("format", "parquet")
    fmt = raw if isinstance(raw, str) else "parquet"
    if fmt not in _SUPPORTED_FORMATS:
        raise ExportError(
            f"Unsupported huggingface data format: {fmt!r}. "
            f"Supported: {', '.join(_SUPPORTED_FORMATS)}"
        )
    return fmt


def _write_data_file(artifact: ArtifactDataset, data_dir: Path, fmt: str) -> Path:
    """Write single shard data file under data/ and return path."""
    data_path = data_dir / f"train-00000-of-00001.{fmt}"
    if fmt == "parquet":
        source = artifact.data_source.parquet_path
        if source is not None:
            # The Gold table's own file: rows are not read into Python (#873).
            shutil.copyfile(source, data_path)
        else:
            records_to_dataframe(list(artifact.data_source.iter_records())).write_parquet(data_path)
    else:
        # allow_nan=False: NaN/Infinity are non-standard JSON tokens, so fail with
        # ValueError (#217).
        #
        # json_safe needed for same reason as jsonl exporter (#629). Gold tables come from
        # Polars, so if declared `casts: {deal_date: date}`, records contain
        # `date`/`Decimal` objects as-is, and `json.dumps` cannot
        # serialize them. that fix applied only to jsonl, so same spec works in jsonl
        # but fails with TypeError in huggingface.
        with data_path.open("w", encoding="utf-8") as handle:
            write_jsonl(artifact, handle)
    return data_path


def _render_card(artifact: ArtifactDataset) -> str:
    """Create dataset card with YAML front matter and Markdown body."""
    metadata = artifact.metadata
    front_matter: dict[str, object] = {
        "language": [metadata.get("language", "ko")],
        "pretty_name": metadata.get("title", "dataset"),
    }
    if metadata.get("license"):
        front_matter["license"] = metadata["license"]
    # Hugging Face records a licence outside its list as `license: other` with a name
    # and a link. Without them the card says only "other" (#764).
    for key in ("license_name", "license_link"):
        if metadata.get(key):
            front_matter[key] = metadata[key]
    front_yaml = yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=True).strip()

    title = metadata.get("title", "dataset")
    description = metadata.get("description", "")
    body = [f"---\n{front_yaml}\n---", "", f"# {title}", ""]
    if description:
        body += [description, ""]
    body += ["## 출처", ""]
    if artifact.provenance:
        body += [f"- {entry}" for entry in artifact.provenance]
    else:
        body.append("공공데이터포털 (data.go.kr)")
    attribution = metadata.get("attribution")
    if isinstance(attribution, str) and attribution.strip():
        # Public License attribution must appear in card body to fulfill obligation—
        # license identifier in front matter alone is insufficient (ADR 0018).
        body += ["", attribution.strip()]
    return "\n".join(body) + "\n"


def _dataset_infos(artifact: ArtifactDataset) -> dict[str, object]:
    """Construct metadata to be included in HF dataset_infos.json."""
    return {
        "features": dict(artifact.schema),
        "num_examples": artifact.data_source.row_count,
        "provenance": list(artifact.provenance),
        "metadata": dict(artifact.metadata),
    }


class HuggingFaceExporter(BaseExporter):
    """Export ArtifactDataset to Hugging Face Hub layout.

    Example:
        >>> HuggingFaceExporter().name
        'huggingface'
    """

    @property
    def name(self) -> str:
        """Return exporter tool name."""
        return "huggingface"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """Export ArtifactDataset to HF directory layout.

        Args:
            artifact: output to export.
            target: target with output_path (layout root) and options (format).
            output_dir: build-based output directory.

        Returns:
            ExportResult: layout root directory and total byte size.

        Raises:
            ExportError: if unsupported format or file write fails.
        """
        fmt = _resolve_format(target)
        # prevent user-controlled output_path from escaping build workspace (#210).
        hf_dir = safe_output_path(output_dir, target.output_path)
        hf_dir.parent.mkdir(parents=True, exist_ok=True)

        # write layout entirely to temp directory then atomically replace. in-place updates
        # leave old shard files on format change (JSONL->Parquet), causing layout mismatch (#203).
        tmp_dir = Path(tempfile.mkdtemp(dir=hf_dir.parent, prefix=".hf_tmp_"))
        try:
            data_dir = tmp_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            data_path = _write_data_file(artifact, data_dir, fmt)
            readme_path = tmp_dir / "README.md"
            _ = readme_path.write_text(_render_card(artifact), encoding="utf-8")
            infos_path = tmp_dir / "dataset_infos.json"
            _ = infos_path.write_text(
                json.dumps(
                    _dataset_infos(artifact),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )
            total_size = sum(path.stat().st_size for path in (data_path, readme_path, infos_path))
            atomic_replace_dir(tmp_dir, hf_dir)
        except ExportError:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        except ValueError:
            # ValueError raised by allow_nan=False for NaN/Infinity is non-standard JSON
            # token rejection contract (#217) so propagate as-is without wrapping in ExportError.
            # (but clean up temp directory.)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise ExportError(f"Failed to export Hugging Face layout to {hf_dir}: {exc}") from exc
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        # HF exporter returns the layout directory (not a single file) since it
        # produces multiple files. Consumers should use output_path as a directory.
        return ExportResult(output_path=hf_dir, file_size=total_size, format=self.name)
