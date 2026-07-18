# 実験ジャーナル: distributed-llm

research-cycle が読み書きする実験ジャーナル．**新しいイテレーションを常に先頭へ挿入する（逆時系列）**．
1 イテレーション = 単一レバー変更．各ブロックに仮説・単一レバー・成功条件（planner 記入）と，
変更・結果・判定・学び（reflector 記入）をまとめる．

---

## Iteration 1

**フェーズ**: 計画（2026-07-18）．担当＝計画 subagent．対象＝ config.yml `research_frontier①`（結果永続化基盤の実装）．
本イテレーションは**コード実装のみ**で，実機クラスタへの deploy / 推論実行は行わない（backlog B1 の合意）．
実機での end-to-end 検証はフェーズ4（実験）へ持ち越し，人間確認を経てから実施する．

### 1. 仮説

rank 0（wafl-ctrl1）の `docker logs` にしか存在しない定量指標（step 時間・TTFT・ITL・tokens/sec・prompt/output tokens・
埋め込み統計）を，非介入の外部ツールでパースして `results/Iter{n}.jsonl` に 1 実行 = 1 レコードで構造化保存できれば，
以後のレバー比較（②NUM_MICRO_BATCHES 感度分析 以降）を定量評価できる土台が整う．この基盤が無い限り，レバー比較は
stdout の目視に留まり判定できない．基盤の実装は `pipeline_inference.py`（ホットパス）を非改変・追加のみ・完全可逆で行える．

### 2. 単一レバー・変更内容

単一レバー＝**「結果永続化基盤の新規実装」そのもの**（他のパイプライン設定は直近構成に固定，コード上の既定値のまま触らない）．
採用アーキは調査フェーズ推奨の案 A（外部収集ツール）．案 B（`pipeline_inference.py` 内での in-process JSONL 書き出し）は
ホットパス改変・再デプロイが必要で iter1 の可逆性・非介入方針に反するため不採用．

**(a) ファイル構成（新規のみ）**
- `tools/collect_results.py`（新規）: 1 回の「記録付き推論」をオーケストレーションする．
  1. レバー値と実行メタを収集（`ClusterConfig` + `os.environ`）．
  2. 送信直前の UTC 時刻 `run_start`（RFC3339）を記録し，`run_id` を採番（`Iter{n}-{UTCyyyymmddThhmmssZ}-{短縮 uuid}`）．
  3. プロンプト送信は `tools/predict.py` の `send_prompt_ssh`（または `--http` 時 `send_prompt_http`）を**import して再利用**する
     （送信ロジックを複製しない．`predict.py` は非改変）．戻り値 `result_text`（全文）と e2e 実測時間を保持．
  4. 送信後に wafl-ctrl1 の rank 0 コンテナから `docker logs --since {run_start} distributed-llm 2>&1` を
     `ssh_via_master(ssh_user, master_addr, master_addr, ...)` で取得（`--since` で当該実行のみに区間限定＝並行実行や
     過去実行の混入を防ぐ）．ANSI 除去（`re.sub(r'\x1b\[[0-9;]*m','',line)`）後，`^\[R0 \w+\] (.*)$` に一致する rank0 行だけを対象にパース．
  5. 導出指標を計算し，`results/Iter{n}.jsonl` へ 1 レコード追記（`open(..., "a")`，`ensure_ascii=False`，末尾改行）．`results/` は無ければ作成．
- `tests/test_collect_results.py`（新規）: 収集ロジックの純パース関数に対する単体テスト（クラスタ・SSH 不要）．
- `tests/fixtures/rank0_sample.log`（新規）: 下記の実ログ行フォーマットを再現した固定サンプル（パーサの仕様書兼回帰テスト入力）．

**(b) パース方式（rank 0 の実ログ行に対応，出典＝`pipeline_inference.py`）**
- プロンプト開始: `Rank 0: prompt='...'`（`:1441`）＝実行ブロックの先頭マーカー．
- prompt tokens + 埋め込み統計: `Rank 0: prompt tokens={seq_len}, embedding shape=... mean=.. std=.. min=.. max=..`（`:1445`）
  → 正規表現 `Rank 0: prompt tokens=(\d+),.*mean=([-\d.eE]+) std=([-\d.eE]+) min=([-\d.eE]+) max=([-\d.eE]+)`．
- 毎ステップ生成時間: `Rank 0: step (\d+) done token=(\d+) dt=([\d.]+)s`（`:1524`）→ `step_dt[]` を step 昇順に構築．
- 生成トークン数: `Rank 0: decoding (\d+) generated tokens \(prompt=(\d+)\)`（`:1528`）．
- 復号時間: `Rank 0: decoded in ([\d.]+)s:`（`:1531`）．
- 実行終了/応答: `[R0 RESULT] Request response: '...'`（`:1814`，先頭 100 文字のみのため全文は predict 戻り値を採用）．
- `--since` で 1 実行に限定しても複数ブロックが残る場合は，末尾（最新）の `Rank 0: prompt=` 以降を採用し，
  さらに RESULT 行の先頭 100 文字が predict 戻り値の先頭と一致するブロックを優先する（防御的照合）．

**(c) 導出指標（純関数で計算，単体テスト対象）**
- `ttft_s = step_dt[0]`（step0 の dt．prefill が全 51 段を通過する時間を含む，との注記付き）．
- `generation_time_s = sum(step_dt)`．
- `output_tokens = decoding の n`（無ければ `len(step_dt)`）．
- `tokens_per_sec = output_tokens / generation_time_s`（`generation_time_s==0` は `null`）．
- `itl_p50_s / itl_p95_s = percentile(step_dt[1:], 50/95)`（step0 を除く．要素 0 個なら `null`）．
- `decode_time_s`（decoded 行），`prompt_tokens`，`embed_stats{mean,std,min,max}`，`e2e_latency_s`（収集側の壁時計実測）．

**(d) JSONL スキーマ確定版（1 行 = 1 実行．調査案に運用フィールドを追加）**
```json
{
  "schema_version": 1,
  "iter": "Iter1",
  "run_id": "Iter1-20260718T081500Z-a1b2c3",
  "timestamp": "2026-07-18T08:15:00Z",      // run_start（UTC, RFC3339）
  "prompt": "Hello!",
  "prompt_tokens": 0,
  "output_tokens": 0,
  "step_dt": [],                              // 生の per-step dt 配列（秒）
  "ttft_s": null,
  "generation_time_s": null,
  "tokens_per_sec": null,
  "itl_p50_s": null,
  "itl_p95_s": null,
  "decode_time_s": null,
  "e2e_latency_s": null,                      // 収集側実測（送信〜応答）
  "result_text": "",                          // predict 戻り値（全文）
  "embed_stats": {"mean": null, "std": null, "min": null, "max": null},
  "levers": {"NUM_MICRO_BATCHES": null, "STAGGER_INTERVAL": null, "SEQ_LEN": null, "WORLD_SIZE": null},
  "parse_ok": true,                           // 必須指標が全て取れたか
  "parse_warnings": []                        // 欠落・不一致の記録（空配列が正常）
}
```
`schema_version` / `parse_ok` / `parse_warnings` は調査案への追加．②以降で欠測レコードを機械的に除外・追跡するため
（測定できなかったものを黙って埋めない，という CLAUDE.md の方針に沿う）．

**(e) mise タスクへの組み込み**
- `mise.toml` の `[tasks."predict:demo"]` の `run` を，デモプロンプトを収集ツール経由で送るよう変更する:
  `uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!"`．
  これで success_criteria の文言（`predict:demo` 実行後に `results/Iter{n}.jsonl` 生成）を満たす．
- 対話用の素の `[tasks.predict]`（`tools/predict.py`）は**非改変**で残す（記録不要の即時実行を維持）．
- `iter` は当面 CLI 引数で明示（既定 `Iter1`）．将来 state.json 連動にする余地はあるが iter1 では過剰なので入れない．

### 3. 成功条件（measurable）

本イテレーションは実機実行をしないため，**コードレベルの検証（今回完了条件）**と**end-to-end 検証（フェーズ4・人間確認後）**に分ける．

- **今回の完了条件（クラスタ・SSH 不要で検証可能）**:
  1. `tests/fixtures/rank0_sample.log`（既知の入力）を `collect_results.py` のパース純関数に通すと，
     `ttft_s`・`generation_time_s`・`tokens_per_sec`・`itl_p50_s`・`itl_p95_s`・`output_tokens`・`prompt_tokens`・
     `embed_stats`・`step_dt[]`（非空）が期待値どおりに埋まり，`parse_ok == true` となる（単体テストで assert）．
  2. `uv run pytest tests/test_collect_results.py` が green．型・lint（プロジェクト既定）を通す．
  3. `results/Iter{n}.jsonl` への書き出し関数が，固定入力に対し 1 行の妥当な JSON（`json.loads` 可・スキーマ全キー存在）を追記する
     ことを一時ディレクトリ上のテストで確認．
- **end-to-end 完了条件（フェーズ4，人間確認後に検証）**:
  4. `mise run predict:demo` 実行後に `results/Iter1.jsonl` が生成され，`step_dt[]` 非空かつ `tokens_per_sec` が正の実測値を含む
     レコードが 1 行追記される（＝ config.yml success_criteria の充足）．

### 4. レバー値の記録方法（調査フェーズの要検討点への判断）

- **判断**: iter1 では `levers` を**収集ツール実行時の環境から埋める**（最小変更）．
  `NUM_MICRO_BATCHES` / `STAGGER_INTERVAL` は `os.environ` → 無ければ `ClusterConfig`（既定 `4` / `3.0`），
  `WORLD_SIZE` は `os.environ` → 無ければ `hosts.txt` 行数（=51），`SEQ_LEN` は `os.environ`（既定は不明のため未設定なら `null`）から取得する．
- **前提と限界（レコードに残す）**: この方式は「コンテナ起動時の env と収集時の env が一致している」ことを暗黙に仮定する．
  一致が保証されない場合は誤った levers を記録し得るため，不一致検出はできない旨を `parse_warnings` 運用で補えないこと自体を明記する．
- **フォローアップ提案（別イテレーション/別 PR，今回は実装しない）** `P1`: `pipeline_inference.py` 起動時に有効な実行設定を
  `Rank 0: effective config NUM_MICRO_BATCHES=.. STAGGER_INTERVAL=.. SEQ_LEN=.. WORLD_SIZE=..` の 1 行 INFO で出力し，
  収集側がログから直接 levers を確定する堅牢化．②のレバー比較で levers の信頼性が要件になった時点で着手する
  （ホットパス外の起動時 1 行なので graph-break リスクは低いが，pipeline 改変のため単一レバー原則上は別イテレーションで扱う）．

### 5. 実装フェーズ（rc-implementer）への申し送り

- **変更/新規ファイル**:
  - 新規 `tools/collect_results.py`: `send_prompt_ssh`/`send_prompt_http` は `from predict import ...`（`tools/` 内相対 import，既存ツールと同様に `common` を素 import）で再利用．`predict.py` は触らない．
  - 新規 `tests/test_collect_results.py`，`tests/fixtures/rank0_sample.log`．
  - 変更 `mise.toml`: `[tasks."predict:demo"].run` を `uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!"` に置換（`[tasks.predict]` は不変）．
- **設定キー/パラメータ**:
  - ログ取得: `ssh_via_master(config.ssh_user, config.master_addr, config.master_addr, "docker logs --since {run_start} distributed-llm 2>&1", timeout=30)`（rank0＝master 自身なので target=master_addr）．
  - levers 取得元: `os.environ["NUM_MICRO_BATCHES"|"STAGGER_INTERVAL"|"SEQ_LEN"|"WORLD_SIZE"]`，フォールバックは `ClusterConfig.num_micro_batches`/`.stagger_interval`/`.world_size`．
  - 出力先: リポジトリ直下 `results/Iter{iter}.jsonl`（`pathlib` で mkdir．`.gitignore` は `results/` を無視していないため成果物は追跡対象になる—`*.log` のみ無視．JSONL を commit する方針で問題ないか，実装時に diff で確認）．
- **パーサ設計上の注意**: (1) `docker logs` は既定でタイムスタンプ無し→ `--since` は付与可能．必要なら `-t` も併用しブロック境界を UTC で判定．
  (2) ANSI 除去を必ず先に行う（非 TTY 前提でも防御的に）．(3) EOS/ループ検出で早期 break した場合 `step_dt[]` が `max_new_tokens` 未満になり得るが正常（`parse_ok` は必須指標が揃えば true）．
  (4) 1 レコードも取れない/ブロック不一致時は `parse_ok=false` + `parse_warnings` を残して 1 行は書く（黙って捨てない）．
- **禁止事項の再掲**: 実機への `deploy`/`predict:demo` 実行はしない（コードとテストのみ）．end-to-end 検証はフェーズ4で人間確認後．

---

### 実装 (Iter1)

**担当**: 実装 subagent（2026-07-18）．計画フェーズ（本ブロック上記 1〜5）の単一レバー「結果永続化基盤の新規実装」を，
最小差分で反映した．実機クラスタへの接続・`deploy`/`predict:demo` 実行は行っていない．

**変更/新規ファイル**
- 新規 `tools/collect_results.py`: `tools/predict.py` の `send_prompt_ssh`/`send_prompt_http`/`get_prompt` を
  `from predict import ...` でそのまま再利用（`predict.py` は非改変）．`tools/common.py` の `ClusterConfig`/`ssh_via_master`
  も非改変で import のみ．パース純関数（`_extract_rank0_messages` → `_split_into_blocks` →
  `_select_relevant_block`（防御的照合）→ 各種 `_extract_*`）と，導出指標計算（`compute_derived_metrics`,
  `_percentile`）を SSH/クラスタ接続部分（`collect_rank0_log`, `run_and_collect`, `main`）から分離し，
  前者のみを単体テスト対象にした．JSONL スキーマは journal.md 記載の確定版（`schema_version=1`）どおりに実装．
- 新規 `tests/test_collect_results.py`: パース純関数・導出指標・`_select_relevant_block`・`build_levers`・
  `make_run_id`・`build_record`・`append_jsonl` に対する単体テスト 23 件．クラスタ・SSH 接続は無し．
- 新規 `tests/fixtures/rank0_sample.log`: 2 実行ブロック（older/latest）・ANSI 色コード混入行・他 rank
  （R1/R2）のノイズ行を含む固定サンプル．
- 新規 `tests/conftest.py`: 既存ツール群の慣行（`tools/` 内スクリプトから `from common import ...` する相対 import）
  に合わせ，pytest 実行時に `tools/` を `sys.path` へ追加するためだけの薄い設定．
- 変更 `mise.toml`: `[tasks."predict:demo"].run` を
  `uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!"` に変更．`[tasks.predict]` は非改変．
- 変更 `pyproject.toml` / `uv.lock`: `uv add --dev pytest` により `[dependency-groups].dev = ["pytest>=9.1.1"]` を追加．
  本リポジトリには pytest 等のテストランナーが一切導入されておらず（`uv run pytest` が `ModuleNotFoundError` になることを確認済み），
  成功条件 2「`uv run pytest tests/test_collect_results.py` が green」を満たすために必須の追加．`uv.lock` の差分の大半は
  既存依存関係の lockfile スキーマ更新（`upload-time` フィールド付与，revision 1→3）で，`pytest` 追加に伴う副作用．

**テスト結果**
```
uv run pytest tests/test_collect_results.py -v
============================== 23 passed in 0.07s ==============================
```
lint/型チェッカー（ruff/mypy 等）はこのリポジトリに未導入（`pyproject.toml`/`mise.toml`/CI 設定を確認したが記載無し）．
そのため `uv run python -m py_compile tools/collect_results.py tests/test_collect_results.py tests/conftest.py` で
構文健全性のみ確認した．

**計画からの差異（理由付き）**
- pytest が未導入だったため `uv add --dev pytest` を実行（計画には明記無しだが，成功条件の「pytest green」を
  満たすために必須．既存の依存関係グループ構成に沿って `dependency-groups.dev` に追加，本体依存には影響しない）．
- `tests/conftest.py` を計画のファイル一覧に無い形で追加した．`tools/collect_results.py` は既存ツール群と同じ流儀
  （`tools/` をスクリプトとして直接実行した際の `sys.path[0]` 依存の相対 import）で `from common import ...`
  / `from predict import ...` するため，pytest からテストするには `tools/` を `sys.path` に足す最小の橋渡しが要る．
  `predict.py`/`common.py`/`pipeline_inference.py` は一切変更していない．
- `send_prompt_ssh`/`send_prompt_http`/`get_prompt` は `predict.py` にトップレベル関数として既に定義されており
  （`if __name__ == "__main__":` に閉じていない），計画で想定された「import して再利用」がそのまま成立した．
  `predict.py` の変更は不要だった．

**次フェーズ（実験）への申し送り**
- コードレベルの完了条件（成功条件 1〜3）はすべて満たした．end-to-end 完了条件（成功条件 4，
  `mise run predict:demo` 実行 → `results/Iter1.jsonl` 生成）は，本イテレーションの合意どおり未実施．
  実機クラスタへの deploy/実行には人間確認が必要．
- `results/` ディレクトリは `.gitignore` に含まれていない（`*.log` のみ無視）ため，実機実行後に生成される
  `results/Iter1.jsonl` はそのまま commit 対象になる．想定どおりか実験フェーズ開始前に確認すること．

---

### 実験 (Iter1)

**担当**: 実験フェーズ subagent（2026-07-18）．オーケストレータが Slack で人間に確認を取り，
「進めてください．現行の実機構成（wafl-ctrl1 + worker 50台）で `mise run deploy` と
`mise run predict:demo`（収集ツール経由）を実行し，`results/Iter1.jsonl` の生成まで確認してください」という
明示的な承認（backlog.md B2）を得た上で，51 ノード実機クラスタに対して実行した．

**実行前の状態確認**

- `uv run python tools/healthcheck.py` を実行し，51 ノード全ての SSH 接続・docker daemon・
  `distributed-llm` コンテナ稼働・モデル重み配置・MTU が既に健全（Healthy: 51/51）であることを確認した．
  → **クラスタは既にデプロイ済みであったため，`mise run deploy`（再デプロイ）は実行しなかった．**
  再デプロイは全ノードのコンテナ再作成を伴う影響範囲の大きい操作であり，「既に動いているものを不要に
  作り直す」のは目的外の操作にあたると判断した．承認内容（deploy と predict:demo の実行確認）のうち，
  deploy 相当の状態確認はヘルスチェックで代替し，実行が必要なもの（predict:demo）のみ実施した．

**実行コマンドと結果**

1. `timeout 900 mise run predict:demo`（内部で `uv run python tools/collect_results.py --iter Iter1
   --prompt "Hello!"` を実行）
   - 標準エラー: `[INFO] Sending to wafl-ctrl1:8082 (iter=Iter1)...` →
     `[INFO] appended 1 record to results/Iter1.jsonl (parse_ok=True, tokens_per_sec=0.0946987...)` →
     `[WARN] no block's RESULT text matched the predict result prefix; used the latest block as a fallback`
   - 標準出力（結果本文）: `Hello! How can I help you today?\nthought`
   - 所要時間: 実測 e2e_latency_s ≈ 166.6 秒（30 分の timeout_min に対し十分短い）．
   - `results/Iter1.jsonl` が新規生成され，1 行のレコードが追記された．

2. 実行後，`uv run python tools/healthcheck.py` を再実行し，51/51 ノードが健全なままであることを確認した
   （クラスタへの悪影響なし）．

**`results/Iter1.jsonl` の内容（1 行，主要フィールド）**

```
schema_version=1, iter=Iter1, run_id=Iter1-20260718T084148Z-adc574
prompt="Hello!", prompt_tokens=15, output_tokens=15
step_dt=[26.012, 7.015, 6.903, ... , 6.924]（20 要素）
ttft_s=26.012, generation_time_s=158.397, tokens_per_sec=0.0946987632...
itl_p50_s=6.964, itl_p95_s=7.0763, decode_time_s=0.0, e2e_latency_s=166.551021
result_text="Hello! How can I help you today?\nthought"
embed_stats={mean:0.004217, std:1.108908, min:-15.3125, max:16.875}
levers={NUM_MICRO_BATCHES:4, STAGGER_INTERVAL:3.0, SEQ_LEN:null, WORLD_SIZE:51}
parse_ok=true
parse_warnings=["no block's RESULT text matched the predict result prefix; used the latest block as a fallback"]
```

**WARN の原因切り分け（コード修正はせず，原因のみ特定）**

rank0 の生 docker logs を直接確認した（`ssh denjo@wafl-ctrl1 "docker logs --since ... distributed-llm"`）．
実際の生成結果は複数行にまたがっていた:

```
[R0 RESULT] Request response: 'Hello! How can I help you today?
thought
'
```

`collect_results.py` の `_RESULT_RE = re.compile(r"^Request response: '(.*)$")` は `[R0 ...]` プレフィックス付き
の 1 行のみに一致するため，`_extract_result_text` は先頭行の `"Hello! How can I help you today?"` までしか
拾えない（2 行目の `thought` はブラケットプレフィックスが無く `_extract_rank0_messages` の対象外）．
一方，`result_text`（JSONL 内の実フィールド）は `send_prompt_ssh` の戻り値をそのまま使うため，
複数行を含む `"Hello! How can I help you today?\nthought"` になる．この 2 つの不一致により，
`_select_relevant_block` の照合（`predict_result[:100]` とログ由来 RESULT テキストの比較）が失敗し，
警告付きでフォールバック（最新ブロック採用）した．**今回は since ウィンドウ内にブロックが 1 個しか
無かったため，フォールバックの選択結果自体は正しく，指標抽出（`parse_ok=True`，全指標が値を持つ）に
実害は無い．** ただし，同一コンテナに複数実行が短時間で連続した場合（例: 次イテレーションで複数プロンプトを
連続送信する場合）は，ブロック取り違えのリスクが残る点は既知の限界として次フェーズへ申し送る．
また `pattern loop detected (6-token pattern at pos 0/21), keeping 15 tokens` という `[R0 WARN]` ログが
デコード直前に出ていた（モデル側の繰り返しパターン検出によるトークン切り詰め．今回の生成トークン数
15 個はこの切り詰め後の値）．これは推論エンジン側の挙動であり，収集ツールの不具合ではない．

**成功/失敗の判定**

- `success_criteria`（①: `mise run predict:demo` 実行後に `results/Iter{n}.jsonl` が
  ステップ時間・tokens/sec を含む形で生成される）は **達成**．
- 実行中の異常（コンテナ未起動・SSH 接続失敗・タイムアウト）は無し．
- 唯一の異常は上記の軽微な parse warning（フォールバック使用．今回は実害なし，既知の限界として記録）．
- クラスタは実行前後とも 51/51 ノード健全．破壊的操作は一切実行していない．

---

### 調査 (Iter1)

**担当**: 調査フェーズ subagent（2026-07-18）．research_frontier①（結果永続化基盤）の実装計画に向け，
既存ログ出力コード・ログ収集経路・パース設計の要点を，実機に触れず読み取り調査した．

**問い**
1. `[R{rank} LEVEL] message` はどこで何を（ステップ時間・tokens/sec・隠れ状態統計）出しているか．
2. `mise run logs` / `predict:demo` のログ収集経路（51 ノードの docker logs 集約）はどうなっているか．
3. 分散パイプライン推論ログ→JSONL 変換の一般的な設計上の要点は何か．

**分かったこと（コード読み取り，出典＝リポジトリ内ファイル:行）**

- ログ実体は `_log(level, msg)`（`pipeline_inference.py:180-192`）が `print(f"[R{_RANK} {tag}] {msg}", flush=True)` で
  stdout に出すだけ．レベルは INFO/DEBUG/OK/FAIL/WARN/ERROR/STEP/RESULT/TRACE の 9 種（`:167-177`）．
  **JSONL 等への構造化保存は一切存在しない**（`grep jsonl/json.dump` で確認．`results/` ディレクトリも無い）．
- **tokens/sec を直接出すログ行は存在しない**．TPS はパーサ側で算出が必要．素材となる行は全て **rank 0（wafl-ctrl1）** が出す:
  - 毎ステップの生成時間: `Rank 0: step {step} done token={id} dt={dt:.3f}s`（`:1524`）．
    正規表現例 `Rank 0: step (\d+) done token=(\d+) dt=([\d.]+)s`．step0 の dt ≒ TTFT（プロンプト prefill を全 51 段パイプライン通過する時間を含む），step≥1 の dt = inter-token latency．
  - プロンプト長＋埋め込みの隠れ状態統計: `Rank 0: prompt tokens={seq_len}, embedding shape=... mean=.. std=.. min=.. max=..`（`:1445`）．
  - 生成トークン数: `Rank 0: decoding {n} generated tokens (prompt={seq_len})...`（`:1528`），復号時間 `Rank 0: decoded in {..}s: '{result}'`（`:1531`）．
  - 実行境界: 開始 `Rank 0: prompt='...'`（`:1441`），終了 `[R0 RESULT] Request response: '{..}'`（`:1814`）．
    → 同一コンテナのログに複数実行が蓄積し得るため，この 2 行で実行単位に区切ってパースする必要がある．
- **隠れ状態統計は既定（INFO）では prompt 埋め込みの mean/std/min/max のみ**．層ごと・rank ごとの詳細な隠れ状態統計
  （`R{rank} L{idx} [{type}] IN shape=.. mean=.. std=.. min=.. max=..`, `:826`）と層別 op 時間（`:839`）は
  **TRACE レベル限定**で，`LOG_LEVEL=TRACE` の時だけ出力（`:166`．既定は無効＝graph-break/性能コスト回避のためのガード）．
- ANSI 色は `sys.stdout.isatty()` が偽なら全て空文字に無効化される（`:148-155`）．`docker run`（`-t` 無し）配下では
  stdout は非 TTY なので `docker logs` は素の `[R0 INFO]` を返す．ただしパーサは防御的に ANSI 除去
  （`re.sub(r'\x1b\[[0-9;]*m','',line)`）してから `^\[R(\d+) (\w+)\] (.*)$` で照合するのが安全．
- **収集経路**: 各 51 ノードがコンテナ名 `distributed-llm`（`--net=host`）で `pipeline_inference.py` を実行し，
  全ログはそのコンテナ stdout → `docker logs` に入る．`mise run logs`→`tools/show_logs.py --all`（`:58-77`）は
  各ホストへ `ssh_via_master`（ProxyJump: local→wafl-ctrl1→node，`common.py:424`）で
  `docker logs --tail 32 distributed-llm 2>&1` を叩き，標準出力へ表示するだけ（永続化・集計なし，tail 32 と少量）．
  rank↔host は hosts.txt の行番号＝rank（[0]=wafl-ctrl1=rank0，wafl100-139=rank1-40，wafl200-209=rank41-50，計 51）．
- `mise run predict:demo`→`tools/predict.py --prompt "Hello!"`（`:121-123`）は master 経由 `docker exec` で
  localhost:8082/predict に POST し，**戻り値は結果文字列のみ**（クライアント側にタイミング情報は無い）．
  → 定量指標は全て rank 0 コンテナの stdout 内にしか存在しない．
- 一般的な指標定義（Web，`onlinescientificresearch.com` の LLM 推論指標レビュー，MLPerf Inference 準拠）:
  TTFT / Generation Time / e2e latency / Inter-Token Latency(ITL) / Tokens Per Second(TPS) / RPS．
  MLPerf は TPS + ITL を主指標に採用．本リポジトリのログからは TTFT・ITL(p50/p95)・TPS・output/prompt tokens が算出可能．

**次フェーズ（計画）への示唆**

- **成功条件（predict:demo 後に results/Iter{n}.jsonl 生成）は rank 0 の docker logs だけで満たせる**．
  ステップ時間・tokens/sec の素材行は全て rank 0 が出すため，基本指標は 51 ノードのマージ不要．
  → 推奨アーキ（A）: 新規ツール `tools/collect_results.py`（仮）を追加し，predict:demo 実行後に
    ローカルから `ssh_via_master` で wafl-ctrl1 の `docker logs distributed-llm`（tail を大きく，または全量）を取得，
    パースして `results/Iter{n}.jsonl` へ 1 実行 1 レコードで追記する．**pipeline_inference.py（ホットパス）に触れず，
    クラスタ負荷ゼロ・完全に可逆・追加のみ**で config の制約に合致する．mise に `predict:demo` → 収集を繋ぐ薄い
    ラッパータスクを足せば success_criteria を満たせる．
  - 対案（B）in-process JSONL 書き出し（pipeline_inference.py が構造化レコードをファイル出力）は堅牢だが，
    ホットパス改変＋マウント先/再デプロイが必要で iter1 の可逆性・非介入方針に反する．iter1 では非推奨．
- **JSONL スキーマ案（1 実行＝1 レコード）**: `iter`, `run_id`, `timestamp`, `prompt`, `prompt_tokens`,
  `output_tokens`, `step_dt[]`（生配列）, `ttft_s`（=step0 dt）, `generation_time_s`（=Σstep_dt）,
  `tokens_per_sec`（=output_tokens / generation_time_s）, `itl_p50_s`, `itl_p95_s`, `decode_time_s`,
  `result_text`, `embed_stats{mean,std,min,max}`, および比較用に `levers{NUM_MICRO_BATCHES, STAGGER_INTERVAL, SEQ_LEN, WORLD_SIZE}`．
- **レバー値の記録は要検討（planner 判断）**: NUM_MICRO_BATCHES / STAGGER_INTERVAL / SEQ_LEN は既定ログに出ない．
  収集ツール実行時の環境変数/config から `levers` を埋めるのが最小変更．より堅牢にするなら pipeline_inference.py の
  起動時に「有効な実行設定を 1 行 INFO ログ出力」する小改修を別提案として検討（②以降のレバー比較で必須になる）．
- **注意点**: `docker logs` は既定でタイムスタンプ無し．実行区切りは上記 RESULT/prompt 行で判定するが，
  絶対時刻や連続実行の分離を堅くするなら収集側で `docker logs -t`（RFC3339 付与）を使うとよい．基本指標は
  埋め込み済み dt 値だけで足りるため必須ではない．

---

### 分析(実行) (Iter1)

**担当**: 分析(実行) subagent（2026-07-18）．`results/Iter1.jsonl`（フェーズ4「実験」で生成された 1 レコード）を
直接読み込み，全フィールドを構造化・機械集計した．config.yml の `analyze` タスク（`tools/show_logs.py --all`）は
ログ表示専用で JSONL 集計に対応していないため，今回は Python ワンライナーで直接読み込んだ（恒久的な集計スクリプト
の新規実装はしていない．②以降のレバー比較が必要になった実装フェーズで検討する）．

**レコード件数**: `results/Iter1.jsonl` は 1 行 = 1 レコード（`record_count=1`）．

**全フィールドの構造化要約（型・値）**

| フィールド | 型 | 値 |
|---|---|---|
| schema_version | int | 1 |
| iter | str | "Iter1" |
| run_id | str | "Iter1-20260718T084148Z-adc574" |
| timestamp | str (RFC3339) | "2026-07-18T08:41:48Z" |
| prompt | str | "Hello!" |
| prompt_tokens | int | 15 |
| output_tokens | int | 15 |
| step_dt | list[float], len=20 | [26.012, 7.015, 6.903, ..., 6.924]（先頭要素のみ突出して大きい．prefill 込みの step0） |
| ttft_s | float | 26.012 |
| generation_time_s | float | 158.397 |
| tokens_per_sec | float | 0.09469876323415216 |
| itl_p50_s | float | 6.964 |
| itl_p95_s | float | 7.0763 |
| decode_time_s | float | 0.0 |
| e2e_latency_s | float | 166.551021 |
| result_text | str | "Hello! How can I help you today?\nthought" |
| embed_stats.mean | float | 0.004217 |
| embed_stats.std | float | 1.108908 |
| embed_stats.min | float | -15.3125 |
| embed_stats.max | float | 16.875 |
| levers.NUM_MICRO_BATCHES | int | 4 |
| levers.STAGGER_INTERVAL | float | 3.0 |
| levers.SEQ_LEN | NoneType | null（既定ログに出ないため未設定．計画フェーズで既知の限界として明記済み） |
| levers.WORLD_SIZE | int | 51 |
| parse_ok | bool | true |
| parse_warnings | list[str], len=1 | ["no block's RESULT text matched the predict result prefix; used the latest block as a fallback"]（実験フェーズで原因特定済み・実害なし） |

**success_criteria①の機械的検証（フィールドの有無・型・非 null 性）**

config.yml の success_criteria①＝「`mise run predict:demo` 実行後に `results/Iter{n}.jsonl` がステップ時間・
tokens/sec を含む形で生成される」ことについて，以下を機械的にチェックした（判定根拠）．

- `step_dt` は非空の数値配列か: **true**（`isinstance(list) and len==20>0 and all(isinstance(x,(int,float)))`）
  → ステップ時間が構造化保存されている．
- `tokens_per_sec` は数値かつ non-null かつ正の値か: **true**（`isinstance(float)`, `0.0946... > 0`）
  → tokens/sec が構造化保存されている．
- `ttft_s` / `generation_time_s` / `itl_p50_s` / `itl_p95_s` / `decode_time_s` / `e2e_latency_s` はいずれも
  数値型かつ non-null か: **すべて true**（`decode_time_s=0.0` は「0 秒」という有効な数値であり欠測ではない）．
- `parse_ok` フィールド自体が `true` か: **true**．
- 計画フェーズで定義した確定版スキーマ（`schema_version`〜`parse_warnings` の 20 キー）に対する欠落キー: **無し**
  （`missing_keys=[]`），余剰キー: **無し**（`extra_keys=[]`）．
- `levers.SEQ_LEN` のみ `null`（既知の限界として計画フェーズで明記済み．success_criteria①の対象フィールド
  （ステップ時間・tokens/sec）には含まれないため，この null は success_criteria①の充足可否に影響しない）．

**判定: success_criteria① = pass**

上記の全チェックが true であり，`mise run predict:demo` 実行後に `results/Iter1.jsonl` がステップ時間
（`step_dt[]` 非空・20 要素）と tokens/sec（`tokens_per_sec=0.0947`，正の実測値）を含む形で生成されたことを
機械的に確認した．異常（欠損フィールド・型不一致・`parse_ok=false`）は検出されなかった．唯一の注意点は
`parse_warnings` に記録された 1 件のフォールバック警告だが，これは実験フェーズで原因（RESULT 行が複数行に
またがりレジェックスが先頭行のみ一致した）が特定済みで，`parse_ok=true`・全指標値が正常に埋まっていることから
今回の判定には影響しない．

**次フェーズ（分析(解釈)）への申し送り**

- 定量値の絶対水準（`tokens_per_sec≈0.095 tok/s`，`ttft_s≈26.0s`，`itl_p50_s≈6.96s`）が実用上妥当かどうかの
  評価・良否判定は本フェーズでは行っていない（analyst の担当）．
- baseline・比較対象イテレーションは現時点で存在しない（`config.yml` の `baselines: []`）ため，本レコードが
  当該レポジトリで最初に得られた定量データである点を解釈時の前提として扱うこと．
- `levers.SEQ_LEN=null` は基盤側の既知の限界（計画フェーズ・実装フェーズ双方で記録済み）であり，②以降の
  レバー比較で `SEQ_LEN` を対象にする場合は事前に対処が必要．

---

### 分析(解釈) (Iter1)

**担当**: 分析(解釈) subagent（2026-07-18）．フェーズ4「実験」とフェーズ5a「分析(実行)」の記録および
`results/Iter1.jsonl`（1 レコード）を読み，定量値の定性的妥当性・内部整合・既知の限界のリスクを解釈した．
実機への新規接続・実行はしていない（読み取りのみ）．

**前提（比較の枠組み）**: `config.yml` の `baselines: []`，過去イテレーション無し．本レコードは当該リポジトリ
初の定量データであり，**「レバー比較の有意差」は本イテレーションの判定対象にならない**（②以降で初めて意味を持つ）．
また **n=1** のため反復ばらつき（ノイズ幅）は未知で，本フェーズでは「基盤が信頼できる値を 1 回出せたか」＝
定性的妥当性・内部整合のみを評価する．

**1. 本イテレーションの成否判定（基盤の信頼性）**

- success_criteria①（基盤が機能し，ステップ時間・tokens/sec を含む JSONL を生成する）は 5a で pass 済み．
  本フェーズは加えて，得られた値が 51 ノードパイプライン並列推論の値として桁・分布とも妥当かを評価した．
- **内部整合（機械的に確認）**:
  - `sum(step_dt) = 158.397 = generation_time_s`（完全一致）．導出指標の定義どおり計算されている．
  - `ttft_s = step_dt[0] = 26.012`，`itl_p50_s = 6.964`（step1–19 の中央値），`itl_p95_s = 7.0763` いずれも
    生配列と整合．NaN/inf・負値・型崩れは無し．
  - `e2e_latency_s(166.55) − generation_time_s(158.40) = 8.15s`＝送信〜応答の壁時計オーバーヘッド．
    生成時間より外側にあり符号・大小関係が正しい（e2e ≥ generation）．
- **定性的妥当性（桁・分布）**:
  - **ITL ≈ 6.96s/token** は，1 トークンごとに 51 段パイプラインを逐次通過する構成では 6.964/51 ≈ **137ms/段**
    （計算＋通信）に相当し，商用単機 GPU（数十〜百 tok/s）とは桁が違うが，51 ホップ逐次通過が律速する
    分散構成の値として桁外れではない．
  - **TTFT ≈ 26.0s** は ITL の約 3.7 倍．15 トークンの prompt prefill ＋パイプライン充填（bubble fill）を
    step0 が含むことと整合し，突出値だが異常ではない．
  - **ITL 分布が極めて安定**（step1–19 が 6.873–7.097s，レンジ 0.224s，変動係数 ≈ 1%）．定常状態の
    パイプラインが安定に回っていることを示し，測定器としての再現性に好材料（ただし run 内の安定性であり，
    run 間ばらつきは別途要検証）．
  - **embed_stats**（mean≈0.004, std≈1.11, min=-15.3, max=16.9）は平均ほぼ 0・std≈1 の分布に外れ次元数個，
    という埋め込み統計として妥当な範囲で，NaN/異常スケールは無い．
- **判定: 基盤は測定器として信頼できる（採用相当）**．1 回の実測で桁外れ・異常値・型崩れ・内部矛盾のいずれも
  検出されず，指標が定義どおり整合して埋まった．success_criteria① は基盤要件として充足．

**2. 既知の限界（WARN・truncation）の評価とリスク**

- **WARN（複数行 RESULT のフォールバック照合）**: 今回は since ウィンドウ内にブロックが 1 個のみで，
  フォールバック（最新ブロック採用）の結果は正しく実害なし．ただし問題の本質は，**複数ブロックを取り違えないための
  防御的照合（`predict_result[:100]` とログ RESULT テキストの一致）が，RESULT が複数行にまたがると常に失敗し，
  設計上の弁別機構が事実上無効化されている**点にある．
  - ②のリスク評価: ②が各 run を**直列化し，run ごとに新しい `--since=run_start` で窓を切る**限り，窓内は
    当該 run の 1 ブロックのみで「最新ブロック」フォールバックは正しく，リスクは**中程度以下**．一方，
    同一 since 窓内に複数ブロックが残る運用（連続送信・窓の重なり・並行実行・秒未満の連投で since 粒度が
    足りない場合）では，弁別機構が働かず**別 run の指標を取り違えて特定レバー値に誤って紐付ける**可能性があり，
    これはレバー比較の**正しさ（correctness）を直接損なう高リスク**になる．
  - 結論: ②着手前に「RESULT 正規表現を複数行対応にして弁別機構を実際に機能させる」か，最低限「run を厳密直列化＋
    run 単位の狭い since 窓」を運用規約として保証すること．前者を推奨（フォールバック頼みは②で破綻し得る）．
- **loop-detection truncation による output_tokens の不安定性**: `output_tokens=15` に対し `step_dt` は 20 要素．
  推論エンジンの繰り返しパターン検出（`pattern loop detected ... keeping 15 tokens`）で生成トークンが切り詰められた
  結果，**分子=15 トークン・分母=20 ステップ分の時間(158.4s)** となり，`tokens_per_sec=0.0947` はやや過小
  （20 ステップで数えれば 0.126）．レバーに依存しない truncation がスループット指標に混入するため，②で
  `tokens_per_sec` をレバー感度の主指標にすると**レバー効果と truncation ノイズが交絡**する．

**3. 次イテレーション（②以降）への示唆**

- **反復回数（最優先）**: 現状 n=1 でノイズ幅が全く未知．②のスループット感度分析は，各レバー値につき
  **最低 3〜5 回**の反復で run 間標準偏差を先に確立しないと，見かけの増減がノイズか有意かを判定できない
  （success_criteria② の「有意差」判定の前提）．今回の run 内 ITL 安定（CV≈1%）は run 間安定を保証しない．
- **主指標の選択**: 上記 truncation 交絡を避けるため，②では truncation の影響を受けにくい **ITL(p50/p95)** と
  **TTFT** をレバー感度の主指標に据え，`tokens_per_sec` は補助として扱うことを推奨．あるいは
  max_new_tokens 固定＋繰り返しにくい安定プロンプトで output_tokens を揃える．
- **levers 記録の堅牢化（②では必須）**: 今回 `levers` は収集ツール実行時の env/config 由来（`SEQ_LEN=null`，
  他はフォールバック値 `NUM_MICRO_BATCHES=4`/`STAGGER_INTERVAL=3.0`/`WORLD_SIZE=51`）．②は
  `NUM_MICRO_BATCHES`・`STAGGER_INTERVAL` を実際に振るため，**「コンテナ起動時 env と収集時 env の一致」仮定が
  崩れると記録レバーが実レバーと食い違う**．計画フェーズ提案 P1（`pipeline_inference.py` 起動時に有効設定を
  1 行 INFO 出力→収集側がログから確定）を②着手前に実装するか，最低限 env 一致を毎 run 検証すること．これは
  ②の妥当性に直結する．
- **WARN 対処**: 上記 2 の RESULT 複数行照合を②前に解消する．

**次フェーズ（考察・次計画 reflector）への結論（採用/棄却の材料）**

- **①（結果永続化基盤）は採用が妥当**: 基盤は測定器として機能し，1 回の実測で桁外れ・異常値・内部矛盾は無く，
  指標が定義どおり整合して埋まった．success_criteria① 充足．
- ただし②へ進む前提として，(a) RESULT 複数行照合の修正（または直列化＋狭い since 窓の運用規約化），
  (b) levers 記録の堅牢化（P1 実装 or env 一致検証），(c) レバー値あたり n≥3 反復でノイズ幅確立，
  (d) truncation に強い主指標（ITL/TTFT）採用，の 4 点を条件として申し送る．
- 本イテレーションは基盤構築であり**レバー比較の有意差判定は対象外**．追加反復の要否は「基盤の合否」に対しては
  不要（1 回で成否は確定）だが，「②の定量比較の土台」としては上記 (c) が必須．

---

### 考察・次計画 (Iter1)

**担当**: 考察・次計画 subagent（2026-07-18）．全フェーズ（計画・実装・実験・調査・分析(実行)・分析(解釈)）の
記録と `results/Iter1.jsonl`（1 レコード），および前フェーズ analyst の結論を読み，Iteration 1 の採否を確定し
次イテレーションの方向を決めた．実機への新規接続・実行はしていない（記録の確定と git 操作のみ）．

**1. 採否判定: 採用（adopt）**

単一レバー「結果永続化基盤の新規実装」（`tools/collect_results.py`．`pipeline_inference.py`/`predict.py` は非改変）を
**採用**とする．判定根拠は以下．

- success_criteria①（`mise run predict:demo` 実行後に `results/Iter{n}.jsonl` がステップ時間・tokens/sec を
  含む形で生成される）を，実機 51 ノードでの 1 実行で **達成**（分析(実行)で機械検証済み，pass）．
- 得られた 1 レコードは内部整合が取れており（`sum(step_dt)=158.397=generation_time_s`，`ttft_s=step_dt[0]`，
  `e2e_latency_s ≥ generation_time_s`），桁・分布とも 51 段パイプライン並列推論の値として妥当（ITL≈6.96s/token，
  TTFT≈26.0s，embed_stats mean≈0・std≈1.11）．NaN/inf・負値・型崩れ・欠落キー・余剰キーはいずれも無し．
  → 基盤は「測定器」として信頼でき，②以降のレバー比較の土台として採用する．
- なお本イテレーションは**基盤構築（単発）であり，レバー掃引ではない**ため「収束」判定の対象外．「棄却」に相当する
  欠陥（基盤が指標を出せない・値が信用できない）は検出されなかった．採否は「採用」で確定する．

**2. 次に振るレバーの決定（可逆・自動判断）: ② へ直行せず，先に基盤の頑健化を 1 イテレーション挟む**

分析(解釈)が②着手前の条件として挙げた 4 点のうち，(a) RESULT 複数行照合の修正が「レバー比較の正しさ
（correctness）を直接損なう高リスク」と評価されている．②（NUM_MICRO_BATCHES / STAGGER_INTERVAL のスループット
感度分析）では複数 run を同一コンテナへ連続送信するため，RESULT が複数行にまたがると防御的照合（`predict_result[:100]`
とログ RESULT テキストの一致）が常に失敗し，**別 run の指標を誤ったレバー値へ紐付ける**可能性が残る．基盤が
信用できない状態でレバー掃引をしても結論が汚染されるため，②へ直行しない．

- **Iteration 2（決定）＝単一レバー「RESULT 複数行対応による照合ロジックの頑健化」**（`collect_results.py` のみ．
  `_RESULT_RE`／`_extract_result_text`／`_select_relevant_block` を複数行 RESULT に対応させ，弁別機構を実際に
  機能させる）．`collect_results.py` 内に閉じ・非侵襲・可逆でクラスタ負荷ゼロ，かつ①と同じ「基盤の信頼性」レバーの
  延長であり，単一レバー原則に整合する．コードレベル（フィクスチャに複数行 RESULT ケースを追加した単体テスト）で
  完了条件を組み，end-to-end 検証が要る場合のみ人間確認の上で実機を 1 回叩く．
- **Iteration 3（方針・後続）＝計画フェーズ提案 P1「levers 記録の堅牢化」**（`pipeline_inference.py` 起動時に有効設定を
  1 行 INFO 出力→収集側がログから levers を確定）．これは**ホットパス改変・再デプロイを伴う別種の変更**であり，
  単一レバー原則上 Iteration 2 と混ぜず独立イテレーションとして扱う．②で `NUM_MICRO_BATCHES`／`STAGGER_INTERVAL` を
  実際に振る前に，記録レバーと実レバーの食い違いを防ぐため必要．
- **② 実施時の実験設計条件（config.success_criteria②の前提として申し送り）**: (c) 各レバー値につき n≥3〜5 反復で
  run 間標準偏差を先に確立してから有意差を判定，(d) loop-detection truncation の交絡を避けるため主指標を
  **ITL(p50/p95)・TTFT** に置き `tokens_per_sec` は補助扱い（または max_new_tokens 固定＋安定プロンプトで
  output_tokens を揃える）．

この決定は**可逆**（掃引に入る前の頑健化順序の選択）であり，不可逆・破壊的要素を含まないため自動判断とし，
`backlog.md` の B3 に `[auto-decided]` として選択・根拠・要レビューを記録した（②へ直行すべきという別判断の余地は
残るため要レビュー）．

**3. 学び（次の自分向け）**

- **防御的フォールバックは「弁別機構が壊れていること」を隠す**: Iter1 の since 窓内にブロックが 1 個しか無かったため
  「最新ブロック採用」フォールバックが正解になり `parse_ok=true` で通ったが，これは照合機構が機能した結果ではなく
  「選択肢が 1 個だった」偶然に過ぎない．フォールバックが常時発火する状態（複数行 RESULT）は，複数ブロックが
  並ぶ②で初めて実害（取り違え）として顕在化する．「今回実害なし」を「問題なし」と読み替えないこと．
- **truncation はレバー非依存のノイズとしてスループット指標に混入する**: `output_tokens=15` / `step_dt` 20 要素の
  食い違い（loop-detection truncation）で `tokens_per_sec` が過小に出た．レバー効果と交絡するため②の主指標には
  truncation に強い ITL/TTFT を使う．
- **`.gitignore` の `*.log` がテストフィクスチャを飲み込む落とし穴**: 回帰テスト入力の `tests/fixtures/rank0_sample.log`
  が `*.log` ルールで無視され，通常 add ではコミットされない（＝新規チェックアウトでテストが再現不能になる）．
  今回は `git add -f` で明示追跡した．次イテレーションでフィクスチャを増やす際も同ルールに注意する（別案として
  フィクスチャ拡張子を `.log` 以外にする改修余地あり＝要レビュー項目）．

---

## 現在の状態（初回セットアップ・2026-07-18 時点）

**このリポジトリには結果永続化基盤が存在しない．** 実験結果の観測手段は stdout ログ
（`[R{rank} LEVEL] message` 形式）のみで，`mise run logs` が各ノードの docker logs を tail するだけである．
JSONL 等への構造化保存・自動集計の仕組みは無い（README/mise.toml 調査で確認済み）．

**過去の作業実績**（git log 3 コミット，コミット時点の作業内容）:
- プロジェクト開始（パイプライン推論エンジンの実装）
- Gemma4 モデル設定・レイヤー処理対応
- コード変更による機能強化・性能改善

**このリポジトリでの研究サイクルの出発点**: config.yml の research_frontier①（結果永続化基盤の実装）が
最初のイテレーションの主対象になる見込み．レバー比較（マイクロバッチ数・stagger interval 等）は
基盤ができてから初めて定量的に意味を持つ．
