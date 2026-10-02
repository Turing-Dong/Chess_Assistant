"""Classify needs-review crops with the exported PP-LCNet model.

The default mode is read-only and prints prediction statistics. Pass ``--apply``
to move every crop into ``needs_review/<predicted-class>/`` and update the
dataset annotations. A JSON audit report is always written when applying.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from pathlib import Path

import numpy as np

from evaluate import build_predictor, list_images
from predict import load_labels, preprocess


LABEL_METADATA = {
    "red_shuai": ("帅", "shuai", "red"),
    "black_jiang": ("将", "jiang", "black"),
    "red_shi": ("仕", "shi", "red"),
    "black_shi": ("士", "shi", "black"),
    "red_xiang": ("相", "xiang", "red"),
    "black_xiang": ("象", "xiang", "black"),
    "red_ma": ("马", "ma", "red"),
    "black_ma": ("馬", "ma", "black"),
    "red_che": ("车", "che", "red"),
    "black_che": ("車", "che", "black"),
    "red_pao": ("炮", "pao", "red"),
    "black_pao": ("砲", "pao", "black"),
    "red_bing": ("兵", "bing", "red"),
    "black_zu": ("卒", "zu", "black"),
}
CHARACTER_LABELS = {metadata[0]: label for label, metadata in LABEL_METADATA.items()}


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parents[1]
    dataset_root = repo_root / "training_dataset" / "piece_character_dataset"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=dataset_root)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=script_dir / "output" / "xiangqi_pplcnet_x1_0" / "inference",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=dataset_root / "paddleclas" / "label_list.txt",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def predict_probabilities(paths: list[Path], model_dir: Path, batch_size: int, cpu: bool):
    predictor = build_predictor(model_dir, cpu)
    parts = []
    for start in range(0, len(paths), batch_size):
        batch_paths = paths[start : start + batch_size]
        batch = np.stack([preprocess(path) for path in batch_paths]).astype("float32")
        input_handle = predictor.get_input_handle(predictor.get_input_names()[0])
        input_handle.reshape(batch.shape)
        input_handle.copy_from_cpu(batch)
        predictor.run()
        output = predictor.get_output_handle(predictor.get_output_names()[0]).copy_to_cpu()
        row_sums = output.sum(axis=1, keepdims=True)
        if not (np.all(output >= 0.0) and np.allclose(row_sums, 1.0, atol=1e-4)):
            output -= output.max(axis=1, keepdims=True)
            output = np.exp(output)
            output /= output.sum(axis=1, keepdims=True)
        parts.append(output)
    return np.concatenate(parts, axis=0)


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
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")

    dataset_root = args.dataset_root.resolve()
    review_root = dataset_root / "images" / "needs_review"
    paths = list_images(review_root)
    if not paths:
        raise ValueError(f"No needs-review crops found under {review_root}")

    labels = load_labels(args.labels.resolve())
    probabilities = predict_probabilities(
        paths, args.model_dir.resolve(), args.batch_size, args.cpu
    )
    top1 = probabilities.argmax(axis=1)
    predictions = []
    confidence_bands = Counter()
    predicted_counts = Counter()
    existing_labeled = 0
    existing_agree = 0
    qwen_evidence = 0
    qwen_agree = 0
    qwen_rejected_evidence = 0
    qwen_rejected_agree = 0
    caches = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (dataset_root / "recognition_cache").glob("*.json")
    }
    for path, class_id, scores in zip(paths, top1, probabilities):
        label = labels[int(class_id)]
        score = float(scores[class_id])
        existing_label = path.parent.name if path.parent != review_root else ""
        if existing_label == "unlabeled":
            existing_label = ""
        if existing_label:
            existing_labeled += 1
            existing_agree += int(existing_label == label)
        frame_id, circle_text = path.stem.rsplit("__c", maxsplit=1)
        cached_classification = (
            caches.get(frame_id, {}).get("classifications", {}).get(str(int(circle_text)), {})
        )
        qwen_character = str(cached_classification.get("char", "")).strip()
        qwen_rejected = False
        if not qwen_character:
            qwen_character = str(cached_classification.get("rejected_char", "")).strip()
            qwen_rejected = bool(qwen_character)
        qwen_label = CHARACTER_LABELS.get(qwen_character, "")
        if qwen_label:
            qwen_evidence += 1
            qwen_agree += int(qwen_label == label)
            if qwen_rejected:
                qwen_rejected_evidence += 1
                qwen_rejected_agree += int(qwen_label == label)
        predicted_counts[label] += 1
        if score >= 0.90:
            confidence_bands[">=0.90"] += 1
        elif score >= 0.75:
            confidence_bands["0.75-0.90"] += 1
        elif score >= 0.50:
            confidence_bands["0.50-0.75"] += 1
        else:
            confidence_bands["<0.50"] += 1
        predictions.append(
            {
                "source": path.relative_to(dataset_root).as_posix(),
                "existing_label": existing_label,
                "qwen_character_label": qwen_label,
                "qwen_character_rejected": qwen_rejected,
                "predicted_label": label,
                "score": round(score, 6),
            }
        )

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "images": len(paths),
        "existing_labeled": existing_labeled,
        "existing_label_agreement": (
            round(existing_agree / existing_labeled, 6) if existing_labeled else None
        ),
        "qwen_character_evidence": qwen_evidence,
        "qwen_character_agreement": round(qwen_agree / qwen_evidence, 6) if qwen_evidence else None,
        "qwen_rejected_character_evidence": qwen_rejected_evidence,
        "qwen_rejected_character_agreement": (
            round(qwen_rejected_agree / qwen_rejected_evidence, 6)
            if qwen_rejected_evidence
            else None
        ),
        "confidence_bands": dict(sorted(confidence_bands.items())),
        "predicted_class_counts": dict(sorted(predicted_counts.items())),
    }

    if args.apply:
        csv_path = dataset_root / "annotations.csv"
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        by_path = {row["image_path"]: row for row in rows}

        for prediction, source_path in zip(predictions, paths):
            old_relative = prediction["source"]
            row = by_path.get(old_relative)
            if row is None:
                raise KeyError(f"Annotation missing for {old_relative}")
            label = prediction["predicted_label"]
            destination = review_root / label / source_path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination != source_path:
                if destination.exists():
                    raise FileExistsError(destination)
                os.replace(source_path, destination)
            new_relative = destination.relative_to(dataset_root).as_posix()
            character, piece_name, piece_color = LABEL_METADATA[label]
            row["image_path"] = new_relative
            row["partition"] = "needs_review"
            row["label"] = label
            row["character"] = character
            row["piece_name"] = piece_name
            row["piece_color"] = piece_color
            row["annotation_status"] = "model_pseudo_label"
            row["annotation_source"] = f"pplcnet_top1:{prediction['score']:.6f}"

        write_annotations(dataset_root, rows, fieldnames)
        report_path = dataset_root / "pplcnet_review_classification.json"
        report_path.write_text(
            json.dumps({"summary": summary, "predictions": predictions}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        summary["report"] = str(report_path)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
