"""`tools/collect_results.py` のパース純関数・導出指標計算・レコード組み立てに対する単体テスト．

クラスタ・SSH 接続は一切行わない（`tests/fixtures/rank0_sample.log` の固定ログのみを入力とする）．
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from collect_results import (
    _extract_result_text,
    _percentile,
    _select_relevant_block,
    build_levers,
    build_record,
    append_jsonl,
    compute_derived_metrics,
    make_run_id,
    parse_rank0_log,
    DerivedMetrics,
    ParsedLog,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "rank0_sample.log"


def _load_fixture_text() -> str:
    """テスト対象のログフィクスチャ（ANSI 混入・2 実行ブロック入り）を読み込む．"""

    return _FIXTURE_PATH.read_text(encoding="utf-8")


# ====================================================================
# parse_rank0_log
# ====================================================================


def test_parse_rank0_log_extracts_latest_block_metrics_with_matching_predict_result() -> None:
    """RESULT テキストが predict 戻り値と一致するとき，末尾（最新）ブロックの指標を正しく抽出する．"""

    log_text = _load_fixture_text()
    parsed = parse_rank0_log(log_text, predict_result="Hi there! How can I help you today?")

    assert parsed.parse_ok is True
    assert parsed.parse_warnings == []
    assert parsed.prompt_tokens == 6
    assert parsed.embed_stats == {
        "mean": pytest.approx(0.012345),
        "std": pytest.approx(0.987654),
        "min": pytest.approx(-3.21),
        "max": pytest.approx(3.45),
    }
    assert parsed.step_dt == pytest.approx([0.8, 0.12, 0.15, 0.11, 0.2])
    assert parsed.output_tokens_from_log == 5
    assert parsed.decode_time_s == pytest.approx(0.045)
    assert parsed.result_text_snippet == "Hi there! How can I help you today?"


def test_parse_rank0_log_defaults_to_latest_block_without_predict_result() -> None:
    """predict_result を渡さない場合でも，既定で末尾（最新）ブロックが採用される．"""

    log_text = _load_fixture_text()
    parsed = parse_rank0_log(log_text)

    assert parsed.parse_ok is True
    assert parsed.prompt_tokens == 6
    assert parsed.step_dt == pytest.approx([0.8, 0.12, 0.15, 0.11, 0.2])


def test_parse_rank0_log_strips_ansi_and_ignores_other_rank_lines() -> None:
    """ANSI 色コード混入行・rank0 以外の行があっても正しくパースできる（フィクスチャに両方含む）．"""

    log_text = _load_fixture_text()
    assert "\x1b[0;34m" in log_text  # フィクスチャに ANSI 混入行があることの前提確認
    assert "[R1 INFO]" in log_text and "[R2 INFO]" in log_text  # 他 rank 行があることの前提確認

    parsed = parse_rank0_log(log_text)
    # 他 rank 行やANSIタグはstep_dt等の数値抽出に混入しない
    assert len(parsed.step_dt) == 5


def test_parse_rank0_log_returns_parse_ok_false_when_no_rank0_lines_present() -> None:
    """rank0 のログ行が全く無い場合，指標を捏造せず parse_ok=False と警告を返す．"""

    parsed = parse_rank0_log("[R1 INFO] Rank 1: something happened\n")

    assert parsed.parse_ok is False
    assert parsed.step_dt == []
    assert parsed.prompt_tokens is None
    assert any("no rank0" in w for w in parsed.parse_warnings)


def test_parse_rank0_log_returns_parse_ok_false_when_no_prompt_marker_present() -> None:
    """rank0 行はあるが実行開始マーカー（prompt=）が無い場合，parse_ok=False とする．"""

    log_text = "[R0 INFO] Rank 0: step 0 done token=1 dt=0.1s\n"
    parsed = parse_rank0_log(log_text)

    assert parsed.parse_ok is False
    assert any("prompt=" in w for w in parsed.parse_warnings)


# ====================================================================
# _select_relevant_block（防御的照合）
# ====================================================================


def test_select_relevant_block_prefers_matching_block_over_incomplete_latest_block() -> None:
    """最新ブロックが（並行実行中などで）RESULT 未確定の場合，predict_result と一致する過去ブロックを優先する．"""

    older_block = [
        "Rank 0: prompt='Hi'",
        "Rank 0: step 0 done token=1 dt=0.1s",
        "Request response: 'Hello'",
    ]
    incomplete_latest_block = [
        "Rank 0: prompt='In progress'",
        "Rank 0: step 0 done token=9 dt=0.2s",
        # RESULT 行が無い（まだ実行完了していない）
    ]

    block, warnings = _select_relevant_block(
        [older_block, incomplete_latest_block], predict_result="Hello",
    )

    assert block == older_block
    assert any("used an earlier block" in w for w in warnings)


def test_select_relevant_block_falls_back_to_latest_when_no_block_matches() -> None:
    """どのブロックの RESULT も predict_result と一致しない場合，末尾（最新）ブロックへフォールバックする．"""

    block_a = ["Rank 0: prompt='A'", "Request response: 'foo'"]
    block_b = ["Rank 0: prompt='B'", "Request response: 'bar'"]

    block, warnings = _select_relevant_block([block_a, block_b], predict_result="not-matching-anything")

    assert block == block_b
    assert any("used the latest block as a fallback" in w for w in warnings)


def test_select_relevant_block_returns_latest_without_warning_when_predict_result_is_none() -> None:
    """predict_result が与えられない場合は，警告無しで単純に末尾ブロックを採用する．"""

    block_a = ["Rank 0: prompt='A'"]
    block_b = ["Rank 0: prompt='B'"]

    block, warnings = _select_relevant_block([block_a, block_b], predict_result=None)

    assert block == block_b
    assert warnings == []


def test_extract_result_text_returns_none_when_no_result_line() -> None:
    """RESULT 行が無いブロックからは None を返す（欠測を黙って埋めない）．"""

    block = ["Rank 0: prompt='A'", "Rank 0: step 0 done token=1 dt=0.1s"]
    assert _extract_result_text(block) is None


# ====================================================================
# compute_derived_metrics
# ====================================================================


def test_compute_derived_metrics_normal_values() -> None:
    """5 ステップ分の step_dt から TTFT・生成時間・TPS・ITL(p50/p95) を正しく導出する．"""

    derived = compute_derived_metrics([0.8, 0.12, 0.15, 0.11, 0.2], output_tokens_from_log=5)

    assert derived.output_tokens == 5
    assert derived.ttft_s == pytest.approx(0.8)
    assert derived.generation_time_s == pytest.approx(1.38)
    assert derived.tokens_per_sec == pytest.approx(5 / 1.38)
    assert derived.itl_p50_s == pytest.approx(0.135)
    assert derived.itl_p95_s == pytest.approx(0.1925)


def test_compute_derived_metrics_falls_back_to_step_count_when_log_count_missing() -> None:
    """ログに `decoding N generated tokens` が無い場合，output_tokens は len(step_dt) にフォールバックする．"""

    derived = compute_derived_metrics([0.5, 0.1, 0.2], output_tokens_from_log=None)

    assert derived.output_tokens == 3


def test_compute_derived_metrics_empty_step_dt_yields_all_none() -> None:
    """step_dt が空（何も生成できなかった等）の場合，指標は全て null とし，ゼロ除算等で捏造しない．"""

    derived = compute_derived_metrics([], output_tokens_from_log=None)

    assert derived.output_tokens == 0
    assert derived.ttft_s is None
    assert derived.generation_time_s is None
    assert derived.tokens_per_sec is None
    assert derived.itl_p50_s is None
    assert derived.itl_p95_s is None


def test_compute_derived_metrics_zero_generation_time_yields_null_tokens_per_sec() -> None:
    """generation_time_s == 0 の場合，tokens_per_sec はゼロ除算せず null とする．"""

    derived = compute_derived_metrics([0.0], output_tokens_from_log=None)

    assert derived.output_tokens == 1
    assert derived.ttft_s == 0.0
    assert derived.generation_time_s == 0.0
    assert derived.tokens_per_sec is None
    # step_dt[1:] が空のため ITL も null
    assert derived.itl_p50_s is None
    assert derived.itl_p95_s is None


def test_compute_derived_metrics_single_step_has_no_itl() -> None:
    """step が 1 つしか無い（step_dt[1:] が空）場合，ITL は null になる．"""

    derived = compute_derived_metrics([0.5], output_tokens_from_log=1)

    assert derived.itl_p50_s is None
    assert derived.itl_p95_s is None


def test_percentile_two_elements_matches_linear_interpolation() -> None:
    """2 要素の百分位数が線形補間（numpy 'linear' 法）どおりに計算される．"""

    assert _percentile([1.0, 2.0], 50.0) == pytest.approx(1.5)
    assert _percentile([1.0, 2.0], 95.0) == pytest.approx(1.95)


def test_percentile_single_element_returns_that_element() -> None:
    """要素数 1 の場合はその値をそのまま返す．"""

    assert _percentile([3.5], 50.0) == pytest.approx(3.5)
    assert _percentile([3.5], 95.0) == pytest.approx(3.5)


# ====================================================================
# build_levers
# ====================================================================


def test_build_levers_reads_config_defaults_and_seq_len_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """NUM_MICRO_BATCHES/STAGGER_INTERVAL/WORLD_SIZE は ClusterConfig 由来，SEQ_LEN は環境変数由来で埋める．"""

    monkeypatch.setenv("SEQ_LEN", "512")
    fake_config = SimpleNamespace(num_micro_batches="8", stagger_interval="1.5", world_size="21")

    levers = build_levers(fake_config)  # type: ignore[arg-type]

    assert levers == {
        "NUM_MICRO_BATCHES": 8,
        "STAGGER_INTERVAL": 1.5,
        "SEQ_LEN": 512,
        "WORLD_SIZE": 21,
    }


def test_build_levers_seq_len_is_null_when_env_var_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEQ_LEN は既定ログに出ない値のため，環境変数が無ければ null のままとする．"""

    monkeypatch.delenv("SEQ_LEN", raising=False)
    fake_config = SimpleNamespace(num_micro_batches="4", stagger_interval="3.0", world_size="51")

    levers = build_levers(fake_config)  # type: ignore[arg-type]

    assert levers["SEQ_LEN"] is None


def test_build_levers_returns_none_for_unparseable_numeric_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """数値変換に失敗する値（不正な環境変数等）は null とし，例外で落とさない．"""

    monkeypatch.delenv("SEQ_LEN", raising=False)
    fake_config = SimpleNamespace(num_micro_batches="not-a-number", stagger_interval="3.0", world_size="51")

    levers = build_levers(fake_config)  # type: ignore[arg-type]

    assert levers["NUM_MICRO_BATCHES"] is None


# ====================================================================
# make_run_id / build_record / append_jsonl
# ====================================================================


def test_make_run_id_matches_expected_format() -> None:
    """run_id が `Iter{n}-{UTCyyyymmddThhmmssZ}-{短縮uuid}` 形式になっている．"""

    from datetime import datetime, timezone

    run_start = datetime(2026, 7, 18, 8, 15, 0, tzinfo=timezone.utc)
    run_id = make_run_id("Iter1", run_start)

    assert re.match(r"^Iter1-20260718T081500Z-[0-9a-f]{6}$", run_id)


def test_build_record_contains_all_schema_keys_and_is_json_serializable() -> None:
    """journal.md Iteration 1 で確定した JSONL スキーマの全キーを含み，JSON へ往復可能である．"""

    from datetime import datetime, timezone

    run_start = datetime(2026, 7, 18, 8, 15, 0, tzinfo=timezone.utc)
    parsed = ParsedLog(
        prompt_tokens=6,
        embed_stats={"mean": 0.01, "std": 0.98, "min": -3.2, "max": 3.4},
        step_dt=[0.8, 0.12, 0.15, 0.11, 0.2],
        output_tokens_from_log=5,
        decode_time_s=0.045,
        result_text_snippet="Hi there!",
        parse_ok=True,
        parse_warnings=[],
    )
    derived = compute_derived_metrics(parsed.step_dt, parsed.output_tokens_from_log)
    levers = {"NUM_MICRO_BATCHES": 4, "STAGGER_INTERVAL": 3.0, "SEQ_LEN": None, "WORLD_SIZE": 51}

    record = build_record(
        iter_name="Iter1", run_id="Iter1-20260718T081500Z-a1b2c3", run_start=run_start,
        prompt="Hello!", parsed=parsed, derived=derived, result_text="Hi there! How can I help you today?",
        e2e_latency_s=1.5, levers=levers,
    )

    expected_keys = {
        "schema_version", "iter", "run_id", "timestamp", "prompt", "prompt_tokens",
        "output_tokens", "step_dt", "ttft_s", "generation_time_s", "tokens_per_sec",
        "itl_p50_s", "itl_p95_s", "decode_time_s", "e2e_latency_s", "result_text",
        "embed_stats", "levers", "parse_ok", "parse_warnings",
    }
    assert set(record.keys()) == expected_keys
    assert record["timestamp"] == "2026-07-18T08:15:00Z"

    round_tripped = json.loads(json.dumps(record, ensure_ascii=False))
    assert round_tripped == record


def test_append_jsonl_creates_parent_dir_and_appends_one_valid_json_line(tmp_path: Path) -> None:
    """results/ ディレクトリが無くても作成し，1 行の妥当な JSON を追記する．"""

    results_path = tmp_path / "results" / "Iter1.jsonl"
    assert not results_path.parent.exists()

    record = {"iter": "Iter1", "run_id": "Iter1-20260718T081500Z-a1b2c3", "prompt_tokens": 6}
    append_jsonl(results_path, record)

    assert results_path.exists()
    lines = results_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_append_jsonl_appends_without_overwriting_existing_records(tmp_path: Path) -> None:
    """既存の JSONL に対しては上書きせず末尾へ追記する．"""

    results_path = tmp_path / "Iter1.jsonl"
    append_jsonl(results_path, {"run_id": "run-1"})
    append_jsonl(results_path, {"run_id": "run-2"})

    lines = results_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["run_id"] == "run-1"
    assert json.loads(lines[1])["run_id"] == "run-2"
