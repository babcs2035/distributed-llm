"""
共通ユーティリティモジュール

全ツールスクリプトで共有される機能:
  - config.json からの設定読み込み
  - hosts.txt の解析
  - SSH / Rsync コマンド実行
  - ログ出力のカラー化
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
# カラー出力
# ====================================================================


class Color:
    """ANSIカラーコード"""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[0;33m"
    BLUE = "\033[0;34m"
    BOLD = "\033[1m"
    NC = "\033[0m"  # No Color

    @classmethod
    def disable(cls) -> None:
        """カラー出力を無効化する（非TTY環境用）"""

        cls.RED = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.BLUE = ""
        cls.BOLD = ""
        cls.NC = ""


# 非TTY環境ではカラーを無効化
if not sys.stdout.isatty():
    Color.disable()


# ====================================================================
# 統一ログ関数
# ====================================================================

# レベル名と絵文字のマッピング
_LOG_LEVELS: dict[str, tuple[str, str]] = {
    "INFO": ("i", Color.BLUE + "i" + Color.NC),
    "OK": ("o", Color.GREEN + "✔" + Color.NC),
    "FAIL": ("!", Color.RED + "✘" + Color.NC),
    "WARN": ("!", Color.YELLOW + "⚠" + Color.NC),
    "ERROR": ("!", Color.RED + "✘" + Color.NC),
    "RESULT": ("*", Color.GREEN + "✔" + Color.NC),
    "DRY-RUN": (">", Color.BLUE + ">" + Color.NC),
    "STEP": ("-", Color.BLUE + "-" + Color.NC),
}


def log(
    level: str,
    msg: str,
    *,
    file: TextIO = sys.stdout,
    prefix: str = "  ",
) -> None:
    """
    統一フォーマットでログを出力する。

    フォーマット:  <prefix>[<emoji> <LEVEL>] <msg>

    Args:
        level: ログレベル（INFO, OK, FAIL, WARN, ERROR, RESULT, DRY-RUN, STEP）
        msg: 出力メッセージ
        file: 出力先（stdout または stderr）
        prefix: 行頭のインデント文字列
    """

    if level not in _LOG_LEVELS:
        tag = level.upper()
    else:
        _, tag = _LOG_LEVELS[level]

    print(f"{prefix}[{tag}] {msg}", file=file)


# ====================================================================
# 後方互換用ログ出力関数
# ====================================================================


def log_ok(msg: str, file: TextIO = sys.stdout) -> None:
    """[OK] プレフィックス付きで成功メッセージを出力する（後方互換）"""

    log("OK", msg, file=file)


def log_fail(msg: str, file: TextIO = sys.stderr) -> None:
    """[FAIL] プレフィックス付きで失敗メッセージを出力する（後方互換）"""

    log("FAIL", msg, file=file)


def log_warn(msg: str, file: TextIO = sys.stderr) -> None:
    """[WARN] プレフィックス付きで警告メッセージを出力する（後方互換）"""

    log("WARN", msg, file=file)


def log_info(msg: str, file: TextIO = sys.stdout) -> None:
    """[INFO] プレフィックス付きで情報メッセージを出力する（後方互換）"""

    log("INFO", msg, file=file)


def log_header(msg: str, file: TextIO = sys.stdout) -> None:
    """セクション見出しを青色で出力する（後方互換）"""

    print(f"\n{Color.BLUE}{msg}{Color.NC}", file=file)


def log_timestamp(msg: str, file: TextIO = sys.stdout) -> None:
    """タイムスタンプ付きログ出力（後方互換）"""

    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=file)


# ====================================================================
# 設定管理
# ====================================================================

_CONFIG_CACHE: dict[str, dict[str, Any]] = {}


def load_config(config_path: str | Path = "config.json") -> dict[str, Any]:
    """config.json を読み込み、結果をキャッシュする"""

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
# クラスタ設定
# ====================================================================


@dataclass
class ClusterConfig:
    """
    クラスタ設定を管理するデータクラス。

    config.json から設定を読み込み、未設定項目は環境変数で補う。
    環境変数が優先される。
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
        # hosts.txt の行数から自動計算（環境変数で上書き可能）
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
            or c.get("docker", {}).get("image_name", "llm-pipeline-image:latest")
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
        """指定された config.json パスから設定を読み込んでインスタンスを生成する。"""

        # _config を直接渡すことで __post_init__ の相対パス問題を回避
        cfg = load_config(config_path)
        instance = object.__new__(cls)
        instance._config = cfg
        instance.__post_init__()
        return instance

    @property
    def registry_addr(self) -> str:
        """Dockerレジストリのアドレス（host:port 形式）"""

        return f"{self.master_addr}:{self.registry_port}"

    @property
    def target_image(self) -> str:
        """Dockerイメージの完全なタグ（registry_addr/image_name）"""

        return f"{self.registry_addr}/{self.image_name}"

    @property
    def hosts_path(self) -> Path:
        """hosts.txt のパス"""

        return Path(self.hosts_file)


# ====================================================================
# ホストファイル解析
# ====================================================================


def read_hosts(hosts_file: str | Path) -> list[str]:
    """
    hosts.txt を読み込み、IPアドレスのリストを返す。

    空行およびコメント行（# で始まる行）は無視する。
    行の出現順が Rank 番号に対応する。
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
    ホスト名をIPアドレスに解決する。

    SSH config（~/.ssh/config）から Hostname エントリを取得し、
    なければ DNS 解決、失敗すれば元の値を返す。
    """

    import socket

    # 1. SSH config から Hostname を取得
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

    # 2. DNS 解決
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        pass

    # 3. 解決失敗時は元の値を返す
    return host


# ====================================================================
# SSH / Rsync コマンド実行
# ====================================================================

SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]

# rsync -e オプション用: SSH_BASE_OPTS を ssh コマンド文字列に変換（引用符なし）
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
    リモートホスト上でSSHコマンドを実行する。

    Args:
        user: SSH接続ユーザー
        host: リモートホストのIPアドレスまたはホスト名
        command: 実行するコマンド
        timeout: タイムアウト秒数
        capture: 標準出力/標準エラーをキャプチャするかどうか
        check: エラーコードで終了するかどうか
        extra_opts: SSHオプションの追加

    Returns:
        subprocess.CompletedProcess: 実行結果
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
    リモートSSHコマンド内で安全に実行できるよう、コマンド内の
    シングルクォートをエスケープする。
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
    管理ノードを経由してターゲットノード上でSSHコマンドを実行する。

    ローカル → SSH → マスター → SSH → ターゲットノード

    Args:
        user: SSH接続ユーザー
        master_addr: 管理ノードのIPアドレス
        target_host: ターゲットノードのIPアドレスまたはホスト名
        command: ターゲットノードで実行するコマンド
        timeout: タイムアウト秒数
        capture: 標準出力/標準エラーをキャプチャするかどうか
        check: エラーコードで終了するかどうか
        extra_opts: SSHオプションの追加（両サイドに適用）

    Returns:
        subprocess.CompletedProcess: 実行結果
    """

    inner_opts = SSH_BASE_OPTS.copy()
    if extra_opts:
        inner_opts.extend(extra_opts)

    inner_ssh = "ssh " + " ".join(inner_opts)
    escaped = _escape_for_remote(command)
    remote_cmd = f"{inner_ssh} {user}@{target_host} '{escaped}'"

    cmd = ["ssh", *SSH_BASE_OPTS, f"{user}@{master_addr}", remote_cmd]

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
    ローカルディレクトリをリモートホストにRsyncで転送する（再帰）。

    rsync の進捗表示（-P）をリアルタイムで出力し、
    完了後に終了コードを返す。

    Args:
        user: SSH接続ユーザー
        host: リモートホストのIPアドレスまたはホスト名
        local_dir: 転送元のローカルディレクトリ
        remote_path: 転送先のリモートパス
        timeout: タイムアウト秒数

    Returns:
        subprocess.CompletedProcess: 実行結果
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
    ローカルでコマンドを実行する。

    Args:
        command: 実行するコマンド（文字列またはリスト）
        check: エラーコードで終了するかどうか
        capture: 標準出力/標準エラーをキャプチャするかどうか
        shell: シェルとして実行するかどうか
        timeout: タイムアウト秒数

    Returns:
        subprocess.CompletedProcess: 実行結果
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
    ターゲットノードの /etc/hosts にエントリが存在することを確認・追加する。

    マスターノード経由でターゲットノードにSSHし、
    /etc/hosts にマスターのホスト名が含まれていれば何もしない。
    含まれていなければ、渡されたエントリを追記する。

    Args:
        user: SSH接続ユーザー
        master_addr: 管理ノードのアドレス（SSH経由）
        target_host: ターゲットノードのアドレス
        hosts_entry: /etc/hosts に追加する行（例: "192.168.1.1 master-hostname"）
        timeout: タイムアウト秒数

    Returns:
        subprocess.CompletedProcess: 実行結果
    """

    # /etc/hosts に存在確認。なければ sudo tee で追記
    # hosts_entry は "IP hostname" 形式のみで構成されるため、
    # シングルクォート内で安全に扱える
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
    ターゲットノードの Docker デーモンに insecure-registries を設定する。

    既存の /etc/docker/daemon.json を読み取り、
    insecure-registries に registry_addr がなければ追加し、Docker を再起動する。
    既存の Docker 設定は保持される。

    Args:
        user: SSH接続ユーザー
        master_addr: 管理ノードのアドレス（SSH経由）
        target_host: ターゲットノードのアドレス
        registry_addr: insecure-registries に追加するレジストリアドレス（例: "wafl-ctrl1:5000"）
        timeout: タイムアウト秒数

    Returns:
        subprocess.CompletedProcess: 実行結果
    """

    import base64

    # base64 エンコーディングで Python コードを渡す（引用符の問題を完全に回避）
    python_code = (
        "import json, os\n"
        'd = json.load(open("/etc/docker/daemon.json")) if os.path.exists("/etc/docker/daemon.json") else {}\n'
        'r = "' + registry_addr + '"\n'
        'if r not in d.get("insecure-registries", []):\n'
        '    d.setdefault("insecure-registries", []).append(r)\n'
        'json.dump(d, open("/etc/docker/daemon.json", "w"), indent=2)\n'
        'os.system("sudo systemctl restart docker")'
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
    ターゲットノードの /models から不要なレイヤーファイルを削除する。

    assigned_layers に含まれない layer_N.{ext} ファイルを削除し、
    embed_tokens と lm_head は常に保持する。
    base64エンコーディングされたPythonコードで安全に実行する。
    """
    import base64

    ext = "safetensors" if weight_format == "safetensors" else "pt"
    needed = str(set(str(i) for i in assigned_layers))

    # base64エンコーディングされたPythonコード
    # ネストされたシングルクォート問題を避けるため
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
    """root権限を要求する。非rootの場合はエラー終了する"""

    if os.geteuid() != 0:
        print(
            "Error: This script requires root privileges. Use: sudo uv run python <script>",
            file=sys.stderr,
        )
        sys.exit(1)
