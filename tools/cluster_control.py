"""
クラスタ制御（停止・再起動・クリーンアップ）

使用法:
  uv run python tools/cluster_control.py stop     # 全ノード停止
  uv run python tools/cluster_control.py restart  # 全ノード再起動
  uv run python tools/cluster_control.py clean    # 全ノードクリーンアップ
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys

from common import ClusterConfig, log, read_hosts, ssh_run, ssh_via_master


def _stop_node(config: ClusterConfig, rank: int, ip: str) -> None:
    """単一ノードのコンテナを停止する"""

    log("INFO", f"Rank {rank} ({ip}): stopping...")
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        "docker stop llm-node >/dev/null 2>&1 && echo 'Stopped' || echo '(not running)'",
        timeout=30,
    )
    output = (result.stdout or "").strip()
    if output:
        log("INFO", output)


def stop_all(config: ClusterConfig) -> None:
    """全ノードの推論コンテナを並列停止する"""

    log("INFO", "Stopping llm-node containers on all nodes...")

    hosts = read_hosts(config.hosts_file)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda p: _stop_node(config, p[0], p[1]), enumerate(hosts)))

    log("INFO", "All nodes stopped.")


def _restart_node(config: ClusterConfig, rank: int, ip: str) -> None:
    """単一ノードのコンテナを再起動する"""

    log("INFO", f"Rank {rank} ({ip}): restarting...")
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        "docker restart llm-node 2>&1 && echo 'Restarted' || echo '(restart failed)'",
        timeout=30,
    )
    output = (result.stdout or "").strip()
    if output:
        log("INFO", output)


def restart_all(config: ClusterConfig) -> None:
    """全ノードの推論コンテナを並列再起動する"""

    log("INFO", "Restarting llm-node containers on all nodes...")

    hosts = read_hosts(config.hosts_file)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda p: _restart_node(config, p[0], p[1]), enumerate(hosts)))

    log("INFO", "All nodes restarted.")


def _clean_node(config: ClusterConfig, rank: int, ip: str, target_image: str) -> None:
    """単一ノードのコンテナとイメージを削除する"""

    log("INFO", f"Rank {rank} ({ip}): cleaning up...")
    ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        f"docker stop llm-node >/dev/null 2>&1 || true; "
        f"docker rm llm-node >/dev/null 2>&1 || true; "
        f"docker rmi {target_image} >/dev/null 2>&1 || true; "
        f"echo 'Cleaned'",
        timeout=60,
    )


def clean_all(config: ClusterConfig) -> None:
    """全ノードのコンテナとイメージを並列削除する"""

    log("INFO", "Removing llm-node containers and images on all nodes...")

    confirm = input("Are you sure? [y/N] ")
    if confirm.lower() != "y":
        log("INFO", "Cancelled.")
        return

    hosts = read_hosts(config.hosts_file)
    target_image = config.target_image

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda p: _clean_node(config, p[0], p[1], target_image), enumerate(hosts)))

    log("INFO", "All nodes cleaned up.")


def main() -> None:
    """コンテナの停止・再起動・クリーンアップを実行する"""

    parser = argparse.ArgumentParser(description="Cluster control")
    parser.add_argument(
        "action",
        choices=["stop", "restart", "clean"],
        help="Action to perform",
    )
    args = parser.parse_args()

    config = ClusterConfig()

    actions = {
        "stop": stop_all,
        "restart": restart_all,
        "clean": clean_all,
    }
    actions[args.action](config)


if __name__ == "__main__":
    main()
