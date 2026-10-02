#!/usr/bin/env python3
"""Move confidently classified flat crops into class directories.

Strict Worker/Qwen pseudo-labels are accepted. PP-LCNet pseudo-labels are
accepted only when their recorded top-1 confidence reaches the configured
threshold. Uncertain images remain in ``all_images``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import build_piece_character_dataset as builder


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    project_root = script_path.parents[2]
    dataset_root = project_root / "training_dataset" / "piece_character_dataset"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=dataset_root)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--destination-dir", type=Path)
    parser.add_argument("--pplcnet-threshold", type=float, default=0.90)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def pplcnet_score(annotation_source: str) -> float | None:
    prefix = "pplcnet_top1:"
    if not annotation_source.startswith(prefix):
        return None
    try:
        return float(annotation_source[len(prefix) :])
    except ValueError:
        return None


def write_annotations(dataset_root: Path, rows: list[dict], fieldnames: list[str]) -> None:
    csv_path = dataset_root / "annotations.csv"
    csv_tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with csv_tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(csv_tmp, csv_path)

    jsonl_path = dataset_root / "annotations.jsonl"
    jsonl_tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    with jsonl_tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(jsonl_tmp, jsonl_path)


def main() -> None:
    args = parse_args()
    if not 0.0 <= args.pplcnet_threshold <= 1.0:
        raise ValueError("--pplcnet-threshold must be between 0 and 1")

    dataset_root = args.dataset_root.resolve()
    source_dir = (args.source_dir or dataset_root / "images" / "all_images").resolve()
    destination_dir = (
        args.destination_dir or dataset_root / "images" / "needs_review"
    ).resolve()
    annotation_path = dataset_root / "annotations.csv"
    with annotation_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    rows_by_name: dict[str, dict] = {}
    for row in rows:
        name = Path(row["image_path"]).name
        if name in rows_by_name:
            raise RuntimeError(f"Duplicate annotation filename: {name}")
        rows_by_name[name] = row

    source_paths = sorted(source_dir.glob("*.jpg"))
    missing_annotations = sorted(path.name for path in source_paths if path.name not in rows_by_name)
    if missing_annotations:
        raise RuntimeError(f"Missing annotations for {len(missing_annotations)} source images")

    existing_by_name: dict[str, list[Path]] = {}
    if destination_dir.exists():
        for path in destination_dir.rglob("*.jpg"):
            existing_by_name.setdefault(path.name, []).append(path)

    decisions = []
    reason_counts = Counter()
    class_counts = Counter()
    conflicts = []
    for source_path in source_paths:
        row = rows_by_name[source_path.name]
        label = row.get("label", "")
        source = row.get("annotation_source", "")
        status = row.get("annotation_status", "")
        score = pplcnet_score(source)
        reason = "uncertain"
        confident = False
        if (
            label in builder.VALID_LABEL_SET
            and source == "worker_candidate_classifier"
            and status == "pseudo_label"
        ):
            reason = "qwen_strict"
            confident = True
        elif label in builder.VALID_LABEL_SET and score is not None and score >= args.pplcnet_threshold:
            reason = "pplcnet_threshold"
            confident = True

        destination = destination_dir / label / source_path.name if confident else None
        same_name_paths = existing_by_name.get(source_path.name, [])
        conflict = False
        if confident and same_name_paths and destination not in same_name_paths:
            conflict = True
            conflicts.append(
                {
                    "image": source_path.name,
                    "expected_destination": str(destination),
                    "existing_paths": [str(path) for path in same_name_paths],
                }
            )
            reason = "destination_conflict"
            confident = False
            destination = None

        reason_counts[reason] += 1
        if confident:
            class_counts[label] += 1
        decisions.append(
            {
                "source": source_path,
                "row": row,
                "label": label,
                "score": score,
                "reason": reason,
                "confident": confident,
                "destination": destination,
                "destination_exists": bool(destination and destination.exists()),
                "conflict": conflict,
            }
        )

    moved = 0
    deduplicated = 0
    uncertain = 0
    if args.apply:
        for decision in decisions:
            source_path = decision["source"]
            row = decision["row"]
            if not decision["confident"]:
                uncertain += 1
                row["image_path"] = source_path.relative_to(dataset_root).as_posix()
                row["partition"] = "unclassified"
                row["annotation_status"] = "uncertain"
                continue

            destination: Path = decision["destination"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if sha256(source_path) != sha256(destination):
                    raise RuntimeError(f"Destination content differs: {destination}")
                source_path.unlink()
                deduplicated += 1
            else:
                os.replace(source_path, destination)
                moved += 1
            row["image_path"] = destination.relative_to(dataset_root).as_posix()
            row["partition"] = "needs_review"

        write_annotations(dataset_root, rows, fieldnames)

    summary = {
        "status": "ok" if not conflicts else "conflicts",
        "mode": "apply" if args.apply else "dry_run",
        "generated_at": utc_now(),
        "pplcnet_threshold": args.pplcnet_threshold,
        "source_images_before": len(source_paths),
        "confident": sum(decision["confident"] for decision in decisions),
        "uncertain": sum(not decision["confident"] for decision in decisions),
        "reasons": dict(sorted(reason_counts.items())),
        "class_counts": dict(sorted(class_counts.items())),
        "destination_conflicts": conflicts,
        "moved": moved,
        "deduplicated": deduplicated,
    }
    if args.apply:
        summary["source_images_after"] = len(list(source_dir.glob("*.jpg")))
        report_path = dataset_root / "all_images_organization_report.json"
        report_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        summary["report"] = str(report_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not conflicts else 2)


if __name__ == "__main__":
    main()
