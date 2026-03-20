"""
Training pipeline for the fraud scoring model.
Uses labeled data from the feedback loop (fraud_score_log table).

Usage:
    python -m ml.training.train --epochs 50 --batch-size 256 --lr 0.001

NOTE: This is a structured training script. In production, integrate with
MLflow for experiment tracking and model registry.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, random_split

from ml.training.model import create_model


class FraudDataset(Dataset):
    """
    Dataset for fraud scoring model training.
    In production, load from PostgreSQL fraud_score_log table
    where actual_fraud labels are available (feedback loop).
    """

    def __init__(self, features: np.ndarray, sequences: np.ndarray, labels: np.ndarray):
        self.features = torch.tensor(features, dtype=torch.float32)
        self.sequences = torch.tensor(sequences, dtype=torch.float32)
        self.labels = torch.tensor(labels, dtype=torch.float32).unsqueeze(-1)

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        return self.features[idx], self.sequences[idx], self.labels[idx]


def generate_synthetic_data(n_samples: int = 10000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Generate synthetic training data for development/testing.
    Replace with real data pipeline in production.
    """
    rng = np.random.default_rng(42)

    features = rng.standard_normal((n_samples, 64)).astype(np.float32)
    sequences = rng.standard_normal((n_samples, 20, 4)).astype(np.float32)

    # Create semi-realistic labels based on feature patterns
    # High amount + late night + no tip = more likely fraud
    fraud_signal = (
        features[:, 0] * 0.3  # amount proxy
        + features[:, 5] * 0.2  # time proxy
        - features[:, 10] * 0.1  # tip proxy
        + rng.standard_normal(n_samples) * 0.5
    )
    labels = (fraud_signal > np.percentile(fraud_signal, 90)).astype(np.float32)

    print(f"Generated {n_samples} samples, fraud rate: {labels.mean():.2%}")
    return features, sequences, labels


def train(
    epochs: int = 50,
    batch_size: int = 256,
    learning_rate: float = 0.001,
    output_dir: str = "ml/training/checkpoints",
) -> None:
    """Train the fraud scoring model."""
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Training on: {device}")

    # Data
    features, sequences, labels = generate_synthetic_data()
    dataset = FraudDataset(features, sequences, labels)

    train_size = int(0.8 * len(dataset))
    val_size = len(dataset) - train_size
    train_ds, val_ds = random_split(dataset, [train_size, val_size])

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size)

    # Model
    model = create_model().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=0.01)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.BCELoss()

    # Training loop
    best_val_loss = float("inf")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for epoch in range(epochs):
        # Train
        model.train()
        train_loss = 0.0
        for features_batch, seq_batch, labels_batch in train_loader:
            features_batch = features_batch.to(device)
            seq_batch = seq_batch.to(device)
            labels_batch = labels_batch.to(device)

            optimizer.zero_grad()
            preds = model(features_batch, seq_batch)
            loss = criterion(preds, labels_batch)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            train_loss += loss.item()

        scheduler.step()

        # Validate
        model.eval()
        val_loss = 0.0
        correct = 0
        total = 0
        with torch.no_grad():
            for features_batch, seq_batch, labels_batch in val_loader:
                features_batch = features_batch.to(device)
                seq_batch = seq_batch.to(device)
                labels_batch = labels_batch.to(device)

                preds = model(features_batch, seq_batch)
                val_loss += criterion(preds, labels_batch).item()
                predicted = (preds > 0.5).float()
                correct += (predicted == labels_batch).sum().item()
                total += labels_batch.size(0)

        avg_train = train_loss / len(train_loader)
        avg_val = val_loss / len(val_loader)
        accuracy = correct / total if total > 0 else 0

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(
                f"Epoch {epoch+1}/{epochs} | "
                f"Train Loss: {avg_train:.4f} | "
                f"Val Loss: {avg_val:.4f} | "
                f"Val Acc: {accuracy:.4f}"
            )

        # Save best model
        if avg_val < best_val_loss:
            best_val_loss = avg_val
            torch.save(
                {
                    "epoch": epoch,
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "val_loss": avg_val,
                    "val_accuracy": accuracy,
                },
                f"{output_dir}/best.pt",
            )

    print(f"\nTraining complete. Best val loss: {best_val_loss:.4f}")
    print(f"Checkpoint saved to: {output_dir}/best.pt")
    print(f"\nTo export: python -m ml.export.to_onnx --checkpoint {output_dir}/best.pt")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train fraud scoring model")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--output-dir", type=str, default="ml/training/checkpoints")
    args = parser.parse_args()

    train(
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
