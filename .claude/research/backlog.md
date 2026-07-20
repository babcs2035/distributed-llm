# backlog: distributed-llm — 人間判断待ち事項 / 自動判断の記録

新しいものを常に先頭に追記する（逆時系列）．
- 可逆な暫定判断: `## B{n} [auto-decided YYYY-MM-DD] 題目`（状況・自動選択・根拠・要レビュー）
- 不可逆・危険な事項: `## B{n} [needs-human YYYY-MM-DD] 題目`（Slack で @mention 済みと明記）

---

## B17 [auto-decided 2026-07-20] Iteration 10 の単一レバー選定（全 51 ノードで単層 local マイクロベンチを回し straggler ノードを特定）
- **状況**: Iteration 9（bench 経路への 3 区間 per-microbatch 計時ログ追加）を **採用（診断として成功）・収束**で確定した．
  全 51 rank×3 区間の分解で，直列化点＝律速ボトルネック段を **rank14（物理ノード wafl113，層 23，1 層割当）に一意特定**
  （基準 (i)：compute/per-step=0.758 ≥ 0.60 が閾値で成立，3 repeat CV<0.1%，約 55σ の外れ値）．2 番手 straggler は
  rank37（wafl136，層 46，2.30 倍）．Iter7 の見かけの完全直列は「通信構造でも全層 compute でもなく，単段 straggler による
  負荷不均衡が主因」と切り分けた．**残る未確定点は「rank14/wafl113（rank37/wafl136）が遅い理由がノード起因（straggler）か
  層 23/46 の構造的な重さか」の二択**（層→rank 割当が固定のため交絡）で，analyst は追加反復 1 回で決着可能と示唆した．
- **自動選択**: Iteration 10 の単一レバーを **「全 51 ノードで単層 local マイクロベンチ（SL1 型・通信なし）を回し，各物理
  ノードの単層 compute 時間を直接ランキングして wafl113/wafl136 が突出するか確認する」**（analyst 示唆 (a)）とする．
  全ノードで**同一の単層ワークロード（層を固定）**を走らせるため，差が出ればノード起因と直接帰属でき，wafl113/wafl136 が
  突出しなければ層起因（層 23/46 が重い）と切り分けられる＝二択を一度で決着させる．具体的なベンチ設計（測定層の選定・
  ウォームアップ・反復数・全ノード並列 SSH の収集経路）は次の rc-planner が決める．state は `phase="investigate"`・
  `current_lever=null` で開始．**iteration_name**: 「全 51 ノードの単層 local マイクロベンチによる straggler ノード特定
  （ノード起因 vs 層起因の切り分け）」．
- **根拠**: (1) 未確定点が「ノード起因か層起因か」の二択に集約されており，(a) はそれを一度で決着でき最も情報利得が高い．
  (2) 通信を伴わない SL1 型 local マイクロベンチ（Iter5/B8 の系譜）で完全に可逆・低リスク．対立案 (b)（層→rank 割当の
  シャッフルで遅さがノード/層どちらに追従するか見る）は deploy 側の割当変更を伴い実装規模がやや大きく，同じ問いに (a) より
  重いため見送る．(3) 過去の一貫方針（B8「大改修の前に near-zero コストの local マイクロベンチで先に測る」，Iter8「棄却の
  前に一次証拠を取る」）と整合し，可逆・低リスクで情報利得の高い方を優先する．
- **可逆性**: 次に振るレバーの選定であり可逆．local 単層マイクロベンチで serving/relay ロジックも層割当も変更せず，コード
  変更は計測スクリプト追加のみ．51 ノードへの実行は SSH を伴うが非破壊（B7 の包括承認範囲内で破壊的操作なし＝自動判断とした）．
- **要レビュー / 要人間判断**: (a) 本軸は local 単層計測で relay プロトコル（B9/SL3）には一切触れず軸が直交・実装衝突なし．
  **B9 は今回も温存（`[needs-human]` 維持，reflector では自動判定しない）**．(b) フォールバック: (a) の実装が予想外に過大と
  判明した場合は (b)（層割当シャッフル）へ振り替える．config `levers`（`STAGGER_INTERVAL`/`SEQ_LEN`/`WORLD_SIZE`）は下位
  フォールバックとして温存（B14(a)）．(c) 処方箋への含意——(a) で straggler 起因と確定すれば負荷分散（遅ノードへ層を減らす／
  WORLD_SIZE 調整）へ，層起因と確定すれば当該層の compute 最適化（量子化・attention 実装）へ向かうが，いずれも (a) の結果を
  見てから改めて単一レバーを立てる．async ホットパス大改修（B14(b)）は不可逆・大規模のため妥当と判明するまで着手せず，その
  時点で `[needs-human]` 登録＋Slack 確認．(d) 任意の将来課題（診断品質の向上，本軸の必須ではない）: Iter9 の整合チェック
  77% の残差 23% は measure ループの per-step 定数オーバーヘッドの計測被覆漏れで，measure ループ全体を t_step で挟む 4 点目の
  計時を足せば残差を per-step overhead 区間として明示回収できる（診断結論には影響しないため優先度は低い）．

---

## B16 [auto-decided 2026-07-20] `.gitignore` 例外追加によるテストフィクスチャの追跡漏れ修正（Iter7 由来の既存欠陥）
- **状況**: Iteration 9 の実装フェーズで `tests/fixtures/microbatch_bench_timing_sample.log` を新規追加したところ，
  `.gitignore:35` の `*.log` にマッチして `git status` にすら現れない（untracked のまま無視される）ことが判明．
  さらに調査で，**Iter7（コミット b6d5a31）で追加された `tests/test_microbatch_bench.py` が依存する既存フィクスチャ
  `tests/fixtures/microbatch_bench_sample.log` も同じ理由で一度も git 管理下に入っていなかった**ことを確認した
  （`git log --all -- tests/fixtures/microbatch_bench_sample.log` が空）．これは Iter9 の単一レバーとは無関係な
  Iter7 由来の既存欠陥であり，クリーンチェックアウトでは `test_microbatch_bench.py` がフィクスチャ欠如で失敗する
  状態が Iter7 完了時点から潜在していたことになる．
- **自動選択**: `.gitignore` に `!tests/fixtures/*.log` の例外パターンを追加し，`microbatch_bench_sample.log`
  （Iter7 分）と `microbatch_bench_timing_sample.log`（Iter9 分）の両方を `git add -f` で追跡対象にする．
  `tests/fixtures/rank0_sample.log` は元々追跡済み（`.gitignore` の `*.log` 追加以前に commit 済みだったと推測）
  で影響なし．
- **根拠**: (1) 今回追加したテストがコミット後にクリーンチェックアウトで動作するための前提条件であり，Iter9 の
  「実装完了」の実質的な要件（テスト追加が意味を持つこと）に直結する．(2) `.gitignore` への例外追加はテスト
  フィクスチャという限定的なパスのみを対象とし，本来の意図（ビルド成果物・実行時ログの除外）を損なわない．
  (3) 変更は可逆（`.gitignore` の1行と `git rm --cached` で戻せる）．(4) Iter7 分の欠陥も同じコミットで拾って
  修正する方が，同種の問題を二度に分けて対処するより一貫している．
- **可逆性**: 可逆．`.gitignore` パターン追加とファイルの追跡開始のみで，本体ロジック（pipeline_inference.py 等）
  には触れていない．
- **要レビュー / 要人間判断**: なし．リポジトリ構成ファイル（`.gitignore`）の変更だが，除外パターンの微修正
  であり public API・スキーマ・設定ファイルの意味的変更ではないため，CLAUDE.md の「事前確認が必要な変更」には
  該当しないと判断した．念のため次回の journal/Slack 報告でこの判断自体も明示する．

---

## B15 [auto-decided 2026-07-20] Iteration 9 の方向選定（実機 bench への per-microbatch timing ログ追加で直列化点を特定）
- **状況**: Iteration 8（`pipeline_fill_microbench` ローカル Gloo 診断）を **採用（診断として結論確定）・収束**で確定．
  Decision1（blocking×sleep, N=16,M=32,repeat=5）で **FF=0.9716（≥0.7 の (1b)，閾値まで約 129σ）**．結論は
  「blocking `recv→compute→send` 構造は段が真並列なら本来ほぼ完全に fill する」＝**Iter7 実機の `time_per_step ∝ m`
  （ほぼ完全直列，含意 FF は m=51 で 0.038・m=204 で 0.024＝1/p にほぼ張り付く）は blocking 通信構造では説明できず，
  別の（大域的な）ハードな同期点由来**と切り分けた（Iter7 §2-i「blocking だから不 fill」の修正）．補足の matmul proxy
  （FF 0.30）はレジームが違いすぎ真因の説明にはならない（F3 の傍証止まり）．async 二重バッファ大改修（B14(b)/F2 overlap
  軸）の事前確度は低下したが，本ローカル bench は実機の同期点そのものを特定できない（計画 §6 の射程外）．
- **自動選択**: Iteration 9 の単一レバーを **「実機 51 ノード bench 経路（`_run_microbatch_bench`/`_process_microbatch`）への
  per-microbatch timing ログ追加による直列化点の特定」**とする（analyst 示唆 (A)）．Iter7 のほぼ完全直列を生む実際の
  同期点（rank0 の microbatch 生成直列・`_reset_kv_cache_for_bench` 同期・barrier 等の候補）を，実測 timing で確定する．
  SL1/SL2/pipeline_fill_microbench と同じ「作る前に測る／攻める前に直す点を特定する」診断系譜．state は
  `phase="investigate"`・`current_lever=null` で開始．
- **根拠**: (1) 未解決点が「実機の直列化点はどこか」の一点に集約されており，(A) はそれを直接特定でき最も情報利得が高い．
  (2) 追加するのは bench 経路への**加算的な計測 INFO ログ**で serving/relay ロジックも計算結果も変えない（Iter3/P1 の
  per-request INFO ログ追加と同種，graph-break リスク低・可逆）．(3) 誤った処方箋（async 大改修＝B14(b)）へ大投資する前に
  真因を一次証拠で押さえる順序が「作る前に測る」系譜と整合．(4) 調査・計画・実装はコードのみ・実機非接続で可逆，
  測定の deploy/predict は B7 の包括承認（非破壊 SSH/deploy）の範囲内で破壊的操作なし．
- **可逆性**: 次に振るレバーの選定であり可逆．コード変更は加算的計測ログのみ・実機 deploy は B7 承認範囲内で破壊的操作なし
  （自動判断とした）．
- **要レビュー / 要人間判断**: (a) 本軸は bench 経路への読み取り専用計測で **relay プロトコル（B9/SL3）には一切触れず
  軸が直交・実装衝突なし**．**B9 は今回も温存（`[needs-human]` 維持，reflector では自動判定しない）**．(b) フォールバック:
  (A) の実装が過大（bench 経路の計測が予想外に serving 経路へ波及する等）と判明した場合は，示唆 (B)「重み int8 dynamic
  quantization を SL1 型 local マイクロベンチで作る前に測る」（compute 92% を直接攻める・可逆）へ振り替える．config
  `levers`（`STAGGER_INTERVAL`/`SEQ_LEN`/`WORLD_SIZE`）はさらに下位のフォールバックとして温存（B14(a)）．(c) async
  ホットパス大改修（B14(b)）は，(A) で直列化点が判明し，かつ async で解消可能と分かるまで着手しない（不可逆・大規模の
  ため，着手が妥当と判明した時点で改めて `[needs-human]` 登録＋Slack 確認）．なお journal（計画 §7）で B15 は Decision2a
  到達時の async 大改修 needs-human 用に条件付きで言及されていたが，Decision1=(1b) により Decision2 は実行されず当該
  条件は成立しなかったため，B15 は本 Iteration 9 方向選定へ充てた（採番の重複なし）．

---

## B14 [auto-decided 2026-07-20] Iteration 8 の方向選定（research_frontier⑤: 通信・計算オーバーラップを起点とする高速化調査）
- **状況**: Iteration 7（`NUM_MICRO_BATCHES` のスループット感度，research_frontier②）を**不採用（仮説棄却）・現実装で
  収束**と確定．実測は m=8→51 で 1.12 倍（2.8478→3.1772→3.2102 microbatch/s）で採用閾値 1.5 に遠く未達，かつ微増は
  バブル低減ではなく固定オーバーヘッド償却の副作用（機構が仮説と異なる＝実質的な反証）．最重要の学びは
  **「本パイプラインの bench 経路には段間の通信・計算オーバーラップが構造的に存在しない」**（blocking Gloo・逐次
  マイクロバッチループ・二重バッファ無し，`time_per_step ≈ 0.31×m + 0.35` の逐次型が適合，バブル式は不成立）．
- **自動選択**: Iteration 8 を **research_frontier⑤（先行研究調査に基づく推論パイプライン高速化）**とし，Iter7 の上記
  発見を具体的な起点として着手する．調査フェーズ（rc-investigator）が「通信・計算オーバーラップ（async `isend`/`irecv`・
  二重バッファ・GPipe 型スケジューリングの CPU/Gloo 上での有効性）」を主軸に，KV キャッシュ最適化・量子化・continuous
  batching 等の⑤候補も併せて文献調査し，計画フェーズが単一レバー原則で 1 つの具体案へ絞り込む．state は
  `phase="investigate"`・`current_lever=null` で開始．
- **根拠**: (1) Iter7 が「段間オーバーラップが無い」という具体的で行動可能な発見を残しており，これは⑤の中核候補
  （通信・計算オーバーラップ）を直接指す．(2) analyst 推奨の次 config レバー `STAGGER_INTERVAL` は起動時 thundering
  herd 回避が主目的で定常状態のスループット/レイテンシに直接効かず（Iter4 で ITL は compute 律速 92% と確定，起動時
  交絡は warm-up 除去済み），振っても Iter7 同様「効かない確認」に終わる公算が高く期待値が低い．(3) config levers の
  優先順位は目安であり，⑤はユーザーの明示指示（2026-07-18）による常設項目で「②③④と重複する場合は一本化」と規定
  ——②の結果が⑤の一軸を名指しした以上，⑤へ一本化するのが config の意図に沿う．(4) 調査・計画フェーズはコードのみ・
  実機非接続・可逆．
- **可逆性**: 次の調査方向の選定であり可逆．破壊的操作を含まない（自動判断とした）．
- **要レビュー / 要人間判断**: **重要な留保（levers 優先順位からの逸脱を含むため要レビュー）**——(a) config `levers` の
  厳密な優先順位に従うなら次は `STAGGER_INTERVAL` だが，本決定はそれを見送り⑤を優先した．`STAGGER_INTERVAL`/`SEQ_LEN`/
  `WORLD_SIZE` は levers に残置し，⑤の調査で行動可能な単一レバー案が得られない場合のフォールバック候補として温存する
  （人間が優先順位を厳守させたい場合はこの B14 を差し替え，`STAGGER_INTERVAL` へ切り替えること）．(b) ⑤の自然な帰結
  （`_process_microbatch` の async `isend`/`irecv`＋二重バッファ化）は `pipeline_inference.py` ホットパス改変を伴い
  **不可逆・大規模になりうる**．計画フェーズが実装案をそこまで踏み込ませると判明した時点で backlog へ `[needs-human]`
  登録し Slack で確認を仰ぐこと（調査・計画自体はコードのみで進めてよい）．(c) ⑤（通信オーバーラップ）は B9/SL3
  （speculative decoding の relay 改修）とは別軸（通信・計算の重なり vs トークン投機）で直交するが，同じ
  `pipeline_inference.py` を触るため，計画時に B9 との実装衝突・重複を確認すること．また Iter7 で再確認された
  `dist.recv`/`dist.send` の例外握り潰し（通信断隠蔽）は，async 通信化の際に通信断の検知・伝播が信頼性の前提になるため
  ⑤の設計時に併せて検討すべき将来課題として記録する．
- **B9 の扱い**: 今回も温存（`[needs-human]` 維持，reflector では自動判定しない）．B9 回答が得られ次第，人間がこの B14 を
  差し替えて SL3 を優先してよい（本 B14 は待ち時間を無駄にしないための直交軸の選定である）．

---

## B13 [auto-decided 2026-07-19] Iteration 7 実験フェーズ失敗への対処（bench 実装バグの修正のため実装フェーズへ差し戻し）
- **状況**: Iteration 7（NUM_MICRO_BATCHES のスループット感度）の実験フェーズが，m=8 でのパイロット実行
  （`MICROBATCH_BENCH_STEPS=5`）で 2 件のブロッキング実装バグを実機で発見した．
  **バグA（クラッシュ）**: `_process_microbatch`（`pipeline_inference.py:995` 付近）が `layer(hidden_state)` を
  `position_ids` 無しで呼び出しており，`_build_transformer_layer` の実シグネチャ（`:829`，`position_ids` に
  既定値なし）と不整合で `TypeError` fatal crash．さらに `dist.recv`/`dist.send` の例外握り潰しにより，
  下流 rank がクラッシュを検知できず「正常完了に見える rank」と「1 時間タイムアウトまでハングする rank」に
  分かれる副作用も確認．**バグB（構造的）**: bench の KV キャッシュ書き込み位置がマイクロバッチ間で共有され
  リセットされないため，本番設定（`MICROBATCH_BENCH_STEPS=100`）では総呼び出し数が `max_gen_tokens=2048` を
  超え，バグA修正後も別クラッシュに至る見込み（コードレビューで確定，実クラッシュ未確認）．
  実験フェーズはこれらを実験フェーズの役割（domain 知識を要する修正）を超えると判断し，パイロットを打ち切り，
  bench 無効で全 51 ノードを健全な serving 状態へ復元済み（`results/Iter7.jsonl` は 0 件）．
- **自動選択**: Iteration 7 の単一レバー（`NUM_MICRO_BATCHES`）は変更せず，**実装フェーズ（rc-implementer）へ
  差し戻し**，バグA（`position_ids` 明示渡し）・バグB（bench 中の KV キャッシュ書き込み位置のリセット/バイパス）
  を修正した上で，実験フェーズへ再度進む．
- **根拠**: (1) B11（Iter6 の HF キャッシュ revision 修正）の前例と同様，**同一イテレーション内の実装バグ修正**
  であり新たなレバー変更ではない（単一レバー原則に抵触しない）．(2) バグ修正自体はコード変更のみで可逆．
  (3) 実験フェーズが自律判断で `tools/deploy.py`/`tools/common.py` の env 転送漏れを先に修正・pytest 88 passed
  確認済みで，実装フェーズが着手しやすい状態に整えられている．
- **可逆性**: 実装フェーズへの差し戻しであり可逆．クラスタは既に健全な状態に復元済みで，破壊的操作は含まない
  （自動判断とした）．
- **要レビュー / 要人間判断**: バグA・Bの修正は RoPE position_ids の意味・KV キャッシュ容量設計という
  domain 知識を要する（実験フェーズ申し送り参照）．実装フェーズが「serving 経路への影響が読めない」等
  過大と判断した場合は，その時点で backlog へ `[needs-human]` 登録し Slack で確認を仰ぐこと．また，
  `dist.recv`/`dist.send` の例外握り潰し（通信断を隠蔽する設計，バグA副作用）は bench 固有の問題ではなく
  `pipeline_inference.py` 全体の設計の弱点である可能性があり，本 Iteration の直接のスコープ外だが，
  将来の research_frontier 候補として記録に値する（reflector が判断）．

---

## B12 [auto-decided 2026-07-19] Iteration 7 の単一レバー選定（NUM_MICRO_BATCHES: research_frontier② のスループット感度）
- **状況**: Iteration 6（SL2: draft 採択率のオフライン見積もり）を「採用」で確定・収束．overall α=0.5856（≳0.5）・
  a_2=1.8562（≳1.5）で計画の go 条件を両方充足し，SL1×SL2 合成の実効 compute 利得は最良 K=4 で ≈1.43 倍と数値化した．
  これで SL1（compute 天井）×SL2（採択率）が出揃い，B9（B3 本体＝relay プロトコル改修＝SL3 の go/no-go）の判断材料は
  揃ったが，B9 は**不可逆・大規模のため `[needs-human]` のまま維持**（reflector では自動判定しない．別途 Slack で mention
  済み，人間回答待ち）．次イテレーションは B9 の人間回答を待たずに自律実行できる軽量な項目を選ぶ段である．
- **自動選択**: Iteration 7 の単一レバーを **`NUM_MICRO_BATCHES`（config `levers` 最優先候補，research_frontier② の
  「マイクロバッチ数・stagger interval のスループット感度分析」の枠組みで振る）**とする．検証対象は「マイクロバッチ数を
  増やしパイプラインバブル（段間の遊び）を埋めるとスループットが上がるか」で，実機 deploy/predict で ITL/TTFT を測る．
  具体的なワークロード設計（複数リクエスト同時投入等）は次の rc-planner が決める．
- **根拠**: (1) B9 が人間判断待ちの間，reflector が自律的に選べる項目は config `research_frontier` ②③④ または `levers`
  の 4 候補（NUM_MICRO_BATCHES/STAGGER_INTERVAL/SEQ_LEN/WORLD_SIZE）に限られ，不可逆な SL3/B9 へは踏み込まない．その中で
  `NUM_MICRO_BATCHES` は `levers` の最優先（順序＝優先度）で，B6 が ②/⑤ の実験前提条件（`--iter Iter{n}` 変数化・冷開始
  交絡除去・各水準 n≥3〜5 反復・主指標 ITL/TTFT）を申し送り済みで planner が即着手できる．(2) B3 の compute 律速（B9）
  とは直交する軸で，人間回答を待つ間に研究を停滞させない．(3) 破壊的操作を含まず可逆．
- **可逆性**: 次に振るレバーの選定であり可逆．実機 deploy/predict を伴うが B7 の包括承認（非破壊 SSH/deploy）の範囲内で
  破壊的操作なし（自動判断とした）．
- **要レビュー / 要人間判断**: **重要な留保**——Iter4 で「`NUM_MICRO_BATCHES` は単一リクエストの ITL では Σcompute 不変・
  残差止まり」と確定済みのため，単発デモ（`"Hello!"` 1 件）を回すだけではスループット差は出ない．②の感度を意味あるものに
  するには planner が**複数リクエスト同時投入／連続バッチのワークロードを設計**する必要がある．この設計が過大
  （`pipeline_inference.py` ホットパス改変を要する等）と判明した場合は，SEQ_LEN（④）や STAGGER_INTERVAL へ振り替えるか
  backlog へ `[needs-human]` 登録して諮ること．また B9（SL3 go/no-go）は温存済みで，人間回答が得られ次第そちらを優先して
  よい（本 B12 は待ち時間を無駄にしないための直交レバーであり，B9 回答が来れば人間がこの B12 を差し替え可）．

---

## B11 [auto-decided 2026-07-19] Iteration 6 実験フェーズ失敗への対処（HF キャッシュ revision 固定）
- **状況**: `scripts/estimate_draft_acceptance.py` の初回実行が `AutoTokenizer.from_pretrained` 段階で即時失敗した．
  当初の懸念（`Gemma4ForConditionalGeneration` を `AutoModelForCausalLM` が非対応）ではなく，ローカル HF キャッシュ
  `~/.cache/huggingface/hub/models--google--gemma-4-31B-it/refs/main`（2026-07-19T10:41 更新）が config.json のみの
  不完全スナップショット（`b9ea41a2...`）を指しており，59GB weights・tokenizer.json を含む完全なスナップショット
  （`fb9ae262...`）が別途存在するのに参照されていなかったことが原因．診断（revision 明示指定での読み込み）では
  `AutoConfig`/`AutoTokenizer` とも成功し，architectures 非対応の懸念は解消済み．
- **自動選択**: 対処案 (A)（スクリプト内の `from_pretrained` 呼び出しに `revision="fb9ae262347c3945692f09a612f8bb189def854f"`
  を明示指定）を選ぶ．対処案 (B)（ローカル HF キャッシュの `refs/main` を書き換えて修復）は不採用．
- **根拠**: (A) はリポジトリ内のコード変更のみで完結し，git 管理下・完全に可逆．(B) は共有 HF キャッシュ（他プロセスが
  参照している可能性あり）への書き込みを伴い，本スクリプト以外への影響範囲が不明なため，可逆性が (A) より劣る．
  同一レバー（SL2）内の実装バグ修正であり，単一レバー原則には抵触しない（新たなレバー変更ではない）．
- **要レビュー**: revision ハッシュをコードにハードコードする方式は，HF キャッシュが将来更新された場合に追随できない
  暫定対応である．恒久対応（例: 最新の完全スナップショットを動的に解決する）は本イテレーションのスコープ外とし，
  必要なら次イテレーション以降の backlog へ改めて起票する．

---

## B10 [auto-decided 2026-07-19] Iteration 6 の単一レバー選定（SL2: draft 採択率のオフライン見積もり）
- **状況**: Iteration 5（SL1: compute 側上限の local マイクロベンチ）を「採用」で確定・収束．実機（i5-8350U，`wafl100`＝rank1，
  cpuset 0-3）で ratio_2=0.753／ratio_4=0.378／ratio_8=0.213，判定「利得あり」＝B3 の compute 側効き源が実在することを確認し，
  「compute 側利得ゼロなら大投資は無駄」というダウンサイドリスクを棄却した．ただし SL1 が測るのは計算効率（上限側）のみで，
  B3 の実運用速度向上は draft 採択率にも依存し，期待値側は未計測のまま残る．
- **自動選択**: Iteration 6 の単一レバーを **SL2（draft 採択率のオフライン見積もり）**とする．検証対象は「K トークン提案の
  うち検証で受理される割合（＝毎回 K を捨てずに済む割合）」で，relay 改修せず・prompt-lookup／n-gram draft または小 draft
  モデルで既存ログ／参照出力に対して見積もる．SL1（compute 天井）と SL2（採択率）が揃えば B3 の実効利得の期待値レンジを
  初めて数値で括れ，B9 go/no-go の質が上がる．具体的な実装方針（draft 戦略・参照データの取り方）は次の rc-planner が決める．
- **根拠**: (1) B3 の残る不確実性が「採択率（期待値側）」の一点に集約されており，SL2 はそれを relay 改修せず安価に潰せる．
  (2) SL1 と同じ「作る前に測る」診断系譜で単一レバー原則に整合する．(3) 通常はオフライン生成／静的解析で完結する可逆な作業．
- **可逆性**: 次に振るレバーの選定であり可逆．採択率のオフライン見積もりは実クラスタへの deploy／relay 改修を伴わず，
  参照出力の取得に実機推論を要する場合も B7 の包括承認範囲内の非破壊 SSH で対応可（破壊的操作なし＝自動判断とした）．
- **要レビュー / 要人間判断**: SL2 自体は自律実行可．ただし **計画フェーズで SL2 の実装がクラスタ本体（`pipeline_inference.py`
  ホットパス改変や 51 ノード再デプロイ）への大きな変更を要すると判明した場合は，その時点で backlog へ `[needs-human]` として
  登録し Slack で確認を仰ぐこと**．また SL2 の先にある B3 本体（SL3: relay プロトコル改修）は B9 として温存済みで，
  SL1×SL2 の期待値レンジが揃った Iteration 6 完了後に改めて人間 go/no-go を諮る．

---

## B9 [needs-human 2026-07-19] B3 本体（speculative decoding の relay プロトコル改修＝SL3）着手の go/no-go
- **状況**: Iteration 4（B0）で「ITL≈7s/token は計算律速（compute 92%・send 0.3%・residual 7.6%）」が確定．支配項
  （92% の compute＝1 トークンずつ 50 段を逐次通過する CPU 計算）を攻められるのは B3（speculative decoding）のみで，
  B1（WORLD_SIZE 絞り込み，Σcompute 不変）・B2（診断ログ削減，compute dt 非含）はいずれも残差止まりと確定した．
- **論点**: B3 本体の実レイテンシ低減には，**どの draft 戦略でも relay プロトコルの改修（K トークン運搬＋検証を 1 往復で
  行う）＝SL3 が避けられない**．これは `pipeline_inference.py` ホットパスの改変・検証木の実装・51 ノードへの再デプロイを
  伴う**大規模かつ不可逆側**の変更であり，research-cycle の自律判断ポリシー（不可逆/大規模は人間判断）に該当する．
- **方針**: Iteration 5 では B3 本体に直行せず，最小サブレバー **SL1（compute 側上限の local マイクロベンチ，B8）**で
  「B3 の compute 側利得がこの CPU（i5-8350U・4 コア・float32）で実在するか」を先に測る．**SL1 の結果（GEMM(K) の
  1 トークンあたりコストが GEMV に対しどれだけ縮むか）を添えて，B3 本体（SL3）着手の go/no-go を人間に諮る**．
  SL1 で compute 側利得が実在しないと判明すれば，B3 の天井は残差償却（≈1.08 倍）に縮み，大投資は見送る判断もあり得る．
- **現在の扱い**: Iteration 5 は path (a)（十分小さい SL1 を自動選定）で `status="running"` 進行．B3 本体は本 B9 として
  温存し，SL1 完了後の考察・次計画フェーズで Slack（`<@U08GLKY1QCW>`）へ go/no-go を諮る．今回の Iteration 4 完了
  サマリー Slack でも，B3 本体は SL1 の結果を見て別途諮る旨を予告済み．

---

## B8 [auto-decided 2026-07-19] Iteration 5 の単一レバー選定（B3 を最小サブレバーへ分解した SL1: compute 側上限の local マイクロベンチ）
- **状況**: Iteration 4（B0: per-stage 内訳診断）を「採用」で確定・収束．「7s/token は計算律速（compute≈92%）」が確定し，
  「計算 vs 通信の弁別」という診断課題は完了した．次は支配項（92% の compute）そのものを攻める段だが，それに効く唯一の
  候補 B3（speculative decoding）本体は実装規模が大きく（draft モデル・relay プロトコル改修・検証木・再デプロイ），
  単一レバー原則に対して過大で不可逆側でもある．
- **自動選択**: Iteration 5 の単一レバーを **SL1（compute 側上限の local マイクロベンチ）**とする．本モデルの実次元
  （`hidden_size=5376`，ノードあたり 1〜2 層相当の GEMM 形状）で `torch.set_num_threads(4)`・float32 のもと，
  seq_len=1（GEMV）と seq_len=K（K=2,4,8 の GEMM）の **1 トークンあたり実行時間**を比較し，「K 位置まとめた GEMM が
  この CPU で演算強度/キャッシュ効率を上げるか（＝B3 の compute 側効き源 (ii) が実在するか）」を測る．重み不要の
  ランダムテンソルで足り，`pipeline_inference.py` 非改変・再デプロイ不要・クラスタ本体非接触・完全に可逆．
- **根拠**: (1) B3 の最大の不確実性（compute 側利得の実在有無）を near-zero コストで潰せ，大規模な relay 改修（SL3, B9）の
  go/no-go の決定的入力になる．(2) B0 と同じ「作る前に測る」診断の系譜で，収束した診断レバー群の延長として単一レバー
  原則に整合する．(3) 破壊的操作を含まず完全に可逆．
- **可逆性**: 次に振るレバーの選定であり可逆．local マイクロベンチのみで破壊的操作なし（自動判断とした）．
- **要レビュー / 要人間判断**: SL1 自体は自律実行可．ただし SL1 の先にある **B3 本体（SL3: relay プロトコル改修・再デプロイ）は
  不可逆・大規模のため B9 として人間 go/no-go を要する**．SL1 を計測用スクリプトの独立イテレーションとして扱うのが
  過剰と判断する場合（別案: B3 本体へ直行して人間確認）は，次回 continue 時に人間がこの B8 を差し替えること．

---

## B7 [resolved 2026-07-19] Iter4 フェーズ4（実験，B0の51ノード実機`--stage-timing`測定run）開始可否の確認依頼（B1の実行）
- **解決**: ユーザーが承認．「ノード・デバイスへの ssh 接続を伴うデプロイなどは全て許可する」との明示的な指示を受け，
  フェーズ4（実験）へ進行する．
- **状況**: Iteration 4（B0: per-stage compute/recv dt の内訳記録）のフェーズ1〜3（調査・計画・実装）が完了．
  `tools/collect_results.py` に `--stage-timing` オプションを追加し，rank1以降のworkerへ並列SSHで`docker logs`を
  取得・パースして`stage_timing`/`timing_breakdown`をJSONLへ記録できるようにした．単体テスト45件green（既存38件＋
  新規7件，回帰無し）．`pipeline_inference.py`は非改変・**再デプロイ不要**（既存稼働イメージのログのみを利用）．
- **論点**: フェーズ4は`pipeline_inference.py`の改変を伴わないが，**51ノード全てへSSHで`docker logs`を並列取得する**
  実機接続を伴う．B1の合意（実機への deploy/推論実行を伴う実験の前に必ずSlack確認）の対象に含めるべきと判断し，
  着手前に一度停止してSlackで`<@U08GLKY1QCW>`へ確認を依頼した（2026-07-19投稿）．
- **今後の運用への影響（重要）**: ユーザーの「ssh 接続を伴うデプロイなどは全て許可する」という指示は，本イテレーション限りの
  承認ではなく，このリポジトリの research-cycle 運用における**SSH接続・deploy・推論実行に対する事前確認要件（B1由来）への
  包括的な事前承認**と解釈する．したがって以降のイテレーションで `mise run deploy`／`mise run predict:demo`／
  `--stage-timing` 等の**SSH接続を伴う非破壊的な実機操作**は，都度Slack確認を挟まずオーケストレータの自律判断で進めてよい．
  ただし **`mise clean --full`（イメージ/コンテナ削除）・`git push --force`等の破壊的操作は本承認の対象外**であり，
  引き続き人間確認が必須．詳細はメモリ `feedback_research_cycle_ssh_deploy_authorized` を参照．

---

## B6 [auto-decided 2026-07-18] Iteration 4 の単一レバー選定（ユーザー指示⑤: 先行研究調査に基づく推論パイプライン高速化）
- **状況**: Iteration 3（P1: levers 記録の堅牢化）を「採用」で確定・収束．②着手前に事前イテレーションで潰すべき
  基盤頑健化 (a)RESULT 複数行照合・(b)levers 記録堅牢化は Iter2・Iter3 で両方解消し，「基盤の信頼性」系レバーは
  やり切った．分析(解釈) は次に②（マイクロバッチ数・stagger interval 感度分析）を推奨していたが，その後ユーザーが
  会話内で 2 回明示的に「ログ収集だけでなく，先行研究・関連研究を調査した上で推論パイプラインのパフォーマンス改善を
  行え」と指示し，オーケストレータが config `research_frontier` に⑤（先行研究調査に基づく高速化）を追加した．
- **自動選択**: Iteration 4 の単一レバーを research_frontier⑤とする．**フェーズ1（調査）で tavily 等により分散
  パイプライン並列推論の高速化手法を文献調査**（通信オーバーラップ・KV キャッシュ最適化・量子化・バッチング戦略・
  continuous batching・speculative decoding 等），**フェーズ2（計画）で単一レバー原則に従い効果の高い 1 案へ
  絞り込む**という 2 段設計．②（感度分析）は⑤の調査対象の一部として⑤に一本化・吸収する．
- **根拠**: ユーザーの直接指示（最優先）に基づく．基盤頑健化が収束した今，config levers/research_frontier の次候補へ
  レバーを移す段であり，⑤はその最優先候補としてユーザーが指定した．②を独立に立てるより，⑤の調査で②を含む広い
  候補群から選ぶ方が重複を避けられる．
- **②/⑤着手時にフェーズ2（計画）が実験設計へ織り込む前提条件（申し送り）**: (i) `mise.toml` の
  `[tasks."predict:demo"]` の `--iter Iter1` 固定を解消（`--iter Iter{n}` 変数化 or `collect_results.py --iter Iter{n}`
  直接呼び出しを正式手順化）＝複数 run の `Iter1.jsonl` 混在という実害を防ぐ．(ii) 冷開始交絡（再デプロイ後の
  プロセスグループ再初期化 348s，Iter3 の `ttft_s=81.637s` 突出が実例）の除去（最初の 1 run を捨てる or warm-up 後計測）．
  (iii) 各レバー水準 n≥3〜5 反復．(iv) 主指標 ITL/TTFT の指定．
- **可逆性**: 次に振るレバーの選定であり可逆．ユーザーの直接指示に基づく自動判断とした（破壊的操作を含まない）．
- **要レビュー / 要人間判断**: 実機 deploy/推論を伴う②/⑤の掃引 run 着手は，B1 の合意通りフェーズ4 直前に別途
  Slack 確認が必須（不可逆側はそこで人間判断を仰ぐ）．調査（フェーズ1）・計画（フェーズ2）はコードのみ・実機非接続で
  進行可能．

---

## B5 [resolved 2026-07-18] Iter3 フェーズ4（実験，ホットパス改変の動作確認）開始可否の確認依頼（B1/B4 の実行）
- **解決**: ユーザーが承認（「継続して実行せよ」）。フェーズ4（実験）へ進行する。
- **状況**: Iteration 3（P1: levers 記録の堅牢化）のフェーズ1〜3（調査・計画・実装）が完了．
  `pipeline_inference.py` にper-request 1行 INFO ログを追加（既存ロジックは変更なし，8行のみの追加），
  `tools/collect_results.py` をログ優先・env フォールバックの2段構えに変更．単体テスト 38 件 green（回帰無し）。
  この変更は**ホットパス（`pipeline_inference.py`）の改変**を伴うため，Iteration 1/2（`collect_results.py` に
  閉じた変更）とは性質が異なる．B4 の合意通り，フェーズ4（実機での動作確認，再デプロイ・推論実行を伴う）へ
  進む前に一度停止し，Slack で `<@U08GLKY1QCW>` へ確認を依頼した（2026-07-18 21:52 頃投稿）．
- 論点: 現行の実機構成（wafl-ctrl1 + worker 50 台）へ変更後のコードを再デプロイ（`mise run deploy`）し，
  `mise run predict:demo` を実行して，rank0 ログに新しい levers 行が実際に出力され，`results/Iter3.jsonl` の
  `levers` フィールドがログ由来の値で埋まることを確認してよいか．
- 現在の状態: `state.json.status=blocked` として待機中．人間の返信（Slack）を受けて `continue` 実行時に，
  承認内容に応じてフェーズ4へ進むか，スコープ変更・延期の指示に従う。

---

## B4 [auto-decided 2026-07-18] Iteration 3 の単一レバー選定（② へ直行せず P1「levers 記録の堅牢化」を先行）
- **状況**: Iteration 2（RESULT 複数行対応による照合ロジックの頑健化）を「採用」で確定．これで (a) 取り違え
  （複数行 RESULT 照合破綻）は単体テストレベルで解消したが，Iter1 分析(解釈) §3 が挙げた②着手前 4 条件のうち
  (b) levers 記録の堅牢化が未解決．(b) は今も収集ツール実行時の env/config 由来で「コンテナ起動時 env と収集時 env の
  一致」を暗黙仮定しており，②で `NUM_MICRO_BATCHES`／`STAGGER_INTERVAL` を振ると env 不一致で記録レバーが実レバーと
  食い違い，比較の妥当性が根本から崩れる恐れがある．
- **自動選択**: Iteration 3 の単一レバーを「P1: levers 記録の堅牢化」とする．実装方針は `pipeline_inference.py`
  起動時に有効な実行設定（levers）を 1 行 INFO ログで出力し，収集側（`collect_results.py`）がそのログ行から levers を
  確定する（env 由来の暗黙仮定を排除）．research_frontier②（レバー掃引）はその後に回す．
- **根拠**: 頑健化の順序として (a)→(b)→② が筋が通る．(a) だけ直して掃引に入ると (b) の env 不一致という別経路で
  結論が汚れる．Iter1 分析(解釈) が (b) を「②の妥当性に直結する」と明記している．
- **可逆性**: 掃引前の頑健化順序の選択であり可逆．破壊的操作は含まない（自動判断とした）．
- **要レビュー / 要人間判断（重要）**: P1 は Iter1・Iter2 の「`collect_results.py` に閉じた・非侵襲・クラスタ非接触」
  という性質とは異なり，**ホットパス（`pipeline_inference.py`）改変・再デプロイを伴う**．起動時 1 行 INFO 追加自体は
  graph-break リスクが低いが，動作確認には `mise run deploy`（再デプロイ）＋推論実行が必要になる可能性が高い．
  したがって **Iteration 3 のフェーズ4（実験）へ進む前に，B1 の合意（実機への deploy/推論実行を伴う実験の前に必ず
  一度 Slack で確認を仰ぐ）に基づく人間確認が必須**である．フェーズ1〜3（調査・計画・実装）はコードのみで進められるが，
  オーケストレータはフェーズ4の直前で必ず人間確認を挟むこと．別案（②直行＋運用規約で env 一致検証）の余地は B3 の
  別案として残るが，堅牢性は P1 実装に劣る（levers 誤記録を検出できない）．

---

## B3 [auto-decided 2026-07-18] Iteration 2 の単一レバー選定（② へ直行せず基盤頑健化を先行）
- **状況**: Iteration 1（結果永続化基盤）を「採用」で確定．分析(解釈)が②着手前の高リスクとして
  「RESULT 複数行照合の破綻（防御的照合が常時失敗し，②の複数 run で別 run の指標を誤レバーに紐付ける）」を指摘．
- **自動選択**: Iteration 2 の単一レバーを「RESULT 複数行対応による照合ロジックの頑健化」（`collect_results.py` の
  `_RESULT_RE`／`_extract_result_text`／`_select_relevant_block` 修正）とする．② のレバー掃引はその後に回す．
  P1（levers 記録堅牢化，`pipeline_inference.py` 起動時 1 行 INFO）はホットパス改変を伴うため Iteration 3 の
  独立イテレーションとして扱う．② 実施時は各レバー値 n≥3〜5 反復・主指標 ITL/TTFT を前提とする．
- **根拠**: RESULT 修正は `collect_results.py` 内に閉じ・非侵襲・可逆でクラスタ負荷ゼロ，かつ①と同じ「基盤の
  信頼性」レバーの延長で単一レバー原則に整合する．基盤が信用できない状態で掃引すると結論が汚染される．
- **可逆性**: 掃引前の頑健化順序の選択であり完全に可逆．破壊的操作は含まない（自動判断とした）．
- **要レビュー**: 「② へ直行し，取り違えは直列化＋狭い since 窓の運用規約で回避する」という別案の余地は残る．
  次回 continue 時に人間が方針を上書きする場合はこの B3 を差し替えること．

---

## B2 [resolved 2026-07-18] Iter1 フェーズ4（実験）開始可否の確認依頼（B1 の実行）
- **解決**: ユーザーが承認．「現行の実機構成（wafl-ctrl1 + worker 50 台）で `mise run deploy` と
  `mise run predict:demo`（収集ツール経由）を実行し，`results/Iter1.jsonl` の生成まで確認してほしい」との
  明示的な指示を受け，フェーズ4（実験）へ進行する．
- 状況: Iteration 1 のフェーズ1〜3（調査・計画・実装）が完了．結果永続化基盤（`tools/collect_results.py`）を
  コードのみ・実機非接続で実装し，単体テスト 23 件 green．B1 の合意方針通り，フェーズ4（実験，51 ノード実機への
  `deploy`/`predict:demo` 実行を伴う）へ進む前に一度停止し，Slack で `<@U08GLKY1QCW>` へ確認を依頼した
  （2026-07-18 17:37 頃投稿）．
- 論点: 現行の実機構成（wafl-ctrl1 + worker 50 台）で `mise run deploy` → `mise run predict:demo`（収集ツール経由）を
  実行し，`results/Iter1.jsonl` の生成を確認してよいか．
- 現在の状態: `state.json.status=blocked` として待機中．人間の返信（Slack）を受けて `continue` 実行時に，
  承認内容に応じてフェーズ4へ進むか，スコープ変更・延期の指示に従う．

---

## B1 [needs-human 2026-07-18] 51 ノード全体への実機デプロイ・推論実行の実行可否
- 状況: このリポジトリの実験（`mise run deploy` / `mise run predict:demo`）は，管理サーバ wafl-ctrl1 と
  worker 50 台（wafl100-139, wafl200-209）という大規模な実機クラスタに対して行われる．WAFL-PEFT（5 ノード）
  より遥かに影響範囲が大きく，他ユーザーとの共有資源への影響・障害時の切り分けコストも大きい．
- 論点: 実験フェーズ（rc-experimenter）が実際に 51 ノードへ deploy・推論実行するのを，人間の確認なしに
  自動実行してよいか．
- 暫定方針（planner が着手前に確認）: 初回イテレーションは research_frontier①（結果永続化基盤の実装，
  コードのみ・クラスタに触れない）を対象とする．実機への deploy/predict を伴う実験（②以降）に進む前に，
  必ず一度 Slack で確認を仰ぐ．確認が取れるまでは投機的な 51 ノード実行はしない．
