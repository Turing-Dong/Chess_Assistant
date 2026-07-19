# Aliyun FC OSS Handler

This Function Compute handler completes the ESP32 image-processing loop:

1. ESP32 uploads `devices/<device_id>/requests/<frame_id>.jpg`.
2. ESP32 uploads `devices/<device_id>/requests/latest.json`.
3. OSS event trigger invokes `index.handler`.
4. The handler reads the manifest and image, then writes
   `devices/<device_id>/results/<frame_id>.json`.
5. ESP32 polls the result JSON and validates it.

The `points` field uses player-local Xiangqi board coordinates, not image or
LCD pixels:

- `x`: `0..8`, from the current player's left side to right side.
- `y`: `0..9`, from the current player's baseline toward the opponent.
- `player1_*` is expressed in player1's local coordinate system.
- `player2_*` is expressed in player2's local coordinate system.
- `player1` is the shuai side, and `player2` is the jiang side.

The handler also writes `canonical_points` for debugging. Canonical coordinates
use one internal board frame: `x = 0..8` from player1 left to right, and
`y = 0..9` from player1 baseline to player2 baseline.

Configure the OSS trigger to invoke the function when `latest.json` is written.
The function needs OSS read/write permissions for the target bucket and should
run in the same region as the bucket, currently `cn-hangzhou`.
