"""Lazy Paddle inference adapter for the worker's 14-class piece model."""

from __future__ import annotations

import atexit
import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


MODEL_LABELS = (
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

LABEL_METADATA = {
    "red_shuai": ("帅", "shuai", "red"),
    "black_jiang": ("将", "jiang", "black"),
    "red_shi": ("仕", "shi", "red"),
    "black_shi": ("士", "shi", "black"),
    "red_xiang": ("相", "xiang", "red"),
    "black_xiang": ("象", "xiang", "black"),
    "red_ma": ("马", "ma", "red"),
    "black_ma": ("馬", "ma", "black"),
    "red_che": ("车", "che", "red"),
    "black_che": ("車", "che", "black"),
    "red_pao": ("炮", "pao", "red"),
    "black_pao": ("砲", "pao", "black"),
    "red_bing": ("兵", "bing", "red"),
    "black_zu": ("卒", "zu", "black"),
}

_DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parents[1]
    / "local_piece_classifier"
    / "output"
    / "xiangqi_pplcnet_x1_0_finetune"
    / "inference"
)
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)
_CACHE_LOCK = threading.Lock()
_PREDICT_LOCK = threading.Lock()
_PREDICTOR_CACHE = {}
_SERVER_LOCK = threading.Lock()
_SERVER_PROCESS = None
_SERVER_RESPONSE_PREFIX = "CHESS_CLASSIFIER_RESPONSE:"


def _bool_env(name, default):
    value = os.getenv(name, default)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _model_dir():
    configured = os.getenv("CHESS_LOCAL_CLASSIFIER_MODEL_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else _DEFAULT_MODEL_DIR


def _build_predictor(model_dir, device):
    try:
        import paddle.inference as paddle_infer
    except ImportError as exc:
        raise RuntimeError(
            "paddlepaddle is not installed in the worker environment"
        ) from exc

    model_file = model_dir / "inference.json"
    params_file = model_dir / "inference.pdiparams"
    if not model_file.is_file() or not params_file.is_file():
        raise FileNotFoundError(f"exported model not found under {model_dir}")

    config = paddle_infer.Config(str(model_file), str(params_file))
    if device == "gpu":
        memory_mb = int(os.getenv("CHESS_LOCAL_CLASSIFIER_GPU_MEMORY_MB", "512"))
        device_id = int(os.getenv("CHESS_LOCAL_CLASSIFIER_GPU_ID", "0"))
        config.enable_use_gpu(memory_mb, device_id)
    else:
        config.disable_gpu()
        config.set_cpu_math_library_num_threads(
            max(1, int(os.getenv("CHESS_LOCAL_CLASSIFIER_CPU_THREADS", "2")))
        )
    config.switch_ir_optim(True)
    return paddle_infer.create_predictor(config)


def _get_predictor(model_dir, device):
    cache_key = (str(model_dir), device)
    with _CACHE_LOCK:
        predictor = _PREDICTOR_CACHE.get(cache_key)
        if predictor is None:
            predictor = _build_predictor(model_dir, device)
            _PREDICTOR_CACHE[cache_key] = predictor
        return predictor


def _crop_array(image, circle, crop_scale, image_size, np, Image):
    center_x = float(circle["cx"])
    center_y = float(circle["cy"])
    radius = max(float(circle.get("radius", 20.0)), 12.0)
    half = radius * crop_scale
    box = (
        max(0, int(round(center_x - half))),
        max(0, int(round(center_y - half))),
        min(image.width, int(round(center_x + half))),
        min(image.height, int(round(center_y + half))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return None

    crop = image.crop(box).convert("RGB")
    rotation_degrees = int(circle.get("crop_rotation_degrees", 0) or 0) % 360
    if rotation_degrees == 180:
        transpose = getattr(Image, "Transpose", Image)
        crop = crop.transpose(transpose.ROTATE_180)
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
    crop = crop.resize((image_size, image_size), resample=resampling)
    array = np.asarray(crop, dtype="float32") / 255.0
    mean = np.asarray(_MEAN, dtype="float32").reshape(1, 1, 3)
    std = np.asarray(_STD, dtype="float32").reshape(1, 1, 3)
    return np.transpose((array - mean) / std, (2, 0, 1))


def _classify_candidates_inprocess(image_body, circles):
    """Classify worker circle candidates and return accepted results plus audit data."""
    model_dir = _model_dir()
    device = os.getenv("CHESS_LOCAL_CLASSIFIER_DEVICE", "cpu").strip().lower()
    if device not in ("cpu", "gpu"):
        raise ValueError("CHESS_LOCAL_CLASSIFIER_DEVICE must be cpu or gpu")

    min_confidence = float(os.getenv("CHESS_LOCAL_CLASSIFIER_MIN_CONFIDENCE", "0.70"))
    min_margin = float(os.getenv("CHESS_LOCAL_CLASSIFIER_MIN_MARGIN", "0.10"))
    crop_scale = float(os.getenv("CHESS_LOCAL_CLASSIFIER_CROP_SCALE", "1.35"))
    image_size = int(os.getenv("CHESS_LOCAL_CLASSIFIER_IMAGE_SIZE", "160"))
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("CHESS_LOCAL_CLASSIFIER_MIN_CONFIDENCE must be between 0 and 1")
    if not 0.0 <= min_margin <= 1.0:
        raise ValueError("CHESS_LOCAL_CLASSIFIER_MIN_MARGIN must be between 0 and 1")
    if crop_scale <= 0 or image_size <= 0:
        raise ValueError("local classifier crop scale and image size must be positive")

    report = {
        "status": "not_run",
        "model_dir": str(model_dir),
        "device": device,
        "candidate_count": len(circles),
        "accepted_count": 0,
        "rejected_count": 0,
        "min_confidence": min_confidence,
        "min_margin": min_margin,
        "predictions": [],
    }
    if not image_body or not circles:
        report["status"] = "no_candidates"
        return {}, report

    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("numpy and Pillow are required for local classification") from exc

    image = Image.open(io.BytesIO(image_body)).convert("RGB")
    arrays = []
    valid_circles = []
    for index, circle in enumerate(circles):
        array = _crop_array(image, circle, crop_scale, image_size, np, Image)
        if array is None:
            continue
        arrays.append(array)
        valid_circles.append((index, circle))

    if not arrays:
        report["status"] = "no_valid_crops"
        report["rejected_count"] = len(circles)
        return {}, report

    predictor = _get_predictor(model_dir, device)
    batch = np.stack(arrays).astype("float32")
    with _PREDICT_LOCK:
        input_handle = predictor.get_input_handle(predictor.get_input_names()[0])
        input_handle.reshape(batch.shape)
        input_handle.copy_from_cpu(batch)
        predictor.run()
        output = predictor.get_output_handle(
            predictor.get_output_names()[0]
        ).copy_to_cpu()

    row_sums = output.sum(axis=1, keepdims=True)
    if np.all(output >= 0.0) and np.allclose(row_sums, 1.0, atol=1e-4):
        probabilities = output
    else:
        logits = output - output.max(axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)

    classifications = {}
    for (_, circle), scores in zip(valid_circles, probabilities):
        indices = np.argsort(scores)[-2:][::-1]
        class_id = int(indices[0])
        second_id = int(indices[1]) if len(indices) > 1 else class_id
        score = float(scores[class_id])
        second_score = float(scores[second_id]) if second_id != class_id else 0.0
        margin = score - second_score
        label = MODEL_LABELS[class_id]
        accepted = score >= min_confidence and margin >= min_margin
        circle_id = int(circle.get("circle_id", 0))
        report["predictions"].append(
            {
                "circle_id": circle_id,
                "label": label,
                "score": round(score, 6),
                "second_label": MODEL_LABELS[second_id],
                "second_score": round(second_score, 6),
                "margin": round(margin, 6),
                "accepted": accepted,
            }
        )
        if not accepted:
            continue
        character, name, color = LABEL_METADATA[label]
        classifications[circle_id] = {
            "char": character,
            "name": name,
            "color": color,
            "classification_source": "local_pplcnet",
            "local_classifier_label": label,
            "local_classifier_confidence": round(score, 6),
            "local_classifier_margin": round(margin, 6),
            "color_classification_status": "confident",
            "color_confidence": round(score, 6),
            "color_evidence": "local_pplcnet",
        }

    report["accepted_count"] = len(classifications)
    report["rejected_count"] = len(circles) - len(classifications)
    report["status"] = "ok" if len(classifications) == len(circles) else "partial"
    return classifications, report


def _default_server_python():
    classifier_root = Path(__file__).resolve().parents[1] / "local_piece_classifier"
    windows_python = classifier_root / ".venv" / "Scripts" / "python.exe"
    posix_python = classifier_root / ".venv" / "bin" / "python"
    if windows_python.is_file():
        return windows_python
    if posix_python.is_file():
        return posix_python
    return Path(sys.executable)


def _stop_server():
    global _SERVER_PROCESS
    process = _SERVER_PROCESS
    _SERVER_PROCESS = None
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


atexit.register(_stop_server)


def _get_server():
    global _SERVER_PROCESS
    if _SERVER_PROCESS is not None and _SERVER_PROCESS.poll() is None:
        return _SERVER_PROCESS

    configured = os.getenv("CHESS_LOCAL_CLASSIFIER_PYTHON", "").strip()
    python_executable = Path(configured).expanduser().resolve() if configured else _default_server_python()
    server_script = Path(__file__).resolve().with_name("local_piece_classifier_server.py")
    if not python_executable.is_file():
        raise FileNotFoundError(f"local classifier Python not found: {python_executable}")
    if not server_script.is_file():
        raise FileNotFoundError(f"local classifier server not found: {server_script}")

    child_environment = os.environ.copy()
    child_environment["CHESS_LOCAL_CLASSIFIER_BACKEND"] = "inprocess"
    child_environment["PYTHONIOENCODING"] = "utf-8"
    _SERVER_PROCESS = subprocess.Popen(
        [str(python_executable), str(server_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_environment,
    )
    return _SERVER_PROCESS


def _classify_candidates_subprocess(image_body, circles):
    request = {
        "image_base64": base64.b64encode(image_body).decode("ascii"),
        "circles": circles,
    }
    with _SERVER_LOCK:
        process = _get_server()
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("local classifier server pipes are unavailable")
        process.stdin.write(json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n")
        process.stdin.flush()
        while True:
            response_line = process.stdout.readline()
            if not response_line:
                return_code = process.poll()
                _stop_server()
                raise RuntimeError(
                    f"local classifier server stopped unexpectedly (exit={return_code})"
                )
            if response_line.startswith(_SERVER_RESPONSE_PREFIX):
                response_line = response_line[len(_SERVER_RESPONSE_PREFIX):]
                break
    response = json.loads(response_line)
    if response.get("error"):
        raise RuntimeError(response["error"])
    classifications = {
        int(circle_id): classification
        for circle_id, classification in response.get("classifications", {}).items()
    }
    report = response.get("report", {})
    report["backend"] = "subprocess"
    return classifications, report


def classify_candidates(image_body, circles):
    """Use in-process Paddle when available, otherwise use the model venv."""
    backend = os.getenv("CHESS_LOCAL_CLASSIFIER_BACKEND", "auto").strip().lower()
    if backend not in ("auto", "inprocess", "subprocess"):
        raise ValueError(
            "CHESS_LOCAL_CLASSIFIER_BACKEND must be auto, inprocess, or subprocess"
        )
    if backend == "inprocess" or (
        backend == "auto" and importlib.util.find_spec("paddle") is not None
    ):
        classifications, report = _classify_candidates_inprocess(image_body, circles)
        report["backend"] = "inprocess"
        return classifications, report
    return _classify_candidates_subprocess(image_body, circles)


def enabled():
    return _bool_env("CHESS_LOCAL_CLASSIFIER_ENABLED", "true")
