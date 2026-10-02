#!/usr/bin/env python3
"""Report exact and perceptually similar images in the local training dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def perceptual_hash(gray: np.ndarray) -> np.ndarray:
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    frequency = cv2.dct(resized.astype(np.float32))[:8, :8]
    values = frequency.flatten()
    threshold = np.median(values[1:])
    return values > threshold


def normalized_preview(gray: np.ndarray) -> np.ndarray:
    preview = cv2.resize(gray, (192, 192), interpolation=cv2.INTER_AREA)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(preview)


def correlation(first: np.ndarray, second: np.ndarray) -> float:
    first_values = first.astype(np.float32)
    second_values = second.astype(np.float32)
    first_values -= first_values.mean()
    second_values -= second_values.mean()
    denominator = float(np.linalg.norm(first_values) * np.linalg.norm(second_values))
    if denominator == 0:
        return 0.0
    return float(np.sum(first_values * second_values) / denominator)


def structural_similarity(first: np.ndarray, second: np.ndarray) -> float:
    first_values = first.astype(np.float32)
    second_values = second.astype(np.float32)
    constant_1 = (0.01 * 255) ** 2
    constant_2 = (0.03 * 255) ** 2
    mean_first = cv2.GaussianBlur(first_values, (11, 11), 1.5)
    mean_second = cv2.GaussianBlur(second_values, (11, 11), 1.5)
    variance_first = cv2.GaussianBlur(first_values * first_values, (11, 11), 1.5) - mean_first**2
    variance_second = cv2.GaussianBlur(second_values * second_values, (11, 11), 1.5) - mean_second**2
    covariance = cv2.GaussianBlur(first_values * second_values, (11, 11), 1.5) - mean_first * mean_second
    score = ((2 * mean_first * mean_second + constant_1) * (2 * covariance + constant_2)) / (
        (mean_first**2 + mean_second**2 + constant_1)
        * (variance_first + variance_second + constant_2)
    )
    return float(score.mean())


def load_images(dataset_dir: Path) -> list[dict]:
    records = []
    for path in sorted((dataset_dir / "raw").rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise RuntimeError(f"Unable to decode image: {path}")
        records.append(
            {
                "path": path,
                "relative_path": path.relative_to(dataset_dir).as_posix(),
                "sha256": file_sha256(path),
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "phash": perceptual_hash(image),
                "preview": normalized_preview(image),
            }
        )
    return records


def main() -> int:
    default_dataset = Path(__file__).resolve().parents[2] / "training_dataset"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=default_dataset)
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()

    records = load_images(args.dataset_dir.resolve())
    exact_groups = {}
    for record in records:
        exact_groups.setdefault(record["sha256"], []).append(record["relative_path"])
    exact_groups = [paths for paths in exact_groups.values() if len(paths) > 1]

    pairs = []
    for first_index, first in enumerate(records):
        for second in records[first_index + 1 :]:
            hamming = int(np.count_nonzero(first["phash"] != second["phash"]))
            if hamming > 20:
                continue
            pairs.append(
                {
                    "first": first["relative_path"],
                    "second": second["relative_path"],
                    "first_size": [first["width"], first["height"]],
                    "second_size": [second["width"], second["height"]],
                    "phash_distance": hamming,
                    "correlation": round(correlation(first["preview"], second["preview"]), 6),
                    "ssim": round(structural_similarity(first["preview"], second["preview"]), 6),
                }
            )

    pairs.sort(
        key=lambda pair: (
            pair["phash_distance"],
            -pair["ssim"],
            -pair["correlation"],
        )
    )
    report = {
        "images": len(records),
        "exact_duplicate_groups": exact_groups,
        "candidate_pairs": pairs[: max(0, args.top)],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
