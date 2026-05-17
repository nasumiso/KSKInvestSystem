"""shihyou.py の計算関数テスト"""

import pytest
import shihyou


# ==================================================
# get_from_kabutan
# ==================================================
def _build_cf_html(cash="9000", prev_cash="8000",
                   period="2023.03", prev_period="2022.03"):
    """キャッシュフロー推移テーブル(cashflow_name アンカー直後)のHTMLを生成。
    cash/prev_cash は現金等残高(百万円)。td[5]に配置。"""
    return (
        '<a name="cashflow_name" id="cashflow_name"></a>'
        '<table>'
        '<tr ><th scope="row">{prev_period}</th>'
        '<td>x</td><td>x</td><td>x</td><td>x</td><td>x</td>'
        '<td>{prev_cash}</td><td>x</td></tr>'
        '<tr ><th scope="row">{period}</th>'
        '<td>x</td><td>x</td><td>x</td><td>x</td><td>x</td>'
        '<td>{cash}</td><td>x</td></tr>'
        '</table>'
    ).format(
        cash=cash,
        prev_cash=prev_cash,
        period=period,
        prev_period=prev_period,
    )


class TestGetFromKabutan:
    """株探HTML財務テーブルからの指標抽出テスト"""

    @staticmethod
    def _build_zaimu_html(jiko_ratio="50.0", debt_ratio="0.5",
                          roe="15.0", profit_margin="10.0",
                          prev_jiko="45.0", prev_debt="0.8",
                          prev_roe="12.0", prev_profit="8.0",
                          jikoshihon_mil="20000", prev_jikoshihon_mil="18000",
                          cf_html=None,
                          latest_period="2023.03", prev_period="2022.03"):
        """財務テーブルとROEテーブルを含む最小限HTMLを生成。
        jikoshihon_mil: 自己資本(百万円, items[3])
        latest_period: 最終行の期表記(<th>内)。"YY.MM-MM" 形式なら中間期扱いとなる
        cf_html: キャッシュフロー推移テーブルHTML(None=含めない)"""
        zaimu = (
            '財務 【実績】<table>'
            '<tr ><th scope="row">{prev_period}</th>'
            '<td>x</td><td>{prev_jiko}</td><td>x</td>'
            '<td>{prev_jikoshihon}</td><td>x</td><td>{prev_debt}</td></tr>'
            '<tr ><th scope="row">{latest_period}</th>'
            '<td>x</td><td>{jiko}</td><td>x</td>'
            '<td>{jikoshihon}</td><td>x</td><td>{debt}</td></tr>'
            '</table>'
            '<table><th scope="col" class="fb_02">　ＲＯＥ</th>'
            '<tbody>'
            '<tr ><td>2022</td><td>x</td><td>{prev_profit}</td><td>{prev_roe}</td></tr>'
            '<tr ><td>2023</td><td>x</td><td>{profit}</td><td>{roe}</td></tr>'
            '</tbody></table>'
        ).format(
            jiko=jiko_ratio, debt=debt_ratio,
            roe=roe, profit=profit_margin,
            prev_jiko=prev_jiko, prev_debt=prev_debt,
            prev_roe=prev_roe, prev_profit=prev_profit,
            jikoshihon=jikoshihon_mil, prev_jikoshihon=prev_jikoshihon_mil,
            latest_period=latest_period, prev_period=prev_period,
        )
        if cf_html is not None:
            zaimu += cf_html
        return zaimu

    def test_normal(self):
        """正常系: 全指標が正しく抽出される"""
        html = self._build_zaimu_html()
        result = shihyou.get_from_kabutan(html)
        assert result["debt_ratio"] == pytest.approx(0.5)
        assert result["capital_ratio"] == pytest.approx(50.0)
        assert result["ROE"] == pytest.approx(15.0)
        assert result["profit_margin"] == pytest.approx(10.0)

    def test_empty_html(self):
        """空HTMLの場合は空辞書を返す"""
        assert shihyou.get_from_kabutan("") == {}
        assert shihyou.get_from_kabutan(None) == {}

    def test_no_zaimu_table(self):
        """財務テーブルが見つからない場合"""
        result = shihyou.get_from_kabutan("<html>nothing</html>")
        assert result == {}

    def test_dash_fallback(self):
        """ダッシュ(－)の場合は前期データにフォールバック"""
        html = self._build_zaimu_html(debt_ratio="－", prev_debt="0.8")
        result = shihyou.get_from_kabutan(html)
        assert result["debt_ratio"] == pytest.approx(0.8)

    def test_dash_both_rows(self):
        """両期ともダッシュ(－)の場合は0になる"""
        html = self._build_zaimu_html(debt_ratio="－", prev_debt="－")
        result = shihyou.get_from_kabutan(html)
        assert result["debt_ratio"] == pytest.approx(0.0)

    def test_no_roe_table(self):
        """ROEテーブルがない場合でも財務データは返す"""
        html = (
            '財務 【実績】<table>'
            '<tr ><td>2023</td><td>50.0</td><td>x</td><td>x</td>'
            '<td>x</td><td>0.5</td></tr>'
            '</table>'
        )
        result = shihyou.get_from_kabutan(html)
        assert "debt_ratio" in result
        assert "capital_ratio" in result
        assert "ROE" not in result

    def test_roe_fallback_zero(self):
        """ROEが0の場合は前期にフォールバック"""
        html = self._build_zaimu_html(roe="0", profit_margin="0",
                                      prev_roe="12.0", prev_profit="8.0")
        result = shihyou.get_from_kabutan(html)
        assert result["ROE"] == pytest.approx(12.0)
        assert result["profit_margin"] == pytest.approx(8.0)

    def test_jikoshihon_extraction(self):
        """自己資本(実額・億円)の抽出: items[4]=百万円→億円換算"""
        # 20000百万円 = 200億円
        html = self._build_zaimu_html(jikoshihon_mil="20000")
        result = shihyou.get_from_kabutan(html)
        assert result["jikoshihon"] == pytest.approx(200.0)

    def test_jikoshihon_dash_fallback(self):
        """自己資本がダッシュ(－)の場合は前期にフォールバック"""
        html = self._build_zaimu_html(
            jikoshihon_mil="－", prev_jikoshihon_mil="15000"
        )
        result = shihyou.get_from_kabutan(html)
        assert result["jikoshihon"] == pytest.approx(150.0)

    def test_cash_equiv_extraction(self):
        """現金等残高(億円)が CFテーブルから抽出される"""
        cf = _build_cf_html(cash="9000")  # 9000百万円=90億円
        html = self._build_zaimu_html(cf_html=cf)
        result = shihyou.get_from_kabutan(html)
        assert result["cash_equiv"] == pytest.approx(90.0)

    def test_cash_equiv_missing_anchor(self):
        """CFテーブル(cashflow_name アンカー)がないHTMLでは cash_equiv キーが入らない"""
        html = self._build_zaimu_html()  # cf_html=None
        result = shihyou.get_from_kabutan(html)
        assert "cash_equiv" not in result

    def test_jikoshihon_skips_quarter_row(self):
        """最終行が中間期(YY.MM-MM)なら、EVR用の自己資本は通期(YYYY.MM)行から取る"""
        # 通期2023.03の自己資本=200億, 中間期25.04-12の自己資本=350億
        # → EVR用の jikoshihon は 通期の 200億 になることを確認
        html = (
            '財務 【実績】<table>'
            '<tr ><th scope="row">2023.03</th>'
            '<td>x</td><td>50.0</td><td>x</td>'
            '<td>20000</td><td>x</td><td>0.4</td></tr>'
            '<tr ><th scope="row">25.04-12</th>'
            '<td>x</td><td>55.0</td><td>x</td>'
            '<td>35000</td><td>x</td><td>0.7</td></tr>'
            '</table>'
        )
        result = shihyou.get_from_kabutan(html)
        # 既存の debt_ratio/capital_ratio は最終行(中間期)から
        assert result["debt_ratio"] == pytest.approx(0.7)
        assert result["capital_ratio"] == pytest.approx(55.0)
        # EVR用 jikoshihon と debt_ratio_annual は通期行から
        assert result["jikoshihon"] == pytest.approx(200.0)  # 20000百万円→200億
        assert result["debt_ratio_annual"] == pytest.approx(0.4)

    def test_evr_period_fallback_aligns_cash(self):
        """最新通期のEVR用BS値が欠損なら、現金もフォールバック先の通期に合わせる"""
        cf = _build_cf_html(
            cash="9000",
            prev_cash="8000",
            period="2023.03",
            prev_period="2022.03",
        )
        html = self._build_zaimu_html(
            jikoshihon_mil="－",
            debt_ratio="－",
            prev_jikoshihon_mil="18000",
            prev_debt="0.8",
            latest_period="2023.03",
            prev_period="2022.03",
            cf_html=cf,
        )
        result = shihyou.get_from_kabutan(html)
        assert result["evr_period"] == "2022.03"
        assert result["jikoshihon"] == pytest.approx(180.0)
        assert result["debt_ratio_annual"] == pytest.approx(0.8)
        assert result["cash_equiv"] == pytest.approx(80.0)

    def test_period_normalization_with_kessan_kubun(self):
        """期表記に <span class="kubun1">連</span> や末尾 "*" が混入していても通期として認識する。
        実HTMLでは新興上場銘柄(402A等)で "連　2023.05*" のように出る。"""
        # 財務とCFで同じ正規化された期が evr_period として使われることを確認
        zaimu = (
            '財務 【実績】<table>'
            '<tr ><th scope="row">'
            '<span class="kubun1">連&nbsp;&nbsp;</span>2023.05*&nbsp;&nbsp;</th>'
            '<td>x</td><td>50.0</td><td>x</td>'
            '<td>20000</td><td>x</td><td>0.4</td></tr>'
            '<tr ><th scope="row">'
            '<span class="kubun1">連&nbsp;&nbsp;</span>25.06-02&nbsp;&nbsp;</th>'
            '<td>x</td><td>48.0</td><td>x</td>'
            '<td>30000</td><td>x</td><td>0.7</td></tr>'
            '</table>'
        )
        cf = (
            '<a name="cashflow_name" id="cashflow_name"></a>'
            '<table>'
            '<tr ><th scope="row">'
            '<span class="kubun1">&nbsp;&nbsp;</span>2023.05*&nbsp;&nbsp;</th>'
            '<td>x</td><td>x</td><td>x</td><td>x</td><td>x</td>'
            '<td>5000</td><td>x</td></tr>'
            '</table>'
        )
        result = shihyou.get_from_kabutan(zaimu + cf)
        # 通期 "2023.05*" → "2023.05" に正規化されてマッチする
        assert result["evr_period"] == "2023.05"
        assert result["jikoshihon"] == pytest.approx(200.0)
        assert result["debt_ratio_annual"] == pytest.approx(0.4)
        # 現金もCFテーブルの 2023.05* 行から正規化された期で突き合わせ取得
        assert result["cash_equiv"] == pytest.approx(50.0)

    def test_jikoshihon_no_annual_row(self):
        """通期行が無い(全部中間期)場合、EVR用キーは付与されない"""
        html = (
            '財務 【実績】<table>'
            '<tr ><th scope="row">25.01-03</th>'
            '<td>x</td><td>50.0</td><td>x</td>'
            '<td>20000</td><td>x</td><td>0.5</td></tr>'
            '<tr ><th scope="row">25.04-12</th>'
            '<td>x</td><td>55.0</td><td>x</td>'
            '<td>35000</td><td>x</td><td>0.7</td></tr>'
            '</table>'
        )
        result = shihyou.get_from_kabutan(html)
        assert "jikoshihon" not in result
        assert "debt_ratio_annual" not in result


# ==================================================
# parse_cash_kabutan
# ==================================================
class TestParseCashKabutan:
    """CFテーブルから現金等残高(億円)抽出テスト"""

    def test_normal(self):
        """正常系: 最新行のtd[5]が現金等残高(百万円→億円)"""
        html = _build_cf_html(cash="9000", prev_cash="8000")
        assert shihyou.parse_cash_kabutan(html) == pytest.approx(90.0)

    def test_no_anchor(self):
        """cashflow_name アンカーがない場合は None"""
        html = '<table><tr ><td>1</td></tr></table>'
        assert shihyou.parse_cash_kabutan(html) is None

    def test_no_rows(self):
        """テーブルに <tr > 行がない場合は None"""
        html = '<a name="cashflow_name"></a><table><thead><tr><th>x</th></tr></thead></table>'
        assert shihyou.parse_cash_kabutan(html) is None

    def test_dash_fallback_to_prev(self):
        """最新行が－の場合は前期にフォールバック"""
        html = _build_cf_html(cash="－", prev_cash="8000")
        assert shihyou.parse_cash_kabutan(html) == pytest.approx(80.0)

    def test_target_period(self):
        """target_period 指定時は同じ期の現金等残高だけを返す"""
        html = _build_cf_html(
            cash="9000",
            prev_cash="8000",
            period="2023.03",
            prev_period="2022.03",
        )
        assert shihyou.parse_cash_kabutan(
            html, target_period="2022.03"
        ) == pytest.approx(80.0)

    def test_target_period_does_not_fallback_to_other_period(self):
        """target_period 指定時に対象期が欠損なら他期へフォールバックしない"""
        html = _build_cf_html(
            cash="9000",
            prev_cash="8000",
            period="2023.03",
            prev_period="2022.03",
        )
        assert shihyou.parse_cash_kabutan(html, target_period="2021.03") is None

    def test_too_few_tds(self):
        """tdが6個未満の場合は None"""
        html = (
            '<a name="cashflow_name"></a>'
            '<table><tr ><td>1</td><td>2</td></tr></table>'
        )
        assert shihyou.parse_cash_kabutan(html) is None


# ==================================================
# parse_jikasogaku_kabutan
# ==================================================
class TestParseJikasogakuKabutan:
    """時価総額抽出テスト"""

    def test_normal(self):
        """通常の億円表記"""
        html = '<td colspan="2" class="v_zika2">1,234<span>億円</span></td>'
        assert shihyou.parse_jikasogaku_kabutan(html) == pytest.approx(1234.0)

    def test_small(self):
        """小数点付き"""
        html = '<td colspan="2" class="v_zika2">56.7<span>億円</span></td>'
        assert shihyou.parse_jikasogaku_kabutan(html) == pytest.approx(56.7)

    def test_trillion(self):
        """兆円表記"""
        html = '<td colspan="2" class="v_zika2">11<span>兆</span>5899<span>億円</span></td>'
        assert shihyou.parse_jikasogaku_kabutan(html) == pytest.approx(115899.0)

    def test_trillion_with_comma(self):
        """兆円表記（カンマ付き）"""
        html = '<td colspan="2" class="v_zika2">1<span>兆</span>2,345<span>億円</span></td>'
        assert shihyou.parse_jikasogaku_kabutan(html) == pytest.approx(12345.0)

    def test_no_match(self):
        """マッチしない・空文字列の場合は0を返す"""
        assert shihyou.parse_jikasogaku_kabutan("<html></html>") == 0
        assert shihyou.parse_jikasogaku_kabutan("") == 0


# ==================================================
# get_from_kabutan_base
# ==================================================
class TestGetFromKabutanBase:
    """PER/PBR/PSR/MPER計算テスト"""

    @staticmethod
    def _build_base_html(jikasogaku="100",
                         per="10.5", pbr="1.2", credit="3.5",
                         uriage="50", keijo="10", saishu="7"):
        """stockinfo_i3とgyouseki_blockを含むHTML生成"""
        jika_html = '<td colspan="2" class="v_zika2">{jika}<span>億円</span></td>'.format(
            jika=jikasogaku
        )
        stockinfo = (
            '<div id="stockinfo_i3">\r\n'
            '<td>{per}<span>倍</span></td>\r\n'
            '<td>{pbr}<span>倍</span></td>\r\n'
            '<td>{credit}<span>倍</span></td>\r\n'
            '<td>2.5<span>％</span></td>\r\n'
            '</div>'
        ).format(per=per, pbr=pbr, credit=credit)
        gyoseki = (
            '<div class="gyouseki_block">\r\n'
            '<div class="title"><table>\r\n'
            "<tr>\r\n"
            "    <th scope='row'><span class=\"kubun1\">連</span>2024.03&nbsp;</th>"
            "<td>{uriage}</td>\r\n"
            "    <td>{keijo}</td>\r\n"
            "    <td>{saishu}</td>\r\n"
            "</tr>\r\n"
            '</table>\r\n</div>'
        ).format(uriage=uriage, keijo=keijo, saishu=saishu)
        return jika_html + stockinfo + gyoseki

    def test_jikasogaku_zero_early_return(self):
        """時価総額0の場合は早期リターン"""
        html = '<html>no market cap</html>'
        result = shihyou.get_from_kabutan_base(html, {})
        assert result["jikasogaku"] == 0

    def test_per_pbr_extraction(self):
        """PER・PBRの抽出"""
        html = self._build_base_html()
        result = shihyou.get_from_kabutan_base(html, {})
        assert result["PER"] == pytest.approx(10.5)
        assert result["PBR"] == pytest.approx(1.2)

    def test_psr_calculation(self):
        """PSR計算（時価総額/売上高）"""
        html = self._build_base_html(jikasogaku="100", uriage="50")
        result = shihyou.get_from_kabutan_base(html, {})
        # PSR = 100 / 50 = 2.0
        assert result["PSR"] == pytest.approx(2.0)

    def test_mper_calculation(self):
        """MPER計算"""
        # keijo=10, saishu=7 → profit(7) >= keijo*0.6(6) and <= keijo*0.7(7)
        # → MPER = jikasogaku/profit = 100/7 ≈ 14.3
        html = self._build_base_html(jikasogaku="100", keijo="10", saishu="7")
        result = shihyou.get_from_kabutan_base(html, {})
        assert result["MPER"] == pytest.approx(14.3, abs=0.1)

    def test_mper_modified(self):
        """修正MPER計算（利益が経常利益の60-70%範囲外の場合）"""
        # keijo=10, saishu=5 → profit(5) < keijo*0.6(6)
        # → 修正PER適用: MPER = jikasogaku/(keijo*0.65) = 100/6.5 ≈ 15.4
        html = self._build_base_html(jikasogaku="100", keijo="10", saishu="5")
        result = shihyou.get_from_kabutan_base(html, {})
        assert result["MPER"] == pytest.approx(15.4, abs=0.1)

    def test_credit_ratio(self):
        """信用倍率の抽出"""
        html = self._build_base_html(credit="3.5")
        result = shihyou.get_from_kabutan_base(html, {})
        assert result["credit_ratio"] == pytest.approx(3.5)

    def test_dividend_yield(self):
        """配当利回りの抽出"""
        html = self._build_base_html()
        result = shihyou.get_from_kabutan_base(html, {})
        assert result["dividend_yield"] == pytest.approx(2.5)

    def test_ev_sales_calculation(self):
        """EVR計算: Issue記載の例 時価500・負債100(=jiko200×0.5)・現金50・売上300 → EV=550, EVR≒1.8"""
        html = self._build_base_html(jikasogaku="500", uriage="300")
        result = shihyou.get_from_kabutan_base(
            html,
            {"debt_ratio_annual": 0.5, "jikoshihon": 200.0, "cash_equiv": 50.0},
        )
        assert result["EV_Sales"] == pytest.approx(1.8)
        assert "EV_Sales_approx" not in result

    def test_ev_sales_low_debt_high_cash(self):
        """無借金高キャッシュ: EVR < PSR (時価100, 売上50, debt=0, cash=50)"""
        # PSR=100/50=2.0, EV=100+0-50=50, EVR=50/50=1.0
        html = self._build_base_html(jikasogaku="100", uriage="50")
        result = shihyou.get_from_kabutan_base(
            html,
            {"debt_ratio_annual": 0.0, "jikoshihon": 100.0, "cash_equiv": 50.0},
        )
        assert result["PSR"] == pytest.approx(2.0)
        assert result["EV_Sales"] == pytest.approx(1.0)
        assert result["EV_Sales"] < result["PSR"]

    def test_ev_sales_high_debt(self):
        """高負債: EVR > PSR (時価100, 売上50, jiko=100, debt_ratio=3 → 負債300)"""
        # PSR=100/50=2.0, EV=100+300-0=400, EVR=400/50=8.0
        html = self._build_base_html(jikasogaku="100", uriage="50")
        result = shihyou.get_from_kabutan_base(
            html,
            {"debt_ratio_annual": 3.0, "jikoshihon": 100.0, "cash_equiv": 0.0},
        )
        assert result["PSR"] == pytest.approx(2.0)
        assert result["EV_Sales"] == pytest.approx(8.0)
        assert result["EV_Sales"] > result["PSR"]

    def test_ev_sales_negative(self):
        """ネットキャッシュ企業: EVが負になる(時価100, 負債=0, 現金150, 売上50) → EVR=-1.0"""
        # EV = 100 + 0 - 150 = -50, EVR = -50/50 = -1.0
        html = self._build_base_html(jikasogaku="100", uriage="50")
        result = shihyou.get_from_kabutan_base(
            html,
            {"debt_ratio_annual": 0.0, "jikoshihon": 100.0, "cash_equiv": 150.0},
        )
        assert result["EV_Sales"] == pytest.approx(-1.0)

    def test_ev_sales_missing_cash_approx(self):
        """cash_equiv 未提供の場合は現金=0で近似計算し EV_Sales_approx=True"""
        html = self._build_base_html(jikasogaku="500", uriage="300")
        result = shihyou.get_from_kabutan_base(
            html,
            {"debt_ratio_annual": 0.5, "jikoshihon": 200.0},
        )
        # EV = 500 + 100 - 0 = 600, EVR = 600/300 = 2.0
        assert result["EV_Sales"] == pytest.approx(2.0)
        assert result.get("EV_Sales_approx") is True

    def test_ev_sales_missing_debt_ratio(self):
        """debt_ratio_annual/jikoshihon が無い場合は EV_Sales 未設定(PSRフォールバック対象)"""
        html = self._build_base_html(jikasogaku="100", uriage="50")
        result = shihyou.get_from_kabutan_base(html, {})
        assert "EV_Sales" not in result
        # PSR は通常通り計算される
        assert result["PSR"] == pytest.approx(2.0)

    def test_ev_sales_uses_annual_debt_not_quarter(self):
        """期ズレ防止: shiyo_data に debt_ratio(中間期)があっても EVR は debt_ratio_annual を優先する"""
        html = self._build_base_html(jikasogaku="500", uriage="300")
        # 中間期 debt_ratio=2.0 を入れても無視され、通期 debt_ratio_annual=0.5 が使われる想定
        result = shihyou.get_from_kabutan_base(
            html,
            {
                "debt_ratio": 2.0,         # 中間期（既存キー）→EVR計算では使わない
                "debt_ratio_annual": 0.5,  # 通期（新キー）→こちらを使う
                "jikoshihon": 200.0,
                "cash_equiv": 50.0,
            },
        )
        # 通期=0.5 を使った場合: 負債100, EV=550, EVR=1.8
        # 中間期=2.0 を使った場合: 負債400, EV=850, EVR=2.8 になってしまう
        assert result["EV_Sales"] == pytest.approx(1.8)


# ==================================================
# get_credit_expr
# ==================================================
class TestGetCreditExpr:
    """信用情報フォーマットテスト"""

    def test_normal(self):
        """正常系: 信用倍率と出来高買残倍率を表示"""
        stock_data = {
            "shihyo": {
                "credit_ratio": 5.23,
                "credit_buy": 100000,
            },
            "avg_volume_d": [200000],
        }
        result = shihyou.get_credit_expr(stock_data)
        # credit_ratio=5.23, volume_creditbuy=100000/200000=0.5
        assert "売5.23" in result
        assert "出0.50" in result

    def test_missing_fields(self):
        """信用倍率/買残/出来高が欠損した場合のフォールバック"""
        # 信用倍率なし
        result = shihyou.get_credit_expr({"shihyo": {}, "avg_volume_d": [200000]})
        assert "売," in result
        # 買残なし
        result = shihyou.get_credit_expr({"shihyo": {"credit_ratio": 3.0}, "avg_volume_d": [200000]})
        assert "売3.0" in result and result.endswith(",出")
        # 出来高0（ゼロ除算ガード）
        result = shihyou.get_credit_expr({"shihyo": {"credit_buy": 100000}, "avg_volume_d": [0]})
        assert result.endswith(",出")

    def test_volume_ratio_medium(self):
        """出来高買残倍率が1～10の場合は小数1桁"""
        stock_data = {
            "shihyo": {"credit_buy": 500000},
            "avg_volume_d": [100000],
        }
        result = shihyou.get_credit_expr(stock_data)
        # 500000/100000 = 5.0
        assert "出5.0" in result

    def test_volume_ratio_large(self):
        """出来高買残倍率が10以上の場合は整数"""
        stock_data = {
            "shihyo": {"credit_buy": 2000000},
            "avg_volume_d": [100000],
        }
        result = shihyou.get_credit_expr(stock_data)
        # 2000000/100000 = 20
        assert "出20" in result


# ==================================================
# get_shihyo_expr
# ==================================================
class TestGetShihyoExpr:
    """指標フォーマットテスト"""

    def test_full_indicators(self):
        """全指標あり"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {
                "MPER": 15.3,
                "PBR": 1.25,
                "PSR": 2.5,
                "dividend_yield": 3.2,
                "ROE": 12,
                "profit_margin": 8.5,
                "debt_ratio": 0.35,
                "capital_ratio": 55.0,
            },
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "500億" in result
        assert "PER15" in result  # MPER=15.3 → int=15
        assert "PBR1.2" in result  # keta=1 → round(1.25, 1) = 1.2
        assert "PSR2.5" in result
        assert "配当3.2" in result
        assert "ROE12" in result
        assert "利益率8.5%" in result  # 1桁台は小数1桁
        assert "負債0.35" in result
        assert "自己55%" in result  # int

    def test_missing_data(self):
        """shihyo空・market_capなしの場合もエラーにならない"""
        result = shihyou.get_shihyo_expr({"market_cap": 0, "shihyo": {}})
        assert "PER" in result
        result = shihyou.get_shihyo_expr({"shihyo": {"MPER": 10}})
        assert "0億" in result
        assert "PER10" in result

    def test_ev_sales_display(self):
        """EV_Sales があれば EVR{値} を表示し PSR は出さない"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {"MPER": 15, "PBR": 1.2, "EV_Sales": 2.5, "PSR": 3.0},
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "EVR2.5" in result
        assert "PSR" not in result

    def test_ev_sales_approx_display(self):
        """EV_Sales_approx=True の場合は EVR{値}~ 表記"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {
                "MPER": 15, "PBR": 1.2,
                "EV_Sales": 2.5, "EV_Sales_approx": True,
            },
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "EVR2.5~" in result

    def test_psr_fallback_display(self):
        """EV_Sales 欠損で PSR にフォールバック"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {"MPER": 15, "PBR": 1.2, "PSR": 3.0},
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "PSR3.0" in result
        assert "EVR" not in result

    def test_ev_sales_negative_display(self):
        """EV_Sales が負値（ネットキャッシュ企業）でも文字列に含まれる"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {"MPER": 15, "PBR": 1.2, "EV_Sales": -0.3},
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "EVR-0.3" in result

    def test_profit_margin_two_digits_int(self):
        """利益率の整数部が2桁以上は整数表示 (例: 12.7 → 12%)"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {"MPER": 15, "PBR": 1.2, "profit_margin": 12.7},
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "利益率12%" in result
        assert "12.7" not in result

    def test_profit_margin_one_digit_decimal(self):
        """利益率の整数部が1桁は小数1桁表示 (例: 2.5 → 2.5%)"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {"MPER": 15, "PBR": 1.2, "profit_margin": 2.5},
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "利益率2.5%" in result

    def test_profit_margin_negative_one_digit(self):
        """利益率が負値で1桁台 (例: -3.4 → -3.4%)"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {"MPER": 15, "PBR": 1.2, "profit_margin": -3.4},
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "利益率-3.4%" in result

    def test_profit_margin_negative_two_digits(self):
        """利益率が負値で2桁以上 (例: -15.6 → -15%)"""
        stock_data = {
            "market_cap": 500,
            "shihyo": {"MPER": 15, "PBR": 1.2, "profit_margin": -15.6},
        }
        result = shihyou.get_shihyo_expr(stock_data)
        assert "利益率-15%" in result
        assert "-15.6" not in result
