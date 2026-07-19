# 実験ジャーナル: distributed-llm

research-cycle が読み書きする実験ジャーナル．**新しいイテレーションを常に先頭へ挿入する（逆時系列）**．
1 イテレーション = 単一レバー変更．各ブロックに仮説・単一レバー・成功条件（planner 記入）と，
変更・結果・判定・学び（reflector 記入）をまとめる．

---

## Iteration 8

### 考察・次計画 (Iter8)

**担当**: 考察・次計画 subagent（rc-reflector，2026-07-20 JST）．`### 分析(解釈) (Iter8)` の判定（Decision1=(1b) が
約 129σ で確定・ノイズ余地なし，示唆 (A)(B)(C)）を受け，Iter8 の単一レバー（pipeline_fill_microbench 診断）の採否と
Iteration 9 の方向を reflector として確定した．実機非接続（journal・`results/Iter8.jsonl` の読み取りと commit 操作のみ，
`pipeline_inference.py` 非改変）．**逆時系列維持のため本ブロックを Iteration 8 内の最上段に置く**．

**1. 採否判定: 採用（診断として結論確定）＝この診断レバーは収束（accepted-as-diagnostic / converged）**

- 本イテレーションのレバーは「レバーが効いたか」を測る感度実験ではなく，「実機の sequential 化がどこ由来か」を切り分ける
  **診断実験**である（タスク指示の注記どおり）．その診断課題に対して **明確な結論が出た**ため「採用（診断として成功）」
  と判定する．具体的には Decision1（blocking×sleep，N=16,M=32,repeat=5）で **FF=0.9716（≥0.7 の (1b)）**，閾値 0.7 まで
  約 129σ 離れており n=5 でも反転余地皆無．計画 §4 の (1b) 分岐に従い Decision2（async）は実行せず（構造起因でないため
  不要），この判断は正しかった．
- **診断としての収束**: Decision1=(1b) の結論（blocking `recv→compute→send` 構造は，段が真並列なら本来ほぼ完全に fill
  する）は 129σ で頑健であり，**この結論の確認のための追加ローカル反復は不要**（analyst §4 追加反復要否と一致）．
  したがってこの診断レバーは収束させ，次は未解決点（実機の直列化点そのものの特定）へレバーを移す．

**2. 非自明な学び（次の自分向け）**

- **(i) Iter7 考察の修正（今回の最重要の学び）**: Iter7 は「本 bench の `time_per_step ∝ m` は blocking 逐次構造
  （段間オーバーラップ欠如）そのものが原因」と読んでいた（Iter7 §2-i）．Iter8 はこれを切り分け直し，**「blocking でも
  段が真並列なら fill する（FF=0.97）→ 実機の不 fill は blocking 通信構造では説明できず，別の（大域的な）同期点由来」**
  と修正した．矛盾ではなく診断の解像度が上がった結果．sleep proxy は「段が別マシンで真並列」という実機条件を
  machine-count 非依存に模す主信号（計画 §6 の射程内）であり，その条件下で fill が成立した事実がこの修正の根拠．
- **(ii) matmul proxy の FF 急落（0.97→0.30）は実機の真因の説明にはならない（傍証にはなる）**: 同一 blocking 構造でも
  compute を実演算にすると FF が 0.30 へ落ちるのは F3（CPU/Gloo で comm と compute が同一コアを食い合う）の実測的
  裏付けだが，**レジームが違いすぎる**——local matmul の 0.30 は「部分劣化」にすぎず，Iter7 実機の含意 FF は m=51 で
  0.038・m=204 で 0.024 と **1/p≈0.0196 にほぼ張り付く＝ほぼ完全直列**．ソフトな資源競合では届かない水準で，むしろ
  「実機の不 fill はハードな同期点（rank0 の microbatch 生成直列・`_reset_kv_cache_for_bench` 同期・どこかの barrier）
  由来」という §1 の解釈を補強する consistent な傍証．また「local で matmul proxy を採ると実機を模せない」という
  方法論上の caveat としても記録する．
- **(iii) async 二重バッファ大改修（B14(b)/F2 overlap 軸）の事前確度は下がった**: 段が真並列なら blocking でも fill する
  以上，async が埋めるべき「構造的な穴」は sleep proxy の射程では見当たらない（fill 回収目的の余地が薄い）．加えて
  実機の不 fill はハードな同期点由来（async の `isend`/`irecv` は barrier や rank0 直列生成を解消しない）で，通信隠蔽
  として見た利得上限も compute 律速 92%（F1）ゆえ数 %．**fill 回収・通信隠蔽のどちらの観点でも async 軸の期待値は
  低下**し，現時点の証拠で async ホットパス大改修へ進むのは非推奨（analyst 示唆 (C)）．ただし本ローカル bench は
  実機の同期点そのものを特定できない（計画 §6 の射程外）ため，async を正式に棄却する前に (iv) の実機 timing 診断で
  直列化点を確定させる（棄却の前に一次証拠を取る）．
- **(iv) 記録上の軽微な齟齬（結論に影響なし）**: analyst(実行) が，実験ブロックの標準偏差ラベル「母標準偏差」が実際には
  標本標準偏差（n-1）の値だったという表記齟齬を検出した．平均値・FF 判定・129σ の結論には一切影響しない（n=5 では
  どちらの分母でも「非常に小さいばらつき」の定性判断は不変）．次回以降ラベルと計算式を一致させること．

**3. Iteration 9 の方向決定: 示唆 (A)＝実機 bench への per-microbatch timing ログ追加で直列化点を特定（B15 に自動記録）**

- **決定**: 次イテレーションは **実機 51 ノード bench 経路（`_run_microbatch_bench`/`_process_microbatch`）への
  per-microbatch timing ログ追加**で，Iter7 の `time_per_step ∝ m`（ほぼ完全直列）を生む実際の直列化点
  （rank0 の microbatch 生成直列・`_reset_kv_cache_for_bench` 同期・barrier 等の候補）を特定する軸を採る．
  これが最も情報利得が高く，かつ計画・実装フェーズはコードのみで可逆（analyst 示唆 (A)）．
- **可逆性の判断（自律判断ポリシーとの照合）**: 追加するのは bench 経路への**加算的な計測 INFO ログ**であり，
  既存の serving/relay ロジックも計算結果も変えない（読み取り専用の計測，Iter3/P1 の per-request INFO ログ追加と同種で
  graph-break リスクは低い）．コード変更自体は可逆．測定に要する実機 deploy/predict は **B7 の包括承認（非破壊 SSH/
  deploy）の範囲内**で破壊的操作を含まない．したがって **Iteration 9 の方向選定は可逆＝自動判断とし，B15 に記録**する
  （調査・計画・実装はコードのみで進め，実験フェーズの deploy も B7 の範囲内）．
- **B9（B3 本体＝relay プロトコル改修＝SL3）との衝突確認**: 本軸は bench 経路への読み取り専用計測であり，relay
  プロトコル（トークン投機・K トークン運搬）には一切触れない．B9 とは軸が直交し実装衝突もない．**B9 は今回も温存
  （`[needs-human]` 維持，reflector では自動判定しない）**でよい．
- **フォールバック**: (A) の実装が過大（bench 経路の計測が予想外に serving 経路へ波及する等）と判明した場合は，示唆 (B)
  「重み int8 dynamic quantization を SL1 型 local マイクロベンチで作る前に測る」（compute 92% を直接攻める・可逆）へ
  振り替える．config `levers`（`STAGGER_INTERVAL`/`SEQ_LEN`/`WORLD_SIZE`）は B14(a) 同様さらに下位のフォールバックとして
  温存．async ホットパス大改修（B14(b)）は (A) で実機の直列化点が判明し，かつそれが async で解消可能と分かるまで着手
  しない（不可逆・大規模のため，着手が妥当と判明した時点で改めて `[needs-human]` 登録＋Slack 確認）．

**4. 要人間判断の有無**

- 本フェーズで新規の要人間判断（不可逆・破壊的判断）は発生していない．Iteration 9 の方向（実機 timing ログ追加）は
  可逆のため自動判断（B15）とした．B9 は従来どおり人間回答待ちで温存する．

---

### 実験 (Iter8)

**担当**: 実験フェーズ subagent（2026-07-20T02:07〜02:08 JST，約 1 分）．`### 実装 (Iter8)` §4 の手順に従い，
完全にローカル（`torch.multiprocessing.spawn`，Gloo backend，localhost）で `scripts/pipeline_fill_microbench.py`
を実行した．**51 ノード実機クラスタへは一切接続していない**（SSH・`mise run deploy`・`mise run predict:demo` は
未使用）．事前に `unset VIRTUAL_ENV && uv run pytest tests/` で **116 passed**（既存 93＋新規 23，回帰なし）を確認済み．

**1. Decision 1（blocking・sleep proxy・N=16, M=32, repeat=5）**

```
unset VIRTUAL_ENV && uv run python scripts/pipeline_fill_microbench.py \
    --variant blocking --proxy sleep --num-stages 16 --num-microbatches 32 --repeat 5
```

- 実行時間: 5.3 秒（16 プロセス起動込み）．
- rank0 実測 `t_stage_s`（sleep proxy の実測較正値）= 0.006064s（指定 `t_stage` 既定 0.006s とほぼ一致）．
- `total_time_s`（5 repeat）= {0.2930, 0.2926, 0.2938, 0.2931, 0.2941}，平均 0.29334s，母標準偏差 0.000633s（CV≈0.22%）．
- `fill_factor`（スクリプト算出値，そのまま採用）= {0.9727, 0.9740, 0.9699, 0.9723, 0.9689}，平均 **0.9716**，
  母標準偏差 0.0021（2σ≈0.0042，非常に小さい）．
- **検算**: `ideal_pipelined_time_s = (M+N-1)*t_stage = (32+16-1)*0.006064 = 0.285008s`．
  `FF = 0.285008 / 0.29334 ≈ 0.9714`（スクリプト算出値と一致，検算成功）．参考: 完全 sequential なら
  `M*N*t_stage = 32*16*0.006064 = 3.1048s` となるはずだが，実測 0.293s はこれよりずっと `ideal_pipelined_time_s`
  に近い．
- **Decision 1 判定材料**: `FF=0.9716 ≥ 0.7` の閾値 **(1b)** に該当する（2σ 込みでも 0.7 を大きく上回り疑義なし）．
  タスク指示の分岐規則に従い，**Decision 2（async variant）は実行しない**（構造起因ではないため）．

**2. Decision 2: 実行せず（Decision 1 が FF≥0.7 のため，タスク指示どおりスキップ）**

**3. 補足（時間に余裕があったため実施）: matmul proxy（blocking・N=16, M=32, repeat=5, F3 補足観察）**

```
unset VIRTUAL_ENV && uv run python scripts/pipeline_fill_microbench.py \
    --variant blocking --proxy matmul --num-stages 16 --num-microbatches 32 --repeat 5
```

- 実行時間: 8.6 秒．rank0 実測 `t_stage_s`（matmul 1 回・5376×5376 float32 GEMV/GEMM の実測較正値）= 0.005935s
  （sleep proxy の指定値とほぼ同水準）．
- `total_time_s`（5 repeat）= {0.9481, 0.9263, 0.9516, 0.9147, 0.9292}，平均 0.9340s，母標準偏差 0.0155s（CV≈1.7%）．
- `fill_factor` = {0.2942, 0.3012, 0.2931, 0.3050, 0.3002}，平均 **0.2987**，母標準偏差 0.0050（2σ≈0.0099）．
- **観察（事実のみ，評価は analyst に委ねる）**: 同じ blocking 構造・同じ N=16, M=32 でも，compute proxy を
  sleep から matmul（実演算，16 プロセスが同一マシンの CPU コアを奪い合う）に替えると FF が 0.97→0.30 へ
  大幅に低下した．これは journal 記載の F3（CPU/Gloo では通信も計算も同一コアを食い合う）と整合する事実として
  記録する．なお本ローカル環境では N=16 プロセスに対し実コア数が多い（後述）ため，この低下は「コア不足による
  順番待ち」よりも「16 プロセス同時実行時の CPU 競合（OS スケジューリング・メモリ帯域・BLAS スレッド競合等）」
  由来である可能性が高いが，原因の切り分けは本実験のスコープ外（判定は sleep proxy が主，matmul は補足）．

**4. 環境情報（再現性のため記録）**

- 実行マシンの論理コア数: 64（`nproc`）．N=16 プロセスに対し十分な余裕があり，sleep proxy の測定は
  「CPU コア不足による見かけの sequential 化」を排除できている．
- `results/Iter8.jsonl` は本フェーズで新規作成（既存ファイルなし）．計 10 レコード
  （blocking×sleep×5 + blocking×matmul×5，いずれも `record_type="pipeline_fill_microbench"`，
  `schema_version=1`）．

**5. 完了条件（`### 検討・計画 (Iter8)` §5）との対比**

- (i) 済（blocking/async・sleep/matmul を CLI で選択可，スクリプトは変更なし）．
- (ii) 一部達成: blocking×sleep（Decision 1 判定点，repeat=5）・blocking×matmul（補足，repeat=5）の 2 設定を
  `results/Iter8.jsonl` へ構造化保存した．Decision 1 が FF≥0.7（1b）と判定されたため，タスク指示に従い
  async variant（Decision 2 用の設定）は実行していない．
- (iii) 済（作業開始前に 116 passed を確認．本フェーズ中はコード変更なし）．
- (iv) 済（`pipeline_inference.py`・serving 経路・51 ノードクラスタ非接触．ローカル `torch.multiprocessing.spawn`
  のみで完結）．

**6. 分析フェーズへの申し送り**

- Decision 1 は明確に **(1b)**（`FF=0.9716 ≥ 0.7`，2σ 込みでも疑義なし）．計画 §4 の (1b) 分岐に従えば，
  「blocking 構造そのものは本来 fill する」＝Iter7 の実機での sequential 化（`time_per_step ∝ m`）は
  **blocking recv→compute→send 構造由来ではなく，別の同期点由来**という解釈が示唆される．ただし本ベンチは
  計画 §6 の留保どおり「単一マシン上のプロトコル構造検証」であり，「実機 51 ノードで実際に何が sequential 化の
  原因か」を直接特定するものではない（それは計画 §4 (1b) が推奨する「実機 bench への per-microbatch timing
  ログ追加」という次の診断ステップの役割）．
- 補足の matmul proxy 観察（FF 0.97→0.30）は，「sleep proxy で fill が成立すること」自体を覆すものではないが，
  「CPU/Gloo 上で実演算を伴うと fill が大きく劣化しうる」という F3 の実測的裏付けとして analyst が参照できる．
- git commit/push はこのフェーズでは行っていない．

---

### 分析(実行) (Iter8)

**担当**: 分析(実行) フェーズ subagent（2026-07-20 JST）．`results/Iter8.jsonl` の生データ 10 レコード
（`record_type="pipeline_fill_microbench"`，`schema_version=1`）を Python（`statistics` モジュール）で独立に
再集計し，`### 実験 (Iter8)` 記載値との一致を検算した．実機クラスタへは接続していない．新規実験も実行していない
（既存ファイルの再集計のみ）．

**1. `tools/show_logs.py --all`（config.yml `analyze` タスク）について**

- 冒頭を確認したところ，`RANK=0 uv run python tools/show_logs.py` / `--all` は `read_hosts` で
  `hosts_file` を読み取り，`ssh` で master → worker ノードへ接続して `docker logs --tail 100 -f distributed-llm`
  を実行する，**実機 51 ノードクラスタ専用の docker ログ tail ツール**であることが分かった．`results/Iter8.jsonl`
  はローカル `torch.multiprocessing.spawn` 実行で生成された JSONL であり，このツールが読む対象（コンテナログ）
  とは形式・取得経路とも一致しない．実機への SSH 接続が必要になるため，タスク指示（実機接続禁止）に従い
  **実行しなかった**．代わりに Python での直接集計を行った．

**2. 再集計結果（(variant, proxy) 別，母集団標準偏差 `statistics.pstdev` を主指標として使用）**

| variant | proxy | n(repeat) | total_time_s 平均 | total_time_s pstdev (CV) | fill_factor 平均 | fill_factor pstdev (2σ) |
|---|---|---|---|---|---|---|
| blocking | sleep | 5 | 0.2933362 s | 0.0005660 s (0.193%) | **0.9715647** | 0.0018740 (0.003748) |
| blocking | matmul | 5 | 0.9339658 s | 0.0138963 s (1.488%) | **0.2987368** | 0.0044428 (0.008886) |

- 集計対象: `results/Iter8.jsonl` 全 10 レコードすべてが `record_type="pipeline_fill_microbench"` であり，
  (variant, proxy) の組は blocking×sleep（5 レコード）・blocking×matmul（5 レコード）の 2 組のみ
  （async・sink proxy 等の他の組み合わせは存在しない．`### 実験 (Iter8)` の記載どおり Decision 2 未実行のため）．
- 各レコードについて `fill_factor = (num_microbatches + num_stages - 1) * t_stage_s / total_time_s` を
  レコード自身の `t_stage_s`（rank0 実測較正値）から再計算し，保存済み `fill_factor` と全 10 レコードで
  完全一致（誤差 < 1e-9）を確認した．スクリプトの FF 算出ロジック自体に矛盾は無い．

**3. `### 実験 (Iter8)` 記載値との一致確認**

- **fill_factor 平均**: blocking×sleep = 0.9715647 → 記載値「0.9716」と**一致**（四捨五入誤差の範囲内）．
  blocking×matmul = 0.2987368 → 記載値「0.2987」と**一致**．→ **本フェーズが最も重視する Decision 1 判定の根拠数値
  （FF=0.9716 ≥ 0.7）は生データからの独立検算でも再現された．**
- **total_time_s 平均**: sleep 0.29334s・matmul 0.9340s も記載値と一致．
- **標準偏差の表記に軽微な不整合を発見**: 記載では「母標準偏差」（population stdev, 分母 n）と明記されているが，
  実際の数値は**標本標準偏差**（sample stdev, 分母 n-1，`statistics.stdev`）と一致し，母標準偏差
  （`statistics.pstdev`，分母 n）とは一致しない．具体的には，sleep の total_time_s は記載「0.000633」＝標本標準偏差
  0.0006329（母標準偏差は 0.0005660），sleep の fill_factor は記載「0.0021」＝標本標準偏差 0.002095（母標準偏差は
  0.0018740），matmul の total_time_s は記載「0.0155」＝標本標準偏差 0.015537（母標準偏差は 0.0138963），matmul の
  fill_factor は記載「0.0050」＝標本標準偏差 0.004967（母標準偏差は 0.0044428）．CV（変動係数）も同じ標本標準偏差
  ベースで計算されている（例: sleep の CV「≈0.22%」は 0.000633/0.29334，母標準偏差ベースでは 0.193%）．
  **平均値・FF 判定結論には影響しない**（n=5 と小さい repeat 数のため，どちらの分母を使っても「非常に小さいばらつき」
  という定性的判断は変わらない）が，ラベルと計算式の不一致という記録上の事実として指摘する．

**4. まとめ（事実のみ，評価は行わない）**

- 平均値（total_time_s・fill_factor）は完全一致，FF 算出ロジックの内部整合性も確認済み．
- 標準偏差の「母標準偏差」ラベルは，実際には標本標準偏差（n-1）の値であるという表記上の齟齬が見つかった
  （数値の計算自体は誤りではなく，どちらの分母を使ったかのラベル付けの問題）．
- `tools/show_logs.py --all` は実機専用のため今回は未実行．

---

### 分析(解釈) (Iter8)

**担当**: 分析(解釈)フェーズ subagent（2026-07-20 JST）．`### 実験 (Iter8)`・`### 分析(実行) (Iter8)` の確定数値
（blocking×sleep FF=0.9716，blocking×matmul FF=0.2987）を，`### 検討・計画 (Iter8)` の Decision1 分岐規則・Iter7 の
発見（`time_per_step ∝ m`）・調査 F1〜F6 と突き合わせて意味づけた．実機非接続・新規実験なし（既存数値の再集計と
journal・コード読解のみ）．最終レバー決定は reflector の役割のため，本節は「ノイズ/有意の判定」「機構の解釈」
「reflector への示唆」に留める．

**1. ノイズか有意かの判定（結論: いずれも有意，ノイズの余地なし）**

- blocking×sleep: FF=0.9716，標本 sd=0.0021（2σ≈0.0042，CV≈0.2%）．閾値 0.7 まで **約 129σ** 上方に離れており，
  n=5 の小標本でも判定が反転する余地は皆無．**Decision1=(1b)（FF≥0.7）は確定**．
- blocking×matmul: FF=0.2987，標本 sd=0.0050．sleep の FF とは 0.67 の差（sleep 側 2σ の約 160 倍）で完全に分離し，
  0.7 閾値からも約 80σ 下方．sleep→matmul の FF 低下（0.97→0.30）は明確な有意差でノイズではない．
- 両者とも過去反復（Iter7 の CV≈0.07〜0.14%，Iter8 の CV≈0.2〜1.5%）と同水準の低ノイズであり，見かけの増減を
  ノイズと見誤る状況ではない．

**2. Decision1=(1b) が Iter8 の当初仮説に対して意味すること（矛盾なく解釈可能・確信度高）**

- 計画の分岐規則は「(1a) FF≤0.3 なら blocking 構造そのものが fill を潰す→async 化に価値，(1b) FF≥0.7 なら別の同期点
  由来」であった．今回は (1b)．すなわち **blocking `recv→compute→send` というプロトコル構造は，段が真に並列
  （sleep proxy＝各段が別コアで同時進行，64 コア>16 プロセスで確認済み）なら本来ほぼ完全に fill する**（FF=0.97）．
- したがって **Iter7 実機で観測された `time_per_step ∝ m`（＝段間 fill 不成立）は，blocking recv/send という通信構造
  そのものが原因ではない**，と強く示唆される．sleep proxy は「段が別マシンで真に並列」という実機 51 ノードの条件を
  machine-count 非依存に模す主信号（計画 §6 の射程）であり，その条件下で fill が成立する以上，実機の sequential 化は
  blocking 構造では説明できず，**別の（大域的な）同期点由来**と解釈するのが整合的．
- これは Iter7 の考察（§2-i「段間オーバーラップが構造的に欠如」＝blocking 逐次ループが原因）に対する**修正**である．
  Iter7 は「blocking だから fill しない」と読んでいたが，Iter8 は「blocking でも（真並列なら）fill する→実機の不 fill は
  別要因」と切り分けた．矛盾ではなく，診断の解像度が上がった結果と位置づける．

**3. matmul proxy の FF 急落（0.97→0.30）が Iter7 実機の真因である可能性（評価: 低〜中，直接の説明ではない）**

- matmul の FF=0.30 は「同一マシン上に 16 プロセスを co-locate し実演算させると，CPU コア/メモリ帯域/BLAS スレッドの
  資源競合（F3）で fill が部分的に崩れる」ことの独立した実測的裏付けである．これ自体は確かな発見．
- ただし **Iter7 実機の真因としては条件が一致しない**：
  - (a) **レジームが違う**．local matmul の FF=0.30 は「部分劣化」にすぎない（N=16 の完全 sequential は FF≈1/16=0.0625）．
    一方 Iter7 実機の含意 FF は m=51 で **0.038**，m=204 で **0.024** と **1/p=0.0196 にほぼ張り付く＝ほぼ完全 sequential**．
    Iter7 の方が桁で深刻で，soft な資源競合（matmul の部分劣化）では届かない水準．**「ほぼ完全な直列化」はハードな
    同期点（例: rank0 の microbatch 生成の直列，`_reset_kv_cache_for_bench` 同期，どこかの `barrier`）の署名**に近く，
    資源競合の署名とは異なる．
  - (b) **競合の場が違う**．local の競合は「段を同一マシンに co-locate したこと」由来の人工物で，実機 51 ノードでは各段が
    物理的に別 CPU にあり，段間の compute 競合は起きない（node 内の Gloo comm↔compute 競合＝F3 は残るが，これは
    within-stage であって段間 fill を full-sequential まで潰す機序ではない）．
  - 以上より，matmul 観察は **「本ローカル bench で matmul proxy を採ると実機を模せない」という方法論上の caveat** としては
    重要だが，**Iter7 の m 比例の主因の説明にはならない**．むしろ (a) の FF レジーム差が「実機の不 fill はハードな同期点」
    という §2 の (1b) 解釈をさらに補強する（competing explanation ではなく，consistent な傍証）．

**4. research_frontier⑤／B14(b)（async 二重バッファ化が本命）への影響（示唆・確信度中）**

- 今回の結果は **「async 化そのもの」の価値を否定するものではなく，攻めどころ（ボトルネックの所在）の理解を修正する**もの．
  修正後の見立ては次のとおりで，いずれも B14(b)（`_process_microbatch` の async 化＝不可逆・大規模ホットパス改変）に
  着手する事前確度を**下げる**方向に働く：
  - (i) **fill 回収目的での async の余地が薄い**：段が真並列なら blocking でも fill する（FF=0.97）以上，実機で「段間 fill が
    構造的に欠けている」わけではない．async 二重バッファが埋めるべき「構造的な穴」は sleep proxy の範囲では見当たらない．
  - (ii) **実機の不 fill はハードな同期点由来（§2・§3a）**．async `isend`/`irecv` は barrier や rank0 直列生成のような
    大域同期点を解消しない．誤った処方箋に大改修コストを払うリスクがある（計画 §4 (1b) の警告と一致）．
  - (iii) 調査 F1 の既知事実（実機 ITL は compute≈92%・send≈0.3%）を重ねると，async を**通信隠蔽**として見た利得上限も
    数 % で低い．fill 回収（F2）・通信隠蔽（F1）のどちらの観点でも async 軸の期待値は低下した．
- **reflector への示唆（決定は委ねる）**：
  - (A) 証拠は「async 軸は no-go／低価値へ収束」方向を指すが，本ローカル bench は**実機の同期点そのものを特定できない**
    （計画 §6 の射程外）．したがって async を棄却する前に，**実機 bench への per-microbatch timing ログ追加（軽量・可逆・
    ホットパス非改変）で実際の直列化点を特定する**のが次の自然な診断（計画 §4 (1b) が推奨した経路）．これが最も情報利得が
    高く可逆．
  - (B) 代替として，支配項（compute 92%）を直接攻める **示唆3（重み int8 dynamic quantization を SL1 型 local マイクロ
    ベンチで「作る前に測る」）**は，async/overlap より攻撃対象が桁違いに大きく単一レバーに収まりやすい．
  - (C) **現時点の証拠で B14(b)/B15（async ホットパス大改修）へ進むのは非推奨**．Decision2(async) を local で追試する価値も
    限定的（sleep では既に blocking が fill 済みで差が出にくく，matmul は実機を模せない）．
  - 追加反復の要否：Decision1=(1b) の判定自体は 129σ で頑健であり，**この結論の確認のための追加ローカル反復は不要**．
    未解決なのは「実機の直列化点の特定」で，これは (A) の実機 timing ログ（別レバー・別イテレーション）の仕事．

---

### 実装 (Iter8)

**担当**: 実装フェーズ subagent（2026-07-20 JST）．`### 検討・計画 (Iter8)` §3・§5 の設計をそのまま実装した．
実機非接続・`pipeline_inference.py` 非改変（読解のみ）．**逆時系列維持のため本ブロックを `### 検討・計画 (Iter8)`
の上に置く**（同ブロックの注記と同じ理由）．

**1. 変更したファイル**

- **新規** `scripts/pipeline_fill_microbench.py`: 単一マシン上の `torch.distributed`（gloo backend，
  `torch.multiprocessing.spawn` で N プロセス起動，localhost・空きポート自動選択）によるパイプライン fill 診断
  マイクロベンチ．計画 §3 の設計どおり実装した．
- **新規** `tests/test_pipeline_fill_microbench.py`: 純粋ロジックの単体テスト（23 件）．

**2. 実装の要点（設計判断とその理由）**

- **`Channel` Protocol による通信抽象化**: `run_blocking_stage`/`run_async_stage`（pipeline 構造そのもの）を
  `recv`/`send`/`irecv`/`isend` を持つ `Channel` Protocol にのみ依存させ，実行時は `TorchDistChannel`
  （`dist.recv`/`dist.send`/`dist.irecv`/`dist.isend` への薄いラッパー），単体テストは実通信を伴わない
  `FakeChannel`（呼び出し順序を `trace` に記録）に差し替える設計にした．これにより，
  timing 依存で不安定になりがちな「structure・呼び出し回数・同期タイミング」の検証を，実際の Gloo 通信を
  一切起動せず決定的にテストできる（`bench_compute_ceiling.py` が計測部分をタイミング依存として単体テスト対象外に
  した方針と整合）．
- **blocking 変種**（`run_blocking_stage`）: `_process_microbatch`（pipeline_inference.py:1019-1050）と同じ
  「mb ごとに recv→compute→send を完了してから次の mb へ進む」逐次構造をそのまま再現．source（`prev_rank=None`）は
  recv を省略し乱数生成（:1020-1021 相当），sink（`next_rank=None`）は send を省略．
- **async 二重バッファ変種**（`run_async_stage`）: 計画 §3(b) のとおり，mb を処理する際 (1) mb 用 irecv の wait，
  (2) **compute 前に** mb+1 用 irecv を先行発行，(3) compute 後に mb 用 isend を発行するが即座に wait しない，
  という構造にした．全 mb 処理後にまとめて残りの isend を wait する．単体テストでは `FakeChannel` の呼び出し順
  トレースで「mb+1 の irecv が mb の compute より前に呼ばれる」「isend の wait がループ終了後まで遅延する」ことを
  直接検証している（`test_run_async_stage_middle_rank_issues_next_irecv_before_compute_and_defers_send_wait` 等）．
- **compute proxy**（`make_compute_fn`）: `sleep`（主，`time.sleep(t_stage)` のみ・入力をそのまま返す）・
  `matmul`（副，`hidden_size` 正方行列との行列積を `matmul_iters` 回）を CLI で選択可能にした．
- **t_stage の実測**（`calibrate_compute_fn`）: FF 計算に使う `t_stage` は固定値（sleep proxy の `--t-stage` 引数）
  ではなく，rank0 のみが実行前に `compute_fn` を実測（warmup + reps 回・中央値）した値を使う．sleep proxy では
  ほぼ `--t-stage` と一致し，matmul proxy では実際の演算時間が得られるため，proxy の種類によらず FF 計算が
  意味を持つ（全 rank 同一 `compute_fn` のため rank0 の実測値で代表させてよい，と設計判断した）．
- **計測方法**: 各 repeat の開始・終了を `dist.barrier()` で全 rank 同期し，rank0 が観測する
  `barrier→pipeline実行→barrier` の経過時間を `total_time_s`（makespan）として採用した（barrier の完了条件上，
  rank0 は全 rank が終わるまで待たされるため，rank0 の経過時間がパイプライン全体の makespan に一致する）．
  他 rank は計測・保存を行わず，rank0 のみが `results/Iter8.jsonl` へ (variant, N, M, proxy, repeat_index) ごとに
  1 レコード追記する（`record_type="pipeline_fill_microbench"`，`schema_version=1`＝本スクリプト独自のスキーマ
  空間．`tools/collect_results.py` の `schema_version=2` とは無関係）．
- **`compute_fill_factor`**: 計画 §4 の式 `FF = (M+N-1)*t_stage / measured_total_time` をそのまま純関数として実装．
  非正の入力（M・N・t_stage・measured_total_time のいずれか 0 以下）は `ValueError` を送出する．

**3. テスト結果**

- 新規 `tests/test_pipeline_fill_microbench.py`: **23 passed**（FF 計算の境界値・単調性・異常系，blocking/async
  それぞれの呼び出し回数・順序・source/sink 省略・async の先行発行と wait 遅延タイミング，`make_compute_fn` の
  proxy 別振る舞い，レコード組み立て，`append_jsonl` の追記・親ディレクトリ作成）．
- 既存回帰確認: `unset VIRTUAL_ENV && uv run pytest tests/` → **116 passed**（既存 93 + 新規 23，既存分への回帰なし）．
- スモークテスト（一時ファイルへ出力，`results/Iter8.jsonl` は汚していない）: `--variant blocking/async --proxy
  sleep`（N=4, M=4, repeat=2）・`--proxy matmul`（N=4, M=4, hidden-size=64）をそれぞれ実行し，エラーなく
  レコードが JSONL として妥当に出力されることを確認した．なお，この smoke run では N=4・M=4 という小さい構成
  かつローカル 64 コアマシンのため，blocking 版でも FF≈0.98（ほぼ pipelined）が観測された．これは Iter7 の
  cluster 挙動（sequential 型）を再現していない可能性を示すが，**判定は代表点 N=16, M=32 で行うべきもの**であり，
  本 smoke run は「スクリプトが正しく動く」ことの確認のみを目的としている（判定は実験フェーズが行う）．

**4. 実験フェーズへの申し送り（実行コマンド例）**

- 計画 §3 の代表判定点（Decision 1，blocking・sleep proxy・N=16, M=32）:
  ```
  unset VIRTUAL_ENV && uv run python scripts/pipeline_fill_microbench.py \
      --variant blocking --proxy sleep --num-stages 16 --num-microbatches 32 --repeat 5
  ```
- 同条件の async 二重バッファ版（Decision 1a のときのみ Decision 2 で使用）:
  ```
  unset VIRTUAL_ENV && uv run python scripts/pipeline_fill_microbench.py \
      --variant async --proxy sleep --num-stages 16 --num-microbatches 32 --repeat 5
  ```
- スイープ（計画 §3: N∈{4,8,16}，M∈{4,8,16,32}）は上記コマンドの `--variant`/`--num-stages`/`--num-microbatches`
  を変えて繰り返し実行する（1 回の実行 = 1 つの (variant, N, M, proxy) 設定について `--repeat` 回分のレコードを
  `results/Iter8.jsonl` へ追記，既定 `--iter-name Iter8`）．
- matmul proxy 副測定（F3 補足）は `--proxy matmul`（既定 `--hidden-size 5376 --matmul-iters 1`）を追加して同様に実行する．
- 出力は `results/Iter8.jsonl` に蓄積される．各レコードの `fill_factor`（FF）・`total_time_s` を variant・proxy 別に
  集計し，計画 §4 の Decision 1/2 判定基準（FF 閾値・speedup 閾値）と照合するのが次フェーズの作業．
- **完了条件チェック**: (i) 済（blocking/async・sleep/matmul を CLI で選択可），(ii) 未（実験フェーズが
  `results/Iter8.jsonl` を生成する．実装フェーズでは smoke run を一時ファイルへ出力し本番ファイルは未生成のまま
  にした），(iii) 済（23 passed，既存 93 件に回帰なし，計 116 passed），(iv) 済（`pipeline_inference.py`・
  serving 経路・51 ノードクラスタ非接触）．

---

### 検討・計画 (Iter8)

**担当**: 検討・計画フェーズ subagent（rc-planner，2026-07-20 JST）．`### 調査 (Iter8)` の F1〜F6・示唆1〜5 を受け，
単一レバー原則で Iter8 に実験する 1 案へ絞り込んだ．実機非接続・コード読解のみ（`pipeline_inference.py` は一切改変
していない）．**逆時系列維持のため本ブロックを `### 調査 (Iter8)` の上に置く**（launching agent の「調査の直後」指示と
journal 規約「新しいものを先頭」が競合するため，ファイル全体で一貫する逆時系列＝最新フェーズを上，を優先した）．

**1. 仮説**

Iter7 で確定した「本 bench では同時に 1 microbatch しか pipeline に滞在しない（`time_per_step ∝ m`，段間 fill 不成立）」
挙動について，その原因が **blocking `recv`→`compute`→`send` 構造そのもの**にあるなら，async `isend`/`irecv`＋二重バッファ
化で fill を回収でき F2 の潜在利得（最大 ~p 倍，p=51）に近づける．逆に **構造由来でない（別の同期点由来）**なら，
async 化しても効かず F2 の潜在利得は幻となる．この二択を，実機・ホットパスに一切触れずに判定する．

**2. 採用する単一レバー（何を何から何へ）**

- **レバー**: 単一マシン上の `torch.distributed`（gloo backend）マルチプロセス **pipeline-fill 診断マイクロベンチ**を
  新規作成する（`scripts/pipeline_fill_microbench.py`）．SL1（Iter5, compute 天井の local マイクロベンチ）・
  SL2（Iter6, 採択率オフライン見積もり）と同じ「作る前に測る」系譜．`current_lever = "pipeline_fill_microbench
  (local Gloo diagnostic, F2 overlap 軸)"`．
- **変更前**: F2 の潜在利得（~p 倍）の実在性が未検証で，その回収に必須の `_process_microbatch` async 化
  （B14(b) の不可逆・大規模改変）の go/no-go 判断材料が無い．
- **変更後**: 新規スクリプト 1 本を追加し，N 段 × M microbatch の `recv`→`compute`→`send` パイプラインを
  (a) blocking 版・(b) async `isend`/`irecv`＋二重バッファ版で回し，集約時間が sequential 型（≈`M·N·t_stage`）か
  pipelined 型（≈`(M+N-1)·t_stage`）かを測る．
- **固定する構成（直近最良に固定，単一レバー原則）**: `pipeline_inference.py`（ホットパス・serving 経路とも）非改変，
  51 ノードクラスタ非接触，config `levers` は全て既定（`NUM_MICRO_BATCHES=4`・`STAGGER_INTERVAL`・`SEQ_LEN`・
  `WORLD_SIZE` 既定），B9/SL3（relay 改修）非着手．

**3. スクリプト設計（実装フェーズが着手できる粒度）**

- **新規ファイル**: `scripts/pipeline_fill_microbench.py`（冒頭にファイル責務コメント 1 行）．`torch.multiprocessing.spawn`
  で N プロセス起動，`dist.init_process_group(backend="gloo", ...)`（localhost，`MASTER_ADDR`/`MASTER_PORT` を
  スクリプト内で設定）．
- **各 rank のループ**は `_process_microbatch`（`pipeline_inference.py:1019-1050`）の `recv`→`compute`→`send` 構造を模す．
  rank0=source（compute→send のみ），rank N-1=sink（recv→compute のみ），中間 rank は recv→compute→send．
  microbatch ループ `for mb in range(M)`．
- **compute proxy 2 種（CLI で選択）**:
  - **主（sleep proxy）**: compute を `time.sleep(t_stage)` で模す．CPU コア競合を排除し「pipeline 構造が fill を許すか」
    を純粋に切り分ける（N > ローカルコア数でも真の段間並列が観測可能）．`t_stage` 既定は Iter7 実測の 1 段あたり
    ≈6ms（0.31s/51）．**主判定はこの sleep proxy で行う**．
  - **副（matmul proxy）**: `hidden_size=5376` 相当の GEMV/GEMM を float32・`torch.set_num_threads` 制限下で実行し，
    F3（CPU/Gloo で comm と compute が同一コアを食い合う）の影響を補足測定（情報提供のみ）．
- **変種 (a) blocking**: `dist.recv`/`dist.send`（現行 bench と同じ）．
- **変種 (b) async 二重バッファ**: mb+1 の `irecv` と mb-1 の `isend` を先行発行→handle 保持→mb を compute→使用前に
  `wait()`（F4 が指摘した「`recv`→`compute`→`send` 構造の作り替え」の最小プロトタイプ．ここは prototype なので
  ホットパスではなく本スクリプト内に閉じる）．
- **計測・保存**: 各 (variant, N, M, proxy) を n≥5 反復し total wall time の平均・母標準偏差を，`results/Iter8.jsonl` へ
  `record_type="pipeline_fill_microbench"` で構造化保存（ローカル実行のため SSH 収集不要，スクリプトが直接 append）．
- **スイープ**: N∈{4, 8, 16}，M∈{4, 8, 16, 32}．代表判定点は **N=16, M=32**（sequential でも `t_stage=6ms` なら
  ~3s/run と軽量）．

**4. 成功条件・判定基準（定量，Iter7 同様に閾値明記）**

Fill factor を `FF(variant,N,M) = (M+N-1)·t_stage / measured_time` と定義する（`FF≈1`→完全 pipelined＝fill 成立，
`FF≈1/N`→完全 sequential）．ノイズは制御されたローカル計測で小（CV<5% 見込み），n=5 の 2σ を有意基準とする．

- **Decision 1（Iter7 の cluster 挙動をローカルで再現するか / 主 sleep proxy・N=16,M=32・blocking 版）**:
  - **(1a) blocking `FF ≤ 0.3`（≈sequential，Iter7 と一致）**: 「blocking 構造そのものが段間 fill を潰す」を確認 →
    **Decision 2 へ**．
  - **(1b) blocking `FF ≥ 0.7`（≈pipelined，ローカルでは fill する）**: blocking 構造は本来 fill する → 実機 Iter7 の
    sequential 化は **別の同期点**（rank0 の microbatch 生成直列化・`_reset_kv_cache_for_bench` の同期・どこかの
    `barrier` 等）由来であり，**async ホットパス大改修（F2/B14(b)）は誤った処方箋**．→ needs-human は登録せず，
    次イテレーションは「実機 bench への per-microbatch timing ログ追加（軽量・可逆）で実 sync 点を特定」へ振り替えを
    推奨．async 軸は「不要」で収束方向．
- **Decision 2（Decision 1a のときのみ / async 二重バッファ版 FF と blocking 比 speedup）**:
  - **(2a) GO: async `FF ≥ 0.6` かつ speedup（=blocking_time/async_time）≥ 2.0**（主 sleep proxy，N=16,M=32，各 2σ 超）:
    CPU/Gloo でも async 二重バッファで fill が回収でき F2 の ~p 倍潜在利得が実在 → `_process_microbatch` async 化
    （B14(b) の不可逆・大規模ホットパス改変）に着手する価値が実証された → **backlog に B15 として `[needs-human]` 登録し
    Slack で `<@U08GLKY1QCW>` に go/no-go を諮る．実装フェーズ（async 化）へは進めず一旦停止**．
  - **(2b) NO-GO/収束: async speedup < 1.3 または async `FF < 0.6`**: async 化しても fill しない（F3 の通り CPU/Gloo は
    comm と compute が同一コアを食い合い overlap 利得が乗らない）→ F2 の潜在利得は本 HW で回収不能 → async 軸は棄却・
    収束．次イテレーションは compute 直撃軸（示唆3: 重み int8 dynamic quantization を SL1 型 local マイクロベンチで
    先に検証）または `STAGGER_INTERVAL` フォールバックへ．
  - **(2c) 中間（1.3 ≤ speedup < 2.0 または境界）**: matmul proxy 副測定と併せ reflector が判断（F3 の CPU 競合で
    理論値が削れている可能性を考慮）．

**5. 完了条件（実装・実験フェーズが満たすべきもの）**

- (i) `scripts/pipeline_fill_microbench.py` 新規作成，blocking/async 両変種・sleep/matmul 両 proxy を CLI 引数で選択可．
- (ii) `results/Iter8.jsonl` に (variant, N, M, proxy, repeat) ごとの total_time を `record_type="pipeline_fill_microbench"`
  で構造化保存（各点 n≥5）．
- (iii) 新規スクリプトのロジック（FF 計算・pipeline 構造）に対する単体テスト green，既存 93 passed に回帰なし．
- (iv) `pipeline_inference.py`・serving 経路・51 ノードクラスタ非接触（完全にローカル・可逆）．

**6. 診断の射程に関する明示的な留保（reflector 向け）**

- 本ベンチは単一マシン上で N プロセスを走らせるため，N > ローカルコア数のとき**真の段間 compute 並列（ノードが物理的に
  別 CPU で同時計算する状態）は再現できない**．そのため **sleep proxy を主信号**とし，「comm プロトコルが fill を許すか」
  という machine-count 非依存の構造問題だけを切り分ける（matmul proxy は CPU/Gloo 競合 F3 の補足のみ）．本ベンチは
  cluster の絶対スループット予測を主張せず，「blocking/async の pipeline 構造が CPU/Gloo で fill を許すか」という
  狭く決定的な問いに答える．

**7. needs-human 判断・B9/B14 との関係**

- **本レバー自体（ローカル診断マイクロベンチ）は可逆・コードのみ・クラスタ非接触**であり，B14(b) の「ホットパス改変で
  不可逆・大規模」に該当しない．よって **今回は needs-human を登録せず，単一レバーとして実装フェーズへ進めてよい**．
  ただし Decision 2a（GO）に至った場合は，その次のステップ（async ホットパス改修）が B14(b) 該当のため，その時点で
  B15 として `[needs-human]` 登録＋Slack 確認を仰ぐ（本イテレーションでは async 化を実装しない）．
- 本レバーは通信・計算オーバーラップ軸（F2）の go/no-go を安価に潰すもので，B9（speculative decoding/relay 改修＝
  トークン投機軸）とは直交．B14(b) の申し送り（async 化に踏み込むなら needs-human）を先取りせず，「踏み込む価値が
  あるか」を先に測る設計とした．F6（`recv`/`send` 例外握り潰し）は本ローカル診断（単一マシン・障害注入なし）では
  影響しないが，Decision 2a で hotpath 改修へ進む際の設計前提として B15 に併記する．

---

### 調査 (Iter8)

**担当**: 調査フェーズ subagent（rc-investigator，2026-07-20 JST）．B14／Iter7 の発見（bench 経路に段間の
通信・計算オーバーラップが構造的に欠如）を起点に，research_frontier⑤（先行研究調査に基づく推論パイプライン
高速化）の主軸（通信・計算オーバーラップの CPU/Gloo 上での有効性）と副軸（KV キャッシュ最適化・量子化・
バッチング戦略）を文献調査した．実機非接続・コード読解と tavily 検索のみ（`pipeline_inference.py` は読むだけで
一切改変していない）．次の計画フェーズが単一レバー原則で 1 案へ絞り込めるよう，複数候補を実装コスト・可逆性・
期待効果の目安付きで整理する．

**調査の問い**

- Q1（主軸）: 分散パイプライン並列推論で async `isend`/`irecv`＋二重バッファ＋GPipe/1F1B 型スケジューリングは，
  CPU/Gloo バックエンド（GPU/NCCL 前提の研究が多い制約下）でどれだけ有効か．本リポジトリの bench 経路
  （`_process_microbatch`）に適用すると何が起きるか．
- Q2（本命候補の転用可否）: serving relay 経路（:1706-1813）の既存 `irecv` パターンは bench 経路の二重バッファ化に
  そのまま転用できるか（Iter7 が名指しした本命）．
- Q3（副軸）: KV キャッシュ最適化・量子化・（continuous batching / speculative decoding 以外の）バッチング戦略のうち，
  本ワークロード（compute 律速 92%・CPU float32・Gemma sliding-window）で行動可能な単一レバー候補はどれか．
- Q4（前提条件）: async 化に際し `dist.recv`/`dist.send` の例外握り潰し（Iter7 §2-iv の設計弱点）はどう影響するか．

**分かったこと（出典付き）**

- **(F1) 通信・計算オーバーラップの利得は「通信が占める時間」で上限が決まる（compute 律速では利得が小さい）**:
  overlap の makespan は compute 律速シナリオでは計算オペレータ時間の総和で決まる（Lagom, arXiv:2409.15184 "Lagom:
  Unleashing the Power of Communication and Computation Overlapping for Distributed LLM Training"）．PyTorch 公式の
  分散論文も「overlap の speedup は計算時間と通信時間がほぼ等しいときに最も効く」と述べる（"PyTorch Distributed:
  Experiences on Accelerating Data Parallel Training", arXiv:2006.15704）．本リポジトリは Iter4 で **ITL の
  compute≈92%・send≈0.3%・residual≈7.6%** と確定済み＝通信は極小．したがって「async 化で通信を隠す」目的で見た
  期待利得は数 % 未満で **低い**．overlap の一般的知見（Compute-Communication Overlap Patterns, emergentmind）も，
  利得は「隠せる通信量」に比例すると整理している．
- **(F2) ただし本 bench の律速は『通信』ではなく『パイプラインが充填されていない（段間並列が起きていない）』こと**:
  GPipe/1F1B のバブル率は `(p-1)/(m+p-1)`（v=virtual pipeline size 使用時 `(p-1)/(vm+p-1)`．Michael Brenndoerfer
  "Pipeline Parallelism: Stages, Micro-Batching, GPipe, 1F1B"; perform.digital "Pipeline Parallelism and the Microbatch
  Bubble"）で，m を増やせばバブルは縮む「はず」．しかし Iter7 は m=8→204 で 1.12 倍しか出ず，step 時間が m にほぼ
  比例（限界コスト 0.31s/mb 一定＝1 microbatch が 51 段を単独貫通）した＝**同時に 1 microbatch しか pipeline に
  居ない（fill が起きていない）**．理論上ここを直せば集約スループットは最大で ~p 倍（本構成 p=51）に近づく余地があり，
  **これは通信隠蔽（F1，数 % 上限）とは桁違いに大きい潜在利得**．ただし後述 F5 の通り「bench の集約スループット」が
  実 serving 指標に対応するかは別問題．
- **(F3) CPU/Gloo では GPU/NCCL 流の overlap 機構がそのまま効かない**: overlap を制御する Megatron の
  `overlap_p2p_comm`／`batch_p2p_comm` や，PyTorch の「pipeline 用に複数 CUDA stream で comm を並列化する」議論
  （pytorch/pytorch Issue #175225，docs.nvidia.com Megatron Core model_parallel_config）はいずれも **GPU/NCCL 前提**．
  GPU は DMA エンジンが計算と別に転送を進めるが，**CPU/Gloo は転送も計算も同じ CPU コアを食い合う**（Gloo は CPU
  推奨だが NCCL の 30〜60% の速度，"Why GLOO's performance is much worse than NCCL?" PyTorch Forums）．
  PyTorch dev-discuss（"Memcpy based P2P communication for pipeline parallelism"）でも「2 つの isend は overlap する
  が利点がない場合がある／irecv 同士は劣化なく走る」と，CPU 側 overlap の利得が限定的な実測が報告されている．
  → **CPU/Gloo で async 化しても『通信を計算の裏に隠す』効果は薄い**が，**『複数 microbatch を異なる段に同時滞在
  させる（pipeline fill）』効果は blocking でも本来起きるはずで，現状それが起きていない原因の切り分けが先決**．
- **(F4/Q2) serving relay の "async" パターンは overlap の雛形にならない（コードレベル確認）**: :1706-1709・:1810-1813 の
  `op = dist.irecv(...); op.wait()` は **irecv 発行直後に wait()** しており，seq_len と hidden の 2 本の irecv を互いに
  並列化するだけで **通信と計算は一切 overlap していない**（async-in-form, sync-in-effect）．Iter7 journal が「既存
  irecv パターンの転用」と表現したが，**このパターンをそのまま bench にコピーしても overlap は生まれない**．真に
  overlap するには `_process_microbatch` の recv→compute→send 構造を作り替え，「mb+1 の `irecv` と mb-1 の `isend` を
  発行→handle 保持→mb を compute→次段で使う前に `wait()`」という二重バッファ・ソフトウェアパイプラインへ再構成する
  必要がある（≠既存コードの転用）．**追い風となる既存資産**: `recv_buffers`/`send_buffers` は既に **microbatch 毎の
  list（`[mb]` 添字，:617-624）**として確保済みで，二重バッファのバッファ側インフラは概ね揃っている（新規確保は不要，
  ロジック再構成が主）．
- **(F5/Q3) 副軸の候補比較**:
  - **量子化（重み int8 dynamic quantization, CPU）**: PyTorch は CPU 向け `torch.ao.quantization` の dynamic
    quantization（Linear→int8）を持ち，**compute 律速（92%）の支配項＝GEMM/GEMV を直接削れる**唯一の副軸．overlap が
    攻める通信（0.3%）より攻撃対象が桁違いに大きい．LLM 推論最適化の一般整理でも量子化・KV量子化は主要手段
    （"Optimizing LLM Inference: KV Cache, Batching, and Quantization Tradeoffs"; "LLM Inference at Scale: 10 KV-Cache
    & Batching Wins", medium）．**期待効果=中〜高／実装コスト=中（load 時に Linear を量子化）／可逆性=高（load フラグ）／
    リスク=数値品質の劣化（51 ノードで要検証）・per-node model 改変**．ただし Gemma-4 の実重み・アーキテクチャで
    CPU int8 が実際に速くなるかは要実測（GEMV は memory-bound 寄りで int8 の演算利得が乗りにくい場合がある）．
  - **KV キャッシュ量子化（int8/4bit）／PagedAttention 系**: メモリ削減が主目的（vLLM PagedAttention は KV 断片化を
    70%→4% に，"Ultimate Guide to LLM Inference Optimization", latitude）．本ワークロードは compute 律速でメモリ律速
    ではなく，Gemma sliding-window で KV 上限も既に抑制済み（config `SEQ_LEN` レバー）．**latency への期待効果=低**．
  - **chunked prefill / prefill batching**: TTFT（prefill 段）にのみ効き，decode ITL に効かない（NVIDIA TensorRT-LLM
    chunked prefill; "Prefill and Decode for Concurrent Requests", HuggingFace）．本デモは短い単発 prompt で prefill 比率が
    小さく **優先度低**．
  - **static/dynamic な複数リクエスト・バッチング**: bench の「集約 microbatch スループット」に実 serving 上の意味を
    与えうる唯一の道だが（"High-Throughput LLM Inference", emergentmind），**relay プロトコルへ複数リクエスト運搬を
    足す改修**が要り，B9/SL3（relay 改修＝不可逆・大規模）と実装面で衝突する（speculative decoding とは軸が直交＝
    トークン投機 vs 並行リクエストだが，触る場所が同じ）．**単一レバーには過大・needs-human 隣接**．
- **(F6/Q4) async 化は通信断検知・伝播の整備が前提**: `_process_microbatch` は `dist.recv`/`dist.send` を
  `except Exception: return`/`pass` で握り潰す（:1023-1027, :1047-1050）．blocking でも「クラッシュした rank を
  正常完了に見せかける」弊害が Iter7 で顕在化したが，**async（`isend`/`irecv`＋`wait()`）では失敗・timeout が
  `wait()` 段で表面化し，握り潰したままだと buffer 内容が未定義のまま計算が進み pipeline がサイレント破壊される**
  危険が増す．overlap 実装に踏み込むなら **通信断の検知・伝播（例外の上位伝播 or 明示的な health チェック）を
  先に／同時に入れる**のが信頼性の前提（Iter7 §2-iv・B13・B14 の申し送りと整合）．

**次フェーズ（計画）への示唆（単一レバー候補の絞り込み材料）**

- **示唆1（主軸の期待値の再評価）**: 「async `isend`/`irecv`＋二重バッファ」を **通信隠蔽**として見ると，本ワークロード
  （通信 0.3%・CPU/Gloo）では利得上限が数 % で **期待値が低い**（F1・F3）．一方 **pipeline fill（段間並列）を成立させる**
  観点で見ると潜在利得は桁違い（F2）．計画フェーズは「何を攻めるレバーか」を明確に切り分けるべき——本命候補の真価は
  「通信を隠す」ではなく「複数 microbatch を段間に同時滞在させる」ことにある．
- **示唆2（fill 不成立の原因切り分けを先に）**: Iter7 の linear-in-m は「blocking Gloo でも本来起きるはずの段間 fill が
  起きていない」ことを示す．async 化に踏み込む前に，**なぜ blocking 版で fill しないのか（handoff の直列化か，
  乱数生成等の rank0 固定オーバーヘッドか，`_reset_kv_cache_for_bench` の同期点か）を軽量に切り分ける**診断レバーが，
  Iter5/SL1 の「作る前に測る」系譜と整合し，かつコードのみ・可逆で単一レバー原則に最も収まりやすい（推奨の第一候補）．
  これにより「二重バッファ化すれば fill するのか，それとも別要因か」を実装前に判定できる．
- **示唆3（compute 律速を直接攻める副軸）**: 通信でなく **支配項（compute 92%）を攻める**なら **重み int8 dynamic
  quantization（F5）**が overlap より攻撃対象が大きく，単一レバー（load 時フラグ・可逆）に収めやすい．ただし CPU int8 が
  Gemma-4 の実形状で実速くなるかは Iter5/SL1 型の local マイクロベンチで**作る前に測る**のが安全（GEMV は memory-bound
  寄りで利得が乗らないリスク）．overlap（示唆1）と量子化（示唆3）は攻撃対象が別（通信 vs 計算）なので，計画は
  どちらのレバーを 1 本選ぶかを明示すること．
- **示唆4（本命候補の実装見立て・計画への申し送り，実装はしない）**: 二重バッファ化は「既存コードの転用」ではなく
  `_process_microbatch` の **recv→compute→send 構造の作り替え**が必要（F4）．追い風は `recv_buffers`/`send_buffers` が
  既に mb 毎 list である点（新規確保不要）．逆風は (a) serving relay の irecv+即 wait パターンは overlap の雛形に
  ならない，(b) `pipeline_inference.py` ホットパス改変で **B14(b) の通り不可逆・大規模になりうる→計画がここまで
  踏み込むと判明した時点で `[needs-human]` 登録＋Slack 確認**，(c) 通信断検知・伝播（F6）を前提として同時に設計する
  必要，の 3 点．**この 3 点は計画フェーズが実装案を書く際の必須の前提として申し送る**．
- **示唆5（バッチング／relay 改修は今回のスコープ外）**: 複数リクエスト・バッチングは bench 指標に実意味を与える唯一の
  道だが relay 改修＝B9/SL3 と衝突し不可逆・大規模（F5）．今回の単一レバーには選ばず，B9 の人間判断待ちに委ねる
  （speculative decoding とは軸が直交する点は峻別済み）．config `levers` の `STAGGER_INTERVAL`/`SEQ_LEN`/`WORLD_SIZE`
  は，上記示唆 2/3 のいずれも計画が過大と判断した場合の **フォールバック**として温存（B14(a) の通り）．

---

## Iteration 7

### 考察・次計画 (Iter7)

**担当**: 考察・次計画 subagent（2026-07-20 JST）．`### 分析(解釈) (Iter7)` の判定（反証条件に実質的に近い中間事例，
機構＝段間オーバーラップ欠如の confidence 高・収束方向 confidence 中〜高）を受け，単一レバー **`NUM_MICRO_BATCHES`
（research_frontier② のスループット感度）**の採否を reflector として確定し，次イテレーション（Iteration 8）の方向を
決めた．実機への新規接続・実行はしていない（journal・`results/Iter7.jsonl` の読み取りと commit 操作のみ）．

**1. 採否判定: 不採用（仮説棄却）＝このレバーは現実装で収束（reject / converged）**

- **判定**: `### 検討・計画 (Iter7)` の仮説「m を増やすとパイプラインバブルが償却されスループットが上がる（p=51 で
  m=8→51 は約 3.6 倍・m=204 は約 5.8 倍）」は**棄却**する．実測は m=8→51 で **1.12 倍**（2.8478→3.1772→3.2102
  microbatch/s），計画の採用側閾値 1.5 に遠く未達で，理論予測（3.6 倍）と 1 桁近く乖離した．よって
  `NUM_MICRO_BATCHES` は「現行 bench 実装（blocking Gloo・逐次 mb ループ・二重バッファ無し）の下では集約スループットの
  実効的なレバーにならない」と判定し，**このレバーは収束**（同じ問いへ m を再び振っても新情報は得られない）とする．
- **中間事例の解釈と reflector としての最終判断**: analyst が留保したとおり，形式的には成功条件 1 の一部（有意な単調
  増加＝2σ の 40〜75 倍）を満たし，厳密な反証条件（3 水準が 2σ 内で平坦）そのものは満たさない．しかし reflector は
  「形式条件の機械的当てはめ」ではなく「レバーが目的（バブル低減によるスループット向上）を達成したか」で判定する．
  観測された 1.12 倍は，コードと step 時間スケーリングの二重証拠（§2-i）から**バブル低減ではなく固定オーバーヘッド
  償却の副作用**と機構が特定されており，仮説が想定した効き源とは別物である．効果の向きが正でも「仮説の機構で効いて
  いない」以上，レバーとしては不採用が妥当と判断した．**「有意だが機構が仮説と異なり効果量も閾値未達」＝実質的な反証**
  であり，analyst の収束・振り替え推奨を採用する．
- **追加反復の要否**: 不要．m=204 の repeats 補完（n=1→3）は，主要結論（機構＝オーバーラップ欠如・成功条件 2 未達）が
  m=8/m=51 の n=3 データと `_process_microbatch` のコード構造だけで確定しており（analyst §4），かつ throughput が
  漸近値 ≈3.23 に張り付いている以上，覆らない見込み．約 6.3 時間（既定 repeats=3）のコスト対効果が低く見送る．

**2. 非自明な学び（次の自分向け）**

- **(i) 本パイプラインには段間の通信・計算オーバーラップが構造的に存在しない（今回の最重要の学び）**: bench 経路
  `_run_microbatch_bench`→`_process_microbatch` は各マイクロバッチを (A) blocking `dist.recv` → (B) compute →
  (C) blocking `dist.send` で**同期・逐次**に処理し，二重バッファも async `irecv`/`isend` も持たない（async は serving
  relay 経路 :1706-1813 にのみ存在）．このため `time_per_step ≈ 0.31×m + 0.35`（限界コスト 0.31s/microbatch が m に
  依らず一定）と**m にほぼ比例して step 時間が増える**逐次型モデルが適合し，GPipe/バブル式 `(p−1)/(m+p−1)` が前提とする
  「t_stage 一定・fill/drain 償却」は実装上・実測上いずれも成立しない．**「マイクロバッチ数を増やせばバブルが埋まる」
  という一般的直観は，段間オーバーラップを実装して初めて成り立つ**——本リポジトリの bench 経路はその前提を満たさない．
- **(ii) 観測された 1.12 倍は latency 悪化と引き換えの見かけ上の微増**: 集約スループットの微増は固定オーバーヘッド
  （≈0.35s/step，rank0 の乱数生成・`_reset_kv_cache_for_bench`・drain 等）が m 本に償却される副次効果で，
  `microbatch_per_s = m/(0.31m+0.35)` が m→∞ で 1/0.31≈3.23 へ漸近する（m=204 の 3.21 は既に漸近値近傍）．一方
  step あたり latency は m に線形に悪化する（m=8:2.8s→m=204:63.5s）ため，**m を増やす実利は事実上ない**．
- **(iii) この学びは research_frontier⑤（高速化）の具体的な次の一手を指し示す**: 「もしバブル式を本当に効かせたいなら，
  レバー変更ではなく `_process_microbatch` を async `isend`/`irecv`＋二重バッファ化する**実装変更**（serving 経路
  :1706-1813 の既存 irecv パターンの転用）が前提」という analyst の示唆は，そのまま⑤の「通信・計算オーバーラップ」
  という具体的な高速化軸である．ただしこれは `pipeline_inference.py` ホットパス改変を伴い単一レバーの範囲を超える
  可能性が高く，B9/SL3（relay 改修）に隣接する不可逆側の判断を含みうる．**自動実装はせず，Iter8 の調査フェーズの
  起点（seed）として扱う**（§4，B14）．
- **(iv) recv/send 例外握り潰しは pipeline 全体の設計弱点として記録（B13 の申し送り事項）**: バグ A の副作用として
  発見された `_process_microbatch` の `dist.recv`/`dist.send` の例外握り潰し（通信断を「正常完了」に見せかける）は，
  bench 固有ではなく `pipeline_inference.py` 全体の設計弱点でありうる（B13 が reflector 判断に委ねた点）．本 Iteration の
  直接スコープ外だが，⑤（高速化）の実装で async 通信へ踏み込む際には**通信断の検知・伝播**が信頼性の前提になるため，
  ⑤の調査・計画時に併せて検討すべき将来課題として記録する（今回は変更しない）．

**3. B9（B3 本体＝relay プロトコル改修＝SL3 の go/no-go）の扱い: needs-human のまま維持（今回 reflector では自動判定しない）**

- 依頼どおり B9 は温存する．B9 は「不可逆・大規模な relay プロトコル改修」の go/no-go であり research-cycle の自律判断
  ポリシー（不可逆/大規模は人間判断）に該当する不可逆判断のため，本 reflector では自動選択しない．Iter6 完了時に既に
  Slack（`<@U08GLKY1QCW>` mention 付き）で報告済みで，今回の通常サマリー投稿では重複 mention を避ける．なお §2-iii の
  ⑤（通信オーバーラップ）は speculative decoding（B9/SL3）とは**別軸**（通信・計算の重なり vs トークン投機）であり，
  B9 とは直交する（B9 の判断を先取りしない）．

**4. 次に振るレバーの決定（Iteration 8）: research_frontier⑤（通信・計算オーバーラップを起点とする高速化）の調査を自動選定**

- **決定（自律判断・可逆）**: Iteration 8 は **research_frontier⑤（先行研究調査に基づく推論パイプライン高速化）**を，
  Iter7 で判明した「段間オーバーラップの構造的欠如」（§2-i）を具体的な起点として着手する．state は `phase="investigate"`・
  `current_lever=null` とし，調査フェーズ（rc-investigator）が「分散パイプライン並列推論における通信・計算オーバーラップ
  （async `isend`/`irecv`・二重バッファ・GPipe 型スケジューリングの CPU/Gloo 上での有効性）」を主軸に，KV キャッシュ
  最適化・量子化・continuous batching 等の⑤候補も併せて文献調査し，計画フェーズが**単一レバー原則で 1 つの具体案へ
  絞り込む**．
- **analyst 推奨（STAGGER_INTERVAL への振り替え）に対する reflector の判断**: analyst は config `levers` の次候補
  `STAGGER_INTERVAL` を推奨したが，reflector は⑤（通信オーバーラップ調査）を優先する．理由: (1) `STAGGER_INTERVAL` は
  「起動時 thundering herd 回避の待機間隔」で**定常状態のスループット/レイテンシに直接効かない**（Iter4 で ITL は
  compute 律速＝92% と確定済み，起動時交絡は Iter6 までに warm-up で除去済み）ため，振っても Iter7 と同様「効かない
  ことの確認」に 1 イテレーションを費やす公算が高く期待値が低い．(2) 対して Iter7 は「段間オーバーラップが無い」という
  **具体的で行動可能な発見**を残しており，これは⑤の中核候補（通信・計算オーバーラップ）を直接指す．同じ「待ち時間を
  無駄にしない自律実行可能な項目」でも，調査・計画フェーズはコードのみ・実機非接続・可逆で，期待値が明確に高い方
  （⑤）を選ぶのが妥当．(3) config の levers 優先順位は目安であり，⑤はユーザーの明示指示（2026-07-18）による常設項目で
  「②③④と重複する場合は一本化」と規定されている——②（今回のマイクロバッチ感度）の結果が⑤の一軸（通信オーバー
  ラップ）を名指しした以上，⑤へ一本化するのが config の意図に沿う．
- **見送り（非選定）の理由と扱い**: `STAGGER_INTERVAL`/`SEQ_LEN`/`WORLD_SIZE` は config `levers` に残置し，⑤の調査で
  行動可能な単一レバー案が得られない，または不可逆な実装変更が必要で人間判断待ちになった場合の**フォールバック候補**
  として温存する（特に `STAGGER_INTERVAL` は次の軽量 config レバー）．SL3/B3 本体は §3 のとおり B9（人間判断待ち）．
  backlog に `## B14 [auto-decided 2026-07-20]` として本決定を記録した（⑤優先は levers 優先順位からの逸脱を含むため
  要レビュー扱い）．

**次イテレーションへの結論**: Iteration 7（`NUM_MICRO_BATCHES` のスループット感度）を**不採用（仮説棄却）・このレバーは
現実装で収束**と確定した（実測 1.12 倍で採用閾値 1.5 に遠く未達，かつ微増の機構はバブル低減ではなく固定オーバーヘッド
償却の副作用で仮説と異なる＝実質的な反証）．最重要の学びは「本パイプラインの bench 経路には段間の通信・計算オーバー
ラップが構造的に存在しない（blocking Gloo・逐次 mb ループ）」ことで，これが research_frontier⑤（高速化）の具体的な次の
一手（async 通信＋二重バッファによる通信・計算オーバーラップ）を名指しした．Iteration 8 は⑤の調査を，この発見を起点に
開始する（analyst 推奨の STAGGER_INTERVAL はフォールバックとして温存）．B9（SL3 go/no-go）は不可逆判断のため
`[needs-human]` のまま維持する．

### 実験 (Iter7, 再実行)

**担当**: 実験フェーズ subagent（2026-07-19T20:22〜2026-07-20T01:23 JST，約 5 時間 1 分）．`### 実装 (Iter7, 差し戻し後)`
§5 の手順に従い，バグ A・B 修正後の m=8 パイロット再確認 → m∈{8, 51, 204} の本掃引 → クラスタ復元まで**完走した**．
`uv run pytest tests/` を作業開始前に再確認し 93 passed（回帰なし）．

**1. m=8 パイロット再確認（`MICROBATCH_BENCH_STEPS=5, WARMUP=2`）**

- 事前に 51 ノード健全性確認（`mise run status` 相当）→ 51/51 healthy．
- deploy 51/51 成功（約 52 秒）．bench 実行中・実行後ともログにクラッシュ（`TypeError: ... missing 1 required
  positional argument: 'position_ids'`）・ハングは再発せず，`[R50 RESULT] MICROBATCH_BENCH m=8 ... microbatch_per_s=
  {2.8644, 2.9188, 2.7892}`（3 repeat）が正常に出力された．バグ A・B の修正は実機で有効と確認．

**2. 本掃引（m∈{8, 51, 204}，`MICROBATCH_BENCH_STEPS=100`，warmup/repeats は既定 20/3）**

- **m=8**: deploy 51/51 成功．3 repeat 完走，クラッシュ・エラーなし．
  `microbatch_per_s = {2.8429, 2.8480, 2.8525}`（平均 2.8478，母標準偏差 σ≈0.0039，CV≈0.14%）．
  `results/Iter7.jsonl` へ 3 レコード追記．
- **m=51**: deploy 51/51 成功．3 repeat 完走．**1 repeat あたり measure=100 ステップの所要時間が elapsed_s≈1605s
  （m=8 の elapsed_s≈281s の約 5.7 倍）**と，`(p-1)/(m+p-1)` バブル式が予測する緩やかな短縮ではなく m にほぼ比例して
  増加する挙動を観測した（後述 §4）．全 3 repeat の途中，`docker stats` で全ランクの CPU 使用率（90〜250%）を複数回
  確認し，ハングでないこと（実際に計算が進行していること）を確かめた上で待機を継続した．クラッシュ・エラーなし．
  `microbatch_per_s = {3.1758, 3.1755, 3.1802}`（平均 3.1772，σ≈0.0021，CV≈0.068%）．`results/Iter7.jsonl` へ
  3 レコード追記．
- **m=204（計画からの逸脱・要報告）**: deploy 前に pre-flight を実施——(a) RAM: `free -m` で全ノード空きメモリ
  数百 MB〜数 GB を確認，実際の通信バッファ確保は 8.37 MB（deploy 後のログで実測）で計画の見積もり
  （約 17MB 以内）を下回り，RAM リスクは想定どおり無し．(b) **実行時間**: m=8→m=51 の実測から
  「measure=100 の所要時間が m にほぼ比例して伸びる」ことが判明したため，m=204 へ進む前に
  `MICROBATCH_BENCH_STEPS=5, WARMUP=2, REPEATS=1` の小規模タイミングパイロットを実施した．結果
  elapsed_s=317.7s（measure=5 ステップ，63.5s/step）．これは m=51 の 16.06s/step のほぼ 4 倍（m 比 204/51=4 と
  ほぼ一致）で，**既定 `REPEATS=3, MICROBATCH_BENCH_STEPS=100` のまま本掃引すると 1 repeat あたり約 127 分
  （(20+100)×63.5s），3 repeat で約 6.3 時間**かかると見積もられた．これは `### 検討・計画 (Iter7)` §5 の
  pre-flight リスク（RAM のみ想定）には無かった，実行時間に関する未想定のリスクである．m=51 の 3 repeat
  完走に実測約 93 分要した実績と比べても著しく長いため，**`MICROBATCH_BENCH_REPEATS=1`（既定 3 から削減，
  `MICROBATCH_BENCH_STEPS=100`・`WARMUP` は既定 20 のまま変更なし）に縮退して 1 repeat のみ実行する判断を，
  実験フェーズの裁量で行った**（水準自体を落とす計画済みの縮退案「OOM なら {8,51} の 2 水準へ」とは異なり，
  m=204 という水準は維持しつつ，実行時間超過という新たに判明したリスクに対して repeats のみを縮退した）．
  deploy 51/51 成功，comm buffer 実測 8.37MB．約 105 分後（elapsed_s=6354.7s，63.55s/step，pilot の 63.5s/step と
  ほぼ一致）に `[R50 RESULT] MICROBATCH_BENCH m=204 p=51 warmup=20 measure=100 elapsed_s=6354.7024
  steps_per_s=0.0157 microbatch_per_s=3.2102` を取得．クラッシュ・エラーなし．`results/Iter7.jsonl` へ 1 レコード
  追記（**n=1，σ・CV は算出不可**．m=8/m=51 の 3 repeat と異なり，このレベルのみ反復数が異なる点に注意）．

**3. 完了条件（`### 検討・計画 (Iter7)` §4）との対比**

- (i) `results/Iter7.jsonl` への構造化保存: **達成**（m=8: 3 レコード，m=51: 3 レコード，m=204: 1 レコード，計 7
  レコード）．ただし m=204 のみ `repeats≥3` を満たしていない（上記 §2 の縮退による．理由は明記のとおり）．
- (ii) 既存パーサ単体テスト green・回帰なし: 作業開始前に再確認済み（93 passed，本フェーズ中はコード変更なし）．
- (iii) bench 分岐が env 既定で非改変: クラスタ復元後に `docker inspect` で `NUM_MICRO_BATCHES=4` かつ
  `MICROBATCH_BENCH_*` 系 env が一切設定されていないことを確認．`healthcheck.py` で 51/51 healthy を確認済み．

**4. 成功条件（`### 検討・計画 (Iter7)` §4）への当てはめ（数値の提示のみ，良否判定は analyst に委ねる）**

- throughput(m=8)=2.8478，throughput(m=51)=3.1772，throughput(m=204)=3.2102（いずれも microbatch_per_s 平均，
  m=204 は n=1）．
- 単調増加は成立（2.8478 < 3.1772 < 3.2102）．throughput(m=51)−throughput(m=8)=0.3294 は m=8/m=51 双方の 2σ
  （それぞれ約 0.0078／0.0043）を大きく上回る．
- throughput(m=51)/throughput(m=8) = 1.1157（計画の閾値 1.5 との比較は analyst 判断事項として提示のみ）．
- throughput(m=204)/throughput(m=51) = 1.0104（m=204 が m=51 をわずかに上回る）．
- **実行時間スケーリングの観測（副次的だが重要な事実）**: measure=100 の所要時間は m=8: 281.4s（2.814s/step），
  m=51: 1605.9s 平均（16.06s/step），m=204: 6354.7s（63.55s/step）であった．step 時間の比は m=8→51 で 5.71 倍
  （m 比 6.375），m=51→204 で 3.96 倍（m 比 4.0）と，いずれも **`(p-1)/(m+p-1)` バブル式が予測する「段間オーバー
  ラップによる緩やかな短縮」ではなく，step あたりの所要時間が m にほぼ比例して増加するパターン**を示した．

**5. 分析フェーズへの申し送り**

- 上記 §4 の実行時間スケーリングの事実（time_per_step ∝ m にほぼ比例）は，`_process_microbatch` が m 個の
  マイクロバッチを段間オーバーラップなく逐次処理している可能性（バブル式 A-1 が前提とする「複数マイクロバッチが
  異なる段で同時に稼働する」状態になっていない可能性）を示唆する．集約スループット `microbatch_per_s` 自体は
  m とともに増加しているが，計画が予測した比率（m=51/m=8 で 3.6 倍）には遠く届かず（実測 1.12 倍），
  `### 検討・計画 (Iter7)` §4 の「定量整合（ratio≥1.5）」は満たしていない．一方「単調増加＋2σ超過」の条件は
  満たしている．analyst はこの中間的な結果（効果はある方向だが理論の予測より著しく小さい）を，成功条件・
  反証条件のいずれにも完全には当てはまらない事例として扱い，実行時間スケーリングの事実（オーバーラップ欠如の
  可能性）と合わせて判定すること．
- m=204 のみ `repeats=1`（他 2 水準は `repeats=3`）である点は，CV 比較の際に注意が必要（m=204 の測定誤差幅は
  不明．ただし pilot の 63.5s/step と本番の 63.55s/step が高精度で一致しており，測定自体の再現性は高いと推測
  される）．
- `results/Iter7.jsonl` は本フェーズで新規作成（7 レコード，全て `record_type=microbatch_bench`）．
- git commit/push はこのフェーズでは行っていない．

**6. クラスタの状態**

- 全 3 水準の deploy・bench・collect 完了後，env 未設定（bench 無効・`NUM_MICRO_BATCHES=4` 既定）で全 51 ノードを
  再 deploy し，`docker inspect` で `NUM_MICRO_BATCHES=4` かつ bench 系 env 未設定を確認，`healthcheck.py` で
  51/51 healthy を確認済み．**健全な serving 状態に復元済み**．

---

### 分析(解釈) (Iter7)

**担当**: 分析(解釈)フェーズ subagent（2026-07-20 JST）．`### 実験 (Iter7, 再実行)` の生データ（`results/Iter7.jsonl`
7 レコード）を検算し，成功条件・反証条件のどちらに近いかを，`pipeline_inference.py` のコード読解と合わせて分析した．
最終判定（採用/不採用）は reflector の役割のため，ここでは「事実としてどちらに近いか」「原因は何か」に留める．

**1. 集計値の検算（journal 記載値と完全一致）**

- `results/Iter7.jsonl` を直接読み，`microbatch_per_s` を再集計した．m=8: mean=2.8478，母 σ=0.0039，n=3．
  m=51: mean=3.1772，母 σ=0.0021，n=3．m=204: 3.2102，n=1．いずれも journal §4 の記載値と一致（不一致なし）．
- `microbatch_per_s = m × 100 / elapsed_s` を各レコードから逆算しても記録値と一致（m=8→2.8478，m=51→3.1772，
  m=204→3.2102）．比率も一致: throughput(m=51)/throughput(m=8)=1.1157，差=0.3294，throughput(m=204)/throughput(m=51)=1.0104．

**2. ノイズか有意かの判定**

- throughput(m=51)−throughput(m=8)=0.3294 は m=8/m=51 双方の 2σ（0.0078／0.0043）を約 40〜75 倍上回る．
  **単調増加は統計的に有意でありノイズではない**（成功条件 1 の「2σ 超過」は形式的に成立）．
- ただし変化の**大きさ**は 1.12 倍にすぎず，成功条件 2 の定量閾値 1.5 に遠く届かない．反証条件の平坦しきい値
  （ratio ≤ ~1.1）を辛うじて上回るのみで，計画が予測した 3.6 倍とは 1 桁近く乖離する．「ノイズではないが，
  理論が要求する効果量には達しない有意微増」と整理できる．

**3. 段間オーバーラップの有無（コードによる原因切り分け・確信度高）**

- **step 時間スケーリングの定量診断**: time_per_step は m=8:2.809s，m=51:16.052s，m=204:63.547s．線形回帰すると
  `time_per_step ≈ 0.31×m + 0.35`（1 マイクロバッチ当たり限界コスト ≈0.31s が m に依らずほぼ一定；区間傾き
  m=8→51 で 0.308，m=51→204 で 0.310 と一致）．
  - **オーバーラップ型モデル（GPipe/バブル式）は棄却される**: 理論では step_time=(m+p−1)×t_stage で t_stage は m に
    依らず一定のはず．実測から t_stage を逆算すると m=8:0.048s，m=51:0.159s，m=204:0.250s と m とともに増大し，
    一定にならない．つまり `(p−1)/(m+p−1)` バブル式が前提とする「t_stage 一定・fill/drain 償却」は成立していない．
  - **逐次型モデルが適合**: 限界コスト 0.31s/microbatch が一定（51 段全体の 1 貫通 ≈6ms/段）＝マイクロバッチが
    段間オーバーラップなく 1 本ずつ 51 段を貫通してから次が始まる挙動と整合する．
- **コード上の根拠**: bench 経路 `_pipeline_loop`（:1192 bench 分岐）→ `_run_microbatch_bench`（:1237-1262）は各 step で
  `for mb in range(num_micro_batches): self._process_microbatch(mb, ...)` と**マイクロバッチを同期・逐次ループ**する．
  `_process_microbatch`（:1019-1050）は各 mb について (A) `dist.recv`（:1024，blocking）→ (B) 全 my_layers を compute →
  (C) `dist.send`（:1048，blocking）を行い，**二重バッファリングも async 通信も無い**．async の `dist.irecv`/`wait()` は
  serving relay 経路（:1706-1813）にのみ存在し，bench 経路には一切使われていない（grep 確認済み）．したがって各 rank は
  mb を送出（blocking send）し終えるまで mb+1 の受信を開始できず，Gloo の blocking send/recv が段間の
  オーバーラップを構造的に潰す．**「複数マイクロバッチが異なる段で同時稼働する」状態は実装上発生しない**．
- **1.12 倍の微増の解釈**: 実測の微増は，バブル低減ではなく step 固定オーバーヘッド（≈0.35s／step，rank0 の
  乱数生成・`_reset_kv_cache_for_bench`・drain 等）が m 本のマイクロバッチに償却されることによる副次効果と
  考えられる（`microbatch_per_s = m/(0.31m+0.35)` が m→∞ で 1/0.31≈3.23 へ漸近；m=204 の 3.21 は既に漸近値近傍）．
  効果の向きは正だが，機構は計画の仮説（パイプライン充填）とは異なる．

**4. m=204 が n=1 であることの確信度への影響**

- 上記 3 の「オーバーラップ欠如」の結論は，主に n=3 の m=8・m=51 の step 時間（2.809s／16.052s）と
  `_process_microbatch` のコード構造に依拠しており，m=204 の値には依存しない．m=204 は「限界コスト一定」の
  傾向を 3 点目として補強するのみで，pilot（63.5s/step）と本番（63.55s/step）の高精度一致から測定再現性は
  高いと推測される．したがって n=1 は主要結論（機構・成功条件 2 未達）の確信度を実質的に下げない．
- ただし throughput(m=204)/throughput(m=51)=1.0104 という**微差そのもの**は，m=204 の σ が不明なため
  「m=204 が m=51 を有意に上回るか」までは断定できない（成功条件 2 後段「m=204 ≥ m=51」は点推定では成立するが
  誤差幅は未確認）．もっとも寛容にノイズ幅を見積もっても 1.01/1.12 を 1.5 に到達させることはできないため，
  この不確かさは全体判定を左右しない．

**5. 成功条件・反証条件のどちらに近いか（confidence 付き・最終判定は reflector）**

- **反証条件（このレバーは本ハードで無効＝収束・振り替え）に実質的に近い**と分析する．根拠: (1) 効果量 1.12 倍は
  平坦しきい値 ~1.1 の直上にすぎず，採用側閾値 1.5 に遠く未達．(2) コードと step 時間スケーリングの双方から，
  計画の仮説が依拠するバブル式の前提（段間オーバーラップ）が実装上・実測上いずれも成立していないことが確認された．
  (3) m を p の 4 倍（204）まで増やしても throughput は漸近値 ≈3.23 に張り付き，増分に対し latency のみが線形悪化する．
- ただし**厳密な反証条件（3 水準が互いに 2σ 内で平坦）そのものは満たさない**（単調増加は 2σ を大きく超え有意）．
  よって「成功条件 1 の一部（有意な単調増加）は満たすが，成功条件 2（効果量・機構）は満たさず，機構は仮説と異なる」
  という中間事例であり，形式条件の機械的当てはめでは決着しない．
- **confidence**: 「段間オーバーラップが起きていない（＝バブル式が本実装で成立しない）」はコード＋定量スケーリングの
  二重証拠により **高**．「レバーとして収束方向」は効果量が閾値の 1/1.5 未満である事実に基づき **中〜高**．
- **次フェーズ（reflector）への示唆**: 現行 bench 実装（blocking Gloo・逐次 mb ループ・オーバーラップ無し）の下では
  `NUM_MICRO_BATCHES` は集約スループットに実効的に効かず，**このレバーは収束と扱い次候補 `STAGGER_INTERVAL` へ
  振り替える**のが妥当と考えられる．追加反復（m=204 の repeats 補完）は，主要結論を覆さない見込みのためコスト対効果が
  低い．一方，もし「バブル式が成り立つか」を本気で問うなら，レバー変更ではなく `_process_microbatch` を async
  `isend`/`irecv`＋二重バッファ化する**実装変更**（serving 経路の :1706-1813 に既存する irecv パターンの bench 転用）が
  前提となる——これは単一レバーの範囲を超え，可逆性・スコープを reflector/planner が別途判断する必要がある（断定は避ける）．

---

### 実装 (Iter7, 差し戻し後)

**担当**: 実装フェーズ subagent（2026-07-19 JST）．backlog.md B13（`[auto-decided 2026-07-19]`）の判断に従い，
単一レバー `NUM_MICRO_BATCHES` 自体は変更せず，`### 実験 (Iter7)` が発見したバグ A・B のみを修正した．
実機クラスタへの接続・deploy・SSH は一切行っていない（コード編集とローカル `uv run pytest tests/` のみ）．

**1. バグ A（`position_ids` 欠落によるクラッシュ）の修正**

- `_process_microbatch`（pipeline_inference.py :1029-1035 付近）で，`hidden_state.shape[1]` から
  `_seq_len` を取り出し，`position_ids = torch.arange(mb * _seq_len, (mb + 1) * _seq_len,
  dtype=torch.long).unsqueeze(0)` を構築，`is_first = (mb == 0)` を添えて
  `layer(hidden_state, position_ids=position_ids, is_first=is_first)` と明示的にキーワード渡しする形に
  変更した（旧: `layer(hidden_state)`）．
- **設計判断（単調増加 vs 固定値）**: 「単調増加」を採用した．理由は，バグ B の対処（後述）で
  `_run_microbatch_bench` が各ステップ冒頭に KV キャッシュと write_pos をゼロリセットするため，
  1 ステップ内では `_process_microbatch` が `mb=0,1,...,num_micro_batches-1` の順に呼ばれるたびに
  write_pos が `0, seq_len, 2*seq_len, ...` と累積していく（serving 経路のトークン逐次生成と同じ
  「cache 書き込み位置＝RoPE 位置」の対応関係）．position_ids を固定値（例えば全 mb で 0）にすると，
  この対応が崩れ（sliding window マスクの `_row`/`_col` 比較や `_apply_rope` の絶対位置が cache の
  実書き込み位置と食い違う），bench が計測する attention コストが実際の書き込みパターンと乖離し，
  「定常状態のスループット測定」という bench の意図と矛盾する．単調増加はこの対応を厳密に保ち，かつ
  serving 経路（:1719, :1726 等の `positions = torch.arange(recv_seq_len, ...)` パターン）と一貫した
  「絶対位置」の扱いになるため，実装上も自然（追加の特別扱いが不要）と判断した．

**2. バグ B（KV キャッシュ write_pos オーバーフロー）の修正**

- 対応案は 2 択（申し送り (b)）のうち，**「bench 専用に各ステップ前に KV キャッシュ本体と write_pos を
  ゼロリセットする」**（`_reset_kv_cache_for_bench` 新設メソッド，pipeline_inference.py :971-995）を採用した．
  理由: (1) `_broadcast_prompt_and_wait`（実リクエスト開始時のリセット，:1514-1522 付近）が既に
  「kv_cache 本体と `_kv_cache_write_pos_ref` を揃えてゼロクリアする」同一イディオムを持っており，
  それを bench 経路にも適用するだけで済み実装が最小差分になる．(2) 「kv_cache 書き込み自体をバイパス」
  する案は，`_process_microbatch`／layer.forward 内部（バグ A 修正後も維持している既存の cache 書き込み
  コードパス）に bench 専用の分岐を新設する必要があり，serving 経路と bench 経路で forward の挙動が
  分岐する（＝改変範囲が `_build_transformer_layer` 内部に及ぶ）ため，最小差分の原則に反すると判断した．
- `_run_microbatch_bench`（新設，:1221-1275 付近）は warmup・measure 双方のループで，各ステップの
  `_process_microbatch` 呼び出し前に必ず `self._reset_kv_cache_for_bench()` を呼ぶ．これにより，
  1 ステップ内で write_pos が到達しうる最大値は構造的に `num_micro_batches * seq_len` に収まり，
  `repeats × (warmup+measure)` がいくら大きくても（本番設定 `MICROBATCH_BENCH_STEPS=100` で
  `num_micro_batches=204` の場合でも既定 `SEQ_LEN=1` なら `204*1=204 ≪ max_gen_tokens=2048`）
  write_pos が `max_gen_tokens` を跨がない．
- **副次確認（コードレビュー，bench 計算量の定常性）**: 毎ステップリセットにより，非 sliding 層の
  attention 計算量（`cache_end` に比例）もステップを跨いで同一パターンで再現されるため，
  「定常状態のスループット計測」という bench の前提とも整合する（journal.md `### 実験 (Iter7)` が
  指摘した「計算量がステップごとに単調に伸びる」問題も同時に解消）．

**3. 再発防止テスト（新規）**

- `tests/test_pipeline_microbatch_bugfix.py`（新設，5 ケース）を追加した．`FullyOptimizedPipelineNode.__init__`
  は分散プロセスグループ初期化・safetensors 読み込みを要するため呼ばず，`object.__new__` で最小構成の
  単一ノード（`prev_rank=next_rank=None` で `dist.recv`/`dist.send` を経由しない）を組み立て，実 forward の
  シグネチャ（`position_ids` に既定値なし）と KV キャッシュ書き込みパターン（`write_pos:write_pos+sl`）だけを
  模した `fake_layer` で検証する．
  - バグ A: `position_ids` を渡さないと `TypeError` になるシグネチャに対し，`_process_microbatch` が
    実際に `TypeError` を起こさず呼べること／`position_ids` が `mb*seq_len` を起点に正しくオフセットする
    ことを検証（2 ケース）．
  - バグ B: (i) reset を挟まずに `_process_microbatch` を連続呼び出しすると `max_gen_tokens` を超えて
    実際に `RuntimeError`（shape 不一致）になること（対処前の挙動の再現，1 ケース），(ii)
    `_reset_kv_cache_for_bench` が KV キャッシュ本体・write_pos の双方をゼロへ戻すこと（1 ケース），(iii)
    `_run_microbatch_bench` が `max_gen_tokens` ちょうどの境界値・多数ステップでも例外にならず完走し，
    各呼び出し直前の write_pos が常に `mb*seq_len` の範囲に収まること（1 ケース）．
- `tests/conftest.py` にリポジトリルートを `sys.path` へ追加する行を 1 行追加した（`pipeline_inference.py` は
  リポジトリ直下にあり，既存の `tools/` 追加行だけでは import できないため．最小差分）．
- `uv run pytest tests/` 実行結果: **93 passed**（既存 88 + 新規 5，回帰なし）．

**4. 完了条件チェック**

- serving ホットパス（`process_pipeline_inference`／`_broadcast_prompt_and_wait`／`_relay_request` 系）は
  **非改変**（`git diff pipeline_inference.py` の全 hunk が bench 用定数・`_process_microbatch`・
  `_reset_kv_cache_for_bench`・`_pipeline_loop`・`_run_microbatch_bench`・`main()` の bench ゲートのみに
  限定されることを diff で確認済み）．`MICROBATCH_BENCH_STEPS=0`（既定）での挙動は変更していない
  （bench ゲート自体は前フェーズで実装済みの `if microbatch_bench_steps > 0:` のまま）．
- 単一レバー原則: `NUM_MICRO_BATCHES` の値・既定・levers 定義は一切変更していない．
- `uv run pytest tests/` 93 passed（回帰なし，新規再発防止テスト green）．
- `tools/common.py`／`tools/deploy.py`（bench env 転送修正）は `### 実験 (Iter7)` が既に自律修正・検証済みの
  ため本フェーズでは触れていない（journal 記載のとおり残置）．

**5. 実験フェーズへの申し送り（再掃引の手順）**

- バグ A・B は修正済みのため，`### 実験 (Iter7)` §5 で打ち切った m=8 パイロット（`MICROBATCH_BENCH_STEPS=5,
  MICROBATCH_BENCH_WARMUP_STEPS=2`）から**再開してよい**．まず m=8 の小規模パイロットで実クラッシュが
  再発しないことを確認してから，`### 検討・計画 (Iter7)` §2 の本掃引（m∈{8,51,204}，
  `MICROBATCH_BENCH_STEPS=100`，warmup/repeats は既定 20/3）に進むこと．
- `_process_microbatch` の `dist.recv`/`dist.send` の例外握り潰し（バグ A の副作用として発見された，
  通信断を隠蔽する設計の弱点）は本 Iteration のスコープ外のまま（journal 既存の将来課題として維持．
  今回も変更していない）．m=8 パイロット時，rank クラッシュ時にこの握り潰しが下流 rank の異常検知を
  妨げないか，pilot 実行中はログを注視すること．
- pre-flight 確認事項（m=204 の通信バッファ RAM 見積もり等）は `### 実験 (Iter7)` §4 で既に解消済み
  （再確認不要）．

---

### 実験 (Iter7)

**担当**: 実験フェーズ subagent（2026-07-19T17:05〜17:26 JST，約 21 分）．`### 実装 (Iter7)` §5 の手順に従い
m=8 でのパイロット（`MICROBATCH_BENCH_STEPS=5`, `MICROBATCH_BENCH_WARMUP_STEPS=2`）を実行しようとしたが，
**2 件のブロッキング実装バグを発見し，3 水準の本掃引には進めなかった**．実機 51 ノードは現在健全な
serving 状態（bench 無効・`NUM_MICRO_BATCHES=4` 既定）に復元済み．

**1. 事前に発見・修正したデプロイ経路の欠落（可逆・低リスクと判断し自律修正）**

- `tools/deploy.py` の `docker run` コマンドは `NUM_MICRO_BATCHES`/`STAGGER_INTERVAL` は `-e` 転送していたが，
  Iter7 実装で追加された `MICROBATCH_BENCH_STEPS`/`MICROBATCH_BENCH_WARMUP_STEPS`/`MICROBATCH_BENCH_REPEATS`
  は**一切コンテナへ転送されていなかった**（deploy 実行元シェルで export しても無効化されたまま＝
  `MICROBATCH_BENCH_STEPS` 既定 "0" が常にコンテナに渡り bench は起動しない）．`tools/common.py`
  `ClusterConfig` に 3 フィールドを追加し，`tools/deploy.py::deploy_single_node` の `docker run` へ
  未設定時は `-e` 行ごと省略する形で条件付き転送する変更を加えた（`git diff` で確認可能，2 ファイルのみ）．
  `uv run pytest tests/` 88 passed（回帰なし）．**この修正がないと bench は原理的に一度も起動しないため，
  実験を進めるための必須の前提修正**として自律判断で実施した（可逆・deploy スクリプトの追加のみ・
  serving ホットパス非改変）．

**2. パイロット実行（m=8, MICROBATCH_BENCH_STEPS=5）で発見したブロッキングバグ**

- 51 ノード健全性確認 → `NUM_MICRO_BATCHES=8 MICROBATCH_BENCH_STEPS=5 MICROBATCH_BENCH_WARMUP_STEPS=2 mise run
  deploy` 実行（deploy 自体は 54 秒で完了，51/51 成功）．
- **バグ A（クラッシュ）**: 実レイヤーを 1 枚以上持つ rank（rank1 で確認）が bench 開始直後に例外で fatal crash：
  ```
  TypeError: FullyOptimizedPipelineNode._build_transformer_layer.<locals>.forward()
  missing 1 required positional argument: 'position_ids'
  ```
  原因: `_process_microbatch`（pipeline_inference.py:995 付近）が `layer(hidden_state)` を **位置引数
  `position_ids` なしで**呼び出しているが，`_build_transformer_layer` が返す `forward`（:829）の実シグネチャは
  `forward(hidden_state, position_ids, is_first=True)` で `position_ids` に既定値が無い．serving 経路
  （:1682, :1788）は常に `layer(hidden_state, position_ids=positions, is_first=is_first)` と呼んでおり，
  `_process_microbatch` はこの引数追加に追従していなかった（既存のデッドコードのバグで，Iter7 が初めて
  このパスを実行して顕在化させた）．
- **バグ A の副作用（サイレント伝播）**: `_process_microbatch` の `dist.recv`/`dist.send` は
  `except Exception: return`/`pass` で例外を握りつぶす設計のため，rank1 がクラッシュ＆再起動（restart
  policy）した後，**下流 rank は通信断を検知できず**，rank0/rank2 のように compute 0 層 or 通信断が
  即座に return するランクは 21 ステップを「正常完了」したように見えてしまう（実際は乱数を右へ受け流した
  だけで実計算をしていない）．一方 rank3 以降の実レイヤー保持 rank は `dist.recv` がブロックしたまま
  停止（`GLOO_SOCKET_TIMEOUT_MS=3600000`＝1 時間のタイムアウト待ち）し，6 分以上ログが進行しなかった．
  → **この完了 vs ハングの非対称性自体が，bench 分岐の結果を無条件に信用してはいけないことを示す**
  （最終 rank が RESULT を出したとしても，途中区間の通信が実際に成立していたかは別途要検証）．
- **バグ B（構造的・未クラッシュ確認だがコードレビューで確定）**: `_process_microbatch` が使う KV キャッシュの
  `write_pos`（:879, `_kv_cache_write_pos[layer_idx] += _sl`）は**マイクロバッチ間で共有**され，かつ
  bench の各 repeat/step 間でリセットされない．`KV-cache initialized: ... max_gen_tokens=2048` に対し，
  1 回の bench 呼び出し総数は `repeats × (warmup+measure) × num_micro_batches` 回（本番設定
  `MICROBATCH_BENCH_STEPS=100` なら m=8 で `3×120×8=2880`，m=51/204 はさらに大きい）で，**いずれも 2048 を
  超え** `key_cache[:, :, write_pos:write_pos+1, :] = k` の代入が範囲外スライスで shape 不一致エラーになる
  見込み（バグ A を直しても本番設定では別クラッシュに至る）．さらに range 内でも非 sliding 層の attention は
  `cache_end`（=蓄積済み位置）に比例して計算量が増えるため，bench 中の 1 ステップの所要時間が単調に伸びる
  ＝「定常状態のスループット」という前提そのものと矛盾する（warmup で吸収できない）．

**3. 対応**

- パイロットを打ち切り，bench 無効（`MICROBATCH_BENCH_STEPS` 未設定＝既定 0）で全 51 ノードを再 deploy し，
  健全な serving 状態（`micro_batches=4` 既定）へ復元済み（`mise run status`＝51/51 healthy を確認）．
  **m=51/204 の本掃引は実施していない**（バグ A がある限り実レイヤー保持 rank は必ず同じ形でクラッシュ／
  ハングするため，先に進めても同じ結果になると判断し，時間を消費しなかった）．
  M=204 の RAM 事前懸念（`### 実装 (Iter7)` §5）は，`BATCH_SIZE`/`SEQ_LEN` がいずれも deploy 経路で未設定
  （コンテナ内既定 "1"）であることをコードで確認し，通信バッファは `hidden_size(5376) × 4B × 2 × m` で
  m=204 でも約 8.4 MB（実測 rank50 ログ `Comm buffers allocated: 0.33 MB` は m=8 分．m=204 相当は 51 倍
  ≈16.9MB）と算出，RAM リスクは実質無い（懸念は解消，別途要確認は不要）．
- バグ A・B は `_process_microbatch`/`_run_microbatch_bench`（D-1a で「生きている」と判断されたコード）の
  **domain 知識（RoPE position_ids の意味・KV キャッシュ容量設計）を要する修正**であり，deploy.py の env
  転送漏れ（機械的な配線ミス）とは性質が異なると判断し，本フェーズでは着手しなかった（実験フェーズの
  役割を超えるため）．`results/Iter7.jsonl` は 0 件のまま（record_type=microbatch_bench のレコードは
  1 件も取得できていない）．

**4. 完了条件チェック（`### 検討・計画 (Iter7)` §4）との対比**

- (i) `results/Iter7.jsonl` への構造化保存: **未達成**（0 件．バグ A/B により bench が実測データを生成しない）．
- (ii)/(iii)（テスト green・env 既定での非改変）: 実装フェーズ時点の確認は有効（本フェーズでは変更していない）．
  ただし deploy.py の修正後も `uv run pytest tests/` 88 passed を再確認済み．

**5. 次フェーズへの申し送り**

- rc-planner/rc-implementer での再検討が必要な事項:
  (a) `_process_microbatch` の `layer(hidden_state)` 呼び出しに `position_ids`（と，必要なら `is_first`）を
  明示的に渡す修正（RoPE 用に単調増加または固定値のいずれが steady-state 測定として妥当かは設計判断）．
  (b) bench 中の KV キャッシュ書き込み位置が `max_gen_tokens` を超えないための対処（例: bench 専用に
  write_pos を各 step 前にリセットする，または `_process_microbatch` の bench 経路では kv_cache 書き込み
  自体をバイパスして「毎回同じ固定長キャッシュに対する attention」に固定し，計算量を一定に保つ）．
  (c) `_process_microbatch` の recv/send 例外握り潰しが，通信断を「正常完了」に見せかける問題（bench の
  結果の信頼性に関わる．最終 rank の RESULT が出ても，途中区間が実際に通信できていた保証がない）．
- 本フェーズの副産物として `tools/common.py`/`tools/deploy.py` の bench env 転送修正は残置（次回の bench
  再挑戦時に必要）．git diff 未コミット（このフェーズでは commit/push を行っていない．次フェーズ以降の
  判断に委ねる）．

---

### 実装 (Iter7)

**担当**: 実装フェーズ subagent（2026-07-19 JST）．`### 検討・計画 (Iter7)` §2「変更ファイル・設定キー」に従い，
**単一レバー `NUM_MICRO_BATCHES` のスループット感度分析用 bench モード**を実装した．実機クラスタへの接続・deploy・
SSH・推論実行は一切行っていない（コード編集とローカル `pytest` のみ）．

**1. `pipeline_inference.py` の変更**

- `DEFAULT_MICROBATCH_BENCH_WARMUP=20`／`DEFAULT_MICROBATCH_BENCH_MEASURE=100`／`DEFAULT_MICROBATCH_BENCH_REPEATS=3`
  を `DEFAULT_NUM_MICRO_BATCHES` 近傍（旧 :63 近傍）に追加．
- `_pipeline_loop()`: `warmup_steps`/`measure_steps`/`repeats`（すべて既定 `None`）を引数化．3 つとも非 `None` の
  ときのみ bench 分岐（`_run_microbatch_bench` 新設）へ入り，従来の無限ループ（`while not _shutdown_requested and
  not _pipeline_stopped`）は else 節としてそのまま温存．bench 分岐は `repeats` 回，各回 `warmup_steps` を捨てたあと
  `measure_steps` の wall-clock を計測し，**最終 rank（`next_rank is None`）でのみ**
  `[R{rank} RESULT] MICROBATCH_BENCH m=... p=... warmup=... measure=... elapsed_s=... steps_per_s=... microbatch_per_s=...`
  を出力する（既存 `_log`/RESULT 形式に準拠）．
- `main()`: 新 env `MICROBATCH_BENCH_STEPS`（既定 `"0"`）を読み，`int(...) > 0` のときのみ `node._pipeline_loop(
  warmup_steps=..., measure_steps=microbatch_bench_steps, repeats=...)` を**同期的に**実行してから
  `process_pipeline_inference()` へフォールスルーする分岐を追加．**既定値 0 では if 分岐自体が実行されず，
  `process_pipeline_inference()` の呼び出しは変更前と完全に同一**（可逆性の担保．コードレビューで確認済み，
  該当 diff は `if microbatch_bench_steps > 0:` ブロックの追加のみで既存行の変更なし）．
- **計画からの実装判断（申し送り）**: 計画は `MICROBATCH_BENCH_STEPS` を「bench の有効化ゲート」とのみ記述し，
  warmup/measure/repeats の env 分割は明記していなかった．env サーフェスを最小に保つため，**`MICROBATCH_BENCH_STEPS`
  自体を `measure_steps` としても兼用**し（0 で無効化，>0 の値がそのまま計測ステップ数になる），warmup/repeats は
  追加の任意 env（`MICROBATCH_BENCH_WARMUP_STEPS`／`MICROBATCH_BENCH_REPEATS`，既定は上記定数）で上書き可能にした．
  `DEFAULT_MICROBATCH_BENCH_MEASURE`（100）はこの設計では直接コードから参照されず，operator 向けの推奨値ドキュメント
  （`MICROBATCH_BENCH_STEPS=100` を使う場合の目安）としてコメントに残した．実験フェーズは
  `MICROBATCH_BENCH_STEPS=100`（推奨）を基本に，`m∈{8,51,204}` ごとに `NUM_MICRO_BATCHES=m` と併せて設定すること．

**2. `tools/collect_results.py` の変更**

- `_MICROBATCH_BENCH_RE` を追加し，`MicrobatchBenchRecord`（dataclass）・`parse_microbatch_bench_log`（純関数．
  `[R{rank} RESULT] MICROBATCH_BENCH ...` 行を全件抽出，1 行 = 1 計測窓）・`build_microbatch_bench_record`（JSONL
  レコード組み立て．`record_type="microbatch_bench"` で通常の serving レコードと区別）を新設．
- オーケストレーション: `collect_last_rank_log`（`world_size - 1` の host から `docker logs` を SSH 取得．
  `MICROBATCH_BENCH` は最終 rank でのみ出力されるため rank0 ではなくこちらを対象にする）・
  `run_microbatch_bench_collect`（ログ取得 → 全計測窓パース → レコード化，プロンプト送信は行わない）を追加．
- CLI: `main()` に `--microbatch-bench`（プロンプト送信をスキップし bench 収集のみ行う）・`--since`（`docker logs
  --since` を絞り込む任意 ISO8601．省略時はコンテナの全ログを取得するため，**同一デプロイに対して複数回 collect
  すると `MICROBATCH_BENCH` 行が重複記録され得る**点を `--help` とコード内コメントに明記．実験フェーズは m ごとに
  再 deploy する運用のため実害は小さいが，同一 deploy で複数回 collect する場合は `--since` を使うこと）．

**3. テスト**

- `tests/fixtures/microbatch_bench_sample.log`（実 ESC バイト付き ANSI 混入 2 行＋非混入 1 行の RESULT，他 rank・
  非 RESULT 行のノイズを含む）を新設．
- `tests/test_microbatch_bench.py`（5 ケース）: `parse_microbatch_bench_log` の全件抽出・フィールド正確性・ANSI
  有無両対応・0 件時の空リスト，`build_microbatch_bench_record` の必須フィールド網羅．
- `uv run pytest tests/` 実行結果: **88 passed**（既存 83 件 + 新規 5 件，回帰なし）．

**4. 完了条件チェック（`### 検討・計画 (Iter7)` §4「完了条件」）**

- (i) `results/Iter7.jsonl` への構造化保存: 実装済み（`--microbatch-bench` 実行で `record_type=microbatch_bench`
  レコードが 1 計測窓 = 1 行で追記される）．**実データはまだ無い**（実機 deploy 未実施，これは実験フェーズの担当）．
- (ii) 新パーサ単体テスト green・既存回帰なし: **満たした**（88 passed）．
- (iii) `MICROBATCH_BENCH_STEPS=0`（既定）で現行 serving 挙動を変えない: **コードレビューで確認**（`main()` の
  追加分岐は `if microbatch_bench_steps > 0:` の中にのみ新規コードがあり，既定 `"0"` では従来どおり
  `node.process_pipeline_inference()` のみが呼ばれる．`_pipeline_loop()` も引数 3 つとも `None` のとき else 節で
  従来の無限ループ実装を一字一句保持）．serving ホットパス（`process_pipeline_inference`／`_broadcast_prompt_and_wait`／
  `_relay_request` 系）は非改変（diff に含まれない）．

**5. 実験フェーズへの申し送り（env 設定・deploy 手順の要点）**

- 各 `m∈{8,51,204}` について，deploy 時の env に `NUM_MICRO_BATCHES=m` と `MICROBATCH_BENCH_STEPS=100`（推奨．
  warmup/repeats は既定 20/3 のままでよい）を設定し `mise run deploy` する．`predict:demo` の実行は不要（bench は
  コンテナ起動時に自動実行される）．
- 収集は `uv run python tools/collect_results.py --iter Iter7 --microbatch-bench` を各 deploy 後に 1 回実行する
  （最終 rank ＝ `WORLD_SIZE - 1` の docker logs を SSH 取得しパースする．`WORLD_SIZE=51` 固定なら rank50）．
- **要 pre-flight 確認（`### 検討・計画 (Iter7)` §5 のリスク）**: `m=204` はバッファを既定比 51 倍確保する
  （`_allocate_communication_buffers`）．worker ノードの RAM で収まるか deploy 前に見積もること．OOM の兆候があれば
  `m=204` を外し `{8, 51}` の 2 水準に縮退する．

---

### 検討・計画 (Iter7)

**担当**: 計画フェーズ subagent（2026-07-19 JST）．`### 調査 (Iter7)` の結論（A-2 の 51 段バブル数値・B-2 主指標の差し替え・
C-1〜C-3 のデッドコード事実・D-1a/b/c の 3 択・D-2〜D-5 の申し送り）を精読し，単一レバー **`NUM_MICRO_BATCHES`
（research_frontier② のスループット感度）**の実験を実装可能な粒度へ落とし込んだ．本フェーズは実機クラスタへの接続・
deploy・推論を一切行わない（`pipeline_inference.py` のコード読み取りのみ）．**確認したコード事実**: `main`（:2001-2032）が
呼ぶのは `process_pipeline_inference`（relay serving 経路）のみで，`num_micro_batches` を実消費する `_pipeline_loop`
（:1109-1147）→`_process_microbatch`（:963-1002）は serving 経路から起動されない＝**現状デッドコード**．`_process_microbatch` は
乱数入力（`recv_buffers[mb].normal_`, :976）で，`_relay_active` 中は即 return（:970-972）．バッファは `num_micro_batches` 本
線形確保（:609-624）．`_pipeline_stopped` は定義（:101）と参照（:1127）のみで真になる箇所は無い．

#### 0. 採用する案と選定理由（D-1a を選択）

調査 D-1a/b/c のうち **(案 D-1a: 生きている `_pipeline_loop` を明示起動し，乱数パイプラインの集約スループットが m で
どう変わるかを測る)** を採用する．理由: (1) **可逆・低リスク**（serving 経路 `process_pipeline_inference`/`_relay_request` を
一切改変せず，現状デッドコードの `_pipeline_loop` を env ゲートで明示起動するだけ．env 既定では現行挙動と完全に同一）．
(2) **バブル式 A-1 `(p−1)/(m+p−1)` が実 51 段 Gloo/CPU 上で成り立つかの独立した検証価値**があり，②（マイクロバッチ数の
スループット感度）という当初目的そのものに合致する．(3) **B12 が想定した「過大なら振り替え」の分岐に忠実**——(案 D-1b:
relay の in-flight batching ホットパス改変) は SL3/B9 隣接の不可逆・大規模で `[needs-human]` 送りになるため今回は選ばない．
(案 D-1c: SEQ_LEN/STAGGER への振り替え) は②の感度分析という目的から外れるため選ばない．D-1a は単一レバー原則に最も忠実．

#### 1. 仮説

51 段（p=WORLD_SIZE=51）の実 Gloo/CPU パイプラインで，乱数入力の `_pipeline_loop` を回して**集約マイクロバッチ・
スループット（microbatch/sec ＝ 最終 rank が単位時間に完了させる隠れ状態テンソル数）**を測ると，m を増やすほどパイプラインの
fill/drain バブルが償却されてスループットが上がるはずである（GPipe/Megatron の効率 `1 − bubble = m/(m+p−1)`）．p=51 での
予測効率は **m=8→0.138／m=51→0.505／m=204→0.803**（対応バブル 86.2%／49.5%／19.7%）．したがって理論が実機で成り立てば
**throughput(m=51) は throughput(m=8) の約 3.6 倍，throughput(m=204) は約 5.8 倍**へ単調増加する（compute-ceiling `1/t_stage`
へ漸近）．逆に，Gloo の blocking send/recv が段間オーバーラップを潰す，または CPU 計算律速（Iter4，ITL の 92% が compute）で
早々に飽和する場合は，m を振っても throughput がノイズ内で**平坦**になる．前者なら②のレバーは意味を持ち（採用方向），
後者なら本ハードでは `NUM_MICRO_BATCHES` は集約スループットにも効かない（収束＝次レバーへ振り替え）と判定できる．

#### 2. 単一レバー・変更内容

**単一レバー**: **`NUM_MICRO_BATCHES` の値のみ**を `{8, 51, 204}` の 3 水準で振る．他は直近最良／既定に固定する（下記 §3）．

- **レバー値の選定（過去値と非重複）**: config の既定候補 `[2, 4, 8]` は 51 段には桁不足（A-2）で，かつ serving 経路では
  一度も実際に掃引されていない（C-2，デッドコードのため）．**`{8, 51, 204}`＝{旧 config 上限, p, 4p}** に拡張する．8 を
  低アンカー（旧 config 上限と重複させ連続性を担保），51=p（バブル 50%），204=4p（GPipe「ほぼ無視可」域）とし，予測効率が
  0.138 : 0.505 : 0.803 と大きく開くため，理論の成否をノイズ上で明瞭に弁別できる．いずれも過去反復で未掃引の新規値．

- **主指標の差し替え（D-2 の申し送りを反映）**: 主指標を Iter6/B6 が指定していた単発 ITL/TTFT から
  **集約マイクロバッチ・スループット `microbatch_per_s = m × measure_steps / elapsed_s`（最終 rank，warmup 除外区間）**へ
  更新する．乱数パイプラインにはトークンが無いため，D-2 が求めた「集約 tokens/sec」の**乱数入力アナログ**が microbatch/sec で
  ある（各 microbatch＝shape (batch, seq_len, hidden) の隠れ状態が 51 段を貫通する 1 単位）．単発 ITL はバブル充填で原理的に
  下がらない（B-2）ため主指標にしない．副指標: `steps_per_s`，および含意 `t_stage = elapsed_s / measure_steps / (m + p − 1)`
  （compute-ceiling 整合の確認用，D-4）．

**変更ファイル・設定キー（rc-implementer 向け・具体箇所）**:

- `pipeline_inference.py`:
  - `main()`（:2025-2032）: 新 env `MICROBATCH_BENCH_STEPS`（int, 既定 0＝無効）が >0 のとき，`process_pipeline_inference()`
    の代わりに（または前段で）全 rank が `_pipeline_loop` をバウンド実行する bench 分岐を追加する．**既定 0 では現行挙動と
    完全に同一**（可逆性の担保）．bench 完了後は結果行をログに出し，コンテナを健全に保つため `process_pipeline_inference()`
    へフォールスルー（idle serving）してログ収集可能な状態を維持する案を推奨（rc-implementer 判断可）．
  - `_pipeline_loop()`（:1109-1147）: 現在は `_shutdown_requested/_pipeline_stopped` まで無限ループ．**`warmup_steps`・
    `measure_steps`・`repeats` を引数（既定 None＝現行の無限ループを保持）として追加**し，bench 時は「warmup 区間を捨て →
    measure 区間の wall-clock を計測」を `repeats` 回繰り返す．**最終 rank（`next_rank is None`）でのみ**，各 measure 窓ごとに
    構造化行 `[R{rank} RESULT] MICROBATCH_BENCH m={m} p={world_size} warmup={W} measure={M} elapsed_s={t} steps_per_s={M/t}
    microbatch_per_s={m*M/t}` を出力する（既存の `[R{rank} LEVEL] message` 形式・RESULT ブロック規約に沿わせる）．全 rank が
    同一 env から同一 `warmup/measure/repeats` を読むためロックステップは blocking send/recv で保たれる（recv は timeout で
    自己回復，:978-982）．
  - 新定数（マジックナンバー回避・CLAUDE.md 準拠）: `DEFAULT_MICROBATCH_BENCH_WARMUP`（例 20）・`_MEASURE`（例 100）・
    `_REPEATS`（例 3）を `DEFAULT_NUM_MICRO_BATCHES`（:63）近傍に定義．env 読み取りは `PipelineConfig`（:294 近傍）または
    `main` 内で行う．
- `tools/collect_results.py`: `MICROBATCH_BENCH` RESULT 行をパースして **`results/Iter7.jsonl`** へ追記する処理を追加
  （フィールド: `num_micro_batches, world_size, warmup_steps, measure_steps, elapsed_s, steps_per_s, microbatch_per_s, rank`）．
  既存の `--stage-timing`（B7 で追加した並列 SSH `docker logs` 取得）で全 rank ログを引ける前提で，最終 rank の
  `MICROBATCH_BENCH` 行を拾う．純関数パーサの単体テストを `tests/` に追加（回帰なしを確認）．
- serving ホットパス（`process_pipeline_inference`・`_broadcast_prompt_and_wait`・`_relay_request` 系）は**非改変**．

**掃引手順（実験フェーズ用）**: 各 m∈{8,51,204} について，env `NUM_MICRO_BATCHES=m` と `MICROBATCH_BENCH_STEPS>0`（＋
warmup/measure/repeats）で `mise run deploy` → コンテナ起動時に bench が自動実行 → `tools/collect_results.py --iter Iter7
--stage-timing` で最終 rank の結果行を回収．**`predict:demo` の HTTP プロンプト投入は bench では不要**なため，B6(i) 申し送りの
`mise.toml` `predict:demo` の `--iter Iter1` ハードコード問題を回避できる（`collect_results.py --iter Iter7` を直接呼ぶ）．
各 m で bench は `repeats(≥3)` の measure 窓を出すので **1 deploy あたり n≥3 反復**が得られ（B6(iv) 充足），冷開始（再デプロイ後の
プロセスグループ再初期化）は warmup 区間で除外する（B6(ii) 充足）．

#### 3. 固定する構成（直近最良／既定に固定）

- `WORLD_SIZE=51`（p=51 固定．段数を変えるとバブルは m/p 比で決まり第 2 レバーになる＝単一レバー原則違反，A-2）．
- `STAGGER_INTERVAL`＝既定 3.0（起動時 thundering herd 用で bench 定常スループットに寄与せず，固定）．
- `SEQ_LEN`・`BATCH_SIZE`＝現行既定（バッファ shape を固定して m のみを独立変数に保つ）．モデル・重み・`COMPUTE_DTYPE` 不変．
- serving 経路・relay プロトコル不変（本 bench は乱数パイプラインのみを測り，実プロンプト serving は変えない）．

#### 4. 期待効果・成功条件（measurable）

- **ノイズ幅の見積もり**: 各 m で `repeats≥3` の measure 窓から `microbatch_per_s` の平均と標準偏差 σ（相対 CV）を出す．
  過去反復の交絡（冷開始 348s・Iter3 の ttft 81.6s 突出）は warmup 除外で排除済みのため，ここでのノイズは定常区間の
  step 時間ばらつきのみ．CV は未実測だが，仮に 20〜30% でも予測分離（3.6 倍）を優に下回る．**ノイズ band ≡ 2σ**．
- **成功条件（②の感度が実在＝レバー採用方向）**:
  1. `microbatch_per_s` が m について**単調増加**し，かつ **throughput(m=51) − throughput(m=8) > 2σ**（ノイズを超えて増加）．
  2. 定量整合: **throughput(m=51)/throughput(m=8) ≥ 1.5**（予測 3.6 倍に対し保守的閾値．1（平坦＝無効果）から明瞭に離れることを
     要求）．さらに throughput(m=204) ≥ throughput(m=51)（compute-ceiling への漸近・単調性）．
  → この場合，バブル式 A-1 が実 51 段でも成立＝`NUM_MICRO_BATCHES` は集約スループットに効くレバーだと確認（採用方向）．
     ただし D-4 の CPU 計算律速の天井（全段常時稼働時の最遅段計算時間 `1/t_stage`）で頭打ちする現実値として報告する
     （段数倍の理論上限をそのまま期待効果とはしない）．
- **反証条件（②のレバーは本ハードで無効＝収束・振り替え方向）**:
  - 3 水準の `microbatch_per_s` が互いに 2σ 内で**平坦**（throughput(m=51)/throughput(m=8) ≤ ~1.1）．
  → Gloo blocking の段間非オーバーラップ，または CPU 計算律速の早期飽和により，本ハードでは `NUM_MICRO_BATCHES` は
     集約スループットにも効かないと判定．**このレバーは収束**とし，次は config `levers` の次候補 `STAGGER_INTERVAL`（②の
     stagger 側）へ振り替える（B12 の「過大／無効なら振り替え」分岐に合致）．
- **完了条件（実装・実験が満たすべき最低限）**: (i) `results/Iter7.jsonl` に 3 水準 × repeats≥3 の `microbatch_per_s` が
  構造化保存される，(ii) `tools/collect_results.py` の新パーサ単体テストが green（既存回帰なし），(iii) bench 分岐が env 既定
  （`MICROBATCH_BENCH_STEPS=0`）で現行 serving 挙動を変えないことをコード上で担保．

#### 5. 人間判断の要否・リスク

- **自律実行可（needs-human 不要）**: 本案 D-1a は可逆・低リスクで，serving ホットパス・relay プロトコルを改変しない．実機
  deploy/collect は B7 の包括承認（非破壊 SSH/deploy）の範囲内で破壊的操作を含まない．`[needs-human]` 登録は不要（D-5 と一致）．
- **要確認リスク（実験フェーズで pre-flight）**: m=204 でバッファは m=4 既定比 51 倍（:619-624 の `buffer_bytes` 式）．
  worker ノードの RAM で `2 × 204 × batch × seq_len × hidden × dtype_bytes` が収まるか，deploy 前に見積もること（hidden=5376．
  seq_len・batch 既定次第だが数百 MB〜1 GB 級の見込み．収まらなければ m=204 を落とすか seq_len を一時縮小＝ただし縮小は
  別レバーになるので慎重に）．OOM の兆候があれば m=204 を外し `{8, 51}` の 2 水準で単調性のみ確認する縮退案を許容する．

---

### 調査 (Iter7)

**担当**: 調査フェーズ subagent（2026-07-19T17:00 頃 JST）．単一レバー **`NUM_MICRO_BATCHES`（research_frontier② の
スループット感度）**の計画に向け，(A) パイプライン並列でのマイクロバッチ数とパイプラインバブル（段間の遊び）・スループットの
関係，(B) その感度を測定するために必要なワークロード条件（複数リクエスト同時投入・in-flight/continuous batching 等），
(C) 本リポジトリのコード上でこのレバーが実際に何に効くか，を調べた．実機クラスタへの接続・deploy・推論実行は一切していない
（`pipeline_inference.py` のコード読み取りと tavily での文献調査のみ）．

#### 調査の問い

1. パイプライン並列でマイクロバッチ数 m を増やすとバブルはどれだけ減るか（定式化）．本リポジトリの 51 段構成で config 候補
   `m∈{2,4,8}` はバブル低減に足りるか．
2. 自己回帰デコードでバブルを埋めるのに必要なワークロード条件は何か（何リクエスト程度の同時投入が要るか，continuous/in-flight
   batching・GPipe 系マイクロバッチ理論の適用限界）．測定の主指標は何にすべきか（ITL/TTFT か集約スループットか）．
3. 本リポジトリのコード上，`NUM_MICRO_BATCHES` は現状どの経路で消費され，掃引すると実際に何が変わるか．

#### A. 分かったこと（バブル理論・出典付き）

- **A-1 バブル率の定式化（planner が真っ先に使うべき式）**: マイクロバッチ pipeline のバブル率（アイドル割合）は
  **`bubble = (p − 1) / (m + p − 1)`**（p＝パイプライン段数，m＝マイクロバッチ数）．Megatron-LM の 1T パラメータ論文で明示され，
  GPipe と同型（出典: Megatron-LM 経由の解説 perform.digital/blogs/pipeline-parallelism-microbatch；PipeFill, CMU PDL,
  arxiv.org/abs/2410.07192；GPipe, Huang et al. 2019, arxiv.org/abs/1811.06965）．**GPipe の経験則: m ≥ 4×p でバブルはほぼ無視
  できる水準になる**（GPipe 原論文 Table 2；medium 解説も同旨）が，**m を増やしても効果は逓減しゼロにはならない**（16 段・
  128 マイクロバッチでもバブル ≈10.5%，mbrenndoerfer.com/writing/pipeline-parallelism-stages-micro-batching-gpipe-1f1b）．
- **A-2 本リポジトリの 51 段への当てはめ（決定的に重要な数値）**: `p=WORLD_SIZE=51` を式に入れると，config 候補の
  **`m=2→バブル 96.2%`／`m=4→92.6%`／`m=8→86.2%`**．**候補 3 水準ではバブルはほとんど動かない**（96%→86%，10 ポイント差）．
  バブルを 50% まで下げるには m≈51（＝p），GPipe 基準の「ほぼ無視」まで下げるには **m≈204（4p）が必要**．すなわち **現行 config の
  `levers: NUM_MICRO_BATCHES: [2,4,8]` は 51 段構成に対して桁が 1〜2 つ小さく，そのまま掃引しても（たとえワークロードが理想でも）
  スループット差はほぼ出ない**という理論的予測になる．バブルは m/p の比で決まるため，段数を減らす（WORLD_SIZE を絞る）と
  同じ m でも比が上がり効きやすくなる（例 p=11 なら m=8 でバブル 56%）が，それは第 2 のレバーであり単一レバー原則に抵触する．
- **A-3 GPipe のマイクロバッチ理論は「学習（forward+backward）」前提で，推論デコードには直接は移らない**: 上式の m は
  学習で 1 ミニバッチを分割した並列作業単位を指す（Iter4 調査で引いた Zero-Bubble も学習主眼）．**自己回帰デコードは
  step ごとに seq_len=1 で，1 リクエスト内には並列に流せる作業がない**．したがってデコードでバブルを埋める「マイクロバッチ」に
  相当するのは **同時に飛んでいる複数リクエスト**である（各デコード step で各段が別リクエストのトークンを処理する）．

#### B. 分かったこと（デコードのバブルを埋めるワークロード条件・出典付き）

- **B-1 デコードのバブル充填は「並列マイクロバッチ」ではなく「同時リクエスト数（concurrency）」で行う**: 推論の PP では各
  デコード step で各段が KV ストレージ内の系列の 1/PP を処理し，同時リクエストを増やすほど段が埋まる（Seesaw, Cao et al.,
  arxiv.org/abs/2503.06433；HF blog「Prefill and Decode for Concurrent Requests」, huggingface.co/blog/tngtech/…）．**深さ p の
  パイプラインを埋めるには最低でも p のオーダーの同時リクエストが要る**（1 段 1 リクエストでも p 本，通信・ACK 往復の遊びまで
  覆うにはそれ以上）．本リポジトリでは **数十本規模（p=51 に対し ≳51 本）の同時投入**が「バブルが埋まる状況」の目安になる．
- **B-2 主指標はスループット（集約 tokens/sec）であって単発 ITL/TTFT ではない**: continuous batching は per-request の
  レイテンシを下げるのではなく，早期終了スロットに待機リクエストを詰めて**集約スループット**を上げる仕組み（デコードは
  s=1 で 1 リクエストでは計算を飽和できず，多数バッチで初めて飽和：haoailab.com CSE234 講義ノート；mbrenndoerfer.com/writing/
  continuous-batching）．**Iter6 の B6 申し送りが主指標を ITL/TTFT としていた点は，②（スループット感度）では集約 tokens/sec へ
  差し替えるべき**．単発 ITL は concurrency を上げても下がらない（むしろ僅かに悪化しうる）ため，ITL のまま測ると「効果なし」と
  誤判定する．
- **B-3 PP と continuous batching を組んでもバブルは残り，prefill/decode の混在が新たな段間不均衡を生む**: TD-Pipe
  （arxiv.org/abs/2506.10470）は「continuous batching＋PP は依然バブルに苦しむ」とし，prefill と decode の混在でマイクロバッチ間の
  仕事量が偏り，速い段が遅い段を待つと指摘．SARATHI 系の chunked prefill は prefill チャンクに decode を相乗り（piggyback）させて
  PP バブルを縮める（donmoon.medium.com；bentoml.com；IoT 規模の動的マイクロバッチ/トークン予算スケジューリング, MDPI Sensors
  26(4):1101, mdpi.com/1424-8220/26/4/1101）．**prefill は計算律速でリクエスト間依存が無く 1 リクエストでも段利用率が高い**一方，
  **バブルが問題になるのは decode 相**という切り分けが重要（Seesaw §2.3；haoailab）．
- **B-4 CPU クラスタ特有の留意点（本リポジトリの支配項＝計算律速との相互作用）**: 上記文献は GPU（decode がメモリ律速ゆえ
  バッチ追加が「ほぼ無料」）を前提とする．**本リポジトリは Iter4 で「ITL の 92% が CPU 計算（float32・4 コア）」＝計算律速**と
  確定済みで，GPU のようなメモリ律速ではない．したがって同時リクエストを段に足すと **各段の GEMM がバッチ次元で線形に重くなり，
  バッチ追加は「ほぼ無料」にならない**可能性が高い（BLAS の演算強度改善で多少は逓減するが，SL1 の GEMM(K) 測定＝ratio が 1 を
  大きく下回らなかった傾向と整合）．**それでも throughput は上がりうる**: 現状はどの瞬間も 51 段中 1 段しか働かず利用率 ≒1/51≒2%
  （Iter4）で，残り 98% は純粋なアイドルだから，同時リクエストでこのアイドルを実仕事に変換できれば集約スループットは段数オーダーで
  伸びる余地がある．**上限は「全段が常時稼働したときの 1 step 時間＝最遅段の計算時間」**で決まり，計算律速ゆえその天井は
  GPU ほど高くない，というのが本ハード特有の見立て（推測を含むが Iter4 の計算律速確定と B-2 の理論から導かれる）．

#### C. 本リポジトリのコード上での `NUM_MICRO_BATCHES` の実際の効き（コード読み取り・確定事実）

- **C-1 レバーの唯一の消費者（`_process_microbatch`/`_pipeline_loop`）は serving 経路から呼ばれない＝実質デッドコード**:
  `NUM_MICRO_BATCHES` を実際に使うのは `_process_microbatch`（`pipeline_inference.py:963-1002`）と，それを
  `for mb in range(self.config.num_micro_batches)` で回す `_pipeline_loop`（`:1109-1147`）のみ．しかし**実行時の本体ループ
  `process_pipeline_inference`（`:1004-1107`，`main` が呼ぶのはこちら＝`:2032`）は `_pipeline_loop` を一切呼ばず**，HTTP で来た
  プロンプトを relay（`_broadcast_prompt_and_wait`→`_relay_request`）でトークン毎・逐次処理するだけである（`:1092-1107`）．
  さらに `_process_microbatch` は先頭で **`if _relay_active: return`（`:970-972`）** と relay 中は即 return し，リクエストが無い
  ときの入力は乱数（`recv_buffers[mb].normal_(...)`, `:976`）＝実プロンプトではない．
- **C-2 したがって現状 `NUM_MICRO_BATCHES` を掃引しても，リクエスト処理のスループットにも ITL にも一切効かない**: 実際に
  変わるのは (i) `recv_buffers`/`send_buffers` を `num_micro_batches` 本 事前確保するバッファサイズ（`:611-623`）と (ii) 起動ログ 1 行
  （`:1020`）だけ．**`mise run predict:demo`（relay 経路を叩く）で m を振っても，単発でも複数リクエストでも差はゼロ**というのが，
  A-2 の「51 段では m∈{2,4,8} は理論上も効かない」よりさらに強い結論（そもそもレバーの消費者が serving 経路で起動しない）．
- **C-3 relay 経路は 1 リクエスト直列**: `_request_prompt` グローバル 1 個と `_relay_active` ゲートで，リクエストは 1 本ずつ順に
  処理される（`:1096-1107, 1290, 1739`）．**複数リクエスト同時投入で段を埋めるには relay 経路自体に in-flight batching（複数系列の
  KV を保持し各 step で段へ相乗り）を実装するホットパス改変が必須**で，これは B12 が警告した「`pipeline_inference.py` ホットパス
  改変を要する過大設計」に該当し，規模的に SL3/B9（relay プロトコル改修）に隣接する．

#### D. 次フェーズ（rc-planner）への具体的示唆

- **D-1（最重要・方針の岐路）**: 「`NUM_MICRO_BATCHES` を振ってスループット差を測る」を**現行コードのまま実機で回すと差はゼロ**
  （C-2）．planner は次のいずれかを選ぶ必要がある．
  - **(案 D-1a) レバー値を 51 段に合わせて拡張し，かつ「レバーが生きている」経路で測る**: config の `[2,4,8]` は 51 段には桁不足
    （A-2）なので，測るなら m を `{8, 51, 204}`（＝1/p/4p 近傍）まで広げる．ただし生きている消費者は warmup 相当の `_pipeline_loop`
    だけなので，これを serving 本体から明示的に起動して**乱数パイプラインの集約スループット（step/sec）が m でどう変わるか**を測る
    小改修に留める案（実プロンプト serving は変えない＝ホットパス非改変寄り）．バブル式 A-1 が実 51 段 Gloo/CPU 上で成り立つかの
    検証になり，可逆・低リスク．**単一レバー原則に最も忠実**．
  - **(案 D-1b) 実リクエストのスループットを測るなら in-flight batching のホットパス改変が要る**: relay を複数系列同時処理へ拡張
    （C-3）．これは規模的に SL3/B9 隣接で **`[needs-human]` 登録が妥当**．B12 が「過大なら SEQ_LEN/STAGGER へ振り替えるか backlog
    登録」と指示済みの分岐に当たる．
  - **(案 D-1c) レバーを振り替える**: B12 の代替どおり `SEQ_LEN`（④，KV 上限と品質/メモリ）や `STAGGER_INTERVAL` へ移す．ただし
    STAGGER は起動時 thundering herd 用で単発 ITL 寄与は小（Iter6 §4 で既述）．
- **D-2 主指標を ITL/TTFT から集約スループット（tokens/sec, 全同時リクエスト合算）へ差し替える申し送り**: B6 が②の主指標を
  ITL/TTFT としていたが，②（バブル低減＝スループット）では単発 ITL は原理的に動かない（B-2）．**バブル充填の効果は集約 tokens/sec
  でしか観測できない**ので，planner は主指標を明示的に集約スループットへ更新すること．
- **D-3 ワークロード設計の定量目安**: もし実リクエスト concurrency を作る（D-1b）なら，**同時リクエスト数 N を段数オーダー
  （p=51 に対し 8→32→64 など）で振り**，バブルが N とともに埋まって集約スループットが飽和に向かう曲線を測るのが筋（B-1）．
  N を段数未満に留めると効果が出ずに「効果なし」と誤判定する（B-2 と同じ落とし穴）．
- **D-4 CPU 計算律速の天井を明記して過大評価を避ける**: 本ハードは decode がメモリ律速でなく計算律速（Iter4）ゆえ，同時リクエスト
  追加は各段 GEMM を線形に重くし，GPU のような「バッチはほぼ無料」は成り立たない（B-4）．throughput 向上の天井は「全段常時稼働時の
  最遅段計算時間」で決まると見立て，planner は期待値を段数倍（理論上限）ではなく計算律速で頭打ちする現実値として設計・報告すること．
- **D-5 人間判断の要否**: 案 D-1a・D-1c は可逆・低リスクで自律実行可．**案 D-1b（in-flight batching のホットパス改変）を選ぶ場合は
  規模が SL3/B9 隣接のため `[needs-human]` 登録が妥当**（新規の needs-human 事項候補として planner/reflector へ申し送る）．

**出典**: バブル率 (p−1)/(m+p−1)・GPipe「m≥4×p で無視可」＝ Huang et al. 2019, arxiv.org/abs/1811.06965（GPipe）／Megatron-LM 経由
解説 perform.digital／PipeFill, arxiv.org/abs/2410.07192／mbrenndoerfer.com（16 段でも 10.5% 残る）；デコードは同時リクエストで
段を埋める＝ Seesaw, arxiv.org/abs/2503.06433／HF blog tngtech「Prefill and Decode for Concurrent Requests」／haoailab.com CSE234
講義ノート；continuous batching は集約スループット向上策＝ mbrenndoerfer.com/writing/continuous-batching／bentoml.com；PP＋
continuous batching のバブル残存・prefill/decode 混在＝ TD-Pipe, arxiv.org/abs/2506.10470／SARATHI chunked prefill, donmoon.medium.com／
動的マイクロバッチ・トークン予算, MDPI Sensors 26(4):1101, mdpi.com/1424-8220/26/4/1101；コード事実＝
`pipeline_inference.py:611-623,963-1002,970-972,976,1004-1107,1020,1109-1147,1092-1107,1290,1739,2032`；本リポジトリの計算律速 92%・
利用率 ≒1/51 ＝ journal Iter4 `### 調査/分析(解釈) (Iter4)`．

---

## Iteration 6

### 考察・次計画 (Iter6)

**担当**: 考察・次計画 subagent（2026-07-19）．`### 分析(解釈) (Iter6)` の結論（go 方向を強める）を受け，単一レバー
**SL2（draft 採択率のオフライン見積もり）**の採否を確定し，次イテレーション（Iteration 7）の方向を決めた．実機への新規
接続・実行はしていない（記録の読み取り・`results/draft_acceptance.jsonl` の読み取り・commit 操作のみ）．

**1. 採否判定: 採用で確定・収束（adopt & converged）**

- **判定根拠**: SL2 は診断（計測）レバーであり，判定対象は「B3（speculative decoding）の**期待値側の因子＝draft 採択率**が，
  SL1 で確定した compute 天井を実効利得として現実化させる下限条件を満たすか（向き）」．analyst 判定どおり実測は
  **overall α=0.5856（≳0.5）・a_2=1.8562（≳1.5）**で計画 §4 の go 条件を**両方充足**し，no-go 域（a_K≈1・α≲0.1）からは
  大きく離れる．ノイズは greedy ゆえ決定的量で，全体 α は 555 位置・全 4 カテゴリが位置数 ≥50 を満たし（doc_qa は 52 で
  境界だが全体値優先），ラベルが反転する余地は無い．実装は新規 2 ファイル（`scripts/estimate_draft_acceptance.py`／
  `tests/test_estimate_draft_acceptance.py`）＋ HF キャッシュ revision 固定のみで `pipeline_inference.py` 非改変，
  `pytest` 83 passed（回帰なし）．計画 §4 の完了条件（α・カテゴリ別 α・経験 a_K・α→a_K 写像併記・prompt-lookup 層別・
  SL1×SL2 合成の素数値・純関数テスト green）を全て満たした．**採用で確定**．
- **追加反復の要否**: 不要．α・a_K は greedy で決定的（run 間分散ゼロ）ゆえ，同一プロンプト集合での反復では新情報は
  得られない（推定誤差は位置数のみに支配され，555 位置で十分）．
- **このレバーの収束状況**: SL2 は「採択率が SL1 の compute 天井を実効利得へ現実化する下限条件を満たすか」という単一の
  問いに，決定的な答え（満たす＝下限クリア．ただし満額は取れず実効 compute 利得は最良 K=4 で ≈1.43 倍）を出したため
  **このサブレバーは収束**．同じ問いへ SL2 を再び振っても新情報は得られない．次は B3 go/no-go（B9，人間判断待ち）へ
  論点が移るが，それ自体はこの reflector では決めない（§3）．

**2. 非自明な学び（次の自分向け）**

- **(i) SL1 の 4.7 倍 compute 天井の大半は採択率が食い潰す**: SL1 単独では ratio_8=0.213＝per-token compute を最大
  1/0.213≈4.7 倍にできる余地があった（Iter5 の学び）が，現実の採択率（α=0.586・a_K が K に届かない）を織り込むと
  **SL1×SL2 合成の実効 compute 利得は最良でも ≈1.43 倍（K=4）**に縮む（K=2:1.23／K=4:1.43／K=8:1.35）．B3 の compute 側
  期待値は「no-go にするほど低くはない（下限クリア）が，天井を満額は取れない」水準．Iter5 §2(iii)「残る不確実性は
  期待値側（採択率）に集約」への定量的回答である．
- **(ii) K=4 が最良点で，draft 長を伸ばすほど得ではない**: a_K は K とともに単調増加（1.86→2.16→2.29）するが増分は
  逓減し，一方で検証コスト K·ratio_K は K=8 で増える（1.506→1.512→1.704）ため，実効利得は K=4 で最大・K=8 で目減り
  （1.43→1.35）する．**B3 を進める場合の draft 長の第一候補は K=4**．K=8 への延伸は a_K の飽和で割に合わない．
- **(iii) prompt-lookup（n-gram）は E2B 主軸の妥当性を補強しただけ**: prompt-lookup は open_chat（α=0.013）・code
  （α=0.057）で無力で，入力接地が n-gram 重複として現れる要約・抽出的 QA（α=0.36／0.52）でのみ効く．同じ code で E2B は
  α=0.516 と機能しており，**汎用 draft には n-gram 単独では不適，go/no-go は E2B に依存させた計画判断は正しかった**．
  補助指標は主指標を動かさずに枠組みの妥当性検証に徹する使い方が有効，という運用面の学びでもある．
- **(iv) B9 を諮る前に残る未測 3 因子（analyst 申し送り）**: (a) **relay 1 往復化のプロトコルオーバーヘッド**（＝SL3/B9
  本体そのもので，1.43 倍の compute 利得を通信コストがどれだけ食うかが B3 の実 end-to-end 速度を左右する最大の未測因子），
  (b) **プロンプト分布の代表性**（本測定は 4 カテゴリ均等 16 件のトイ集合．open_chat 比率が高い運用では全体 α は下振れ），
  (c) **長文生成での α 安定性**（本測定は `N_MAX_NEW_TOKENS=48` の短い生成長）．(a) は B9 本体，(b)(c) はオフラインで
  安価に潰せる余地があるが，本 reflector では次レバーを config 既定候補から選ぶ制約に従い自律選定の対象外とした（§4）．

**3. B9（B3 本体＝relay プロトコル改修＝SL3 の go/no-go）の扱い: needs-human のまま維持（今回 reflector では自動判定しない）**

- **判断**: SL1（compute 天井）×SL2（採択率）が出揃い，B9 の判断材料（実効 compute 利得 ≈1.4 倍・K=4 最良）は揃った．
  ただし B9 は「不可逆・大規模な relay プロトコル改修（`pipeline_inference.py` ホットパス改変・検証木・51 ノード再デプロイ）」の
  go/no-go であり，research-cycle の自律判断ポリシー（不可逆/大規模は人間判断）に該当する**不可逆判断**である．したがって
  この reflector フェーズでは go/no-go を自動選択せず，B9 は `[needs-human]` のまま**維持（差し替えない）**．
- **状況**: B9 の論点は「この ≈1.4 倍の compute 利得を relay プロトコル改修コスト（未測）が上回るか」へ移行しており，
  既に別途 Slack（`<@U08GLKY1QCW>` mention 付き）で報告済み．今回の通常サマリー投稿では重複 mention を避ける．人間の
  回答を待って continue 時に，回答内容に応じて SL3 着手（go）か別レバー継続（no-go/保留）を決める．

**4. 次に振るレバーの決定（Iteration 7）: NUM_MICRO_BATCHES（research_frontier② のスループット感度）を自動選定**

- **決定（自律判断・可逆）**: Iteration 7 の単一レバーを **`NUM_MICRO_BATCHES`（config `levers` 最優先候補，
  research_frontier② の「マイクロバッチ数・stagger interval のスループット感度分析」の枠組みで振る）**とする．B9 の人間
  回答を待たずに自律実行できる軽量な項目で，B3 の compute 律速（B9，人間判断待ち）とは**直交する軸**であり，人間回答を
  待つ間に研究を停滞させないための選定である．
- **選定理由**: (1) B9 が人間判断待ちの間，reflector が自律的に選べる項目は config `research_frontier` ②③④ または `levers`
  の 4 候補に限られる（不可逆な SL3/B9 へは踏み込まない）．その中で `NUM_MICRO_BATCHES` は `levers` 一覧の**最優先**
  （順序＝優先度）で，B6 が ②/⑤ 着手時の実験前提条件（`--iter Iter{n}` 変数化・冷開始交絡除去・各水準 n≥3〜5 反復・
  主指標 ITL/TTFT）を既に申し送り済みで planner が即着手できる．(2) 実機 deploy/predict を伴うが B7 の包括承認（非破壊
  SSH/deploy）の範囲内で自律実行可．(3) 破壊的操作を含まず可逆．
- **重要な留保（planner への申し送り）**: Iter4 で「`NUM_MICRO_BATCHES` は**単一リクエストの ITL では Σcompute 不変・
  残差止まり**（支配項 compute に効かない）」と確定済みである．したがって単発デモ（`"Hello!"` 1 件）を回すだけでは
  スループット差は出ない．②の感度を意味あるものにするには，**planner が複数リクエスト同時投入／連続バッチのワークロード
  を設計し，パイプラインバブル低減が効く条件（micro-batch 数を増やすと段間の遊びが埋まる状況）を作る**必要がある．
  この設計が過大（`pipeline_inference.py` ホットパス改変を要する等）と判明した場合は，SEQ_LEN（④）や STAGGER_INTERVAL
  へ振り替えるか，backlog へ `[needs-human]` 登録して諮ること．
- **見送り（非選定）の理由**: SL3／B3 本体は §3 のとおり B9（人間判断待ち・不可逆）．STAGGER_INTERVAL は起動時
  thundering herd 回避が主目的で単発 ITL への寄与が小さく，SEQ_LEN（④）・WORLD_SIZE（③）は品質/メモリ・層割当粒度の
  トレードオフで今回の「軽量に振れる待ち時間の使い道」としては `NUM_MICRO_BATCHES`（最優先レバー）に劣後する．
  backlog に `## B12 [auto-decided 2026-07-19]` として本決定を記録した．

**次イテレーションへの結論**: Iteration 6（SL2: draft 採択率のオフライン見積もり）を**採用で確定・収束**（overall α=0.5856・
a_2=1.8562 で go 条件を両方充足＝SL1 の compute 天井を実効利得へ現実化する下限条件をクリア．SL1×SL2 合成の実効 compute
利得は最良 K=4 で ≈1.43 倍）．B3 本体（SL3）go/no-go の B9 は判断材料が揃ったが不可逆判断のため `[needs-human]` のまま
維持し，人間の回答を待つ．Iteration 7 は `NUM_MICRO_BATCHES`（research_frontier② のスループット感度）を自動選定して開始
する（B9 回答を待つ間，直交軸で研究を進める）．

### 実験（再実行・成功） (Iter6)

**担当**: 実験フェーズ subagent（2026-07-19T15:35〜16:15 JST）．`### 実装（HF キャッシュ revision 修正）(Iter6)` の
対処後，`unset VIRTUAL_ENV && uv run python scripts/estimate_draft_acceptance.py` を再実行（既に起動済みだったプロセス
PID 1097153/1097157 を監視するのみで，新規プロセスは起動していない）．実機クラスタへの接続・deploy・SSH・HF キャッシュ
の書き換えは行っていない．

- **結果: 正常完了**．所要時間 約 39 分（開始 15:35 → `results/draft_acceptance.jsonl` 書き込み完了 16:15:17）．
  `run.log` にエラー・例外なし（`torch_dtype` deprecated 警告のみ）．デッドライン 17:35 に対し余裕あり．
- **出力先**: `results/draft_acceptance.jsonl` 1 行目に 1 レコード追記（新規ファイル，既存 `Iter1/3/4.jsonl` や
  `bench_compute_ceiling.jsonl` は無変更）．

#### 主要数値（解釈・良否判定は analyst フェーズへ）

- `overall_alpha_e2b = 0.5856`（E2B draft の全体採択率）．カテゴリ別: `open_chat=0.5000`／`summarization=0.7197`／
  `doc_qa=0.6923`／`code=0.5156`．
- prompt-lookup（n-gram, 補助・A-2 検証用）カテゴリ別 α: `open_chat=0.0130`／`summarization=0.3567`／
  `doc_qa=0.5192`／`code=0.0573`（入力接地型で高く開放チャットでほぼゼロ，計画で想定した傾向と一致）．
- 経験 `a_K`（K別平均採択長，E2B）: `K=2: 1.8562`／`K=4: 2.1595`／`K=8: 2.2934`．α からの予測値
  （`K=2: 1.9285`／`K=4: 2.2469`／`K=8: 2.3935`）と近似し，大きな乖離なし．
- **SL1×SL2 合成**（`a_K × ratio_K` と `gain_over_baseline`，SL1 の実測 `ratio_K={2:0.753,4:0.378,8:0.213}` と合成）:
  `K=2: product=1.3977, gain=1.2325`／`K=4: product=0.8163, gain=1.4283`／`K=8: product=0.4885, gain=1.3459`．
- プロンプト数 16（4 カテゴリ×4 件），`n_max_new_tokens=48`．per-prompt 内訳は `results/draft_acceptance.jsonl`
  の `per_prompt` フィールドに全件保存済み．

### 分析(解釈) (Iter6)

**担当**: 分析(解釈) subagent（2026-07-19）．`### 実験（再実行・成功） (Iter6)` の実測値と `results/draft_acceptance.jsonl`
の `per_prompt` 全 16 件を Read し，`### 検討・計画 (Iter6)` §4 の成功条件・判定の解釈指針（位置数 ≥50 のみ有意な層別値／
a_2≳1.5・α≳0.5 で go 方向／a_K≈1・α≲0.1 で no-go 方向／中間は SL1 ratio_K との積で期待値レンジ）に照らして解釈した．
実機への接続・実行・`results/draft_acceptance.jsonl` への書き込みはしていない（読み取りのみ）．α は greedy で決定的
（run 間分散ゼロ）ゆえ，ノイズ評価は位置数（＝参照トークン数）で行い，過去反復の標準偏差ではなく決定的量の位置数依存性で判定した．

**前提（判定の枠組み）**: SL2 の判定対象は「B3（speculative decoding）の**期待値側の因子＝draft 採択率**が，draft の per-token
compute 削減（SL1 で確定した compute 天井）を実効利得として現実化させる下限条件を満たすか」であり，B3 本体の実レイテンシ低減量
そのものではない．判定は E2B draft の全体 α・a_K を主根拠とし，prompt-lookup は A-2 検証（E2B を主軸に据える妥当性の裏付け）
の補助に留める（計画 §2 の一本化どおり，prompt-lookup で go/no-go を動かさない）．

**1. ノイズ判定: 全 4 カテゴリが位置数 ≥50 を満たし，層別値は全て有意（ただし doc_qa は下限ぎりぎり）．全体 α は十分な母数**

- `per_prompt` の `num_reference_tokens` をカテゴリ別に集計した位置数（＝α を測った照合位置数）: **open_chat=154／
  summarization=157／doc_qa=52／code=192，全体=555**．計画 §4 の閾値「位置数 ≥50 のみ有意な層別値」を**全カテゴリが充足**する
  ため，カテゴリ別 α は 4 種とも有意な層別値として扱える．ただし **doc_qa は 52 と閾値ぎりぎり**（計画 §3 が目標とした
  ≈150 位置/カテゴリには届かない．doc_qa の参照出力が短い＝QA 回答が 18/10/15/9 トークンと簡潔なため）で，doc_qa の α=0.6923 は
  有意だが層別推定としては相対的に薄い（解釈は全体値優先）．
- **全体 α=0.5856 は 555 位置に基づく**（計画 §3 の全体 ≈600 位置想定にほぼ一致）．greedy ゆえ α は決定的量で run 分散はゼロ，
  推定誤差は位置数のみに支配される．555 位置は「見かけの増減か有意か」を論じるまでもなく，**全体値を主たる判断根拠に据えられる
  水準**である（計画の指針どおり，doc_qa の薄さを含む層別ノイズは全体値で吸収する）．
- カテゴリ内ばらつき（per-prompt α の min/max）: open_chat 0.479–0.542／summarization 0.629–0.783／doc_qa 0.556–0.778／
  code 0.417–0.625．各カテゴリ内で符号が割れる（一部が α≈0 に落ちる）ことはなく，**層別値の向きは安定**している．

**2. 判定結果: go 方向を強める（下限条件クリア）．全体 a_2=1.86≳1.5 かつ α=0.586≳0.5 に明確に該当**

- 計画 §4 の解釈ガイド「**E2B の全体 a_2≳1.5（α≳0.5）**なら SL1 の compute 天井が実効利得として現実化する下限条件クリア＝
  B3 go 方向を強める」に対し，実測は **a_2=1.8562（≳1.5）・overall_α=0.5856（≳0.5）**で**両条件とも充足**．no-go 方向の
  条件（a_K≈1・α≲0.1）からは大きく離れる（最小の a_2=1.86 でも 1 を大幅に超える）．したがって本 SL2 は **go 方向を強める**
  領域に該当し，中間域（SL1 ratio_K との積で期待値レンジを提示して B9 で諮る）ではなく**下限条件を明確にクリアした**と判定する．
- **α→a_K 写像の整合**: A-1 の式 `E=(1-α^(K+1))/(1-α)` による予測 a_K（K=2:1.9285／4:2.2469／8:2.3935）と経験 a_K
  （1.8562／2.1595／2.2934）の乖離は各 K で 3.8%／3.9%／4.2% と小さく，**経験値が一貫して予測をわずかに下回る**．これは iid 幾何
  近似に対し実系列が非 iid（序盤位置ほど採択されやすく，後半で不一致が出やすい）であることの符号として自然で，想定外の挙動
  ではない．写像が数％以内で成立するため，α と a_K は相互変換可能な整合した量として扱える．
- 想定外挙動（言語崩れ・発散・OOM 等）は無し．実験は 39 分で正常完了し，run.log にエラーなし（`torch_dtype` deprecated 警告のみ）．

**3. prompt-lookup（A-2 検証）: 開放チャット≈0 は予想どおり．ただし code も低く，E2B を主軸に据える妥当性をむしろ補強**

- prompt-lookup（n-gram）カテゴリ別 α: open_chat=0.0130／summarization=0.3567／doc_qa=0.5192／code=0.0573．**開放チャットで
  ほぼゼロ（0.013）**は A-2 の予想「開放チャットでは入力↔出力の n-gram 重複が乏しく採択率≈0」と**一致**．summarization=0.357／
  doc_qa=0.519 が高いのも「入力接地型で高い」予想と一致する．
- **予想からの部分的ズレ（重要な補強材料）**: A-2 は code（コード編集）も入力接地型として高採択を予想したが，実測 code=0.0573 は
  open_chat 並みに低い．本イテレーションの code タスク（docstring 追加・バグ修正・型ヒント付与・実装補完）は**出力が入力の
  n-gram コピーではなく新規生成**のため，直近 n-gram の入力内マッチが効かなかったと解釈できる．一方で**同じ code で E2B は
  α=0.5156 と機能している**．すなわち prompt-lookup は「入力接地の形が n-gram 重複として現れるタスク（要約・抽出的 QA）」に
  限って効き，code のような生成的タスクでは開放チャット同様に崩れる．これは計画 §2 が E2B を主軸（開放チャットでも効く汎用性・
  prompt-lookup の弱点 A-2 を持たない）に据えた判断を**むしろ補強する**（prompt-lookup 単独では code・open_chat の 2 カテゴリで
  無力＝汎用 draft には不適，go/no-go は E2B に依存させて正解）．

**4. B9（B3 本体＝relay プロトコル改修 go/no-go）への材料: compute 側の実効利得は 1.23〜1.43 倍（K=4 が最良）．上限側は埋まったが期待値側の一部と relay コストは未測**

- **合成利得の意味**: `gain_over_baseline = a_K /(K·ratio_K)`（実測で K=2:1.2325／K=4:1.4283／K=8:1.3459．算出式を per_prompt から
  逆算し確認済み）は，「1 検証ステップで draft の K 提案を 1 回の GEMM（K 位置）で検証し a_K トークンを確定する」ときの，通常の
  逐次 GEMV（per-token compute=1）に対する **per-token compute の実効削減倍率**である．SL1（GEMM 効率＝ratio_K）と SL2（採択率＝a_K）
  を掛け合わせた B3 の **compute 側実効利得の期待値**にあたる．
- **数値の大小の意味づけ（B9 の核心）**: SL1 単独では ratio_8=0.213＝理論上 per-token compute を最大 1/0.213≈4.7 倍にできる余地が
  あった（Iter5 の学び）．しかし採択が理想化されない現実（α=0.586，a_K が K に届かない）を織り込むと，**compute 側利得は最良でも
  ≈1.43 倍（K=4）に縮む**．つまり **SL1 の 4.7 倍の compute 天井のうち，採択率が大半を食い潰し，残る実効 compute 利得は 1.4 倍
  程度**というのが SL1×SL2 合成の結論である．これは Iter5 §2(iii)「残る不確実性は期待値側（採択率）に集約」への定量的回答で，
  採択率は「B3 を no-go にするほど低くはない（下限クリア）が，compute 天井を実効利得として満額は取れない」水準だと分かった．
- **K 依存性の解釈**: 利得は **K=4 で最大（1.43 倍）**．a_K は K とともに単調増加（1.86→2.16→2.29）するが増分は逓減し，一方
  K·ratio_K（検証コスト）は K=8 で増える（1.506→1.512→1.704）ため，積の比は K=4 が最良点になる．**B3 を進める場合の draft 長は
  K=4 が第一候補**．K=8 まで伸ばしても a_K の伸びが飽和し検証コスト増で利得はむしろ目減りする（1.43→1.35）．
- **何が測れて何が未知か（B9 の残論点）**:
  - 測れた: (i) draft 採択率 α と a_K（E2B・16 プロンプト・4 カテゴリ層別），(ii) SL1×SL2 合成の compute 側実効利得（1.23〜1.43 倍，
    K=4 最良），(iii) 生成品質は greedy exact-match 採択の構成上ロスレス（speculative decoding のアルゴリズム的性質で品質劣化なし）．
  - **未知（B9 で人間が織り込むべき）**: (a) **relay 1 往復化のプロトコルオーバーヘッド（SL3/B9 本体）**＝分散 51 ノードで draft 提案と
    target 検証を 1 往復に畳む通信コストは本 SL2 に含まれない．この relay コストが 1.4 倍の compute 利得をどれだけ食うかが B3 の
    実 end-to-end 速度を左右する最大の未測因子．(b) **プロンプト分布の代表性**: 実運用のタスク構成（開放チャット比率が高いか，
    入力接地型が多いか）で全体 α は動く（本測定は 4 カテゴリ均等 16 件のトイ集合．open_chat が多い運用なら α は下振れ）．
    (c) 本測定は N_MAX_NEW_TOKENS=48 の短い生成長での α．長文生成での α の安定性は未確認．

**分析の結論**: SL2（E2B draft 採択率）は **go 方向を強める**（全体 a_2=1.86≳1.5・α=0.586≳0.5 で下限条件を明確にクリア，
no-go 域からは遠い）．ノイズ判定は全 4 カテゴリが位置数 ≥50 を満たし層別値は有意（doc_qa は 52 で薄いため全体値を優先），
全体 α=0.5856 は 555 位置に基づく決定的量で確信度は高い．prompt-lookup は open_chat・code で崩れ E2B 主軸の妥当性を補強．
SL1×SL2 合成の compute 側実効利得は 1.23〜1.43 倍（**K=4 が最良**）で，SL1 の 4.7 倍 compute 天井の大半は採択率が食い潰すが
B3 を棄却するほど低くはない．B3 go/no-go は「この 1.4 倍の compute 利得を relay プロトコル改修コスト（未測）が上回るか」に論点が
移る．追加反復の要否: SL2 の judgment 自体は決定的量ゆえ**追加反復不要**．ただし B9 を諮る前に，relay オーバーヘッドの見積もり
（別レバー）とプロンプト分布の代表性の確認が残課題として次の考察・次計画フェーズ（rc-reflector）へ申し送る．

### 調査 (Iter6)

**担当**: 調査フェーズ subagent（2026-07-19）．単一レバー **SL2（draft 採択率のオフライン見積もり）**の計画に必要な，
(A) 採択率見積もり手法の先行研究，(B) 既存コード（`pipeline_inference.py` の生成・トークナイズ・サンプリング）と
ローカル資産（モデル重み・トークナイザ）の制約，を調べた．実機クラスタへの接続・deploy・relay 改修は一切していない
（コードと文献の読み取り，および `~/.cache/huggingface`・`models/splits` のメタデータ確認のみ）．

#### 調査の問い

1. speculative decoding の draft 採択率（acceptance rate）をオフラインで見積もる標準手法は何か（prompt-lookup/n-gram
   と小 draft モデルの両系統）．測定指標はどう定義され，採択率から実効速度への写像はどう与えられるか．
2. 本リポジトリのユースケース（Gemma-4-31B・分散パイプライン並列・CPU・greedy 生成）で，relay 改修・再デプロイなしに
   「draft 生成 → 検証 → 採択率算出」を単一プロセスで完結できるか．既存コード・ローカル資産の制約は何か．

#### A. 分かったこと（採択率見積もり手法・出典付き）

- **A-1 採択率の指標定義（写像が計画の要）**: speculative decoding の実効利得は「1 検証ステップあたり確定するトークン数
  （block efficiency / mean accepted length）」で決まる．greedy target 検証では **draft の第 i トークンは target の argmax と
  一致したときのみ採択**（決定的な exact-match）で，最初の不一致で打ち切り＋target が 1 個の bonus トークンを出す．
  Leviathan et al. 2023「Fast Inference from Transformers via Speculative Decoding」の期待トークン数は
  **E[生成トークン/ステップ] = (1 − α^(γ+1)) / (1 − α)**（α＝1 トークンあたり採択確率，γ＝draft 長＝K）．
  これが **SL2 で測る α を実効利得へ写像する式**であり，SL1 の per-token compute 比（ratio_K）と掛け合わせると B3 の
  期待利得レンジが数値で括れる（出典: arxiv.org/abs/2211.17192，および vLLM の acceptance/mean-acceptance-length 定義
  docs.vllm.ai の `vllm.v1.spec_decode.metrics`）．
- **A-2 prompt-lookup（n-gram）draft の傾向と測定法**: draft モデルを使わず，直近生成 n-gram を **入力（プロンプト＋既生成）
  内で文字列マッチ**し，一致継続列を candidate として提案する（apoorvumang/prompt-lookup-decoding，github.com）．著者の
  実測条件は **max n-gram=3・continuation length=10・greedy**．**入力接地型タスク（要約・文書 QA・コード編集・多ターン
  チャット）で入力↔出力の n-gram 重複が高いとき 2〜4 倍高速化，品質不変**．逆に**開放的な短文チャットでは重複が乏しく
  採択率はほぼゼロに落ちる**（zenml.io，aphrodite/vLLM の ngram prompt-lookup 解説も同旨）．**含意（重要）**: 本リポジトリの
  デモプロンプトは `"Hello!"`（出力 15 トークン `"Hello! How can I help you today?\nthought"`，`results/Iter4.jsonl`）で入力接地性が
  無く，prompt-lookup をこの 1 プロンプトだけで測ると採択率≈0 という**過小評価**になる．prompt-lookup を公平に測るなら
  要約・QA・コードなど入力接地型プロンプト集合が要る．
- **A-3 小 draft モデルの採択率オフライン測定法**: 標準手順は「target の greedy 参照系列を確定 → 同一プレフィックスを
  draft に食わせ K トークン提案 → target argmax と逐位置照合し，最初の不一致までの採択長を集計」．必要データは
  (i) target と **同一トークナイザ／語彙**の小 draft モデル，(ii) 評価プロンプト集合，(iii) target の参照 argmax 系列．
  採択率が低いと draft の予測精度不足で利得が出ない点が既知の弱点（Online Speculative Decoding, arxiv.org/abs/2310.07177）．
- **A-4 本ユースケースに近い事例**: CPU・分散パイプライン・Gemma に完全一致する公開事例は見当たらなかった（Gemma-3 の
  spec decoding は主に GPU ランタイム LM Studio/Ollama/TensorRT-LLM 文脈）．ただし **採択率の測定自体はランタイム非依存**で，
  target と draft の logits/argmax があれば CPU 単機でも成立する（採択率はモデル対の性質で，実行ハードに依らない）．

#### B. 既存コード・ローカル資産の制約（SL2 をローカル単一プロセスで完結できるか）

- **B-1 生成規則（複製すべき target の判定則）**: 最終 rank は `hidden → final_norm → lm_head(F.linear) →
  final_logit_softcapping=30 の tanh → argmax`（`pipeline_inference.py:1600-1618`）で **greedy・決定的**．softcapping は
  単調変換で argmax を変えないが，参照再現では忠実に含めてよい．**greedy なので採択判定は exact-match で厳密**（サンプリング
  時の確率的採択の近似は不要）＝オフライン測定が素直．
- **B-2 トークナイズ**: `_tokenize()`（`:110-129`）が Gemma-4 chat template（`apply_chat_template` + `encode`,
  `add_generation_prompt=True`）を適用．参照系列・draft の入出力は**この同じ経路でトークナイズすべき**（生テキスト直 encode は
  IT モデルで挙動が変わる）．
- **B-3 ローカル資産（オフライン化の決定的な後押し）**:
  - target **`google/gemma-4-31B-it` のフル重みがローカルに二重に存在**: `~/.cache/huggingface/hub/models--google--gemma-4-31B-it`
    （59GB・safetensors 5 本，tokenizer/config 含む）と `models/splits/`（60GB・`embed_tokens`＋`layer_0..59`＋`lm_head`）．
    語彙は embed サイズ 2818572416B ÷ (5376 hidden × 2B bf16) ≈ **262144**．
  - **小 draft 候補 `google/gemma-4-E2B` がローカルに存在**（`~/.cache/.../models--google--gemma-4-E2B`，9.6GB・
    `model.safetensors`，tokenizer/config 揃い）．`model_type=gemma4`・**`vocab_size=262144`＝target と一致**（＝同一トークナイザ，
    token ID 直接照合可），`num_hidden_layers=35`・`hidden_size=1536`・`num_kv_heads=1`（実効 ~2B 級）．**同一 Gemma-4 系
    ＝語彙一致の小 draft が既に手元にある**のは A-3 の要件 (i) を満たす理想条件．
  - 実行ホスト RAM: 125GB（available 89GB）．draft(E2B)＝軽量で確実に載る．target 31B は bf16 で 59GB＝**単機ロードも一応可能
    （余裕は小）**．より安全には `models/splits` を layer 単位でストリーム（load→compute→free）して参照生成する手もあるが，
    **手元 HF キャッシュに 31B フル重みがある以上，`transformers` で `AutoModelForCausalLM.from_pretrained(..., torch_dtype=bfloat16,
    device_map="cpu", local_files_only=True)` を単機ロードして greedy 参照を出すのが最短**（クラスタ・relay・deploy 完全不要）．
- **B-4 既存ログの限界**: `results/*.jsonl` は `result_text`（デコード済み文字列）は持つが **generated_ids（トークン列）を持たない**
  し，プロンプトは `"Hello!"` 1 種のみ（`results/Iter4.jsonl`）．よって**既存ログだけでは採択率は測れず**，参照系列は
  ローカルで新規生成する必要がある（B-3 によりこれはクラスタ非接触で可能）．
- **B-5 relay 改修は不要**: SL2 は「採択率という**モデル対の統計量**」の推定であり，実運用の relay 1 往復化（SL3/B9）を一切
  含まない．`pipeline_inference.py` ホットパスも 51 ノード再デプロイも不要で，B10 の申し送り「クラスタ本体への大改変が要れば
  needs-human 登録」に**抵触しない**（ローカル単一プロセスで完結する見込み）．

#### C. 次フェーズ（rc-planner）への具体的示唆

- **測定指標の定義（planner が固定すべき）**: 主指標は **K∈{2,4,8} ごとの平均採択長 a_K＝1 検証ステップで確定するトークン数**
  （＝1＋最初の不一致までの採択数）と，**1 トークンあたり採択率 α**．A-1 の式で a_K と α は相互変換でき，SL1 の ratio_K と
  組めば実効利得 ≈ a_K × (per-cycle コスト)⁻¹ の期待値レンジが出る．SL1 の K 値（2/4/8）に採択率の K を揃えると接続が綺麗．
- **候補 draft 戦略（2 系統，両方測るのが安価で情報量大）**:
  - **(C-1) 小 draft モデル＝`gemma-4-E2B`**: 語彙一致・ローカル在・軽量で第一候補．target(31B)greedy 参照に対する
    exact-match 採択長を測る．**開放チャットでも効く**汎用性が prompt-lookup より高い見込み．
  - **(C-2) prompt-lookup（n-gram, max=3・cont=10）**: モデル追加ゼロ・実装数十行．ただし A-2 より**入力接地型プロンプトで
    ないと採択率≈0**．`"Hello!"` 単独では過小評価になるため，公平比較には入力接地型プロンプトが必須．
- **参照データの取り方**: (a) **プロンプト集合**を小規模（例: 開放チャット数件＋要約/QA/コード編集など入力接地型数件，計
  10〜30 件）に定義し，(b) 各プロンプトで **target(gemma-4-31B-it) を単機 greedy 生成**（`_tokenize` と同じ chat template・
  `final_logit_softcapping=30`・argmax を再現）して参照 argmax 系列を作る（クラスタ非接触）．(c) 同系列上で (C-1)(C-2) の
  採択長を集計．prompt-lookup の弱さを可視化するため**タスク種別ごとに採択率を層別集計**すること．
- **既存コードとの接続点**: トークナイズは `pipeline_inference.py:_tokenize()` を再利用（chat template 一致），採択判定則は
  `:1600-1618` の greedy+softcapping+argmax を複製．実装は Iter5 の `scripts/bench_compute_ceiling.py` と同じ **`scripts/` 配下の
  独立スクリプト＋純関数テスト**方針が踏襲可能（`pipeline_inference.py` 非改変）．結果は `results/*.jsonl` へ追記（SL1 と同形式）．
- **想定コスト/リスク**: target 31B の CPU 単機 greedy 生成は低速（実機 i5 で ~7s/token，実行ホスト 64 コアなら数倍速いが
  依然重い）．**参照生成の総トークン数を絞る**（プロンプト数×最大新規トークン数を小さく）ことで数十分〜数時間に収める設計を
  planner が置くべき．採択率は決定的量（greedy）ゆえ n=1 でも安定だが，プロンプト多様性が結論を左右する（A-2 の教訓）．
- **人間判断の要否**: 現時点では **needs-human 事項は発生していない**（SL2 はローカル完結・可逆）．ただし planner が
  「31B 参照生成を実機クラスタ経由で取得する」設計を選ぶ場合は B7 包括承認内の非破壊 SSH で可（それでも破壊的操作なし）．
  ローカル完結（B-3 の HF キャッシュ利用）が最短かつクラスタ無負荷で推奨．

**出典**: Leviathan et al. 2023, arxiv.org/abs/2211.17192（採択率→期待トークン式）; apoorvumang/prompt-lookup-decoding,
github.com（n-gram draft・入力接地型で 2〜4×・max n-gram=3/cont=10/greedy）; Online Speculative Decoding, arxiv.org/abs/2310.07177
（低採択率が利得を削ぐ）; vLLM `vllm.v1.spec_decode.metrics`, docs.vllm.ai（acceptance rate / mean acceptance length 定義）;
zenml.io・aphrodite prompt-lookup 解説（n-gram 適用範囲）; ローカル資産: `~/.cache/huggingface/hub/models--google--gemma-4-{31B-it,E2B}`,
`models/splits/split_info.json`, `pipeline_inference.py:110-129,1600-1618`, `results/Iter4.jsonl`．

### 検討・計画 (Iter6)

**担当**: 計画フェーズ subagent（2026-07-19）．`### 調査 (Iter6)` の結論（A-1 写像式・A-2 prompt-lookup の適用域・A-3
小 draft の測定法・B-1〜B-5 のローカル資産と非改変性）を受け，単一レバー **SL2（draft 採択率のオフライン見積もり）**を
実装可能な粒度へ落とし込んだ．本フェーズは実機クラスタへの接続・deploy・推論を一切行わない（コード／config／HF キャッシュ
メタデータの読み取りのみ）．**確認事実**: `google/gemma-4-31B-it`（target, vocab=262144, `final_logit_softcapping=30.0`）と
`google/gemma-4-E2B`（draft, `model_type=gemma4`, text vocab=**262144 一致**, softcapping=30.0, 35 層, hidden=1536,
単一 `model.safetensors` 9.6GB）が HF キャッシュにローカル在．**語彙・softcapping が完全一致**＝**token ID を直接 exact-match
照合可**．softcapping は単調変換で greedy argmax を変えない（B-1）ため，参照 argmax はモデル logits の argmax で取得してよい．

#### 1. 仮説

B3（speculative decoding）の**期待値側の因子＝draft 採択率**を，relay 改修・再デプロイなしにオフライン単一プロセスで
見積もれる．具体的には，target(31B) の greedy 参照系列に対し draft(E2B) が提案する K トークンの exact-match 採択長を測れば，
**1 トークンあたり採択率 α** と **K∈{2,4,8} ごとの平均採択長 a_K** が確定する．α が十分高ければ（例 α≳0.5 で a_2≳1.5），
SL1 の実機 ratio_K（0.753/0.378/0.213）と組んで B3 の実効利得レンジを数値で括れる（go 方向を強める）．逆に α≈0（a_K≈1）なら
draft は無力で，SL1 の compute 天井があっても B3 の実効利得は崩れる（no-go 方向）．prompt-lookup（n-gram）は入力接地型
タスクでのみ α>0，開放チャットでは α≈0 という A-2 の傾向が，タスク種別層別で再現するはずである．

#### 2. 単一レバー・変更内容

**単一レバー**: 「診断対象を，SL1 の compute マイクロベンチ（GEMV vs GEMM）から，**draft/target 対の採択率オフライン測定**へ移す」
の 1 点．クラスタ・relay・`pipeline_inference.py` は一切変更しない（固定）．draft 戦略の位置づけは以下に一本化する（単一レバー原則）:

- **主軸（go/no-go の決定的入力）＝小 draft モデル `gemma-4-E2B`**．理由: (i) 語彙・softcapping が target と一致し token ID 直接
  照合可，(ii) 軽量・ローカル在で単一プロセス完結，(iii) 開放チャットでも効く汎用性（prompt-lookup の弱点 A-2 を持たない），
  (iv) B3/SL3 が実運用で載せる draft の実体に最も近い．**SL2 完了判定と B3 期待値レンジの数値化は E2B の α・a_K で行う**．
- **補助（安価な比較・A-2 の検証のみ）＝prompt-lookup（n-gram, max=3・cont=10）**．追加モデルゼロ・実装数十行．**主指標には
  用いず**，タスク種別層別で「入力接地型では α>0／開放チャットでは α≈0」を可視化して E2B を主軸に据える妥当性を裏付けるだけの
  位置づけとする（結論は E2B に依存させ，prompt-lookup の結果で go/no-go を動かさない）．

**変更ファイル（新規のみ・クラスタ非接触，Iter5 の `scripts/` 独立スクリプト方針を踏襲）**:
- 新規 `scripts/estimate_draft_acceptance.py`．責務: target(31B) の greedy 参照生成 → draft(E2B) 提案 → exact-match 採択判定 →
  α・a_K 算出 → SL1 ratio_K との合成で B3 実効利得レンジを出力．
- 新規 `tests/test_estimate_draft_acceptance.py`（純関数の単体テスト）．
- （`pipeline_inference.py`・`tools/*.py` は非改変．結果は `results/draft_acceptance.jsonl` へ追記＝SL1 と同形式．）

**スクリプト設計**:
- **(a) プロンプト集合（層別）**: 4 カテゴリ × 4 件 = **計 16 プロンプト**をスクリプト内に定数定義する．カテゴリは
  `open_chat`（開放チャット・入力非接地，既存デモ `"Hello!"` を含む）／`summarization`（短文書＋要約指示）／`doc_qa`（提示文脈への
  QA）／`code`（短いコード補完・編集）．入力接地型 3 種を含めることで prompt-lookup を過小評価しない公平な設計にする（A-2）．
- **(b) トークナイズ**: `pipeline_inference.py:_tokenize()`（:110-129, Gemma-4 chat template・`add_generation_prompt=True`）を
  複製し，target・draft とも同一経路でトークナイズする（生 encode は IT モデルで挙動が変わるため不可，B-2）．
- **(c) target 参照生成**: `AutoModelForCausalLM.from_pretrained("google/gemma-4-31B-it", torch_dtype=bfloat16,
  device_map="cpu", local_files_only=True)` を単一プロセスにロードし，各プロンプトで **greedy に最大 `N_MAX_NEW_TOKENS` 個**
  生成して参照 argmax token 列を得る（`torch.set_num_threads` は SL1 と同条件・EOS で打ち切り）．判定則は `:1600-1618` の
  greedy を踏襲するが，softcapping は argmax 不変（B-1）のため **model logits の argmax を参照に採る**（softcap 適用は任意，
  適用しても結果同一）．参照系列はカテゴリ別に `results/draft_acceptance.jsonl` へ保存し，再現・再解析可能にする．
- **(d) draft 提案と採択判定（純関数化）**: 参照系列上を **検証ステップのブロック単位**で歩く．各ブロック開始プレフィックス
  （chat template + 既確定 token）を draft(E2B) に食わせ **greedy に K トークン自己回帰提案**，参照 argmax と逐位置 exact-match
  照合して**最初の不一致まで**を採択（最大 K）＋bonus 1 個で前進（accepted+1）．これを K∈{2,4,8} それぞれで実施し，
  ブロック集計から **経験的 a_K（1 検証ステップで確定するトークン数）**を得る．
- **(e) α と写像の相互検証**: K 非依存の**1 位置あたり採択率 α**（＝「真プレフィックス条件下で draft の greedy top-1 が target
  argmax に一致する位置割合」）も別途集計する．A-1 の式 **E=(1-α^(K+1))/(1-α)** で α から予測した a_K と，(d) の経験 a_K を
  併記し，iid 幾何近似の妥当性（実際は序盤位置ほど採択されやすく非 iid）を可視化する．
- **(f) B3 実効利得レンジ（SL1×SL2 の合成・本イテレーションの成果物）**: 実機 SL1 の `ratio_K`（K=2/4/8=0.753/0.378/0.213）と
  経験 a_K を組み，**1 検証サイクルで a_K トークンを K 位置 GEMM 1 回のコストで確定する**関係から，per-token compute の実効
  利得レンジ（例 `a_K /(K·ratio_K)` 等の候補式）を数値出力する．**最終的な式選定と解釈は analyst に委ねる**が，スクリプトは
  a_K・ratio_K・その積/比を素の数値として吐き，analyst が B9 go/no-go の材料にできる形にする．
- **(g) prompt-lookup（補助）**: draft を使わず直近 n-gram（max=3）をプレフィックス内マッチ・continuation=10（K で切詰）で提案し，
  同じ採択判定でカテゴリ別 α を出す．主指標に混ぜず，**A-2 検証用の層別テーブルとして併記**する．

**定数化（マジックナンバー回避）**: `K_VALUES=(2,4,8)`（SL1 と一致）・`N_MAX_NEW_TOKENS=48`・`NUM_PROMPTS=16`（4 カテゴリ×4）・
`NGRAM_MAX=3`・`NGRAM_CONT=10`・`TARGET_DTYPE=torch.bfloat16`・`DRAFT_DTYPE=torch.bfloat16`．

#### 3. 実験規模（target 31B CPU 生成が律速．具体値）

- **参照生成の総量上限**: 16 プロンプト × 最大 48 新規トークン = **target forward ≤ 768 ステップ**．SL1 実測（本 research-cycle
  実行ホスト 64 コアで GEMV 1 層中央値 80.97ms）から 60 層＋lm_head で 1 トークン概ね数秒と見積もり，**総計 1〜2 時間程度**を想定
  （EOS 早期打ち切りで実際は下振れ）．超過・OOM 時のフォールバック: (i) `N_MAX_NEW_TOKENS` を 48→32 に，(ii) 31B 単機ロードが
  RAM 逼迫（bf16 59GB / available 89GB）で不安定なら `models/splits` の layer ストリーム（load→compute→free）に切替．いずれも
  クラスタ非接触で可逆．
- **draft(E2B) の生成コストは無視できる**（35 層・hidden 1536・9.6GB）．採択判定はブロック当たり最大 K 回の draft forward のみ．
- **統計的安定性**: greedy ゆえ α・a_K は**決定的（run 間分散ゼロ）**．推定誤差は位置数に支配される．各カテゴリ 4 プロンプト×
  最大 48 位置 ≈ **150 位置/カテゴリ**を確保し，カテゴリ別 α を安定推定する（全体 α は ≈600 位置）．

#### 4. 成功条件（measurable．「SL2 が完了」と言える基準）

実装・実行の完了条件（決定的）:
1. `scripts/estimate_draft_acceptance.py` が単一プロセス（クラスタ・relay 非接触）で完走し，**E2B draft** について
   (a) 全体 α，(b) カテゴリ別 α（`open_chat`/`summarization`/`doc_qa`/`code`），(c) K∈{2,4,8} ごとの**経験 a_K** を出力する．
2. α→a_K の写像式 `E=(1-α^(K+1))/(1-α)` で予測した a_K と経験 a_K を**併記**し，乖離を数値で示す．
3. prompt-lookup（補助）のカテゴリ別 α を併記し，**入力接地型>開放チャット**の傾向（A-2）を層別テーブルで可視化する
   （傾向の向きが出れば可．prompt-lookup では go/no-go を判定しない）．
4. **SL1×SL2 合成**: 実機 ratio_K（0.753/0.378/0.213）と経験 a_K を組んだ B3 実効利得レンジの素数値（a_K・ratio_K・積/比）を
   出力する＝本イテレーションの成果物（analyst が B9 go/no-go の材料に使える形）．
5. 純関数（exact-match 採択判定＝最初の不一致で打切り＋bonus，ブロック前進 accepted+1，α↔a_K 写像，n-gram lookup 提案）の
   単体テストが **green（最低 4 件）**，`uv run python -m py_compile scripts/estimate_draft_acceptance.py
   tests/test_estimate_draft_acceptance.py` がエラー無し．
6. 変更は `scripts/estimate_draft_acceptance.py`／`tests/test_estimate_draft_acceptance.py` の**新規 2 ファイルのみ**
   （`pipeline_inference.py` 他既存本体は非改変），結果は `results/draft_acceptance.jsonl` へ追記．

判定の解釈指針（判定は analyst．ノイズは決定的量ゆえ位置数依存）:
- α は greedy で決定的（run 分散ゼロ）．**カテゴリ別 α は位置数 ≥50 を満たすものだけを有意な層別値**として扱う．
- 解釈ガイド（analyst 向け・拘束はしない）: **E2B の全体 a_2≳1.5（α≳0.5）**なら SL1 の compute 天井が実効利得として現実化する
  下限条件クリア＝B3 go 方向を強める．**a_K≈1（α≲0.1）**なら draft 無力で B3 実効利得は崩れ no-go 方向．中間は SL1 ratio_K との
  積で期待値レンジを提示し B9 で人間に諮る．

#### 5. 実装フェーズ（rc-implementer）への申し送り

- **対象ファイル・キー**: 新規 `scripts/estimate_draft_acceptance.py`（定数 `K_VALUES`／`N_MAX_NEW_TOKENS`／`NUM_PROMPTS`／
  `NGRAM_MAX`／`NGRAM_CONT`／`TARGET_DTYPE`／`DRAFT_DTYPE`，純関数 `accepted_length()`（exact-match 打切り）／
  `simulate_block_walk()`（ブロック前進で a_K 集計）／`alpha_to_expected_len()`（A-1 写像）／`ngram_lookup_propose()`，
  参照生成 `generate_target_reference()`／draft 提案 `draft_propose()`），新規 `tests/test_estimate_draft_acceptance.py`．
  トークナイズは `pipeline_inference.py:_tokenize()`（:110-129）を複製，判定則は `:1600-1618` の greedy を踏襲（softcap は
  argmax 不変ゆえ任意）．実行は `unset VIRTUAL_ENV && uv run python scripts/estimate_draft_acceptance.py`（SL1 と同様の
  `VIRTUAL_ENV` 汚染回避）．
- **ローカル資産パス**: HF キャッシュに `google/gemma-4-31B-it`・`google/gemma-4-E2B` 在（`local_files_only=True` で単機ロード）．
  E2B は `AutoModelForCausalLM`／`Gemma4ForConditionalGeneration` の text バックボーンとして読み込む（vocab 262144 一致）．
- **やらないこと**: 実機 relay・deploy・`pipeline_inference.py` 改変・分散推論は本イテレーションでは一切行わない（**ローカル
  単一プロセス完結・クラスタ本体への大改変を要さないため needs-human 事項なし**＝B10 の申し送りに非抵触）．B3 本体（SL3:
  relay プロトコル改修）は B9 として温存し，本 SL2 の α・a_K・実効利得レンジを SL1 と合わせて別途人間に go/no-go を諮る．

### 実装（HF キャッシュ revision 修正）(Iter6)

**担当**: 実装フェーズ subagent（2026-07-19）．`### 実験 (Iter6)` が報告した `AutoTokenizer.from_pretrained` 失敗
（HF キャッシュ `refs/main` の不完全スナップショット参照）の対処として，backlog B11 で自動選定された対処案 (A) を
実施した．実機クラスタへの接続・deploy・SSH・HF キャッシュの書き換えは一切行っていない（`~/.cache/huggingface` は
読み取り確認のみ）．

- **裏取り**: `~/.cache/huggingface/hub/models--google--gemma-4-31B-it/snapshots/` を確認し，完全なスナップショット
  （`config.json`・`tokenizer.json`・`model-0000{1,2}-of-00002.safetensors` を含む）が `fb9ae262347c3945692f09a612f8bb189def854f`
  （および内容同一の `3548789868c5356dbf307c98e6f609007b82b3eb`）であることを確認した．journal 記載のハッシュと一致．
  **draft(`google/gemma-4-E2B`) 側も同様の不整合を発見**: `refs/main` は `d29ff6b45f081a49ee2733a859c9c9c2d95d1a6f` を
  指すが，このハッシュに対応する snapshot ディレクトリ自体が存在しない（target よりさらに壊れた状態）．実在する
  スナップショットは `19f17d3255f458aa49ebe8843d65ec7b7386db1f`（Jul 10）と `63db66a33dc06d58c02b1e887446e103c202602c`
  （Jul 8）の 2 つで，両者は全ファイル（`config.json`/`tokenizer.json`/`model.safetensors`/`generation_config.json`/
  `tokenizer_config.json`）の blob ハッシュが完全一致（内容同一の重複スナップショット）．より新しい
  `19f17d3255f458aa49ebe8843d65ec7b7386db1f` を採用した．
- **変更ファイル**: `scripts/estimate_draft_acceptance.py` のみ．
  - 定数追加（`TARGET_MODEL_NAME`/`DRAFT_MODEL_NAME` 直後，:52-60）: `TARGET_MODEL_REVISION =
    "fb9ae262347c3945692f09a612f8bb189def854f"`，`DRAFT_MODEL_REVISION = "19f17d3255f458aa49ebe8843d65ec7b7386db1f"`．
    直前にコメントで固定理由（`refs/main` 不整合の暫定回避，キャッシュ自体は非改変）を明記．
  - `main()` 内の 3 箇所の `from_pretrained` 呼び出し（旧 :542-549 相当）に `revision=TARGET_MODEL_REVISION` /
    `revision=DRAFT_MODEL_REVISION` を追加（`AutoTokenizer.from_pretrained`／target `AutoModelForCausalLM.from_pretrained`／
    draft `AutoModelForCausalLM.from_pretrained` の全て）．他のロジック・定数・プロンプト集合・純関数は無改変．
- **検証**: `unset VIRTUAL_ENV && uv run python -m py_compile scripts/estimate_draft_acceptance.py
  tests/test_estimate_draft_acceptance.py` → エラー無し．`uv run pytest tests/test_estimate_draft_acceptance.py -v`
  → **30 passed**（回帰無し，モデルロードを伴わない純関数テストのみで revision 追加の影響を受けない範囲）．
  `uv run pytest`（全体）→ **83 passed**．実際のモデルロード・生成の実行（revision 固定が実機で解決するかの確認）は
  本フェーズでは行っていない（次の実験フェーズへ委譲）．
- **申し送り**: `AutoModelForCausalLM.from_pretrained(...)` が `Gemma4ForConditionalGeneration` アーキテクチャを
  直接ロードできるかは，実験フェーズの診断（`AutoConfig`/`AutoTokenizer` は revision 指定で成功済み）で有望だが
  実際の重みロードは未確認．revision 修正後も同様のロード時エラーが出た場合は，前回申し送りどおり
  `AutoModelForImageTextToText` 等への切替を検討すること．draft 側 revision の選定は「2 スナップショットが内容同一」
  という確認に基づく（内容が違えば選定基準を人間に諮る必要があったが，今回は不要だった）．

### 実験 (Iter6)

**担当**: 実験フェーズ subagent（2026-07-19T15:26 JST）．`unset VIRTUAL_ENV && uv run python
scripts/estimate_draft_acceptance.py` をバックグラウンド起動（nohup）．**結果: 失敗（tokenizer ロード段階，即時終了・
数十分待たず数秒で `ValueError`）**．`results/draft_acceptance.jsonl` は未生成（既存 `results/*.jsonl` は無変更）．

#### エラー内容

`AutoTokenizer.from_pretrained(TARGET_MODEL_NAME, ...)` にて:

```
ValueError: Couldn't instantiate the backend tokenizer from one of:
(1) a `tokenizers` library serialization file,
(2) a slow tokenizer instance to convert or
(3) an equivalent slow tokenizer class to instantiate and convert.
You need to have sentencepiece or tiktoken installed to convert a slow tokenizer to a fast one.
```

#### 原因調査（実装フェーズが懸念していた `Gemma4ForConditionalGeneration` 非対応ではない）

- `~/.cache/huggingface/hub/models--google--gemma-4-31B-it/refs/main` が指すスナップショット
  `b9ea41a2887d8607f594846523f94c6cc75ac8a4`（更新日時 2026-07-19T10:41，本日の SL1/Iter4 系実行と時刻が近接）は
  `config.json` **のみ**を含む不完全なスナップショットであり，`tokenizer.json`・weights を含まない．
- 一方，同リポジトリ配下には完全なスナップショット（`3548789868c5356dbf307c98e6f609007b82b3eb` と
  `fb9ae262347c3945692f09a612f8bb189def854f`）が存在し，`tokenizer.json`・`model-0000{1,2}-of-00002.safetensors`
  （計 59GB）・`config.json`（`main` と同一内容）を保持している．`local_files_only=True` かつ revision 未指定のため
  `AutoTokenizer`/`AutoModelForCausalLM` は `refs/main` の不完全なスナップショットを解決してしまい失敗した．
- **診断（読み取りのみ・キャッシュ改変なし）**: 完全なスナップショット `fb9ae262...` を revision 指定で明示的に読み込むと，
  `AutoConfig.from_pretrained(...)` は `architectures=['Gemma4ForConditionalGeneration']`／
  `type(cfg) in MODEL_FOR_CAUSAL_LM_MAPPING == True`（transformers 5.9.0 で登録済み）となり，`AutoTokenizer` も
  `GemmaTokenizer` として正常ロードできた．すなわち実装フェーズが未検証としていたリスク（`Gemma4ForConditionalGeneration`
  を `AutoModelForCausalLM` が扱えるか）は，**この診断範囲では解消**（`refs/main` を経由しなければロード可能）．
  ただし重み込みの実フォワードまでは実行していないため，モデル本体の完全な動作確認はまだ済んでいない．
- `refs/main` の不整合発生原因は未特定（本日 10:41 前後に別プロセスが `local_files_only=False` でオンライン解決した際，
  config.json のみ取得できて weights/tokenizer の再取得が走らなかった可能性がある，推測の域を出ない）．

#### 実施した操作・変更していないもの

- 診断のための `AutoConfig`/`AutoTokenizer.from_pretrained(..., revision="fb9ae262...")` 読み取りのみ実行．
  `refs/main` ファイルは読み取り前後で内容不変（`b9ea41a2887d8607f594846523f94c6cc75ac8a4` のまま）を確認済み．
- `scripts/estimate_draft_acceptance.py`・HF キャッシュとも一切変更していない（禁止事項の破壊的操作も未実施）．
- `python -c "import sentencepiece"` / `import tiktoken"` は共に `ModuleNotFoundError`（`pyproject.toml` の
  dependencies に含まれていない）．ただし `tokenizer.json`（fast tokenizer serialization）が完全スナップショットに
  存在するため，正しい revision が解決されれば sentencepiece/tiktoken は不要（`GemmaTokenizer` は `tokenizers` backend
  で動作，診断ログで確認済み）．

#### 申し送り（analyst/planner 判断事項）

- 対処案の候補（判断は analyst/planner に委ねる）:
  (A) `scripts/estimate_draft_acceptance.py` の `AutoTokenizer`/`AutoModelForCausalLM.from_pretrained` 呼び出しに
      `revision="fb9ae262347c3945692f09a612f8bb189def854f"`（または `3548789868c5356dbf307c98e6f609007b82b3eb`）を
      明示指定する．
  (B) ローカル HF キャッシュ側で `refs/main` を完全スナップショットのハッシュに修復する（キャッシュ操作，人間確認が
      望ましい可能性あり）．
- 環境未検証事項（次回実験時に確認要）: 上記いずれかの対処後，実際に target 31B の重みロード＋ greedy 生成が完走するか
  （メモリ 125GB 中空き 87GB，bfloat16 で target 62GB 程度＋draft 19GB 程度と見積もられ理論上は収まる想定だが未実測）．

---

### 実装 (Iter6)

**担当**: 実装フェーズ subagent（2026-07-19）．`### 検討・計画 (Iter6)` §2〜§5 の設計どおり，新規 2 ファイルのみを追加した
（`pipeline_inference.py` を含む既存ファイルは一切改変していない）．実機クラスタへの接続・deploy・SSH は行っていない．
モデルの実ロード・実推論（target 31B・draft E2B）はこのフェーズでは実行していない（次の実験フェーズへ委譲）．

#### 変更ファイル

- 新規 `scripts/estimate_draft_acceptance.py`（624 行）: 計画 (a)〜(g) を実装．
  - 定数: `K_VALUES=(2,4,8)`／`N_MAX_NEW_TOKENS=48`／`NUM_PROMPTS=16`／`NGRAM_MAX=3`／`NGRAM_CONT=10`／
    `TARGET_DTYPE=DRAFT_DTYPE=torch.bfloat16`／`NUM_THREADS=4`／`NUM_INTEROP_THREADS=1`（SL1 と同条件，計画 (c)）／
    `SL1_RATIO_BY_K={2:0.753,4:0.378,8:0.213}`（Iteration 5 実測値）．
  - `build_prompt_set()`: 4 カテゴリ（`open_chat`/`summarization`/`doc_qa`/`code`）×4 件＝16 プロンプト．`open_chat` に
    既存デモの `"Hello!"` を含む．`summarization`/`doc_qa`/`code` は入力接地型（prompt-lookup を過小評価しない設計，A-2）．
  - `tokenize_prompt()`: `pipeline_inference.py:_tokenize()`（:110-129）を複製（chat template + `add_generation_prompt=True`）．
  - `generate_target_reference()` / `draft_teacher_forced_predictions()`: モデル呼び出しを伴う関数として分離（単体テスト対象外）．
    **設計判断（計画からの効率化）**: draft の block 提案は，計画で想定された「各ブロックで K トークンずつ自己回帰生成」の
    代わりに，**参照系列全体に対する draft の 1 回の teacher-forced forward**から全位置の greedy top-1 予測を事前計算し，
    `make_block_propose_fn()` で `simulate_block_walk()` の `propose_fn` に変換する方式にした．根拠:
    一致が続く区間では「draft の自己回帰提案」と「真の参照プレフィックスを条件とした teacher-forced 予測」は同一コンテキスト
    から計算されるため常に一致し，判定に使うのは最初の不一致位置までなので，不一致後の自己回帰予測との差は判定へ影響しない
    （`draft_teacher_forced_predictions()` の docstring に理由を明記）．これにより 1 プロンプトあたり draft forward が
    K×プロンプト数回ではなく 1 回で済み，実験フェーズの実行時間を大幅に削減できる見込み．計画の意図（exact-match 採択判定・
    K∈{2,4,8}集計・α算出）は変えていない．
  - 純関数（単体テスト対象）: `accepted_length()`（exact-match 打切り＋bonus+1）／`simulate_block_walk()`（ブロック前進で
    a_K 集計，終端クリップ処理を追加）／`alpha_to_expected_len()`（A-1 写像，α=1.0 の特異点を極限値 K+1 で処理）／
    `compute_alpha()`（teacher-forced 一致率）／`ngram_lookup_propose()`（prompt-lookup, 直近一致優先）／
    `compute_ngram_alpha()`／`aggregate_alpha_by_category()`（位置数重み付き）／`compute_effective_gain_candidates()`
    （SL1×SL2 合成，`product_a_k_ratio_k`・`gain_over_baseline` を算出）．
  - `main()`: target/draft モデルをロードし，全プロンプトを処理して `results/draft_acceptance.jsonl` へ 1 レコード追記．
- 新規 `tests/test_estimate_draft_acceptance.py`（30 件）: `accepted_length`（全一致／即不一致／部分一致／K=1相当／
  提案空）5 件，`simulate_block_walk`（常時一致／常時不一致／終端クリップ／K≤0拒否）4 件，`alpha_to_expected_len`
  （具体値検証／α=0／α=1極限／範囲外拒否／K≤0拒否）5 件，`compute_alpha` 3 件，`ngram_lookup_propose`（直近一致／
  不一致／continuation打切り）3 件，`compute_ngram_alpha`（入力接地型で正／開放チャットでゼロ）2 件，
  `aggregate_alpha_by_category` 2 件，`compute_effective_gain_candidates` 2 件，`build_prompt_set` 2 件，
  `BlockWalkResult` frozen 検証 1 件．計画の最低 4 件要件を超える網羅度で実装した．

#### 検証結果

- `unset VIRTUAL_ENV && uv run python -m py_compile scripts/estimate_draft_acceptance.py
  tests/test_estimate_draft_acceptance.py` → エラー無し．
- `unset VIRTUAL_ENV && uv run pytest tests/test_estimate_draft_acceptance.py -v` → **30 passed**（モデルロードなしの
  純粋関数テストのみ．target/draft の実ロード・実推論は含まない）．
- `unset VIRTUAL_ENV && uv run pytest`（既存スイート全体）→ **83 passed**（Iter5 時点の 53 passed + 新規 30 件，回帰無し）．

#### 計画との差異・申し送り

- **計画からの唯一の設計逸脱**: draft 提案を「ブロックごとの自己回帰生成」から「1 回の teacher-forced forward」に効率化
  （上記「設計判断」参照）．exact-match 判定・K∈{2,4,8}集計という計画の意図・出力形式は変えていない．次の実験フェーズで
  実行した際，何らかの理由でこの前提（一致区間での自己回帰予測とteacher-forced予測の同値性）が成立しないと判明した場合は
  （通常は成立するはずだが），`draft_propose()` 相当の素朴な自己回帰実装への切り替えを検討すること．
- **`AutoModelForCausalLM.from_pretrained` の適用可否は未検証**: 両モデルの `config.json` の `architectures` は
  `Gemma4ForConditionalGeneration`（マルチモーダルラッパークラス）であり，`AutoModelForCausalLM` が this checkpoint を
  直接ロードできるかは実機で未確認．計画 §5 の指示どおり `AutoModelForCausalLM.from_pretrained(...)` を実装したが，
  実験フェーズで `ValueError` 等が出た場合は `AutoModelForImageTextToText` または `Gemma4ForConditionalGeneration` への
  切り替えを検討すること（本実装フェーズではモデル実ロードを行っていないため未検証，計画・制約どおり）．
- 実験を開始してよい状態: **可**（新規ファイル 2 点のみ，構文健全性・純関数テスト・既存スイート回帰確認済み．
  `pipeline_inference.py` 他既存ファイルは非改変）．

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
