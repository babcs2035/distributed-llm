"""
Gemma4 decoder layer - safetensors weight loader.

Loads weights from safetensors and builds a Gemma4TextDecoderLayer
for inference.
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
    Load weights from a safetensors file and build a Gemma4TextDecoderLayer.

    Args:
        layer_idx: Layer index in the model (0-59)
        weight_file: Path to the safetensors file

    Returns:
        Gemma4TextDecoderLayer instance with loaded weights
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
