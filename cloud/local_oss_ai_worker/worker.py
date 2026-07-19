import argparse
import base64
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import oss2


DEFAULT_REGION = "cn-hangzhou"
DEFAULT_AI_PROVIDER = "openai"
DEFAULT_AI_MODEL = "gpt-5.6"
DEFAULT_QWEN_MODEL = "qwen3-vl-plus"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
QWEN_COMPAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
BOARD_FILES = 9
BOARD_RANKS = 10
PLAYER1_SIDE = "shuai"
PLAYER2_SIDE = "jiang"

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
    x = int(point["x"])
    y = int(point["y"])
    if x < 0 or x >= BOARD_FILES or y < 0 or y >= BOARD_RANKS:
        raise ValueError(f"board point out of range: ({x}, {y})")
    return {"x": x, "y": y}


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
            point = validate_board_point(piece)
        except (KeyError, TypeError, ValueError):
            continue

        normalized.append(
            {
                "side": validate_side(piece.get("side", "unknown")),
                "name": str(piece.get("name", "unknown"))[:16],
                "x": point["x"],
                "y": point["y"],
            }
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
            "name": {"type": "string"},
            "x": {"type": "integer", "minimum": 0, "maximum": BOARD_FILES - 1},
            "y": {"type": "integer", "minimum": 0, "maximum": BOARD_RANKS - 1},
        },
        "required": ["side", "name", "x", "y"],
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


def extract_response_text(response):
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and isinstance(content.get("text"), str):
                return content["text"]

    raise ValueError("OpenAI response did not contain output text")


def call_openai_vision(image_bytes, image_width, image_height, ai_api_key):
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    model = os.getenv("CHESS_AI_MODEL", DEFAULT_AI_MODEL)
    prompt = (
        "Analyze this Xiangqi (Chinese chess) endgame image. Return only JSON that matches the schema. "
        "First identify board orientation: bottom_side and top_side must be shuai, jiang, or unknown. "
        "Then identify the text color of the Jiang piece and the Shuai piece. "
        "The jiang side is every piece with the same text color as the Jiang piece. "
        "The shuai side is every piece with the same text color as the Shuai piece. "
        "If the Jiang text is black, all black-text pieces are jiang and all red-text pieces are shuai; "
        "if the Jiang text is red, all red-text pieces are jiang and all black-text pieces are shuai. "
        "Do not infer piece side from whether a piece is near the top or bottom of the board. "
        "Then list all visible pieces with this color-based side, piece name, and board coordinates. "
        "Finally recommend one legal move for the shuai side and one legal move for the jiang side. "
        "Do not include reasons. "
        "Use canonical Xiangqi board coordinates, not pixel coordinates: x is 0..8 from the shuai side's left to right, "
        "and y is 0..9 from the shuai baseline toward the jiang baseline. "
        "Each coordinate must be the nearest board intersection to a piece center. "
        "If recognition is uncertain, lower confidence, but still return conservative legal-looking moves."
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{image_b64}",
                    },
                ],
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
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {ai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    LOGGER.info(
        "Calling OpenAI vision model=%s image_size=%dx%d bytes=%d",
        model,
        image_width,
        image_height,
        len(image_bytes),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {error_body}") from exc

    result = json.loads(response_body)
    output_text = extract_response_text(result)
    ai_result = json.loads(output_text)
    return build_processed_result_from_ai(ai_result)


def call_qwen_vision(image_bytes, image_width, image_height, ai_api_key):
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    model = os.getenv("CHESS_AI_MODEL", DEFAULT_QWEN_MODEL)
    base_url = os.getenv("CHESS_AI_BASE_URL", QWEN_COMPAT_BASE_URL).rstrip("/")
    prompt = (
        "Analyze this Xiangqi (Chinese chess) endgame image. "
        "Return only JSON, with no markdown and no explanations. Use this exact shape: "
        "{\"confidence\":0.0,"
        "\"orientation\":{\"bottom_side\":\"shuai\",\"top_side\":\"jiang\"},"
        "\"pieces\":[{\"side\":\"shuai\",\"name\":\"shuai\",\"x\":4,\"y\":0}],"
        "\"recommended_moves\":{\"shuai\":{\"from\":{\"x\":4,\"y\":0},\"to\":{\"x\":4,\"y\":1}},"
        "\"jiang\":{\"from\":{\"x\":4,\"y\":9},\"to\":{\"x\":4,\"y\":8}}}}. "
        "Do not include move reasons. "
        "bottom_side and top_side must be shuai, jiang, or unknown. "
        "Identify the text color of the Jiang piece and the Shuai piece. "
        "The jiang side is every piece with the same text color as the Jiang piece. "
        "The shuai side is every piece with the same text color as the Shuai piece. "
        "If the Jiang text is black, all black-text pieces are jiang and all red-text pieces are shuai; "
        "if the Jiang text is red, all red-text pieces are jiang and all black-text pieces are shuai. "
        "Do not infer piece side from whether a piece is near the top or bottom of the board. "
        "Use canonical Xiangqi board coordinates, not pixel coordinates: x is 0..8 from the shuai side's left to right, "
        "and y is 0..9 from the shuai baseline toward the jiang baseline. "
        "Each piece coordinate must be the nearest board intersection to the piece center. "
        "Recommend one legal move for the shuai side and one legal move for the jiang side based on the current position. "
        "If uncertain, lower confidence, but still return conservative legal-looking moves."
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {ai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    LOGGER.info(
        "Calling Qwen vision model=%s image_size=%dx%d bytes=%d",
        model,
        image_width,
        image_height,
        len(image_bytes),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Qwen API error {exc.code}: {error_body}") from exc

    result = json.loads(response_body)
    content = result["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    ai_result = json.loads(str(content))
    return build_processed_result_from_ai(ai_result)


def call_ai_vision(image_bytes, image_width, image_height, ai_api_key):
    provider = os.getenv("CHESS_AI_PROVIDER", DEFAULT_AI_PROVIDER).strip().lower()
    if provider in ("qwen", "dashscope", "tongyi", "aliyun"):
        return call_qwen_vision(image_bytes, image_width, image_height, ai_api_key)
    if provider in ("openai", ""):
        return call_openai_vision(image_bytes, image_width, image_height, ai_api_key)
    raise ValueError(f"Unsupported CHESS_AI_PROVIDER: {provider}")


def process_image(image_bytes, image_width, image_height, ai_api_key=None):
    """Local AI hook.

    Preferred path:
    OpenAI vision -> structured move points.

    Future stronger path:
    image recognition -> board state/FEN -> Xiangqi engine best moves.
    Keep the returned JSON shape stable because ESP32 already parses points.
    """
    if not image_bytes:
        raise ValueError("empty image")

    LOGGER.info(
        "Processing image locally, bytes=%d, size=%dx%d, ai_key_configured=%s",
        len(image_bytes),
        image_width,
        image_height,
        bool(ai_api_key),
    )

    if ai_api_key:
        try:
            return call_ai_vision(image_bytes, image_width, image_height, ai_api_key)
        except Exception:
            LOGGER.exception("AI vision failed; falling back to placeholder moves")

    return fixed_placeholder_result()


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
    }


def save_debug_artifacts(debug_dir, manifest, image_body, result_json):
    if not debug_dir:
        return

    frame_id = str(manifest["frame_id"])
    safe_frame_id = "".join(char if char.isalnum() or char in "-_." else "_" for char in frame_id)
    debug_path = Path(debug_dir)
    debug_path.mkdir(parents=True, exist_ok=True)

    image_suffix = Path(urllib.parse.urlparse(str(manifest["image_key"])).path).suffix or ".jpg"
    image_path = debug_path / f"{safe_frame_id}{image_suffix}"
    result_path = debug_path / f"{safe_frame_id}.result.json"

    image_path.write_bytes(image_body)
    result_path.write_text(result_json + "\n", encoding="utf-8")
    LOGGER.debug("Debug artifacts saved: %s, %s", image_path, result_path)


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
    ai_api_key = os.getenv(args.ai_api_key_env) or os.getenv("OPENAI_API_KEY")
    processed = process_image(
        image_body,
        image_width,
        image_height,
        ai_api_key=ai_api_key,
    )
    result = build_result(manifest, processed, image_width, image_height)
    result_json = json.dumps(result, separators=(",", ":"))

    save_debug_artifacts(args.debug_dir, manifest, image_body, result_json)

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
