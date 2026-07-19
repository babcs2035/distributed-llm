"""`tools/collect_results.py` の `MICROBATCH_BENCH` パース・レコード組み立て純関数の単体テスト
（research-cycle Iter7: `NUM_MICRO_BATCHES` のスループット感度分析．journal.md Iteration 7 参照）．

クラスタ・SSH 接続は一切行わない（`tests/fixtures/microbatch_bench_sample.log` の固定ログのみを入力とする）．
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from collect_results import (
    MicrobatchBenchRecord,
    build_microbatch_bench_record,
    make_run_id,
    parse_microbatch_bench_log,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "microbatch_bench_sample.log"


def _load_fixture_text() -> str:
    """最終 rank（R50）の RESULT 行 3 件（ANSI 混入 2 件・非混入 1 件）を含むログフィクスチャを読み込む．"""

    return _FIXTURE_PATH.read_text(encoding="utf-8")


# ====================================================================
# parse_microbatch_bench_log
# ====================================================================


def test_parse_microbatch_bench_log_extracts_all_measurement_windows_in_order() -> None:
    """3 回分の計測窓（repeat）を出現順にすべて抽出し，他 rank・非 RESULT 行は無視する．"""

    records = parse_microbatch_bench_log(_load_fixture_text())

    assert len(records) == 3
    assert all(isinstance(record, MicrobatchBenchRecord) for record in records)
    assert [record.rank for record in records] == [50, 50, 50]


def test_parse_microbatch_bench_log_extracts_correct_fields_for_first_window() -> None:
    """1 件目の計測窓（ANSI カラーコード混入）の全フィールドが正しく数値化される．"""

    records = parse_microbatch_bench_log(_load_fixture_text())
    first = records[0]

    assert first.rank == 50
    assert first.num_micro_batches == 51
    assert first.world_size == 51
    assert first.warmup_steps == 20
    assert first.measure_steps == 100
    assert first.elapsed_s == 12.3456
    assert first.steps_per_s == 8.1000
    assert first.microbatch_per_s == 413.1000


def test_parse_microbatch_bench_log_handles_ansi_free_result_line() -> None:
    """カラー無効環境（`_Color.disable()`）で出力された ANSI 無しの RESULT 行も同様に抽出できる．"""

    records = parse_microbatch_bench_log(_load_fixture_text())
    last = records[2]

    assert last.elapsed_s == 12.2000
    assert last.microbatch_per_s == 418.0328


def test_parse_microbatch_bench_log_returns_empty_list_when_no_result_lines() -> None:
    """`MICROBATCH_BENCH` RESULT 行が無いログ（bench 無効時の通常ログ）では空リストを返す．"""

    log_text = "[R0 INFO] Rank 0: prompt='Hi'\n[R0 RESULT] Request response: 'Hello'\n"

    assert parse_microbatch_bench_log(log_text) == []


# ====================================================================
# build_microbatch_bench_record
# ====================================================================


def test_build_microbatch_bench_record_includes_all_required_fields() -> None:
    """journal.md Iteration 7「計画」§2 が指定した必須フィールドを過不足なく含む．"""

    bench = MicrobatchBenchRecord(
        rank=50, num_micro_batches=51, world_size=51, warmup_steps=20, measure_steps=100,
        elapsed_s=12.3456, steps_per_s=8.1000, microbatch_per_s=413.1000,
    )
    run_start = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    run_id = make_run_id("Iter7", run_start)

    record = build_microbatch_bench_record(
        iter_name="Iter7", run_id=run_id, run_start=run_start, bench=bench,
    )

    assert record["record_type"] == "microbatch_bench"
    assert record["iter"] == "Iter7"
    assert record["run_id"] == run_id
    assert record["timestamp"] == "2026-07-19T12:00:00Z"
    assert record["rank"] == 50
    assert record["num_micro_batches"] == 51
    assert record["world_size"] == 51
    assert record["warmup_steps"] == 20
    assert record["measure_steps"] == 100
    assert record["elapsed_s"] == 12.3456
    assert record["steps_per_s"] == 8.1000
    assert record["microbatch_per_s"] == 413.1000
