"""Run board rectification, piece detection, and local classification on one image."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--min-confidence", type=float, default=0.50)
    parser.add_argument("--min-margin", type=float, default=0.10)
    parser.add_argument(
        "--skip-grid-roi-fusion",
        action="store_true",
        help="Classify all deduplicated Hough candidates before the grid/ROI gate.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parent
    worker_dir = root.parent / "local_oss_ai_worker"
    sys.path.insert(0, str(worker_dir))

    import worker  # pylint: disable=import-error,import-outside-toplevel

    worker.load_env_file(worker_dir / ".env")
    os.environ["CHESS_LOCAL_CLASSIFIER_MODEL_DIR"] = str(args.model_dir.resolve())
    os.environ["CHESS_LOCAL_CLASSIFIER_DEVICE"] = args.device
    os.environ["CHESS_LOCAL_CLASSIFIER_MIN_CONFIDENCE"] = str(args.min_confidence)
    os.environ["CHESS_LOCAL_CLASSIFIER_MIN_MARGIN"] = str(args.min_margin)

    import local_piece_classifier  # pylint: disable=import-error,import-outside-toplevel

    source_image_body = args.image.resolve().read_bytes()
    image_body, _, _, center_crop = worker.center_crop_image(source_image_body)
    ai_body, width, height, preprocessing = worker.rectify_board_image(image_body)
    preprocessing["center_crop"] = center_crop
    if width is None or height is None:
        with Image.open(io.BytesIO(ai_body)) as image:
            width, height = image.size

    board_crop, board_crop_metadata = worker.get_effective_board_crop(
        ai_body, width, height
    )
    raw_circles, circle_detection = worker.detect_piece_circles_fused(
        ai_body, board_crop=board_crop
    )
    provisional_orientation = {
        "bottom_side": worker.PLAYER1_SIDE,
        "top_side": worker.PLAYER2_SIDE,
    }
    circles = worker.annotate_circle_candidates_with_board_points(
        raw_circles,
        width,
        height,
        board_crop=board_crop,
        orientation=provisional_orientation,
    )
    if args.skip_grid_roi_fusion:
        grid_roi_fusion = {
            "enabled": False,
            "status": "skipped_by_test",
            "raw_count": len(circles),
            "accepted_count": len(circles),
            "rejected_count": 0,
        }
    else:
        circles, grid_roi_fusion = worker.apply_grid_roi_hough_fusion(
            ai_body, circles, board_crop=board_crop
        )
    visual_colors = worker.estimate_candidate_text_colors(ai_body, circles)
    orientation, orientation_detection = worker.infer_board_orientation_from_text_colors(
        circles,
        visual_colors,
        width,
        height,
        board_crop=board_crop,
    )
    circles = worker.annotate_circle_candidates_with_board_points(
        circles,
        width,
        height,
        board_crop=board_crop,
        orientation=orientation,
    )
    circles, rotated_ids = worker.apply_orientation_to_candidate_crops(
        circles, visual_colors, orientation
    )
    orientation_detection["rotated_crop_circle_ids"] = rotated_ids

    classifications, report = local_piece_classifier.classify_candidates(
        ai_body, circles
    )
    predictions = {
        int(item["circle_id"]): item for item in report.get("predictions", [])
    }
    results = []
    for circle in circles:
        circle_id = int(circle["circle_id"])
        prediction = predictions.get(circle_id, {})
        classification = classifications.get(circle_id)
        results.append(
            {
                "circle_id": circle_id,
                "board_x": circle.get("board_x"),
                "board_y": circle.get("board_y"),
                "cx": round(float(circle["cx"]), 2),
                "cy": round(float(circle["cy"]), 2),
                "radius": round(float(circle.get("radius", 0.0)), 2),
                "label": prediction.get("label"),
                "score": prediction.get("score"),
                "second_label": prediction.get("second_label"),
                "second_score": prediction.get("second_score"),
                "margin": prediction.get("margin"),
                "accepted": bool(prediction.get("accepted", False)),
                "character": classification.get("char") if classification else None,
            }
        )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "rectified.jpg").write_bytes(ai_body)
    crop_sheet = worker.create_candidate_crop_sheet(ai_body, circles)
    if crop_sheet:
        (output_dir / "candidates.jpg").write_bytes(crop_sheet)
    worker.save_candidate_crop_images(output_dir, "test", ai_body, circles)

    with Image.open(io.BytesIO(ai_body)) as source:
        overlay = source.convert("RGB")
    draw = ImageDraw.Draw(overlay)
    for circle in circles:
        circle_id = int(circle["circle_id"])
        prediction = predictions.get(circle_id, {})
        cx = float(circle["cx"])
        cy = float(circle["cy"])
        radius = float(circle.get("radius", 20.0))
        accepted = bool(prediction.get("accepted", False))
        color = (20, 210, 70) if accepted else (255, 120, 20)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=color,
            width=3,
        )
        label = prediction.get("label", "unclassified")
        score = float(prediction.get("score", 0.0) or 0.0)
        board_x = circle.get("board_x", "?")
        board_y = circle.get("board_y", "?")
        text = f"{circle_id} {label} {score:.2f} ({board_x},{board_y})"
        text_y = max(0, int(cy - radius - 15))
        draw.rectangle(
            (int(cx - radius), text_y, int(cx - radius) + 210, text_y + 14),
            fill=(250, 250, 250),
        )
        draw.text((int(cx - radius) + 2, text_y + 1), text, fill=color)
    overlay.save(output_dir / "overlay.jpg", quality=96, subsampling=0)

    payload = {
        "image": str(args.image.resolve()),
        "model_dir": str(args.model_dir.resolve()),
        "image_size": [width, height],
        "preprocessing": preprocessing,
        "board_crop": list(board_crop),
        "board_crop_detection": board_crop_metadata,
        "circle_detection": circle_detection,
        "grid_roi_fusion": grid_roi_fusion,
        "orientation": orientation,
        "orientation_detection": orientation_detection,
        "classifier": report,
        "pieces": results,
    }
    (output_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "detected": len(circles),
                "accepted": report.get("accepted_count", 0),
                "rejected": report.get("rejected_count", 0),
                "status": report.get("status"),
                "orientation": orientation,
                "pieces": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
