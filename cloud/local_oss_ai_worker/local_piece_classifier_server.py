"""Persistent JSON-lines inference process for the worker classifier adapter."""

from __future__ import annotations

import base64
import json
import os
import sys

from local_piece_classifier import (
    _SERVER_RESPONSE_PREFIX,
    _classify_candidates_inprocess,
)


def main():
    os.environ["CHESS_LOCAL_CLASSIFIER_BACKEND"] = "inprocess"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            image_body = base64.b64decode(request["image_base64"], validate=True)
            classifications, report = _classify_candidates_inprocess(
                image_body,
                request.get("circles", []),
            )
            response = {
                "classifications": classifications,
                "report": report,
            }
        except Exception as exc:  # Keep the server alive for later frames.
            response = {"error": f"{type(exc).__name__}: {exc}"}
        print(
            _SERVER_RESPONSE_PREFIX
            + json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )


if __name__ == "__main__":
    main()
