"""
分散LLM推論クラスタ ヘルスチェックスクリプト

全ノードの以下を検証する:
  1. SSH接続性
  2. Dockerデーモンの稼働状態
  3. llm-nodeコンテナの実行状態
  4. モデル重みの存在確認
  5. ネットワークMTU整合性
  6. CPU温度（サーマルスロットリング検出）

使用法:
  uv run python tools/healthcheck.py [--verbose]
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from common import (
    ClusterConfig,
    Color,
    log,
    log_fail,
    log_header,
    log_info,
    log_ok,
    log_warn,
    read_hosts,
    run_local,
    ssh_run,
    ssh_via_master,
)


def check_master(config: ClusterConfig) -> None:
    """管理サーバのチェックを実行する"""

    log_header(f"=== Management Server ({config.master_addr}) ===")

    # Dockerレジストリ（HTTP）— ローカルポートフォワード → SSH 経由
    try:
        result = run_local(
            ["curl", "-s", f"http://localhost:{config.registry_port}/v2/_catalog"],
            capture=True, check=False, timeout=10,
        )
        if result.returncode == 0 and "repositories" in result.stdout:
            log("OK", f"Docker registry (port {config.registry_port}) responding")
        else:
            # ローカルポートフォワードがない場合はSSH経由でチェック
            result = run_local(
                f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "
                f"{config.ssh_user}@{config.master_addr} "
                f"curl -s http://localhost:{config.registry_port}/v2/_catalog",
                capture=True, check=False, shell=True, timeout=10,
            )
            if result.returncode == 0 and "repositories" in (result.stdout or ""):
                log("OK", f"Docker registry (port {config.registry_port}) responding")
            else:
                log("FAIL", "Cannot connect to Docker registry")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log_fail("Cannot connect to Docker registry")

    # PyTorch分散マスターポート — SSH 経由で wafl-ctrl1 上でチェック
    result = run_local(
        f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "
        f"{config.ssh_user}@{config.master_addr} "
        f"ss -tlnp | grep ':{config.master_port}'",
        capture=True, check=False, shell=True, timeout=10,
    )
    if result.returncode == 0:
        log("OK", f"PyTorch master port ({config.master_port}) is listening")
    else:
        log("WARN", f"PyTorch master port ({config.master_port}) not listening (inference may not have started)")


def check_node(
    config: ClusterConfig,
    ip: str,
    rank: int,
    *,
    verbose: bool = False,
) -> bool:
    """
    単一ノードのヘルスチェックを実行する。

    検証項目:
      1. SSH接続性
      2. Dockerデーモンの稼働
      3. llm-nodeコンテナの状態
      4. モデル重みの存在
      5. MTU設定
      6. CPU温度（verbose時のみ）

    Returns:
        True: 全チェック正常, False: 何らかの異常あり
    """

    log_header(f"--- Rank {rank} ({ip}) ---")
    node_healthy = True

    # 1. SSH接続性
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip, "true",
        timeout=10,
        extra_opts=["-o", "ConnectTimeout=5"],
    )
    if result.returncode != 0:
        log("FAIL", "SSH connection failed")
        return False
    log("OK", "SSH connection OK")

    # 2. Dockerデーモン
    result = ssh_via_master(config.ssh_user, config.master_addr, ip, "docker info >/dev/null 2>&1")
    if result.returncode == 0:
        log("OK", "Docker daemon running")
    else:
        log("FAIL", "Docker daemon stopped")
        node_healthy = False

    # 3. llm-nodeコンテナ
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        "docker inspect -f '{{.State.Status}}' llm-node 2>/dev/null",
    )
    container_status = (result.stdout or "").strip() if result.returncode == 0 else "not_found"

    if container_status == "running":
        log("OK", "llm-node container: running")
        if verbose:
            log_result = ssh_via_master(
                config.ssh_user, config.master_addr, ip,
                "docker logs --tail 3 llm-node 2>&1",
            )
            if log_result.stdout:
                log("INFO", f"Latest logs: {log_result.stdout.strip()}")
    elif container_status == "not_found":
        log("WARN", "llm-node container: not created")
        node_healthy = False
    else:
        log("FAIL", f"llm-node container: {container_status}")
        node_healthy = False

    # 4. モデル重みの存在確認
    # Rank 0 はレイヤー担当なし（TCPStoreのみ）→ embed_tokens + lm_head のみ
    # Rank 1+ はパイプライン並列化で異なるレイヤーを持つ → layer_*.safetensors も必要
    if rank == 0:
        model_check = (
            f"test -f {config.model_mount_path}/embed_tokens.safetensors && "
            f"test -f {config.model_mount_path}/lm_head.safetensors && "
            f"echo 'OK' || echo 'MISSING'"
        )
    else:
        model_check = (
            f"test -f {config.model_mount_path}/embed_tokens.safetensors && "
            f"test -f {config.model_mount_path}/lm_head.safetensors && "
            f"ls {config.model_mount_path}/layer_*.safetensors >/dev/null 2>&1 && "
            f"echo 'OK' || echo 'MISSING'"
        )
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        model_check,
        timeout=10,
    )
    model_status = (result.stdout or "").strip()
    if model_status == "OK":
        log("OK", f"Model weights: {config.model_mount_path}")
    else:
        log("FAIL", f"Model weights not deployed: {config.model_mount_path}")
        node_healthy = False

    # 5. MTU確認
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        "ip -o link show $(ip -o -4 route show to default | awk '{print $5}') 2>/dev/null"
        " | awk -F'mtu ' '{print $2}' | awk '{print $1}'",
    )
    mtu = (result.stdout or "").strip() or "unknown"
    if mtu in ("1500", "9000"):
        log("OK", f"MTU: {mtu}")
    else:
        log("WARN", f"MTU: {mtu} (1500 or 9000 recommended)")

    # 6. CPU温度確認（verbose時のみ）
    if verbose:
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip,
            "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null",
        )
        temp_raw = (result.stdout or "").strip()
        if temp_raw.isdigit() and int(temp_raw) > 0:
            temp_c = int(temp_raw) // 1000
            if temp_c > 85:
                log("WARN", f"CPU temp: {temp_c}°C (possible thermal throttling)")
            else:
                log("OK", f"CPU temp: {temp_c}°C")

    return node_healthy


def main() -> None:
    """全ノードのヘルスチェックを実行する"""

    parser = argparse.ArgumentParser(description="Distributed LLM cluster health check")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Verbose check (logs, CPU temperature)",
    )
    args = parser.parse_args()

    config = ClusterConfig()

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log("STEP", "=" * 68)
    log("INFO", f"Distributed LLM Cluster Health Check ({now})")
    log("STEP", "=" * 68)

    # 管理サーバチェック
    check_master(config)

    # 全ノードチェック
    hosts = read_hosts(config.hosts_file)
    healthy_count = 0
    unhealthy_nodes: list[tuple[int, str]] = []

    for rank, ip in enumerate(hosts):
        if check_node(config, ip, rank, verbose=args.verbose):
            healthy_count += 1
        else:
            unhealthy_nodes.append((rank, ip))

    # サマリー
    total = len(hosts)
    unhealthy_count = len(unhealthy_nodes)

    print()
    log("STEP", "=" * 68)
    log("INFO", "Health Check Summary")
    log("STEP", "=" * 68)
    log("INFO", f"  Total nodes:  {total}")
    log("OK", f"  Healthy:      {healthy_count}")
    log("FAIL", f"  Unhealthy:    {unhealthy_count}")
    print()

    if unhealthy_nodes:
        log("FAIL", "Unhealthy nodes:")
        for rank, ip in unhealthy_nodes:
            log("FAIL", f"  Rank {rank} ({ip})")
        print()
        sys.exit(1)
    else:
        log("OK", "All nodes are healthy.")


if __name__ == "__main__":
    main()
