"""`scripts/bench_all_nodes.py` の純関数に対する単体テスト．

実 SSH・実通信は行わない（subprocess.run を mock する）．
クラスタ接続は不要．
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_SCRIPTS_DIR))

from bench_all_nodes import (  # noqa: E402 -- sys.path 設定後に import
    MEASURE_ITERS,
    MEASURE_LAYERS,
    NUM_THREADS,
    WARMUP_ITERS,
    NodeResult,
    append_jsonl,
    read_hosts,
    run_on_node,
)


# ====================================================================
# read_hosts
# ====================================================================


def test_read_hosts_parses_simple_hosts_file(tmp_path: Path) -> None:
    """hosts.txt から空行・コメント行を除外したホスト名リストが得られる．"""

    hosts_file = tmp_path / "hosts.txt"
    hosts_file.write_text(
        "wafl-ctrl1\n"
        "\n"
        "# comment line\n"
        "wafl100\n"
        "  wafl101  # inline comment\n"
        "\n",
        encoding="utf-8",
    )

    result = read_hosts(hosts_file)

    assert result == ["wafl-ctrl1", "wafl100", "wafl101"]


def test_read_hosts_raises_on_missing_file(tmp_path: Path) -> None:
    """存在しない hosts.txt に対しては sys.exit(1) を呼ぶ．"""

    missing = tmp_path / "nonexistent.txt"

    with pytest.raises(SystemExit):
        read_hosts(missing)


# ====================================================================
# NodeResult
# ====================================================================


def test_node_result_defaults_fields() -> None:
    """NodeResult の既定値が定数と一致すること．"""

    result = NodeResult(rank=0, host="test", layer_idx=5, median_s=1.0, min_s=0.5)

    assert result.num_threads == NUM_THREADS
    assert result.dtype == "torch.float32"
    assert result.warmup_iters == WARMUP_ITERS
    assert result.measure_iters == MEASURE_ITERS


def test_node_result_asdict_contains_all_fields() -> None:
    """asdict() でシリアライズ可能な全フィールドが含まれる．"""

    from dataclasses import asdict

    result = NodeResult(rank=10, host="wafl113", layer_idx=23, median_s=0.5, min_s=0.3)
    d = asdict(result)

    assert d["rank"] == 10
    assert d["host"] == "wafl113"
    assert d["layer_idx"] == 23
    assert d["median_s"] == 0.5
    assert d["min_s"] == 0.3
    assert "num_threads" in d


# ====================================================================
# run_on_node (mock subprocess.run)
# ====================================================================


def test_run_on_node_success_parses_json_result() -> None:
    """ベンチマークが成功したとき，NodeResult が正しく構築される．"""

    mock_subprocess = MagicMock()
    mock_subprocess.return_value = MagicMock(
        returncode=0, stdout='{"layer_idx": 23, "median_s": 0.15, "min_s": 0.1}',
    )

    with patch("bench_all_nodes.subprocess.run", mock_subprocess):
        result = run_on_node(rank=14, host="wafl113", layer_idx=23, master_addr="ctrl", ssh_user="user")

    assert result is not None
    assert result.rank == 14
    assert result.host == "wafl113"
    assert result.layer_idx == 23
    assert result.median_s == 0.15
    assert result.min_s == 0.1


def test_run_on_node_failure_returns_none() -> None:
    """ベンチマークが失敗（returncode != 0）したときは None を返す．"""

    mock_subprocess = MagicMock()
    mock_subprocess.return_value = MagicMock(returncode=1, stderr="error")

    with patch("bench_all_nodes.subprocess.run", mock_subprocess):
        result = run_on_node(rank=0, host="wafl-ctrl1", layer_idx=0, master_addr="ctrl", ssh_user="user")

    assert result is None


def test_run_on_node_json_decode_error_returns_none() -> None:
    """ベンチマークの出力が JSON でない場合は None を返す．"""

    mock_subprocess = MagicMock()
    mock_subprocess.return_value = MagicMock(returncode=0, stdout="not json output")

    with patch("bench_all_nodes.subprocess.run", mock_subprocess):
        result = run_on_node(rank=1, host="wafl100", layer_idx=0, master_addr="ctrl", ssh_user="user")

    assert result is None


def test_run_on_node_error_field_returns_none() -> None:
    """ベンチマークが error フィールドを含む JSON を返した場合は None．"""

    mock_subprocess = MagicMock()
    mock_subprocess.return_value = MagicMock(
        returncode=0,
        stdout='{"error": "build_linear_shapes failed: something went wrong"}',
    )

    with patch("bench_all_nodes.subprocess.run", mock_subprocess):
        result = run_on_node(rank=5, host="wafl105", layer_idx=46, master_addr="ctrl", ssh_user="user")

    assert result is None


def test_run_on_node_timeout_returns_none() -> None:
    """subprocess.TimeoutExpired の発生時は None を返す．"""

    with patch("bench_all_nodes.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 300)):
        result = run_on_node(rank=1, host="wafl100", layer_idx=0, master_addr="ctrl", ssh_user="user")

    assert result is None


# ====================================================================
# append_jsonl
# ====================================================================


def test_append_jsonl_creates_file_and_appends(tmp_path: Path) -> None:
    """存在しないパスへ NodeResult のリストを追記できる．"""

    jsonl_path = tmp_path / "results" / "bench_all_nodes.jsonl"
    records = [
        NodeResult(rank=0, host="wafl-ctrl1", layer_idx=0, median_s=0.01, min_s=0.005),
        NodeResult(rank=14, host="wafl113", layer_idx=23, median_s=0.15, min_s=0.1),
    ]

    append_jsonl(jsonl_path, records)

    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 2

    for line in lines:
        data = json.loads(line)
        assert "rank" in data
        assert "host" in data
        assert "layer_idx" in data
        assert "median_s" in data
        assert "min_s" in data


def test_append_jsonl_appends_to_existing_file(tmp_path: Path) -> None:
    """既存の JSONL ファイルへ追記できる（上書きしない）．"""

    jsonl_path = tmp_path / "bench_all_nodes.jsonl"
    # まず 1 レコード書き込み
    records_a = [
        NodeResult(rank=0, host="wafl-ctrl1", layer_idx=0, median_s=0.01, min_s=0.005),
    ]
    append_jsonl(jsonl_path, records_a)

    # さらに 1 レコード追記
    records_b = [
        NodeResult(rank=14, host="wafl113", layer_idx=23, median_s=0.15, min_s=0.1),
    ]
    append_jsonl(jsonl_path, records_b)

    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 2


# ====================================================================
# MEASURE_LAYERS 不変性
# ====================================================================


def test_measure_layers_contains_expected_indices() -> None:
    """測定対象層が (0, 23, 46) の 3 種である．"""

    assert MEASURE_LAYERS == (0, 23, 46)
    assert len(MEASURE_LAYERS) == 3


def test_constants_match_bench_compute_ceiling_defaults() -> None:
    """ベンチ定数が bench_compute_ceiling.py の既定値と整合すること．"""

    assert NUM_THREADS == 4
    assert WARMUP_ITERS == 50
    assert MEASURE_ITERS == 200
