"""
記録付き推論オーケストレーションツール（結果永続化基盤，config.yml research_frontier① 対応）．

1 回のプロンプト送信を `tools/predict.py` の送信ロジック（`send_prompt_ssh` / `send_prompt_http`）に
そのまま再利用し，送信後に wafl-ctrl1（rank0）コンテナの `docker logs` を取得して
ステップ時間・TTFT・ITL・tokens/sec・prompt/output tokens・埋め込み統計を抽出，
`results/Iter{n}.jsonl` へ 1 実行 = 1 レコードで追記する．`--stage-timing` 指定時は rank1 以降の
worker ノードの `docker logs` も並列 SSH 取得し，段別 compute/send 時間の内訳（`stage_timing`/
`timing_breakdown`，schema_version=2）を追加で記録する（journal.md Iteration 4 参照）．
`--microbatch-bench` 指定時はプロンプト送信を行わず，最終 rank（`world_size - 1`）の `docker logs`
から `[R{rank} RESULT] MICROBATCH_BENCH ...` 行（`pipeline_inference.py` の env ゲート付き bench
モードが出力．journal.md Iteration 7 参照）を全件パースし，`record_type="microbatch_bench"` の
レコードとして 1 行 = 1 計測窓（repeat）で追記する．
`pipeline_inference.py`（ホットパス）と `tools/predict.py` は非改変（import して再利用するのみ）．

Usage:
  uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!"
  uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!" --http
  uv run python tools/collect_results.py --iter Iter4 --prompt "Hello!" --stage-timing
  uv run python tools/collect_results.py --iter Iter7 --microbatch-bench
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from common import ClusterConfig, read_hosts, ssh_via_master
from predict import get_prompt, send_prompt_http, send_prompt_ssh

# rank0 の docker logs を取得する際の SSH タイムアウト（秒）．
DOCKER_LOGS_SSH_TIMEOUT_SEC = 30

# `--stage-timing` 指定時，worker（rank1 以降）の docker logs を並列 SSH 取得する際の最大同時数．
_STAGE_TIMING_MAX_WORKERS = 8

# 秒 → ミリ秒変換係数（per-stage 時間はミリ秒で記録するため使用．マジックナンバー回避）．
_SEC_TO_MS = 1000.0

# JSONL スキーマのバージョン（journal.md Iteration 4 で stage_timing/timing_breakdown を追加し 2 へ更新．
# Iteration 1〜3 の v1 レコードは stage_timing/timing_breakdown を持たない＝後方互換）．
SCHEMA_VERSION = 2

_EMPTY_EMBED_STATS: dict[str, float | None] = {
    "mean": None, "std": None, "min": None, "max": None,
}

# ====================================================================
# ログパース（純関数．SSH/クラスタ接続に非依存，単体テスト対象）
# ====================================================================

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# 任意 rank のログ行プレフィックス．`pipeline_inference.py` の `_log`（print("[R{rank} {tag}] {msg}")）
# が出す本物のレコードは必ずこの形式で始まる．マッチしない行は，直前レコードの本文に埋め込まれた
# 生の改行（例: 複数行 RESULT の継続行）とみなす（下記 `_extract_rank0_messages` 参照）．
_LOG_LINE_RE = re.compile(r"^\[R(\d+) (\w+)\] (.*)$")
_PROMPT_START_RE = re.compile(r"^Rank 0: prompt='")
_PROMPT_TOKENS_EMBED_RE = re.compile(
    r"^Rank 0: prompt tokens=(\d+),.*mean=([-\d.eE]+) std=([-\d.eE]+) "
    r"min=([-\d.eE]+) max=([-\d.eE]+)$"
)
_LEVERS_RE = re.compile(
    r"^Rank 0: levers NUM_MICRO_BATCHES=(\d+) STAGGER_INTERVAL=([\d.]+) "
    r"SEQ_LEN=(\d+) WORLD_SIZE=(\d+)$"
)
_STEP_DONE_RE = re.compile(r"^Rank 0: step (\d+) done token=(\d+) dt=([\d.]+)s$")
_DECODING_RE = re.compile(r"^Rank 0: decoding (\d+) generated tokens \(prompt=(\d+)\)")
_DECODED_RE = re.compile(r"^Rank 0: decoded in ([\d.]+)s:")
# RESULT 本文は改行を含み得る（`_log` は改行をエスケープせず生のまま print するため）．
# `_extract_rank0_messages` で継続行を `\n` 連結済みの 1 論理メッセージを渡す前提で，
# re.DOTALL により `.` を改行にもマッチさせ，全文を group(1) で捕捉する．
_RESULT_RE = re.compile(r"^Request response: '(.*)$", re.DOTALL)

# per-stage 時間ログ（非 rank0 全ノード．journal.md Iteration 4「計画」§0 参照）．
# `compute dt`/`sent to next dt` は行末に `hidden_mean=...` 等が続くため prefix マッチにし，
# `recv_hidden dt` は他の行と衝突しない単独行のため `$` 終端で厳密一致させる．
_COMPUTE_DT_RE = re.compile(r"^Rank (\d+): step (\d+) compute dt=([\d.]+)s")
_RECV_HIDDEN_DT_RE = re.compile(r"^Rank (\d+): recv_hidden dt=([\d.]+)s$")
_SENT_TO_NEXT_DT_RE = re.compile(r"^Rank (\d+): step (\d+) sent to next dt=([\d.]+)s$")

# `MICROBATCH_BENCH` RESULT 行（journal.md Iteration 7．`pipeline_inference.py::_run_microbatch_bench`
# が最終 rank（`next_rank is None`）でのみ計測窓ごとに出力する）．行はそれ自体が単発の物理行のため，
# `_extract_rank0_messages` のような継続行連結は不要（`_LOG_LINE_RE` で行頭のみ照合すれば十分）．
_MICROBATCH_BENCH_RE = re.compile(
    r"^MICROBATCH_BENCH m=(\d+) p=(\d+) warmup=(\d+) measure=(\d+) elapsed_s=([\d.]+) "
    r"steps_per_s=([\d.]+) microbatch_per_s=([\d.]+)$"
)


def _extract_rank0_messages(log_text: str) -> list[str]:
    """ANSI 除去後，継続行を結合した rank0（`[R0 LEVEL] ...`）論理メッセージを順序を保って抽出する．

    `[R{rank} LEVEL] ...` に一致する行を新しい論理レコードの開始とみなし，一致しない行は
    直前レコードの本文へ `\n` で連結する継続行として扱う（RESULT 等の応答本文に埋め込まれた
    生の改行が，プレフィックス無しの物理行として出力されるため）．先頭プレフィックスより前に
    現れる行や，現在レコードが無い状態の継続行は捨てる．全レコードを構築した後，rank0（`R0`）の
    レコードのみを本文（結合済み）で返す．
    """

    records: list[tuple[int, str]] = []
    current_rank: int | None = None
    current_lines: list[str] = []

    def _flush_current_record() -> None:
        if current_rank is not None:
            records.append((current_rank, "\n".join(current_lines)))

    for raw_line in log_text.splitlines():
        clean_line = _ANSI_RE.sub("", raw_line)
        match = _LOG_LINE_RE.match(clean_line)
        if match:
            _flush_current_record()
            current_rank = int(match.group(1))
            current_lines = [match.group(3)]
        elif current_rank is not None:
            current_lines.append(clean_line)
        # else: 先頭プレフィックスより前の行は継続先レコードが無いため捨てる．
    _flush_current_record()

    return [text for rank, text in records if rank == 0]


def _split_into_blocks(messages: list[str]) -> list[list[str]]:
    """`Rank 0: prompt='...'` 行を実行の開始マーカーとして，メッセージ列を実行単位に分割する．

    同一コンテナのログには複数実行が蓄積し得るため，`--since` で区間限定してもブロックが
    複数残る場合がある（例: 別実行が並行して進行中で境界に断片が混入する等）．
    """

    start_indices = [i for i, msg in enumerate(messages) if _PROMPT_START_RE.match(msg)]
    blocks: list[list[str]] = []
    for pos, start in enumerate(start_indices):
        end = start_indices[pos + 1] if pos + 1 < len(start_indices) else len(messages)
        blocks.append(messages[start:end])
    return blocks


def _extract_result_text(block: list[str]) -> str | None:
    """ブロック内の `[R0 RESULT] Request response: '...'` メッセージから応答テキストを取り出す．

    ログ本文は `pipeline_inference.py` 側で先頭 100 文字に truncate 済み（`result[:100]`）．
    応答が複数行の場合，継続行は `_extract_rank0_messages` で `\n` 連結済みのため，
    `_RESULT_RE`（`re.DOTALL`）で全文を 1 回のマッチとして捕捉できる．
    """

    for msg in reversed(block):
        match = _RESULT_RE.match(msg)
        if match:
            text = match.group(1)
            if text.endswith("'"):
                text = text[:-1]
            return text
    return None


def _select_relevant_block(
    blocks: list[list[str]], predict_result: str | None,
) -> tuple[list[str], list[str]]:
    """複数ブロックの中から今回の実行に対応するブロックを選ぶ．

    既定では末尾（最新）のブロックを採用する．`predict_result` が与えられた場合は，
    RESULT 行のスニペット（ログ側は先頭 100 文字で truncate 済み）で predict_result が
    始まる（前方一致）ブロックを優先する（防御的照合．並行実行の断片が末尾に混入していても
    取り違えないため）．

    両辺は比較前に `.strip()` する．SSH 経路（`predict.py` の `send_prompt_ssh` は
    `.stdout.strip()` 済み）と HTTP 経路（`send_prompt_http` は未 strip）とで
    predict_result の前後空白の有無が非対称なため，照合側で吸収する．完全一致（`==`）ではなく
    前方一致にするのは，ログ側スニペットが 100 文字で truncate されており，predict_result
    全文の方が長く続き得るため．
    """

    warnings: list[str] = []
    if not blocks:
        return [], warnings

    latest_block = blocks[-1]
    if predict_result is None:
        return latest_block, warnings

    predict_norm = predict_result.strip()
    for block in reversed(blocks):
        snippet = _extract_result_text(block)
        if snippet is None:
            continue
        snippet_norm = snippet.strip()
        if not snippet_norm:
            # 空スニペット（照合材料が無い）は誤って全ブロックに一致してしまうため対象外とする．
            continue
        if predict_norm.startswith(snippet_norm):
            if block is not latest_block:
                warnings.append(
                    "RESULT text of the latest block did not match the predict "
                    "result; used an earlier block whose RESULT text matched instead"
                )
            return block, warnings

    warnings.append(
        "no block's RESULT text matched the predict result prefix; "
        "used the latest block as a fallback"
    )
    return latest_block, warnings


def _extract_prompt_tokens_and_embed(
    block: list[str],
) -> tuple[int | None, dict[str, float | None]]:
    """`Rank 0: prompt tokens=..., embedding shape=... mean=.. std=.. min=.. max=..` 行を解析する．"""

    for msg in block:
        match = _PROMPT_TOKENS_EMBED_RE.match(msg)
        if match:
            prompt_tokens = int(match.group(1))
            embed_stats: dict[str, float | None] = {
                "mean": float(match.group(2)),
                "std": float(match.group(3)),
                "min": float(match.group(4)),
                "max": float(match.group(5)),
            }
            return prompt_tokens, embed_stats
    return None, dict(_EMPTY_EMBED_STATS)


def _extract_levers(block: list[str]) -> dict[str, int | float | None] | None:
    """ブロック内の `Rank 0: levers NUM_MICRO_BATCHES=... ...` 行から実効 levers を抽出する．

    見つからなければ `None`（levers 行の無い旧形式ログとの互換のため，呼び出し元は
    `build_levers` の env/`ClusterConfig` フォールバックへ回す）．
    """

    for msg in block:
        match = _LEVERS_RE.match(msg)
        if match:
            return {
                "NUM_MICRO_BATCHES": int(match.group(1)),
                "STAGGER_INTERVAL": float(match.group(2)),
                "SEQ_LEN": int(match.group(3)),
                "WORLD_SIZE": int(match.group(4)),
            }
    return None


def _extract_step_dt(block: list[str]) -> list[float]:
    """`Rank 0: step N done token=... dt=...s` 行を step 昇順の dt 配列に変換する．"""

    steps: dict[int, float] = {}
    for msg in block:
        match = _STEP_DONE_RE.match(msg)
        if match:
            steps[int(match.group(1))] = float(match.group(3))
    return [steps[step] for step in sorted(steps)]


def _extract_output_tokens(block: list[str]) -> int | None:
    """`Rank 0: decoding N generated tokens (prompt=M)` 行から生成トークン数 N を取り出す．"""

    for msg in block:
        match = _DECODING_RE.match(msg)
        if match:
            return int(match.group(1))
    return None


def _extract_decode_time(block: list[str]) -> float | None:
    """`Rank 0: decoded in Xs: '...'` 行から復号時間（秒）を取り出す．"""

    for msg in block:
        match = _DECODED_RE.match(msg)
        if match:
            return float(match.group(1))
    return None


# ====================================================================
# per-stage 時間パース（`--stage-timing` 用．非 rank0 全ノードのログが対象）
# ====================================================================


@dataclass
class NodeStageTiming:
    """1 ノード（= 1 rank）分の docker logs から抽出した per-stage 時間．

    worker ノードはブロック開始マーカー（`Rank 0: prompt=`）を持たないため，`run_and_collect`
    が `--since {run_start}` で当該 run の時間窓に限定して取得したログ全体をそのまま渡す前提で
    構築する（journal.md Iteration 4「計画」§2-A 参照．複数 run 混在は `--iter` 変数化＋単発運用で回避）．
    """

    rank: int | None                              # 本文 "Rank {N}:" から検出（ノード = 1 rank）
    compute_dt_ms_by_step: dict[int, float]       # step -> compute dt（ms）
    recv_hidden_dt_ms_step0: float | None         # step0（prefill 受信）の recv_hidden dt（ms）．無ければ None
    sent_to_next_dt_ms_by_step: dict[int, float]  # 中間 rank のみ（最終 rank は空のまま）


def parse_node_stage_timing(log_text: str) -> NodeStageTiming:
    """1 ノード分の docker logs テキストから per-stage 時間（`NodeStageTiming`）を構築する（純関数）．

    `_LOG_LINE_RE`（`[R{rank} LEVEL] ...`）に一致する行は本文部分のみを対象に照合し，一致しない行
    （プレフィックス無しの継続行等）はそのまま本文として照合する．対象の 3 種の物理ログ行はいずれも
    単発行（RESULT のような複数行本文を持たない）ため，`_extract_rank0_messages` のような継続行連結は
    不要である．
    """

    rank: int | None = None
    compute_dt_ms_by_step: dict[int, float] = {}
    recv_hidden_dt_ms_step0: float | None = None
    sent_to_next_dt_ms_by_step: dict[int, float] = {}

    for raw_line in log_text.splitlines():
        clean_line = _ANSI_RE.sub("", raw_line)
        line_match = _LOG_LINE_RE.match(clean_line)
        body = line_match.group(3) if line_match else clean_line

        compute_match = _COMPUTE_DT_RE.match(body)
        if compute_match:
            rank = rank if rank is not None else int(compute_match.group(1))
            step = int(compute_match.group(2))
            compute_dt_ms_by_step[step] = round(float(compute_match.group(3)) * _SEC_TO_MS, 3)
            continue

        recv_match = _RECV_HIDDEN_DT_RE.match(body)
        if recv_match:
            rank = rank if rank is not None else int(recv_match.group(1))
            recv_hidden_dt_ms_step0 = round(float(recv_match.group(2)) * _SEC_TO_MS, 3)
            continue

        sent_match = _SENT_TO_NEXT_DT_RE.match(body)
        if sent_match:
            rank = rank if rank is not None else int(sent_match.group(1))
            step = int(sent_match.group(2))
            sent_to_next_dt_ms_by_step[step] = round(float(sent_match.group(3)) * _SEC_TO_MS, 3)

    return NodeStageTiming(
        rank=rank,
        compute_dt_ms_by_step=compute_dt_ms_by_step,
        recv_hidden_dt_ms_step0=recv_hidden_dt_ms_step0,
        sent_to_next_dt_ms_by_step=sent_to_next_dt_ms_by_step,
    )


# ====================================================================
# MICROBATCH_BENCH パース（`--microbatch-bench` 用．journal.md Iteration 7）
# ====================================================================


@dataclass
class MicrobatchBenchRecord:
    """1 回の計測窓（repeat）分の `MICROBATCH_BENCH` RESULT 行から抽出した集約スループット計測結果．"""

    rank: int
    num_micro_batches: int
    world_size: int
    warmup_steps: int
    measure_steps: int
    elapsed_s: float
    steps_per_s: float
    microbatch_per_s: float


def parse_microbatch_bench_log(log_text: str) -> list[MicrobatchBenchRecord]:
    """docker logs 生テキストから `[R{rank} RESULT] MICROBATCH_BENCH ...` 行を全件抽出する（純関数）．

    最終 rank（`next_rank is None`）のみがこの行を出力する
    （`pipeline_inference.py::_run_microbatch_bench`）ため，rank でのフィルタは不要．
    `repeats` 回分の計測窓が同一ログに複数行並ぶ想定で，出現順にすべて返す
    （1 行 = 1 計測窓 = `results/Iter{n}.jsonl` の 1 レコードに対応させる）．
    """

    records: list[MicrobatchBenchRecord] = []
    for raw_line in log_text.splitlines():
        clean_line = _ANSI_RE.sub("", raw_line)
        line_match = _LOG_LINE_RE.match(clean_line)
        if not line_match:
            continue
        bench_match = _MICROBATCH_BENCH_RE.match(line_match.group(3))
        if not bench_match:
            continue
        records.append(
            MicrobatchBenchRecord(
                rank=int(line_match.group(1)),
                num_micro_batches=int(bench_match.group(1)),
                world_size=int(bench_match.group(2)),
                warmup_steps=int(bench_match.group(3)),
                measure_steps=int(bench_match.group(4)),
                elapsed_s=float(bench_match.group(5)),
                steps_per_s=float(bench_match.group(6)),
                microbatch_per_s=float(bench_match.group(7)),
            )
        )
    return records


@dataclass
class ParsedLog:
    """rank0 の docker logs 1 ブロック分から抽出した生指標．"""

    prompt_tokens: int | None
    embed_stats: dict[str, float | None]
    step_dt: list[float]
    output_tokens_from_log: int | None
    decode_time_s: float | None
    result_text_snippet: str | None
    parse_ok: bool
    parse_warnings: list[str] = field(default_factory=list)
    levers_from_log: dict[str, int | float | None] | None = None


def parse_rank0_log(log_text: str, predict_result: str | None = None) -> ParsedLog:
    """rank0 の docker logs テキストから 1 実行分の指標を抽出する（純関数．SSH/クラスタ接続に非依存）．

    Args:
        log_text: `docker logs --since {run_start} distributed-llm 2>&1` の生テキスト．
        predict_result: `tools/predict.py` の送信結果（全文）．与えられた場合は複数ブロックの
            中から対応するブロックを選ぶ防御的照合に使う（`_select_relevant_block` 参照）．

    Returns:
        ParsedLog: 必須指標（`prompt_tokens` / `step_dt` / `embed_stats`）が全て取れていれば
        `parse_ok=True`．欠落・ブロック不一致があっても値を黙って捨てず，`parse_warnings` に残す．
        `levers_from_log` は選択済みブロックの `Rank 0: levers ...` 行から抽出した実効 levers
        （`build_levers` がログ優先で使う．行が無い旧形式ログでは `None`）．
    """

    messages = _extract_rank0_messages(log_text)
    if not messages:
        return ParsedLog(
            prompt_tokens=None, embed_stats=dict(_EMPTY_EMBED_STATS), step_dt=[],
            output_tokens_from_log=None, decode_time_s=None, result_text_snippet=None,
            parse_ok=False, parse_warnings=["no rank0 (\"[R0 ...]\") log lines found"],
        )

    blocks = _split_into_blocks(messages)
    if not blocks:
        return ParsedLog(
            prompt_tokens=None, embed_stats=dict(_EMPTY_EMBED_STATS), step_dt=[],
            output_tokens_from_log=None, decode_time_s=None, result_text_snippet=None,
            parse_ok=False,
            parse_warnings=["no \"Rank 0: prompt=\" marker found in rank0 log lines"],
        )

    block, warnings = _select_relevant_block(blocks, predict_result)

    prompt_tokens, embed_stats = _extract_prompt_tokens_and_embed(block)
    if prompt_tokens is None:
        warnings.append("prompt tokens / embedding stats line not found in selected block")

    step_dt = _extract_step_dt(block)
    if not step_dt:
        warnings.append("no \"step N done ... dt=\" lines found in selected block")

    output_tokens_from_log = _extract_output_tokens(block)
    if output_tokens_from_log is None:
        warnings.append("\"decoding N generated tokens\" line not found in selected block")

    decode_time_s = _extract_decode_time(block)
    if decode_time_s is None:
        warnings.append("\"decoded in Xs\" line not found in selected block")

    result_text_snippet = _extract_result_text(block)

    parse_ok = (
        prompt_tokens is not None
        and bool(step_dt)
        and embed_stats["mean"] is not None
    )

    return ParsedLog(
        prompt_tokens=prompt_tokens, embed_stats=embed_stats, step_dt=step_dt,
        output_tokens_from_log=output_tokens_from_log, decode_time_s=decode_time_s,
        result_text_snippet=result_text_snippet, parse_ok=parse_ok,
        parse_warnings=warnings, levers_from_log=_extract_levers(block),
    )


# ====================================================================
# 導出指標（純関数，単体テスト対象）
# ====================================================================


def _percentile(values: list[float], pct: float) -> float:
    """線形補間による百分位数を計算する（numpy.percentile 既定の 'linear' 法と同じ計算式）．"""

    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]

    rank = (len(sorted_values) - 1) * (pct / 100.0)
    lower_index = math.floor(rank)
    upper_index = math.ceil(rank)
    if lower_index == upper_index:
        return sorted_values[int(rank)]

    lower_weight = sorted_values[lower_index] * (upper_index - rank)
    upper_weight = sorted_values[upper_index] * (rank - lower_index)
    return lower_weight + upper_weight


@dataclass
class DerivedMetrics:
    """`step_dt` から導出される TTFT・生成時間・TPS・ITL 等．"""

    output_tokens: int
    ttft_s: float | None
    generation_time_s: float | None
    tokens_per_sec: float | None
    itl_p50_s: float | None
    itl_p95_s: float | None


def compute_derived_metrics(
    step_dt: list[float], output_tokens_from_log: int | None,
) -> DerivedMetrics:
    """rank0 の生 `step_dt` 配列から TTFT・生成時間・tokens/sec・ITL(p50/p95) を導出する（純関数）．

    - `ttft_s` は step0 の dt（prefill が全パイプライン段を通過する時間を含む）．
    - `generation_time_s` は `step_dt` の総和．`==0` の場合 `tokens_per_sec` は `null`．
    - `output_tokens` はログの `decoding N generated tokens` 由来の値を優先し，
      無ければ `len(step_dt)` にフォールバックする．
    - `itl_p50_s`/`itl_p95_s` は step0 を除いた `step_dt[1:]` の百分位数．要素 0 個なら `null`．
    """

    output_tokens = (
        output_tokens_from_log if output_tokens_from_log is not None else len(step_dt)
    )

    if not step_dt:
        return DerivedMetrics(
            output_tokens=output_tokens, ttft_s=None, generation_time_s=None,
            tokens_per_sec=None, itl_p50_s=None, itl_p95_s=None,
        )

    ttft_s = step_dt[0]
    generation_time_s = sum(step_dt)
    tokens_per_sec = (
        output_tokens / generation_time_s if generation_time_s > 0 else None
    )

    itl_values = step_dt[1:]
    if itl_values:
        itl_p50_s = _percentile(itl_values, 50.0)
        itl_p95_s = _percentile(itl_values, 95.0)
    else:
        itl_p50_s = None
        itl_p95_s = None

    return DerivedMetrics(
        output_tokens=output_tokens, ttft_s=ttft_s, generation_time_s=generation_time_s,
        tokens_per_sec=tokens_per_sec, itl_p50_s=itl_p50_s, itl_p95_s=itl_p95_s,
    )


@dataclass
class StageTimingSummary:
    """非 rank0 全ノードの `NodeStageTiming` を横断集計した結果（デコードステップ = step≥1 が対象）．"""

    n_ranks_reporting: int                      # compute dt を報告できた非 rank0 rank 数
    compute_sum_ms_by_step: dict[int, float]    # Σ_ranks compute（step 別）
    send_sum_ms_by_step: dict[int, float]       # Σ_intermediate (sent_to_next − compute)（step 別）
    compute_sum_ms_median: float | None         # デコードステップ（step≥1）中央値
    send_sum_ms_median: float | None            # 同上
    prefill_recv_ms_by_rank: dict[int, float]   # step0 recv_hidden（rank 別．prefill 診断用）


# デコードステップと prefill（step0）を分ける境界（step0 は桁が違うため代表値から除外する）．
_FIRST_DECODE_STEP = 1


def aggregate_stage_timing(
    nodes: list[NodeStageTiming],
) -> tuple[StageTimingSummary, list[str]]:
    """複数ノードの `NodeStageTiming` を集約し，step 別 compute/send 総和と中央値代表値を算出する．

    send は中間 rank の `sent_to_next_dt − compute_dt`（同一 step）で近似する．最終 rank は
    `sent_to_next` を持たないため自動的に送信総和から除外される．差分が負になる場合
    （ログ欠損・step 対応ずれ）は 0 クランプせず warning を積んでその (rank, step) を送信集計から
    除外する（黙って歪めない．journal.md Iteration 4「計画」§2-B 参照）．
    """

    warnings: list[str] = []
    compute_sum_ms_by_step: dict[int, float] = {}
    send_sum_ms_by_step: dict[int, float] = {}
    prefill_recv_ms_by_rank: dict[int, float] = {}
    n_ranks_reporting = 0

    for node in nodes:
        if node.compute_dt_ms_by_step:
            n_ranks_reporting += 1

        for step, compute_ms in node.compute_dt_ms_by_step.items():
            compute_sum_ms_by_step[step] = compute_sum_ms_by_step.get(step, 0.0) + compute_ms

        for step, sent_ms in node.sent_to_next_dt_ms_by_step.items():
            compute_ms = node.compute_dt_ms_by_step.get(step)
            if compute_ms is None:
                warnings.append(
                    f"rank {node.rank}: sent_to_next dt at step {step} has no matching "
                    "compute dt; excluded from send aggregation"
                )
                continue
            send_ms = sent_ms - compute_ms
            if send_ms < 0:
                warnings.append(
                    f"rank {node.rank}: negative send dt ({send_ms:.3f}ms) at step {step} "
                    "(sent_to_next < compute); excluded from send aggregation"
                )
                continue
            send_sum_ms_by_step[step] = send_sum_ms_by_step.get(step, 0.0) + send_ms

        if node.recv_hidden_dt_ms_step0 is not None and node.rank is not None:
            prefill_recv_ms_by_rank[node.rank] = node.recv_hidden_dt_ms_step0

    decode_compute_values = [
        v for step, v in compute_sum_ms_by_step.items() if step >= _FIRST_DECODE_STEP
    ]
    decode_send_values = [
        v for step, v in send_sum_ms_by_step.items() if step >= _FIRST_DECODE_STEP
    ]

    summary = StageTimingSummary(
        n_ranks_reporting=n_ranks_reporting,
        compute_sum_ms_by_step=compute_sum_ms_by_step,
        send_sum_ms_by_step=send_sum_ms_by_step,
        compute_sum_ms_median=_percentile(decode_compute_values, 50.0) if decode_compute_values else None,
        send_sum_ms_median=_percentile(decode_send_values, 50.0) if decode_send_values else None,
        prefill_recv_ms_by_rank=prefill_recv_ms_by_rank,
    )
    return summary, warnings


def build_timing_breakdown(
    step_dt: list[float], summary: StageTimingSummary,
) -> dict[str, float | int | None]:
    """rank0 の `step_dt`（デコードステップ = step≥1）と `StageTimingSummary` から残差内訳を算出する．

    `residual_ms_median = rank0_step_dt_median_ms − compute_sum_ms_median − send_sum_ms_median`
    （recv 待ち＋ACK 往復＋Gloo/Python オーバーヘッド＋rank0/最終 rank の周辺処理に相当．
    journal.md Iteration 4「計画」§2-B 参照）．compute/send いずれかの中央値が `None`（報告ノード無し
    等）の場合，残差は算出不能として `None` にする（捏造しない）．
    """

    decode_step_dt_s = step_dt[_FIRST_DECODE_STEP:]
    rank0_step_dt_median_ms = (
        _percentile(decode_step_dt_s, 50.0) * _SEC_TO_MS if decode_step_dt_s else None
    )

    compute_ms = summary.compute_sum_ms_median
    send_ms = summary.send_sum_ms_median
    if rank0_step_dt_median_ms is not None and compute_ms is not None and send_ms is not None:
        residual_ms_median: float | None = rank0_step_dt_median_ms - compute_ms - send_ms
    else:
        residual_ms_median = None

    return {
        "compute_sum_ms_median": compute_ms,
        "send_sum_ms_median": send_ms,
        "residual_ms_median": residual_ms_median,
        "rank0_step_dt_median_ms": rank0_step_dt_median_ms,
        "n_ranks_reporting": summary.n_ranks_reporting,
    }


# ====================================================================
# レコード組み立て・永続化
# ====================================================================


def make_run_id(iter_name: str, run_start: datetime) -> str:
    """`Iter{n}-{UTCyyyymmddThhmmssZ}-{短縮uuid}` 形式の一意な run_id を採番する．"""

    timestamp_part = run_start.strftime("%Y%m%dT%H%M%SZ")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{iter_name}-{timestamp_part}-{short_uuid}"


def build_levers(
    config: ClusterConfig,
    levers_from_log: dict[str, int | float | None] | None = None,
) -> dict[str, int | float | None]:
    """levers（NUM_MICRO_BATCHES / STAGGER_INTERVAL / SEQ_LEN / WORLD_SIZE）を収集する．

    `levers_from_log`（`parse_rank0_log` が選択済みブロックの `Rank 0: levers ...` 行から
    抽出した実効値）があれば，それをそのまま採用する（rank0 コンテナが起動時に実際に解決した
    値であり，本ツール実行時の env と食い違っても正しい．journal.md Iteration 3 参照）．

    `levers_from_log` が `None`（levers 行の無い旧形式ログ・パース失敗）の場合のみ，従来どおり
    `os.environ` → `ClusterConfig` 既定値のフォールバックで構築する．NUM_MICRO_BATCHES /
    STAGGER_INTERVAL / WORLD_SIZE は `os.environ` を優先し，無ければ `ClusterConfig` の既定値
    （`num_micro_batches`/`stagger_interval`/`world_size`，いずれも `ClusterConfig.__post_init__`
    内で `os.environ` → 既定値の順に解決済み）を使う．SEQ_LEN は既定ログに出ない値のため
    `os.environ` のみを見て，未設定なら `null` とする．

    前提と限界（フォールバック時のみ）: この方式は「コンテナ起動時の env と本ツール実行時の
    env が一致している」ことを暗黙に仮定する．不一致があっても本ツールからは検出できない
    （journal.md Iteration 1 参照）．
    """

    if levers_from_log is not None:
        return dict(levers_from_log)

    def _to_number(raw: str | None, cast: type) -> int | float | None:
        if raw is None:
            return None
        try:
            return cast(raw)
        except ValueError:
            return None

    return {
        "NUM_MICRO_BATCHES": _to_number(config.num_micro_batches, int),
        "STAGGER_INTERVAL": _to_number(config.stagger_interval, float),
        "SEQ_LEN": _to_number(os.environ.get("SEQ_LEN"), int),
        "WORLD_SIZE": _to_number(config.world_size, int),
    }


def build_record(
    *,
    iter_name: str,
    run_id: str,
    run_start: datetime,
    prompt: str,
    parsed: ParsedLog,
    derived: DerivedMetrics,
    result_text: str,
    e2e_latency_s: float,
    levers: dict[str, int | float | None],
    stage_timing: dict[str, object] | None = None,
    timing_breakdown: dict[str, float | int | None] | None = None,
) -> dict[str, object]:
    """journal.md Iteration 1〜4 で確定した JSONL スキーマ（schema_version=2）に沿って 1 レコードを組み立てる．

    `stage_timing`/`timing_breakdown` は `--stage-timing` 指定時のみ非 null（`StageTimingSummary`/
    `build_timing_breakdown` の結果）．未指定（既定・schema v1 相当の挙動）では両方 `null` とし，
    Iteration 1〜3 の v1 レコードと後方互換を保つ．
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "iter": iter_name,
        "run_id": run_id,
        "timestamp": run_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prompt": prompt,
        "prompt_tokens": parsed.prompt_tokens,
        "output_tokens": derived.output_tokens,
        "step_dt": parsed.step_dt,
        "ttft_s": derived.ttft_s,
        "generation_time_s": derived.generation_time_s,
        "tokens_per_sec": derived.tokens_per_sec,
        "itl_p50_s": derived.itl_p50_s,
        "itl_p95_s": derived.itl_p95_s,
        "decode_time_s": parsed.decode_time_s,
        "e2e_latency_s": e2e_latency_s,
        "result_text": result_text,
        "embed_stats": parsed.embed_stats,
        "levers": levers,
        "stage_timing": stage_timing,
        "timing_breakdown": timing_breakdown,
        "parse_ok": parsed.parse_ok,
        "parse_warnings": parsed.parse_warnings,
    }


def build_microbatch_bench_record(
    *,
    iter_name: str,
    run_id: str,
    run_start: datetime,
    bench: MicrobatchBenchRecord,
) -> dict[str, object]:
    """journal.md Iteration 7 の `MICROBATCH_BENCH` 計測窓 1 件を JSONL レコードとして組み立てる．

    通常の serving レコード（`build_record`）とスキーマが異なる（`prompt`/`result_text` 等を持たない）
    ため，`record_type="microbatch_bench"` で区別できるようにする．必須フィールドは
    `num_micro_batches, world_size, warmup_steps, measure_steps, elapsed_s, steps_per_s,
    microbatch_per_s, rank`（journal.md Iteration 7「計画」§2 で指定済み）．
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "microbatch_bench",
        "iter": iter_name,
        "run_id": run_id,
        "timestamp": run_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rank": bench.rank,
        "num_micro_batches": bench.num_micro_batches,
        "world_size": bench.world_size,
        "warmup_steps": bench.warmup_steps,
        "measure_steps": bench.measure_steps,
        "elapsed_s": bench.elapsed_s,
        "steps_per_s": bench.steps_per_s,
        "microbatch_per_s": bench.microbatch_per_s,
    }


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    """レコードを JSONL ファイルへ 1 行追記する（親ディレクトリが無ければ作成，末尾改行付き）．"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json.dumps(record, ensure_ascii=False))
        jsonl_file.write("\n")


# ====================================================================
# オーケストレーション（実機 SSH/クラスタ接続を伴う．単体テスト対象外）
# ====================================================================


def collect_rank0_log(config: ClusterConfig, run_start: datetime) -> tuple[str, list[str]]:
    """wafl-ctrl1（rank0，= master 自身）から `docker logs --since {run_start}` を取得する．

    Returns:
        (log_text, warnings): SSH 失敗時は `log_text=""` と失敗内容を含む warnings を返す
        （黙って例外を握りつぶさない）．
    """

    since_str = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = ssh_via_master(
        config.ssh_user, config.master_addr, config.master_addr,
        f"docker logs --since {since_str} distributed-llm 2>&1",
        timeout=DOCKER_LOGS_SSH_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return "", [f"failed to fetch rank0 docker logs via ssh: {stderr}"]
    return result.stdout, []


def collect_worker_stage_timing_logs(
    config: ClusterConfig, run_start: datetime,
) -> tuple[list[str], list[str]]:
    """`--stage-timing` 指定時，rank1 以降の worker から `docker logs --since {run_start}` を並列取得する．

    rank0（= master 自身）は `collect_rank0_log` で別途取得済みのためここでは対象外とする
    （`read_hosts` は `hosts[i]` = rank i の順で IP を返す．journal.md Iteration 4「計画」§0 参照）．
    個々のノードの SSH 失敗は握りつぶさず warnings へ積み，成功ノードのログのみ返す（一部欠損許容）．
    """

    since_str = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    hosts = read_hosts(config.hosts_file)
    worker_ranks = list(range(1, len(hosts)))

    def _fetch_one(rank: int) -> tuple[int, str, str | None]:
        result = ssh_via_master(
            config.ssh_user, config.master_addr, hosts[rank],
            f"docker logs --since {since_str} distributed-llm 2>&1",
            timeout=DOCKER_LOGS_SSH_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            return rank, "", (result.stderr or "").strip()
        return rank, result.stdout, None

    log_texts: list[str] = []
    warnings: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_STAGE_TIMING_MAX_WORKERS) as executor:
        for rank, log_text, error in executor.map(_fetch_one, worker_ranks):
            if error is not None:
                warnings.append(f"failed to fetch rank {rank} docker logs: {error}")
                continue
            log_texts.append(log_text)

    return log_texts, warnings


def run_and_collect(
    config: ClusterConfig, prompt: str, iter_name: str, use_http: bool,
    stage_timing: bool = False,
) -> dict[str, object]:
    """プロンプト送信からログ取得・パース・レコード組み立てまでを一括で実行する．

    `stage_timing=True` の場合のみ，rank1 以降の worker ログを追加取得して per-stage 内訳
    （`stage_timing`/`timing_breakdown`）を算出する．既定（`False`）では現行挙動を完全に維持する
    （既存の `predict:demo` 相当 run を重くしない．journal.md Iteration 4「計画」§2-C 参照）．
    """

    run_start = datetime.now(timezone.utc)
    run_id = make_run_id(iter_name, run_start)

    if use_http:
        result_text = send_prompt_http(config, prompt)
    else:
        result_text = send_prompt_ssh(config, prompt)
    e2e_latency_s = (datetime.now(timezone.utc) - run_start).total_seconds()

    log_text, log_warnings = collect_rank0_log(config, run_start)
    parsed = parse_rank0_log(log_text, predict_result=result_text)
    parsed.parse_warnings = log_warnings + parsed.parse_warnings
    if log_warnings:
        parsed.parse_ok = False

    derived = compute_derived_metrics(parsed.step_dt, parsed.output_tokens_from_log)
    levers = build_levers(config, parsed.levers_from_log)

    stage_timing_dict: dict[str, object] | None = None
    timing_breakdown: dict[str, float | int | None] | None = None
    if stage_timing:
        worker_logs, worker_warnings = collect_worker_stage_timing_logs(config, run_start)
        node_timings = [parse_node_stage_timing(log_text) for log_text in worker_logs]
        summary, aggregate_warnings = aggregate_stage_timing(node_timings)
        parsed.parse_warnings = parsed.parse_warnings + worker_warnings + aggregate_warnings
        stage_timing_dict = asdict(summary)
        timing_breakdown = build_timing_breakdown(parsed.step_dt, summary)

    return build_record(
        iter_name=iter_name, run_id=run_id, run_start=run_start, prompt=prompt,
        parsed=parsed, derived=derived, result_text=result_text,
        e2e_latency_s=e2e_latency_s, levers=levers,
        stage_timing=stage_timing_dict, timing_breakdown=timing_breakdown,
    )


def collect_last_rank_log(
    config: ClusterConfig, since: datetime | None = None,
) -> tuple[int, str, list[str]]:
    """最終 rank（`world_size - 1`）の `docker logs` を取得する．

    `MICROBATCH_BENCH` RESULT 行は最終 rank（`next_rank is None`）でしか出力されない
    （`pipeline_inference.py::_run_microbatch_bench`）ため，rank0 ではなくこちらを対象にする．
    bench は HTTP リクエストと無関係にコンテナ起動時に自動実行され，serving レコードのような
    自然な `run_start` を持たないため，`since` は呼び出し側が明示的に絞り込みたい場合のみ渡す
    （省略時はコンテナの全ログを取得する．同一デプロイに対して複数回 collect すると
    `MICROBATCH_BENCH` 行が重複記録され得る点に注意．journal.md Iteration 7 参照）．

    Returns:
        (last_rank, log_text, warnings): SSH 失敗時は `log_text=""` と失敗内容を含む warnings を返す．
    """

    hosts = read_hosts(config.hosts_file)
    last_rank = int(config.world_size) - 1
    if not (0 <= last_rank < len(hosts)):
        return last_rank, "", [
            f"last rank {last_rank} is out of range for hosts.txt ({len(hosts)} hosts)"
        ]

    since_opt = f"--since {since.strftime('%Y-%m-%dT%H:%M:%SZ')} " if since is not None else ""
    result = ssh_via_master(
        config.ssh_user, config.master_addr, hosts[last_rank],
        f"docker logs {since_opt}distributed-llm 2>&1",
        timeout=DOCKER_LOGS_SSH_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return last_rank, "", [f"failed to fetch rank {last_rank} docker logs via ssh: {stderr}"]
    return last_rank, result.stdout, []


def run_microbatch_bench_collect(
    config: ClusterConfig, iter_name: str, since: datetime | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """最終 rank の docker logs から `MICROBATCH_BENCH` 行を取得し，1 計測窓 = 1 レコードで組み立てる．

    プロンプト送信は行わない（bench は乱数入力の `_pipeline_loop` を測るためのモードであり，
    実プロンプト serving（`run_and_collect`）とは独立している．journal.md Iteration 7 参照）．
    """

    run_start = datetime.now(timezone.utc)
    last_rank, log_text, warnings = collect_last_rank_log(config, since=since)
    if not log_text:
        return [], warnings

    bench_records = parse_microbatch_bench_log(log_text)
    if not bench_records:
        warnings.append(
            f"no MICROBATCH_BENCH RESULT lines found in rank {last_rank} docker logs "
            "(MICROBATCH_BENCH_STEPS may be 0, or bench has not run yet)"
        )

    records: list[dict[str, object]] = []
    for bench in bench_records:
        run_id = make_run_id(iter_name, run_start)
        records.append(
            build_microbatch_bench_record(
                iter_name=iter_name, run_id=run_id, run_start=run_start, bench=bench,
            )
        )
    return records, warnings


def main() -> None:
    """CLI エントリポイント．プロンプト送信 → rank0 ログ収集 → `results/Iter{n}.jsonl` への追記を行う．"""

    parser = argparse.ArgumentParser(
        description="Send a prompt and persist rank0 metrics to results/Iter{n}.jsonl",
    )
    parser.add_argument("--config", "-c", default="config.json",
                        help="Path to config.json")
    parser.add_argument("--http", action="store_true",
                        help="Connect directly via HTTP (not SSH)")
    parser.add_argument("--prompt", "-p",
                        help="Prompt text (skip input())")
    parser.add_argument("--iter", default="Iter1",
                        help="Iteration name used for run_id / results/{iter}.jsonl (default: Iter1)")
    parser.add_argument("--results-dir", default="results",
                        help="Directory to write results/{iter}.jsonl into (default: results)")
    parser.add_argument("--stage-timing", action="store_true",
                        help="Also fetch rank1+ worker docker logs (parallel SSH) and record "
                             "per-stage compute/send timing breakdown (default: off)")
    parser.add_argument("--microbatch-bench", action="store_true",
                        help="Skip prompt sending; instead fetch the last rank's docker logs, "
                             "parse MICROBATCH_BENCH RESULT lines (pipeline_inference.py's "
                             "MICROBATCH_BENCH_STEPS bench mode), and append one record per "
                             "measurement window to results/{iter}.jsonl (default: off)")
    parser.add_argument("--since",
                        help="ISO8601 UTC timestamp (e.g. 2026-07-19T12:00:00Z) to narrow "
                             "'docker logs --since' when using --microbatch-bench (default: "
                             "fetch the full container log; repeated collection without --since "
                             "may duplicate MICROBATCH_BENCH records from earlier deploys)")
    args = parser.parse_args()

    config = ClusterConfig.load(args.config)

    if args.microbatch_bench:
        since = (
            datetime.strptime(args.since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if args.since else None
        )
        records, warnings = run_microbatch_bench_collect(config, args.iter, since=since)

        results_path = Path(args.results_dir) / f"{args.iter}.jsonl"
        for record in records:
            append_jsonl(results_path, record)

        print(
            f"[INFO] appended {len(records)} MICROBATCH_BENCH record(s) to {results_path}",
            file=sys.stderr,
        )
        for warning in warnings:
            print(f"[WARN] {warning}", file=sys.stderr)
        return

    prompt = get_prompt(args.prompt)
    if not prompt:
        print("[ERROR] Empty prompt", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Sending to {config.master_addr}:8082 (iter={args.iter})...", file=sys.stderr)
    record = run_and_collect(config, prompt, args.iter, args.http, stage_timing=args.stage_timing)

    results_path = Path(args.results_dir) / f"{args.iter}.jsonl"
    append_jsonl(results_path, record)

    print(record["result_text"])
    print(
        f"[INFO] appended 1 record to {results_path} "
        f"(parse_ok={record['parse_ok']}, tokens_per_sec={record['tokens_per_sec']})",
        file=sys.stderr,
    )
    if record["parse_warnings"]:
        for warning in record["parse_warnings"]:
            print(f"[WARN] {warning}", file=sys.stderr)


if __name__ == "__main__":
    main()
