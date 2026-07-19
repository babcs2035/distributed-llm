"""Iteration 6 (SL2) の draft 採択率オフライン見積もり: target(31B) の greedy 参照系列に対する
draft(gemma-4-E2B) の exact-match 採択長を測定し，SL1 の ratio_K と合成して B3 実効利得レンジを出力する．

実機クラスタ・relay プロトコル・`pipeline_inference.py` には一切接続・変更しない．単一プロセス内で
`transformers.AutoModelForCausalLM` により target(31B)・draft(E2B) をローカル HF キャッシュから
ロードし，(1) target の greedy 参照系列生成，(2) draft による 1 回の teacher-forced forward で
全位置の greedy top-1 予測を取得（自己回帰生成を繰り返す必要が無い理由は
`draft_teacher_forced_predictions` の docstring を参照），(3) exact-match 採択判定，を行う．

背景・判定ルール・実装方針の詳細は `.claude/research/journal.md` の `## Iteration 6`
`### 検討・計画 (Iter6)` を参照．

使い方:
    unset VIRTUAL_ENV && uv run python scripts/estimate_draft_acceptance.py

結果は `results/draft_acceptance.jsonl` へ 1 レコード追記される（追記のみ，既存レコードは変更しない）．
モデルの実ロード・実推論を伴うため実行には数十分〜数時間かかる見込み（journal.md Iter6 §3 参照）．
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

import torch

if TYPE_CHECKING:
    from transformers import PreTrainedModel, PreTrainedTokenizerBase

# ====================================================================
# 定数（マジックナンバー回避．値の根拠は journal.md Iter6 計画を参照）
# ====================================================================

K_VALUES: tuple[int, ...] = (2, 4, 8)  # SL1 の ratio_K と揃える
N_MAX_NEW_TOKENS = 48
NUM_PROMPTS = 16  # 4 カテゴリ x 4 件
NGRAM_MAX = 3
NGRAM_CONT = 10
TARGET_DTYPE = torch.bfloat16
DRAFT_DTYPE = torch.bfloat16

# SL1（scripts/bench_compute_ceiling.py）と同じスレッド条件で実行する（journal.md Iter6 計画 (c)）
NUM_THREADS = 4
NUM_INTEROP_THREADS = 1

TARGET_MODEL_NAME = "google/gemma-4-31B-it"
DRAFT_MODEL_NAME = "google/gemma-4-E2B"

# revision 固定の理由（Iter6 実験フェーズで判明・暫定回避）: ローカル HF キャッシュの
# `refs/main` が，target/draft とも実際の重み・tokenizer を含む完全なスナップショットを
# 指していない（target は config.json のみの不完全スナップショットを指し，draft に至っては
# 存在しないスナップショットハッシュを指していた）．`local_files_only=True` かつ revision 未指定
# だと `refs/main` 経由で解決され失敗するため，実在する完全スナップショットのコミットハッシュを
# 明示指定して回避する（journal.md Iter6 `### 実験 (Iter6)` の申し送り対処案 (A)）．HF キャッシュ
# 自体（`refs/main`）は書き換えない．
TARGET_MODEL_REVISION = "fb9ae262347c3945692f09a612f8bb189def854f"
DRAFT_MODEL_REVISION = "19f17d3255f458aa49ebe8843d65ec7b7386db1f"

# SL1（Iteration 5，実機 i5-8350U 計測）の ratio_K（journal.md Iteration 5 参照）．
# SL2 の経験 a_K と合成して B3 実効利得レンジ候補値を算出する（compute_effective_gain_candidates）．
SL1_RATIO_BY_K: dict[int, float] = {2: 0.753, 4: 0.378, 8: 0.213}

# プロンプトカテゴリ（層別集計のキー．A-2: 入力接地型 vs 開放チャットで prompt-lookup の効きが変わる）
CATEGORY_OPEN_CHAT = "open_chat"
CATEGORY_SUMMARIZATION = "summarization"
CATEGORY_DOC_QA = "doc_qa"
CATEGORY_CODE = "code"

_REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_JSONL_PATH = _REPO_ROOT / "results" / "draft_acceptance.jsonl"


# ====================================================================
# データ型
# ====================================================================


@dataclass(frozen=True)
class Prompt:
    """1 件の評価プロンプト（カテゴリ・本文）．"""

    category: str
    text: str


@dataclass(frozen=True)
class BlockWalkResult:
    """`simulate_block_walk` の戻り値．参照系列を検証ブロック単位で歩いた集計結果．"""

    block_accepted_lengths: list[int]
    mean_accepted_length: float
    num_blocks: int


# ====================================================================
# (a) プロンプト集合
# ====================================================================


def build_prompt_set() -> list[Prompt]:
    """4 カテゴリ x 4 件 = `NUM_PROMPTS` 件の評価プロンプト集合を返す（journal.md Iter6 計画 (a)）．

    `open_chat`（入力非接地）は既存デモの "Hello!" を含む．`summarization`/`doc_qa`/`code` は
    入力接地型プロンプトとし，prompt-lookup（n-gram）を過小評価しない公平な設計にする（A-2）．
    """

    return [
        Prompt(CATEGORY_OPEN_CHAT, "Hello!"),
        Prompt(CATEGORY_OPEN_CHAT, "What's your favorite color, and why?"),
        Prompt(CATEGORY_OPEN_CHAT, "Tell me an interesting fact about space."),
        Prompt(CATEGORY_OPEN_CHAT, "How was your day?"),
        Prompt(
            CATEGORY_SUMMARIZATION,
            "Here is a short report: \"Quarterly revenue increased by 12% year over year, driven mainly "
            "by growth in the cloud services division. Operating costs rose slightly due to increased "
            "hiring in the engineering team. The board approved a new investment plan for the next fiscal "
            "year.\" Summarize the above in two sentences.",
        ),
        Prompt(
            CATEGORY_SUMMARIZATION,
            "Consider this paragraph: \"Weather patterns in the region have shifted over the past decade, "
            "with warmer winters and more frequent heavy rainfall events in summer. Local farmers have "
            "adapted by changing planting schedules.\" Summarize this in one sentence.",
        ),
        Prompt(
            CATEGORY_SUMMARIZATION,
            "Recipe steps: \"Preheat the oven to 180C. Mix flour, sugar, and butter until crumbly. Press "
            "the mixture into a baking tray. Bake for 25 minutes until golden brown. Let it cool before "
            "cutting into bars.\" Summarize the key steps briefly.",
        ),
        Prompt(
            CATEGORY_SUMMARIZATION,
            "Technical note: \"The system uses a pipeline-parallel architecture that splits model layers "
            "across multiple worker nodes connected over a network, allowing a single large model to run "
            "on hardware that could not hold it in memory alone.\" Give a one-sentence summary.",
        ),
        Prompt(
            CATEGORY_DOC_QA,
            "Context: \"Alice has a meeting with the design team at 10am, a lunch with a client at "
            "12:30pm, and a code review session at 3pm.\" Question: What time is Alice's lunch with the "
            "client?",
        ),
        Prompt(
            CATEGORY_DOC_QA,
            "Context: \"The dataset contains 4,215 rows and 12 columns. The target column is named "
            "'label' and has 3 unique classes.\" Question: How many unique classes does the target column "
            "have?",
        ),
        Prompt(
            CATEGORY_DOC_QA,
            "Context: \"The /predict endpoint accepts a POST request with a JSON body containing 'prompt' "
            "(string) and 'max_tokens' (integer, optional, default 48).\" Question: What is the default "
            "value of 'max_tokens'?",
        ),
        Prompt(
            CATEGORY_DOC_QA,
            "Context: \"In the story, a young sailor named Mira discovers a hidden island while repairing "
            "her boat during a storm.\" Question: What is the name of the protagonist in the story?",
        ),
        Prompt(
            CATEGORY_CODE,
            "Add a docstring to this function:\ndef add(a, b):\n    return a + b",
        ),
        Prompt(
            CATEGORY_CODE,
            "Fix the bug in this function:\ndef average(values):\n    return sum(values) / len(value)",
        ),
        Prompt(
            CATEGORY_CODE,
            "Complete the following function to return the sum of a list:\ndef total(values):\n"
            "    # TODO: implement",
        ),
        Prompt(
            CATEGORY_CODE,
            "Add type hints to this function:\ndef greet(name):\n    return \"Hello, \" + name",
        ),
    ]


# ====================================================================
# (b) トークナイズ（pipeline_inference.py:_tokenize() の複製）
# ====================================================================


def tokenize_prompt(tokenizer: "PreTrainedTokenizerBase", prompt_text: str) -> list[int]:
    """`pipeline_inference.py:_tokenize()`（:110-129）を複製したトークナイズ処理．

    Gemma-4 chat template（`apply_chat_template` + `add_generation_prompt=True`）を適用してから
    encode する．instruction-tuned モデルは生テキストの直接 encode と挙動が変わるため，
    このテンプレート適用済み経路を必ず経由する（B-2, journal.md Iter6 参照）．
    """

    messages = [{"role": "user", "content": prompt_text}]
    chat_text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    token_ids: list[int] = tokenizer.encode(chat_text, add_special_tokens=False)
    return token_ids


# ====================================================================
# (c) target 参照生成・draft 予測（モデル呼び出しを伴う。単体テスト対象外）
# ====================================================================


def generate_target_reference(
    model: "PreTrainedModel",
    prompt_token_ids: Sequence[int],
    max_new_tokens: int,
) -> list[int]:
    """target モデルに greedy 生成させ，新規生成分の token id 列（参照 argmax 系列）を返す．

    `do_sample=False, num_beams=1` により各ステップで argmax を取る．final_logit_softcapping は
    単調変換（tanh）で argmax を変えない（journal.md Iter6 B-1）ため，model 内蔵の
    softcapping 適用済みロジットに対しそのまま greedy デコードすればよい．EOS は
    `model.generation_config`（config.json の `eos_token_id`）に従って自動打ち切りされる．
    """

    input_ids = torch.tensor([list(prompt_token_ids)], dtype=torch.long)
    with torch.no_grad():
        output_ids = model.generate(
            input_ids=input_ids,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            num_beams=1,
            temperature=None,
            top_p=None,
            top_k=None,
        )
    generated_ids: list[int] = output_ids[0, input_ids.shape[1] :].tolist()
    return generated_ids


def draft_teacher_forced_predictions(
    draft_model: "PreTrainedModel",
    prompt_token_ids: Sequence[int],
    reference_token_ids: Sequence[int],
) -> list[int]:
    """draft モデルに 1 回の forward で teacher-forced に全位置の greedy top-1 予測を取得する．

    採択判定は「ブロック先頭プレフィックスに対する draft の自己回帰提案」を要求するが，
    exact-match 判定では最初の不一致位置までしか使わない．一致が続いている間は
    「draft の自己回帰提案」と「真の参照プレフィックスを条件とした teacher-forced 予測」は
    同一のコンテキストから計算されるため常に一致し，不一致が起きた"後"の draft 予測は
    判定に使われず無視してよい（journal.md Iter6 A-1・(d) 参照）．したがって 1 プロンプトあたり
    draft を 1 回だけ forward すればよく，位置ごとに K 回の自己回帰生成を繰り返すより
    大幅に安価である（`make_block_propose_fn` で `simulate_block_walk` の `propose_fn` に変換する）．
    """

    full_token_ids = list(prompt_token_ids) + list(reference_token_ids)
    input_ids = torch.tensor([full_token_ids], dtype=torch.long)
    prompt_len = len(prompt_token_ids)
    with torch.no_grad():
        logits = draft_model(input_ids=input_ids).logits  # (1, seq_len, vocab)

    # reference_token_ids[j] を予測する logits は，1つ前の position (prompt_len + j - 1) にある．
    start = prompt_len - 1
    end = start + len(reference_token_ids)
    predicted_ids: list[int] = logits[0, start:end, :].argmax(dim=-1).tolist()
    return predicted_ids


def make_block_propose_fn(predicted_tokens: Sequence[int]) -> Callable[[Sequence[int], int], list[int]]:
    """事前計算済み teacher-forced 予測列から，`simulate_block_walk` 用の `propose_fn` を作る．

    プレフィックス長 `len(prefix)` がそのまま参照系列内の現在位置に対応するため，
    `predicted_tokens[len(prefix) : len(prefix)+k]` を返す．
    """

    def propose(prefix: Sequence[int], k: int) -> list[int]:
        position = len(prefix)
        return list(predicted_tokens[position : position + k])

    return propose


# ====================================================================
# (d) 採択判定・ブロック前進集計（純関数・単体テスト対象）
# ====================================================================


def accepted_length(reference_window: Sequence[int], proposed_tokens: Sequence[int]) -> int:
    """draft の提案トークン列を参照 window と逐位置 exact-match 照合し，確定トークン数を返す．

    最初の不一致位置までを「採択」とし（全一致なら window 全体を採択），その直後の 1 トークンは
    target が検証時に必ず確定させる「bonus/訂正トークン」として常に +1 する
    （Leviathan et al. 2023 の speculative decoding アルゴリズム．journal.md Iter6 A-1 参照）．
    """

    accepted = 0
    for reference_token, proposed_token in zip(reference_window, proposed_tokens):
        if reference_token != proposed_token:
            break
        accepted += 1
    return accepted + 1


def simulate_block_walk(
    reference_tokens: Sequence[int],
    propose_fn: Callable[[Sequence[int], int], Sequence[int]],
    k: int,
) -> BlockWalkResult:
    """参照系列を検証ブロック単位で歩き，各ブロックの確定トークン数（採択+bonus）を集計する．

    各ブロック開始位置で `propose_fn(reference_tokens[:position], k)` を呼び draft の K トークン
    提案を取得し，`accepted_length` で確定数を求める．確定数だけ position を前進させ，系列終端に
    達するまで繰り返す．系列終端付近では確定数が残りトークン数を超えうるため，残数でクリップする
    （bonus トークンは参照系列の外側には存在しないため）．
    """

    if k <= 0:
        raise ValueError(f"k は正の整数である必要がある: {k}")

    total_length = len(reference_tokens)
    block_lengths: list[int] = []
    position = 0
    while position < total_length:
        window = reference_tokens[position : position + k]
        proposed = propose_fn(reference_tokens[:position], k)
        confirmed = accepted_length(window, proposed)
        confirmed = min(confirmed, total_length - position)
        block_lengths.append(confirmed)
        position += confirmed

    mean_accepted_length = sum(block_lengths) / len(block_lengths) if block_lengths else 0.0
    return BlockWalkResult(
        block_accepted_lengths=block_lengths,
        mean_accepted_length=mean_accepted_length,
        num_blocks=len(block_lengths),
    )


def alpha_to_expected_len(alpha: float, k: int) -> float:
    """1トークンあたり採択率 alpha から，Leviathan et al. 2023 の式で期待確定トークン数を予測する．

    E = (1 - alpha^(K+1)) / (1 - alpha)（iid 幾何近似．実際は序盤位置ほど採択されやすく非 iid の
    ため経験値と乖離しうる，journal.md Iter6 A-1・(e) 参照）．`alpha == 1.0`（常に採択）のときは
    (1-alpha) が 0 になり式が定義できないため，極限値 K+1 を返す．
    """

    if k <= 0:
        raise ValueError(f"k は正の整数である必要がある: {k}")
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha は [0, 1] の範囲である必要がある: {alpha}")

    if alpha >= 1.0:
        return float(k + 1)
    return (1.0 - alpha ** (k + 1)) / (1.0 - alpha)


def compute_alpha(predicted_tokens: Sequence[int], reference_tokens: Sequence[int]) -> float:
    """teacher-forced 予測列と参照列を逐位置比較し，1 位置あたり採択率 alpha（一致割合）を返す．

    `predicted_tokens[i]` は「真の参照プレフィックス `reference_tokens[:i]` を条件とした draft の
    greedy top-1 予測」であることを前提とする（`draft_teacher_forced_predictions` の出力形式）．
    """

    if len(predicted_tokens) != len(reference_tokens):
        raise ValueError(
            "predicted_tokens と reference_tokens は同じ長さである必要がある: "
            f"{len(predicted_tokens)} != {len(reference_tokens)}"
        )
    if not reference_tokens:
        return 0.0

    matches = sum(1 for predicted, reference in zip(predicted_tokens, reference_tokens) if predicted == reference)
    return matches / len(reference_tokens)


# ====================================================================
# (g) prompt-lookup（n-gram, 補助）
# ====================================================================


def ngram_lookup_propose(context_tokens: Sequence[int], ngram_max: int, continuation_len: int) -> list[int]:
    """prompt-lookup (n-gram) decoding: context の末尾 n-gram（n=ngram_max..1）が過去に出現していれば，
    その直後の続き（最大 continuation_len トークン）を提案する（apoorvumang/prompt-lookup-decoding 方式，
    journal.md Iter6 A-2 参照）．

    最も末尾に近い出現（直近の一致）を優先して検索する．どの n でも一致が見つからなければ空リストを
    返す（draft 提案なし＝この位置の採択率は 0 として扱われる）．
    """

    context = list(context_tokens)
    context_len = len(context)
    max_n = min(ngram_max, context_len - 1)

    for n in range(max_n, 0, -1):
        ngram = context[context_len - n :]
        # 直近の出現を優先するため末尾から遡って検索する
        # （範囲の "-1" は末尾の ngram 自身との自明な一致を除外するため）
        for start in range(context_len - n - 1, -1, -1):
            if context[start : start + n] == ngram:
                match_end = start + n
                candidate = context[match_end : match_end + continuation_len]
                if candidate:
                    return candidate
    return []


def compute_ngram_alpha(
    prompt_token_ids: Sequence[int],
    reference_token_ids: Sequence[int],
    ngram_max: int,
    continuation_len: int,
) -> float:
    """prompt+reference 全体を通じ，各生成位置で `ngram_lookup_propose` の先頭トークンが参照と
    一致する割合（1位置あたり採択率 alpha 相当）を返す．

    (g) 主指標には混ぜない補助指標．入力接地型タスクでは alpha>0，開放チャットでは alpha≈0 に
    なる想定（A-2）．
    """

    if not reference_token_ids:
        return 0.0

    context = list(prompt_token_ids)
    matches = 0
    for reference_token in reference_token_ids:
        proposed = ngram_lookup_propose(context, ngram_max, continuation_len)
        if proposed and proposed[0] == reference_token:
            matches += 1
        context.append(reference_token)
    return matches / len(reference_token_ids)


# ====================================================================
# (e)/(f) 集計・SL1×SL2 合成（純関数・単体テスト対象）
# ====================================================================


def aggregate_alpha_by_category(per_prompt_alpha: Sequence[tuple[str, float, int]]) -> dict[str, float]:
    """`(category, alpha, num_positions)` の並びから，位置数で重み付けしたカテゴリ別 alpha を計算する．

    位置数で重み付けすることで，プロンプトごとに参照系列長が異なっても（EOS早期打ち切り等）
    カテゴリ全体の位置数を正しく反映した alpha になる．
    """

    weighted_sums: dict[str, float] = {}
    weights: dict[str, int] = {}
    for category, alpha, num_positions in per_prompt_alpha:
        weighted_sums[category] = weighted_sums.get(category, 0.0) + alpha * num_positions
        weights[category] = weights.get(category, 0) + num_positions

    return {
        category: (weighted_sums[category] / weights[category] if weights[category] > 0 else 0.0)
        for category in weighted_sums
    }


def compute_effective_gain_candidates(
    a_k_by_k: Mapping[int, float],
    ratio_by_k: Mapping[int, float],
) -> dict[int, dict[str, float]]:
    """SL1 の ratio_K（compute比）と SL2 の経験 a_K（採択長）を合成し，B3 実効利得レンジの
    素の数値を出す．最終的な解釈（go/no-go）は analyst に委ねるため，ここでは a_K・ratio_K・
    その積・比のみを出力する（journal.md Iter6 (f) 参照）．

    - `product_a_k_ratio_k = a_K * ratio_K`: 1検証ステップで確定するトークン数と，そのブロック
      GEMM 相対コストの積．小さいほど「少ない相対コストで多くのトークンを確定できている」ことを
      意味する候補指標．
    - `gain_over_baseline = a_K / (K * ratio_K)`: baseline（K=1 逐次実行，コスト比1・確定1トークン）
      に対する相対利得の候補指標．1.0 超なら baseline より効率的な可能性を示唆する．
    """

    common_ks = set(a_k_by_k) & set(ratio_by_k)
    if not common_ks:
        raise ValueError(
            f"a_k_by_k と ratio_by_k に共通する K が無い: {sorted(a_k_by_k)} / {sorted(ratio_by_k)}"
        )

    candidates: dict[int, dict[str, float]] = {}
    for k in sorted(common_ks):
        a_k = a_k_by_k[k]
        ratio_k = ratio_by_k[k]
        candidates[k] = {
            "a_k": a_k,
            "ratio_k": ratio_k,
            "product_a_k_ratio_k": a_k * ratio_k,
            "gain_over_baseline": (a_k / (k * ratio_k)) if ratio_k != 0 else float("inf"),
        }
    return candidates


# ====================================================================
# 出力
# ====================================================================


def _print_report(
    overall_alpha: float,
    category_alpha: Mapping[str, float],
    category_ngram_alpha: Mapping[str, float],
    empirical_a_k: Mapping[int, float],
    predicted_a_k: Mapping[int, float],
    effective_gain: Mapping[int, Mapping[str, float]],
) -> None:
    """人間可読テーブルを stdout へ出力する．"""

    print(f"E2B 全体 alpha: {overall_alpha:.4f}")
    print("E2B カテゴリ別 alpha:")
    for category, alpha in category_alpha.items():
        print(f"  {category}: {alpha:.4f}")
    print("prompt-lookup（補助）カテゴリ別 alpha（A-2 検証用）:")
    for category, alpha in category_ngram_alpha.items():
        print(f"  {category}: {alpha:.4f}")

    print("\nK別 経験 a_K と alpha からの予測 a_K:")
    for k in sorted(empirical_a_k.keys()):
        print(f"  K={k}: empirical_a_k={empirical_a_k[k]:.4f}, predicted_a_k={predicted_a_k[k]:.4f}")

    print("\nSL1 x SL2 合成（B3 実効利得レンジ候補値）:")
    for k in sorted(effective_gain.keys()):
        row = effective_gain[k]
        print(
            f"  K={k}: a_k={row['a_k']:.4f}, ratio_k={row['ratio_k']:.4f}, "
            f"product={row['product_a_k_ratio_k']:.4f}, gain_over_baseline={row['gain_over_baseline']:.4f}"
        )


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    """レコードを JSONL ファイルへ 1 行追記する（親ディレクトリが無ければ作成，末尾改行付き）．"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json.dumps(record, ensure_ascii=False))
        jsonl_file.write("\n")


# ====================================================================
# エントリポイント
# ====================================================================


def main() -> None:
    """SL2: draft(E2B) の採択率オフライン測定を実行し，結果を stdout と JSONL へ出力する．

    target(31B)・draft(E2B) をローカル HF キャッシュから単一プロセスにロードするため，
    実行には数十分〜数時間かかる見込み（journal.md Iter6 §3 参照）．
    """

    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(NUM_THREADS)
    torch.set_num_interop_threads(NUM_INTEROP_THREADS)

    prompts = build_prompt_set()
    if len(prompts) != NUM_PROMPTS:
        raise ValueError(f"想定プロンプト数と不一致: {len(prompts)} != {NUM_PROMPTS}")

    tokenizer = AutoTokenizer.from_pretrained(
        TARGET_MODEL_NAME, revision=TARGET_MODEL_REVISION, trust_remote_code=True, local_files_only=True,
    )
    target_model = AutoModelForCausalLM.from_pretrained(
        TARGET_MODEL_NAME,
        revision=TARGET_MODEL_REVISION,
        torch_dtype=TARGET_DTYPE,
        device_map="cpu",
        local_files_only=True,
    )
    target_model.eval()
    draft_model = AutoModelForCausalLM.from_pretrained(
        DRAFT_MODEL_NAME,
        revision=DRAFT_MODEL_REVISION,
        torch_dtype=DRAFT_DTYPE,
        device_map="cpu",
        local_files_only=True,
    )
    draft_model.eval()

    per_prompt_records: list[dict[str, object]] = []
    alpha_rows: list[tuple[str, float, int]] = []
    ngram_alpha_rows: list[tuple[str, float, int]] = []
    block_walks_by_k: dict[int, list[BlockWalkResult]] = {k: [] for k in K_VALUES}

    for prompt in prompts:
        prompt_token_ids = tokenize_prompt(tokenizer, prompt.text)
        reference_token_ids = generate_target_reference(target_model, prompt_token_ids, N_MAX_NEW_TOKENS)
        if not reference_token_ids:
            print(f"[WARN] {prompt.category}/{prompt.text[:20]!r}: 参照系列が空だったためスキップ")
            continue

        predicted_tokens = draft_teacher_forced_predictions(draft_model, prompt_token_ids, reference_token_ids)
        alpha = compute_alpha(predicted_tokens, reference_token_ids)
        ngram_alpha = compute_ngram_alpha(prompt_token_ids, reference_token_ids, NGRAM_MAX, NGRAM_CONT)

        propose_fn = make_block_propose_fn(predicted_tokens)
        block_walk_by_k: dict[int, BlockWalkResult] = {}
        for k in K_VALUES:
            walk = simulate_block_walk(reference_token_ids, propose_fn, k)
            block_walk_by_k[k] = walk
            block_walks_by_k[k].append(walk)

        alpha_rows.append((prompt.category, alpha, len(reference_token_ids)))
        ngram_alpha_rows.append((prompt.category, ngram_alpha, len(reference_token_ids)))

        per_prompt_records.append(
            {
                "category": prompt.category,
                "prompt_text": prompt.text,
                "num_reference_tokens": len(reference_token_ids),
                "alpha_e2b": alpha,
                "ngram_alpha": ngram_alpha,
                "mean_accepted_length_by_k": {str(k): v.mean_accepted_length for k, v in block_walk_by_k.items()},
            }
        )

    total_positions = sum(num_positions for _, _, num_positions in alpha_rows)
    overall_alpha = (
        sum(alpha * num_positions for _, alpha, num_positions in alpha_rows) / total_positions
        if total_positions > 0
        else 0.0
    )
    category_alpha = aggregate_alpha_by_category(alpha_rows)
    category_ngram_alpha = aggregate_alpha_by_category(ngram_alpha_rows)

    empirical_a_k: dict[int, float] = {}
    predicted_a_k: dict[int, float] = {}
    for k in K_VALUES:
        all_block_lengths = [length for walk in block_walks_by_k[k] for length in walk.block_accepted_lengths]
        empirical_a_k[k] = sum(all_block_lengths) / len(all_block_lengths) if all_block_lengths else 0.0
        predicted_a_k[k] = alpha_to_expected_len(overall_alpha, k)

    effective_gain = compute_effective_gain_candidates(empirical_a_k, SL1_RATIO_BY_K)

    _print_report(overall_alpha, category_alpha, category_ngram_alpha, empirical_a_k, predicted_a_k, effective_gain)

    record: dict[str, object] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target_model": TARGET_MODEL_NAME,
        "draft_model": DRAFT_MODEL_NAME,
        "k_values": list(K_VALUES),
        "n_max_new_tokens": N_MAX_NEW_TOKENS,
        "num_prompts": len(prompts),
        "overall_alpha_e2b": overall_alpha,
        "category_alpha_e2b": category_alpha,
        "category_alpha_ngram_lookup": category_ngram_alpha,
        "empirical_a_k": {str(k): v for k, v in empirical_a_k.items()},
        "predicted_a_k_from_alpha": {str(k): v for k, v in predicted_a_k.items()},
        "sl1_ratio_by_k": {str(k): v for k, v in SL1_RATIO_BY_K.items()},
        "effective_gain_candidates": {str(k): v for k, v in effective_gain.items()},
        "per_prompt": per_prompt_records,
    }
    append_jsonl(RESULTS_JSONL_PATH, record)
    print(f"\nresults へ 1 レコード追記した: {RESULTS_JSONL_PATH}")


if __name__ == "__main__":
    main()
