"""
tools パッケージ: 分散LLM推論クラスタの運用管理ツール群

各ツールは独立したスクリプトとして実行できる:
  - common.py:      共通ユーティリティ（SSH, Rsync, ログ, 設定管理）
  - deploy.py:      自動デプロイ（ローカルビルド + モデル分割 + 配布 + 起動）
  - setup_registry.py: 管理ノード上のプライベートDockerレジストリ構築
  - healthcheck.py: クラスタヘルスチェック
  - cluster_control.py: コンテナ制御（停止・再起動・クリーンアップ）
  - show_logs.py:   コンテナログ表示
  - debug_tools.py: デバッグツール群
  - split_model.py: HF モデルをレイヤーごとに分割
"""
