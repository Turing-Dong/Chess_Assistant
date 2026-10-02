"""Evaluate the exported classifier on a class-folder test dataset."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import numpy as np
import paddle.inference as paddle_infer

from predict import load_labels, preprocess


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    repo_root = root.parents[1]
    dataset_root = repo_root / "training_dataset" / "piece_character_dataset" / "images"
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-dir", type=Path, default=dataset_root / "auto_labeled")
    parser.add_argument(
        "--exclude-frames-from",
        type=Path,
        default=dataset_root / "needs_review",
        help="Exclude test images whose source frame occurs under this directory.",
    )
    parser.add_argument(
        "--include-overlapping-frames",
        action="store_true",
        help="Evaluate every image, including source frames also present in training/validation data.",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=root / "output" / "xiangqi_pplcnet_x1_0" / "inference",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=repo_root
        / "training_dataset"
        / "piece_character_dataset"
        / "paddleclas"
        / "label_list.txt",
    )
    parser.add_argument("--output-dir", type=Path, default=root / "output" / "test_evaluation")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--cpu", action="store_true")
    return parser.parse_args()


def get_frame_id(path: Path) -> str:
    return path.stem.split("__c", maxsplit=1)[0]


def list_images(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


def build_predictor(model_dir: Path, cpu: bool):
    config = paddle_infer.Config(
        str(model_dir / "inference.json"), str(model_dir / "inference.pdiparams")
    )
    if cpu:
        config.disable_gpu()
    else:
        config.enable_use_gpu(512, 0)
    config.switch_ir_optim(True)
    return paddle_infer.create_predictor(config)


def main() -> None:
    args = parse_args()
    labels = load_labels(args.labels.resolve())
    name_to_id = {name: index for index, name in labels.items()}
    paths = list_images(args.test_dir.resolve())

    unknown_classes = sorted({path.parent.name for path in paths} - set(name_to_id))
    if unknown_classes:
        raise ValueError(f"Unknown test class directories: {unknown_classes}")

    excluded_frames: set[str] = set()
    if args.exclude_frames_from and not args.include_overlapping_frames:
        excluded_frames = {get_frame_id(path) for path in list_images(args.exclude_frames_from.resolve())}
    excluded = [path for path in paths if get_frame_id(path) in excluded_frames]
    paths = [path for path in paths if get_frame_id(path) not in excluded_frames]
    if not paths:
        raise ValueError("No leakage-free test images remain")

    predictor = build_predictor(args.model_dir.resolve(), args.cpu)
    probabilities_parts = []
    for start in range(0, len(paths), args.batch_size):
        batch_paths = paths[start : start + args.batch_size]
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
        probabilities_parts.append(output)
    probabilities = np.concatenate(probabilities_parts, axis=0)

    truth = np.asarray([name_to_id[path.parent.name] for path in paths], dtype="int64")
    top1 = probabilities.argmax(axis=1)
    top3 = np.argsort(probabilities, axis=1)[:, -3:]
    confusion = np.zeros((len(labels), len(labels)), dtype="int64")
    for true_id, predicted_id in zip(truth, top1):
        confusion[true_id, predicted_id] += 1

    per_class = []
    supported_f1 = []
    for class_id, name in labels.items():
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
                "label": name,
                "support": support,
                "correct": correct,
                "accuracy": round(recall, 6) if support else None,
                "precision": round(precision, 6) if support else None,
                "recall": round(recall, 6) if support else None,
                "f1": round(f1, 6) if support else None,
            }
        )

    errors = []
    for path, true_id, predicted_id, scores in zip(paths, truth, top1, probabilities):
        if true_id != predicted_id:
            errors.append(
                {
                    "image": str(path),
                    "expected": labels[int(true_id)],
                    "predicted": labels[int(predicted_id)],
                    "confidence": round(float(scores[predicted_id]), 6),
                    "expected_score": round(float(scores[true_id]), 6),
                }
            )

    result = {
        "test_directory": str(args.test_dir.resolve()),
        "label_quality": "auto_labeled_not_human_reviewed",
        "leakage_control": {
            "overlapping_frames_included": args.include_overlapping_frames,
            "excluded_reference": (
                None if args.include_overlapping_frames else str(args.exclude_frames_from.resolve())
            ),
            "excluded_images": len(excluded),
            "excluded_frames": sorted({get_frame_id(path) for path in excluded}),
        },
        "test_images": len(paths),
        "test_frames": len({get_frame_id(path) for path in paths}),
        "class_support": dict(sorted(Counter(path.parent.name for path in paths).items())),
        "metrics": {
            "top1_accuracy": round(float((top1 == truth).mean()), 6),
            "top3_accuracy": round(float(np.any(top3 == truth[:, None], axis=1).mean()), 6),
            "macro_f1_supported_classes": round(float(np.mean(supported_f1)), 6),
        },
        "per_class": per_class,
        "errors": errors,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "test_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (args.output_dir / "confusion_matrix.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["actual\\predicted", *labels.values()])
        for class_id, name in labels.items():
            writer.writerow([name, *confusion[class_id].tolist()])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
