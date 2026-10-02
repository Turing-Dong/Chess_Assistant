"""Evaluate an exported classifier against a PaddleClas image list."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import numpy as np

from evaluate import build_predictor
from predict import load_labels, preprocess


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    repo_root = root.parents[1]
    dataset_root = repo_root / "training_dataset" / "piece_character_dataset"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-file", type=Path, default=dataset_root / "paddleclas" / "val_list.txt")
    parser.add_argument("--image-root", type=Path, default=dataset_root / "images" / "needs_review")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--labels", type=Path, default=dataset_root / "paddleclas" / "label_list.txt")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--cpu", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")
    labels = load_labels(args.labels.resolve())
    image_root = args.image_root.resolve()
    samples = []
    for line_number, raw_line in enumerate(args.list_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        relative, class_text = line.rsplit(maxsplit=1)
        path = image_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"Line {line_number}: {path}")
        class_id = int(class_text)
        if class_id not in labels:
            raise ValueError(f"Line {line_number}: invalid class id {class_id}")
        samples.append((path, class_id, relative))
    if not samples:
        raise ValueError("No evaluation samples")

    predictor = build_predictor(args.model_dir.resolve(), args.cpu)
    probability_parts = []
    for start in range(0, len(samples), args.batch_size):
        batch_samples = samples[start : start + args.batch_size]
        batch = np.stack([preprocess(path) for path, _, _ in batch_samples]).astype("float32")
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
        probability_parts.append(output)
    probabilities = np.concatenate(probability_parts, axis=0)

    truth = np.asarray([class_id for _, class_id, _ in samples], dtype="int64")
    top1 = probabilities.argmax(axis=1)
    top3 = np.argsort(probabilities, axis=1)[:, -3:]
    confusion = np.zeros((len(labels), len(labels)), dtype="int64")
    for truth_id, predicted_id in zip(truth, top1):
        confusion[truth_id, predicted_id] += 1

    per_class = []
    supported_f1 = []
    for class_id, label in labels.items():
        support = int(confusion[class_id].sum())
        correct = int(confusion[class_id, class_id])
        predicted = int(confusion[:, class_id].sum())
        precision = correct / predicted if predicted else 0.0
        recall = correct / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if support:
            supported_f1.append(f1)
        per_class.append(
            {
                "class_id": class_id,
                "label": label,
                "support": support,
                "correct": correct,
                "precision": round(precision, 6),
                "recall": round(recall, 6),
                "f1": round(f1, 6),
            }
        )

    errors = []
    for (_, truth_id, relative), predicted_id, scores in zip(samples, top1, probabilities):
        if truth_id != predicted_id:
            errors.append(
                {
                    "image": relative,
                    "expected": labels[int(truth_id)],
                    "predicted": labels[int(predicted_id)],
                    "confidence": round(float(scores[predicted_id]), 6),
                    "expected_score": round(float(scores[truth_id]), 6),
                }
            )

    result = {
        "model_dir": str(args.model_dir.resolve()),
        "list_file": str(args.list_file.resolve()),
        "images": len(samples),
        "frames": len({Path(relative).stem.split("__c", 1)[0] for _, _, relative in samples}),
        "class_support": dict(sorted(Counter(labels[class_id] for class_id in truth).items())),
        "metrics": {
            "top1_accuracy": round(float((top1 == truth).mean()), 6),
            "top3_accuracy": round(float(np.any(top3 == truth[:, None], axis=1).mean()), 6),
            "macro_f1": round(float(np.mean(supported_f1)), 6),
            "mean_top1_confidence": round(float(probabilities.max(axis=1).mean()), 6),
        },
        "per_class": per_class,
        "errors": errors,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (args.output_dir / "confusion_matrix.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["actual\\predicted", *labels.values()])
        for class_id, label in labels.items():
            writer.writerow([label, *confusion[class_id].tolist()])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
