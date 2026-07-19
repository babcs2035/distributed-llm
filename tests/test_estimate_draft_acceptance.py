"""`scripts/estimate_draft_acceptance.py` の純関数（採択判定・ブロック前進・alpha 写像・n-gram 提案・
集計・SL1xSL2 合成）に対する単体テスト．

target(31B)・draft(E2B) の実ロード・実推論はタイミング・RAM コストが大きいため対象外とする
（決定的な入力を与えられる純関数のみを検証する）．クラスタ・SSH 接続は行わない．
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from estimate_draft_acceptance import (  # noqa: E402 -- sys.path 設定後に import する必要がある
    CATEGORY_CODE,
    CATEGORY_DOC_QA,
    CATEGORY_OPEN_CHAT,
    CATEGORY_SUMMARIZATION,
    NUM_PROMPTS,
    BlockWalkResult,
    accepted_length,
    aggregate_alpha_by_category,
    alpha_to_expected_len,
    build_prompt_set,
    compute_alpha,
    compute_effective_gain_candidates,
    compute_ngram_alpha,
    make_block_propose_fn,
    ngram_lookup_propose,
    simulate_block_walk,
)


# ====================================================================
# accepted_length（exact-match 打切り＋bonus）
# ====================================================================


def test_accepted_length_returns_full_window_plus_bonus_on_full_match() -> None:
    """window 全体が一致する場合，確定数は len(window)+1（採択全部＋bonus）．"""

    assert accepted_length([10, 20, 30], [10, 20, 30]) == 4


def test_accepted_length_returns_one_on_immediate_mismatch() -> None:
    """先頭位置から不一致の場合，採択数は 0，確定数は bonus 分の 1 のみ．"""

    assert accepted_length([10, 20, 30], [99, 20, 30]) == 1


def test_accepted_length_stops_at_first_mismatch_for_partial_match() -> None:
    """途中位置で不一致が起きた場合，そこまでの採択数＋bonus 1 を返す．"""

    assert accepted_length([10, 20, 30, 40], [10, 20, 99, 40]) == 3


def test_accepted_length_handles_k1_single_token_match() -> None:
    """K=1 相当（window 長 1）で一致する場合，確定数は 2（採択1＋bonus1）．"""

    assert accepted_length([10], [10]) == 2


def test_accepted_length_handles_k1_single_token_mismatch() -> None:
    """K=1 相当（window 長 1）で不一致の場合，確定数は 1（bonus のみ）．"""

    assert accepted_length([10], [99]) == 1


def test_accepted_length_handles_empty_proposed_tokens() -> None:
    """draft が何も提案しない場合でも，bonus 分の 1 は必ず確定する．"""

    assert accepted_length([10, 20], []) == 1


# ====================================================================
# simulate_block_walk（ブロック前進で a_K 集計）
# ====================================================================


def test_simulate_block_walk_advances_by_k_plus_one_when_always_matching() -> None:
    """draft の提案が常に参照と完全一致するなら，各ブロックは K+1 トークンずつ前進する．"""

    reference = [1, 2, 3, 4, 5, 6]
    propose_fn = make_block_propose_fn(reference)  # 参照そのものを提案する = 常に全一致

    result = simulate_block_walk(reference, propose_fn, k=2)

    assert result.block_accepted_lengths == [3, 3]
    assert result.mean_accepted_length == pytest.approx(3.0)
    assert result.num_blocks == 2


def test_simulate_block_walk_advances_by_one_when_never_matching() -> None:
    """draft の提案が常に外れるなら，各ブロックは 1 トークンずつしか前進しない（bonus のみ）．"""

    reference = [1, 2, 3, 4]
    propose_fn = lambda prefix, k: [-1] * k  # noqa: E731 -- テスト用の単純なダミー提案関数

    result = simulate_block_walk(reference, propose_fn, k=2)

    assert result.block_accepted_lengths == [1, 1, 1, 1]
    assert result.mean_accepted_length == pytest.approx(1.0)
    assert result.num_blocks == 4


def test_simulate_block_walk_clips_confirmed_length_at_sequence_end() -> None:
    """系列終端付近で確定数が残りトークン数を超える場合，残数でクリップされる．"""

    reference = [1, 2, 3]
    propose_fn = make_block_propose_fn(reference)  # 常に全一致

    result = simulate_block_walk(reference, propose_fn, k=8)

    # k=8 でも参照系列長は 3 しかないため，1 ブロックで系列全体（3 トークン）が確定して終わる．
    assert result.block_accepted_lengths == [3]
    assert result.num_blocks == 1


def test_simulate_block_walk_rejects_non_positive_k() -> None:
    """K が 0 以下のときは誤った結果を黙って返さず例外を送出する．"""

    with pytest.raises(ValueError):
        simulate_block_walk([1, 2, 3], make_block_propose_fn([1, 2, 3]), k=0)


# ====================================================================
# alpha_to_expected_len（A-1 写像式）
# ====================================================================


def test_alpha_to_expected_len_matches_geometric_series_formula() -> None:
    """E=(1-alpha^(K+1))/(1-alpha) の式どおりに計算される（alpha=0.5, K=2 の具体値で検証）．"""

    # E = (1 - 0.5^3) / (1 - 0.5) = (1 - 0.125) / 0.5 = 1.75
    assert alpha_to_expected_len(0.5, 2) == pytest.approx(1.75)


def test_alpha_to_expected_len_returns_one_when_alpha_is_zero() -> None:
    """alpha=0（draft が全く当たらない）なら，期待確定トークン数は常に 1（bonus のみ）．"""

    assert alpha_to_expected_len(0.0, 4) == pytest.approx(1.0)


def test_alpha_to_expected_len_returns_k_plus_one_at_alpha_limit_of_one() -> None:
    """alpha=1.0（常に採択）は (1-alpha) がゼロになるため，極限値 K+1 を返す．"""

    assert alpha_to_expected_len(1.0, 8) == pytest.approx(9.0)


def test_alpha_to_expected_len_rejects_out_of_range_alpha() -> None:
    """alpha が [0, 1] の範囲外のときは例外を送出する．"""

    with pytest.raises(ValueError):
        alpha_to_expected_len(1.5, 2)


def test_alpha_to_expected_len_rejects_non_positive_k() -> None:
    """K が 0 以下のときは例外を送出する．"""

    with pytest.raises(ValueError):
        alpha_to_expected_len(0.5, 0)


# ====================================================================
# compute_alpha（teacher-forced 一致率）
# ====================================================================


def test_compute_alpha_computes_match_fraction() -> None:
    """predicted_tokens と reference_tokens の一致割合を返す（4 件中 3 件一致で 0.75）．"""

    predicted = [1, 2, 99, 4]
    reference = [1, 2, 3, 4]

    assert compute_alpha(predicted, reference) == pytest.approx(0.75)


def test_compute_alpha_rejects_mismatched_lengths() -> None:
    """predicted_tokens と reference_tokens の長さが異なる場合は例外を送出する．"""

    with pytest.raises(ValueError):
        compute_alpha([1, 2], [1, 2, 3])


def test_compute_alpha_returns_zero_for_empty_reference() -> None:
    """参照系列が空（EOS 即時打切り等）の場合は 0.0 を返す（ゼロ除算を避ける）．"""

    assert compute_alpha([], []) == 0.0


# ====================================================================
# ngram_lookup_propose（prompt-lookup, 補助）
# ====================================================================


def test_ngram_lookup_propose_finds_recent_repeated_ngram() -> None:
    """末尾 n-gram が過去に出現していれば，その直後の続きを提案する（入力接地型を想定）．"""

    # "...4, 5, 6, 2, 3..." の後に再び "4, 5" が現れたら，過去の出現直後の続き "6, 2, 3" を提案するはず．
    context = [1, 4, 5, 6, 2, 3, 4, 5]

    proposed = ngram_lookup_propose(context, ngram_max=2, continuation_len=3)

    assert proposed == [6, 2, 3]


def test_ngram_lookup_propose_returns_empty_when_no_match_found() -> None:
    """過去に一致する n-gram が無い場合（開放チャット想定）は空リストを返す．"""

    context = [1, 2, 3, 4, 5]

    assert ngram_lookup_propose(context, ngram_max=3, continuation_len=10) == []


def test_ngram_lookup_propose_truncates_continuation_at_context_end() -> None:
    """一致直後の続きが continuation_len より短い場合，実際に存在する分だけ返す．"""

    context = [7, 8, 9, 1, 2, 7, 8, 9]

    proposed = ngram_lookup_propose(context, ngram_max=3, continuation_len=10)

    assert proposed == [1, 2, 7, 8, 9]


# ====================================================================
# compute_ngram_alpha（prompt-lookup の位置別採択率）
# ====================================================================


def test_compute_ngram_alpha_is_positive_for_input_grounded_repetition() -> None:
    """プロンプト中の文字列が生成でそのまま繰り返される（入力接地型）場合，alpha は正になる．"""

    prompt_token_ids = [100, 200, 300, 400]
    reference_token_ids = [200, 300, 400]  # プロンプト内の部分列がそのまま生成される

    alpha = compute_ngram_alpha(prompt_token_ids, reference_token_ids, ngram_max=2, continuation_len=10)

    assert alpha > 0.0


def test_compute_ngram_alpha_is_zero_for_open_chat_without_repetition() -> None:
    """開放チャットのように入力接地性が無い生成では，alpha は 0 になる（A-2）．"""

    prompt_token_ids = [1, 2, 3]
    reference_token_ids = [40, 50, 60]  # プロンプトと全く重複しない生成

    alpha = compute_ngram_alpha(prompt_token_ids, reference_token_ids, ngram_max=3, continuation_len=10)

    assert alpha == 0.0


# ====================================================================
# aggregate_alpha_by_category（カテゴリ別重み付き集計）
# ====================================================================


def test_aggregate_alpha_by_category_weights_by_num_positions() -> None:
    """同一カテゴリ内で位置数の異なる複数プロンプトを，位置数で重み付け平均する．"""

    rows = [
        ("open_chat", 0.0, 10),
        ("open_chat", 1.0, 30),  # 位置数が多い方に平均が引っ張られるはず
    ]

    result = aggregate_alpha_by_category(rows)

    assert result["open_chat"] == pytest.approx(30.0 / 40.0)


def test_aggregate_alpha_by_category_keeps_categories_separate() -> None:
    """異なるカテゴリの alpha は混ざらずそれぞれ独立に集計される．"""

    rows = [
        ("open_chat", 0.1, 10),
        ("code", 0.9, 10),
    ]

    result = aggregate_alpha_by_category(rows)

    assert result["open_chat"] == pytest.approx(0.1)
    assert result["code"] == pytest.approx(0.9)


# ====================================================================
# compute_effective_gain_candidates（SL1 x SL2 合成）
# ====================================================================


def test_compute_effective_gain_candidates_computes_product_and_ratio() -> None:
    """a_K・ratio_K の積と，baseline に対する比が K ごとに正しく算出される．"""

    a_k_by_k = {2: 1.5, 4: 2.0}
    ratio_by_k = {2: 0.8, 4: 0.4}

    candidates = compute_effective_gain_candidates(a_k_by_k, ratio_by_k)

    assert candidates[2]["product_a_k_ratio_k"] == pytest.approx(1.5 * 0.8)
    assert candidates[2]["gain_over_baseline"] == pytest.approx(1.5 / (2 * 0.8))
    assert candidates[4]["product_a_k_ratio_k"] == pytest.approx(2.0 * 0.4)
    assert candidates[4]["gain_over_baseline"] == pytest.approx(2.0 / (4 * 0.4))


def test_compute_effective_gain_candidates_rejects_disjoint_k_sets() -> None:
    """a_k_by_k と ratio_by_k に共通する K が無い場合は例外を送出する．"""

    with pytest.raises(ValueError):
        compute_effective_gain_candidates({2: 1.0}, {8: 0.2})


# ====================================================================
# build_prompt_set（プロンプト集合の定義）
# ====================================================================


def test_build_prompt_set_has_four_categories_with_four_prompts_each() -> None:
    """4 カテゴリ x 4 件 = NUM_PROMPTS 件のプロンプトが定義されている．"""

    prompts = build_prompt_set()

    assert len(prompts) == NUM_PROMPTS
    categories = [prompt.category for prompt in prompts]
    for category in (CATEGORY_OPEN_CHAT, CATEGORY_SUMMARIZATION, CATEGORY_DOC_QA, CATEGORY_CODE):
        assert categories.count(category) == 4


def test_build_prompt_set_includes_existing_demo_prompt_in_open_chat() -> None:
    """open_chat カテゴリに既存デモのプロンプト "Hello!" が含まれる（results/Iter4.jsonl 参照）．"""

    prompts = build_prompt_set()

    open_chat_texts = [prompt.text for prompt in prompts if prompt.category == CATEGORY_OPEN_CHAT]
    assert "Hello!" in open_chat_texts


# ====================================================================
# BlockWalkResult（データ型の健全性）
# ====================================================================


def test_block_walk_result_is_immutable_dataclass() -> None:
    """BlockWalkResult は frozen dataclass であり，生成後にフィールドを書き換えられない．"""

    result = BlockWalkResult(block_accepted_lengths=[1, 2], mean_accepted_length=1.5, num_blocks=2)

    with pytest.raises(AttributeError):
        result.num_blocks = 99  # type: ignore[misc]
