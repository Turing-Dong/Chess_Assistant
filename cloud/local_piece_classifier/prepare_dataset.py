"""Validate the manually classified crops and create leakage-safe PaddleClas lists."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
FRAME_RE = re.compile(r"^(?P<frame>.+)__c\d+$")


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=repo_root
        / "training_dataset"
        / "piece_character_dataset"
        / "images"
        / "needs_review",
    )
    parser.add_argument("--classes", type=Path, default=script_dir / "classes.txt")
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "training_dataset" / "piece_character_dataset" / "paddleclas",
    )
    parser.add_argument("--val-ratio", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--search-iterations", type=int, default=50000)
    return parser.parse_args()


def frame_id(path: Path) -> str:
    match = FRAME_RE.match(path.stem)
    return match.group("frame") if match else path.stem


def score_split(
    val_frames: set[str],
    grouped: dict[str, list[tuple[Path, int]]],
    totals: Counter[int],
    val_ratio: float,
) -> float:
    val_counts: Counter[int] = Counter()
    val_total = 0
    for frame in val_frames:
        for _, label in grouped[frame]:
            val_counts[label] += 1
            val_total += 1

    total_images = sum(totals.values())
    score = 8.0 * abs(val_total / total_images - val_ratio)
    for label, total in totals.items():
        count = val_counts[label]
        if count == 0 or count == total:
            score += 100.0
        score += abs(count / total - val_ratio)
    return score


def choose_validation_frames(
    grouped: dict[str, list[tuple[Path, int]]],
    totals: Counter[int],
    val_ratio: float,
    seed: int,
    iterations: int,
) -> set[str]:
    frames = sorted(grouped)
    rng = random.Random(seed)
    target_groups = max(1, round(len(frames) * val_ratio))
    candidate_sizes = sorted(
        {max(1, target_groups + delta) for delta in range(-2, 3) if target_groups + delta < len(frames)}
    )
    best: tuple[float, tuple[str, ...]] | None = None
    for _ in range(iterations):
        size = rng.choice(candidate_sizes)
        candidate = tuple(sorted(rng.sample(frames, size)))
        value = score_split(set(candidate), grouped, totals, val_ratio)
        if best is None or (value, candidate) < best:
            best = (value, candidate)
    assert best is not None
    return set(best[1])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    classes = [line.strip() for line in args.classes.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not 0.0 < args.val_ratio < 1.0:
        raise ValueError("--val-ratio must be between 0 and 1")
    if not source.is_dir():
        raise FileNotFoundError(source)

    unknown_dirs = sorted(p.name for p in source.iterdir() if p.is_dir() and p.name not in classes)
    if unknown_dirs:
        raise ValueError(f"Unknown class directories: {unknown_dirs}")

    samples: list[tuple[Path, int, str]] = []
    hashes: dict[str, Path] = {}
    duplicate_files: list[tuple[str, str]] = []
    for label, class_name in enumerate(classes):
        class_dir = source / class_name
        if not class_dir.is_dir():
            raise FileNotFoundError(f"Missing class directory: {class_dir}")
        files = sorted(p for p in class_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
        if not files:
            raise ValueError(f"Class has no images: {class_name}")
        for path in files:
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    if image.mode != "RGB" or image.size != (160, 160):
                        raise ValueError(f"Expected RGB 160x160, got {image.mode} {image.size}: {path}")
            except Exception as exc:
                raise ValueError(f"Invalid image {path}: {exc}") from exc
            digest = sha256(path)
            if digest in hashes:
                duplicate_files.append((str(hashes[digest]), str(path)))
            else:
                hashes[digest] = path
            samples.append((path, label, frame_id(path)))

    grouped: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    totals: Counter[int] = Counter()
    for path, label, frame in samples:
        grouped[frame].append((path, label))
        totals[label] += 1

    val_frames = choose_validation_frames(
        grouped, totals, args.val_ratio, args.seed, args.search_iterations
    )
    train = [(path, label, frame) for path, label, frame in samples if frame not in val_frames]
    val = [(path, label, frame) for path, label, frame in samples if frame in val_frames]

    train_frames = {frame for _, _, frame in train}
    if train_frames & val_frames:
        raise RuntimeError("Frame leakage detected")

    output.mkdir(parents=True, exist_ok=True)
    image_root = source
    for name, split in (("train_list.txt", train), ("val_list.txt", val)):
        lines = [f"{path.relative_to(image_root).as_posix()} {label}" for path, label, _ in split]
        (output / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    class_counts = {
        split_name: {
            classes[label]: sum(1 for _, item_label, _ in split if item_label == label)
            for label in range(len(classes))
        }
        for split_name, split in (("train", train), ("val", val))
    }
    summary = {
        "source": str(source),
        "image_root": str(image_root),
        "seed": args.seed,
        "requested_val_ratio": args.val_ratio,
        "images": {"total": len(samples), "train": len(train), "val": len(val)},
        "frames": {
            "total": len(grouped),
            "train": len(train_frames),
            "val": len(val_frames),
            "validation_ids": sorted(val_frames),
        },
        "classes": {name: index for index, name in enumerate(classes)},
        "class_counts": class_counts,
        "duplicate_file_pairs": duplicate_files,
    }
    (output / "split_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    label_lines = [f"{index} {name}" for index, name in enumerate(classes)]
    (output / "label_list.txt").write_text("\n".join(label_lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
