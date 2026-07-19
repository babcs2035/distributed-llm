"""Iteration 5 (SL1) の local マイクロベンチ: seq_len=1 GEMV と seq_len=K GEMM の 1 トークンあたり計算時間を比較する．

実機クラスタ・relay プロトコル・`pipeline_inference.py` には一切接続・変更しない．単一プロセス内で
random-init の `Gemma4TextDecoderLayer`（重みファイル非ロード）から実際の線形層形状を取得し，
`torch.set_num_threads(NUM_THREADS)` / `float32` という `pipeline_inference.py` と同じ計算条件のもとで
GEMV（seq_len=1）と GEMM（seq_len=K, K in K_VALUES）を計測する．

背景・判定ルールの詳細は `.claude/research/journal.md` の `## Iteration 5` `### 計画 (Iter5)` を参照．

使い方:
    unset VIRTUAL_ENV && uv run python scripts/bench_compute_ceiling.py

結果は `results/bench_compute_ceiling.jsonl` へ 1 レコード追記される（追記のみ，既存レコードは変更しない）．
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

import torch
from torch import nn

# ====================================================================
# 定数（マジックナンバー回避．値の根拠は journal.md Iter5 計画を参照）
# ====================================================================

WARMUP_ITERS = 50
MEASURE_ITERS = 200
K_VALUES: tuple[int, ...] = (2, 4, 8)
NUM_THREADS = 4
NUM_INTEROP_THREADS = 1

COMPUTE_DTYPE = torch.float32  # pipeline_inference.py:38 と同一設定
GEMMA4_MODEL_NAME = "google/gemma-4-31B-it"

# config.json に intermediate_size が無い場合のフォールバック（実測値 21504 = 5376*4 に一致することを確認済み）
HIDDEN_SIZE_FALLBACK = 5376
HEAD_DIM_FALLBACK = 256
NUM_ATTENTION_HEADS_FALLBACK = 32
NUM_KEY_VALUE_HEADS_FALLBACK = 16
INTERMEDIATE_SIZE_FALLBACK_MULTIPLIER = 4

# 判定ルール（journal.md Iter5 計画 §3 の判定ルールに準拠）
GAIN_RATIO_THRESHOLD = 0.85  # ratio_8 <= これ かつ単調減少 -> 利得あり
NO_GAIN_RATIO_THRESHOLD = 0.95  # 全 K で ratio >= これ -> 利得なし（1.0 からの有意差 ±0.05 の下限に相当）
LABEL_GAIN = "利得あり"
LABEL_NO_GAIN = "利得なし"
LABEL_AMBIGUOUS = "曖昧"

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_JSON_PATH = _REPO_ROOT / "config.json"
RESULTS_JSONL_PATH = _REPO_ROOT / "results" / "bench_compute_ceiling.jsonl"

_NS_TO_MS = 1e-6  # ナノ秒 -> ミリ秒


# ====================================================================
# データ型
# ====================================================================


@dataclass(frozen=True)
class LinearShape:
    """1つの nn.Linear の形状（GEMM 対象）．"""

    name: str
    in_features: int
    out_features: int


@dataclass(frozen=True)
class LinearShapeResult:
    """`build_linear_shapes` の戻り値．実構築が失敗した場合はフォールバック値である旨を warnings に積む．"""

    shapes: list[LinearShape]
    source: str  # "real_gemma4_layer" または "config_fallback"
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LinearMeasurement:
    """1つの nn.Linear・1つの seq_len についての計測結果（中央値・最小値，単位はナノ秒）．"""

    median_ns: float
    min_ns: float


# ====================================================================
# (b) 形状取得
# ====================================================================


def build_linear_shapes(config_path: Path | None = None) -> LinearShapeResult:
    """実 `Gemma4TextDecoderLayer` を random-init で構築し，`nn.Linear` の形状一覧を取得する．

    重みファイルはロードしない（形状のみが必要なため）．transformers の Gemma4 実装や
    HuggingFace Hub への config 取得に失敗した場合は，`config.json` とフォールバック定数から
    形状を導出する（`_build_linear_shapes_from_config_fallback` を参照，仮定値は warnings に明記する）．
    """

    try:
        from transformers import AutoConfig
        from transformers.models.gemma4.modeling_gemma4 import Gemma4TextDecoderLayer

        config = AutoConfig.from_pretrained(GEMMA4_MODEL_NAME, trust_remote_code=True)
        text_config = config.text_config if hasattr(config, "text_config") else config

        layer = Gemma4TextDecoderLayer(text_config, layer_idx=0)
        layer.eval()

        shapes = [
            LinearShape(name=name, in_features=module.in_features, out_features=module.out_features)
            for name, module in layer.named_modules()
            if isinstance(module, nn.Linear)
        ]
        if not shapes:
            raise RuntimeError("Gemma4TextDecoderLayer から nn.Linear が1つも見つからなかった")

        return LinearShapeResult(shapes=shapes, source="real_gemma4_layer", warnings=[])
    except Exception as exc:  # noqa: BLE001 -- フォールバックへ切り替えるため意図的に広く捕捉し，理由をログする
        return _build_linear_shapes_from_config_fallback(config_path, reason=str(exc))


def _build_linear_shapes_from_config_fallback(config_path: Path | None, reason: str) -> LinearShapeResult:
    """`config.json` の `model.overrides` とフォールバック定数から線形層形状を導出する（仮定値は明示する）．"""

    warnings = [f"実 Gemma4TextDecoderLayer の構築に失敗したため config.json + フォールバック定数から形状を導出する: {reason}"]

    path = config_path or DEFAULT_CONFIG_JSON_PATH
    hidden_size = HIDDEN_SIZE_FALLBACK
    num_attention_heads = NUM_ATTENTION_HEADS_FALLBACK
    num_key_value_heads = NUM_KEY_VALUE_HEADS_FALLBACK
    try:
        with path.open(encoding="utf-8") as config_file:
            config_dict = json.load(config_file)
        overrides = config_dict.get("model", {}).get("overrides", {})
        hidden_size = overrides.get("hidden_size", HIDDEN_SIZE_FALLBACK)
        num_attention_heads = overrides.get("num_attention_heads", NUM_ATTENTION_HEADS_FALLBACK)
        num_key_value_heads = overrides.get("num_key_value_heads", NUM_KEY_VALUE_HEADS_FALLBACK)
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"{path} の読み込みに失敗したため，全フォールバック定数を使用する: {exc}")

    head_dim = HEAD_DIM_FALLBACK
    q_out_features = num_attention_heads * head_dim
    kv_out_features = num_key_value_heads * head_dim
    intermediate_size = hidden_size * INTERMEDIATE_SIZE_FALLBACK_MULTIPLIER
    warnings.append(
        "intermediate_size は config.json に存在しないため仮定値 "
        f"hidden_size*{INTERMEDIATE_SIZE_FALLBACK_MULTIPLIER}={intermediate_size} を使用する"
    )

    shapes = [
        LinearShape("self_attn.q_proj", hidden_size, q_out_features),
        LinearShape("self_attn.k_proj", hidden_size, kv_out_features),
        LinearShape("self_attn.v_proj", hidden_size, kv_out_features),
        LinearShape("self_attn.o_proj", q_out_features, hidden_size),
        LinearShape("mlp.gate_proj", hidden_size, intermediate_size),
        LinearShape("mlp.up_proj", hidden_size, intermediate_size),
        LinearShape("mlp.down_proj", intermediate_size, hidden_size),
    ]
    return LinearShapeResult(shapes=shapes, source="config_fallback", warnings=warnings)


# ====================================================================
# (c) 計測
# ====================================================================


def measure_linear(
    shape: LinearShape,
    seq_len: int,
    warmup_iters: int = WARMUP_ITERS,
    measure_iters: int = MEASURE_ITERS,
) -> LinearMeasurement:
    """1つの `LinearShape` について，(batch=1, seq_len, in_features) の入力での実行時間を計測する．

    `warmup_iters` 回のウォームアップ後，`measure_iters` 回を `time.perf_counter_ns()` で 1 回ずつ計測し，
    中央値（scheduler jitter に頑健な主指標）と最小値（参考値）を返す．
    """

    linear = nn.Linear(shape.in_features, shape.out_features, bias=False, dtype=COMPUTE_DTYPE)
    linear.eval()
    x = torch.randn(1, seq_len, shape.in_features, dtype=COMPUTE_DTYPE)

    samples_ns: list[int] = []
    with torch.no_grad():
        for _ in range(warmup_iters):
            linear(x)
        for _ in range(measure_iters):
            start_ns = time.perf_counter_ns()
            linear(x)
            samples_ns.append(time.perf_counter_ns() - start_ns)

    return LinearMeasurement(median_ns=float(statistics.median(samples_ns)), min_ns=float(min(samples_ns)))


def measure_layer_ns(
    shapes: list[LinearShape],
    seq_len: int,
    warmup_iters: int = WARMUP_ITERS,
    measure_iters: int = MEASURE_ITERS,
) -> tuple[float, float]:
    """1層分（全 `LinearShape` の総和）の中央値・最小値時間（ナノ秒）を返す．"""

    total_median_ns = 0.0
    total_min_ns = 0.0
    for shape in shapes:
        measurement = measure_linear(shape, seq_len, warmup_iters, measure_iters)
        total_median_ns += measurement.median_ns
        total_min_ns += measurement.min_ns
    return total_median_ns, total_min_ns


# ====================================================================
# (d) 指標
# ====================================================================


def compute_ratios(gemv_layer_ns: float, gemm_layer_ns_by_k: Mapping[int, float]) -> dict[int, float]:
    """`ratio_K = (GEMM(K) の1層時間 / K) / (GEMV(seq_len=1) の1層時間)` を K 別に計算する（純関数）．

    `ratio_K` が 1.0 未満なら，K トークンをまとめて計算した方が 1 トークンあたりの
    compute 時間が短い（=演算強度の向上による利得が実在する）ことを意味する．
    """

    if gemv_layer_ns <= 0:
        raise ValueError(f"gemv_layer_ns は正の値である必要がある: {gemv_layer_ns}")

    ratios: dict[int, float] = {}
    for k, gemm_layer_ns in gemm_layer_ns_by_k.items():
        if k <= 0:
            raise ValueError(f"K は正の整数である必要がある: {k}")
        per_token_ns = gemm_layer_ns / k
        ratios[k] = per_token_ns / gemv_layer_ns
    return ratios


def classify_ratio(ratios: Mapping[int, float]) -> str:
    """`ratio_K` の辞書から利得判定ラベルを付与する（判定ルールは journal.md Iter5 計画 §3 に準拠）．

    (i) K の昇順で単調減少し，かつ最大 K の ratio が `GAIN_RATIO_THRESHOLD` 以下 -> 利得あり．
    (ii) 全 K の ratio が `NO_GAIN_RATIO_THRESHOLD` 以上 -> 利得なし．
    (iii) それ以外 -> 曖昧．
    """

    if not ratios:
        raise ValueError("ratios は空であってはならない")

    sorted_ks = sorted(ratios.keys())
    ratios_in_k_order = [ratios[k] for k in sorted_ks]
    is_monotonic_decreasing = all(
        ratios_in_k_order[i] >= ratios_in_k_order[i + 1] for i in range(len(ratios_in_k_order) - 1)
    )
    max_k_ratio = ratios[sorted_ks[-1]]

    if max_k_ratio <= GAIN_RATIO_THRESHOLD and is_monotonic_decreasing:
        return LABEL_GAIN
    if all(ratio >= NO_GAIN_RATIO_THRESHOLD for ratio in ratios.values()):
        return LABEL_NO_GAIN
    return LABEL_AMBIGUOUS


# ====================================================================
# (e) 出力
# ====================================================================


def _print_report(
    shape_result: LinearShapeResult,
    gemv_layer_ns: float,
    gemm_layer_ns_by_k: Mapping[int, float],
    ratios: Mapping[int, float],
    label: str,
) -> None:
    """人間可読テーブルを stdout へ出力する．"""

    print(f"形状ソース: {shape_result.source}")
    for warning in shape_result.warnings:
        print(f"  [WARN] {warning}")
    print("線形層形状:")
    for shape in shape_result.shapes:
        print(f"  {shape.name}: ({shape.in_features} -> {shape.out_features})")

    print(f"\nGEMV (seq_len=1) 1層あたり中央値: {gemv_layer_ns * _NS_TO_MS:.4f} ms")
    print("K別 GEMM 1トークンあたり中央値 / ratio:")
    for k in sorted(gemm_layer_ns_by_k.keys()):
        per_token_ms = (gemm_layer_ns_by_k[k] / k) * _NS_TO_MS
        print(f"  K={k}: per_token={per_token_ms:.4f} ms, ratio={ratios[k]:.4f}")

    print(f"\n判定: {label}")


def _build_record(
    shape_result: LinearShapeResult,
    gemv_layer_ns: float,
    gemm_layer_ns_by_k: Mapping[int, float],
    ratios: Mapping[int, float],
    label: str,
) -> dict[str, object]:
    """results/bench_compute_ceiling.jsonl へ追記する 1 レコードを組み立てる．"""

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "num_threads": NUM_THREADS,
        "num_interop_threads": NUM_INTEROP_THREADS,
        "dtype": str(COMPUTE_DTYPE),
        "torch_version": torch.__version__,
        "cpu": platform.processor(),
        "os_cpu_count": os.cpu_count(),
        "warmup_iters": WARMUP_ITERS,
        "measure_iters": MEASURE_ITERS,
        "k_values": list(K_VALUES),
        "shape_source": shape_result.source,
        "shape_warnings": shape_result.warnings,
        "linear_shapes": [asdict(shape) for shape in shape_result.shapes],
        "gemv_layer_median_ms": gemv_layer_ns * _NS_TO_MS,
        "gemm_layer_median_ms_by_k": {str(k): v * _NS_TO_MS for k, v in gemm_layer_ns_by_k.items()},
        "ratio_by_k": {str(k): v for k, v in ratios.items()},
        "classification": label,
    }


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    """レコードを JSONL ファイルへ 1 行追記する（親ディレクトリが無ければ作成，末尾改行付き）．"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json.dumps(record, ensure_ascii=False))
        jsonl_file.write("\n")


# ====================================================================
# エントリポイント
# ====================================================================


def main() -> None:
    """GEMV vs GEMM(K) の local マイクロベンチを実行し，結果を stdout と JSONL へ出力する．"""

    torch.set_num_threads(NUM_THREADS)
    torch.set_num_interop_threads(NUM_INTEROP_THREADS)

    shape_result = build_linear_shapes()

    gemv_layer_median_ns, _gemv_layer_min_ns = measure_layer_ns(shape_result.shapes, seq_len=1)

    gemm_layer_median_ns_by_k: dict[int, float] = {}
    for k in K_VALUES:
        median_ns, _min_ns = measure_layer_ns(shape_result.shapes, seq_len=k)
        gemm_layer_median_ns_by_k[k] = median_ns

    ratios = compute_ratios(gemv_layer_median_ns, gemm_layer_median_ns_by_k)
    label = classify_ratio(ratios)

    _print_report(shape_result, gemv_layer_median_ns, gemm_layer_median_ns_by_k, ratios, label)

    record = _build_record(shape_result, gemv_layer_median_ns, gemm_layer_median_ns_by_k, ratios, label)
    append_jsonl(RESULTS_JSONL_PATH, record)
    print(f"\nresults へ 1 レコード追記した: {RESULTS_JSONL_PATH}")


if __name__ == "__main__":
    main()
