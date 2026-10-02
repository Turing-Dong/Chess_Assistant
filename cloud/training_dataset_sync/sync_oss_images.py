#!/usr/bin/env python3
"""Incrementally mirror device request images from Aliyun OSS for training."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import oss2


IMAGE_KEY_PATTERN = re.compile(
    r"^devices/(?P<device_id>[^/]+)/requests/(?P<frame_id>[^/]+)\.(?P<extension>jpe?g|png)$",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if name:
            os.environ.setdefault(name, value)


def safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "unknown"


def parse_device_ids(values: list[str] | None) -> list[str]:
    device_ids = []
    seen = set()
    for value in values or []:
        for item in value.split(","):
            device_id = item.strip()
            if not device_id or device_id in seen:
                continue
            if "/" in device_id:
                raise RuntimeError(f"Invalid device id: {device_id}")
            seen.add(device_id)
            device_ids.append(device_id)
    return device_ids


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "objects": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read sync state {path}: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("objects"), dict):
        raise RuntimeError(f"Invalid sync state format: {path}")
    return state


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_metadata(output_dir: Path, objects: dict) -> None:
    metadata_path = output_dir / "metadata.jsonl"
    temporary = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file_obj:
        for key in sorted(objects):
            record = dict(objects[key])
            record["source_key"] = key
            file_obj.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, metadata_path)


def build_bucket(env_file: Path):
    load_env_file(env_file)
    bucket_name = os.getenv("OSS_BUCKET")
    region = os.getenv("OSS_REGION", "cn-hangzhou")
    endpoint = os.getenv("OSS_ENDPOINT") or f"https://oss-{region}.aliyuncs.com"
    access_key_id = os.getenv("ALIYUN_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIYUN_ACCESS_KEY_SECRET")
    security_token = os.getenv("ALIYUN_SECURITY_TOKEN")

    missing = [
        name
        for name, value in (
            ("OSS_BUCKET", bucket_name),
            ("ALIYUN_ACCESS_KEY_ID", access_key_id),
            ("ALIYUN_ACCESS_KEY_SECRET", access_key_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))

    if security_token:
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
    else:
        auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name), bucket_name


def download_object(bucket, key: str, destination: Path) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    total_size = 0
    try:
        result = bucket.get_object(key)
        with temporary.open("wb") as file_obj:
            while True:
                chunk = result.read(1024 * 1024)
                if not chunk:
                    break
                file_obj.write(chunk)
                digest.update(chunk)
                total_size += len(chunk)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest.hexdigest(), total_size


def sync_once(
    bucket,
    bucket_name: str,
    output_dir: Path,
    state_file: Path,
    device_ids: list[str],
) -> dict:
    state = load_state(state_file)
    objects = state["objects"]
    downloaded = 0
    skipped = 0
    failures = []
    devices = set()

    for configured_device_id in device_ids:
        prefix = f"devices/{configured_device_id}/requests/"
        for obj in oss2.ObjectIterator(bucket, prefix=prefix):
            match = IMAGE_KEY_PATTERN.match(obj.key)
            if match is None or match.group("device_id") != configured_device_id:
                continue

            device_id = match.group("device_id")
            frame_id = match.group("frame_id")
            extension = match.group("extension").lower().replace("jpeg", "jpg")
            devices.add(device_id)
            relative_path = Path("raw") / safe_component(device_id) / (
                safe_component(frame_id) + "." + extension
            )
            destination = output_dir / relative_path
            etag = (obj.etag or "").strip('"')
            previous = objects.get(obj.key)

            if (
                isinstance(previous, dict)
                and previous.get("etag") == etag
                and previous.get("size") == obj.size
                and destination.exists()
                and destination.stat().st_size == obj.size
            ):
                skipped += 1
                continue

            try:
                sha256, downloaded_size = download_object(bucket, obj.key, destination)
                if downloaded_size != obj.size:
                    raise RuntimeError(
                        f"size mismatch: expected {obj.size}, downloaded {downloaded_size}"
                    )
                objects[obj.key] = {
                    "bucket": bucket_name,
                    "device_id": device_id,
                    "frame_id": frame_id,
                    "etag": etag,
                    "size": downloaded_size,
                    "last_modified": obj.last_modified,
                    "local_path": relative_path.as_posix(),
                    "sha256": sha256,
                    "downloaded_at": utc_now(),
                }
                downloaded += 1
                print(f"downloaded {obj.key} -> {destination}", file=sys.stderr)
            except Exception as exc:
                failures.append({"key": obj.key, "error": str(exc)})

    state["version"] = 1
    state["last_sync_at"] = utc_now()
    state["objects"] = objects
    write_json_atomic(state_file, state)
    write_metadata(output_dir, objects)

    return {
        "status": "ok" if not failures else "partial_failure",
        "downloaded": downloaded,
        "skipped": skipped,
        "tracked": len(objects),
        "devices": sorted(devices),
        "failures": failures,
        "output_dir": str(output_dir.resolve()),
        "state_file": str(state_file.resolve()),
        "synced_at": state["last_sync_at"],
    }


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    project_root = script_path.parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=script_path.parents[1] / "local_oss_ai_worker" / ".env",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_root / "training_dataset",
    )
    parser.add_argument(
        "--device-id",
        action="append",
        help="Device ID to synchronize. Repeat or comma-separate for multiple devices; defaults to DEVICE_IDS/DEVICE_ID.",
    )
    parser.add_argument("--state-file", type=Path)
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continue monitoring instead of performing one synchronization.",
    )
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    state_file = (args.state_file or output_dir / ".sync_state.json").resolve()
    if args.interval_seconds <= 0:
        raise RuntimeError("--interval-seconds must be greater than zero")

    bucket, bucket_name = build_bucket(args.env_file.resolve())
    configured_values = args.device_id
    if not configured_values:
        configured_values = [os.getenv("DEVICE_IDS", ""), os.getenv("DEVICE_ID", "")]
    device_ids = parse_device_ids(configured_values)
    if not device_ids:
        raise RuntimeError("No device IDs configured; set DEVICE_IDS or use --device-id")
    while True:
        summary = sync_once(bucket, bucket_name, output_dir, state_file, device_ids)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
        if summary["failures"]:
            return 2
        if not args.watch:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1)
