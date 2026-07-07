# ====================================================================
# Distributed LLM Inference Pipeline - Docker Image Definition
# ====================================================================
#
# This image builds a lightweight Python environment optimized for
# PyTorch distributed inference on CPU (Gloo backend).
#
# Key components:
#   - OpenBLAS: High-performance matrix computation library
#   - numactl: NUMA memory affinity control
#   - iproute2: Physical NIC detection (ip route command)
#   - Intel OpenMP: Thread affinity optimization
# ====================================================================

FROM python:3.12-slim

# Install system packages
# build-essential: compilation toolchain
# libopenblas-dev: high-performance matrix computation library
# numactl: NUMA memory affinity control
# iproute2: physical NIC detection (ip route command)
# procps: process monitoring tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libopenblas-dev \
    locales \
    numactl \
    iproute2 \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i '/^# C.UTF-8/s/^# //' /etc/locale.gen 2>/dev/null; locale-gen 2>/dev/null; true

# Install PyTorch (CPU version)
# Explicitly specify index-url to get CPU-only wheels
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

# Install Intel OpenMP runtime library
# Replacing GNU OpenMP via LD_PRELOAD enables
# Intel's proprietary thread affinity control (KMP_AFFINITY, etc.)
RUN pip install --no-cache-dir intel-openmp

# Hugging Face model libraries (safetensors loading, AutoConfig)
RUN pip install --no-cache-dir transformers safetensors huggingface_hub

# Progress bar display
RUN pip install --no-cache-dir tqdm

# Create directory for HF cache as root (for pre-caching)
ENV HF_HOME=/root/.cache/huggingface
# Pre-cache Hugging Face model config (prevent network hang at startup)
RUN python3 -c "from transformers import AutoConfig, AutoTokenizer; c = AutoConfig.from_pretrained('google/gemma-4-31B-it', trust_remote_code=True, timeout=120); t = AutoTokenizer.from_pretrained('google/gemma-4-31B-it', trust_remote_code=True, timeout=120); print(f'Config: {type(c).__name__}, Tokenizer: {type(t).__name__}')" \
    && mkdir -p /app/.cache/huggingface \
    && cp -a /root/.cache/huggingface/* /app/.cache/huggingface/ 2>/dev/null || true \
    && chmod -R a+rx /root/.cache/huggingface 2>/dev/null || true
# Change HF cache location for llmuser
ENV HF_HOME=/app/.cache/huggingface
# Persist torch.compile (inductor) compiled kernel cache under /models.
# Default /tmp/torchinductor_<user>/ is deleted on container restart,
# so placing it in the same volume as model weights skips recompilation after restart.
ENV TORCHINDUCTOR_CACHE_DIR=/torch_compile_cache
# Set UTF-8 locale as default (ensures Japanese output support)
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONIOENCODING=utf-8

WORKDIR /app

# Place optimized LLM pipeline communication script
COPY pipeline_inference.py .
# Place config.json (AutoConfig references model name and override settings)
COPY config.json .
# Cluster node list (for cross-node signaling)
COPY hosts.txt .

# Security: Run as non-root user
RUN groupadd -r llmuser && useradd -r -g llmuser -d /app -s /sbin/nologin llmuser
# /models directory is used as destination for model weights distributed via scp,
# so create the directory inside the container
RUN mkdir -p /models && chown llmuser:llmuser /models
# Create and set ownership of Hugging Face cache directory (written by AutoConfig)
# chown /app entirely so COPYed files (pipeline_inference.py, etc.) are
# readable by llmuser (COPY inherits host's 600 permissions)
RUN mkdir -p /app/.cache && chown -R llmuser:llmuser /app
USER llmuser

# Health check: Verify process liveness
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import torch; print('healthy')" || exit 1

# Startup entry point
ENTRYPOINT ["python", "-u", "pipeline_inference.py"]
