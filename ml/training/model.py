"""
Fraud scoring model architecture.

Sequence-based behavioral model inspired by approaches that use sequential
transaction patterns for anomaly detection. Uses a GRU encoder for behavioral
sequences combined with dense feature aggregation.

Architecture:
    ┌──────────────────┐     ┌─────────────────┐
    │ Transaction       │     │ Behavioral       │
    │ Feature Vector    │     │ Sequence (N,4)   │
    │ (64-dim)          │     │                  │
    └────────┬─────────┘     └────────┬─────────┘
             │                        │
    ┌────────▼─────────┐     ┌────────▼─────────┐
    │ Dense Encoder     │     │ GRU Encoder      │
    │ 64 → 128 → 64    │     │ input=4, h=64    │
    └────────┬─────────┘     └────────┬─────────┘
             │                        │
             └──────────┬─────────────┘
                        │ concat (128-dim)
               ┌────────▼─────────┐
               │ Fusion Network    │
               │ 128 → 64 → 32 → 1│
               └────────┬─────────┘
                        │ sigmoid
                   fraud_score ∈ [0,1]

Export: PyTorch → ONNX for production inference.
"""

from __future__ import annotations

import torch
import torch.nn as nn


class SequenceEncoder(nn.Module):
    """GRU-based encoder for transaction behavioral sequences."""

    def __init__(
        self,
        input_dim: int = 4,
        hidden_dim: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
    ):
        super().__init__()
        self.gru = nn.GRU(
            input_size=input_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
            bidirectional=False,
        )
        self.layer_norm = nn.LayerNorm(hidden_dim)

    def forward(self, x: torch.Tensor, lengths: torch.Tensor | None = None) -> torch.Tensor:
        """
        Args:
            x: (batch, seq_len, input_dim) - padded sequence
            lengths: (batch,) - actual lengths for packing (optional)
        Returns:
            (batch, hidden_dim) - final hidden state
        """
        if lengths is not None:
            packed = nn.utils.rnn.pack_padded_sequence(
                x, lengths.cpu(), batch_first=True, enforce_sorted=False
            )
            _, h_n = self.gru(packed)
        else:
            _, h_n = self.gru(x)

        # Use final layer hidden state
        out = h_n[-1]  # (batch, hidden_dim)
        return self.layer_norm(out)


class DenseEncoder(nn.Module):
    """Dense feature encoder for transaction-level features."""

    def __init__(self, input_dim: int = 64, hidden_dim: int = 128, output_dim: int = 64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
            nn.Dropout(0.2),
            nn.Linear(hidden_dim, output_dim),
            nn.LayerNorm(output_dim),
            nn.GELU(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class FraudScoringModel(nn.Module):
    """
    Combined fraud scoring model.
    Fuses transaction-level features with behavioral sequence patterns.
    """

    def __init__(
        self,
        feature_dim: int = 64,
        sequence_input_dim: int = 4,
        sequence_hidden_dim: int = 64,
        fusion_hidden_dim: int = 64,
    ):
        super().__init__()
        self.dense_encoder = DenseEncoder(
            input_dim=feature_dim,
            hidden_dim=128,
            output_dim=sequence_hidden_dim,
        )
        self.sequence_encoder = SequenceEncoder(
            input_dim=sequence_input_dim,
            hidden_dim=sequence_hidden_dim,
            num_layers=2,
        )

        combined_dim = sequence_hidden_dim * 2  # dense + sequence

        self.fusion = nn.Sequential(
            nn.Linear(combined_dim, fusion_hidden_dim),
            nn.LayerNorm(fusion_hidden_dim),
            nn.GELU(),
            nn.Dropout(0.3),
            nn.Linear(fusion_hidden_dim, 32),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(32, 1),
        )

    def forward(
        self,
        features: torch.Tensor,
        sequence: torch.Tensor,
        seq_lengths: torch.Tensor | None = None,
    ) -> torch.Tensor:
        """
        Args:
            features: (batch, feature_dim) - transaction feature vector
            sequence: (batch, seq_len, 4) - behavioral sequence
            seq_lengths: (batch,) - actual sequence lengths
        Returns:
            (batch, 1) - fraud probability in [0, 1]
        """
        dense_out = self.dense_encoder(features)
        seq_out = self.sequence_encoder(sequence, seq_lengths)

        fused = torch.cat([dense_out, seq_out], dim=-1)
        logits = self.fusion(fused)
        return torch.sigmoid(logits)


def create_model(
    feature_dim: int = 64,
    sequence_input_dim: int = 4,
) -> FraudScoringModel:
    """Factory for creating a new model instance."""
    return FraudScoringModel(
        feature_dim=feature_dim,
        sequence_input_dim=sequence_input_dim,
    )
