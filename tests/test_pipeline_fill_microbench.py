"""`scripts/pipeline_fill_microbench.py` の純粋ロジック（FF 計算・pipeline 構造・レコード組み立て）に
対する単体テスト．

実際の `torch.distributed`（Gloo backend）通信・`torch.multiprocessing.spawn` によるマルチプロセス
起動はタイミング・OS リソース依存のため対象外とする（`bench_compute_ceiling.py` の
`measure_linear`/`measure_layer_ns` と同じ扱い）．`run_blocking_stage`/`run_async_stage` は
`Channel` Protocol にのみ依存する設計のため，本テストでは実通信を行わない `FakeChannel` に
差し替えて，呼び出し回数・順序（同期タイミング）を検証する．クラスタ・SSH 接続は一切行わない．
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import torch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pipeline_fill_microbench import (  # noqa: E402 -- sys.path 設定後に import する必要がある
    PROXY_MATMUL,
    PROXY_SLEEP,
    RECORD_TYPE,
    SCHEMA_VERSION,
    append_jsonl,
    build_pipeline_fill_microbench_record,
    compute_fill_factor,
    make_compute_fn,
    run_async_stage,
    run_blocking_stage,
)


# ====================================================================
# フェイク通信チャネル（Channel Protocol の実装．実通信を伴わない）
# ====================================================================


class _FakeHandle:
    """`Handle` Protocol のフェイク実装．`.wait()` 呼び出しをトレースへ記録する．"""

    def __init__(self, trace: list[str], label: str) -> None:
        self._trace = trace
        self._label = label

    def wait(self) -> None:
        self._trace.append(self._label)


class _FakeChannel:
    """`Channel` Protocol のフェイク実装．全呼び出しを `trace` へ出現順に記録する（実通信なし）．"""

    def __init__(self) -> None:
        self.trace: list[str] = []

    def recv(self, tensor: torch.Tensor, src: int) -> None:
        self.trace.append(f"recv:{src}")

    def send(self, tensor: torch.Tensor, dst: int) -> None:
        self.trace.append(f"send:{dst}")

    def irecv(self, tensor: torch.Tensor, src: int) -> _FakeHandle:
        self.trace.append(f"irecv:{src}")
        return _FakeHandle(self.trace, f"wait_recv:{src}")

    def isend(self, tensor: torch.Tensor, dst: int) -> _FakeHandle:
        self.trace.append(f"isend:{dst}")
        return _FakeHandle(self.trace, f"wait_send:{dst}")


def _make_recv_bufs(num_microbatches: int) -> list[torch.Tensor]:
    """小さな (1, 1, 4) 形状のダミーテンソルを `num_microbatches` 個用意する．"""

    return [torch.zeros(1, 1, 4) for _ in range(num_microbatches)]


def _identity_compute_fn(trace: list[str]) -> "Callable[[torch.Tensor], torch.Tensor]":  # type: ignore[name-defined]
    """呼び出し順を `trace` に "compute" として記録し，入力をそのまま返す compute_fn．"""

    def _compute(hidden_state: torch.Tensor) -> torch.Tensor:
        trace.append("compute")
        return hidden_state

    return _compute


# ====================================================================
# compute_fill_factor
# ====================================================================


def test_compute_fill_factor_returns_one_when_measured_time_equals_ideal_pipelined_time() -> None:
    """`measured_total_time = (M+N-1)*t_stage`（完全 pipelined）のとき FF=1.0 を返す．"""

    ff = compute_fill_factor(num_microbatches=32, num_stages=16, t_stage_s=0.006, measured_total_time_s=(32 + 16 - 1) * 0.006)

    assert ff == pytest.approx(1.0)


def test_compute_fill_factor_approaches_one_over_n_for_sequential_time_with_large_m() -> None:
    """完全 sequential（`measured_total_time = M*N*t_stage`）かつ M≫N のとき FF≈1/N に近づく．"""

    num_stages = 16
    num_microbatches = 1000  # M >> N
    t_stage_s = 0.006
    sequential_time_s = num_microbatches * num_stages * t_stage_s

    ff = compute_fill_factor(
        num_microbatches=num_microbatches, num_stages=num_stages, t_stage_s=t_stage_s, measured_total_time_s=sequential_time_s,
    )

    assert ff == pytest.approx(1.0 / num_stages, rel=0.05)


def test_compute_fill_factor_smaller_for_sequential_than_pipelined_at_same_m_n() -> None:
    """同一 (M, N, t_stage) で sequential 時間の方が pipelined 時間より長いため FF は小さくなる．"""

    num_microbatches, num_stages, t_stage_s = 32, 16, 0.006
    pipelined_time_s = (num_microbatches + num_stages - 1) * t_stage_s
    sequential_time_s = num_microbatches * num_stages * t_stage_s

    ff_pipelined = compute_fill_factor(
        num_microbatches=num_microbatches, num_stages=num_stages, t_stage_s=t_stage_s, measured_total_time_s=pipelined_time_s,
    )
    ff_sequential = compute_fill_factor(
        num_microbatches=num_microbatches, num_stages=num_stages, t_stage_s=t_stage_s, measured_total_time_s=sequential_time_s,
    )

    assert ff_pipelined == pytest.approx(1.0)
    assert ff_sequential < ff_pipelined


@pytest.mark.parametrize(
    "num_microbatches, num_stages, t_stage_s, measured_total_time_s",
    [
        (0, 16, 0.006, 1.0),
        (32, 0, 0.006, 1.0),
        (32, 16, 0.0, 1.0),
        (32, 16, -0.006, 1.0),
        (32, 16, 0.006, 0.0),
        (32, 16, 0.006, -1.0),
    ],
)
def test_compute_fill_factor_raises_value_error_for_non_positive_inputs(
    num_microbatches: int, num_stages: int, t_stage_s: float, measured_total_time_s: float,
) -> None:
    """M・N・t_stage・measured_total_time のいずれかが 0 以下なら `ValueError` を送出する．"""

    with pytest.raises(ValueError):
        compute_fill_factor(
            num_microbatches=num_microbatches,
            num_stages=num_stages,
            t_stage_s=t_stage_s,
            measured_total_time_s=measured_total_time_s,
        )


# ====================================================================
# run_blocking_stage（呼び出し回数・順序）
# ====================================================================


def test_run_blocking_stage_middle_rank_alternates_recv_and_send_strictly_in_order() -> None:
    """中間 rank は mb ごとに recv→compute→send を完了してから次の mb へ進む（先行発行なし）．"""

    trace: list[str] = []
    channel = _FakeChannel()
    num_microbatches = 3

    run_blocking_stage(
        prev_rank=0,
        next_rank=2,
        num_microbatches=num_microbatches,
        channel=channel,
        recv_bufs=_make_recv_bufs(num_microbatches),
        compute_fn=_identity_compute_fn(trace),
    )

    # channel と compute の呼び出しは 1 つのトレースに時系列でマージされていないため，
    # それぞれの出現順序を個別に検証する．
    assert channel.trace == ["recv:0", "send:2", "recv:0", "send:2", "recv:0", "send:2"]
    assert trace == ["compute", "compute", "compute"]


def test_run_blocking_stage_source_rank_skips_recv_and_only_sends() -> None:
    """source（`prev_rank=None`）は recv を行わず，全 mb で send のみ発行する．"""

    channel = _FakeChannel()
    num_microbatches = 4

    run_blocking_stage(
        prev_rank=None,
        next_rank=1,
        num_microbatches=num_microbatches,
        channel=channel,
        recv_bufs=_make_recv_bufs(num_microbatches),
        compute_fn=lambda x: x,
    )

    assert channel.trace == ["send:1"] * num_microbatches


def test_run_blocking_stage_sink_rank_skips_send_and_only_recvs() -> None:
    """sink（`next_rank=None`）は send を行わず，全 mb で recv のみ発行する．"""

    channel = _FakeChannel()
    num_microbatches = 4

    run_blocking_stage(
        prev_rank=3,
        next_rank=None,
        num_microbatches=num_microbatches,
        channel=channel,
        recv_bufs=_make_recv_bufs(num_microbatches),
        compute_fn=lambda x: x,
    )

    assert channel.trace == ["recv:3"] * num_microbatches


# ====================================================================
# run_async_stage（呼び出し回数・先行発行・wait の遅延タイミング）
# ====================================================================


def test_run_async_stage_middle_rank_issues_next_irecv_before_compute_and_defers_send_wait() -> None:
    """mb+1 の irecv を compute 前に先行発行し，isend の wait はループ終了後まで遅延する．"""

    trace: list[str] = []
    channel = _FakeChannel()
    num_microbatches = 3

    run_async_stage(
        prev_rank=0,
        next_rank=2,
        num_microbatches=num_microbatches,
        channel=channel,
        recv_bufs=_make_recv_bufs(num_microbatches),
        compute_fn=_identity_compute_fn(trace),
    )

    # 期待する順序:
    #   irecv(mb=0) を事前発行 -> [wait_recv(0), irecv(1), isend(0)]（compute は trace 側で挟まる）
    #   -> [wait_recv(1), irecv(2), isend(1)] -> [wait_recv(2), isend(2)]
    #   -> 最後にまとめて wait_send(0), wait_send(1), wait_send(2)
    assert channel.trace == [
        "irecv:0",
        "wait_recv:0", "irecv:0", "isend:2",
        "wait_recv:0", "irecv:0", "isend:2",
        "wait_recv:0", "isend:2",
        "wait_send:2", "wait_send:2", "wait_send:2",
    ]
    assert trace == ["compute", "compute", "compute"]


def test_run_async_stage_next_irecv_is_issued_strictly_before_compute_call() -> None:
    """mb+1 の irecv 発行が，同じ mb の compute 呼び出しより前に行われることを直接検証する．"""

    channel = _FakeChannel()
    combined_trace: list[str] = []

    def _compute_fn(hidden_state: torch.Tensor) -> torch.Tensor:
        combined_trace.append(("compute", len(channel.trace)))
        return hidden_state

    num_microbatches = 2
    run_async_stage(
        prev_rank=0,
        next_rank=2,
        num_microbatches=num_microbatches,
        channel=channel,
        recv_bufs=_make_recv_bufs(num_microbatches),
        compute_fn=_compute_fn,
    )

    # mb=0 の compute が呼ばれた時点で，channel.trace には既に irecv(mb=1) 分（2 要素目）まで積まれている．
    first_compute_channel_len = combined_trace[0][1]
    assert channel.trace[:first_compute_channel_len] == ["irecv:0", "wait_recv:0", "irecv:0"]


def test_run_async_stage_source_rank_has_no_recv_calls_and_isend_count_matches_microbatches() -> None:
    """source（`prev_rank=None`）は irecv/wait_recv を一切発行せず，isend のみ M 回発行する．"""

    channel = _FakeChannel()
    num_microbatches = 4

    run_async_stage(
        prev_rank=None,
        next_rank=1,
        num_microbatches=num_microbatches,
        channel=channel,
        recv_bufs=_make_recv_bufs(num_microbatches),
        compute_fn=lambda x: x,
    )

    isend_calls = [entry for entry in channel.trace if entry.startswith("isend")]
    wait_send_calls = [entry for entry in channel.trace if entry.startswith("wait_send")]
    assert len(isend_calls) == num_microbatches
    assert len(wait_send_calls) == num_microbatches
    assert not any(entry.startswith("irecv") or entry.startswith("wait_recv") for entry in channel.trace)


def test_run_async_stage_sink_rank_has_no_send_calls() -> None:
    """sink（`next_rank=None`）は isend/wait_send を一切発行しない．"""

    channel = _FakeChannel()
    num_microbatches = 4

    run_async_stage(
        prev_rank=3,
        next_rank=None,
        num_microbatches=num_microbatches,
        channel=channel,
        recv_bufs=_make_recv_bufs(num_microbatches),
        compute_fn=lambda x: x,
    )

    assert not any(entry.startswith("isend") or entry.startswith("wait_send") for entry in channel.trace)
    irecv_calls = [entry for entry in channel.trace if entry.startswith("irecv")]
    assert len(irecv_calls) == num_microbatches


def test_run_async_stage_with_zero_microbatches_does_nothing() -> None:
    """`num_microbatches=0` のとき，通信も compute も一切発行しない．"""

    channel = _FakeChannel()
    calls = []

    run_async_stage(
        prev_rank=0, next_rank=1, num_microbatches=0, channel=channel, recv_bufs=[],
        compute_fn=lambda x: calls.append(1) or x,
    )

    assert channel.trace == []
    assert calls == []


# ====================================================================
# make_compute_fn
# ====================================================================


def test_make_compute_fn_sleep_proxy_returns_input_tensor_unchanged() -> None:
    """`sleep` proxy は入力テンソルをそのまま（同一オブジェクト）返す．"""

    compute_fn = make_compute_fn(PROXY_SLEEP, t_stage_s=0.001, matmul_size=4, matmul_iters=1)
    x = torch.randn(1, 1, 4)

    assert compute_fn(x) is x


def test_make_compute_fn_matmul_proxy_preserves_shape_and_is_deterministic_for_fixed_weight() -> None:
    """`matmul` proxy は入力形状を保ち，同一インスタンス（同一重み）に対して決定的な出力を返す．"""

    compute_fn = make_compute_fn(PROXY_MATMUL, t_stage_s=0.001, matmul_size=4, matmul_iters=2)
    x = torch.randn(1, 1, 4)

    output_first = compute_fn(x)
    output_second = compute_fn(x)

    assert output_first.shape == x.shape
    assert torch.equal(output_first, output_second)


def test_make_compute_fn_raises_value_error_for_unknown_proxy() -> None:
    """未知の proxy 名を渡すと `ValueError` を送出する．"""

    with pytest.raises(ValueError):
        make_compute_fn("unknown_proxy", t_stage_s=0.001, matmul_size=4, matmul_iters=1)


# ====================================================================
# build_pipeline_fill_microbench_record
# ====================================================================


def test_build_pipeline_fill_microbench_record_includes_all_required_fields() -> None:
    """journal.md Iter8 計画 §5(ii) が指定した (variant, N, M, proxy, repeat) を含む必須フィールドを持つ．"""

    from datetime import datetime, timezone

    run_start = datetime(2026, 7, 20, 3, 0, 0, tzinfo=timezone.utc)
    record = build_pipeline_fill_microbench_record(
        iter_name="Iter8",
        run_id="Iter8-20260720T030000Z-abc123",
        run_start=run_start,
        variant="blocking",
        proxy="sleep",
        num_stages=16,
        num_microbatches=32,
        repeat_index=1,
        total_repeats=5,
        total_time_s=2.94,
        t_stage_s=0.006,
        fill_factor=1.02,
        hidden_size=5376,
        seq_len=1,
        matmul_iters=1,
        torch_threads=1,
    )

    assert record["schema_version"] == SCHEMA_VERSION
    assert record["record_type"] == RECORD_TYPE
    assert record["iter"] == "Iter8"
    assert record["run_id"] == "Iter8-20260720T030000Z-abc123"
    assert record["timestamp"] == "2026-07-20T03:00:00Z"
    assert record["variant"] == "blocking"
    assert record["proxy"] == "sleep"
    assert record["num_stages"] == 16
    assert record["num_microbatches"] == 32
    assert record["repeat_index"] == 1
    assert record["total_repeats"] == 5
    assert record["total_time_s"] == 2.94
    assert record["t_stage_s"] == 0.006
    assert record["fill_factor"] == 1.02

    # JSON へ往復できる（datetime 等の非 JSON 直列化可能型が混入していないこと）を確認する．
    round_tripped = json.loads(json.dumps(record, ensure_ascii=False))
    assert round_tripped == record


# ====================================================================
# append_jsonl
# ====================================================================


def test_append_jsonl_creates_parent_dir_and_appends_one_valid_json_line(tmp_path: Path) -> None:
    """results/ ディレクトリが無くても作成し，1 行の妥当な JSON を追記する．"""

    results_path = tmp_path / "results" / "Iter8.jsonl"
    assert not results_path.parent.exists()

    record = {"record_type": RECORD_TYPE, "variant": "blocking", "repeat_index": 1}
    append_jsonl(results_path, record)

    assert results_path.exists()
    lines = results_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_append_jsonl_appends_without_overwriting_existing_records(tmp_path: Path) -> None:
    """既存の JSONL に対しては上書きせず末尾へ追記する．"""

    results_path = tmp_path / "Iter8.jsonl"
    append_jsonl(results_path, {"repeat_index": 1})
    append_jsonl(results_path, {"repeat_index": 2})

    lines = results_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["repeat_index"] == 1
    assert json.loads(lines[1])["repeat_index"] == 2
