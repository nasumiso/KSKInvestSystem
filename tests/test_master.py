"""master.py の計算関数テスト"""

import pytest
import master


# ==================================================
# parse_master_html_kabutan
# ==================================================
class TestParseMasterHtmlKabutan:
    """株探基本情報HTMLパースのテスト"""

    @staticmethod
    def _build_html(code="1234", stock_name="テスト銘柄",
                    market="東証プライム", sector="情報・通信業",
                    sector_id="36", market_id="1",
                    jikasogaku="500", overview="テスト概要",
                    themes=None, relates=None,
                    kessan="2025/02/14", purchase="150,000",
                    corporate_url="https://example.com"):
        """最小限の株探基本情報HTMLを生成"""
        if themes is None:
            themes = ["AI", "半導体"]
        if relates is None:
            relates = ["5678", "9012"]

        theme_html = "".join(
            '<li><a href="/themes?theme={t}">{t}</a></li>'.format(t=t)
            for t in themes
        )
        relate_html = "".join(
            "<dd><a href=\"javascript:set_stock_url(2,'{c}'\">{c}</a></dd>".format(c=c)
            for c in relates
        )
        jika_html = '<td colspan="2" class="v_zika2">{j}<span>億円</span></td>'.format(
            j=jikasogaku
        )

        purchase_td = ""
        if purchase:
            purchase_td = (
                "<th scope='row'>売買最低代金</th>\r\n"
                "      <td>{p}&nbsp;円</td>"
            ).format(p=purchase)

        overview_td = (
            "<th scope='row'>概要</th>\r\n"
            "      <td>{o}</td>"
        ).format(o=overview)

        return (
            '{jika}'
            '{purchase}'
            '<h1 id="kobetsu">{name}({code})  </h1>'
            '<span class="market">{market}</span>'
            '<a href="/themes/?industry={sid}&market={mid}">{sector}</a>'
            '{overview}'
            '{theme_html}'
            '{relate_html}'
            '<div id="kessan_happyoubi"><time datetime="2025-02-14">{kessan}</time></div>'
            "<th scope='row'>会社サイト</th><td><a href=\"{url}\">HP</a></td>"
        ).format(
            jika=jika_html, name=stock_name, code=code,
            market=market, sector=sector, sid=sector_id, mid=market_id,
            overview=overview_td, theme_html=theme_html,
            relate_html=relate_html, kessan=kessan,
            purchase=purchase_td, url=corporate_url,
        )

    def test_normal(self):
        """正常系: 全フィールドが正しく抽出される"""
        html = self._build_html()
        result = master.parse_master_html_kabutan(html)
        assert result["market_cap"] == pytest.approx(500.0)
        assert result["lowest_purchase_money"] == 150000
        assert result["stock_name"] == "テスト銘柄"
        assert result["market"] == "東証プライム"
        assert result["sector"] == "情報・通信業"
        assert result["overview"] == "テスト概要"
        assert result["themes"] == "AI,半導体"
        assert result["relates"] == "5678,9012"
        assert result["kessanbi"] == "2025/02/14"
        assert result["corporate_url"] == "https://example.com"

    def test_abbr_stock_name(self):
        """<abbr>付き銘柄名の場合は略称を取得"""
        html = self._build_html()
        # abbr付きのh1を手動構築
        html = html.replace(
            '<h1 id="kobetsu">テスト銘柄(1234)  </h1>',
            '<h1 id="kobetsu"><abbr title="テスト長い名前の銘柄">テスト略称</abbr>(1234)  </h1>'
        )
        result = master.parse_master_html_kabutan(html)
        assert result["stock_name"] == "テスト略称"

    def test_multiple_themes(self):
        """テーマが複数ある場合"""
        html = self._build_html(themes=["AI", "半導体", "DX", "クラウド"])
        result = master.parse_master_html_kabutan(html)
        assert result["themes"] == "AI,半導体,DX,クラウド"

    def test_no_themes(self):
        """テーマがない場合"""
        html = self._build_html(themes=[])
        result = master.parse_master_html_kabutan(html)
        assert result["themes"] == ""

    def test_no_relates(self):
        """関連銘柄がない場合"""
        html = self._build_html(relates=[])
        result = master.parse_master_html_kabutan(html)
        assert result["relates"] == ""

    def test_no_purchase(self):
        """購入代金がない場合は0"""
        html = self._build_html(purchase=None)
        result = master.parse_master_html_kabutan(html)
        assert result["lowest_purchase_money"] == 0


# ==================================================
# is_delist
# ==================================================
class TestIsDelist:
    """上場廃止判定テスト"""

    def test_delist(self):
        """market_cap=0かつpurchase=0の場合はTrue"""
        assert master.is_delist({"market_cap": 0, "lowest_purchase_money": 0}) is True

    def test_has_market_cap(self):
        """market_capが正の場合はFalse"""
        assert master.is_delist({"market_cap": 100, "lowest_purchase_money": 0}) is False

    def test_has_purchase(self):
        """purchaseが正の場合はFalse"""
        assert master.is_delist({"market_cap": 0, "lowest_purchase_money": 50000}) is False

    def test_both_positive(self):
        """両方正の場合はFalse"""
        assert master.is_delist({"market_cap": 100, "lowest_purchase_money": 50000}) is False

    def test_empty_dict(self):
        """空dictの場合はTrue（デフォルト値0）"""
        assert master.is_delist({}) is True

    def test_missing_keys(self):
        """片方のキーのみの場合"""
        assert master.is_delist({"market_cap": 100}) is False
        assert master.is_delist({"lowest_purchase_money": 50000}) is False
