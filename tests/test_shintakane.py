"""shintakane.py のHTMLパース関数テスト"""

import pytest

import shintakane


# ==================================================
# テスト用HTML生成ヘルパー
# ==================================================

def _make_kabutan_shintakane_table(*rows_data):
    """株探・新高値HTMLのテーブルを生成する

    rows_data: (code, name, market, price, zenjitsuhi, zenjitsuhi_per) のタプル
    zenjitsuhi_per は数値部分のみ（例: "+1.95"）
    """
    rows_html = ""
    for code, name, market, price, zenjitsuhi, zenjitsuhi_per in rows_data:
        rows_html += (
            f'<tr>\n'
            f'<td class="tac"><a href="/stock/?code={code}">{code}</a></td>\n'
            f'<th scope="row" class="tal">{name}</th>\n'
            f'<td class="tac">{market}</td>\n'
            f'<td class="gaiyou_icon"><a href="/stock/?code={code}"></a></td>\n'
            f'<td class="chart_icon"><a href="/stock/chart?code={code}"></a></td>\n'
            f'<td>{price}</td>\n'
            f'<td></td>\n'
            f'<td class="w61"><span class="up">{zenjitsuhi}</span></td>\n'
            f'<td class="w50"><span class="up">{zenjitsuhi_per}</span>%</td>\n'
            f'<td class="news_icon"><a href="/stock/news?code={code}"></a></td>\n'
            f'<td>15.4</td>\n'
            f'<td>1.07</td>\n'
            f'<td>2.46</td>\n'
            f'</tr>\n'
        )
    return f'<table class="stock_table st_market">{rows_html}</table>'


def _make_kabutan_dekidakaup_table(*rows_data):
    """株探・出来高急増HTMLのテーブルを生成する

    rows_data: (code, name, market, price, zenjitsuhi, volume, dekidaka_up) のタプル
    zenjitsuhi/dekidaka_up は span.up 付きの値（例: "+40"）
    """
    rows_html = ""
    for code, name, market, price, zenjitsuhi, volume, dekidaka_up in rows_data:
        rows_html += (
            f'<tr>\n'
            f'<td class="tac"><a href="/stock/chart?code={code}&ashi=1&tech=1_1,2_5">{code}</a></td>\n'
            f'<th scope="row" class="tal">{name}</th>\n'
            f'<td class="tac">{market}</td>\n'
            f'<td class="gaiyou_icon"><a href="/stock/?code={code}"></a></td>\n'
            f'<td class="chart_icon"><a href="/stock/chart?code={code}&ashi=1&tech=1_1,2_5"></a></td>\n'
            f'<td>{price}</td>\n'
            f'<td></td>\n'
            f'<td><span class="up">{zenjitsuhi}</span></td>\n'
            f'<td>{volume}</td>\n'
            f'<td><span class="up">{dekidaka_up}</span></td>\n'
            f'<td>15.4</td>\n'
            f'<td>1.07</td>\n'
            f'<td>2.46</td>\n'
            f'</tr>\n'
        )
    return f'<table class="stock_table st_market">{rows_html}</table>'


# ==================================================
# convert_kabutan_shintakane_html（株探・新高値）
# ==================================================
class TestConvertKabutanShintakaneHtml:
    """株探の新高値HTMLパーステスト"""

    def test_単一銘柄のパース(self):
        html = _make_kabutan_shintakane_table(
            ("1605", "ＩＮＰＥＸ", "東Ｐ", "4,383", "+84", "+1.95"),
        )
        rows = shintakane.convert_kabutan_shintakane_html(html)
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "1"  # ランク
        assert "1605" in row[1]  # コード+銘柄名
        assert "ＩＮＰＥＸ" in row[1]
        assert row[2] == "東Ｐ"  # 市場
        assert row[3] == "セクター"
        assert row[4] == "4,383"  # 株価
        assert row[5] == "+84"  # 前日比
        assert row[6] == "+1.95%"  # 前日比%

    def test_複数銘柄のパース(self):
        html = _make_kabutan_shintakane_table(
            ("1605", "ＩＮＰＥＸ", "東Ｐ", "4,383", "+84", "+1.95"),
            ("6758", "ソニーＧ", "東Ｐ", "15,230", "+250", "+1.67"),
        )
        rows = shintakane.convert_kabutan_shintakane_html(html)
        assert len(rows) == 2
        assert rows[0][0] == "1"  # ランク連番
        assert rows[1][0] == "2"
        assert "6758" in rows[1][1]

    def test_英数字コード(self):
        """英数字混在コード（例: 133A）のパース"""
        html = _make_kabutan_shintakane_table(
            ("133A", "ＧＸ超短米債", "東Ｅ", "1,081", "+3", "+0.28"),
        )
        rows = shintakane.convert_kabutan_shintakane_html(html)
        assert len(rows) == 1
        assert "133A" in rows[0][1]

    def test_下落銘柄はスキップされる(self):
        """spanにupクラスがない場合、zenjitsuhi=0になる"""
        html = (
            '<table class="stock_table st_market">'
            '<tr>\n'
            '<td class="tac"><a href="/stock/?code=1234">1234</a></td>\n'
            '<th scope="row" class="tal">テスト銘柄</th>\n'
            '<td class="tac">東Ｐ</td>\n'
            '<td class="gaiyou_icon"><a href="/stock/?code=1234"></a></td>\n'
            '<td class="chart_icon"><a href="/stock/chart?code=1234"></a></td>\n'
            '<td>1,000</td>\n'
            '<td></td>\n'
            '<td class="w61"><span class="down">-50</span></td>\n'
            '<td class="w50"><span class="down">-2.00</span>%</td>\n'
            '<td class="news_icon"><a href="/stock/news?code=1234"></a></td>\n'
            '<td>10.0</td>\n'
            '<td>1.00</td>\n'
            '<td>3.00</td>\n'
            '</tr>\n'
            '</table>'
        )
        rows = shintakane.convert_kabutan_shintakane_html(html)
        assert len(rows) == 1
        assert rows[0][5] == 0  # zenjitsuhi
        assert rows[0][6] == 0  # zenjitsuhi_per

    def test_空テーブル(self):
        html = '<table class="stock_table st_market"></table>'
        rows = shintakane.convert_kabutan_shintakane_html(html)
        assert rows == []

    def test_出力カラム数(self):
        """出力行のカラム数が正しいこと（8カラム）"""
        html = _make_kabutan_shintakane_table(
            ("1605", "ＩＮＰＥＸ", "東Ｐ", "4,383", "+84", "+1.95"),
        )
        rows = shintakane.convert_kabutan_shintakane_html(html)
        # ランク, コード+銘柄名, 市場, セクター, 株価, 前日比, 前日比%, 出来高
        assert len(rows[0]) == 8


# ==================================================
# convert_kabutan_dekidakaup_html（株探・出来高急増）
# ==================================================
class TestConvertKabutanDekidakaupHtml:
    """株探の出来高急増HTMLパーステスト"""

    def test_単一銘柄のパース(self):
        html = _make_kabutan_dekidakaup_table(
            ("6613", "ＱＤレーザ", "東Ｇ", "1,140", "+40", "28,227,500", "+8,170.58"),
        )
        rows = shintakane.convert_kabutan_dekidakaup_html(html)
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "1"  # ランク
        assert "6613" in row[1]  # コード+銘柄名
        assert "ＱＤレーザ" in row[1]
        assert row[2] == "東Ｇ"  # 市場
        assert row[3] == "セクター"
        assert row[4] == "1,140"  # 株価
        assert row[5] == "+40"  # 前日比
        # 前日比% = +40 / (1140-40) * 100 = +3.64%
        assert row[6] == "+3.64%"  # 前日比%（株価と前日比から算出）
        assert row[7] == "28,227,500"  # 出来高
        assert row[9] == "+8,170.58"  # 出来高前日比

    def test_複数銘柄のパース(self):
        html = _make_kabutan_dekidakaup_table(
            ("6613", "ＱＤレーザ", "東Ｇ", "1,140", "+40", "28,227,500", "+8,170.58"),
            ("3782", "ＤＤＳ", "東Ｇ", "500", "+10", "5,000,000", "+500.00"),
        )
        rows = shintakane.convert_kabutan_dekidakaup_html(html)
        assert len(rows) == 2
        assert rows[0][0] == "1"
        assert rows[1][0] == "2"

    def test_英数字コード(self):
        """英数字混在コード（例: 496A）のパース"""
        html = _make_kabutan_dekidakaup_table(
            ("496A", "ＯｎｅＪ２０", "東Ｅ", "1,009.0", "+5", "144,020", "+9,313.07"),
        )
        rows = shintakane.convert_kabutan_dekidakaup_html(html)
        assert len(rows) == 1
        assert "496A" in rows[0][1]

    def test_下落銘柄はスキップされる(self):
        """spanにupクラスがない場合、zenjitsuhi=0になる"""
        html = (
            '<table class="stock_table st_market">'
            '<tr>\n'
            '<td class="tac"><a href="/stock/chart?code=1234&ashi=1&tech=1_1,2_5">1234</a></td>\n'
            '<th scope="row" class="tal">テスト銘柄</th>\n'
            '<td class="tac">東Ｐ</td>\n'
            '<td class="gaiyou_icon"><a href="/stock/?code=1234"></a></td>\n'
            '<td class="chart_icon"><a href="/stock/chart?code=1234&ashi=1&tech=1_1,2_5"></a></td>\n'
            '<td>1,000</td>\n'
            '<td></td>\n'
            '<td><span class="down">-50</span></td>\n'
            '<td>100,000</td>\n'
            '<td><span class="down">-50.00</span></td>\n'
            '<td>10.0</td>\n'
            '<td>1.00</td>\n'
            '<td>3.00</td>\n'
            '</tr>\n'
            '</table>'
        )
        rows = shintakane.convert_kabutan_dekidakaup_html(html)
        assert len(rows) == 1
        assert rows[0][5] == 0  # zenjitsuhi
        assert rows[0][6] == "0"  # zenjitsuhi_per（算出不可）
        assert rows[0][9] == 0  # dekidaka_up

    def test_空テーブル(self):
        html = '<table class="stock_table st_market"></table>'
        rows = shintakane.convert_kabutan_dekidakaup_html(html)
        assert rows == []

    def test_出力カラム数(self):
        """出力行のカラム数が正しいこと（10カラム）"""
        html = _make_kabutan_dekidakaup_table(
            ("6613", "ＱＤレーザ", "東Ｇ", "1,140", "+40", "28,227,500", "+8,170.58"),
        )
        rows = shintakane.convert_kabutan_dekidakaup_html(html)
        # ランク, コード+銘柄名, 市場, セクター, 株価, 前日比, 前日比%, 出来高, 平均出来高, 出来高前日比
        assert len(rows[0]) == 10
