"""
Distributed LLM inference pipeline node.

Compute node for LLM inference using serial pipeline parallelism (PP) across N nodes.
Reads model specs (num_layers, hidden_size, etc.) from config.json
and adapts dynamically to the specified model.

Key features:
  - Asymmetric layer assignment (auto-adapts to WORLD_SIZE)
  - Zero-allocation communication (in-place receive via pre-allocated buffers)
  - Micro-batch splitting to minimize pipeline bubble
  - Staggered model loading to avoid congestion
  - Physical NIC binding for Gloo backend stability
  - Supports both safetensors and PyTorch weight formats
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

# S2: Compute dtype.
# Intel i5-8350U lacks AVX-512 BF16 support, causing bfloat16 ops to convert to float32 internally (overhead).
# Using float32 directly leverages optimized BLAS (OpenBLAS/MKL) routines.
COMPUTE_DTYPE = torch.float32

# ====================================================================
# Graceful shutdown signal handler
# ====================================================================

_shutdown_requested = False


def _signal_handler(signum: int, frame: object) -> None:
    """Set graceful shutdown flag on SIGTERM or SIGINT."""

    global _shutdown_requested
    _shutdown_requested = True
    _log("INFO", f"Shutting down (signal={signum}). Exiting inference loop...")


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


# ====================================================================
# Configuration constants
# ====================================================================

DEFAULT_NUM_MICRO_BATCHES = 4
DEFAULT_STAGGER_INTERVAL = 3.0
DEFAULT_INIT_TIMEOUT_MINUTES = 25
DEFAULT_GLOO_TIMEOUT_MS = 3600000  # 60 min: exceeds 100 tokens x 7s = 700s, default 600s is insufficient

# Mock inference constants (mock layer increment value, input initialization stddev)
MOCK_INCREMENT = 0.01
INPUT_STDDEV = 0.02

# ====================================================================
# HTTP request handling (Rank 0 only)
# ====================================================================

# Pipeline thread management (relay mode)
_pipeline_thread: threading.Thread | None = None

# Request notification (socket-based cross-node signaling)
_request_event = threading.Event()
_request_prompt: str | None = None
_request_lock = threading.Lock()
_request_result: str | None = None
_result_available = threading.Event()
# Socket port for non-Rank-0 nodes to connect to Rank 0
# Rank 0 listens, other ranks connect to Rank 0 (bidirectional protocol)
_SIGNAL_PORT = 8081
# Destination port when connecting to Rank 0 (usually same as _SIGNAL_PORT)
_SIGNAL_CONNECT_PORT = 8081
# Barrier done flag (prevent requests before barrier completes)
_barrier_done = False
# Relay active flag (prevent buffer contention with pipeline loop)
_relay_active = False
_relay_lock = threading.Lock()
# Flag indicating whether HTTP handler has initiated a request
_http_initiated = False
_http_lock = threading.Lock()
# Relay ACK receive port (step synchronization)
_RELAY_ACK_PORT = 8083
# Pipeline loop stopped flag
_pipeline_stopped = False
# Tokenizer and model components (lazy loading)
_model_name = None
_tokenizer = None
_embed_tokens = None
_lm_head = None
_final_norm = None


def _tokenize(prompt: str) -> torch.Tensor:
    """Convert prompt string to token IDs.

    Applies Gemma-4 instruction-tuned chat template.
    Raw text without template does not work correctly with instruction-tuned models.
    """
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(
            _model_name, trust_remote_code=True, local_files_only=True, timeout=60,
        )
    messages = [{"role": "user", "content": prompt}]
    # Get template string with tokenize=False, then encode (avoids return type differences)
    chat_text = _tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True,
    )
    import torch as _torch
    token_ids = _tokenizer.encode(chat_text, add_special_tokens=False)
    return _torch.tensor([token_ids], dtype=_torch.long)  # shape: (1, seq_len)


# ====================================================================
# Color output
# ====================================================================

class _Color:
    """ANSI color codes"""

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
# Unified logging function
# ====================================================================

_RANK: int = -1
# Enable TRACE logging only when LOG_LEVEL=TRACE environment variable is set.
# When False, torch.compile recognizes this flag as a guard and
# excludes the entire TRACE block as dead code to prevent graph breaks.
_TRACE_ENABLED: bool = os.environ.get("LOG_LEVEL", "INFO").upper() == "TRACE"
_LOG_LEVELS: dict[str, str] = {
    "INFO": f"{_Color.BLUE}INFO{_Color.NC}",
    "DEBUG": f"{_Color.DIM}DEBUG{_Color.NC}",
    "OK": f"{_Color.GREEN}OK{_Color.NC}",
    "FAIL": f"{_Color.RED}FAIL{_Color.NC}",
    "WARN": f"{_Color.YELLOW}WARN{_Color.NC}",
    "ERROR": f"{_Color.RED}ERROR{_Color.NC}",
    "STEP": f"{_Color.BOLD}STEP{_Color.NC}",
    "RESULT": f"{_Color.GREEN}RESULT{_Color.NC}",
    "TRACE": f"{_Color.DIM}TRACE{_Color.NC}",
}


def _log(level: str, msg: str) -> None:
    """
    Output log in unified format.

    Format:  [R<rank> <LEVEL>] message

    Args:
        level: Log level (INFO, DEBUG, OK, FAIL, WARN, ERROR, STEP, RESULT, TRACE)
        msg: Message to output
    """

    tag = _LOG_LEVELS.get(level, level)
    print(f"[R{_RANK} {tag}] {msg}", flush=True)


# ====================================================================
# Configuration loading
# ====================================================================

def _load_model_config(config_path: str = "config.json") -> dict[str, Any]:
    """Load model config from config.json"""

    path = Path(config_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _resolve_model_specs(config: dict[str, Any]) -> tuple[int, int, int, int]:
    """
    Get model specs from config.json, fill in missing values via AutoConfig.

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

            _log("INFO", f"Loading config via AutoConfig: {model_name}")
            hf_config = AutoConfig.from_pretrained(model_name, trust_remote_code=True, local_files_only=True, timeout=60)

            # Gemma4 etc. nest config inside text_config
            text_config = getattr(hf_config, "text_config", hf_config)
            num_hidden_layers = getattr(text_config, "num_hidden_layers", None)
            hidden_size = getattr(text_config, "hidden_size", None)
            num_attention_heads = getattr(text_config, "num_attention_heads", hidden_size)
            num_key_value_heads = getattr(
                text_config,
                "num_key_value_heads",
                num_attention_heads,
            )

            _log("INFO", f"AutoConfig done: layers={num_hidden_layers}, hidden={hidden_size}")
        except Exception as e:
            _log("ERROR", f"AutoConfig failed: {e}")
            _log("ERROR", "Specify values via config.json model.overrides.")
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
# Pipeline configuration
# ====================================================================

class PipelineConfig:
    """
    Read pipeline inference parameters from environment variables and config.json.

    Required parameters (env vars):
        RANK, WORLD_SIZE, MASTER_ADDR, MASTER_PORT
    Optional parameters (env vars or defaults):
        NUM_MICRO_BATCHES, STAGGER_INTERVAL, BATCH_SIZE, SEQ_LEN
    """

    def __init__(self, config_path: str = "config.json") -> None:
        # Get model specs from config.json
        file_config = _load_model_config(config_path)
        self.model_name = file_config.get("model", {}).get("name", "")
        (
            self.num_hidden_layers,
            self.hidden_size,
            self.num_attention_heads,
            self.num_key_value_heads,
        ) = _resolve_model_specs(file_config)

        # Required parameters: from environment variables
        self.rank = int(os.environ["RANK"])
        self.world_size = int(os.environ["WORLD_SIZE"])
        self.master_addr = os.environ["MASTER_ADDR"]
        self.master_port = int(os.environ.get("MASTER_PORT", "29500"))

        # Optional parameters
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

        # Inference parameters
        self.batch_size = int(os.environ.get("BATCH_SIZE", "1"))
        self.seq_len = int(os.environ.get("SEQ_LEN", "1"))

        # Model path and format
        self.model_path = os.environ.get("MODEL_PATH", "/models")
        self.weight_format = file_config.get("model", {}).get("format", "safetensors")

        # Input validation: node count cannot exceed layer count
        if self.world_size > self.num_hidden_layers:
            _log("ERROR", f"WORLD_SIZE({self.world_size}) exceeds num_hidden_layers({self.num_hidden_layers}).")
            sys.exit(1)

        # Input validation: max layers per node limited to 2
        layers_high = self.num_hidden_layers - self.world_size
        if layers_high > self.world_size:
            min_ws = (self.num_hidden_layers + 1) // 2
            _log("ERROR", f"WORLD_SIZE({self.world_size}) is too small. Minimum {min_ws} nodes required (max 2 layers per node).")
            sys.exit(1)

    @property
    def prev_rank(self) -> int | None:
        """Rank of the previous node (None for Rank 0)"""

        return self.rank - 1 if self.rank > 0 else None

    @property
    def next_rank(self) -> int | None:
        """Rank of the next node (None for the last rank)"""

        return self.rank + 1 if self.rank < (self.world_size - 1) else None

    @property
    def total_layers(self) -> int:
        """Total layer count (same as num_hidden_layers)"""

        return self.num_hidden_layers

    def get_assigned_layers(self) -> list[int]:
        """
        Asymmetric layer assignment scheme.

        Rank 0 (master node) gets no layers (TCPStore only).
        Each node gets at least 1 layer; extra layers are assigned as
        2 layers to nodes from the front.

        Since Rank 0 is empty:
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
# Pipeline inference node
# ====================================================================

class FullyOptimizedPipelineNode:
    """
    Optimized pipeline parallel inference node.

    Implemented optimizations:
      1. Zero-allocation communication: in-place receive via pre-allocated buffers
      2. Micro-batch splitting: minimize pipeline bubble
      3. Asymmetric layer assignment: reduce load on later nodes
      4. Staggered startup: avoid thundering herd problem
      5. Fixed physical NIC: stabilize Gloo backend communication
    """

    def __init__(self, config: PipelineConfig) -> None:
        global _RANK
        _RANK = config.rank
        self.config = config

        # B1: Load and cache HuggingFace config once in the instance.
        # Eliminates duplicate AutoConfig loads in _init_kv_cache and _build_transformer_layer.
        from transformers import AutoConfig as _AutoConfig
        _log("INFO", f"Loading HuggingFace config: {_model_name}")
        _hf_config = _AutoConfig.from_pretrained(
            _model_name, trust_remote_code=True, local_files_only=True, timeout=60,
        )
        self._text_config = _hf_config.text_config if hasattr(_hf_config, "text_config") else _hf_config
        _log("INFO", "HuggingFace config cached")

        # S3: Match OMP/BLAS thread count to CPU core count.
        # Intel i5-8350U runs on 4 cores (cpuset 0-3),
        # so OMP_NUM_THREADS=1 uses only 1 core, making BLAS ops a bottleneck.
        cpu_count = os.cpu_count() or 1
        torch.set_num_threads(cpu_count)
        torch.set_num_interop_threads(1)
        _log("INFO", f"torch.set_num_threads({cpu_count}), set_num_interop_threads(1)")

        # 1. Fixed physical NIC binding for Gloo backend
        self._configure_network_binding()

        # 2. Build distributed process group
        self._init_process_group()

        # 3. Initialize KV-cache (needed before layer construction)
        self._init_kv_cache()

        # 4. Pre-allocated buffers for zero-allocation communication
        self._allocate_communication_buffers()

        # 5. Load model weights
        self.my_layers = self._load_local_weights()

    def _configure_network_binding(self) -> None:
        """Fix the physical NIC used by the Gloo backend"""

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
        Return the IPv4 address of the specified NIC.

        Uses the ip command to get the IP of the physical interface.
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
        """Resolve MASTER_ADDR and return an external IP (not loopback)."""
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

    def _wait_for_master_listener(self, host: str, port: int, timeout: float = 180.0) -> None:
        """Poll via TCP connection until Rank0's TCPStore listener opens the port.

        If lower-rank workers (1,2,3) connect before Rank0 starts listening,
        Connection reset occurs, Gloo/TCPStore rendezvous epochs desync,
        and init_process_group retries cannot recover, causing deadlock.
        To prevent this, workers verify Rank0's port is open before init.
        Connection check is a lightweight reachability probe that closes immediately;
        it does not participate in the TCPStore rendezvous protocol.
        """
        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                with socket.create_connection((host, port), timeout=5.0):
                    _log("INFO", f"Rank {self.config.rank}: master {host}:{port} reachable (attempt {attempt})")
                    return
            except OSError:
                time.sleep(1.0)
        _log("WARN", f"Rank {self.config.rank}: master {host}:{port} unreachable after {timeout}s, proceeding anyway")

    def _init_process_group(self) -> None:
        """Initialize the distributed process group with Gloo backend"""

        # Avoid MASTER_ADDR resolving to localhost inside containers
        # (e.g., wafl-ctrl1 -> 127.0.1.1). Use external IP for loopback.
        master_addr = self.config.master_addr
        try:
            resolved = socket.gethostbyname(master_addr)
            if resolved.startswith("127."):
                # When localhost resolves inside container,
                # get and use external IP directly
                ifc = os.environ.get("GLOO_SOCKET_IFNAME", "eth0")
                master_addr = self._get_external_ip(ifc)
                _log("WARN", f"MASTER_ADDR {self.config.master_addr} resolves to {resolved}, using {master_addr}")
        except socket.gaierror:
            pass

        # Use TCPStore rendezvous with init_method
        init_method = f"tcp://{master_addr}:{self.config.master_port}"
        timeout = timedelta(minutes=self.config.init_timeout_minutes)
        max_retries = 5
        retry_delay = 10

        # Improve Gloo full mesh connection reliability
        os.environ.setdefault("PYTORCH_GLOO_SOCKET_FLUSH_TIMEOUT_MS", "300000")

        # Rank 0 starts listener immediately.
        # Workers verify Rank 0's port is open before connecting.
        # Fixed rank-second wait misses Rank 0's delayed listen start,
        # so we changed to a polling reachability check.
        if self.config.rank == 0:
            _log("INFO", "Rank 0: starting listener immediately...")
        else:
            self._wait_for_master_listener(master_addr, self.config.master_port)
            # Slight stagger by rank to mitigate thundering herd (max 5s)
            herd_delay = min(self.config.rank * 0.1, 5.0)
            time.sleep(herd_delay)

        _log(
            "INFO",
            f"Initializing process group: backend=gloo, init={init_method}, "
            f"world_size={self.config.world_size}, rank={self.config.rank}, timeout={timeout}",
        )
        _log("INFO", f"Waiting for {self.config.world_size} nodes to join...")
        start = time.monotonic()

        for attempt in range(1, max_retries + 1):
            try:
                # If previous attempt partially initialized, destroy before retry
                if dist.is_initialized():
                    _log("WARN", f"Process group already initialized, destroying before retry")
                    dist.destroy_process_group()
                dist.init_process_group(
                    backend="gloo",
                    init_method=init_method,
                    world_size=self.config.world_size,
                    rank=self.config.rank,
                    timeout=timeout,
                )
                elapsed = time.monotonic() - start
                _log("OK", f"Process group initialized on attempt {attempt} ({elapsed:.1f}s)")
                # Each rank resolves Rank 0 IP directly (no broadcast = avoid collective blocking)
                self._rank0_ip_bytes = list(self._resolve_master_ip().encode())
                _log("INFO", f"Rank {self.config.rank}: rank0_ip={self._resolve_master_ip()}")
                return
            except ValueError as e:
                # On "initialize twice" error, clean up and retry
                if "twice" in str(e) and attempt < max_retries:
                    _log("WARN", f"Attempt {attempt}/{max_retries} failed (double init): {e}")
                    try:
                        dist.destroy_process_group()
                    except Exception:
                        pass
                    _log("INFO", f"Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    _log("WARN", f"Attempt {attempt}/{max_retries} failed: {e}")
                    if attempt < max_retries:
                        _log("INFO", f"Retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                    else:
                        raise
            except Exception as e:
                err_str = str(e)
                _log("WARN", f"Attempt {attempt}/{max_retries} failed: {e}")
                # If already initialized with Connection closed / transport error,
                # all ranks have already joined, so continue without retry
                if dist.is_initialized() and any(kw in err_str for kw in ["Connection closed", "transport", "pair.cc", "pair closure", "Application timeout"]):
                    _log("INFO", "Transport error after init -- resolving IP directly and proceeding")
                    self._rank0_ip_bytes = list(self._resolve_master_ip().encode())
                    break
                if attempt < max_retries:
                    _log("INFO", f"Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    raise

    def _allocate_communication_buffers(self) -> None:
        """
        Pre-allocate zero-allocation communication buffers.

        No torch.zeros or tensor.clone during inference loop;
        communication uses only in-place receive/copy to pre-allocated buffers.
        """

        shape = (
            self.config.batch_size,
            self.config.seq_len,
            self.config.hidden_size,
        )

        _log("INFO", f"Allocating communication buffers: shape={shape}, dtype={COMPUTE_DTYPE}")
        start = time.monotonic()

        self.recv_buffers = [
            torch.zeros(shape, dtype=COMPUTE_DTYPE)
            for _ in range(self.config.num_micro_batches)
        ]
        self.send_buffers = [
            torch.zeros(shape, dtype=COMPUTE_DTYPE)
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
        Initialize KV-cache for each layer.

        Pre-allocate key_cache and value_cache for inference.
        Cache shape: (batch, num_kv_heads, max_seq_len, head_dim).
        """

        # B1: Use text_config cached in __init__
        text_config = self._text_config

        self.kv_cache: dict[int, tuple[torch.Tensor, torch.Tensor]] = {}
        max_gen_tokens = 2048  # Max generation tokens (large enough to avoid overflow of prompt+generation total)

        for layer_idx in range(text_config.num_hidden_layers):
            layer_type = text_config.layer_types[layer_idx]
            is_sliding = (layer_type == "sliding_attention")
            head_dim = text_config.head_dim if is_sliding else (getattr(text_config, "global_head_dim", None) or text_config.head_dim)

            if is_sliding:
                n_kv_heads = text_config.num_key_value_heads
            else:
                n_kv_heads = getattr(text_config, "num_global_key_value_heads", text_config.num_key_value_heads)

            # KV-cache: (batch=1, num_kv_heads, max_gen_tokens, head_dim)
            # Inference adds 1 token per step, so allocate max_gen_tokens
            key_cache = torch.zeros(
                (1, n_kv_heads, max_gen_tokens, head_dim),
                dtype=COMPUTE_DTYPE,
            )
            value_cache = torch.zeros(
                (1, n_kv_heads, max_gen_tokens, head_dim),
                dtype=COMPUTE_DTYPE,
            )
            self.kv_cache[layer_idx] = (key_cache, value_cache)

        # Per-layer write position counter (resettable between requests)
        # All ranks write to the same KV-cache position for the same layer,
        # so each layer has an independent write_pos.
        self._kv_cache_write_pos_ref: dict[int, int] = {layer_idx: 0 for layer_idx in self.kv_cache}

        _log(
            "OK",
            f"KV-cache initialized: {len(self.kv_cache)} layers, max_gen_tokens={max_gen_tokens}",
        )

    def _load_local_weights(self) -> list:
        """
        Load model weights from local disk.

        Supports safetensors or PyTorch format files.
        Uses mock layers as fallback when files are missing.
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
            # Fallback: try splits/ subdirectory if not found at top level
            if not os.path.exists(weight_file):
                weight_file = os.path.join(
                    self.config.model_path, "splits", f"layer_{layer_idx}.{ext}"
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

        # Rank 0: load embed_tokens
        if self.config.rank == 0:
            embed_file = os.path.join(self.config.model_path, f"embed_tokens.{ext}")
            if not os.path.exists(embed_file):
                embed_file = os.path.join(self.config.model_path, "splits", f"embed_tokens.{ext}")
            if os.path.exists(embed_file):
                weights = self._load_weight_file(embed_file)
                for k, v in weights.items():
                    if "embed_tokens" in k:
                        _log("INFO", f"Rank 0: loading embed_tokens ({v.shape})")
                        _load_embed_tokens(v, embed_scale=self.config.hidden_size ** 0.5)
            else:
                _log("WARN", "Rank 0: embed_tokens file not found")
        else:
            _log("INFO", f"Rank {self.config.rank}: no embed_tokens needed")

        # Final rank: load lm_head + final_norm
        is_last_rank = (self.config.next_rank is None)
        if is_last_rank:
            lm_head_file = os.path.join(self.config.model_path, f"lm_head.{ext}")
            if not os.path.exists(lm_head_file):
                lm_head_file = os.path.join(self.config.model_path, "splits", f"lm_head.{ext}")
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
        """Load weight file in safetensors or PyTorch format"""

        if path.endswith(".safetensors"):
            try:
                from safetensors.torch import load_file
                return load_file(path)
            except ImportError:
                _log("ERROR", "'pip install safetensors' is required.")
                sys.exit(1)
        else:
            return torch.load(
                path, map_location="cpu", mmap=True, weights_only=True
            )

    def _build_transformer_layer(self, weight_file: str, layer_idx: int, rank: int = 0, kv_cache: dict[int, tuple[torch.Tensor, torch.Tensor]] | None = None, kv_cache_write_pos_ref: list[int] | None = None):
        """Build a minimal decoder layer from safetensors weights.

        KV-cache support enables efficient sequential token processing during inference.
        Each step appends new KV to cache and attention uses full cache.
        """

        from safetensors.torch import load_file

        # B1: Use text_config cached in __init__ (eliminates per-layer duplicate loads)
        text_config = self._text_config

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

        # Gemma-4: attention scaling uses head_dim^-0.5 (not query_pre_attn_scalar)
        # Gemma-4 text model does NOT use attn_logit_softcapping (only final_logit_softcapping)
        final_logit_softcapping = getattr(text_config, "final_logit_softcapping", 0.0)

        # Load weights
        if weight_file:
            weights = load_file(weight_file)
            prefix = f"model.language_model.layers.{layer_idx}."
            state_dict = {}
            for k, v in weights.items():
                if k.startswith(prefix):
                    # S2: Convert bfloat16 weights in safetensors to float32.
                    # CPUs without AVX-512 BF16 support convert bfloat16 ops to float32 internally,
                    # causing overhead; convert to float32 upfront.
                    state_dict[k[len(prefix):]] = v.to(COMPUTE_DTYPE) if v.dtype != COMPUTE_DTYPE else v
        else:
            state_dict = {}

        _layer_idx = layer_idx
        _rank = rank
        _kv_cache = kv_cache
        # Shared write position counter (create local copy if None)
        _kv_cache_write_pos = kv_cache_write_pos_ref if kv_cache_write_pos_ref is not None else {}

        def forward(hidden_state: torch.Tensor, position_ids: torch.Tensor, is_first: bool = True) -> torch.Tensor:
            if _TRACE_ENABLED:
                import time as _time
                _t0 = _time.monotonic()
                _bh, _sl, _hs = hidden_state.shape
                _log("TRACE", f"R{_rank} L{_layer_idx} [{layer_type}] IN shape=({_bh},{_sl},{_hs}) dtype={hidden_state.dtype} mean={hidden_state.mean().item():.6f} std={hidden_state.std().item():.6f} min={hidden_state.min().item():.6f} max={hidden_state.max().item():.6f}")

            # --- Match input dtype to weight dtype (prevent relay communication dtype mismatch) ---
            _weight_dtype = state_dict["self_attn.q_proj.weight"].dtype
            if hidden_state.dtype != _weight_dtype:
                hidden_state = hidden_state.to(_weight_dtype)

            # Residual connection
            residual = hidden_state

            # --- Pre-attention RMSNorm ---
            hidden_state = _rms_norm(hidden_state, state_dict.get("input_layernorm.weight"), rms_norm_eps)
            if _TRACE_ENABLED:
                _log("TRACE", f"R{_rank} L{_layer_idx} input_layernorm dt={_time.monotonic()-_t0:.4f}s")

            # --- Self-attention: linear projections ---
            q = F.linear(hidden_state, state_dict["self_attn.q_proj.weight"])
            k = F.linear(hidden_state, state_dict["self_attn.k_proj.weight"])
            v = F.linear(hidden_state, state_dict["self_attn.v_proj.weight"]) if "self_attn.v_proj.weight" in state_dict else k

            # --- Reshape for attention ---
            n_heads = text_config.num_attention_heads
            q = q.view(q.size(0), q.size(1), n_heads, head_dim).transpose(1, 2)
            k = k.view(k.size(0), k.size(1), n_kv_heads, head_dim).transpose(1, 2)
            v = v.view(v.size(0), v.size(1), n_kv_heads, head_dim).transpose(1, 2)

            # --- RMSNorm AFTER reshape (q_norm/k_norm/v_norm) ---
            # Gemma-4: q_norm and k_norm have learned scales.
            # v_norm uses with_scale=False (weight-less RMSNorm), so apply with weight=None.
            q = _rms_norm(q, state_dict.get("self_attn.q_norm.weight"), rms_norm_eps)
            k = _rms_norm(k, state_dict.get("self_attn.k_norm.weight"), rms_norm_eps)
            v = _rms_norm(v, None, rms_norm_eps)

            # --- Apply RoPE ---
            partial_rotary_factor = rope_cfg.get('partial_rotary_factor', 1.0)
            q = _apply_rope(q, position_ids, rope_cfg, text_config, partial_rotary_factor)
            k = _apply_rope(k, position_ids, rope_cfg, text_config, partial_rotary_factor)

            # --- KV-cache: append new KV to cache (per-layer write position) ---
            # In pipeline parallelism, save KV during forward chain.
            # Each layer has an independent write_pos, so all ranks
            # write to the correct position.
            if _kv_cache is not None and layer_idx in _kv_cache:
                key_cache, value_cache = _kv_cache[layer_idx]
                _sl = q.shape[2]
                write_pos = _kv_cache_write_pos[layer_idx]
                key_cache[:, :, write_pos:write_pos+_sl, :] = k
                value_cache[:, :, write_pos:write_pos+_sl, :] = v
                _kv_cache_write_pos[layer_idx] += _sl
                cache_end = _kv_cache_write_pos[layer_idx]
                if is_sliding:
                    window_size = 1024
                    cache_start = max(0, cache_end - window_size)
                    k_full = key_cache[:, :, cache_start:cache_end, :]
                    v_full = value_cache[:, :, cache_start:cache_end, :]
                else:
                    cache_start = 0
                    k_full = key_cache[:, :, :cache_end, :]
                    v_full = value_cache[:, :, :cache_end, :]
                if _TRACE_ENABLED:
                    _log("TRACE", f"R{_rank} L{_layer_idx} kv_cache cache_end={cache_end} is_sliding={is_sliding} dt={_time.monotonic()-_t0:.4f}s")
            else:
                k_full = k
                v_full = v
                cache_start = 0
                cache_end = k_full.size(2)

            # --- KV groups (GQA) ---
            # B3: expand+reshape defers copy; if scaled_dot_product_attention can
            # consume broadcast directly, no copy is needed.
            if n_heads != n_kv_heads:
                _ratio = n_heads // n_kv_heads
                _bsz_g, _nkv_g, _seq_k, _hd_g = k_full.shape
                k_full = k_full.unsqueeze(2).expand(_bsz_g, _nkv_g, _ratio, _seq_k, _hd_g).reshape(_bsz_g, n_heads, _seq_k, _hd_g)
                v_full = v_full.unsqueeze(2).expand(_bsz_g, _nkv_g, _ratio, _seq_k, _hd_g).reshape(_bsz_g, n_heads, _seq_k, _hd_g)

            # --- Scaled dot-product attention ---
            # B5: F.scaled_dot_product_attention auto-selects optimized kernels like Flash Attention.
            # With KV-cache, q_len=1 < cache_len, so is_causal=True cannot be used;
            # either specify attn_mask explicitly (sliding) or use is_causal=False to attend all cache (full).
            # Gemma-4: Q/K normalized to unit norm by q_norm/k_norm, so use scale=1.0.
            q_len = q.size(2)
            cache_len = k_full.size(2)
            if is_sliding:
                window_size = 1024
                # Build causal+window mask using absolute position indices of cache and query.
                # torch.arange(q_len) with step>0 produces _row=[0], blocking all positions except 0,
                # so use absolute positions from position_ids instead.
                _col = torch.arange(cache_start, cache_end, dtype=torch.long, device=q.device)
                _row = position_ids[0].long().to(q.device)
                _causal_ok = _col.unsqueeze(0) <= _row.unsqueeze(1)
                _window_ok = _col.unsqueeze(0) >= _row.unsqueeze(1) - window_size + 1
                _attn_bias = torch.zeros(q_len, cache_len, dtype=q.dtype, device=q.device)
                _attn_bias = _attn_bias.masked_fill(~(_causal_ok & _window_ok), float('-inf'))
                attn_output = F.scaled_dot_product_attention(q, k_full, v_full, attn_mask=_attn_bias, scale=1.0)
            elif q_len == cache_len:
                # Prompt batch processing: q_len == cache_len, so is_causal=True is exact
                attn_output = F.scaled_dot_product_attention(q, k_full, v_full, is_causal=True, scale=1.0)
            else:
                # Autoregressive step: q_len=1 < cache_len, attend all cache
                attn_output = F.scaled_dot_product_attention(q, k_full, v_full, is_causal=False, scale=1.0)

            # --- Reshape back + output projection ---
            attn_output = attn_output.transpose(1, 2).contiguous()
            attn_output = attn_output.view(attn_output.size(0), attn_output.size(1), -1)
            attn_output = F.linear(attn_output, state_dict["self_attn.o_proj.weight"])

            # --- Post-attention RMSNorm + residual ---
            hidden_state = residual + _rms_norm(attn_output, state_dict.get("post_attention_layernorm.weight"), rms_norm_eps)

            # --- Pre-FFN RMSNorm + MLP (GELU) ---
            residual_ffn = hidden_state
            hidden_state = _rms_norm(hidden_state, state_dict.get("pre_feedforward_layernorm.weight"), rms_norm_eps)
            gate = F.linear(hidden_state, state_dict["mlp.gate_proj.weight"])
            up = F.linear(hidden_state, state_dict["mlp.up_proj.weight"])
            hidden_state = F.gelu(gate, approximate="tanh") * up
            hidden_state = F.linear(hidden_state, state_dict["mlp.down_proj.weight"])

            # --- Post-FFN RMSNorm + residual ---
            hidden_state = _rms_norm(hidden_state, state_dict.get("post_feedforward_layernorm.weight"), rms_norm_eps)
            hidden_state = residual_ffn + hidden_state

            # --- Layer scalar (Gemma-4 per_layer_input_scale) ---
            # Values around 0.03-0.09, legitimately small; multiply as-is.
            layer_scalar = state_dict.get("layer_scalar", None)
            if layer_scalar is not None:
                hidden_state = hidden_state * layer_scalar

            if _TRACE_ENABLED:
                _dt = _time.monotonic() - _t0
                _bh2, _sl2, _hs2 = hidden_state.shape
                _log("TRACE", f"R{_rank} L{_layer_idx} DONE total={_dt:.4f}s mean={hidden_state.mean().item():.6f} std={hidden_state.std().item():.6f} min={hidden_state.min().item():.6f} max={hidden_state.max().item():.6f}")
            return hidden_state

        return forward

 
    def _process_microbatch(self, mb: int, step_count: int, step_start_time: float, pbar: tqdm | None) -> None:
        """Process one microbatch per step (standard pipeline)

        Skip communication during relay mode execution to prevent buffer contention.
        """

        # Skip communication during relay execution (recv_buffers/send_buffers are exclusive)
        global _relay_active
        if _relay_active:
            return

        # [A] Receive data (with timeout)
        if self.config.prev_rank is None:
            self.recv_buffers[mb].normal_(mean=0.0, std=INPUT_STDDEV)
        else:
            try:
                dist.recv(tensor=self.recv_buffers[mb], src=self.config.prev_rank)
            except Exception:
                # Do nothing on timeout or error
                return

        # [B] Compute
        hidden_state = self.recv_buffers[mb]
        for layer in self.my_layers:
            hidden_state = layer(hidden_state)
        self.send_buffers[mb].copy_(hidden_state)

        # [C] Send (with timeout)
        if self.config.next_rank is None:
            # Last rank: update progress bar
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
        Main inference loop.

        Processes requests via relay communication after barrier.
        On HTTP request receipt:
          1. Send signal to all nodes (10s wait)
          2. Relay communication (Rank 0 sends -> other ranks relay -> final rank returns result)
        """

        # Process group initialization is already complete (all ranks synchronized in init_process_group).
        # HTTP handler controls request acceptance via _barrier_done flag.
        _log("INFO", "All nodes connected. Process group initialized.")
        global _barrier_done, _request_prompt, _request_result

        _barrier_done = True
        _log("OK", f"Inference loop started. micro_batches={self.config.num_micro_batches}, pipeline_stages={self.config.world_size}")

        # Start signal socket listener in a background thread
        def _signal_listener_thread():
            # Global declaration required because we assign to _request_prompt inside inner function.
            # Without this, the assignment at line 1032 becomes a local variable assignment,
            # module-global _request_prompt is not updated,
            # and the main polling loop never picks up the prompt, causing a deadlock.
            global _request_prompt
            _sig_sock = None
            try:
                _log("INFO", f"Rank {self.config.rank}: signal listener thread started")
                _sig_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                _sig_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                _sig_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                _sig_sock.bind(("0.0.0.0", _SIGNAL_PORT))
                _sig_sock.listen(16)
                _sig_sock.settimeout(0.5)
                _log("INFO", f"Rank {self.config.rank}: signal listener bound to port {_SIGNAL_PORT}")
                while not _shutdown_requested:
                    try:
                        conn, _ = _sig_sock.accept()
                        try:
                            conn.settimeout(5.0)
                            if self.config.rank == 0:
                                with _request_lock:
                                    prompt_text = _request_prompt
                                if prompt_text:
                                    conn.sendall((prompt_text + "\n").encode("utf-8"))
                                    try:
                                        conn.recv(1024)
                                    except Exception:
                                        pass
                                else:
                                    conn.sendall(b"ACK\n")
                            else:
                                data = conn.recv(65536)
                                if data:
                                    prompt_text = data.decode("utf-8").strip()
                                    if prompt_text:
                                        with _request_lock:
                                            _request_prompt = prompt_text
                                        _log("INFO", f"Rank {self.config.rank}: prompt received via signal socket ({len(prompt_text)} chars)")
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
            except Exception as e:
                _log("ERROR", f"Rank {self.config.rank}: signal listener thread crashed: {e}")
                import traceback
                _log("ERROR", f"Rank {self.config.rank}: {traceback.format_exc()}")
            finally:
                if _sig_sock is not None:
                    try:
                        _sig_sock.close()
                    except Exception:
                        pass

        _signal_thread = threading.Thread(target=_signal_listener_thread, daemon=True)
        _signal_thread.start()
        _log("INFO", f"Rank {self.config.rank}: signal listener thread started (id={_signal_thread.ident})")

        # Main thread: prompt check + relay processing
        _log("INFO", f"Rank {self.config.rank}: main thread starting prompt polling loop")
        while not _shutdown_requested:
            # Use lock only for prompt extraction; release during relay processing.
            # signal listener thread acquires the same lock, so running relay outside the lock
            # prevents deadlocks (holding the lock for 150s+ would block the signal listener).
            with _request_lock:
                prompt = _request_prompt
                if prompt is not None:
                    _request_prompt = None
            if prompt is not None:
                _log("INFO", f"Rank {self.config.rank}: main thread found prompt, starting relay")
                result = self._broadcast_prompt_and_wait(prompt)
                _request_result = result if result is not None else "ERROR: relay returned None"
                _result_available.set()  # A4: Immediately unblock HTTP handler's blocking wait
                _log("INFO", f"Rank {self.config.rank}: relay completed, result set")
                prompt = None
            time.sleep(0.05)

    def _pipeline_loop(self) -> None:
        """Pipeline inference loop running in a background thread.

        Rank 0 (prev_rank=None) generates input data and sends to Rank 1.
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
        """Destroy the Gloo backend distributed process group and release resources."""

        if dist.is_initialized():
            dist.destroy_process_group()
            _log("INFO", "Process group destroyed.")

    def _run_relay_background(self, prompt: str) -> None:
        """Run relay in a background thread and set the result to _request_result."""

        global _request_result
        try:
            result = self._broadcast_prompt_and_wait(prompt)
            _request_result = result if result is not None else "ERROR: relay returned None"
        except Exception:
            err_msg = f"ERROR: {traceback.format_exc()}"
            _request_result = err_msg
            _log("ERROR", f"Rank {self.config.rank}: background relay failed")

    def _broadcast_prompt_and_wait(self, prompt: str) -> str | None:
        """
        Broadcast prompt to all nodes and execute relay inference.

        Signal socket is a best-effort mechanism for prompt acquisition. Relay proceeds even on connection failure.
        All nodes transition to relay mode, synchronize via barrier, then execute inference.
        Returns the inference result (None on failure).
        """
        rank = dist.get_rank()
        world_size = dist.get_world_size()

        # Get Rank 0 IP (broadcast during init)
        rank0_ip = bytes(self._rank0_ip_bytes).decode().rstrip("\x00")

        # Node IP mapping (adjust for your environment)
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

        # Signal socket for prompt acquisition (best-effort, relay proceeds even on failure)
        if rank != 0:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(10.0)
                s.connect((rank0_ip, _SIGNAL_CONNECT_PORT))
                # Receive prompt from Rank 0
                data = b""
                while not data.endswith(b"\n"):
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                s.close()
                if data and data.strip():
                    _log("INFO", f"Rank {rank}: received prompt from Rank 0 ({len(data)} bytes)")
            except Exception:
                _log("WARN", f"Rank {rank}: signal socket connection failed (will use provided prompt)")
        else:
            # A2: Parallelize signal sending to all nodes via ThreadPoolExecutor.
            # Serial: N nodes x max 30s = long latency.
            # Parallel: latency reduced to that of the slowest single node.
            import concurrent.futures as _futures
            _log("INFO", f"Rank 0: connecting to {world_size - 1} nodes in parallel")

            def _signal_one_rank(r: int) -> int | None:
                """Send signal to one node. Returns r on success, None on failure."""
                target_ip = get_node_ip(r)
                for retry in range(3):
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        s.settimeout(10.0)
                        s.connect((target_ip, _SIGNAL_PORT))
                        s.sendall((prompt + "\n").encode("utf-8"))
                        ack_data = s.recv(1024)
                        if ack_data:
                            _log("INFO", f"Rank 0: signalled rank {r} ({target_ip})")
                            return r
                    except Exception:
                        _log("WARN", f"Rank 0: retry {retry + 1}/3 for rank {r} ({target_ip})")
                    finally:
                        try:
                            s.close()
                        except Exception:
                            pass
                    if retry < 2:
                        time.sleep(2)
                return None

            with _futures.ThreadPoolExecutor(max_workers=world_size - 1) as _pool:
                _results = list(_pool.map(_signal_one_rank, range(1, world_size)))

            succeeded = [r for r in _results if r is not None]
            failed_ranks = [r for r in range(1, world_size) if r not in succeeded]
            if failed_ranks:
                _log("WARN", f"Rank 0: failed ranks: {failed_ranks} ({len(failed_ranks)}/{world_size - 1})")
            _log("INFO", f"Rank 0: all connections complete ({len(succeeded)}/{world_size - 1} ACKs)")

        # A1: Removed sleep(2). dist.barrier() at the start of _relay_request guarantees
        # synchronization across all ranks, so Rank 0 blocks at the barrier until all other ranks arrive.

        # All nodes: transition to relay mode (regardless of signal socket success/failure)
        _log("INFO", f"Rank {rank}: >>> _broadcast_prompt_and_wait calling _relay_request")
        result = None
        try:
            result = self._relay_request(prompt)
            _log("RESULT", f"Rank {rank}: relay result: '{result[:100]}'")
        except Exception as e:
            import traceback as _tb
            _log("ERROR", f"Rank {rank}: relay_request failed: {e}\n{_tb.format_exc()}")
            _log("WARN", f"Rank {rank}: skipping relay (broadcast failed)")
        return result

    def _relay_request(self, prompt: str) -> str:
        """
        Process autoregressive inference in relay mode.

        Per step:
          Forward chain: Rank 0 -> Rank 1 -> ... -> Rank N
            Rank 0: send embed to Rank 1 (dist.send)
            Other ranks: recv from prev -> compute -> send to next (dist.send)
            Rank N: compute -> final_norm + lm_head -> send token_id to Rank 0 (dist.send)

          Synchronization: ACK chain (TCP socket) ensures step ordering.
          Asynchronous recv (irecv) parallelizes sequence length and hidden state reception.
        """
        import time as _time
        import os as _os

        _log("INFO", f"Rank {self.config.rank}: >>> _relay_request ENTER pid={_os.getpid()}")
        global _relay_active
        _log("INFO", f"Rank {self.config.rank}: >>> acquiring _relay_lock")
        with _relay_lock:
            _relay_active = True
            _log("INFO", f"Rank {self.config.rank}: >>> _relay_lock acquired")

        # Synchronize all nodes: retry barrier up to 10 times (fault tolerance for unresponsive nodes)
        barrier_success = False
        _log("INFO", f"Rank {self.config.rank}: >>> calling dist.barrier() world_size={dist.get_world_size()}")
        import torch.distributed as _dist
        _log("INFO", f"Rank {self.config.rank}: >>> dist.is_initialized()={_dist.is_initialized()}, rank={_dist.get_rank()}")
        for _b_retry in range(10):
            try:
                _log("INFO", f"Rank {self.config.rank}: >>> dist.barrier() call #{_b_retry + 1}")
                _dist.barrier()
                barrier_success = True
                _log("INFO", f"Rank {self.config.rank}: barrier acquired on try {_b_retry + 1}")
                break
            except Exception as e:
                _log("WARN", f"Rank {self.config.rank}: barrier try {_b_retry + 1} failed: {e}")
                time.sleep(10)
        if not barrier_success:
            _log(
                "WARN",
                f"Rank {self.config.rank}: barrier failed after 10 retries. "
                "Proceeding with relay (some ranks may not participate).",
            )

        # Generation parameters
        # Stop on EOS token detection or max token count reached.
        # step0 ~57s (prompt processing) + each step ~7s/token.
        max_new_tokens = 100  # Safe upper limit
        last_rank = self.config.world_size - 1

        # EOS stop signal: send -1.0 as seq_len to propagate stop signal through the pipeline.
        EOS_STOP_SIGNAL = -1.0

        def _send_eos_to_rank1(d: "dist", signal: float, dummy_buf: torch.Tensor) -> None:
            """Send EOS stop signal from Rank 0 to Rank 1.

            When step > 0, the receiver waits for a pair of irecv(seq_len) + irecv(hidden),
            so seq_len + dummy hidden must always be sent as a pair.
            """
            d.send(torch.tensor([signal], dtype=torch.float32), dst=1)
            d.send(tensor=dummy_buf, dst=1)

        # Gemma 4: final logit softcapping (default value 30.0)
        final_logit_softcapping = 30.0

        # ACK listener socket (for step synchronization)
        _ack_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _ack_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _ack_listener.bind(("0.0.0.0", _RELAY_ACK_PORT))
        _ack_listener.listen(4)
        _ack_listener.settimeout(30.0)  # Set longer for persistent connection establishment
        _log("INFO", f"Rank {self.config.rank}: ACK listener started on port {_RELAY_ACK_PORT}")

        # A3: Establish persistent ACK socket connections before the loop to eliminate per-step TCP handshakes.
        # Connection direction: next rank (ACK sender) connects to prev rank (ACK receiver)'s _RELAY_ACK_PORT.
        # accept runs in a thread to run in parallel with connect and prevent deadlocks.
        rank0_ip_str = bytes(self._rank0_ip_bytes).decode().rstrip("\x00")

        def _get_rank_ip_relay(r: int) -> str:
            """Resolve IP address from rank number."""
            if r == 0:
                return rank0_ip_str
            elif r <= 40:
                return f"192.168.11.{100 + (r - 1)}"
            else:
                return f"192.168.12.{100 + (r - 41)}"

        _prev_ip_relay = _get_rank_ip_relay(self.config.prev_rank) if self.config.prev_rank is not None else None
        _ack_to_prev_conn: "socket.socket | None" = None
        _ack_to_rank0_conn: "socket.socket | None" = None  # Only used by the last rank
        _ack_from_next_conn: "socket.socket | None" = None

        # Start accept in a thread (Rank 0 or intermediate ranks receive connections from next/final rank)
        _accept_holder: list = [None]
        _accept_done = threading.Event()
        _needs_accept = (self.config.next_rank is not None) or (self.config.rank == 0)

        def _do_accept_relay() -> None:
            """Accept persistent ACK connection from next rank."""
            try:
                conn, addr = _ack_listener.accept()
                conn.settimeout(300.0)
                _accept_holder[0] = conn
                _log("INFO", f"Rank {self.config.rank}: ACK persistent conn accepted from {addr[0]}")
            except Exception as e:
                _log("WARN", f"Rank {self.config.rank}: ACK accept failed: {e}")
            finally:
                _accept_done.set()

        if _needs_accept:
            _accept_thread = threading.Thread(target=_do_accept_relay, daemon=True)
            _accept_thread.start()
        else:
            _accept_done.set()

        # P2 (A2a): If all ranks connect simultaneously, connection attempts fail before the target's listener starts
        # (confirmed in logs: "ACK connect to prev failed").
        # Adopt the same herd_delay approach as process group initialization, delaying
        # connection start by rank (max 5 seconds). This ensures the target's listener
        # is definitely in accept state before connect is executed.
        _herd_delay_ack = min(self.config.rank * 0.1, 5.0)
        if _herd_delay_ack > 0:
            _log("INFO", f"Rank {self.config.rank}: ACK connect herd delay {_herd_delay_ack:.1f}s")
            time.sleep(_herd_delay_ack)

        # Connect to prev rank (executed in parallel with accept)
        if _prev_ip_relay is not None:
            _c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _c.settimeout(30.0)
            try:
                _c.connect((_prev_ip_relay, _RELAY_ACK_PORT))
                _c.settimeout(300.0)
                _ack_to_prev_conn = _c
                _log("INFO", f"Rank {self.config.rank}: ACK persistent conn to prev ({_prev_ip_relay}) established")
            except Exception as e:
                _log("WARN", f"Rank {self.config.rank}: ACK connect to prev failed: {e}")
                try: _c.close()
                except Exception: pass

        # Last rank also establishes a persistent connection to Rank 0
        if self.config.next_rank is None and self.config.rank != 0:
            _c0 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _c0.settimeout(30.0)
            try:
                _c0.connect((rank0_ip_str, _RELAY_ACK_PORT))
                _c0.settimeout(300.0)
                _ack_to_rank0_conn = _c0
                _log("INFO", f"Rank {self.config.rank}: ACK persistent conn to Rank 0 ({rank0_ip_str}) established")
            except Exception as e:
                _log("WARN", f"Rank {self.config.rank}: ACK connect to Rank 0 failed: {e}")
                try: _c0.close()
                except Exception: pass

        _accept_done.wait(timeout=30.0)
        _ack_from_next_conn = _accept_holder[0]
        _ack_listener.settimeout(5.0)  # Shorter timeout during the loop

        try:
            # --- All ranks common: Reset KV-cache and write position ---
            if self.kv_cache:
                for lid, (kc, vc) in self.kv_cache.items():
                    kc.zero_()
                    vc.zero_()
                _log("INFO", f"Rank {self.config.rank}: KV-cache reset ({len(self.kv_cache)} layers)")
            if hasattr(self, '_kv_cache_write_pos_ref'):
                for lid in self._kv_cache_write_pos_ref:
                    self._kv_cache_write_pos_ref[lid] = 0

            if self.config.rank == 0:
                # ===== Prompt processing + generation loop =====
                _log("INFO", f"Rank 0: prompt='{prompt}'")
                _log(
                    "INFO",
                    "Rank 0: levers "
                    f"NUM_MICRO_BATCHES={self.config.num_micro_batches} "
                    f"STAGGER_INTERVAL={self.config.stagger_interval} "
                    f"SEQ_LEN={self.config.seq_len} "
                    f"WORLD_SIZE={self.config.world_size}",
                )
                input_ids = _tokenize(prompt)  # (1, seq_len)
                seq_len = input_ids.size(1)
                embed = F.embedding(input_ids, _embed_tokens)  # (1, seq_len, hidden_size)
                _log("INFO", f"Rank 0: prompt tokens={seq_len}, embedding shape={embed.shape} mean={embed.mean().item():.6f} std={embed.std().item():.6f} min={embed.min().item():.6f} max={embed.max().item():.6f}")

                generated_ids = []
                consecutive_same = 0
                last_token = None

                for step in range(max_new_tokens):
                    is_first = (step == 0)
                    _log("INFO", f"Rank 0: === step {step}/{max_new_tokens} is_first={is_first} ===")
                    _t_step = _time.monotonic()

                    if is_first:
                        seq_len_buf = torch.tensor([float(seq_len)], dtype=torch.float32)
                        dist.send(seq_len_buf, dst=1)
                        # P4: Also clamp embed before COMPUTE_DTYPE conversion (defensive)
                        dist.send(torch.clamp(embed, -10.0, 10.0).to(COMPUTE_DTYPE), dst=1)
                        _log("INFO", f"Rank 0: sent prompt embed (seq_len={seq_len})")
                    else:
                        new_token_tensor = torch.tensor([[generated_ids[-1]]], dtype=torch.long)
                        # P4: Also convert token embed to COMPUTE_DTYPE after clamping
                        new_embed = torch.clamp(F.embedding(new_token_tensor, _embed_tokens), -10.0, 10.0).to(COMPUTE_DTYPE)
                        dist.send(torch.tensor([1.0], dtype=torch.float32), dst=1)
                        dist.send(new_embed, dst=1)
                        _log("INFO", f"Rank 0: step {step} sent token_embed token={generated_ids[-1]}")

                    # A3: Receive ACK from last rank via persistent connection (eliminates per-step accept)
                    if _ack_from_next_conn is not None:
                        try:
                            _ack_from_next_conn.recv(1024)
                            _log("INFO", f"Rank 0: step {step} ACK received from Rank {last_rank}")
                        except Exception:
                            _log("WARN", f"Rank 0: ACK recv failed for step {step}")
                    else:
                        _log("WARN", f"Rank 0: no ACK conn available for step {step}, skipping")

                    # Receive token_id from Rank 50
                    token_id_buf = torch.zeros(1, dtype=torch.float32)
                    dist.recv(tensor=token_id_buf, src=last_rank)
                    token_id = int(token_id_buf.item())
                    generated_ids.append(token_id)

                    # EOS token detection (highest priority)
                    if token_id in (1, 2):
                        _log("INFO", f"Rank 0: EOS token={token_id} at step {step}, sending stop signal")
                        _send_eos_to_rank1(dist, EOS_STOP_SIGNAL, self.send_buffers[0])
                        _log("INFO", "Rank 0: EOS stop signal sent to Rank 1")
                        break

                    # Token loop detection: stop if same token repeats 5 times
                    if token_id == last_token:
                        consecutive_same += 1
                    else:
                        consecutive_same = 0
                        last_token = token_id
                    if consecutive_same >= 5:
                        _log("WARN", f"Rank 0: token loop detected (token={token_id} x{consecutive_same}), sending stop signal")
                        _send_eos_to_rank1(dist, EOS_STOP_SIGNAL, self.send_buffers[0])
                        break

                    # Pattern loop detection: check if the last 6-token pattern has appeared before
                    _pattern_loop = False
                    if len(generated_ids) >= 12:
                        _recent = tuple(generated_ids[-6:])
                        for _start in range(0, len(generated_ids) - 12):
                            if tuple(generated_ids[_start:_start + 6]) == _recent:
                                # Keep up to the second occurrence position (first cycle)
                                _keep = max(_start + 6, len(generated_ids) - 6)
                                if _start == 0:
                                    _keep = len(generated_ids) - 6  # Exclude last cycle if looping from the start
                                _log("WARN", f"Rank 0: pattern loop detected (6-token pattern at pos {_start}/{len(generated_ids)}), keeping {_keep} tokens")
                                _send_eos_to_rank1(dist, EOS_STOP_SIGNAL, self.send_buffers[0])
                                generated_ids = generated_ids[:_keep]
                                _pattern_loop = True
                                break

                    if _pattern_loop:
                        break

                    step_dt = _time.monotonic() - _t_step
                    _log("INFO", f"Rank 0: step {step} done token={token_id} dt={step_dt:.3f}s")

                # After applying chat template, decode only generated_ids.
                # (input_ids contains chat template special tokens, so exclude them)
                _log("INFO", f"Rank 0: decoding {len(generated_ids)} generated tokens (prompt={seq_len})...")
                _t_decode = _time.monotonic()
                result = _tokenizer.decode(generated_ids, skip_special_tokens=True)
                _log("INFO", f"Rank 0: decoded in {_time.monotonic()-_t_decode:.3f}s: '{result}'")
                return result

            elif self.config.rank == last_rank:
                # ===== Last rank: receive forward chain -> compute -> send token_id =====
                seq_len = 0

                for step in range(max_new_tokens):
                    is_first = (step == 0)
                    _log("INFO", f"Rank {self.config.rank}: === step {step}/{max_new_tokens} is_first={is_first} ===")
                    _t_step = _time.monotonic()

                    if is_first:
                        seq_len_buf = torch.zeros(1, dtype=torch.float32)
                        dist.recv(tensor=seq_len_buf, src=self.config.prev_rank)
                        recv_seq_len = int(seq_len_buf.item())

                        # EOS stop signal detection
                        if recv_seq_len < 0:
                            _log("INFO", f"Rank {self.config.rank}: received EOS stop signal, stopping")
                            # Receive dummy hidden state and clean up
                            dist.recv(tensor=self.recv_buffers[0], src=self.config.prev_rank)
                            break

                        if recv_seq_len > 1:
                            hidden_state = torch.zeros(
                                (self.config.batch_size, recv_seq_len, self.config.hidden_size),
                                dtype=COMPUTE_DTYPE,
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

                        # EOS stop signal detection
                        if recv_seq_len < 0:
                            _log("INFO", f"Rank {self.config.rank}: received EOS stop signal, stopping")
                            break

                    _t = _time.monotonic()
                    if is_first:
                        positions = torch.arange(recv_seq_len, dtype=torch.long).unsqueeze(0)
                    else:
                        pos_id = seq_len + step - 1
                        positions = torch.tensor([[pos_id]], dtype=torch.long)
                    _log("INFO", f"Rank {self.config.rank}: step {step} computing pos_id={positions.max().item()}")

                    for layer in self.my_layers:
                        hidden_state = layer(hidden_state, position_ids=positions, is_first=is_first)
                    _log("INFO", f"Rank {self.config.rank}: step {step} compute dt={_time.monotonic()-_t:.3f}s hidden_mean={hidden_state.mean().item():.6f} hidden_std={hidden_state.std().item():.6f} hidden_min={hidden_state.min().item():.6f} hidden_max={hidden_state.max().item():.6f}")

                    # final_norm + lm_head
                    last_hidden = hidden_state[:, -1:, :]
                    final_hidden = _final_norm(last_hidden)
                    if final_hidden.dtype != torch.float32:
                        final_hidden = final_hidden.to(torch.float32)
                    _log("INFO", f"Rank {self.config.rank}: step {step} final_hidden mean={final_hidden.mean().item():.6f} std={final_hidden.std().item():.6f} min={final_hidden.min().item():.6f} max={final_hidden.max().item():.6f}")
                    # B4: _lm_head is already converted to float32 at startup
                    logits = F.linear(final_hidden, _lm_head)
                    logits_flat = logits.squeeze()  # (vocab_size,)
                    _log("INFO", f"Rank {self.config.rank}: step {step} logits_raw mean={logits_flat.mean().item():.4f} std={logits_flat.std().item():.4f} min={logits_flat.min().item():.4f} max={logits_flat.max().item():.4f}")
                    if not torch.isfinite(logits).all():
                        _log("WARN", f"Rank {self.config.rank}: NaN/Inf in logits, clamping")
                        logits = logits.nan_to_num(nan=0.0, posinf=10.0, neginf=-10.0)
                    if final_logit_softcapping > 0:
                        logits = torch.tanh(logits / final_logit_softcapping) * final_logit_softcapping
                    logits_flat2 = logits.squeeze()
                    _top5_vals, _top5_idx = torch.topk(logits_flat2, 5)
                    _log("INFO", f"Rank {self.config.rank}: step {step} top5_after_cap: {[(int(_top5_idx[i].item()), float(_top5_vals[i].item())) for i in range(5)]}")
                    token_id = torch.argmax(logits, dim=-1).to(torch.int64)
                    _log("INFO", f"Rank {self.config.rank}: step {step} final token_id={token_id.item()}")

                    dist.send(token_id.float(), dst=0)
                    _log("INFO", f"Rank {self.config.rank}: step {step} sent token_id to Rank 0")

                    # EOS detection: send stop signal on next step
                    if token_id.item() in (1, 2):
                        _log("INFO", f"Rank {self.config.rank}: EOS token={token_id.item()} detected, will stop after ACK")

                    # A3: Send ACK via persistent connection.
                    # Last rank sends ACK to both prev (Rank N-1) and Rank 0.
                    # ACK to prev unblocks intermediate ranks waiting for "ACK from next".
                    if _ack_to_prev_conn is not None:
                        try:
                            _ack_to_prev_conn.sendall(b"ACK\n")
                            _log("INFO", f"Rank {self.config.rank}: step {step} ACK sent to prev")
                        except Exception:
                            _log("WARN", f"Rank {self.config.rank}: ACK to prev failed for step {step}")
                    if _ack_to_rank0_conn is not None:
                        try:
                            _ack_to_rank0_conn.sendall(b"ACK\n")
                            _log("INFO", f"Rank {self.config.rank}: step {step} ACK sent to Rank 0")
                        except Exception:
                            _log("WARN", f"Rank {self.config.rank}: ACK to Rank 0 failed for step {step}")
                return ""

            else:
                # ===== Intermediate rank: forward chain only =====
                seq_len = 0

                for step in range(max_new_tokens):
                    is_first = (step == 0)
                    _log("INFO", f"Rank {self.config.rank}: === step {step}/{max_new_tokens} is_first={is_first} ===")
                    _t_step = _time.monotonic()

                    if is_first:
                        seq_len_buf = torch.zeros(1, dtype=torch.float32)
                        dist.recv(tensor=seq_len_buf, src=self.config.prev_rank)
                        recv_seq_len = int(seq_len_buf.item())

                        # EOS stop signal detection
                        if recv_seq_len < 0:
                            _log("INFO", f"Rank {self.config.rank}: received EOS stop signal, forwarding and stopping")
                            dist.send(torch.tensor([EOS_STOP_SIGNAL], dtype=torch.float32), dst=self.config.next_rank)
                            dist.recv(tensor=self.recv_buffers[0], src=self.config.prev_rank)
                            dist.send(tensor=self.send_buffers[0], dst=self.config.next_rank)
                            break

                        if recv_seq_len > 1:
                            hidden_state = torch.zeros(
                                (self.config.batch_size, recv_seq_len, self.config.hidden_size),
                                dtype=COMPUTE_DTYPE,
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

                        # EOS stop signal detection
                        if recv_seq_len < 0:
                            _log("INFO", f"Rank {self.config.rank}: received EOS stop signal, forwarding and stopping")
                            dist.send(torch.tensor([EOS_STOP_SIGNAL], dtype=torch.float32), dst=self.config.next_rank)
                            dist.send(tensor=self.send_buffers[0], dst=self.config.next_rank)
                            break

                    _t = _time.monotonic()
                    if is_first:
                        positions = torch.arange(recv_seq_len, dtype=torch.long).unsqueeze(0)
                    else:
                        pos_id = seq_len + step - 1
                        positions = torch.tensor([[pos_id]], dtype=torch.long)
                    _log("INFO", f"Rank {self.config.rank}: step {step} computing pos_id={positions.max().item()}")

                    for layer in self.my_layers:
                        hidden_state = layer(hidden_state, position_ids=positions, is_first=is_first)
                    _log("INFO", f"Rank {self.config.rank}: step {step} compute dt={_time.monotonic()-_t:.3f}s hidden_mean={hidden_state.mean().item():.6f} hidden_std={hidden_state.std().item():.6f} hidden_min={hidden_state.min().item():.6f} hidden_max={hidden_state.max().item():.6f}")

                    dist.send(torch.tensor([float(recv_seq_len)], dtype=torch.float32), dst=self.config.next_rank)
                    # P4: Clamp before COMPUTE_DTYPE conversion. If hidden state has exploded,
                    # large values cause precision loss and numerical instability in subsequent ranks.
                    # Also clamp defensively before sending to detect cases where
                    # the forward function's clamping is not working.
                    if hidden_state.dtype != COMPUTE_DTYPE:
                        hidden_state = torch.clamp(hidden_state, -10.0, 10.0).to(COMPUTE_DTYPE)
                    dist.send(tensor=hidden_state, dst=self.config.next_rank)
                    _log("INFO", f"Rank {self.config.rank}: step {step} sent to next dt={_time.monotonic()-_t:.3f}s")

                    # A3: Send ACK to prev via persistent connection, receive ACK from next
                    if _ack_to_prev_conn is not None:
                        try:
                            _ack_to_prev_conn.sendall(b"ACK\n")
                            _log("INFO", f"Rank {self.config.rank}: step {step} ACK sent to prev")
                        except Exception:
                            _log("WARN", f"Rank {self.config.rank}: ACK to prev failed for step {step}")
                    if _ack_from_next_conn is not None:
                        try:
                            _ack_from_next_conn.recv(1024)
                            _log("INFO", f"Rank {self.config.rank}: step {step} ACK received from next")
                        except Exception:
                            _log("WARN", f"Rank {self.config.rank}: ACK from next failed for step {step}")
                return ""
        finally:
            # A3: Close persistent ACK connections
            for _conn in [_ack_to_prev_conn, _ack_to_rank0_conn, _ack_from_next_conn]:
                if _conn is not None:
                    try: _conn.close()
                    except Exception: pass
            try: _ack_listener.close()
            except Exception: pass
            with _relay_lock:
                _relay_active = False


# ====================================================================
# HTTP server (Rank 0 only)
# ====================================================================

class _PredictHandler(BaseHTTPRequestHandler):
    """Handle POST /predict: accept prompt and return pipeline inference result."""

    def log_message(self, format, *args):
        pass  # Suppress standard stderr logging

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
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _handle_predict(self, prompt: str) -> None:

        # A4: Clear previous request result before setting prompt.
        # _result_available.clear() resets the event so wait() responds immediately.
        global _request_prompt, _http_initiated, _request_result
        with _request_lock:
            _request_result = None
            _result_available.clear()
            _request_prompt = prompt
        with _http_lock:
            _http_initiated = True

        try:
            if not dist.is_initialized():
                self._respond(503, '{"error": "process group not ready"}')
                return

            # Reject requests before barrier completion
            global _barrier_done
            if not _barrier_done:
                self._respond(503, '{"error": "barrier not completed"}')
                return

            # Only Rank 0 supports relay mode
            if dist.get_rank() != 0:
                self._respond(500, '{"error": "only rank 0 handles requests"}')
                return

            _log("INFO", f"Request received: prompt='{prompt[:60]}...'")

            # main loop calls _result_available.set() when relay completes.
            # Instead of 0.5s polling, respond immediately via Event.wait() when result arrives.
            # P1: Safety margin of max 100 tokens x 10s/token + 200s init = 1200s.
            _http_timeout = 1200.0
            _log("DEBUG", f"HTTP handler waiting on _result_available (timeout={_http_timeout:.0f}s)")
            if _result_available.wait(timeout=_http_timeout):
                _log("DEBUG", "_result_available set, reading result")
                result = _request_result
                if result is not None:
                    if result.startswith("ERROR:"):
                        _log("ERROR", f"Request failed: {result}")
                        self._respond(500, '{"error": "relay failed"}')
                    else:
                        _log("RESULT", f"Request response: '{result[:100]}'")
                        self._respond(200, json.dumps({"result": result}, ensure_ascii=False))
                    return
            self._respond(504, '{"error": "relay timeout"}')
        finally:
            with _http_lock:
                _http_initiated = False


def _load_embed_tokens(weight: torch.Tensor, embed_scale: float = 1.0) -> None:
    """Set embedding weights to global variable.

    For Gemma-4, apply hidden_size^0.5 scaling to embed_tokens
    (same as Gemma4TextScaledWordEmbedding.embed_scale).
    """
    global _embed_tokens
    if embed_scale != 1.0:
        weight = weight * embed_scale
    # S2: Also convert embedding weights to COMPUTE_DTYPE
    _embed_tokens = weight.to(COMPUTE_DTYPE) if weight.dtype != COMPUTE_DTYPE else weight


def _load_lm_head(weight: torch.Tensor) -> None:
    """Set LM head weights to global variable.
    B4: Converting to float32 at startup eliminates per-step conversion cost in the inference loop.
    """
    global _lm_head
    _lm_head = weight.to(torch.float32)  # shape: (vocab_size, hidden_size)


def _rms_norm(hidden_state: torch.Tensor, weight: torch.Tensor | None, eps: float) -> torch.Tensor:
    """Apply RMSNorm."""
    variance = (hidden_state ** 2).mean(-1, keepdim=True)
    hidden_state = hidden_state * torch.rsqrt(variance + eps)
    if weight is not None:
        hidden_state = hidden_state * weight
    return hidden_state


# B2: inv_freq depends on (rope_type, head_dim, rope_theta, partial_rotary_factor),
# so cache by these keys to eliminate per-forward recalculation.
_rope_inv_freq_cache: dict[tuple, torch.Tensor] = {}


def _apply_rope(
    x: torch.Tensor, position_ids: torch.Tensor, rope_cfg: dict, text_config,
    partial_rotary_factor: float = 1.0,
) -> torch.Tensor:
    """Apply Rotary Position Embedding.

    Accurately reproduces Gemma-4's two types of RoPE:

    - sliding_attention (rope_type="default"): Standard RoPE, rotates all head_dim dimensions.
      inv_freq = 1 / (theta ^ (arange(0, head_dim, 2) / head_dim)), all head_dim//2 pairs.

    - full_attention (rope_type="proportional", partial_rotary_factor=0.25):
      Only the first rope_angles = int(0.25 * head_dim // 2) pairs are non-zero, rest are NOPE (zero).
      Denominator of inv_freq is head_dim (not rotary_dim).

    Both use HuggingFace's rotate_half style (non-interleaved, neox format):
      rotate_half(x) = cat(-x[..., d//2:], x[..., :d//2])
      output = x * cos + rotate_half(x) * sin
    """
    head_dim = x.size(-1)
    rope_theta = rope_cfg["rope_theta"]
    rope_type = rope_cfg.get("rope_type", "default")

    _inv_cache_key = (rope_type, head_dim, float(rope_theta), partial_rotary_factor)
    if _inv_cache_key not in _rope_inv_freq_cache:
        if rope_type == "proportional":
            # rope_angles: number of non-zero freq pairs
            rope_angles = int(partial_rotary_factor * head_dim // 2)
            # Denominator is head_dim (all dimensions), arange is [0, 2, 4, ..., 2*(rope_angles-1)]
            inv_freq_rotated = 1.0 / (
                rope_theta ** (
                    torch.arange(0, 2 * rope_angles, 2, dtype=torch.float32) / head_dim
                )
            )
            # Zero padding for NOPE dimensions: inv_freq is head_dim//2 size
            nope_count = head_dim // 2 - rope_angles
            if nope_count > 0:
                inv_freq = torch.cat(
                    [inv_freq_rotated, torch.zeros(nope_count, dtype=torch.float32)], dim=0
                )
            else:
                inv_freq = inv_freq_rotated
        else:
            # default: Standard RoPE, all head_dim//2 pairs
            inv_freq = 1.0 / (
                rope_theta ** (
                    torch.arange(0, head_dim, 2, dtype=torch.float32) / head_dim
                )
            )
        _rope_inv_freq_cache[_inv_cache_key] = inv_freq
    inv_freq = _rope_inv_freq_cache[_inv_cache_key].to(device=x.device)

    # freqs: (seq_len, head_dim//2)
    positions = position_ids.reshape(-1).float()
    freqs = torch.outer(positions, inv_freq)

    # emb: (seq_len, head_dim) = cat([freqs, freqs])
    emb = torch.cat([freqs, freqs], dim=-1)
    cos = emb.cos().to(dtype=x.dtype)  # (seq_len, head_dim)
    sin = emb.sin().to(dtype=x.dtype)

    # Broadcast: x is (batch, heads, seq_len, head_dim)
    cos = cos.unsqueeze(0).unsqueeze(0)  # (1, 1, seq_len, head_dim)
    sin = sin.unsqueeze(0).unsqueeze(0)

    # rotate_half (neox style): swap first and second halves for rotation
    x1 = x[..., :head_dim // 2]   # first half
    x2 = x[..., head_dim // 2:]   # second half
    rotated = torch.cat([-x2, x1], dim=-1)  # rotate_half(x)

    return x * cos + rotated * sin


def _get_layer_class(text_config) -> type:
    """Return decoder layer class based on model type."""
    model_type = getattr(text_config, "model_type", "")
    if "gemma4" in model_type or "gemma" in model_type:
        from transformers.models.gemma4.modeling_gemma4 import Gemma4TextDecoderLayer
        return Gemma4TextDecoderLayer
    raise ValueError(f"Unsupported model type: {model_type}")


def _load_final_norm() -> None:
    """Build final RMSNorm."""
    global _final_norm
    from pathlib import Path

    eps = 1e-5

    model_path = os.environ.get('MODEL_PATH', '/models')
    # Search both top-level and splits/ subdirectory
    candidates = [
        Path(model_path) / 'norm.safetensors',
        Path(model_path) / 'splits' / 'norm.safetensors',
    ]
    for nf in candidates:
        if nf.exists():
            from safetensors.torch import load_file
            w = load_file(str(nf))
            for k, v in w.items():
                if 'norm' in k:
                    _log("INFO", f"Rank: loaded norm.weight from {nf} ({v.shape})")
                    _final_norm = lambda x, weight=v, e=eps: _rms_norm(x, weight, e)
                    return
            _log("WARN", f"Rank: 'norm' key not found in {nf}")
    _log("WARN", f"Rank: norm.safetensors not found in {candidates}")
    _final_norm = lambda x, e=eps: _rms_norm(x, None, e)


def _decode_result_token(token_id: int) -> str:
    """Decode token ID to string."""
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(
            _model_name, trust_remote_code=True, local_files_only=True, timeout=60,
        )
    return _tokenizer.decode([token_id])


def _start_http_server(config: PipelineConfig, node: "FullyOptimizedPipelineNode", host: str = "0.0.0.0", port: int = 8082) -> None:
    """Start HTTP server (background thread)."""
    server = HTTPServer((host, port), _PredictHandler)
    server.pipeline_config = config
    server.node = node
    server.request_queue_size = 16
    _log("INFO", f"HTTP server listening on {host}:{port}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


# ====================================================================
# Entry point
# ====================================================================

def main() -> None:
    """Entry point."""

    # Pre-validate environment variables
    required_env_vars = ["RANK", "WORLD_SIZE", "MASTER_ADDR"]
    missing = [v for v in required_env_vars if v not in os.environ]
    if missing:
        _log("ERROR", f"Missing environment variables: {', '.join(missing)}")
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

        # Start HTTP server only on Rank 0 (accept requests from management node)
        if config.rank == 0:
            _start_http_server(config, node)

        node.process_pipeline_inference()
    except Exception:
        _log("ERROR", f"Fatal error at rank {config.rank}: {traceback.format_exc()}")
        # Let Docker restart policy (--restart=unless-stopped) handle restart
        try:
            if dist.is_initialized():
                dist.destroy_process_group()
        except Exception:
            pass
        time.sleep(3)
        sys.exit(1)


if __name__ == "__main__":
    main()
