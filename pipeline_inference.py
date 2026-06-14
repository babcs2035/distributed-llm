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
import torch.nn.functional as F
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
# HTTPハンドラがリクエストを開始したかどうかのフラグ
_http_initiated = False
# リレー用ACK受信用ポート
_RELAY_PORT = 8083
# パイプラインループ停止フラグ
_pipeline_stopped = False
# トークナイザーとモデル部品（遅延ロード）
_model_name = None
_tokenizer = None
_embed_tokens = None
_lm_head = None
_final_norm = None


def _tokenize(prompt: str) -> torch.Tensor:
    """プロンプト文字列をトークン ID に変換する。"""
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(
            _model_name, trust_remote_code=True,
        )
    inputs = _tokenizer(prompt, return_tensors="pt")
    return inputs.input_ids  # shape: (1, seq_len)


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
    DIM = "\033[2m"
    NC = "\033[0m"  # No Color


if not sys.stdout.isatty():
    _Color.RED = ""
    _Color.GREEN = ""
    _Color.YELLOW = ""
    _Color.BLUE = ""
    _Color.BOLD = ""
    _Color.DIM = ""
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
    "TRACE": f"{_Color.DIM}.{_Color.NC} TRACE",
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
        self.model_name = file_config.get("model", {}).get("name", "")
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

        # 3. KV-cache 初期化 (レイヤー構築より前に必要)
        self._init_kv_cache()

        # 4. ゼロアロケーション通信用の事前確保バッファ
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

    def _resolve_master_ip(self) -> str:
        """MASTER_ADDRを解決し、ループバック以外の外部IPを返す。"""
        master_addr = self.config.master_addr
        try:
            resolved = socket.gethostbyname(master_addr)
            if not resolved.startswith("127."):
                return resolved
            ifc = os.environ.get("GLOO_SOCKET_IFNAME", "eth0")
            return self._get_external_ip(ifc)
        except socket.gaierror:
            pass
        return self.config.master_addr

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
                # Rank 0: 自分のIPを全ノードにbroadcast
                if self.config.rank == 0:
                    rank0_ip = self._resolve_master_ip()
                    ip_buf = torch.tensor(list(rank0_ip.encode()), dtype=torch.uint8)
                    dist.broadcast(ip_buf, src=0)
                    self._rank0_ip_bytes = ip_buf.tolist()
                    _log("INFO", f"Rank 0: broadcast rank0_ip={rank0_ip}")
                else:
                    ip_buf = torch.zeros(16, dtype=torch.uint8)
                    dist.broadcast(ip_buf, src=0)
                    self._rank0_ip_bytes = ip_buf.tolist()
                    _log("INFO", f"Rank {self.config.rank}: received rank0_ip={bytes(ip_buf.tolist()).decode()}")
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

    def _init_kv_cache(self) -> None:
        """
        各レイヤーの KV-cache を初期化する。

        推論用に、各レイヤーの key_cache と value_cache を事前確保する。
        cache は (batch, num_kv_heads, max_seq_len, head_dim) 形式。
        """

        from transformers import AutoConfig
        config = AutoConfig.from_pretrained(_model_name, trust_remote_code=True)
        text_config = config.text_config if hasattr(config, "text_config") else config

        self.kv_cache: dict[int, tuple[torch.Tensor, torch.Tensor]] = {}
        max_gen_tokens = 128  # 最大生成トークン数

        for layer_idx in range(text_config.num_hidden_layers):
            layer_type = text_config.layer_types[layer_idx]
            is_sliding = (layer_type == "sliding_attention")
            head_dim = text_config.head_dim if is_sliding else (getattr(text_config, "global_head_dim", None) or text_config.head_dim)

            if is_sliding:
                n_kv_heads = text_config.num_key_value_heads
            else:
                n_kv_heads = getattr(text_config, "num_global_key_value_heads", text_config.num_key_value_heads)

            # KV-cache: (batch=1, num_kv_heads, max_gen_tokens, head_dim)
            # 推論時は各ステップで 1 トークン追加するため、max_gen_tokens 分を確保
            key_cache = torch.zeros(
                (1, n_kv_heads, max_gen_tokens, head_dim),
                dtype=torch.bfloat16,
            )
            value_cache = torch.zeros(
                (1, n_kv_heads, max_gen_tokens, head_dim),
                dtype=torch.bfloat16,
            )
            self.kv_cache[layer_idx] = (key_cache, value_cache)

        # 全レイヤー共有の書き込み位置カウンター（リクエスト間でリセット可能）
        self._kv_cache_write_pos_ref = [0]

        _log(
            "OK",
            f"KV-cache initialized: {len(self.kv_cache)} layers, max_gen_tokens={max_gen_tokens}",
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
                loaded_layers.append(self._build_transformer_layer(weight_file, layer_idx, self.config.rank, self.kv_cache, self._kv_cache_write_pos_ref))
                elapsed = time.monotonic() - start
                _log("INFO", f"  [{i+1}/{len(assigned_layers)}] Layer {layer_idx} loaded ({elapsed:.2f}s)")
            else:
                _log(
                    "WARN",
                    f"  [{i+1}/{len(assigned_layers)}] Weight file not found: {weight_file} (using mock layer {layer_idx})",
                )
                loaded_layers.append(self._build_transformer_layer("", layer_idx, self.config.rank, self.kv_cache, self._kv_cache_write_pos_ref))

        # Rank 0: embed_tokens を読み込み
        if self.config.rank == 0:
            embed_file = os.path.join(self.config.model_path, f"embed_tokens.{ext}")
            if os.path.exists(embed_file):
                weights = self._load_weight_file(embed_file)
                for k, v in weights.items():
                    if "embed_tokens" in k:
                        _log("INFO", f"Rank 0: loading embed_tokens ({v.shape})")
                        _load_embed_tokens(v)
            else:
                _log("WARN", "Rank 0: embed_tokens file not found")
        else:
            _log("INFO", f"Rank {self.config.rank}: no embed_tokens needed")

        # 最終ランク: lm_head + final_norm を読み込み
        is_last_rank = (self.config.next_rank is None)
        if is_last_rank:
            lm_head_file = os.path.join(self.config.model_path, f"lm_head.{ext}")
            if os.path.exists(lm_head_file):
                weights = self._load_weight_file(lm_head_file)
                for k, v in weights.items():
                    if "lm_head" in k:
                        _log("INFO", f"Rank {self.config.rank}: loading lm_head ({v.shape})")
                        _load_lm_head(v)
            else:
                _log("WARN", "Rank 50: lm_head file not found")
            _log("INFO", f"Rank {self.config.rank}: loading final_norm")
            _load_final_norm()
        else:
            _log("INFO", f"Rank {self.config.rank}: no lm_head/final_norm needed")

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

    def _build_transformer_layer(self, weight_file: str, layer_idx: int, rank: int = 0, kv_cache: dict[int, tuple[torch.Tensor, torch.Tensor]] | None = None, kv_cache_write_pos_ref: list[int] | None = None):
        """safetensors 重みからミニマルデコーダーレイヤーを構築する。

        KV-cache 対応により、推論時に逐次トークンを効率的に処理できる。
        各ステップで new KV を cache に追加し、attention は full cache を使用。
        """

        from safetensors.torch import load_file
        from transformers import AutoConfig

        config = AutoConfig.from_pretrained(
            _model_name, trust_remote_code=True,
        )
        text_config = config.text_config if hasattr(config, "text_config") else config

        layer_type = text_config.layer_types[layer_idx]
        is_sliding = (layer_type == "sliding_attention")
        head_dim = text_config.head_dim if is_sliding else (getattr(text_config, "global_head_dim", None) or text_config.head_dim)
        rope_cfg = text_config.rope_parameters[layer_type]
        rms_norm_eps = text_config.rms_norm_eps

        # sliding_attention: num_key_value_heads, full_attention: num_global_key_value_heads
        if is_sliding:
            n_kv_heads = text_config.num_key_value_heads
        else:
            n_kv_heads = getattr(text_config, "num_global_key_value_heads", text_config.num_key_value_heads)

        # Gemma 4: attention scaling & softcapping
        # Gemma 4 uses standard head_dim**-0.5 scaling (no query_pre_attn_scalar)
        # Gemma 4 has final_logit_softcapping but NO attn_logit_softcapping
        query_pre_attn_scalar = head_dim
        attn_logit_softcapping = 0.0
        final_logit_softcapping = getattr(text_config, "final_logit_softcapping", 0.0)

        # Load weights
        if weight_file:
            weights = load_file(weight_file)
            prefix = f"model.language_model.layers.{layer_idx}."
            state_dict = {}
            for k, v in weights.items():
                if k.startswith(prefix):
                    state_dict[k[len(prefix):]] = v
        else:
            state_dict = {}

        _layer_idx = layer_idx
        _rank = rank
        _kv_cache = kv_cache
        # 共有書き込み位置カウンター（Noneの場合はローカル作成）
        _kv_cache_write_pos = kv_cache_write_pos_ref if kv_cache_write_pos_ref is not None else [0]

        def forward(hidden_state: torch.Tensor, position_ids: torch.Tensor) -> torch.Tensor:
            import time as _time
            _t0 = _time.monotonic()
            _bh, _sl, _hs = hidden_state.shape
            _t_layer = _time.monotonic()

            # --- 詳細ログ: 入力統計 ---
            _log("TRACE", f"R{_rank} L{_layer_idx} [{layer_type}] IN shape=({_bh},{_sl},{_hs}) dtype={hidden_state.dtype} mean={hidden_state.mean().item():.6f} std={hidden_state.std().item():.6f} min={hidden_state.min().item():.6f} max={hidden_state.max().item():.6f}")

            # Residual
            residual = hidden_state

            # --- Pre-attention RMSNorm ---
            _t1 = _time.monotonic()
            hidden_state = _rms_norm(hidden_state, state_dict.get("input_layernorm.weight"), rms_norm_eps)
            _log("TRACE", f"R{_rank} L{_layer_idx} input_layernorm dt={_time.monotonic()-_t1:.4f}s")

            # --- Self-attention: linear projections ---
            _t2 = _time.monotonic()
            q = F.linear(hidden_state, state_dict["self_attn.q_proj.weight"])  # (bh, sl, n_heads*head_dim)
            k = F.linear(hidden_state, state_dict["self_attn.k_proj.weight"])
            v = F.linear(hidden_state, state_dict["self_attn.v_proj.weight"]) if "self_attn.v_proj.weight" in state_dict else k
            _log("TRACE", f"R{_rank} L{_layer_idx} qkv_proj q=({_bh},{_sl},{text_config.num_attention_heads*head_dim}) k/v=({_bh},{_sl},{n_kv_heads*head_dim}) dt={_time.monotonic()-_t2:.4f}s")

            # --- Reshape for attention ---
            _t3 = _time.monotonic()
            n_heads = text_config.num_attention_heads
            q = q.view(q.size(0), q.size(1), n_heads, head_dim).transpose(1, 2)  # (bh, n_heads, sl, head_dim)
            k = k.view(k.size(0), k.size(1), n_kv_heads, head_dim).transpose(1, 2)
            v = v.view(v.size(0), v.size(1), n_kv_heads, head_dim).transpose(1, 2)
            _log("TRACE", f"R{_rank} L{_layer_idx} reshape q=({_bh},{n_heads},{_sl},{head_dim}) k/v=({_bh},{n_kv_heads},{_sl},{head_dim}) dt={_time.monotonic()-_t3:.4f}s")

            # --- RMSNorm AFTER reshape (q_norm/k_norm) ---
            _t4 = _time.monotonic()
            q = _rms_norm(q, state_dict.get("self_attn.q_norm.weight"), rms_norm_eps)
            k = _rms_norm(k, state_dict.get("self_attn.k_norm.weight"), rms_norm_eps)
            _log("TRACE", f"R{_rank} L{_layer_idx} q_norm/k_norm dt={_time.monotonic()-_t4:.4f}s")

            # --- Apply RoPE ---
            _t5 = _time.monotonic()
            q = _apply_rope(q, position_ids, rope_cfg, text_config)
            k = _apply_rope(k, position_ids, rope_cfg, text_config)
            _log("TRACE", f"R{_rank} L{_layer_idx} rope dt={_time.monotonic()-_t5:.4f}s")

            # --- KV-cache: append new KV to cache (position-based for autoregressive) ---
            _t6 = _time.monotonic()
            if _kv_cache is not None and layer_idx in _kv_cache:
                nonlocal _kv_cache_write_pos
                key_cache, value_cache = _kv_cache[layer_idx]
                _sl = q.shape[2]
                # 書き込み位置: step 0 では prompt 全体、step 1+ では新トークン1つ
                write_pos = _kv_cache_write_pos[0]
                _kv_cache_write_pos[0] += _sl
                # 新しい KV を cache に書き込み
                key_cache[:, :, write_pos:write_pos+_sl, :] = k
                value_cache[:, :, write_pos:write_pos+_sl, :] = v
                # attention は full cache (0..write_pos+_sl-1) を使用
                k_full = key_cache[:, :, :write_pos+_sl, :]
                v_full = value_cache[:, :, :write_pos+_sl, :]
                _log("TRACE", f"R{_rank} L{_layer_idx} kv_cache pos={write_pos}+{_sl}={write_pos+_sl}/{key_cache.size(2)} dt={_time.monotonic()-_t6:.4f}s")
            else:
                k_full = k
                v_full = v
                _log("TRACE", f"R{_rank} L{_layer_idx} kv_cache skip (not available) dt={_time.monotonic()-_t6:.4f}s")

            # --- KV groups (GQA) ---
            _t7 = _time.monotonic()
            if n_heads != n_kv_heads:
                k_full = k_full.repeat_interleave(n_heads // n_kv_heads, dim=1)
                v_full = v_full.repeat_interleave(n_heads // n_kv_heads, dim=1)
            _log("TRACE", f"R{_rank} L{_layer_idx} gqa_expand heads={n_heads} kv_heads={n_kv_heads} ratio={n_heads//n_kv_heads} dt={_time.monotonic()-_t7:.4f}s")

            # --- Attention scores ---
            _t8 = _time.monotonic()
            scores = torch.matmul(q, k_full.transpose(2, 3)) * (query_pre_attn_scalar ** -0.5)  # (bh, n_heads, 1, cache_len)
            # Gemma 2: logit softcapping
            if attn_logit_softcapping > 0:
                scores = torch.tanh(scores / attn_logit_softcapping) * attn_logit_softcapping
            attn_weights = F.softmax(scores, dim=-1)
            attn_output = torch.matmul(attn_weights, v_full)  # (bh, n_heads, 1, head_dim)
            _log("TRACE", f"R{_rank} L{_layer_idx} attention scores=({_bh},{n_heads},1,{write_pos+_sl}) attn_out=({_bh},{n_heads},1,{head_dim}) dt={_time.monotonic()-_t8:.4f}s")

            # --- Reshape back + output projection ---
            _t9 = _time.monotonic()
            attn_output = attn_output.transpose(1, 2).contiguous()  # (bh, 1, n_heads*head_dim)
            attn_output = attn_output.view(attn_output.size(0), attn_output.size(1), -1)  # (bh, 1, hidden)
            attn_output = F.linear(attn_output, state_dict["self_attn.o_proj.weight"])
            _log("TRACE", f"R{_rank} L{_layer_idx} o_proj attn_out=({_bh},{_sl},{_hs}) dt={_time.monotonic()-_t9:.4f}s")

            # --- Post-attention RMSNorm + residual ---
            _t10 = _time.monotonic()
            hidden_state = residual + _rms_norm(attn_output, state_dict.get("post_attention_layernorm.weight"), rms_norm_eps)
            _log("TRACE", f"R{_rank} L{_layer_idx} post_attn_norm+residual dt={_time.monotonic()-_t10:.4f}s")

            # --- Pre-FFN RMSNorm ---
            _t11 = _time.monotonic()
            residual_ffn = hidden_state
            hidden_state = _rms_norm(hidden_state, state_dict.get("pre_feedforward_layernorm.weight"), rms_norm_eps)

            # --- MLP (GELU) ---
            gate = F.linear(hidden_state, state_dict["mlp.gate_proj.weight"])
            up = F.linear(hidden_state, state_dict["mlp.up_proj.weight"])
            hidden_state = F.gelu(gate, approximate="tanh") * up
            hidden_state = F.linear(hidden_state, state_dict["mlp.down_proj.weight"])
            _log("TRACE", f"R{_rank} L{_layer_idx} mlp gate=({_bh},{_sl},{_hs}) up=({_bh},{_sl},{_hs}) down=({_bh},{_sl},{_hs}) dt={_time.monotonic()-_t11:.4f}s")

            # --- Post-FFN RMSNorm + residual ---
            _t12 = _time.monotonic()
            hidden_state = _rms_norm(hidden_state, state_dict.get("post_feedforward_layernorm.weight"), rms_norm_eps)
            hidden_state = residual_ffn + hidden_state
            _log("TRACE", f"R{_rank} L{_layer_idx} post_ffn_norm+residual dt={_time.monotonic()-_t12:.4f}s")

            # --- Layer scalar ---
            layer_scalar = state_dict.get("layer_scalar", torch.ones(1))
            hidden_state = hidden_state * layer_scalar

            _dt = _time.monotonic() - _t0
            _log("TRACE", f"R{_rank} L{_layer_idx} DONE total={_dt:.4f}s mean={hidden_state.mean().item():.6f} std={hidden_state.std().item():.6f} min={hidden_state.min().item():.6f} max={hidden_state.max().item():.6f}")
            return hidden_state

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
                dist.recv(tensor=self.recv_buffers[mb], src=self.config.prev_rank)
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
                dist.send(tensor=self.send_buffers[mb], dst=self.config.next_rank)
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
        # 最終ランクのthundering herd待機(100秒) + PG初期化時間を考慮
        time.sleep(120.0)
        # barrier完了をHTTPハンドラに通知
        # 最終ランクのthundering herd待機(100秒)を考慮し、十分待機してからリクエスト受け付け開始
        global _barrier_done, _request_prompt, _request_result
        _barrier_done_time = time.monotonic()
        _barrier_done = True
        _log("OK", f"Inference loop started. micro_batches={self.config.num_micro_batches}, pipeline_stages={self.config.world_size}")

        # メインスレッド待機 + リクエスト処理（HTTPサーバーが稼働し続けるため）
        # 全ノード: _SIGNAL_PORT でシグナル接続を待機（ソケットベース）
        _signal_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _signal_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _signal_socket.bind(("0.0.0.0", _SIGNAL_PORT))
        _signal_socket.listen(16)
        _signal_socket.settimeout(0.5)

        try:
            while not _shutdown_requested:
                # シグナルソケットで接続を受容（0.5s タイムアウト）
                try:
                    conn, _ = _signal_socket.accept()
                    # クライアントからpromptテキストを受信
                    try:
                        conn.settimeout(2.0)
                        data = conn.recv(65536)
                        if data:
                            prompt_text = data.decode("utf-8").strip()
                            if prompt_text:
                                with _request_lock:
                                    _request_prompt = prompt_text
                                _log("INFO", f"Rank {self.config.rank}: prompt received via signal socket ({len(prompt_text)} chars)")
                                # ACKを同じ接続で返信
                                try:
                                    conn.sendall(b"ACK\n")
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    finally:
                        conn.close()
                except socket.timeout:
                    pass
                except OSError:
                    pass

                # プロンプトチェック
                with _request_lock:
                    prompt = _request_prompt
                    if prompt is not None:
                        _request_prompt = None
                _log("TRACE", f"Rank {self.config.rank}: checking prompt={prompt is not None}")
                if prompt is not None:
                    # Rank 0のsignal threadはrelayを呼ばない（HTTP handlerが呼ぶ）
                    # 他ノードのsignal threadはrelayを呼ぶ
                    if self.config.rank != 0:
                        _request_event.clear()
                        _log("INFO", f"Rank {self.config.rank}: entering relay (prompt_len={len(prompt)})")
                        decoded = self._relay_request(prompt)
                        _request_result = decoded
                        _result_available.set()
                        _log("INFO", f"Rank {self.config.rank}: relay complete, result set")
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

    def _broadcast_prompt_and_wait(self, prompt: str) -> None:
        """
        全ノードにpromptをブロードキャストし、全ノードが受信するのを待機する。

        Rank 0は全ノードのIPに直接接続し、他ノードはRank 0に接続する。
        """
        rank = dist.get_rank()
        world_size = dist.get_world_size()

        # Rank 0のIPを取得（init時にbroadcast済み）
        rank0_ip = bytes(self._rank0_ip_bytes).decode().rstrip("\x00")

        # ノードIPマッピング（環境に応じて調整）
        # Rank 0: wafl-ctrl1 = 192.168.11.10
        # Rank 1-40: wafl100-139 = 192.168.11.100-139
        # Rank 41-50: wafl200-209 = 192.168.12.100-109
        def get_node_ip(r):
            if r == 0:
                return rank0_ip
            elif r <= 40:
                # wafl100 -> 192.168.11.100, wafl101 -> .101, ...
                return f"192.168.11.{100 + (r - 1)}"
            else:
                # wafl200 -> 192.168.12.100, wafl201 -> .101, ...
                return f"192.168.12.{100 + (r - 41)}"

        _log("INFO", f"Rank {rank}: rank0_ip={rank0_ip}")

        # Rank 0以外: Rank 0のsignal portにpromptを送信
        if rank != 0:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect((rank0_ip, _SIGNAL_PORT))
                s.sendall((prompt + "\n").encode("utf-8"))
                ack_data = s.recv(1024)
                s.close()
                if ack_data:
                    _log("INFO", f"Rank {rank}: prompt sent to Rank 0")
            except Exception:
                _log("WARN", f"Rank {rank}: failed to connect to Rank 0")
            return  # 他ノードはRank 0に送ったら終了

        # Rank 0: 全ノードにpromptを送信
        _log("INFO", f"Rank 0: broadcasting to {world_size - 1} nodes")
        ack_count = 0
        for r in range(1, world_size):
            target_ip = get_node_ip(r)
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect((target_ip, _SIGNAL_PORT))
                s.sendall((prompt + "\n").encode("utf-8"))
                ack_data = s.recv(1024)
                s.close()
                if ack_data:
                    ack_count += 1
                    _log("INFO", f"Rank 0: ACK from rank {r} ({target_ip}) ({ack_count}/{world_size - 1})")
            except Exception:
                _log("WARN", f"Rank 0: failed to communicate with rank {r} ({target_ip})")

        _log("INFO", f"Rank 0: broadcast complete ({ack_count} ACKs)")

    def _relay_request(self, prompt: str) -> str:
        """
        リレーモードでオート回帰推論を処理する。

        各ステップ:
          Phase 1 (順チェーン): Rank 0 → Rank 1 → ... → Rank N
            Rank 0: seq_len + hidden_state を Rank 1 に送信
            他ランク: prevから受信 → 計算 → nextへ送信
            Rank N: 計算 → final_norm → lm_head → token_id を next=(rank-1)へ

          Phase 2 (逆チェーン): Rank N → Rank N-1 → ... → Rank 0
            Rank N: token_id を (rank-1)へ送信
            他ランク: nextからtoken_idを受信 → prevへ送信
            Rank 0: Rank 1からtoken_idを受信

        完全なblocking send/recvによりデッドロックを回避。
        """
        import time as _time

        global _relay_active
        with _relay_lock:
            _relay_active = True

        # 全ノードの同期: 全ノードがrelay開始前にここで合流
        try:
            dist.barrier()
        except Exception:
            pass

        # 生成パラメータ
        max_new_tokens = 32
        last_rank = self.config.world_size - 1

        # Gemma 4: final logit softcapping (標準値 30.0)
        final_logit_softcapping = 30.0

        try:
            # --- 全ランク共通: KV-cacheと書き込み位置をリセット ---
            if self.kv_cache:
                for lid, (kc, vc) in self.kv_cache.items():
                    kc.zero_()
                    vc.zero_()
                _log("INFO", f"Rank {self.config.rank}: KV-cache reset ({len(self.kv_cache)} layers)")
            if hasattr(self, '_kv_cache_write_pos_ref'):
                self._kv_cache_write_pos_ref[0] = 0

            if self.config.rank == 0:
                # ===== ステップ 0: プロンプト処理 =====
                _log("INFO", f"Rank 0: prompt='{prompt}'")
                input_ids = _tokenize(prompt)  # (1, seq_len)
                seq_len = input_ids.size(1)
                embed = F.embedding(input_ids, _embed_tokens)  # (1, seq_len, hidden_size)
                _log("INFO", f"Rank 0: prompt tokens={seq_len}, embedding shape={embed.shape} mean={embed.mean().item():.6f} std={embed.std().item():.6f} min={embed.min().item():.6f} max={embed.max().item():.6f}")

                # 生成トークン蓄積
                generated_ids = []

                # ===== ステップ 0..N-1: 統一ループ =====
                for step in range(max_new_tokens):
                    is_first = (step == 0)
                    _log("INFO", f"Rank 0: === step {step}/{max_new_tokens} is_first={is_first} ===")
                    _t_step = _time.monotonic()

                    if is_first:
                        # プロンプトを送信
                        seq_len_buf = torch.tensor([float(seq_len)], dtype=torch.float32)
                        dist.send(seq_len_buf, dst=1)
                        dist.send(embed, dst=1)
                        _log("INFO", f"Rank 0: sent prompt embed (seq_len={seq_len})")
                    else:
                        # 前ステップの生成トークンを埋め込み
                        new_token_tensor = torch.tensor([[generated_ids[-1]]], dtype=torch.long)
                        new_embed = F.embedding(new_token_tensor, _embed_tokens)
                        dist.send(torch.tensor([1.0], dtype=torch.float32), dst=1)
                        dist.send(new_embed, dst=1)
                        _log("INFO", f"Rank 0: step {step} sent token_embed token={generated_ids[-1]}")

                    # 逆チェーン: Rank 1からtoken_idを受信（非同期）
                    token_id_buf = torch.zeros(1, dtype=torch.float32)
                    op = dist.irecv(token_id_buf, src=1)
                    op.wait()
                    token_id = int(token_id_buf.item())
                    generated_ids.append(token_id)

                    step_dt = _time.monotonic() - _t_step
                    _log("INFO", f"Rank 0: step {step} done token={token_id} dt={step_dt:.3f}s")

                    # 特殊トークンで終了
                    if token_id in (2, 3):
                        _log("INFO", f"Rank 0: stop token={token_id} at step {step}")
                        break

                # ===== デコード =====
                _log("INFO", f"Rank 0: decoding {len(generated_ids)} tokens...")
                _t_decode = _time.monotonic()
                result = _tokenizer.decode(generated_ids)
                _log("INFO", f"Rank 0: decoded in {_time.monotonic()-_t_decode:.3f}s: '{result}'")
                return result

            elif self.config.rank == last_rank:
                # ===== 最終ランク: 計算 → final_norm → lm_head → 逆チェーンで返却 =====
                seq_len = 0

                for step in range(max_new_tokens):
                    is_first = (step == 0)
                    _log("INFO", f"Rank {self.config.rank}: === step {step}/{max_new_tokens} is_first={is_first} ===")
                    _t_step = _time.monotonic()

                    # 前ランクからhidden_stateを受信
                    if is_first:
                        seq_len_buf = torch.zeros(1, dtype=torch.float32)
                        dist.recv(tensor=seq_len_buf, src=self.config.prev_rank)
                        recv_seq_len = int(seq_len_buf.item())

                        if recv_seq_len > 1:
                            hidden_state = torch.zeros(
                                (self.config.batch_size, recv_seq_len, self.config.hidden_size),
                                dtype=torch.bfloat16,
                            )
                            _log("INFO", f"Rank {self.config.rank}: allocated hidden for seq_len={recv_seq_len}")
                        else:
                            hidden_state = self.recv_buffers[0]
                        dist.recv(tensor=hidden_state, src=self.config.prev_rank)
                        seq_len = recv_seq_len
                        _log("INFO", f"Rank {self.config.rank}: recv_hidden dt={_time.monotonic()-_t_step:.3f}s")
                    else:
                        seq_len_buf = torch.zeros(1, dtype=torch.float32)
                        hidden_state = self.recv_buffers[0]
                        op_seq = dist.irecv(seq_len_buf, src=self.config.prev_rank)
                        op_hidden = dist.irecv(hidden_state, src=self.config.prev_rank)
                        op_seq.wait()
                        op_hidden.wait()
                        recv_seq_len = int(seq_len_buf.item())

                    # 計算
                    _t = _time.monotonic()
                    if is_first:
                        positions = torch.arange(recv_seq_len, dtype=torch.long).unsqueeze(0)
                    else:
                        pos_id = seq_len + step - 1
                        positions = torch.tensor([[pos_id]], dtype=torch.long)
                    _log("INFO", f"Rank {self.config.rank}: step {step} computing pos_id={positions.max().item()}")

                    for layer in self.my_layers:
                        hidden_state = layer(hidden_state, position_ids=positions)
                    _log("INFO", f"Rank {self.config.rank}: step {step} compute dt={_time.monotonic()-_t:.3f}s hidden_mean={hidden_state.mean().item():.6f} hidden_std={hidden_state.std().item():.6f} hidden_min={hidden_state.min().item():.6f} hidden_max={hidden_state.max().item():.6f}")

                    # final_norm + lm_head
                    last_hidden = hidden_state[:, -1:, :]
                    hidden_state = _final_norm(last_hidden)
                    logits = F.linear(hidden_state, _lm_head)
                    # Gemma 4: final logit softcapping
                    raw_top5_val, raw_top5_id = torch.topk(logits[0, 0], 5)
                    raw_logit_max = raw_top5_val[0].item()
                    raw_diff = (raw_top5_val[0] - raw_top5_val[1]).item()
                    if final_logit_softcapping > 0:
                        logits = torch.tanh(logits / final_logit_softcapping) * final_logit_softcapping
                    token_id = torch.argmax(logits, dim=-1).to(torch.int64)
                    top5_val, top5_id = torch.topk(logits[0, 0], 5)
                    _log("INFO", f"Rank {self.config.rank}: step {step} token_id={token_id.item()} raw_max={raw_logit_max:.2f} raw_diff={raw_diff:.4f} raw_top5={raw_top5_id.tolist()} soft_top5={top5_id.tolist()} soft_vals={top5_val.tolist()}")

                    # 逆チェーン: 前のランクへtoken_idを送信
                    dist.send(token_id.float(), dst=self.config.prev_rank)
                    _log("INFO", f"Rank {self.config.rank}: step {step} sent token_id to prev")

            else:
                # ===== 中間ランク: 順チェーン + 逆チェーン =====
                seq_len = 0

                for step in range(max_new_tokens):
                    is_first = (step == 0)
                    _log("INFO", f"Rank {self.config.rank}: === step {step}/{max_new_tokens} is_first={is_first} ===")
                    _t_step = _time.monotonic()

                    # --- Phase 1: 順チェーン (prev → self → next) ---
                    if is_first:
                        # step 0: Rank 0が先にsendするのでblocking recv
                        seq_len_buf = torch.zeros(1, dtype=torch.float32)
                        dist.recv(tensor=seq_len_buf, src=self.config.prev_rank)
                        recv_seq_len = int(seq_len_buf.item())

                        if recv_seq_len > 1:
                            hidden_state = torch.zeros(
                                (self.config.batch_size, recv_seq_len, self.config.hidden_size),
                                dtype=torch.bfloat16,
                            )
                            _log("INFO", f"Rank {self.config.rank}: allocated hidden for seq_len={recv_seq_len}")
                        else:
                            hidden_state = self.recv_buffers[0]
                        dist.recv(tensor=hidden_state, src=self.config.prev_rank)
                        seq_len = recv_seq_len
                        _log("INFO", f"Rank {self.config.rank}: recv_hidden dt={_time.monotonic()-_t_step:.3f}s")
                    else:
                        # step 1+: Rank 0がsendする前にirecvをposteする
                        seq_len_buf = torch.zeros(1, dtype=torch.float32)
                        hidden_state = self.recv_buffers[0]
                        op_seq = dist.irecv(seq_len_buf, src=self.config.prev_rank)
                        op_hidden = dist.irecv(hidden_state, src=self.config.prev_rank)
                        op_seq.wait()
                        op_hidden.wait()
                        recv_seq_len = int(seq_len_buf.item())

                    # 計算
                    _t = _time.monotonic()
                    if is_first:
                        positions = torch.arange(recv_seq_len, dtype=torch.long).unsqueeze(0)
                    else:
                        pos_id = seq_len + step - 1
                        positions = torch.tensor([[pos_id]], dtype=torch.long)
                    _log("INFO", f"Rank {self.config.rank}: step {step} computing pos_id={positions.max().item()}")

                    for layer in self.my_layers:
                        hidden_state = layer(hidden_state, position_ids=positions)
                    _log("INFO", f"Rank {self.config.rank}: step {step} compute dt={_time.monotonic()-_t:.3f}s hidden_mean={hidden_state.mean().item():.6f} hidden_std={hidden_state.std().item():.6f} hidden_min={hidden_state.min().item():.6f} hidden_max={hidden_state.max().item():.6f}")

                    # nextランクへ送信
                    dist.send(torch.tensor([float(recv_seq_len)], dtype=torch.float32), dst=self.config.next_rank)
                    dist.send(tensor=hidden_state, dst=self.config.next_rank)
                    _log("INFO", f"Rank {self.config.rank}: step {step} send to next dt={_time.monotonic()-_t:.3f}s")

                    # --- Phase 2: 逆チェーン (next → self → prev) ---
                    # nextランクからtoken_idを受信（非同期）
                    token_id_buf = torch.zeros(1, dtype=torch.float32)
                    op_token = dist.irecv(token_id_buf, src=self.config.next_rank)
                    op_token.wait()
                    token_id = int(token_id_buf.item())

                    # prevランクへtoken_idを送信
                    if self.config.prev_rank is not None:
                        dist.send(token_id_buf, dst=self.config.prev_rank)
                    _log("INFO", f"Rank {self.config.rank}: step {step} token_id={token_id}")
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
            raw = self.rfile.read(length)
            _log("INFO", f"Raw request body: {raw}")
            body = json.loads(raw)
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

        # Rank 0 のみリレーモードに対応
        if dist.get_rank() != 0:
            self._respond(500, '{"error": "only rank 0 handles requests"}')
            return

        _log("INFO", f"Request received: prompt='{prompt[:60]}...'")

        try:
            # Rank 0のsignal threadが検知できるようpromptを設定
            global _request_prompt
            with _request_lock:
                _request_prompt = prompt
            self.server.node._broadcast_prompt_and_wait(prompt)
            decoded = self.server.node._relay_request(prompt)
            _log("RESULT", f"Request response: '{decoded[:100]}'")
            self._respond(200, json.dumps({"result": decoded}))
        except Exception:
            _log("ERROR", f"Request failed: {traceback.format_exc()}")
            self._respond(500, '{"error": "request failed"}')


def _load_embed_tokens(weight: torch.Tensor) -> None:
    """埋め込み重みをグローバル変数に設定する。"""
    global _embed_tokens
    _embed_tokens = weight  # shape: (vocab_size, hidden_size)


def _load_lm_head(weight: torch.Tensor) -> None:
    """LM ヘッド重みをグローバル変数に設定する。"""
    global _lm_head
    _lm_head = weight  # shape: (vocab_size, hidden_size)


def _rms_norm(hidden_state: torch.Tensor, weight: torch.Tensor | None, eps: float) -> torch.Tensor:
    """RMSNorm を適用する。"""
    variance = (hidden_state ** 2).mean(-1, keepdim=True)
    hidden_state = hidden_state * torch.rsqrt(variance + eps)
    if weight is not None:
        hidden_state = hidden_state * weight
    return hidden_state


def _apply_rope(
    x: torch.Tensor, position_ids: torch.Tensor, rope_cfg: dict, text_config,
) -> torch.Tensor:
    """Rotary Position Embedding を適用する。"""
    head_dim = x.size(-1)
    rope_theta = rope_cfg["rope_theta"]
    rope_type = rope_cfg.get("rope_type", "default")

    # inv_freq
    inv_freq = 1.0 / (
        rope_theta ** (torch.arange(0, head_dim, 2, dtype=torch.float32, device=x.device) / head_dim)
    )

    # position ids
    max_seq = 2048
    position_ids_pos = position_ids.reshape(-1)  # (seq_len,)

    # freqs: (seq_len, head_dim//2)
    freqs = torch.outer(position_ids_pos.float(), inv_freq)
    emb = freqs  # already head_dim//2; cos/sin will match x1/x2

    # scaling
    attention_scaling = 1.0
    if rope_type == "proportional":
        n_ctx = text_config.max_position_embeddings
        attention_scaling = float(n_ctx) / float(max_seq)

    cos = (emb.cos() * attention_scaling).to(dtype=x.dtype)  # (seq_len, head_dim//2)
    sin = (emb.sin() * attention_scaling).to(dtype=x.dtype)

    # Apply RoPE: x * cos + rotate_half(x) * sin
    x1 = x[..., 0::2]  # even indices
    x2 = x[..., 1::2]  # odd indices
    out = torch.zeros_like(x)
    cos = cos.unsqueeze(0).unsqueeze(0)  # (1, 1, seq_len, head_dim//2)
    sin = sin.unsqueeze(0).unsqueeze(0)
    out[..., 0::2] = x1 * cos - x2 * sin
    out[..., 1::2] = x2 * cos + x1 * sin
    return out


def _get_layer_class(text_config) -> type:
    """モデルタイプに応じたデコーダーレイヤークラスを返す。"""
    model_type = getattr(text_config, "model_type", "")
    if "gemma4" in model_type or "gemma" in model_type:
        from transformers.models.gemma4.modeling_gemma4 import Gemma4TextDecoderLayer
        return Gemma4TextDecoderLayer
    raise ValueError(f"Unsupported model type: {model_type}")


def _load_final_norm() -> None:
    """最終 RMSNorm を構築する。"""
    global _final_norm
    from pathlib import Path

    # Gemma-4 標準の rms_norm_eps。layer 内でも使用されている値と同一。
    eps = 1e-5

    model_path = os.environ.get('MODEL_PATH', '/models')
    nf = Path(model_path) / 'norm.safetensors'
    if nf.exists():
        from safetensors.torch import load_file
        w = load_file(str(nf))
        for k, v in w.items():
            if 'norm' in k:
                _log("INFO", f"Rank: loaded norm.weight from safetensors ({v.shape})")
                _final_norm = lambda x, weight=v, e=eps: _rms_norm(x, weight, e)
                return
        _log("WARN", f"Rank: 'norm' key not found in {nf}")
    else:
        _log("WARN", f"Rank: norm.safetensors not found at {nf}")
    _final_norm = lambda x, e=eps: _rms_norm(x, None, e)


def _decode_result_token(token_id: int) -> str:
    """トークン ID を文字列にデコードする。"""
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(
            _model_name, trust_remote_code=True,
        )
    return _tokenizer.decode([token_id])


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
    global _model_name
    _model_name = config.model_name

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
