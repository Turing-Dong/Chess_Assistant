#!/usr/bin/env python3
"""Extract every cached Xiangqi piece crop into one flat directory.

This reuses the same board rectification, orientation, crop scale, resize, and
JPEG settings as ``build_piece_character_dataset.py``. It performs no online
classification and does not depend on the classified crop directories.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path

import build_piece_character_dataset as builder


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MANIFEST_FIELDS = (
    "image_path",
    "device_id",
    "frame_id",
    "circle_id",
    "source_image",
    "center_x",
    "center_y",
    "radius",
    "rotation_degrees",
    "crop_left",
    "crop_top",
    "crop_right",
    "crop_bottom",
    "source_sha256",
)


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    project_root = script_path.parents[2]
    worker_dir = script_path.parents[1] / "local_oss_ai_worker"
    dataset_dir = project_root / "training_dataset"
    piece_dataset = dataset_dir / "piece_character_dataset"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=dataset_dir)
    parser.add_argument("--worker-dir", type=Path, default=worker_dir)
    parser.add_argument("--debug-dir", type=Path, default=worker_dir / "debug")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--cache-dir", type=Path, default=piece_dataset / "recognition_cache")
    parser.add_argument("--output-dir", type=Path, default=piece_dataset / "images" / "all_images")
    parser.add_argument("--manifest", type=Path, default=piece_dataset / "all_images_manifest.csv")
    parser.add_argument("--image-size", type=int, default=160)
    parser.add_argument("--crop-scale", type=float, default=1.35)
    parser.add_argument("--jpeg-quality", type=int, default=96)
    return parser.parse_args()


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def main() -> None:
    args = parse_args()
    if args.image_size <= 0:
        raise ValueError("--image-size must be positive")
    if args.crop_scale <= 0:
        raise ValueError("--crop-scale must be positive")

    dataset_dir = args.dataset_dir.resolve()
    source_dir = dataset_dir / "raw" / args.device_id
    cache_dir = args.cache_dir.resolve()
    output_dir = args.output_dir.resolve()
    debug_dir = args.debug_dir.resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(source_dir)
    if not cache_dir.is_dir():
        raise FileNotFoundError(cache_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    worker = builder.load_worker(args.worker_dir.resolve())
    source_images = sorted(
        path for path in source_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    rows: list[dict] = []
    expected_names: set[str] = set()
    failures: list[dict] = []

    for source_index, source_path in enumerate(source_images, start=1):
        frame_id = source_path.stem
        try:
            image_body = source_path.read_bytes()
            source_sha256 = builder.sha256_bytes(image_body)
            cache_path = cache_dir / f"{frame_id}.json"
            cache = builder.load_cache(cache_path, source_sha256)
            if cache is None:
                raise RuntimeError(f"Missing or stale recognition cache: {cache_path}")

            image_source = cache.get("image_source")
            if image_source == "debug_rectified":
                ai_body = (debug_dir / f"{frame_id}.rectified.jpg").read_bytes()
            elif image_source == "raw":
                ai_body = image_body
            else:
                ai_body, _, _, _ = worker.rectify_board_image(image_body)

            image = worker.Image.open(io.BytesIO(ai_body)).convert("RGB")
            circles = cache.get("circles", [])
            for circle_index, circle in enumerate(circles):
                circle_id = int(circle.get("circle_id", circle_index))
                crop, box = builder.normalize_crop(
                    image,
                    circle,
                    args.crop_scale,
                    args.image_size,
                    worker,
                )
                if crop is None:
                    continue
                filename = f"{frame_id}__c{circle_id:03d}.jpg"
                if filename in expected_names:
                    raise RuntimeError(f"Duplicate output filename: {filename}")
                expected_names.add(filename)
                destination = output_dir / filename
                builder.save_crop(destination, crop, args.jpeg_quality)
                left, top, right, bottom = box
                rows.append(
                    {
                        "image_path": destination.relative_to(dataset_dir).as_posix(),
                        "device_id": args.device_id,
                        "frame_id": frame_id,
                        "circle_id": circle_id,
                        "source_image": source_path.relative_to(dataset_dir).as_posix(),
                        "center_x": round(float(circle["cx"]), 3),
                        "center_y": round(float(circle["cy"]), 3),
                        "radius": round(float(circle.get("radius", 0.0)), 3),
                        "rotation_degrees": int(circle.get("crop_rotation_degrees", 0) or 0),
                        "crop_left": left,
                        "crop_top": top,
                        "crop_right": right,
                        "crop_bottom": bottom,
                        "source_sha256": source_sha256,
                    }
                )
            print(f"[{source_index}/{len(source_images)}] {frame_id}: crops={len(circles)}", flush=True)
        except Exception as exc:
            failures.append({"frame_id": frame_id, "error": str(exc)})
            print(f"[{source_index}/{len(source_images)}] {frame_id}: ERROR {exc}", flush=True)

    write_manifest(args.manifest.resolve(), rows)
    existing_names = {path.name for path in output_dir.glob("*.jpg")}
    summary = {
        "status": "ok" if not failures else "partial_failure",
        "source_images": len(source_images),
        "crops": len(rows),
        "output_images": len(existing_names),
        "unexpected_existing_images": sorted(existing_names - expected_names),
        "failures": failures,
        "output_dir": str(output_dir),
        "manifest": str(args.manifest.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    raise SystemExit(0 if not failures and not summary["unexpected_existing_images"] else 2)


if __name__ == "__main__":
    main()
