"""
Cluster control (stop, restart, cleanup).

Usage:
  uv run python tools/cluster_control.py stop     # Stop all nodes
  uv run python tools/cluster_control.py restart  # Restart all nodes
  uv run python tools/cluster_control.py clean    # Clean up all nodes
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys

from common import ClusterConfig, log, read_hosts, ssh_run, ssh_via_master


def _stop_node(config: ClusterConfig, rank: int, ip: str) -> None:
    """Stop containers on a single node."""

    log("INFO", f"Rank {rank} ({ip}): stopping...")
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        "docker stop distributed-llm >/dev/null 2>&1 && echo 'Stopped' || echo '(not running)'",
        timeout=30,
    )
    output = (result.stdout or "").strip()
    if output:
        log("INFO", output)


def stop_all(config: ClusterConfig) -> None:
    """Stop inference containers on all nodes in parallel."""

    log("INFO", "Stopping distributed-llm containers on all nodes...")

    hosts = read_hosts(config.hosts_file)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda p: _stop_node(config, p[0], p[1]), enumerate(hosts)))

    log("INFO", "All nodes stopped.")


def _restart_node(config: ClusterConfig, rank: int, ip: str) -> None:
    """Restart containers on a single node."""

    log("INFO", f"Rank {rank} ({ip}): restarting...")
    result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        "docker restart distributed-llm 2>&1 && echo 'Restarted' || echo '(restart failed)'",
        timeout=30,
    )
    output = (result.stdout or "").strip()
    if output:
        log("INFO", output)


def restart_all(config: ClusterConfig) -> None:
    """Restart inference containers on all nodes in parallel."""

    log("INFO", "Restarting distributed-llm containers on all nodes...")

    hosts = read_hosts(config.hosts_file)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda p: _restart_node(config, p[0], p[1]), enumerate(hosts)))

    log("INFO", "All nodes restarted.")


def _clean_node(config: ClusterConfig, rank: int, ip: str, target_image: str) -> None:
    """Remove containers and images from a single node."""

    log("INFO", f"Rank {rank} ({ip}): cleaning up...")
    ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        f"docker stop distributed-llm >/dev/null 2>&1 || true; "
        f"docker rm distributed-llm >/dev/null 2>&1 || true; "
        f"docker rmi {target_image} >/dev/null 2>&1 || true; "
        f"echo 'Cleaned'",
        timeout=60,
    )


def clean_all(config: ClusterConfig, force: bool = False) -> None:
    """Remove containers and images from all nodes in parallel."""

    log("INFO", "Removing distributed-llm containers and images on all nodes...")

    if not force:
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
    """Execute container stop, restart, or cleanup."""

    parser = argparse.ArgumentParser(description="Cluster control")
    parser.add_argument(
        "action",
        choices=["stop", "restart", "clean"],
        help="Action to perform",
    )
    parser.add_argument("--force", "-f", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    config = ClusterConfig()

    actions = {
        "stop": lambda c: stop_all(c),
        "restart": lambda c: restart_all(c),
        "clean": lambda c: clean_all(c, force=args.force),
    }
    actions[args.action](config)


if __name__ == "__main__":
    main()
