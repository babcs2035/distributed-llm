"""
記録付き推論オーケストレーションツール（結果永続化基盤，config.yml research_frontier① 対応）．

1 回のプロンプト送信を `tools/predict.py` の送信ロジック（`send_prompt_ssh` / `send_prompt_http`）に
そのまま再利用し，送信後に wafl-ctrl1（rank0）コンテナの `docker logs` を取得して
ステップ時間・TTFT・ITL・tokens/sec・prompt/output tokens・埋め込み統計を抽出，
`results/Iter{n}.jsonl` へ 1 実行 = 1 レコードで追記する．`pipeline_inference.py`（ホットパス）と
`tools/predict.py` は非改変（import して再利用するのみ）．

Usage:
  uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!"
  uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!" --http
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from common import ClusterConfig, ssh_via_master
from predict import get_prompt, send_prompt_http, send_prompt_ssh

# rank0 の docker logs を取得する際の SSH タイムアウト（秒）．
DOCKER_LOGS_SSH_TIMEOUT_SEC = 30

# JSONL スキーマのバージョン（journal.md Iteration 1 の確定版に対応）．
SCHEMA_VERSION = 1

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
_STEP_DONE_RE = re.compile(r"^Rank 0: step (\d+) done token=(\d+) dt=([\d.]+)s$")
_DECODING_RE = re.compile(r"^Rank 0: decoding (\d+) generated tokens \(prompt=(\d+)\)")
_DECODED_RE = re.compile(r"^Rank 0: decoded in ([\d.]+)s:")
# RESULT 本文は改行を含み得る（`_log` は改行をエスケープせず生のまま print するため）．
# `_extract_rank0_messages` で継続行を `\n` 連結済みの 1 論理メッセージを渡す前提で，
# re.DOTALL により `.` を改行にもマッチさせ，全文を group(1) で捕捉する．
_RESULT_RE = re.compile(r"^Request response: '(.*)$", re.DOTALL)


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


def parse_rank0_log(log_text: str, predict_result: str | None = None) -> ParsedLog:
    """rank0 の docker logs テキストから 1 実行分の指標を抽出する（純関数．SSH/クラスタ接続に非依存）．

    Args:
        log_text: `docker logs --since {run_start} distributed-llm 2>&1` の生テキスト．
        predict_result: `tools/predict.py` の送信結果（全文）．与えられた場合は複数ブロックの
            中から対応するブロックを選ぶ防御的照合に使う（`_select_relevant_block` 参照）．

    Returns:
        ParsedLog: 必須指標（`prompt_tokens` / `step_dt` / `embed_stats`）が全て取れていれば
        `parse_ok=True`．欠落・ブロック不一致があっても値を黙って捨てず，`parse_warnings` に残す．
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
        parse_warnings=warnings,
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


# ====================================================================
# レコード組み立て・永続化
# ====================================================================


def make_run_id(iter_name: str, run_start: datetime) -> str:
    """`Iter{n}-{UTCyyyymmddThhmmssZ}-{短縮uuid}` 形式の一意な run_id を採番する．"""

    timestamp_part = run_start.strftime("%Y%m%dT%H%M%SZ")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{iter_name}-{timestamp_part}-{short_uuid}"


def build_levers(config: ClusterConfig) -> dict[str, int | float | None]:
    """levers（NUM_MICRO_BATCHES / STAGGER_INTERVAL / SEQ_LEN / WORLD_SIZE）を収集する．

    NUM_MICRO_BATCHES / STAGGER_INTERVAL / WORLD_SIZE は `os.environ` を優先し，
    無ければ `ClusterConfig` の既定値（`num_micro_batches`/`stagger_interval`/`world_size`，
    いずれも `ClusterConfig.__post_init__` 内で `os.environ` → 既定値の順に解決済み）を使う．
    SEQ_LEN は既定ログに出ない値のため `os.environ` のみを見て，未設定なら `null` とする．

    前提と限界: この方式は「コンテナ起動時の env と本ツール実行時の env が一致している」ことを
    暗黙に仮定する．不一致があっても本ツールからは検出できない（journal.md Iteration 1 参照）．
    """

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
) -> dict[str, object]:
    """journal.md Iteration 1 で確定した JSONL スキーマ（schema_version=1）に沿って 1 レコードを組み立てる．"""

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
        "parse_ok": parsed.parse_ok,
        "parse_warnings": parsed.parse_warnings,
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


def run_and_collect(config: ClusterConfig, prompt: str, iter_name: str, use_http: bool) -> dict[str, object]:
    """プロンプト送信からログ取得・パース・レコード組み立てまでを一括で実行する．"""

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
    levers = build_levers(config)

    return build_record(
        iter_name=iter_name, run_id=run_id, run_start=run_start, prompt=prompt,
        parsed=parsed, derived=derived, result_text=result_text,
        e2e_latency_s=e2e_latency_s, levers=levers,
    )


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
    args = parser.parse_args()

    config = ClusterConfig.load(args.config)

    prompt = get_prompt(args.prompt)
    if not prompt:
        print("[ERROR] Empty prompt", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Sending to {config.master_addr}:8082 (iter={args.iter})...", file=sys.stderr)
    record = run_and_collect(config, prompt, args.iter, args.http)

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
