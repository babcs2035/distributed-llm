"""
debug_tools.py - デバッグ・トラブルシューティングツール

使用法:
  uv run python tools/debug_tools.py ssh    # SSH接続テスト
  uv run python tools/debug_tools.py mtu    # MTU設定確認
  uv run python tools/debug_tools.py models # モデル重み配置状態確認
  uv run python tools/debug_tools.py ports  # ポート開放確認
  uv run python tools/debug_tools.py temp   # CPU温度確認
"""

from __future__ import annotations

import argparse

from common import ClusterConfig, log, read_hosts, run_local, ssh_run, ssh_via_master


def debug_ssh(config: ClusterConfig) -> None:
    """全ノードへのSSH接続テストを実行する"""

    log("INFO", "Testing SSH connection to all nodes...")

    hosts = read_hosts(config.hosts_file)
    ok_count = 0
    fail_count = 0

    for rank, ip in enumerate(hosts):
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip, "true",
            timeout=10,
            extra_opts=["-o", "ConnectTimeout=3"],
        )
        if result.returncode == 0:
            log("OK", f"Rank {rank} ({ip})")
            ok_count += 1
        else:
            log("FAIL", f"Rank {rank} ({ip})")
            fail_count += 1

    log("RESULT", f"SSH results: OK={ok_count}, FAIL={fail_count}")


def debug_mtu(config: ClusterConfig) -> None:
    """全ノードのMTU設定を確認する"""

    log("INFO", "Checking MTU settings on all nodes...")

    hosts = read_hosts(config.hosts_file)

    for rank, ip in enumerate(hosts):
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip,
            "ip -o link show $(ip -o -4 route show to default | awk '{print $5}') 2>/dev/null"
            " | awk -F'mtu ' '{print $2}' | awk '{print $1}'",
            timeout=10,
            extra_opts=["-o", "ConnectTimeout=3"],
        )
        mtu = (result.stdout or "").strip() or "N/A"
        log("INFO", f"Rank {rank} ({ip}): MTU={mtu}")


def debug_models(config: ClusterConfig) -> None:
    """全ノードのモデル重み配置状態を確認する"""

    log("INFO", "Checking model weight deployment on all nodes...")

    hosts = read_hosts(config.hosts_file)

    for rank, ip in enumerate(hosts):
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip,
            f"ls {config.model_mount_path}/layer_0.* >/dev/null 2>&1 "
            "&& echo 'OK' || echo 'MISSING'",
            timeout=10,
            extra_opts=["-o", "ConnectTimeout=3"],
        )
        status = (result.stdout or "").strip() if result.returncode == 0 else "UNREACHABLE"
        log("INFO", f"Rank {rank} ({ip}): {status}")


def debug_ports(config: ClusterConfig) -> None:
    """管理サーバの必要ポートの開放状態を確認する"""

    log("INFO", "Checking port status on management server...")

    log("INFO", f"PyTorch master port ({config.master_port})")
    result = run_local(
        f"ss -tlnp | grep ':{config.master_port}'",
        capture=True, check=False, shell=True,
    )
    log("INFO", result.stdout.strip() if result.stdout else "  Not listening")

    log("INFO", f"Docker registry port ({config.registry_port})")
    result = run_local(
        f"ss -tlnp | grep ':{config.registry_port}'",
        capture=True, check=False, shell=True,
    )
    log("INFO", result.stdout.strip() if result.stdout else "  Not listening")

    log("INFO", "Firewall status")
    result = run_local("sudo ufw status 2>/dev/null", capture=True, check=False, shell=True)
    if result.returncode == 0 and result.stdout:
        log("INFO", result.stdout.strip())
    else:
        result = run_local(
            "sudo iptables -L -n 2>/dev/null | head -20",
            capture=True, check=False, shell=True,
        )
        log("INFO", result.stdout.strip() if result.stdout else "  Cannot check")


def debug_temp(config: ClusterConfig) -> None:
    """全ノードのCPU温度を確認する（サーマルスロットリング検出）"""

    log("INFO", "Checking CPU temperature on all nodes...")

    hosts = read_hosts(config.hosts_file)

    for rank, ip in enumerate(hosts):
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip,
            "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null",
            timeout=10,
            extra_opts=["-o", "ConnectTimeout=3"],
        )
        temp_raw = (result.stdout or "").strip()

        if temp_raw.isdigit() and int(temp_raw) > 0:
            temp_c = int(temp_raw) // 1000
            if temp_c > 85:
                log("WARN", f"Rank {rank} ({ip}): {temp_c}°C (possible thermal throttling)")
            else:
                log("OK", f"Rank {rank} ({ip}): {temp_c}°C")
        else:
            log("WARN", f"Rank {rank} ({ip}): temperature unavailable")


def main() -> None:
    """デバッグ・トラブルシューティングツールを実行する"""

    parser = argparse.ArgumentParser(description="Debug and troubleshooting tools")
    parser.add_argument(
        "action",
        choices=["ssh", "mtu", "models", "ports", "temp"],
        help="Debug action to perform",
    )
    args = parser.parse_args()

    config = ClusterConfig()

    actions = {
        "ssh": debug_ssh,
        "mtu": debug_mtu,
        "models": debug_models,
        "ports": debug_ports,
        "temp": debug_temp,
    }
    actions[args.action](config)


if __name__ == "__main__":
    main()
