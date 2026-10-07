# Full-frame upload and worker preprocessing log

- Date: 2026-10-07
- Branch: `codex/preview-region-upload`
- Scope: ESP32 image upload, local OSS worker preprocessing, grid placement,
  classifier threshold, training utilities, and pipeline documentation

## Reason for the change

The device-side 1080x1080 crop/re-encode path produced a fixed-size truncated
JPEG in a runtime test. Smaller dimensions therefore did not reduce the failure
risk: the uploaded byte stream itself was incomplete. The crop was moved to the
worker, where the downloaded JPEG can be fully decoded and validated first.

## Device upload behavior

- Capture the camera's original JPEG frame (normally `1920x1080`).
- Copy the complete JPEG to a PSRAM-capable upload buffer.
- Upload that JPEG without device-side decode, crop, or re-encode.
- Publish the source dimensions in the manifest.
- Log each upload attempt, success, and the final frame ID/dimensions/byte count.

Expected device log messages:

```text
Captured image: 1920x1080, <bytes> bytes
Uploading camera JPEG: bytes=<bytes> attempt=<n>/<max>
Camera JPEG upload succeeded: bytes=<bytes> attempt=<n>
Full-frame image uploaded: frame_id=<frame_id> dimensions=1920x1080 bytes=<bytes>
```

## Worker preprocessing behavior

- Decode the complete source image with Pillow.
- Center-crop it to `1080x1080` and encode at JPEG quality 95.
- Run rectification, grid detection, circle detection, classification, position
  validation, and move generation on the cropped image.
- Preserve the original manifest dimensions in the device-facing result. This
  is required because the ESP32 validates result dimensions against its upload.
- Anchor the first vertical grid line to the expected offset from the left edge
  of the 1080x1080 image (`0.117 * width` by default), while searching nearby
  lines inside a bounded dynamic window.

Expected worker log message:

```text
Center crop complete: source=1920x1080 crop=[420, 0, 1500, 1080] output=1080x1080 bytes=<bytes> status=center_cropped
```

## Classifier and training updates

- Local classifier minimum confidence default: `0.50`.
- Board-image test utility uses the same `0.50` default.
- Dataset2 utilities cover conservative subset creation, deterministic physical
  augmentation, class balancing, fine-tuning, and single-image validation.
- Local datasets and generated samples remain excluded from Git.

## Runtime evidence

- Latest processed frame observed before commit:
  `chess-441BF68B2D54-4D0C5644-000001`.
- Result status: `ok`.
- Source protocol dimensions: `1920x1080`.
- Recognized pieces in the saved debug result: 19.
- The prior truncated `1080x1080` upload failure is retained only in local
  worker logs for diagnosis and is not committed.

## Files intentionally not uploaded

- `training_dataset2/`: reviewed samples, generated augmentations, manifests,
  and training outputs.
- `新建文件夹/`: local synchronized image cache.
- Worker `.env`, debug images, state files, logs, virtual environments, and
  classifier weights.

## Validation checklist

- Python syntax compilation: passed for the worker, classifier, board-image test,
  subset builder, and augmentation materializer.
- Worker smoke tests: passed for a synthetic `1920x1080` to `1080x1080`
  center crop (`[420, 0, 1500, 1080]`) and left-reference grid anchoring.
- PowerShell training-script parse: passed.
- Both PaddleClas YAML files: parsed successfully; `class_num` is 14.
- Git whitespace/error check: passed.
- ESP-IDF build: attempted through both the configured CMake executable and the
  ESP-IDF environment script. The host toolchain could not start because Windows
  reported error 623 (system DLL relocation) before compilation; no C source
  diagnostic was produced. The last previously built firmware artifact is not
  treated as validation of this log-only firmware edit.
- Remote branch synchronization and push: performed after this checklist.
