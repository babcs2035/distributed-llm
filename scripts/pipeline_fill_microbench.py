"""Iteration 8 の local パイプライン fill 診断マイクロベンチ: `torch.distributed`（Gloo backend）を
単一マシン上で N プロセス起動し，`_process_microbatch`（pipeline_inference.py:997-1050）が持つ
「recv → compute → send」構造を最小再現して，複数 microbatch が同時に異なる段へ滞在する状態
（段間 fill）が (a) blocking 版・(b) async `isend`/`irecv` 二重バッファ版のそれぞれで構造的に
起きるかどうかを測る．

実機 51 ノードクラスタ・relay プロトコル・`pipeline_inference.py` には一切接続・変更しない．

compute は 2 種の proxy から選べる: `sleep`（主，CPU コア競合を排除して構造のみを切り分ける）・
`matmul`（副，CPU/Gloo 上の実際の演算競合を補足的に見る）．

背景・判定ルール・実装方針の詳細は `.claude/research/journal.md` の `## Iteration 8`
`### 検討・計画 (Iter8)` を参照．

使い方（1 回の実行 = 1 つの (variant, N, M, proxy) 設定について `repeat` 回計測）:
    unset VIRTUAL_ENV && uv run python scripts/pipeline_fill_microbench.py \\
        --variant blocking --proxy sleep --num-stages 16 --num-microbatches 32 --repeat 5

    unset VIRTUAL_ENV && uv run python scripts/pipeline_fill_microbench.py \\
        --variant async --proxy sleep --num-stages 16 --num-microbatches 32 --repeat 5

結果は `results/Iter8.jsonl`（既定，`--iter-name`/`--results-path` で変更可）へ
`record_type="pipeline_fill_microbench"` として (variant, N, M, proxy, repeat_index) ごとに
1 レコード追記される（追記のみ，既存レコードは変更しない）．
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

import torch
import torch.distributed as dist
import torch.multiprocessing as mp

# ====================================================================
# 定数（マジックナンバー回避．値の根拠は journal.md Iter8 計画を参照）
# ====================================================================

VARIANT_BLOCKING = "blocking"
VARIANT_ASYNC = "async"
VARIANTS = (VARIANT_BLOCKING, VARIANT_ASYNC)

PROXY_SLEEP = "sleep"
PROXY_MATMUL = "matmul"
PROXIES = (PROXY_SLEEP, PROXY_MATMUL)

# Iter7 実測の 1 段あたり compute 時間（0.31s/51 ノード ≈ 6.08ms）を既定の t_stage（sleep proxy）とする．
DEFAULT_T_STAGE_S = 0.006
# bench_compute_ceiling.py の HIDDEN_SIZE_FALLBACK（Gemma-4 実測 21504=5376*4 に一致確認済み）と揃える．
DEFAULT_HIDDEN_SIZE = 5376
DEFAULT_SEQ_LEN = 1  # decode（1 トークン）を模す GEMV 形状
DEFAULT_MATMUL_ITERS = 1
DEFAULT_TORCH_THREADS = 1  # rank（プロセス）間のスレッド競合を避け，1 rank=1 コア相当に近づける
DEFAULT_REPEAT = 5
DEFAULT_NUM_STAGES = 16  # journal.md Iter8 §3 の代表判定点 N=16
DEFAULT_NUM_MICROBATCHES = 32  # 同上 M=32
DEFAULT_CALIBRATION_WARMUP = 3
DEFAULT_CALIBRATION_REPS = 5
DEFAULT_INIT_TIMEOUT_S = 60.0
DEFAULT_ITER_NAME = "Iter8"

SCHEMA_VERSION = 1  # 本スクリプト独自のレコードスキーマ（tools/collect_results.py の schema_version とは別空間）
RECORD_TYPE = "pipeline_fill_microbench"

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ====================================================================
# 通信チャネル抽象（実行時は TorchDistChannel，単体テストではフェイク実装に差し替える）
# ====================================================================


class Handle(Protocol):
    """非同期通信ハンドル（`dist.Work` 相当）．"""

    def wait(self) -> None: ...


class Channel(Protocol):
    """`recv`/`send`（blocking）・`irecv`/`isend`（non-blocking，ハンドルを返す）を提供する通信抽象．

    `run_blocking_stage`/`run_async_stage`（パイプライン構造そのもの）は本 Protocol にのみ依存し，
    実際の通信手段（`torch.distributed` か，単体テスト用フェイクか）を知らない．
    """

    def recv(self, tensor: torch.Tensor, src: int) -> None: ...
    def send(self, tensor: torch.Tensor, dst: int) -> None: ...
    def irecv(self, tensor: torch.Tensor, src: int) -> Handle: ...
    def isend(self, tensor: torch.Tensor, dst: int) -> Handle: ...


class TorchDistChannel:
    """`torch.distributed`（Gloo backend）への薄いラッパー．実行時のみ使用する．"""

    def recv(self, tensor: torch.Tensor, src: int) -> None:
        dist.recv(tensor=tensor, src=src)

    def send(self, tensor: torch.Tensor, dst: int) -> None:
        dist.send(tensor=tensor, dst=dst)

    def irecv(self, tensor: torch.Tensor, src: int) -> Handle:
        return dist.irecv(tensor, src=src)

    def isend(self, tensor: torch.Tensor, dst: int) -> Handle:
        return dist.isend(tensor, dst=dst)


# ====================================================================
# パイプライン構造（純粋にロジックのみ．Channel 経由の呼び出し回数・順序で単体テスト可能）
# ====================================================================


def run_blocking_stage(
    *,
    prev_rank: int | None,
    next_rank: int | None,
    num_microbatches: int,
    channel: Channel,
    recv_bufs: list[torch.Tensor],
    compute_fn: Callable[[torch.Tensor], torch.Tensor],
) -> None:
    """`_process_microbatch`（pipeline_inference.py:1019-1050）の recv→compute→send 構造を模した
    blocking 変種．

    `prev_rank` が `None` なら source（recv を省略し `recv_bufs[mb]` を乱数で埋める，:1020-1021 に対応），
    `next_rank` が `None` なら sink（send を省略）．mb はこの rank 内で 0→M-1 まで完全に逐次処理する
    （recv→compute→send が完了してから次の mb へ進む．実コードの `for mb in range(num_micro_batches):
    self._process_microbatch(mb, ...)` ループと同じ構造，pipeline_inference.py:1202-1203）．
    """

    for mb in range(num_microbatches):
        if prev_rank is None:
            recv_bufs[mb].normal_()
        else:
            channel.recv(recv_bufs[mb], prev_rank)

        hidden = compute_fn(recv_bufs[mb])

        if next_rank is not None:
            channel.send(hidden, next_rank)


def run_async_stage(
    *,
    prev_rank: int | None,
    next_rank: int | None,
    num_microbatches: int,
    channel: Channel,
    recv_bufs: list[torch.Tensor],
    compute_fn: Callable[[torch.Tensor], torch.Tensor],
) -> None:
    """async `isend`/`irecv` 二重バッファ変種（journal.md Iter8 計画 §3 (b) の最小プロトタイプ）．

    mb を処理する際:
      1. mb 用の irecv 完了を待つ（前の mb ループで先行発行済み．source は乱数生成で代替）．
      2. **compute の前に** mb+1 用の irecv を先行発行する（次段からの受信を compute とオーバーラップさせる）．
      3. compute 後に mb 用の isend を発行するが，**即座に wait しない**（次の mb の処理へ進む＝
         mb の isend が mb+1 以降の recv-wait/compute とオーバーラップする）．
    全 mb 処理後にまとめて残りの isend を待つ（この関数を抜ける時点で送信完了を保証するため）．
    source（`prev_rank is None`）・sink（`next_rank is None`）では該当する recv/send を行わない．
    """

    if num_microbatches == 0:
        return

    pending_recv: dict[int, Handle] = {}
    if prev_rank is not None:
        pending_recv[0] = channel.irecv(recv_bufs[0], prev_rank)

    pending_sends: list[Handle] = []
    for mb in range(num_microbatches):
        if prev_rank is None:
            recv_bufs[mb].normal_()
        else:
            pending_recv.pop(mb).wait()

        if prev_rank is not None and mb + 1 < num_microbatches:
            pending_recv[mb + 1] = channel.irecv(recv_bufs[mb + 1], prev_rank)

        hidden = compute_fn(recv_bufs[mb])

        if next_rank is not None:
            pending_sends.append(channel.isend(hidden, next_rank))

    for handle in pending_sends:
        handle.wait()


# ====================================================================
# compute proxy（sleep=主，構造のみ切り分け／matmul=副，CPU/Gloo 競合を補足）
# ====================================================================


def make_compute_fn(
    proxy: str,
    *,
    t_stage_s: float,
    matmul_size: int,
    matmul_iters: int,
) -> Callable[[torch.Tensor], torch.Tensor]:
    """`proxy` に応じた compute 代替関数を構築する．

    `sleep`: `time.sleep(t_stage_s)` のみ（入力をそのまま返す．CPU コア競合を排除し，pipeline 構造が
    fill を許すかを純粋に切り分ける．journal.md Iter8 §3 主 proxy）．
    `matmul`: `(matmul_size, matmul_size)` の重みとの行列積を `matmul_iters` 回行う（Gemma-4 の
    GEMV/GEMM 相当の演算負荷．CPU/Gloo 上の実際の競合を補足測定する副 proxy）．
    """

    if proxy == PROXY_SLEEP:
        def _sleep_compute(hidden_state: torch.Tensor) -> torch.Tensor:
            time.sleep(t_stage_s)
            return hidden_state

        return _sleep_compute

    if proxy == PROXY_MATMUL:
        weight = torch.randn(matmul_size, matmul_size, dtype=torch.float32)

        def _matmul_compute(hidden_state: torch.Tensor) -> torch.Tensor:
            output = hidden_state
            for _ in range(matmul_iters):
                output = torch.matmul(output, weight)
            return output

        return _matmul_compute

    raise ValueError(f"未知の compute proxy: {proxy}（{PROXIES} のいずれかを指定すること）")


def calibrate_compute_fn(
    compute_fn: Callable[[torch.Tensor], torch.Tensor],
    sample_input: torch.Tensor,
    *,
    warmup: int,
    reps: int,
) -> float:
    """`compute_fn` 1 回あたりの実測時間（中央値，秒）を計測する．

    `sleep` proxy では指定した `t_stage_s` がほぼそのまま実測されることの確認に，`matmul` proxy では
    実際の演算時間を得て FF 計算の `t_stage` として使うことに用いる（タイミング依存のため単体テスト
    対象外，bench_compute_ceiling.py の `measure_linear` と同じ扱い）．
    """

    for _ in range(warmup):
        compute_fn(sample_input.clone())

    samples_s: list[float] = []
    for _ in range(reps):
        start_s = time.perf_counter()
        compute_fn(sample_input.clone())
        samples_s.append(time.perf_counter() - start_s)

    return statistics.median(samples_s)


# ====================================================================
# fill factor（純関数．単体テスト対象）
# ====================================================================


def compute_fill_factor(
    *,
    num_microbatches: int,
    num_stages: int,
    t_stage_s: float,
    measured_total_time_s: float,
) -> float:
    """`FF = (M+N-1)*t_stage / measured_total_time` を計算する（journal.md Iter8 計画 §4）．

    `FF≈1` は理想的な pipelined 実行（段間 fill 成立）に，`FF≈1/N`（M≫N のとき）は完全 sequential
    （fill 不成立，1 microbatch が全段を単独貫通）にそれぞれ近づく．
    """

    if num_microbatches <= 0 or num_stages <= 0:
        raise ValueError(f"num_microbatches・num_stages は正の整数である必要がある: {num_microbatches}, {num_stages}")
    if t_stage_s <= 0:
        raise ValueError(f"t_stage_s は正の値である必要がある: {t_stage_s}")
    if measured_total_time_s <= 0:
        raise ValueError(f"measured_total_time_s は正の値である必要がある: {measured_total_time_s}")

    ideal_pipelined_time_s = (num_microbatches + num_stages - 1) * t_stage_s
    return ideal_pipelined_time_s / measured_total_time_s


# ====================================================================
# レコード組み立て・永続化
# ====================================================================


def build_pipeline_fill_microbench_record(
    *,
    iter_name: str,
    run_id: str,
    run_start: datetime,
    variant: str,
    proxy: str,
    num_stages: int,
    num_microbatches: int,
    repeat_index: int,
    total_repeats: int,
    total_time_s: float,
    t_stage_s: float,
    fill_factor: float,
    hidden_size: int,
    seq_len: int,
    matmul_iters: int,
    torch_threads: int,
) -> dict[str, object]:
    """(variant, N, M, proxy, repeat) 1 点分の `results/Iter{n}.jsonl` レコードを組み立てる
    （journal.md Iter8 計画 §5 完了条件 (ii)）．"""

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "iter": iter_name,
        "run_id": run_id,
        "timestamp": run_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "variant": variant,
        "proxy": proxy,
        "num_stages": num_stages,
        "num_microbatches": num_microbatches,
        "repeat_index": repeat_index,
        "total_repeats": total_repeats,
        "total_time_s": total_time_s,
        "t_stage_s": t_stage_s,
        "fill_factor": fill_factor,
        "hidden_size": hidden_size,
        "seq_len": seq_len,
        "matmul_iters": matmul_iters,
        "torch_threads": torch_threads,
    }


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    """レコードを JSONL ファイルへ 1 行追記する（親ディレクトリが無ければ作成，末尾改行付き）．"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json.dumps(record, ensure_ascii=False))
        jsonl_file.write("\n")


# ====================================================================
# worker（torch.multiprocessing.spawn で N プロセス起動．実 dist 通信を伴うため単体テスト対象外）
# ====================================================================


@dataclass(frozen=True)
class WorkerConfig:
    """`torch.multiprocessing.spawn` の各 worker プロセスへ渡す設定一式（pickle 可能な素の型のみ）．"""

    variant: str
    proxy: str
    num_stages: int
    num_microbatches: int
    repeat: int
    t_stage_s: float
    hidden_size: int
    seq_len: int
    matmul_iters: int
    torch_threads: int
    calibration_warmup: int
    calibration_reps: int
    master_addr: str
    master_port: int
    init_timeout_s: float
    iter_name: str
    results_path: str


def _run_pipeline_fill_worker(local_rank: int, config: WorkerConfig) -> None:
    """1 つの `torch.multiprocessing.spawn` worker（= 1 pipeline 段）のエントリポイント．

    rank0（source）のみが `calibrate_compute_fn` で t_stage を実測し，各 repeat 計測後に
    `results/Iter{n}.jsonl` へレコードを追記する（他 rank はパイプライン処理のみ実行し，
    保存は行わない．全 rank が同一 `compute_fn` を実行するため rank0 の実測値で代表させる）．
    """

    torch.set_num_threads(config.torch_threads)
    os.environ["MASTER_ADDR"] = config.master_addr
    os.environ["MASTER_PORT"] = str(config.master_port)

    dist.init_process_group(
        backend="gloo",
        init_method=f"tcp://{config.master_addr}:{config.master_port}",
        world_size=config.num_stages,
        rank=local_rank,
        timeout=timedelta(seconds=config.init_timeout_s),
    )

    try:
        prev_rank = None if local_rank == 0 else local_rank - 1
        next_rank = None if local_rank == config.num_stages - 1 else local_rank + 1

        recv_bufs = [
            torch.zeros(1, config.seq_len, config.hidden_size, dtype=torch.float32)
            for _ in range(config.num_microbatches)
        ]
        compute_fn = make_compute_fn(
            config.proxy,
            t_stage_s=config.t_stage_s,
            matmul_size=config.hidden_size,
            matmul_iters=config.matmul_iters,
        )
        channel = TorchDistChannel()

        # rank0 のみ実測 t_stage を計測する（全 rank 同一 compute_fn のため代表値として十分）．
        t_stage_measured_s = config.t_stage_s
        if local_rank == 0:
            sample_input = torch.zeros(1, config.seq_len, config.hidden_size, dtype=torch.float32)
            t_stage_measured_s = calibrate_compute_fn(
                compute_fn,
                sample_input,
                warmup=config.calibration_warmup,
                reps=config.calibration_reps,
            )

        run_stage = run_blocking_stage if config.variant == VARIANT_BLOCKING else run_async_stage

        dist.barrier()
        elapsed_by_repeat_s: list[float] = []
        for _ in range(config.repeat):
            # 各 repeat の開始を全 rank で揃える（開始不揃いによる計測誤差を避ける）．
            dist.barrier()
            start_s = time.perf_counter()
            run_stage(
                prev_rank=prev_rank,
                next_rank=next_rank,
                num_microbatches=config.num_microbatches,
                channel=channel,
                recv_bufs=recv_bufs,
                compute_fn=compute_fn,
            )
            # 終了も全 rank で揃える．rank0 は「全 rank が完了するまで」待たされるため，
            # rank0 が観測する elapsed が pipeline 全体の makespan に一致する．
            dist.barrier()
            elapsed_by_repeat_s.append(time.perf_counter() - start_s)

        if local_rank == 0:
            run_start = datetime.now(timezone.utc)
            run_id = f"{config.iter_name}-{run_start.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
            results_path = Path(config.results_path)
            for repeat_index, total_time_s in enumerate(elapsed_by_repeat_s, start=1):
                fill_factor = compute_fill_factor(
                    num_microbatches=config.num_microbatches,
                    num_stages=config.num_stages,
                    t_stage_s=t_stage_measured_s,
                    measured_total_time_s=total_time_s,
                )
                record = build_pipeline_fill_microbench_record(
                    iter_name=config.iter_name,
                    run_id=run_id,
                    run_start=run_start,
                    variant=config.variant,
                    proxy=config.proxy,
                    num_stages=config.num_stages,
                    num_microbatches=config.num_microbatches,
                    repeat_index=repeat_index,
                    total_repeats=config.repeat,
                    total_time_s=total_time_s,
                    t_stage_s=t_stage_measured_s,
                    fill_factor=fill_factor,
                    hidden_size=config.hidden_size,
                    seq_len=config.seq_len,
                    matmul_iters=config.matmul_iters,
                    torch_threads=config.torch_threads,
                )
                append_jsonl(results_path, record)
            print(
                f"[pipeline_fill_microbench] rank0: {config.repeat} レコードを {results_path} へ追記した "
                f"(variant={config.variant}, proxy={config.proxy}, N={config.num_stages}, M={config.num_microbatches}, "
                f"t_stage_measured_s={t_stage_measured_s:.6f})"
            )
    finally:
        dist.destroy_process_group()


def _find_free_port() -> int:
    """localhost 上の未使用 TCP ポートを 1 つ確保して返す（多重起動時の衝突回避）．"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ====================================================================
# CLI エントリポイント
# ====================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 引数パーサーを構築する（journal.md Iter8 計画 §3 のスイープ軸を引数化）．"""

    parser = argparse.ArgumentParser(
        description="単一マシン上の Gloo マルチプロセスでパイプライン段間 fill を診断するマイクロベンチ",
    )
    parser.add_argument("--variant", choices=VARIANTS, default=VARIANT_BLOCKING, help="pipeline 実行変種")
    parser.add_argument("--proxy", choices=PROXIES, default=PROXY_SLEEP, help="compute 代替（主: sleep, 副: matmul）")
    parser.add_argument("--num-stages", "-N", type=int, default=DEFAULT_NUM_STAGES, help="pipeline 段数（プロセス数）")
    parser.add_argument("--num-microbatches", "-M", type=int, default=DEFAULT_NUM_MICROBATCHES, help="microbatch 数")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="計測反復回数（各回 1 レコード）")
    parser.add_argument("--t-stage", type=float, default=DEFAULT_T_STAGE_S, help="sleep proxy の 1 段あたり秒数")
    parser.add_argument("--hidden-size", type=int, default=DEFAULT_HIDDEN_SIZE, help="通信テンソル最終次元／matmul 次元")
    parser.add_argument("--seq-len", type=int, default=DEFAULT_SEQ_LEN, help="通信テンソルの seq_len 次元")
    parser.add_argument("--matmul-iters", type=int, default=DEFAULT_MATMUL_ITERS, help="matmul proxy の反復回数")
    parser.add_argument("--torch-threads", type=int, default=DEFAULT_TORCH_THREADS, help="rank あたり torch スレッド数")
    parser.add_argument("--calibration-warmup", type=int, default=DEFAULT_CALIBRATION_WARMUP)
    parser.add_argument("--calibration-reps", type=int, default=DEFAULT_CALIBRATION_REPS)
    parser.add_argument("--init-timeout-s", type=float, default=DEFAULT_INIT_TIMEOUT_S)
    parser.add_argument("--port", type=int, default=None, help="MASTER_PORT（未指定なら空きポートを自動選択）")
    parser.add_argument("--iter-name", type=str, default=DEFAULT_ITER_NAME, help="results/Iter{n}.jsonl の {n} 部分")
    parser.add_argument(
        "--results-path", type=Path, default=None,
        help="出力 JSONL パス（未指定なら results/{iter-name}.jsonl）",
    )
    return parser


def main() -> None:
    """CLI 引数を解釈し，`torch.multiprocessing.spawn` で N プロセスのパイプライン fill 診断を実行する．"""

    args = _build_arg_parser().parse_args()

    if args.num_stages <= 0 or args.num_microbatches <= 0:
        raise ValueError(f"--num-stages・--num-microbatches は正の整数である必要がある: {args.num_stages}, {args.num_microbatches}")

    port = args.port if args.port is not None else _find_free_port()
    results_path = args.results_path or (_REPO_ROOT / "results" / f"{args.iter_name}.jsonl")

    config = WorkerConfig(
        variant=args.variant,
        proxy=args.proxy,
        num_stages=args.num_stages,
        num_microbatches=args.num_microbatches,
        repeat=args.repeat,
        t_stage_s=args.t_stage,
        hidden_size=args.hidden_size,
        seq_len=args.seq_len,
        matmul_iters=args.matmul_iters,
        torch_threads=args.torch_threads,
        calibration_warmup=args.calibration_warmup,
        calibration_reps=args.calibration_reps,
        master_addr="127.0.0.1",
        master_port=port,
        init_timeout_s=args.init_timeout_s,
        iter_name=args.iter_name,
        results_path=str(results_path),
    )

    print(
        f"[pipeline_fill_microbench] variant={config.variant} proxy={config.proxy} "
        f"N={config.num_stages} M={config.num_microbatches} repeat={config.repeat} "
        f"port={port} results_path={results_path}"
    )
    mp.spawn(_run_pipeline_fill_worker, args=(config,), nprocs=config.num_stages, join=True)
    print(f"[pipeline_fill_microbench] done. results appended to {results_path}")


if __name__ == "__main__":
    main()
