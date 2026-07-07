"""
Container log display tool.

Usage:
  RANK=0 uv run python tools/show_logs.py          # Logs for a specific node
  uv run python tools/show_logs.py --all            # Latest logs from all nodes
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from common import ClusterConfig, log, read_hosts

SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]


def show_single_node_logs(config: ClusterConfig) -> None:
    """Follow logs for a specific node's container."""

    rank_str = os.environ.get("RANK")
    if rank_str is None:
        log("INFO", "Usage: RANK=0 uv run python tools/show_logs.py")
        log("INFO", "       RANK=N uv run python tools/show_logs.py")
        sys.exit(1)

    rank = int(rank_str)
    hosts = read_hosts(config.hosts_file)

    if rank < 0 or rank >= len(hosts):
        log("ERROR", f"Rank {rank} is out of range (0-{len(hosts) - 1})", file=sys.stderr)
        sys.exit(1)

    ip = hosts[rank]
    log("INFO", f"Container logs for Rank {rank} ({ip})")

    # Follow log display (Ctrl+C to exit) runs directly via subprocess
    cmd = [
        "ssh", *SSH_BASE_OPTS,
        f"{config.ssh_user}@{config.master_addr}",
        "ssh", *SSH_BASE_OPTS,
        f"{config.ssh_user}@{ip}",
        "docker logs --tail 100 -f distributed-llm",
    ]
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\nLog display ended.")


def show_all_logs(config: ClusterConfig) -> None:
    """Display latest logs (last 32 lines) from all nodes at once."""

    from common import ssh_via_master

    hosts = read_hosts(config.hosts_file)

    for rank, ip in enumerate(hosts):
        log("INFO", f"Rank {rank} ({ip})")
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip,
            "docker logs --tail 32 distributed-llm 2>&1",
            timeout=15,
            extra_opts=["-o", "ConnectTimeout=5"],
        )
        if result.returncode == 0 and result.stdout:
            print(result.stdout.strip())
        else:
            print("  (connection failed or container not running)")
        print()


def main() -> None:
    """Display container logs."""

    parser = argparse.ArgumentParser(description="Container log display")
    parser.add_argument(
        "--all", action="store_true", dest="show_all",
        help="Show latest logs from all nodes",
    )
    args = parser.parse_args()

    config = ClusterConfig()

    if args.show_all:
        show_all_logs(config)
    else:
        show_single_node_logs(config)


if __name__ == "__main__":
    main()
