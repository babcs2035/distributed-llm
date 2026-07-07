"""
Common utility module.

Shared across all tool scripts:
  - Config loading from config.json
  - hosts.txt parsing
  - SSH / Rsync command execution
  - Colored log output
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

# ====================================================================
# Color output
# ====================================================================


class Color:
    """ANSI color codes."""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[0;33m"
    BLUE = "\033[0;34m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    NC = "\033[0m"  # No Color

    @classmethod
    def disable(cls) -> None:
        """Disable color output (for non-TTY environments)."""

        cls.RED = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.BLUE = ""
        cls.BOLD = ""
        cls.DIM = ""
        cls.NC = ""


# Disable colors in non-TTY environments
if not sys.stdout.isatty():
    Color.disable()


# ====================================================================
# Unified logging functions
# ====================================================================

# Level name to colored tag mapping
_LOG_LEVELS: dict[str, str] = {
    "INFO": Color.BLUE + "INFO" + Color.NC,
    "DEBUG": Color.DIM + "DEBUG" + Color.NC,
    "OK": Color.GREEN + "OK" + Color.NC,
    "FAIL": Color.RED + "FAIL" + Color.NC,
    "WARN": Color.YELLOW + "WARN" + Color.NC,
    "ERROR": Color.RED + "ERROR" + Color.NC,
    "RESULT": Color.GREEN + "RESULT" + Color.NC,
    "DRY-RUN": Color.BLUE + "DRY-RUN" + Color.NC,
    "STEP": Color.BLUE + "STEP" + Color.NC,
}


def log(
    level: str,
    msg: str,
    *,
    file: TextIO = sys.stdout,
    prefix: str = "  ",
) -> None:
    """
    Output a log message in unified format.

    Format:  <prefix>[<LEVEL>] <msg>

    Args:
        level: Log level (INFO, OK, FAIL, WARN, ERROR, RESULT, DRY-RUN, STEP)
        msg: Message to output
        file: Output destination (stdout or stderr)
        prefix: Leading indent string
    """

    tag = _LOG_LEVELS.get(level, level.upper())
    print(f"{prefix}[{tag}] {msg}", file=file)


# ====================================================================
# Backward-compatible logging functions
# ====================================================================


def log_ok(msg: str, file: TextIO = sys.stdout) -> None:
    """Output a success message with [OK] prefix (backward compatible)."""

    log("OK", msg, file=file)


def log_fail(msg: str, file: TextIO = sys.stderr) -> None:
    """Output a failure message with [FAIL] prefix (backward compatible)."""

    log("FAIL", msg, file=file)


def log_warn(msg: str, file: TextIO = sys.stderr) -> None:
    """Output a warning message with [WARN] prefix (backward compatible)."""

    log("WARN", msg, file=file)


def log_info(msg: str, file: TextIO = sys.stdout) -> None:
    """Output an info message with [INFO] prefix (backward compatible)."""

    log("INFO", msg, file=file)


def log_header(msg: str, file: TextIO = sys.stdout) -> None:
    """Output a section header in blue (backward compatible)."""

    print(f"\n{Color.BLUE}{msg}{Color.NC}", file=file)


def log_timestamp(msg: str, file: TextIO = sys.stdout) -> None:
    """Output a message with timestamp (backward compatible)."""

    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=file)


# ====================================================================
# Config management
# ====================================================================

_CONFIG_CACHE: dict[str, dict[str, Any]] = {}


def load_config(config_path: str | Path = "config.json") -> dict[str, Any]:
    """Load config.json and cache the result."""

    key = str(Path(config_path).resolve())
    if key in _CONFIG_CACHE:
        return _CONFIG_CACHE[key]

    path = Path(config_path)
    if not path.exists():
        print(f"Error: config file not found: {path}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(path.read_text())
    _CONFIG_CACHE[key] = config
    return config


# ====================================================================
# Cluster configuration
# ====================================================================


@dataclass
class ClusterConfig:
    """
    Dataclass managing cluster configuration.

    Reads settings from config.json, fills unspecified items from environment variables.
    Environment variables take precedence.
    """

    _config: dict[str, Any] = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._config is None:
            self._config = load_config()

        c = self._config

        # --- cluster ---
        self.master_addr = (
            os.environ.get("MASTER_ADDR")
            or c.get("cluster", {}).get("master_addr", "192.168.1.100")
        )
        self.master_port = (
            os.environ.get("MASTER_PORT")
            or str(c.get("cluster", {}).get("master_port", 29500))
        )
        self.hosts_file = (
            os.environ.get("HOSTS_FILE")
            or c.get("cluster", {}).get("hosts_file", "hosts.txt")
        )
        # Auto-computed from hosts.txt line count (overridable via env var)
        self.world_size = (
            os.environ.get("WORLD_SIZE") or str(len(read_hosts(self.hosts_file)))
        )

        # --- ssh ---
        self.ssh_user = (
            os.environ.get("SSH_USER") or c.get("ssh", {}).get("user", "user")
        )

        # --- docker ---
        self.image_name = (
            os.environ.get("IMAGE_NAME")
            or c.get("docker", {}).get("image_name", "distributed-llm:latest")
        )
        self.registry_port = (
            os.environ.get("REGISTRY_PORT")
            or c.get("docker", {}).get("registry_port", "5000")
        )

        # --- deploy ---
        self.work_dir = os.environ.get("WORK_DIR") or c.get("deploy", {}).get("work_dir")
        if not self.work_dir:
            print(
                "Error: deploy.work_dir is not set in config.json. "
                "Set WORK_DIR env var or add 'deploy.work_dir' to config.json.",
                file=sys.stderr,
            )
            sys.exit(1)
        self.work_dir = os.path.expanduser(self.work_dir)
        self.model_mount_path = os.path.join(self.work_dir, "models")

        # --- pipeline ---
        self.num_micro_batches = (
            os.environ.get("NUM_MICRO_BATCHES") or "4"
        )
        self.stagger_interval = (
            os.environ.get("STAGGER_INTERVAL") or "3.0"
        )

        # --- model ---
        model_cfg = c.get("model", {})
        self.model_name = (
            os.environ.get("MODEL_NAME")
            or model_cfg.get("name", "google/gemma-4-31B-it")
        )
        overrides = model_cfg.get("overrides", {})
        self.total_layers = (
            int(os.environ.get("TOTAL_LAYERS") or overrides.get("num_hidden_layers", 60))
        )
        self.weight_format = (
            os.environ.get("WEIGHT_FORMAT") or model_cfg.get("format", "safetensors")
        )

        # --- cpu ---
        self.cpuset_cpus = os.environ.get("CPUSET_CPUS") or "0-3"
        self.omp_num_threads = os.environ.get("OMP_NUM_THREADS") or "4"

    @classmethod
    def load(cls, config_path: str | Path = "config.json") -> "ClusterConfig":
        """Load configuration from the specified config.json path."""

        # Pass _config directly to avoid __post_init__ relative path issues
        cfg = load_config(config_path)
        instance = object.__new__(cls)
        instance._config = cfg
        instance.__post_init__()
        return instance

    @property
    def registry_addr(self) -> str:
        """Docker registry address (host:port format)."""

        return f"{self.master_addr}:{self.registry_port}"

    @property
    def target_image(self) -> str:
        """Full Docker image tag (registry_addr/image_name)."""

        return f"{self.registry_addr}/{self.image_name}"

    @property
    def hosts_path(self) -> Path:
        """Path to hosts.txt."""

        return Path(self.hosts_file)


# ====================================================================
# Host file parsing
# ====================================================================


def read_hosts(hosts_file: str | Path) -> list[str]:
    """
    Read hosts.txt and return a list of IP addresses.

    Skips empty lines and comment lines (starting with #).
    Line order corresponds to Rank number.
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


def resolve_host_to_ip(host: str) -> str:
    """
    Resolve a hostname to an IP address.

    Gets Hostname entry from SSH config (~/.ssh/config),
    falls back to DNS resolution, returns original value on failure.
    """

    import socket

    # 1. Get Hostname from SSH config
    ssh_config_path = Path.home() / ".ssh" / "config"
    if ssh_config_path.exists():
        current_hosts = None
        for line in ssh_config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("Host "):
                hosts_in_line = line[5:].split()
                current_hosts = hosts_in_line if line[5:].strip() else None
            elif line.startswith("Hostname ") and current_hosts and host in current_hosts:
                return line.split()[1]

    # 2. DNS resolution
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        pass

    # 3. Return original value on resolution failure
    return host


# ====================================================================
# SSH / Rsync command execution
# ====================================================================

SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/control_masters/%r@%h-%p",
    "-o", "ControlPersist=600",
]


def ensure_ssh_control_master_dir() -> None:
    """
    Ensure the SSH ControlMaster socket directory exists.

    ControlMaster requires the directory to exist beforehand.
    It is not created automatically on first connection, so create it explicitly.
    """

    ctrl_dir = Path.home() / ".ssh" / "control_masters"
    ctrl_dir.mkdir(parents=True, exist_ok=True)

# rsync -e option: Convert SSH_BASE_OPTS to ssh command string (no quotes)
SSH_BASE_OPTS_STR = "ssh " + " ".join(SSH_BASE_OPTS)


def ssh_run(
    user: str,
    host: str,
    command: str,
    *,
    timeout: int = 60,
    capture: bool = True,
    check: bool = False,
    extra_opts: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Execute an SSH command on a remote host.

    Args:
        user: SSH connection user
        host: Remote host IP address or hostname
        command: Command to execute
        timeout: Timeout in seconds
        capture: Whether to capture stdout/stderr
        check: Whether to raise on non-zero exit code
        extra_opts: Additional SSH options

    Returns:
        subprocess.CompletedProcess: Execution result
    """

    opts = SSH_BASE_OPTS.copy()
    if extra_opts:
        opts.extend(extra_opts)

    cmd = ["ssh", *opts, f"{user}@{host}", command]

    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=check,
    )


def _escape_for_remote(command: str) -> str:
    """
    Escape single quotes in a command for safe execution in remote SSH.
    """

    return command.replace("'", "'\\''")


def ssh_via_master(
    user: str,
    master_addr: str,
    target_host: str,
    command: str,
    *,
    timeout: int = 60,
    capture: bool = True,
    check: bool = False,
    extra_opts: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Execute an SSH command on a target node via the management node.

    Uses OpenSSH ProxyJump (-J): local -> master -> target node.
    Replaces nested SSH to reduce connection overhead.

    Args:
        user: SSH connection user
        master_addr: Management node IP address
        target_host: Target node IP address or hostname
        command: Command to execute on the target node
        timeout: Timeout in seconds
        capture: Whether to capture stdout/stderr
        check: Whether to raise on non-zero exit code
        extra_opts: Additional SSH options

    Returns:
        subprocess.CompletedProcess: Execution result
    """

    opts = SSH_BASE_OPTS.copy()
    if extra_opts:
        opts.extend(extra_opts)

    # ProxyJump (-J) syntax: single SSH command via master
    cmd = [
        "ssh", *opts,
        "-J", f"{user}@{master_addr}",
        f"{user}@{target_host}",
        command,
    ]

    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=check,
    )


def rsync_dir(
    user: str,
    host: str,
    local_dir: str,
    remote_path: str,
    *,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """
    Transfer a local directory to a remote host via rsync (recursive).

    Streams rsync progress (-P) in real time and returns exit code on completion.

    Args:
        user: SSH connection user
        host: Remote host IP address or hostname
        local_dir: Local source directory
        remote_path: Remote destination path
        timeout: Timeout in seconds

    Returns:
        subprocess.CompletedProcess: Execution result
    """

    cmd = [
        "rsync",
        "-avzP",
        f"--rsh=ssh -o StrictHostKeyChecking=no -o ConnectTimeout={timeout}",
        f"{local_dir}/",
        f"{user}@{host}:{remote_path}",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in iter(proc.stdout.readline, ""):
        if line:
            log("rsync", line.rstrip("\n"))
    proc.stdout.close()
    proc.wait()

    return subprocess.CompletedProcess(
        cmd,
        returncode=proc.returncode,
        stdout=None,
        stderr=None,
    )


def run_local(
    command: str | list[str],
    *,
    check: bool = True,
    capture: bool = False,
    shell: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Execute a command locally.

    Args:
        command: Command to execute (string or list)
        check: Whether to raise on non-zero exit code
        capture: Whether to capture stdout/stderr
        shell: Whether to run as a shell command
        timeout: Timeout in seconds

    Returns:
        subprocess.CompletedProcess: Execution result
    """

    return subprocess.run(
        command,
        capture_output=capture,
        text=True,
        check=check,
        shell=shell,
        timeout=timeout,
    )


def ensure_hosts_entry(
    user: str,
    master_addr: str,
    target_host: str,
    hosts_entry: str,
    *,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str]:
    """
    Check and add an entry to /etc/hosts on the target node.

    SSHes to the target node via the master node and checks if
    the master's hostname is in /etc/hosts. If present, does nothing.
    Otherwise, appends the given entry.

    Args:
        user: SSH connection user
        master_addr: Management node address (via SSH)
        target_host: Target node address
        hosts_entry: Line to add to /etc/hosts (e.g., "192.168.1.1 master-hostname")
        timeout: Timeout in seconds

    Returns:
        subprocess.CompletedProcess: Execution result
    """

    # Check /etc/hosts for existence. If missing, append via sudo tee.
    # hosts_entry contains only "IP hostname" format, so safe within single quotes.
    check_cmd = (
        f"grep -q '{master_addr}' /etc/hosts || "
        f"printf '%s\\n' '{hosts_entry}' | sudo tee -a /etc/hosts >/dev/null"
    )
    return ssh_via_master(user, master_addr, target_host, check_cmd, timeout=timeout)


def configure_insecure_registry(
    user: str,
    master_addr: str,
    target_host: str,
    registry_addr: str,
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """
    Configure insecure-registries on the target node's Docker daemon.

    Reads existing /etc/docker/daemon.json, adds registry_addr to
    insecure-registries if missing, and restarts Docker.
    Existing Docker configuration is preserved.

    Args:
        user: SSH connection user
        master_addr: Management node address (via SSH)
        target_host: Target node address
        registry_addr: Registry address to add to insecure-registries (e.g., "wafl-ctrl1:5000")
        timeout: Timeout in seconds

    Returns:
        subprocess.CompletedProcess: Execution result
    """

    import base64

    # Pass Python code via base64 encoding to fully avoid quote issues.
    # Skip Docker restart if already registered in insecure-registries.
    python_code = (
        "import json, os\n"
        'd = json.load(open("/etc/docker/daemon.json")) if os.path.exists("/etc/docker/daemon.json") else {}\n'
        'r = "' + registry_addr + '"\n'
        "changed = False\n"
        'if r not in d.get("insecure-registries", []):\n'
        '    d.setdefault("insecure-registries", []).append(r)\n'
        "    changed = True\n"
        'if changed:\n'
        '    json.dump(d, open("/etc/docker/daemon.json", "w"), indent=2)\n'
        '    os.system("sudo systemctl restart docker")\n'
        'print("changed" if changed else "already_configured")'
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = f"sudo python3 -c 'import base64; exec(base64.b64decode(\"{encoded}\").decode())'"
    return ssh_via_master(user, master_addr, target_host, cmd, timeout=timeout)


def clean_unassigned_layers(
    user: str,
    master_addr: str,
    target_host: str,
    assigned_layers: list[int],
    total_layers: int,
    weight_format: str,
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """
    Remove unassigned layer files from /models on the target node.

    Deletes layer_N.{ext} files not in assigned_layers.
    Always keeps embed_tokens and lm_head.
    Executes safely via base64-encoded Python code.
    """
    import base64

    ext = "safetensors" if weight_format == "safetensors" else "pt"
    needed = str(set(str(i) for i in assigned_layers))

    # Base64-encoded Python code to avoid nested single-quote issues
    lines = [
        "import os, glob, re",
        'ext = "' + ext + '"',
        "needed = " + needed,
        'pattern = re.compile(r"layer_(\\d+)\\." + ext)',
        'for filepath in glob.glob("/models/layer_*." + ext):',
        "    m = pattern.match(os.path.basename(filepath))",
        "    if m and int(m.group(1)) not in needed:",
        "        os.remove(filepath)",
        '        print("  removed: " + filepath)',
    ]
    python_code = "\n".join(lines) + "\n"
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = "sudo python3 -c 'import base64; exec(base64.b64decode(\"" + encoded + "\").decode())'"
    return ssh_via_master(user, master_addr, target_host, cmd, timeout=timeout)


def require_root() -> None:
    """Require root privileges. Exit with error if not running as root."""

    if os.geteuid() != 0:
        print(
            "Error: This script requires root privileges. Use: sudo uv run python <script>",
            file=sys.stderr,
        )
        sys.exit(1)
