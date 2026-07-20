# 実験ジャーナル: distributed-llm

research-cycle が読み書きする実験ジャーナル．**新しいイテレーションを常に先頭へ挿入する（逆時系列）**．
1 イテレーション = 単一レバー変更．各ブロックに仮説・単一レバー・成功条件（planner 記入）と，
変更・結果・判定・学び（reflector 記入）をまとめる．

---

## Iteration 9

### 考察・次計画 (Iter9)

**担当**: 考察・次計画 subagent（rc-reflector，2026-07-20 JST）．`### 分析(解釈) (Iter9)` の判定（直列化点を rank14=wafl113 に
一意特定・基準 (i) が閾値で成立・整合チェック未達は計測被覆漏れで診断信頼性は毀損せず・「ノード起因 vs 層 23/46 起因」の
切り分けは未確定で追加反復 1 回で決着可能）を受け，Iter9 の単一レバーの採否と Iteration 10 の方向を reflector として確定した．
実機非接続（journal・backlog・state の読み書きと commit 操作のみ，`pipeline_inference.py` 非改変）．**逆時系列維持のため本ブロックを
Iteration 9 内の最上段に置く**．

**1. 採否判定: 採用（診断として成功）＝この計時レバーは収束（accepted-as-diagnostic / converged）**

- 本イテレーションのレバー（bench 経路 `_process_microbatch`/`_run_microbatch_bench` への 3 区間 per-microbatch 計時
  `recv_wait_s`/`compute_s`/`send_wait_s` ログ追加）は「レバーが効いたか」を測る感度実験ではなく，Iter7 の見かけの完全直列
  （`time_per_step ∝ m`）の**直列化点がどこか**を特定する診断実験である．その診断課題に対して **明確な結論が出た**ため
  「採用（診断として成功）」と判定する．具体的には全 51 rank×3 区間の分解で，直列化点＝律速ボトルネック段が **rank14
  （物理ノード wafl113，層 23，1 層割当）に一意に局在**した（基準 (i)：rank14 の compute/per-step=0.758 ≥ 0.60 が閾値で成立，
  3 repeat CV<0.1% で反転なし，約 55σ 級の外れ値）．「rank0〜13 は send_wait 支配（下流バックプレッシャ）→ rank14 は両側無待ちの
  compute 律速 → rank15〜50 は recv_wait 支配（上流飢餓）」という**単一ボトルネック段の教科書的署名**を得た．serving 経路は
  非改変（`self._bench_timing` は既定 `None` で計時コード非実行）で可逆．
- **診断としての収束**: 「直列化点はどこか」という Iter8 が積み残した未解決点は rank14 に一意特定できて決着した．この計時ログ
  自体は今後も診断資産として残るが，**この単一レバーでこれ以上得られる情報は無い**（直列化点は特定済み）ため収束させ，次は
  新たに立ち上がった未解決点（rank14/wafl113 が遅い理由＝ノード起因か層起因か）へレバーを移す．

**2. 非自明な学び（次の自分向け）**

- **(i) Iter7/Iter8 の見かけの矛盾は「単段 straggler による負荷不均衡」で解消（今回の最重要の学び）**: Iter7「ほぼ完全直列
  （含意 FF≈1/p）」× Iter8「blocking でも段が真並列なら FF=0.97 で fill する」の矛盾は，**実クラスタに単一の遅い段（rank14，
  per-mb compute 0.297s）が存在し全 microbatch がそこを直列通過するため，実効スループットが rank14 の compute×m で律速**される
  ことで説明できた．パイプライン自体は概ね fill しているが，単段 straggler で約 3 倍劣化し「あたかも完全直列」に見えていた．
  すなわち Iter7 の見かけの完全直列は，**通信構造（async 大改修 B14(b)）でも全層 compute（量子化）でもなく，段間の負荷不均衡
  （straggler ノード）が主因**という切り分けが得られた．これは Iter8 §1(iii) が想定した「rank0 直列生成・barrier 等のハードな
  同期点由来」という予想を**修正**する（同期点ではなく負荷不均衡だった）．
- **(ii) 調査の候補(a)「rank0 生成が直列元凶」は反証**: rank0 の recv_wait は 6.4e-5s（他 rank compute の 1/1450）で無視でき，
  rank0 が待つのは生成コストではなく下流バックプレッシャ（send_wait 支配 0.304s）だった．事前確度「低」の見立てと整合し，
  「作る前に測る／攻める前に直す点を特定する」診断系譜が誤った処方箋（rank0 生成の並列化）への投資を未然に防いだ．
- **(iii) 整合チェック 77%（95% 未達）は診断を毀損しない＝残差の一様性が鍵**: 残差（per-mb 0.392s − 3 区間合計）は**層数に
  依らず全群でほぼ一定**（rank0=0.0881s，1 層 rank 平均 0.0887s，2 層 rank 平均 0.0886s）で，KV キャッシュを持たない rank0 も
  同一残差を持つ．よって実験フェーズが主因候補とした `_reset_kv_cache_for_bench`（層数比例するはず）は残差主因では**ない**と
  否定でき，残差の正体は計時窓（t0〜t3）の外側の**全 rank 共通の per-step 定数オーバーヘッド**（measure ループの Python
  ステップ制御・tqdm・ステップ境界処理）と特定した．定数オフセットは rank 間の相対構造を変えないため直列化点の局在という
  結論に影響しない．**教訓: 整合率が閾値未達でも，残差が診断対象の軸（ここでは層数/rank 位置）に相関しなければ診断は成立する**．
  backlog 候補として「measure ループ全体を t_step で挟む 4 点目の計時を足し残差を per-step overhead 区間として明示回収する」を
  申し送る（B17 に記載，任意の将来課題）．
- **(iv) 2 番手 straggler（rank37=wafl136，層 46，2.30 倍・約 34σ）の存在**: rank37 の recv_wait が他の下流 rank より小さい
  （0.075s vs 0.20s）のは rank37 自身の compute が長く待ち時間を食っているためで，**rank14 を解消すると rank37 が次の律速に
  昇格する**と読める．負荷分散を処方する場合，単一ノードだけでなく straggler 群として扱う必要がある．
- **(v) 記録上の軽微な齟齬（結論に影響なし）**: analyst(実行) が (a) 2 層 rank の compute 絶対値レンジ表記（報告
  「0.201〜0.231s」対 実測「0.1988〜0.2323s」の丸め齟齬），(b) 基準 (i) の次点は報告の rank37（比 0.573）ではなく 2 層 rank9
  （比 0.586〜0.593）が 0.60 に僅かに近い（報告漏れ），の 2 点を検出した．いずれも整合チェック比率・rank14/37 の数値・診断基準
  の判定（rank14 のみ 0.60 超）には影響しない．次回以降，絶対値レンジ表記と次点の言及を正確にすること．

**3. Iteration 10 の方向決定: 示唆 (a)＝全 51 ノードで単層 local マイクロベンチ（SL1 型）を回し straggler ノードを特定（B17 に自動記録）**

- **決定**: 次イテレーションの単一レバーを **「全 51 ノードで単層 local マイクロベンチ（SL1 型・通信なし）を回し，各物理ノードの
  単層 compute 時間を直接ランキングして wafl113/wafl136 が突出するか確認する」**（analyst 示唆 (a)）とする．
- **なぜ (a) を選び (b) を見送るか（情報利得 × 可逆・低リスクの一貫方針）**: 未確定点が「rank14/wafl113（および rank37/wafl136）が
  遅い理由は**ノード起因（straggler）か，層 23/46 の構造的な重さか**」の二択に集約されている（層→rank 割当固定による交絡）．
  (a) は**全ノードで同一の単層ワークロード（層を固定）を走らせる**ため，差が出ればそれは純粋にノード起因と直接帰属でき，逆に
  wafl113/wafl136 が突出しなければ層起因（層 23/46 が重い）と切り分けられる——**二択を一度で決着させる最も情報利得の高い設計**．
  かつ通信を伴わない **SL1 型 local マイクロベンチ（Iter5 の系譜）で完全に可逆・低リスク**．一方 (b)（層→rank 割当のシャッフルで
  遅さがノードに追従するか層に追従するかを見る）は **deploy 側の割当変更を伴い実装規模がやや大きく**，同じ問いに答えるのに
  (a) より重い．過去の一貫方針（Iter5 の B8＝「大規模な relay 改修の前に near-zero コストの local マイクロベンチで先に測る」，
  Iter8 の「棄却の前に一次証拠を取る」）に従い，**可逆・低リスクで情報利得が高い (a) を優先**する．
- **可逆性の判断（自律判断ポリシーとの照合）**: (a) は通信なしの local マイクロベンチ（各ノードで単層 forward の実行時間を測る
  だけ）で，`pipeline_inference.py` の serving/relay ロジックも層割当も変更しない．コード変更は計測スクリプトの追加のみで可逆．
  51 ノードへの実行は SSH を伴うが**非破壊**（B7 の包括承認範囲内）．したがって **Iteration 10 の方向選定は可逆＝自動判断とし，
  B17 に記録**する（調査・計画・実装はコードのみで進め，実験の実機実行も B7 の範囲内）．具体的なベンチ設計（測定する層の選定・
  ウォームアップ・反復数・全ノード並列 SSH の収集経路）は次の rc-planner が決める．
- **処方箋への含意（次イテレーション以降）**: (a) で **straggler 起因と確定**すれば，処方箋は async 大改修（B14(b)）や全層量子化
  ではなく**負荷分散**（遅ノードへ層を減らす／遅ノードを除外して WORLD_SIZE を調整）に向かい，config `levers` の WORLD_SIZE 軸
  （削減は要ホスト健全性確認）と接続する．**層起因と確定**すれば当該層の compute 最適化（量子化・attention 実装）へ向かう．
  いずれも (a) の結果を見てから改めて単一レバーを立てる（今は二択の決着を優先し，処方箋レバーには踏み込まない）．
- **フォールバック / 温存レバー**: (a) の実装が予想外に過大と判明した場合は (b)（層割当シャッフル）へ振り替える．config `levers`
  （`STAGGER_INTERVAL`/`SEQ_LEN`/`WORLD_SIZE`）は下位フォールバックとして温存（B14(a)）．async ホットパス大改修（B14(b)）は
  真因が straggler と確定し，かつ async で解消可能と分かるまで着手しない（不可逆・大規模のため，妥当と判明した時点で改めて
  `[needs-human]` 登録＋Slack 確認）．
- **B9（B3 本体＝relay プロトコル改修＝SL3）との関係**: (a) は local 単層計測で relay プロトコルには一切触れず軸が直交．
  **B9 は今回も温存（`[needs-human]` 維持，reflector では自動判定しない）**．

**4. 要人間判断の有無**

- 本フェーズで新規の要人間判断（不可逆・破壊的判断）は発生していない．Iteration 10 の方向（全ノード単層 local マイクロベンチ）は
  通信なし・serving 非改変で可逆のため自動判断（B17）とした．B9 は従来どおり人間回答待ちで温存する．
- なお `results/Iter9.jsonl`（実験生データ 153 レコード）は今回のコミット対象に含めず**未追跡のまま残した**（タスク指定の
  コミット対象ファイル一覧に含まれないため）．診断の一次証拠として実機に紐づく生データであり，追跡要否は別途の判断に委ねる
  （本イテレーションの学びは journal に確定済みで，結論の再現に生データ commit は必須ではない）．
- git commit/push は本フェーズで実施した（下記コミットで journal・backlog・state・実装差分を確定）．

---

### 分析(解釈) (Iter9)

**担当**: 分析(解釈)フェーズ subagent（2026-07-20 JST）．`### 実験 (Iter9)` の一次事実と `### 分析(実行) (Iter9)` の独立検算
（数値は完全一致・軽微な丸め齟齬 1 件のみ）を前提に，`results/Iter9.jsonl`（153 レコード）を全 51 rank×3 区間で再集計し直し，
`### 検討・計画 (Iter9)` §6 の成功条件に照らして「直列化点を一意に特定できたか」を判定した．追加で `tools/deploy.py::get_assigned_layers`／
`hosts.txt`／`pipeline_inference.py::_reset_kv_cache_for_bench`（:978-1002）を Read で確認した．実機非接続（既存データの再解釈のみ）．
**逆時系列維持のため本ブロックを `### 分析(実行) (Iter9)` の上に置く．**

**1. 全 rank 3 区間分解で見えた構造（実験フェーズが §5 で提示しきれていなかった全体像）**

- 各 rank の 3 区間合計（recv_wait+compute+send_wait，3 repeat 平均）は**全 51 rank でほぼ一定（≈0.304s，per-mb）**．
  変動するのは「合計」ではなく「その 0.304s を recv待ち／compute／send待ち のどれに費やしているか」の内訳である．
  これは blocking 同期パイプラインで**全 rank の 1 ステップ周期が大域的にロックステップ同期している**ことの直接の署名．
- **支配区間が rank 位置で階段状に切り替わる**（per-mb 3 repeat 平均）:
  - rank0（wafl-ctrl1，0 層）: **send_wait 支配** 0.304s（recv=6.4e-5s，compute=4.2e-4s）＝下流が受信 post するまで送れない待ち．
  - rank1〜13（wafl100〜112）: compute（2 層 rank）または send_wait（1 層 rank11〜13，send≈0.20s）が支配．
  - **rank14（wafl113，1 層）: compute 支配 0.2973s，recv待ち・send待ち はともに ≈0（0.0017s／0.0003s）＝ステップ中ほぼ常時計算中で両側とも待たない．**
  - rank15〜50（wafl114〜wafl209）: **recv_wait 支配** ≈0.20s（compute≈0.095s，send≈0）＝上流が未送出で飢える待ち．
- この「rank0〜13 は send待ち（下流バックプレッシャ）→ rank14 は無待ちの compute 律速 → rank15〜50 は recv待ち（上流飢餓）」
  という**バックプレッシャと飢餓が rank14 の一点で交わる**パターンは，教科書的な「単一ボトルネック段」の署名そのものである．
  上流の余剰能力は send待ちに，下流の余剰能力は recv待ちに現れ，両者の境界（送信待ち→受信待ちの反転点）が rank14 に一致する
  ことで，**直列化点＝ボトルネック段が rank14（物理ノード wafl113）に一意に局在**すると読める．

**2. 「整合チェック 95% 未達（実測約 77%）」の解釈 — 計測漏れか，診断の信頼性毀損か（委譲元 問い 1）**

- **結論: 計測漏れ（per-step ループの被覆漏れ）であり，診断の信頼性は毀損しない．** 根拠は残差の分布にある．
- 残差（per-mb 時間 0.3924s − 3 区間合計）は**層数に依らずほぼ完全に一定**: 0 層 rank（rank0）=0.08814s，1 層 rank（40 台）=
  平均 0.08867s（0.08727〜0.09421），2 層 rank（10 台）=平均 0.08864s（0.08807〜0.08945）．**rank0 は KV キャッシュを一切持たない
  のに他 rank と同一の残差を持つ**．
- したがって実験フェーズが「主因候補」として挙げた `_reset_kv_cache_for_bench()` は残差 23% の主因では**ない**．同関数を Read で
  確認した通り，中身は `self.kv_cache` の各層 `key_cache.zero_()/value_cache.zero_()` と `_kv_cache_write_pos_ref` のローカル 0 代入
  のみ（跨 rank 同期なし・軽量）で，**層数に比例するはず**である．もし reset が残差主因なら 2 層 rank は 1 層 rank の約 2 倍，rank0 は
  ≈0 の残差になるはずだが，実測は全群で一定＝reset 起源ではないと否定できる（実験フェーズの「reset がどの区間にも計上されない構造」
  という観察自体は正しいが，その所要は残差の規模を説明しない）．
- 残差の正体は，`_process_microbatch` の計時窓（t0 受信前〜t3 送信後）の**外側**にある**全 rank 共通の per-step オーバーヘッド**
  （measure ループの Python ステップ制御・`_reset_kv_cache_for_bench` 呼び出し込みのステップ境界処理・tqdm・最後の mb 送信完了から
  次ステップ先頭 recv までの空隙）である．per-step 0.354s（=0.0886×4mb）が全 rank に等しく加算される定数オフセットにすぎず，
  **rank 間の相対構造（どの rank がどの区間で待つか）を一切変えない**ため，直列化点の局在という診断結論には影響しない．
- 整合チェックは「計時の被覆率」を測る健全性指標としては未達（77%）だが，これは「計時が per-microbatch 窓に限定され per-step 定数
  オーバーヘッドを取り漏らした」という**計測設計上の被覆漏れ**であって，収集値の信頼性や診断の一意性を損なうものではない
  （backlog 候補: measure ループ全体を t_step で挟む 4 点目の計時を足せば残差を明示的に per-step overhead 区間として回収できる）．

**3. 診断基準 (i)(ii)(iii) への当てはまり（委譲元 問い 2）**

- **(i) compute 支配（ある rank で compute_s ≥ per-step の 60%）: 成立．** rank14 の compute_mean_s/per-step = 0.2973/0.3924 = **0.758 ≥ 0.60**．
  かつ rank14 は recv/send 待ちが両側ほぼ 0 の**クリティカルパス上の段**であり，基準 (i) が捉えようとした「compute 律速の段が fill を
  阻む」機構と正確に一致する．基準 (i) は成立し，直列化機構を正しく指している．
- **(ii) recv_wait の rank 勾配（単調かつ ±5% 超）: 字義どおりには不成立だが，捉えたかった現象はより鋭い形で成立．** 観測は滑らかな
  単調勾配ではなく rank14→15 境界での約 127 倍の階段状ジャンプ（send待ち→recv待ちの支配反転）である．計画は「リング状の連続勾配」を
  想定していたが，実データは**単一ボトルネック段の前後で待ち種別が二値的に切り替わる**構造で，これは基準 (ii) が意図した「待ち分布が
  ボトルネックを局在させる」ことを字義以上に明瞭に満たす．計画の想定パターンとは異なるが「想定外で解釈不能」ではなく，
  **より単純な原因（単段ボトルネック）に対応する予期可能なパターン**として扱うのが妥当．
- **(iii) rank0 の recv_wait（`normal_()` 生成コスト）: 不成立＝候補(a)を反証．** rank0 の recv_wait_mean_s=6.4e-5s は他 rank の compute
  最小値の約 1/1450 で無視できる．ただし rank0 は**send_wait 支配**（0.304s）である＝rank0 が待つのは生成コストではなく下流への
  バックプレッシャであり，「rank0 の入力生成が直列ボトルネック」という Iter9 調査の候補(a)は明確に否定された（調査の事前確度「低」と整合）．
- **総括**: 診断は主に基準 (i)（rank14 の compute≥60%）で成立し，(ii) の待ちパターン（階段状反転）が補強証拠として rank14 を一意に指す．
  計画は「(i)(ii)(iii) のいずれか 1 つ成立で診断成功」としており，(i) が閾値で成立し 3 repeat（rank14 CV<0.1%）で判定が反転しない
  ことを確認済みのため，**成功条件を満たす**．

**4. rank14・rank37 異常値の解釈（委譲元 問い 2 の最重要手がかり）**

- rank↔ホスト対応（`hosts.txt` 行順＝rank 番号，`read_hosts`）: **rank14=wafl113，rank37=wafl136**（ともに wafl100-139 群，物理的に別ノード）．
  層割当（`get_assigned_layers`，layers_high=11）: **rank14→層 23，rank37→層 46**（ともに 1 層のみ，他の 1 層 rank と同種の通常 decoder 層）．
- 1 層 rank（rank11〜50，14・37 除く 38 台）の compute_mean_s は mean=0.09761s，sd=0.00361s，max=0.10668s と**極めて密に分布**（CV≈3.7%）．
  これに対し rank14=0.2973s は分布平均の **3.05 倍・約 55σ 相当**，rank37=0.2249s は **2.30 倍・約 34σ 相当**の外れ値で，
  3 repeat とも高精度に再現（rank14 CV<0.1%，rank37 CV<0.5%）．**単発ノイズでは断じてなく，明確な信号**（過去のノイズ幅 Iter7 CV0.07〜0.14%・
  Iter8 CV0.2〜1.5% と比べても桁違いの逸脱）．
- **層構造要因より「ノード性能ばらつき（straggler）」が有力**: Gemma-4 の decoder 層は全層で shape・FLOPs がほぼ同一で，層 23・46 が
  近傍層より 3.0×・2.3× 重くなる構造的理由は乏しい（sliding-window/global attention の別は seq_len=512 の bench では層あたり総コストを
  3 倍にはしない）．一方，CPU 推論（`docker stats` で CPU 192%）では周波数スケーリング・熱スロットリング・同居負荷等でノード単位の 2〜3 倍
  の遅速差は容易に生じ，1 デプロイセッション内で持続すれば高い再現性（CV<0.5%）とも整合する．よって **wafl113・wafl136 が遅い物理ノード
  （straggler）で，rank14 が現在の律速ボトルネック，rank37 は「rank14 の陰に隠れた 2 番手の straggler」**（rank37 の recv待ち 0.075s が他の
  下流 rank の 0.20s より小さいのは，rank37 自身の compute が長く待ち時間を食っているため＝rank14 を解消すると rank37 が次の律速になる）
  と解釈するのが最も整合的．**ただし「ノード起因」か「層 23/46 起因」かは本データだけでは確定できない**（層→rank 割当が固定のため交絡）．

**5. Iter7/Iter8 との接続（前回比・仮説整合）**

- Iter7「time_per_step ∝ m（含意 FF≈1/p のほぼ完全直列に見える）」× Iter8「blocking パイプラインは段が真並列なら FF=0.97 で fill する」
  の見かけの矛盾を，Iter9 は**負荷不均衡で説明して解消した**: 実クラスタには単一の遅い段（rank14）が存在し，全 microbatch が rank14 を
  直列通過するため throughput が rank14 の per-mb compute（0.297s）×m で律速される．per-step≈1.57s ≈ 4×0.297s(=1.19s)＋per-step overhead0.35s と
  概ね対応し，パイプライン自体は概ね fill しているが**単段 straggler により実効スループットが約 3 倍劣化**して「あたかも直列（FF≈1/p）」に
  見えていた，と読める．すなわち Iter7 の見かけの完全直列は**通信構造（async 大改修 B14(b)）でも全層 compute（量子化）でもなく，
  段間の負荷不均衡（straggler ノード）が主因**という切り分けができた．

**6. 総合判定（委譲元 問い 3，採否の確定は reflector に委ねる示唆として）**

- **単一レバー（bench 経路への 3 区間計時ログ追加）: 診断ツールとして機能した＝採用が妥当．** 全 rank×3 区間の分解により直列化点を
  一意に局在でき，調査の候補(a)（rank0 生成）を反証し，Iter7/8 の矛盾を負荷不均衡で解消した．serving 経路は非改変で可逆．
- **研究目的（直列化点の特定）に照らして: 診断成功（部分的成功ではない）．** 直列化点＝ボトルネック段は rank14（wafl113）に一意特定され，
  基準 (i) が閾値で成立し repeat 間で反転しない．整合チェック 77% 未達は「per-step 定数オーバーヘッドの計測被覆漏れ」であって診断の
  一意性・信頼性を毀損しないことを残差の一様性（rank0 含む全群一定）で立証した．
- **確信度**: 直列化点の局在（rank14 が律速ボトルネック）は高確信（52σ 級外れ値・CV<0.1%・待ちパターンの一致）．一方で**「ノード起因 vs 層 23/46 起因」
  の切り分けは未確定**（層→rank 割当固定による交絡）で，ここは**追加反復 1 回で決着可能**．
- **次イテレーションへの有力仮説と示唆（レバー案，収束/追加反復の別）**:
  - 有力仮説: 「wafl113・wafl136 が straggler ノードで，rank14 が律速」．対立仮説: 「層 23・46 が構造的に重い」．
  - 切り分けレバー候補（単一レバー原則）: (a) **全 51 ノードで単層 local マイクロベンチ（SL1 型・通信なし）**を回し，各物理ノードの単層 compute
    時間を直接ランキングして wafl113/wafl136 が突出するか確認（可逆・低リスク・ノード起因説を直接検証）．(b) 層→rank 割当をシャッフル
    （例 offset/shuffle）して**遅さがノードに追従するか層に追従するか**を見る（deploy 側の割当変更が必要）．
  - もし straggler 起因と確定すれば，処方箋は async 大改修や全層量子化ではなく**負荷分散（遅ノードへ層を減らす／遅ノードを除外して WORLD_SIZE を
    調整）**に向かう（config `levers` の WORLD_SIZE 軸・ノード健全性確認と接続）．層起因と確定すれば当該層の compute 最適化（量子化・attention 実装）へ．
  - **レバー収束はまだ早い**（真因の二択が未確定）．次は上記 (a) を最優先の追加反復として推奨する（reflector 判断）．
- git commit/push は本フェーズでは行っていない．

---

### 分析(実行) (Iter9)

**担当**: 分析(実行)フェーズ subagent（2026-07-20 JST）．`### 実験 (Iter9)` §5 が報告した集計値（一次事実として提示された
比率・境界・rank14/37 の異常値）を，`results/Iter9.jsonl` を Python で独立に再集計して検算した．実機非接続（再デプロイ・
再実験は行っていない，既存データの独立検算のみ）．

**1. 検算方法**

- `results/Iter9.jsonl` を Read／Python（`json.loads` を行ごとに適用）で直接読み込み，レコード件数・`rank`／`repeat`
  の網羅性（0〜50 × 0〜2 の 153 通りに過不足なく 1 件ずつ対応するか）・`n_samples`／`num_micro_batches`／`world_size`／
  `schema_version`／`record_type` の一様性を確認した後，実験フェーズと同じ定義式で独立に再計算した．
- **制約（申し送り）**: per-step-per-mb 時間の算出に使う `elapsed_s`（rank50 の `MICROBATCH_BENCH`＝スループット行）は
  `results/Iter9.jsonl` に**保存されていない**（本ファイルは `--microbatch-bench-timing` で収集した
  `microbatch_bench_timing` レコードのみを含み，`collect_results.py` は `MICROBATCH_BENCH`（スループット）を別収集経路
  として扱う設計．journal 該当ブロックの申し送りとも整合）．実機への再接続・再収集はこのフェーズの範囲外のため，
  `### 実験 (Iter9)` §5 に記載された `elapsed_s`（repeat0=157.0292，repeat1=157.0793，repeat2=156.7528）を**外部入力
  として採用**し，そこから導出される比率・閾値判定のみを独立に再計算した．`elapsed_s` 自体の生ログ照合は行っていない
  （次回コミット等で `MICROBATCH_BENCH` 行も `results/Iter9.jsonl` へ保存する収集経路を追加すれば，この外部依存は解消
  できる＝ backlog 候補として申し送る）．
- 検算項目: (a) 全 153 レコードでの整合チェック比率（`(recv_wait_mean_s+compute_mean_s+send_wait_mean_s)/per_step`）の
  repeat 別 min/max/mean，(b) rank14・rank37 の `compute_mean_s` と他 1 層 rank との比，(c) `recv_wait_mean_s` の
  rank 方向の並び（rank14→15，rank36→37→38，rank0→1 の各境界），(d) 診断基準 (i)〜(iii) の再判定，(e) 層割当
  （`get_assigned_layers`，`tools/deploy.py:228-248`）の式をコード読解で直接確認．

**2. 検算結果**

- **(a) 整合チェック比率**: repeat0: min=0.7603, max=0.7788, mean=0.7748／repeat1: min=0.7602, max=0.7769,
  mean=0.7737／repeat2: min=0.7592, max=0.7780, mean=0.7737／全 153 件通算: min=0.7592, max=0.7788, mean=0.7741．
  **報告値と完全一致**（小数第 4 位まで一致，通算 mean≈0.774 も一致）．per-step-per-mb 時間（0.392573s／0.392698s／
  0.391882s）も一致．
- **(b) rank14・rank37 の `compute_mean_s`**: rank14 は 3 repeat で 0.297174〜0.297550s（CV=0.058%，「CV<0.1%」と整合），
  比は 0.7567〜0.7584（報告の repeat0 比 0.7579 と一致）．rank37 は 0.224399〜0.225394s（CV=0.183%，「CV<0.5%」と整合，
  報告の下限・上限値 0.224399s／0.225394s も完全一致），比は 0.5714〜0.5752（報告の repeat0 比 0.5732 と一致）．
  1 層 rank（11〜50，rank14・37 除く）の `compute_mean_s`（3 repeat 平均）は min=0.093517s, max=0.106676s
  （報告「0.09〜0.11s」と整合）で，rank14 はその平均（0.097608s）比 **3.046 倍**，rank37 は **2.305 倍**（報告の
  「約 3.0 倍・2.3 倍」と一致）．
  - **軽微な不一致を 1 件検出**: 2 層 rank（rank1〜10）の `compute_mean_s` の実際のレンジは **0.198798s〜0.232286s**
    （3 repeat×10 rank＝30 件の最小・最大）であり，報告値「0.201〜0.231s」とは下限・上限とも約 0.001〜0.002s
    ずれている（最小値は rank10 repeat1 の 0.198798s，最大値は rank9 repeat2 の 0.232286s）．比率換算（0.5062〜0.5927，
    報告「0.51〜0.59」）は丸めれば整合する．絶対値レンジのみの表記誤差であり，整合チェック比率・rank14/37 の数値・
    診断基準の判定には影響しない．
- **(c) `recv_wait_mean_s` の rank 方向の並び**: rank14→15 の境界ジャンプは repeat0: 0.001620s→0.206308s（倍率 127.35
  倍），repeat1: 0.001667s→0.205946s（123.54 倍），repeat2: 0.001711s→0.207446s（121.24 倍）で，**報告の
  「rank14→15 で 0.001620s→0.206308s へ約 127 倍」は repeat0 の値として完全一致**．rank36→37→38 の非単調性も
  3 repeat とも再現（例 repeat0: 0.205095s→0.075522s→0.201133s，報告値と完全一致）．rank0→1（repeat0:
  0.000064s→0.001688s）も完全一致．**全て報告どおりデータに存在することを確認**．
- **(d) 診断基準の再判定**: (i) 全 153 件中，`compute_mean_s/per_step ≥ 0.60` を満たすのは rank14 の 3 repeat
  （0.7567〜0.7584）のみで確認．次点は rank9（2 層 rank，0.5856〜0.5927）で rank37（0.5714〜0.5752）より僅かに
  0.60 に近いが，いずれも未達（報告は rank37 のみ言及し rank9 には触れていないが，矛盾ではなく報告漏れの可能性がある
  追加事実として申し送る）。(ii) recv_wait_s は単調勾配ではなく rank14/15 境界・rank36/37/38 周辺の階段状変化として
  確認，報告と一致．(iii) rank0 の `recv_wait_mean_s`（0.000063〜0.000064s）は他 rank の `compute_mean_s` 最小値
  （rank39 等 約 0.093056s）の約 1/1453 で「1,000 分の 1 以下」と整合，不成立の判定も一致．
- **(e) 層割当の式**: `tools/deploy.py::get_assigned_layers`（:228-248）を Read で確認し，`layers_high = total_layers -
  world_size + 2` で `total_layers=60, world_size=51` のとき `layers_high=11`，rank<11（rank1〜10）は 2 層
  `[(rank-1)*2, (rank-1)*2+1]`，rank≥11（rank11〜50）は 1 層，という報告の記述と完全に一致することをコードで確認した．

**3. 結論（数値の一致・不一致のみ，良否判定は行わない）**

- 報告された主要数値（整合チェック比率の min/max/mean，per-step-per-mb 時間，rank14/37 の `compute_mean_s` と比率・CV，
  recv_wait_mean_s の境界ジャンプ，診断基準 (i)〜(iii) の判定，層割当の式）は**独立再計算・コード確認いずれも完全一致**．
- **軽微な不一致 1 件**: rank1〜10（2 層 rank）の `compute_mean_s` 絶対値レンジの表記が「0.201〜0.231s」（報告）に対し
  実際は「0.198798〜0.232286s」（検算）で，下限・上限とも約 0.001〜0.002s のずれがある（比率換算では整合）．
  過去イテレーションで見られた「標準偏差表記の軽微な齟齬」と同種の丸め・記述レベルの齟齬であり，主要な結論（整合
  チェック未達・診断基準の判定）には影響しない．
- **追加で気づいた事実**: 診断基準 (i) の次点は，報告が言及した rank37（比 0.5714〜0.5752）ではなく，2 層 rank である
  rank9（比 0.5856〜0.5927）の方が 0.60 の閾値に近い．報告はこの事実に触れていないが，矛盾する記載ではなく，1 層
  rank に限定した記述（rank37 の言及箇所は「同じ 1 層割当」の文脈）だった可能性がある．機構的な解釈・重要性の判断は
  次フェーズ（分析(解釈)）に委ねる．
- `results/Iter9.jsonl` 自体には `elapsed_s`（per-step-per-mb 時間の算出根拠）が保存されておらず，本検算はこの値を
  実験フェーズの報告どおり外部入力として採用した．生ログでの `elapsed_s` 照合は未実施（次回以降，`collect_results.py`
  の収集経路に `MICROBATCH_BENCH`（スループット）行も同一 `results/Iter{n}.jsonl` へ保存する拡張を行えば，この外部
  依存なしに完全な再現ができるようになる．backlog 候補として申し送る）．
- git commit/push はこのフェーズでは行っていない．

---

### 実験 (Iter9)

**担当**: 実験フェーズ subagent（2026-07-20T13:36〜13:54 JST，約 18 分）．`### 実装 (Iter9)` §5 の申し送りに従い，
実機 51 ノードクラスタ（master wafl-ctrl1 + worker wafl100-139/200-209）へ deploy → パイロット → 本掃引 →
収集 → クラスタ復元まで**完走した**．固定構成（`NUM_MICRO_BATCHES=4`・`WORLD_SIZE=51`・`STAGGER_INTERVAL`/
`SEQ_LEN` 既定）は変更していない．事前に `unset VIRTUAL_ENV && uv run pytest tests/ -q` で **123 passed**
（Iter9 実装フェーズ完了時点と同数，回帰なし）を確認済み．

**1. 事前ヘルスチェック**

- `uv run python tools/healthcheck.py` で **51/51 healthy** を確認してから着手．

**2. パイロット（`MICROBATCH_BENCH_STEPS=5, MICROBATCH_BENCH_WARMUP_STEPS=2, MICROBATCH_BENCH_REPEATS=1`）**

- deploy 51/51 成功（52.77 秒）．
- 全 51 rank が `MICROBATCH_BENCH_TIMING`（`n_samples=20`＝measure5×m4）を出力し，クラッシュ・Traceback・
  ハングは皆無（`tools/show_logs.py --all` の全ログを grep 確認）．最終 rank の `MICROBATCH_BENCH`（スループット行）
  も `elapsed_s=8.3937s`（測定 5 ステップ）で正常出力．1 ステップあたり約 1.68s/step と見積もり，本掃引
  （measure=100, warmup=20, repeats=3 → 総ステップ 360）の所要時間を **約 10 分**と概算した．

**3. 本掃引（`MICROBATCH_BENCH_STEPS=100`，warmup/repeats は既定 20/3 のまま）**

- deploy 51/51 成功（44.53 秒，13:39:51 UTC 開始）．
- rank50（最終段）の `MICROBATCH_BENCH`（スループット）行: repeat0 `elapsed_s=157.0292`，repeat1
  `elapsed_s=157.0793`，repeat2 `elapsed_s=156.7528`（いずれも measure=100，warmup=20，3 repeat 完走）．
  `docker stats` で CPU 192%・メモリ 9.06GiB/15GiB を確認し，ハングではなく計算が進行中であることを確認した．
- 待機中は `poll_interval_sec`（60s）目安で SSH 経由 `docker logs --tail` をポーリングし，そのたびに
  `state.json.updated_at` を更新（heartbeat）．
- 完了後 `uv run python tools/healthcheck.py` で **51/51 healthy** を再確認．クラッシュ・エラーは一切観測されなかった
  （`tools/show_logs.py --all` の全 1734 行を `traceback|exception|fatal|crash`（大小文字無視）で grep して 0 件）．

**4. 収集（`tools/collect_results.py --iter Iter9 --microbatch-bench-timing`）**

- `[INFO] appended 153 MICROBATCH_BENCH_TIMING record(s) to results/Iter9.jsonl`．
  153 = 51 rank × 3 repeat（過不足なし，欠損 rank・欠損 repeat なし）．各レコード `n_samples=400`
  （measure100×m4）で統一．`results/Iter9.jsonl` は本フェーズで新規作成（既存ファイルなし）．

**5. 完了条件（`### 検討・計画 (Iter9)` §6）との対比（数値提示のみ，良否判定は analyst に委ねる）**

- **必須完了条件**: 達成（全 51 rank が `MICROBATCH_BENCH_TIMING` を出力し，`results/Iter9.jsonl` へ per-rank
  レコードとして保存済み）．
- **整合チェック**（`recv_wait_mean_s+compute_mean_s+send_wait_mean_s` が per-step の 1-mb あたり時間の 95% 以上を
  説明するか）: **未達**．per-step-per-mb 時間 = `elapsed_s/(measure×m)` は repeat0=0.392573s，repeat1=0.392698s，
  repeat2=0.391882s．各 rank の 3 区間合計との比（ratio）は repeat0: 全 51 rank で **min=0.7603, max=0.7788,
  mean=0.7748**，repeat1: min=0.7602, max=0.7769, mean=0.7737，repeat2: min=0.7592, max=0.7780, mean=0.7737
  （全 3 repeat・全 51 rank を通じて 0.95 に届く rank は 0 件）．つまり 3 区間の合計は per-step 時間の
  約 77%（残差約 23%）しか説明していない．コード読解で確認した事実として，`_run_microbatch_bench` の
  measure ループは各ステップ冒頭で `self._reset_kv_cache_for_bench()` を呼んでから `step_start_time` を
  取得し `_process_microbatch` の 3 区間打刻（t0〜t3）を開始する構造であり，**この reset 呼び出し自体の所要時間は
  どの 3 区間にも計上されない**（`elapsed`（全体計測）には含まれるが，rank ごとの `recv/compute/send` 配列には
  含まれない）．残差の原因がこの reset コストか，Python ループ呼び出しオーバーヘッドか，他の要因かは本フェーズ
  では特定していない（事実の提示のみ，原因分析は analyst に委ねる）．
- **診断成功の判定基準（3 分岐，事実の提示のみ）**:
  - **(i) compute 支配（ある rank で `compute_s ≥ per-step時間の60%`）**: repeat0 の per-step 時間 0.392573s の
    60% = 0.235544s に対し，**rank14 の `compute_mean_s=0.297550s`（比 0.7579）が唯一この閾値を超えた**
    （他の 1-layer rank は概ね 0.09〜0.11s，比 0.23〜0.28 程度）．rank37 も `compute_mean_s=0.225024s`（比 0.5732）
    で近いが 0.60 未満．rank1〜10（2-layer rank，`get_assigned_layers` で rank<11 は 2 層割当）は
    `compute_mean_s≈0.201〜0.231s`（比 0.51〜0.59）で全て 0.60 未満．3 repeat とも rank14 の `compute_mean_s` は
    0.297174〜0.297550s（CV<0.1%）で高精度に再現し，rank37 も 0.224399〜0.225394s で再現した．
  - **(ii) recv_wait_s の rank 勾配（隣接rank間で単調かつ±5%超で増減）**: 観測されたのは滑らかな単調勾配ではなく，
    **rank14→15 の境界で `recv_wait_mean_s` が 0.001620s→0.206308s へ約 127 倍に階段状に跳ぶ**（以降 rank15〜50 は
    概ね 0.075〜0.22s のレンジで推移し，rank36→37 で 0.205095s→0.075522s に低下，rank37→38 で 0.075522s→0.201133s
    に再上昇する非単調な例外を1 箇所含む）．rank0→1 も 0.000064s→0.001688s（rank0 は生成のみでこの区間の意味が
    他 rank と異なる）．いずれも「隣接 rank 間の滑らかな単調勾配」ではなく，特定 rank（14, 37）を境にした階段状の
    パターンとして観測された．
  - **(iii) rank0 の `recv_wait_s`（`normal_()` 生成コスト）が他 rank の `compute_s` と同等以上**: **不成立**．
    rank0 の `recv_wait_mean_s`（3 repeat 平均で 0.000063〜0.000064s）は他 rank の `compute_mean_s`
    （最小でも rank39 等の約 0.093〜0.095s）の 1,000 分の 1 以下で，明確に小さい．
- **付随して判明した事実（layer 割当との対応）**: `tools/deploy.py::get_assigned_layers` は `layers_high=60-51+2=11`
  で rank1〜10 に 2 層，rank11〜50 に 1 層を割り当てる．rank1〜10 の `compute_mean_s`（0.201〜0.231s）は
  rank11〜50 の大多数（0.093〜0.111s，ともに 1 層）のおよそ 2 倍で，層数比とおおむね対応する。ただし
  **rank14（`compute_mean_s≈0.2975s`）と rank37（`compute_mean_s≈0.2250s`）は，同じ 1 層割当にもかかわらず，
  他の 1 層 rank（0.09〜0.11s）よりそれぞれ約 3.0 倍・2.3 倍高い**．この 2 rank の異常値は 3 repeat すべてで
  高精度に再現しており（CV<0.5%），単発ノイズではない．

**6. 分析フェーズへの申し送り**

- 生データは `results/Iter9.jsonl`（153 レコード，`record_type="microbatch_bench_timing"`，`schema_version=2`）．
  上記 §5 の集計値（比・境界・rank14/37 の異常値）は本フェーズが `results/Iter9.jsonl` を Python で直接集計した
  一次事実であり，機構的な解釈（なぜ rank14/37 が遅いか，23% 残差の正体，診断基準への最終的な当てはめ）は行って
  いない．analyst には次を申し送る: (a) 整合チェック 95% 未達（実測約 77%）という完了条件の一部未達，
  (b) 診断基準 (i) は rank14 のみで成立（rank37 は僅かに未達），基準 (ii) は「単調勾配」ではなく「rank14/15 境界
  および rank36/37/38 周辺での階段状変化」，基準 (iii) は不成立，という 3 基準それぞれの事実関係，
  (c) rank14・rank37 の compute_mean_s 異常値は layer 数（1 層で他と同一）では説明されない再現性の高い外れ値で
  あること．
- git commit/push はこのフェーズでは行っていない．

**7. クラスタの状態**

- bench 完了・収集後，env 未設定（bench 無効・`NUM_MICRO_BATCHES=4` 既定）で全 51 ノードを再 deploy
  （44.92 秒，51/51 成功）し，`docker inspect`（rank0・rank50 で確認）で `NUM_MICRO_BATCHES=4` のみが設定され
  `MICROBATCH_BENCH_*` 系 env が一切無いことを確認，`healthcheck.py` で 51/51 healthy を確認済み．**健全な
  serving 状態に復元済み**．

---

### 実装 (Iter9)

**担当**: 実装フェーズ subagent（2026-07-20 JST，セッション再開）．`### 検討・計画 (Iter9)` §4 の設計に対し，
前回セッションが `pipeline_inference.py`／`tools/collect_results.py` の変更まで完了させた状態から再開し，
残っていた単体テスト追加のみを実施した．実機非接続（journal・コード読解・pytest 実行のみ．`mise run deploy`／
`predict:demo` は未実行）．**逆時系列維持のため本ブロックを `### 検討・計画 (Iter9)` の上に置く**．

**1. 前回セッション分の内容確認（本フェーズでは変更していない）**

- `pipeline_inference.py`: `git diff` で確認し，計画 §4 の設計（`__init__` での `self._bench_timing: dict[str,
  list[float]] | None = None` 初期化，`_process_microbatch` での 3 区間 [A]recv_wait_s/[B]compute_s/[C]send_wait_s
  の加算的計時，`_run_microbatch_bench` の warmup 中 `None` ガード・measure ループ直前の空リスト辞書セット・
  measure ループ直後の**全 rank**による `MICROBATCH_BENCH_TIMING` RESULT 行出力）と完全に一致していることを確認．
  `_relay_active` の早期 return・recv 例外の早期 return では打刻しない設計も計画どおり．serving/非 bench 経路
  （`self._bench_timing` が既定 `None`）では `time.monotonic()` を一切呼ばず挙動不変．
- `tools/collect_results.py`: `_MICROBATCH_BENCH_TIMING_RE`・`MicrobatchBenchTimingRecord`・
  `parse_microbatch_bench_timing_log`・`build_microbatch_bench_timing_record`・`collect_all_rank_logs`
  （`collect_worker_stage_timing_logs` と同型の全 rank 並列 SSH 取得，rank0 も含む）・
  `run_microbatch_bench_timing_collect`・CLI `--microbatch-bench-timing` フラグを確認し，計画 §4 の設計と一致．
  既存 `MICROBATCH_BENCH`（最終 rank のみ・スループット）収集は非改変．

**2. 本フェーズで実施した変更**

- **`tests/test_collect_results.py`**: `parse_microbatch_bench_timing_log`／`build_microbatch_bench_timing_record`
  の単体テストを 7 件追加（既存の `parse_microbatch_bench_log`／`build_microbatch_bench_record`（Iter7 相当，
  `tests/test_microbatch_bench.py`）と同じ書き方に合わせた）。
  - 正常系: 全 rank・全 repeat（rank=0 repeat=0,1／rank=50 repeat=0）を出現順に抽出すること，ANSI カラーコード
    混入行・非混入行の両方を正しく数値化すること．
  - 境界: 最終 rank（rank=50）は `send_wait_*` が 0（次段が無いため）で他 rank と区別されること．
  - 異常系: 該当行が無いログでは空リストを返すこと，既存 `MICROBATCH_BENCH`（スループットのみ）行は別の正規表現
    のため抽出されないこと（型混同がないことの確認）．
  - `build_microbatch_bench_timing_record`: `record_type="microbatch_bench_timing"` を含む全フィールドが
    過不足なく組み立てられ，JSON シリアライズ可能であること．
  - 実 SSH・実通信は一切使わず，`tests/fixtures/microbatch_bench_timing_sample.log`（固定ログ）のみを入力とする
    純関数テスト．`collect_all_rank_logs`／`run_microbatch_bench_timing_collect`（SSH 収集を伴う経路）は，既存の
    `collect_worker_stage_timing_logs`／`run_microbatch_bench_collect` 同様このリポジトリに単体テストが無い方針
    （SSH 実行を伴うため）と整合させ，今回も対象外とした．
- **`tests/fixtures/microbatch_bench_timing_sample.log`（既存ファイルの内容修正）**: 前回セッションが作成した
  フィクスチャに ANSI エスケープシーケンスの実バイト（`\x1b`）が欠落しており（`[0;32m` が literal text のまま
  ESC 文字なしで書かれていた），`_ANSI_RE`（`\x1b\[[0-9;]*m` のみに一致）が全く除去できず該当行がパースされない
  不具合を発見した．実際の bench 出力（`_Color` クラス，ANSI カラー付き）を模すため，Python で `\x1b` を明示的に
  埋め込み再生成した（`tests/fixtures/microbatch_bench_sample.log`（Iter7 作成分）の実バイトパターンと突き合わせて
  確認済み）．
- **`tests/test_pipeline_microbatch_bugfix.py`（既存回帰テストの最小修正・1 行追加）**: フルスイート実行で
  `AttributeError: 'FullyOptimizedPipelineNode' object has no attribute '_bench_timing'` が 4 件発生した．
  原因は `_build_single_node`（`object.__new__` で `__init__` を経由せず最小構成インスタンスを組み立てるテスト
  ヘルパー，Iter7 由来）が，Iter9 で `__init__` に追加された `self._bench_timing = None` の初期化を経由しないため．
  `_process_microbatch` の `timing = self._bench_timing` 参照で属性エラーになっていた．`_build_single_node` に
  `node._bench_timing = None` を 1 行追加し（他の手動設定属性 `node.config`／`node.kv_cache` 等と同じパターン），
  `__init__` の既定値と揃えた．**serving/非 bench 経路の挙動自体は変えておらず，テストヘルパーが `__init__` の
  新しい既定属性に追従していなかった不整合の修正**（本体ロジックの変更ではない）．

**3. 検証結果**

- `unset VIRTUAL_ENV && uv run pytest tests/test_collect_results.py -q` → **52 passed**（既存 45 ＋新規 7）．
- `unset VIRTUAL_ENV && uv run pytest tests/ -q` → **123 passed**（Iter8 完了時点の 116 ＋新規 7，回帰なし）．
  変更前（`git stash` で本セッションの全差分を退避した状態，HEAD=`4e01490`）でも同一コマンドで **116 passed** と
  なることを確認済みで，新規テストが正しく既存 116 件に対して非破壊であることを確認した．
- 型チェッカー・リンタ: `pyproject.toml`／`mise.toml` を確認したが，mypy／ruff 等の設定・タスクは本リポジトリに
  存在しない（Iter7/Iter8 の実装フェーズでも同様に未実行）．そのため今回も実行していない．
- serving 経路への非影響確認: `_process_microbatch`／`_run_microbatch_bench` の計時コードは
  `self._bench_timing is not None` のガード内でのみ動作し，serving（bench 無効時，`self._bench_timing` は既定
  `None`）では `time.monotonic()` を一切呼ばない設計（前回セッションの実装をそのまま踏襲，本フェーズでは
  `pipeline_inference.py` 本体を変更していない）．`tests/test_pipeline_microbatch_bugfix.py` の既存 4 件
  （バグ A／B の回帰テスト，単一ノード・通信バイパス構成）が引き続き全件成功することでも間接的に確認した．

**4. オーケストレータによる追補（B16）**

- 上記テスト追加後，`tests/fixtures/microbatch_bench_timing_sample.log` が `.gitignore:35` の `*.log` に一致し
  git 管理外（untracked）のままであることが判明した．さらに調べると，**Iter7 で追加された既存フィクスチャ
  `tests/fixtures/microbatch_bench_sample.log` も同じ理由で一度も commit されていなかった**（`git log --all` が
  空）．クリーンチェックアウトでは `test_microbatch_bench.py`／今回追加分ともにフィクスチャ欠如で失敗する状態
  だったことになる．`.gitignore` に `!tests/fixtures/*.log` の例外を追加し，両フィクスチャを `git add -f` で
  追跡対象にした（backlog B16，可逆な自動判断として記録）．本体ロジックには触れていない．

**4. 完了条件（`### 検討・計画 (Iter9)` §6）との対比**

- 実装フェーズ完了条件（コードとテストの整合）: 済．計画 §4 の全項目（`pipeline_inference.py`・
  `tools/collect_results.py`・`tests/test_collect_results.py` 等）を実装済みで整合を確認．
- 実機での完了条件（bench 実行→全 rank が `MICROBATCH_BENCH_TIMING` を出力→`results/Iter9.jsonl` へ保存）は
  実験フェーズの役割であり，本フェーズでは未実施（実機 deploy/predict は行っていない）．

**5. 次フェーズへの申し送り**

- 実験フェーズは `mise run deploy` 後，`MICROBATCH_BENCH_STEPS>0` を設定した状態で `predict:demo` 相当を実行し，
  その後 `uv run python tools/collect_results.py --iter Iter9 --microbatch-bench-timing` で全 rank の
  `MICROBATCH_BENCH_TIMING` を収集する（コマンド例は `tools/collect_results.py` docstring 参照）．
- 発見した 2 件の周辺不整合（フィクスチャの ANSI バイト欠落／テストヘルパーの新規属性未追従）はいずれも
  「計画の単一レバー」自体の変更ではなく，その単一レバーを正しくテストするための整合性修正であり，
  serving/bench 経路のロジック・既存テストの意図は変えていない．
- なお `tests/fixtures/*.log` は `.gitignore` の `*.log` パターンに一致するため，`microbatch_bench_sample.log`
  （Iter7 作成）・`microbatch_bench_timing_sample.log`（本イテレーション）とも **git 管理外（untracked）**の
  ままである．`git status`／`git ls-files` で確認済み．次回コミット時にこのままでは両フィクスチャが commit
  対象に含まれず，クリーンな checkout では `tests/test_microbatch_bench.py`／本フェーズで追加したテストが
  フィクスチャ欠如で失敗する．`.gitignore` の変更は本イテレーションの単一レバー（計時ログ追加）の範囲外のため
  実装フェーズでは変更していない．コミット時に `git add -f` で明示的に追跡するか，`.gitignore` に
  `!tests/fixtures/*.log` の例外を追加するかの判断を委譲先（コミットを行うフェーズ）に委ねる．

---

### 検討・計画 (Iter9)

**担当**: 検討・計画フェーズ subagent（rc-planner，2026-07-20 JST）．B15（自動選定）と `### 調査 (Iter9)` の示唆を
実装可能な最小差分まで具体化し，本イテレーションの単一レバー・変更方針・成功条件を確定した．実機非接続
（journal・backlog・config・`pipeline_inference.py`:960-1279・`tools/collect_results.py` の Read のみ，コード非改変）．
**逆時系列維持のため本ブロックを `### 調査 (Iter9)` の上に置く**．B15 で方向は決定済みのため方向転換はせず，実装が
過大にならない最小設計に絞る．

**1. 仮説**

- Iter8 で「blocking `recv→compute→send` は段が真並列なら本来ほぼ完全に fill する（FF=0.9716，129σ）」が確定し，
  Iter7 実機の `time_per_step ∝ m`（含意 FF≈1/p でほぼ完全直列）は blocking 通信構造では説明できず**別のハードな
  同期点由来**と切り分けられた．静的読解（調査）で候補(b)`_reset_kv_cache_for_bench` 同期・(c)per-mb barrier は
  既に排除済み（measure ループ内に `dist.barrier()` は無く，リセットは跨 rank 同期を含まないローカル `zero_()`）．
- したがって残る主容疑は各 rank の **recv 待ち／send 待ちの分布** か **compute 支配**（F1: 実機 ITL は compute≈92%）
  のいずれか，および候補(a)rank0 の `normal_()` 生成直列である．**per-microbatch を「recv_wait_s / compute_s /
  send_wait_s」の 3 区間に分解して全 rank で持ち寄れば，どの区間が per-step 時間を支配するかで直列化点を一意に
  切り分けられる**（StageFrontier「単一 rank のタイマー最大値では真因を特定できず，compute と wait を分離して
  rank 間で相関を取る必要がある」）．

**2. 単一レバー（今回変更する唯一のもの）**

- **bench 経路（`pipeline_inference.py` の `_process_microbatch` / `_run_microbatch_bench`）への
  per-microbatch 3 区間計時（recv_wait_s / compute_s / send_wait_s）の加算的計測ログ追加**．
  何を: 現状「最終 rank のみが計測窓ごとに `MICROBATCH_BENCH`（スループットのみ）を出力」→
  「**全 rank が計測窓ごとに `MICROBATCH_BENCH_TIMING`（3 区間の mean/min/max）を追加出力**」へ変える．
- serving/relay ロジック・計算結果・既存 `MICROBATCH_BENCH` 行は一切変更しない（読み取り専用の加算的計測，
  Iter3/P1・Iter4/B7 の INFO ログ追加と同種で graph-break リスク低・可逆）．

**3. 固定する構成（単一レバー原則）**

- config `levers` は直近最良構成に固定: `NUM_MICRO_BATCHES=4`（既定）・`WORLD_SIZE=51`・`STAGGER_INTERVAL`/`SEQ_LEN`
  は既定．bench の実行パラメータ（`MICROBATCH_BENCH_STEPS` の warmup/measure/repeats）は Iter7/Iter8 の判定点と
  同等に固定し，計時ログ追加以外は変えない．relay プロトコル（B9/SL3）には触れない（軸直交・実装衝突なし，B9 は
  `[needs-human]` 温存）．

**4. 変更方針（最小差分・変更すべきファイルと設定キー）**

- **`pipeline_inference.py::_process_microbatch`（:997-1050）**: `self._bench_timing`（既定 `None`）が非 `None` の
  ときのみ `time.monotonic()` で 3 点打刻し 3 区間を append する．
  - t0（:1019 直前）→ [A] rank0 は `normal_()`（:1021）／他 rank は `dist.recv`（:1024）→ t1 = **recv_wait_s**
    （rank0 では生成コスト＝候補(a)の実測）．
  - [B] compute（layers＋`send_buffers.copy_`，:1030-1036）→ t2 = **compute_s**．
  - [C] `dist.send`（:1048）／最終 rank は pbar 分岐（bench では実質 no-op）→ t3 = **send_wait_s**．
  - recv 例外の早期 return（:1027）や `_relay_active` return（:1017）の経路では append しない（bench では relay 非活性
    のため通常は通らない）．`self._bench_timing is None` の serving/非 bench 経路は打刻ゼロで挙動完全不変．
- **`pipeline_inference.py::_run_microbatch_bench`（:1221-1275）**: 各 repeat の **warmup 中は `self._bench_timing=None`**
  （計測窓外は打刻しない），measure ループ直前（:1252 付近）に `self._bench_timing={"recv":[],"compute":[],"send":[]}` を
  セット→ measure ループで累積→ measure ループ直後（:1265 付近）に **全 rank が**（`is_last_node` ゲートを付けず）
  `[R{rank} RESULT] MICROBATCH_BENCH_TIMING m=.. p=.. rank=.. repeat=.. n_samples=.. recv_wait_mean_s=.. recv_wait_min_s=..
  recv_wait_max_s=.. compute_mean_s=.. compute_min_s=.. compute_max_s=.. send_wait_mean_s=.. send_wait_min_s=..
  send_wait_max_s=..` を 1 行出力（既存 `MICROBATCH_BENCH` の key=value 形式を踏襲）．計測擾乱回避のため
  **per-mb ループ内に `dist.barrier()` を追加しない**（調査 §Q2「barrier 自体が fill を壊す」）．per-mb 生値は
  stdout に吐かず measure 窓で集約する．`self._bench_timing` は `__init__` でインスタンス属性 `None` として初期化する．
- **`tools/collect_results.py`**: (i) `_MICROBATCH_BENCH_TIMING_RE` と `parse_microbatch_bench_timing_log`（純関数）を
  追加，(ii) `record_type="microbatch_bench_timing"` のレコードビルダ（`schema_version=2` 空間，rank/repeat/3 区間 stat
  を保持）を追加，(iii) 計時行は**全 rank が出す**ため，現状 `collect_last_rank_log`（最終 rank のみ，:919）ではなく
  `collect_worker_stage_timing_logs`（`--stage-timing` の全 rank 並列 SSH 取得経路，:836）と同型で全 rank の docker logs を
  集めてパースする収集関数を追加する（`run_microbatch_bench_collect`:954 を拡張 or 兄弟関数）．既存
  `MICROBATCH_BENCH`（スループット）収集は非改変・後方互換を保つ．
- **`tests/test_collect_results.py` 等**: 新規 `MICROBATCH_BENCH_TIMING` パーサの正常・境界・複数 rank / 複数 repeat の
  単体テストを追加（実 SSH・実通信なしで純関数として検証．Iter4/Iter8 のテスト方針と整合）．

**5. 期待効果**

- 全 rank × repeat の 3 区間 stat が得られ，(i) compute_s 支配なら fill 不成立の主因は計算量＝**量子化軸（示唆B）へ**，
  (ii) recv_wait_s が rank 方向に単調勾配なら **Gloo p2p のリング状バックプレッシャ**，(iii) rank0 の recv_wait_s
  （`normal_()` 区間）が想定外に大きければ **候補(a)を初めて実証**，と Iter7 のほぼ完全直列の一次証拠に基づく切り分けが
  できる．これにより async 大改修（B14(b)）へ投資する前に真因を確定し，誤った処方箋を回避できる．

**6. 成功条件（measurable）**

- **完了条件（必須）**: bench モード（`MICROBATCH_BENCH_STEPS>0`）で `deploy`→`predict:demo` を実行後，
  全 p 台の rank が `MICROBATCH_BENCH_TIMING`（recv_wait/compute/send_wait の mean/min/max）を docker logs に
  出力し，`tools/collect_results.py` 経由で `results/Iter9.jsonl` に **per-rank レコード**として保存される．
- **整合チェック**: 各 rank で `recv_wait_mean_s + compute_mean_s + send_wait_mean_s` が per-step の 1-mb あたり
  時間（＝既存 `MICROBATCH_BENCH` の `elapsed_s / (measure × m)`）の **95% 以上**を説明する（計時漏れ・
  オーバーヘッドが小さいことの確認）．
- **診断成功（真因の分類）**: 過去反復のばらつき（Iter7 CV≈0.07〜0.14%，Iter8 CV≈0.2〜1.5%，安全側で repeat 間
  ±5% をノイズ幅とみなす）を**超える有意差**で，次のいずれかに一意分類できる:
  (i) compute_s 支配＝ある rank の critical path で `compute_s ≥ per-step 時間の 60%`，
  (ii) recv_wait_s の rank 勾配＝隣接 rank 間で recv_wait_mean_s が単調かつ ±5% を超えて増減，
  (iii) 候補(a)＝rank0 の recv_wait_s（`normal_()`）が他 rank の compute_s と同等以上．
  いずれか 1 つが上記閾値で成立し，かつ repeat 間ばらつきがその判定を反転させないことを確認できれば診断成功とする．

**7. フォールバック / 要人間判断**

- **要人間判断: なし**（新規）．本変更は bench 経路への加算的な計測ログのみで serving/relay を変えず可逆．実機 deploy/
  predict は B7 の包括承認（非破壊 SSH/deploy）の範囲内で破壊的操作を含まない（B15 記載どおり）．B9 は温存
  （`[needs-human]` 維持，reflector で自動判定しない）．
- **フォールバック（B15）**: 実装が予想外に serving 経路へ波及する（`_process_microbatch` の計時が serving の
  非 bench ループ挙動を変える等）と判明した場合は，示唆(B)「重み int8 dynamic quantization を SL1 型 local マイクロ
  ベンチで作る前に測る」（compute 92% を直接攻める・可逆）へ振り替える．config `levers` はさらに下位のフォールバックと
  して温存．

---

### 調査 (Iter9)

**担当**: 調査フェーズ subagent（rc-investigator，2026-07-20 JST）．B15（実機 51 ノード bench 経路への per-microbatch
timing ログ追加で `time_per_step ∝ m`＝ほぼ完全直列の実際の直列化点を特定する）を受け，(1) 分散パイプライン並列推論の
段間直列化・同期点の診断手法，(2) `torch.distributed`（Gloo）での軽量な分散 timing 収集のベストプラクティス，
(3) rank0 が全 microbatch の入力生成を担う設計が直列化ボトルネックになりやすいかの既知知見，を文献調査した．
併せて `pipeline_inference.py` の bench 経路（`_run_microbatch_bench`:1221-1275 / `_process_microbatch`:997-1050 /
`_reset_kv_cache_for_bench`:971-995 / `_pipeline_loop`:1157-1219）を Read で確認し，どこに何のログを足すべきかの当たりを
つけた．実機非接続・コード読解と tavily 検索のみ（`pipeline_inference.py` は一切改変していない）．

**問い**

- Q1: Megatron-LM／DeepSpeed／vLLM 等の PP 実装は，バブル・直列化の原因診断にどんな timing/tracing を使うか
  （per-microbatch のどの時点で打刻し，rank 間でどう突き合わせるか）．
- Q2: Gloo backend での軽量な分散 timing 収集のベストプラクティス（rank 間のローカル時計ずれの扱い，
  オーバーヘッドを抑えつつ意味ある粒度で記録する方法）．
- Q3: rank0 が全 microbatch の入力生成を担う設計は PP の直列化ボトルネックになりやすいという既知知見があるか．

**分かったこと（出典付き）**

- **[Q1] 直列化診断の基準は「バブル率式との乖離」**: 完全に fill したパイプラインのバブル率は `(p-1)/(m+p-1)`
  （Megatron-LM 1T パラメータ論文，NVIDIA Technical Blog "Scaling Language Model Training..."／Narayanan et al.
  "Efficient Large-Scale Language Model Training on GPU Clusters", people.eecs.berkeley.edu）．Iter7 の
  `time_per_step ∝ m`（含意 FF≈1/p）はこの理想 fill から桁で外れており，「バブルが理論値を超えて増大」ではなく
  「そもそも fill していない＝直列」の署名．
- **[Q1] 診断の実務は per-microbatch × per-stage のイベント打刻→spacetime(Gantt) 再構成**: torch.profiler で
  各 rank のトレースを取り `export_chrome_trace` で `chrome://tracing` に読ませる方式が標準だが，複数 rank の
  トレースを 1 画面に重ねるのは未解決の課題として PyTorch issue が立っている（github.com/pytorch/pytorch#128292
  "put profiling results from different ranks into one webpage"）＝**全 rank のフル profiler トレースを突き合わせる
  のは重く扱いにくい**ので，軽量な手打ちの区間計時の方が本ケースには向く．
- **[Q1・最重要] 「同期は症状を転移させる」**: StageFrontier（arXiv "Synchronization-Aware Stage Accounting for
  Distributed ML Training"）が明言——"a slow data stage on one rank surfaces as backward wait on the others,
  so the stage with the largest [timer] is not where the delay originated"．**単一 rank のタイマー最大値からは
  真の直列化点を特定できず，各 microbatch を「compute 時間」と「wait(recv/send) 時間」に分解して rank 間で相関を
  取る必要がある**．これが B15 のログ設計の中核指針になる．
- **[Q1] blocking p2p の同期送信セマンティクス**: MPI 標準（mpi-forum.org "Nonblocking communication"）では
  synchronous/standard mode の send は「matching receive が post されるまで完了が遅延しうる」．naive な
  `recv→compute→send`（全 rank 同一）は rendezvous 型 send でも段が真並列なら fill しうる（Iter8 が FF=0.97 で実証）
  ——つまり**実機の完全直列は blocking 構造単独では説明できず，各 rank の recv 待ち／send 待ちの偏りに出るはず**．
  siboehm.com "Pipeline-Parallelism" も「first stage が microbatch 入力をロードし，各段のスケジュールを generator で
  進める」構造を示し，段が詰まる箇所は wait 区間に現れると整理している．
- **[Q2] 経過時間は monotonic clock，跨ノード絶対時刻は使わない**: wall clock（CLOCK_REALTIME）は NTP 同期で
  前後に飛ぶため duration 計測・跨ノード順序付けに不適，monotonic clock を使うのが原則（codelit.io "Clock
  Synchronization in Distributed Systems"，baeldung.com "Clock Offset vs Clock Skew"）．跨ノードの絶対時刻ずれ
  （skew/offset）は一般に数十 ms オーダーで観測系にアラートを張るほどの量（oneuptime.com のスパン時刻補正記事は
  50ms 閾値でアラート）．**51 ノードの絶対タイムスタンプを整列させるより，各 rank が自分の monotonic 区間長
  （recv待ち・compute・send待ち）をローカルに測って持ち寄る方が，時計同期不要で自己完結し軽量かつ正確**．
  既存コードは既に `time.monotonic()` を使用（:1042,:1201,:1247,:1252）でこの原則と整合している．
- **[Q2] オーバーヘッドと計測擾乱の回避**: `time.monotonic()` 自体は ns オーダーで安価だが，microbatch×step×51 rank の
  毎回 stdout 出力は洪水になる→**measure 窓のみ（warmup 除外）計時し，配列に貯めて repeat ごとに 1 行に集約
  （mean/min/max/合計）して吐く**のが定石．また**計時目的の `dist.barrier()` を per-microbatch ループ内に入れては
  ならない**（barrier 自体が直列化を生み，測ろうとしている fill を壊す＝観測が対象を変える）．barrier は measure 窓の
  境界のみ．
- **[Q3] first-stage 入力生成がボトルネックになる形はあるが本ケースでは軽い**: GPipe/1F1B の一般論では first stage が
  各 microbatch の入力を供給し，供給が遅いと下流が飢える（siboehm.com が明記）．ただし本リポジトリの rank0 生成は
  `recv_buffers[mb].normal_()`（:1021，ローカル RNG 充填）で，ディスク I/O や tokenize を伴わず**構造的に重くない**
  ため，候補(a)「rank0 生成の直列」が主因である事前確度は低い（が，計測で潰す価値はある＝rank0 の normal_() 区間も
  ログ対象にする）．

**コード読解で判明した重要事実（ログ設計に直結）**

- **B15 が挙げた候補(b)(c) は静的読解でほぼ排除できる**: measure ループ（`_run_microbatch_bench`:1237-1275）内に
  `dist.barrier()` は存在せず，`_reset_kv_cache_for_bench`（:989-995）は KV テンソルの `zero_()` と write_pos の
  ローカル初期化のみで**跨 rank 同期を含まない**．relay 側 barrier（:1430 等）は serving 経路で bench 経路とは別．
  よって残る主容疑は候補(a)＝rank0 生成 ではなく，**各 rank の recv待ち/send待ちの分布に現れる何か**（Gloo p2p の
  実ネット rendezvous コスト・下流バックプレッシャ等）に絞られる．これ自体が「barrier 由来ではない」という一次証拠と
  してログで確認する価値がある．
- **現状 RESULT は last rank でしか出ない**（:1265 `if is_last_node`）．per-rank の直列化点を見るには
  **全 rank が自分の timing 集約行を出す**必要がある（`tools/collect_results.py --stage-timing` は Iter4/B7 で全 rank へ
  SSH して `docker logs` を集める経路を既に持つので，各 rank が `[R{rank} RESULT] ...` を吐けば収集できる）．
- **`_process_microbatch` は値を返さない**ため，計時値は `timing_accumulator` 等のオプション引数（bench 経路からのみ
  非 None）か `self` の一時属性へ貯め，serving 経路（`timing_accumulator=None`）の挙動は不変に保つ設計が自然
  （Iter3/P1 の per-request INFO 追加・可逆と同種）．

**次フェーズ（rc-planner）への具体的な示唆**

- **打刻箇所**（すべて `_process_microbatch`:1019-1050 内，bench フラグ時のみ有効化）: [A]recv 前 t0 →
  rank0 は `normal_()`（:1021），他 rank は `dist.recv`（:1024）直後 t1＝**recv_wait_s**（＝上流が未送出なら大きい）→
  [B]compute 後（send 前）t2＝**compute_s** → [C]`dist.send`（:1048）後 t3＝**send_wait_s**（＝下流が recv 未 post なら
  大きい，rendezvous 由来のバックプレッシャ）．この 3 区間分解が StageFrontier の「compute と wait を分けて rank 間で
  相関」を満たす最小構成．
- **集約と出力**: warmup を除いた measure 窓で 3 区間を rank ごとに配列蓄積し，repeat 終了時（`_run_microbatch_bench`
  の measure ループ直後，:1263 付近）に**全 rank が** `[R{rank} RESULT] MICROBATCH_BENCH_TIMING recv_wait_s=...
  compute_s=... send_wait_s=... (mean/min/max)` を 1 行出力（既存 `MICROBATCH_BENCH` 行の key=value 形式を踏襲し
  collect_results.py が拡張パースしやすい形に）．per-microbatch の生値を毎回 stdout に出さない．
- **時計方針**: 追加も `time.monotonic()` で区間長のみ記録し，跨 rank の絶対時刻整列はしない（時計同期不要・軽量）．
  spacetime 図が要る場合でも rank r の send_wait と rank r+1 の recv_wait を突き合わせれば直列パターン（staircase）は
  相対値だけで読める．
- **判定の当たり**: 集約後に (i) compute_s が支配的なら既知の compute 律速 92%（F1）で fill 不成立の主因は計算量→
  量子化軸（示唆(B)）へ，(ii) recv_wait_s が上流ほど小さく下流ほど大きい／逆の勾配を持つなら Gloo p2p のリング状
  バックプレッシャ＝blocking 構造の実機挙動，(iii) rank0 の normal_() 区間が想定外に大きければ候補(a) を初めて実証，と
  切り分けられる．計測擾乱回避のため per-microbatch ループ内に barrier を足さないことを計画に明記すること．
- **フォールバック（B15 記載）**: 本計測が予想外に serving 経路へ波及するなら示唆(B)（重み int8 dynamic quantization の
  SL1 型 local マイクロベンチ）へ振り替え．新規の要人間判断は発生しない（加算的計測ログのみ・可逆，実機 deploy は B7
  包括承認の範囲内で破壊的操作なし）．

**出典**

- NVIDIA Technical Blog, "Scaling Language Model Training to a Trillion Parameters Using Megatron"（バブル率
  `(p-1)/(m+p-1)`，1F1B スケジュール），developer.nvidia.com．
- Narayanan et al., "Efficient Large-Scale Language Model Training on GPU Clusters"（m,p,t_f/t_b の定式化），
  people.eecs.berkeley.edu．
- StageFrontier: "Synchronization-Aware Stage Accounting for Distributed ML Training"（同期が症状を転移させる／
  compute と wait の分離が必要），arXiv．
- MPI Forum, "Nonblocking communication"（synchronous send は matching recv まで完了遅延），mpi-forum.org．
- S. Böhm, "Pipeline-Parallelism: Distributed Training via Model Partitioning"（first stage が入力供給・schedule
  generator），siboehm.com．
- PyTorch issue #128292 "put profiling results from different ranks into one webpage"（複数 rank トレース統合は
  未解決課題），github.com/pytorch/pytorch．
- "Clock Synchronization in Distributed Systems"（codelit.io）／"Clock Offset vs Clock Skew"（baeldung.com）／
  "How to Fix Incorrect Span Timestamps Caused by Clock Skew"（oneuptime.com）——monotonic clock 使用・跨ノード
  絶対時刻回避・skew は数十 ms オーダー．
- torch.distributed docs（Gloo が CPU の send/recv を supports），docs.pytorch.org．

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
