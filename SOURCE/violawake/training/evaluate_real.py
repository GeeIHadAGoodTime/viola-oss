from pathlib import Path

import numpy as np
import torch
import torchaudio
from sklearn.metrics import auc, roc_curve


def score_file(model, wav_path, device):
    audio, sr = torchaudio.load(wav_path)

    if audio.shape[0] > 1:
        audio = audio.mean(dim=0)

    # log-mel spectrogram (matches Conv2d expectations)
    transform = torchaudio.transforms.MelSpectrogram(sample_rate=sr, n_mels=64)

    spec = transform(audio)
    spec = torch.log1p(spec)

    # 4D shape: (batch=1, channel=1, height, width)
    spec = spec.unsqueeze(0).unsqueeze(0)

    with torch.no_grad():
        output = model(spec.to(device))

    return torch.sigmoid(output).item()


def compute_dprime(pos, neg):
    pos = np.array(pos)
    neg = np.array(neg)

    return (pos.mean() - neg.mean()) / np.sqrt(0.5 * (pos.var() + neg.var()))


def compute_eer(fpr, tpr):
    fnr = 1 - tpr
    idx = np.nanargmin(np.absolute(fnr - fpr))
    eer = (fpr[idx] + fnr[idx]) / 2
    return float(eer), int(idx)


def evaluate_real_samples(model, dataset_dir, device="cpu"):
    dataset_dir = Path(dataset_dir)

    pos_dir = dataset_dir / "positives"
    neg_dir = dataset_dir / "negatives"

    model.eval()
    model.to(device)

    pos_scores = []
    neg_scores = []

    print("Scoring positive samples...")

    for wav in pos_dir.rglob("*.wav"):
        pos_scores.append(score_file(model, wav, device))

    print("Scoring negative samples...")

    for wav in neg_dir.rglob("*.wav"):
        neg_scores.append(score_file(model, wav, device))

    pos_scores = np.array(pos_scores)
    neg_scores = np.array(neg_scores)

    if len(pos_scores) == 0:
        raise RuntimeError("No positives found under eval_real/positives")

    if len(neg_scores) == 0:
        raise RuntimeError("No negatives found under eval_real/negatives")

    # ROC
    scores = np.concatenate([pos_scores, neg_scores])
    labels = np.concatenate([np.ones(len(pos_scores)), np.zeros(len(neg_scores))])

    fpr, tpr, thresholds = roc_curve(labels, scores)
    roc_auc = auc(fpr, tpr)

    # EER
    fnr = 1 - tpr
    idx = np.nanargmin(np.abs(fnr - fpr))
    eer = (fpr[idx] + fnr[idx]) / 2

    # optimal threshold
    optimal_idx = np.argmax(tpr - fpr)
    optimal_threshold = thresholds[optimal_idx]

    far = fpr[optimal_idx]
    frr = 1 - tpr[optimal_idx]

    d_prime = compute_dprime(pos_scores, neg_scores)

    return {
        "d_prime": float(d_prime),
        "auc": float(roc_auc),
        "eer": float(eer),
        "optimal_threshold": float(optimal_threshold),
        "far": float(far),
        "frr": float(frr),
        "pos_mean": float(pos_scores.mean()),
        "neg_mean": float(neg_scores.mean()),
    }
