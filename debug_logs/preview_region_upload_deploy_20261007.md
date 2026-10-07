# Preview-region upload deployment log

- Date: 2026-10-07
- Target: ESP32-S3 (device identifier omitted)
- Serial port: `COM9` (`USB-SERIAL CH340`)
- Flash target: application partition at `0x10000`
- Result: build succeeded, flash hash verified, device hard-reset succeeded

## Image upload behavior

- Camera source: `1920x1080` JPEG
- Uploaded region: centered `1080x1080`, source rectangle `(420,0)-(1499,1079)`
- Uploaded format: JPEG, quality 90
- Manifest dimensions: `1080x1080`
- Failure policy: abort upload if crop allocation, decode, or JPEG encoding fails; never fall back to the full frame

## Firmware artifact

- File: `build/05_Time_slice_scheduling.bin`
- Size: `1355056` bytes
- SHA-256: `4D68800099380DD80E4E879A40FD4341596084485D859D126C32DE553B358CF5`
- Application partition free space after build: approximately 33%

## Runtime verification

- Booted from application partition at `0x10000`
- Detected ESP32-S3 revision v0.2 and 8 MB PSRAM
- OV5640 camera initialized in FHD JPEG mode
- First `1920x1080` preview frame captured, decoded, and displayed
- Wi-Fi connected successfully; device obtained a local-network IP address

## Upload log messages

The following messages are emitted during the next image upload:

```text
Captured source image: 1920x1080, <source_bytes> bytes
Preview-region JPEG ready: source=1920x1080 crop=(420,0 1080x1080) bytes=<jpeg_bytes> decode=<ms> ms encode=<ms> ms
Prepared preview-region upload: 1080x1080, <jpeg_bytes> bytes
Uploading preview-region JPEG: bytes=<jpeg_bytes> attempt=<n>/<max>
Preview-region JPEG upload succeeded: bytes=<jpeg_bytes> attempt=<n>
Preview-region frame uploaded: frame_id=<frame_id> dimensions=1080x1080 bytes=<jpeg_bytes>
```

An end-to-end crop/upload event still requires the device upload key to be triggered once after deployment.
