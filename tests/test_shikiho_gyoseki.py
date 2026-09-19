"""四季報業績予想パーサーのテスト (issue #346)。"""

import pytest

from shikiho_gyoseki import parse_shikiho_gyoseki

# issue #346 のサンプル (7318)。実績・予想の通期行が混在する。
SAMPLE = """【業績】\t売上高\t営業利益\t経常利益\t利益\t1株益（円）\t1株配（円）
連22.3*\t13,805\t194\t179\t199\t12.3\t0
連26.3\t51,163\t2,189\t2,418\t4,147\t227.3\t0
連27.3予\t70,000\t3,700\t3,500\t2,300\t126.5\t0
連28.3予\t85,000\t4,300\t4,200\t2,600\t143.0\t0
"""


def test_parse_sample_extracts_three_periods():
    """実績1期 + 予想2期を取り出し、前期比成長率を計算する。"""
    result = parse_shikiho_gyoseki(SAMPLE)

    assert result["prev_year"]["label"] == "連26.3"
    assert result["prev_year"]["sales"] == 51163
    assert result["this_year"]["label"] == "連27.3予"
    assert result["this_year"]["sales"] == 70000
    assert result["this_year"]["op_profit"] == 3700
    # 70000/51163 = +36.8%, 3700/2189 = +69.0%
    assert result["this_year"]["sales_growth"] == 36.8
    assert result["this_year"]["op_growth"] == 69.0
    # 来季は今季予想比
    assert result["next_year"]["label"] == "連28.3予"
    assert result["next_year"]["sales_growth"] == 21.4
    assert result["next_year"]["op_growth"] == 16.2
    assert result["raw_text"] == SAMPLE.strip()


@pytest.mark.parametrize(
    "text, this_label, next_year, prev_year",
    [
        # 予想1件のみ → next_year は None
        ("連26.3\t100\t10\n連27.3予\t120\t15\n", "連27.3予", None, "連26.3"),
        # 実績行なし → prev_year は None、今季の成長率も出せない
        ("連27.3予\t120\t15\n連28.3予\t140\t18\n", "連27.3予", "連28.3予", None),
        # 中間期行は通期でないので無視される
        (
            "連26.3\t100\t10\n26.9中\t60\t7\n連27.3予\t120\t15\n",
            "連27.3予",
            None,
            "連26.3",
        ),
        # 予想が3期並んでも、今季は直前実績の次の期 (位置で選ばない)
        (
            "連26.3\t100\t10\n連27.3予\t120\t15\n連28.3予\t140\t18\n連29.3予\t160\t20\n",
            "連27.3予",
            "連28.3予",
            "連26.3",
        ),
        # 決算月の違う中間期予想 (連27.9予) は通期でないので除外する
        (
            "連26.3\t100\t10\n連27.3予\t120\t15\n連27.9予\t70\t8\n連28.3予\t140\t18\n",
            "連27.3予",
            "連28.3予",
            "連26.3",
        ),
        # 決算期変更後、新しい決算月の実績 (連25.12) が載っていれば通る
        (
            "連25.3\t100\t10\n連25.12\t80\t8\n連26.12予\t90\t9\n連27.12予\t120\t13\n",
            "連26.12予",
            "連27.12予",
            "連25.12",
        ),
        # 累計行 (9～2, 9～5) は通期でないので除外する。8月決算の SHIFT (3697) は
        # 累計開始月 9 が妥当な月なので月では弾けず、"連25.9～5" を通期実績と
        # 誤認して決算月を9と取り違え、通期予想を全て捨てて上期予想を今季に
        # していた (半期 ÷ 3Q累計で -32.7% という無意味な成長率になる)。
        (
            "連25.8\t129819\t15628\n連26.8予\t160000\t16000\n"
            "連27.8予\t175000\t18000\n連25.9～2\t72035\t6907\n"
            "連26.9～2予\t78000\t7500\n連25.9～5\t115848\t11383\n",
            "連26.8予",
            "連27.8予",
            "連25.8",
        ),
    ],
)
def test_parse_row_selection(text, this_label, next_year, prev_year):
    """予想・実績行の選別と、通期以外の行の除外を確認する。"""
    result = parse_shikiho_gyoseki(text)

    assert result["this_year"]["label"] == this_label
    assert (result["next_year"] or {}).get("label") == next_year
    assert (result["prev_year"] or {}).get("label") == prev_year
    if prev_year is None:
        assert result["this_year"]["sales_growth"] is None


@pytest.mark.parametrize(
    "text",
    [
        "連26.3\t100\t10\n連27.3\t120\t15\n",  # 予想行が1件も無い
        "見出しだけ\nテキスト\n",  # 期ラベルが無い
        "",  # 空入力
        # 予想が全て実績より過去 = 古い四季報。陳腐化した予想を保存させない
        "連25.3予\t100\t10\n連26.3\t110\t11\n連27.3\t120\t12\n",
        # 3月本決算だが貼付範囲に中間期予想 (連26.9予) しかない。決算期変更と
        # 区別できないため、半期の値を通期として保存せず拒否する
        "連25.3\t100,000\t10,000\n連26.3\t110,000\t11,000\n連26.9予\t55,000\t5,500\n",
        # 過去に決算月を変更した会社 (連23.12 → 連25.3) でも同じ。実績行の月が
        # 複数あることを「決算期変更中」の根拠にはできない
        "連23.12\t80,000\t8,000\n連25.3\t100,000\t10,000\n連25.9予\t55,000\t5,500\n",
    ],
)
def test_parse_raises_without_forecast(text):
    """使える予想通期行が取れなければ ValueError にする (保存させない)。"""
    with pytest.raises(ValueError):
        parse_shikiho_gyoseki(text)


@pytest.mark.parametrize(
    "prev_op, expected",
    [
        (-500, None),   # 赤字からの回復は率で語れない
        (0, None),      # ゼロ除算
        (None, None),   # 前期の数値欠損
    ],
)
def test_growth_is_none_for_invalid_base(prev_op, expected):
    """分母が負/0/欠損のとき成長率を None にする。"""
    op_cell = "-" if prev_op is None else f"{prev_op}"
    text = f"連26.3\t1,000\t{op_cell}\n連27.3予\t1,200\t300\n"

    result = parse_shikiho_gyoseki(text)

    assert result["this_year"]["op_growth"] is expected
    # 売上側は正常に計算できる (欠損は営業利益列のみ)
    assert result["this_year"]["sales_growth"] == 20.0


def test_non_numeric_cell_does_not_shift_columns():
    """営業利益が非開示 ("-") の行で、経常利益を繰り上げて拾わない。

    「数値としてパースできた順に2つ」方式だと経常利益が営業利益として保存され、
    もっともらしい誤値が DB・AI 分析まで流れる (銀行・REIT等で現実に起こる)。
    """
    text = "連26.3\t60,000\t-\t3,000\n連27.3予\t70,000\t-\t3,500\n"

    result = parse_shikiho_gyoseki(text)

    assert result["this_year"]["sales"] == 70000
    assert result["this_year"]["op_profit"] is None  # 3500 (経常利益) を拾わない
    assert result["this_year"]["op_growth"] is None
    assert result["this_year"]["sales_growth"] == 16.7
