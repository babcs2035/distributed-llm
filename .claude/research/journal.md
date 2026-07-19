# 実験ジャーナル: distributed-llm

research-cycle が読み書きする実験ジャーナル．**新しいイテレーションを常に先頭へ挿入する（逆時系列）**．
1 イテレーション = 単一レバー変更．各ブロックに仮説・単一レバー・成功条件（planner 記入）と，
変更・結果・判定・学び（reflector 記入）をまとめる．

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

## Iteration 5

### 考察・次計画 (Iter5)

**担当**: 考察・次計画 subagent（2026-07-19）．分析(解釈) の結論（本ブロック `### 分析(解釈) (Iter5)`）を受け，
単一レバー「SL1: compute 側上限の local マイクロベンチ」の採否を確定し，次イテレーション（Iteration 6）の方向を
決めた．実機への新規接続・実行はしていない（記録の読み取りとコミット操作のみ）．

**1. 採否判定: 採用で確定・収束（adopt & converged）**

- **判定根拠**: SL1 は診断（計測）レバーであり，判定対象は「B3 の compute 側効き源 (ii)（seq_len=1 GEMV を K 位置
  まとめた GEMM に変えると 4 コア CPU の演算強度／キャッシュ効率が上がる）が対象 CPU で実在するか（向き）」．計画 §3 の
  成功条件を全て充足した．実装（`scripts/bench_compute_ceiling.py`／`tests/test_bench_compute_ceiling.py` の新規 2 ファイル
  のみ・`pipeline_inference.py` 非改変・`pytest` 53 passed／回帰なし・`py_compile` 健全）と，判定（実機 i5-8350U，
  `wafl100`＝rank1，cpuset 0-3＝デプロイ対象そのもので ratio_2=0.753／ratio_4=0.378／ratio_8=0.213，K 昇順単調減少かつ
  `ratio_8 ≤ 0.85` を満たし判定ルール (i)「利得あり」に明確該当）が揃った．効果量（ratio_8 の 1.0 からの乖離 0.787）は
  計画 §3 のノイズ幅 ±0.05 の十数倍で，n=1 でもラベルが反転する余地は無い．**採用で確定**．
- **追加反復の要否**: 不要．CPU マイクロアーキ・BLAS・torch 版が異なる 2 環境（ローカル 64 コア／実機 i5-8350U 4 コア
  専有）が独立に単調減少・同一ラベルを再現しており，同一ホスト 3 回反復より強い外的頑健性が既に得られている．
- **このレバーの収束状況**: SL1（compute 天井の診断）は「利得あり」で目的を達成し，**このサブレバーは収束**．「compute 側
  利得がこの CPU で実在するか」という単一の問いに決定的な答え（実在する）が出たため，同じ問いへ SL1 を再び振っても
  新情報は得られない．次は B3 の go/no-go を決めるもう一方の因子（draft 採択率）へ論点を移す段である．

**2. 非自明な学び（次の自分向け）**

- **(i) SL1 は B3 のダウンサイドリスクの片側だけを消した**: B8／B9 が SL1 に課した唯一の問い「compute 側効き源が実在
  しなければ B3 の天井は残差償却 ≈1.08 倍に縮み大投資の意味が無い」というダウンサイドリスクは，実機 ratio_8=0.213
  （per-token compute を実機で 79% 削減，理論上 82%）により**棄却**された．B3 の compute 天井は採択が理想化されれば
  per-token compute を最大 1/0.213≈4.7 倍に高める余地がある．ただし SL1 が測るのは計算効率のみで，**実運用の速度向上は
  draft 採択率・検証コスト・relay 1 往復化のプロトコルオーバーヘッドにも依存する**．したがって B3 の go/no-go は
  「compute 天井 × 採択率 × 検証コスト」の積で期待値が決まり，SL1 は積の 1 因子（上限側）を埋めたにすぎない．
- **(ii) 利得の絶対量は CPU 依存で，実機値を基準にすべき**: ratio_2 が 0.497（ローカル）→0.753（実機）と 0.25 も乖離した．
  非力な CPU（1.7GHz・4 コア）では固定オーバーヘッドの相対比が大きく，K=2 程度の小さなまとめでは利得が縮む．B3 の
  期待効果を見積もる際はローカル値ではなく**実機値（ratio_8=0.213）**を用いること．向き（利得の有無）は CPU アーキの
  違いで覆らないが，倍率の絶対値はローカル推定を鵜呑みにしてはいけない，というのが SL1 の非自明な学びである．
- **(iii) 残る不確実性は期待値側（採択率）に一点集約された**: SL1 で上限側の空白は埋まったが，採択率が低ければ K を
  捨て直す割合が増え実効利得は上限から目減りする．B3 は「投資に値する下限条件はクリアだが期待値は未確定」の状態．

**3. B9（B3 本体 go/no-go）の扱い: 温存（今回は人間に諮らない）**

- **判断**: 分析(解釈) の推奨どおり，B9（SL3＝relay プロトコル改修の go/no-go）を**今のタイミングで人間に諮るのは
  時期尚早**とし，B9 は `[needs-human]` のまま**温存**する（差し替えない）．理由は §2(i) のとおり，採択率が未計測の
  まま大投資の是非を人間へ丸投げすると，SL1 で下げたリスクの半分（期待値側）を人間判断へ転嫁するだけで情報不足だから
  である．Iteration 6 で SL2（採択率）を埋め，SL1×SL2 で B3 の実効利得の期待値レンジを数値で括ってから B9 を諮る．

**4. 次に振るレバーの決定（Iteration 6）: SL2（draft 採択率のオフライン見積もり）を自動選定**

- **決定（自律判断・可逆）**: Iteration 6 の単一レバーを **SL2（draft 採択率のオフライン見積もり）**とする．検証するのは
  「K トークン提案のうち検証で受理される割合（＝毎回 K を捨てずに済む割合）」を，relay 改修せず・prompt-lookup／n-gram
  draft または小 draft モデルで既存ログ／参照出力に対して見積もること．SL1（compute 天井）と SL2（採択率）が揃えば
  B3 の実効利得の期待値レンジを初めて数値で括れ，B9 go/no-go の質が上がる．具体的な実装方針（draft 戦略・参照データの
  取り方）は次の rc-planner が決める．
- **可逆性の確認（この決定を自律判断とした根拠）**: 採択率のオフライン見積もりは通常，実クラスタへの deploy／relay 改修を
  伴わない静的解析または小規模なオフライン生成（既存の軽量モデルでの試行）で完結する可逆な作業であり，SL1 と同じ
  「作る前に測る」診断系譜に収まる．参照出力の取得に実機推論を要する場合があるが，それは B7 の包括承認範囲内の非破壊
  SSH で対応可（破壊的操作ではない）．**もし計画フェーズで SL2 の実装がクラスタ本体（`pipeline_inference.py` ホットパス
  や 51 ノード再デプロイ）への大きな変更を要すると判明すれば，その時点で backlog へ `[needs-human]` として登録し
  Slack で確認を仰ぐこと**（申し送り）．
- **見送り（非選定）**: SL3／B3 本体（relay プロトコル改修）は不可逆・大規模で B9 の人間 go/no-go 事項のため見送り．
  config `levers`（NUM_MICRO_BATCHES 等）は Iter4 で「支配項 compute に効かない（Σcompute 不変・残差止まり）」と確定済み
  で，支配項を攻める文脈では優先度が低い．backlog に `## B10 [auto-decided 2026-07-19]` として本決定を記録した．

**次イテレーションへの結論**: Iteration 5（SL1: compute 側上限の local マイクロベンチ）を**採用で確定・収束**
（実機で ratio_8=0.213，判定「利得あり」＝B3 の compute 側利得の実在を確認し，ダウンサイドリスクを棄却）．
Iteration 6 は SL2（draft 採択率のオフライン見積もり）を自動選定して開始する．B3 本体（SL3）go/no-go の B9 は温存し，
SL1×SL2 で期待値レンジを固めてから改めて人間に諮る．

---

### 分析(解釈) (Iter5)

**担当**: 分析(解釈) subagent（2026-07-19）．`## Iteration 5` の全ブロック（計画・実装・実験）と backlog B8／B9 を
Read し，単一レバー「SL1: compute 側上限の local マイクロベンチ」が測った `ratio_K` を，過去反復（Iter4 の run 間
ばらつき）と計画 §3 の判定ルール・ノイズ幅（±0.05）に照らして解釈した．実機への新規接続・実行はしていない（記録の
読み取りのみ）．

**前提（判定の枠組み）**: 本イテレーションの判定対象は「B3 の compute 側効き源 (ii)（seq_len=1 GEMV を K 位置
まとめた GEMM に変えると 4 コア CPU の演算強度／キャッシュ効率が上がる）が対象 CPU で**実在するか（向き）**」であり，
B3 本体の実レイテンシ低減量そのものではない．したがって判定は「`ratio_K` の 1.0 からの乖離が計画 §3 のノイズ幅
±0.05 を超え，K 昇順で単調減少するか」で行う．

**1. 有意性の判定: signal（利得は実在）．ラベル「利得あり」はノイズに対して頑健．ただし n=1／環境依存の限界は明記する**

- **効果量がノイズ幅を桁違いに上回る**: 実機（i5-8350U，`wafl100`，cpuset 0-3）で ratio_2=0.753／ratio_4=0.378／
  ratio_8=0.213．1.0 からの乖離は最小の ratio_2 でも 0.247，ratio_8 では 0.787 に達し，計画 §3 のノイズ幅 ±0.05 の
  5〜16 倍大きい．K 昇順で単調減少（0.753→0.378→0.213）かつ `ratio_8=0.213 ≤ 0.85` を満たし，判定ルール (i) の
  **「compute 側利得が実在（≥15% 短縮）」に明確に該当**する．曖昧域 (iii)（0.85<ratio<0.95）や利得なし (ii)（全 K で
  ≥0.95）からは大きく離れており，このラベルが反転する余地は無い．
- **n=1 の限界を，2 環境の一致とスクリプト内 200 反復中央値で補う**: 各環境の実行は 1 回ずつ（n=1）で，`ratio` 自体の
  run 間分散は直接は得ていない．ただし (a) スクリプトは内部で `WARMUP_ITERS=50`／`MEASURE_ITERS=200` の中央値を採り
  timer jitter を均している，(b) **CPU マイクロアーキ・BLAS・torch 版が異なる 2 環境**（ローカル 64 コア／実機 i5-8350U
  4 コア専有）が**独立に**単調減少・同一ラベルを再現した——これは同一ホストでの 3 回反復より強い外的頑健性の証拠である，
  (c) 効果量が上記のとおりノイズ幅を桁違いに上回る，の 3 点から，**ラベル「利得あり」は n=1 でも有意**と断定できる．
  Iter4 で確認した実機 run 間ばらつき（compute% レンジ 0.27pp，step_dt 幅 0.55%）を参照しても，この規模の効果量を
  覆すノイズは想定しにくい．
- **ただし絶対値は環境依存で，実機値を基準にすべき**: ratio_2 が 0.497（ローカル）→0.753（実機）と 0.25 も乖離しており，
  **利得の絶対量は CPU 依存**である．非力な CPU（1.7GHz・4 コア）では固定オーバーヘッドの相対比が大きく，K=2 程度の
  小さなまとめでは利得が縮む．B3 の期待効果を見積もる際はローカル値ではなく**実機値（ratio_8=0.213，K=8 で per-token
  compute を実測 79% 削減）**を用いること．なお実機追試は**デプロイ対象そのもの**（wafl100＝rank1，i5-8350U，cpuset 0-3）
  で行われており，代表性の点でも local 実行より信頼できる．想定外挙動（言語崩れ・発散・OOM 等）は無く，形状取得も
  `real_gemma4_layer`（フォールバック未使用）で成立している．

**2. B3 本体への示唆: SL1 は compute 側「天井の存在」だけを保証．実運用の速度向上は保証しないが，ダウンサイドリスクの一部は解消**

- **SL1 が測ったもの／測っていないもの**: `ratio_K` は「K 位置を 1 度の GEMM にまとめたときの，1 トークンあたり計算
  効率」のみを測る．speculative decoding の実レイテンシ低減は，これに加えて **draft 採択率**（提案 K のうち検証で受理
  される割合＝毎回 K を捨てずに済む割合）・**検証コスト**・**relay 1 往復化のプロトコルオーバーヘッド**に依存する．
  したがって **SL1 の結果だけで B3 の実運用速度向上（FlowSpec/PipeDec の 1.36–1.77× 等）を保証することはできない**．
- **解消されたのは「compute 側が利得ゼロ」というダウンサイドリスク**: B8／B9 が SL1 に課した唯一の問いは「compute 側
  効き源がこの CPU で実在するか（実在しなければ B3 の天井は残差償却 ≈1.08 倍に縮み，大投資の意味が無い）」であった．
  実機で ratio_8=0.213（理論上 82% 削減／実機 79% 削減）が確認され，**「compute 側の利得が存在しないので投資しても
  無駄」というダウンサイドリスクは棄却された**．すなわち B3 の compute 側の天井は「残差償却 1.08 倍止まり」ではなく，
  採択が理想化されれば per-token compute を実機で最大 1/0.213≈4.7 倍に高める余地がある，という上限が引けた．
- **残る不確実性は期待値側（採択率）**: SL1 は B3 の**上限（ceiling）を引き上げた**が，**期待値（実際に何倍出るか）は
  採択率が未計測のため依然不定**である．採択率が低ければ K を捨て直す割合が増え，実効利得は上限から大きく目減りする．
  つまり B3 は「上限はゼロではない（投資に値する下限条件はクリア）が，期待値は未確定」という状態にある．

**3. backlog B9（B3 本体 go/no-go）への推奨: SL1 は決定的入力の片側を埋めた．採択率（SL2）を埋めてから人間に諮るのが妥当**

- SL1 は B9 が求めた「compute 側利得の実在有無」という決定的入力の**片側を確定的に埋めた（＝go 方向の下限条件クリア）**．
  一方で B3 の go/no-go は本来「compute 天井 × 採択率 × 検証コスト」の**積**で期待値が決まる意思決定であり，SL1 だけでは
  積の 1 因子しか埋まっていない．採択率がゼロに近ければ SL3（不可逆・大規模な relay 改修・51 ノード再デプロイ）の投資は
  依然回収できない．
- **推奨（考察フェーズ＝rc-reflector への材料提示）**: **「compute 側効き源は実在確認済み．ただし draft 採択率という
  期待値側の不確実性が残るため，SL2（draft 受理率のオフライン見積もり）を先に潰してから B3 本体 go/no-go を人間に諮る」**
  のが妥当と考える．SL1 と同じ「作る前に測る」診断系譜（Iter1〜5 の一貫した方針）に沿い，不可逆な SL3 に踏み込む前に
  期待値側の因子を安価に埋める順序が筋が通る．「SL1 で十分な確証が得られたので今すぐ SL3 go/no-go を諮る」案は，採択率
  未知のまま大投資の是非を人間に丸投げすることになり，SL1 で下げたリスクの半分（期待値側）を人間判断へ転嫁するだけで
  情報不足と判断する．ただし go/no-go 自体は人間確認事項（B9）であり，最終的な諮り方は考察フェーズが決めること．

**4. 次イテレーション（Iteration 6）のレバー選定材料（判定は考察フェーズ）**

- **第一候補: SL2（draft 採択率のオフライン見積もり）**．B3 の期待値を決めるもう一方の因子（採択率）を，relay 改修せず・
  prompt-lookup／n-gram draft または小 draft モデルで既存ログ／参照出力に対して見積もる．SL1（compute 天井）と SL2
  （採択率）が揃えば，B3 の実効利得の期待値レンジを**初めて数値で括れる**＝B9 go/no-go の質が上がる．規模は中
  （参照出力の取得に実機推論を要する場合があり，その場合は B7 の包括承認範囲内の非破壊 SSH で対応可．draft 生成の
  実装粒度は計画フェーズで要精査）．「作る前に測る」系譜に整合し，単一レバー原則にも収まる．
- **非推奨（今回は見送り）**: SL3／B3 本体（relay プロトコル改修）は不可逆・大規模で B9 の人間 go/no-go 事項．SL2 を
  経ずに直行するのは上記 §3 のとおり期待値側が空白のまま大投資を諮ることになり，時期尚早．config `levers`
  （NUM_MICRO_BATCHES 等）は Iter4 で「支配項 compute に効かない（Σcompute 不変・残差止まり）」と確定済みで，
  支配項を攻める文脈では優先度が低い．
- **レバー収束の状況**: SL1（compute 天井の診断）は「利得あり」で目的を達成し，このサブレバーは収束．ただし B3 全体の
  go/no-go はまだ収束しておらず，採択率（SL2）という単一の残不確実性へ論点が移った段階である．

---

### 実験 (Iter5)

**担当**: 実験フェーズ subagent（2026-07-19）．`### 実装 (Iter5)` で完了した `scripts/bench_compute_ceiling.py`
のローカル実行結果（`os_cpu_count=64` の非対象ホスト，ratio_8=0.178，「利得あり」）が，対象実機（i5-8350U，
4 コア専有）でも同じ向きを示すかを追試した．クラスタの relay プロトコルや `pipeline_inference.py` の常駐推論
プロセスには一切接続・変更していない（`docker exec` による追加プロセスの起動のみ）．

**1. 実行可能性の確認**

- `hosts.txt` の rank 1（`hosts[1]`，read_hosts の順序規約は `tools/collect_results.py:745` 参照）は `wafl100`．
  `tools/common.py` の `ssh_via_master`（ProxyJump 経由，master=`wafl-ctrl1`，user=`denjo`）で疎通確認．
- `wafl100` 上の `distributed-llm` コンテナは稼働中（`docker ps` で `Up 13 hours (healthy)`）．
- コンテナ内 `python3` で `torch==2.13.0+cpu`／`transformers==5.14.1` が利用可能なことを確認．
- `lscpu`（ホスト側）で `Intel(R) Core(TM) i5-8350U CPU @ 1.70GHz` を確認．`docker inspect
  --format '{{.HostConfig.CpusetCpus}}'` で `0-3`（4 コア専有）を確認．計画が想定した対象 CPU・コア数と一致．

**2. 転送・実行**

- ローカルの `scripts/bench_compute_ceiling.py`（15335 バイト）を base64 化し，`docker exec distributed-llm sh -c
  'echo <base64> | base64 -d > ...'` でコンテナ内 `/tmp/iter5_bench_check/scripts/bench_compute_ceiling.py` へ
  書き込み（転送後にバイト数一致を確認済み）．`config.json`（`/app/config.json`）はコンテナ内で読み取り専用コピー
  を `/tmp/iter5_bench_check/config.json` へ作成（`/app` 側は非変更）．
- `docker exec -w /tmp/iter5_bench_check distributed-llm python3 scripts/bench_compute_ceiling.py` で実行．
  既存の常駐推論プロセス（メインプロセス）とは別の追加プロセスとして起動しており，メインプロセスの停止・再起動は
  行っていない（実行前後で `docker ps` の稼働時間が変化していないことを確認）．
- 所要時間 347.7 秒（ローカル実行時の 113 秒より約 3 倍．4 コア・1.7GHz という非力な CPU での実行のため妥当）．

**3. 実行結果（実機 i5-8350U，`wafl100`）**

- `shape_source="real_gemma4_layer"`（`shape_warnings=[]`）．コンテナ内から `AutoConfig.from_pretrained
  ("google/gemma-4-31B-it")` に到達でき，ローカル実行と同じ実 Gemma4 線形層形状（`q_proj: 5376->8192` 等）を使用．
- GEMV（seq_len=1）1 層中央値: 209.80ms．
- K=2: per_token=158.04ms，**ratio_2=0.7533**
- K=4: per_token=79.28ms，**ratio_4=0.3779**
- K=8: per_token=44.76ms，**ratio_8=0.2133**
- K 昇順で単調減少かつ `ratio_8=0.2133 ≤ 0.85` を満たし，判定は**「利得あり」**．

**4. ローカル実行結果との比較（同じ向きの確認）**

| | ratio_2 | ratio_4 | ratio_8 | 単調減少 | 判定 |
|---|---|---|---|---|---|
| ローカル（os_cpu_count=64） | 0.497 | 0.292 | 0.178 | Yes | 利得あり |
| 実機 i5-8350U（`wafl100`，cpuset 0-3） | 0.753 | 0.378 | 0.213 | Yes | 利得あり |

実機でも K 昇順で比率が単調に低下し「利得あり」の判定が再現された（向きは一致）．ただし絶対値は実機の方が
全体的に高め（特に ratio_2 が 0.50→0.75 と差が大きい）で，1.7GHz・4 コアという非力な CPU では固定オーバーヘッド
の相対比率が大きく，K=2 程度の小さなまとめでは利得が縮小することを示唆する．K=8 まで見れば利得は十分明確
（ratio_8=0.2133，1.0 から大きく乖離）であり，判定ラベルとしては反転していない．

**5. 後片付け**

- 実行後，コンテナ内の一時ディレクトリ `/tmp/iter5_bench_check`（スクリプト・config.json コピー・結果 jsonl を
  含む）を `docker exec distributed-llm rm -rf /tmp/iter5_bench_check` で削除し，削除確認（`ls` が
  `No such file or directory` を返すこと）を実施．ローカルの base64 中間ファイルも削除済み．
- クラスタ側に変更は一切残していない（`/app/config.json` は読み取りのみ，コンテナのメインプロセスは無停止）．

**6. 気づいた点**

- 実機（i5-8350U，4 コア専有）でもローカル代替ホスト（64 コア）と同じ「向き」（K を増やすほど 1 トークンあたり
  compute 時間が減る）が確認され，B3（speculative decoding）go/no-go 判断における compute 側効き源の実在性は，
  CPU マイクロアーキテクチャの違いによって覆らないことが実測で裏付けられた．
- 絶対比率は実機の方が高め（利得がローカル推定より小さい）ため，B3 の期待効果を見積もる際はローカル値
  （ratio_8=0.178）ではなく実機値（ratio_8=0.213）を基準にすべきである．
- コンテナ内の `transformers`／`torch` バージョンはローカル環境と異なる（`transformers==5.14.1`／
  `torch==2.13.0+cpu` vs ローカルの版）が，形状取得（`real_gemma4_layer`）に成功しており，判定への影響は
  無いと考えられる．

---

### 実装 (Iter5)

**担当**: 実装フェーズ subagent（2026-07-19）．計画（本ブロック直下 `### 計画 (Iter5)` §2・§4）に従い，単一レバー
「SL1: compute 側上限の local マイクロベンチ」を実装した．`pipeline_inference.py`／`tools/*.py` は一切非改変．
実機クラスタへの接続・deploy・推論実行は行っていない（ローカル単一プロセスでの実装・単体テスト・1 回実行のみ）．

**1. 変更ファイル（新規 2 つのみ，計画どおり）**

- **`scripts/bench_compute_ceiling.py`**（`scripts/` ディレクトリ新設）:
  - 定数 `WARMUP_ITERS=50`／`MEASURE_ITERS=200`／`K_VALUES=(2,4,8)`／`NUM_THREADS=4`／`COMPUTE_DTYPE=torch.float32`
    （`pipeline_inference.py:38` と同一）を計画どおりに定義．
  - `build_linear_shapes()`: 実 `Gemma4TextDecoderLayer(text_config, layer_idx=0)` を `AutoConfig.from_pretrained
    ("google/gemma-4-31B-it")` 経由で random-init 構築し（重みファイル非ロード），`named_modules()` から `nn.Linear`
    を列挙して形状（`LinearShape(name, in_features, out_features)`）を取得する．実構築が失敗した場合のみ
    `_build_linear_shapes_from_config_fallback()` が `config.json` の `model.overrides` と `head_dim=256` 等の
    フォールバック定数から形状を導出し，`intermediate_size` が仮定値である旨を `warnings` に明記する（黙って歪めない）．
  - `measure_linear()`／`measure_layer_ns()`: `time.perf_counter_ns()` でウォームアップ `WARMUP_ITERS` 回後，
    `MEASURE_ITERS` 回を 1 回ずつ計測し中央値・最小値を返す．1 層分は全 `nn.Linear` の中央値総和．
  - `compute_ratios()`: `ratio_K = (GEMM(K) 1層時間/K) / GEMV(seq_len=1) 1層時間` を純関数として算出．
  - `classify_ratio()`: 計画 §3 の判定ルールどおり，`ratio_8 ≤ GAIN_RATIO_THRESHOLD(=0.85)` かつ K 昇順で単調減少なら
    「利得あり」，全 K が `NO_GAIN_RATIO_THRESHOLD(=0.95)` 以上なら「利得なし」，それ以外は「曖昧」を返す．
  - 結果は人間可読テーブルを stdout へ，かつ `results/bench_compute_ceiling.jsonl`（新規）へ 1 レコード追記
    （`num_threads`／`dtype`／`torch_version`／`cpu`／線形形状／per-layer 時間／K 別 `ratio`／判定ラベルを含む）．
  - `if __name__ == "__main__":` の単独実行スクリプトとして完結．

- **`tests/test_bench_compute_ceiling.py`**（新規）: 純関数 `_build_linear_shapes_from_config_fallback`／
  `compute_ratios`／`classify_ratio` に対し計 8 件のテストを追加（計画の「最低 4 件」を上回る）．
  タイミング依存の `measure_linear`／`measure_layer_ns` は対象外とした（計画どおり）．`scripts/` を `sys.path` へ
  追加する処理はテストファイル自身に閉じ込め，`tests/conftest.py`（`tools/` 追加専用）は非改変とした．

**2. 検証結果**

- `uv run python -m py_compile scripts/bench_compute_ceiling.py tests/test_bench_compute_ceiling.py`: エラー無し．
- `unset VIRTUAL_ENV && uv run pytest tests/test_bench_compute_ceiling.py -v`: **8 passed**（計画の「最低 4 件」を
  満たす）．`unset VIRTUAL_ENV && uv run pytest tests/ -v`: **53 passed, 0 failed/error**（既存 45 件＋新規 8 件，
  回帰なし）．
- `unset VIRTUAL_ENV && uv run python scripts/bench_compute_ceiling.py` を実行環境（i5-8350U ではなく本 research-cycle
  実行ホスト，`os_cpu_count=64`・`cpu=x86_64`）で 1 回実行．約 113 秒で完走し `results/bench_compute_ceiling.jsonl` へ
  1 レコード追記された．`shape_source="real_gemma4_layer"`（フォールバック未使用，`shape_warnings=[]`）で，実際に
  `AutoConfig.from_pretrained` が到達可能だったことを確認．
- **実行結果（ratio 値）**: GEMV(seq_len=1) 1層中央値 80.97ms に対し，K=2: per_token=40.27ms（ratio=0.497），
  K=4: per_token=23.63ms（ratio=0.292），K=8: per_token=14.38ms（ratio=0.178）．K 昇順で単調減少かつ
  `ratio_8=0.178 ≤ 0.85` を満たし，判定は「利得あり」．
- `git status --short`: 変更は `scripts/bench_compute_ceiling.py`（新規ディレクトリ含む）／
  `tests/test_bench_compute_ceiling.py`（新規）／`results/bench_compute_ceiling.jsonl`（新規，スクリプト実行の
  自然な出力）の 3 エントリのみ．`pipeline_inference.py`／`tools/*.py` は非改変．`.claude/research/config.yml`・
  `journal.md`・`state.json`・`agent.json` の差分は計画フェーズ以前から存在した未コミット変更であり，本実装
  フェーズが持ち込んだものではない（触れていない）．

**3. 気づいた点・申し送り**

- **実行ホストは i5-8350U ではない**（`os_cpu_count=64` の多コアサーバ．計画 §4 が既に指摘済みの CPU 代表性の注意）．
  絶対値は実ノードと異なりうるが，本イテレーションの判定対象は「向き（K をまとめることで 1 トークンあたり compute
  時間が減るか）」であり，K=2/4/8 で単調に大きく減少（ratio 0.50→0.29→0.18）しているため，方向としての利得は
  このホストでも明確に観測された．i5-8350U（4 コア専有・OpenBLAS/MKL 構成が異なる可能性）での追試により絶対値は
  変わりうるが，判定ラベルが反転するほどの余地（ratio_8 が 0.85 を超える）は小さいと考えられる．
- `AutoConfig.from_pretrained("google/gemma-4-31B-it")` は本実行ホストから到達可能で（ネットワークまたは既存
  キャッシュ経由），フォールバック分岐は未使用で終わった．フォールバック分岐（config.json 由来）は単体テストで
  別途検証済み（`intermediate_size=hidden_size*4=21504` が実測値と一致することも事前確認済み）．
- **実験を開始してよい状態か**: 本 SL1 は計測・実装・1 回実行まで完了しており，追加の実機接続・deploy は不要
  （計画どおりクラスタ非接触で完結する単発診断のため，本イテレーションに「実験フェーズ」の別途着手は無い）．
  結果（ratio・判定ラベル）は次の分析・考察フェーズが B3 本体（SL3: relay プロトコル改修，backlog B9）の
  go/no-go 判断の入力として解釈すること．

---

### 計画 (Iter5)

**担当**: 計画フェーズ subagent（2026-07-19）．`journal` Iter4（分析(解釈)§3 の B3／FlowSpec／PipeDec 記述），backlog B8／B9，
`pipeline_inference.py`（`COMPUTE_DTYPE`:38・`set_num_threads`:404・層 forward hot path :1702-1704・decode の hidden 形状），
`config.json`（`hidden_size=5376` 等），`tools/gemma4_layer.py`（`Gemma4TextDecoderLayer` 構築）を実際に Read し，backlog B8
（SL1: compute 側上限の local マイクロベンチ）を実装可能な粒度へ落とし込んだ．**本フェーズは実機クラスタへの接続・deploy・
推論実行を一切行わない（コード読み取りのみ）**．

#### 0. コードで確認した前提（計画の土台）

- **compute の実体**: decode step の計算は `for layer in self.my_layers: hidden_state = layer(hidden_state,
  position_ids=positions, is_first=is_first)`（`pipeline_inference.py:1702-1703`）で，`hidden_state` は
  **(batch=1, seq_len=1, hidden=5376) の GEMV**．`compute dt` はこのループ直後（`:1704`）に確定し，Iter4 で ITL の 92% と
  確定した支配項そのものである．
- **計算条件（固定すべき値）**: `COMPUTE_DTYPE = torch.float32`（`:38`），`torch.set_num_threads(os.cpu_count())`＝
  i5-8350U で 4（`:404`），`torch.set_num_interop_threads(1)`（`:405`）．
- **層本体**: transformers の `Gemma4TextDecoderLayer`（`gemma4_layer.py:13,35`）．ノードあたり層数は 60/51 で大半 1 層・
  9 ノードが 2 層．支配的な線形層（GEMM 対象）は `self_attn.{q,k,v,o}_proj` と `mlp.{gate,up,down}_proj`．正確な out 次元は
  `head_dim`／`intermediate_size` に依存し，`config.json` には `intermediate_size` が無い．そのため実 `Gemma4TextDecoderLayer` を
  **重みロードせず random-init で構築**して `nn.Linear` 子モジュールから `(in_features, out_features)` を列挙し，正確な GEMM 形状を
  得る（ハードコードを避ける）．
- **B8 の性質**: 重み不要のランダムテンソルで足り，`pipeline_inference.py` 非改変・再デプロイ不要・クラスタ本体（分散推論）
  非接触・完全に可逆．Iter1〜4 と同じ「本体を作る前に測る」診断の系譜に属する．

#### 1. 仮説

B3（speculative decoding）の compute 側効き源 (ii)「seq_len=1 の GEMV を K 位置まとめた GEMM に変えると 4 コア CPU の演算強度／
キャッシュ効率が上がる」がこの実機（i5-8350U 相当・4 スレッド・float32・OpenBLAS/MKL）で実在するなら，1 層分の全線形層について
**「K 位置 GEMM の総時間 ÷ K」が「seq_len=1 GEMV の総時間」より小さくなる**（重み行列の読み出しが K トークンに 1 回へ償却され，
GEMV の帯域律速が緩和されるため）．逆に演算強度が上がらない CPU では GEMM(K)≈K×GEMV となり **比率≈1**．この比率が B3 本体
（SL3: relay プロトコル改修，backlog B9）着手の go/no-go の決定的入力になる．

#### 2. 単一レバー・変更内容

**単一レバー**: 「記録・診断の対象を，実機の分散 relay 経路（Iter1〜4）から，**単一プロセスの local compute マイクロベンチ
（GEMV vs GEMM）**へ移す」の 1 点．クラスタ・relay・`pipeline_inference.py` は一切変更しない（固定）．振るのは **seq_len（=1 vs K）**
のみで，計算条件（hidden=5376・実 Gemma4 層の線形形状・float32・num_threads=4・batch=1）は直近最良＝実機 Iter4 の compute 条件に
合わせて固定する．

**変更ファイル（新規のみ・クラスタ非接触）**:
- 新規 `scripts/bench_compute_ceiling.py`（`scripts/` ディレクトリ新設）．責務: i5-8350U 相当条件下で GEMV（seq_len=1）と
  GEMM（seq_len=K, K∈{2,4,8}）の 1 トークンあたり compute 時間を比較する local マイクロベンチ．
- 新規 `tests/test_bench_compute_ceiling.py`（純関数の単体テスト）．

**スクリプト設計**:
- **(a) セットアップ**: `torch.set_num_threads(NUM_THREADS=4)`・`torch.set_num_interop_threads(1)`・dtype=float32．
  `platform.processor()`／`os.cpu_count()`／`torch.__version__` を記録（CPU 代表性の解釈のため）．
- **(b) 形状取得**: 実 `Gemma4TextDecoderLayer(text_config, layer_idx=0)` を random-init で構築（重みファイル非ロード）し，
  `named_modules()` から `nn.Linear` を列挙して `LinearShape(name, in_features, out_features)` のリストを得る．構築失敗時
  （transformers 層や model config が local に無い場合）は `config.json` ＋ `head_dim=256`（`query_pre_attn_scalar=256` 由来）から
  q=32×256=8192・k/v=16×256=4096・o=8192→5376 を導出し，`intermediate_size` のみ **log 付き仮定値**へフォールバックする
  （黙って歪めず，仮定を明示）．
- **(c) 計測**: 各 `LinearShape` について `W[out,in]`・`x1[1,in]`・`xK[K,in]` を float32 random 生成し，`F.linear(x, W)` を
  warmup=`WARMUP_ITERS(=50)` 回捨てたのち，`MEASURE_ITERS(=200)` 回を `time.perf_counter_ns()` で 1 回ずつ計測して**中央値**
  `t_median` を採る（微小 matmul の scheduler jitter 対策に median を主指標，min も併記）．1 層＝全 Linear の `t_median` 総和を
  per-layer 時間とする．
- **(d) 指標**: 各 K について `per_token_K = t_gemm_layer(K) / K`，`ratio_K = per_token_K / t_gemv_layer(1)`．
- **(e) 出力**: 人間可読テーブル（stdout）＋ `results/bench_compute_ceiling.jsonl` へ 1 レコード追記（`num_threads`・`dtype`・
  `torch_version`・`cpu`・線形形状・per-layer 時間・K 別 `ratio`・判定ラベル）．

**計測パラメータの定数化**（マジックナンバー回避）: `WARMUP_ITERS=50`・`MEASURE_ITERS=200`・`K_VALUES=(2,4,8)`・`NUM_THREADS=4`．

#### 3. 成功条件（measurable）

実装・実行の完了条件（決定的）:

1. `scripts/bench_compute_ceiling.py` がエラー無く完走し，K∈{2,4,8} それぞれについて `ratio_K` を算出・出力する．
2. 純関数（形状フォールバック導出・`per_token=t/K`・`ratio=per_token/t1`・判定ラベル付与）の単体テストが **green（最低 4 件）**，
   `uv run python -m py_compile scripts/bench_compute_ceiling.py tests/test_bench_compute_ceiling.py` がエラー無し．
3. 変更は `scripts/bench_compute_ceiling.py`／`tests/test_bench_compute_ceiling.py` の **新規 2 ファイルのみ**
   （`pipeline_inference.py` 他，既存本体は非改変）．

判定（go/no-go の決定的入力，判定は analyst）:

4. **判定ルール**: (i) `ratio_8 ≤ 0.85` かつ K について単調減少 → **compute 側利得が実在**（GEMM で 1 トークンあたり ≥15% 短縮）
   ＝ B3 go 方向の根拠．(ii) 全 K で `ratio ≥ 0.95` → **compute 側利得なし** ＝ B3 の天井は残差償却（≈1.08 倍）に縮小し，大規模な
   relay 改修（SL3）は見送り方向．(iii) `0.85 < ratio < 0.95` → 曖昧，追加 K や実ノード計測を検討．
5. **ノイズ幅**: 中央値採用＋`MEASURE_ITERS=200` で微小 matmul の timer jitter を均す．`ratio` の 1.0 からの差が **±0.05 を超えるもの
   だけ**を有意な向き付けとして扱う（Iter4 集計の run 間ばらつき <1% を参考に，microbench では保守的に ±5% を採る）．

#### 4. 実装フェーズ（rc-implementer）への申し送り

- **対象ファイル・キー**: 新規 `scripts/bench_compute_ceiling.py`（定数 `WARMUP_ITERS`／`MEASURE_ITERS`／`K_VALUES`／
  `NUM_THREADS`，純関数 `build_linear_shapes()`／`measure_linear()`／`compute_ratios()`／`classify_ratio()`），
  新規 `tests/test_bench_compute_ceiling.py`．実行は `unset VIRTUAL_ENV && uv run python scripts/bench_compute_ceiling.py`
  （Iter4 と同様の `VIRTUAL_ENV` 汚染回避）．
- **CPU 代表性の注意（重要）**: この判定は CPU アーキテクチャ（キャッシュ／メモリ帯域）依存である．B8 の指定どおり
  `num_threads=4`・float32 で local 実行するが，research-cycle 実行ホストは i5-8350U ではない可能性が高く，比率の**絶対値**は
  実ノードと差が出る．よって analyst は `ratio` を「**向き（利得の有無）**」として解釈し，判定が (iii) 曖昧域に落ちた場合は，
  実験フェーズで worker ノード 1 台上の軽量単独プロセス（**推論コンテナ非接触**）で追試する案を B9 go/no-go の補助に据える．
  この追試はクラスタ本体（分散推論）に触れないが SSH を伴うため，実施時は B7 の包括承認（非破壊 SSH は自律可）の範囲内で行う．
- **やらないこと**: 実機 relay・deploy・`pipeline_inference.py` 改変・推論実行は本イテレーションでは一切行わない．B3 本体
  （SL3: relay プロトコル改修）は backlog B9 として温存し，本 SL1 の `ratio` 結果を添えて別途人間に go/no-go を諮る．

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
