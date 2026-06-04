"""
分散LLM推論パイプライン 自動デプロイスクリプト

本スクリプトは以下を一括して自動実行する:
  1. ローカルでの Docker イメージビルドと SSH トンネル経由のレジストリへのプッシュ
  2. ローカルでのモデル分割とマスターへの転送
  3. マスターからの全ノードへの rsync 配布
  4. SSH トンネル経由のイメージプルとコンテナ起動

使用法:
  uv run python tools/deploy.py [--build-only] [--deploy-only] [--split-only] [--dry-run]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from common import (
    ClusterConfig,
    SSH_BASE_OPTS,
    clean_unassigned_layers,
    configure_insecure_registry,
    ensure_hosts_entry,
    log,
    read_hosts,
    resolve_host_to_ip,
    rsync_dir,
    run_local,
    ssh_run,
    ssh_via_master,
)

# コンテナ環境変数のデフォルト値
GLOO_SOCKET_TIMEOUT_MS = 600000
KMP_BLOCKTIME_STR = "1"


# ====================================================================
# フェーズ1: Dockerイメージのビルドとプッシュ（Master 上）
# ====================================================================


def build_and_push(config: ClusterConfig, *, dry_run: bool = False) -> None:
    """
    ローカルで Docker イメージをビルドし、SSH トンネル経由で
    マスターのプライベートレジストリにプッシュする。
    """

    log("STEP", "=" * 60)
    log("INFO", "Phase 1: Docker image build & push (local build, push via SSH tunnel)")
    log("STEP", "=" * 60)

    if dry_run:
        log("DRY-RUN", f"docker build -t {config.image_name} .")
        log("DRY-RUN", f"docker tag {config.image_name} localhost:{config.registry_port}/{config.image_name}")
        log("DRY-RUN", f"ssh -f -N -L {config.registry_port}:localhost:{config.registry_port} {config.ssh_user}@{config.master_addr}")
        log("DRY-RUN", f"docker push localhost:{config.registry_port}/{config.image_name}")
        log("INFO", "Build and push complete.")
        return

    # Step 1: ローカルビルド
    log("INFO", f"Building image locally: {config.image_name}...")
    result = run_local(
        ["docker", "build", "-t", config.image_name, "."],
        check=True, timeout=600,
    )
    log("OK", "Local build complete")

    # Step 2: SSH トンネル経由で push 用のタグ付け
    tunnel_tag = f"localhost:{config.registry_port}/{config.image_name}"
    log("INFO", f"Tagging image for push: {tunnel_tag}")
    run_local(
        ["docker", "tag", config.image_name, tunnel_tag],
        check=True,
    )

    # Step 3: SSH トンネル起動
    log("INFO", f"Opening SSH tunnel: localhost:{config.registry_port} -> {config.master_addr}:{config.registry_port}")
    tunnel_proc = subprocess.Popen(
        ["ssh", *SSH_BASE_OPTS, "-f", "-N",
         f"-L {config.registry_port}:localhost:{config.registry_port}",
         f"{config.ssh_user}@{config.master_addr}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        time.sleep(2)

        # Step 4: トンネル経由でプッシュ
        log("INFO", f"Pushing to registry via tunnel: {tunnel_tag}")
        result = run_local(
            ["docker", "push", tunnel_tag],
            check=True, timeout=300,
        )
        log("OK", "Image pushed to master's registry")
    finally:
        tunnel_proc.terminate()
        tunnel_proc.wait()


# ====================================================================
# フェーズ2: モデル重みの分割と配布
# ====================================================================


def split_models_locally(config: ClusterConfig, *, dry_run: bool = False) -> None:
    """
    ローカルでモデルをダウンロード・分割し、
    結果をマスターの work_dir/models へ rsync で転送する。
    """

    log("STEP", "=" * 60)
    log("INFO", "Phase 2: Split model locally and transfer to master")
    log("STEP", "=" * 60)

    local_model_splits = "models/splits"
    remote_model_dir = os.path.join(config.model_mount_path, "splits")

    if dry_run:
        log("DRY-RUN", f"uv run python tools/split_model.py --output-dir {local_model_splits}")
        log("DRY-RUN", f"ssh {config.ssh_user}@{config.master_addr}: mkdir -p {remote_model_dir}")
        log("DRY-RUN", f"rsync to master: {local_model_splits}/ -> {config.ssh_user}@{config.master_addr}:{remote_model_dir}/")
        return

    # Step 1: ローカルで分割
    log("INFO", "Downloading and splitting model locally...")
    result = run_local(
        ["uv", "run", "python", "tools/split_model.py", "--output-dir", local_model_splits],
        check=True, timeout=3600,
    )
    log("OK", f"Model split complete: {local_model_splits}/")

    # Step 2: マスター宛先ディレクトリ作成
    result = ssh_run(
        config.ssh_user, config.master_addr,
        f"mkdir -p {remote_model_dir}",
        timeout=30,
    )
    if result.returncode != 0:
        log("FAIL", f"Failed to create remote dir on master: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    # Step 3: マスターへ転送
    log("INFO", f"Transferring split models to master: {config.master_addr}:{remote_model_dir}/")
    result = rsync_dir(
        config.ssh_user, config.master_addr,
        local_model_splits, remote_model_dir,
        timeout=600,
    )
    if result.returncode != 0:
        log("FAIL", f"rsync to master failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    log("OK", f"Models transferred to master: {remote_model_dir}/")


def get_assigned_layers(rank: int, world_size: int, total_layers: int) -> list[int]:
    """
    非対称レイヤー割り当てに基づき、指定 Rank の担当レイヤー番号を返す。

    Rank 0（マスターノード）はレイヤーを割り当てない（TCPStore のみ）。
    各ノードが少なくとも1レイヤーを持つことを保証した上で、
    余ったレイヤーを先頭ノードから順に2レイヤーとして割り当てる。

      layers_high = TOTAL_LAYERS - WORLD_SIZE + 2
      Rank 0           : []
      Rank < layers_high: [(rank-1)*2, (rank-1)*2+1]
      Rank >= layers_high: 2*layers_high + rank - layers_high - 2
    """

    if rank == 0:
        return []

    layers_high = total_layers - world_size + 2
    if rank < layers_high:
        return [(rank - 1) * 2, (rank - 1) * 2 + 1]
    else:
        return [2 * layers_high + rank - layers_high - 2]


def _distribute_to_rank(
    config: ClusterConfig,
    model_dir: str,
    rank: int,
    ip: str,
    world_size: int,
    total_layers: int,
    weight_format: str,
) -> tuple[bool, str]:
    """
    単一ノードへのモデル重み配布を実行する。

    必要なファイルの存在を確認し、不足分のみ rsync で転送する。
    rsync はチェックサム検証により、壊れたファイルも自動検出する。

    Returns:
        (success, message) のタプル
    """

    assigned_layers = get_assigned_layers(rank, world_size, total_layers)

    # ターゲットノードにモデル配置用ディレクトリを作成（sudo 使用）
    mkdir_result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        f"sudo mkdir -p {config.model_mount_path}",
        timeout=30,
    )
    if mkdir_result.returncode != 0:
        return False, f"failed to create model directory on {ip}"

    # ディレクトリの所有権を SSH ユーザーに変更
    chown_result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        f"sudo chown -R {config.ssh_user}:{config.ssh_user} {config.model_mount_path}",
        timeout=30,
    )
    if chown_result.returncode != 0:
        return False, f"failed to change ownership of {config.model_mount_path} on {ip}"

    # ターゲットノードのディスク容量確認
    df_result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        f"df -h {config.model_mount_path} | tail -1 | awk '{{print $4}}'",
        timeout=10,
    )
    available_kb = (df_result.stdout or "").strip()
    _safe_log("INFO", f"  Available space on {ip}: {available_kb}KB")

    needed_files = []
    for i in assigned_layers:
        ext = "safetensors" if weight_format == "safetensors" else "pt"
        needed_files.append(f"layer_{i}.{ext}")
    needed_files.append("embed_tokens.safetensors")
    needed_files.append("lm_head.safetensors")
    needed_files.append("split_info.json")

    # ターゲットノードに既に全てのファイルが存在するか確認
    check_files = " && ".join(
        f"test -f {config.model_mount_path}/{f} || echo 'missing'"
        for f in needed_files
    )
    check_result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        check_files,
        timeout=30,
    )
    missing = [f for f in needed_files if "missing" in check_result.stdout]
    if not missing:
        return True, f"all files already present, skipping"

    _safe_log("INFO", f"  Missing files: {', '.join(missing)}")

    # rsync で必要なファイルのみ転送（マスター経由）
    # rsync はチェックサム検証・中断再開・差分転送に対応
    # scp はチェックサム検証がないため、rsync の方が確実に過不足なく配布できる
    rsync_files = " ".join(f"{model_dir}/{f}" for f in missing)
    rsh_opts = "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=600 -o ServerAliveInterval=30 -o ServerAliveCountMax=10"
    rsync_cmd = (
        f"rsync -avz --checksum --partial "
        f"--rsh='{rsh_opts}' "
        f"{rsync_files} "
        f"{config.ssh_user}@{ip}:{config.model_mount_path}/"
    )
    result = ssh_run(
        config.ssh_user, config.master_addr,
        rsync_cmd,
        timeout=1800,
    )
    if result.returncode != 0:
        _safe_log("ERROR", f"  rsync stderr: {result.stderr.strip()}")
        return False, f"rsync failed (rc={result.returncode})"
    return True, f"distributed {len(missing)} files"


def distribute_models(config: ClusterConfig, *, dry_run: bool = False) -> None:
    """
    Master 上のモデル重みを各ノードの必要最小限のレイヤーのみへ rsync で配布する。

    各ノードは非対称レイヤー割り当てに基づき、担当するレイヤーと
    embed_tokens / lm_head のみを受信する。
    rsync はチェックサム検証により、壊れたファイルも自動検出する。
    最大10並列で配布を実行する。
    """

    log("STEP", "=" * 60)
    log("INFO", "Phase 3: Distribute model weights to all nodes")
    log("STEP", "=" * 60)

    model_dir = os.path.join(config.model_mount_path, "splits")
    hosts = read_hosts(config.hosts_file)

    # Master 上のモデルディレクトリの存在確認
    if not dry_run:
        result = ssh_run(
            config.ssh_user, config.master_addr,
            f"test -d {model_dir} && echo 'exists' || echo 'missing'",
            timeout=10,
        )
        if (result.stdout or "").strip() != "exists":
            log("ERROR", f"Model directory not found on master: {model_dir}", file=sys.stderr)
            sys.exit(1)

    # split_info.json から total_layers と weight_format を取得
    world_size = len(hosts)
    weight_format = config._config.get("model", {}).get("format", "safetensors")

    if dry_run:
        # dry-run では config.json の overrides またはデフォルト値を使用
        total_layers = config._config.get("model", {}).get("overrides", {}).get(
            "num_hidden_layers", 80
        )
    else:
        result = ssh_run(
            config.ssh_user, config.master_addr,
            f"cat {model_dir}/split_info.json",
            timeout=10,
        )
        if result.returncode != 0:
            log("ERROR", f"Failed to read split_info.json on master", file=sys.stderr)
            sys.exit(1)
        split_info = json.loads(result.stdout)
        total_layers = len([k for k in split_info if k.startswith("layer_")])

    log("INFO", f"Total layers: {total_layers}, World size: {world_size}")

    if not dry_run:
        # 並列配布（最大10並列）
        max_workers = min(10, len(hosts))
        _safe_log("INFO", f"Using {max_workers} parallel workers for model distribution")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _distribute_to_rank, config, model_dir, rank, ip,
                    world_size, total_layers, weight_format
                ): rank
                for rank, ip in enumerate(hosts)
            }
            for future in concurrent.futures.as_completed(futures):
                rank = futures[future]
                ip = hosts[rank]
                try:
                    success, msg = future.result()
                    if success:
                        _safe_log("OK", f"Rank {rank} ({ip}): {msg}")
                    else:
                        _safe_log("FAIL", f"Rank {rank} ({ip}): {msg}")
                except Exception as e:
                    _safe_log("FAIL", f"Rank {rank} ({ip}): unexpected error: {e}")
    else:
        for rank, ip in enumerate(hosts):
            assigned_layers = get_assigned_layers(rank, world_size, total_layers)
            files_str = ", ".join(f"layer_{i}" for i in assigned_layers)
            _safe_log("DRY-RUN", f"Rank {rank} needs: {files_str} + embed_tokens, lm_head")

    log("INFO", "Model distribution complete.")


# ====================================================================
# フェーズ4: ノードへのデプロイ
# ====================================================================


def deploy_single_node(
    config: ClusterConfig, ip: str, rank: int, master_ip: str, all_hosts: list[str]
) -> bool:
    """
    単一ノードへのデプロイを実行する。

    1. SSHトンネル経由でイメージをプル
    2. コンテナを起動（CPUアフィニティ、ネットワーク設定等）

    Args:
        config: クラスタ設定
        ip: ターゲットノードのIPアドレス
        rank: ノードのRank番号
        master_ip: 既に解決済みのマスターIP（並列SSHオーバーロード防止）

    Returns:
        True: 成功, False: 失敗
    """

    try:
        # Step 0: マスターのIPをターゲットノードの /etc/hosts に追加
        hosts_entry = f"{master_ip} {config.master_addr}"
        log("INFO", f"Rank {rank} ({ip}): Ensuring {config.master_addr} -> {master_ip} in /etc/hosts")
        result = ensure_hosts_entry(
            config.ssh_user, config.master_addr, ip,
            hosts_entry, timeout=10,
        )
        if result.returncode != 0:
            log("WARN", f"Failed to update /etc/hosts on Rank {rank}: {result.stderr.strip()}")

        # Step 1: コンテナの停止・削除（LC_ALL=C で locale ウォーニングを抑制）
        stop_command = (
            f"LC_ALL=C docker stop llm-node >/dev/null 2>&1 || true; "
            f"LC_ALL=C docker rm llm-node >/dev/null 2>&1 || true"
        )
        log("INFO", f"Rank {rank} ({ip}): Stopping existing container...")
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip, stop_command, timeout=30,
        )
        if result.returncode != 0:
            log("FAIL", f"Stop command error: {result.stderr.strip()}", file=sys.stderr)
            return False

        # Step 2: Docker デーモンの insecure-registries 設定（HTTP レジストリ対応）
        # マスターノード（Rank 0）はレジストリと同じマシン上のためスキップ
        if rank == 0:
            log("INFO", f"Rank {rank} ({ip}): Skipping insecure registry (local registry)")
        else:
            log("INFO", f"Rank {rank} ({ip}): Configuring insecure registry {config.registry_addr}...")
            result = configure_insecure_registry(
                config.ssh_user, config.master_addr, ip,
                config.registry_addr, timeout=30,
            )
            if result.returncode != 0:
                log("WARN", f"insecure-registries setup failed on Rank {rank}: {result.stderr.strip()}")

        # Step 3: イメージをプル（マスターのレジストリから直接）
        pull_command = (
            f"LC_ALL=C docker pull {config.registry_addr}/{config.image_name} && "
            f"LC_ALL=C docker tag {config.registry_addr}/{config.image_name} {config.target_image}"
        )
        log("INFO", f"Rank {rank} ({ip}): Pulling image...")
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip, pull_command, timeout=300,
        )
        if result.returncode != 0:
            log("FAIL", f"Image pull error: {result.stderr.strip()}", file=sys.stderr)
            return False

        # クリーンアップ: このノードが必要としないレイヤーファイルを削除
        # docker run より前に実行し、コンテナ起動時に不要なファイルが残らないようにする
        assigned_layers = get_assigned_layers(rank, int(config.world_size), config.total_layers)
        log("INFO", f"Rank {rank} ({ip}): Cleaning unassigned layers (assigned={assigned_layers})")
        clean_result = clean_unassigned_layers(
            config.ssh_user, config.master_addr, ip,
            assigned_layers, config.total_layers, config.weight_format,
            timeout=30,
        )
        if clean_result.returncode != 0:
            log("WARN", f"Cleanup warning on Rank {rank}: {clean_result.stderr.strip()}")

        # Step 3: コンテナ起動
        deploy_command = f"""
PHYS_IFACE=$(ip -o -4 route show to default | awk '{{print $5}}')
if [ -z "${{PHYS_IFACE}}" ]; then
    echo 'WARN: Failed to detect physical NIC. Falling back to eth0.'
    PHYS_IFACE='eth0'
fi
echo "Detected physical NIC: ${{PHYS_IFACE}}"

LC_ALL=C docker run -d \\
    --name llm-node \\
    --net=host \\
    --restart=unless-stopped \\
    --cpuset-cpus='{config.cpuset_cpus}' \\
    -v {config.model_mount_path}:/models:ro \\
    -e MASTER_ADDR={config.master_addr} \\
    -e MASTER_PORT={config.master_port} \\
    -e RANK={rank} \\
    -e WORLD_SIZE={config.world_size} \\
    -e NODE_IPS={','.join(all_hosts)} \\
    -e GLOO_SOCKET_IFNAME=${{PHYS_IFACE}} \\
    -e TP_SOCKET_IFNAME=${{PHYS_IFACE}} \\
    -e GLOO_SOCKET_TIMEOUT_MS={GLOO_SOCKET_TIMEOUT_MS} \\
    -e GLOO_INET2=ipv4 \\
    -e OMP_NUM_THREADS={config.omp_num_threads} \\
    -e OMP_PROC_BIND=CLOSE \\
    -e KMP_AFFINITY=granularity=fine,compact,1,0 \\
    -e KMP_BLOCKTIME={KMP_BLOCKTIME_STR} \\
    -e NUM_MICRO_BATCHES={config.num_micro_batches} \\
    {config.target_image}
"""
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip, deploy_command, timeout=120,
        )
        if result.returncode != 0:
            log("FAIL", f"Deploy command error: {result.stderr}", file=sys.stderr)
            return False

        return True

    except Exception as e:
        log("FAIL", f"Exception: {e}", file=sys.stderr)
        return False


# ログ出力用のロック（スレッド間でのログ干渉防止）
_log_lock = threading.Lock()


def _safe_log(level: str, msg: str, *, file=sys.stdout) -> None:
    """ロック付きでログを出力する"""
    with _log_lock:
        log(level, msg, file=file)


def _resolve_master_ip(config: ClusterConfig) -> str:
    """
    マスターノードのIPアドレスを解決する。

    ローカルのDNSで解決できない場合はSSHでマスターに接続して取得する。
    結果はデプロイ全体で再利用される。
    """

    try:
        return socket.gethostbyname(config.master_addr)
    except socket.gaierror:
        log("INFO", f"Local DNS cannot resolve {config.master_addr}, resolving via SSH...")
        result = ssh_run(config.ssh_user, config.master_addr, "hostname -I | awk '{print $1}'", timeout=30)
        if result.returncode == 0:
            return result.stdout.strip()
        log("FAIL", f"Failed to resolve master address: {config.master_addr}")
        sys.exit(1)


def deploy_to_nodes(config: ClusterConfig, *, dry_run: bool = False) -> None:
    """
    全ノードへの自動デプロイを実行する。

    ThreadPoolExecutor で各ノードに並列デプロイし、
    成功数と失敗数をカウントして結果を報告する。
    """

    log("STEP", "=" * 60)
    log("INFO", "Phase 4: Deploy to all nodes")
    log("STEP", "=" * 60)

    # 前置き: マスターIPを1回だけ解決して全スレッドで共有（並列SSHオーバーロード防止）
    master_ip = _resolve_master_ip(config)
    log("INFO", f"Master IP resolved: {config.master_addr} -> {master_ip}")

    hosts = read_hosts(config.hosts_file)
    # ホスト名をIPアドレスに変換（NODE_IPS用）
    host_ips = [resolve_host_to_ip(h) for h in hosts]
    max_workers = min(10, len(hosts))  # 最大10並列
    log("INFO", f"Using {max_workers} parallel workers for {len(hosts)} nodes")

    success_count = 0
    fail_count = 0
    results: dict[int, bool] = {}

    if not dry_run:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(deploy_single_node, config, ip, rank, master_ip, host_ips): rank
                for rank, ip in enumerate(hosts)
            }
            for future in concurrent.futures.as_completed(futures):
                rank = futures[future]
                ip = hosts[rank]
                try:
                    success = future.result()
                    results[rank] = success
                except Exception as e:
                    log("FAIL", f"Rank {rank} ({ip}): unexpected error: {e}")
                    results[rank] = False

    # 結果を集計（dry_run 含む）
    for rank, ip in enumerate(hosts):
        if dry_run:
            _safe_log("DRY-RUN", f"SSH via master {config.master_addr} -> {ip}: stop container + image pull")
            _safe_log("DRY-RUN", f"SSH via master {config.master_addr} -> {ip}: container start (Rank={rank})")
            continue

        success = results.get(rank, False)
        if success:
            success_count += 1
            _safe_log("OK", f"Node Rank {rank} ({ip}): deployed successfully")
        else:
            fail_count += 1
            _safe_log("FAIL", f"Node Rank {rank} ({ip}): deployment failed")

    log("STEP", "=" * 60)
    log("RESULT", f"Deploy results: success={success_count}, failed={fail_count}, total={len(hosts)}")
    log("STEP", "=" * 60)

    if fail_count > 0:
        log(
            "ERROR",
            f"Deployment failed on {fail_count} node(s).",
            file=sys.stderr,
        )
        sys.exit(1)


# ====================================================================
# メイン
# ====================================================================


def main() -> None:
    """ビルド・配布・デプロイの各フェーズを実行する"""

    parser = argparse.ArgumentParser(
        description="Distributed LLM pipeline auto-deploy script"
    )
    parser.add_argument(
        "--build-only", action="store_true",
        help="Build and push image only",
    )
    parser.add_argument(
        "--deploy-only", action="store_true",
        help="Deploy to nodes only (image must be pushed, models distributed beforehand)",
    )
    parser.add_argument(
        "--split-only", action="store_true",
        help="Split model locally and transfer to master only",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show planned commands without executing",
    )
    args = parser.parse_args()

    config = ClusterConfig()

    log("INFO", "Distributed LLM pipeline deploy script starting")
    log("INFO", f"  Master: {config.master_addr}:{config.master_port}")
    log("INFO", f"  Registry: {config.master_addr}:{config.registry_port}")
    log("INFO", f"  World Size: {config.world_size}")
    log("INFO", f"  Hosts file: {config.hosts_file}")
    log("INFO", f"  Work dir: {config.work_dir}")
    log("INFO", f"  Model mount: {config.model_mount_path}")

    # 入力検証
    if not Path(config.hosts_file).exists():
        log("ERROR", f"Hosts file not found: {config.hosts_file}", file=sys.stderr)
        sys.exit(1)

    if args.build_only:
        build_and_push(config, dry_run=args.dry_run)
    elif args.deploy_only:
        distribute_models(config, dry_run=args.dry_run)
        deploy_to_nodes(config, dry_run=args.dry_run)
    elif args.split_only:
        split_models_locally(config, dry_run=args.dry_run)
    else:
        build_and_push(config, dry_run=args.dry_run)
        split_models_locally(config, dry_run=args.dry_run)
        distribute_models(config, dry_run=args.dry_run)
        deploy_to_nodes(config, dry_run=args.dry_run)

    log("INFO", "Deploy script complete.")


if __name__ == "__main__":
    main()
