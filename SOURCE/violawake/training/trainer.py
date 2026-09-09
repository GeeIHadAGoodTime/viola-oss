"""
ViolaWake Training Functions
=============================

Training loop and evaluation utilities.

Supports:
- FocalLoss (default) or BCELoss
- AdamW optimizer with weight decay
- Gradient clipping
- Linear warmup + cosine annealing LR schedule
- Exponential Moving Average (EMA) of model parameters
- Stochastic Weight Averaging (SWA, optional)
"""

from __future__ import annotations

import time
from pathlib import Path

from core.console import console

# Check for PyTorch
_TORCH_AVAILABLE = False
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.optim.swa_utils import SWALR, AveragedModel, update_bn
    from torch.utils.data import DataLoader

    _TORCH_AVAILABLE = True
except ImportError:
    pass


def evaluate_real_samples(
    model,
    eval_dir: str | Path,
    device: str = "cpu",
) -> dict:
    """Score real-world samples to detect overtraining.

    Returns d-prime: (tp_mean - fp_mean) / sqrt(tp_std^2 + fp_std^2)
    Higher is better. Track this across epochs — declining d-prime means
    overtraining on synthetic/augmented data at the expense of real-world
    performance.

    Args:
        model: Trained model (will be set to eval mode)
        eval_dir: Directory with positives/ and negatives/ subdirectories
        device: Device for inference

    Returns:
        Dictionary with d_prime, tp_mean, tp_std, fp_mean, fp_max, tp_scores, fp_scores
    """
    import numpy as np

    from violawake.audio import center_crop, compute_features, load_audio
    from violawake.config import CLIP_SAMPLES

    eval_path = Path(eval_dir)
    pos_dir = eval_path / "positives"
    neg_dir = eval_path / "negatives"

    model.eval()
    model = model.to(device)

    def _score_file(wav_path: Path) -> float | None:
        audio = load_audio(wav_path)
        if audio is None:
            return None
        # Match inference pipeline: center_crop → features → model
        # NO normalize_audio — matching production inference (2026-03-02)
        audio = center_crop(audio, CLIP_SAMPLES)
        mel = compute_features(audio)
        mel_tensor = torch.from_numpy(mel).float().unsqueeze(0).to(device)
        with torch.no_grad():
            score = float(model(mel_tensor).item())
        return score

    tp_scores = []
    # Walk subdirectories recursively (eval set uses provenance subdirs)
    pos_files = sorted(list(pos_dir.rglob("*.wav")) + list(pos_dir.rglob("*.flac")))
    for f in pos_files:
        s = _score_file(f)
        if s is not None:
            tp_scores.append(s)

    fp_scores = []
    neg_files = sorted(list(neg_dir.rglob("*.wav")) + list(neg_dir.rglob("*.flac")))
    for f in neg_files:
        s = _score_file(f)
        if s is not None:
            fp_scores.append(s)

    if not tp_scores or not fp_scores:
        return {
            "d_prime": 0.0,
            "tp_mean": 0.0,
            "tp_std": 0.0,
            "fp_mean": 0.0,
            "fp_max": 0.0,
            "tp_scores": tp_scores,
            "fp_scores": fp_scores,
        }

    tp_mean = float(np.mean(tp_scores))
    tp_std = float(np.std(tp_scores))
    fp_mean = float(np.mean(fp_scores))
    fp_std = float(np.std(fp_scores))
    fp_max = float(np.max(fp_scores))

    denom = np.sqrt(tp_std**2 + fp_std**2)
    d_prime = (tp_mean - fp_mean) / denom if denom > 1e-10 else 0.0

    return {
        "d_prime": d_prime,
        "tp_mean": tp_mean,
        "tp_std": tp_std,
        "fp_mean": fp_mean,
        "fp_max": fp_max,
        "tp_scores": tp_scores,
        "fp_scores": fp_scores,
    }


def train_model(
    model,
    train_loader,
    val_loader,
    epochs: int = 30,
    learning_rate: float = 0.001,
    device: str = "cpu",
    verbose: bool = True,
    *,
    use_focal_loss: bool = True,
    weight_decay: float = 1e-4,
    max_grad_norm: float = 1.0,
    warmup_epochs: int = 5,
    use_ema: bool = True,
    ema_decay: float = 0.999,
    use_swa: bool = False,
    checkpoint_dir: str | None = None,
    eval_real_dir: str | None = None,
    eval_real_interval: int = 5,
    early_stop_patience: int = 3,
    resume_state: dict | None = None,
) -> dict:
    """
    Train a wake word model.

    Args:
        model: WakeWordModel instance
        train_loader: DataLoader for training data
        val_loader: DataLoader for validation data
        epochs: Number of training epochs
        learning_rate: Initial learning rate
        device: Device to train on
        verbose: Whether to print progress
        use_focal_loss: Use FocalLoss instead of BCELoss (default True)
        weight_decay: AdamW weight decay (default 1e-4)
        max_grad_norm: Max gradient norm for clipping (default 1.0)
        warmup_epochs: Number of linear warmup epochs (default 5)
        use_ema: Use Exponential Moving Average (default True)
        ema_decay: EMA decay rate (default 0.999)
        use_swa: Use Stochastic Weight Averaging (default False)
        resume_state: Checkpoint dict from --resume-from. If provided, restores
            optimizer, scheduler, EMA, and tracking state to continue training.

    Returns:
        Dictionary with training results and best model state
    """
    if not _TORCH_AVAILABLE:
        raise ImportError("PyTorch required for training")

    from violawake.config import get_feature_config
    from violawake.training.ema import ExponentialMovingAverage
    from violawake.training.losses import FocalLoss

    model = model.to(device)

    # --- Loss function ---
    if use_focal_loss:
        criterion = FocalLoss(gamma=2.0, alpha=0.75, label_smoothing=0.05)
        if verbose:
            console("  Loss: FocalLoss (gamma=2.0, alpha=0.75, smoothing=0.05)")
    else:
        criterion = nn.BCELoss()
        if verbose:
            console("  Loss: BCELoss")

    # --- Optimizer ---
    optimizer = optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    if verbose:
        console(f"  Optimizer: AdamW (lr={learning_rate}, weight_decay={weight_decay})")

    # --- LR Scheduler: warmup + cosine annealing ---
    # During warmup: linearly ramp from 1e-5 to learning_rate over warmup_epochs
    # After warmup: plain cosine annealing to eta_min over remaining epochs
    warmup_start_lr = 1e-5

    cosine_scheduler = optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(1, epochs - warmup_epochs), eta_min=1e-6
    )

    def warmup_lambda(epoch: int) -> float:
        """Linear warmup factor for LambdaLR (epoch is 0-indexed)."""
        if epoch < warmup_epochs:
            # Linear interpolation from warmup_start_lr to learning_rate
            alpha = epoch / max(warmup_epochs, 1)
            return (warmup_start_lr + alpha * (learning_rate - warmup_start_lr)) / learning_rate
        return 1.0  # After warmup, factor is 1.0 (cosine takes over)

    warmup_scheduler = optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=warmup_lambda)

    if verbose:
        console(
            f"  Schedule: {warmup_epochs}-epoch warmup ({warmup_start_lr:.0e} -> {learning_rate})"
            f" + CosineAnnealingLR(T_max={max(1, epochs - warmup_epochs)})"
        )

    # --- EMA ---
    ema = None
    if use_ema:
        ema = ExponentialMovingAverage(model, decay=ema_decay)
        if verbose:
            console(f"  EMA: enabled (decay={ema_decay})")

    # --- SWA ---
    # SWA window: epochs 30-40. Checkpoint sweep (2026-03-02) showed
    # quality peaked at ep040 (d-prime 8.87) and declined after.
    # Averaging overfit epochs 41-50 reduced SWA model d-prime to 7.96.
    swa_model = None
    swa_scheduler = None
    swa_start_epoch = 30
    if use_swa:
        swa_model = AveragedModel(model)
        swa_scheduler = SWALR(optimizer, swa_lr=learning_rate * 0.1, anneal_epochs=5)
        if verbose:
            console(f"  SWA: enabled (starts epoch {swa_start_epoch}," f" swa_lr={learning_rate * 0.1:.0e})")

    if verbose:
        console(f"  Gradient clipping: max_norm={max_grad_norm}")
        if eval_real_dir:
            console(
                f"  Real-sample eval: every {eval_real_interval} epochs," f" early stop patience={early_stop_patience}"
            )
        console()

    best_f1 = 0
    best_model_state = None
    best_dprime_model_state = None
    history = []

    # --- D-prime early stopping state ---
    best_dprime = -float("inf")
    best_dprime_epoch = 0
    dprime_patience_counter = 0

    # --- Resume from checkpoint ---
    start_epoch = 1
    if resume_state is not None:
        start_epoch = resume_state["epoch"] + 1
        if verbose:
            console(f"  Resuming from epoch {resume_state['epoch']}, starting at {start_epoch}")

        # Restore optimizer
        if "optimizer_state_dict" in resume_state:
            optimizer.load_state_dict(resume_state["optimizer_state_dict"])
            if verbose:
                console("  Restored optimizer state")
        else:
            if verbose:
                console("  No optimizer state in checkpoint, stepping schedulers to correct position")

        # Restore schedulers
        if "warmup_scheduler_state_dict" in resume_state:
            warmup_scheduler.load_state_dict(resume_state["warmup_scheduler_state_dict"])
            cosine_scheduler.load_state_dict(resume_state["cosine_scheduler_state_dict"])
            if use_swa and swa_scheduler is not None and resume_state.get("swa_scheduler_state_dict"):
                swa_scheduler.load_state_dict(resume_state["swa_scheduler_state_dict"])
            if verbose:
                console("  Restored scheduler states")
        else:
            # Legacy checkpoint: step schedulers forward to match epoch
            for ep in range(1, start_epoch):
                in_swa = use_swa and ep >= swa_start_epoch
                if in_swa and swa_scheduler is not None:
                    swa_scheduler.step()
                elif ep <= warmup_epochs:
                    warmup_scheduler.step()
                else:
                    cosine_scheduler.step()
            if verbose:
                current_lr = optimizer.param_groups[0]["lr"]
                console(f"  Stepped schedulers to epoch {start_epoch - 1} (LR={current_lr:.2e})")

        # Restore EMA
        if ema is not None and "ema_shadow" in resume_state and resume_state["ema_shadow"]:
            for name in ema.shadow:
                if name in resume_state["ema_shadow"]:
                    ema.shadow[name].copy_(resume_state["ema_shadow"][name])
            if verbose:
                console("  Restored EMA shadow parameters")

        # Restore SWA model
        if use_swa and swa_model is not None and "swa_model_state_dict" in resume_state:
            swa_model.load_state_dict(resume_state["swa_model_state_dict"])
            if "swa_n_averaged" in resume_state:
                swa_model.n_averaged.fill_(resume_state["swa_n_averaged"])
            if verbose:
                console(f"  Restored SWA model (n_averaged={int(swa_model.n_averaged)})")

        # Restore best tracking
        best_f1 = resume_state.get("best_f1", 0)
        best_dprime = resume_state.get("best_dprime", -float("inf"))
        best_dprime_epoch = resume_state.get("best_dprime_epoch", 0)
        dprime_patience_counter = resume_state.get("dprime_patience_counter", 0)
        if verbose:
            console(f"  Restored tracking: best_f1={100*best_f1:.1f}%, best_dprime={best_dprime:.2f}")
            console()

    for epoch in range(start_epoch, epochs + 1):
        in_swa_phase = use_swa and epoch >= swa_start_epoch

        # Train
        train_loss, train_acc = _train_epoch(
            model,
            train_loader,
            optimizer,
            criterion,
            device,
            epoch,
            epochs,
            verbose,
            max_grad_norm=max_grad_norm,
            ema=ema,
        )

        # Update EMA after each epoch (update was also called per-step in _train_epoch)
        # No additional update needed here — per-step updates in _train_epoch are sufficient

        # Update SWA model if in SWA phase
        if in_swa_phase and swa_model is not None:
            swa_model.update_parameters(model)

        # LR scheduling
        if in_swa_phase and swa_scheduler is not None:
            swa_scheduler.step()
        elif epoch <= warmup_epochs:
            warmup_scheduler.step()
        else:
            cosine_scheduler.step()

        # Evaluate (use EMA weights if available)
        if ema is not None:
            ema.apply(model)

        val_metrics = evaluate_model(model, val_loader, criterion, device)

        if ema is not None:
            ema.restore(model)

        # Record history
        current_lr = optimizer.param_groups[0]["lr"]
        history.append(
            {
                "epoch": epoch,
                "train_loss": train_loss,
                "train_acc": train_acc,
                "lr": current_lr,
                **val_metrics,
            }
        )

        # Compute F1 for model selection (avoids picking epoch 1 with 100% recall / 50% precision)
        recall = val_metrics["recall"]
        precision = val_metrics["precision"]
        f1 = 2 * recall * precision / (recall + precision + 1e-10)

        # Save per-epoch checkpoint so any epoch can be selected later
        if checkpoint_dir is not None:
            ckpt_path = Path(checkpoint_dir)
            ckpt_path.mkdir(parents=True, exist_ok=True)
            # Save EMA weights for inference (backward compat) + raw weights for resume
            if ema is not None:
                ema.apply(model)
            ema_applied_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            if ema is not None:
                ema.restore(model)
            raw_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            ckpt_dict = {
                "epoch": epoch,
                "model_state_dict": ema_applied_state,
                "raw_model_state_dict": raw_state,
                "optimizer_state_dict": optimizer.state_dict(),
                "warmup_scheduler_state_dict": warmup_scheduler.state_dict(),
                "cosine_scheduler_state_dict": cosine_scheduler.state_dict(),
                "swa_scheduler_state_dict": (swa_scheduler.state_dict() if swa_scheduler is not None else None),
                "ema_shadow": ({k: v.cpu().clone() for k, v in ema.shadow.items()} if ema is not None else None),
                "swa_model_state_dict": (swa_model.state_dict() if swa_model is not None else None),
                "swa_n_averaged": (int(swa_model.n_averaged) if swa_model is not None else 0),
                "best_f1": best_f1,
                "best_dprime": best_dprime,
                "best_dprime_epoch": best_dprime_epoch,
                "dprime_patience_counter": dprime_patience_counter,
                "feature_config": get_feature_config(),
                "metrics": {**val_metrics, "f1": f1, "train_loss": train_loss},
            }
            torch.save(ckpt_dict, str(ckpt_path / f"epoch_{epoch:03d}.pt"))

        if verbose:
            console(f"    Train Loss: {train_loss:.4f} | Train Acc: {100*train_acc:.1f}%")
            console(f"    Val Loss: {val_metrics['loss']:.4f} | Val Acc: {100*val_metrics['accuracy']:.1f}%")
            console(f"    Recall: {100*recall:.1f}% | Precision: {100*precision:.1f}% | F1: {100*f1:.1f}%")
            console(
                f"    TP: {val_metrics['true_positives']} | FP: {val_metrics['false_positives']} | "
                f"TN: {val_metrics['true_negatives']} | FN: {val_metrics['false_negatives']}"
            )
            phase = "SWA" if in_swa_phase else ("warmup" if epoch <= warmup_epochs else "cosine")
            console(f"    LR: {current_lr:.2e} ({phase})")
            console()

        # Save best model (based on F1 score to balance recall and precision)
        if f1 > best_f1:
            best_f1 = f1
            # Save the EMA weights as best if EMA is active
            if ema is not None:
                ema.apply(model)
                best_model_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                ema.restore(model)
            else:
                best_model_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            if verbose:
                console(
                    f"    New best F1: {100*best_f1:.1f}% (recall={100*recall:.1f}%, precision={100*precision:.1f}%)"
                )
                console()

        # --- D-prime early stopping on real samples ---
        if eval_real_dir and epoch % eval_real_interval == 0:
            # Apply EMA weights for evaluation if active
            if ema is not None:
                ema.apply(model)

            dprime_result = evaluate_real_samples(model, eval_real_dir, device)

            if ema is not None:
                ema.restore(model)

            current_dprime = dprime_result["d_prime"]

            if verbose:
                console(
                    f"    [Real eval] d-prime: {current_dprime:.2f}"
                    f" | TP mean: {dprime_result['tp_mean']:.3f}"
                    f" | FP max: {dprime_result['fp_max']:.3f}"
                )

            if current_dprime > best_dprime:
                best_dprime = current_dprime
                best_dprime_epoch = epoch
                dprime_patience_counter = 0
                # Track best d-prime model state in memory for final export
                if ema is not None:
                    ema.apply(model)
                best_dprime_model_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                if ema is not None:
                    ema.restore(model)
                # Save best d-prime checkpoint
                if checkpoint_dir is not None:
                    ckpt_path = Path(checkpoint_dir)
                    if ema is not None:
                        ema.apply(model)
                    ema_applied = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                    if ema is not None:
                        ema.restore(model)
                    raw = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                    torch.save(
                        {
                            "epoch": epoch,
                            "model_state_dict": ema_applied,
                            "raw_model_state_dict": raw,
                            "optimizer_state_dict": optimizer.state_dict(),
                            "warmup_scheduler_state_dict": warmup_scheduler.state_dict(),
                            "cosine_scheduler_state_dict": cosine_scheduler.state_dict(),
                            "swa_scheduler_state_dict": (
                                swa_scheduler.state_dict() if swa_scheduler is not None else None
                            ),
                            "ema_shadow": (
                                {k: v.cpu().clone() for k, v in ema.shadow.items()} if ema is not None else None
                            ),
                            "swa_model_state_dict": (swa_model.state_dict() if swa_model is not None else None),
                            "swa_n_averaged": (int(swa_model.n_averaged) if swa_model is not None else 0),
                            "best_f1": best_f1,
                            "best_dprime": best_dprime,
                            "best_dprime_epoch": best_dprime_epoch,
                            "dprime_patience_counter": dprime_patience_counter,
                            "feature_config": get_feature_config(),
                            "metrics": {
                                **val_metrics,
                                "d_prime": current_dprime,
                                "tp_mean": dprime_result["tp_mean"],
                                "fp_max": dprime_result["fp_max"],
                            },
                        },
                        str(ckpt_path / "best_dprime.pt"),
                    )
                if verbose:
                    console(f"    [Real eval] New best d-prime: {current_dprime:.2f}" f" at epoch {epoch}")
            else:
                dprime_patience_counter += 1
                if verbose:
                    console(f"    [Real eval] d-prime declined" f" ({dprime_patience_counter}/{early_stop_patience})")
                if dprime_patience_counter >= early_stop_patience:
                    if verbose:
                        console(
                            f"  Early stopping: d-prime declined for" f" {dprime_patience_counter} consecutive evals"
                        )
                        console(f"  Best: epoch {best_dprime_epoch}" f" with d-prime {best_dprime:.2f}")
                    break

            if verbose:
                console()

    # --- Finalize model weights ---

    if use_swa and swa_model is not None:
        # SWA: update batch norm statistics with averaged model
        if verbose:
            console("  Updating SWA batch norm statistics...")
        update_bn(train_loader, swa_model, device=torch.device(device))
        # Copy SWA weights to model
        model.load_state_dict(swa_model.module.state_dict())
        if verbose:
            console("  SWA model loaded")
    elif best_dprime_model_state is not None:
        # Prefer d-prime-best over F1-best — d-prime is the metric that
        # matters for production wake word detection (separation between
        # positive and negative score distributions).
        model.load_state_dict(best_dprime_model_state)
        if verbose:
            ema_note = " (EMA)" if ema is not None else ""
            console(
                f"  Loaded best d-prime model{ema_note}" f" (d-prime: {best_dprime:.2f}, epoch {best_dprime_epoch})"
            )
    elif best_model_state is not None:
        # Fallback: load best F1 model (when no real-sample eval was run)
        model.load_state_dict(best_model_state)
        if verbose:
            ema_note = " (EMA)" if ema is not None else ""
            console(f"  Loaded best F1 model{ema_note} (F1: {100*best_f1:.1f}%)")
    elif ema is not None:
        # No best model saved but EMA active — apply EMA for final export
        ema.apply(model)
        if verbose:
            console("  Applied EMA weights for final model")

    return {
        "best_recall": best_f1,  # Legacy key name kept for compat, now tracks F1
        "best_f1": best_f1,
        "best_dprime": best_dprime,
        "best_dprime_epoch": best_dprime_epoch,
        "best_model_state": best_dprime_model_state or best_model_state,
        "history": history,
        "final_metrics": evaluate_model(model, val_loader, criterion, device),
    }


def _train_epoch(
    model,
    dataloader,
    optimizer,
    criterion,
    device,
    epoch,
    total_epochs,
    verbose,
    *,
    max_grad_norm: float = 1.0,
    ema: object | None = None,
):
    """Train for one epoch."""
    model.train()
    total_loss = 0
    correct = 0
    total = 0

    start_time = time.time()

    for batch_idx, (data, target) in enumerate(dataloader):
        data, target = data.to(device), target.to(device)

        optimizer.zero_grad()
        output = model(data)
        loss = criterion(output, target)
        loss.backward()

        # Gradient clipping
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=max_grad_norm)

        optimizer.step()

        # Update EMA after each optimizer step
        if ema is not None:
            ema.update()

        total_loss += loss.item()
        pred = (output > 0.5).float()
        correct += (pred == target).sum().item()
        total += target.size(0)

        # Progress every 10 batches
        if verbose and (batch_idx + 1) % 10 == 0:
            elapsed = time.time() - start_time
            batches_per_sec = (batch_idx + 1) / elapsed
            eta = (len(dataloader) - batch_idx - 1) / batches_per_sec
            console(
                f"\r  Epoch {epoch}/{total_epochs} | Batch {batch_idx+1}/{len(dataloader)} | "
                f"Loss: {total_loss/(batch_idx+1):.4f} | Acc: {100*correct/total:.1f}% | "
                f"ETA: {eta:.0f}s",
                end="",
                flush=True,
            )

    if verbose:
        console()  # Newline

    return total_loss / len(dataloader), correct / total


def evaluate_model(model, dataloader, criterion=None, device: str = "cpu") -> dict:
    """
    Evaluate model on a dataset.

    Args:
        model: Model to evaluate
        dataloader: DataLoader with evaluation data
        criterion: Loss function (optional)
        device: Device to evaluate on

    Returns:
        Dictionary with metrics
    """
    if not _TORCH_AVAILABLE:
        raise ImportError("PyTorch required for evaluation")

    if criterion is None:
        criterion = nn.BCELoss()

    model.eval()
    model = model.to(device)

    total_loss = 0
    correct = 0
    total = 0

    true_positives = 0
    false_positives = 0
    true_negatives = 0
    false_negatives = 0

    with torch.no_grad():
        for data, target in dataloader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            loss = criterion(output, target)

            total_loss += loss.item()
            pred = (output > 0.5).float()
            correct += (pred == target).sum().item()
            total += target.size(0)

            # Confusion matrix
            for p, t in zip(pred, target, strict=False):
                if t == 1 and p == 1:
                    true_positives += 1
                elif t == 0 and p == 1:
                    false_positives += 1
                elif t == 0 and p == 0:
                    true_negatives += 1
                else:
                    false_negatives += 1

    recall = true_positives / (true_positives + false_negatives + 1e-10)
    precision = true_positives / (true_positives + false_positives + 1e-10)

    return {
        "loss": total_loss / max(len(dataloader), 1),
        "accuracy": correct / max(total, 1),
        "recall": recall,
        "precision": precision,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "true_negatives": true_negatives,
        "false_negatives": false_negatives,
    }


def export_onnx(model, output_path: Path, device: str = "cpu") -> Path:
    """
    Export model to ONNX format.

    Args:
        model: Trained model
        output_path: Path to save ONNX model
        device: Device model is on

    Returns:
        Path to saved ONNX model
    """
    if not _TORCH_AVAILABLE:
        raise ImportError("PyTorch required for ONNX export")

    import numpy as np

    from violawake.audio import compute_features, pad_or_trim
    from violawake.config import CLIP_SAMPLES

    model.eval()

    # Compute feature shape dynamically from compute_features to match training
    dummy_audio = np.zeros(CLIP_SAMPLES, dtype=np.float32)
    dummy_features = compute_features(dummy_audio)
    n_mels, time_frames = dummy_features.shape
    dummy_input = torch.randn(1, n_mels, time_frames).to(device)

    torch.onnx.export(
        model,
        (dummy_input,),
        str(output_path),
        export_params=True,
        opset_version=11,
        do_constant_folding=True,
        input_names=["mel_spectrogram"],
        output_names=["score"],
        dynamic_axes={
            "mel_spectrogram": {0: "batch_size", 2: "time_frames"},
            "score": {0: "batch_size"},
        },
    )

    # Verify
    try:
        import onnx

        onnx_model = onnx.load(str(output_path))
        onnx.checker.check_model(onnx_model)
    except ImportError:
        pass  # onnx not installed, skip verification

    return output_path
