"""`scripts/bench_compute_ceiling.py` の純関数（形状導出・比率算出・判定ラベル付与）に対する単体テスト．

実際の計測（`measure_linear`／`measure_layer_ns`）はタイミング依存のため対象外とする
（決定的な入力を与えられる純関数のみを検証する）．クラスタ・SSH 接続は行わない．
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from bench_compute_ceiling import (  # noqa: E402 -- sys.path 設定後に import する必要がある
    GAIN_RATIO_THRESHOLD,
    LABEL_AMBIGUOUS,
    LABEL_GAIN,
    LABEL_NO_GAIN,
    NO_GAIN_RATIO_THRESHOLD,
    _build_linear_shapes_from_config_fallback,
    classify_ratio,
    compute_ratios,
)


# ====================================================================
# _build_linear_shapes_from_config_fallback
# ====================================================================


def test_build_linear_shapes_from_config_fallback_derives_shapes_from_config_json(tmp_path: Path) -> None:
    """config.json の overrides から q/k/v/o/mlp の形状が hidden_size・head_dim を反映して導出される．"""

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "model": {
                    "overrides": {
                        "hidden_size": 5376,
                        "num_attention_heads": 32,
                        "num_key_value_heads": 16,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = _build_linear_shapes_from_config_fallback(config_path, reason="test: 実構築をスキップ")

    shapes_by_name = {shape.name: shape for shape in result.shapes}
    assert shapes_by_name["self_attn.q_proj"].in_features == 5376
    assert shapes_by_name["self_attn.q_proj"].out_features == 32 * 256
    assert shapes_by_name["self_attn.k_proj"].out_features == 16 * 256
    assert shapes_by_name["self_attn.o_proj"].in_features == 32 * 256
    assert shapes_by_name["self_attn.o_proj"].out_features == 5376
    assert result.source == "config_fallback"
    # 仮定値（intermediate_size）を使った旨が warnings に明記されていること
    assert any("intermediate_size" in warning for warning in result.warnings)


def test_build_linear_shapes_from_config_fallback_reports_missing_config_file(tmp_path: Path) -> None:
    """config.json が存在しない場合でも例外を投げず，全フォールバック定数使用の旨を warnings に積む．"""

    missing_path = tmp_path / "does_not_exist.json"

    result = _build_linear_shapes_from_config_fallback(missing_path, reason="test: 実構築をスキップ")

    assert len(result.shapes) == 7  # q,k,v,o,gate,up,down
    assert any("読み込みに失敗" in warning for warning in result.warnings)


# ====================================================================
# compute_ratios
# ====================================================================


def test_compute_ratios_computes_per_token_ratio_against_gemv_baseline() -> None:
    """ratio_K = (GEMM(K)時間/K) / GEMV時間 が正しく算出される．"""

    gemv_layer_ns = 1000.0
    gemm_layer_ns_by_k = {2: 1600.0, 4: 2800.0, 8: 4800.0}

    ratios = compute_ratios(gemv_layer_ns, gemm_layer_ns_by_k)

    assert ratios[2] == pytest.approx(1600.0 / 2 / 1000.0)
    assert ratios[4] == pytest.approx(2800.0 / 4 / 1000.0)
    assert ratios[8] == pytest.approx(4800.0 / 8 / 1000.0)


def test_compute_ratios_rejects_non_positive_gemv_baseline() -> None:
    """GEMV 基準時間が 0 以下のときは，誤った比率を黙って返さず例外を送出する．"""

    with pytest.raises(ValueError):
        compute_ratios(0.0, {2: 100.0})


# ====================================================================
# classify_ratio
# ====================================================================


def test_classify_ratio_returns_gain_label_when_monotonic_decreasing_below_threshold() -> None:
    """K の昇順で単調減少し，最大 K の ratio が GAIN_RATIO_THRESHOLD 以下なら「利得あり」．"""

    ratios = {2: 0.95, 4: 0.90, 8: GAIN_RATIO_THRESHOLD - 0.01}

    assert classify_ratio(ratios) == LABEL_GAIN


def test_classify_ratio_returns_no_gain_label_when_all_ratios_at_or_above_threshold() -> None:
    """全 K の ratio が NO_GAIN_RATIO_THRESHOLD 以上なら「利得なし」．"""

    ratios = {2: NO_GAIN_RATIO_THRESHOLD, 4: 1.0, 8: 1.02}

    assert classify_ratio(ratios) == LABEL_NO_GAIN


def test_classify_ratio_returns_ambiguous_label_for_mixed_ratios() -> None:
    """利得ありの条件（単調減少かつ閾値以下）も利得なしの条件（全 K が閾値以上）も満たさない場合は「曖昧」．"""

    ratios = {2: 0.99, 4: 0.90, 8: 0.90}  # 単調減少しない（かつ全 K が NO_GAIN 閾値以上でもない）

    assert classify_ratio(ratios) == LABEL_AMBIGUOUS


def test_classify_ratio_rejects_empty_ratios() -> None:
    """空の ratios を渡した場合は，誤ったラベルを黙って返さず例外を送出する．"""

    with pytest.raises(ValueError):
        classify_ratio({})
