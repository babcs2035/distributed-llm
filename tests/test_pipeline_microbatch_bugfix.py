"""`pipeline_inference.py` の `_process_microbatch` / `_run_microbatch_bench` に対する回帰テスト．

research-cycle Iteration 7（実装フェーズ差し戻し）で発見・修正した 2 件のブロッキングバグの
再発防止を目的とする（詳細は journal.md Iteration 7 `### 実験 (Iter7)` / `### 実装 (Iter7, 差し戻し後)` 参照）．

- バグ A: `_process_microbatch` が `layer(hidden_state)` を `position_ids` なしで呼び出し，
  `_build_transformer_layer` の実シグネチャ `forward(hidden_state, position_ids, is_first=True)`
  （`position_ids` に既定値なし）に対し `TypeError` で fatal crash していた．
- バグ B: bench (`_run_microbatch_bench`) が使う KV キャッシュの write_pos がマイクロバッチ・
  ステップ間で共有されたままリセットされず，`max_gen_tokens` を超えて破損・例外に至っていた．

実クラスタ・実モデル読み込み（`FullyOptimizedPipelineNode.__init__` が要求する分散プロセス
グループ初期化・safetensors 読み込み）は行わない．`object.__new__` で最小限のインスタンスを
組み立て，対象メソッドが参照する属性のみを直接設定した「単一ノード」（`prev_rank`/`next_rank`
がともに `None` で `dist.recv`/`dist.send` を経由しない）で検証する．
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import torch

import pipeline_inference as pi

_HEAD_DIM = 4  # テスト用ダミー KV キャッシュのダミー head_dim（テストの意味に無関係な定数）


def _build_single_node(
    *, num_micro_batches: int, seq_len: int, hidden_size: int, max_gen_tokens: int,
    recorded_calls: list[dict[str, object]],
) -> pi.FullyOptimizedPipelineNode:
    """通信をバイパスする単一ノード（prev_rank=next_rank=None）を最小構成で組み立てる．

    `fake_layer` は実 `_build_transformer_layer` の forward と同じ 2 点（`position_ids` に
    既定値が無いシグネチャ，KV キャッシュへの `write_pos:write_pos+sl` 書き込み）だけを模した
    簡易実装であり，実際の attention/RoPE 計算は行わない（回帰テストの対象はテンソル演算の
    正しさではなく `_process_microbatch`/`_run_microbatch_bench` の呼び出し契約のため）．
    """

    node = object.__new__(pi.FullyOptimizedPipelineNode)
    node.config = SimpleNamespace(
        rank=0, world_size=1, prev_rank=None, next_rank=None,
        num_micro_batches=num_micro_batches, seq_len=seq_len,
    )
    node.recv_buffers = [torch.zeros(1, seq_len, hidden_size) for _ in range(num_micro_batches)]
    node.send_buffers = [torch.zeros(1, seq_len, hidden_size) for _ in range(num_micro_batches)]
    node.kv_cache = {
        0: (
            torch.zeros(1, 1, max_gen_tokens, _HEAD_DIM),
            torch.zeros(1, 1, max_gen_tokens, _HEAD_DIM),
        ),
    }
    node._kv_cache_write_pos_ref = {0: 0}
    # research-cycle Iter9: `__init__` が設定する bench 計測フラグ（既定 None）．
    # `object.__new__` は `__init__` を経由しないため，`_process_microbatch` が参照する前に
    # ここで明示的に既定値へ揃える（journal.md Iteration 9 参照）．
    node._bench_timing = None

    def fake_layer(hidden_state: torch.Tensor, position_ids: torch.Tensor, is_first: bool = True) -> torch.Tensor:
        recorded_calls.append(
            {
                "position_ids": position_ids.clone(),
                "is_first": is_first,
                "write_pos_before": node._kv_cache_write_pos_ref[0],
            }
        )
        key_cache, value_cache = node.kv_cache[0]
        write_pos = node._kv_cache_write_pos_ref[0]
        sl = hidden_state.shape[1]
        dummy_kv = torch.ones(1, 1, sl, _HEAD_DIM)
        # 実 forward (:880-881 付近) と同じ書き込みパターン．write_pos が max_gen_tokens を
        # 部分的に超えていると，左辺スライス長が sl 未満になり RuntimeError（shape 不一致）になる．
        key_cache[:, :, write_pos:write_pos + sl, :] = dummy_kv
        value_cache[:, :, write_pos:write_pos + sl, :] = dummy_kv
        node._kv_cache_write_pos_ref[0] += sl
        return hidden_state

    node.my_layers = [fake_layer]
    return node


# ====================================================================
# バグ A: position_ids 欠落によるクラッシュの再発防止
# ====================================================================


def test_process_microbatch_passes_position_ids_without_typeerror() -> None:
    """`position_ids` に既定値の無い layer.forward に対し，TypeError を起こさず呼び出せる．"""

    calls: list[dict[str, object]] = []
    node = _build_single_node(
        num_micro_batches=1, seq_len=3, hidden_size=8, max_gen_tokens=64, recorded_calls=calls,
    )

    node._process_microbatch(mb=0, step_count=0, step_start_time=time.monotonic(), pbar=None)

    assert len(calls) == 1
    assert calls[0]["is_first"] is True
    assert torch.equal(calls[0]["position_ids"], torch.arange(0, 3).unsqueeze(0))


def test_process_microbatch_position_ids_offset_by_microbatch_index() -> None:
    """mb 番目の呼び出しの position_ids は `[mb*seq_len, (mb+1)*seq_len)` となり，
    write_pos の累積書き込み開始位置（`mb*seq_len`）と一致する．"""

    calls: list[dict[str, object]] = []
    node = _build_single_node(
        num_micro_batches=3, seq_len=2, hidden_size=8, max_gen_tokens=64, recorded_calls=calls,
    )

    for mb in range(3):
        node._process_microbatch(mb=mb, step_count=0, step_start_time=time.monotonic(), pbar=None)

    assert torch.equal(calls[0]["position_ids"], torch.tensor([[0, 1]]))
    assert torch.equal(calls[1]["position_ids"], torch.tensor([[2, 3]]))
    assert torch.equal(calls[2]["position_ids"], torch.tensor([[4, 5]]))
    # position_ids の開始値が，実際に fake_layer が観測した write_pos と一致していること
    assert [c["write_pos_before"] for c in calls] == [0, 2, 4]
    assert calls[0]["is_first"] is True
    assert calls[1]["is_first"] is False
    assert calls[2]["is_first"] is False


# ====================================================================
# バグ B: bench 中の KV キャッシュ write_pos オーバーフローの再発防止
# ====================================================================


def test_process_microbatch_without_reset_overflows_kv_cache_write_pos() -> None:
    """回帰確認（対処前の挙動の再現）: write_pos をステップ間でリセットせずに mb 呼び出しを
    重ねると，`max_gen_tokens` を超えたところで KV キャッシュへの書き込みが shape 不一致で
    例外になる（バグ B の実体）．"""

    calls: list[dict[str, object]] = []
    max_gen_tokens = 4
    node = _build_single_node(
        num_micro_batches=1, seq_len=2, hidden_size=8, max_gen_tokens=max_gen_tokens,
        recorded_calls=calls,
    )

    with pytest.raises(RuntimeError):
        # reset を挟まずに 4 ステップ連続実行 -> write_pos は 0,2,4,6 と単調増加し，
        # 3 回目 (write_pos=4) で max_gen_tokens=4 を跨いで例外になる．
        for step in range(4):
            node._process_microbatch(
                mb=0, step_count=step, step_start_time=time.monotonic(), pbar=None,
            )


def test_reset_kv_cache_for_bench_zeros_cache_and_write_pos() -> None:
    """`_reset_kv_cache_for_bench` は KV キャッシュ本体・write_pos の両方をゼロへ戻す．"""

    calls: list[dict[str, object]] = []
    node = _build_single_node(
        num_micro_batches=1, seq_len=2, hidden_size=8, max_gen_tokens=8, recorded_calls=calls,
    )
    node._process_microbatch(mb=0, step_count=0, step_start_time=time.monotonic(), pbar=None)
    assert node._kv_cache_write_pos_ref[0] != 0

    node._reset_kv_cache_for_bench()

    assert node._kv_cache_write_pos_ref[0] == 0
    key_cache, value_cache = node.kv_cache[0]
    assert torch.count_nonzero(key_cache).item() == 0
    assert torch.count_nonzero(value_cache).item() == 0


def test_run_microbatch_bench_does_not_overflow_kv_cache_across_many_steps() -> None:
    """`_run_microbatch_bench` は各ステップ冒頭で write_pos をリセットするため，
    `repeats × (warmup+measure)` の総ステップ数が `max_gen_tokens` を大幅に超えても
    （本番設定 `MICROBATCH_BENCH_STEPS=100` 相当の構造を小スケールで再現）例外にならない．
    """

    calls: list[dict[str, object]] = []
    num_micro_batches = 2
    seq_len = 2
    # 1 ステップ内の write_pos 最大到達値 = num_micro_batches * seq_len = max_gen_tokens ちょうど
    # （reset が無ければ 2 ステップ目で確実にオーバーフローする境界値を選ぶ）．
    max_gen_tokens = num_micro_batches * seq_len
    node = _build_single_node(
        num_micro_batches=num_micro_batches, seq_len=seq_len, hidden_size=8,
        max_gen_tokens=max_gen_tokens, recorded_calls=calls,
    )

    # repeats * (warmup + measure) = 4 * (3 + 5) = 32 ステップ．reset が無ければ
    # write_pos は 32 * (num_micro_batches*seq_len) = 128 まで単調増加し max_gen_tokens=4 を
    # 即座に超えて例外になるはずの規模だが，reset があるため最後まで例外なく完走する．
    step_count = node._run_microbatch_bench(
        is_last_node=True, warmup_steps=3, measure_steps=5, repeats=4,
    )

    assert step_count == 4 * (3 + 5)
    # 各呼び出し直前の write_pos は必ず `mb * seq_len`（reset 直後からの累積）に収まっている
    assert all(c["write_pos_before"] == (i % num_micro_batches) * seq_len for i, c in enumerate(calls))
