#!/usr/bin/env python3
"""Build a fixed-size, auto-annotated Xiangqi piece character dataset."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


CACHE_VERSION = 2
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VALID_LABELS = (
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
)
VALID_LABEL_SET = set(VALID_LABELS)
LABEL_CHARACTERS = {
    "red_shuai": "帅",
    "black_jiang": "将",
    "red_shi": "仕",
    "black_shi": "士",
    "red_xiang": "相",
    "black_xiang": "象",
    "red_ma": "马",
    "black_ma": "馬",
    "red_che": "车",
    "black_che": "車",
    "red_pao": "炮",
    "black_pao": "砲",
    "red_bing": "兵",
    "black_zu": "卒",
}
CSV_FIELDS = (
    "image_path",
    "partition",
    "label",
    "character",
    "piece_name",
    "piece_color",
    "annotation_status",
    "annotation_source",
    "device_id",
    "frame_id",
    "circle_id",
    "board_x",
    "board_y",
    "center_x",
    "center_y",
    "radius",
    "rotation_degrees",
    "crop_left",
    "crop_top",
    "crop_right",
    "crop_bottom",
    "source_image",
    "source_sha256",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    project_root = script_path.parents[2]
    worker_dir = script_path.parents[1] / "local_oss_ai_worker"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=project_root / "training_dataset",
    )
    parser.add_argument(
        "--worker-dir",
        type=Path,
        default=worker_dir,
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=worker_dir / ".env",
    )
    parser.add_argument(
        "--debug-dir",
        type=Path,
        default=worker_dir / "debug",
        help="Existing Worker result directory used before making any AI request.",
    )
    parser.add_argument("--device-id")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--image-size", type=int)
    parser.add_argument("--refresh", action="store_true", help="Ignore cached recognition data.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use cached recognition only; uncached crops are sent to needs_review/unlabeled.",
    )
    parser.add_argument("--max-images", type=int, help="Process at most this many source images.")
    return parser.parse_args()


def load_worker(worker_dir: Path):
    worker_path = worker_dir / "worker.py"
    if not worker_path.exists():
        raise RuntimeError(f"Worker script not found: {worker_path}")
    sys.path.insert(0, str(worker_dir))
    import worker  # pylint: disable=import-error,import-outside-toplevel

    return worker


def crop_box(image, circle: dict, crop_scale: float) -> tuple[int, int, int, int]:
    center_x = float(circle["cx"])
    center_y = float(circle["cy"])
    radius = max(float(circle.get("radius", 20.0)), 12.0)
    half = radius * crop_scale
    return (
        max(0, int(round(center_x - half))),
        max(0, int(round(center_y - half))),
        min(image.width, int(round(center_x + half))),
        min(image.height, int(round(center_y + half))),
    )


def normalize_crop(image, circle: dict, crop_scale: float, image_size: int, worker):
    box = crop_box(image, circle, crop_scale)
    left, top, right, bottom = box
    if right <= left or bottom <= top:
        return None, box
    crop = image.crop(box).convert("RGB")
    rotation = int(circle.get("crop_rotation_degrees", 0) or 0) % 360
    if rotation == 180:
        transpose = getattr(worker.Image, "Transpose", worker.Image)
        crop = crop.transpose(transpose.ROTATE_180)
    resampling = getattr(getattr(worker.Image, "Resampling", worker.Image), "LANCZOS", worker.Image.BICUBIC)
    crop = crop.resize((image_size, image_size), resample=resampling)
    return crop, box


def classification_label(classification: dict, worker) -> tuple[str, str, str, str]:
    if not classification:
        return "", "", "unknown", "unknown"
    character = str(classification.get("char", "")).strip()
    name = worker.CHARACTER_NAME_HINTS.get(
        character,
        worker.validate_piece_name(classification.get("name", "unknown")),
    )
    color = worker.CHARACTER_COLOR_HINTS.get(
        character,
        worker.validate_piece_color(classification.get("color", "unknown")),
    )
    label = f"{color}_{name}"
    if label not in VALID_LABEL_SET:
        label = ""
    return label, character, name, color


def load_existing_worker_result(debug_dir: Path, frame_id: str, worker) -> dict | None:
    result_path = debug_dir / f"{frame_id}.result.json"
    if not result_path.exists():
        return None
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    circles = result.get("detected_piece_circles")
    if not isinstance(circles, list) or not circles:
        return None

    rotated_ids = set(
        result.get("orientation_detection", {}).get("rotated_crop_circle_ids", [])
    )
    normalized_circles = []
    for source_circle in circles:
        circle = dict(source_circle)
        circle_id = int(circle.get("circle_id", len(normalized_circles)))
        circle["crop_rotation_degrees"] = 180 if circle_id in rotated_ids else int(
            circle.get("crop_rotation_degrees", 0) or 0
        )
        normalized_circles.append(circle)

    classifications = {}
    for piece in result.get("pieces", []):
        if piece.get("circle_id") is None:
            continue
        circle_id = int(piece["circle_id"])
        name = worker.validate_piece_name(piece.get("name", "unknown"))
        color = worker.validate_piece_color(piece.get("color", "unknown"))
        label = f"{color}_{name}"
        classifications[str(circle_id)] = {
            "char": LABEL_CHARACTERS.get(label, ""),
            "name": name,
            "color": color,
            "annotation_origin": "existing_worker_result",
            "result_status": result.get("status", "unknown"),
            "color_classification_status": piece.get("color_classification_status", ""),
            "color_confidence": piece.get("color_confidence", ""),
        }

    return {
        "result_path": str(result_path),
        "image_source": "debug_rectified"
        if (debug_dir / f"{frame_id}.rectified.jpg").exists()
        else "raw",
        "ai_width": result.get("image_width"),
        "ai_height": result.get("image_height"),
        "preprocessing": result.get("image_preprocessing", {}),
        "board_crop_detection": result.get("board_crop_detection", {}),
        "circle_detection": result.get("circle_detection", {}),
        "grid_roi_hough_fusion": result.get("circle_detection", {}).get(
            "grid_roi_hough_fusion", {}
        ),
        "orientation": result.get("orientation", {}),
        "orientation_detection": result.get("orientation_detection", {}),
        "circles": normalized_circles,
        "classifications": classifications,
    }


def detect_and_classify(image_body: bytes, worker, api_key: str | None, offline: bool) -> dict:
    original = worker.Image.open(io.BytesIO(image_body)).convert("RGB")
    ai_body, ai_width, ai_height, preprocessing = worker.rectify_board_image(image_body)
    if ai_width is None or ai_height is None:
        ai_width, ai_height = original.size
    board_crop, board_crop_metadata = worker.get_effective_board_crop(
        ai_body,
        ai_width,
        ai_height,
    )
    raw_circles, circle_detection = worker.detect_piece_circles_fused(
        ai_body,
        board_crop=board_crop,
    )
    provisional_orientation = {
        "bottom_side": worker.PLAYER1_SIDE,
        "top_side": worker.PLAYER2_SIDE,
    }
    circles = worker.annotate_circle_candidates_with_board_points(
        raw_circles,
        ai_width,
        ai_height,
        board_crop=board_crop,
        orientation=provisional_orientation,
    )
    circles, grid_roi_fusion = worker.apply_grid_roi_hough_fusion(
        ai_body,
        circles,
        board_crop=board_crop,
    )
    visual_colors = worker.estimate_candidate_text_colors(ai_body, circles)
    orientation, orientation_detection = worker.infer_board_orientation_from_text_colors(
        circles,
        visual_colors,
        ai_width,
        ai_height,
        board_crop=board_crop,
    )
    circles = worker.annotate_circle_candidates_with_board_points(
        circles,
        ai_width,
        ai_height,
        board_crop=board_crop,
        orientation=orientation,
    )
    circles, rotated_ids = worker.apply_orientation_to_candidate_crops(
        circles,
        visual_colors,
        orientation,
    )
    orientation_detection["rotated_crop_circle_ids"] = rotated_ids
    classifications = {}
    if not offline:
        if not api_key:
            raise RuntimeError("CHESS_AI_API_KEY is required unless --offline is used")
        crop_sheet = worker.create_candidate_crop_sheet(ai_body, circles)
        classifications = worker.call_ai_candidate_classifier(
            crop_sheet,
            circles,
            api_key,
        )
        worker.reject_character_color_conflicts(classifications, visual_colors)
        worker.apply_character_names_to_classifications(classifications)
        worker.apply_character_text_colors_to_classifications(classifications)
        worker.calibrate_visual_text_colors_with_character_anchors(
            classifications,
            visual_colors,
        )
        worker.apply_visual_text_colors_to_classifications(classifications, visual_colors)

    return {
        "ai_image_body": ai_body,
        "ai_width": ai_width,
        "ai_height": ai_height,
        "preprocessing": preprocessing,
        "board_crop_detection": board_crop_metadata,
        "circle_detection": circle_detection,
        "grid_roi_hough_fusion": grid_roi_fusion,
        "orientation": orientation,
        "orientation_detection": orientation_detection,
        "circles": circles,
        "classifications": {str(key): value for key, value in classifications.items()},
    }


def write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_cache(path: Path, source_sha256: str) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        value.get("cache_version") != CACHE_VERSION
        or value.get("source_sha256") != source_sha256
    ):
        return None
    return value


def save_crop(path: Path, crop, jpeg_quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(path, format="JPEG", quality=jpeg_quality, subsampling=0)


def write_annotations(output_dir: Path, rows: list[dict]) -> None:
    csv_path = output_dir / "annotations.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    jsonl_path = output_dir / "annotations.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_dataset_info(
    output_dir: Path,
    device_id: str,
    image_size: int,
    jpeg_quality: int,
    crop_scale: float,
    rows: list[dict],
    source_count: int,
    failures: list[dict],
) -> None:
    auto_count = sum(row["partition"] == "auto_labeled" for row in rows)
    review_count = len(rows) - auto_count
    class_counts = {
        label: sum(
            row["partition"] == "auto_labeled" and row["label"] == label
            for row in rows
        )
        for label in VALID_LABELS
    }
    info = {
        "version": 1,
        "generated_at": utc_now(),
        "device_id": device_id,
        "source_image_count": source_count,
        "crop_count": len(rows),
        "auto_labeled_count": auto_count,
        "needs_review_count": review_count,
        "image": {
            "width": image_size,
            "height": image_size,
            "mode": "RGB",
            "format": "JPEG",
            "jpeg_quality": jpeg_quality,
            "jpeg_subsampling": 0,
            "crop_scale": crop_scale,
            "orientation": "worker crop_rotation_degrees",
        },
        "classes": {label: index for index, label in enumerate(VALID_LABELS)},
        "class_counts": class_counts,
        "failures": failures,
    }
    (output_dir / "dataset_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    readme = f"""# Xiangqi piece character dataset

- Device: `{device_id}`
- Source board images: {source_count}
- Output crops: {len(rows)}
- Auto-labeled crops: {auto_count}
- Needs review: {review_count}
- Image format: RGB JPEG, {image_size}x{image_size}, quality {jpeg_quality}, subsampling 0
- Crop scale: {crop_scale} times the detected piece radius

`images/auto_labeled/<class>/` contains pseudo-labels produced by the Worker's
single-piece character classifier. `images/needs_review/` contains missing,
invalid, or rejected classifications. Review pseudo-labels before treating them
as ground truth. Machine-readable annotations are in `annotations.csv` and
`annotations.jsonl`.
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.max_images is not None and args.max_images <= 0:
        raise RuntimeError("--max-images must be greater than zero")

    worker_dir = args.worker_dir.resolve()
    debug_dir = args.debug_dir.resolve()
    worker = load_worker(worker_dir)
    worker.load_env_file(args.env_file.resolve())
    device_id = args.device_id or os.getenv("DEVICE_ID")
    if not device_id:
        configured = worker.parse_device_id_list(os.getenv("DEVICE_IDS", ""))
        if len(configured) == 1:
            device_id = configured[0]
    if not device_id:
        raise RuntimeError("Specify --device-id or configure exactly one DEVICE_IDS value")

    dataset_dir = args.dataset_dir.resolve()
    source_dir = dataset_dir / "raw" / device_id
    output_dir = (args.output_dir or dataset_dir / "piece_character_dataset").resolve()
    cache_dir = output_dir / "recognition_cache"
    if not source_dir.exists():
        raise RuntimeError(f"Source image directory not found: {source_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    image_size = args.image_size or int(os.getenv("CHESS_CROP_SHEET_TILE_SIZE", "160"))
    if image_size <= 0:
        raise RuntimeError("--image-size must be greater than zero")
    crop_scale = float(os.getenv("CHESS_CROP_SHEET_SCALE", "1.35"))
    jpeg_quality = int(os.getenv("CHESS_CROP_SHEET_JPEG_QUALITY", "96"))
    api_key = os.getenv(os.getenv("CHESS_AI_API_KEY_ENV", "CHESS_AI_API_KEY"))

    source_images = sorted(
        path for path in source_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    if args.max_images is not None:
        source_images = source_images[: args.max_images]

    rows = []
    failures = []
    for source_index, source_path in enumerate(source_images, start=1):
        frame_id = source_path.stem
        image_body = source_path.read_bytes()
        source_hash = sha256_bytes(image_body)
        cache_path = cache_dir / f"{frame_id}.json"
        cache = None if args.refresh else load_cache(cache_path, source_hash)
        try:
            if cache is None:
                existing = load_existing_worker_result(debug_dir, frame_id, worker)
                analysis = existing or detect_and_classify(
                    image_body,
                    worker,
                    api_key,
                    args.offline,
                )
                cache = {
                    "cache_version": CACHE_VERSION,
                    "generated_at": utc_now(),
                    "device_id": device_id,
                    "frame_id": frame_id,
                    "source_image": source_path.relative_to(dataset_dir).as_posix(),
                    "source_sha256": source_hash,
                    "image_source": analysis.get("image_source", "current_rectified"),
                    "ai_width": analysis["ai_width"],
                    "ai_height": analysis["ai_height"],
                    "preprocessing": analysis["preprocessing"],
                    "board_crop_detection": analysis["board_crop_detection"],
                    "circle_detection": analysis["circle_detection"],
                    "grid_roi_hough_fusion": analysis["grid_roi_hough_fusion"],
                    "orientation": analysis["orientation"],
                    "orientation_detection": analysis["orientation_detection"],
                    "circles": analysis["circles"],
                    "classifications": analysis["classifications"],
                }
                write_cache(cache_path, cache)
                if existing and analysis["image_source"] == "debug_rectified":
                    ai_body = (debug_dir / f"{frame_id}.rectified.jpg").read_bytes()
                elif existing:
                    ai_body = image_body
                else:
                    ai_body = analysis["ai_image_body"]
            else:
                if cache.get("image_source") == "debug_rectified":
                    ai_body = (debug_dir / f"{frame_id}.rectified.jpg").read_bytes()
                elif cache.get("image_source") == "raw":
                    ai_body = image_body
                else:
                    ai_body, _, _, _ = worker.rectify_board_image(image_body)

            image = worker.Image.open(io.BytesIO(ai_body)).convert("RGB")
            classifications = cache.get("classifications", {})
            for circle_index, circle in enumerate(cache.get("circles", [])):
                circle_id = int(circle.get("circle_id", circle_index))
                classification = classifications.get(str(circle_id), {})
                label, character, piece_name, piece_color = classification_label(
                    classification,
                    worker,
                )
                rejected = bool(classification.get("character_rejected"))
                character_is_known = character in worker.CHARACTER_NAME_HINTS
                weak_color_status = classification.get("color_classification_status") in {
                    "low_confidence",
                    "unresolved",
                }
                failed_result = classification.get("result_status") not in {None, "", "ok"}
                auto_labeled = bool(
                    label
                    and character_is_known
                    and not rejected
                    and not weak_color_status
                    and not failed_result
                )
                partition = "auto_labeled" if auto_labeled else "needs_review"
                folder_label = label or "unlabeled"
                filename = f"{frame_id}__c{circle_id:03d}.jpg"
                relative_crop = Path("images") / partition / folder_label / filename
                crop, box = normalize_crop(
                    image,
                    circle,
                    crop_scale,
                    image_size,
                    worker,
                )
                if crop is None:
                    continue
                save_crop(output_dir / relative_crop, crop, jpeg_quality)
                left, top, right, bottom = box
                rows.append(
                    {
                        "image_path": relative_crop.as_posix(),
                        "partition": partition,
                        "label": label,
                        "character": character,
                        "piece_name": piece_name,
                        "piece_color": piece_color,
                        "annotation_status": "pseudo_label" if auto_labeled else "needs_review",
                        "annotation_source": classification.get(
                            "annotation_origin",
                            "worker_candidate_classifier" if classification else "none",
                        ),
                        "device_id": device_id,
                        "frame_id": frame_id,
                        "circle_id": circle_id,
                        "board_x": circle.get("board_x", ""),
                        "board_y": circle.get("board_y", ""),
                        "center_x": round(float(circle["cx"]), 3),
                        "center_y": round(float(circle["cy"]), 3),
                        "radius": round(float(circle.get("radius", 0.0)), 3),
                        "rotation_degrees": int(circle.get("crop_rotation_degrees", 0) or 0),
                        "crop_left": left,
                        "crop_top": top,
                        "crop_right": right,
                        "crop_bottom": bottom,
                        "source_image": source_path.relative_to(dataset_dir).as_posix(),
                        "source_sha256": source_hash,
                    }
                )
            print(
                f"[{source_index}/{len(source_images)}] {frame_id}: "
                f"circles={len(cache.get('circles', []))} "
                f"classified={len(classifications)}",
                flush=True,
            )
        except Exception as exc:  # Continue so a transient API failure is resumable.
            failures.append({"frame_id": frame_id, "error": str(exc)})
            print(f"[{source_index}/{len(source_images)}] {frame_id}: ERROR {exc}", file=sys.stderr, flush=True)

    rows.sort(key=lambda row: (row["frame_id"], int(row["circle_id"])))
    write_annotations(output_dir, rows)
    write_dataset_info(
        output_dir,
        device_id,
        image_size,
        jpeg_quality,
        crop_scale,
        rows,
        len(source_images),
        failures,
    )
    summary = {
        "status": "ok" if not failures else "partial_failure",
        "device_id": device_id,
        "source_images": len(source_images),
        "crops": len(rows),
        "auto_labeled": sum(row["partition"] == "auto_labeled" for row in rows),
        "needs_review": sum(row["partition"] == "needs_review" for row in rows),
        "failures": failures,
        "output_dir": str(output_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
