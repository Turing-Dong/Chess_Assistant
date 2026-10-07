"""Create a deterministic, class-balanced, physically augmented train set."""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    dataset_root = project_root / "training_dataset2" / "piece_character_dataset"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image-root",
        type=Path,
        default=dataset_root / "images" / "trainable",
    )
    parser.add_argument(
        "--train-list",
        type=Path,
        default=dataset_root / "paddleclas" / "train_list.txt",
    )
    parser.add_argument(
        "--label-list",
        type=Path,
        default=dataset_root / "paddleclas" / "label_list.txt",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=dataset_root / "images" / "augmented_train_v1",
    )
    parser.add_argument(
        "--output-list-dir",
        type=Path,
        default=dataset_root / "paddleclas_augmented_v1",
    )
    parser.add_argument("--target-per-class", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261006)
    return parser.parse_args()


def rotate(image: np.ndarray, rng: random.Random) -> tuple[np.ndarray, dict]:
    height, width = image.shape[:2]
    angle = rng.uniform(-180.0, 180.0)
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    result = cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    return result, {"rotation_degrees": round(angle, 3)}


def perspective(image: np.ndarray, rng: random.Random) -> tuple[np.ndarray, dict]:
    height, width = image.shape[:2]
    ratio = rng.uniform(0.025, 0.060)
    limit = ratio * min(width, height)
    source = np.float32(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    )
    offsets = np.asarray(
        [[rng.uniform(-limit, limit), rng.uniform(-limit, limit)] for _ in range(4)],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(source, source + offsets)
    result = cv2.warpPerspective(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    return result, {"perspective_ratio": round(ratio, 4)}


def blur(image: np.ndarray, rng: random.Random) -> tuple[np.ndarray, dict]:
    if rng.random() < 0.5:
        kernel = rng.choice((3, 5))
        return cv2.GaussianBlur(image, (kernel, kernel), 0), {
            "blur": "gaussian",
            "kernel": kernel,
        }
    kernel = rng.choice((3, 5))
    angle = rng.uniform(-35.0, 35.0)
    matrix = cv2.getRotationMatrix2D((kernel / 2.0, kernel / 2.0), angle, 1.0)
    motion_kernel = np.diag(np.ones(kernel, dtype=np.float32))
    motion_kernel = cv2.warpAffine(motion_kernel, matrix, (kernel, kernel))
    motion_kernel /= max(float(motion_kernel.sum()), 1.0)
    return cv2.filter2D(image, -1, motion_kernel), {
        "blur": "motion",
        "kernel": kernel,
        "motion_angle": round(angle, 3),
    }


def glare(image: np.ndarray, rng: random.Random) -> tuple[np.ndarray, dict]:
    height, width = image.shape[:2]
    mask = np.zeros((height, width), dtype=np.float32)
    center = (rng.randint(width // 6, 5 * width // 6), rng.randint(height // 6, 5 * height // 6))
    axes = (
        rng.randint(max(4, width // 12), max(5, width // 3)),
        rng.randint(max(3, height // 18), max(4, height // 5)),
    )
    ellipse_angle = rng.uniform(0.0, 180.0)
    cv2.ellipse(mask, center, axes, ellipse_angle, 0, 360, 1.0, -1)
    sigma = max(1.0, min(width, height) * rng.uniform(0.025, 0.055))
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=sigma, sigmaY=sigma)
    alpha_value = rng.uniform(0.16, 0.30)
    alpha = (mask * alpha_value)[..., None]
    result = np.clip(
        image.astype(np.float32) * (1.0 - alpha) + 255.0 * alpha,
        0,
        255,
    ).astype(np.uint8)
    return result, {
        "glare_alpha": round(alpha_value, 4),
        "glare_center": list(center),
        "glare_axes": list(axes),
        "glare_angle": round(ellipse_angle, 3),
    }


RECIPES = (
    ("rotation",),
    ("perspective",),
    ("blur",),
    ("glare",),
    ("rotation", "perspective"),
    ("rotation", "blur"),
    ("perspective", "glare"),
    ("rotation", "perspective", "blur", "glare"),
)


def augment(image: np.ndarray, recipe: tuple[str, ...], rng: random.Random) -> tuple[np.ndarray, dict]:
    result = image.copy()
    metadata: dict[str, object] = {"recipe": list(recipe)}
    operations = {
        "rotation": rotate,
        "perspective": perspective,
        "blur": blur,
        "glare": glare,
    }
    for name in recipe:
        result, operation_metadata = operations[name](result, rng)
        metadata.update(operation_metadata)
    return result, metadata


def main() -> None:
    args = parse_args()
    if args.target_per_class <= 0:
        raise ValueError("--target-per-class must be positive")
    image_root = args.image_root.resolve()
    output_dir = args.output_dir.resolve()
    output_list_dir = args.output_list_dir.resolve()
    if output_dir.exists() or output_list_dir.exists():
        raise FileExistsError(
            "Output already exists; choose a new versioned output path: "
            f"{output_dir} or {output_list_dir}"
        )

    labels: dict[int, str] = {}
    for raw_line in args.label_list.read_text(encoding="utf-8").splitlines():
        class_id_text, label = raw_line.split(maxsplit=1)
        labels[int(class_id_text)] = label

    by_class: dict[int, list[tuple[Path, str]]] = defaultdict(list)
    for line_number, raw_line in enumerate(
        args.train_list.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line:
            continue
        relative, class_id_text = line.rsplit(maxsplit=1)
        class_id = int(class_id_text)
        source = image_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"Line {line_number}: {source}")
        if class_id not in labels:
            raise ValueError(f"Line {line_number}: unknown class id {class_id}")
        by_class[class_id].append((source, relative))

    if set(by_class) != set(labels):
        raise ValueError("Training list does not contain every class")

    output_dir.mkdir(parents=True)
    output_list_dir.mkdir(parents=True)
    rng = random.Random(args.seed)
    manifest_rows: list[dict[str, object]] = []
    output_list: list[str] = []
    output_counts: Counter[str] = Counter()

    for class_id in sorted(labels):
        label = labels[class_id]
        class_dir = output_dir / label
        class_dir.mkdir()
        sources = sorted(by_class[class_id], key=lambda item: item[1])

        for index, (source, relative) in enumerate(sources):
            suffix = source.suffix.lower() if source.suffix else ".jpg"
            output_name = f"orig_{index:04d}_{source.stem}{suffix}"
            destination = class_dir / output_name
            shutil.copy2(source, destination)
            output_relative = f"{label}/{output_name}"
            output_list.append(f"{output_relative} {class_id}")
            output_counts[label] += 1
            manifest_rows.append(
                {
                    "output": output_relative,
                    "class_id": class_id,
                    "label": label,
                    "source": relative,
                    "kind": "original",
                    "recipe": "original",
                    "parameters": "{}",
                }
            )

        needed = max(0, args.target_per_class - len(sources))
        for index in range(needed):
            source, relative = sources[index % len(sources)]
            with Image.open(source) as image:
                source_array = np.asarray(image.convert("RGB"), dtype=np.uint8)
            recipe = RECIPES[index % len(RECIPES)]
            augmented, metadata = augment(source_array, recipe, rng)
            output_name = f"aug_{index:04d}_{source.stem}.jpg"
            output_relative = f"{label}/{output_name}"
            Image.fromarray(augmented).save(
                class_dir / output_name,
                format="JPEG",
                quality=95,
                subsampling=0,
            )
            output_list.append(f"{output_relative} {class_id}")
            output_counts[label] += 1
            manifest_rows.append(
                {
                    "output": output_relative,
                    "class_id": class_id,
                    "label": label,
                    "source": relative,
                    "kind": "augmented",
                    "recipe": "+".join(recipe),
                    "parameters": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                }
            )

    (output_list_dir / "train_list.txt").write_text(
        "\n".join(output_list) + "\n", encoding="utf-8"
    )
    shutil.copy2(args.label_list.resolve(), output_list_dir / "label_list.txt")
    with (output_list_dir / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest_rows[0]))
        writer.writeheader()
        writer.writerows(manifest_rows)

    summary = {
        "source_image_root": str(image_root),
        "source_train_list": str(args.train_list.resolve()),
        "output_image_root": str(output_dir),
        "seed": args.seed,
        "target_per_class": args.target_per_class,
        "source_count": sum(len(items) for items in by_class.values()),
        "output_count": len(output_list),
        "augmented_count": sum(1 for row in manifest_rows if row["kind"] == "augmented"),
        "source_class_counts": {
            labels[class_id]: len(by_class[class_id]) for class_id in sorted(labels)
        },
        "output_class_counts": dict(sorted(output_counts.items())),
    }
    (output_list_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
