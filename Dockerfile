# ====================================================================
# 分散LLM推論パイプライン - Dockerイメージ定義
# ====================================================================
#
# 本イメージは、CPU上でのPyTorch分散推論（Glooバックエンド）に最適化された
# 軽量なPython環境を構築する。
#
# 主要コンポーネント:
#   - OpenBLAS: 高速行列演算ライブラリ
#   - numactl: NUMAメモリアフィニティ制御
#   - iproute2: 物理NIC動的検出（ip route コマンド）
#   - Intel OpenMP: スレッドアフィニティ最適化
# ====================================================================

FROM python:3.12-slim

# システムパッケージのインストール
# build-essential: コンパイルツールチェーン
# libopenblas-dev: 高速行列演算ライブラリ
# numactl: NUMAメモリアフィニティ制御
# iproute2: 物理NIC動的検出（ip route コマンド）
# procps: プロセス監視ツール
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libopenblas-dev \
    numactl \
    iproute2 \
    procps \
    && rm -rf /var/lib/apt/lists/*

# PyTorch (CPU版) のインストール
# index-url を明示指定し、CPU専用ホイールのみを取得する
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

# Intel OpenMP ランタイムライブラリのインストール
# GNU OpenMP利用時にLD_PRELOADで差し替えることで、
# KMP_AFFINITY等のIntel独自スレッドアフィニティ制御が有効になる
RUN pip install --no-cache-dir intel-openmp

# Hugging Face モデル用ライブラリ（safetensors 読み込み、AutoConfig 用）
RUN pip install --no-cache-dir transformers safetensors huggingface_hub

# 進捗表示用
RUN pip install --no-cache-dir tqdm

WORKDIR /app

# 最適化されたLLMパイプライン通信スクリプトの配置
COPY pipeline_inference.py .
# config.json の配置（モデル名やオーバーライド設定を AutoConfig が参照）
COPY config.json .
# クラスタノード一覧（クロスノードシグナリング用）
COPY hosts.txt .

# セキュリティ: 非rootユーザーでの実行
RUN groupadd -r llmuser && useradd -r -g llmuser -d /app -s /sbin/nologin llmuser
# /models ディレクトリは scp で配布されたモデル重みの配置先として使用されるため、
# コンテナ内にディレクトリを作成しておく
RUN mkdir -p /models && chown llmuser:llmuser /models
# Hugging Face のキャッシュディレクトリを作成（AutoConfig が書き込む）
RUN mkdir -p /app/.cache && chown -R llmuser:llmuser /app/.cache
USER llmuser

# ヘルスチェック: プロセスの生存確認
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import torch; print('healthy')" || exit 1

# 起動エントリポイント
ENTRYPOINT ["python", "-u", "pipeline_inference.py"]
