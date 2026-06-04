# 分散LLM推論パイプライン

ThinkPad を超多段パイプライン並列に接続し、大規模言語モデル（LLM）の推論を分散実行するシステム。

単一のCPUでは現実的な速度で実行できない大規模モデルを、ネットワークで接続した複数ノードで分割推論することで、実用的なトークン生成速度を実現する。

## アーキテクチャ

```mermaid
graph TB
    subgraph Local["ローカル開発環境"]
        Build[Docker Build]
        Split[モデル分割]
    end

    subgraph Master["管理ノード"]
        Reg[Docker Registry<br/>HTTP :5000]
        PyTorch[PyTorch Master<br/>TCP :29500]
        Orch[オーケストレーション]
    end

    subgraph Network["1GbE Switch"]
        Switch((Switch))
    end

    subgraph Nodes["推論ノード群"]
        R0[Rank 0<br/>入力生成 + 計算]
        R1[Rank 1<br/>中継 + 計算]
        R2[Rank 2<br/>中継 + 計算]
        RN[最終Rank<br/>計算 + 出力]
    end

    Local -.SSHトンネル.->|docker push| Master
    Local -.rsync.->|モデル配布| Master
    Master --> Network
    Network --> R0
    Network --> R1
    Network --> R2
    Network --> RN

    R0 -->|テンソル送信| R1
    R1 -->|テンソル送信| R2
    R2 -->|テンソル送信| RN

    Reg -.->|SSHトンネル経由でpull| R0
    Reg -.->|SSHトンネル経由でpull| R1
    Reg -.->|SSHトンネル経由でpull| R2
    Reg -.->|SSHトンネル経由でpull| RN

    Master -.rsync.->|モデル配布| R0
    Master -.rsync.->|モデル配布| R1
    Master -.rsync.->|モデル配布| R2
    Master -.rsync.->|モデル配布| RN
```

## 仕組み

本システムは **パイプライン並列（Pipeline Parallelism）** を採用している。

1. モデルの Transformer レイヤーをノード数で分割
2. 各ノードが担当レイヤーの重みをロード
3. 入力をマイクロバッチに分割し、パイプラインに順次投入
4. 各ノードは前段からテンソルを受信し、自分のレイヤーで計算した結果を次段へ送信
5. 最終ノードが生成したトークンを出力

### デプロイフロー

デプロイは以下の 4 フェーズで構成される。

| フェーズ | 場所 | 処理 |
|---|---|---|
| 1. ビルド | ローカル | Docker イメージをビルドし、SSH トンネル経由でマスターのレジストリにプッシュ |
| 2. 分割 | ローカル | Hugging Face からモデルをダウンロード・分割し、rsync でマスターへ転送 |
| 3. 配布 | マスター | rsync で全ノードへモデル重みを配布 |
| 4. デプロイ | 各ノード | SSH トンネル経由でイメージをプルし、コンテナを起動 |

### ローカル環境と管理ノードの役割

| 環境 | 役割 |
|---|---|
| ローカル | Docker ビルド、モデルダウンロード・分割、オーケストレーション実行 |
| 管理ノード | Docker レジストリ、モデル staging、全ノードへのファイル配布、パイプライン推論の制御 |
| 推論ノード | 担当レイヤーの推論実行 |

### 非対称レイヤー割り当て

レイヤー数がノード数を整数倍でない場合でも、効率的に割り当てるスキームを採用している。

| 条件 | 割り当て |
|---|---|
| `Rank < (TOTAL_LAYERS - WORLD_SIZE)` | 2レイヤー担当 |
| `Rank >= (TOTAL_LAYERS - WORLD_SIZE)` | 1レイヤー担当 |

例: `WORLD_SIZE=50`, `TOTAL_LAYERS=80`

- Rank 0-29: 各2レイヤー（計60レイヤー）
- Rank 30-49: 各1レイヤー（計20レイヤー）
- 合計: 80レイヤー

各ノードの最大担当レイヤー数は2に制限しており、バブル（待機時間）を抑制する。

## 主要最適化技術

| 最適化項目 | 手法 | 効果 |
|---|---|---|
| ネットワーク | `--net=host` + 物理NIC固定 | 仮想ブリッジのオーバーヘッド排除 |
| メモリ | ゼロアロケーション通信バッファ | GC / malloc による遅延を完全排除 |
| パイプライン | マイクロバッチ分割（M=4） | パイプラインバブルを最小化 |
| ストレージ | 時間差起動（δ=3.0秒） | 全ノードの一斉アクセスによる輻輳回避 |
| CPU | 物理コア固定 + HT排除 | スレッドマイグレーションを抑止 |
| 量子化 | safetensors 形式 | モデル読み込みの高速化とメモリ効率向上 |

### ゼロアロケーション通信

各ノードは推論開始前に受信・送信バッファを事前に確保し、以降の通信では `torch.Tensor.add_()` などのインプレース演算のみでテンソルを更新する。これにより、推論ループ中にヒープ割り当てが一切発生せず、GC や malloc の遅延によるジャマーが生じない。

### マイクロバッチによるバブル削減

入力バッチを M 個のマイクロバッチに分割し、隙間なくパイプラインに投入する。これにより、パイプラインバブルの割合は以下の式で計算される。

`D = WORLD_SIZE`, `M = マイクロバッチ数` のとき:

`バブル割合 = (D - 1) / (M + D - 1)`

M=4, D=50 の場合: バブル割合 ≈ 92.4% → 実際には前段ノードの計算が完了次第次段が稼働するため、実効バブルはこれより小さい。

### 時間差起動（Staggered Model Loading）

全ノードが同時に Docker イメージのプルとモデル重みの読み込みを開始すると、ネットワークとディスクI/Oが輻輳する（サンダリングハード問題）。本システムでは、Rank番号に比例した遅延（δ=3.0秒）を挿入することで、全ノードの起動を時系列に分散する。

`T_delay = Rank × δ`

## 前提条件

- 管理/開発サーバ 1台 + ThinkPad N台（全て1GbEで接続）
- 全マシンに Docker がインストール済み
- 管理サーバから全ThinkPadへのSSH接続が可能（鍵認証推奨）
- [mise](https://mise.jdx.dev/) がインストール済み
- 全マシンに `uv` がインストール済み（miseが自動インストールする）

## クイックスタート

### 1. mise で環境を構築

```bash
# mise が Python と uv を自動インストール
mise install

# Python 仮想環境を構築・依存パッケージをインストール
mise run sync
```

### 2. 設定ファイルの作成

```bash
# config.json テンプレートを作成
mise run setup:env

# 環境に合わせて編集
vim config.json
```

### 3. ホスト一覧の設定

`hosts.txt` にThinkPadのIPアドレスまたはホスト名を1行1つ記述する。

```
192.168.1.101
192.168.1.102
192.168.1.103
```

行の順番が Rank 番号に対応する（1行目がRank 0、2行目がRank 1...）。

### 4. Dockerレジストリの構築

```bash
# 管理サーバ上でHTTP（TLSなし）のDockerレジストリを起動
mise run setup:registry
```

レジストリは管理サーバの `localhost:5000` で起動する。各ノードはSSHトンネル経由でイメージにアクセスする。

### 5. モデルのダウンロード・分割

```bash
# config.json の model.name にHFモデル名を指定
# モデルをダウンロードし、レイヤーごとに分割
uv run python tools/split_model.py

# ドライラン（分割計画のみ表示）
uv run python tools/split_model.py --dry-run
```

分割されたファイル（`layer_0.safetensors`, `layer_1.safetensors`...）は `models/splits/` ディレクトリに出力され、マスターへ転送後、全ノードの `work_dir/models/splits/` に配置される。

### 6. デプロイ

```bash
# フルデプロイ（ビルド + モデル配布 + 全ノードへ配信・起動）
mise run deploy

# ドライラン（実行内容の確認のみ）
mise run deploy:dry-run

# ビルドのみ
mise run build

# デプロイのみ（ビルド済みイメージ使用）
mise run deploy:only
```

デプロイのフロー:

1. ローカルで Docker イメージをビルドし、SSH トンネル経由でマスターのレジストリにプッシュ
2. ローカルでモデルをダウンロード・分割し、マスターへ転送
3. マスターから rsync で全ノードへモデル重みを配布
4. 各ノードで SSH トンネル経由でイメージをプルし、コンテナを起動

### 7. 監視・運用

```bash
# ヘルスチェック（全ノードのステータスを確認）
mise run status

# 詳細ヘルスチェック（ログ・CPU温度を含む）
mise run status:verbose

# 特定ノードのログ表示（RANK環境変数で指定）
RANK=0 mise run logs

# 全ノードの最新ログを一括表示
mise run logs:all

# 全ノード停止
mise run stop

# 全ノード再起動
mise run restart

# 全ノードクリーンアップ（コンテナ・イメージ削除）
mise run clean
```

### 8. デバッグ

```bash
# SSH接続テスト
mise run debug:ssh

# MTU設定確認
mise run debug:mtu

# モデル重み配置状態確認
mise run debug:models

# ポート開放状態確認
mise run debug:ports

# CPU温度確認
mise run debug:temp
```

## 全 mise タスク一覧

| タスク | 説明 |
|---|---|
| `sync` | Python 仮想環境の構築・依存パッケージの同期 |
| `setup:env` | config.json テンプレートを作成 |
| `setup:registry` | プライベートDockerレジストリ（HTTP）を管理ノード上に構築 |
| `split:models` | HF モデルをダウンロード・分割し、マスターへ転送 |
| `split:models:dry-run` | モデル分割の計画のみ表示（ローカル） |
| `build` | Dockerイメージをローカルビルドしてレジストリにプッシュ（SSHトンネル経由） |
| `deploy` | フルデプロイ（ローカル build + split + transfer + distribute + deploy） |
| `deploy:only` | モデル配布 + デプロイ（ビルド済みイメージ使用） |
| `deploy:dry-run` | デプロイのドライラン |
| `status` | 全ノードのヘルスチェック |
| `status:verbose` | 詳細ヘルスチェック（ログ・CPU温度含む） |
| `logs` | 全ノードの最新ログを一括表示 |
| `stop` | 全ノードの推論コンテナを停止 |
| `restart` | 全ノードの推論コンテナを再起動 |
| `clean` | 全ノードのコンテナとイメージを完全削除 |
| `debug:ssh` | 全ノードへのSSH接続テスト |
| `debug:mtu` | 全ノードのMTU設定を確認 |
| `debug:models` | 全ノードのモデル重み配置状態を確認 |
| `debug:ports` | 管理ノードの必要ポートの開放状態を確認 |
| `debug:temp` | 全ノードのCPU温度を確認 |

## ファイル構成

```
distributed-llm/
├── mise.toml               # mise タスク定義
├── pyproject.toml          # uv プロジェクト定義（依存パッケージ）
├── config.json             # 環境設定ファイル
├── config.json.example     # 設定ファイルテンプレート
├── hosts.txt               # ThinkPad IPアドレス / ホスト名一覧
├── Dockerfile              # コンテナイメージ定義
├── pipeline_inference.py   # パイプライン推論メインコード（ノード上で動作）
├── tools/                  # 運用管理ツール群（Python）
│   ├── __init__.py
│   ├── common.py           # 共通ユーティリティ（SSH, Rsync, ログ, 設定管理）
│   ├── deploy.py           # 自動デプロイ（ビルド・配布・起動）
│   ├── setup_registry.py   # プライベートDockerレジストリ構築（HTTP）
│   ├── healthcheck.py      # クラスタヘルスチェック
│   ├── cluster_control.py  # 停止・再起動・クリーンアップ
│   ├── show_logs.py        # コンテナログ表示
│   ├── debug_tools.py      # デバッグツール群
│   └── split_model.py      # HF モデルをレイヤーごとに分割
└── README.md               # 本ドキュメント
```

## config.json の設定項目

| セクション | キー | 説明 | デフォルト |
|---|---|---|---|
| **model** | `name` | Hugging Face のモデル名 | `meta-llama/Llama-3.1-70B` |
| | `format` | 重み形式（`safetensors` または `pt`） | `safetensors` |
| **cluster** | `master_addr` | PyTorchマスター / 管理サーバのIPまたはホスト名 | `192.168.1.100` |
| | `master_port` | PyTorch分散通信のマスターポート | `29500` |
| | `hosts_file` | ホストファイルのパス | `hosts.txt` |
| **ssh** | `user` | SSH接続時のユーザー名 | `user` |
| **docker** | `image_name` | Dockerイメージ名 | `llm-pipeline-image:latest` |
| | `registry_port` | Dockerレジストリのポート | `5000` |
| **deploy** | `work_dir` | 全ノード共通のベースディレクトリ | （必須） |

### 環境変数による上書き

主要パラメータは環境変数で上書き可能。

| 環境変数 | 対応パラメータ | 説明 |
|---|---|---|
| `WORLD_SIZE` | 自動計算 | ノード数（hosts.txt の行数から自動計算） |
| `MASTER_ADDR` | `cluster.master_addr` | マネージャのIPアドレス |
| `MASTER_PORT` | `cluster.master_port` | マネージャのポート |
| `NUM_MICRO_BATCHES` | 4 | マイクロバッチ数 |
| `STAGGER_INTERVAL` | 3.0 | 時間差起動のインターバル（秒） |
| `GLOO_SOCKET_IFNAME` | `eth0` | 使用するネットワークインターフェース名 |
| `CPUSET_CPUS` | `0-3` | コンテナに割り当てるCPUコア範囲 |
| `OMP_NUM_THREADS` | `4` | OpenMPスレッド数 |
| `WORK_DIR` | `deploy.work_dir` | 全ノード共通のベースディレクトリ |

## 技術スタック

| コンポーネント | 技術 |
|---|---|
| ツールチェーン管理 | [mise](https://mise.jdx.dev/) |
| Python パッケージ管理 | [uv](https://docs.astral.sh/uv/) |
| コンテナランタイム | Docker（ホストネットワークモード） |
| 分散通信 | PyTorch `torch.distributed`（Gloo バックエンド） |
| モデル形式 | safetensors / PyTorch |
| モデル取得 | Hugging Face `transformers` |
| 運用ツール | Python 3.12（標準ライブラリのみ） |

## 推論フローの詳細

### 1. Docker イメージのビルド・プッシュ

ローカルで Dockerfile からイメージをビルドし、SSH トンネル経由で管理ノードのプライベートレジストリにプッシュする。イメージには Python 環境・PyTorch・モデル推論コードが含まれる。

### 2. モデル分割

ローカルで `tools/split_model.py` を実行すると、Hugging Face からモデルをダウンロードし、Transformer レイヤーごとに分割してファイルに保存する。分割結果は rsync で管理ノードの `work_dir/models` へ転送される。

```
meta-llama/Llama-3.1-70B
├── model.layers.0.*  →  layer_0.safetensors
├── model.layers.1.*  →  layer_1.safetensors
├── ...
└── model.layers.79.* →  layer_79.safetensors
```

### 3. モデル配布

管理ノード上の `work_dir/models/splits/` から rsync で全ノードへモデル重みを配布する。各ノードは `work_dir/models/splits/` に配置されたファイルを読み込む。

### 4. ノード起動

各ノードは SSH トンネル経由で管理ノードのレジストリからイメージをプルし、コンテナを起動する。起動時に以下の環境変数が渡される。

- `MASTER_ADDR`, `MASTER_PORT`: 分散通信のマスター情報
- `RANK`, `WORLD_SIZE`: ノードの位置とクラスタサイズ
- `GLOO_SOCKET_IFNAME`: 使用するNIC
- `OMP_NUM_THREADS`, `KMP_AFFINITY`: CPUアフィニティ設定
- `NUM_MICRO_BATCHES`, `STAGGER_INTERVAL`: パイプライン制御パラメータ

### 5. 推論実行

コンテナ起動後、`pipeline_inference.py` が以下のループを継続する。

1. 前段ノードからテンソルを受信（Rank 0 の場合はランダム入力生成）
2. 担当レイヤーでインプレース計算を実行
3. 計算結果を次段ノードへ送信（最終Rank の場合は出力）

## トラブルシューティング

### `dist.init_process_group` でハングアップする

**原因**: Wi-Fi 等の誤ったNICにGlooがバインドしている可能性。

**対策**:

```bash
# コンテナログで検出NICを確認
RANK=0 mise run logs

# 特定ノードのNICを手動指定してデプロイ
GLOO_SOCKET_IFNAME=enp0s31f6 mise run deploy
```

### モデルロード時にタイムアウトする

**原因**: 時間差起動の間隔が短すぎるか、rsync 配布に時間がかかりすぎている。

**対策**:

```bash
# 起動遅延係数を延長
STAGGER_INTERVAL=10.0 mise run deploy
```

### トークン生成速度が低下する（サーマルスロットリング）

**原因**: CPU温度が許容限界に達しクロック周波数が降下。

**対策**:

```bash
# CPU温度を確認
mise run debug:temp

# 物理コア数を削減して発熱を抑制
CPUSET_CPUS=0-1 OMP_NUM_THREADS=2 mise run deploy
```

### モデル重みがノードに配置されていない

**対策**:

```bash
# モデル配置状態を確認
mise run debug:models

# 手動で再配布
uv run python tools/deploy.py --deploy-only
```

### Dockerイメージのビルド・プッシュに失敗する

**原因**: SSHトンネルが正常に確立されていない、またはレジストリが停止している。

**対策**:

```bash
# マスターへのSSHトンネルを手動でテスト
ssh -f -N -L 5000:localhost:5000 user@master_addr

# レジストリへの接続を確認
curl http://localhost:5000/v2/_catalog

# トンネルを終了
kill $(lsof -t -i:5000) 2>/dev/null || true

# デプロイ前にレジストリを再起動
mise run setup:registry
```

### Dockerイメージのプルに失敗する

**原因**: SSHトンネルが正常に確立されていない、またはレジストリが停止している。

**対策**:

```bash
# レジストリの状態を確認
curl http://<master_addr>:5000/v2/_catalog

# デプロイ前にレジストリを再起動
mise run setup:registry
```

## ライセンス

MIT License
