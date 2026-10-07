#!/usr/bin/env python3
"""Build a conservative trainable subset from generated piece crops."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


VALID_LABELS = {
    "red_shuai",
    "black_jiang",
    "red_shi",
    "black_shi",
    "red_xiang",
    "black_xiang",
    "red_ma",
    "black_ma",
    "red_che",
    "black_che",
    "red_pao",
    "black_pao",
    "red_bing",
    "black_zu",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--min-confidence", type=float, default=0.90)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_model_score(source: str) -> float | None:
    prefix = "pplcnet_top1:"
    if not source.startswith(prefix):
        return None
    try:
        return float(source[len(prefix) :])
    except ValueError:
        return None


def main() -> None:
    args = parse_args()
    if not 0.0 <= args.min_confidence <= 1.0:
        raise ValueError("--min-confidence must be between 0 and 1")

    dataset_root = args.dataset_root.resolve()
    output_dir = (args.output_dir or dataset_root / "images" / "trainable").resolve()
    annotations_path = dataset_root / "annotations.csv"
    with annotations_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    output_dir.mkdir(parents=True, exist_ok=True)
    selected: list[dict] = []
    rejected: list[dict] = []
    class_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()

    for row in rows:
        label = row.get("label", "")
        annotation_source = row.get("annotation_source", "")
        annotation_status = row.get("annotation_status", "")
        score = parse_model_score(annotation_source)
        selection_source = ""
        if (
            label in VALID_LABELS
            and annotation_status == "pseudo_label"
            and annotation_source == "existing_worker_result"
        ):
            selection_source = "existing_worker_result"
        elif label in VALID_LABELS and score is not None and score >= args.min_confidence:
            selection_source = "pplcnet_high_confidence"

        if not selection_source:
            rejected.append(row)
            continue

        source_path = dataset_root / row["image_path"]
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        destination = output_dir / label / source_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256(source_path) != sha256(destination):
                raise RuntimeError(f"Conflicting destination: {destination}")
        else:
            shutil.copy2(source_path, destination)

        selected_row = dict(row)
        selected_row["image_path"] = destination.relative_to(dataset_root).as_posix()
        selected_row["partition"] = "trainable"
        selected_row["selection_source"] = selection_source
        selected_row["selection_confidence"] = "" if score is None else f"{score:.6f}"
        selected.append(selected_row)
        class_counts[label] += 1
        source_counts[selection_source] += 1

    manifest_fields = fieldnames + ["selection_source", "selection_confidence"]
    manifest_path = dataset_root / "trainable_annotations.csv"
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest_fields)
        writer.writeheader()
        writer.writerows(selected)
    jsonl_path = dataset_root / "trainable_annotations.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    missing_classes = sorted(VALID_LABELS - set(class_counts))
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_root": str(dataset_root),
        "output_dir": str(output_dir),
        "min_confidence": args.min_confidence,
        "input_crops": len(rows),
        "selected": len(selected),
        "held_for_review": len(rejected),
        "selection_sources": dict(sorted(source_counts.items())),
        "class_counts": dict(sorted(class_counts.items())),
        "missing_classes": missing_classes,
        "manifest": str(manifest_path),
    }
    report_path = dataset_root / "trainable_subset_report.json"
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary["report"] = str(report_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not missing_classes else 2)


if __name__ == "__main__":
    main()
