"""
Gemma4 デコーダーレイヤー - safetensors 重み読み込み用

safetensors から重みを読み込み、transformers の Gemma4TextDecoderLayer を
構築して推論に使用する。
"""

from __future__ import annotations

import torch
from safetensors.torch import load_file
from transformers import AutoConfig
from transformers.models.gemma4.modeling_gemma4 import Gemma4TextDecoderLayer


def build_gemma4_layer(
    layer_idx: int,
    weight_file: str,
) -> Gemma4TextDecoderLayer:
    """
    safetensors ファイルから重みを読み込み、Gemma4TextDecoderLayer を構築する。

    Args:
        layer_idx: モデル内のレイヤーインデックス (0-59)
        weight_file: safetensors ファイルへのパス

    Returns:
        重み付き Gemma4TextDecoderLayer インスタンス
    """
    config = AutoConfig.from_pretrained(
        "google/gemma-4-31B-it", trust_remote_code=True,
    )
    text_config = config.text_config if hasattr(config, "text_config") else config

    layer = Gemma4TextDecoderLayer(text_config, layer_idx)
    layer.eval()

    weights = load_file(weight_file)
    prefix = f"model.language_model.layers.{layer_idx}."
    state_dict = {}
    for k, v in weights.items():
        if k.startswith(prefix):
            state_dict[k[len(prefix):]] = v

    layer.load_state_dict(state_dict, strict=False)
    return layer
