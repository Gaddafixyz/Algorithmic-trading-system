"""
Machine-learning signal filter package.

Provides a scikit-learn / XGBoost classifier that is trained on historical
SMC confluence features and then used at inference time to filter low-quality
signals before they reach the execution layer.

Public API::

    from src.machine_learning import SignalFilter, SignalFilterTrainer, extract_features

Quick-start::

    from src.machine_learning import SignalFilter, SignalFilterTrainer

    # Train
    trainer = SignalFilterTrainer()
    model   = trainer.train(feature_rows, labels)

    # Persist / load
    trainer.save(model, path="models/signal_filter.pkl")
    model = trainer.load(path="models/signal_filter.pkl")

    # Inference
    sf = SignalFilter(model)
    keep = sf.should_trade(signal, analysis)   # True / False
"""

from __future__ import annotations

from src.machine_learning.features import extract_features
from src.machine_learning.signal_filter import SignalFilter
from src.machine_learning.trainer import SignalFilterTrainer

__all__ = [
    "SignalFilter",
    "SignalFilterTrainer",
    "extract_features",
]
