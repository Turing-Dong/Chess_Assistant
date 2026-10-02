"""Run the exported PP-LCNet inference model on one or more piece crops."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

import paddle.inference as paddle_infer


MEAN = np.asarray([0.485, 0.456, 0.406], dtype="float32").reshape(1, 1, 3)
STD = np.asarray([0.229, 0.224, 0.225], dtype="float32").reshape(1, 1, 3)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    repo_root = root.parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=root / "output" / "xiangqi_pplcnet_x1_0" / "inference",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=repo_root
        / "training_dataset"
        / "piece_character_dataset"
        / "paddleclas"
        / "label_list.txt",
    )
    parser.add_argument("--topk", type=int, default=3)
    parser.add_argument("--cpu", action="store_true")
    return parser.parse_args()


def load_labels(path: Path) -> dict[int, str]:
    labels: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        index, name = line.split(maxsplit=1)
        labels[int(index)] = name
    return labels


def preprocess(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image = image.convert("RGB").resize((160, 160), Image.Resampling.BILINEAR)
        array = np.asarray(image, dtype="float32") / 255.0
    array = (array - MEAN) / STD
    return np.transpose(array, (2, 0, 1))


def main() -> None:
    args = parse_args()
    model_file = args.model_dir / "inference.json"
    params_file = args.model_dir / "inference.pdiparams"
    if not model_file.is_file() or not params_file.is_file():
        raise FileNotFoundError(f"Exported model not found under {args.model_dir}")

    config = paddle_infer.Config(str(model_file), str(params_file))
    if args.cpu:
        config.disable_gpu()
    else:
        config.enable_use_gpu(512, 0)
    config.switch_ir_optim(True)
    predictor = paddle_infer.create_predictor(config)

    batch = np.stack([preprocess(path) for path in args.images]).astype("float32")
    input_handle = predictor.get_input_handle(predictor.get_input_names()[0])
    input_handle.reshape(batch.shape)
    input_handle.copy_from_cpu(batch)
    predictor.run()
    output = predictor.get_output_handle(predictor.get_output_names()[0]).copy_to_cpu()
    row_sums = output.sum(axis=1, keepdims=True)
    if np.all(output >= 0.0) and np.allclose(row_sums, 1.0, atol=1e-4):
        probabilities = output
    else:
        output -= output.max(axis=1, keepdims=True)
        probabilities = np.exp(output)
        probabilities /= probabilities.sum(axis=1, keepdims=True)

    labels = load_labels(args.labels)
    topk = min(max(1, args.topk), probabilities.shape[1])
    results = []
    for path, scores in zip(args.images, probabilities):
        indices = np.argsort(scores)[-topk:][::-1]
        results.append(
            {
                "image": str(path.resolve()),
                "predictions": [
                    {
                        "class_id": int(index),
                        "label": labels[int(index)],
                        "score": round(float(scores[index]), 6),
                    }
                    for index in indices
                ],
            }
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
