"""全 51 ノードの単層 local マイクロベンチ（Iter10）．

hosts.txt の全ホストに対し，layer_idx=0/23/46 の単一 decoder 層 forward を
並列 SSH 実行で測定し，結果を results/bench_all_nodes.jsonl へ追記する．

serving/relay パイプラインは一切変更しない（計測スクリプトの追加のみ）．

使い方:
    unset VIRTUAL_ENV && uv run python scripts/bench_all_nodes.py

結果フォーマット (JSONL 1 レコード):
    {
        "rank": int,
        "host": str,
        "layer_idx": int,
        "median_s": float,   # forward パス中央値時間（秒）
        "min_s": float,       # forward パス最小値時間（秒）
        "num_threads": int,
        "dtype": str,
        "torch_version": str,
        "warmup_iters": int,
        "measure_iters": int,
    }
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ====================================================================
# 定数
# ====================================================================

WARMUP_ITERS = 50
MEASURE_ITERS = 200
NUM_THREADS = 4
MEASURE_LAYERS: tuple[int, ...] = (0, 23, 46)
MAX_WORKERS = 15  # 各タスク約77秒、15並列で全体約20分を見込む

RESULTS_JSONL_PATH = Path(__file__).resolve().parent.parent / "results" / "bench_all_nodes.jsonl"


def _build_remote_script() -> str:
    """SSH 経由で各ノード上で実行するベンチスクリプトを生成する．

    bench_compute_ceiling.py の build_linear_shapes() + measure_layer_ns() を
    最小限に再実装し，引数 layer_idx で層を指定して計測する．
    """

    return (
        "import json, os, statistics, time, sys\n"
        "\n"
        "try:\n"
        "    import torch\n"
        "    from torch import nn\n"
        "except ImportError:\n"
        '    print(json.dumps({"error": "torch not available"}))\n'
        "    sys.exit(1)\n"
        "\n"
        "\n"
        "def build_linear_shapes(layer_idx):\n"
        "    try:\n"
        "        from transformers import AutoConfig\n"
        "        from transformers.models.gemma4.modeling_gemma4 import Gemma4TextDecoderLayer\n"
        "        config = AutoConfig.from_pretrained(\n"
        '            "google/gemma-4-31B-it", trust_remote_code=True\n'
        "        )\n"
        "        text_config = config.text_config if hasattr(config, 'text_config') else config\n"
        "        layer = Gemma4TextDecoderLayer(text_config, layer_idx=layer_idx)\n"
        "        layer.eval()\n"
        "        shapes = []\n"
        "        for name, module in layer.named_modules():\n"
        "            if isinstance(module, nn.Linear):\n"
        "                shapes.append({\n"
        '                    "name": name,\n'
        '                    "in_features": module.in_features,\n'
        '                    "out_features": module.out_features,\n'
        "                })\n"
        "        return shapes\n"
        "    except Exception as exc:\n"
        '        print(json.dumps({"error": f"build_linear_shapes failed: {exc}"}))\n'
        "        sys.exit(1)\n"
        "\n"
        "\n"
        "def measure_layer(layer_idx, warmup_iters, measure_iters):\n"
        "    torch.set_num_threads(%d)\n"
        "    shapes = build_linear_shapes(layer_idx)\n"
        '    if not shapes:\n'
        '        print(json.dumps({"error": "no linear layers found"}))\n'
        "        sys.exit(1)\n"
        "    total_median_ns = 0.0\n"
        '    total_min_ns = float("inf")\n'
        "    for shape_info in shapes:\n"
        "        linear = nn.Linear(\n"
        '            shape_info["in_features"],\n'
        '            shape_info["out_features"],\n'
        "            bias=False,\n"
        "            dtype=torch.float32,\n"
        "        )\n"
        "        linear.eval()\n"
        '        x = torch.randn(1, 1, shape_info["in_features"], dtype=torch.float32)\n'
        "        samples_ns = []\n"
        "        with torch.no_grad():\n"
        "            for _ in range(warmup_iters):\n"
        "                linear(x)\n"
        "            for _ in range(measure_iters):\n"
        "                start_ns = time.perf_counter_ns()\n"
        "                linear(x)\n"
        "                samples_ns.append(time.perf_counter_ns() - start_ns)\n"
        "        median_ns = float(statistics.median(samples_ns))\n"
        "        min_ns = float(min(samples_ns))\n"
        "        total_median_ns += median_ns\n"
        "        if min_ns < total_min_ns:\n"
        "            total_min_ns = min_ns\n"
        '    result = {\n'
        '        "layer_idx": layer_idx,\n'
        '        "median_s": total_median_ns * 1e-9,\n'
        '        "min_s": total_min_ns * 1e-9,\n'
        "    }\n"
        "    print(json.dumps(result))\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        '    layer_idx = int(os.environ.get("BENCH_LAYER_IDX", "0"))\n'
        "    measure_layer(layer_idx, %d, %d)\n"
    ) % (NUM_THREADS, WARMUP_ITERS, MEASURE_ITERS)


# ====================================================================
# データ型
# ====================================================================


@dataclass(frozen=True)
class NodeResult:
    """1 ノード・1 層の計測結果．"""

    rank: int
    host: str
    layer_idx: int
    median_s: float
    min_s: float
    num_threads: int = NUM_THREADS
    dtype: str = "torch.float32"
    torch_version: str = ""
    warmup_iters: int = WARMUP_ITERS
    measure_iters: int = MEASURE_ITERS


# ====================================================================
# SSH 実行
# ====================================================================


def run_on_node(
    rank: int,
    host: str,
    layer_idx: int,
    master_addr: str,
    ssh_user: str,
) -> NodeResult | None:
    """単一ノードで単層 forward ベンチを SSH 経由で実行し，結果を返す．

    wafl-ctrl1 経由の ProxyJump (ssh -J) で target_host へ接続し，
    埋め込みスクリプトを実行する．

    Args:
        rank: このノードの rank 番号（hosts.txt の行順）．
        host: ホスト名（hosts.txt のエントリ）．
        layer_idx: 測定対象の decoder 層インデックス．
        master_addr: マネジメントノード (wafl-ctrl1) のホスト名/IP．
        ssh_user: SSH ユーザー名．

    Returns:
        NodeResult に成功した結果，失敗時は None．
    """

    script = _build_remote_script()

    # リモートホスト上に一時ファイルを作成し，docker cp でコンテナにコピーして実行する．
    # 1 SSH コールで完結させる（オーバーヘッド削減）．
    import base64

    encoded = base64.b64encode(script.encode()).decode()
    tmp_file = f"/tmp/bench_node_{rank}_l{layer_idx}.py"

    # 1 SSH コール: echo | base64 -d > host_tmp → docker cp → container_exec
    cmd = (
        f"ssh -J {ssh_user}@{master_addr} "
        f"-o StrictHostKeyChecking=no "
        f"-o ConnectTimeout=10 "
        f"{ssh_user}@{host} "
        f"'echo {encoded} | base64 -d > {tmp_file} && "
        f"docker cp {tmp_file} distributed-llm:{tmp_file} && "
        f"BENCH_LAYER_IDX={layer_idx} docker exec -i distributed-llm python3 {tmp_file}'"
    )

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,  # 5 分 timeout（各ノードは独立）
        )
    except subprocess.TimeoutExpired:
        return None

    if proc.returncode != 0:
        return None

    output = proc.stdout.strip()

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None

    if "error" in data:
        return None

    return NodeResult(
        rank=rank,
        host=host,
        layer_idx=layer_idx,
        median_s=data["median_s"],
        min_s=data["min_s"],
    )


# ====================================================================
# 結果集約・出力
# ====================================================================


def collect_results(
    hosts: list[str],
    master_addr: str,
    ssh_user: str,
) -> list[NodeResult]:
    """全ノード×全測定層を並列 SSH で実行し，結果一覧を返す．

    ThreadPoolExecutor(max_workers=MAX_WORKERS) で全組み合わせを並列実行する．
    1 ノードの失敗は他ノードに影響しない（None をスキップ）．
    各タスク完了ごとに JSONL へ追記＋進捗表示する．

    Args:
        hosts: hosts.txt のホスト名リスト（行順＝rank番号）．
        master_addr: マネジメントノード．
        ssh_user: SSH ユーザー名．

    Returns:
        全 NodeResult のリスト．
    """

    tasks: list[tuple[int, str, int]] = []
    for rank, host in enumerate(hosts):
        for layer_idx in MEASURE_LAYERS:
            tasks.append((rank, host, layer_idx))

    results: list[NodeResult] = []
    failed_count = 0
    total_expected = len(hosts) * len(MEASURE_LAYERS)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_task = {
            executor.submit(run_on_node, rank, host, layer_idx, master_addr, ssh_user): (
                rank,
                host,
                layer_idx,
            )
            for rank, host, layer_idx in tasks
        }

        for future in as_completed(future_to_task):
            rank, host, layer_idx = future_to_task[future]
            try:
                result = future.result(timeout=360)
            except Exception:
                failed_count += 1
                continue

            if result is None:
                failed_count += 1
            else:
                results.append(result)
                append_jsonl(RESULTS_JSONL_PATH, [result])
                print(f"[{len(results)}/{total_expected}] rank={rank} host={host} layer={layer_idx} median_s={result.median_s:.4f}s", flush=True)

    print(f"\n完了: {len(results)}/{total_expected} レコード取得 "
          f"(失敗 {failed_count})")

    return results


def append_jsonl(path: Path, records: list[NodeResult]) -> None:
    """NodeResult のリストを JSONL ファイルへ追記する．"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as jsonl_file:
        for record in records:
            line = json.dumps(asdict(record), ensure_ascii=False)
            jsonl_file.write(line + "\n")


# ====================================================================
# hosts.txt 読み込み
# ====================================================================


def read_hosts(hosts_file: str | Path) -> list[str]:
    """hosts.txt を読み込み，空行・コメント行を除外したホスト名リストを返す．

    Args:
        hosts_file: hosts.txt のパス．

    Returns:
        ホスト名のリスト（行順＝rank番号）．
    """

    path = Path(hosts_file)
    if not path.exists():
        print(f"Error: hosts file not found: {path}", file=sys.stderr)
        sys.exit(1)

    hosts: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.split("#")[0].strip()
        if stripped:
            hosts.append(stripped)

    return hosts


# ====================================================================
# エントリポイント
# ====================================================================


def main() -> None:
    """全ノード単層マイクロベンチを実行し，結果を JSONL へ追記する．"""

    parser = argparse.ArgumentParser(
        description="全51ノードの単層localマイクロベンチ（Iter10）"
    )
    parser.add_argument(
        "--hosts",
        type=str,
        default="hosts.txt",
        help="hosts.txt のパス（既定: hosts.txt）",
    )
    parser.add_argument(
        "--master",
        type=str,
        default="wafl-ctrl1",
        help="マネージメントノードのホスト名/IP（既定: wafl-ctrl1）",
    )
    parser.add_argument(
        "--user",
        type=str,
        default="denjo",
        help="SSH ユーザー名（既定: user）",
    )
    args = parser.parse_args()

    hosts = read_hosts(args.hosts)
    print(f"hosts.txt から {len(hosts)} ホストを読み込んだ")
    print(f"測定層: {MEASURE_LAYERS}")
    print(f"並列度: max_workers={MAX_WORKERS}")
    print(f"ウォームアップ: {WARMUP_ITERS} 回, 測定: {MEASURE_ITERS} 回\n")

    start_time = time.time()
    results = collect_results(hosts, args.master, args.user)
    elapsed = time.time() - start_time

    if not results:
        print("Error: 結果が取得できなかった", file=sys.stderr)
        sys.exit(1)

    print(f"\nresults へ {len(results)} レコード追記した: {RESULTS_JSONL_PATH}")
    print(f"所要時間: {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()
