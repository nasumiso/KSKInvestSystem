"""shihyou.py の計算関数テスト"""

import pytest
import shihyou


# ==================================================
# get_from_kabutan
# ==================================================
class TestGetFromKabutan:
    """株探HTML財務テーブルからの指標抽出テスト"""

    @staticmethod
    def _build_zaimu_html(jiko_ratio="50.0", debt_ratio="0.5",
                          roe="15.0", profit_margin="10.0",
                          prev_jiko="45.0", prev_debt="0.8",
                          prev_roe="12.0", prev_profit="8.0"):
        """財務テーブルとROEテーブルを含む最小限HTMLを生成"""
        return (
            '財務 【実績】<table>'
            '<tr ><td>2022</td><td>{prev_jiko}</td><td>x</td><td>x</td>'
            '<td>x</td><td>{prev_debt}</td></tr>'
            '<tr ><td>2023</td><td>{jiko}</td><td>x</td><td>x</td>'
            '<td>x</td><td>{debt}</td></tr>'
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
        )

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
        assert "利益率8%" in result  # int
        assert "負債0.35" in result
        assert "自己55%" in result  # int

    def test_missing_data(self):
        """shihyo空・market_capなしの場合もエラーにならない"""
        result = shihyou.get_shihyo_expr({"market_cap": 0, "shihyo": {}})
        assert "PER" in result
        result = shihyou.get_shihyo_expr({"shihyo": {"MPER": 10}})
        assert "0億" in result
        assert "PER10" in result
