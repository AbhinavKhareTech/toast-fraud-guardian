"""
Export trained PyTorch fraud scoring model to ONNX format for production inference.
ONNX Runtime provides deterministic, low-latency inference without PyTorch dependency.

Usage:
    python -m ml.export.to_onnx --checkpoint ml/training/checkpoints/best.pt --output ml/export/fraud_scorer_v1.onnx
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch

from ml.training.model import FraudScoringModel, create_model


def export_to_onnx(
    model: FraudScoringModel,
    output_path: str,
    feature_dim: int = 64,
    sequence_length: int = 20,
    sequence_input_dim: int = 4,
    opset_version: int = 17,
) -> None:
    """Export model to ONNX with dynamic batch size."""
    model.eval()

    # Dummy inputs for tracing
    batch_size = 1
    dummy_features = torch.randn(batch_size, feature_dim)
    dummy_sequence = torch.randn(batch_size, sequence_length, sequence_input_dim)
    dummy_lengths = torch.tensor([sequence_length], dtype=torch.long)

    torch.onnx.export(
        model,
        (dummy_features, dummy_sequence, dummy_lengths),
        output_path,
        export_params=True,
        opset_version=opset_version,
        do_constant_folding=True,
        input_names=["features", "sequence", "seq_lengths"],
        output_names=["fraud_score"],
        dynamic_axes={
            "features": {0: "batch_size"},
            "sequence": {0: "batch_size"},
            "seq_lengths": {0: "batch_size"},
            "fraud_score": {0: "batch_size"},
        },
    )

    # Verify exported model
    import onnx
    onnx_model = onnx.load(output_path)
    onnx.checker.check_model(onnx_model)

    # Verify inference matches
    import onnxruntime as ort
    session = ort.InferenceSession(output_path)
    onnx_result = session.run(
        None,
        {
            "features": dummy_features.numpy(),
            "sequence": dummy_sequence.numpy(),
            "seq_lengths": dummy_lengths.numpy(),
        },
    )

    with torch.no_grad():
        torch_result = model(dummy_features, dummy_sequence, dummy_lengths).numpy()

    diff = np.abs(onnx_result[0] - torch_result).max()
    print(f"Exported to {output_path}")
    print(f"Max numerical difference (PyTorch vs ONNX): {diff:.8f}")
    assert diff < 1e-5, f"ONNX export verification failed: max diff {diff}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export fraud model to ONNX")
    parser.add_argument("--checkpoint", type=str, default=None, help="Path to PyTorch checkpoint")
    parser.add_argument("--output", type=str, default="ml/export/fraud_scorer_v1.onnx")
    parser.add_argument("--feature-dim", type=int, default=64)
    parser.add_argument("--sequence-length", type=int, default=20)
    args = parser.parse_args()

    model = create_model(feature_dim=args.feature_dim)

    if args.checkpoint and Path(args.checkpoint).exists():
        state = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
        model.load_state_dict(state["model_state_dict"])
        print(f"Loaded checkpoint: {args.checkpoint}")
    else:
        print("WARNING: No checkpoint provided. Exporting randomly initialized model.")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    export_to_onnx(model, args.output, feature_dim=args.feature_dim, sequence_length=args.sequence_length)


if __name__ == "__main__":
    main()
