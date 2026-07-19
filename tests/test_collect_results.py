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
    _COMPUTE_DT_RE,
    _RECV_HIDDEN_DT_RE,
    _SENT_TO_NEXT_DT_RE,
    _extract_levers,
    _extract_rank0_messages,
    _extract_result_text,
    _percentile,
    _select_relevant_block,
    aggregate_stage_timing,
    build_levers,
    build_record,
    build_timing_breakdown,
    append_jsonl,
    compute_derived_metrics,
    make_run_id,
    parse_node_stage_timing,
    parse_rank0_log,
    DerivedMetrics,
    NodeStageTiming,
    ParsedLog,
    StageTimingSummary,
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
# 複数行 RESULT 対応（Iteration 2: T1〜T6）
# ====================================================================


def test_extract_rank0_messages_joins_continuation_lines_into_one_record() -> None:
    """T1: プレフィックス行＋継続行（プレフィックス無し）が 1 論理レコードへ `\n` 連結される．

    先頭プレフィックスより前に現れる行（継続先レコードが無い状態の継続行）は捨てられる．
    """

    log_text = (
        "orphan continuation line before any prefix\n"
        "[R0 RESULT] Request response: 'Hello! How can I help you today?\n"
        "thought\n"
        "'\n"
    )

    messages = _extract_rank0_messages(log_text)

    assert messages == [
        "Request response: 'Hello! How can I help you today?\nthought\n'"
    ]


def test_extract_result_text_restores_multiline_body_and_strips_closing_quote() -> None:
    """T2: 複数行本文（例: Iteration 1 の実観測に類似したケース）を先頭行だけでなく全文で復元する．"""

    block = [
        "Rank 0: prompt='Hello!'",
        "Request response: 'Hello! How can I help you today?\nthought\n'",
    ]

    assert _extract_result_text(block) == "Hello! How can I help you today?\nthought\n"


def test_extract_result_text_does_not_break_on_apostrophe_inside_multiline_body() -> None:
    """T2 補足: 応答本文に `'`（例: `I'm`）が含まれても，DOTALL greedy マッチが途中で切れない（回帰）．"""

    block = ["Request response: 'I'm fine\nthanks\nfor asking'"]

    assert _extract_result_text(block) == "I'm fine\nthanks\nfor asking"


def test_parse_rank0_log_multiline_result_matches_without_fallback_warning() -> None:
    """T3: Iteration 1 で実際に観測された複数行 RESULT を再現し，フォールバック警告が出ないことを確認する．

    修正前は `_extract_rank0_messages` が継続行（`thought`）を捨て，`_select_relevant_block` の
    `==` 照合が改行差で必ず失敗してフォールバック警告
    （`no block's RESULT text matched the predict result prefix; used the latest block as a fallback`）
    を出していた（journal.md Iteration 1「実験」参照）．この回帰が再発しないことを守る．
    """

    log_text = (
        "[R0 INFO] Rank 0: prompt='Hello!'\n"
        "[R0 INFO] Rank 0: prompt tokens=15, embedding shape=torch.Size([1, 15, 4096]) "
        "mean=0.004217 std=1.108908 min=-15.312500 max=16.875000\n"
        "[R0 INFO] Rank 0: step 0 done token=101 dt=26.012s\n"
        "[R0 INFO] Rank 0: step 1 done token=102 dt=7.015s\n"
        "[R0 INFO] Rank 0: decoding 15 generated tokens (prompt=15)...\n"
        "[R0 INFO] Rank 0: decoded in 0.000s: 'ok'\n"
        "[R0 RESULT] Request response: 'Hello! How can I help you today?\n"
        "thought\n"
        "'\n"
    )
    # send_prompt_ssh（predict.py）は .stdout.strip() 済みの戻り値を返す（末尾改行無し）．
    predict_result = "Hello! How can I help you today?\nthought"

    parsed = parse_rank0_log(log_text, predict_result=predict_result)

    assert parsed.parse_ok is True
    assert parsed.parse_warnings == []
    assert parsed.result_text_snippet == "Hello! How can I help you today?\nthought\n"


def test_select_relevant_block_matches_both_ssh_stripped_and_http_unstripped_predict_result() -> None:
    """T4: SSH 経路（strip 済み）と HTTP 経路（末尾改行未 strip）の両方で照合が成功する．"""

    block = [
        "Rank 0: prompt='Hello!'",
        "Request response: 'Hello! How can I help you today?\nthought\n'",
    ]

    ssh_style_result = "Hello! How can I help you today?\nthought"  # strip 済み
    http_style_result = "Hello! How can I help you today?\nthought\n"  # 末尾改行未 strip

    ssh_block, ssh_warnings = _select_relevant_block([block], predict_result=ssh_style_result)
    http_block, http_warnings = _select_relevant_block([block], predict_result=http_style_result)

    assert ssh_block == block and ssh_warnings == []
    assert http_block == block and http_warnings == []


def test_select_relevant_block_picks_earlier_block_when_correct_block_is_not_latest() -> None:
    """T5: 正しいブロックが「最新」ではない順序で並んでいても，取り違えずに選択される．

    ②（レバー掃引で複数 run が同一コンテナに連続する）で別 run 指標を誤レバーに紐付ける
    リスクが無いことの検証．
    """

    matching_older_block = [
        "Rank 0: prompt='Hi'",
        "Rank 0: step 0 done token=1 dt=0.1s",
        "Request response: 'Hello! How can I help you today?\nthought\n'",
    ]
    unrelated_newer_block = [
        "Rank 0: prompt='Something else'",
        "Rank 0: step 0 done token=9 dt=0.2s",
        "Request response: 'Different response\n'",
    ]
    predict_result = "Hello! How can I help you today?\nthought"

    block, warnings = _select_relevant_block(
        [matching_older_block, unrelated_newer_block], predict_result=predict_result,
    )

    assert block == matching_older_block
    assert any("used an earlier block" in w for w in warnings)


def test_select_relevant_block_empty_snippet_guard_does_not_vacuously_match_latest() -> None:
    """T6: 空スニペット（RESULT が空文字）に対する防御ガードが機能し，誤って前方一致しない．

    ガードが無い場合，空文字は任意の文字列の `startswith` として常に真になり，latest ブロック
    （RESULT 空）が「一致ブロック」として即座に採用されてしまう（正しいブロックを取り違える）．
    """

    matching_older_block = [
        "Rank 0: prompt='A'",
        "Request response: 'Hello'",
    ]
    empty_result_latest_block = [
        "Rank 0: prompt='B'",
        "Request response: ''",
    ]

    block, warnings = _select_relevant_block(
        [matching_older_block, empty_result_latest_block], predict_result="Hello",
    )

    assert block == matching_older_block
    assert any("used an earlier block" in w for w in warnings)


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
# levers 記録の堅牢化（Iteration 3: TL1〜TL8）
# ログ由来の levers（`Rank 0: levers NUM_MICRO_BATCHES=... ...` 行）を env より優先する．
# ====================================================================


def test_extract_levers_parses_typed_values() -> None:
    """TL1: levers 行から NUM_MICRO_BATCHES/WORLD_SIZE を int，STAGGER_INTERVAL/SEQ_LEN を数値として抽出する．"""

    block = [
        "Rank 0: prompt='Hi'",
        "Rank 0: levers NUM_MICRO_BATCHES=8 STAGGER_INTERVAL=0.5 SEQ_LEN=512 WORLD_SIZE=21",
    ]

    levers = _extract_levers(block)

    assert levers == {
        "NUM_MICRO_BATCHES": 8,
        "STAGGER_INTERVAL": 0.5,
        "SEQ_LEN": 512,
        "WORLD_SIZE": 21,
    }
    assert isinstance(levers["NUM_MICRO_BATCHES"], int)
    assert isinstance(levers["STAGGER_INTERVAL"], float)
    assert isinstance(levers["SEQ_LEN"], int)
    assert isinstance(levers["WORLD_SIZE"], int)


def test_extract_levers_returns_none_when_line_absent() -> None:
    """TL2: levers 行を含まない（旧形式の）ブロックでは None を返す．"""

    block = ["Rank 0: prompt='Hi'", "Rank 0: step 0 done token=1 dt=0.1s"]

    assert _extract_levers(block) is None


def test_extract_levers_handles_stagger_zero_and_default() -> None:
    """TL3: STAGGER_INTERVAL=0.0（掃引の最小値）と 3.0（既定値）がいずれも float として拾える．"""

    zero_block = [
        "Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=0.0 SEQ_LEN=1 WORLD_SIZE=51",
    ]
    default_block = [
        "Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=3.0 SEQ_LEN=1 WORLD_SIZE=51",
    ]

    assert _extract_levers(zero_block)["STAGGER_INTERVAL"] == pytest.approx(0.0)
    assert _extract_levers(default_block)["STAGGER_INTERVAL"] == pytest.approx(3.0)


def test_parse_rank0_log_populates_levers_from_log() -> None:
    """TL4: 物理ログ（`[R0 INFO]` プレフィックス付き）全体から levers_from_log が正しく設定される．"""

    log_text = (
        "[R0 INFO] Rank 0: prompt='Hi'\n"
        "[R0 INFO] Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=0.5 SEQ_LEN=256 WORLD_SIZE=11\n"
        "[R0 INFO] Rank 0: prompt tokens=6, embedding shape=(1,6,3) mean=0.01 std=0.98 min=-3.2 max=3.4\n"
        "[R0 INFO] Rank 0: step 0 done token=1 dt=0.1s\n"
    )

    parsed = parse_rank0_log(log_text)

    assert parsed.levers_from_log == {
        "NUM_MICRO_BATCHES": 4,
        "STAGGER_INTERVAL": 0.5,
        "SEQ_LEN": 256,
        "WORLD_SIZE": 11,
    }
    assert parsed.parse_ok is True


def test_parse_rank0_log_levers_from_log_none_for_legacy_log() -> None:
    """TL5: levers 行の無い旧形式ログでは levers_from_log が None のまま（後方互換）．"""

    log_text = (
        "[R0 INFO] Rank 0: prompt='Hi'\n"
        "[R0 INFO] Rank 0: step 0 done token=1 dt=0.1s\n"
    )

    parsed = parse_rank0_log(log_text)

    assert parsed.levers_from_log is None


def test_build_levers_prefers_log_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """TL6（本レバーの核心）: env と食い違っても，levers_from_log が与えられればそちらを採用する．"""

    monkeypatch.setenv("SEQ_LEN", "1024")
    fake_config = SimpleNamespace(num_micro_batches="2", stagger_interval="1.0", world_size="51")
    levers_from_log: dict[str, int | float | None] = {
        "NUM_MICRO_BATCHES": 8,
        "STAGGER_INTERVAL": 0.5,
        "SEQ_LEN": 256,
        "WORLD_SIZE": 21,
    }

    levers = build_levers(fake_config, levers_from_log)  # type: ignore[arg-type]

    assert levers == levers_from_log


def test_build_levers_falls_back_to_env_when_log_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """TL7: levers_from_log が None（旧ログ・パース失敗）の場合は従来の env/config フォールバックを使う．"""

    monkeypatch.delenv("SEQ_LEN", raising=False)
    fake_config = SimpleNamespace(num_micro_batches="4", stagger_interval="3.0", world_size="51")

    levers = build_levers(fake_config, None)  # type: ignore[arg-type]

    assert levers == {
        "NUM_MICRO_BATCHES": 4,
        "STAGGER_INTERVAL": 3.0,
        "SEQ_LEN": None,
        "WORLD_SIZE": 51,
    }


def test_levers_bound_to_selected_block_in_multiblock_log() -> None:
    """TL8: 2 ブロックが存在する状況で，predict_result が一致する（最新でない）ブロックの
    levers に紐づき，別ブロックの levers と混同しない（Iteration 2 の T5 と対になる levers 版）．
    """

    log_text = (
        "[R0 INFO] Rank 0: prompt='Older'\n"
        "[R0 INFO] Rank 0: levers NUM_MICRO_BATCHES=2 STAGGER_INTERVAL=0.0 SEQ_LEN=256 WORLD_SIZE=11\n"
        "[R0 INFO] Request response: 'older-result'\n"
        "[R0 INFO] Rank 0: prompt='Newer'\n"
        "[R0 INFO] Rank 0: levers NUM_MICRO_BATCHES=8 STAGGER_INTERVAL=1.0 SEQ_LEN=1024 WORLD_SIZE=51\n"
        "[R0 INFO] Request response: 'newer-result'\n"
    )

    parsed = parse_rank0_log(log_text, predict_result="older-result")

    assert parsed.levers_from_log == {
        "NUM_MICRO_BATCHES": 2,
        "STAGGER_INTERVAL": 0.0,
        "SEQ_LEN": 256,
        "WORLD_SIZE": 11,
    }


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
        "embed_stats", "levers", "stage_timing", "timing_breakdown", "parse_ok",
        "parse_warnings",
    }
    assert set(record.keys()) == expected_keys
    assert record["timestamp"] == "2026-07-18T08:15:00Z"
    # `--stage-timing` 未指定（既定）では stage_timing/timing_breakdown は null（Iteration 1〜3 の
    # v1 レコードとの後方互換．journal.md Iteration 4「計画」§2-C 参照）．
    assert record["stage_timing"] is None
    assert record["timing_breakdown"] is None

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


# ====================================================================
# per-stage 時間内訳（Iteration 4: TS1〜TS6）
# `--stage-timing` で非 rank0 全ノードから収集する compute/recv/send dt のパース・集約・残差計算．
# ====================================================================


def test_compute_dt_regex_extracts_rank_step_and_seconds() -> None:
    """TS1: `_COMPUTE_DT_RE` が行末に hidden_mean=... が続く行から (rank, step, dt) を取り出す．"""

    match = _COMPUTE_DT_RE.match("Rank 7: step 3 compute dt=0.123s hidden_mean=0.01 hidden_std=0.02")

    assert match is not None
    assert match.group(1) == "7"
    assert match.group(2) == "3"
    assert match.group(3) == "0.123"


def test_recv_hidden_dt_regex_matches_only_dedicated_line() -> None:
    """TS2: `_RECV_HIDDEN_DT_RE` は step0 の recv_hidden 行にのみ一致し，compute dt 行には一致しない．"""

    recv_match = _RECV_HIDDEN_DT_RE.match("Rank 7: recv_hidden dt=1.234s")
    assert recv_match is not None
    assert recv_match.group(1) == "7"
    assert recv_match.group(2) == "1.234"

    # step>0 の行（recv_hidden 行を持たない compute dt 行）ではマッチしない．
    assert _RECV_HIDDEN_DT_RE.match("Rank 7: step 3 compute dt=0.123s hidden_mean=0.01") is None


def test_sent_to_next_dt_regex_does_not_collide_with_compute_dt_line() -> None:
    """TS3: `_SENT_TO_NEXT_DT_RE` が sent to next 行を取り出し，compute dt 行とは誤マッチしない．"""

    sent_match = _SENT_TO_NEXT_DT_RE.match("Rank 7: step 3 sent to next dt=0.456s")
    assert sent_match is not None
    assert sent_match.group(1) == "7"
    assert sent_match.group(2) == "3"
    assert sent_match.group(3) == "0.456"

    assert _SENT_TO_NEXT_DT_RE.match(
        "Rank 7: step 3 compute dt=0.123s hidden_mean=0.01 hidden_std=0.02"
    ) is None


def test_parse_node_stage_timing_builds_timing_in_milliseconds_from_physical_log() -> None:
    """TS4: `[R7 INFO]` 形式の 1 ノードログ全体から NodeStageTiming を構築し，単位が ms（秒×1000）になる．"""

    log_text = (
        "[R7 INFO] Rank 7: recv_hidden dt=1.234s\n"
        "[R7 INFO] Rank 7: step 0 compute dt=0.100s hidden_mean=0.01 hidden_std=0.02\n"
        "[R7 INFO] Rank 7: step 0 sent to next dt=0.150s\n"
        "[R7 INFO] Rank 7: step 1 compute dt=0.110s hidden_mean=0.01 hidden_std=0.02\n"
        "[R7 INFO] Rank 7: step 1 sent to next dt=0.160s\n"
    )

    timing = parse_node_stage_timing(log_text)

    assert timing.rank == 7
    assert timing.compute_dt_ms_by_step == {0: pytest.approx(100.0), 1: pytest.approx(110.0)}
    assert timing.recv_hidden_dt_ms_step0 == pytest.approx(1234.0)
    assert timing.sent_to_next_dt_ms_by_step == {0: pytest.approx(150.0), 1: pytest.approx(160.0)}


def test_aggregate_stage_timing_sums_by_step_and_excludes_final_rank_from_send() -> None:
    """TS5: 複数ノードの集約で compute_sum_ms_by_step/send_sum_ms_by_step が step 別に加算され，
    sent_to_next を持たない最終 rank は送信総和に含まれない．
    """

    intermediate_rank_1 = NodeStageTiming(
        rank=1,
        compute_dt_ms_by_step={1: 100.0, 2: 110.0},
        recv_hidden_dt_ms_step0=None,
        sent_to_next_dt_ms_by_step={1: 150.0, 2: 160.0},  # send = 50.0, 50.0
    )
    intermediate_rank_2 = NodeStageTiming(
        rank=2,
        compute_dt_ms_by_step={1: 80.0, 2: 90.0},
        recv_hidden_dt_ms_step0=None,
        sent_to_next_dt_ms_by_step={1: 120.0, 2: 130.0},  # send = 40.0, 40.0
    )
    final_rank = NodeStageTiming(
        rank=3,
        compute_dt_ms_by_step={1: 60.0, 2: 70.0},
        recv_hidden_dt_ms_step0=None,
        sent_to_next_dt_ms_by_step={},  # 最終 rank は send を持たない
    )

    summary, warnings = aggregate_stage_timing([intermediate_rank_1, intermediate_rank_2, final_rank])

    assert warnings == []
    assert summary.n_ranks_reporting == 3
    assert summary.compute_sum_ms_by_step == {1: pytest.approx(240.0), 2: pytest.approx(270.0)}
    assert summary.send_sum_ms_by_step == {1: pytest.approx(90.0), 2: pytest.approx(90.0)}


def test_aggregate_stage_timing_excludes_negative_send_delta_with_warning() -> None:
    """TS5 補足: sent_to_next − compute が負になるケースは 0 クランプせず除外し，warning を積む．"""

    corrupted_rank = NodeStageTiming(
        rank=9,
        compute_dt_ms_by_step={1: 200.0},
        recv_hidden_dt_ms_step0=None,
        sent_to_next_dt_ms_by_step={1: 150.0},  # sent(150) < compute(200) => 負の差分
    )

    summary, warnings = aggregate_stage_timing([corrupted_rank])

    assert summary.send_sum_ms_by_step == {}
    assert any("negative send dt" in w for w in warnings)


def test_build_timing_breakdown_residual_reconciles_with_rank0_step_dt() -> None:
    """TS6: compute_sum + send_sum + residual が rank0_step_dt_median_ms と一致する（丸め許容）．"""

    summary = StageTimingSummary(
        n_ranks_reporting=2,
        compute_sum_ms_by_step={1: 6000.0, 2: 6100.0, 3: 5900.0},
        send_sum_ms_by_step={1: 500.0, 2: 480.0, 3: 520.0},
        compute_sum_ms_median=6000.0,
        send_sum_ms_median=500.0,
        prefill_recv_ms_by_rank={},
    )
    # step_dt[0] は prefill（step0），step_dt[1:] がデコードステップ．中央値は 7.0s。
    step_dt = [80.0, 6.9, 7.0, 7.1]

    breakdown = build_timing_breakdown(step_dt, summary)

    assert breakdown["compute_sum_ms_median"] == pytest.approx(6000.0)
    assert breakdown["send_sum_ms_median"] == pytest.approx(500.0)
    assert breakdown["rank0_step_dt_median_ms"] == pytest.approx(7000.0)
    assert breakdown["residual_ms_median"] == pytest.approx(500.0)
    assert breakdown["n_ranks_reporting"] == 2

    reconciled = (
        breakdown["compute_sum_ms_median"] + breakdown["send_sum_ms_median"] + breakdown["residual_ms_median"]
    )
    assert reconciled == pytest.approx(breakdown["rank0_step_dt_median_ms"])
