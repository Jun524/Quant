"""Chronological behavior-distillation diagnostics for the AOA archive.

The objective is not to claim access to a private discretionary strategy.  It
tests how much of the observed entry timing, direction, and profitable-trade
selection can be explained by causal hourly market features.  Every feature is
formed from a completed BitMEX bar before the corresponding execution.
"""

from __future__ import annotations

import bisect
import math
from typing import Iterable

import numpy as np
import pandas as pd


FEATURE_COLUMNS = (
    "return_1h",
    "return_4h",
    "return_12h",
    "return_24h",
    "return_72h",
    "return_168h",
    "volatility_6h",
    "volatility_24h",
    "volatility_72h",
    "distance_ema24",
    "distance_ema72",
    "distance_ema200",
    "ema24_slope_24h",
    "ema200_slope_24h",
    "range_position_24h",
    "range_width_24h",
    "volume_ratio_24h",
    "candle_body",
    "candle_range",
    "hour_sin",
    "hour_cos",
)


def build_causal_features(bars: list[dict]) -> pd.DataFrame:
    """Build hourly features without using a future or incomplete bar."""
    frame = pd.DataFrame(bars).copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_localize(None)
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    hourly_return = frame["close"].pct_change()
    for hours in (1, 4, 12, 24, 72, 168):
        frame[f"return_{hours}h"] = frame["close"].pct_change(hours)
    for hours in (6, 24, 72):
        frame[f"volatility_{hours}h"] = hourly_return.rolling(hours, min_periods=hours).std()
    for hours in (24, 72, 200):
        ema = frame["close"].ewm(span=hours, adjust=False, min_periods=hours).mean()
        frame[f"ema_{hours}"] = ema
        frame[f"distance_ema{hours}"] = frame["close"] / ema - 1.0
    frame["ema24_slope_24h"] = frame["ema_24"].pct_change(24)
    frame["ema200_slope_24h"] = frame["ema_200"].pct_change(24)

    prior_high = frame["high"].rolling(24, min_periods=24).max()
    prior_low = frame["low"].rolling(24, min_periods=24).min()
    width = prior_high - prior_low
    frame["range_position_24h"] = (frame["close"] - prior_low) / width.replace(0, np.nan)
    frame["range_width_24h"] = width / frame["close"]
    prior_volume_median = frame["volume"].rolling(24, min_periods=24).median().shift(1)
    frame["volume_ratio_24h"] = frame["volume"] / prior_volume_median.replace(0, np.nan)
    frame["candle_body"] = (frame["close"] - frame["open"]) / frame["open"]
    frame["candle_range"] = (frame["high"] - frame["low"]) / frame["open"]
    hour_angle = 2.0 * math.pi * frame["timestamp"].dt.hour / 24.0
    frame["hour_sin"] = np.sin(hour_angle)
    frame["hour_cos"] = np.cos(hour_angle)
    return frame.replace([np.inf, -np.inf], np.nan)


def attach_episode_labels(features: pd.DataFrame, episodes: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return flat-hour action candidates and one row per mapped episode."""
    timestamps = features["timestamp"].tolist()
    action_by_index: dict[int, list[dict]] = {}
    active = np.zeros(len(features), dtype=bool)

    for episode in episodes:
        entry_index = bisect.bisect_left(timestamps, episode["start"]) - 1
        if entry_index < 0 or entry_index >= len(features):
            continue
        action_by_index.setdefault(entry_index, []).append(episode)
        active_start = bisect.bisect_left(timestamps, episode["start"])
        active_end = bisect.bisect_left(timestamps, episode["end"])
        if active_end > active_start:
            active[active_start:active_end] = True

    complete = features[list(FEATURE_COLUMNS)].notna().all(axis=1)
    candidate_rows: list[dict] = []
    episode_rows: list[dict] = []
    for index, row in features.iterrows():
        mapped = action_by_index.get(index, [])
        if not complete.iloc[index]:
            continue
        # Multiple independent flat-to-flat episodes beginning inside the same
        # hour are retained for episode quality, but ambiguous direction labels
        # are excluded from the direction task.
        sides = {item["side"] for item in mapped}
        if not active[index] or mapped:
            item = {column: float(row[column]) for column in FEATURE_COLUMNS}
            item.update(
                {
                    "timestamp": row["timestamp"],
                    "year": int(row["timestamp"].year),
                    "action": int(bool(mapped)),
                    "direction": 1 if sides == {"long"} else (0 if sides == {"short"} else np.nan),
                }
            )
            candidate_rows.append(item)
        for episode in mapped:
            item = {column: float(row[column]) for column in FEATURE_COLUMNS}
            item.update(
                {
                    "timestamp": row["timestamp"],
                    "year": int(row["timestamp"].year),
                    "direction": int(episode["side"] == "long"),
                    "won": int(episode["net_pnl_xbt"] > 0),
                    "net_pnl_xbt": float(episode["net_pnl_xbt"]),
                    "net_return_proxy": float(episode.get("net_return_proxy") or 0.0),
                    "duration_minutes": float(episode["duration_minutes"]),
                    "maker_share": float(
                        (episode["maker_entry_qty"] + episode["maker_exit_qty"])
                        / (
                            episode["maker_entry_qty"]
                            + episode["taker_entry_qty"]
                            + episode["maker_exit_qty"]
                            + episode["taker_exit_qty"]
                        )
                    ),
                }
            )
            episode_rows.append(item)
    return pd.DataFrame(candidate_rows), pd.DataFrame(episode_rows)


def _profit_factor(values: Iterable[float]) -> float | None:
    values = list(values)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return gains / losses if losses else None


def _top_coefficients(pipeline, *, count: int = 8) -> list[dict]:
    coefficients = pipeline.named_steps["model"].coef_.ravel()
    ranked = sorted(zip(FEATURE_COLUMNS, coefficients), key=lambda item: abs(item[1]), reverse=True)
    return [{"feature": feature, "coefficient": float(value)} for feature, value in ranked[:count]]


def _linear_model_spec(pipeline, *, threshold: float) -> dict:
    scaler = pipeline.named_steps["scale"]
    model = pipeline.named_steps["model"]
    return {
        "features": list(FEATURE_COLUMNS),
        "mean": [float(value) for value in scaler.mean_],
        "scale": [float(value) for value in scaler.scale_],
        "coefficient": [float(value) for value in model.coef_.ravel()],
        "intercept": float(model.intercept_.ravel()[0]),
        "threshold": float(threshold),
    }


def fit_chronological_behavior_models(candidates: pd.DataFrame, episode_rows: pd.DataFrame) -> dict:
    """Fit 2018-19, calibrate only thresholds on 2020, test once on 2021."""
    try:
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.metrics import (
            accuracy_score,
            average_precision_score,
            balanced_accuracy_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError("scikit-learn is required for AOA behavior distillation") from exc

    train = candidates[candidates["year"] <= 2019]
    calibration = candidates[candidates["year"] == 2020]
    test = candidates[candidates["year"] == 2021]
    action_model = Pipeline(
        [
            ("scale", StandardScaler()),
            ("model", LogisticRegression(class_weight="balanced", max_iter=2000, random_state=7)),
        ]
    )
    action_model.fit(train[list(FEATURE_COLUMNS)], train["action"])
    calibration_probability = action_model.predict_proba(calibration[list(FEATURE_COLUMNS)])[:, 1]
    calibration_rate = float(calibration["action"].mean())
    action_threshold = float(np.quantile(calibration_probability, 1.0 - calibration_rate))
    test_probability = action_model.predict_proba(test[list(FEATURE_COLUMNS)])[:, 1]
    test_prediction = test_probability >= action_threshold
    base_rate = float(test["action"].mean())
    precision = float(precision_score(test["action"], test_prediction, zero_division=0))

    direction_train = episode_rows[episode_rows["year"] <= 2019]
    direction_calibration = episode_rows[episode_rows["year"] == 2020]
    direction_test = episode_rows[episode_rows["year"] == 2021]
    direction_model = Pipeline(
        [
            ("scale", StandardScaler()),
            ("model", LogisticRegression(class_weight="balanced", max_iter=2000, random_state=11)),
        ]
    )
    direction_model.fit(direction_train[list(FEATURE_COLUMNS)], direction_train["direction"])
    direction_calibration_probability = direction_model.predict_proba(
        direction_calibration[list(FEATURE_COLUMNS)]
    )[:, 1]
    direction_confidence_threshold = float(
        np.quantile(np.abs(direction_calibration_probability - 0.5), 0.70)
    )
    direction_probability = direction_model.predict_proba(direction_test[list(FEATURE_COLUMNS)])[:, 1]
    direction_prediction = direction_probability >= 0.5
    confident_direction = np.abs(direction_probability - 0.5) >= direction_confidence_threshold
    momentum_prediction = direction_test["return_24h"] > 0

    # A linear return model asks whether the teacher's losing trades can be
    # filtered using only pre-entry state.  The selection fraction is fixed at
    # 30% on calibration probabilities, never optimized on 2021 outcomes.
    quality_train = episode_rows[episode_rows["year"] <= 2019]
    quality_calibration = episode_rows[episode_rows["year"] == 2020]
    quality_test = episode_rows[episode_rows["year"] == 2021]
    quality_model = Pipeline([("scale", StandardScaler()), ("model", Ridge(alpha=10.0))])
    quality_model.fit(quality_train[list(FEATURE_COLUMNS)], quality_train["net_return_proxy"])
    calibration_score = quality_model.predict(quality_calibration[list(FEATURE_COLUMNS)])
    quality_threshold = float(np.quantile(calibration_score, 0.70))
    quality_test_score = quality_model.predict(quality_test[list(FEATURE_COLUMNS)])
    selected = quality_test[quality_test_score >= quality_threshold]

    return {
        "schema_version": 1,
        "split": {
            "train": "2018-2019",
            "threshold_calibration": "2020",
            "locked_test": "2021",
            "features": list(FEATURE_COLUMNS),
        },
        "sample_counts": {
            "candidate_train": len(train),
            "candidate_calibration": len(calibration),
            "candidate_test": len(test),
            "episode_train": len(direction_train),
            "episode_calibration": len(quality_calibration),
            "episode_test": len(direction_test),
        },
        "action_timing": {
            "test_base_rate": base_rate,
            "calibrated_threshold": action_threshold,
            "test_average_precision": float(average_precision_score(test["action"], test_probability)),
            "test_roc_auc": float(roc_auc_score(test["action"], test_probability)),
            "test_precision": precision,
            "test_recall": float(recall_score(test["action"], test_prediction, zero_division=0)),
            "precision_lift_over_base": precision / base_rate if base_rate else None,
            "top_coefficients": _top_coefficients(action_model),
        },
        "direction": {
            "test_accuracy": float(accuracy_score(direction_test["direction"], direction_prediction)),
            "test_balanced_accuracy": float(
                balanced_accuracy_score(direction_test["direction"], direction_prediction)
            ),
            "test_roc_auc": float(roc_auc_score(direction_test["direction"], direction_probability)),
            "24h_momentum_baseline_accuracy": float(
                accuracy_score(direction_test["direction"], momentum_prediction)
            ),
            "confidence_rule": "top 30% absolute probability distance from 0.5, threshold fixed on 2020",
            "confidence_threshold": direction_confidence_threshold,
            "confident_test_episodes": int(confident_direction.sum()),
            "confident_test_selection_rate": float(confident_direction.mean()),
            "confident_test_accuracy": float(
                accuracy_score(
                    direction_test.loc[confident_direction, "direction"],
                    direction_prediction[confident_direction],
                )
            ) if confident_direction.any() else None,
            "top_coefficients": _top_coefficients(direction_model),
        },
        "quality_filter": {
            "fixed_selection_rule": "top 30% predicted return proxy by 2020 calibration threshold",
            "calibrated_threshold": quality_threshold,
            "test_all": {
                "episodes": len(quality_test),
                "win_rate": float(quality_test["won"].mean()),
                "mean_return_proxy": float(quality_test["net_return_proxy"].mean()),
                "net_pnl_xbt": float(quality_test["net_pnl_xbt"].sum()),
                "profit_factor": _profit_factor(quality_test["net_pnl_xbt"]),
            },
            "test_selected": {
                "episodes": len(selected),
                "selection_rate": len(selected) / len(quality_test) if len(quality_test) else 0.0,
                "win_rate": float(selected["won"].mean()) if len(selected) else None,
                "mean_return_proxy": float(selected["net_return_proxy"].mean()) if len(selected) else None,
                "net_pnl_xbt": float(selected["net_pnl_xbt"].sum()),
                "profit_factor": _profit_factor(selected["net_pnl_xbt"]),
            },
            "top_coefficients": _top_coefficients(quality_model),
        },
        "deployable_model_spec": {
            "basis": "entry timing and direction only; quality filter excluded because locked-test PF deteriorated",
            "action": _linear_model_spec(action_model, threshold=action_threshold),
            "direction": {
                **_linear_model_spec(direction_model, threshold=0.5),
                "minimum_confidence": direction_confidence_threshold,
            },
        },
        "limitations": [
            "The model observes public fills, not cancelled orders or private intent.",
            "Hourly labels cannot reconstruct sub-hour discretionary timing.",
            "The return proxy is not account return because collateral and leverage are unknown.",
            "The 2021 split is held out only within the source archive and is not evidence for Binance futures.",
        ],
    }
