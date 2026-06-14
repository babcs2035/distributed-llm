"""
split_model.py - Hugging Face モデルをパイプライン並列用に分割

config.json からモデル名を読み込み、Hugging Face からダウンロードしたモデルを
パイプライン並列推論用に各トランスフォーマーレイヤーごとに分割する。

使用法:
  uv run python tools/split_model.py                    # 全レイヤーを分割
  uv run python tools/split_model.py --output-dir DIR   # 出力ディレクトリ指定
  uv run python tools/split_model.py --format pt        # PyTorch 形式で出力
  uv run python tools/split_model.py --dry-run          # 分割計画のみ表示
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from transformers import AutoConfig, AutoModelForCausalLM
except ImportError:
    print("ERROR ! transformers | pip install transformers safetensors", file=sys.stderr)
    sys.exit(1)

import torch

try:
    from safetensors.torch import save_file as safetensors_save_file
except ImportError:
    safetensors_save_file = None

from huggingface_hub import snapshot_download

from common import log


def load_config(config_path: str = "config.json") -> dict[str, Any]:
    """config.json を読み込む"""

    path = Path(config_path)
    if not path.exists():
        log("FAIL", f"config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text())


def get_model_specs(model_name: str, overrides: dict[str, Any] | None, *, dry_run: bool = False) -> dict[str, Any]:
    """
    Hugging Face からモデル仕様を取得する。

    overrides で指定された値があればそれを優先し、
    なければ AutoConfig から自動取得する。
    dry_run=True の場合は HF にアクセスせずにデフォルト値を使用する。
    """

    num_hidden_layers = overrides.get("num_hidden_layers")

    if num_hidden_layers is not None:
        log("INFO", f"num_hidden_layers from config: {num_hidden_layers}")
    elif dry_run:
        log("WARN", f"Cannot access HF gated repo in dry-run mode. Using default num_hidden_layers=80.")
        num_hidden_layers = 80
    else:
        log("INFO", f"Loading model specs from Hugging Face: {model_name}")
        config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
        num_hidden_layers = _resolve_num_hidden_layers(config, model_name)

    specs = {
        "num_hidden_layers": int(num_hidden_layers),
    }

    log("INFO", f"num_hidden_layers: {specs['num_hidden_layers']}")
    return specs


def _resolve_num_hidden_layers(config: Any, model_name: str) -> int:
    """
    config オブジェクトから num_hidden_layers を堅牢に取得する。

    異なるアーキテクチャで異なる属性名を使う可能性があるため、
    複数の候補を試す。また、`text_config` のようなネストされた
    config 内の値も再帰的に探索する。
    """

    candidates = ["num_hidden_layers", "num_layers", "n_layers"]

    # 1. トップレベルの属性を検索
    for attr in candidates:
        if hasattr(config, attr):
            value = getattr(config, attr)
            log("INFO", f"Found num_hidden_layers via '{attr}': {value}")
            return int(value)

    # 2. config.__dict__ から layer 関連のキーを検索
    for key in config.__dict__:
        if "layer" in key.lower() and "hidden" in key.lower():
            value = config.__dict__[key]
            if isinstance(value, (int, float)):
                log("WARN", f"Inferred num_hidden_layers from '{key}': {value}")
                return int(value)

    # 3. ネストされた config (text_config / vision_config / audio_config) を再帰的に検索
    for key in config.__dict__:
        nested = getattr(config, key, None)
        if nested is not None and hasattr(nested, "__dict__"):
            try:
                result = _resolve_num_hidden_layers(nested, model_name)
                log("INFO", f"Found num_hidden_layers in nested '{key}': {result}")
                return result
            except AttributeError:
                continue

    raise AttributeError(
        f"Cannot find num_hidden_layers in config for {model_name}. "
        f"Available keys: {list(config.__dict__.keys())}"
    )


def detect_layer_prefix(weights: dict[str, Any]) -> str:
    """重み辞書からレイヤープレフィックスを自動検出する。

    候補として 'model.layers.', 'model.language_model.layers.',
    'model.vision_tower.encoder.layers.' 等をスキャンし、
    最も多くのレイヤー番号を含むプレフィックスを返す。
    """

    candidates = [
        "model.language_model.layers.",
        "model.vision_tower.encoder.layers.",
        "model.layers.",
        "layers.",
    ]
    best_prefix = ""
    best_count = 0
    for prefix in candidates:
        indices: set[int] = set()
        for key in weights:
            if key.startswith(prefix):
                try:
                    parts = key[len(prefix):].split(".")
                    idx = int(parts[0])
                    indices.add(idx)
                except (ValueError, IndexError):
                    continue
        if len(indices) > best_count:
            best_count = len(indices)
            best_prefix = prefix
    log("INFO", f"Detected layer prefix: '{best_prefix}' ({best_count} layers)")
    return best_prefix


def detect_embed_key(weights: dict[str, Any]) -> str | None:
    """重み辞書から embed_tokens のキーを自動検出する"""

    candidates = [
        "model.language_model.embed_tokens.weight",
        "model.embed_tokens.weight",
        "model.tok_embeddings.weight",
        "embed_tokens.weight",
    ]
    for key in candidates:
        if key in weights:
            return key
    return None


def get_layer_weight_keys(weights: dict[str, Any], layer_prefix: str) -> set[int]:
    """重み辞書からレイヤー番号のセットを抽出する"""

    layers: set[int] = set()
    for key in weights:
        if key.startswith(layer_prefix):
            try:
                parts = key[len(layer_prefix):].split(".")
                idx = int(parts[0])
                layers.add(idx)
            except (ValueError, IndexError):
                continue
    return layers


def _download_model(model_name: str) -> str:
    """
    Hugging Face からモデルをダウンロードし、ローカルキャッシュのパスを返す。

    既にキャッシュに存在する場合は再ダウンロードしない。
    """

    log("INFO", f"Downloading/loading model: {model_name}")
    cache_dir = snapshot_download(
        model_name,
        ignore_patterns=["*.pt", "*.bin"],
    )
    log("OK", f"Model ready (cache): {cache_dir}")
    return cache_dir


def _is_layer_complete(output_dir: Path, i: int, fmt: str) -> bool:
    """レイヤー番号 i の分割ファイルが既に存在するか判定する"""

    fname = f"layer_{i}.{'safetensors' if fmt == 'safetensors' else 'pt'}"
    return (output_dir / fname).exists()


def split_model(
    model_name: str,
    output_dir: Path,
    weight_format: str,
    specs: dict[str, Any],
    *,
    dry_run: bool = False,
    layer_prefix: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    モデルをレイヤーごとに分割して保存する。

    既存のレイヤーファイルはスキップし、中断から再開可能。

    Args:
        model_name: Hugging Face のモデル名
        output_dir: 出力ディレクトリ
        weight_format: 出力形式（'safetensors' または 'pt'）
        specs: モデル仕様（num_hidden_layers）
        dry_run: 実際の分割を行わず計画のみを表示するかどうか
        layer_prefix: レイヤープレフィックス（None の場合は自動検出）

    Returns:
        layer_info: 各レイヤーのファイル情報辞書
    """

    num_layers = specs["num_hidden_layers"]

    if dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        ext = 'safetensors' if weight_format == 'safetensors' else 'pt'
        log("DRY-RUN", "Split plan:")
        if num_layers <= 80:
            for i in range(num_layers):
                log("DRY-RUN", f"layer_{i}.{ext}")
        else:
            log("DRY-RUN", f"layer_0.{ext} ... layer_{num_layers - 1}.{ext}")
        log("DRY-RUN", f"embed_tokens.{ext}")
        log("DRY-RUN", f"lm_head.{ext}")
        return {}

    # 出力ディレクトリを事前に作成
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = 'safetensors' if weight_format == 'safetensors' else 'pt'

    # 既存の分割情報をロード
    info_path = output_dir / "split_info.json"
    existing_info: dict[str, Any] = {}
    if info_path.exists():
        existing_info = json.loads(info_path.read_text())

    # 全レイヤーが既に完了している場合は何もしない
    all_complete = True
    for i in range(num_layers):
        if not _is_layer_complete(output_dir, i, weight_format) or f"layer_{i}" not in existing_info:
            all_complete = False
            break

    if all_complete and "embed_tokens" in existing_info and "lm_head" in existing_info:
        log("INFO", f"All {num_layers} layers + embed + lm_head already exist. Nothing to do.")
        info_path.write_text(json.dumps(existing_info, indent=2, ensure_ascii=False))
        return existing_info

    # Hugging Face からモデルをダウンロード（キャッシュ已有れば再利用）
    local_path = _download_model(model_name)

    model = AutoModelForCausalLM.from_pretrained(
        local_path,
        trust_remote_code=True,
        torch_dtype="auto",
        device_map="cpu",
    )

    all_weights = model.state_dict()

    # レイヤープレフィックスの自動検出
    if layer_prefix is None:
        layer_prefix = detect_layer_prefix(all_weights)

    layer_indices = sorted(get_layer_weight_keys(all_weights, layer_prefix))

    log("INFO", f"Detected layers: {len(layer_indices)} (expected: {num_layers})")

    if layer_indices:
        max_idx = max(layer_indices)
        log("INFO", f"Layer range: {min(layer_indices)}-{max_idx}")

    # 既存のレイヤーをスキップ
    skipped = 0
    layer_info: dict[str, dict[str, Any]] = {}

    for i in range(num_layers):
        if _is_layer_complete(output_dir, i, weight_format) and f"layer_{i}" in existing_info:
            skipped += 1
            layer_info[f"layer_{i}"] = existing_info[f"layer_{i}"]
            continue

        layer_keys = {k: v for k, v in all_weights.items() if k.startswith(f"{layer_prefix}{i}.")}
        if not layer_keys:
            log("WARN", f"No weights found for layer {i}")
            continue

        fname = f"layer_{i}.{ext}"
        fpath = output_dir / fname

        if weight_format == "safetensors" and safetensors_save_file is not None:
            safetensors_save_file(layer_keys, str(fpath))
        else:
            torch.save(layer_keys, str(fpath))

        total_params = sum(k.numel() for k in layer_keys.values())
        log("OK", f"Layer {i:2d}: {fname} ({total_params:,} params)")

        layer_info[f"layer_{i}"] = {
            "file": fname,
            "params": int(total_params),
            "keys": list(layer_keys.keys()),
        }

    if skipped > 0:
        log("INFO", f"Skipped existing layers: {skipped}/{num_layers}")

    # 特殊レイヤー
    embed_key = detect_embed_key(all_weights)
    if embed_key is not None:
        fname = f"embed_tokens.{ext}"
        fpath = output_dir / fname
        if not fpath.exists() or "embed_tokens" not in existing_info:
            if weight_format == "safetensors" and safetensors_save_file is not None:
                safetensors_save_file({embed_key: all_weights[embed_key]}, str(fpath))
            else:
                torch.save({embed_key: all_weights[embed_key]}, str(fpath))
            log("OK", f"embed_tokens: {fname}")
        else:
            log("INFO", f"Skip existing: {fname}")
        layer_info["embed_tokens"] = {"file": fname, "keys": [embed_key]}

    lm_head_key = "lm_head.weight"
    if lm_head_key in all_weights:
        fname = f"lm_head.{ext}"
        fpath = output_dir / fname
        if not fpath.exists() or "lm_head" not in existing_info:
            if weight_format == "safetensors" and safetensors_save_file is not None:
                safetensors_save_file({lm_head_key: all_weights[lm_head_key]}, str(fpath))
            else:
                torch.save({lm_head_key: all_weights[lm_head_key]}, str(fpath))
            log("OK", f"lm_head: {fname}")
        else:
            log("INFO", f"Skip existing: {fname}")
        layer_info["lm_head"] = {"file": fname, "keys": [lm_head_key]}

    # final norm (Gemma-4: model.language_model.norm.weight)
    norm_key = "model.language_model.norm.weight"
    if norm_key in all_weights:
        fname = f"norm.{ext}"
        fpath = output_dir / fname
        if not fpath.exists() or "norm" not in existing_info:
            if weight_format == "safetensors" and safetensors_save_file is not None:
                safetensors_save_file({norm_key: all_weights[norm_key]}, str(fpath))
            else:
                torch.save({norm_key: all_weights[norm_key]}, str(fpath))
            log("OK", f"norm: {fname}")
        else:
            log("INFO", f"Skip existing: {fname}")
        layer_info["norm"] = {"file": fname, "keys": [norm_key]}

    # 分割情報を保存
    info_path.write_text(json.dumps(layer_info, indent=2, ensure_ascii=False))
    log("INFO", f"Split info saved: {info_path}")

    return layer_info


def main() -> None:
    """Hugging Face モデルをダウンロード・分割する"""

    parser = argparse.ArgumentParser(description="Split HF model for pipeline parallelism")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Config file path (default: config.json)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="models/splits",
        help="Output directory (default: model_splits)",
    )
    parser.add_argument(
        "--format",
        choices=["safetensors", "pt"],
        default=None,
        help="Output format (config.json format takes precedence)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show split plan only",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    model_name = config["model"]["name"]
    weight_format = args.format or config["model"].get("format", "safetensors")
    overrides = config["model"].get("overrides", {})
    output_dir = Path(args.output_dir)

    log("STEP", "=" * 60)
    log("INFO", "Hugging Face Model Split Tool")
    log("STEP", "=" * 60)

    # モデル仕様取得
    specs = get_model_specs(model_name, overrides, dry_run=args.dry_run)

    # モデル分割
    split_model(
        model_name=model_name,
        output_dir=output_dir,
        weight_format=weight_format,
        specs=specs,
        dry_run=args.dry_run,
    )

    log("INFO", "Split complete.")
    log("INFO", f"Output: {output_dir.resolve()}")
    log("INFO", f"Format: {weight_format}")


if __name__ == "__main__":
    main()
