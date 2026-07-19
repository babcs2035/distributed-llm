"""
Distributed LLM inference pipeline auto-deploy script.

Automatically executes the following in sequence:
  1. Local Docker image build and push to registry via SSH tunnel
  2. Local model split and transfer to master
  3. Rsync distribution from master to all nodes
  4. Image pull and container startup via SSH tunnel

Usage:
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
    ensure_ssh_control_master_dir,
    log,
    read_hosts,
    resolve_host_to_ip,
    rsync_dir,
    run_local,
    ssh_run,
    ssh_via_master,
)

# Default container environment variables
# Default 600s is insufficient for 100 tokens x 7s = 700s. Extended to 60 minutes.
GLOO_SOCKET_TIMEOUT_MS = 3600000
KMP_BLOCKTIME_STR = "1"


# ====================================================================
# Phase 1: Build and push Docker image (on Master)
# ====================================================================


def build_and_push(config: ClusterConfig, *, dry_run: bool = False) -> None:
    """
    Build a Docker image locally and push to the master's private registry via SSH tunnel.
    """

    phase_start = time.time()
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

    # Step 1: Local build
    log("INFO", f"Building image locally: {config.image_name}...")
    result = run_local(
        ["docker", "build", "-t", config.image_name, "."],
        check=True, timeout=600,
    )
    log("OK", "Local build complete")

    # Step 2: Tag for push via SSH tunnel
    tunnel_tag = f"localhost:{config.registry_port}/{config.image_name}"
    log("INFO", f"Tagging image for push: {tunnel_tag}")
    run_local(
        ["docker", "tag", config.image_name, tunnel_tag],
        check=True,
    )

    # Step 3: Start SSH tunnel (disable all ControlMaster options, remove existing tunnel)
    log("INFO", f"Opening SSH tunnel: localhost:{config.registry_port} -> {config.master_addr}:{config.registry_port}")
    # Remove existing tunnel (force-kill port-using processes via fuser)
    subprocess.run(
        ["fuser", "-k", f"{config.registry_port}/tcp"],
        capture_output=True,
    )
    import time as _time
    _time.sleep(2)
    # Disable ControlMaster, ControlPath, ControlPersist entirely
    # SSH_BASE_OPTS is ["-o", "Key=Value", ...] format, convert to dict for precise exclusion
    _base_dict = {}
    _i = 0
    while _i < len(SSH_BASE_OPTS):
        if SSH_BASE_OPTS[_i] == "-o" and _i + 1 < len(SSH_BASE_OPTS):
            _kv = SSH_BASE_OPTS[_i + 1]
            _k = _kv.split("=", 1)[0]
            if _k not in ("ControlMaster", "ControlPath", "ControlPersist"):
                _base_dict[_k] = _kv
            _i += 2
        else:
            _i += 1
    tunnel_opts = []
    for _k, _v in _base_dict.items():
        tunnel_opts.extend(["-o", _v])
    # Additional options for tunnel
    tunnel_opts.extend([
        "-o", "ServerAliveInterval=5",
        "-o", "ExitOnForwardFailure=yes",
    ])
    tunnel_proc = subprocess.Popen(
        ["ssh", *tunnel_opts, "-N",
         f"-L {config.registry_port}:localhost:{config.registry_port}",
         f"{config.ssh_user}@{config.master_addr}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for tunnel to be ready (max 15s)
    for _wait_i in range(30):
        _time.sleep(0.5)
        if tunnel_proc.poll() is not None:
            _stdout, _stderr = tunnel_proc.communicate()
            log("ERROR", f"SSH tunnel exited prematurely at iteration {_wait_i}")
            log("ERROR", f"SSH stderr: {_stderr.decode() if _stderr else '(none)'}")
            sys.exit(1)
        try:
            import socket as _socket
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _s.settimeout(2)
            _s.connect(("127.0.0.1", int(config.registry_port)))
            _s.close()
            break
        except Exception:
            pass
    else:
        tunnel_proc.terminate()
        try:
            stdout, stderr = tunnel_proc.communicate(timeout=3)
        except Exception:
            tunnel_proc.kill()
            stdout, stderr = tunnel_proc.communicate(timeout=3)
        log("ERROR", f"SSH tunnel failed to start")
        log("ERROR", f"SSH stderr: {stderr.decode() if stderr else '(none)'}")
        sys.exit(1)
    log("INFO", "SSH tunnel established successfully")

    try:
        # Step 4: Push via tunnel
        log("INFO", f"Pushing to registry via tunnel: {tunnel_tag}")
        result = run_local(
            ["docker", "push", tunnel_tag],
            check=True, timeout=300,
        )
        log("OK", "Image pushed to master's registry")
    finally:
        tunnel_proc.terminate()
        tunnel_proc.wait()

    duration = time.time() - phase_start
    log("INFO", f"Phase 1 completed in {_format_duration(duration)}")


# ====================================================================
# Phase 2: Split model weights and distribute
# ====================================================================


def split_models_locally(config: ClusterConfig, *, dry_run: bool = False) -> None:
    """
    Download and split the model locally, then rsync results to master's work_dir/models.
    """

    phase_start = time.time()
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

    # Step 1: Split locally
    log("INFO", "Downloading and splitting model locally...")
    result = run_local(
        ["uv", "run", "python", "tools/split_model.py", "--output-dir", local_model_splits],
        check=True, timeout=3600,
    )
    log("OK", f"Model split complete: {local_model_splits}/")

    # Step 2: Create master destination directory
    result = ssh_run(
        config.ssh_user, config.master_addr,
        f"mkdir -p {remote_model_dir}",
        timeout=30,
    )
    if result.returncode != 0:
        log("FAIL", f"Failed to create remote dir on master: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Transfer to master
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
    duration = time.time() - phase_start
    log("INFO", f"Phase 2 completed in {_format_duration(duration)}")


def get_assigned_layers(rank: int, world_size: int, total_layers: int) -> list[int]:
    """
    Return layer numbers assigned to the given rank based on asymmetric assignment.

    Rank 0 (master node) gets no layers (TCPStore only).
    Each node gets at least 1 layer, with leftover layers assigned as 2 to nodes from the start.

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
    Distribute model weights to a single node.

    Checks for required files and transfers only missing ones via rsync.
    Rsync checksum verification automatically detects corrupted files.

    Returns:
        (success, message) tuple
    """

    assigned_layers = get_assigned_layers(rank, world_size, total_layers)

    # Create model directory on target node (uses sudo)
    mkdir_result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        f"sudo mkdir -p {config.model_mount_path}",
        timeout=30,
    )
    if mkdir_result.returncode != 0:
        return False, f"failed to create model directory on {ip}"

    # Change directory ownership to SSH user
    chown_result = ssh_via_master(
        config.ssh_user, config.master_addr, ip,
        f"sudo chown -R {config.ssh_user}:{config.ssh_user} {config.model_mount_path}",
        timeout=30,
    )
    if chown_result.returncode != 0:
        return False, f"failed to change ownership of {config.model_mount_path} on {ip}"

    # Check disk space on target node
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
    needed_files.append("norm.safetensors")
    needed_files.append("split_info.json")

    # Check if all files already exist on target node
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

    # Transfer only needed files via rsync (via master)
    # Rsync supports checksum verification, resume, and delta transfer
    # SCP lacks checksum verification, so rsync is more reliable for accurate distribution
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
    Distribute model weights from master to each node via rsync (minimal required layers only).

    Each node receives only the layers assigned by the asymmetric assignment scheme,
    plus embed_tokens / lm_head. Rsync checksum verification detects corrupted files.
    Runs distribution with up to 10 parallel workers.
    """

    phase_start = time.time()
    log("STEP", "=" * 60)
    log("INFO", "Phase 3: Distribute model weights to all nodes")
    log("STEP", "=" * 60)

    model_dir = os.path.join(config.model_mount_path, "splits")
    hosts = read_hosts(config.hosts_file)

    # Check model directory exists on master
    if not dry_run:
        result = ssh_run(
            config.ssh_user, config.master_addr,
            f"test -d {model_dir} && echo 'exists' || echo 'missing'",
            timeout=10,
        )
        if (result.stdout or "").strip() != "exists":
            log("ERROR", f"Model directory not found on master: {model_dir}", file=sys.stderr)
            sys.exit(1)

    # Get total_layers and weight_format from split_info.json
    world_size = len(hosts)
    weight_format = config._config.get("model", {}).get("format", "safetensors")

    if dry_run:
        # Use overrides from config.json or default values in dry-run mode
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
        # Parallel distribution (max 10 workers)
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
    duration = time.time() - phase_start
    log("INFO", f"Phase 3 completed in {_format_duration(duration)}")


# ====================================================================
# Phase 4: Deploy to nodes
# ====================================================================


def _deploy_single_node(config, ip, rank, master_ip, all_hosts, host=None):
    """Deploy to a single node without stagger (ThreadPoolExecutor controls parallelism)."""
    return deploy_single_node(config, ip, rank, master_ip, all_hosts, host)


def deploy_single_node(
    config: ClusterConfig, ip: str, rank: int, master_ip: str, all_hosts: list[str], host: str | None = None
) -> bool:
    """
    Deploy to a single node.

    1. Pull image via SSH tunnel
    2. Start container (CPU affinity, network settings, etc.)

    Args:
        config: Cluster configuration
        ip: Target node IP address
        rank: Node rank number
        master_ip: Already-resolved master IP (prevents parallel SSH overload)

    Returns:
        True: Success, False: Failure
    """

    try:
        display = host or ip
        # Step 0: Add master IP to target node's /etc/hosts
        hosts_entry = f"{master_ip} {config.master_addr}"
        log("INFO", f"Rank {rank} ({display}): Ensuring {config.master_addr} -> {master_ip} in /etc/hosts")
        result = ensure_hosts_entry(
            config.ssh_user, config.master_addr, ip,
            hosts_entry, timeout=10,
        )
        if result.returncode != 0:
            log("WARN", f"Failed to update /etc/hosts on Rank {rank}: {result.stderr.strip()}")

        # Step 1: Stop and remove containers (only if they exist)
        # "llm-node" is the old name. Remove both for backward compatibility.
        stop_command = (
            f"for _c in distributed-llm llm-node; do "
            f"if [ -n \"$(docker ps -aq -f name=^/${{_c}}$ 2>/dev/null)\" ]; then "
            f"LC_ALL=C docker stop ${{_c}} >/dev/null 2>&1; "
            f"LC_ALL=C docker rm ${{_c}} >/dev/null 2>&1; fi; done"
        )
        log("INFO", f"Rank {rank} ({display}): Stopping existing container...")
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip, stop_command, timeout=30,
        )
        if result.returncode != 0:
            log("FAIL", f"Stop command error: {result.stderr.strip()}", file=sys.stderr)
            return False

        # Step 2: Configure Docker daemon insecure-registries (HTTP registry support)
        # Skip master node (Rank 0) since it runs the registry on the same machine
        if rank == 0:
            log("INFO", f"Rank {rank} ({display}): Skipping insecure registry (local registry)")
        else:
            log("INFO", f"Rank {rank} ({display}): Configuring insecure registry {config.registry_addr}...")
            result = configure_insecure_registry(
                config.ssh_user, config.master_addr, ip,
                config.registry_addr, timeout=30,
            )
            if result.returncode != 0:
                log("WARN", f"insecure-registries setup failed on Rank {rank}: {result.stderr.strip()}")

        # Step 3: Pull image (directly from master's registry)
        pull_command = (
            f"LC_ALL=C docker pull {config.registry_addr}/{config.image_name} && "
            f"LC_ALL=C docker tag {config.registry_addr}/{config.image_name} {config.target_image}"
        )
        log("INFO", f"Rank {rank} ({display}): Pulling image...")
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip, pull_command, timeout=300,
        )
        if result.returncode != 0:
            log("FAIL", f"Image pull error: {result.stderr.strip()}", file=sys.stderr)
            return False

        # Cleanup: Remove layer files not needed by this node
        # Run before docker run to prevent unnecessary files from remaining at container startup
        assigned_layers = get_assigned_layers(rank, int(config.world_size), config.total_layers)
        log("INFO", f"Rank {rank} ({display}): Cleaning unassigned layers (assigned={assigned_layers})")
        clean_result = clean_unassigned_layers(
            config.ssh_user, config.master_addr, ip,
            assigned_layers, config.total_layers, config.weight_format,
            timeout=30,
        )
        if clean_result.returncode != 0:
            log("WARN", f"Cleanup warning on Rank {rank}: {clean_result.stderr.strip()}")

        # Step 4: Start container
        # research-cycle Iter7: MICROBATCH_BENCH_* はシェル起動時に env で渡されても，従来この
        # docker run コマンドには一切転送されていなかった（NUM_MICRO_BATCHES/STAGGER_INTERVAL と
        # 異なり見落とされていた）ため，未設定時は `-e` 行ごと省略する形で明示的に転送を追加する．
        bench_env_lines = ""
        if config.microbatch_bench_steps:
            bench_env_lines += f"    -e MICROBATCH_BENCH_STEPS={config.microbatch_bench_steps} \\\n"
        if config.microbatch_bench_warmup_steps:
            bench_env_lines += (
                f"    -e MICROBATCH_BENCH_WARMUP_STEPS={config.microbatch_bench_warmup_steps} \\\n"
            )
        if config.microbatch_bench_repeats:
            bench_env_lines += f"    -e MICROBATCH_BENCH_REPEATS={config.microbatch_bench_repeats} \\\n"

        deploy_command = f"""
PHYS_IFACE=$(ip -o -4 route show to default | awk '{{print $5}}')
if [ -z "${{PHYS_IFACE}}" ]; then
    echo 'WARN: Failed to detect physical NIC. Falling back to eth0.'
    PHYS_IFACE='eth0'
fi
echo "Detected physical NIC: ${{PHYS_IFACE}}"

# Remove old unrestricted rule (backward compatibility)
sudo ufw delete allow 8083/tcp >/dev/null 2>&1 || true
# Add firewall rules for ports needed for inter-node communication
# Restrict source to internal networks (192.168.11.0/24, 192.168.12.0/24) only
sudo ufw allow from 192.168.11.0/24 to any port 10000 proto tcp >/dev/null 2>&1 || true
sudo ufw allow from 192.168.12.0/24 to any port 10000 proto tcp >/dev/null 2>&1 || true
sudo ufw allow from 192.168.11.0/24 to any port 8081 proto tcp >/dev/null 2>&1 || true
sudo ufw allow from 192.168.12.0/24 to any port 8081 proto tcp >/dev/null 2>&1 || true
sudo ufw allow from 192.168.11.0/24 to any port 8082 proto tcp >/dev/null 2>&1 || true
sudo ufw allow from 192.168.12.0/24 to any port 8082 proto tcp >/dev/null 2>&1 || true
sudo ufw allow from 192.168.11.0/24 to any port 8083 proto tcp >/dev/null 2>&1 || true
sudo ufw allow from 192.168.12.0/24 to any port 8083 proto tcp >/dev/null 2>&1 || true

# Ensure writable directory for torch.compile cache (persists across container restarts)
# chmod 777: Allows llmuser (UID != root) in container to write with full user write permissions
sudo mkdir -p /var/cache/torch_compile
sudo chmod 777 /var/cache/torch_compile

LC_ALL=C docker run -d \\
    --name distributed-llm \\
    --net=host \\
    --restart=unless-stopped \\
    --cpuset-cpus='{config.cpuset_cpus}' \\
    -v {config.model_mount_path}:/models:ro \\
    -v /var/cache/torch_compile:/torch_compile_cache:rw \\
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
{bench_env_lines}    {config.target_image}
"""
        result = ssh_via_master(
            config.ssh_user, config.master_addr, ip, deploy_command, timeout=300,
        )
        if result.returncode != 0:
            log("FAIL", f"Deploy command error: {result.stderr}", file=sys.stderr)
            return False

        return True

    except Exception as e:
        log("FAIL", f"Exception: {e}", file=sys.stderr)
        return False


# Lock for log output (prevents log interference between threads)
_log_lock = threading.Lock()


def _safe_log(level: str, msg: str, *, file=sys.stdout) -> None:
    """Output a log message with locking."""
    with _log_lock:
        log(level, msg, file=file)


def _resolve_master_ip(config: ClusterConfig) -> str:
    """
    Resolve the master node's IP address.

    If local DNS cannot resolve it, connects to the master via SSH to get the address.
    The result is reused across the entire deploy.
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
    Run auto-deploy to all nodes.

    Deploys to each node in parallel via ThreadPoolExecutor,
    counts successes and failures, and reports results.
    """

    phase_start = time.time()
    log("STEP", "=" * 60)
    log("INFO", "Phase 4: Deploy to all nodes")
    log("STEP", "=" * 60)

    # Resolve master IP once and share across all threads (prevents parallel SSH overload)
    master_ip = _resolve_master_ip(config)
    log("INFO", f"Master IP resolved: {config.master_addr} -> {master_ip}")

    hosts = read_hosts(config.hosts_file)
    # Convert hostnames to IP addresses (for NODE_IPS)
    host_ips = [resolve_host_to_ip(h) for h in hosts]
    max_workers = min(10, len(hosts))  # Max 10 parallel
    log("INFO", f"Using {max_workers} parallel workers for {len(hosts)} nodes")

    success_count = 0
    fail_count = 0
    results: dict[int, bool] = {}

    if not dry_run:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for rank, host in enumerate(hosts):
                target_ip = host_ips[rank]
                # ThreadPoolExecutor controls parallelism via max_workers,
                # so per-node delays are unnecessary. 10 parallel is sufficient and safe.
                log("INFO", f"Scheduling Rank {rank} ({host})")
                future = executor.submit(_deploy_single_node, config, target_ip, rank, master_ip, host_ips, host)
                futures[future] = rank
            for future in concurrent.futures.as_completed(futures):
                rank = futures[future]
                host = hosts[rank]
                try:
                    success = future.result()
                    results[rank] = success
                except Exception as e:
                    log("FAIL", f"Rank {rank} ({host}): unexpected error: {e}")
                    results[rank] = False

    # Aggregate results (includes dry_run)
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

    duration = time.time() - phase_start
    log("INFO", f"Phase 4 completed in {_format_duration(duration)}")


# ====================================================================
# Main
# ====================================================================


def _format_duration(seconds: float) -> str:
    """Convert seconds to HH:MM:SS formatted string."""

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"
    return f"{minutes:02d}:{secs:05.2f}"


def main() -> None:
    """Execute build, distribute, and deploy phases."""

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

    # Create SSH ControlMaster socket directory (for connection reuse)
    ensure_ssh_control_master_dir()

    log("INFO", "Distributed LLM pipeline deploy script starting")
    log("INFO", f"  Master: {config.master_addr}:{config.master_port}")
    log("INFO", f"  Registry: {config.master_addr}:{config.registry_port}")
    log("INFO", f"  World Size: {config.world_size}")
    log("INFO", f"  Hosts file: {config.hosts_file}")
    log("INFO", f"  Work dir: {config.work_dir}")
    log("INFO", f"  Model mount: {config.model_mount_path}")

    # Input validation
    if not Path(config.hosts_file).exists():
        log("ERROR", f"Hosts file not found: {config.hosts_file}", file=sys.stderr)
        sys.exit(1)

    start_time = time.time()

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

    total_duration = time.time() - start_time
    log("INFO", f"Deploy script complete. Total time: {_format_duration(total_duration)}")


if __name__ == "__main__":
    main()
