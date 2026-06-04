"""
分散LLM推論パイプラインノード

N台のノードによる直列パイプライン並列（PP）でのLLM推論を実現する計算ノード。
config.json からモデル仕様（レイヤー数、隠れ層サイズ等）を読み込み、
指定されたモデルに応じて動的に動作する。

主要機能:
  - 非対称レイヤー割り当て（WORLD_SIZEに自動適応）
  - ゼロアロケーション通信プロトコル（事前確保バッファによるIn-place受信）
  - マイクロバッチ分割によるパイプラインバブル最小化
  - 時間差起動プロトコル（Staggered Model Loading）による輻輳回避
  - Glooバックエンドの物理NIC固定バインド
  - safetensors / PyTorch 形式の両方対応
"""

import json
import os
import socket
import sys
import traceback
import time
import signal
import threading
from datetime import timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import torch
import torch.distributed as dist
from tqdm import tqdm

# ====================================================================
# グレースフルシャットダウン用シグナルハンドラ
# ====================================================================

_shutdown_requested = False


def _signal_handler(signum: int, frame: object) -> None:
    """SIGTERMおよびSIGINTを受信時にグレースフルシャットダウンフラグを立てる"""

    global _shutdown_requested
    _shutdown_requested = True
    print(f"[INFO] Shutting down (signal={signum}). Exiting inference loop...")


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


# ====================================================================
# 設定定数
# ====================================================================

DEFAULT_NUM_MICRO_BATCHES = 4
DEFAULT_STAGGER_INTERVAL = 3.0
DEFAULT_INIT_TIMEOUT_MINUTES = 10
DEFAULT_GLOO_TIMEOUT_MS = 600000

# モック推論用の定数（モックレイヤーの演算値、入力初期化の標準偏差）
MOCK_INCREMENT = 0.01
INPUT_STDDEV = 0.02

# ====================================================================
# HTTP リクエスト処理用（Rank 0 のみ使用）
# ====================================================================

# パイプラインスレッド管理（リレーモード用）
_pipeline_thread: threading.Thread | None = None

# リクエスト通知用（ソケットベースのクロスノードシグナリング）
_request_event = threading.Event()
_request_prompt: str | None = None
_request_lock = threading.Lock()
_request_result: str | None = None
_result_available = threading.Event()
# Rank 0以外がリクエストを検知するためのソケットポート
_SIGNAL_PORT = 8081
# barrier完了フラグ（barrier完了前からのリクエストを防止）
_barrier_done = False
# リレー実行中フラグ（パイプラインループとのバッファ競合防止）
_relay_active = False
_relay_lock = threading.Lock()
# パイプラインループ停止フラグ
_pipeline_stopped = False


def _encode_prompt(prompt: str, shape: tuple[int, ...]) -> torch.Tensor:
    """プロンプト文字列をテンソルにエンコードする。"""
    size = shape[0] * shape[1] * shape[2]
    data = [0.0] * size
    for i, ch in enumerate(prompt):
        if i < size:
            data[i] = ord(ch) / 255.0
    return torch.tensor(data, dtype=torch.bfloat16).reshape(shape)


# ====================================================================
# カラー出力
# ====================================================================

class _Color:
    """ANSIカラーコード"""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[0;33m"
    BLUE = "\033[0;34m"
    BOLD = "\033[1m"
    NC = "\033[0m"  # No Color


if not sys.stdout.isatty():
    _Color.RED = ""
    _Color.GREEN = ""
    _Color.YELLOW = ""
    _Color.BLUE = ""
    _Color.BOLD = ""
    _Color.NC = ""


# ====================================================================
# 統一ログ関数
# ====================================================================

_RANK: int = -1
_LOG_LEVELS: dict[str, str] = {
    "INFO": f"{_Color.BLUE}i{_Color.NC} INFO",
    "OK": f"{_Color.GREEN}o{_Color.NC} OK",
    "FAIL": f"{_Color.RED}x{_Color.NC} FAIL",
    "WARN": f"{_Color.YELLOW}!{_Color.NC} WARN",
    "ERROR": f"{_Color.RED}x{_Color.NC} ERROR",
    "STEP": f"{_Color.BOLD}-{_Color.NC} STEP",
    "RESULT": f"{_Color.GREEN}*{_Color.NC} RESULT",
}


def _log(level: str, msg: str) -> None:
    """
    統一フォーマットでログを出力する。

    フォーマット:  [<emoji> LEVEL] message

    Args:
        level: ログレベル（INFO, OK, FAIL, WARN, ERROR, STEP, RESULT）
        msg: 出力メッセージ
    """

    tag = _LOG_LEVELS.get(level, f"? {level}")
    print(f"[R{_RANK}{tag}] {msg}")


# ====================================================================
# 設定読み込み
# ====================================================================

def _load_model_config(config_path: str = "config.json") -> dict[str, Any]:
    """config.json からモデル設定を読み込む"""

    path = Path(config_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _resolve_model_specs(config: dict[str, Any]) -> tuple[int, int, int, int]:
    """
    config.json からモデル仕様を取得し、AutoConfig で補完する。

    Returns:
        (num_hidden_layers, hidden_size, num_attention_heads, num_key_value_heads)
    """

    model_cfg = config.get("model", {})
    model_name = model_cfg.get("name", "")
    overrides = model_cfg.get("overrides", {})

    num_hidden_layers: int | None = overrides.get("num_hidden_layers")
    hidden_size: int | None = overrides.get("hidden_size")
    num_attention_heads: int | None = overrides.get("num_attention_heads")
    num_key_value_heads: int | None = overrides.get("num_key_value_heads")

    if model_name and (num_hidden_layers is None or hidden_size is None):
        try:
            from transformers import AutoConfig

            print(f"[i INFO] Loading config via AutoConfig: {model_name}", flush=True)
            hf_config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)

            # Gemma4 等は text_config 内にネストしている
            text_config = getattr(hf_config, "text_config", hf_config)
            num_hidden_layers = getattr(text_config, "num_hidden_layers", None)
            hidden_size = getattr(text_config, "hidden_size", None)
            num_attention_heads = getattr(text_config, "num_attention_heads", hidden_size)
            num_key_value_heads = getattr(
                text_config,
                "num_key_value_heads",
                num_attention_heads,
            )

            print(
                f"[i INFO] AutoConfig done: layers={num_hidden_layers}, hidden={hidden_size}",
                flush=True,
            )
        except Exception as e:
            print(f"[x ERROR] AutoConfig failed: {e}", file=sys.stderr)
            print(
                "[x ERROR] Please specify values via config.json model.overrides.",
                file=sys.stderr,
            )
            raise

    if num_hidden_layers is None or hidden_size is None:
        raise ValueError(
            "Failed to load model specs. Check model.name in config.json."
        )

    return (
        num_hidden_layers,
        hidden_size,
        num_attention_heads or hidden_size,
        num_key_value_heads or hidden_size,
    )


# ====================================================================
# パイプライン設定
# ====================================================================

class PipelineConfig:
    """
    パイプライン推論の設定パラメータを環境変数および config.json から読み込む。

    必須パラメータ（環境変数）:
        RANK, WORLD_SIZE, MASTER_ADDR, MASTER_PORT
    オプションパラメータ（環境変数またはデフォルト値）:
        NUM_MICRO_BATCHES, STAGGER_INTERVAL, BATCH_SIZE, SEQ_LEN
    """

    def __init__(self, config_path: str = "config.json") -> None:
        # config.json からモデル仕様を取得
        file_config = _load_model_config(config_path)
        (
            self.num_hidden_layers,
            self.hidden_size,
            self.num_attention_heads,
            self.num_key_value_heads,
        ) = _resolve_model_specs(file_config)

        # 必須パラメータ: 環境変数から取得
        self.rank = int(os.environ["RANK"])
        self.world_size = int(os.environ["WORLD_SIZE"])
        self.master_addr = os.environ["MASTER_ADDR"]
        self.master_port = int(os.environ.get("MASTER_PORT", "29500"))

        # オプションパラメータ
        self.num_micro_batches = int(
            os.environ.get("NUM_MICRO_BATCHES", str(DEFAULT_NUM_MICRO_BATCHES))
        )
        self.stagger_interval = float(
            os.environ.get("STAGGER_INTERVAL", str(DEFAULT_STAGGER_INTERVAL))
        )
        self.init_timeout_minutes = int(
            os.environ.get("INIT_TIMEOUT_MINUTES", str(DEFAULT_INIT_TIMEOUT_MINUTES))
        )
        self.gloo_timeout_ms = int(
            os.environ.get("GLOO_SOCKET_TIMEOUT_MS", str(DEFAULT_GLOO_TIMEOUT_MS))
        )

        # 推論パラメータ
        self.batch_size = int(os.environ.get("BATCH_SIZE", "1"))
        self.seq_len = int(os.environ.get("SEQ_LEN", "1"))

        # モデルパスと形式
        self.model_path = os.environ.get("MODEL_PATH", "/models")
        self.weight_format = file_config.get("model", {}).get("format", "safetensors")

        # 入力検証: ノード数はレイヤー数を超えられない
        if self.world_size > self.num_hidden_layers:
            print(
                f"[x ERROR] WORLD_SIZE({self.world_size}) exceeds num_hidden_layers({self.num_hidden_layers}).",
                file=sys.stderr,
            )
            sys.exit(1)

        # 入力検証: 各ノードの最大レイヤー数は2に制限
        layers_high = self.num_hidden_layers - self.world_size
        if layers_high > self.world_size:
            min_ws = (self.num_hidden_layers + 1) // 2
            print(
                f"[x ERROR] WORLD_SIZE({self.world_size}) is too small. "
                f"Minimum {min_ws} nodes required (max 2 layers per node).",
                file=sys.stderr,
            )
            sys.exit(1)

    @property
    def prev_rank(self) -> int | None:
        """前段ノードのRank（Rank 0の場合はNone）"""

        return self.rank - 1 if self.rank > 0 else None

    @property
    def next_rank(self) -> int | None:
        """次段ノードのRank（最終Rankの場合はNone）"""

        return self.rank + 1 if self.rank < (self.world_size - 1) else None

    @property
    def total_layers(self) -> int:
        """総レイヤー数（num_hidden_layers と同じ値）"""

        return self.num_hidden_layers

    def get_assigned_layers(self) -> list[int]:
        """
        非対称レイヤー割り当てスキーム。

        Rank 0（マスターノード）はレイヤーを割り当てない（TCPStore のみ）。
        各ノードが少なくとも1レイヤーを持つことを保証した上で、
        余ったレイヤーを先頭ノードから順に2レイヤーとして割り当てる。

        Rank 0 が空のため:
          layers_high = TOTAL_LAYERS - WORLD_SIZE + 2
          Rank < layers_high : [(rank-1)*2, (rank-1)*2+1]
          Rank >= layers_high: 2*layers_high + rank - layers_high - 2
        """

        if self.rank == 0:
            return []

        layers_high = self.total_layers - self.world_size + 2
        if self.rank < layers_high:
            return [(self.rank - 1) * 2, (self.rank - 1) * 2 + 1]
        else:
            return [2 * layers_high + self.rank - layers_high - 2]


# ====================================================================
# パイプライン推論ノード
# ====================================================================

class FullyOptimizedPipelineNode:
    """
    最適化されたパイプライン並列推論ノード。

    実装している最適化:
      1. ゼロアロケーション通信: 事前確保バッファへのIn-place受信
      2. マイクロバッチ分割: パイプラインバブルの最小化
      3. 非対称レイヤー割り当て: 後段ノードの負荷軽減
      4. 時間差起動: サンダリングハード問題の回避
      5. 物理NIC固定: Glooバックエンドの通信安定化
    """

    def __init__(self, config: PipelineConfig) -> None:
        global _RANK
        _RANK = config.rank
        self.config = config

        # 1. Glooバックエンドの物理NIC固定バインド設定
        self._configure_network_binding()

        # 2. 分散プロセスグループの構築
        self._init_process_group()

        # 3. ゼロアロケーション通信用の事前確保バッファ
        self._allocate_communication_buffers()

        # 5. モデル重みのロード
        self.my_layers = self._load_local_weights()

    def _configure_network_binding(self) -> None:
        """Glooバックエンドが使用する物理NICを固定する"""

        socket_interface = os.environ.get("GLOO_SOCKET_IFNAME", "eth0")
        os.environ["GLOO_SOCKET_IFNAME"] = socket_interface
        os.environ["TP_SOCKET_IFNAME"] = socket_interface
        os.environ["GLOO_SOCKET_TIMEOUT_MS"] = str(self.config.gloo_timeout_ms)
        os.environ["GLOO_INET2"] = "ipv4"

        _log(
            "INFO",
            f"Network binding: NIC={socket_interface}, timeout={self.config.gloo_timeout_ms}ms, ipv4_only",
        )

    @staticmethod
    def _get_external_ip(interface: str = "eth0") -> str:
        """
        指定したNICのIPv4アドレスを返す。

        ipコマンドを使用して物理インターフェースのIPを取得する。
        """

        try:
            import subprocess
            result = subprocess.run(
                ["ip", "-o", "-4", "addr", "show", interface],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if "inet " in line:
                    ip_cidr = parts[3]
                    return ip_cidr.split("/")[0]
        except Exception:
            pass
        return "0.0.0.0"

    def _init_process_group(self) -> None:
        """Glooバックエンドによる分散プロセスグループを初期化する"""

        # コンテナ内部でMASTER_ADDRがlocalhostに解決される問題を回避
        # （例: wafl-ctrl1 -> 127.0.1.1）。loopbackの場合は外部IPを使用。
        master_addr = self.config.master_addr
        try:
            resolved = socket.gethostbyname(master_addr)
            if resolved.startswith("127."):
                # コンテナ内部でlocalhostに解決される場合、
                # 外部IPを直接取得して使用
                ifc = os.environ.get("GLOO_SOCKET_IFNAME", "eth0")
                master_addr = self._get_external_ip(ifc)
                _log("WARN", f"MASTER_ADDR {self.config.master_addr} resolves to {resolved}, using {master_addr}")
        except socket.gaierror:
            pass

        # init_methodでTCPStore rendezvousを使用
        init_method = f"tcp://{master_addr}:{self.config.master_port}"
        timeout = timedelta(minutes=self.config.init_timeout_minutes)
        max_retries = 5
        retry_delay = 10

        # 一斉接続（thundering herd）を避けるためランダムウェイト
        random_delay = self.config.rank * 2
        _log("INFO", f"Waiting {random_delay}s before connecting to avoid thundering herd...")
        time.sleep(random_delay)

        _log(
            "INFO",
            f"Initializing process group: backend=gloo, init={init_method}, "
            f"world_size={self.config.world_size}, rank={self.config.rank}, timeout={timeout}",
        )
        _log("INFO", f"Waiting for {self.config.world_size} nodes to join...")
        start = time.monotonic()

        for attempt in range(1, max_retries + 1):
            try:
                dist.init_process_group(
                    backend="gloo",
                    init_method=init_method,
                    world_size=self.config.world_size,
                    rank=self.config.rank,
                    timeout=timeout,
                )
                elapsed = time.monotonic() - start
                _log("OK", f"Process group initialized on attempt {attempt} ({elapsed:.1f}s)")
                return
            except Exception as e:
                _log("WARN", f"Attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    _log("INFO", f"Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    raise

    def _allocate_communication_buffers(self) -> None:
        """
        ゼロアロケーション通信用バッファを事前確保する。

        推論ループ中にtorch.zerosやtensor.clone等を一切実行せず、
        事前に確保したバッファへのin-place受信・コピーのみで通信を行う。
        """

        shape = (
            self.config.batch_size,
            self.config.seq_len,
            self.config.hidden_size,
        )

        _log("INFO", f"Allocating communication buffers: shape={shape}, dtype=bfloat16")
        start = time.monotonic()

        self.recv_buffers = [
            torch.zeros(shape, dtype=torch.bfloat16)
            for _ in range(self.config.num_micro_batches)
        ]
        self.send_buffers = [
            torch.zeros(shape, dtype=torch.bfloat16)
            for _ in range(self.config.num_micro_batches)
        ]

        elapsed = time.monotonic() - start
        buffer_bytes = (
            self.recv_buffers[0].nelement()
            * self.recv_buffers[0].element_size()
            * 2  # recv + send
            * self.config.num_micro_batches
        )
        _log(
            "OK",
            f"Comm buffers allocated: {buffer_bytes / (1024 * 1024):.2f} MB ({elapsed:.2f}s)",
        )

    def _load_local_weights(self) -> list:
        """
        ローカルディスクからモデル重みを読み込む。

        safetensors 形式または PyTorch 形式のファイルに対応。
        ファイルが存在しない場合はモックレイヤーで代替する。
        """

        assigned_layers = self.config.get_assigned_layers()
        ext = (
            "safetensors"
            if self.config.weight_format == "safetensors"
            else "pt"
        )
        _log(
            "INFO",
            f"Loading weights: layers={assigned_layers}, format={ext}, path={self.config.model_path}",
        )

        loaded_layers: list = []
        for i, layer_idx in enumerate(assigned_layers):
            weight_file = os.path.join(
                self.config.model_path, f"layer_{layer_idx}.{ext}"
            )
            _log("INFO", f"  [{i+1}/{len(assigned_layers)}] Loading layer {layer_idx}...")
            start = time.monotonic()

            if os.path.exists(weight_file):
                layer_weights = self._load_weight_file(weight_file)
                loaded_layers.append(self._build_transformer_layer(layer_weights))
                elapsed = time.monotonic() - start
                _log("INFO", f"  [{i+1}/{len(assigned_layers)}] Layer {layer_idx} loaded ({elapsed:.2f}s)")
            else:
                _log(
                    "WARN",
                    f"  [{i+1}/{len(assigned_layers)}] Weight file not found: {weight_file} (using mock layer {layer_idx})",
                )
                loaded_layers.append(self._build_transformer_layer(None))

        _log("OK", f"Weights loaded: {len(loaded_layers)} layers")
        return loaded_layers

    @staticmethod
    def _load_weight_file(path: str) -> dict[str, torch.Tensor] | None:
        """safetensors または PyTorch 形式で重みファイルを読み込む"""

        if path.endswith(".safetensors"):
            try:
                from safetensors.torch import load_file
                return load_file(path)
            except ImportError:
                print(
                    "[x ERROR] 'pip install safetensors' is required.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            return torch.load(
                path, map_location="cpu", mmap=True, weights_only=True
            )

    @staticmethod
    def _build_transformer_layer(weights: object):
        """
        Transformerブロックの構築（モック実装）。

        実際のプロダクション環境では、ここに完全なTransformerDecoderLayerの
        実装（Self-Attention + FFN + RMSNorm）が入る。
        本モックではパイプライン通信の検証のため、入力をそのまま出力する
        アイデンティティ演算を行う。
        """

        def forward(tensor: torch.Tensor) -> torch.Tensor:
            return tensor

        return forward

 
    def _process_microbatch(self, mb: int, step_count: int, step_start_time: float, pbar: tqdm | None) -> None:
        """1ステップ分のマイクロバッチを処理する（通常パイプライン用）

        リレーモード実行中は通信をスキップし、バッファ競合を防止する。
        """

        # リレー実行中は通信をスキップ（relayがrecv_buffers/send_buffersを独占）
        global _relay_active
        if _relay_active:
            return

        # [A] データ受け取り（タイムアウト付き）
        if self.config.prev_rank is None:
            self.recv_buffers[mb].normal_(mean=0.0, std=INPUT_STDDEV)
        else:
            try:
                dist.recv(tensor=self.recv_buffers[mb], src=self.config.prev_rank, timeout=timedelta(seconds=2))
            except Exception:
                # タイムアウトまたはエラー時は何もしない
                return

        # [B] 計算
        hidden_state = self.recv_buffers[mb]
        for layer in self.my_layers:
            hidden_state = layer(hidden_state)
        self.send_buffers[mb].copy_(hidden_state)

        # [C] 転送（タイムアウト付き）
        if self.config.next_rank is None:
            # 最終段: 進捗表示
            if mb == (self.config.num_micro_batches - 1):
                step_elapsed = time.monotonic() - step_start_time
                if pbar is not None:
                    pbar.set_postfix_str(f"{step_elapsed:.3f}s", refresh=False)
                    pbar.update(1)
        else:
            try:
                dist.send(tensor=self.send_buffers[mb], dst=self.config.next_rank, timeout=timedelta(seconds=2))
            except Exception:
                pass

    def process_pipeline_inference(self) -> None:
        """
        メイン推論ループ。

        バリア後にリレー通信でリクエストを処理する。
        HTTPリクエスト受信時:
          1. 全ノードへの信号送信（10秒待機）
          2. リレー通信（Rank 0送信→他ランク中継→最終段結果返却）
        """

        # 全ノードの起動同期（短時間待機して全てのカンファレンスが完了するのを待つ）
        # dist.barrier() はGloo接続を占有するため、接続切れの原因になる。
        # 代わりに単に待機してbarrier完了フラグを立てる。
        _log("INFO", f"All nodes connected. Waiting for model loading to complete...")
        time.sleep(5.0)
        # barrier完了をHTTPハンドラに通知
        global _barrier_done
        _barrier_done = True
        _log("OK", f"Inference loop started. micro_batches={self.config.num_micro_batches}, pipeline_stages={self.config.world_size}")

        # メインスレッド待機 + リクエスト処理（HTTPサーバーが稼働し続けるため）
        # 全ノード: _SIGNAL_PORT でシグナル接続を待機（ソケットベース）
        _signal_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _signal_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _signal_socket.bind(("0.0.0.0", _SIGNAL_PORT))
        _signal_socket.listen(16)
        _signal_socket.settimeout(1.0)

        try:
            while not _shutdown_requested:
                # シグナルソケットで接続を受容
                try:
                    _signal_socket.accept()
                    _log("INFO", f"Rank {self.config.rank}: detected request signal")
                    _request_event.set()
                except socket.timeout:
                    pass
                except OSError:
                    pass

                if _request_event.is_set():
                    global _request_prompt, _request_result
                    # 他ノードが信号を検知する時間を確保
                    time.sleep(10.0)
                    # リレーモード: 全ノードでリレー通信
                    with _request_lock:
                        prompt = _request_prompt
                    _request_event.clear()
                    with _request_lock:
                        _request_prompt = None
                    decoded = self._relay_request(prompt or "")
                    _request_result = decoded
                    _result_available.set()
        finally:
            _signal_socket.close()

    def _pipeline_loop(self) -> None:
        """バックグラウンドスレッドで実行されるパイプライン推論ループ

        Rank 0（prev_rank=None）は入力データを生成して Rank 1 に送信。
        """

        is_last_node = (self.config.next_rank is None)
        pbar: tqdm | None = None
        if is_last_node:
            pbar = tqdm(
                desc=f"R{self.config.rank}",
                initial=0,
                unit="step",
                dynamic_ncols=True,
                bar_format="{desc} |{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            )

        step_count = 0
        while not _shutdown_requested and not _pipeline_stopped:
            step_start_time = time.monotonic()
            for mb in range(self.config.num_micro_batches):
                self._process_microbatch(mb, step_count, step_start_time, pbar)
            step_count += 1

        if pbar is not None:
            pbar.set_description(f"R{self.config.rank} (stopped)")
            pbar.close()
        if _shutdown_requested:
            _log(
                "INFO",
                f"Inference loop ended: {step_count} steps executed. Cleaning up...",
            )
            self._cleanup()
        else:
            _log(
                "INFO",
                f"Inference loop paused: {step_count} steps executed. Relay mode active.",
            )

    def _cleanup(self) -> None:
        """Glooバックエンドの分散プロセスグループを破棄し、リソースを解放する"""

        if dist.is_initialized():
            dist.destroy_process_group()
            _log("INFO", "Process group destroyed.")

    def _relay_request(self, prompt: str) -> str:
        """
        リレーモードで1リクエストを処理する。

        Rank 0: プロンプトをエンコード → dist.send(dst=1)
        他ランク: prevから受信 → 計算 → nextへ送信（最終段のみ結果返却）
        リレー中はパイプラインループとのバッファ競合を防止するため、
        全体としてロックする（timeout付き）。
        """
        global _relay_active
        with _relay_lock:
            _relay_active = True

        try:
            if self.config.rank == 0:
                shape = (self.config.batch_size,
                         self.config.seq_len,
                         self.config.hidden_size)
                input_tensor = _encode_prompt(prompt, shape)
                _log("INFO", f"Rank 0: sending input to rank 1")
                dist.send(input_tensor, dst=1)
                result_buf = torch.zeros(shape, dtype=torch.bfloat16)
                _log("INFO", f"Rank 0: waiting for result from rank {self.config.world_size - 1}")
                dist.recv(tensor=result_buf, src=self.config.world_size - 1)
                return _decode_result(result_buf)
            else:
                hidden_state = self.recv_buffers[0]
                if self.config.prev_rank is not None:
                    _log("INFO", f"Rank {self.config.rank}: receiving from rank {self.config.prev_rank}")
                    dist.recv(tensor=hidden_state, src=self.config.prev_rank)
                for layer in self.my_layers:
                    hidden_state = layer(hidden_state)
                self.send_buffers[0].copy_(hidden_state)
                if self.config.next_rank is not None:
                    _log("INFO", f"Rank {self.config.rank}: sending to rank {self.config.next_rank}")
                    dist.send(tensor=self.send_buffers[0], dst=self.config.next_rank)
                else:
                    _log("INFO", f"Rank {self.config.rank}: sending result back to rank 0")
                    dist.send(tensor=self.send_buffers[0], dst=0)
                    return _decode_result(self.send_buffers[0])
        finally:
            with _relay_lock:
                _relay_active = False


# ====================================================================
# HTTP サーバー（Rank 0 のみ）
# ====================================================================

class _PredictHandler(BaseHTTPRequestHandler):
    """POST /predict でプロンプトを受け取り、パイプライン推論結果を返す"""

    def log_message(self, format, *args):
        pass  # 標準 stderr ログを抑制

    def do_POST(self):
        if self.path != "/predict":
            self._respond(404, '{"error": "not found"}')
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            prompt = body.get("prompt", "")
        except Exception:
            self._respond(400, '{"error": "invalid json"}')
            return

        if not prompt:
            self._respond(400, '{"error": "empty prompt"}')
            return

        self._handle_predict(prompt)

    def _respond(self, code: int, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _handle_predict(self, prompt: str) -> None:

        if not dist.is_initialized():
            self._respond(503, '{"error": "process group not ready"}')
            return

        # barrier完了前はリクエストを拒否
        global _barrier_done
        if not _barrier_done:
            self._respond(503, '{"error": "barrier not completed"}')
            return

        _log("INFO", f"Request received: prompt='{prompt[:60]}...'")

        try:
            # プロンプトを保存
            global _request_prompt
            with _request_lock:
                _request_prompt = prompt
            # 全ノードへのシグナル（ソケット接続）
            node_ips_str = os.environ.get("NODE_IPS", "")
            if node_ips_str:
                node_ips = node_ips_str.split(",")
            else:
                node_ips = []
            for node_ip in node_ips:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                try:
                    s.connect((node_ip, _SIGNAL_PORT))
                    s.close()
                except Exception:
                    pass
            # 全ノードが検知する時間を確保
            time.sleep(1.0)
            _request_event.set()

            # 結果が返ってくるまで待つ
            _result_available.wait()
            global _request_result
            decoded = _request_result
            _request_result = None
            _result_available.clear()

            _log("RESULT", f"Request response: '{decoded[:100]}'")
            self._respond(200, json.dumps({"result": decoded}))
        except Exception:
            _log("ERROR", f"Request failed: {traceback.format_exc()}")
            self._respond(500, '{"error": "request failed"}')


def _decode_result(tensor: torch.Tensor) -> str:
    """推論結果テンソルを文字列にデコードする。"""
    chars = [round(v.item() * 255) for v in tensor.flatten() if v.item() > 0]
    return "".join(chr(c) for c in chars if 32 <= c < 0x110000)


def _start_http_server(config: PipelineConfig, node: "FullyOptimizedPipelineNode", host: str = "0.0.0.0", port: int = 8082) -> None:
    """HTTP サーバーを開始する（バックグラウンドスレッド）。"""
    server = HTTPServer((host, port), _PredictHandler)
    server.pipeline_config = config
    server.node = node
    server.request_queue_size = 16
    _log("INFO", f"HTTP server listening on {host}:{port}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


# ====================================================================
# エントリポイント
# ====================================================================

def main() -> None:
    """エントリポイント"""

    # 環境変数の事前検証
    required_env_vars = ["RANK", "WORLD_SIZE", "MASTER_ADDR"]
    missing = [v for v in required_env_vars if v not in os.environ]
    if missing:
        print(
            f"[x ERROR] Missing environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)

    config = PipelineConfig()

    assigned = config.get_assigned_layers()
    _log("STEP", "=" * 60)
    _log("INFO", "Distributed LLM pipeline node starting")
    _log("INFO", f"  Rank: {config.rank} / {config.world_size}")
    _log("INFO", f"  Assigned layers: {'none (TCPStore only)' if not assigned else assigned}")
    _log("INFO", f"  Hidden size: {config.hidden_size}")
    _log("INFO", f"  Weight format: {config.weight_format}")
    _log("INFO", f"  Master: {config.master_addr}:{config.master_port}")
    _log("STEP", "=" * 60)

    try:
        node = FullyOptimizedPipelineNode(config)

        # Rank 0 のみ HTTP サーバー起動（管理ノードからのリクエスト受理用）
        if config.rank == 0:
            _start_http_server(config, node)

        node.process_pipeline_inference()
    except Exception:
        print(f"[x ERROR] Fatal error at rank {config.rank}: {traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
