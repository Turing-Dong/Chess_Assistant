import argparse
import base64
import io
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import queue
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import oss2

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

try:
    import cv2
except ImportError:
    cv2 = None


DEFAULT_REGION = "cn-hangzhou"
DEFAULT_AI_PROVIDER = "openai"
DEFAULT_AI_MODEL = "gpt-5.6"
DEFAULT_QWEN_MODEL = "qwen3-vl-plus"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
QWEN_COMPAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
BOARD_FILES = 9
BOARD_RANKS = 10
AI_MAX_ATTEMPTS = 2
AI_REVIEW_MAX_ATTEMPTS = 1
DEFAULT_BOARD_MARGIN_X = 0.06
DEFAULT_BOARD_MARGIN_TOP = 0.08
DEFAULT_BOARD_MARGIN_BOTTOM = 0.08
# The playable 9x10 intersection frame is inset from the detected outer board
# frame. These defaults match the canonical rectified board layout.
DEFAULT_BOARD_GRID_LEFT = 0.117
DEFAULT_BOARD_GRID_RIGHT = 0.878
DEFAULT_BOARD_GRID_TOP = 0.127
DEFAULT_BOARD_GRID_BOTTOM = 0.913
GRID_CROP_FALLBACK_SOURCE = "default_grid_crop"
DEFAULT_MAX_VISIBLE_PIECES = 32
DEFAULT_RECTIFIED_BOARD_WIDTH = 1080
DEFAULT_RECTIFIED_BOARD_HEIGHT = 1215
PLAYER1_SIDE = "shuai"
PLAYER2_SIDE = "jiang"
# These characters identify the text color directly, even when the crop-level
# visual color estimate is weak or affected by lighting and compression.
CHARACTER_COLOR_HINTS = {
    "相": "red",
    "帅": "red",
    "帥": "red",
    "仕": "red",
    "兵": "red",
    "象": "black",
    "将": "black",
    "將": "black",
    "士": "black",
    "卒": "black",
}
COLOR_ANCHOR_CHARACTER_HINTS = {
    "相": "red",
    "帅": "red",
    "帥": "red",
    "象": "black",
    "将": "black",
    "將": "black",
}
CHARACTER_NAME_HINTS = {
    "帅": "shuai",
    "帥": "shuai",
    "将": "jiang",
    "將": "jiang",
    "士": "shi",
    "仕": "shi",
    "相": "xiang",
    "象": "xiang",
    "马": "ma",
    "馬": "ma",
    "车": "che",
    "車": "che",
    "炮": "pao",
    "砲": "pao",
    "兵": "bing",
    "卒": "zu",
}
PIECE_NAMES = {"shuai", "jiang", "shi", "xiang", "ma", "che", "pao", "bing", "zu"}
PIECE_COLORS = {"red", "black", "unknown"}
ENGINE_PIECE_LETTERS = {
    "shuai": "k",
    "jiang": "k",
    "shi": "a",
    "xiang": "b",
    "ma": "n",
    "che": "r",
    "pao": "c",
    "bing": "p",
    "zu": "p",
}
ENGINE_FILES = "abcdefghi"

LOGGER = logging.getLogger("oss_ai_worker")
SHOULD_STOP = False


def _handle_stop(signum, frame):
    del signum, frame
    global SHOULD_STOP
    SHOULD_STOP = True


def parse_args():
    parser = argparse.ArgumentParser(
        description="Poll Aliyun OSS manifests, run local AI, and publish ESP32 result JSON."
    )
    parser.add_argument(
        "--bucket",
        default=None,
        help="OSS bucket name. Can also be set with OSS_BUCKET in .env.",
    )
    parser.add_argument(
        "--device-id",
        default=None,
        help="Single ESP32 device_id. Can also be set with DEVICE_ID in .env.",
    )
    parser.add_argument(
        "--device-ids",
        default=None,
        help="Comma-separated ESP32 device IDs. Can also be set with DEVICE_IDS in .env.",
    )
    parser.add_argument(
        "--discover-devices",
        action="store_true",
        help="Discover devices by scanning OSS for devices/*/requests/latest.json.",
    )
    parser.add_argument(
        "--discover-interval",
        type=float,
        default=30.0,
        help="Seconds between OSS device discovery scans. Default: 30.0.",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"OSS region. Default: {DEFAULT_REGION}.",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="Optional OSS endpoint. Default: https://oss-<region>.aliyuncs.com.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds. Default: 1.0.",
    )
    parser.add_argument(
        "--state-file",
        default=".oss_ai_worker_state.json",
        help="Local state file for processed frame tracking.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional local env file for credentials. Default: .env.",
    )
    parser.add_argument(
        "--debug-dir",
        default=None,
        help="Optional directory to save downloaded images and result JSON for debugging.",
    )
    parser.add_argument(
        "--ai-api-key-env",
        default="CHESS_AI_API_KEY",
        help="Environment variable name that stores the AI service API key. Default: CHESS_AI_API_KEY.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one new manifest and exit.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log verbosity. Default: INFO.",
    )
    return parser.parse_args()


def parse_bool_env(value):
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def parse_device_id_list(value):
    if not value:
        return []

    normalized = value.replace(";", ",").replace("\n", ",")
    device_ids = []
    seen = set()
    for item in normalized.split(","):
        device_id = item.strip()
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)
        device_ids.append(device_id)
    return device_ids


def apply_env_defaults(args):
    if args.bucket is None:
        args.bucket = os.getenv("OSS_BUCKET")

    device_ids = []
    if args.device_ids is None:
        args.device_ids = os.getenv("DEVICE_IDS")
    device_ids.extend(parse_device_id_list(args.device_ids))

    if args.device_id is None:
        args.device_id = os.getenv("DEVICE_ID")
    device_ids.extend(parse_device_id_list(args.device_id))
    args.device_ids = list(dict.fromkeys(device_ids))

    args.region = os.getenv("OSS_REGION", args.region)
    if args.endpoint is None:
        args.endpoint = os.getenv("OSS_ENDPOINT")
    if not args.discover_devices:
        args.discover_devices = parse_bool_env(os.getenv("DISCOVER_DEVICES"))

    missing = []
    if not args.bucket:
        missing.append("OSS bucket (--bucket or OSS_BUCKET)")
    if not args.device_ids and not args.discover_devices:
        missing.append("device ids (--device-id, --device-ids, DEVICE_ID, DEVICE_IDS, or DISCOVER_DEVICES=true)")

    if missing:
        raise RuntimeError("Missing required configuration: " + ", ".join(missing))


def load_env_file(path):
    env_path = Path(path)
    if not env_path.exists():
        return

    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            LOGGER.warning("Ignoring invalid env line %s:%d", env_path, line_number)
            continue

        name, value = line.split("=", 1)
        name = name.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if not name:
            LOGGER.warning("Ignoring env line with empty name %s:%d", env_path, line_number)
            continue

        os.environ.setdefault(name, value)


def build_bucket(args):
    access_key_id = os.getenv("ALIYUN_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIYUN_ACCESS_KEY_SECRET")
    security_token = os.getenv("ALIYUN_SECURITY_TOKEN")

    if not access_key_id or not access_key_secret:
        raise RuntimeError(
            "Missing ALIYUN_ACCESS_KEY_ID or ALIYUN_ACCESS_KEY_SECRET environment variable."
        )

    if security_token:
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
    else:
        auth = oss2.Auth(access_key_id, access_key_secret)

    endpoint = args.endpoint or f"https://oss-{args.region}.aliyuncs.com"
    return oss2.Bucket(auth, endpoint, args.bucket)


def load_state(path):
    state_path = Path(path)
    if not state_path.exists():
        return {}

    try:
        with state_path.open("r", encoding="utf-8") as file_obj:
            state = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read state file %s: %s", state_path, exc)
        return {}

    if isinstance(state, dict):
        return state
    return {}


def save_state(path, state):
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(state_path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as file_obj:
        json.dump(state, file_obj, indent=2, sort_keys=True)
        file_obj.write("\n")

    temp_path.replace(state_path)


def extract_device_id_from_manifest_key(key):
    prefix = "devices/"
    suffix = "/requests/latest.json"
    if not key.startswith(prefix) or not key.endswith(suffix):
        return None

    device_id = key[len(prefix):-len(suffix)]
    if not device_id or "/" in device_id:
        return None
    return device_id


def discover_device_ids(bucket):
    discovered = []
    seen = set()
    for obj in oss2.ObjectIterator(bucket, prefix="devices/"):
        device_id = extract_device_id_from_manifest_key(obj.key)
        if device_id is None or device_id in seen:
            continue
        seen.add(device_id)
        discovered.append(device_id)
    return discovered


def merge_device_ids(configured, discovered):
    merged = []
    seen = set()
    for device_id in configured + discovered:
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)
        merged.append(device_id)
    return merged


def get_device_state(state, device_id):
    devices = state.setdefault("devices", {})
    if not isinstance(devices, dict):
        devices = {}
        state["devices"] = devices

    device_state = devices.setdefault(device_id, {})
    if not isinstance(device_state, dict):
        device_state = {}
        devices[device_id] = device_state
    return device_state


def _get_positive_int(manifest, name):
    value = int(manifest[name])
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def validate_board_point(point):
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        x = int(point[0])
        y = int(point[1])
    else:
        x = int(point["x"])
        y = int(point["y"])
    if x < 0 or x >= BOARD_FILES or y < 0 or y >= BOARD_RANKS:
        raise ValueError(f"board point out of range: ({x}, {y})")
    return {"x": x, "y": y}


def _get_optional_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_number_list(raw):
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def parse_board_corners(image_width, image_height):
    """Return canonical board corners in image pixels, or None if not configured.

    CHESS_BOARD_CORNERS order:
    (0,0) shuai-left baseline, (8,0) shuai-right baseline,
    (8,9) jiang-right baseline, (0,9) jiang-left baseline.
    Values can be absolute pixels or normalized 0..1 coordinates.
    """
    raw = os.getenv("CHESS_BOARD_CORNERS", "").strip()
    if not raw:
        return None

    parts = _split_number_list(raw)
    if len(parts) != 8:
        raise ValueError(
            "CHESS_BOARD_CORNERS must contain x0,y0,x1,y1,x2,y2,x3,y3"
        )
    values = [float(part) for part in parts]
    normalized = all(0.0 <= value <= 1.0 for value in values)
    corners = []
    for index in range(0, len(values), 2):
        x_value = values[index]
        y_value = values[index + 1]
        if normalized:
            x_value *= image_width
            y_value *= image_height
        corners.append((x_value, y_value))
    return corners


def _solve_linear_system(matrix, vector):
    size = len(vector)
    augmented = [list(matrix[row]) + [float(vector[row])] for row in range(size)]

    for col in range(size):
        pivot_row = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot_row][col]) < 1e-9:
            raise ValueError("singular homography matrix")
        if pivot_row != col:
            augmented[col], augmented[pivot_row] = augmented[pivot_row], augmented[col]

        pivot = augmented[col][col]
        for entry in range(col, size + 1):
            augmented[col][entry] /= pivot

        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            if abs(factor) < 1e-12:
                continue
            for entry in range(col, size + 1):
                augmented[row][entry] -= factor * augmented[col][entry]

    return [augmented[row][size] for row in range(size)]


def _compute_homography(src_points, dst_points):
    matrix = []
    vector = []
    for (src_x, src_y), (dst_x, dst_y) in zip(src_points, dst_points):
        matrix.append([src_x, src_y, 1.0, 0.0, 0.0, 0.0, -dst_x * src_x, -dst_x * src_y])
        vector.append(dst_x)
        matrix.append([0.0, 0.0, 0.0, src_x, src_y, 1.0, -dst_y * src_x, -dst_y * src_y])
        vector.append(dst_y)
    solved = _solve_linear_system(matrix, vector)
    return solved + [1.0]


def _apply_homography(homography, x_value, y_value):
    denominator = homography[6] * x_value + homography[7] * y_value + homography[8]
    if abs(denominator) < 1e-9:
        raise ValueError("homography point maps to infinity")
    out_x = (homography[0] * x_value + homography[1] * y_value + homography[2]) / denominator
    out_y = (homography[3] * x_value + homography[4] * y_value + homography[5]) / denominator
    return out_x, out_y


def _canonical_corner_points():
    return [
        (0.0, 0.0),
        (float(BOARD_FILES - 1), 0.0),
        (float(BOARD_FILES - 1), float(BOARD_RANKS - 1)),
        (0.0, float(BOARD_RANKS - 1)),
    ]


def _normalize_board_crop_values(values, image_width, image_height):
    if len(values) != 4:
        raise ValueError("board crop must contain left,top,right,bottom")
    values = [float(value) for value in values]
    if all(0.0 <= value <= 1.0 for value in values):
        left = values[0] * image_width
        top = values[1] * image_height
        right = values[2] * image_width
        bottom = values[3] * image_height
    else:
        left, top, right, bottom = values
    if right <= left or bottom <= top:
        raise ValueError("invalid board crop bounds")
    return left, top, right, bottom


def parse_board_crop(image_width, image_height, board_crop=None):
    """Return board bounds as left, top, right, bottom in image pixels."""
    if board_crop is not None:
        if isinstance(board_crop, dict):
            values = [
                board_crop["left"],
                board_crop["top"],
                board_crop["right"],
                board_crop["bottom"],
            ]
        else:
            values = board_crop
        return _normalize_board_crop_values(values, image_width, image_height)

    raw = os.getenv("CHESS_BOARD_CROP", "").strip()
    if raw:
        parts = _split_number_list(raw)
        left, top, right, bottom = _normalize_board_crop_values(parts, image_width, image_height)
    else:
        left = image_width * DEFAULT_BOARD_GRID_LEFT
        right = image_width * DEFAULT_BOARD_GRID_RIGHT
        top = image_height * DEFAULT_BOARD_GRID_TOP
        bottom = image_height * DEFAULT_BOARD_GRID_BOTTOM

    if right <= left or bottom <= top:
        raise ValueError("invalid board crop bounds")
    return left, top, right, bottom


def _weighted_line_position_clusters(values, tolerance):
    if not values:
        return []

    clusters = []
    for position, weight in sorted(values, key=lambda item: item[0]):
        if not clusters or abs(position - clusters[-1]["position"]) > tolerance:
            clusters.append(
                {
                    "position": float(position),
                    "weight": float(weight),
                    "count": 1,
                }
            )
            continue

        cluster = clusters[-1]
        total_weight = cluster["weight"] + float(weight)
        if total_weight > 0:
            cluster["position"] = (
                cluster["position"] * cluster["weight"] + float(position) * float(weight)
            ) / total_weight
        cluster["weight"] = total_weight
        cluster["count"] += 1

    return clusters


def _fit_even_grid_from_clusters(clusters, expected_count, image_limit):
    if len(clusters) < 2:
        return None

    positions = [float(cluster["position"]) for cluster in clusters]
    weights = [max(1.0, float(cluster.get("weight", 1.0))) for cluster in clusters]
    min_spacing = image_limit * 0.045
    max_spacing = image_limit * 0.18
    best = None

    for left_index, left_position in enumerate(positions):
        for right_index in range(left_index + 1, len(positions)):
            right_position = positions[right_index]
            for grid_left_index in range(expected_count - 1):
                for grid_right_index in range(grid_left_index + 1, expected_count):
                    spacing = (right_position - left_position) / (grid_right_index - grid_left_index)
                    if spacing < min_spacing or spacing > max_spacing:
                        continue
                    start = left_position - grid_left_index * spacing
                    end = start + (expected_count - 1) * spacing
                    if start < -image_limit * 0.08 or end > image_limit * 1.08:
                        continue

                    tolerance = max(8.0, spacing * 0.16)
                    matched = 0
                    score = 0.0
                    residual = 0.0
                    for position, weight in zip(positions, weights):
                        nearest_index = round((position - start) / spacing)
                        if nearest_index < 0 or nearest_index >= expected_count:
                            continue
                        predicted = start + nearest_index * spacing
                        distance = abs(position - predicted)
                        if distance > tolerance:
                            continue
                        matched += 1
                        score += weight * (1.0 - distance / tolerance)
                        residual += distance

                    coverage_bonus = matched * image_limit * 0.002
                    boundary_penalty = 0.0
                    if start < 0:
                        boundary_penalty += abs(start)
                    if end > image_limit - 1:
                        boundary_penalty += end - (image_limit - 1)
                    candidate_score = score + coverage_bonus - boundary_penalty * 0.05
                    item = {
                        "start": float(start),
                        "end": float(end),
                        "spacing": float(spacing),
                        "matched": matched,
                        "residual": float(residual),
                        "score": float(candidate_score),
                    }
                    if best is None or item["score"] > best["score"]:
                        best = item

    if best is None:
        return None

    min_matched = max(4, expected_count // 2)
    if best["matched"] < min_matched:
        return None
    if best["end"] <= best["start"]:
        return None
    best["start"] = max(0.0, min(float(image_limit - 1), best["start"]))
    best["end"] = max(0.0, min(float(image_limit - 1), best["end"]))
    return best


def _fit_grid_axis_from_circle_centers(circles, coordinate, initial_grid, expected_count, image_limit):
    """Refine one grid axis using piece centers after the rough Hough fit."""
    if not circles or not initial_grid:
        return None

    try:
        import numpy as np

        initial_start = float(initial_grid["start"])
        initial_spacing = float(initial_grid["spacing"])
        if initial_spacing <= 0:
            return None
        min_radius_ratio = float(os.getenv("CHESS_GRID_CIRCLE_MIN_RADIUS_RATIO", "0.70"))
        radii = [float(circle.get("radius", 0.0)) for circle in circles if float(circle.get("radius", 0.0)) > 0]
        if not radii:
            return None
        median_radius = float(np.median(radii))
        max_distance = initial_spacing * float(os.getenv("CHESS_GRID_CIRCLE_FIT_MAX_DISTANCE_RATIO", "0.32"))
        usable = []
        for circle in circles:
            radius = float(circle.get("radius", 0.0))
            if radius < median_radius * min_radius_ratio:
                continue
            value = float(circle.get(coordinate, 0.0))
            grid_index = int(round((value - initial_start) / initial_spacing))
            if grid_index < 0 or grid_index >= expected_count:
                continue
            residual = abs(value - (initial_start + grid_index * initial_spacing))
            if residual <= max_distance:
                usable.append((float(grid_index), value, max(1.0, radius * radius)))

        min_matches = max(6, int(os.getenv("CHESS_GRID_CIRCLE_FIT_MIN_MATCHES", "8")))
        if len(usable) < min_matches:
            return None

        fit_points = usable
        for _ in range(2):
            design = np.asarray([[1.0, index] for index, _, _ in fit_points], dtype=np.float64)
            values = np.asarray([value for _, value, _ in fit_points], dtype=np.float64)
            weights = np.sqrt(np.asarray([weight for _, _, weight in fit_points], dtype=np.float64))
            coefficients, _, _, _ = np.linalg.lstsq(design * weights[:, None], values * weights, rcond=None)
            start = float(coefficients[0])
            spacing = float(coefficients[1])
            if spacing <= 0:
                return None
            residual_limit = max_distance * 1.25
            fit_points = [
                point
                for point in usable
                if abs(point[1] - (start + point[0] * spacing)) <= residual_limit
            ]
            if len(fit_points) < min_matches:
                return None

        end = start + (expected_count - 1) * spacing
        old_end = initial_start + (expected_count - 1) * initial_spacing
        max_shift = initial_spacing * float(os.getenv("CHESS_GRID_CIRCLE_FIT_MAX_SHIFT_RATIO", "0.45"))
        if (
            spacing < initial_spacing * 0.82
            or spacing > initial_spacing * 1.18
            or abs(start - initial_start) > max_shift
            or abs(end - old_end) > max_shift
            or start < -image_limit * 0.03
            or end > image_limit * 1.03
        ):
            return None

        rms = float(
            np.sqrt(
                np.mean(
                    [
                        (value - (start + index * spacing)) ** 2
                        for index, value, _ in fit_points
                    ]
                )
            )
        )
        return {
            "start": max(0.0, min(float(image_limit - 1), start)),
            "end": max(0.0, min(float(image_limit - 1), end)),
            "spacing": spacing,
            "matched": len(fit_points),
            "median_radius": median_radius,
            "rms": rms,
        }
    except Exception:
        LOGGER.exception("Grid circle-center refinement failed")
        return None


def _refine_grid_crop_with_circle_centers(image_body, rough_crop, image_width, image_height):
    """Use plausible piece centers to reduce Hough boundary drift."""
    if not parse_bool_env(os.getenv("CHESS_GRID_CROP_REFINE_WITH_CIRCLES", "true")):
        return rough_crop, {"status": "disabled"}
    if cv2 is None:
        return rough_crop, {"status": "opencv_unavailable"}

    try:
        detection_body, suppression_metadata = suppress_board_grid_lines(
            image_body,
            board_crop=rough_crop,
        )
        circles = detect_piece_circles(
            detection_body,
            suppress_grid_lines=False,
            board_crop=rough_crop,
        )
        if not circles:
            return rough_crop, {
                "status": "circle_detection_empty",
                "suppression": suppression_metadata,
            }

        left, top, right, bottom = rough_crop
        initial_x = {
            "start": float(left),
            "end": float(right),
            "spacing": (float(right) - float(left)) / float(BOARD_FILES - 1),
        }
        initial_y = {
            "start": float(top),
            "end": float(bottom),
            "spacing": (float(bottom) - float(top)) / float(BOARD_RANKS - 1),
        }
        refined_x = _fit_grid_axis_from_circle_centers(
            circles, "cx", initial_x, BOARD_FILES, image_width
        )
        refined_y = _fit_grid_axis_from_circle_centers(
            circles, "cy", initial_y, BOARD_RANKS, image_height
        )
        if refined_x is None or refined_y is None:
            return rough_crop, {
                "status": "circle_fit_failed",
                "circle_count": len(circles),
                "x_refined": refined_x is not None,
                "y_refined": refined_y is not None,
            }

        refined_crop = (
            refined_x["start"],
            refined_y["start"],
            refined_x["end"],
            refined_y["end"],
        )
        return refined_crop, {
            "status": "applied",
            "circle_count": len(circles),
            "x": {
                "matched": refined_x["matched"],
                "spacing": round(refined_x["spacing"], 2),
                "rms": round(refined_x["rms"], 2),
                "median_radius": round(refined_x["median_radius"], 2),
            },
            "y": {
                "matched": refined_y["matched"],
                "spacing": round(refined_y["spacing"], 2),
                "rms": round(refined_y["rms"], 2),
                "median_radius": round(refined_y["median_radius"], 2),
            },
            "rough_crop": [round(float(value), 2) for value in rough_crop],
            "refined_crop": [round(float(value), 2) for value in refined_crop],
        }
    except Exception as exc:
        LOGGER.exception("Grid crop circle-center refinement failed")
        return rough_crop, {"status": "failed", "error": str(exc)}


def detect_rectified_grid_crop(image_body):
    """Detect the playable 9x10 intersection frame inside a rectified board image."""
    metadata = {
        "enabled": parse_bool_env(os.getenv("CHESS_OPENCV_DETECT_GRID_CROP", "true")),
        "status": "disabled",
        "method": "rectified_hough_grid",
        "source": GRID_CROP_FALLBACK_SOURCE,
    }
    if not metadata["enabled"] or cv2 is None:
        return None, metadata

    try:
        import numpy as np

        image_array = np.frombuffer(image_body, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if image is None:
            metadata["status"] = "decode_failed"
            return None, metadata

        image_height, image_width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(gray, 45, 135)
        min_dim = min(image_width, image_height)
        min_line_length = int(
            os.getenv("CHESS_GRID_CROP_HOUGH_MIN_LINE", str(max(42, round(min_dim * 0.055))))
        )
        max_line_gap = int(os.getenv("CHESS_GRID_CROP_HOUGH_MAX_GAP", "24"))
        threshold = int(os.getenv("CHESS_GRID_CROP_HOUGH_THRESHOLD", "45"))
        lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180.0,
            threshold=threshold,
            minLineLength=min_line_length,
            maxLineGap=max_line_gap,
        )
        if lines is None:
            metadata["status"] = "line_not_found"
            return None, metadata

        horizontal = []
        vertical = []
        for raw_line in lines.reshape(-1, 4):
            x1, y1, x2, y2 = [float(value) for value in raw_line]
            length = _line_length((x1, y1, x2, y2))
            if length < min_line_length:
                continue
            angle = abs(float(np.degrees(np.arctan2(y2 - y1, x2 - x1))))
            if angle > 90.0:
                angle = 180.0 - angle
            mid_x = (x1 + x2) / 2.0
            mid_y = (y1 + y2) / 2.0
            if angle <= 10.0:
                horizontal.append((mid_y, length))
            elif angle >= 80.0:
                vertical.append((mid_x, length))

        cluster_tolerance = max(6.0, min_dim * float(os.getenv("CHESS_GRID_CROP_CLUSTER_TOLERANCE", "0.012")))
        x_clusters = _weighted_line_position_clusters(vertical, cluster_tolerance)
        y_clusters = _weighted_line_position_clusters(horizontal, cluster_tolerance)
        x_grid = _fit_even_grid_from_clusters(x_clusters, BOARD_FILES, image_width)
        y_grid = _fit_even_grid_from_clusters(y_clusters, BOARD_RANKS, image_height)
        if x_grid is None or y_grid is None:
            metadata.update(
                {
                    "status": "grid_fit_failed",
                    "x_cluster_count": len(x_clusters),
                    "y_cluster_count": len(y_clusters),
                }
            )
            return None, metadata

        left = x_grid["start"]
        right = x_grid["end"]
        top = y_grid["start"]
        bottom = y_grid["end"]
        min_width = image_width * 0.45
        min_height = image_height * 0.55
        if right - left < min_width or bottom - top < min_height:
            metadata.update(
                {
                    "status": "grid_too_small",
                    "crop": [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)],
                }
            )
            return None, metadata

        rough_crop = (left, top, right, bottom)
        crop, circle_refinement = _refine_grid_crop_with_circle_centers(
            image_body,
            rough_crop,
            image_width,
            image_height,
        )
        left, top, right, bottom = crop
        metadata.update(
            {
                "status": "detected",
                "source": (
                    "opencv_rectified_grid_circle_refined"
                    if circle_refinement.get("status") == "applied"
                    else "opencv_rectified_grid"
                ),
                "crop": [round(float(left), 2), round(float(top), 2), round(float(right), 2), round(float(bottom), 2)],
                "x_cluster_count": len(x_clusters),
                "y_cluster_count": len(y_clusters),
                "x_matched": x_grid["matched"],
                "y_matched": y_grid["matched"],
                "x_spacing": round(float(x_grid["spacing"]), 2),
                "y_spacing": round(float(y_grid["spacing"]), 2),
                "circle_refinement": circle_refinement,
            }
        )
        return crop, metadata
    except Exception as exc:
        LOGGER.exception("OpenCV rectified grid crop detection failed")
        metadata.update({"status": "failed", "error": str(exc)})
        return None, metadata


def get_effective_board_crop(image_body, image_width, image_height):
    detected_crop, metadata = detect_rectified_grid_crop(image_body)
    if detected_crop is not None:
        return detected_crop, metadata

    fallback_crop = parse_board_crop(image_width, image_height)
    metadata = dict(metadata)
    metadata["fallback_crop"] = [
        round(float(fallback_crop[0]), 2),
        round(float(fallback_crop[1]), 2),
        round(float(fallback_crop[2]), 2),
        round(float(fallback_crop[3]), 2),
    ]
    return fallback_crop, metadata


def _order_quad_points(points):
    import numpy as np

    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums = pts.sum(axis=1)
    diffs = pts[:, 0] - pts[:, 1]
    top_left = pts[int(np.argmin(sums))]
    bottom_right = pts[int(np.argmax(sums))]
    top_right = pts[int(np.argmax(diffs))]
    bottom_left = pts[int(np.argmin(diffs))]
    return np.asarray([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)


def _quad_side_lengths(quad):
    import numpy as np

    return [
        float(np.linalg.norm(quad[1] - quad[0])),
        float(np.linalg.norm(quad[2] - quad[1])),
        float(np.linalg.norm(quad[3] - quad[2])),
        float(np.linalg.norm(quad[0] - quad[3])),
    ]


def _line_length(line):
    import numpy as np

    x1, y1, x2, y2 = [float(value) for value in line]
    return float(np.hypot(x2 - x1, y2 - y1))


def _line_intersection(line_a, line_b):
    x1, y1, x2, y2 = [float(value) for value in line_a]
    x3, y3, x4, y4 = [float(value) for value in line_b]
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) < 1e-6:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / denominator
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / denominator
    return px, py


def _lower_line_endpoint(line):
    x1, y1, x2, y2 = [float(value) for value in line]
    if y1 >= y2:
        return x1, y1
    return x2, y2


def _fit_line_from_points(points):
    if cv2 is None or len(points) < 20:
        return None

    import numpy as np

    pts = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).flatten()
    if abs(vx) < 1e-6 and abs(vy) < 1e-6:
        return None
    scale = 10000.0
    return (
        float(x0 - vx * scale),
        float(y0 - vy * scale),
        float(x0 + vx * scale),
        float(y0 + vy * scale),
    )


def _detect_board_quad_from_red_frame(image):
    """Detect red-framed physical boards where thin grid lines dominate edges."""
    if cv2 is None:
        return None

    try:
        import numpy as np

        height, width = image.shape[:2]
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        lower_red_a = np.array([0, 55, 35], dtype=np.uint8)
        upper_red_a = np.array([15, 255, 255], dtype=np.uint8)
        lower_red_b = np.array([165, 55, 35], dtype=np.uint8)
        upper_red_b = np.array([179, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_red_a, upper_red_a) | cv2.inRange(hsv, lower_red_b, upper_red_b)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

        component_count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        min_area = width * height * 0.002
        left_points = []
        right_points = []
        top_points = []
        bottom_points = []

        for label in range(1, component_count):
            x_value, y_value, comp_width, comp_height, area = stats[label]
            if area < min_area:
                continue
            center_x = x_value + comp_width / 2.0
            center_y = y_value + comp_height / 2.0
            ys, xs = np.where(labels == label)
            component_points = list(zip(xs.astype(float), ys.astype(float)))

            if comp_height > height * 0.32 and comp_width < width * 0.22:
                if center_x < width * 0.45:
                    left_points.extend(component_points)
                elif center_x > width * 0.55:
                    right_points.extend(component_points)
            if comp_width > width * 0.28 and comp_height < height * 0.22:
                if center_y < height * 0.25:
                    top_points.extend(component_points)
                elif center_y > height * 0.58:
                    bottom_points.extend(component_points)

        left_line = _fit_line_from_points(left_points)
        right_line = _fit_line_from_points(right_points)
        bottom_line = _fit_line_from_points(bottom_points)
        top_line = _fit_line_from_points(top_points)

        if left_line is None or right_line is None:
            return None

        if top_line is None:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            median = float(np.median(gray))
            edges = cv2.Canny(gray, int(max(0, 0.66 * median)), int(min(255, 1.33 * median)))
            lines = cv2.HoughLinesP(
                edges,
                rho=1,
                theta=np.pi / 180.0,
                threshold=45,
                minLineLength=max(120, int(width * 0.42)),
                maxLineGap=60,
            )
            top_candidates = []
            if lines is not None:
                for raw_line in lines.reshape(-1, 4):
                    x1, y1, x2, y2 = [float(value) for value in raw_line]
                    length = _line_length((x1, y1, x2, y2))
                    angle = abs(float(np.degrees(np.arctan2(y2 - y1, x2 - x1))))
                    if angle > 90.0:
                        angle = 180.0 - angle
                    mid_y = (y1 + y2) / 2.0
                    if angle <= 12.0 and mid_y < height * 0.25 and length > width * 0.38:
                        top_candidates.append(
                            {
                                "line": (x1, y1, x2, y2),
                                "length": length,
                                "mid_y": mid_y,
                            }
                        )
            if top_candidates:
                top_line = min(top_candidates, key=lambda item: (item["mid_y"], -item["length"]))["line"]

        if bottom_line is None:
            bottom_line = (
                0.0,
                float(height - 1),
                float(width - 1),
                float(height - 1),
            )

        if top_line is None or bottom_line is None:
            return None

        corners = [
            _line_intersection(top_line, left_line),
            _line_intersection(top_line, right_line),
            _line_intersection(bottom_line, right_line),
            _line_intersection(bottom_line, left_line),
        ]
        if any(corner is None for corner in corners):
            return None

        tolerance = max(width, height) * 0.12
        clipped = []
        for x_value, y_value in corners:
            if x_value < -tolerance or x_value > width + tolerance:
                return None
            if y_value < -tolerance or y_value > height + tolerance:
                return None
            clipped.append((min(max(x_value, 0.0), float(width - 1)), min(max(y_value, 0.0), float(height - 1))))

        quad = _order_quad_points(clipped)
        area = abs(float(cv2.contourArea(quad)))
        if area < width * height * float(os.getenv("CHESS_BOARD_MIN_AREA_RATIO", "0.18")):
            return None
        return [(float(x), float(y)) for x, y in quad]
    except Exception:
        LOGGER.exception("OpenCV red frame board detection failed")
        return None


def _detect_board_quad_from_lines(edges, width, height):
    if cv2 is None:
        return None

    try:
        import numpy as np

        min_dim = min(width, height)
        min_line_length = int(os.getenv("CHESS_BOARD_HOUGH_MIN_LINE", str(max(120, round(min_dim * 0.45)))))
        max_line_gap = int(os.getenv("CHESS_BOARD_HOUGH_MAX_GAP", "35"))
        threshold = int(os.getenv("CHESS_BOARD_HOUGH_THRESHOLD", "70"))
        lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180.0,
            threshold=threshold,
            minLineLength=min_line_length,
            maxLineGap=max_line_gap,
        )
        if lines is None:
            return None

        horizontal = []
        vertical = []
        lines = lines.reshape(-1, 4)
        for raw_line in lines:
            x1, y1, x2, y2 = [float(value) for value in raw_line]
            length = _line_length((x1, y1, x2, y2))
            if length < min_line_length:
                continue
            angle = abs(float(np.degrees(np.arctan2(y2 - y1, x2 - x1))))
            if angle > 90.0:
                angle = 180.0 - angle
            mid_x = (x1 + x2) / 2.0
            mid_y = (y1 + y2) / 2.0
            item = {
                "line": (x1, y1, x2, y2),
                "length": length,
                "mid_x": mid_x,
                "mid_y": mid_y,
            }
            if angle <= 18.0:
                horizontal.append(item)
            elif angle >= 72.0:
                vertical.append(item)

        if len(horizontal) < 2 or len(vertical) < 2:
            return None

        top_candidates = [item for item in horizontal if item["mid_y"] <= height * 0.45]
        bottom_candidates = [item for item in horizontal if item["mid_y"] >= height * 0.55]
        left_candidates = [item for item in vertical if item["mid_x"] <= width * 0.45]
        right_candidates = [item for item in vertical if item["mid_x"] >= width * 0.55]
        if not top_candidates or not bottom_candidates or not left_candidates or not right_candidates:
            top_candidates = horizontal
            bottom_candidates = horizontal
            left_candidates = vertical
            right_candidates = vertical

        top = min(top_candidates, key=lambda item: (item["mid_y"], -item["length"]))
        bottom = max(bottom_candidates, key=lambda item: (item["mid_y"], item["length"]))
        left = min(left_candidates, key=lambda item: (item["mid_x"], -item["length"]))
        right = max(right_candidates, key=lambda item: (item["mid_x"], item["length"]))

        if top["mid_y"] > height * 0.22:
            return None
        if left["mid_x"] > width * 0.22:
            return None
        if right["mid_x"] < width * 0.78:
            return None

        top_left = _line_intersection(top["line"], left["line"])
        top_right = _line_intersection(top["line"], right["line"])
        if bottom["mid_y"] >= height * 0.65:
            bottom_right = _line_intersection(bottom["line"], right["line"])
            bottom_left = _line_intersection(bottom["line"], left["line"])
        else:
            bottom_left = _lower_line_endpoint(left["line"])
            bottom_right = _lower_line_endpoint(right["line"])
            if min(bottom_left[1], bottom_right[1]) < height * 0.65:
                return None

        corners = [top_left, top_right, bottom_right, bottom_left]
        if any(corner is None for corner in corners):
            return None

        clipped = []
        tolerance = max(width, height) * 0.08
        for x_value, y_value in corners:
            if x_value < -tolerance or x_value > width + tolerance:
                return None
            if y_value < -tolerance or y_value > height + tolerance:
                return None
            clipped.append((min(max(x_value, 0.0), float(width - 1)), min(max(y_value, 0.0), float(height - 1))))

        quad = _order_quad_points(clipped)
        area = abs(float(cv2.contourArea(quad)))
        if area < width * height * float(os.getenv("CHESS_BOARD_MIN_AREA_RATIO", "0.18")):
            return None
        return [(float(x), float(y)) for x, y in quad]
    except Exception:
        LOGGER.exception("OpenCV board line detection failed")
        return None


def detect_board_quad(image_body):
    """Detect the outer board quadrilateral in original image pixels."""
    if cv2 is None:
        return None

    try:
        import numpy as np

        image_array = np.frombuffer(image_body, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if image is None:
            return None

        height, width = image.shape[:2]
        max_dim = max(width, height)
        target_max_dim = int(os.getenv("CHESS_BOARD_DETECT_MAX_DIM", "1200"))
        scale = 1.0
        if target_max_dim > 0 and max_dim > target_max_dim:
            scale = target_max_dim / float(max_dim)
            image_for_detection = cv2.resize(
                image,
                (int(round(width * scale)), int(round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            image_for_detection = image

        gray = cv2.cvtColor(image_for_detection, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        median = float(np.median(gray))
        lower = int(max(0, 0.66 * median))
        upper = int(min(255, 1.33 * median))
        if upper <= lower:
            lower, upper = 50, 150
        edges = cv2.Canny(gray, lower, upper)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
        edges = cv2.dilate(edges, kernel, iterations=1)

        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            line_quad = _detect_board_quad_from_lines(edges, image_for_detection.shape[1], image_for_detection.shape[0])
            if line_quad and scale != 1.0:
                line_quad = [(x / scale, y / scale) for x, y in line_quad]
            if line_quad:
                return line_quad
            red_quad = _detect_board_quad_from_red_frame(image_for_detection)
            if red_quad and scale != 1.0:
                red_quad = [(x / scale, y / scale) for x, y in red_quad]
            return red_quad

        image_area = float(image_for_detection.shape[0] * image_for_detection.shape[1])
        min_area_ratio = float(os.getenv("CHESS_BOARD_MIN_AREA_RATIO", "0.18"))
        min_area = image_area * min_area_ratio
        best = None
        best_score = -1.0

        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
            area = float(cv2.contourArea(contour))
            if area < min_area:
                continue

            perimeter = cv2.arcLength(contour, True)
            if perimeter <= 0:
                continue

            approximations = []
            for epsilon_ratio in (0.015, 0.02, 0.03, 0.04, 0.055, 0.07):
                approx = cv2.approxPolyDP(contour, epsilon_ratio * perimeter, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    approximations.append(approx)
                    break

            if not approximations:
                rect = cv2.minAreaRect(contour)
                box = cv2.boxPoints(rect)
                approximations.append(box.reshape(4, 1, 2).astype("float32"))

            for approx in approximations:
                quad = _order_quad_points(approx.reshape(4, 2))
                lengths = _quad_side_lengths(quad)
                width_estimate = (lengths[0] + lengths[2]) / 2.0
                height_estimate = (lengths[1] + lengths[3]) / 2.0
                if width_estimate <= 1.0 or height_estimate <= 1.0:
                    continue
                aspect = width_estimate / height_estimate
                if not 0.55 <= aspect <= 1.35:
                    continue

                quad_area = abs(float(cv2.contourArea(quad)))
                rectangularity = min(1.0, quad_area / max(area, 1.0))
                score = area * rectangularity
                if score > best_score:
                    best = quad
                    best_score = score

        if best is None:
            line_quad = _detect_board_quad_from_lines(edges, image_for_detection.shape[1], image_for_detection.shape[0])
            if line_quad and scale != 1.0:
                line_quad = [(x / scale, y / scale) for x, y in line_quad]
            if line_quad:
                return line_quad
            red_quad = _detect_board_quad_from_red_frame(image_for_detection)
            if red_quad and scale != 1.0:
                red_quad = [(x / scale, y / scale) for x, y in red_quad]
            return red_quad

        if scale != 1.0:
            best = best / scale
        return [(float(x), float(y)) for x, y in best]
    except Exception:
        LOGGER.exception("OpenCV board quadrilateral detection failed")
        return None


def rectify_board_image(image_body):
    """Return a perspective-corrected board image and preprocessing metadata."""
    metadata = {
        "enabled": parse_bool_env(os.getenv("CHESS_OPENCV_RECTIFY_BOARD", "true")),
        "status": "disabled",
        "method": "opencv_perspective_quad",
    }
    if not metadata["enabled"]:
        return image_body, None, None, metadata
    if cv2 is None:
        metadata["status"] = "opencv_missing"
        return image_body, None, None, metadata

    try:
        import numpy as np

        image_array = np.frombuffer(image_body, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if image is None:
            metadata["status"] = "decode_failed"
            return image_body, None, None, metadata

        source_height, source_width = image.shape[:2]
        quad = detect_board_quad(image_body)
        if not quad:
            metadata.update(
                {
                    "status": "quad_not_found",
                    "source_image_width": source_width,
                    "source_image_height": source_height,
                }
            )
            return image_body, source_width, source_height, metadata

        output_width = int(os.getenv("CHESS_RECTIFIED_BOARD_WIDTH", str(DEFAULT_RECTIFIED_BOARD_WIDTH)))
        output_height = int(os.getenv("CHESS_RECTIFIED_BOARD_HEIGHT", str(DEFAULT_RECTIFIED_BOARD_HEIGHT)))
        output_width = max(320, output_width)
        output_height = max(360, output_height)

        src = _order_quad_points(quad)
        margin_x = max(0.02, min(0.20, float(DEFAULT_BOARD_MARGIN_X)))
        margin_top = max(0.02, min(0.20, float(DEFAULT_BOARD_MARGIN_TOP)))
        margin_bottom = max(0.02, min(0.20, float(DEFAULT_BOARD_MARGIN_BOTTOM)))
        dst = np.asarray(
            [
                [margin_x * float(output_width - 1), margin_top * float(output_height - 1)],
                [(1.0 - margin_x) * float(output_width - 1), margin_top * float(output_height - 1)],
                [
                    (1.0 - margin_x) * float(output_width - 1),
                    (1.0 - margin_bottom) * float(output_height - 1),
                ],
                [
                    margin_x * float(output_width - 1),
                    (1.0 - margin_bottom) * float(output_height - 1),
                ],
            ],
            dtype=np.float32,
        )
        transform = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(
            image,
            transform,
            (output_width, output_height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )

        quality = int(os.getenv("CHESS_RECTIFIED_JPEG_QUALITY", "95"))
        ok, encoded = cv2.imencode(".jpg", warped, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            metadata["status"] = "encode_failed"
            return image_body, source_width, source_height, metadata

        metadata.update(
            {
                "status": "rectified",
                "source_image_width": source_width,
                "source_image_height": source_height,
                "ai_image_width": output_width,
                "ai_image_height": output_height,
                "rectified_board_crop": [
                    round(float(dst[0][0]), 2),
                    round(float(dst[0][1]), 2),
                    round(float(dst[2][0]), 2),
                    round(float(dst[2][1]), 2),
                ],
                "source_board_corners": [
                    {"x": round(float(x), 2), "y": round(float(y), 2)}
                    for x, y in quad
                ],
            }
        )
        return encoded.tobytes(), output_width, output_height, metadata
    except Exception:
        LOGGER.exception("OpenCV board rectification failed")
        metadata["status"] = "failed"
        return image_body, None, None, metadata


def get_board_geometry(image_width, image_height, orientation, board_crop=None):
    corners = None if board_crop is not None else parse_board_corners(image_width, image_height)
    if corners:
        canonical_corners = _canonical_corner_points()
        return {
            "mode": "corners",
            "corners": corners,
            "canonical_to_pixel": _compute_homography(canonical_corners, corners),
            "pixel_to_canonical": _compute_homography(corners, canonical_corners),
        }

    left, top, right, bottom = parse_board_crop(image_width, image_height, board_crop=board_crop)
    bottom_side = normalize_orientation(orientation).get("bottom_side")
    if bottom_side == PLAYER2_SIDE:
        corners = [(right, top), (left, top), (left, bottom), (right, bottom)]
    else:
        corners = [(left, bottom), (right, bottom), (right, top), (left, top)]
    canonical_corners = _canonical_corner_points()
    return {
        "mode": "crop",
        "corners": corners,
        "crop": (left, top, right, bottom),
        "canonical_to_pixel": _compute_homography(canonical_corners, corners),
        "pixel_to_canonical": _compute_homography(corners, canonical_corners),
    }


def serialize_board_geometry(geometry, image_width, image_height):
    debug_geometry = {
        "mode": geometry.get("mode", "unknown"),
        "image_width": int(image_width),
        "image_height": int(image_height),
        "corners": [
            {"x": round(float(x_value), 2), "y": round(float(y_value), 2)}
            for x_value, y_value in geometry.get("corners", [])
        ],
    }
    if "crop" in geometry:
        left, top, right, bottom = geometry["crop"]
        debug_geometry["crop"] = {
            "left": round(float(left), 2),
            "top": round(float(top), 2),
            "right": round(float(right), 2),
            "bottom": round(float(bottom), 2),
        }
    return debug_geometry


def board_crop_from_result_geometry(board_geometry):
    if not isinstance(board_geometry, dict):
        return None
    crop = board_geometry.get("crop")
    if not isinstance(crop, dict):
        return None
    try:
        return (
            float(crop["left"]),
            float(crop["top"]),
            float(crop["right"]),
            float(crop["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def canonical_point_to_pixel(point, orientation, image_width, image_height, board_crop=None):
    geometry = get_board_geometry(image_width, image_height, orientation, board_crop=board_crop)
    return _apply_homography(
        geometry["canonical_to_pixel"],
        float(point["x"]),
        float(point["y"]),
    )


def pixel_to_canonical_point(cx, cy, orientation, image_width, image_height, board_crop=None):
    geometry = get_board_geometry(image_width, image_height, orientation, board_crop=board_crop)
    board_x, board_y = _apply_homography(geometry["pixel_to_canonical"], cx, cy)
    x = round(board_x)
    y = round(board_y)
    return validate_board_point({"x": x, "y": y})


def canonical_to_player1(point):
    return {"x": point["x"], "y": point["y"]}


def canonical_to_player2(point):
    return {
        "x": BOARD_FILES - 1 - point["x"],
        "y": BOARD_RANKS - 1 - point["y"],
    }


def validate_side(value):
    value = str(value).strip().lower()
    if value in (PLAYER1_SIDE, PLAYER2_SIDE, "unknown"):
        return value
    return "unknown"


def validate_piece_color(value):
    value = str(value).strip().lower()
    if value in PIECE_COLORS:
        return value
    return "unknown"


def validate_piece_name(value):
    value = str(value).strip().lower()
    if value in PIECE_NAMES:
        return value
    return "unknown"


def normalize_orientation(orientation):
    if not isinstance(orientation, dict):
        orientation = {}

    return {
        "bottom_side": validate_side(orientation.get("bottom_side", "unknown")),
        "top_side": validate_side(orientation.get("top_side", "unknown")),
    }


def normalize_pieces(pieces):
    normalized = []
    if not isinstance(pieces, list):
        return normalized

    for piece in pieces:
        if not isinstance(piece, dict):
            continue
        try:
            point = validate_board_point(
                {
                    "x": piece.get("x", piece.get("ai_x", 0)),
                    "y": piece.get("y", piece.get("ai_y", 0)),
                }
            )
        except (KeyError, TypeError, ValueError):
            point = {"x": 0, "y": 0}

        normalized_piece = {
            "side": validate_side(piece.get("side", "unknown")),
            "color": validate_piece_color(piece.get("color", "unknown")),
            "name": validate_piece_name(piece.get("name", "unknown")),
            "x": point["x"],
            "y": point["y"],
        }
        ai_x = _get_optional_float(piece.get("x", piece.get("ai_x")))
        ai_y = _get_optional_float(piece.get("y", piece.get("ai_y")))
        if ai_x is not None and ai_y is not None:
            normalized_piece["ai_x"] = int(round(ai_x))
            normalized_piece["ai_y"] = int(round(ai_y))
        cx = _get_optional_float(piece.get("cx"))
        cy = _get_optional_float(piece.get("cy"))
        if cx is not None and cy is not None:
            normalized_piece["cx"] = cx
            normalized_piece["cy"] = cy
        if "circle_id" in piece:
            try:
                normalized_piece["circle_id"] = int(piece["circle_id"])
            except (TypeError, ValueError):
                pass

        normalized.append(
            normalized_piece
        )

    return normalized


def build_processed_result(shuai_from,
                           shuai_to,
                           jiang_from,
                           jiang_to,
                           confidence,
                           orientation=None,
                           pieces=None):
    shuai_from = validate_board_point(shuai_from)
    shuai_to = validate_board_point(shuai_to)
    jiang_from = validate_board_point(jiang_from)
    jiang_to = validate_board_point(jiang_to)
    confidence = max(0.0, min(1.0, float(confidence)))
    orientation = normalize_orientation(orientation)
    pieces = normalize_pieces(pieces)

    return {
        "confidence": confidence,
        "orientation": orientation,
        "pieces": pieces,
        "recommended_moves": {
            PLAYER1_SIDE: {
                "from": shuai_from,
                "to": shuai_to,
            },
            PLAYER2_SIDE: {
                "from": jiang_from,
                "to": jiang_to,
            },
        },
        "coordinate_system": "player_local",
        "player_sides": {
            "player1": PLAYER1_SIDE,
            "player2": PLAYER2_SIDE,
        },
        "points": {
            "player1_start": canonical_to_player1(shuai_from),
            "player1_end": canonical_to_player1(shuai_to),
            "player2_start": canonical_to_player2(jiang_from),
            "player2_end": canonical_to_player2(jiang_to),
        },
        "canonical_points": {
            "player1_start": shuai_from,
            "player1_end": shuai_to,
            "player2_start": jiang_from,
            "player2_end": jiang_to,
        },
        "moves": {
            "player1": {
                "from": canonical_to_player1(shuai_from),
                "to": canonical_to_player1(shuai_to),
            },
            "player2": {
                "from": canonical_to_player2(jiang_from),
                "to": canonical_to_player2(jiang_to),
            },
        },
    }


def build_processed_result_from_ai(ai_result):
    moves = ai_result["recommended_moves"]
    shuai = moves[PLAYER1_SIDE]
    jiang = moves[PLAYER2_SIDE]
    return build_processed_result(
        shuai["from"],
        shuai["to"],
        jiang["from"],
        jiang["to"],
        ai_result["confidence"],
        orientation=ai_result.get("orientation"),
        pieces=ai_result.get("pieces"),
    )


def sync_move_compatibility_fields(processed):
    moves = processed.get("recommended_moves", {})
    shuai = moves.get(PLAYER1_SIDE, {})
    jiang = moves.get(PLAYER2_SIDE, {})
    shuai_from = validate_board_point(shuai.get("from", {"x": 0, "y": 0}))
    shuai_to = validate_board_point(shuai.get("to", {"x": 0, "y": 0}))
    jiang_from = validate_board_point(jiang.get("from", {"x": 0, "y": BOARD_RANKS - 1}))
    jiang_to = validate_board_point(jiang.get("to", {"x": 0, "y": BOARD_RANKS - 1}))

    processed["points"] = {
        "player1_start": canonical_to_player1(shuai_from),
        "player1_end": canonical_to_player1(shuai_to),
        "player2_start": canonical_to_player2(jiang_from),
        "player2_end": canonical_to_player2(jiang_to),
    }
    processed["canonical_points"] = {
        "player1_start": shuai_from,
        "player1_end": shuai_to,
        "player2_start": jiang_from,
        "player2_end": jiang_to,
    }
    processed["moves"] = {
        "player1": {
            "from": canonical_to_player1(shuai_from),
            "to": canonical_to_player1(shuai_to),
        },
        "player2": {
            "from": canonical_to_player2(jiang_from),
            "to": canonical_to_player2(jiang_to),
        },
    }
    return processed


def build_xiangqi_fen(processed, active_side=PLAYER1_SIDE):
    """Convert the canonical worker board coordinates to Xiangqi FEN."""
    board = {}
    king_counts = {PLAYER1_SIDE: 0, PLAYER2_SIDE: 0}
    for piece in processed.get("pieces", []):
        name = validate_piece_name(piece.get("name", "unknown"))
        side = validate_side(piece.get("side", "unknown"))
        if name == "unknown" or side not in (PLAYER1_SIDE, PLAYER2_SIDE):
            raise ValueError("engine position contains an unknown piece or side")
        point = validate_board_point({"x": piece.get("x"), "y": piece.get("y")})
        key = (point["x"], point["y"])
        if key in board:
            raise ValueError(f"engine position has duplicate square ({key[0]},{key[1]})")
        if name in (PLAYER1_SIDE, PLAYER2_SIDE):
            king_counts[name] += 1
        letter = ENGINE_PIECE_LETTERS[name]
        board[key] = letter.upper() if side == PLAYER1_SIDE else letter

    for side in (PLAYER1_SIDE, PLAYER2_SIDE):
        if king_counts[side] != 1:
            raise ValueError(
                f"engine position requires one {side} king, got {king_counts[side]}"
            )
    if active_side not in (PLAYER1_SIDE, PLAYER2_SIDE):
        raise ValueError(f"invalid engine active side: {active_side}")

    rows = []
    for y in range(BOARD_RANKS - 1, -1, -1):
        empty = 0
        row = []
        for x in range(BOARD_FILES):
            piece_letter = board.get((x, y))
            if piece_letter is None:
                empty += 1
                continue
            if empty:
                row.append(str(empty))
                empty = 0
            row.append(piece_letter)
        if empty:
            row.append(str(empty))
        rows.append("".join(row))

    active_color = "w" if active_side == PLAYER1_SIDE else "b"
    return "/".join(rows) + f" {active_color} - - 0 1"


def parse_engine_coordinate_move(move_text):
    """Convert an engine coordinate move into the worker's canonical 0-based grid."""
    match = re.search(r"([a-i])(10|[0-9])([a-i])(10|[0-9])", str(move_text).lower())
    if not match:
        return None

    try:
        rank_base = int(os.getenv("CHESS_ENGINE_COORDINATE_BASE", "1"))
    except ValueError:
        rank_base = 1
    if rank_base not in (0, 1):
        rank_base = 1

    ranks = (int(match.group(2)), int(match.group(4)))
    if any(rank < rank_base or rank >= rank_base + BOARD_RANKS for rank in ranks):
        return None
    return {
        "from": {
            "x": ENGINE_FILES.index(match.group(1)),
            "y": ranks[0] - rank_base,
        },
        "to": {
            "x": ENGINE_FILES.index(match.group(3)),
            "y": ranks[1] - rank_base,
        },
    }


class _UciEngineSession:
    def __init__(self, executable):
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creation_flags,
        )
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _read_output(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\r\n"))
        finally:
            self.lines.put(None)

    def send(self, command):
        if self.process.poll() is not None:
            raise RuntimeError("Xiangqi engine exited before command")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def wait_for(self, predicate, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Xiangqi engine response timed out")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Xiangqi engine response timed out") from exc
            if line is None:
                raise RuntimeError("Xiangqi engine closed its output")
            if predicate(line):
                return line

    def initialize(self, timeout_seconds):
        self.send("uci")
        self.wait_for(lambda line: line == "uciok", timeout_seconds)
        self.send("setoption name UCI_Variant value xiangqi")
        self.send("isready")
        self.wait_for(lambda line: line == "readyok", timeout_seconds)

    def best_move(self, fen, movetime_ms, timeout_seconds):
        self.send("ucinewgame")
        self.send(f"position fen {fen}")
        self.send(f"go movetime {movetime_ms}")
        line = self.wait_for(lambda value: value.startswith("bestmove "), timeout_seconds)
        return line.split()[1] if len(line.split()) > 1 else None

    def close(self):
        if self.process.poll() is None:
            try:
                self.send("quit")
                self.process.wait(timeout=2)
            except Exception:
                self.process.kill()


def apply_engine_recommendations(processed):
    processed.setdefault("engine_status", "not_run")
    processed.setdefault("engine_error", "")
    processed.setdefault("engine_fen", "")
    processed.setdefault("engine_fens", {})
    processed.setdefault("engine_recommended_moves", {})

    if not parse_bool_env(os.getenv("CHESS_ENGINE_ENABLED", "true")):
        processed["engine_status"] = "disabled"
        return processed

    executable = os.getenv("CHESS_ENGINE_PATH", "").strip()
    if not executable:
        processed["engine_status"] = "not_configured"
        processed["engine_error"] = "CHESS_ENGINE_PATH is not configured"
        return processed
    if not Path(executable).is_file():
        processed["engine_status"] = "unavailable"
        processed["engine_error"] = f"engine executable not found: {executable}"
        return processed

    try:
        timeout_ms = max(1000, int(os.getenv("CHESS_ENGINE_TIMEOUT_MS", "20000")))
        movetime_ms = max(100, int(os.getenv("CHESS_ENGINE_MOVETIME_MS", "1500")))
        timeout_seconds = timeout_ms / 1000.0
        fens = {
            side: build_xiangqi_fen(processed, active_side=side)
            for side in (PLAYER1_SIDE, PLAYER2_SIDE)
        }
        processed["engine_fens"] = fens
        processed["engine_fen"] = fens[PLAYER1_SIDE]

        moves = {}
        session = _UciEngineSession(executable)
        try:
            session.initialize(timeout_seconds)
            for side in (PLAYER1_SIDE, PLAYER2_SIDE):
                bestmove = session.best_move(fens[side], movetime_ms, timeout_seconds)
                parsed = parse_engine_coordinate_move(bestmove)
                if parsed is None:
                    continue
                start_piece = _piece_at(processed.get("pieces", []), parsed["from"])
                target_piece = _piece_at(processed.get("pieces", []), parsed["to"])
                if start_piece is None or start_piece.get("side") != side:
                    continue
                if target_piece is not None and target_piece.get("side") == side:
                    continue
                moves[side] = parsed
        finally:
            session.close()

        processed["engine_recommended_moves"] = moves
        if not moves:
            processed["engine_status"] = "no_move"
            return processed
        processed["recommended_moves"].update(moves)
        processed["engine_status"] = "ok" if len(moves) == 2 else "partial"
        return sync_move_compatibility_fields(processed)
    except Exception as exc:
        LOGGER.exception("Xiangqi engine analysis failed")
        processed["engine_status"] = "failed"
        processed["engine_error"] = str(exc)
        return processed


def _piece_distance_to_board_point(piece, point, orientation, image_width, image_height, board_crop=None):
    cx = _get_optional_float(piece.get("cx"))
    cy = _get_optional_float(piece.get("cy"))
    if cx is None or cy is None:
        return None
    px, py = canonical_point_to_pixel(point, orientation, image_width, image_height, board_crop=board_crop)
    return ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5


def resolve_duplicate_piece_points(processed, image_width, image_height, board_crop=None):
    if not parse_bool_env(os.getenv("CHESS_AI_ENABLE_DUPLICATE_RESOLVE", "true")):
        processed["grid_snap_disambiguations"] = []
        return []

    pieces = processed.get("pieces", [])
    orientation = processed.get("orientation", {})
    groups = {}
    for piece in pieces:
        groups.setdefault((piece["x"], piece["y"]), []).append(piece)

    occupied_by_unique = {
        key
        for key, grouped in groups.items()
        if len(grouped) == 1
    }
    disambiguations = []
    radius = int(os.getenv("CHESS_GRID_DISAMBIGUATE_RADIUS", "1"))

    for key, grouped in groups.items():
        if len(grouped) <= 1:
            continue
        if any(piece.get("visual_evidence") == "duplicate_circle" for piece in grouped):
            continue

        candidate_points = []
        base_x, base_y = key
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                point = {"x": base_x + dx, "y": base_y + dy}
                try:
                    point = validate_board_point(point)
                except ValueError:
                    continue
                point_key = (point["x"], point["y"])
                if point_key in occupied_by_unique:
                    continue
                candidate_points.append(point)

        candidate_pairs = []
        for piece_index, piece in enumerate(grouped):
            for point in candidate_points:
                distance = _piece_distance_to_board_point(
                    piece,
                point,
                orientation,
                image_width,
                image_height,
                board_crop=board_crop,
            )
                if distance is None:
                    continue
                candidate_pairs.append((distance, piece_index, point))

        assigned_pieces = set()
        assigned_points = set()
        for distance, piece_index, point in sorted(candidate_pairs, key=lambda item: item[0]):
            point_key = (point["x"], point["y"])
            if piece_index in assigned_pieces or point_key in assigned_points:
                continue
            piece = grouped[piece_index]
            old_point = {"x": piece["x"], "y": piece["y"]}
            if old_point != point:
                piece["x"] = point["x"]
                piece["y"] = point["y"]
                disambiguations.append(
                    {
                        "side": piece["side"],
                        "name": piece["name"],
                        "from": old_point,
                        "to": point,
                        "distance": round(distance, 2),
                    }
                )
            assigned_pieces.add(piece_index)
            assigned_points.add(point_key)
            if len(assigned_pieces) == len(grouped):
                break

    processed["grid_snap_disambiguations"] = disambiguations
    return disambiguations


def suppress_board_grid_lines(image_body, board_crop=None):
    """Return an image for circle detection with known board lines inpainted."""
    metadata = {
        "enabled": parse_bool_env(os.getenv("CHESS_OPENCV_SUPPRESS_GRID_LINES", "true")),
        "status": "disabled",
        "method": "canonical_grid_line_mask",
    }
    if not metadata["enabled"] or cv2 is None:
        return image_body, metadata

    try:
        import numpy as np

        image_array = np.frombuffer(image_body, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if image is None:
            metadata["status"] = "decode_failed"
            return image_body, metadata

        image_height, image_width = image.shape[:2]
        if board_crop is None:
            board_crop, crop_metadata = get_effective_board_crop(image_body, image_width, image_height)
        else:
            crop_metadata = {"status": "provided"}
        left, top, right, bottom = parse_board_crop(image_width, image_height, board_crop=board_crop)
        mask = np.zeros((image_height, image_width), dtype=np.uint8)
        line_width = max(1, int(os.getenv("CHESS_GRID_LINE_MASK_WIDTH", "7")))
        x_points = np.linspace(left, right, BOARD_FILES)
        y_points = np.linspace(top, bottom, BOARD_RANKS)

        for x_value in x_points:
            cv2.line(
                mask,
                (int(round(x_value)), int(round(top))),
                (int(round(x_value)), int(round(bottom))),
                255,
                line_width,
            )
        for y_value in y_points:
            cv2.line(
                mask,
                (int(round(left)), int(round(y_value))),
                (int(round(right)), int(round(y_value))),
                255,
                line_width,
            )

        palace_x_left = x_points[3]
        palace_x_right = x_points[5]
        palace_y_top = y_points[0]
        palace_y_top_inner = y_points[2]
        palace_y_bottom_inner = y_points[7]
        palace_y_bottom = y_points[9]
        diagonal_lines = [
            (palace_x_left, palace_y_top, palace_x_right, palace_y_top_inner),
            (palace_x_right, palace_y_top, palace_x_left, palace_y_top_inner),
            (palace_x_left, palace_y_bottom_inner, palace_x_right, palace_y_bottom),
            (palace_x_right, palace_y_bottom_inner, palace_x_left, palace_y_bottom),
        ]
        for x0, y0, x1, y1 in diagonal_lines:
            cv2.line(
                mask,
                (int(round(x0)), int(round(y0))),
                (int(round(x1)), int(round(y1))),
                255,
                line_width,
            )

        inpaint_radius = max(1, int(os.getenv("CHESS_GRID_INPAINT_RADIUS", "3")))
        cleaned = cv2.inpaint(image, mask, inpaint_radius, cv2.INPAINT_TELEA)
        quality = int(os.getenv("CHESS_GRID_SUPPRESSED_JPEG_QUALITY", "95"))
        ok, encoded = cv2.imencode(
            ".jpg",
            cleaned,
            [int(cv2.IMWRITE_JPEG_QUALITY), quality],
        )
        if not ok:
            metadata["status"] = "encode_failed"
            return image_body, metadata

        metadata.update(
            {
                "status": "applied",
                "line_width": line_width,
                "inpaint_radius": inpaint_radius,
                "board_crop": [
                    round(float(left), 2),
                    round(float(top), 2),
                    round(float(right), 2),
                    round(float(bottom), 2),
                ],
                "board_crop_detection": crop_metadata,
            }
        )
        return encoded.tobytes(), metadata
    except Exception as exc:
        LOGGER.exception("Board grid line suppression failed")
        metadata.update({"status": "failed", "error": str(exc)})
        return image_body, metadata


def filter_piece_circle_candidates(image_body, circles, board_crop=None):
    """Reject Hough circles whose interior looks like board or frame, not a piece."""
    if not parse_bool_env(os.getenv("CHESS_OPENCV_APPEARANCE_FILTER", "true")):
        return circles
    if cv2 is None or not circles:
        return circles

    try:
        import numpy as np

        image = cv2.imdecode(
            np.frombuffer(image_body, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        if image is None:
            return circles
        image_height, image_width = image.shape[:2]
        if board_crop is None:
            board_crop, _ = get_effective_board_crop(image_body, image_width, image_height)
        left, top, right, bottom = parse_board_crop(image_width, image_height, board_crop=board_crop)
        min_radius = float(
            os.getenv(
                "CHESS_CIRCLE_MIN_ACCEPT_RADIUS",
                str(max(16, round(min(image_width, image_height) * 0.024))),
            )
        )
        max_empty_gray = float(os.getenv("CHESS_CIRCLE_MAX_EMPTY_GRAY", "225"))
        min_piece_saturation = float(os.getenv("CHESS_CIRCLE_MIN_PIECE_SATURATION", "85"))
        max_piece_saturation = float(os.getenv("CHESS_CIRCLE_MAX_PIECE_SATURATION", "190"))
        min_center_dark_ratio = float(os.getenv("CHESS_CIRCLE_MIN_CENTER_DARK_RATIO", "0.12"))
        min_body_dark_ratio = float(os.getenv("CHESS_CIRCLE_MIN_BODY_DARK_RATIO", "0.20"))
        max_empty_body_std = float(os.getenv("CHESS_CIRCLE_MAX_EMPTY_BODY_STD", "36"))
        radius_outlier_filter_enabled = parse_bool_env(
            os.getenv("CHESS_CIRCLE_FILTER_RADIUS_OUTLIERS", "true")
        )
        ring_rescue_enabled = parse_bool_env(
            os.getenv("CHESS_CIRCLE_RING_EDGE_RESCUE", "true")
        )
        min_ring_edge_coverage = float(
            os.getenv("CHESS_CIRCLE_MIN_RING_EDGE_COVERAGE", "0.70")
        )
        ring_edge_bins = max(
            12,
            int(os.getenv("CHESS_CIRCLE_RING_EDGE_BINS", "36")),
        )
        narrow_ring_edge_bins = max(
            ring_edge_bins,
            int(os.getenv("CHESS_CIRCLE_NARROW_RING_EDGE_BINS", "72")),
        )
        narrow_ring_band_ratio = min(
            0.20,
            max(
                0.03,
                float(os.getenv("CHESS_CIRCLE_NARROW_RING_BAND_RATIO", "0.10")),
            ),
        )
        min_narrow_ring_edge_coverage = float(
            os.getenv("CHESS_CIRCLE_MIN_NARROW_RING_EDGE_COVERAGE", "0.70")
        )
        radius_values = [
            float(circle.get("radius", 0.0))
            for circle in circles
            if float(circle.get("radius", 0.0)) >= min_radius
        ]
        median_radius = float(np.median(radius_values)) if radius_values else 0.0
        min_median_radius_ratio = float(
            os.getenv("CHESS_CIRCLE_MIN_MEDIAN_RADIUS_RATIO", "0.70")
        )
        filtered = []
        gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edge_image = cv2.Canny(
            gray_image,
            int(os.getenv("CHESS_CIRCLE_RING_CANNY_LOW", "45")),
            int(os.getenv("CHESS_CIRCLE_RING_CANNY_HIGH", "120")),
        )
        yy, xx = np.ogrid[:image_height, :image_width]

        def angular_edge_coverage(mask, cx, cy, bin_count):
            edge_y, edge_x = np.nonzero(mask & (edge_image > 0))
            if not edge_x.size:
                return 0.0
            edge_angles = np.arctan2(edge_y - cy, edge_x - cx) + np.pi
            edge_bins = np.floor(
                edge_angles * bin_count / (2.0 * np.pi)
            ).astype(np.int32)
            edge_bins = np.clip(edge_bins, 0, bin_count - 1)
            return float(len(np.unique(edge_bins)) / bin_count)

        for circle in circles:
            cx = float(circle["cx"])
            cy = float(circle["cy"])
            radius = float(circle.get("radius", 0.0))
            radial_distance = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
            ring_mask = (
                (radial_distance >= radius * 0.78)
                & (radial_distance <= radius * 1.18)
            )
            narrow_ring_mask = (
                (radial_distance >= radius * (1.0 - narrow_ring_band_ratio))
                & (radial_distance <= radius * (1.0 + narrow_ring_band_ratio))
            )
            ring_edge_coverage = angular_edge_coverage(
                ring_mask,
                cx,
                cy,
                ring_edge_bins,
            )
            narrow_ring_edge_coverage = angular_edge_coverage(
                narrow_ring_mask,
                cx,
                cy,
                narrow_ring_edge_bins,
            )
            has_ring_evidence = (
                ring_rescue_enabled
                and ring_edge_coverage >= min_ring_edge_coverage
                and narrow_ring_edge_coverage >= min_narrow_ring_edge_coverage
            )
            rescue_reasons = []
            if radius < min_radius:
                if not has_ring_evidence:
                    continue
                rescue_reasons.append("radius_below_min_with_ring_evidence")
            if (
                radius_outlier_filter_enabled
                and median_radius > 0
                and radius < median_radius * min_median_radius_ratio
            ):
                if not has_ring_evidence:
                    continue
                rescue_reasons.append("radius_outlier_with_ring_evidence")
            boundary_margin = max(6.0, radius * float(os.getenv("CHESS_CIRCLE_CROP_EDGE_MARGIN_RATIO", "0.60")))
            if (
                cx < left - boundary_margin
                or cx > right + boundary_margin
                or cy < top - boundary_margin
                or cy > bottom + boundary_margin
            ):
                continue

            sample_radius = max(8.0, radius * 0.58)
            mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= sample_radius ** 2
            pixels = image[mask]
            if pixels.size == 0:
                continue
            hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
            gray = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).reshape(-1)
            median_gray = float(np.median(gray))
            median_saturation = float(np.median(hsv[:, 1]))
            median_hue = float(np.median(hsv[:, 0]))

            is_red_frame = median_saturation >= 170 and (median_hue <= 4 or median_hue >= 176)
            is_over_saturated_frame = median_saturation > max_piece_saturation
            looks_like_empty_board = (
                median_gray >= max_empty_gray and median_saturation < min_piece_saturation
            )
            if is_red_frame or is_over_saturated_frame or looks_like_empty_board:
                continue

            center_radius = max(6.0, radius * 0.48)
            body_radius = max(8.0, radius * 0.78)
            center_mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= center_radius ** 2
            body_mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= body_radius ** 2
            center_gray = gray_image[center_mask]
            body_gray = gray_image[body_mask]
            if center_gray.size == 0 or body_gray.size == 0:
                continue
            center_dark_ratio = float(np.mean(center_gray < 120))
            body_dark_ratio = float(np.mean(body_gray < 135))
            body_std = float(np.std(body_gray))
            looks_like_shadow_or_grid = (
                center_dark_ratio < min_center_dark_ratio
                and body_dark_ratio < min_body_dark_ratio
                and body_std < max_empty_body_std
            )
            if looks_like_shadow_or_grid:
                if not has_ring_evidence:
                    continue
                rescue_reasons.append("appearance_filter_with_ring_evidence")

            item = dict(circle)
            item["appearance_gray"] = round(median_gray, 1)
            item["appearance_saturation"] = round(median_saturation, 1)
            item["appearance_center_dark"] = round(center_dark_ratio, 3)
            item["appearance_body_dark"] = round(body_dark_ratio, 3)
            item["appearance_body_std"] = round(body_std, 1)
            item["appearance_ring_edge_coverage"] = round(ring_edge_coverage, 3)
            item["appearance_narrow_ring_edge_coverage"] = round(
                narrow_ring_edge_coverage,
                3,
            )
            if rescue_reasons:
                item["appearance_filter_rescue"] = rescue_reasons
            filtered.append(item)

        filtered.sort(key=lambda circle: (circle["cy"], circle["cx"]))
        for index, circle in enumerate(filtered):
            circle["circle_id"] = index
        return filtered
    except Exception:
        LOGGER.exception("Piece circle appearance filtering failed")
        return circles


def detect_piece_circles(image_body, suppress_grid_lines=True, board_crop=None):
    if cv2 is None:
        return []
    try:
        detection_body = image_body
        if suppress_grid_lines:
            detection_body, _ = suppress_board_grid_lines(image_body, board_crop=board_crop)
        import numpy as np
        image_array = np.frombuffer(detection_body, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_GRAYSCALE)
        if image is None:
            return []
        blurred = cv2.medianBlur(image, 5)
        min_dim = min(image.shape[:2])
        min_radius = int(os.getenv("CHESS_CIRCLE_MIN_RADIUS", str(max(12, round(min_dim * 0.012)))))
        max_radius = int(os.getenv("CHESS_CIRCLE_MAX_RADIUS", str(max(min_radius + 8, round(min_dim * 0.049)))))
        min_dist = int(os.getenv("CHESS_CIRCLE_MIN_DIST", str(max(24, round(min_dim * 0.055)))))
        param1 = int(os.getenv("CHESS_CIRCLE_PARAM1", "80"))
        param2 = int(os.getenv("CHESS_CIRCLE_PARAM2", "30"))
        circles = cv2.HoughCircles(
            blurred,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=min_dist,
            param1=param1,
            param2=param2,
            minRadius=min_radius,
            maxRadius=max_radius,
        )
        if circles is None:
            return []
        detected = [
            {
                "circle_id": index,
                "cx": float(circle[0]),
                "cy": float(circle[1]),
                "radius": float(circle[2]),
            }
            for index, circle in enumerate(circles[0])
        ]
        detected.sort(key=lambda circle: (circle["cy"], circle["cx"]))
        for index, circle in enumerate(detected):
            circle["circle_id"] = index
        detected = filter_piece_circle_candidates(detection_body, detected, board_crop=board_crop)
        max_candidates = int(
            os.getenv("CHESS_OPENCV_MAX_CANDIDATES", str(DEFAULT_MAX_VISIBLE_PIECES))
        )
        if max_candidates > 0:
            detected = detected[:max_candidates]
        return detected
    except Exception:
        LOGGER.exception("Piece circle detection failed")
        return []


def _candidate_crop_box(image, circle):
    crop_scale = float(os.getenv("CHESS_CANDIDATE_CROP_SCALE", "1.55"))
    cx = float(circle["cx"])
    cy = float(circle["cy"])
    radius = max(float(circle.get("radius", 20.0)), 12.0)
    half = radius * crop_scale
    left = max(0, int(round(cx - half)))
    top = max(0, int(round(cy - half)))
    right = min(image.width, int(round(cx + half)))
    bottom = min(image.height, int(round(cy + half)))
    return left, top, right, bottom


def create_candidate_crop_sheet(image_body, circles):
    if not parse_bool_env(os.getenv("CHESS_AI_ENABLE_CROP_SHEET", "true")):
        return None
    if Image is None or ImageDraw is None or not circles:
        return None

    try:
        image = Image.open(io.BytesIO(image_body)).convert("RGB")
    except Exception:
        LOGGER.exception("Failed to open image for candidate crop sheet")
        return None

    tile_size = int(os.getenv("CHESS_CROP_SHEET_TILE_SIZE", "160"))
    label_height = int(os.getenv("CHESS_CROP_SHEET_LABEL_HEIGHT", "20"))
    padding = int(os.getenv("CHESS_CROP_SHEET_PADDING", "8"))
    crop_scale = float(os.getenv("CHESS_CROP_SHEET_SCALE", "1.35"))
    jpeg_quality = int(os.getenv("CHESS_CROP_SHEET_JPEG_QUALITY", "96"))
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
    columns = int(os.getenv("CHESS_CROP_SHEET_COLUMNS", "4"))
    columns = max(1, columns)
    rows = (len(circles) + columns - 1) // columns
    sheet_width = columns * tile_size
    sheet_height = rows * (tile_size + label_height)
    sheet = Image.new("RGB", (sheet_width, sheet_height), (248, 248, 248))
    draw = ImageDraw.Draw(sheet)

    for index, circle in enumerate(circles):
        col = index % columns
        row = index // columns
        x0 = col * tile_size
        y0 = row * (tile_size + label_height)
        cx = float(circle["cx"])
        cy = float(circle["cy"])
        radius = max(float(circle.get("radius", 20.0)), 12.0)
        half = radius * crop_scale
        left = max(0, int(round(cx - half)))
        top = max(0, int(round(cy - half)))
        right = min(image.width, int(round(cx + half)))
        bottom = min(image.height, int(round(cy + half)))
        crop = image.crop((left, top, right, bottom))
        original_crop_width = max(1, right - left)
        original_crop_height = max(1, bottom - top)
        crop.thumbnail(
            (tile_size - 2 * padding, tile_size - label_height - 2 * padding),
            resample=resampling,
        )
        paste_x = x0 + (tile_size - crop.width) // 2
        paste_y = y0 + label_height + (tile_size - label_height - crop.height) // 2
        sheet.paste(crop, (paste_x, paste_y))
        scale_x = crop.width / original_crop_width
        scale_y = crop.height / original_crop_height
        marker_cx = paste_x + (cx - left) * scale_x
        marker_cy = paste_y + (cy - top) * scale_y
        marker_radius = max(8.0, radius * min(scale_x, scale_y))
        draw.ellipse(
            (
                marker_cx - marker_radius,
                marker_cy - marker_radius,
                marker_cx + marker_radius,
                marker_cy + marker_radius,
            ),
            outline=(0, 170, 80),
            width=2,
        )
        draw.rectangle((x0, y0, x0 + tile_size - 1, y0 + tile_size + label_height - 1), outline=(40, 120, 220))
        label = f"id {int(circle['circle_id'])}"
        if "board_x" in circle and "board_y" in circle:
            label += f" ({int(circle['board_x'])},{int(circle['board_y'])})"
        draw.text((x0 + 4, y0 + 3), label, fill=(0, 0, 0))

    output = io.BytesIO()
    sheet.save(output, format="JPEG", quality=jpeg_quality, subsampling=0)
    return output.getvalue()


def save_candidate_crop_images(debug_path, safe_frame_id, image_body, circles):
    """Save one source-resolution crop per OpenCV candidate for inspection or labeling."""
    if not parse_bool_env(os.getenv("CHESS_DEBUG_SAVE_CANDIDATE_CROPS", "true")):
        return None
    if Image is None or not circles:
        return None

    try:
        image = Image.open(io.BytesIO(image_body)).convert("RGB")
        crop_dir = debug_path / f"{safe_frame_id}.candidate_crops"
        crop_dir.mkdir(parents=True, exist_ok=True)
        metadata = []
        for index, circle in enumerate(circles):
            left, top, right, bottom = _candidate_crop_box(image, circle)
            if right <= left or bottom <= top:
                continue
            crop = image.crop((left, top, right, bottom))
            circle_id = int(circle.get("circle_id", index))
            filename = (
                f"candidate_{circle_id:03d}_"
                f"x{circle.get('board_x', 'na')}_y{circle.get('board_y', 'na')}.jpg"
            )
            crop.save(crop_dir / filename, format="JPEG", quality=96, subsampling=0)
            metadata.append(
                {
                    "filename": filename,
                    "circle_id": circle_id,
                    "cx": round(float(circle["cx"]), 2),
                    "cy": round(float(circle["cy"]), 2),
                    "radius": round(float(circle.get("radius", 0.0)), 2),
                    "board_x": circle.get("board_x"),
                    "board_y": circle.get("board_y"),
                    "crop_box": [left, top, right, bottom],
                }
            )
        (crop_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return crop_dir
    except Exception:
        LOGGER.exception("Candidate crop export failed")
        return None


def annotate_circle_candidates_with_board_points(circles, image_width, image_height, board_crop=None):
    orientation = {"bottom_side": PLAYER1_SIDE, "top_side": PLAYER2_SIDE}
    annotated = []
    for circle in circles:
        item = dict(circle)
        try:
            point = pixel_to_canonical_point(
                float(circle["cx"]),
                float(circle["cy"]),
                orientation,
                image_width,
                image_height,
                board_crop=board_crop,
            )
            item["board_x"] = point["x"]
            item["board_y"] = point["y"]
        except Exception as exc:
            item["board_error"] = str(exc)
        annotated.append(item)
    return annotated


def apply_circle_candidates_to_processed(processed, circles):
    if not circles:
        processed["opencv_candidate_status"] = "none"
        return processed

    by_id = {int(circle["circle_id"]): circle for circle in circles if "circle_id" in circle}
    max_distance = float(os.getenv("CHESS_VISUAL_EVIDENCE_MAX_DISTANCE", "30"))
    used_ids = set()
    adjustments = []
    unmatched_indexes = []
    removal_reasons = {}

    for piece_index, piece in enumerate(processed.get("pieces", [])):
        circle = None
        circle_id = piece.get("circle_id")
        if circle_id is not None:
            try:
                circle = by_id.get(int(circle_id))
            except (TypeError, ValueError):
                circle = None

        if circle is None:
            cx = _get_optional_float(piece.get("cx"))
            cy = _get_optional_float(piece.get("cy"))
            if cx is not None and cy is not None:
                circle = min(
                    circles,
                    key=lambda item: ((item["cx"] - cx) ** 2 + (item["cy"] - cy) ** 2),
                )
                distance = ((circle["cx"] - cx) ** 2 + (circle["cy"] - cy) ** 2) ** 0.5
                if distance > max_distance:
                    circle = None
                    unmatched_indexes.append(piece_index)
                    removal_reasons[piece_index] = "no_opencv_circle_within_visual_evidence_distance"
                    continue

        if circle is None:
            unmatched_indexes.append(piece_index)
            removal_reasons[piece_index] = "no_opencv_circle_within_visual_evidence_distance"
            continue

        old_center = {
            "cx": _get_optional_float(piece.get("cx")),
            "cy": _get_optional_float(piece.get("cy")),
        }
        piece["circle_id"] = int(circle["circle_id"])
        piece["cx"] = float(circle["cx"])
        piece["cy"] = float(circle["cy"])
        piece["circle_radius"] = float(circle["radius"])
        old_point = {"x": piece["x"], "y": piece["y"]}
        if "board_x" in circle and "board_y" in circle:
            try:
                point = validate_board_point(
                    {
                        "x": circle["board_x"],
                        "y": circle["board_y"],
                    }
                )
                piece["x"] = point["x"]
                piece["y"] = point["y"]
                piece["coordinate_source"] = "opencv_candidate_board_xy"
            except (TypeError, ValueError):
                piece["coordinate_source"] = "opencv_candidate_center_only"
        else:
            piece["coordinate_source"] = "opencv_candidate_center_only"
        if old_center["cx"] != piece["cx"] or old_center["cy"] != piece["cy"]:
            adjustments.append(
                {
                    "side": piece["side"],
                    "name": piece["name"],
                    "circle_id": piece["circle_id"],
                    "from": old_center,
                    "to": {"cx": piece["cx"], "cy": piece["cy"]},
                }
            )
        if old_point["x"] != piece["x"] or old_point["y"] != piece["y"]:
            adjustments.append(
                {
                    "side": piece["side"],
                    "name": piece["name"],
                    "circle_id": piece["circle_id"],
                    "from": old_point,
                    "to": {"x": piece["x"], "y": piece["y"]},
                    "coordinate_source": piece["coordinate_source"],
                }
            )
        if piece["circle_id"] in used_ids:
            piece["visual_evidence"] = "duplicate_circle"
            unmatched_indexes.append(piece_index)
            removal_reasons[piece_index] = "duplicate_opencv_circle_id"
            continue
        used_ids.add(piece["circle_id"])

    strict_circle_binding = parse_bool_env(
        os.getenv("CHESS_OPENCV_REJECT_UNMATCHED_AI_PIECES", "true")
    )
    if strict_circle_binding and unmatched_indexes:
        pieces = processed.get("pieces", [])
        unmatched_set = set(unmatched_indexes)
        retained_pieces = []
        for piece_index, piece in enumerate(pieces):
            if piece_index not in unmatched_set:
                retained_pieces.append(piece)
                continue
            adjustments.append(
                {
                    "side": piece.get("side"),
                    "name": piece.get("name"),
                    "action": "duplicate_circle_ai_piece_removed"
                    if removal_reasons.get(piece_index) == "duplicate_opencv_circle_id"
                    else "unmatched_ai_piece_removed",
                    "reason": removal_reasons.get(
                        piece_index,
                        "no_opencv_circle_within_visual_evidence_distance",
                    ),
                    "circle_id": piece.get("circle_id"),
                    "x": piece.get("x"),
                    "y": piece.get("y"),
                }
            )
        processed["pieces"] = retained_pieces

    processed["opencv_candidate_status"] = "used"
    processed["opencv_candidate_adjustments"] = adjustments
    return processed


def apply_visual_evidence_to_processed(processed, image_body, circles=None, board_crop=None):
    if not parse_bool_env(os.getenv("CHESS_AI_REQUIRE_VISUAL_EVIDENCE", "true")):
        processed["visual_evidence_status"] = "disabled"
        processed["visual_evidence_errors"] = []
        processed["detected_piece_circles"] = []
        return processed

    circles = circles if circles is not None else detect_piece_circles(image_body, board_crop=board_crop)
    processed["detected_piece_circles"] = circles
    if cv2 is None:
        processed["visual_evidence_status"] = "skipped"
        processed["visual_evidence_errors"] = ["opencv is not installed"]
        return processed
    if not circles:
        processed["visual_evidence_status"] = "failed"
        processed["visual_evidence_errors"] = ["no piece circles detected"]
        return processed

    max_distance = float(os.getenv("CHESS_VISUAL_EVIDENCE_MAX_DISTANCE", "30"))
    errors = []
    for piece in processed.get("pieces", []):
        cx = _get_optional_float(piece.get("cx"))
        cy = _get_optional_float(piece.get("cy"))
        if cx is None or cy is None:
            piece["visual_evidence"] = "missing_center"
            errors.append(f"{piece['side']} {piece['name']} is missing cx/cy")
            continue
        if piece.get("visual_evidence") == "duplicate_circle":
            errors.append(
                f"{piece['side']} {piece['name']} uses a detected circle already assigned to another piece"
            )
            continue
        nearest = min(
            circles,
            key=lambda circle: ((circle["cx"] - cx) ** 2 + (circle["cy"] - cy) ** 2),
        )
        distance = ((nearest["cx"] - cx) ** 2 + (nearest["cy"] - cy) ** 2) ** 0.5
        piece["visual_evidence_distance"] = round(distance, 2)
        if distance <= max_distance:
            piece["visual_evidence"] = "circle"
        else:
            piece["visual_evidence"] = "missing"
            errors.append(
                f"{piece['side']} {piece['name']} at cx/cy ({cx:.1f},{cy:.1f}) "
                f"is {distance:.1f}px from nearest detected piece circle"
            )

    processed["visual_evidence_status"] = "ok" if not errors else "failed"
    processed["visual_evidence_errors"] = errors[:8]
    return processed


def apply_grid_snap_to_processed(processed, image_width, image_height, board_crop=None):
    if not parse_bool_env(os.getenv("CHESS_AI_ENABLE_GRID_SNAP", "true")):
        processed["grid_snap_status"] = "disabled"
        processed["grid_snap_errors"] = []
        processed["grid_snap_adjustments"] = []
        processed["grid_snap_disambiguations"] = []
        processed["grid_snap_move_adjustments"] = []
        processed["piece_coordinate_source"] = "ai_direct"
        return processed

    adjustments = []
    orientation = processed.get("orientation", {})
    try:
        geometry = get_board_geometry(image_width, image_height, orientation, board_crop=board_crop)
    except Exception as exc:
        processed["grid_snap_status"] = "failed"
        processed["grid_snap_errors"] = [str(exc)]
        processed.setdefault("board_geometry_mode", "unknown")
        processed.setdefault("board_geometry", {})
        processed.setdefault("piece_coordinate_source", "worker_grid_from_cxcy")
        processed["grid_snap_adjustments"] = adjustments
        processed["grid_snap_disambiguations"] = []
        processed["grid_snap_move_adjustments"] = []
        return processed

    processed["board_geometry_mode"] = geometry["mode"]
    processed["board_geometry"] = serialize_board_geometry(geometry, image_width, image_height)
    processed["piece_coordinate_source"] = "worker_grid_from_cxcy"
    for piece in processed.get("pieces", []):
        cx = _get_optional_float(piece.get("cx"))
        cy = _get_optional_float(piece.get("cy"))
        if cx is None or cy is None:
            continue
        if cx < 0 or cy < 0 or cx >= image_width or cy >= image_height:
            adjustments.append(
                {
                    "side": piece["side"],
                    "name": piece["name"],
                    "from": {"x": piece["x"], "y": piece["y"]},
                    "to": None,
                    "cx": cx,
                    "cy": cy,
                    "error": "piece center is outside image bounds",
                }
            )
            continue
        try:
            snapped = pixel_to_canonical_point(cx, cy, orientation, image_width, image_height, board_crop=board_crop)
        except Exception as exc:
            adjustments.append(
                {
                    "side": piece["side"],
                    "name": piece["name"],
                    "from": {"x": piece["x"], "y": piece["y"]},
                    "to": None,
                    "cx": cx,
                    "cy": cy,
                    "error": str(exc),
                }
            )
            continue
        if snapped["x"] != piece["x"] or snapped["y"] != piece["y"]:
            adjustments.append(
                {
                    "side": piece["side"],
                    "name": piece["name"],
                    "from": {"x": piece["x"], "y": piece["y"]},
                    "to": snapped,
                    "cx": cx,
                    "cy": cy,
                }
            )
            piece["x"] = snapped["x"]
            piece["y"] = snapped["y"]

    disambiguations = resolve_duplicate_piece_points(processed, image_width, image_height, board_crop=board_crop)
    move_adjustments = []
    for adjustment in adjustments + disambiguations:
        if adjustment.get("to") is None:
            continue
        for side, move in processed.get("recommended_moves", {}).items():
            start = move.get("from", {})
            if side == adjustment["side"] and start == adjustment["from"]:
                move["from"] = adjustment["to"]
                move_adjustments.append(
                    {
                        "side": side,
                        "from": adjustment["from"],
                        "to": adjustment["to"],
                    }
                )

    snap_errors = [adjustment["error"] for adjustment in adjustments if "error" in adjustment]
    if snap_errors:
        processed["grid_snap_status"] = "partial_failed"
    else:
        processed["grid_snap_status"] = "ok" if adjustments else "no_adjustments"
    processed["grid_snap_errors"] = snap_errors[:8]
    processed["grid_snap_adjustments"] = adjustments
    processed["grid_snap_disambiguations"] = disambiguations
    processed["grid_snap_move_adjustments"] = move_adjustments
    return processed


def _piece_at(pieces, point):
    for piece in pieces:
        if piece["x"] == point["x"] and piece["y"] == point["y"]:
            return piece
    return None


def _is_inside_palace(side, point):
    if point["x"] < 3 or point["x"] > 5:
        return False
    if side == PLAYER1_SIDE:
        return 0 <= point["y"] <= 2
    if side == PLAYER2_SIDE:
        return 7 <= point["y"] <= 9
    return False


def _basic_move_is_legal(piece, move, pieces):
    start = move["from"]
    end = move["to"]
    dx = end["x"] - start["x"]
    dy = end["y"] - start["y"]
    adx = abs(dx)
    ady = abs(dy)
    target = _piece_at(pieces, end)
    if target and target["side"] == piece["side"]:
        return False

    name = piece["name"]
    side = piece["side"]
    if name in ("shuai", "jiang"):
        return adx + ady == 1 and _is_inside_palace(side, end)
    if name == "shi":
        return adx == 1 and ady == 1 and _is_inside_palace(side, end)
    if name == "xiang":
        return adx == 2 and ady == 2
    if name == "ma":
        return (adx == 1 and ady == 2) or (adx == 2 and ady == 1)
    if name == "che":
        return (adx == 0) != (ady == 0)
    if name == "pao":
        return (adx == 0) != (ady == 0)
    if name in ("bing", "zu"):
        forward = 1 if side == PLAYER1_SIDE else -1
        if dx == 0 and dy == forward:
            return True
        crossed_river = start["y"] >= 5 if side == PLAYER1_SIDE else start["y"] <= 4
        return crossed_river and adx == 1 and dy == 0
    return False


def _inside_board(point):
    return 0 <= point["x"] < BOARD_FILES and 0 <= point["y"] < BOARD_RANKS


def _candidate_move_points(piece):
    x = piece["x"]
    y = piece["y"]
    name = piece["name"]
    side = piece["side"]

    if name in ("shuai", "jiang"):
        for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            yield {"x": x + dx, "y": y + dy}
    elif name == "shi":
        for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            yield {"x": x + dx, "y": y + dy}
    elif name == "xiang":
        for dx, dy in ((2, 2), (2, -2), (-2, 2), (-2, -2)):
            yield {"x": x + dx, "y": y + dy}
    elif name == "ma":
        for dx, dy in ((1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2)):
            yield {"x": x + dx, "y": y + dy}
    elif name in ("che", "pao"):
        for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            for step in range(1, max(BOARD_FILES, BOARD_RANKS)):
                yield {"x": x + dx * step, "y": y + dy * step}
    elif name in ("bing", "zu"):
        forward = 1 if side == PLAYER1_SIDE else -1
        yield {"x": x, "y": y + forward}
        crossed_river = y >= 5 if side == PLAYER1_SIDE else y <= 4
        if crossed_river:
            yield {"x": x + 1, "y": y}
            yield {"x": x - 1, "y": y}


def _basic_legal_moves_for_side(pieces, side):
    for piece in pieces:
        if piece["side"] != side or piece["name"] == "unknown":
            continue
        start = {"x": piece["x"], "y": piece["y"]}
        for end in _candidate_move_points(piece):
            if not _inside_board(end):
                continue
            move = {"from": start, "to": end}
            if _basic_move_is_legal(piece, move, pieces):
                yield piece, move


def repair_recommended_moves_to_basic_legal(processed):
    pieces = processed.get("pieces", [])
    moves = processed.get("recommended_moves", {})
    repairs = []

    for side in (PLAYER1_SIDE, PLAYER2_SIDE):
        move = moves.get(side)
        start_piece = _piece_at(pieces, move.get("from", {})) if isinstance(move, dict) else None
        if start_piece and start_piece["side"] == side and _basic_move_is_legal(start_piece, move, pieces):
            continue

        fallback_piece = None
        fallback_move = None
        for candidate_piece, candidate_move in _basic_legal_moves_for_side(pieces, side):
            fallback_piece = candidate_piece
            fallback_move = candidate_move
            break

        if fallback_move is None:
            continue

        moves[side] = fallback_move
        repairs.append(
            {
                "side": side,
                "piece": fallback_piece["name"],
                "from": fallback_move["from"],
                "to": fallback_move["to"],
            }
        )

    if repairs:
        processed["recommended_moves"] = moves
        processed["move_repair_status"] = "repaired"
        processed["move_repair_adjustments"] = repairs
        sync_move_compatibility_fields(processed)
    else:
        processed.setdefault("move_repair_status", "no_adjustments")
        processed.setdefault("move_repair_adjustments", [])
    return processed


def validate_processed_result(processed):
    errors = []
    pieces = processed.get("pieces", [])
    if not pieces:
        errors.append("pieces must contain visible board pieces")
    max_visible_pieces = int(os.getenv("CHESS_AI_MAX_VISIBLE_PIECES", str(DEFAULT_MAX_VISIBLE_PIECES)))
    if max_visible_pieces > 0 and len(pieces) > max_visible_pieces:
        errors.append(f"too many visible pieces: got {len(pieces)}, max {max_visible_pieces}")

    occupied = {}
    for piece in pieces:
        if _get_optional_float(piece.get("cx")) is None or _get_optional_float(piece.get("cy")) is None:
            errors.append(f"piece {piece['side']} {piece['name']} at ({piece['x']},{piece['y']}) is missing cx/cy")
        if (
            parse_bool_env(os.getenv("CHESS_AI_REQUIRE_VISUAL_EVIDENCE", "true"))
            and piece.get("visual_evidence") not in (None, "circle")
        ):
            errors.append(
                f"piece {piece['side']} {piece['name']} at ({piece['x']},{piece['y']}) lacks visual circle evidence"
            )
        key = (piece["x"], piece["y"])
        if key in occupied:
            prev = occupied[key]
            errors.append(
                "duplicate piece coordinate "
                f"({key[0]},{key[1]}): {prev['side']} {prev['name']} and {piece['side']} {piece['name']}"
            )
        else:
            occupied[key] = piece
        if piece["side"] not in (PLAYER1_SIDE, PLAYER2_SIDE):
            errors.append(f"piece at ({piece['x']},{piece['y']}) has unknown side")
        if piece["name"] == "unknown":
            errors.append(f"piece at ({piece['x']},{piece['y']}) has unknown name")
        if parse_bool_env(
            os.getenv("CHESS_AI_REQUIRE_CONFIDENT_TEXT_COLOR", "true")
        ) and piece.get("color_classification_status") in (
            "low_confidence",
            "unresolved",
        ):
            errors.append(
                f"piece {piece['name']} at ({piece['x']},{piece['y']}) has "
                f"{piece['color_classification_status']} text color "
                f"(confidence={piece.get('color_confidence', 0.0)})"
            )

    shuai_pieces = [p for p in pieces if p["side"] == PLAYER1_SIDE and p["name"] == "shuai"]
    jiang_pieces = [p for p in pieces if p["side"] == PLAYER2_SIDE and p["name"] == "jiang"]
    if len(shuai_pieces) != 1:
        errors.append(f"expected exactly one shuai piece, got {len(shuai_pieces)}")
    elif not _is_inside_palace(PLAYER1_SIDE, shuai_pieces[0]):
        errors.append("shuai piece is outside the shuai palace")
    if len(jiang_pieces) != 1:
        errors.append(f"expected exactly one jiang piece, got {len(jiang_pieces)}")
    elif not _is_inside_palace(PLAYER2_SIDE, jiang_pieces[0]):
        errors.append("jiang piece is outside the jiang palace")

    if len(shuai_pieces) == 1 and len(jiang_pieces) == 1:
        shuai_color = shuai_pieces[0].get("color", "unknown")
        jiang_color = jiang_pieces[0].get("color", "unknown")
        if shuai_color == "unknown":
            errors.append("shuai piece color is unknown")
        if jiang_color == "unknown":
            errors.append("jiang piece color is unknown")
        if shuai_color != "unknown" and jiang_color != "unknown":
            if shuai_color == jiang_color:
                errors.append("shuai and jiang pieces cannot have the same text color")
            else:
                for piece in pieces:
                    if piece["color"] == shuai_color and piece["side"] != PLAYER1_SIDE:
                        errors.append(
                            f"{piece['color']} piece at ({piece['x']},{piece['y']}) must belong to shuai side"
                        )
                    if piece["color"] == jiang_color and piece["side"] != PLAYER2_SIDE:
                        errors.append(
                            f"{piece['color']} piece at ({piece['x']},{piece['y']}) must belong to jiang side"
                        )

    orientation = processed.get("orientation", {})
    if orientation.get("bottom_side") == orientation.get("top_side"):
        errors.append("orientation bottom_side and top_side must be different")
    if orientation.get("bottom_side") == "unknown" or orientation.get("top_side") == "unknown":
        errors.append("orientation must identify both bottom_side and top_side")

    for side in (PLAYER1_SIDE, PLAYER2_SIDE):
        move = processed["recommended_moves"][side]
        start_piece = _piece_at(pieces, move["from"])
        if start_piece is None:
            errors.append(
                f"{side} recommended move starts from an empty point "
                f"({move['from']['x']},{move['from']['y']})"
            )
            continue
        if start_piece["side"] != side:
            errors.append(
                f"{side} recommended move starts from {start_piece['side']} piece "
                f"at ({move['from']['x']},{move['from']['y']})"
            )
            continue
        if not _basic_move_is_legal(start_piece, move, pieces):
            errors.append(
                f"{side} recommended move is not a basic legal-looking {start_piece['name']} move "
                f"from ({move['from']['x']},{move['from']['y']}) to ({move['to']['x']},{move['to']['y']})"
            )

    return errors


def mark_invalid_ai_result(processed, validation_errors):
    processed["confidence"] = min(float(processed.get("confidence", 0.0)), 0.1)
    processed["validation_status"] = "structural_failed"
    processed["validation_errors"] = validation_errors[:8]
    processed.setdefault("review_status", "not_run")
    processed.setdefault("review_errors", [])
    return processed


def mark_valid_ai_result(processed):
    processed["validation_status"] = "structural_ok"
    processed["validation_errors"] = []
    processed.setdefault("review_status", "not_run")
    processed.setdefault("review_errors", [])
    return processed


def mark_reviewed_ai_result(processed):
    processed["validation_status"] = "structural_ok"
    processed["validation_errors"] = []
    processed["review_status"] = "review_ok"
    processed["review_errors"] = []
    return processed


def mark_review_failed_result(processed, review_errors):
    processed["review_status"] = "review_failed"
    processed["review_errors"] = review_errors[:8]
    processed["confidence"] = min(float(processed.get("confidence", 0.0)), 0.2)
    return processed


def mark_invalid_review_result(processed, validation_errors):
    processed = mark_invalid_ai_result(processed, validation_errors)
    processed["review_status"] = "review_failed"
    processed["review_errors"] = validation_errors[:8]
    return processed


def create_debug_overlay(image_body, result, overlay_path):
    if Image is None or ImageDraw is None:
        LOGGER.warning("Pillow is not installed; debug overlay was not generated")
        return False

    try:
        image = Image.open(io.BytesIO(image_body)).convert("RGB")
    except Exception:
        LOGGER.exception("Failed to open image for debug overlay")
        return False

    draw = ImageDraw.Draw(image)
    image_width, image_height = image.size
    orientation = result.get("orientation", {})
    board_crop = board_crop_from_result_geometry(result.get("board_geometry"))

    try:
        geometry = get_board_geometry(image_width, image_height, orientation, board_crop=board_crop)
    except Exception:
        LOGGER.exception("Failed to compute board geometry for debug overlay")
        return False

    grid_color = (30, 120, 255)
    label_color = (20, 80, 180)
    move_color = (0, 190, 80)
    red_piece = (220, 40, 40)
    black_piece = (20, 20, 20)
    unknown_piece = (130, 80, 180)
    evidence_circle = (120, 220, 40)

    for circle in result.get("detected_piece_circles", []):
        cx = _get_optional_float(circle.get("cx"))
        cy = _get_optional_float(circle.get("cy"))
        radius = _get_optional_float(circle.get("radius"))
        if cx is None or cy is None or radius is None:
            continue
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=evidence_circle, width=2)

    for file_index in range(BOARD_FILES):
        x0, y0 = canonical_point_to_pixel(
            {"x": file_index, "y": 0},
            orientation,
            image_width,
            image_height,
            board_crop=board_crop,
        )
        x1, y1 = canonical_point_to_pixel(
            {"x": file_index, "y": BOARD_RANKS - 1},
            orientation,
            image_width,
            image_height,
            board_crop=board_crop,
        )
        draw.line((x0, y0, x1, y1), fill=grid_color, width=1)

    for rank_index in range(BOARD_RANKS):
        x0, y0 = canonical_point_to_pixel(
            {"x": 0, "y": rank_index},
            orientation,
            image_width,
            image_height,
            board_crop=board_crop,
        )
        x1, y1 = canonical_point_to_pixel(
            {"x": BOARD_FILES - 1, "y": rank_index},
            orientation,
            image_width,
            image_height,
            board_crop=board_crop,
        )
        draw.line((x0, y0, x1, y1), fill=grid_color, width=1)

    corners = geometry["corners"]
    draw.line(corners + [corners[0]], fill=grid_color, width=3)

    for file_index in range(BOARD_FILES):
        for rank_index in range(BOARD_RANKS):
            px, py = canonical_point_to_pixel(
                {"x": file_index, "y": rank_index},
                orientation,
                image_width,
                image_height,
                board_crop=board_crop,
            )
            draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=grid_color)
            if rank_index in (0, BOARD_RANKS - 1):
                draw.text((px + 4, py + 4), f"{file_index},{rank_index}", fill=label_color)

    for piece in result.get("pieces", []):
        point = {"x": int(piece["x"]), "y": int(piece["y"])}
        px, py = canonical_point_to_pixel(point, orientation, image_width, image_height, board_crop=board_crop)
        color = red_piece if piece.get("color") == "red" else black_piece if piece.get("color") == "black" else unknown_piece
        if piece.get("visual_evidence") == "missing":
            color = (255, 128, 0)
        draw.ellipse((px - 15, py - 15, px + 15, py + 15), outline=color, width=4)
        label = f"{piece.get('side','?')[0]}:{piece.get('name','?')} {point['x']},{point['y']}"
        draw.text((px + 16, py - 12), label, fill=color)
        cx = _get_optional_float(piece.get("cx"))
        cy = _get_optional_float(piece.get("cy"))
        if cx is not None and cy is not None:
            draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=(255, 190, 0))
            draw.line((cx, cy, px, py), fill=(255, 190, 0), width=2)

    for side, move in result.get("recommended_moves", {}).items():
        try:
            sx, sy = canonical_point_to_pixel(
                move["from"],
                orientation,
                image_width,
                image_height,
                board_crop=board_crop,
            )
            ex, ey = canonical_point_to_pixel(
                move["to"],
                orientation,
                image_width,
                image_height,
                board_crop=board_crop,
            )
        except Exception:
            continue
        draw.line((sx, sy, ex, ey), fill=move_color, width=4)
        draw.ellipse((sx - 6, sy - 6, sx + 6, sy + 6), fill=move_color)
        draw.rectangle((ex - 6, ey - 6, ex + 6, ey + 6), fill=move_color)
        draw.text((ex + 8, ey + 8), side, fill=move_color)

    status = f"validation={result.get('validation_status','?')} review={result.get('review_status','?')}"
    draw.rectangle((0, 0, image_width, 24), fill=(255, 255, 255))
    draw.text((6, 5), status, fill=(0, 0, 0))

    image.save(overlay_path, quality=92)
    return True


def fixed_placeholder_result():
    return build_processed_result(
        {"x": 4, "y": 0},
        {"x": 4, "y": 1},
        {"x": 2, "y": 9},
        {"x": 2, "y": 7},
        0.95,
        orientation={
            "bottom_side": PLAYER1_SIDE,
            "top_side": PLAYER2_SIDE,
        },
        pieces=[],
    )


def _point_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "x": {"type": "integer", "minimum": 0, "maximum": BOARD_FILES - 1},
            "y": {"type": "integer", "minimum": 0, "maximum": BOARD_RANKS - 1},
        },
        "required": ["x", "y"],
    }


def ai_response_schema():
    point_schema = _point_schema()
    side_schema = {"type": "string", "enum": [PLAYER1_SIDE, PLAYER2_SIDE, "unknown"]}
    color_schema = {"type": "string", "enum": ["red", "black", "unknown"]}
    move_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "from": point_schema,
            "to": point_schema,
        },
        "required": ["from", "to"],
    }
    piece_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "side": side_schema,
            "color": color_schema,
            "name": {"type": "string"},
            "circle_id": {"type": "integer", "minimum": 0},
            "cx": {"type": "number", "minimum": 0},
            "cy": {"type": "number", "minimum": 0},
        },
        "required": ["side", "color", "name", "circle_id", "cx", "cy"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "orientation": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "bottom_side": side_schema,
                    "top_side": side_schema,
                },
                "required": ["bottom_side", "top_side"],
            },
            "pieces": {
                "type": "array",
                "items": piece_schema,
            },
            "recommended_moves": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    PLAYER1_SIDE: move_schema,
                    PLAYER2_SIDE: move_schema,
                },
                "required": [PLAYER1_SIDE, PLAYER2_SIDE],
            },
        },
        "required": ["confidence", "orientation", "pieces", "recommended_moves"],
    }


def build_circle_candidate_text(circle_candidates):
    if not circle_candidates:
        return (
            "OpenCV did not provide circle candidates. Detect visible pieces directly from the image, "
            "but still avoid invented pieces. "
        )
    items = []
    for circle in circle_candidates:
        items.append(
            {
                "circle_id": int(circle["circle_id"]),
                "cx": round(float(circle["cx"]), 1),
                "cy": round(float(circle["cy"]), 1),
                "radius": round(float(circle["radius"]), 1),
                "board_x": circle.get("board_x"),
                "board_y": circle.get("board_y"),
            }
        )
    return (
        "OpenCV detected these circular piece candidates. "
        "You must classify only these candidates; do not create a piece at any other center. "
        "If a candidate is not a chess piece, omit it. "
        "For every returned piece, copy exactly one circle_id and its cx/cy from this list. "
        "Use the candidate's board_x/board_y as the piece location context; do not guess another location. "
        "Do not reuse the same circle_id for multiple pieces. "
        "If a second image is provided, it is a numbered crop sheet of the same candidates. "
        "Each crop may contain neighboring pieces; classify only the piece marked by the green ring. "
        "Classify each numbered crop by reading the Chinese character printed on the green-marked piece. "
        "Map characters to names as follows: 帅=shuai, 将=jiang, 士/仕=shi, 相/象=xiang, 马/馬=ma, 车/車=che, 炮/砲=pao, 兵=bing, 卒=zu. "
        "Do not assign a name from the palace location, expected setup, or move logic when the crop character says something else. "
        "Candidates JSON: "
        + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        + ". "
    )


def build_circle_candidate_text_clean(circle_candidates):
    if not circle_candidates:
        return (
            "OpenCV did not provide circle candidates. Detect visible pieces directly from the image, "
            "but do not invent pieces. "
        )
    items = [
        {
            "circle_id": int(circle["circle_id"]),
            "cx": round(float(circle["cx"]), 1),
            "cy": round(float(circle["cy"]), 1),
            "radius": round(float(circle["radius"]), 1),
            "board_x": circle.get("board_x"),
            "board_y": circle.get("board_y"),
        }
        for circle in circle_candidates
    ]
    return (
        "OpenCV detected these circular piece candidates. Classify only these candidates and do not "
        "create a piece at another center. Omit a candidate if it is not a chess piece. "
        "For every returned piece, copy exactly one circle_id and its cx/cy from this list. "
        "Use board_x/board_y as the location context and do not guess another location. "
        "Do not reuse a circle_id. If a numbered crop sheet is provided, classify the piece marked by "
        "the green ring and ignore neighboring pieces. Read the printed character, not the board position. "
        "Unicode character mapping: U+5E05=shuai, U+5C06=jiang, U+58EB/U+4ED5=shi, "
        "U+76F8/U+8C61=xiang, U+9A6C/U+99AC=ma, U+8F66/U+8ECA=che, "
        "U+70AE/U+7832=pao, U+5175=bing, U+5352=zu. Candidates JSON: "
        + json.dumps(items, ensure_ascii=True, separators=(",", ":"))
        + ". "
    )


def build_candidate_classifier_prompt(circle_candidates):
    items = [
        {
            "circle_id": int(circle["circle_id"]),
            "board_x": circle.get("board_x"),
            "board_y": circle.get("board_y"),
        }
        for circle in circle_candidates
    ]
    return (
        "Analyze only the Xiangqi candidate crop sheet image. "
        "Each tile has a numeric id and one target piece marked by a green ring. "
        "Read the Chinese character on the green-ringed target piece only; ignore neighboring pieces. "
        "Do not infer from board position, palace location, side, or common setup. "
        "Return only JSON with one top-level key items. "
        "items must be an array of objects with circle_id, char, name, and color. "
        "circle_id must be the numeric id only, not the board coordinate. "
        "name must be one of shuai, jiang, shi, xiang, ma, che, pao, bing, zu, unknown. "
        "color must be red, black, or unknown. "
        "Color supplement rule: 相/帅/帥/仕/兵 means red; 象/将/將/士/卒 means black. "
        "Character mapping: U+5E05=shuai, U+5C06=jiang, U+58EB/U+4ED5=shi, "
        "U+76F8/U+8C61=xiang, U+9A6C/U+99AC=ma, U+8F66/U+8ECA=che, "
        "U+70AE/U+7832=pao, U+5175=bing, U+5352=zu. "
        "Classify every readable candidate id from this list: "
        + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    )


def _parse_classifier_circle_id(value):
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"\d+", str(value))
    if match:
        return int(match.group(0))
    return None


def normalize_candidate_classifications(classifier_result):
    items = classifier_result.get("items", []) if isinstance(classifier_result, dict) else []
    classifications = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        circle_id = _parse_classifier_circle_id(item.get("circle_id"))
        if circle_id is None:
            continue
        classifications[circle_id] = {
            "char": str(item.get("char", "")),
            "name": validate_piece_name(item.get("name", "unknown")),
            "color": validate_piece_color(item.get("color", "unknown")),
        }
    return classifications


def infer_character_color(char):
    """Return a deterministic color hint for characters with red/black variants."""
    return CHARACTER_COLOR_HINTS.get(str(char or "").strip())


def apply_character_names_to_classifications(classifications):
    """Make an exact recognized Xiangqi character authoritative for its name."""
    if not classifications:
        return []

    adjustments = []
    for circle_id, classification in classifications.items():
        char = str(classification.get("char", "")).strip()
        name = CHARACTER_NAME_HINTS.get(char)
        if name is None:
            continue
        classification["character_name_authoritative"] = True
        old_name = classification.get("name", "unknown")
        if old_name == name:
            continue
        classification["name"] = name
        adjustments.append(
            {
                "circle_id": int(circle_id),
                "char": char,
                "from_name": old_name,
                "to_name": name,
                "reason": "character_name_hint",
            }
        )
    return adjustments


def merge_dedicated_character_classifications(classifications, dedicated_classifications):
    """Prefer exact characters from the dedicated pass before color calibration."""
    adjustments = []
    for circle_id, dedicated in dedicated_classifications.items():
        if not dedicated.get("character_name_authoritative", False):
            continue
        target = classifications.setdefault(
            int(circle_id),
            {"char": "", "name": "unknown", "color": "unknown"},
        )
        old_char = target.get("char", "")
        old_name = target.get("name", "unknown")
        new_char = dedicated.get("char", "")
        new_name = dedicated.get("name", "unknown")
        if old_char != new_char or old_name != new_name:
            adjustments.append(
                {
                    "circle_id": int(circle_id),
                    "from_char": old_char,
                    "to_char": new_char,
                    "from_name": old_name,
                    "to_name": new_name,
                    "reason": "dedicated_character_preferred",
                }
            )
        target["char"] = new_char
        target["name"] = new_name
        target["character_name_authoritative"] = True

        if dedicated.get("character_color_authoritative", False):
            for key in (
                "color",
                "character_color_authoritative",
                "color_classification_status",
                "color_confidence",
                "color_evidence",
            ):
                if key in dedicated:
                    target[key] = dedicated[key]
            continue

        if target.pop("character_color_authoritative", False):
            old_color = target.get("color", "unknown")
            target["color"] = "unknown"
            target.pop("color_classification_status", None)
            target.pop("color_confidence", None)
            target.pop("color_evidence", None)
            adjustments.append(
                {
                    "circle_id": int(circle_id),
                    "char": new_char,
                    "from_color": old_color,
                    "to_color": "unknown",
                    "reason": "conflicting_character_color_invalidated",
                }
            )
    return adjustments


def apply_character_text_colors_to_classifications(classifications):
    """Use exact recognized characters as authoritative color evidence."""
    if not classifications:
        return []

    adjustments = []
    for circle_id, classification in classifications.items():
        color = infer_character_color(classification.get("char"))
        if color is None:
            continue
        classification["character_color_authoritative"] = True
        classification["color_classification_status"] = "confident"
        classification["color_confidence"] = 1.0
        classification["color_evidence"] = "character_hint"
        old_color = classification.get("color", "unknown")
        if old_color == color:
            continue
        classification["color"] = color
        adjustments.append(
            {
                "circle_id": int(circle_id),
                "char": classification.get("char", ""),
                "from_color": old_color,
                "to_color": color,
                "reason": "character_color_hint",
            }
        )
    return adjustments


def estimate_candidate_text_colors(image_body, circles):
    if cv2 is None or not image_body or not circles:
        return {}

    try:
        import numpy as np

        image = cv2.imdecode(np.frombuffer(image_body, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return {}
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        height, width = image.shape[:2]
        y_grid, x_grid = np.ogrid[:height, :width]
        red_delta = float(os.getenv("CHESS_TEXT_COLOR_LAB_RED_DELTA", "12"))
        dark_delta = float(os.getenv("CHESS_TEXT_COLOR_LAB_DARK_DELTA", "25"))
        anchor_dark_delta = float(
            os.getenv("CHESS_TEXT_COLOR_ANCHOR_DARK_DELTA", "18")
        )
        min_ink_ratio = float(os.getenv("CHESS_TEXT_COLOR_MIN_INK_RATIO", "0.08"))
        min_cluster_size = max(
            2,
            int(os.getenv("CHESS_TEXT_COLOR_MIN_CLUSTER_SIZE", "2")),
        )
        min_cluster_separation = float(
            os.getenv("CHESS_TEXT_COLOR_MIN_CLUSTER_SEPARATION", "0.20")
        )
        min_cluster_margin = float(
            os.getenv("CHESS_TEXT_COLOR_MIN_CLUSTER_MARGIN", "0.10")
        )
        estimates = {}
        feature_rows = []

        for circle in circles:
            circle_id = _parse_classifier_circle_id(circle.get("circle_id"))
            if circle_id is None:
                continue
            cx = float(circle.get("cx", 0.0))
            cy = float(circle.get("cy", 0.0))
            radius = max(float(circle.get("radius", 20.0)), 12.0)
            distance = np.sqrt((x_grid - cx) ** 2 + (y_grid - cy) ** 2)
            inner_mask = (distance <= radius * 0.58) & (distance >= radius * 0.10)
            background_mask = (distance <= radius * 0.88) & (distance >= radius * 0.72)
            if not np.any(inner_mask) or not np.any(background_mask):
                continue

            hsv_pixels = hsv[inner_mask]
            lab_pixels = lab[inner_mask]
            lab_background = lab[background_mask]
            saturation = hsv_pixels[:, 1]
            value = hsv_pixels[:, 2]
            lightness = lab_pixels[:, 0].astype(np.float32)
            a_channel = lab_pixels[:, 1].astype(np.float32)
            background_lightness = float(np.median(lab_background[:, 0]))
            background_a = float(np.median(lab_background[:, 1]))
            red_pixels = (
                (a_channel > background_a + red_delta)
                & (a_channel > 145)
                & (saturation > 25)
                & (value < 250)
            )
            black_pixels = (
                (lightness < background_lightness - dark_delta)
                & (a_channel <= background_a + red_delta * 0.75)
            )
            red_ratio = float(np.mean(red_pixels))
            black_ratio = float(np.mean(black_pixels))
            ink_ratio = min(1.0, red_ratio + black_ratio)
            median_a = float(np.median(a_channel))
            a_delta = median_a - background_a
            color_score = red_ratio - black_ratio + a_delta / 100.0
            anchor_ink_mask = lightness < background_lightness - anchor_dark_delta
            anchor_ink_a_delta = a_channel[anchor_ink_mask] - background_a
            if anchor_ink_a_delta.size:
                ink_a_delta_median = float(np.median(anchor_ink_a_delta))
                ink_a_delta_p75 = float(np.percentile(anchor_ink_a_delta, 75))
                ink_redness_score = (
                    ink_a_delta_median * 0.65 + ink_a_delta_p75 * 0.35
                )
            else:
                ink_a_delta_median = None
                ink_a_delta_p75 = None
                ink_redness_score = None
            estimates[circle_id] = {
                "color": "unknown",
                "red_ratio": round(red_ratio, 3),
                "black_ratio": round(black_ratio, 3),
                "ink_ratio": round(ink_ratio, 3),
                "lab_a_median": round(median_a, 1),
                "lab_a_delta": round(a_delta, 1),
                "color_score": round(color_score, 3),
                "ink_pixel_count": int(anchor_ink_a_delta.size),
                "ink_lab_a_delta_median": (
                    round(ink_a_delta_median, 2)
                    if ink_a_delta_median is not None else None
                ),
                "ink_lab_a_delta_p75": (
                    round(ink_a_delta_p75, 2)
                    if ink_a_delta_p75 is not None else None
                ),
                "ink_redness_score": (
                    round(ink_redness_score, 3)
                    if ink_redness_score is not None else None
                ),
                "color_method": "global_kmeans_lab_hsv",
            }
            if ink_ratio >= min_ink_ratio:
                feature_rows.append(
                    {
                        "circle_id": circle_id,
                        "features": [
                            red_ratio,
                            black_ratio,
                            a_delta / 40.0,
                            ink_ratio,
                        ],
                        "color_score": color_score,
                    }
                )

        if len(feature_rows) < min_cluster_size * 2:
            return estimates

        raw_features = np.asarray(
            [row["features"] for row in feature_rows],
            dtype=np.float64,
        )
        feature_center = np.median(raw_features, axis=0)
        feature_scale = np.std(raw_features, axis=0)
        feature_scale[feature_scale < 1e-6] = 1.0
        normalized = (raw_features - feature_center) / feature_scale
        scores = np.asarray(
            [row["color_score"] for row in feature_rows],
            dtype=np.float64,
        )
        initial_indices = [int(np.argmin(scores)), int(np.argmax(scores))]
        if initial_indices[0] == initial_indices[1]:
            return estimates
        centers = normalized[initial_indices].copy()
        labels = np.zeros(len(feature_rows), dtype=np.int32)
        for _ in range(12):
            distances = np.sum((normalized[:, None, :] - centers[None, :, :]) ** 2, axis=2)
            new_labels = np.argmin(distances, axis=1)
            new_centers = centers.copy()
            for cluster_index in (0, 1):
                members = normalized[new_labels == cluster_index]
                if len(members):
                    new_centers[cluster_index] = np.mean(members, axis=0)
            if np.array_equal(new_labels, labels):
                centers = new_centers
                labels = new_labels
                break
            labels = new_labels
            centers = new_centers

        cluster_sizes = [int(np.sum(labels == index)) for index in (0, 1)]
        cluster_scores = [
            float(np.mean(scores[labels == index])) if cluster_sizes[index] else 0.0
            for index in (0, 1)
        ]
        separation = abs(cluster_scores[0] - cluster_scores[1])
        if (
            min(cluster_sizes) < min_cluster_size
            or separation < min_cluster_separation
        ):
            return estimates

        red_cluster = 0 if cluster_scores[0] > cluster_scores[1] else 1
        score_midpoint = (cluster_scores[0] + cluster_scores[1]) / 2.0
        for row, cluster_index in zip(feature_rows, labels):
            estimate = estimates[row["circle_id"]]
            distance_from_midpoint = abs(row["color_score"] - score_midpoint)
            confidence_margin = min(
                1.0,
                distance_from_midpoint / max(separation / 2.0, 1e-6),
            )
            estimate["color_cluster"] = int(cluster_index)
            estimate["color_cluster_separation"] = round(separation, 3)
            estimate["color_cluster_margin"] = round(confidence_margin, 3)
            if confidence_margin >= min_cluster_margin:
                estimate["color"] = (
                    "red" if int(cluster_index) == red_cluster else "black"
                )

        return estimates
    except Exception:
        LOGGER.exception("Visual text color estimation failed")
        return {}


def calibrate_visual_text_colors_with_character_anchors(classifications, visual_text_colors):
    """Classify text colors against image-local red/black character anchors."""
    report = {
        "status": "not_run",
        "method": "character_anchored_lab_a",
        "anchors": {"red": [], "black": []},
        "low_confidence_circle_ids": [],
        "unresolved_circle_ids": [],
    }
    if not visual_text_colors:
        return report

    min_anchor_count = max(
        1,
        int(os.getenv("CHESS_TEXT_COLOR_MIN_ANCHOR_COUNT", "1")),
    )
    min_anchor_separation = float(
        os.getenv("CHESS_TEXT_COLOR_MIN_ANCHOR_SEPARATION", "3.0")
    )
    min_confidence = float(
        os.getenv("CHESS_TEXT_COLOR_MIN_ANCHOR_CONFIDENCE", "0.25")
    )

    anchor_scores = {"red": [], "black": []}
    for circle_id, classification in classifications.items():
        char = str(classification.get("char", "")).strip()
        anchor_color = COLOR_ANCHOR_CHARACTER_HINTS.get(char)
        estimate = visual_text_colors.get(int(circle_id))
        score = _get_optional_float(
            estimate.get("ink_redness_score") if estimate else None
        )
        if anchor_color is None or score is None:
            continue
        anchor_scores[anchor_color].append(score)
        report["anchors"][anchor_color].append(
            {
                "circle_id": int(circle_id),
                "char": char,
                "score": round(score, 3),
            }
        )

    def median(values):
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return float(ordered[middle])
        return float((ordered[middle - 1] + ordered[middle]) / 2.0)

    anchors_ready = all(
        len(anchor_scores[color]) >= min_anchor_count
        for color in ("red", "black")
    )
    if not anchors_ready:
        report["status"] = "anchors_not_ready"
        for circle_id, estimate in visual_text_colors.items():
            classification = classifications.setdefault(
                int(circle_id),
                {"char": "", "name": "unknown", "color": "unknown"},
            )
            if classification.get("character_color_authoritative", False):
                continue
            estimate["color"] = "unknown"
            estimate["anchored_color_status"] = "unresolved"
            classification["color_classification_status"] = "unresolved"
            classification["color_confidence"] = 0.0
            classification["color_evidence"] = "character_anchors_not_ready"
            report["unresolved_circle_ids"].append(int(circle_id))
        return report

    centers = {
        color: median(anchor_scores[color])
        for color in ("red", "black")
    }
    separation = abs(centers["red"] - centers["black"])
    boundary = (centers["red"] + centers["black"]) / 2.0
    report["centers"] = {
        color: round(value, 3) for color, value in centers.items()
    }
    report["separation"] = round(separation, 3)
    report["boundary"] = round(boundary, 3)
    report["minimum_confidence"] = round(min_confidence, 3)

    if separation < min_anchor_separation:
        report["status"] = "anchor_separation_too_small"
        for circle_id, estimate in visual_text_colors.items():
            classification = classifications.setdefault(
                int(circle_id),
                {"char": "", "name": "unknown", "color": "unknown"},
            )
            if classification.get("character_color_authoritative", False):
                continue
            estimate["color"] = "unknown"
            estimate["anchored_color_status"] = "low_confidence"
            classification["color_classification_status"] = "low_confidence"
            classification["color_confidence"] = 0.0
            classification["color_evidence"] = "weak_character_anchor_separation"
            report["low_confidence_circle_ids"].append(int(circle_id))
        return report

    red_is_higher = centers["red"] > centers["black"]
    for circle_id, estimate in visual_text_colors.items():
        classification = classifications.setdefault(
            int(circle_id),
            {"char": "", "name": "unknown", "color": "unknown"},
        )
        if classification.get("character_color_authoritative", False):
            estimate["color"] = classification.get("color", "unknown")
            estimate["anchored_color_status"] = "character_authoritative"
            estimate["anchored_color_confidence"] = 1.0
            continue

        score = _get_optional_float(estimate.get("ink_redness_score"))
        if score is None:
            estimate["color"] = "unknown"
            estimate["anchored_color_status"] = "unresolved"
            classification["color_classification_status"] = "unresolved"
            classification["color_confidence"] = 0.0
            classification["color_evidence"] = "missing_ink_color_features"
            report["unresolved_circle_ids"].append(int(circle_id))
            continue

        color = "red" if (score > boundary) == red_is_higher else "black"
        confidence = min(
            1.0,
            abs(score - boundary) / max(separation / 2.0, 1e-6),
        )
        estimate["anchored_color"] = color
        estimate["anchored_color_confidence"] = round(confidence, 3)
        classification["color_confidence"] = round(confidence, 3)
        classification["color_evidence"] = "character_anchored_lab_a"
        if confidence < min_confidence:
            estimate["color"] = "unknown"
            estimate["anchored_color_status"] = "low_confidence"
            classification["color_classification_status"] = "low_confidence"
            report["low_confidence_circle_ids"].append(int(circle_id))
            continue

        estimate["color"] = color
        estimate["anchored_color_status"] = "confident"
        classification["color_classification_status"] = "confident"

    report["status"] = (
        "low_confidence" if report["low_confidence_circle_ids"]
        or report["unresolved_circle_ids"] else "applied"
    )
    return report


def apply_visual_text_colors_to_classifications(classifications, visual_text_colors):
    if not visual_text_colors:
        return []

    adjustments = []
    for circle_id, estimate in visual_text_colors.items():
        color = estimate.get("color", "unknown")
        classification = classifications.setdefault(
            int(circle_id),
            {
                "char": "",
                "name": "unknown",
                "color": "unknown",
            },
        )
        if not classification.get("character_color_authoritative", False):
            for source_key, target_key in (
                ("anchored_color_status", "color_classification_status"),
                ("anchored_color_confidence", "color_confidence"),
            ):
                if source_key in estimate:
                    classification[target_key] = estimate[source_key]
            if "anchored_color_status" in estimate:
                classification["color_evidence"] = "character_anchored_lab_a"
        if classification.get("character_color_authoritative", False):
            continue
        classification["visual_color_authoritative"] = color != "unknown"
        if color == "unknown":
            continue
        old_color = classification.get("color", "unknown")
        if old_color != color:
            classification["color"] = color
            adjustments.append(
                {
                    "circle_id": int(circle_id),
                    "from_color": old_color,
                    "to_color": color,
                    "red_ratio": estimate.get("red_ratio"),
                    "black_ratio": estimate.get("black_ratio"),
                    "color_cluster": estimate.get("color_cluster"),
                    "color_cluster_margin": estimate.get("color_cluster_margin"),
                }
            )

    return adjustments


def apply_candidate_classifications_to_processed(processed, classifications):
    if not classifications:
        processed["candidate_classifier_status"] = "none"
        processed["candidate_classifier_adjustments"] = []
        return processed

    adjustments = []
    for piece in processed.get("pieces", []):
        circle_id = piece.get("circle_id")
        if circle_id is None:
            continue
        classification = classifications.get(int(circle_id))
        if not classification:
            continue
        for metadata_key in (
            "color_classification_status",
            "color_confidence",
            "color_evidence",
        ):
            if metadata_key in classification:
                piece[metadata_key] = classification[metadata_key]
        color_status = classification.get("color_classification_status")
        color_is_unresolved = color_status in ("low_confidence", "unresolved")
        if color_is_unresolved and piece.get("side") != "unknown":
            adjustments.append(
                {
                    "circle_id": int(circle_id),
                    "from_side": piece.get("side"),
                    "to_side": "unknown",
                    "reason": f"{color_status}_text_color",
                }
            )
            piece["side"] = "unknown"
        new_name = classification.get("name", "unknown")
        if new_name != "unknown" and new_name != piece.get("name"):
            adjustments.append(
                {
                    "circle_id": int(circle_id),
                    "from_name": piece.get("name"),
                    "to_name": new_name,
                    "char": classification.get("char", ""),
                }
            )
            piece["name"] = new_name
        new_color = classification.get("color", "unknown")
        color_is_authoritative = (
            classification.get("visual_color_authoritative", False)
            or classification.get("character_color_authoritative", False)
        )
        if not color_is_unresolved and (
            color_is_authoritative or new_color != "unknown"
        ) and new_color != piece.get("color"):
            adjustments.append(
                {
                    "circle_id": int(circle_id),
                    "from_color": piece.get("color"),
                    "to_color": new_color,
                    "char": classification.get("char", ""),
                }
            )
            piece["color"] = new_color

    processed["candidate_classifier_status"] = "applied" if adjustments else "no_adjustments"
    processed["candidate_classifier_adjustments"] = adjustments
    return processed


def apply_authoritative_character_classifications_to_processed(processed, classifications):
    """Apply exact character-derived names and colors from a secondary classifier."""
    if not classifications:
        processed["dedicated_character_classifier_status"] = "none"
        processed["dedicated_character_classifier_adjustments"] = []
        return processed

    adjustments = []
    by_circle_id = {
        int(piece["circle_id"]): piece
        for piece in processed.get("pieces", [])
        if piece.get("circle_id") is not None
    }
    for circle_id, classification in classifications.items():
        piece = by_circle_id.get(int(circle_id))
        if piece is None:
            continue
        if classification.get("character_name_authoritative", False):
            new_name = classification.get("name", "unknown")
            if new_name != "unknown" and new_name != piece.get("name"):
                adjustments.append(
                    {
                        "circle_id": int(circle_id),
                        "char": classification.get("char", ""),
                        "from_name": piece.get("name"),
                        "to_name": new_name,
                        "reason": "dedicated_character_name_hint",
                    }
                )
                piece["name"] = new_name
        if classification.get("character_color_authoritative", False):
            new_color = classification.get("color", "unknown")
            if new_color != "unknown" and new_color != piece.get("color"):
                adjustments.append(
                    {
                        "circle_id": int(circle_id),
                        "char": classification.get("char", ""),
                        "from_color": piece.get("color"),
                        "to_color": new_color,
                        "reason": "dedicated_character_color_hint",
                    }
                )
                piece["color"] = new_color
            for metadata_key in (
                "color_classification_status",
                "color_confidence",
                "color_evidence",
            ):
                if metadata_key in classification:
                    piece[metadata_key] = classification[metadata_key]

    processed["dedicated_character_classifier_status"] = (
        "applied" if adjustments else "no_adjustments"
    )
    processed["dedicated_character_classifier_adjustments"] = adjustments
    return processed


def apply_color_side_mapping_to_processed(processed):
    pieces = processed.get("pieces", [])
    shuai_pieces = [
        piece for piece in pieces
        if piece.get("name") == PLAYER1_SIDE and
        piece.get("color", "unknown") != "unknown"
    ]
    jiang_pieces = [
        piece for piece in pieces
        if piece.get("name") == PLAYER2_SIDE and
        piece.get("color", "unknown") != "unknown"
    ]

    if len(shuai_pieces) != 1 or len(jiang_pieces) != 1:
        processed["color_side_mapping_status"] = "king_color_not_ready"
        processed["color_side_mapping_adjustments"] = []
        return processed

    shuai_color = shuai_pieces[0]["color"]
    jiang_color = jiang_pieces[0]["color"]
    if shuai_color == jiang_color:
        processed["color_side_mapping_status"] = "king_color_conflict"
        processed["color_side_mapping_adjustments"] = []
        return processed

    color_to_side = {
        shuai_color: PLAYER1_SIDE,
        jiang_color: PLAYER2_SIDE,
    }
    adjustments = []
    for piece in pieces:
        if piece.get("color_classification_status") in (
            "low_confidence",
            "unresolved",
        ):
            continue
        color = piece.get("color", "unknown")
        expected_side = color_to_side.get(color)
        if expected_side is None:
            continue
        old_side = piece.get("side", "unknown")
        if old_side != expected_side:
            piece["side"] = expected_side
            adjustments.append(
                {
                    "circle_id": piece.get("circle_id"),
                    "name": piece.get("name", "unknown"),
                    "color": color,
                    "from_side": old_side,
                    "to_side": expected_side,
                }
            )

    processed["color_side_mapping_status"] = "applied" if adjustments else "no_adjustments"
    processed["color_side_mapping"] = {
        shuai_color: PLAYER1_SIDE,
        jiang_color: PLAYER2_SIDE,
    }
    processed["color_side_mapping_adjustments"] = adjustments
    return processed


def build_vision_prompt(validation_feedback=None, image_width=None, image_height=None, circle_candidates=None):
    image_size_text = ""
    if image_width and image_height:
        image_size_text = (
            f"The input image size is {int(image_width)}x{int(image_height)} pixels. "
            f"Each cx must be between 0 and {int(image_width) - 1}; "
            f"each cy must be between 0 and {int(image_height) - 1}. "
        )
    max_visible_pieces = int(os.getenv("CHESS_AI_MAX_VISIBLE_PIECES", str(DEFAULT_MAX_VISIBLE_PIECES)))
    prompt = (
        "Analyze this Xiangqi (Chinese chess) endgame image. "
        "The image may already be OpenCV perspective-corrected so the board is close to a flat rectangular view. "
        + image_size_text
        + build_circle_candidate_text_clean(circle_candidates)
        + "Return only JSON, with no markdown and no explanations. "
        "Return a JSON object with exactly these top-level keys: confidence, orientation, pieces, recommended_moves. "
        "orientation must contain bottom_side and top_side. "
        "pieces must be an array of objects, each with side, color, name, circle_id, cx, and cy only. "
        "recommended_moves must contain shuai and jiang, each with from and to points. "
        "Do not include move reasons. "
        "Use canonical Xiangqi board coordinates for recommended move from/to only: "
        "x is 0..8 from the shuai side's left to right, and y is 0..9 from the shuai baseline "
        "toward the jiang baseline. This is an endgame image, so do not assume opening or home positions. "
        "The Jiang/Shuai pieces may be away from their original palace-center squares. "
        "Use only the actual visible piece centers in the image. "
        "If a visible piece is one file to the right of x=4 and two ranks below the top baseline, its coordinate is x=5,y=7; "
        "do not output x=4,y=9 unless the piece center is actually on the top palace-center intersection. "
        "For pieces, do not output board x/y or ai_x/ai_y. The worker will compute final board x/y from cx/cy and board geometry. "
        "Do not invent hidden pieces. Do not complete a standard opening setup. Do not mirror or add symmetric pieces. "
        "List only circular chess pieces that are actually visible in the image. "
        "If a standard starting-position piece is not visibly present as a round piece, omit it. "
        f"This is an endgame detector: return at most {max_visible_pieces} visible pieces. "
        "If you think there are more, keep only the clearly visible round pieces and omit uncertain inferred ones. "
        "For every piece include only side, color, name, circle_id, cx, and cy. "
        "cx and cy are the image pixel coordinates of the visible piece center, measured from the image top-left corner. "
        "Estimate cx/cy from the actual circular piece center. "
        "Piece names must use pinyin: shuai, jiang, shi, xiang, ma, che, pao, bing, or zu. "
        "Character color supplement: 相/帅/帥/仕/兵 are red text and belong to the shuai side; "
        "象/将/將/士/卒 are black text and belong to the jiang side. "
        "Identify the text color of the Jiang piece and the Shuai piece before assigning sides. "
        "The jiang side is every piece with the same text color as the Jiang piece. "
        "The shuai side is every piece with the same text color as the Shuai piece. "
        "If the Jiang text is black, all black-text pieces are jiang and all red-text pieces are shuai; "
        "if the Jiang text is red, all red-text pieces are jiang and all black-text pieces are shuai. "
        "Do not infer piece side from whether a piece is near the top or bottom of the board. "
        "Before recommending moves, verify that recommended_moves.shuai.from contains a visible shuai-side piece "
        "and recommended_moves.jiang.from contains a visible jiang-side piece. "
        "The to point must be inside the board and must not contain a friendly piece. "
        "Recommend one legal-looking move for the shuai side and one legal-looking move for the jiang side "
        "based on the listed pieces. If recognition is uncertain, lower confidence."
    )
    if validation_feedback:
        prompt += (
            " Your previous answer failed validation. Fix these exact issues and return a corrected full JSON object: "
            + "; ".join(validation_feedback[:8])
        )
    return prompt


def build_review_prompt(candidate,
                        validation_feedback=None,
                        image_width=None,
                        image_height=None,
                        circle_candidates=None):
    candidate_json = json.dumps(
        {
            "confidence": candidate.get("confidence"),
            "orientation": candidate.get("orientation"),
            "pieces": candidate.get("pieces"),
            "recommended_moves": candidate.get("recommended_moves"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    image_size_text = ""
    if image_width and image_height:
        image_size_text = (
            f"The input image size is {int(image_width)}x{int(image_height)} pixels. "
            f"Every cx must be between 0 and {int(image_width) - 1}; "
            f"every cy must be between 0 and {int(image_height) - 1}. "
        )
    max_visible_pieces = int(os.getenv("CHESS_AI_MAX_VISIBLE_PIECES", str(DEFAULT_MAX_VISIBLE_PIECES)))
    prompt = (
        "You are the second-stage verifier for a Xiangqi image recognition result. "
        + image_size_text
        + build_circle_candidate_text_clean(circle_candidates)
        + "Compare the candidate JSON against the original image. "
        "Return only a corrected full JSON object with exactly these top-level keys: "
        "confidence, orientation, pieces, recommended_moves. Do not include markdown or explanations. "
        "Do not simply copy the candidate. Check every visible piece one by one against the image. "
        "Correct any wrong piece name, side, color, cx, cy, missing piece, invented piece, duplicate coordinate, "
        "or recommended move that starts from an empty point or the wrong side. "
        "Use canonical Xiangqi board coordinates: x is 0..8 from the shuai side's left to right, "
        "and y is 0..9 from the shuai baseline toward the jiang baseline. "
        "This is an endgame; do not assume Jiang or Shuai is on its original square. "
        "Use the actual visible piece centers only. Every piece must include only side, color, name, circle_id, cx and cy. "
        "Do not output board x/y or ai_x/ai_y for pieces. The worker will recompute final x/y from cx/cy and board geometry. "
        "If a candidate piece lacks a matching OpenCV circle candidate, remove it. "
        "Remove any candidate piece that appears to be inferred from a standard setup rather than visibly present. "
        f"The final pieces array must contain at most {max_visible_pieces} visible pieces; "
        "when uncertain, prefer omitting a piece over inventing one. "
        "The Jiang/Shuai color rule is mandatory: the Jiang text color defines the jiang side, "
        "and the Shuai text color defines the shuai side. "
        "If the candidate places Jiang at x=4,y=9 but the Jiang piece center is visibly elsewhere, correct it. "
        "After correcting pieces, recommend one legal-looking move for each side using from points that exist in pieces. "
        "Lower confidence if the board coordinates remain uncertain. "
        "Candidate JSON to verify: "
        + candidate_json
    )
    if validation_feedback:
        prompt += (
            " Your previous review answer failed structural validation. Fix these exact issues and return corrected JSON: "
            + "; ".join(validation_feedback[:8])
        )
    return prompt


def extract_response_text(response):
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and isinstance(content.get("text"), str):
                return content["text"]

    raise ValueError("OpenAI response did not contain output text")


def candidate_classifier_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "circle_id": {"type": "integer", "minimum": 0},
                        "char": {"type": "string"},
                        "name": {
                            "type": "string",
                            "enum": ["shuai", "jiang", "shi", "xiang", "ma", "che", "pao", "bing", "zu", "unknown"],
                        },
                        "color": {"type": "string", "enum": ["red", "black", "unknown"]},
                    },
                    "required": ["circle_id", "char", "name", "color"],
                },
            }
        },
        "required": ["items"],
    }


def call_openai_candidate_classifier(candidate_sheet_bytes,
                                     circle_candidates,
                                     ai_api_key,
                                     prompt_text=None,
                                     schema=None,
                                     schema_name="xiangqi_candidate_classifier"):
    sheet_b64 = base64.b64encode(candidate_sheet_bytes).decode("ascii")
    prompt = prompt_text or build_candidate_classifier_prompt(circle_candidates)
    payload = {
        "model": os.getenv("CHESS_AI_MODEL", DEFAULT_AI_MODEL),
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{sheet_b64}",
                    },
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema or candidate_classifier_schema(),
            }
        },
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response_body = post_ai_json(
        OPENAI_RESPONSES_URL,
        data,
        {
            "Authorization": f"Bearer {ai_api_key}",
            "Content-Type": "application/json",
        },
        service_name="OpenAI",
    )
    result = json.loads(response_body)
    return normalize_candidate_classifications(json.loads(extract_response_text(result)))


def call_qwen_candidate_classifier(candidate_sheet_bytes,
                                   circle_candidates,
                                   ai_api_key,
                                   prompt_text=None):
    sheet_b64 = base64.b64encode(candidate_sheet_bytes).decode("ascii")
    prompt = prompt_text or build_candidate_classifier_prompt(circle_candidates)
    model = os.getenv("CHESS_AI_MODEL", DEFAULT_QWEN_MODEL)
    base_url = os.getenv("CHESS_AI_BASE_URL", QWEN_COMPAT_BASE_URL).rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{sheet_b64}"},
                    },
                ],
            }
        ],
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response_body = post_ai_json(
        f"{base_url}/chat/completions",
        data,
        {
            "Authorization": f"Bearer {ai_api_key}",
            "Content-Type": "application/json",
        },
        service_name="Qwen",
    )
    result = json.loads(response_body)
    content = result["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return normalize_candidate_classifications(json.loads(str(content)))


def call_ai_candidate_classifier(candidate_sheet_bytes,
                                 circle_candidates,
                                 ai_api_key,
                                 prompt_text=None,
                                 schema=None,
                                 schema_name="xiangqi_candidate_classifier"):
    if not parse_bool_env(os.getenv("CHESS_AI_ENABLE_CANDIDATE_CLASSIFIER", "true")):
        return {}
    if not candidate_sheet_bytes or not circle_candidates:
        return {}

    provider = os.getenv("CHESS_AI_PROVIDER", DEFAULT_AI_PROVIDER).strip().lower()
    try:
        if provider in ("qwen", "dashscope", "tongyi", "aliyun"):
            return call_qwen_candidate_classifier(
                candidate_sheet_bytes,
                circle_candidates,
                ai_api_key,
                prompt_text=prompt_text,
            )
        if provider in ("openai", ""):
            return call_openai_candidate_classifier(
                candidate_sheet_bytes,
                circle_candidates,
                ai_api_key,
                prompt_text=prompt_text,
                schema=schema,
                schema_name=schema_name,
            )
    except Exception:
        LOGGER.exception("AI candidate classifier failed")
    return {}


def build_king_classifier_prompt(circle_candidates):
    items = [
        {"circle_id": int(circle["circle_id"])}
        for circle in circle_candidates
    ]
    return (
        "Analyze only the numbered Xiangqi candidate crop sheet. "
        "The green ring marks the target piece in each tile. Read the printed Chinese character, "
        "not the board position, color grouping, or expected setup. "
        "This is a dedicated king detector: identify only exact U+5E05 as shuai and exact U+5C06 as jiang. "
        "Do not call any other character shuai or jiang. Return JSON with one top-level key items. "
        "For every readable candidate, return circle_id, char, name, and color. "
        "name must be shuai, jiang, or unknown. color must be red, black, or unknown. "
        "There must be at most one shuai and at most one jiang. "
        "If the character is not clearly readable, use unknown instead of guessing. Candidate ids: "
        + json.dumps(items, ensure_ascii=True, separators=(",", ":"))
    )


def king_classifier_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "circle_id": {"type": "integer", "minimum": 0},
                        "char": {"type": "string"},
                        "name": {"type": "string", "enum": ["shuai", "jiang", "unknown"]},
                        "color": {"type": "string", "enum": ["red", "black", "unknown"]},
                    },
                    "required": ["circle_id", "char", "name", "color"],
                },
            }
        },
        "required": ["items"],
    }


def post_ai_json(url, data, headers, timeout_seconds=60, service_name="AI"):
    """POST JSON with urllib, falling back to native curl on local TLS failures."""
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{service_name} API error {exc.code}: {error_body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if not parse_bool_env(os.getenv("CHESS_AI_CURL_FALLBACK", "true")):
            raise

        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            raise RuntimeError(
                f"{service_name} HTTPS request failed and curl is unavailable: {exc}"
            ) from exc

        command = [
            curl_path,
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--max-time",
            str(max(1, int(timeout_seconds))),
            "-X",
            "POST",
            url,
            "--data-binary",
            "@-",
        ]
        for name, value in headers.items():
            command.extend(["-H", f"{name}: {value}"])
        try:
            result = subprocess.run(
                command,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds + 5,
                check=False,
            )
        except OSError as curl_exc:
            raise RuntimeError(f"{service_name} curl fallback failed: {curl_exc}") from curl_exc

        if result.returncode != 0:
            error_body = result.stderr.decode("utf-8", errors="replace").strip()
            response_body = result.stdout.decode("utf-8", errors="replace").strip()
            detail = "\n".join(
                value for value in (response_body, error_body) if value
            ) or f"curl exit code {result.returncode}"
            raise RuntimeError(f"{service_name} curl API error: {detail[:2000]}")
        return result.stdout.decode("utf-8")


def call_ai_king_classifier(candidate_sheet_bytes, circle_candidates, ai_api_key):
    if not parse_bool_env(os.getenv("CHESS_AI_ENABLE_KING_CLASSIFIER", "true")):
        return {}
    if not candidate_sheet_bytes or not circle_candidates:
        return {}

    return call_ai_candidate_classifier(
        candidate_sheet_bytes,
        circle_candidates,
        ai_api_key,
        prompt_text=build_king_classifier_prompt(circle_candidates),
        schema=king_classifier_schema(),
        schema_name="xiangqi_king_classifier",
    )


def apply_king_classifications_to_processed(processed, classifications, circles):
    selected = {}
    for circle_id, classification in classifications.items():
        name = classification.get("name")
        if name in (PLAYER1_SIDE, PLAYER2_SIDE) and name not in selected:
            selected[name] = (int(circle_id), classification)

    if not selected:
        processed["king_classifier_status"] = "no_exact_king"
        processed["king_classifier_adjustments"] = []
        return processed

    adjustments = []
    pieces = processed.setdefault("pieces", [])

    by_id = {int(circle["circle_id"]): circle for circle in circles}
    piece_by_circle_id = {
        int(piece["circle_id"]): piece
        for piece in pieces
        if piece.get("circle_id") is not None
    }

    # A king outside its palace is a classifier contradiction. Prefer a
    # plausible same-side candidate inside the palace before validation sees
    # the contradiction; this is especially useful when a character is partly
    # occluded by a board line or neighboring piece.
    for name, (circle_id, classification) in list(selected.items()):
        selected_circle = by_id.get(circle_id)
        selected_point = (
            {
                "x": int(selected_circle.get("board_x", 0)),
                "y": int(selected_circle.get("board_y", 0)),
            }
            if selected_circle is not None
            else None
        )
        if selected_point is not None and _is_inside_palace(name, selected_point):
            continue

        color = classification.get("color", "unknown")
        fallback_candidates = []
        for circle in circles:
            candidate_id = int(circle["circle_id"])
            point = {
                "x": int(circle.get("board_x", 0)),
                "y": int(circle.get("board_y", 0)),
            }
            if not _is_inside_palace(name, point):
                continue
            existing_piece = piece_by_circle_id.get(candidate_id)
            if existing_piece is None:
                continue
            if existing_piece.get("side") not in (name, "unknown"):
                continue
            existing_color = existing_piece.get("color", "unknown")
            if color != "unknown" and existing_color not in ("unknown", color):
                continue
            center_distance = abs(point["x"] - 4) + abs(point["y"] - (2 if name == PLAYER1_SIDE else 7))
            name_bonus = 0 if existing_piece.get("name") in ("unknown", "shi", "xiang") else 1
            fallback_candidates.append(
                (
                    center_distance - name_bonus * 0.25,
                    candidate_id,
                    existing_piece,
                )
            )

        if not fallback_candidates:
            del selected[name]
            adjustments.append(
                {
                    "from_circle_id": circle_id,
                    "name": name,
                    "action": "rejected",
                    "reason": "classifier_selected_candidate_outside_palace_without_fallback",
                    "from_point": selected_point,
                }
            )
            continue
        _, fallback_id, fallback_piece = min(fallback_candidates, key=lambda item: item[0])
        selected[name] = (fallback_id, classification)
        adjustments.append(
            {
                "from_circle_id": circle_id,
                "to_circle_id": fallback_id,
                "name": name,
                "action": "palace_fallback",
                "reason": "classifier_selected_candidate_outside_palace",
                "from_point": selected_point,
                "to_point": {
                    "x": int(fallback_piece.get("x", 0)),
                    "y": int(fallback_piece.get("y", 0)),
                },
            }
        )

    selected_ids = {item[0] for item in selected.values()}
    selected_names = set(selected)
    prior_non_king_names = {}
    for adjustment in processed.get("candidate_classifier_adjustments", []):
        circle_id = _parse_classifier_circle_id(adjustment.get("circle_id"))
        from_name = validate_piece_name(adjustment.get("from_name", "unknown"))
        to_name = validate_piece_name(adjustment.get("to_name", "unknown"))
        if (
            circle_id is not None
            and to_name in (PLAYER1_SIDE, PLAYER2_SIDE)
            and from_name not in (PLAYER1_SIDE, PLAYER2_SIDE, "unknown")
        ):
            prior_non_king_names[circle_id] = from_name
    retained_pieces = []
    for piece in pieces:
        if piece.get("name") not in (PLAYER1_SIDE, PLAYER2_SIDE):
            retained_pieces.append(piece)
            continue
        # The dedicated classifier may return only one king when the other
        # crop is partially occluded. Preserve an existing king whose name was
        # not covered by that response instead of deleting a usable result.
        if piece.get("name") not in selected_names:
            retained_pieces.append(piece)
            continue
        if piece.get("circle_id") not in selected_ids:
            circle_id = _parse_classifier_circle_id(piece.get("circle_id"))
            dedicated_name = validate_piece_name(
                classifications.get(circle_id, {}).get("name", "unknown")
                if circle_id is not None else "unknown"
            )
            restored_name = (
                dedicated_name
                if dedicated_name not in (PLAYER1_SIDE, PLAYER2_SIDE, "unknown")
                else prior_non_king_names.get(circle_id)
            )
            if restored_name is not None:
                old_name = piece.get("name")
                piece["name"] = restored_name
                retained_pieces.append(piece)
                adjustments.append(
                    {
                        "circle_id": piece.get("circle_id"),
                        "from_name": old_name,
                        "to_name": restored_name,
                        "action": "restored_non_king",
                        "reason": "not_confirmed_by_king_classifier",
                    }
                )
                continue
            adjustments.append(
                {
                    "circle_id": piece.get("circle_id"),
                    "from_name": piece.get("name"),
                    "action": "removed",
                    "reason": "not_confirmed_by_king_classifier",
                }
            )
            continue
        retained_pieces.append(piece)
    processed["pieces"] = retained_pieces
    pieces = retained_pieces

    for name, (circle_id, classification) in selected.items():
        circle = by_id.get(circle_id)
        if circle is None:
            continue
        matching_piece = next(
            (piece for piece in pieces if piece.get("circle_id") == circle_id),
            None,
        )
        if matching_piece is None:
            point = {
                "x": int(circle.get("board_x", 0)),
                "y": int(circle.get("board_y", 0)),
            }
            matching_piece = {
                "side": name,
                "color": classification.get("color", "unknown"),
                "name": name,
                "x": point["x"],
                "y": point["y"],
                "cx": float(circle["cx"]),
                "cy": float(circle["cy"]),
                "circle_id": circle_id,
                "circle_radius": float(circle.get("radius", 0.0)),
                "coordinate_source": "king_classifier_candidate",
            }
            pieces.append(matching_piece)
        old_name = matching_piece.get("name")
        old_side = matching_piece.get("side")
        matching_piece["name"] = name
        matching_piece["side"] = name
        if classification.get("color") != "unknown":
            matching_piece["color"] = classification["color"]
        if old_name != name or old_side != name:
            adjustments.append(
                {
                    "circle_id": circle_id,
                    "from_name": old_name,
                    "to_name": name,
                    "from_side": old_side,
                    "to_side": name,
                    "char": classification.get("char", ""),
                }
            )

    processed["king_classifier_status"] = "applied"
    processed["king_classifier_adjustments"] = adjustments
    return processed


def repair_king_palace_candidates(processed):
    """Ensure a king classification cannot leave its own palace."""
    pieces = processed.get("pieces", [])
    adjustments = []

    for king_name in (PLAYER1_SIDE, PLAYER2_SIDE):
        kings = [piece for piece in pieces if piece.get("name") == king_name]
        inside_kings = [
            piece
            for piece in kings
            if _is_inside_palace(king_name, {"x": piece.get("x", -1), "y": piece.get("y", -1)})
        ]
        if len(inside_kings) == 1:
            for piece in list(kings):
                if piece is inside_kings[0]:
                    continue
                pieces.remove(piece)
                adjustments.append(
                    {
                        "circle_id": piece.get("circle_id"),
                        "name": king_name,
                        "action": "removed_outside_palace_king",
                        "point": {"x": piece.get("x"), "y": piece.get("y")},
                    }
                )
            continue

        if len(inside_kings) > 1:
            keep = min(
                inside_kings,
                key=lambda piece: abs(int(piece.get("x", 0)) - 4)
                + abs(int(piece.get("y", 0)) - (1 if king_name == PLAYER1_SIDE else 8)),
            )
            for piece in list(inside_kings):
                if piece is keep:
                    continue
                pieces.remove(piece)
                adjustments.append(
                    {
                        "circle_id": piece.get("circle_id"),
                        "name": king_name,
                        "action": "removed_duplicate_palace_king",
                        "point": {"x": piece.get("x"), "y": piece.get("y")},
                    }
                )
            continue

        if not kings:
            continue

        outside_king = kings[0]
        king_color = outside_king.get("color", "unknown")
        candidates = []
        for piece in pieces:
            if piece is outside_king or piece.get("side") != king_name:
                continue
            if not _is_inside_palace(
                king_name,
                {"x": piece.get("x", -1), "y": piece.get("y", -1)},
            ):
                continue
            piece_color = piece.get("color", "unknown")
            if king_color != "unknown" and piece_color not in ("unknown", king_color):
                continue
            old_cx = float(outside_king.get("cx", 0.0))
            old_cy = float(outside_king.get("cy", 0.0))
            distance = (float(piece.get("cx", 0.0)) - old_cx) ** 2 + (
                float(piece.get("cy", 0.0)) - old_cy
            ) ** 2
            center_bonus = 0 if piece.get("x") == 4 else 1
            candidates.append((distance, center_bonus, piece))

        # Do not promote a normal piece to king here. Only the dedicated king
        # classifier is allowed to make that semantic assignment. This stage
        # only removes an impossible outside-palace king and lets validation
        # stop the engine when no confirmed replacement exists.
        old_point = {"x": outside_king.get("x"), "y": outside_king.get("y")}
        pieces.remove(outside_king)
        adjustments.append(
            {
                "from_circle_id": outside_king.get("circle_id"),
                "name": king_name,
                "action": "removed_unresolved_outside_palace_king",
                "from_point": old_point,
                "reason": "no_confirmed_king_candidate_inside_palace",
                "same_side_palace_candidates": len(candidates),
            }
        )

    processed["king_palace_repair_status"] = "applied" if adjustments else "no_adjustments"
    processed["king_palace_repair_adjustments"] = adjustments
    return processed


def call_openai_vision(image_bytes,
                       image_width,
                       image_height,
                       ai_api_key,
                       validation_feedback=None,
                       prompt=None,
                       candidate_sheet_bytes=None):
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    candidate_sheet_b64 = (
        base64.b64encode(candidate_sheet_bytes).decode("ascii")
        if candidate_sheet_bytes
        else None
    )
    model = os.getenv("CHESS_AI_MODEL", DEFAULT_AI_MODEL)
    prompt = prompt or build_vision_prompt(validation_feedback, image_width, image_height)
    content = [
        {"type": "input_text", "text": prompt},
        {
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{image_b64}",
        },
    ]
    if candidate_sheet_b64:
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{candidate_sheet_b64}",
            }
        )
    payload = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "xiangqi_recommended_moves",
                "strict": True,
                "schema": ai_response_schema(),
            }
        },
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response_body = post_ai_json(
        OPENAI_RESPONSES_URL,
        data,
        {
            "Authorization": f"Bearer {ai_api_key}",
            "Content-Type": "application/json",
        },
        service_name="OpenAI",
    )

    LOGGER.info(
        "Calling OpenAI vision model=%s image_size=%dx%d bytes=%d",
        model,
        image_width,
        image_height,
        len(image_bytes),
    )
    result = json.loads(response_body)
    output_text = extract_response_text(result)
    ai_result = json.loads(output_text)
    return build_processed_result_from_ai(ai_result)


def call_qwen_vision(image_bytes,
                     image_width,
                     image_height,
                     ai_api_key,
                     validation_feedback=None,
                     prompt=None,
                     candidate_sheet_bytes=None):
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    candidate_sheet_b64 = (
        base64.b64encode(candidate_sheet_bytes).decode("ascii")
        if candidate_sheet_bytes
        else None
    )
    model = os.getenv("CHESS_AI_MODEL", DEFAULT_QWEN_MODEL)
    base_url = os.getenv("CHESS_AI_BASE_URL", QWEN_COMPAT_BASE_URL).rstrip("/")
    prompt = prompt or build_vision_prompt(validation_feedback, image_width, image_height)
    content = [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
        },
    ]
    if candidate_sheet_b64:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{candidate_sheet_b64}"},
            }
        )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response_body = post_ai_json(
        f"{base_url}/chat/completions",
        data,
        {
            "Authorization": f"Bearer {ai_api_key}",
            "Content-Type": "application/json",
        },
        service_name="Qwen",
    )

    LOGGER.info(
        "Calling Qwen vision model=%s image_size=%dx%d bytes=%d",
        model,
        image_width,
        image_height,
        len(image_bytes),
    )
    result = json.loads(response_body)
    content = result["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    ai_result = json.loads(str(content))
    return build_processed_result_from_ai(ai_result)


def call_ai_vision(image_bytes,
                   image_width,
                   image_height,
                   ai_api_key,
                   validation_feedback=None,
                   prompt=None):
    board_crop, board_crop_metadata = get_effective_board_crop(image_bytes, image_width, image_height)
    circle_detection_body, grid_line_metadata = suppress_board_grid_lines(
        image_bytes,
        board_crop=board_crop,
    )
    circle_candidates = annotate_circle_candidates_with_board_points(
        detect_piece_circles(
            circle_detection_body,
            suppress_grid_lines=False,
            board_crop=board_crop,
        ),
        image_width,
        image_height,
        board_crop=board_crop,
    )
    candidate_sheet_bytes = create_candidate_crop_sheet(image_bytes, circle_candidates)
    if prompt is None:
        prompt = build_vision_prompt(
            validation_feedback,
            image_width,
            image_height,
            circle_candidates=circle_candidates,
        )
    provider = os.getenv("CHESS_AI_PROVIDER", DEFAULT_AI_PROVIDER).strip().lower()
    if provider in ("qwen", "dashscope", "tongyi", "aliyun"):
        processed = call_qwen_vision(
            image_bytes,
            image_width,
            image_height,
            ai_api_key,
            validation_feedback,
            prompt=prompt,
            candidate_sheet_bytes=candidate_sheet_bytes,
        )
    elif provider in ("openai", ""):
        processed = call_openai_vision(
            image_bytes,
            image_width,
            image_height,
            ai_api_key,
            validation_feedback,
            prompt=prompt,
            candidate_sheet_bytes=candidate_sheet_bytes,
        )
    else:
        raise ValueError(f"Unsupported CHESS_AI_PROVIDER: {provider}")
    processed = apply_circle_candidates_to_processed(processed, circle_candidates)
    classifications = call_ai_candidate_classifier(candidate_sheet_bytes, circle_candidates, ai_api_key)
    character_name_adjustments = apply_character_names_to_classifications(
        classifications
    )
    character_text_color_adjustments = apply_character_text_colors_to_classifications(
        classifications
    )
    king_classifications = call_ai_king_classifier(
        candidate_sheet_bytes,
        circle_candidates,
        ai_api_key,
    )
    character_name_adjustments.extend(
        apply_character_names_to_classifications(king_classifications)
    )
    character_text_color_adjustments.extend(
        apply_character_text_colors_to_classifications(king_classifications)
    )
    character_classifier_merge_adjustments = merge_dedicated_character_classifications(
        classifications,
        king_classifications,
    )
    visual_text_colors = estimate_candidate_text_colors(image_bytes, circle_candidates)
    anchored_text_color_report = calibrate_visual_text_colors_with_character_anchors(
        classifications,
        visual_text_colors,
    )
    visual_text_color_adjustments = apply_visual_text_colors_to_classifications(
        classifications,
        visual_text_colors,
    )
    processed = apply_candidate_classifications_to_processed(processed, classifications)
    processed["visual_text_color_status"] = (
        "applied" if visual_text_color_adjustments else
        "no_adjustments" if visual_text_colors else
        "not_run"
    )
    processed["visual_text_color_method"] = (
        "global_kmeans_lab_hsv" if visual_text_colors else "not_run"
    )
    processed["visual_text_color_estimates"] = visual_text_colors
    processed["visual_text_color_adjustments"] = visual_text_color_adjustments
    processed["anchored_text_color_status"] = anchored_text_color_report.get(
        "status", "not_run"
    )
    processed["anchored_text_color_report"] = anchored_text_color_report
    processed["character_classifier_merge_status"] = (
        "applied" if character_classifier_merge_adjustments else "no_adjustments"
    )
    processed["character_classifier_merge_adjustments"] = (
        character_classifier_merge_adjustments
    )
    has_character_color_hints = any(
        classification.get("character_color_authoritative", False)
        for classification in list(classifications.values())
        + list(king_classifications.values())
    )
    processed = apply_king_classifications_to_processed(
        processed,
        king_classifications,
        circle_candidates,
    )
    processed["character_text_color_status"] = (
        "applied" if has_character_color_hints else
        "no_matching_character" if classifications or king_classifications else
        "not_run"
    )
    processed["character_text_color_adjustments"] = character_text_color_adjustments
    processed["character_piece_name_status"] = (
        "applied" if character_name_adjustments else
        "no_adjustments" if classifications or king_classifications else
        "not_run"
    )
    processed["character_piece_name_adjustments"] = character_name_adjustments
    processed = repair_king_palace_candidates(processed)
    processed = apply_color_side_mapping_to_processed(processed)
    processed = apply_grid_snap_to_processed(processed, image_width, image_height, board_crop=board_crop)
    processed["board_crop_detection"] = board_crop_metadata
    processed = apply_visual_evidence_to_processed(
        processed,
        image_bytes,
        circles=circle_candidates,
        board_crop=board_crop,
    )
    processed = repair_recommended_moves_to_basic_legal(processed)
    processed["grid_line_preprocessing"] = grid_line_metadata
    return sync_move_compatibility_fields(processed)


def review_ai_result(image_bytes,
                     image_width,
                     image_height,
                     ai_api_key,
                     candidate,
                     initial_feedback=None):
    validation_feedback = initial_feedback
    last_reviewed = None
    board_crop, _ = get_effective_board_crop(image_bytes, image_width, image_height)
    circle_detection_body, _ = suppress_board_grid_lines(image_bytes, board_crop=board_crop)
    circle_candidates = annotate_circle_candidates_with_board_points(
        detect_piece_circles(
            circle_detection_body,
            suppress_grid_lines=False,
            board_crop=board_crop,
        ),
        image_width,
        image_height,
        board_crop=board_crop,
    )
    for attempt in range(1, AI_REVIEW_MAX_ATTEMPTS + 1):
        prompt = build_review_prompt(
            candidate,
            validation_feedback,
            image_width,
            image_height,
            circle_candidates=circle_candidates,
        )
        try:
            reviewed = call_ai_vision(
                image_bytes,
                image_width,
                image_height,
                ai_api_key,
                prompt=prompt,
            )
        except Exception:
            LOGGER.exception("AI review failed on attempt %d", attempt)
            continue

        review_errors = validate_processed_result(reviewed)
        if not review_errors:
            LOGGER.info("AI second-stage review passed on attempt %d", attempt)
            return mark_reviewed_ai_result(reviewed)

        last_reviewed = reviewed
        validation_feedback = review_errors
        LOGGER.warning(
            "AI second-stage review returned invalid result on attempt %d/%d: %s",
            attempt,
            AI_REVIEW_MAX_ATTEMPTS,
            "; ".join(review_errors[:8]),
        )

    if last_reviewed is not None:
        return mark_invalid_review_result(last_reviewed, validation_feedback or [])
    return mark_review_failed_result(candidate, ["AI second-stage review failed to return a usable result"])


def process_image(image_bytes,
                  image_width,
                  image_height,
                  ai_api_key=None,
                  preprocessing_metadata=None):
    """Local AI hook.

    Preferred path:
    OpenAI vision -> structured move points.

    Future stronger path:
    image recognition -> board state/FEN -> Xiangqi engine best moves.
    Keep the returned JSON shape stable because ESP32 already parses points.
    """
    if not image_bytes:
        raise ValueError("empty image")

    preprocessing_metadata = preprocessing_metadata or {
        "enabled": False,
        "status": "not_run",
    }

    LOGGER.info(
        "Processing image locally, bytes=%d, size=%dx%d, ai_key_configured=%s preprocessing=%s",
        len(image_bytes),
        image_width,
        image_height,
        bool(ai_api_key),
        preprocessing_metadata.get("status", "unknown"),
    )

    if ai_api_key:
        last_processed = None
        validation_feedback = None
        for attempt in range(1, AI_MAX_ATTEMPTS + 1):
            try:
                processed = call_ai_vision(
                    image_bytes,
                    image_width,
                    image_height,
                    ai_api_key,
                    validation_feedback=validation_feedback,
                )
            except Exception:
                LOGGER.exception("AI vision failed on attempt %d", attempt)
                continue

            validation_errors = validate_processed_result(processed)
            processed["image_preprocessing"] = preprocessing_metadata
            if not validation_errors:
                if attempt > 1:
                    LOGGER.info("AI result validation passed after retry attempt %d", attempt)
                processed = mark_valid_ai_result(processed)
                if parse_bool_env(os.getenv("CHESS_AI_ENABLE_REVIEW", "true")):
                    reviewed = review_ai_result(
                        image_bytes,
                        image_width,
                        image_height,
                        ai_api_key,
                        processed,
                    )
                    reviewed["image_preprocessing"] = preprocessing_metadata
                    if (
                        reviewed.get("validation_status") == "structural_ok"
                        and reviewed.get("review_status") == "review_ok"
                    ):
                        reviewed = apply_engine_recommendations(reviewed)
                    return reviewed
                return apply_engine_recommendations(processed)

            last_processed = processed
            validation_feedback = validation_errors
            LOGGER.warning(
                "AI result validation failed on attempt %d/%d: %s",
                attempt,
                AI_MAX_ATTEMPTS,
                "; ".join(validation_errors[:8]),
            )

        if last_processed is not None:
            if parse_bool_env(os.getenv("CHESS_AI_ENABLE_REVIEW", "true")):
                LOGGER.warning("Trying AI second-stage review after first-stage validation failures")
                reviewed = review_ai_result(
                    image_bytes,
                    image_width,
                    image_height,
                    ai_api_key,
                    last_processed,
                    initial_feedback=validation_feedback,
                )
                reviewed["image_preprocessing"] = preprocessing_metadata
                if (
                    reviewed.get("validation_status") == "structural_ok"
                    and reviewed.get("review_status") == "review_ok"
                ):
                    return apply_engine_recommendations(reviewed)
                return reviewed

            LOGGER.warning("Returning invalid AI result with reduced confidence after retries")
            last_processed = mark_invalid_ai_result(last_processed, validation_feedback or [])
            last_processed["image_preprocessing"] = preprocessing_metadata
            return last_processed

        LOGGER.error("AI vision failed on all attempts; falling back to placeholder moves")

    processed = fixed_placeholder_result()
    processed["image_preprocessing"] = preprocessing_metadata
    return processed


def read_manifest(bucket, device_id):
    manifest_key = f"devices/{device_id}/requests/latest.json"
    body = bucket.get_object(manifest_key).read()
    manifest = json.loads(body)

    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")

    return manifest_key, manifest


def build_result(manifest, processed, image_width, image_height):
    return {
        "protocol_version": int(manifest["protocol_version"]),
        "device_id": manifest["device_id"],
        "session_id": manifest["session_id"],
        "frame_id": manifest["frame_id"],
        "sequence": int(manifest["sequence"]),
        "status": "ok",
        "image_width": image_width,
        "image_height": image_height,
        "confidence": processed["confidence"],
        "orientation": processed["orientation"],
        "pieces": processed["pieces"],
        "recommended_moves": processed["recommended_moves"],
        "coordinate_system": processed["coordinate_system"],
        "player_sides": processed["player_sides"],
        "points": processed["points"],
        "canonical_points": processed["canonical_points"],
        "moves": processed["moves"],
        "validation_status": processed.get("validation_status", "not_run"),
        "validation_errors": processed.get("validation_errors", []),
        "review_status": processed.get("review_status", "not_run"),
        "review_errors": processed.get("review_errors", []),
        "grid_snap_status": processed.get("grid_snap_status", "not_run"),
        "board_geometry_mode": processed.get("board_geometry_mode", "not_run"),
        "board_geometry": processed.get("board_geometry", {}),
        "board_crop_detection": processed.get("board_crop_detection", {"status": "not_run"}),
        "piece_coordinate_source": processed.get("piece_coordinate_source", "not_run"),
        "grid_snap_errors": processed.get("grid_snap_errors", []),
        "grid_snap_adjustments": processed.get("grid_snap_adjustments", []),
        "grid_snap_disambiguations": processed.get("grid_snap_disambiguations", []),
        "grid_snap_move_adjustments": processed.get("grid_snap_move_adjustments", []),
        "move_repair_status": processed.get("move_repair_status", "not_run"),
        "move_repair_adjustments": processed.get("move_repair_adjustments", []),
        "visual_evidence_status": processed.get("visual_evidence_status", "not_run"),
        "visual_evidence_errors": processed.get("visual_evidence_errors", []),
        "detected_piece_circles": processed.get("detected_piece_circles", []),
        "opencv_candidate_status": processed.get("opencv_candidate_status", "not_run"),
        "opencv_candidate_adjustments": processed.get("opencv_candidate_adjustments", []),
        "candidate_classifier_status": processed.get("candidate_classifier_status", "not_run"),
        "candidate_classifier_adjustments": processed.get("candidate_classifier_adjustments", []),
        "visual_text_color_status": processed.get("visual_text_color_status", "not_run"),
        "visual_text_color_method": processed.get("visual_text_color_method", "not_run"),
        "visual_text_color_estimates": processed.get("visual_text_color_estimates", {}),
        "visual_text_color_adjustments": processed.get("visual_text_color_adjustments", []),
        "anchored_text_color_status": processed.get("anchored_text_color_status", "not_run"),
        "anchored_text_color_report": processed.get("anchored_text_color_report", {}),
        "character_text_color_status": processed.get("character_text_color_status", "not_run"),
        "character_text_color_adjustments": processed.get("character_text_color_adjustments", []),
        "character_piece_name_status": processed.get("character_piece_name_status", "not_run"),
        "character_piece_name_adjustments": processed.get("character_piece_name_adjustments", []),
        "dedicated_character_classifier_status": processed.get(
            "dedicated_character_classifier_status", "not_run"
        ),
        "dedicated_character_classifier_adjustments": processed.get(
            "dedicated_character_classifier_adjustments", []
        ),
        "character_classifier_merge_status": processed.get(
            "character_classifier_merge_status", "not_run"
        ),
        "character_classifier_merge_adjustments": processed.get(
            "character_classifier_merge_adjustments", []
        ),
        "king_classifier_status": processed.get("king_classifier_status", "not_run"),
        "king_classifier_adjustments": processed.get("king_classifier_adjustments", []),
        "king_palace_repair_status": processed.get("king_palace_repair_status", "not_run"),
        "king_palace_repair_adjustments": processed.get("king_palace_repair_adjustments", []),
        "color_side_mapping_status": processed.get("color_side_mapping_status", "not_run"),
        "color_side_mapping": processed.get("color_side_mapping", {}),
        "color_side_mapping_adjustments": processed.get("color_side_mapping_adjustments", []),
        "engine_status": processed.get("engine_status", "not_run"),
        "engine_error": processed.get("engine_error", ""),
        "engine_fen": processed.get("engine_fen", ""),
        "engine_fens": processed.get("engine_fens", {}),
        "engine_recommended_moves": processed.get("engine_recommended_moves", {}),
        "image_preprocessing": processed.get("image_preprocessing", {"status": "not_run"}),
        "grid_line_preprocessing": processed.get("grid_line_preprocessing", {"status": "not_run"}),
    }


def save_debug_artifacts(debug_dir,
                         manifest,
                         image_body,
                         result_json,
                         ai_image_body=None):
    if not debug_dir:
        return

    frame_id = str(manifest["frame_id"])
    safe_frame_id = "".join(char if char.isalnum() or char in "-_." else "_" for char in frame_id)
    debug_path = Path(debug_dir)
    debug_path.mkdir(parents=True, exist_ok=True)

    image_suffix = Path(urllib.parse.urlparse(str(manifest["image_key"])).path).suffix or ".jpg"
    image_path = debug_path / f"{safe_frame_id}{image_suffix}"
    result_path = debug_path / f"{safe_frame_id}.result.json"
    overlay_path = debug_path / f"{safe_frame_id}.overlay.jpg"
    crop_sheet_path = debug_path / f"{safe_frame_id}.candidates.jpg"
    grid_suppressed_path = debug_path / f"{safe_frame_id}.grid_suppressed.jpg"
    rectified_path = debug_path / f"{safe_frame_id}.rectified.jpg"

    image_path.write_bytes(image_body)
    if ai_image_body and ai_image_body != image_body:
        rectified_path.write_bytes(ai_image_body)
    result_path.write_text(result_json + "\n", encoding="utf-8")
    try:
        result = json.loads(result_json)
        debug_image_body = ai_image_body or image_body
        create_debug_overlay(debug_image_body, result, overlay_path)
        board_crop = board_crop_from_result_geometry(result.get("board_geometry"))
        grid_detection_body, _ = suppress_board_grid_lines(debug_image_body, board_crop=board_crop)
        grid_suppressed_path.write_bytes(grid_detection_body)
        circles = result.get("detected_piece_circles", []) or detect_piece_circles(
            grid_detection_body,
            suppress_grid_lines=False,
            board_crop=board_crop,
        )
        crop_sheet = create_candidate_crop_sheet(debug_image_body, circles)
        if crop_sheet:
            crop_sheet_path.write_bytes(crop_sheet)
        save_candidate_crop_images(debug_path, safe_frame_id, debug_image_body, circles)
    except Exception:
        LOGGER.exception("Debug overlay generation failed")
    LOGGER.debug(
        "Debug artifacts saved: %s, %s, %s, %s, %s, %s",
        image_path,
        result_path,
        overlay_path,
        crop_sheet_path,
        rectified_path,
        grid_suppressed_path,
    )


def process_one(bucket, args, state, device_id):
    manifest_key, manifest = read_manifest(bucket, device_id)
    frame_id = str(manifest["frame_id"])
    sequence = int(manifest["sequence"])
    manifest_device_id = str(manifest.get("device_id", device_id))
    state_key = f"{manifest_device_id}:{frame_id}:{sequence}"
    device_state = get_device_state(state, device_id)

    if device_state.get("last_processed") == state_key or state.get("last_processed") == state_key:
        device_state["last_processed"] = state_key
        LOGGER.debug(
            "No new frame. device_id=%s manifest=%s frame_id=%s sequence=%d",
            device_id,
            manifest_key,
            frame_id,
            sequence,
        )
        return False

    image_key = urllib.parse.unquote_plus(str(manifest["image_key"]))
    result_key = urllib.parse.unquote_plus(str(manifest["result_key"]))
    image_width = _get_positive_int(manifest, "image_width")
    image_height = _get_positive_int(manifest, "image_height")

    LOGGER.info(
        "New frame detected: device_id=%s frame_id=%s sequence=%d image_key=%s",
        manifest_device_id,
        frame_id,
        sequence,
        image_key,
    )

    image_body = bucket.get_object(image_key).read()
    ai_image_body, ai_image_width, ai_image_height, preprocessing_metadata = rectify_board_image(image_body)
    if ai_image_width is None or ai_image_height is None:
        ai_image_width = image_width
        ai_image_height = image_height
    LOGGER.info(
        "Image preprocessing status=%s ai_size=%dx%d",
        preprocessing_metadata.get("status", "unknown"),
        ai_image_width,
        ai_image_height,
    )
    ai_api_key = os.getenv(args.ai_api_key_env) or os.getenv("OPENAI_API_KEY")
    processed = process_image(
        ai_image_body,
        ai_image_width,
        ai_image_height,
        ai_api_key=ai_api_key,
        preprocessing_metadata=preprocessing_metadata,
    )
    result = build_result(manifest, processed, image_width, image_height)
    result_json = json.dumps(result, separators=(",", ":"))

    save_debug_artifacts(args.debug_dir, manifest, image_body, result_json, ai_image_body=ai_image_body)

    bucket.put_object(
        result_key,
        result_json,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        },
    )

    processed_at = int(time.time())
    device_state["last_processed"] = state_key
    device_state["last_result_key"] = result_key
    device_state["last_processed_at"] = processed_at
    state["last_processed"] = state_key
    state["last_result_key"] = result_key
    state["last_processed_at"] = processed_at
    save_state(args.state_file, state)

    LOGGER.info("Result written: device_id=%s result_key=%s", manifest_device_id, result_key)
    return True


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env_file(args.env_file)
    apply_env_defaults(args)

    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)

    bucket = build_bucket(args)
    state = load_state(args.state_file)
    configured_device_ids = list(args.device_ids)
    discovered_device_ids = []
    last_discovery_at = 0

    LOGGER.info(
        "Worker started. bucket=%s device_ids=%s discover_devices=%s region=%s",
        args.bucket,
        ",".join(configured_device_ids) if configured_device_ids else "(none)",
        args.discover_devices,
        args.region,
    )

    while not SHOULD_STOP:
        now = time.time()
        if args.discover_devices and now - last_discovery_at >= args.discover_interval:
            last_discovery_at = now
            try:
                discovered_device_ids = discover_device_ids(bucket)
                LOGGER.info(
                    "Discovered %d device(s): %s",
                    len(discovered_device_ids),
                    ",".join(discovered_device_ids) if discovered_device_ids else "(none)",
                )
            except Exception:
                LOGGER.exception("Device discovery failed")
                if args.once and not configured_device_ids:
                    return 1

        device_ids = merge_device_ids(configured_device_ids, discovered_device_ids)
        if not device_ids:
            LOGGER.warning("No devices to poll.")
            if args.once:
                return 2

        for device_id in device_ids:
            try:
                processed = process_one(bucket, args, state, device_id)
                if args.once and processed:
                    break
            except oss2.exceptions.NoSuchKey as exc:
                LOGGER.warning("OSS object not found for device_id=%s: %s", device_id, exc)
                if args.once:
                    return 2
            except Exception:
                LOGGER.exception("Worker iteration failed for device_id=%s", device_id)
                if args.once:
                    return 1

        if args.once and processed:
            break

        if args.once:
            break

        time.sleep(args.interval)

    LOGGER.info("Worker stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
