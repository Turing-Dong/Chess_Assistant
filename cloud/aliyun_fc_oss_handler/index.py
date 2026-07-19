import json
import urllib.parse

import oss2


BUCKET_REGION = "cn-hangzhou"
BOARD_FILES = 9
BOARD_RANKS = 10
PLAYER1_SIDE = "shuai"
PLAYER2_SIDE = "jiang"


def build_bucket(bucket_name, context):
    creds = context.credentials

    auth = oss2.StsAuth(
        creds.access_key_id,
        creds.access_key_secret,
        creds.security_token,
    )

    endpoint = f"https://oss-{BUCKET_REGION}-internal.aliyuncs.com"
    return oss2.Bucket(auth, endpoint, bucket_name)


def _get_positive_int(manifest, name):
    value = int(manifest[name])
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def canonical_to_player1(point):
    return {
        "x": point["x"],
        "y": point["y"],
    }


def canonical_to_player2(point):
    return {
        "x": BOARD_FILES - 1 - point["x"],
        "y": BOARD_RANKS - 1 - point["y"],
    }


def validate_board_point(point):
    x = int(point["x"])
    y = int(point["y"])
    if x < 0 or x >= BOARD_FILES or y < 0 or y >= BOARD_RANKS:
        raise ValueError(f"board point out of range: ({x}, {y})")
    return {
        "x": x,
        "y": y,
    }


def process_image(image_bytes, image_width, image_height):
    # TODO: Replace these fixed canonical moves with:
    # image recognition -> FEN conversion -> Xiangqi engine best moves.
    #
    # Player mapping:
    # player1 is shuai side, player2 is jiang side.
    #
    # Canonical coordinates:
    # x: 0..8, from player1 left to player1 right
    # y: 0..9, from player1 baseline to player2 baseline
    player1_move = {
        "from": {"x": 4, "y": 0},
        "to": {"x": 4, "y": 1},
    }
    player2_move = {
        "from": {"x": 2, "y": 9},
        "to": {"x": 2, "y": 7},
    }

    player1_from = validate_board_point(player1_move["from"])
    player1_to = validate_board_point(player1_move["to"])
    player2_from = validate_board_point(player2_move["from"])
    player2_to = validate_board_point(player2_move["to"])

    return {
        "confidence": 0.95,
        "coordinate_system": "player_local",
        "player_sides": {
            "player1": PLAYER1_SIDE,
            "player2": PLAYER2_SIDE,
        },
        "points": {
            "player1_start": canonical_to_player1(player1_from),
            "player1_end": canonical_to_player1(player1_to),
            "player2_start": canonical_to_player2(player2_from),
            "player2_end": canonical_to_player2(player2_to),
        },
        "canonical_points": {
            "player1_start": player1_from,
            "player1_end": player1_to,
            "player2_start": player2_from,
            "player2_end": player2_to,
        },
        "moves": {
            "player1": {
                "from": canonical_to_player1(player1_from),
                "to": canonical_to_player1(player1_to),
            },
            "player2": {
                "from": canonical_to_player2(player2_from),
                "to": canonical_to_player2(player2_to),
            },
        },
    }


def handler(event, context):
    print("event:", event)

    payload = json.loads(event)
    events = payload.get("events", [])

    if not events:
        print("empty events")
        return "ignored"

    for item in events:
        bucket_name = item["oss"]["bucket"]["name"]
        object_key = item["oss"]["object"]["key"]
        object_key = urllib.parse.unquote_plus(object_key)

        print("bucket:", bucket_name)
        print("object_key:", object_key)

        if not object_key.endswith("/requests/latest.json"):
            print("not latest manifest, ignored")
            continue

        bucket = build_bucket(bucket_name, context)

        manifest_body = bucket.get_object(object_key).read()
        manifest = json.loads(manifest_body)

        print("manifest:", manifest)

        image_key = manifest["image_key"]
        result_key = manifest["result_key"]
        image_width = _get_positive_int(manifest, "image_width")
        image_height = _get_positive_int(manifest, "image_height")

        image_body = bucket.get_object(image_key).read()

        processed = process_image(
            image_body,
            image_width,
            image_height,
        )

        result = {
            "protocol_version": int(manifest["protocol_version"]),
            "device_id": manifest["device_id"],
            "session_id": manifest["session_id"],
            "frame_id": manifest["frame_id"],
            "sequence": int(manifest["sequence"]),
            "status": "ok",
            "image_width": image_width,
            "image_height": image_height,
            "confidence": processed["confidence"],
            "coordinate_system": processed["coordinate_system"],
            "player_sides": processed["player_sides"],
            "points": processed["points"],
            "canonical_points": processed["canonical_points"],
            "moves": processed["moves"],
        }

        result_json = json.dumps(result, separators=(",", ":"))

        bucket.put_object(
            result_key,
            result_json,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
            },
        )

        print("result written:", result_key)
        print("result:", result_json)

    return "ok"
