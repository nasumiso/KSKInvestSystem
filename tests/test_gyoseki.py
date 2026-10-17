"""gyoseki.py の計算関数テスト"""

import pytest
import gyoseki


# ==================================================
# calc_growth_tags (issue #121)
# ==================================================
@pytest.mark.parametrize("sales,profit,expected", [
    # 古い順に7四半期。直近3期 (末尾3つ) を、その4期前 (先頭3つ) と比べる
    ([100, 100, 100, 100, 110, 120, 130], [10, 10, 10, 10, 12, 15, 20], "収利率"),
    ([100, 100, 100, 100, 110, 120, 130], [10] * 7, "収"),
    ([100, 100, 100, 100, 200, 140, 130], [10, 10, 10, 10, 16, 18, 21], "利"),
    ([100] * 7, [10, 10, 10, 10, 12, 12, 12], "率"),
    # 前年同期に赤字がある → 利は付かない (林式の伸び率が混ざるため)
    ([100, 100, 100, 100, 110, 120, 130], [-5, 10, 10, 10, 12, 15, 20], "収率"),
    # 減り方が鈍っただけ (-20 → -10 → -5%) は加速と見ない
    ([100, 100, 100, 100, 110, 120, 130], [10, 10, 10, 10, 8, 9, 9.5], "収"),
    # 7期そろわない・欠損セルがある → 判定しない
    ([100, 100, 100, 110, 120, 130], [10, 10, 10, 12, 15, 20], ""),
    (["－", 100, 100, 100, 110, 120, 130], [10, 10, 10, 10, 12, 15, 20], ""),
])
def test_calc_growth_tags(sales, profit, expected):
    tbl = [[f"q{i}", s, p] for i, (s, p) in enumerate(zip(sales, profit))]
    tbl.append(["前年同期比", "+1.0", "1.0"])  # 株探の四半期表の末尾行
    assert gyoseki.calc_growth_tags(tbl) == expected


# ==================================================
# calc_growth_rate
# ==================================================
class TestCalcGrowthRate:
    """成長率計算のテスト（-100~+100%）"""

    def test_normal(self):
        """正の成長・マイナス成長・変化なし"""
        assert gyoseki.calc_growth_rate(100, 120) == 20
        assert gyoseki.calc_growth_rate(100, 80) == -20
        assert gyoseki.calc_growth_rate(100, 100) == 0
        assert gyoseki.calc_growth_rate(0, 0) == 0

    def test_from_negative(self):
        """赤字からの成長（林式計算）"""
        result = gyoseki.calc_growth_rate(-100, 50)
        assert result == 100

    def test_large_growth(self):
        """大幅成長"""
        assert gyoseki.calc_growth_rate(100, 300) == 200

    @pytest.mark.parametrize("cur,nxt", [
        ("－", 0),      # 前年同期が欠損 (calc_quarter_growth で発火した実ケース)
        (100, "－"),    # 当期が欠損
        ("－", "－"),   # 両方欠損
    ])
    def test_missing_dash(self, cur, nxt):
        """欠損セル "－" (str) が混じっても TypeError で落ちず 0 を返す。

        業績パースは欠損を "－" のまま格納する仕様 (parse_kabutan_account2)。
        calc_growth_rate の cur>0 比較に str が到達し
        TypeError: '>' not supported between str and int で
        make_stock_db 全体が落ちた回帰の防止。
        """
        assert gyoseki.calc_growth_rate(cur, nxt) == 0


# ==================================================
# calc_growth_rate2
# ==================================================
class TestCalcGrowthRate2:
    """赤字考慮成長率計算のテスト"""

    def test_positive_to_positive(self):
        """正→正: 単純比率"""
        # 100 → 120 = 1.2
        assert gyoseki.calc_growth_rate2(100, 120) == pytest.approx(1.2)

    def test_both_zero(self):
        """ゼロ同士"""
        assert gyoseki.calc_growth_rate2(0, 0) == 1.0

    def test_negative_to_positive(self):
        """赤字→黒字"""
        result = gyoseki.calc_growth_rate2(-100, 50)
        # (50 - (-100)) / ((|50| + |-100|) * 0.5) = 150 / 75 = 2.0
        assert result == pytest.approx(2.0)

    def test_positive_to_negative(self):
        """黒字→赤字"""
        result = gyoseki.calc_growth_rate2(100, -50)
        # (-50 - 100) / ((|-50| + |100|) * 0.5) = -150 / 75 = -2.0
        assert result == pytest.approx(-2.0)


# ==================================================
# calc_cagr
# ==================================================
class TestCalcCagr:
    """年平均成長率（CAGR）のテスト"""

    def test_normal(self):
        """正常な成長と減少"""
        assert gyoseki.calc_cagr([100, 110, 121]) == pytest.approx(0.1, abs=0.001)
        assert gyoseki.calc_cagr([100, 90, 81]) == pytest.approx(-0.1, abs=0.001)
        assert gyoseki.calc_cagr([100, 100, 100]) == pytest.approx(0.0)

    def test_invalid_inputs(self):
        """空・1要素・初期値ゼロ/マイナス → 0"""
        assert gyoseki.calc_cagr([]) == 0
        assert gyoseki.calc_cagr([100]) == 0
        assert gyoseki.calc_cagr([0, 100, 200]) == 0
        assert gyoseki.calc_cagr([-50, 100, 200]) == 0


# ==================================================
# calc_gyoseki_score
# ==================================================
class TestCalcGyosekiScore:
    """業績スコアリングのテスト"""

    def test_empty_tables(self):
        """空テーブルのとき"""
        result = gyoseki.calc_gyoseki_score({})
        assert result == 20  # check_table 失敗時のデフォルト

    def test_insufficient_data(self):
        """データ不足のとき"""
        tables = {"gyoseki_current": [], "gyoseki_quarter": []}
        result = gyoseki.calc_gyoseki_score(tables)
        assert result == 20

    def test_with_sample_data(self):
        """サンプルデータでスコア計算が正常終了すること"""
        # 通期テーブル: [決算期, 売上高, 営業益, 経常益, 最終益, 一株益, 一株配]
        table_current = [
            ["2020.03", 10000, 1000, 1100, 800, 80, 20],
            ["2021.03", 11000, 1200, 1300, 900, 90, 22],
            ["2022.03", 12000, 1400, 1500, 1000, 100, 25],
            ["2023.03", 13000, 1600, 1700, 1100, 110, 28],
            ["2024.03", 14000, 1800, 1900, 1200, 120, 30],
            ["予2025.03", 15000, 2000, 2100, 1400, 140, 35],
            # 前期比行
            ["前期比", "+7.1", "+11.1", "+10.5", "+16.7", "+16.7", "+16.7"],
        ]
        # 四半期テーブル: 9行（今年4Q＋前年4Q＋前年同期比）
        table_quarter = [
            ["23.04-06", 3000, 400, 420, 280, 28, 5.0],  # 前年1Q
            ["23.07-09", 3100, 420, 440, 290, 29, 5.1],  # 前年2Q
            ["23.10-12", 3200, 440, 460, 300, 30, 5.2],  # 前年3Q
            ["24.01-03", 3300, 460, 480, 310, 31, 5.3],  # 前年4Q
            ["24.04-06", 3500, 500, 520, 350, 35, 5.5],  # 今年1Q
            ["24.07-09", 3600, 520, 540, 360, 36, 5.6],  # 今年2Q
            ["24.10-12", 3700, 540, 560, 370, 37, 5.7],  # 今年3Q
            ["25.01-03", 3800, 560, 580, 380, 38, 5.8],  # 今年4Q
            # 前年同期比行
            ["前年同期比", "+15.2", "+21.7", "+20.8", "+22.6", "+22.6", "+9.4"],
        ]
        tables = {
            "gyoseki_current": table_current,
            "gyoseki_quarter": table_quarter,
        }
        result = gyoseki.calc_gyoseki_score(tables)
        # スコアは0〜100の範囲になる想定
        assert isinstance(result, (int, float))
        assert result >= 0

    def test_quarter_with_missing_dash(self):
        """四半期テーブルに欠損 "－" が混在しても TypeError で落ちずスコアを返す。

        前年四半期セルが "－" (str) のまま数値比較に入り
        TypeError: '>=' not supported between str and int で全体が落ちた回帰の防止。
        """
        table_current = [
            ["2023.03", 13000, 1600, 1700, 1100, 110, 28],
            ["2024.03", 14000, 1800, 1900, 1200, 120, 30],
            ["予2025.03", 15000, 2000, 2100, 1400, 140, 35],
        ]
        table_quarter = [
            # 前年4Q: 売上・営利を欠損 "－" にする (上場間もない等で前年実績なし)
            ["23.04-06", "－", "－", 420, 280, 28, 5.0],
            ["23.07-09", "－", "－", 440, 290, 29, 5.1],
            ["23.10-12", "－", "－", 460, 300, 30, 5.2],
            ["24.01-03", 3300, 460, 480, 310, 31, 5.3],
            ["24.04-06", 3500, 500, 520, 350, 35, 5.5],
            ["24.07-09", 3600, 520, 540, 360, 36, 5.6],
            ["24.10-12", 3700, 540, 560, 370, 37, 5.7],
            ["25.01-03", 3800, 560, 580, 380, 38, 5.8],
            ["前年同期比", "+15.2", "+21.7", "+20.8", "+22.6", "+22.6", "+9.4"],
        ]
        tables = {
            "gyoseki_current": table_current,
            "gyoseki_quarter": table_quarter,
        }
        result = gyoseki.calc_gyoseki_score(tables)
        assert isinstance(result, (int, float))
        assert result >= 0


# ==================================================
# 決算発表日・業績修正日 (issue #512)
# ==================================================

# 株探の業績ページを、日付の取り出しに関わる部分だけに削ったもの
_KESSAN_HTML = """
<div class="fin_year_t0_d fin_year_forecast_d dispnone">
<table>
<tr><td class="td_hpd" rowspan="3">2025.11</td>
<td class="tac fs14">25/01/14</td>
<td class="tal fs12">初</td></tr>
<tr><td class="tac fs14">25/10/07</td>
<td class="tal fs12">修</td>
<td class="nopm"><table><tr><td>プレミアム</td></tr></table></td></tr>
<tr class="bggray"><td class="tac fs14">26/01/13</td>
<td class="tal fs12">実</td></tr>
<tr><td class="tac fs14">26/07/07</td>
<td class="tal fs12">修</td></tr>
</table>
</div><!--/fin_year_forecast_d-->
<div class="fin_half_t0_d fin_half_forecast_d dispnone">
<table>
<tr><td class="tac fs14">26/05/14</td>
<td class="tal fs12">初</td></tr>
<tr><td class="tac fs14">26/09/24</td>
<td class="tal fs12">修</td></tr>
</table>
</div><!--/fin_half_forecast_d-->
<div class="title1">3ヵ月決算【実績】</div>
<table>
<tr  ><th scope="row">25.12-02</th><td>1,200</td>
<td class="fb_pdf1"><a href="x" target="pdf">26/04/14<img src="p.gif" /></a></td></tr>
<tr  ><th scope="row">26.03-05</th><td>1,386</td>
<td class="fb_pdf1"><a href="x" target="pdf">26/07/07<img src="p.gif" /></a></td></tr>
<tr  ><th scope="row">26.06-08</th><td>－</td><td>－</td></tr>
</table>
"""


@pytest.mark.parametrize("html, expected", [
    (_KESSAN_HTML, {"kessan": ["2026-04-14", "2026-07-07"],
                    "shusei": ["2025-10-07", "2026-07-07", "2026-09-24"]}),
    ("<html>フォーマット変更</html>", {"kessan": [], "shusei": []}),
    ("", {"kessan": [], "shusei": []}),
])
def test_parse_kessan_event_dates(html, expected):
    """決算発表日と、通期・上期の「修」の修正日だけを、4桁の年で取る。表が無ければ空。"""
    assert gyoseki.parse_kessan_event_dates(html) == expected


def test_merge_kessan_dates_keeps_existing(tmp_path, monkeypatch):
    """株探の表から消えた古い日付を残して足し込む。追加が無ければ書かない。"""
    monkeypatch.setattr(gyoseki, "KESSAN_DATES_DIR", str(tmp_path))
    assert gyoseki.merge_kessan_dates("215A", {"kessan": ["2024-10-09"], "shusei": []})
    assert gyoseki.merge_kessan_dates("215A", {"kessan": ["2026-07-07"], "shusei": ["2026-07-07"]})
    assert not gyoseki.merge_kessan_dates("215A", {"kessan": ["2026-07-07"], "shusei": []})
    assert gyoseki.load_kessan_dates("215A") == {
        "kessan": ["2024-10-09", "2026-07-07"], "shusei": ["2026-07-07"]}
    assert gyoseki.load_kessan_dates("9999") == {"kessan": [], "shusei": []}
