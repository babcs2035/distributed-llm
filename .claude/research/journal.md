# 実験ジャーナル: distributed-llm

research-cycle が読み書きする実験ジャーナル．**新しいイテレーションを常に先頭へ挿入する（逆時系列）**．
1 イテレーション = 単一レバー変更．各ブロックに仮説・単一レバー・成功条件（planner 記入）と，
変更・結果・判定・学び（reflector 記入）をまとめる．

---

## Iteration 4

### 考察・次計画 (Iter4)

**担当**: 考察・次計画 subagent（2026-07-19）．分析(解釈) の結論（本ブロック `### 分析(解釈) (Iter4)`）を受け，
単一レバー「B0: per-stage compute/recv dt の内訳記録」の採否を確定し，次イテレーション（Iteration 5）の方向を決めた．
実機への新規接続・実行はしていない（記録の読み取りとコミット操作のみ）．

**1. 採否判定: 採用（adopt）**

- **判定根拠**: B0 は診断（計測）レバーであり，判定対象は「7s/token の計算 vs 通信内訳を確定できたか」．計画 §3 の
  成功条件 4〜6 を実機 3 run で全て充足した（条件4: `n_ranks_reporting=50/50`×3，条件5: `compute+send+residual ==
  rank0_step_dt` が丸め誤差すら無く厳密一致・X/Y/Z 数値言明可，条件6: 再デプロイなし n=3・中央値集計・step0 分離）．
  実装フェーズも `pytest` 45 passed（既存 38＋差分 7）・変更 3 ファイル厳守・`pipeline_inference.py` 非改変を満たす．
  実測（3 run 中央値）は **compute≈92.0%・send≈0.32%・residual≈7.6%** で，「ITL≈7s/token は計算律速」を確定した．
- **追加反復の要否**: 不要．内訳比率は決定的（純関数集計）な量で run 間ばらつきが 1pp 未満（compute% レンジ 0.27pp），
  弁別したい「compute ≫ residual ≫ send」の桁違いの大小関係はノイズの数十〜数百倍大きく，n=3 で判定は反転しない．
- **非自明な学び（次の自分向け）**: (i) **residual 7.6% は「純粋な通信/待機」ではない**．実コード上，最終 rank の
  final_norm＋lm_head（5376×語彙数の行列積）＋argmax＋全語彙 topk/診断（`pipeline_inference.py:1600-1622`）は
  `compute dt` にも `send` にも計上されず全て residual に落ちる．正しくは residual ＝段間同期（recv 待ち＋ACK 往復＋
  Gloo/Python オーバーヘッド）＋最終 rank の未計上計算，と分解すべき．(ii) 調査(Iter4)が B2（診断ログ削減）へ付けていた
  「compute dt に含まれる可能性が高い」という見立ては**実コード上は否**（f-string 評価順で `compute dt` が先に確定し，
  `hidden_mean/std/...` はその後に走るため 92% の compute には非含）．これらは B1/B2 の期待効果を大きく下げる知見である．

**2. このレバーの収束状況**

- B0 で「支配項は 50 段の逐次 CPU 計算（float32・4 コア GEMV）で 92%，通信（生転送）は 0.3% で無視できる」が確定し，
  **「計算律速か通信律速かの弁別」という診断課題は完了（収束）**した．Iter1〜4 と続いた「収集ツールに閉じた非侵襲な
  基盤/診断」系レバー（①永続化 → (a)RESULT 照合 → (b)levers 堅牢化 → B0 内訳診断）は，ここでやり切った．
- したがって次は「診断」を離れ，**支配項（92% の compute）そのものを攻めるレバー**へ移す段である．ただし B0 が明らかにした
  副次事実（residual の内実＝段間同期＋最終 rank の未計上計算）から，B1（WORLD_SIZE 絞り込み）は Σcompute 不変で
  残差止まり，B2（診断ログ削減）は compute dt 非含で残差の一部止まり，といずれも支配項に効かないことが確定した．

**3. 次に振るレバーの決定（Iteration 5）: B3 を最小サブレバーへ分解し，SL1（compute 上限の local マイクロベンチ）を自動選定**

- **状況**: 分析(解釈) の推奨は B3（speculative decoding）＝支配項の「逐次性」を崩せる唯一の候補．ただし B3 本体は
  実装規模が大きい（draft モデル追加・relay プロトコル大改修・検証木・51 ノード再デプロイ）．research-cycle の自律判断
  ポリシー（可逆/小規模は自動選択・不可逆/大規模は人間判断）に照らし，B3 を**そのまま Iteration 5 の単一レバーには
  しない**．まず実装規模を落とした最小サブレバーへ分解した（複数案，下記）．
- **B3 の最小サブレバー分解案（実装規模の小さい順）**:
  - **SL1（採用＝Iteration 5）: compute 側上限の local マイクロベンチ**．目的は，分析(解釈) が挙げた B3 の効き源
    (ii)「seq_len=1 の GEMV を K 位置まとめた GEMM に変え，4 コア CPU の演算強度/キャッシュ効率を上げる」が
    **この実機（i5-8350U・4 コア・float32・OpenBLAS/MKL）で実在するか**を，クラスタ本体・relay プロトコルに一切触れず
    測ること．具体的には，本モデルの実次元（`hidden_size=5376`，ノードあたり 1〜2 層相当の GEMM 形状）で
    `torch.set_num_threads(4)`・float32 のもと，seq_len=1（GEMV）と seq_len=K（K=2,4,8 の GEMM）の**1 トークンあたり
    実行時間**を比較する．規模: **小**（単一プロセスのローカル計測スクリプト・重み不要のランダムテンソルで足りる，
    `pipeline_inference.py` 非改変・再デプロイ不要・破壊的操作なし＝完全に可逆）．リスク: 低．決定価値: **大**．
    もし GEMM(K) の 1 トークンあたりコストが GEMV とほぼ同じなら B3 の compute 側利得は実在し，大投資の根拠になる．
    逆に GEMM(K)≈K×GEMV（この CPU では演算強度が上がらない）なら B3 の天井は残差償却（≈7.6% ぶん＝上限 1.08 倍程度）に
    縮み，**大規模な relay プロトコル改修に見合わない**ことが判明する＝B3 本体 go/no-go の決定的入力になる．
  - **SL2: draft 戦略の受理率オフライン検証**（prompt-lookup/n-gram draft か小 draft モデル）．K トークン提案の受理率を
    既存ログ/参照出力で見積もる．規模: 中（参照出力の取得に実機推論を要する場合あり）．relay プロトコル改修は不要だが
    B3 本体の期待値算定に必要．
  - **SL3: relay プロトコル改修（K トークン運搬＋検証を 1 往復で行う本体実装）**．規模: **大**・`pipeline_inference.py`
    ホットパス改変・51 ノード再デプロイを伴い**不可逆側**．どの draft 戦略でも実レイテンシ低減にはこれが避けられない．
- **決定（自律判断・path (a)）**: Iteration 5 の単一レバーを **SL1** とする．理由は，(1) B3 の最大の不確実性（compute 側
  利得がこの CPU で実在するか）を near-zero コストかつクラスタ非接触で潰せ，(2) B0 と同じ「作る前に測る」診断の系譜で
  単一レバー原則に整合し，(3) 完全に可逆で破壊的操作を含まないため**自律判断の範囲内**だからである．backlog に
  `## B8 [auto-decided 2026-07-19]` として記録した．
- **人間判断の申し送り（不可逆側の温存）**: SL3（relay プロトコル改修・再デプロイを伴う B3 本体）は**不可逆・大規模**で
  自律判断の範囲外であり，backlog に `## B9 [needs-human 2026-07-19]` として温存した．**SL1 の結果（compute 側利得の
  実在有無）を添えて，B3 本体着手の go/no-go を人間に諮る**方針．今回は path (a)（十分小さいサブレバーを自動選定）に
  該当するため `status="blocked"` にはせず `running` で進める．ただし透明性のため Slack 完了サマリーで
  `<@U08GLKY1QCW>` に「B3 本体は SL1 の結果を見て別途 go/no-go を諮る」旨を明記し，異論があれば上書きできるようにする．
- **可逆性**: 次に振るレバーの選定（SL1）であり可逆．破壊的操作を含まない（自動判断とした）．

**次イテレーションへの結論**: Iteration 4（B0 内訳診断）を採用で確定・収束（「7s/token は計算律速・compute 92%」を確定）．
Iteration 5 は，B3 を最小サブレバーへ分解した SL1（compute 側上限の local マイクロベンチ）を自動選定して開始する．
B3 本体（SL3: relay プロトコル改修）は不可逆・大規模のため needs-human として温存し，SL1 の結果を添えて go/no-go を諮る．

---

### 分析(解釈) (Iter4)

**担当**: 分析(解釈) subagent（2026-07-19）．`## Iteration 4` の全ブロック（調査・計画・実装・実験）と
`results/Iter4.jsonl`（3 run）を読み，さらに `pipeline_inference.py:1560-1729`（最終 rank／中間 rank の
per-stage 計時と診断ログの実コード）を Read して，単一レバー「B0: per-stage compute/recv dt の内訳記録」の
成否・調査一次推定との整合・次レバーへの示唆を解釈した．実機への新規接続・実行はしていない（記録の読み取りのみ）．

**前提（判定の枠組み）**: 本イテレーションの判定対象は「診断（計測）レバーが目的（7s/token の計算 vs 通信内訳の確定）を
達成したか」であり，レバー効果によるスループット改善そのものではない（B0 はホットパス非改変・掃引なしの計測 run）．
したがって Iter1 の「n が小さくノイズ幅未知でレバー効果を弁別できない」論点は，ここでは「内訳比率が run 間で安定し，
バケット間の大小関係が一意に読めるか」という形に置き換わる．

**1. 有意性・再現性: 3 run で内訳比率は安定，成功条件 4〜6 を全て充足．計算律速の判定はノイズに対して頑健**

- **run 間ばらつきはノイズ相当で小さい**: `rank0_step_dt_median_ms` は 7016〜7055ms（幅 39ms＝最大値の 0.55%），
  `compute_sum_ms_median` は 6443〜6498ms（幅 55ms＝0.85%），`send_sum_ms_median` は 21.5〜25.0ms（幅 3.5ms）．
  比率換算では compute 91.83%/92.09%/92.10%，send 0.31%/0.32%/0.35%，residual 7.86%/7.59%/7.54% で，**3 run とも
  compute≈92%・send≈0.3%・residual≈7.6% にほぼ一定**（compute% のレンジ 0.27pp，residual% のレンジ 0.32pp）．
- **判定は測定ノイズに対して頑健**: 弁別したい命題は「compute ≫ residual ≫ send」という**桁違いの大小関係**であり，
  その差（92% 対 7.6% 対 0.3%）は run 間変動（1pp 未満）の数十倍〜数百倍大きい．n=3 でも判定が反転する余地は無く，
  「計算律速」の結論は有意（noise ではなく signal）と断定できる．内訳比率という決定的（純関数集計）な量である点も，
  Iter1 型のノイズ問題を持ち込まない．
- **計画 §3 の成功条件（フェーズ4，判定は analyst）を全て充足**:
  - 条件 4（`n_ranks_reporting ≥ 45`）: 3 run とも **50/50**．許容幅 5 を使わず全 worker が報告．**充足**．
  - 条件 5（`compute_sum+send_sum+residual ≈ rank0_step_dt` の丸め内成立と，X/Y/Z の数値言明）: 3 run とも
    **厳密一致**（丸め誤差すら無し．`residual` を減算で導出する実装のため定義上一致するが，入力側集計にバグが無い
    ことの傍証）．「ITL≈7s/token のうち計算 Σcompute≈92.0%・送信 Σsend≈0.32%・残差≈7.6%」と数値言明でき，
    `X+Y+Z=100%`．**充足**＝B0 の目的（計算律速か通信律速かの確定）を達成．
  - 条件 6（`--stage-timing` を n≥3 回・再デプロイなし・代表値中央値・step0 は `prefill_recv_ms_by_rank` 別枠）:
    3 run 実施・冷開始交絡なし・中央値集計・step0 分離を確認．**充足**．
- 異常無し（`parse_warnings=[]`×3，`parse_ok=True`，`schema_version=2`，一部ノード到達不可・言語崩れ・発散・OOM 等の
  想定外挙動は無し）．

**2. 調査(Iter4)一次推定との整合: 「計算律速」を確定．ただし residual の内実は一部修正が要る**

- 調査の一次推定「ホップ数×1 ホップ固定レイテンシ＋各段逐次 CPU 計算の和が支配的．生帯域は律速でない」は，**支配項が
  各段逐次 CPU 計算である点は実測で確定**した（Σcompute≈6.47s が 7.02s の 92%）．seq_len=1・単一マイクロバッチの
  自己回帰デコードで 50 段を厳密逐次通過し，任意時刻に 1 段しか稼働しない構造が，そのまま「50 段の float32・4 コア
  GEMV の和」として ITL に現れている．**通信（send）は 0.3%＝無視できるほど小さい**（21KB/ホップ×50 の生転送は
  一次推定どおり律速でない）．
- **修正が要る点（residual の帰属）**: 「残差 7.6%＝recv 待ち＋ACK 往復＋Gloo/Python オーバーヘッド」と**単純に言い切ることは
  できない**．実コードを読むと，(i) 段間計時の起点 `_t`（`:1588,1694`）は layer ループ直前で，`compute dt`（`:1598,1704`）は
  layer ループ直後に確定する．(ii) 最終 rank の **final_norm＋lm_head（`F.linear(final_hidden, _lm_head)`＝5376×語彙数の
  行列積）＋argmax＋全語彙 topk/診断**（`:1600-1622`）は `compute dt` にも `send`（最終 rank は `sent to next` を持たない）
  にも計上されず，**全て residual に落ちる**．4 コア float32 での lm_head 行列積は非自明なコストであり，residual≈551ms の
  一部は「未計上の最終 rank 計算」である．したがって正確には **residual ＝ 段間同期（recv 待ち＋ACK 往復＋Gloo/Python
  オーバーヘッド，≈11ms/ホップ×50）＋ 最終 rank の lm_head・サンプリング・診断（compute dt 未計上分）** と分解すべきで，
  「純粋な通信/待機オーバーヘッド」ではない．いずれにせよ residual は 7.6% と小さく，**支配項が compute であるという結論は
  変わらない**．
- まとめると，言い切れる確定事項は「**ITL≈7s/token は 50 段の逐次 CPU 計算（float32・4 コア GEMV）の累積が支配（≈92%）で，
  通信（Gloo send/recv の生転送）は無視できる（≈0.3%）**」．残差 7.6% の内実は「段間同期＋最終 rank の未計上計算」であり，
  通信のみではない．

**3. 次レバーへの示唆: B1/B2 は支配項（92% の compute）に触れられず低効果．B3 のみが計算逐次性を攻める**

- **B1（WORLD_SIZE 絞り込み 51→21/11）は計算律速下では低効果**: 60 層の総計算量はノード分割の粒度に依らず一定であり，
  ホップ数を 50→10 に減らしても **Σcompute はほぼ不変**（ノードあたり層数が増え 1 段の計算時間は逆に増えるため，
  積＝総和は保存）．B1 が削れるのは residual（段間同期部分＝7.6% の一部）と send（0.3%）の一部に限られ，**上限で数 %**．
  かつ再デプロイ・実機 run・人間確認を要する．調査が B1 に与えていた「ホップ律速 vs 計算律速の弁別」という診断価値は
  **B0 が既に解消済み**のため，B1 の残る意義は小さい．**優先度は下げる**．
- **B2（毎ステップ診断ログ削減）も支配項に効かない**: 実コードで確認したところ，`compute dt` は f-string 評価順で
  `_time.monotonic()-_t` が**先に確定**し，同一ログ行の `hidden_mean/std/min/max`（`:1598,1704`）はその**後**に走るため，
  **診断リダクションは `compute dt`（92%）に含まれていない**．中間 rank の診断は `sent_to_next−compute`＝send バケット
  （合計 21ms＝負担ゼロに近い）へ，最終 rank の全語彙 topk/診断（`:1605,1609,1616,1619`）は residual へ落ちる．よって B2 で
  削減できるのは send（無視可能）と residual の一部（最終 rank 1 個ぶん）に限られ，**92% の compute には一切触れられない**．
  「安価だが効果は残差の一部＝上限 1% 未満」であり，ホットパス改変＋再デプロイ＋人間確認のコストに見合わない．調査が
  B2 に付けていた「compute dt に含まれる可能性が高い」という見立ては，実コード上は**否**（含まれない）と修正する．
- **B3（speculative decoding）が計算律速に唯一整合する方向**: 支配項が「1 トークンずつ 50 段を逐次通過する CPU 計算」で
  ある以上，レイテンシを下げるには**逐次性そのもの**を崩す必要がある．文献（FlowSpec/PipeDec）の 1.36–1.77× の出所は，
  (i) 段間同期・パイプライン充填の固定費（residual 相当）を K トークンに 1 回へ**償却**，(ii) seq_len=1 の GEMV を K 位置
  まとめた GEMM に変え **4 コア CPU の演算強度/キャッシュ利用効率を上げる**（K 位置は K× の単純逐次より安い），
  (iii) draft がパイプラインをより多くの位置で稼働させ利用率（現状≒1/51）を上げる，の 3 点にある．**計算律速下でも
  効く源が (ii) 演算強度改善という形で存在する**点が重要で，B1/B2 の「残差いじり」とは効きどころの桁が違う．ただし
  実装規模は大（draft モデル追加・relay プロトコル大改修・検証木・再デプロイ）で，単一レバー原則には過大．

**4. このイテレーション（B0）の採否: 採用（adopt）**

- B0 は計測（診断）レバーで，計画（§3 成功条件 1〜6）・実装（`pytest` 45 passed・変更 3 ファイル厳守）・実機実証
  （50/50 到達・内訳厳密一致・warning 無し）が全て揃い，目的「7s/token の計算 vs 通信内訳の確定」を達成した．
  レイテンシは下げない性質のレバーだが，Iter1〜3 と同じ「収集ツールに閉じた非侵襲な基盤/診断」系として**採用で確定**．
  追加反復は不要（内訳は決定的量で 3 run 安定，判定に曖昧さが無い）．

**次イテレーションへの推奨（単一レバー，1 つ）**: **B3（speculative decoding）を Iteration 5 のレバー方向とする**．
理由は，B0 で「92% が 50 段逐次 CPU 計算＝計算律速」が確定し，config `levers`／backlog の候補のうち**支配項（compute）を
攻められるのは B3 のみ**（B1 は Σcompute 不変で残差止まり，B2 は compute dt に非含で残差の一部止まり）だからである．
ただし B3 は実装規模が大きく単一レバー原則に対して過大なので，**考察・次計画フェーズは (a) 実機 deploy／プロトコル改修を
伴うため Slack で人間の go/no-go を取り，(b) 最小サブレバー（例: rank0 に小 draft モデル＋K=2 の 1 往復検証プロトタイプを
縮小 WORLD_SIZE で高速反復）へ分解する**ことを条件に据えること．B1/B2 は「残差 7.6%／送信 0.3% の一部を削る低効果レバー」
として優先度を下げ，必要になれば後続で扱う．

---

### 実験 (Iter4)

**担当**: 実験フェーズ subagent（2026-07-19）．実装済みの `--stage-timing` 拡張（本ブロック直下 `### 実装 (Iter4)`）を用い，
稼働中の実機クラスタ（51 ノード）に対し計画 §2-D の正式手順で測定 run を実施した．コード変更・再デプロイは行っていない
（既存稼働イメージのログ収集のみ）．

**1. 事前確認**

- `mise run status`（`uv run python tools/healthcheck.py`）: rank0（wafl-ctrl1）＋ rank1〜50（wafl100-139/200-209）の
  **51/51 ノードが Healthy**（SSH／Docker daemon／`distributed-llm` container running／モデル重み配置／MTU=1500 すべて OK）．
  再デプロイは不要と判断し，`mise run deploy` は実行していない．

**2. 実行コマンド（n=3 回，固定構成，掃引なし）**

```
unset VIRTUAL_ENV && uv run python tools/collect_results.py --iter Iter4 --stage-timing --prompt "Hello!"
```

`mise run predict:demo` ではなく上記直接呼び出しを使用（申し送りどおり，`predict:demo` タスク自体には
`--stage-timing` が渡されないため）．3 回とも正常終了（`appended 1 record to results/Iter4.jsonl`），
`results/Iter4.jsonl` は 0 行 → 3 行へ増加（各回実行直後に行数を確認し 1 行ずつの追記を確認済み）．

| run | timestamp (UTC) | tokens_per_sec | parse_ok |
|---|---|---|---|
| 1 | 2026-07-19T01:01:20Z | 0.0977 | True |
| 2 | 2026-07-19T01:04:32Z | 0.0982 | True |
| 3 | 2026-07-19T01:07:42Z | 0.0975 | True |

**3. `timing_breakdown` の内訳（n_ranks_reporting は全 run で 50/50，欠損なし）**

| run | compute_sum_ms_median | send_sum_ms_median | residual_ms_median | rank0_step_dt_median_ms | n_ranks_reporting |
|---|---|---|---|---|---|
| 1 | 6443.0 | 21.5 | 551.5 | 7016.0 | 50 |
| 2 | 6482.0 | 22.5 | 534.5 | 7039.0 | 50 |
| 3 | 6498.0 | 25.0 | 532.0 | 7055.0 | 50 |

- **検算**（`compute_sum + send_sum + residual == rank0_step_dt`）: 3 run とも厳密一致（run1: 6443.0+21.5+551.5=7016.0，
  run2: 6482.0+22.5+534.5=7039.0，run3: 6498.0+25.0+532.0=7055.0）．丸め誤差すら無く成立（`build_timing_breakdown` が
  残差を減算で導出する実装のため定義上一致するが，入力側の中央値集計にバグが無いことの確認として有効）．
- **比率換算**（参考，判定は分析(解釈)フェーズが行う）: compute 比率 91.8%/92.1%/92.1%，send 比率 0.31%/0.32%/0.35%，
  residual 比率 7.86%/7.59%/7.54%．3 run でほぼ一定．
- **run 間ばらつき**: `rank0_step_dt_median_ms` は 7016〜7055ms（幅 39ms，最大値の 0.55%）．`compute_sum_ms_median` は
  6443〜6498ms（幅 55ms，0.85%）．`send_sum_ms_median` は 21.5〜25.0ms（幅 3.5ms）．いずれも粗く見て run 間変動は小さい
  （詳細な統計判定は次フェーズに委ねる）．

**4. 異常の有無**

- `parse_warnings` は 3 run とも空配列（`[]`）．SSH 失敗・負差分除外の warning は一切無かった．
- `n_ranks_reporting=50`（＝rank1〜50 の全 worker）が 3 run とも達成．計画の成功条件 4（`≥45`）を余裕をもって満たす．
  50 ノード全到達という意味で，一部ノード到達不可等の障害も発生しなかった．
- `schema_version=2`，`parse_ok=True`，`stage_timing`／`timing_breakdown` とも non-null を 3 run 全てで確認．
- 想定外の障害・タイムアウト・コード上のエラーは無かった．各 run の所要時間は開始から完了通知まで概ね 3 分強
  （ヘルスチェック含め全体で計 15 分弱）．

---

### 実装 (Iter4)

**担当**: 実装フェーズ subagent（2026-07-19）．計画（本ブロック直下 `### 計画 (Iter4)` §2・§4）に従い，単一レバー
「B0: per-stage compute/recv dt の内訳記録」を最小差分で実装した．`pipeline_inference.py`／`tools/predict.py`／
`tools/common.py` は非改変（既存関数を import して再利用するのみ）．実機クラスタへの接続・deploy／推論実行は行っていない
（コード実装とローカル単体テストのみ）．

**1. 変更ファイル（3 つのみ，計画どおり）**

- **`tools/collect_results.py`**:
  - 正規表現 3 本を新設（`_COMPUTE_DT_RE`／`_RECV_HIDDEN_DT_RE`／`_SENT_TO_NEXT_DT_RE`）．秒→ms 変換定数
    `_SEC_TO_MS = 1000.0` を追加（マジックナンバー回避）．
  - `NodeStageTiming` dataclass（`rank`／`compute_dt_ms_by_step`／`recv_hidden_dt_ms_step0`／
    `sent_to_next_dt_ms_by_step`）と純関数 `parse_node_stage_timing(log_text)` を追加．`_LOG_LINE_RE` で
    `[R{rank} LEVEL] ...` の本文部分を取り出し，マッチしない行はそのまま本文として 3 正規表現に照合する
    （RESULT のような複数行本文が絡まないため `_extract_rank0_messages` の継続行連結ロジックは不要と判断）．
  - `StageTimingSummary` dataclass と集約関数 `aggregate_stage_timing(nodes)` を追加．send は同一 step の
    `sent_to_next_dt − compute_dt`（中間 rank のみ．最終 rank は `sent_to_next` を持たないため自動除外）で近似．
    差分が負になるケース（ログ欠損・step 対応ずれ）は 0 クランプせず，`(rank, step)` を除外して warning を積む
    （黙って歪めない．計画 §2-B のとおり実装）．デコードステップ（`step ≥ _FIRST_DECODE_STEP = 1`）のみを
    中央値集計の対象とし，step0（prefill）は `prefill_recv_ms_by_rank` に分離した．
  - `build_timing_breakdown(step_dt, summary)` を追加．`residual_ms_median = rank0_step_dt_median_ms −
    compute_sum_ms_median − send_sum_ms_median`（いずれかが `None` なら残差も `None`，捏造しない）．
  - `build_record` に `stage_timing`／`timing_breakdown`（いずれも既定 `None`）フィールドを追加．
    `SCHEMA_VERSION` を **1 → 2** に更新．`--stage-timing` 未指定（既定）では両フィールドとも `null` のまま
    JSONL へ出力され，Iteration 1〜3 の v1 レコードと後方互換．
  - `run_and_collect` に `stage_timing: bool = False` 引数を追加．`True` のときのみ
    `collect_worker_stage_timing_logs(config, run_start)`（新設）を呼び，`read_hosts(config.hosts_file)` で
    rank1 以降の worker（`hosts[i]` = rank i，rank0 は `collect_rank0_log` で取得済みのためスキップ）へ
    `ssh_via_master(...)` 経由で `docker logs --since {since} distributed-llm 2>&1` を取得する．
    `concurrent.futures.ThreadPoolExecutor(max_workers=_STAGE_TIMING_MAX_WORKERS=8)` で並列化し，個々の SSH
    失敗は握りつぶさず `parse_warnings` に積んで成功ノードのみで集約を継続する．
  - `main()` に `--stage-timing`（`action="store_true"`，既定 off）を追加し，`run_and_collect` へ伝播した．
  - モジュール冒頭 docstring を `--stage-timing` の説明・使用例を含む形へ更新．

- **`tests/test_collect_results.py`**: TS1〜TS6 を新設（計画の「任意」TS7 は，既存の
  `test_build_record_contains_all_schema_keys_and_is_json_serializable` に `stage_timing`／`timing_breakdown`
  が `None`（後方互換）であることの assert を追加する形で吸収し，別テストとしては独立させなかった）．
  - TS1〜TS3: 3 正規表現の抽出・非衝突（`_COMPUTE_DT_RE` と `_SENT_TO_NEXT_DT_RE` の誤マッチ無し等）を確認．
  - TS4: `[R7 INFO]` 形式の物理ログから `parse_node_stage_timing` が rank・compute/recv/send を ms 単位で正しく構築．
  - TS5: `aggregate_stage_timing` が複数ノードの `NodeStageTiming` から step 別総和を計算し，最終 rank
    （`sent_to_next_dt_ms_by_step={}`）が送信総和に含まれないことを確認．補足テストとして，
    `sent_to_next < compute` の負差分ケースが 0 クランプされず除外・warning 付与されることも確認．
  - TS6: `build_timing_breakdown` が返す `compute_sum_ms_median + send_sum_ms_median + residual_ms_median ==
    rank0_step_dt_median_ms`（丸め許容）の検算が成立することを確認．
  - 新規 `.log` フィクスチャファイルは作成せず，全てインライン文字列で与えた（Iter2 の `.gitignore` `*.log`
    トラップ回避，計画の指示どおり）．

- **`mise.toml`**: `[tasks."predict:demo"]` の `run` を
  `'uv run python tools/collect_results.py --iter Iter1 --prompt "Hello!"'` から
  `'uv run python tools/collect_results.py --iter "${ITER:-Iter1}" --prompt "Hello!"'` へ変更．
  `ITER` 環境変数が無ければ既定 `Iter1`（後方互換・非破壊）．B0 の測定 run では `ITER=Iter4 mise run
  predict:demo` あるいは `collect_results.py --iter Iter4 --stage-timing` を正式手順とする（計画 §2-D）．

**2. 検証結果**

- `uv run python -m py_compile tools/collect_results.py tests/test_collect_results.py`: エラー無し．
- `unset VIRTUAL_ENV && uv run pytest tests/ -v`: **45 passed, 0 failed/error**（既存 38 件＋新規 TS1〜TS6
  相当 6 件＋既存 `test_build_record_...` 1 件への assert 追加＝差分 7 件，合計 45 件．計画の「合計 44 件以上」を
  満たす）．`VIRTUAL_ENV` が別リポジトリ（WAFL-PEFT）の `.venv` を指す環境変数汚染があったため `unset` してから
  実行した（`uv run` 単体では warning が出るのみで実害は無いが，明示のため記録する）．
- `git status --short`: 変更ファイルは `mise.toml`／`tests/test_collect_results.py`／`tools/collect_results.py`
  の 3 つのみ（新規 `.log` 等の混入無し）．`.claude/research/journal.md`／`state.json` の差分は計画フェーズが
  作業前から持ち込んでいた未コミット変更であり，本実装フェーズでは触れていない．

**3. 気づいた点・申し送り**

- `parse_node_stage_timing` は worker ログにブロック開始マーカー（`Rank 0: prompt=`）が無い前提のため，複数 run が
  同一 `--since` 窓に混在すると компute/send が別 run のものと混ざる余地が残る（計画が明記した既知の限界．
  `--iter` 変数化＋単発運用で回避する運用側の前提）．
- `collect_worker_stage_timing_logs`／`run_and_collect` の `--stage-timing` 分岐は SSH を伴うため計画どおり
  単体テスト対象外とした（Iter1〜3 の `run_and_collect` と同じ扱い．手動レビューで `read_hosts` の返す順序
  （`hosts[i]` = rank i）と rank0 スキップのオフセット（`range(1, len(hosts))`）を確認済み）．
- フェーズ4（実機 `--stage-timing` 測定 run）は計画のとおり，着手前に B1 の合意に基づき Slack で人間確認が必要
  （本実装フェーズでは実施していない）．
- **実験を開始してよい状態か**: コード実装・単体テストは完了しフェーズ4 に進める状態にあるが，フェーズ4 の着手
  （51 ノードへの SSH 並列 `docker logs` 取得を伴う実機 run）自体は計画が明記したとおり別途人間確認が必要であり，
  本実装フェーズの完了はその確認を代替しない．

---

### 計画 (Iter4)

**担当**: 計画フェーズ subagent（2026-07-19）．単一レバー「B0: per-stage の compute/recv 時間内訳を results/Iter4.jsonl へ集約し，
ITL≈7s/token の計算 vs 通信のボトルネックを診断する」（本ブロック下 `### 調査 (Iter4)` の推奨第一手，backlog B6）を，
実コード（`pipeline_inference.py` の該当ログ行・`tools/collect_results.py` 全体・`tools/common.py` の SSH/hosts 機構・`mise.toml`）を
Read して実装可能な粒度へ落とし込んだ．**本イテレーションのフェーズ2・3 はコード実装・単体テストのみで，実機クラスタへの
deploy／推論実行は行わない**（フェーズ4は B1 の人間確認後にオーケストレータが着手する）．前担当（rc-planner）がセッション制限で
中断したため引き継ぎ発見を実コードで再検証したうえでゼロから確定した．

#### 0. コードで再検証した前提（計画の土台）

- **per-stage の時間ログは rank0 ではなく中間 rank・最終 rank のログにのみ出る**（引き継ぎ発見①は正しい）．
  - `compute dt`: 最終 rank `pipeline_inference.py:1598`，中間 rank `:1704`．物理行
    `[R{N} INFO] Rank {N}: step {step} compute dt={x:.3f}s hidden_mean=... hidden_std=...`．**全デコードステップで出る**．
  - `recv_hidden dt`: 最終 rank `:1573`，中間 rank `:1677`．物理行 `[R{N} INFO] Rank {N}: recv_hidden dt={x:.3f}s`．
    **`is_first`（step0＝prefill 受信）でのみ出る**（step>0 の else 分岐 `:1574-1586`/`:1678-1692` には無い）．
  - 中間 rank `sent to next dt`: `:1714`．物理行 `[R{N} INFO] Rank {N}: step {step} sent to next dt={x:.3f}s`．**毎ステップ出る**．
    `_t`（`:1694`．irecv 完了後の計算開始）起点で計測されるため **compute+send を含み，recv 待ちは含まない**．
  - rank0（`:1439-1540`）は per-stage の計算/受信時間を持たず，`Rank 0: step N done ... dt=...s`（`:1532`，現行 `step_dt` の源）＝
    そのトークンの 51 段一周の総時間（≒7s）だけを出す．最終 rank の post-compute（final_norm+lm_head+argmax+送信+ACK）は
    個別 INFO 行はあるが単一 dt では計時されない（残差に吸収する）．
- **worker ノードへの到達手段は既存コードで足りる**（引き継ぎ発見②は正しい）．`tools/common.py:424 ssh_via_master(user,
  master_addr, target_host, command)` が local→master→target の ProxyJump．`read_hosts(config.hosts_file)`（`:292`）は IP を
  返し**行順＝rank 番号**（`hosts[i]` が rank i）．各ノードのコンテナ名は `distributed-llm`（`collect_rank0_log:527` が
  `docker logs ... distributed-llm` を使用）．各コンテナは 1 プロセス＝1 rank なので，そのノードの `docker logs` には
  当該 rank の `[R{i} ...]` 行しか出ない．
- **B0 は `pipeline_inference.py` 改変・再デプロイ不要**．上記ログ行は Iter3 デプロイより前から存在し，稼働中イメージに
  既に含まれる．よって B0 は **収集側（ローカル実行の `tools/collect_results.py`）の拡張だけ**で成立し，ホットパス非改変・
  再デプロイ不要・冷開始交絡（再初期化 348s）なし．Iter1〜3 と同じ「収集ツールに閉じた・非侵襲」性質のイテレーションである．
- **`mise.toml:123` の `predict:demo` は `--iter Iter1` 固定**（引き継ぎ発見③は正しい．backlog B6）．B0 の測定 run が
  `results/Iter1.jsonl` に混在しないよう本計画で解消する（下記 §2-D）．

#### 1. 仮説

ITL≈7s/token を，非 rank0 全ノードの既存ログから **段別 compute 時間の総和 Σcompute と，段間 send 時間の総和 Σsend** に
分解して記録すれば，rank0 の `step_dt`（≒7s）に対し **残差 residual = step_dt − Σcompute − Σsend**（recv 待ち＋ACK 往復＋
Gloo/Python オーバーヘッド＋rank0/最終 rank の周辺処理）を算出でき，7s/token が **計算律速か通信律速かを 1 回の測定で確定** できる．
これは調査の一次推定（「ホップ数×1 ホップ固定レイテンシ＋各段逐次 CPU 計算の和が支配的」）を実測で検証し，次イテレーション以降の
レバー選択（通信律速なら B1: WORLD_SIZE 削減，計算律速なら B2: ホットループ診断ログ削減）を根拠づける土台になる．

#### 2. 単一レバー・変更内容

**単一レバー**: 「results に記録する情報を，rank0 単独の集計から **非 rank0 全ノードの per-stage 時間内訳へ拡張する**」の 1 点．
**固定する構成（直近最良＝Iter3 の既定値，掃引しない）**: `WORLD_SIZE=51`，`NUM_MICRO_BATCHES=4`，`STAGGER_INTERVAL=3.0`，
`SEQ_LEN=1`，prompt=`"Hello!"`，稼働中の 51 ノード実機（再デプロイなし）．B1（WORLD_SIZE 絞り込み）以降は本測定で内訳が確定した後の
**次点候補**として温存する（本イテレーションでは振らない）．

変更ファイルは **`tools/collect_results.py`（段別時間の収集・パース・記録を追加）**，**`tests/test_collect_results.py`（テスト追加）**，
**`mise.toml`（`--iter` 変数化）** の 3 つのみ．**`pipeline_inference.py`／`tools/predict.py`／`tools/common.py` は非改変**（既存関数を
import して再利用するのみ）．

**(A) パース純関数の追加（`tools/collect_results.py`．既存 `_PROMPT_TOKENS_EMBED_RE` 群と同じ場所・同型で単体テスト可能に）**

- 正規表現 3 本を新設する（`compute dt`/`sent to next dt` は行末に `hidden_...` が続くため `$` 終端にせず prefix マッチ）:
  ```python
  _COMPUTE_DT_RE      = re.compile(r"^Rank (\d+): step (\d+) compute dt=([\d.]+)s")
  _RECV_HIDDEN_DT_RE  = re.compile(r"^Rank (\d+): recv_hidden dt=([\d.]+)s$")
  _SENT_TO_NEXT_DT_RE = re.compile(r"^Rank (\d+): step (\d+) sent to next dt=([\d.]+)s$")
  ```
- 1 ノード分のログテキストから段別時間を抽出する純関数を新設する（既存 `_extract_rank0_messages` は `R0` 限定で流用できないため，
  ANSI 除去＋`_LOG_LINE_RE` で全 rank の本文を取り出す軽量版を使うか，本文行の `Rank (\d+):` から rank を検出する）:
  ```python
  @dataclass
  class NodeStageTiming:
      rank: int | None                 # ログ本文 "Rank {N}:" から検出（ノード=1 rank）
      compute_dt_ms_by_step: dict[int, float]     # step -> compute dt（ms）
      recv_hidden_dt_ms_step0: float | None       # step0 の prefill 受信時間（ms）．無ければ None
      sent_to_next_dt_ms_by_step: dict[int, float]  # 中間 rank のみ．最終 rank は空

  def parse_node_stage_timing(log_text: str) -> NodeStageTiming: ...
  ```
  秒→ミリ秒は `round(sec * 1000, 3)` で保持（フィールド名も `_ms` 接尾辞で単位を明示）．マジックナンバー 1000 は
  定数 `_SEC_TO_MS = 1000.0` として定義する．
- **`--since {run_start}` 窓で当該 run に限定**するため，worker ログでも `collect_rank0_log` と同じ `--since` を使う（下記 C）．
  worker ログには rank0 の `Rank 0: prompt='...'` 開始マーカーが無く `_split_into_blocks` は使えないが，単発プロンプトの診断 run
  かつ `--since` で測定 run に絞るためブロック分割は不要（複数 run 混在は §2-D の `--iter` 変数化＋運用で回避）．この前提を
  docstring に明記する．

**(B) 集約・導出（純関数．単体テスト可能）**

- 全ノードの `NodeStageTiming` を集約し，**デコードステップ（step≥1）** ごとに横断集計する（step0 は prefill/TTFT で桁が違うため分離）:
  ```python
  @dataclass
  class StageTimingSummary:
      n_ranks_reporting: int              # compute dt を報告できた非 rank0 rank 数
      compute_sum_ms_by_step: dict[int, float]   # Σ_ranks compute（step 別）
      send_sum_ms_by_step: dict[int, float]      # Σ_intermediate (sent_to_next − compute)（step 別）
      # 代表値（デコードステップ中央値）
      compute_sum_ms_median: float | None
      send_sum_ms_median: float | None
      prefill_recv_ms_by_rank: dict[int, float]  # step0 recv_hidden（rank 別．prefill 診断用）
  ```
  send は中間 rank の `sent_to_next_dt − compute_dt`（同 step）で近似する（`_t` 起点の差分＝送信区間）．最終 rank は send を
  持たない（token_id を rank0 へ返すのみ）ため送信総和には含めない．
- `build_record` 側で rank0 の `step_dt`（既存 `derived`/`parsed.step_dt`）と突き合わせ，**残差** を算出する:
  `residual_ms_by_step[s] = step_dt[s]*1000 − compute_sum_ms_by_step[s] − send_sum_ms_by_step[s]`．
  代表値として `timing_breakdown = {compute_sum_ms_median, send_sum_ms_median, residual_ms_median, rank0_step_dt_median_ms,
  n_ranks_reporting}` を記録する（`compute+send+residual ≈ rank0_step_dt` が丸め誤差内で成立することを分析で検算できる）．

**(C) 実機収集の拡張（`run_and_collect`．SSH を伴うため単体テスト対象外，Iter1〜3 と同じ扱い）**

- `--stage-timing`（`action="store_true"`，既定 off）フラグを新設する．**off のとき現行挙動を完全に維持**（通常の `predict:demo`
  相当 run を重くしない）．on のときのみ以下を追加実行する:
  1. `hosts = read_hosts(config.hosts_file)` を取得（`hosts[i]`＝rank i）．rank0（`hosts[0]`＝master 自身）は既存 `collect_rank0_log`
     が取得済みのためスキップし，**rank 1..len(hosts)-1** の worker から `ssh_via_master(config.ssh_user, config.master_addr,
     hosts[i], f"docker logs --since {since} distributed-llm 2>&1", timeout=DOCKER_LOGS_SSH_TIMEOUT_SEC)` でログ取得する．
  2. SSH は `concurrent.futures.ThreadPoolExecutor`（最大同時数は定数 `_STAGE_TIMING_MAX_WORKERS = 8` 程度）で並列化する
     （50 ノード逐次×数秒は遅いため）．**個々のノードの SSH 失敗は握りつぶさず** `parse_warnings` に
     `f"failed to fetch rank {i} docker logs: {stderr}"` を積み，成功ノードのみで集約を続行する（一部欠損を許容）．
  3. 集約結果を `stage_timing`（rank 別の per-step 内訳．JSON 量が過大なら rank 別に compute の中央値＋step0 recv のみへ間引く．
     初版は raw dict を保持し，肥大化が問題なら間引く方針を docstring に記す）と `timing_breakdown`（§2-B の代表値）として record へ追加する．
- **スキーマ変更**: 新規フィールド `stage_timing`／`timing_breakdown` を追加し，`--stage-timing` off の run では両者を `null` とする
  （既存 Iter1〜3 の v1 レコードと後方互換）．`SCHEMA_VERSION` を **2** へ上げ，v2＝段別時間フィールドを含み得ることを示す
  （`build_record` の docstring も更新する）．

**(D) `mise.toml` の `--iter` 変数化（backlog B6 の解消．非破壊）**

- `mise.toml:123` を `--iter Iter1` 固定から env 上書き可能へ変更する（既定は Iter1 のまま＝後方互換）:
  `run = 'uv run python tools/collect_results.py --iter "${ITER:-Iter1}" --prompt "${PROMPT:-Hello!}"'`．
  B0 の測定 run は `ITER=Iter4 mise run predict:demo` あるいは
  `uv run python tools/collect_results.py --iter Iter4 --stage-timing --prompt "Hello!"` を正式手順とする（フェーズ4 の実験計画で採用）．
  これで複数 run が `Iter1.jsonl` に混在する実害（B6）を絶つ．**公開タスクの semantics 変更**にあたるため実装フェーズは既定値維持
  （非破壊）を厳守すること．

#### 3. 成功条件（measurable）

本イテレーションのフェーズ2・3（実装・単体テスト）の完了条件は決定的で，以下を全て満たすこと:

1. **単体テスト（新規，最低 6 件）が green，既存 38 件が回帰なし**（合計 44 件以上 passed，failed/error 0）:
   - TS1: `_COMPUTE_DT_RE` が `Rank 7: step 3 compute dt=0.123s hidden_mean=...` から `(rank=7, step=3, dt=0.123)` を取り出す．
   - TS2: `_RECV_HIDDEN_DT_RE` が `Rank 7: recv_hidden dt=1.234s` を取り出し，step>0 行（recv_hidden 無し）では None．
   - TS3: `_SENT_TO_NEXT_DT_RE` が `Rank 7: step 3 sent to next dt=0.456s` を取り出す（`compute dt` 行と誤マッチしない）．
   - TS4: `parse_node_stage_timing` が `[R7 INFO] ...` を含む 1 ノードログ全体から rank=7・compute_dt_ms_by_step・
     recv_hidden_dt_ms_step0・sent_to_next_dt_ms_by_step を正しく構築し，単位が ms（秒×1000）で入る．
   - TS5: 集約 `StageTimingSummary` が複数ノードから `compute_sum_ms_by_step`／`send_sum_ms_by_step` を step 別に加算し，
     `send = sent_to_next − compute` の差分が負にならない健全ケースで正の値を返す（最終 rank が send 総和に含まれない）．
   - TS6: 残差計算が `residual = rank0_step_dt_ms − compute_sum_ms − send_sum_ms` を返し，
     `compute_sum + send_sum + residual == rank0_step_dt_ms`（丸め許容）を満たす．
   - （任意）TS7: `--stage-timing` off 相当で `stage_timing`／`timing_breakdown` が `null`，かつ既存レコード形と後方互換．
2. `uv run python -m py_compile tools/collect_results.py tests/test_collect_results.py` がエラー無し．
3. コード変更が `tools/collect_results.py`／`tests/test_collect_results.py`／`mise.toml` の **3 ファイルのみ**
   （`pipeline_inference.py`／`tools/predict.py`／`tools/common.py` 非改変，`git status` に新規 `.log` フィクスチャ混入なし）．

フェーズ4（実機測定．B1 の人間確認後）での成功条件（本計画が指定，判定は analyst）:

4. `ITER=Iter4 ... --stage-timing` の測定 run 後，`results/Iter4.jsonl` の当該レコードに `stage_timing`／`timing_breakdown` が
   非 null で入り，**`timing_breakdown.n_ranks_reporting ≥ 45`**（50 worker 中，SSH/欠損の許容幅 5）で
   compute_dt が集まっていること．
5. `timing_breakdown` から **「ITL≈7s/token のうち計算（Σcompute）が X%・送信（Σsend）が Y%・残差（recv 待ち＋ACK＋
   オーバーヘッド）が Z%」を数値で言明でき**，`X+Y+Z=100%`（丸め誤差内，`compute_sum+send_sum+residual ≈ rank0_step_dt` が
   成立）していること．これにより「7s/token は計算律速か通信律速か」が判定可能になる＝B0 の目的達成．
6. 測定は稼働中クラスタに対し `--stage-timing` run を **n≥3 回**（再デプロイなし＝冷開始交絡なし）実施し，代表値は中央値を採る
   （run 間ばらつきの把握）．step0（prefill/TTFT）は `prefill_recv_ms_by_rank` で別枠診断する．

#### 4. 実装フェーズ（rc-implementer）への申し送り

- **対象ファイルと設定キー**:
  - `tools/collect_results.py`: (A) 正規表現 `_COMPUTE_DT_RE`/`_RECV_HIDDEN_DT_RE`/`_SENT_TO_NEXT_DT_RE`・`NodeStageTiming`・
    `parse_node_stage_timing`，(B) `StageTimingSummary`・集約/残差の純関数，(C) `run_and_collect` に `--stage-timing` 分岐と
    ThreadPoolExecutor 並列 SSH（定数 `_STAGE_TIMING_MAX_WORKERS`・`_SEC_TO_MS`），`build_record` へ `stage_timing`/
    `timing_breakdown` フィールド追加，`SCHEMA_VERSION=2`，`main()` に `--stage-timing` 引数追加．
  - `tests/test_collect_results.py`: TS1〜TS6（＋任意 TS7）．物理ログは `[R{N} INFO] ...` プレフィックス付きインライン文字列で与え，
    **新規 `.log` フィクスチャは作らない**（Iter2 の `*.log` gitignore トラップ回避）．
  - `mise.toml`: `[tasks."predict:demo"]` の `run` を `--iter "${ITER:-Iter1}"`（＋任意で `--prompt "${PROMPT:-Hello!}"`）へ変数化
    （既定値維持＝非破壊）．
- **注意点**:
  - `pipeline_inference.py` は**触らない**（ログは既存・稼働中イメージに含まれる＝再デプロイ不要）．これにより B0 はホットパス
    非改変で，フェーズ4 は再デプロイなしの `--stage-timing` run のみで足りる（冷開始 348s を回避）．
  - `send = sent_to_next_dt − compute_dt` の差分が負になる（ログ欠損・step 対応ずれ）ケースは 0 クランプせず warning を積み，
    当該 step を集約から除外する（黙って歪めない）．
  - worker ログにブロック開始マーカーが無いため `--since {run_start}` で run を限定する前提を守る（複数 run 混在は `--iter`
    変数化＋単発運用で回避）．
  - **フェーズ4（実機 `--stage-timing` 測定 run）は B1 の合意通り着手前に Slack で人間確認が必須**（再デプロイは不要だが 51 ノードへ
    SSH で `docker logs` を並列取得するため，B1 のスコープに含める）．フェーズ2・3（実装・単体テスト）はローカルのみで進行可能．

---

### 調査 (Iter4)

**担当**: 調査フェーズ subagent（2026-07-18）．ユーザー指示⑤（先行研究調査に基づく推論パイプライン高速化）に向け，
(A) `pipeline_inference.py` を実際に読んで現行の通信方式・バッチング方式・レイヤー分割方式を確認し，
(B) tavily で分散パイプライン並列推論の高速化手法を文献調査した．実機クラスタへの接続・deploy/推論実行は一切していない
（コード読み取りと Web 調査のみ）．次フェーズ（計画）が単一レバーとして選べる改善候補を末尾に整理した．

**問い**
1. 「ITL≈7s/token」の主要因は何か（通信オーバーヘッド／CPU 計算／同期待機／アイドル）．コードから一次推定する．
2. 分散パイプライン並列推論の高速化手法（バブル削減・通信オーバーラップ・KV 最適化・量子化・continuous batching・
   speculative decoding 等）の候補を洗い出し，本リポジトリ構成への適用難易度を評価する．
3. 次フェーズ（計画）が単一レバーとして選べる具体候補（概要・期待効果・実装規模・リスク）を用意する．

**A. 現行アーキテクチャの実測確認（コード出典＝リポジトリ内 ファイル:行）**

- **これは GPU クラスタではなく CPU クラスタである（分析の前提を規定する最重要点）**．`COMPUTE_DTYPE = torch.float32`
  （`pipeline_inference.py:38`）で，コメント（`:36-37`）に「Intel i5-8350U は AVX-512 BF16 非対応のため bfloat16 は
  内部で float32 変換されオーバーヘッド．float32 直用で BLAS（OpenBLAS/MKL）を活かす」と明記．重みロードも `map_location="cpu"`
  （`:770`），スレッドは `torch.set_num_threads(os.cpu_count())`（`:404`．i5-8350U は 4 コア／cpuset 0-3，`:401-402`）．
  → **各ノードは 4 コアの弱い CPU**．GPU 前提の高速化手法（NCCL・TensorRT-LLM・PagedAttention の GPU 実装等）は
  そのままは効かない．
- **モデルは Gemma-4-31B-it（`config.json`）: `num_hidden_layers=60`，`hidden_size=5376`，heads=32/kv=16**．これを
  **WORLD_SIZE=51 ノード**に分割する（`get_assigned_layers`，`:345-350`．60 層 ÷ 51 ノード ≒ 大半のノードが 1 層，
  9 ノードが 2 層）．
- **通信バックエンドは Gloo（TCP，物理 NIC 固定）**．`dist.init_process_group(backend="gloo", ...)`（`:547-548`），
  物理 NIC 固定は `:408,424-434`．GPU/NCCL は不使用．
- **デコードは「1 トークンずつ・単一マイクロバッチ・51 段を厳密逐次通過」**．生成本体 `_relay_request`（`:1275-1740`）は，
  rank0 が 1 トークン分の embed（step0 は prompt 全体，`batch_size=1`／`seq_len=1`）を rank1 に `dist.send`（`:1466-1468`），
  中間 rank が `recv → 自分の層を計算 → 次 rank へ send`（`:1655-1714`），最終 rank が `final_norm+lm_head → argmax →
  token_id を rank0 へ send`（`:1600-1621`）．**`NUM_MICRO_BATCHES` を使うマイクロバッチ機構（`process_microbatch`
  `:964-1000`／`_pipeline_loop` `:1109-1147`）はこの自己回帰デコード経路では使われていない**（別経路・実質ウォームアップ相当）．
  つまり `NUM_MICRO_BATCHES` レバーは現状の 1 リクエスト生成レイテンシには効かない可能性が高い．
- **通信は同期ブロッキング（`dist.send`/`dist.recv`）**．step>0 のみ seq_len スカラと hidden を `irecv` 2 本で受ける（`:1577-1580`,
  `:1681-1684`）が，これは 2 値の受信並列化にとどまり，段間（stage i と i+1）の計算オーバーラップではない．
- **段間同期は send/recv に加えて TCP の ACK チェーン**（`_RELAY_ACK_PORT`，永続接続 `:1344-1425`）と，リクエスト毎の
  `dist.barrier()`（`:1298-1307`）がある．barrier はリクエスト毎 1 回で許容範囲だが，**ACK はステップ毎に段間で往復**する
  （`:1478-1486`, `:1628-1642`, `:1716-1728`）．
- **ホットループ内で毎ステップ・毎 rank に診断ログが多数**（テンソル全体の `.mean()/.std()/.min()/.max()` や `torch.topk` を
  毎回計算）．最終 rank は `:1598,1605,1609,1616-1617,1619`，中間 rank は `:1704`．これらは `.item()`／全要素リダクションを
  ホットパスで強制発火させる．
- **通信ペイロードは小さい**: hidden は (1,1,5376) float32 = 5376×4 ≒ **21 KB/ホップ**（step0 の prefill のみ seq_len≒prompt 長で
  数百 KB）．50 ホップでも総転送量は小さく，**生帯域は律速ではない**．効くのは「50 回の逐次ホップ × 1 ホップあたりの固定
  レイテンシ（send/recv + ACK 往復 + Gloo/Python オーバーヘッド）＋各段の CPU 計算」の累積である．

**A の一次推定（ITL≈7s/token の主要因）**

- **根本原因は「単一リクエストのパイプライン並列は本質的にレイテンシを下げない」構造**にある．seq_len=1・単一マイクロバッチの
  自己回帰デコードでは，段 i+1 は段 i の出力を待つ厳密依存のため，**任意時刻に 51 段のうち 1 段しか稼働しない**（利用率
  ≒1/51≒2%）．7s/token を 51 段で割ると **1 段あたり≒140ms**．内訳は「1〜2 層の CPU 計算（float32・4 コア）＋ 21KB の
  send/recv ＋ ACK 往復 ＋ 毎ステップ診断ログのリダクション」．**通信の生帯域ではなく，ホップ数（=段数）× 1 ホップ固定
  レイテンシと，各段の逐次 CPU 計算の和**が支配的と一次推定する．
- **確度の注意**: コード読みだけの推定である．コードは各 rank で `compute dt`（`:1598,1704`）と `recv_hidden dt`（`:1573,1677`）を
  既にログ出力しているが，**現状 `results/Iter{n}.jsonl` は step_dt 集計しか保存しておらず，計算 vs 通信の内訳は未記録**．
  内訳の確定にはこの per-stage ログを 1 度パースする（計画候補 B0 参照）．

**B. 文献調査（Web 出典付き）**

- **この構成特有の問題は文献で定式化済み**．FlowSpec（Sang et al., arXiv:2507.02620, 2025,
  https://arxiv.org/html/2507.02620v1 ）は「エッジの分散パイプライン推論は**リクエストが疎（sparse）だとパイプライン利用率が
  低くレイテンシ低減の恩恵が消える**」と本リポジトリと同じ根本問題を指摘し，pipeline-parallel な**木構造 speculative decoding**で
  対処．実機で **1.36×–1.77× の速度向上**を報告（コード公開 https://github.com/Leosang-lx/FlowSpec ）．
- **PipeDec / SpecPipe**（Chen et al., arXiv:2504.04104, 2025, https://arxiv.org/html/2504.04104v2 ）は「単一タスクのパイプライン
  推論レイテンシを下げるため**パイプライン全体を使って後続の複数トークンをデコード**する」= 51 段を 1 往復するたびに複数
  トークンを検証する方向で，本構成の token-by-token レイテンシに直接効く系譜．
- **Prima.cpp**（Li et al., arXiv:2504.08791, 2025, https://arxiv.org/html/2504.08791v2 ）は**低リソース・ヘテロなホームクラスタ**
  （まさに CPU 主体）で 30–70B を動かす研究で，「Wi-Fi 等の**高レイテンシ網では P2P 通信が少ない PP がむしろ適する**」とし，
  層割当・メモリ配置の最適化を扱う．本リポジトリと最も環境が近い．
- **Zero Bubble Pipeline Parallelism**（Qi et al., ICLR 2024, arXiv:2401.10241, https://github.com/sail-sg/zero-bubble-pipeline-parallelism ）
  はバブル削減の代表だが**学習（forward/backward スケジューリング）主眼**で，単一リクエスト推論デコードには直接は効かない
  （バブル削減はマイクロバッチ／複数リクエストが同時に流れて初めて効く）．
- **OSS の同種プロジェクト**: llama.cpp RPC モード・exo・Petals・distributed-llama（https://github.com/b4rtaz/distributed-llama ）が
  ホームクラスタ分散推論の実装例．コミュニティ知見（https://localaimaster.com/blog/distributed-inference-local-ai ,
  r/LocalLLaMA）は「ボトルネックは多くの場合ネットワーク」「CPU-only は動くが遅い」とし，PP は低帯域向き，TP は高帯域向きと整理．
- **CPU 量子化の効き方（本ハードで重要）**: INT8 の x86 高速化は主に **VNNI / DL Boost（第 2 世代 Xeon Scalable 以降）**に依存
  （Intel, https://community.intel.com/t5/Blogs/... ; PyTorch x86 INT8, https://pytorch.org/blog/int8-quantization ）．**i5-8350U
  (Kaby Lake R, 2017) は VNNI も AVX-512 も非搭載**のため，INT8 演算そのものの高速化は期待薄．一方 llama.cpp/llamafile の
  手書き量子化カーネル（justine.lol/matmul, https://justine.lol/matmul ）は CPU で q8_0/q4 に対し実速度向上を出しており，
  **CPU での量子化の主効果は「演算の INT8 化」より「重みのメモリ帯域削減」**である点に注意（ただし本リポは PyTorch float32
  BLAS 経路で llama.cpp カーネルは未使用）．
- **continuous batching / PagedAttention（vLLM, Sarathi-Serve 等）**: スループット・尾レイテンシ改善が主目的で，**単一リクエストの
  ITL は下げない**（USENIX OSDI'24 Sarathi-Serve, https://www.usenix.org/system/files/osdi24-agrawal.pdf ）．現行ベンチが
  1 プロンプト単発である限り本命ではない（目的がスループットに移れば有効）．

**次フェーズ（計画）への示唆＝単一レバー候補（概要／期待効果／実装規模／リスク）**

- **B0（計測・最小・最推奨の第一手）: per-stage の `compute dt` と `recv_hidden dt` を results に集約し，7s の計算 vs 通信内訳を確定**．
  概要: rank ログに既に出ている段別時間（`:1573,1598,1677,1704`）を `collect_results.py` でパースし JSONL に追記（②の感度分析の
  土台にもなる）．期待効果: レイテンシは下げないが「どのレバーを振るべきか」を確定させる．実装規模: 小（収集ツールに閉じ・実機
  非接触寄り，Iter1〜3 と同性質）．リスク: 低．**根本原因が通信律速か計算律速か未確定な現状，最初にこれを潰すのが単一レバー
  原則にも合致**．
- **B1（既存レバー・診断価値大）: WORLD_SIZE を絞る（51→例 21/11）**．概要: 60 層を少数ノードに厚く割当（11 ノードなら≒5.5 層/
  ノード）．期待効果: **逐次ホップ数が 50→10 に激減**し，1 ホップ固定レイテンシ×ホップ数の累積が減る（通信/ホップ律速なら大，
  計算律速なら小＝B0 と合わせて根本原因を弁別できる）．実装規模: 小（config `levers` に既存，コード変更なし）．リスク: 各ノードの
  層数増→メモリ/計算増（60 層が収まる下限は `:316-323` で制約），再デプロイ要・実機 run 要（B1/フェーズ4 で人間確認）．
- **B2（安価なコード改善）: デコードホットループの毎ステップ診断ログ（全要素リダクション・topk）を削減／デバッグフラグ化**．
  概要: `:1598,1605,1609,1616-1617,1704` 等の毎ステップ統計計算を抑制．期待効果: 各段の CPU オーバーヘッド×50 段×ステップを削減
  （小〜中，計算律速なら効く）．実装規模: 小（`pipeline_inference.py` 内）．リスク: ホットパス改変＝再デプロイ・人間確認要（B1）．
  ログを消すと分析材料が減るためフラグ化が無難．
- **B3（本命＝文献の主流だが大規模）: speculative decoding（rank0 に小さな draft モデル，51 段 1 往復で K トークン検証）**．
  概要: FlowSpec/PipeDec の方式．token-by-token の 51 段逐次通過を「K トークンまとめて検証」に置換しレイテンシを分割償却．
  期待効果: 文献ベースで 1.36–1.77×（FlowSpec）〜それ以上．実装規模: **大**（draft モデル追加・relay プロトコルの大改修・検証木・
  再デプロイ）．リスク: 高（単一レバーとしては過大．north star として位置づけ，まず B0/B1 で根本原因を確定してから段階的に）．
- **不採用寄り**: continuous batching / PagedAttention（単発 ITL には効かない・スループット向き），純粋な INT8 量子化（本 CPU は
  VNNI 非搭載で演算高速化は期待薄），学習向けの 1F1B/Zero-Bubble（単一リクエスト推論には非該当），段間計算オーバーラップ
  （単一トークン・単一マイクロバッチでは重ねる相手がなく非該当）．

**②（マイクロバッチ感度分析）との統合メモ**: 現行コードでは `NUM_MICRO_BATCHES` が自己回帰デコード経路で未使用のため，②を
そのまま回しても ITL は動かない公算が大きい（上記 A）．したがって計画は，②を「B0 で内訳を確定 → B1（WORLD_SIZE）で
ホップ数律速か計算律速かを弁別」という形に吸収するのが合理的．人間判断が要る論点（実機 deploy を伴う B1/B2 の掃引着手）は
既に backlog B1/B6 で「フェーズ4 直前に Slack 確認必須」と登録済みで，本調査で新たな人間判断事項は増えていない．

---

## Iteration 3

### 考察・次計画 (Iter3)

**担当**: 考察・次計画 subagent（2026-07-18）．分析(解釈) の結論（本ブロック `### 分析(解釈) (Iter3)`）を受け，
本イテレーションの単一レバー「P1: levers 記録の堅牢化」の採否を確定し，次イテレーションの方向を決めた．
実機への新規接続・実行はしていない（記録の読み取りとコミット操作のみ）．

**1. 採否判定: 採用（adopt）**

- **判定根拠**: 計画 §4 のコードレベル成功条件 6 件をすべて機械的に充足（`pytest` 38 passed＝既存 30＋新規
  TL1〜TL8，failed/error 0；TL6 が env≠log で log 採用を assert；TL7 が env フォールバック維持；TL4/TL8 が
  選択済みブロック紐付け；`py_compile` エラー無し；変更 3 ファイル厳守）．加えて，実機 51 ノードで仕組みの発火を
  確認した．rank0 ログに `Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=3.0 SEQ_LEN=1 WORLD_SIZE=51` が
  実出力され，`results/Iter3.jsonl` の `levers.SEQ_LEN=1`（Iter1 は同フィールド `null`）で確定した．`build_levers` は
  ログ由来があれば dict を丸ごと返す all-or-nothing 構造のため，`SEQ_LEN` 非 null がログ由来経路の作動を一意に示す．
- **追加反復の要否**: 不要．levers 抽出は決定的（純関数・正規表現一致）で測定ノイズを持たず，判定が一意に定まる．
  「env≠実レバーの食い違いそのものの実機再現」だけが残るが，これは②の初回掃引 run（NMB/STAGGER を実際に振る run）で
  自然に exercised されるため，P1 単独での追加 run は不要（分析(解釈) §2 の申し送りに従う）．
- **副次的成果**: 複数行 RESULT（`Hello! ...\nthought`）の実 run で `parse_ok=true`・`parse_warnings=[]` を確認でき，
  Iter2（RESULT 複数行照合の頑健化）の最大残存点だった「実機 end-to-end 未検証」を追認して解消した．

**2. このレバーの収束状況**

- 「基盤の信頼性」系レバー（①永続化基盤 → Iter2 (a)RESULT 複数行照合 → Iter3 (b)levers 記録堅牢化）は，②着手前に
  事前イテレーションで潰すべき (a)(b) を両方とも解消し，**やり切った（収束）**．残る②着手条件 (c) n≥3 反復・
  (d) 主指標 ITL/TTFT は事前イテレーションではなく②の実験設計に属する（分析(解釈) §4）．したがって次は基盤頑健化を
  離れ，config `levers`／`research_frontier` の次候補へレバーを移す．

**3. 次に振るレバーの決定（Iteration 4）: ユーザー指示⑤「先行研究調査に基づく推論パイプライン高速化」**

- **決定**: Iteration 4 の単一レバーは，ユーザーが会話内で 2 回明示的に指示した「ログ収集だけでなく，先行研究・
  関連研究を調査した上で推論パイプラインのパフォーマンス改善を行え」に基づき，config `research_frontier⑤`
  （2026-07-18 追加）を対象とする．**フェーズ1（調査）で tavily 等により分散パイプライン並列推論の高速化手法を
  文献調査**し（通信オーバーラップ・KV キャッシュ最適化・量子化・バッチング戦略・continuous batching・
  speculative decoding 等），**フェーズ2（計画）で単一レバー原則に従い効果の高い 1 つの改善案へ絞り込む**という
  2 段設計とする．
- **②との統合**: 分析(解釈) は（ユーザー指示を認識する前に）②（マイクロバッチ数・stagger interval 感度分析）へ
  進む方向を推奨していた．②は⑤の調査対象（バッチング戦略・チューニング軸）の一部であり，⑤に一本化できる．
  よって Iteration 4 は「②の感度分析を含む，より広い高速化手法の中から調査で 1 つ選ぶ」形で②を吸収する．
- **②/⑤着手の前提条件（フェーズ2＝計画が実験設計に必ず織り込む申し送り）**: (i) `mise.toml` の
  `[tasks."predict:demo"]` が `--iter Iter1` 固定のため，複数 run が `results/Iter1.jsonl` に混在する実害を解消する
  （`--iter Iter{n}` 変数化，または `collect_results.py --iter Iter{n}` 直接呼び出しを正式手順に固定）．
  (ii) 冷開始交絡（再デプロイ後のプロセスグループ再初期化 348s，本 Iter3 の `ttft_s=81.637s` 突出がその実例）の
  除去（最初の 1 run を捨てるか warm-up 後に計測）．(iii) 各レバー水準 n≥3〜5 反復．(iv) 主指標 ITL/TTFT の指定．
  これらは backlog B6 に auto-decided として記録した．
- **可逆性**: 次に振るレバーの選定であり可逆．ユーザーの直接指示に基づく自動判断とした（破壊的操作を含まない）．
  実機 deploy/推論を伴う②/⑤の掃引 run 着手は，B1 の合意通りフェーズ4 直前に別途 Slack 確認を要する（不可逆側は
  そこで人間判断を仰ぐ）．

**次イテレーションへの結論**: Iteration 3（P1 levers 記録堅牢化）を採用で確定・収束．Iteration 4 は
research_frontier⑤（ユーザー指示：先行研究調査に基づく推論パイプライン高速化）を，調査→計画で単一レバーへ
絞り込む形で開始する（②を吸収）．

---

### 分析(解釈) (Iter3)

**担当**: 分析(解釈) subagent（2026-07-18）．`## Iteration 3` の全ブロック（調査・計画・実装・実験）と
`results/Iter3.jsonl`（journal 転記，1 レコード）を読み，単一レバー「P1: levers 記録の堅牢化」の成否判定・
目的達成度・②着手条件・`mise.toml` 副次問題を解釈した．実機への新規接続・実行はしていない（記録の読み取りのみ）．

**前提（判定の枠組み）**: 本イテレーションの判定対象は「振ったレバー値が正しく記録される仕組み」の成否であり，
levers 抽出は決定的（純関数・正規表現一致）で**測定ノイズを持たない**．したがって Iter1 で問題になった「n が
小さくノイズ幅未知」という論点は levers 記録の合否には該当しない．一方で実験値（`ttft_s` 等）の絶対水準・
Iter1 比較は，レバーが全て既定値のままである本 run では**レバー効果ではない**ため判定材料にしない（下記 1 の注記）．

**1. 成否判定: 採用相当（コードレベル成功条件 6 件を充足＋実機で仕組みが発火したことを機械的に確認）**

- **コードレベル（計画 §4 の 1〜6，決定的）**: 実装フェーズ記録で全て充足．(1) `pytest` 38 passed（既存 30＋
  新規 TL1〜TL8＝8 件，failed/error 0，「36 件以上」の下限超過），(2) TL6 が env と食い違う `levers_from_log` を
  与えてもログ側が採用されること（P1 の核心）を assert，(3) TL7 が `levers_from_log is None` で env フォールバック
  維持，(4) TL4/TL8 が選択済みブロック紐付け，(5) `py_compile` エラー無し，(6) コード変更 3 ファイル厳守
  （`mise.toml`／JSONL スキーマ非改変）．
- **実機での仕組み発火の機械的確認（今回の主眼）**: rank0 ログに新規 levers 行
  `Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=3.0 SEQ_LEN=1 WORLD_SIZE=51` が実出力され，
  `results/Iter3.jsonl` の `levers.SEQ_LEN=1`（Iter1 は同フィールド `null`）で確定した．`build_levers` は
  `levers_from_log is not None` のとき `dict(levers_from_log)` を**丸ごと**返す all-or-nothing 構造（計画 §2 B-4）
  であるため，**SEQ_LEN が非 null であること自体が「ログ由来経路が発火し，4 フィールド全部がログ由来で埋まった」
  ことを一意に示す**（env フォールバックだったなら Iter1 と同じく `SEQ_LEN=null` になる）．TL6 の env≠log 挙動が
  実機の実 run 上でも齟齬なく作動したことの，フィールドレベルの唯一かつ決定的な証拠がこの `SEQ_LEN=1` である．
- **副次的な end-to-end 確認（Iter2 の実機未検証点の解消）**: 応答本文が複数行（`Hello! ...\nthought`）である
  実 run で `parse_ok=true`・`parse_warnings=[]` を確認できた．Iter2（RESULT 複数行照合の頑健化）は単体テスト
  止まりで実機 end-to-end 未検証だったが，本 run が「複数行 RESULT でフォールバック警告が実際に消える」ことを
  実機で追認した（Iter2 分析(解釈) §3・考察 §2 の最大残存点が今回副次的に解消）．
- **注記（判定対象外）**: `ttft_s=81.637s`（Iter1 の 26.0s より大）はレバー効果ではなく，実験 3 節記載の
  再デプロイ後プロセスグループ再初期化（348s）に伴う冷開始の交絡である．本 run は 4 レバーが全て既定値
  （NMB=4／STAGGER=3.0／SEQ_LEN=1／WORLD_SIZE=51）で，レバー掃引ではないため絶対水準の比較評価は行わない．

**2. 目的達成度: 「env 由来の暗黙仮定で記録レバーが実レバーと食い違うリスク」を実機発火まで確認して解消**

- 目的（②着手前の解消対象）は「収集ツール実行時 env とコンテナ起動時 env の一致という暗黙仮定
  （`collect_results.py:405-406`）により，②で `NUM_MICRO_BATCHES`／`STAGGER_INTERVAL` を振ると記録レバーが実
  レバーと食い違うリスク」の除去．今回の実機確認は，(i) rank0 が per-request で実効設定を 1 行出す，(ii) 収集側が
  `--since run_start` 窓内・選択済みブロックからそれを抽出する，(iii) `build_levers` がログ由来を採用する，の 3 段が
  実 run 上で連結して働くことを `SEQ_LEN=1`（非 null）で確定した．all-or-nothing 構造ゆえ，この 1 事例で「ログ由来
  経路の実機発火」が担保され，env 依存はこの run について完全にバイパスされた．
- **残存（限定的）**: 実機では env≠実レバーの**食い違いそのもの**はまだ再現していない（本 run は全レバー既定値で
  env と一致しても不一致でも同値になる 3 フィールドと，唯一弁別できる SEQ_LEN のみで確認）．ただし食い違いケースは
  TL6 が単体で押さえており，かつ build_levers の all-or-nothing 構造上「ログ経路が発火すれば env は一切参照されない」
  ため，仕組みとしては一般化できる．食い違い自体の実機作動は②で NMB／STAGGER を実際に振る初回 run で自然に
  exercised される（②の levers 列がログ由来の実値になることを確認すれば足り，P1 のためだけの追加単独 run は不要）．
- **判定**: 目的（リスク解消）は，コードレベル（TL6）＋実機での仕組み発火（SEQ_LEN=1）まで到達しており達成．
  「食い違いの実機再現」は②に畳み込む残タスクとして申し送る（P1 単独での追加反復は不要）．

**3. `mise.toml` の `--iter Iter1` 固定問題の評価（P1 範囲外だが②の前提を壊す実害あり）**

- 実験フェーズが `mise.toml` の `[tasks."predict:demo"]` に `--iter Iter1` が固定引数として入っており，
  `mise run predict:demo` を素朴に実行すると常に `results/Iter1.jsonl` へ追記されると指摘（今回は
  `collect_results.py --iter Iter3` を直接実行して回避）．これは今回のレバー（levers 記録堅牢化）の範囲外だが，
  **②の前提を壊す実害**を持つ: ②はマイクロバッチ数・stagger interval を振って**複数イテレーション**を回すため，
  `mise run predict:demo` を使う限り全 run が `results/Iter1.jsonl` に混在し，「どの run がどのイテレーション（＝
  どのレバー水準の束）だったか」がファイルレベルで汚染される．皮肉にも，これは P1／Iter2 が levers 列・ブロック
  照合レベルで潰した「run とレバーの取り違え」と**同種の誤紐付けが，より粗いファイル／イテレーション粒度で残る**
  という関係にある．P1 で levers 列を堅牢化しても，出力先ファイルが固定では②の比較土台が別経路で崩れる．
- **扱いの示唆**: これは推論スループット／品質に効く**研究レバーではなく実験ハーネス（tooling）の欠陥**である．
  仮説・成功条件をスループット有意差で立てる「単一レバーの独立研究イテレーション」として扱うのは枠組みの
  カテゴリ不一致であり，過剰である．**②のフェーズ2（実験設計）で，ハーネス前提条件として `--iter` を `Iter{n}`
  へ変数化（あるいは②の run 手順として `collect_results.py --iter Iter{n}` 直接呼び出しを正式手順に固定）して
  対処すること**を推奨する．②は複数イテレーション・複数ファイルが初めて意味を持つフェーズであり，この修正は②の
  マルチ run 設計そのものによって検証される（別イテレーションを新設するより，②の setup に自然に載る）．厳格な
  スコープ分離を優先する場合は，②着手前の小さな独立 tooling 修正コミット（研究レバーとは別枠）としてもよいが，
  いずれにせよ**②の最初の掃引 run を回す前に**解消しないと結果が意図せず同一ファイルへ混在する．

**4. ②（research_frontier②）着手条件の充足状況と進行可否**

Iter1 分析(解釈) §3 が②着手前条件として挙げた 4 点（(a) RESULT 複数行対応・(b) levers 堅牢化・(c) n≥3 反復・
(d) truncation に強い主指標 ITL/TTFT）の Iteration 1〜3 での解消状況を整理する．

- **(a) RESULT 複数行照合の頑健化** → Iter2 で採用（単体テスト），本 Iter3 の実 run で `parse_ok=true`・
  `parse_warnings=[]` を実機追認（上記 1）．**単体＋実機 end-to-end で解消**．
- **(b) levers 記録の堅牢化** → 本 Iter3 で解消（コードレベル TL6 ＋実機で `SEQ_LEN=1` 非 null により仕組み発火を
  確認）．**残存は「env≠実レバーの食い違いそのものの実機再現」のみで，②の初回掃引 run に畳み込み可**（上記 2）．
- **(c) レバー値あたり n≥3〜5 反復でノイズ幅確立** → **未解消（②内で担保すべき実験設計条件）**．Iter1〜3 は
  いずれも n=1 で run 間ばらつきは未知のまま．これは事前イテレーションで潰す種類の欠陥ではなく，②の実験計画に
  組み込む前提であり，②のフェーズ2 で各レバー水準 n≥3〜5 を設計する．
- **(d) truncation に強い主指標 ITL/TTFT の採用** → **仕組みは整備済み・決定は②で行う**．`Iter3.jsonl` は
  `itl_p50_s`／`itl_p95_s`／`ttft_s` を既に算出しており（コード基盤は Iter1 で確立），②の success_criteria で
  これらを主指標，`tokens_per_sec` を補助と明記すればよい（Iter1 で指摘された loop-detection truncation の
  スループット交絡を回避）．コードギャップではなく②設計での指標指定事項．

- **進行可否の判断**: ②着手前に「事前イテレーションで潰すべき基盤頑健化」だった (a)(b) は**両方とも解消した**
  （B3→B4 の経緯＝①基盤→②直行せず (a) 頑健化→②直行せず (b) levers 堅牢化，が完了）．残る (c)(d) は②の実験
  **設計条件**であり，独立の先行イテレーションを要しない．したがって**②（マイクロバッチ数・stagger interval の
  感度分析）へ進む条件は，実験ハーネス側の 1 点を満たせば整う**．すなわち②のフェーズ2 で，(i) `mise.toml` の
  `--iter Iter1` 固定を解消（上記 3），(ii) 各レバー水準 n≥3〜5 反復，(iii) 主指標 ITL/TTFT の指定，(iv) 冷開始
  交絡の除去（再デプロイ直後のプロセスグループ再初期化 348s を避けるため，最初の 1 run は捨てるかクラスタ warm-up
  後に計測する．本 Iter3 の `ttft_s=81.637s` 突出はこの交絡の実例）を設計に織り込むこと．これらは事前イテレーション
  ではなく②の計画そのものに属する．

**次フェーズ（考察・次計画 reflector）への結論（採用/棄却の材料）**

- **P1（levers 記録の堅牢化）は採用が妥当**．コードレベル成功条件 6 件を充足（38 passed 他），実機で仕組みの
  発火（`levers.SEQ_LEN=1` 非 null，all-or-nothing 構造によりログ由来経路の作動を一意に確定）を確認した．
  判定は決定的で**追加反復は不要**（食い違い自体の実機再現は②の初回掃引 run に畳み込む）．
- **次イテレーションの示唆**: ②着手前に先行イテレーションで潰すべき基盤頑健化 (a)(b) は Iteration 2・3 で完了した．
  残る (c) n≥3 反復・(d) 主指標 ITL/TTFT は②の実験設計条件で②内で担保する．よって**② へ進む方向を推奨**するが，
  その直前に (i) `mise.toml` の `--iter Iter1` 固定を②のハーネス設計で解消（②の複数ファイル混在という実害を防ぐ），
  (ii) 冷開始交絡（再デプロイ後 348s 再初期化）の除去，を②のフェーズ2 前提として明記すること．P1 の実機 deploy/
  推論は B1/B5 で承認済み（既に②の最初の承認済み run へ畳み込める状態）．

---

### 実験 (Iter3)

**担当**: 実験フェーズ subagent（2026-07-18）．B5 でユーザーが承認した範囲（`mise run deploy` による
再デプロイ＋ `mise run predict:demo`（収集ツール経由）1 回の実行）で，実装フェーズが確定した levers 記録の
堅牢化（`pipeline_inference.py` の 1 行 INFO ログ追加）を実機 51 ノードで動作確認した．数値の良否判定は行わない．

**1. 事前ヘルスチェック**

- `uv run python tools/healthcheck.py`（デプロイ前）: **51/51 healthy**（SSH／Docker daemon／
  distributed-llm container running／モデル重み／MTU 1500，全項目 OK）．

**2. デプロイ（`mise run deploy`）**

- 実行コマンド: `mise run deploy`（バックグラウンド実行＋ポーリング，`poll_interval_sec=60` 間隔で
  `state.json.updated_at` を更新しつつ待機）．
- フェーズ内訳（ログより）: Phase1〜3（ローカル build → registry push → モデル配布，モデル重みは
  「all files already present, skipping」で既存重みを再利用）が約 2 分強，Phase4（51 ノードへの
  イメージ pull・コンテナ再起動）が **06:34.25**．
- 結果: `[RESULT] Deploy results: success=51, failed=0, total=51`．`Phase 4 completed in 06:34.25`，
  `Deploy script complete. Total time: 08:49.54`．異常・失敗ノード無し．

**3. デプロイ直後の一時的接続エラー（実害なし・原因特定済み）**

- デプロイ完了直後に `uv run python tools/collect_results.py --iter Iter3 --prompt "Hello!"` を実行したところ，
  1 回目は `ConnectionRefusedError: [Errno 111] Connection refused`（HTTP 8082 未リッスン）で失敗した．
  `RANK=0 uv run python tools/show_logs.py` で rank0 ログを確認したところ，コンテナ再起動後
  `Initializing process group ... Waiting for 51 nodes to join... Process group initialized on attempt 1 (347.9s)`
  とあり，51 ノードの再接続（プロセスグループ初期化）に約 348 秒かかっており，その完了・HTTP サーバ起動
  （`HTTP server listening on 0.0.0.0:8082`）前にリクエストを送ってしまったことが原因と特定できた．
  数十秒待って再実行したところ正常に応答した（下記4）．51 ノード再接続に伴う既知の起動レイテンシであり，
  ノード障害・デプロイ失敗ではない．

**4. 推論実行（`mise run predict:demo` 相当，収集ツール経由）**

- 実行コマンド: `uv run python tools/collect_results.py --iter Iter3 --prompt "Hello!"`
  （`mise.toml` の `[tasks."predict:demo"]` は `--iter Iter1` を固定引数として持つため，`results/Iter3.jsonl` へ
  出力させるために `--iter Iter3` を明示指定．`mise run predict:demo` 単体ではファイル名が `Iter1.jsonl` に
  固定される点は運用上の既知事項として次フェーズへ申し送る）．
- 標準出力: `[INFO] appended 1 record to results/Iter3.jsonl (parse_ok=True, tokens_per_sec=0.07037958053769999)`，
  応答本文 `Hello! How can I help you today?\nthought`．

**5. rank0 ログでの levers 行の実測確認（今回の主目的）**

`RANK=0 uv run python tools/show_logs.py` で rank0 ログを取得し，`Rank 0: prompt=` 行の直後に新しい levers 行が
実際に出力されていることを確認した．

```
[R0 INFO] Rank 0: prompt='Hello!'
[R0 INFO] Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=3.0 SEQ_LEN=1 WORLD_SIZE=51
```

**6. `results/Iter3.jsonl` の内容**（1 レコードのみ．ファイル行数 `wc -l` = 1）

```json
{
  "schema_version": 1,
  "iter": "Iter3",
  "run_id": "Iter3-20260718T130743Z-4d3608",
  "timestamp": "2026-07-18T13:07:43Z",
  "prompt": "Hello!",
  "prompt_tokens": 15,
  "output_tokens": 15,
  "step_dt": [81.637, 7.066, 7.017, 7.015, 6.878, 6.81, 6.838, 6.937, 6.871, 6.902,
              6.824, 6.759, 6.864, 6.926, 7.001, 6.784, 6.858, 7.153, 6.942, 7.048],
  "ttft_s": 81.637,
  "generation_time_s": 213.13,
  "tokens_per_sec": 0.07037958053769999,
  "itl_p50_s": 6.902,
  "itl_p95_s": 7.0747,
  "decode_time_s": 0.0,
  "e2e_latency_s": 240.949642,
  "result_text": "Hello! How can I help you today?\nthought",
  "embed_stats": {"mean": 0.004217, "std": 1.108908, "min": -15.3125, "max": 16.875},
  "levers": {"NUM_MICRO_BATCHES": 4, "STAGGER_INTERVAL": 3.0, "SEQ_LEN": 1, "WORLD_SIZE": 51},
  "parse_ok": true,
  "parse_warnings": []
}
```

- **今回の主目的（levers がログ由来で埋まっているか）の機械的確認**: `levers.SEQ_LEN=1` は，
  Iter1 の同フィールド（`null`．env フォールバックでは値を取得できず未設定だった）と異なり，**非 null の
  実測値**で埋まっている．`pipeline_inference.py:309` の実装 `self.seq_len = int(os.environ.get("SEQ_LEN", "1"))`
  と，rank0 ログの levers 行（`SEQ_LEN=1`）が一致しており，5 の実ログ行から `_extract_levers` が値を取り出して
  `levers_from_log` を経由し JSONL の `levers` に採用されていることを確認した（env フォールバックだった場合，
  Iter1 と同じく `SEQ_LEN=null` になっていたはずである）．他 3 フィールド（`NUM_MICRO_BATCHES=4`，
  `STAGGER_INTERVAL=3.0`，`WORLD_SIZE=51`）もログ行の値と完全一致．
- **内部整合の機械的チェック**（Iter1 分析(実行)と同型の検算，`python3` で実施）:
  - `sum(step_dt) = 213.13 = generation_time_s`（完全一致）．
  - `ttft_s(81.637) == step_dt[0](81.637)`: True．
  - `e2e_latency_s(240.9496) − generation_time_s(213.13) = 27.8196s`（符号・大小関係は正常，e2e ≥ generation）．
  - `parse_ok=true`，`parse_warnings=[]`（Iter2 で対処した複数行 RESULT のフォールバック警告も今回発生せず）．
- **注記（判定は行わない）**: 今回の `ttft_s=81.637s` は Iter1（`ttft_s≈26.0s`）より大きいが，3 節で記録した
  デプロイ直後の 51 ノード再接続（プロセスグループ再初期化 348s）に伴う一時的な状態と，Iter1 実行時の
  クラスタ状態（既に稼働継続中）との違いが疑われる．絶対水準・Iter1 との比較評価は本フェーズの対象外
  （analyst の担当）．

**7. 事後ヘルスチェック**

- `uv run python tools/healthcheck.py`（実験後）: **51/51 healthy**（全項目 OK，異常ノード無し）．

**8. 実行/ログ上の異常の有無（まとめ）**

- デプロイ: 異常無し（51/51 成功）．
- 推論実行: 1 回目の `ConnectionRefusedError` は 3 節で原因特定済みの一時的事象（51 ノード再接続完了前の
  リクエスト）であり，実害・ノード障害ではない．2 回目で正常完了．
- ログ: 新しい levers 行が想定どおり出力され，`results/Iter3.jsonl` の `levers` フィールドがログ由来の
  非 null 値で埋まっていることを確認した．`parse_ok=true`，`parse_warnings=[]`．
- クラスタ健全性: デプロイ前後とも 51/51 healthy．

**次フェーズ（分析(実行)）への申し送り**

- `mise run predict:demo` は `mise.toml` 上 `--iter Iter1` に固定されているため，本フェーズは
  `uv run python tools/collect_results.py --iter Iter3 --prompt "Hello!"` を直接実行して `results/Iter3.jsonl`
  へ出力した．`mise.toml` 側の固定値を `Iter{n}` 変数化する改修余地は残る（要レビュー，今回の変更範囲外）．
  `mise run predict:demo` をそのまま使うと現状 `results/Iter1.jsonl` に上書き追記される点に注意．
- n=1 のため run 間ばらつきは未知（Iter1 と同じ既知の限界）．
- success_criteria①（Iter1 で確立済み）に対する機械検証と，levers 記録堅牢化（P1）の成功条件充足可否の
  判定は analyst に委ねる．

---

### 実装 (Iter3)

**担当**: 実装フェーズ subagent（2026-07-18）．計画フェーズ（本ブロック下 `### 計画 (Iter3)`）が確定した単一レバー
「P1: levers 記録の堅牢化（env 由来 → ログ由来）」を，計画の指定どおり 3 ファイルのみ変更して実装した．
実機クラスタへの deploy／推論実行は行っていない（フェーズ4は人間確認後にオーケストレータが着手）．

**変更内容（計画からの差異は無し）**

1. **`pipeline_inference.py`**（`:1441` `Rank 0: prompt='...'` の直後，`if self.config.rank == 0:` ブロック内・
   生成ループ外）に，計画どおり `self.config.num_micro_batches`／`self.config.stagger_interval`／
   `self.config.seq_len`／`self.config.world_size` を 1 行 INFO ログで出す処理を追加した（起動バナー `:2008-2015`
   へは追加していない）。出力書式:
   `Rank 0: levers NUM_MICRO_BATCHES=<int> STAGGER_INTERVAL=<float> SEQ_LEN=<int> WORLD_SIZE=<int>`。
   `:1443` のローカル変数 `seq_len`（prompt token 数）とは取り違えず，`self.config.seq_len` を使用した。
2. **`tools/collect_results.py`**: `_LEVERS_RE` を新設（既存正規表現群の並びに追加），`_extract_levers(block)` を
   `_extract_prompt_tokens_and_embed` と同型で新設，`ParsedLog` に `levers_from_log: dict[str, int | float | None]
   | None = None`（デフォルト付き，既存 keyword 構築テストと非破壊）を追加し，`parse_rank0_log` の本 return で
   選択済み `block` から `_extract_levers(block)` を設定するよう配線した。`build_levers(config,
   levers_from_log=None)` へシグネチャを変更し，`levers_from_log is not None` ならそれを優先採用，`None` なら
   従来の env/`ClusterConfig` フォールバック（`_to_number` 本体は無改変）へ回す 2 段構えにした。
   `run_and_collect` の呼び出しを `build_levers(config, parsed.levers_from_log)` に更新した。
3. **`tests/test_collect_results.py`**: TL1〜TL8（8 件）を追加した。核心の TL6
   （`test_build_levers_prefers_log_over_env`）は env と `levers_from_log` を意図的に食い違わせ，ログ側が
   採用されることを確認する。TL8（`test_levers_bound_to_selected_block_in_multiblock_log`）は 2 ブロック
   （levers 値が異なる）を用意し，`predict_result` が一致する（最新でない）ブロックの levers に正しく紐づき，
   他ブロックの levers と混同しないことを確認する。

**テスト結果**

- `uv run pytest tests/test_collect_results.py -v`: **38 passed, 0 failed**（既存 30 件 + 新規 8 件，回帰無し）。
- `uv run python -m py_compile pipeline_inference.py tools/collect_results.py tests/test_collect_results.py`:
  エラー無し。
- `git diff --name-only` のコード変更は `pipeline_inference.py`／`tools/collect_results.py`／
  `tests/test_collect_results.py` の 3 ファイルのみ（`git status` に新規 `.log` フィクスチャの混入も無し）。
  `tools/predict.py`／`tools/common.py`／`mise.toml`／JSONL スキーマは無改変。

**計画からの差異**: 無し。TL4（`test_parse_rank0_log_populates_levers_from_log`）のみ，計画の例示ログに
`prompt tokens=...` 行が無く `parse_ok` が意図せず `False` になったため，実装時に当該行を追加して `parse_ok=True`
まで確認できるようにした（計画の意図「levers_from_log が選択済みブロックから設定され，かつ parse_ok が従来
どおり決まることを確認する」を満たすための軽微な補完であり，レバー本体・成功条件には影響しない）。

フェーズ4（実機実験）は本イテレーションのフェーズ2・3の範囲外であり，B1 の人間確認後にオーケストレータが着手する。

---

### 計画 (Iter3)

**担当**: 計画フェーズ subagent（2026-07-18）．単一レバー「P1: levers 記録の堅牢化」（backlog B4，auto-decided）を，
調査フェーズの結論（本ブロック下 `### 調査 (Iter3)`）と実コード（`pipeline_inference.py`／`tools/collect_results.py`）に
照らして実装手順・追加テスト・成功条件へ落とし込んだ．**本イテレーションのフェーズ2・3 はコード実装・単体テストのみで，
実機クラスタへの deploy/推論実行は行わない**（フェーズ4は B1 の人間確認後にオーケストレータが着手する）．

#### 1. 仮説

rank0 が **リクエスト毎に**「実際に効いた実行設定（`self.config` 解決後の 4 レバー）」を 1 行 INFO ログで出し，
収集側（`collect_results.py`）が **その当該ブロックのログ行から** levers を確定すれば，現行 `build_levers` が抱える
「コンテナ起動時 env と収集ツール実行時 env の一致」という暗黙仮定（`collect_results.py:405-406` に明記）を排除でき，
②（`NUM_MICRO_BATCHES`／`STAGGER_INTERVAL` 掃引）で env が不一致になっても記録レバーが実レバーと食い違わない．
変更はホットパス外の per-request 1 print とパース純関数に閉じるため単体テストで完了条件を組める．

#### 2. 単一レバー・変更内容

変更ファイルは **`pipeline_inference.py`（1 行追加）**，**`tools/collect_results.py`（levers 確定をログ優先化）**，
**`tests/test_collect_results.py`（テスト追加）** の 3 つのみ．`tools/predict.py`／`tools/common.py`／`mise.toml`／
JSONL スキーマは非改変．固定する構成: 出力位置・書式・rank0 限定・env フォールバック維持は調査フェーズの推奨方式に
固定し，本イテレーションで動かすのは「levers を env 由来からログ由来へ切り替える」1 点のみとする（単一レバー原則）．

**(A) `pipeline_inference.py`（per-request 1 行 INFO 追加）**

- **出力位置**: rank0 のブロック開始マーカー `_log("INFO", f"Rank 0: prompt='{prompt}'")`（`pipeline_inference.py:1441`）の
  **直後**（次行 `input_ids = _tokenize(prompt)` `:1442` の手前）に 1 行追加する．`if self.config.rank == 0:` ブロック内
  （`:1439`）かつ生成ループ（`:1451` 開始）の**外側**なので graph-break・性能影響は無い（`torch.compile` 実呼び出しは
  リポジトリに 0 件）．
- **正確な追加コード**（`self.config` は当該メソッド内で有効．`:1434` `self.config.rank` 等で既出）:
  ```python
  _log("INFO", f"Rank 0: levers NUM_MICRO_BATCHES={self.config.num_micro_batches} STAGGER_INTERVAL={self.config.stagger_interval} SEQ_LEN={self.config.seq_len} WORLD_SIZE={self.config.world_size}")
  ```
- **値の出所**: `PipelineConfig.__init__`（`:288-309`）が env→既定値の順に解決した確定値
  （`num_micro_batches` int，`stagger_interval` float，`seq_len` int（既定 1），`world_size` int）．env を直接読むより
  堅牢で「起動時に実際に効いた値」を出せる（特に `SEQ_LEN` は現行 `build_levers` が env 未設定時 `null` にするが実効は
  既定 1．`config.seq_len` を出せば解消する）．**`prompt` 直後の `seq_len`（`:1443` で prompt token 数に再代入）とは別物**
  なので，必ず `self.config.seq_len`（KV キャッシュ上限レバー）を使うこと．
- **物理ログの見え方**: `_log`（`:180-192`）が `[R{rank} {tag}] {msg}` を付すため，rank0 の物理行は
  `[R0 INFO] Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=3.0 SEQ_LEN=1 WORLD_SIZE=51` となる．
- **rank0 限定で十分**: 収集は rank0 の `docker logs` のみを見る（`collect_rank0_log:477-494`，`_extract_rank0_messages`
  は `R0` レコードのみ残す `:94`）．この位置は `if self.config.rank == 0:` 内なので自然に rank0 のみ出る．
  **起動バナー（`:2008-2015`）への追加は不採用**（`docker logs --since {run_start}` 窓の外になり収集されない．調査フェーズ
  「`--since` 窓の落とし穴」参照）．

**(B) `tools/collect_results.py`（levers をログ優先・env フォールバックの 2 段構えへ）**

- **(B-1) 正規表現 `_LEVERS_RE` を新設**（既存の per-block 抽出用正規表現群 `:45-60` と同じ場所に置く）:
  ```python
  _LEVERS_RE = re.compile(
      r"^Rank 0: levers NUM_MICRO_BATCHES=(\d+) STAGGER_INTERVAL=([\d.]+) "
      r"SEQ_LEN=(\d+) WORLD_SIZE=(\d+)$"
  )
  ```
  `_extract_rank0_messages` が `[R0 INFO] ` プレフィックスを剥がした本文（`Rank 0: levers ...`）に対して照合する
  （既存 `_PROMPT_TOKENS_EMBED_RE` 等と同型）．`STAGGER_INTERVAL` は config lever 値（0.0/0.5/1.0，既定 3.0）が
  `f"{float}"` で `0.0`/`0.5`/`1.0`/`3.0` と出るため `([\d.]+)` で確実に拾える．
- **(B-2) per-block 抽出関数 `_extract_levers` を新設**（既存 `_extract_prompt_tokens_and_embed` 等 `:179-226` と同型）:
  ```python
  def _extract_levers(block: list[str]) -> dict[str, int | float | None] | None:
      """ブロック内の `Rank 0: levers NUM_MICRO_BATCHES=... ...` 行から実効 levers を抽出する．
      見つからなければ None（旧ログ互換で env フォールバックへ回す）．"""
      for msg in block:
          match = _LEVERS_RE.match(msg)
          if match:
              return {
                  "NUM_MICRO_BATCHES": int(match.group(1)),
                  "STAGGER_INTERVAL": float(match.group(2)),
                  "SEQ_LEN": int(match.group(3)),
                  "WORLD_SIZE": int(match.group(4)),
              }
      return None
  ```
- **(B-3) `ParsedLog` に `levers_from_log: dict[str, int | float | None] | None = None` を追加**（`:229-240`．既存
  `parse_warnings` の後ろ＝デフォルト付きフィールドとして追加するので，早期 return 分岐（`:258-271`）は無改変で通り，
  `ParsedLog(...)` を keyword 構築する既存テスト（`:452`）も影響を受けない）．`parse_rank0_log` の本 return（`:299-304`）で
  `levers_from_log=_extract_levers(block)` を渡す（**選択済みブロック** `block` から読むため，複数 run が並んでも
  `_select_relevant_block` が選んだ正しいブロックの levers が紐づく）．
- **(B-4) `build_levers` をログ優先へ変更**（`:397-422`）．シグネチャに任意引数を追加し，ログ由来があればそれを採用，
  無ければ従来の env/config フォールバックを残す（後方互換）:
  ```python
  def build_levers(
      config: ClusterConfig,
      levers_from_log: dict[str, int | float | None] | None = None,
  ) -> dict[str, int | float | None]:
      if levers_from_log is not None:
          return dict(levers_from_log)
      # フォールバック（旧ログ・パース失敗時）: 従来の env/config 由来（既存 `_to_number` 本体をそのまま残す）
      ...
  ```
- **(B-5) `run_and_collect`（`:516`）の呼び出しを `levers = build_levers(config, parsed.levers_from_log)` に変更**．
  これが唯一の配線変更（`build_record` 以降は無改変）．
- docstring（`build_levers` / `parse_rank0_log` の Returns）に「ログ優先・env フォールバック」「`levers_from_log`」の
  記述を追記する．

#### 3. 追加すべきテストケース（`tests/test_collect_results.py`．既存 30 件は全て維持）

既存 3 件（`test_build_levers_reads_config_defaults_and_seq_len_from_env` 他 `:392-427`）は `build_levers(fake_config)` を
1 引数で呼ぶため，任意引数追加後もそのまま pass（＝env フォールバック経路の回帰確認を兼ねる）．新規は物理ログ
（`[R0 ...]` プレフィックス付き）のインライン文字列で与え，新規 `.log` フィクスチャは作らない（Iter2 の `*.log`
gitignore トラップ回避）．追加（最低 6 件）:

- **TL1 `test_extract_levers_parses_typed_values`**: `_extract_levers` が `Rank 0: levers NUM_MICRO_BATCHES=8
  STAGGER_INTERVAL=0.5 SEQ_LEN=512 WORLD_SIZE=21` を含むブロックから `{NUM_MICRO_BATCHES:8, STAGGER_INTERVAL:0.5,
  SEQ_LEN:512, WORLD_SIZE:21}` を返し，型が int/float で正しいことを assert（`isinstance` 検査を含める）．
- **TL2 `test_extract_levers_returns_none_when_line_absent`**: levers 行を含まないブロック（旧形式）で `None` を返す．
- **TL3 `test_extract_levers_handles_stagger_zero_and_default`**: `STAGGER_INTERVAL=0.0` および `=3.0` が `0.0`/`3.0`
  (float) として拾える（掃引で使う 0.0/0.5/1.0 と既定 3.0 の書式カバレッジ）．
- **TL4 `test_parse_rank0_log_populates_levers_from_log`**: 物理ログ全体（`[R0 INFO] Rank 0: prompt='...'` の直後に
  `[R0 INFO] Rank 0: levers ...` を置く）を `parse_rank0_log` に通し，`levers_from_log` が期待 dict になり，かつ
  `_extract_rank0_messages`→`_split_into_blocks`→`_extract_levers` の end-to-end で `[R0 INFO] ` 剥離と正規表現一致が
  効くこと・`parse_ok` が従来同様に決まることを assert（出力書式が収集側正規表現と噛み合うことの検証）．
- **TL5 `test_parse_rank0_log_levers_from_log_none_for_legacy_log`**: levers 行の無い旧形式ログで `levers_from_log is None`
  （後方互換）．
- **TL6 `test_build_levers_prefers_log_over_env`（本レバーの核心）**: `os.environ` の
  NUM_MICRO_BATCHES/STAGGER_INTERVAL/WORLD_SIZE/SEQ_LEN と `fake_config` を，`levers_from_log` と **食い違う値**に設定した上で
  `build_levers(fake_config, levers_from_log=<log値>)` を呼び，戻り値が `levers_from_log` と一致する（ログ側が優先され env が
  無視される）ことを assert．
- **TL7 `test_build_levers_falls_back_to_env_when_log_none`**: `build_levers(fake_config, None)` が従来どおり env/config から
  構築する（＝既存 3 件と同じ経路．env 不在時の SEQ_LEN=null も確認）．
- **TL8 `test_levers_bound_to_selected_block_in_multiblock_log`**: 2 ブロック（levers 値が異なる）を並べ，`predict_result`
  が **先（最新でない）ブロック** の RESULT に一致する状況で，`parse_rank0_log(...).levers_from_log` が**一致した正しい
  ブロックの levers**（最新ブロックのものではない）になることを assert．②の per-run クロス汚染防止が levers 記録でも
  働くことの検証（Iter2 の T5 と対になる）．

#### 4. 成功条件（measurable・コードレベル．実機接続不要）

判定はすべて決定的（純関数・dataclass の pass/fail）でノイズ幅の見積もりは不要．以下を全て満たせば「採用」候補とする．

1. `uv run pytest tests/test_collect_results.py` が green．**既存 30 件が全て pass のまま**，新規 TL1〜TL8（最低 6 件）も
   pass（合計 36 件以上 passed，failed/error 0）．
2. TL6 が示すとおり `build_levers(config, levers_from_log)` は **env と食い違ってもログ由来を採用**する（P1 の核心＝
   env 依存の暗黙仮定の排除）．
3. TL7 が示すとおり `levers_from_log is None`（旧ログ・パース失敗）では従来の env/config フォールバックが維持される
   （後方互換）．
4. TL4/TL8 が示すとおり，levers は `parse_rank0_log` の**選択済みブロック**から抽出され，複数ブロックでも正しい run に
   紐づく．
5. `uv run python -m py_compile pipeline_inference.py tools/collect_results.py tests/test_collect_results.py` が
   エラー無し（lint/型チェッカーはリポジトリ未導入．`py_compile` は import せず構文のみ検査するため，
   `pipeline_inference.py` の重い依存を走らせずに追加行の構文健全性を確認できる）．
6. スコープ厳守: `git diff --name-only` の**コード変更**が `pipeline_inference.py`／`tools/collect_results.py`／
   `tests/test_collect_results.py` の 3 ファイルのみ（`predict.py`／`common.py`／`mise.toml`／JSONL スキーマ非改変．
   `git status` に新規 `.log` が現れないこと）．

#### 5. フェーズ4（実験・実機接続）は本イテレーションの範囲外

- **フェーズ2（本計画）・フェーズ3（実装）はコード実装・単体テストのみで完結する**．`pipeline_inference.py` への 1 行追加
  自体は graph-break リスクが無いが，実際に「rank0 ログに levers 行が出て収集側が拾う」ことの end-to-end 確認には
  `mise run deploy`（再デプロイ）＋推論実行が必要で，これは 51 ノード実機接続を伴う．
- backlog B4 の通り，**Iteration 3 のフェーズ4（実験）へ進む前に，B1 の合意に基づく人間確認（Slack）が必須**．
  実機での最終確認は②（レバー掃引）の最初の承認済み実 run に畳み込めばよく，本レバーのためだけに 51 ノードを単独
  起動する必要は無い．オーケストレータはフェーズ4の直前で必ず人間確認を挟むこと（コードとテストだけでこのフェーズ2・3
  は完了と扱う）．

#### 6. 実装フェーズ（rc-implementer）への申し送り

- **変更キー・箇所**: (A) `pipeline_inference.py:1441` の直後に §2(A) の 1 行を追加（`self.config.*` の解決値を使用．
  `self.config.seq_len` を使い `:1443` の `seq_len` と取り違えない．起動バナー `:2008-2015` には**足さない**）．
  (B) `tools/collect_results.py` に `_LEVERS_RE`（§2 B-1）／`_extract_levers`（B-2）を新設，`ParsedLog.levers_from_log`
  （B-3）を追加し `parse_rank0_log` 本 return で設定，`build_levers` にログ優先分岐（B-4）を追加，`run_and_collect` の
  呼び出しを `build_levers(config, parsed.levers_from_log)`（B-5）へ変更．
- **非改変厳守**: `tools/predict.py`／`tools/common.py`（`ClusterConfig`）／`mise.toml`／JSONL スキーマは触らない．
  `build_levers` の env フォールバック本体（`_to_number` 込み）は既存のまま残す（後方互換）．`_extract_result_text` 等
  Iter2 の照合ロジックは無改変．
- **既存テスト非破壊の確認観点**: 既存 `build_levers` 3 件（`:392-427`，1 引数呼び出し）と `ParsedLog(...)` を keyword 構築
  する既存テスト（`:452`）が，任意引数・デフォルト付き新フィールド追加後もそのまま pass することを実行で確認する．
- **禁止事項の再掲**: 実機への `deploy`/`predict:demo` 実行はしない（フェーズ2・3 はコードとテストのみ）．フェーズ4は
  人間確認後にオーケストレータが着手する．

---

### 調査 (Iter3)

**担当**: 調査フェーズ subagent（2026-07-18）．単一レバー「P1: levers 記録の堅牢化」（backlog B4）の計画に向け，
`pipeline_inference.py` の起動・実行設定の決定箇所と `tools/collect_results.py` の levers 構築を，実機に触れず
コード読み取りのみで調査した．実機クラスタへの接続・deploy/推論実行は一切していない．

**問い**
1. 4 レバー（`NUM_MICRO_BATCHES`/`STAGGER_INTERVAL`/`SEQ_LEN`/`WORLD_SIZE`）の「有効値」はどこでどう決まるか．
2. 起動時 1 行 INFO ログをどこに・どの書式で出すのが自然か．全 rank か rank0 のみか．
3. `collect_results.py` 側のパース統合方式（既存 rank0 ログパースに載せられるか）．
4. graph-break / ホットパス性能への影響．

**分かったこと（コード出典＝リポジトリ内 ファイル:行）**

- **4 レバーの有効値は全て `PipelineConfig.__init__`（`pipeline_inference.py:288-309`）が `os.environ` → 既定値の
  順で解決し，`config.num_micro_batches`/`config.stagger_interval`/`config.seq_len`/`config.world_size` に確定する**．
  既定値は `NUM_MICRO_BATCHES=4`（`DEFAULT_NUM_MICRO_BATCHES`，`:63`），`STAGGER_INTERVAL=3.0`（`:64`），
  `SEQ_LEN=1`（`os.environ.get("SEQ_LEN","1")`，`:309`），`WORLD_SIZE` は必須（既定無し，`:289`）．
  → **1 行ログに出すべきはこの 4 つの解決後の値**（env そのものではなく `config.*` の確定値）．env を直接読むより
  堅牢＝「起動時に実際に効いた値」を出せる．特に `SEQ_LEN` は現行 `build_levers`（下記）が env 未設定時に `null` に
  するが，実際の有効値は既定 `1` である（この食い違いも `config.seq_len` を出せば解消する）．
- **命名の注意（計画・実装向け）**: 委譲元・backlog は「`ClusterConfig`」と呼ぶが，`pipeline_inference.py` の設定
  クラスは `PipelineConfig` である（`ClusterConfig` は `tools/common.py` にある別クラスで，`deploy.py`/`collect_results.py`
  が使う）．両者は独立（deploy 側 `ClusterConfig` の env を各コンテナへ注入し，コンテナ内 `PipelineConfig` が読む）．
- **`main()` には既に起動バナーがある（`pipeline_inference.py:2008-2015`）**．`_log("INFO", ...)` で rank/world_size・
  assigned layers・hidden size・weight format・master を出しているが，**4 レバーのうち出ているのは world_size のみで
  `NUM_MICRO_BATCHES`/`STAGGER_INTERVAL`/`SEQ_LEN` は出ていない**．また `:1020` に
  `_log("OK", "Inference loop started. micro_batches=... pipeline_stages=...")` があり micro_batches と world_size は
  既に出るが，stagger/seq_len は無く，レベルも OK．いずれも 4 レバー全部を機械可読に並べた 1 行ではない．
- **rank0 のみで足りる（重要）**: 収集側 `collect_rank0_log`（`collect_results.py:477-494`）は wafl-ctrl1（=rank0）の
  `docker logs` **だけ**を取得し，パースは `_extract_rank0_messages` が `[R0 ...]` 行だけを残す（`:64-94`）．
  よって**消費されるのは rank0 の 1 行だけ**．env はコンテナ毎注入だが `deploy.py:563,573` が全コンテナへ同一値
  （deploy 時 `ClusterConfig` の `world_size`/`num_micro_batches` 等）を渡すため設計上は全 rank 共通．記録に使うのは
  rank0 自身が実際に使った値であり自己整合する．全 rank が出しても害は無い（バナーは既に全 rank 出力）が，
  **パーサが必要とするのは rank0 の 1 行のみ**．
- **graph-break / 性能影響は無い**: `pipeline_inference.py` に **`torch.compile()` の実呼び出しは無い**
  （`:164-165` はコメントで言及するのみ．grep で呼び出し 0 件）．追加するのは起動時または 1 リクエスト 1 回の `print`
  1 行で，トークン生成ループ（`:1451` 開始）の外側．デコードループは 1 step ごとに複数 INFO 行を既に出している
  （`:1453` 等）ため，1 リクエスト 1 行の増加は定常運用に対し無視できる．

**収集側パース統合と「`--since` 窓」の落とし穴（計画に必須の設計論点）**

- `collect_results.py` の levers は `build_levers`（`:397-422`）が **`ClusterConfig`（＝収集ツール実行時の
  `os.environ`）由来**で構築しており，「コンテナ起動時 env と収集時 env の一致」を暗黙仮定する（`:405-406` に明記）．
  これが B4 で潰す対象．ログ由来へ切り替えれば env 依存を排除できる．
- **落とし穴（最重要）**: `collect_rank0_log` は `docker logs --since {run_start}` を使い，`run_start` は**プロンプト
  送信直前**の `datetime.now(UTC)`（`run_and_collect:500`）．一方コンテナ／`main()` バナーの起動ログは **deploy 時に
  一度だけ**出る（通常は run より遥か前）．したがって **起動時バナーに 1 行足すだけでは `--since run_start` 窓から
  外れて収集側に見えない可能性が高い**．この点を計画が握らないと「ログは出るのに収集できない」齟齬になる．
- **推奨する両立策 ＝ 設定 1 行を「リクエスト毎」に rank0 のブロック内で出す**．具体的には rank0 のブロック開始
  マーカー `_log("INFO", f"Rank 0: prompt='{prompt}'")`（`pipeline_inference.py:1441`）の**直後**に，
  `_log("INFO", f"Rank 0: levers NUM_MICRO_BATCHES={config.num_micro_batches} STAGGER_INTERVAL={config.stagger_interval} SEQ_LEN={config.seq_len} WORLD_SIZE={config.world_size}")`
  のような 1 行を足す（`config` は `self.config`）．こうすると，
  1. `--since run_start` 窓に必ず入る（そのリクエストの処理中に出る）．
  2. `_split_into_blocks`（`collect_results.py:97-109`）の**ブロック内**（開始マーカー `^Rank 0: prompt='` の直後）に
     入り，既存の per-block 抽出（`_extract_prompt_tokens_and_embed` 等，`:179-226`）と**同じ枠組み**で
     `_extract_levers(block) -> dict` を 1 個足せばよい（`_select_relevant_block` で選んだ当該ブロックから読む）．
  3. rank0 行なので `_extract_rank0_messages` を素通りする．
  - 書式は空白区切り `KEY=VALUE`（例: 物理ログ `[R0 INFO] Rank 0: levers NUM_MICRO_BATCHES=4 STAGGER_INTERVAL=3.0 SEQ_LEN=1 WORLD_SIZE=51`）が読みやすく，
    正規表現 `^Rank 0: levers NUM_MICRO_BATCHES=(\d+) STAGGER_INTERVAL=([\d.]+) SEQ_LEN=(\d+) WORLD_SIZE=(\d+)$` で
    確実に拾える．既存マーカーが全て `Rank 0: ` 始まりなので接頭辞を合わせると一貫する（`_PROMPT_TOKENS_EMBED_RE`
    等と同様）．
  - `build_levers` は「ログから取れたらそれを採用，取れなければ現行の env フォールバック（`ClusterConfig`）」の
    2 段構えにすると後方互換（旧ログ・パース失敗時も `null`/env で埋まる）を保てる．
- **代替（起動バナーに 1 行）を採る場合**は，`collect_rank0_log` の `--since` を起動時刻まで広げる（あるいは
  `docker logs` 全取得）改修が別途必要になり，複数 run 蓄積時のブロック分離が重くなる．per-request 方式の方が
  スコープが `collect_results.py` の per-block 抽出追加に閉じて筋が良い．なお per-request で出すと，同一 run を
  複数ブロック取り違える状況（Iter2 で対処済み）でも levers が正しいブロックに紐づく利点がある．

**次フェーズ（計画）への示唆**

- **出力位置**: rank0 の per-request パス，`pipeline_inference.py:1441`（`Rank 0: prompt='...'`）の直後に 1 行 INFO を
  追加（起動バナー `:2008-2015` への追加は `--since` 窓から外れるため非推奨）．値は `self.config.num_micro_batches`
  /`stagger_interval`/`seq_len`/`world_size` の解決後の値を使う．
- **rank0 限定で足りる**: 収集は rank0 の docker logs のみを見るため，パーサは rank0 の 1 行だけ要る（全 rank 出力は
  無害だが不要）．
- **フォーマット案**: `Rank 0: levers NUM_MICRO_BATCHES=<int> STAGGER_INTERVAL=<float> SEQ_LEN=<int> WORLD_SIZE=<int>`
  （空白区切り KEY=VALUE，接頭辞 `Rank 0: ` で既存マーカー群と一貫）．
- **パース方式**: `collect_results.py` に `_LEVERS_RE` と per-block `_extract_levers(block)` を新設し，`build_levers` を
  「ログ優先・env フォールバック」の 2 段構えへ変更（`build_levers` は現状 `ClusterConfig` 引数のみ．計画で
  `parsed`/選択ブロック経由の levers を渡す形へシグネチャ変更が要るか検討）．per-block 抽出の器は既存 4 関数
  （`:179-226`）と同型で載せられる．
- **graph-break リスク無し**: `torch.compile` 実呼び出しは無く，追加は生成ループ外の per-request 1 print．定常性能へ
  の影響は無視できる．
- **回帰テスト**: `_extract_levers` の単体テスト（正常 1 行・欠落時 `null`・env フォールバック）と，設定行を含む
  複数行ブロック入力での `parse_rank0_log`/`build_levers` の統合テストを追加すれば，実機非接続で完了条件を組める
  （Iter1/Iter2 と同じくパース純関数中心）．
- **フェーズ4（実験）前の人間確認**: backlog B4 の通り，本 P1 は `pipeline_inference.py`（ホットパス）改変・再デプロイ
  を伴うため，実機 deploy/推論実行の直前に B1 の人間確認が必須．フェーズ1〜3 はコードのみで進行可能．

---

## Iteration 2

### 考察・次計画 (Iter2)

**担当**: 考察・次計画 subagent（2026-07-18）．分析(解釈) の結論（本ブロック `### 分析(解釈) (Iter2)`）を受け，
本イテレーションの単一レバー「RESULT 複数行対応による照合ロジックの頑健化」の採否を確定し，次イテレーションの
方向を決めた．実機への新規接続・実行はしていない（記録の読み取りとコミット操作のみ）．

**1. 採否判定: 採用（adopt）**

- **判定根拠**: 計画 §4 の成功条件 5 件をすべて機械的に充足している（分析(解釈) §1 の検証表）．具体的には，
  (1) pytest 30 passed（既存 23 + 新規 7，failed/error 0，実験フェーズが独立再実行で再現），
  (2) T3 で `parse_warnings == []`（Iter1 実観測の複数行 RESULT でフォールバック警告が消える），
  (3) T5 で正ブロック選択（最新でない並びでも取り違えない），(4) `py_compile` 構文健全，
  (5) スコープ厳守（`tools/collect_results.py`／`tests/test_collect_results.py` の 2 コードファイルのみ）．
- **追加反復の要否**: 不要．本イテレーションはパース純関数に閉じたコード修正のみで判定が決定的（測定ノイズ無し）で
  あり，1 回の独立再実行で確定済み．追加反復で得られる情報は無い．
- **このレバーの収束状況**: 「照合ロジックの頑健化」というレバー（Iter1 で採用した結果永続化基盤の信頼性を高める
  延長線）について，Iter1 分析(解釈) が指摘した高リスク（複数行 RESULT による弁別機構の常時無効化）は，根源(T3)・
  実作動(T5)・抜け穴(T4/T6) をカバーする回帰テストで**単体テストレベルでは解消**した．collect_results.py に閉じた
  頑健化として本レバーは**やり切った（このファイル内でこれ以上動かす対象は無い）**と判断する．

**2. 残存リスク（次イテレーション以降へ引き継ぐ）**

- **実機 end-to-end 未検証（最大の残存点）**: 妥当性は「調査・実験フェーズが読み取った実ログ形式を T3 が忠実に
  再現している」ことに依存する．実機で「フォールバック警告が実際に消える」ことの最終確認は，②の最初の承認済み
  実 run に畳み込む方針（本イテレーションでは未実施）．次に実機を叩く際の要確認点は分析(解釈) §3 に列挙済み．
- **`docker logs -t` 前提**: 継続行結合方式は継続行が素の本文であることに依存する．将来 `-t`（タイムスタンプ）を
  足すと方式が壊れる（設計上のトレードオフとして残す）．
- **未着手項目**: (b) levers 記録の堅牢化（P1，未解決），(c) レバー値あたり n≥3〜5 反復，(d) 主指標 ITL/TTFT の採用．
  (c)(d) は②の実験設計条件で②内で担保するが，(b) は②の妥当性に直結する独立の欠陥である（下記 3）．

**3. 次に振るレバーの決定: Iteration 3 = backlog B3 の P1（levers 記録の堅牢化）を先行**

- **決定**: research_frontier②（マイクロバッチ数・stagger interval のスループット感度分析）へ直行せず，
  **Iteration 3 として P1（levers 記録の堅牢化）を先に挟む**．分析(解釈) §4 の推奨を採用する．
- **根拠**: ②の本題は `NUM_MICRO_BATCHES`／`STAGGER_INTERVAL` を実際に振ることであり，「振ったレバー値が正しく
  記録されること自体」が比較の大前提である．本イテレーションで (a) 取り違え（RESULT 複数行照合）は解消したが，
  (b) levers 記録は今も収集ツール実行時の env/config 由来で「コンテナ起動時 env と収集時 env の一致」を暗黙仮定して
  いる．②で env が不一致になると，取り違えを直しても別経路で「どの run がどのレバー値だったか」が汚染され，比較の
  妥当性が根本から崩れる．頑健化の順序として (a)→(b)→② が筋が通る（片方だけ直して掃引に入ると結論が別要因で汚れる）．
- **P1 の実装方針（B3 記載）**: `pipeline_inference.py` 起動時に有効な実行設定（levers）を 1 行 INFO ログで出力し，
  収集側（`collect_results.py`）がそのログ行から levers を確定する．これにより env 由来の暗黙仮定を排除する．
- **可逆性**: 掃引前の頑健化順序の選択であり可逆．backlog に `[auto-decided]` として記録した（自動判断とした）．

**4. 要人間判断（オーケストレータへの申し送り）**

- P1 は Iter1・Iter2 の「`collect_results.py` に閉じた・非侵襲・クラスタ非接触」という性質とは異なり，
  **ホットパス（`pipeline_inference.py`）改変・再デプロイを伴う**（Iter1 考察・次計画で明記済み）．起動時 1 行 INFO
  追加自体は graph-break リスクの低い変更だが，動作確認には `mise run deploy`（再デプロイ）＋推論実行が必要になる
  可能性が高い．
- したがって **Iteration 3 のフェーズ4（実験）へ進む前に，backlog B1 の合意（実機への deploy/推論実行を伴う実験の
  前に必ず一度 Slack で確認を仰ぐ）に基づく人間確認が必須**である．Iteration 3 のフェーズ1〜3（調査・計画・実装）は
  コードのみで進められるが，フェーズ4の直前でオーケストレータが人間確認を挟むこと．この点を，state.json の status は
  `running`（次レバーは確定済み）としつつ，実験フェーズ手前でのブロックが必要になる旨として明示的に申し送る．

---

### 分析(解釈) (Iter2)

**担当**: 分析(解釈) subagent（2026-07-18）．`## Iteration 2` の全ブロック（調査・計画・実装・実験）を読み，
計画フェーズが定めた成功条件（`### 計画 (Iter2)` §4 の 1〜5）に照らした成否判定，Iter1 分析(解釈)が指摘した
高リスクの低減度，残存リスク，次イテレーション方針を解釈した．実機への新規接続・実行はしていない（記録の読み取りのみ）．

**前提（判定の枠組み）**: 本イテレーションはパース純関数に閉じたコード修正のみで，判定はすべて**決定的**
（測定ノイズを伴わない単体テストの pass/fail・`git diff --name-only` の集合一致）である．したがって Iter1 で
問題になった「n が小さくノイズ幅が未知」という論点は本イテレーションには当てはまらず，ノイズ/信号の切り分けは
不要（ばらつきの概念が該当しない）．評価はテストの**具体的な検証内容とカバレッジの範囲**で行う．

**1. 成否判定（計画 §4 の成功条件 1〜5 の機械的検証）**

| # | 成功条件（計画 §4） | 実装・実験フェーズの記録 | 判定 |
|---|---|---|---|
| 1 | pytest green・既存 23 件 pass のまま・新規 6 件以上 pass（計 29 件以上・failed/error 0） | 実装フェーズ 30 passed，実験フェーズが独立再実行で 30 passed を再現（FAILED/ERROR 0） | 満たす |
| 2 | T3 で `parse_warnings == []`（Iter1 実観測の複数行 RESULT でフォールバックが消える） | 実験フェーズが T3 本文（`tests/test_collect_results.py:211-239`）を直接確認し `assert parsed.parse_warnings == []` の存在と PASSED を確認 | 満たす |
| 3 | T5 で正しいブロック選択（`used an earlier block` 警告・取り違えない） | `test_select_relevant_block_picks_earlier_block_when_correct_block_is_not_latest` PASSED を実験フェーズが確認 | 満たす |
| 4 | `py_compile` で構文健全性（lint/型は未導入） | 実装フェーズが `py_compile` エラー無しを確認 | 満たす |
| 5 | スコープ厳守（変更が `collect_results.py` と `test_collect_results.py` の 2 ファイルのみ） | 実験フェーズが `git status --short` で 2 コード変更ファイルのみ・新規 `.log` 無しを確認（journal/state 更新は運用上のもの） | 満たす |

- 成功条件 5 件すべてを充足．新規テストは計画の T1〜T6（最低 6 件）に対し**7 件**（T2 に応答本文中の `'` を含む
  DOTALL greedy 回帰を 1 件追加），合計 30 件が pass．「29 件以上」の下限を 1 件上回る．
- **判定: 採用相当（コードレベルの完了条件をすべて満たす）**．修正が純関数に閉じ・判定が決定的であるため，
  この結論に追加反復は不要（1 回の独立再実行で確定済み）．

**2. Iter1 分析(解釈)が指摘した高リスクの低減度**

Iter1 分析(解釈)（`### 分析(解釈) (Iter1)` §2・§3）は，②着手前の高リスクとして「複数行 RESULT により防御的照合
（`predict_result[:100]` とログ RESULT テキストの `==`）が常時失敗し，弁別機構が事実上無効化 → ②の複数 run 連続実行で
**別 run の指標を誤ったレバー値へ紐付ける（correctness を直接損なう高リスク）**」を挙げていた．今回の修正の低減度を，
(a) テストカバレッジと (b) 実ログ形式の理解の正確さの両面で評価する．

- **(a) テストカバレッジ**: 弁別機構が復活するには修正 3 点（継続行結合・DOTALL・両辺 strip＋前方一致）が
  連動する必要がある（調査フェーズの指摘）．対応する回帰テストが個別に存在する:
  - T3 が「複数行 RESULT でフォールバック警告が消える（`parse_warnings == []`）」ことを直接 assert
    ＝弁別機構が常時失敗する根源を潰したことの検証．
  - T5 が「正ブロックが最新でない並びで取り違えず選択」＝**②で顕在化する取り違えの実作動**を直接検証
    （Iter1 が「複数ブロックが並ぶ②で初めて実害になる」と述べた当該ケース）．
  - T4（SSH strip 済み／HTTP 未 strip の両経路で一致）・T6（空スニペットの vacuous match ガード）が，
    Iter1 が「複数行だけでなく predict 側正規化差も効く」と指摘した strip 差・空一致の抜け穴を閉じる．
  → Iter1 が挙げた失敗要因（複数行・SSH/HTTP strip 差・100 文字 truncate 差・vacuous match）が個別ケースで
    カバーされており，弁別機構の復活は回帰テストで守られている．
- **(b) 実ログ形式の理解の正確さ**: T3 の入力は，実験(Iter1) が rank0 の生 `docker logs` を直接確認して記録した
  実観測形式（`[R0 RESULT] Request response: 'Hello! How can I help you today?`／改行／`thought`／改行／`'`＝先頭行のみ
  プレフィックス・継続行はプレフィックス無し）を忠実に再現している．継続行結合方式の前提（本物のレコードは
  `_log`（`pipeline_inference.py:180-192`）が必ず `[R\d+ \w+]` 始まりで出す・RESULT は `print` 1 回で埋め込み改行込み
  まるごと出るため継続行が先頭直後に連続し別レコードが割り込まない）も，調査フェーズがコード出典付きで確認済み．
  → 実ログ形式の理解は実観測とコード読解の両方に裏付けられており，T3 の再現は「机上の想定」ではなく実測に忠実．
- **結論**: Iter1 が指摘した「複数行 RESULT による弁別機構の常時無効化」という高リスクは，**単体テストレベルでは
  解消された**と言える（根源 T3・実作動 T5・抜け穴 T4/T6 をカバー）．ただし後述のとおり実機 end-to-end での
  最終確認は未了である．

**3. 残存リスクと end-to-end 未検証の意味**

- **実機 end-to-end 未検証（最大の残存点）**: 今回の妥当性は「調査・実験フェーズが読み取った実ログ形式を
  T3 が忠実に再現している」ことに依存する．計画 §5・実装の申し送りどおり，実機で「フォールバック警告が実際に
  消える」ことの最終確認は②の最初の承認済み実 run に畳み込む方針であり，本イテレーションでは未実施．
  → **次に実機で①/②を叩く際に注意深く見るべき点**:
  1. 複数行応答が返る run で `parse_warnings` が実際に空になるか（T3 の実機再現）．
  2. 複数 run を連続送信したとき，`run_id` と `result_text` の対応が保たれ，正しいブロックが選ばれるか
     （T5 の実機再現＝取り違え防止の本番作動）．
  3. `--since` 窓の粒度（秒未満の連投で同一窓に複数ブロックが残らないか．残る場合こそ弁別機構が試される）．
  4. rank0 コンテナのログに他 rank 行や想定外プレフィックスの割り込みが無いか（継続行結合の前提の実地確認）．
- **`docker logs -t` 前提**: 現状 `collect_rank0_log` は `-t`（タイムスタンプ）を付けておらず継続行が素の本文で
  あることが継続行結合方式の前提．将来 `-t` を足すと継続行にも時刻プレフィックスが付き方式が壊れる（調査・実装が
  申し送り済み）．②以降でブロック分離を時刻で堅くしたくなった場合の設計上のトレードオフとして残る．
- **今回の修正の対象外で未解決の項目（Iter1 分析(解釈) §3 の 4 条件のうち残り 3 つ）**:
  - **(b) levers 記録の堅牢化**: 未着手．`levers` は今も収集ツール実行時の env/config 由来で，「コンテナ起動時 env と
    収集時 env の一致」を暗黙仮定する．②は `NUM_MICRO_BATCHES`／`STAGGER_INTERVAL` を実際に振るため，この仮定が
    崩れると**記録レバーが実レバーと食い違い，比較の妥当性が根本から崩れる**（P1 の対象）．
  - **(c) レバー値あたり n≥3〜5 反復でノイズ幅確立**・**(d) truncation に強い主指標 ITL/TTFT の採用**: いずれも
    ②の実験設計条件であり，本イテレーション（コード修正）の対象外．②着手時に満たすべき前提として引き続き有効．
  - loop-detection truncation・繰り返しパターン検出はモデル/推論エンジン側の挙動で，収集ツールの範囲外（Iter1 で確認済み）．

**4. 次イテレーションへの示唆（②直行か Iteration 3=P1 先行か）**

backlog B3 に残る 2 択（Iteration 3=P1「levers 記録堅牢化」を先に挟む ／ research_frontier②「レバー掃引」へ直行）を，
Iter1 分析(解釈) §3 が挙げた②着手前の 4 条件の充足状況から判断する．

- (a) RESULT 複数行照合の修正 → **本イテレーションで解消**（上記 1・2）．
- (b) levers 記録の堅牢化 → **未解決**（上記 3）．
- (c) n≥3〜5 反復・(d) 主指標 ITL/TTFT → ②の実験設計条件（②内で担保）．

**示唆: ②へ直行せず，Iteration 3 として P1（levers 記録堅牢化）を先に挟むことを推奨する．** 理由:

- ②の本題は `NUM_MICRO_BATCHES`／`STAGGER_INTERVAL` を実際に振ることであり，**振ったレバー値が正しく記録される
  こと自体が比較の大前提**である．(a) の取り違えを直しても，(b) が未解決だと env 不一致という別経路で「どの run が
  どのレバー値だったか」が汚染され得る．Iter1 分析(解釈) が (b) を「②の妥当性に直結する」と明記しており，
  頑健化の順序として (a)→(b)→② が筋が通る（片方だけ直して掃引に入ると結論が別要因で汚れる）．
- P1 はホットパス外の起動時 1 行 INFO 追加で graph-break リスクは低いが，`pipeline_inference.py` 改変・**再デプロイを
  伴う**ため，②同様に実機接続（B1 の人間確認）が必要になる点は Iteration 3 の性質として reflector へ申し送る．
- **代替（②直行）の余地**: 「env 一致を毎 run 検証する軽量策＋run 厳密直列化＋狭い since 窓＋n≥3＋主指標 ITL/TTFT」を
  運用規約として固めれば②直行も不可能ではない（B3 の別案）．ただし運用規約依存で堅牢性は P1 実装に劣り，
  levers 誤記録の検出はできない．P1 先行を推奨とし，②直行は「実機接続コストを一度に払いたい」場合の次善とする．
- なお②・P1 いずれも実機接続（deploy/掃引 run）を伴うため，Iter1・Iter2 の「コードのみ」フェーズとは性質が変わり，
  B1 に基づく人間確認が必須になる．これは reflector が次計画を確定する際の分岐点として重要．

**次フェーズ（考察・次計画 reflector）への結論（採用/棄却/追加反復/レバー収束の材料）**

- **採用（adopt）が妥当**: 計画 §4 の成功条件 5 件をすべて機械的に充足（30 件 pass・T3 で `parse_warnings==[]`・
  T5 で取り違え回避・スコープ 2 ファイル厳守）．判定は決定的で**追加反復は不要**．
- **リスク低減**: Iter1 が指摘した「複数行 RESULT による弁別機構の常時無効化（②の取り違え高リスク）」は，根源(T3)・
  実作動(T5)・抜け穴(T4/T6) をカバーする回帰テストで**単体テストレベルでは解消**．実ログ形式の再現も実観測に忠実．
- **残存リスク**: 実機 end-to-end 未検証（②の初 run で T3/T5 の実機再現を要確認），`docker logs -t` 前提，および
  (b) levers 記録堅牢化・(c) n≥3 反復・(d) 主指標 ITL/TTFT が未解決（今回の対象外）．
- **次イテレーション**: ②へ直行せず **Iteration 3=P1（levers 記録堅牢化）を先に挟むことを推奨**（(a) は解消したが
  (b) が②の妥当性に直結・未解決のため）．②・P1 いずれも実機接続を伴い B1 の人間確認が必要になる点を申し送る．

---

### 実験 (Iter2)

**担当**: 実験フェーズ subagent（2026-07-18）．計画フェーズの判断（本ブロック下 `### 計画 (Iter2)` §5）
どおり，本イテレーションは**実機クラスタへの接続・deploy/推論実行を伴わないコードレベル検証**として実施した．
実装フェーズが報告した結果（30 件 pass）の独立した再現確認と，スコープ・テスト内容の事実確認のみを行った．

**1. 独立実行によるテスト結果の再現確認**

```
uv run pytest tests/test_collect_results.py -v
============================== 30 passed in 0.04s ==============================
```

実装フェーズの報告（30 件 pass，failed/error 0）と一致した．全 30 件のテスト名を `-v` 出力で確認し，
`FAILED`/`ERROR` は 0 件．

**2. スコープ確認（`git diff --name-only`）**

```
git status --short
 M .claude/research/journal.md
 M .claude/research/state.json
 M tests/test_collect_results.py
 M tools/collect_results.py
?? .claude/research/agent.json
```

コード変更ファイルは `tools/collect_results.py` と `tests/test_collect_results.py` の 2 ファイルのみ
（`git diff --stat` で `tools/collect_results.py` は 70 行変更・`tests/test_collect_results.py` は 145 行追加
のみで削除 0，新規テスト追記であることと整合）．`.claude/research/journal.md`／`state.json` の変更は
research-cycle の各フェーズが自身の記録を追記する運用上の更新であり，実装スコープ（`pipeline_inference.py`／
`tools/predict.py`／`tools/common.py`／`mise.toml`／JSONL スキーマ）には含まれない．`git status` に新規
`.log` ファイルは現れず，`.gitignore` の `*.log` トラップ回避も維持されていることを確認した．

**3. Iteration 1 で観測された複数行 RESULT ケース（T3 等）の動作確認**

`-v` 出力から，複数行 RESULT に関連するテストが全て個別に PASSED であることを確認した．

- `test_extract_rank0_messages_joins_continuation_lines_into_one_record` — PASSED
- `test_extract_result_text_restores_multiline_body_and_strips_closing_quote` — PASSED
- `test_extract_result_text_does_not_break_on_apostrophe_inside_multiline_body` — PASSED
- `test_parse_rank0_log_multiline_result_matches_without_fallback_warning`（T3，Iteration 1 実験で実際に
  観測された `"Hello! How can I help you today?\nthought"` 複数行応答を再現）— PASSED
- `test_select_relevant_block_matches_both_ssh_stripped_and_http_unstripped_predict_result`（T4）— PASSED
- `test_select_relevant_block_picks_earlier_block_when_correct_block_is_not_latest`（T5）— PASSED
- `test_select_relevant_block_empty_snippet_guard_does_not_vacuously_match_latest`（T6）— PASSED

該当テストの本文（`tests/test_collect_results.py:211-239`）を直接確認し，T3 は
`assert parsed.parse_warnings == []` を明示的に検証していることを確認した．これは Iteration 1 実験の
「実験 (Iter1)」ブロックで実際に観測された警告
（`no block's RESULT text matched the predict result prefix; used the latest block as a fallback`）が，
同一の複数行入力（`"Hello! How can I help you today?\nthought\n"`）に対して修正後は再現しないことをコード
レベルで確認するものであり，実装フェーズの主張と整合する．

**実行環境の注記**

`uv run` 実行時に `VIRTUAL_ENV=/mnt/data-raid/ktakahashi/workspace/WAFL-PEFT/.venv does not match the project
environment path .venv` という warning が出たが，`uv` は自動的に `distributed-llm/.venv` を使用しており
（pytest の `rootdir`/実行 python パスから確認），テスト結果には影響していない．

**申し送り（分析フェーズへ）**

- 本フェーズは実機接続・deploy/推論実行を一切行っていない．コードレベル検証（独立再実行によるテスト再現，
  スコープ確認，T3〜T6 の個別確認）のみで完結した．
- 数値の良否判定（採用/不採用の結論）は行っていない．分析フェーズで，計画フェーズの成功条件（本ブロック
  「計画 (Iter2)」§4 の 1〜5）に照らした判定を行うこと．

---

### 実装 (Iter2)

**担当**: 実装フェーズ subagent（2026-07-18）．計画フェーズ（本ブロック直下 `### 計画 (Iter2)`）が確定した
単一レバー「RESULT 複数行対応による照合ロジックの頑健化」を，`tools/collect_results.py` と
`tests/test_collect_results.py` の 2 ファイルのみに最小差分で反映した．実機クラスタへの接続・
`deploy`/`predict:demo` 実行は行っていない．

**変更内容（`tools/collect_results.py`，計画どおり 3 点一体）**

- **`_extract_rank0_messages`（継続行結合方式へ置換）**: `_RANK0_LINE_RE`（`^\[R0 \w+\] (.*)$`）を廃止し，
  任意 rank に一致する新設 `_LOG_LINE_RE = re.compile(r"^\[R(\d+) (\w+)\] (.*)$")` を導入．各物理行を
  ANSI 除去後にこの正規表現へ通し，マッチ＝新しい論理レコード開始（rank・本文を記録），非マッチかつ
  現在レコードが存在する場合のみ本文へ `"\n" + clean_line` を連結する継続行として扱う（現在レコードが
  無い状態の継続行，および先頭プレフィックスより前の行は捨てる）．全レコード構築後，rank0（`R0`）の
  レコードのみを順序保持で返す．
- **`_RESULT_RE` に `re.DOTALL` を付与**: `re.compile(r"^Request response: '(.*)$", re.DOTALL)`．
  継続行結合済みの 1 論理メッセージに対し，複数行本文でも全文を 1 回のマッチで捕捉できる．
  `_extract_result_text` の末尾 `'` 除去ロジックは変更していない（複数行でも閉じ `'` は論理メッセージ
  末尾に来るため `endswith("'")` が成立し，そのまま機能する）．
- **`_select_relevant_block` の照合を「両辺 strip＋前方一致」へ緩和**: `expected_prefix =
  predict_result[:100]` と `==` の完全一致を廃止し，`predict_norm = predict_result.strip()` と
  `snippet_norm = _extract_result_text(block).strip()` を用いた `predict_norm.startswith(snippet_norm)`
  による前方一致へ変更．`snippet` が `None` または `snippet.strip() == ""`（空スニペット）の場合は
  照合対象からスキップするガードを追加した（空文字列は任意の文字列と `startswith` が真になり誤って
  latest ブロックへ vacuously マッチしてしまうため）．一致ブロックが最新でないときの警告
  （`used an earlier block ...`）と，全不一致時のフォールバック警告（`used the latest block as a
  fallback`）の文言・挙動は計画どおり維持した．

**追加テスト（`tests/test_collect_results.py`，計画の T1〜T6 に加え回帰ケースを 1 件追加，計 7 件）**

- T1 `test_extract_rank0_messages_joins_continuation_lines_into_one_record`: プレフィックス行＋継続行の
  結合と，先頭プレフィックス前の孤立継続行が捨てられることを確認．
- T2 `test_extract_result_text_restores_multiline_body_and_strips_closing_quote`: 複数行 RESULT 本文が
  先頭行のみでなく全文で復元され，閉じ `'` が除去されることを確認．
- T2 補足（計画外の追加）`test_extract_result_text_does_not_break_on_apostrophe_inside_multiline_body`:
  応答本文中の `'`（`I'm`）で DOTALL greedy マッチが途中で切れないことの回帰テスト（計画の調査フェーズが
  懸念していた non-greedy 対案の失敗パターンを greedy 版が正しく回避できることを裏付ける）．
- T3 `test_parse_rank0_log_multiline_result_matches_without_fallback_warning`: Iteration 1 実験で実際に
  観測された複数行 RESULT（`"Hello! How can I help you today?\nthought"`）を再現し，`parse_ok is True`
  かつ `parse_warnings == []`（フォールバック警告が出ない）ことを確認．
- T4 `test_select_relevant_block_matches_both_ssh_stripped_and_http_unstripped_predict_result`: SSH 経路
  （strip 済み）・HTTP 経路（末尾改行未 strip）の両方の `predict_result` で同一ブロックに警告無く一致
  することを確認．
- T5 `test_select_relevant_block_picks_earlier_block_when_correct_block_is_not_latest`: 正しいブロックが
  最新でない順序で並んでいても取り違えず選択され，`used an earlier block` 警告が出ることを確認．
- T6 `test_select_relevant_block_empty_snippet_guard_does_not_vacuously_match_latest`: RESULT が空文字の
  ブロックが latest 位置にあっても，空スニペットガードにより vacuous match（誤って latest を採用）が
  起きず，正しい過去ブロックが選ばれることを確認．

すべて既存フィクスチャ（`tests/fixtures/rank0_sample.log`，非改変）に頼らず，複数行ケースはテストモジュール
内のインライン文字列で与えた．新規 `.log` フィクスチャファイルは作成していない．

**テスト結果**

```
uv run pytest tests/test_collect_results.py -v
============================== 30 passed in 0.08s ==============================
```

既存 23 件は全て pass のまま，新規 7 件（成功条件の「最低 6 件」を超過）も pass．failed/error 0．
T3 で `parse_warnings == []` を確認し，T5 で正しいブロックが選択されることを確認した（成功条件 2・3 を充足）．
lint/型チェッカーはこのリポジトリに未導入（Iteration 1 で確認済み，変更無し）のため，
`uv run python -m py_compile tools/collect_results.py tests/test_collect_results.py` で構文健全性を確認した
（エラー無し）．

**スコープ確認**

`git diff --name-only` で変更ファイルが `tools/collect_results.py` と `tests/test_collect_results.py` の
2 ファイルのみであることを確認した（`pipeline_inference.py` / `tools/predict.py` / `tools/common.py` /
`mise.toml` / JSONL スキーマは非改変）．`git status` に新規 `.log` ファイルが現れないことも確認した
（`.gitignore` の `*.log` トラップ回避，計画どおり）．

**計画からの差異（理由付き）**

- 計画で列挙された T1〜T6（最低 6 件）に加え，T2 の回帰ケース（応答本文中の `'` を含むケースで
  DOTALL greedy マッチが途中で切れないことの確認）を 1 件追加した．計画の調査フェーズが「non-greedy
  対案は `I'm` 等の `'` で途中で切れるため不採用」と述べていた判断の妥当性を，実装した greedy 版が
  正しく回避できることを示す形で裏付けるため，回帰の再発防止として追加した．他は計画どおりで差異は無い．
- 既存テストの `_select_relevant_block` 関連 2 件（`falls_back_to_latest_when_no_block_matches` /
  `prefers_matching_block_over_incomplete_latest_block`）は，`==`→`startswith` 化後も無改変のまま pass
  することを確認した（"not-matching-anything" は "foo"/"bar" のいずれとも前方一致しないためフォールバック
  維持，"Hello"は"Hello"と前方一致するため一致維持）．

**次フェーズ（実験・分析）への申し送り**

- 本イテレーションはコード実装・単体テストのみで完結し，実機実行は不要（計画フェーズの判断どおり）．
  実機で「フォールバック警告が実際に消える」ことの最終確認は，backlog B1 の合意に基づき，
  次回②（レバー掃引）の最初の承認済み実 run に畳み込んで行えばよい．
- `docker logs` に将来 `-t`（タイムスタンプ）を追加する場合，継続行にも時刻プレフィックスが付き
  「継続行結合」方式の前提（継続行はプレフィックス無し）が崩れるため，別途対応が必要になる点を
  引き続き申し送る（調査フェーズの指摘を再掲）．

---

### 計画 (Iter2)

**担当**: 計画フェーズ subagent（2026-07-18）．単一レバー「RESULT 複数行対応による照合ロジックの頑健化」
（backlog B3，`tools/collect_results.py` のみ改変・`pipeline_inference.py`/`predict.py` 非改変）を，調査フェーズの
結論（本ブロック直下 `### 調査 (Iter2)`）と実コードに照らして実装手順・追加テスト・成功条件に落とし込んだ．
本イテレーションは**コード実装・単体テストのみ**で，実機クラスタへの deploy/推論実行は行わない．

#### 1. 仮説

`collect_results.py` のパース／弁別ロジックを 3 点一体で複数行 RESULT 対応にすれば，Iter1 実験で観測された
フォールバック警告（`no block's RESULT text matched the predict result prefix; used the latest block as a fallback`）が
生じる入力に対して**フォールバックに落ちず正しいブロックを選び**（`parse_warnings == []`），②（レバー掃引で複数 run が
同一コンテナに連続する）で別 run 指標を誤レバーに紐付けるリスクを解消できる．修正は純関数に閉じるため単体テストで
完了条件を組める．

#### 2. 単一レバー・変更内容（`tools/collect_results.py` のみ・3 点一体）

**変更ファイル**: `tools/collect_results.py`（本体），`tests/test_collect_results.py`（テスト追加）．
`pipeline_inference.py`・`tools/predict.py`・`tools/common.py`・`mise.toml`・JSONL スキーマは**非改変**．

**(i) `_extract_rank0_messages`（現 `:58-67`）を「継続行結合」方式へ置換（関数シグネチャ不変 `(log_text: str) -> list[str]`）**

- 現状: `splitlines()` 各行を `_RANK0_LINE_RE = ^\[R0 \w+\] (.*)$`（`:46`）で照合し，プレフィックス行のみ残す
  → 複数行 RESULT の継続行（プレフィックス無し）を全て捨てるのが根源．
- 変更: **任意 rank の**行頭プレフィックスを新レコード開始とみなす正規表現を新設（案:
  `_LOG_LINE_RE = re.compile(r"^\[R(\d+) \w+\] (.*)$")`，group1=rank・group2=本文）．各物理行を ANSI 除去
  （`_ANSI_RE.sub`）後にこの正規表現へ通し，
  - マッチ＝新しい論理レコード開始．`(rank, 本文)` を「現在のレコード」として開始する．
  - 非マッチ＝継続行．**現在のレコードが存在する場合のみ**，その本文に `"\n" + clean_line` を連結する
    （最初のプレフィックスより前に現れる行や，現在レコードが無い状態の継続行は捨てる）．
  次のプレフィックス行が来たら現在レコードを確定し新レコードを開始する．最終行まで走査後に確定．
  最後に **rank==0 の論理レコードの本文のみ**を順序保持で `list[str]` として返す．
- 妥当性（調査フェーズ出典）: 本物のレコードは `_log`（`pipeline_inference.py:192`）で必ず `[R\d+ \w+]` 始まり，
  継続行はメッセージ内の生改行のみが生む．RESULT は `print` 1 回でまるごと出るため継続行は先頭行直後に連続し，
  途中に別レコードが割り込まない（方式の前提が壊れない）．フィクスチャの R1/R2 ノイズ行はプレフィックス付き＝
  独立レコード扱いで rank0 抽出時に除外される．**現状 `docker logs` に `-t` は付いていない**ため全物理行が素の本文
  （`-t` 追加時は継続行にも時刻が付き本方式が壊れるので，足すなら別対応が要る旨を申し送る）．

**(ii) `_RESULT_RE`（`:55`）／`_extract_result_text`（`:85-95`）を複数行本文へ対応（シグネチャ不変）**

- `_RESULT_RE` に **`re.DOTALL` を付与**: `re.compile(r"^Request response: '(.*)$", re.DOTALL)`．論理レコードが
  複数行（例 `Request response: 'Hello! How can I help you today?\nthought\n'`）でも group1 が閉じ `'` まで含めて
  全文を捕捉する．入力は既に (i) で 1 論理メッセージに畳まれているため，greedy `.*` が別レコードへ食い込む余地は無い
  （非 greedy 案は応答中の `'`（例 `I'm`）で誤って切れるため不採用．調査フェーズの結論どおり）．
- `_extract_result_text` の末尾 `'` 除去（`if text.endswith("'"): text = text[:-1]`）は現状のままで複数行に対応
  （閉じ `'` は論理メッセージ末尾に来るため `endswith("'")` が成立）．戻り値には末尾 `\n` が残り得るが，(iii) の
  照合で strip するため問題ない．

**(iii) `_select_relevant_block`（`:98-130`）の照合を「両辺 strip＋前方一致」へ緩和（シグネチャ不変）**

- 現状: `expected_prefix = predict_result[:100]`（改行保持）と `_extract_result_text(block)`（従来は先頭行のみ）を
  `==` 比較 → 応答先頭 100 文字に改行が含まれると必ず不一致．加えて SSH 経路（`predict.py:86` `send_prompt_ssh` は
  `result.stdout.strip()`）と HTTP 経路（`:46` 未 strip）・ログ側 `result[:100]` truncate の非対称で末尾空白差が残る．
- 変更: 照合を以下に置き換える．
  - `predict_norm = predict_result.strip()`．
  - 各 block について `snippet = _extract_result_text(block)`；`snippet` が `None` または `snippet.strip() == ""`
    ならスキップ（空スニペットは全一致してしまうので照合対象外）．
  - `snippet_norm = snippet.strip()` とし，**`predict_norm.startswith(snippet_norm)`** が真なら一致とみなす．
    ログ側スニペットは `result[:100]` の truncate 済みで predict 全文の前方部分に相当するため，**「predict 全文が
    ログスニペットで始まる」方向の前方一致**が SSH/HTTP の strip 差・100 文字 truncate 差の両方を吸収する．
  - `expected_prefix` 変数（`predict_result[:100]`）は不要になるため削除する．
  - 一致ブロックが最新でないときの warning（`used an earlier block ...`）と，全不一致時のフォールバック warning
    （`used the latest block as a fallback`）は**現状の文言・挙動を維持**する（既存テストが文言に依存するため変更しない）．

#### 3. 追加すべきテストケース（`tests/test_collect_results.py`）

既存 23 件は**すべて維持**（(i)〜(iii) は単一行入力に対し従来と同一結果になるよう設計；特に `==`→`startswith` は
等文字列で真，`falls_back` テストの `"not-matching-anything".startswith("foo"/"bar")` は偽でフォールバック維持）．
新規に最低 6 件を追加する．**フィクスチャ `.log` の gitignore トラップ（Iter1 の学び）を避けるため，複数行ケースの
入力は原則テストモジュール内のインライン複数行文字列定数で与える**（新規 `.log` フィクスチャファイルを作らない＝
`git add -f` 依存を無くす．ANSI ESC は `"\x1b[0;32m"` としてインラインで表現可能）．既存 `rank0_sample.log` は非改変で残す．

- **T1 `_extract_rank0_messages` の継続行結合**: 入力
  `"[R0 RESULT] Request response: 'Hello! How can I help you today?\nthought\n'\n"` を渡し，返る rank0 メッセージ列に
  埋め込み `\n` を保持した 1 要素 `Request response: 'Hello! How can I help you today?\nthought\n'` が含まれることを assert．
  併せて先頭プレフィックス前の行・現在レコード不在時の継続行が捨てられることも確認する．
- **T2 `_extract_result_text` の複数行復元**: 上記 RESULT を含むブロックから，先頭行のみでなく
  `Hello! How can I help you today?\nthought`（末尾 `\n` は残ってよい）まで復元し，閉じ `'` が除去されることを assert．
  応答本文に `'` を含むケース（例 `I'm fine`）でも途中で切れないことを 1 ケース入れる（DOTALL greedy の回帰）．
- **T3 先頭 100 文字に改行を含む照合（Iter1 実観測ケースの再現）**: 複数行 RESULT を持つ単一ブロックのログに対し
  `parse_rank0_log(log_text, predict_result="Hello! How can I help you today?\nthought")` を通し，
  **`parse_ok is True` かつ `parse_warnings == []`**（フォールバックに落ちない）ことを assert（現状はここで必ず
  フォールバック警告が出る＝この修正が守るべき回帰）．
- **T4 SSH（strip 済み）／HTTP（末尾 `\n` 未 strip）の両方が一致**: 同一ログに対し，`predict_result` を
  `"...thought"`（SSH 相当）と `"...thought\n"`（HTTP 相当・末尾改行付き）の両方で `_select_relevant_block` を呼び，
  どちらも一致ブロックを返し警告無しであることを assert．
- **T5 正しいブロックが最新でない順序**: 同一 since 窓に「別 run（先）＝複数行 RESULT で predict と一致」と
  「最新 run（後）＝別応答 or RESULT 未確定」が並ぶ 2 ブロック入力で，`_select_relevant_block` が**先の一致ブロック**を選び，
  `used an earlier block` 警告を返す（フォールバックに落ちない）ことを assert．②の取り違え防止が実際に働くことの検証．
- **T6 空スニペットのガード**: RESULT が空（`Request response: ''`）のブロックが並ぶとき，空スニペットで
  誤って前方一致しない（空 snippet はスキップされフォールバックへ回る）ことを assert．

#### 4. 成功条件（measurable・コードレベル）

本イテレーションは実機実行を伴わないため，成功条件はコード（テスト）で定義する（config.yml success_criteria① は
Iter1 で充足済み・本イテレーションの判定対象外）．以下すべてを満たせば「採用」候補とする．

1. `uv run pytest tests/test_collect_results.py` が **green**．既存 23 件は全て pass のまま，新規 T1〜T6（最低 6 件）も
   pass（合計 29 件以上 passed，failed/error 0）．
2. T3 が示すとおり，Iter1 実験で実際に観測された複数行 RESULT 入力に対し `parse_rank0_log(...).parse_warnings == []`
   （フォールバック警告が消える）．
3. T5 が示すとおり，一致ブロックが最新でない並びでも取り違えず一致ブロックを選ぶ（`used an earlier block` 警告）．
4. lint/型チェッカーはリポジトリ未導入のため（Iter1 で確認済み），
   `uv run python -m py_compile tools/collect_results.py tests/test_collect_results.py` で構文健全性を確認する．
5. スコープ厳守: `git diff --name-only` の変更が `tools/collect_results.py` と `tests/test_collect_results.py` の
   2 ファイルのみ（`pipeline_inference.py`/`predict.py`/`common.py`/`mise.toml`/JSONL スキーマ非改変）．

判定はすべて決定的（測定ノイズを伴わない純関数のテスト）であり，ノイズ幅の見積もりは不要．

#### 5. end-to-end 再検証の要否（判断）

- **本イテレーションの完了条件としては end-to-end 再検証は不要**．修正はパース純関数に閉じ，Iter1 実験で観測された
  生ログ形式（`[R0 RESULT] Request response: 'Hello! How can I help you today?\nthought\n'`）を T1〜T4 の回帰入力として
  忠実に再現しているため，単体テストで「フォールバックが消える」ことまで検証できる．
- **任意の確認（推奨タイミング）**: 実機で「フォールバック警告が実際に消える」ことの最終確認は，②の最初の承認済み
  実 run に**畳み込んで**行えば足りる（本修正のためだけに 51 ノードを単独起動する必要は無い）．単独で実機確認を
  行う場合は 51 ノードへの接続を伴うため**人間確認が必須**（backlog B1）．本イテレーションでは実行しない．

#### 6. `.gitignore` `*.log` トラップへの対処方針

- 新規複数行ケースは**テストモジュール内のインライン複数行文字列**で与え，新規 `.log` フィクスチャファイルを作らない
  ことを原則とする（＝`git add -f` 依存・チェックアウト再現不能リスクを最初から回避）．既存 `tests/fixtures/rank0_sample.log`
  は Iter1 で `git add -f` 追跡済みのため非改変で残す．どうしてもファイル化する場合のみ拡張子を `.log` 以外にするか
  `git add -f` すること（実装フェーズで `git status` に新規 `.log` が現れないことを確認して申し送る）．

#### 7. 実装フェーズ（rc-implementer）への申し送り

- **変更キー・箇所**: `tools/collect_results.py` の `_RESULT_RE`（`:55`，`re.DOTALL` 付与）／`_extract_rank0_messages`
  （`:58-67`，継続行結合へ置換・新設 `_LOG_LINE_RE` 使用・`_RANK0_LINE_RE` は不要になり削除可）／
  `_select_relevant_block`（`:98-130`，`==`→両辺 strip＋`predict_norm.startswith(snippet_norm)`＋空スニペットガード，
  `expected_prefix` 削除）の 3 点のみ．`_extract_result_text` の末尾 `'` 除去ロジックは維持．
- **非改変厳守**: `pipeline_inference.py`（`result[:100]` truncate・RESULT 書式）・`predict.py`（SSH strip / HTTP 未 strip の
  非対称）・`common.py`・`mise.toml`・JSONL スキーマは触らない．strip 差は「照合側で吸収」する方針．
- **既存テスト非破壊の確認観点**: `==`→`startswith` 化で `test_select_relevant_block_falls_back_...`（'foo'/'bar' が
  predict 前方一致しない）と exact-match テスト（等文字列で startswith 真・警告無し）が従来どおり通ることを実行で確認する．
- **禁止事項の再掲**: 実機への `deploy`/`predict:demo` 実行はしない（コードとテストのみ）．

---

### 調査 (Iter2)

**担当**: 調査フェーズ subagent（2026-07-18）．単一レバー「RESULT 複数行対応による照合ロジックの頑健化」
（backlog B3，`tools/collect_results.py` のみ改変・`pipeline_inference.py` 非改変）の計画に向け，
現状の単一行前提のパース／弁別ロジックと，複数行 RESULT の実際のログ出力形式を，実機に触れず読み取り調査した．

**問い**
1. `collect_results.py` の現状パース・弁別ロジックはどこで単一行前提になっているか．
2. `pipeline_inference.py` の RESULT ログ行は，複数行応答をどう物理ログへ書き出すか（改行はそのまま出るか）．
3. 現行フィクスチャに複数行 RESULT ケースは含まれるか．
4. このログ形式に最も適した複数行再構成方式は何か．

**分かったこと（コード読み取り，出典＝リポジトリ内ファイル:行）**

- **RESULT ログは改行を「そのまま」物理ログへ出す（エスケープしない）**．`pipeline_inference.py:1814` は
  `_log("RESULT", f"Request response: '{result[:100]}'")`．`_log`（`:180-192`）は `print(f"[R{_RANK} {tag}] {msg}", flush=True)`
  で出力するだけで，`result[:100]` に含まれる `\n` はエスケープされず生の改行として書かれる．結果，応答が複数行だと
  ログは次のように**先頭行だけが `[R0 RESULT]` プレフィックスを持ち，継続行（`thought` や閉じ `'`）はプレフィックス無し**になる（実験フェーズの生ログと一致）:
  ```
  [R0 RESULT] Request response: 'Hello! How can I help you today?
  thought
  '
  ```
  なお本文は `result[:100]`（先頭 100 文字に truncate）である点も重要（後述の照合で効く）．

- **単一行前提の箇所は 3 つ**（すべて `collect_results.py`）:
  1. `_extract_rank0_messages`（`:58-67`）: `log_text.splitlines()` した各物理行を `_RANK0_LINE_RE = ^\[R0 \w+\] (.*)$`
     （`:46`）で照合し，プレフィックスを持つ行だけ残す．→ **複数行 RESULT の継続行（プレフィックス無し）は
     この時点で全て捨てられる**．これが単一行前提の根源．
  2. `_RESULT_RE = ^Request response: '(.*)$`（`:55`）と `_extract_result_text`（`:85-95`）: `.` は改行に一致せず，
     かつ入力は既に 1 物理行に分解済みなので，**復元できるのは RESULT の先頭物理行のみ**．末尾 `'` の除去も先頭行が
     `'` で終わる場合しか働かない（複数行では閉じ `'` は最終行にあり除去対象にならない）．
  3. `_select_relevant_block`（`:98-130`）の防御的照合: `expected_prefix = predict_result[:100]`（改行を保持）と
     `_extract_result_text(block)`（先頭行のみ・改行喪失）を `==` 比較．→ **応答の先頭 100 文字に改行が含まれると
     必ず不一致**になり，フォールバック（最新ブロック採用）に落ちる．弁別機構が事実上無効化される（backlog B3 の指摘どおり）．

- **照合失敗の原因は「複数行」だけではない．predict 側の正規化差も効く**（新規発見，計画に必須）:
  `send_prompt_ssh`（`predict.py:86`）は `result.stdout.strip()` を返す＝**前後空白（末尾 `\n` 含む）を除去**する．
  一方 `send_prompt_http`（`predict.py:46`）は `result.get("result","")` で**未除去**．さらにログ側は raw な `result[:100]`．
  実験フェーズの `result_text="...thought"`（末尾 `\n` 無し）とログの `'...thought\n'`（末尾 `\n` 有り）の食い違いは
  この strip 差に由来する．→ **複数行を正しく再構成しても，SSH 経路では末尾空白差で `==` が依然失敗し得る**．
  照合は両辺を正規化（`strip`）し，かつ log 本文は truncate されているので「predict 側が log スニペットで startswith」
  という**前方一致方向**で判定するのが安全（SSH/HTTP 差・100 文字 truncate 差の両方を吸収できる）．

- **フィクスチャに複数行 RESULT ケースは無い**（`tests/fixtures/rank0_sample.log`）．2 ブロックとも RESULT は単一行
  （`'Hello'`／`'Hi there! How can I help you today?'`）．ANSI 混入行・他 rank（R1/R2）ノイズ行は含むが，
  **複数行応答・照合失敗・前後空白差のいずれも現テストは検証していない**（＝今回の修正は回帰テストで守られていない）．

- **推奨する複数行再構成方式＝「継続行結合（次の `[R\d+ \w+]` プレフィックス行までを 1 論理メッセージとみなす）」**．
  ログ集約ツールの標準的なマルチライン方式（Filebeat の start パターン＋`negate:true`/`match:after`，Fluentd multiline
  parser。出典: elastic.co "Manage multiline messages", docs.fluentd.org "multiline"）と同型で，本ログ形式に最も適する:
  - 全ての本物のレコードは `_log` により必ず `[R\d+ \w+]` で始まる（`:192`）．継続行はメッセージ内の生改行のみが生む．
  - `print` は RESULT メッセージ（埋め込み改行込み）を 1 回の呼び出しで出力するため，**継続行は先頭行の直後に連続する**
    （途中に別レコードが割り込む余地は無い）＝方式の前提が壊れない．
  - 対案の「DOTALL で `Request response: '(.*?)'` を non-greedy マッチ」は，応答本文に `'`（例: `I'm`）が含まれると
    途中で切れるため**非推奨**．終端マーカー方式も閉じ `'` が曖昧なため不可．
  - 実装上の注意: 継続行の帰属を正しく決めるため，境界判定は `[R0 ...]` だけでなく**任意の `[R\d+ \w+]`** を「新レコード開始」
    として扱い，rank を付与して論理レコードへ畳んでから rank0 だけを残す（rank0 コンテナのログには実際上 rank0 行しか
    出ないが，フィクスチャの R1/R2 ノイズ耐性のため）．また `collect_rank0_log`（`:439-444`）は現状 `docker logs --since`
    のみで **`-t`（タイムスタンプ）は付けていない**ため全物理行が素の本文であり本方式が成立する（将来 `-t` を足すと
    継続行にも時刻プレフィックスが付き方式が壊れるので，足すなら別途対応が要る点を申し送る）．

**次フェーズ（計画）への示唆**

- **修正の骨子（3 点セットで一体）**: (i) `_extract_rank0_messages` を「継続行結合」方式に置き換え，rank0 の各論理
  メッセージを `\n` 連結で復元する；(ii) `_RESULT_RE`/`_extract_result_text` を複数行本文に対応（`re.DOTALL`
  相当 or 行に依存しない末尾 `'` 除去）；(iii) `_select_relevant_block` の照合を**両辺 strip＋前方一致（predict が
  log スニペットで startswith）**へ緩め，SSH/HTTP の strip 差と 100 文字 truncate 差を吸収する．この 3 つは連動して
  はじめて弁別機構が復活するため，1 つでも欠けると②で取り違えリスクが残る．
- **回帰テストの追加が完了条件の中核**: フィクスチャ（または新規フィクスチャ）に「複数行 RESULT を含むブロック」
  「先頭 100 文字に改行を含む応答」「SSH 経路想定で末尾 `\n` を strip した predict_result」「同一 since 窓内に
  複数ブロックが並び，正ブロックが最新でない」ケースを足し，`_select_relevant_block` が**フォールバックに落ちず
  正しいブロックを選ぶ**ことを assert する（現行 23 件はこれを検証しない）．`.gitignore` の `*.log` がフィクスチャを
  飲む落とし穴（Iter1 の学び）に注意し，フィクスチャ拡張子を `.log` 以外にするか `git add -f` する．
- **スコープ厳守**: 改変は `collect_results.py`（と tests/fixtures）のみ．`pipeline_inference.py`（`result[:100]` の
  truncate や RESULT ログ書式）・`predict.py`（strip 差）は非改変（backlog B3）．strip 差は「照合側で吸収」する方針で，
  ログ側・送信側の書式は触らない．
- **end-to-end 検証は原則不要**: 本修正はパース純関数に閉じるため単体テストで完了条件を組める．実機叩き（人間確認要）は
  任意．なお②着手前に本修正が入れば，②の複数 run 連続送信でも弁別が機能する（B3 の目的を満たす）．

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
