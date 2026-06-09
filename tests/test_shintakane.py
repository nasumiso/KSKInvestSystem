"""shintakane.py のHTMLパース関数テスト"""

from datetime import datetime

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


# ==================================================
# parse_kessan_html（株探・決算速報）
# ==================================================

def _build_kessan_row(date, ctg_class, code_s, link, summary):
    """決算速報HTMLの1行を生成する

    date: "2025-03-14" 形式
    ctg_class: "ctg3_ks"（修正）or "ctg3_kk"（発表）
    """
    return (
        f'<tr>'
        f'<td class="news_time"><time datetime="{date}T15:00:00+09:00">{date}</time></td>'
        f'<td><div class="{ctg_class}" data-code="{code_s}">決算</div></td>'
        f'<td><a href="{link}">{summary}</a></td>'
        f'</tr>'
    )


def _build_kessan_html(*rows):
    """決算速報ページの最小限HTMLを生成する"""
    rows_html = "".join(rows)
    return f'<table class="s_news_list mgbt0">{rows_html}</table>'


class TestParseKessanHtml:
    """株探の決算速報HTMLパーステスト"""

    def test_修正と発表の振り分け(self):
        """ctg3_ksは修正リスト、ctg3_kkは発表リストに振り分けられる"""
        html = _build_kessan_html(
            _build_kessan_row("2025-03-14", "ctg3_ks", "1234", "/news/1", "上方修正"),
            _build_kessan_row("2025-03-14", "ctg3_kk", "5678", "/news/2", "3Q決算発表"),
        )
        mod_lst, announce_lst = shintakane.parse_kessan_html(html)
        assert len(mod_lst) == 1
        assert len(announce_lst) == 1
        assert mod_lst[0][0] == "1234"
        assert announce_lst[0][0] == "5678"

    def test_日付フォーマット変換(self):
        """YYYY-MM-DD → YYYY/MM/DD に変換される"""
        html = _build_kessan_html(
            _build_kessan_row("2025-03-14", "ctg3_ks", "1234", "/news/1", "上方修正"),
        )
        mod_lst, _ = shintakane.parse_kessan_html(html)
        assert mod_lst[0][1] == "2025/03/14"

    def test_コードとリンクとサマリーの抽出(self):
        """code_s, link, summary が正しく抽出される"""
        html = _build_kessan_html(
            _build_kessan_row("2025-03-10", "ctg3_kk", "7203", "/news/article/123", "通期経常25%増益"),
        )
        _, announce_lst = shintakane.parse_kessan_html(html)
        assert announce_lst[0][0] == "7203"
        assert announce_lst[0][2] == "/news/article/123"
        assert announce_lst[0][3] == "通期経常25%増益"

    def test_修正のみ(self):
        """修正のみの場合、発表リストは空"""
        html = _build_kessan_html(
            _build_kessan_row("2025-03-14", "ctg3_ks", "1234", "/news/1", "上方修正"),
            _build_kessan_row("2025-03-13", "ctg3_ks", "5678", "/news/2", "下方修正"),
        )
        mod_lst, announce_lst = shintakane.parse_kessan_html(html)
        assert len(mod_lst) == 2
        assert len(announce_lst) == 0

    def test_発表のみ(self):
        """発表のみの場合、修正リストは空"""
        html = _build_kessan_html(
            _build_kessan_row("2025-03-14", "ctg3_kk", "9999", "/news/3", "1Q決算発表"),
        )
        mod_lst, announce_lst = shintakane.parse_kessan_html(html)
        assert len(mod_lst) == 0
        assert len(announce_lst) == 1

    def test_複数銘柄の修正(self):
        """複数の修正が全て抽出される"""
        html = _build_kessan_html(
            _build_kessan_row("2025-03-14", "ctg3_ks", "1111", "/news/a", "増益"),
            _build_kessan_row("2025-03-14", "ctg3_ks", "2222", "/news/b", "減益"),
            _build_kessan_row("2025-03-14", "ctg3_ks", "3333", "/news/c", "黒字浮上"),
        )
        mod_lst, _ = shintakane.parse_kessan_html(html)
        assert len(mod_lst) == 3
        codes = [item[0] for item in mod_lst]
        assert codes == ["1111", "2222", "3333"]

    def test_タプル要素数(self):
        """各要素が(code_s, date, link, summary)の4要素タプル"""
        html = _build_kessan_html(
            _build_kessan_row("2025-03-14", "ctg3_ks", "1234", "/news/1", "上方修正"),
        )
        mod_lst, _ = shintakane.parse_kessan_html(html)
        assert len(mod_lst[0]) == 4

    def test_空文字列入力で空リストが返る(self):
        """HTTP接続失敗等でHTMLが空の場合、両リストとも空で返ること。
        update_todays_kessan の IndexError 回避はこの挙動に依存する。"""
        mod_lst, announce_lst = shintakane.parse_kessan_html("")
        assert mod_lst == []
        assert announce_lst == []

    def test_2つのテーブル両方からパースする(self):
        """株探の決算速報ページは `s_news_list mgbt0` と `s_news_list mgt0`
        の2テーブル構成。後半テーブルの記事も拾える必要がある (regression)"""
        rows_front = _build_kessan_row(
            "2026-04-24", "ctg3_ks", "7774", "/news/a", "前期経常を上方修正"
        )
        rows_back = _build_kessan_row(
            "2026-04-24", "ctg3_ks", "6324", "/news/b", "ハーモニック、前期経常を67％上方修正"
        )
        html = (
            f'<table class="s_news_list mgbt0">{rows_front}</table>'
            f'<table class="s_news_list mgt0">{rows_back}</table>'
        )
        mod_lst, _ = shintakane.parse_kessan_html(html)
        codes = [item[0] for item in mod_lst]
        assert "7774" in codes
        assert "6324" in codes


# ==================================================
# convert_kabutan_pts_html（株探・PTSナイトランキング）
# ==================================================

def _make_kabutan_pts_table(*rows_data, direction="up"):
    """株探・PTSランキングHTMLのテーブルを生成する

    rows_data: (code, name, market, trade_close, pts_price, zenjitsuhi, zenjitsuhi_per, volume) のタプル
    direction: "up" (値上がりページ) または "down" (値下がりページ) で span class を切替
    """
    span_cls = direction
    rows_html = ""
    for code, name, market, trade_close, pts_price, zenjitsuhi, zenjitsuhi_per, volume in rows_data:
        rows_html += (
            f'<tr>\n'
            f'<td class="tac"><a href="/stock/?code={code}">{code}</a></td>\n'
            f'<th scope="row" class="tal">{name}</th>\n'
            f'<td class="tac">{market}</td>\n'
            f'<td class="gaiyou_icon"><a href="/stock/?code={code}"></a></td>\n'
            f'<td class="chart_icon"><a href="/stock/chart?code={code}"></a></td>\n'
            f'<td>{trade_close}</td>\n'
            f'<td>{pts_price}</td>\n'
            f'<td class="w61"><span class="{span_cls}">{zenjitsuhi}</span></td>\n'
            f'<td class="w50"><span class="{span_cls}">{zenjitsuhi_per}</span>%</td>\n'
            f'<td>{volume}</td>\n'
            f'<td>16.5</td>\n'
            f'<td>6.42</td>\n'
            f'<td>2.16</td>\n'
            f'</tr>\n'
        )
    return f'<table class="stock_table st_market">{rows_html}</table>'


class TestConvertKabutanPtsHtml:
    """株探のPTSナイトランキングHTMLパーステスト"""

    def test_単一銘柄のパース(self):
        html = _make_kabutan_pts_table(
            ("446A", "ノースサンド", "東Ｇ", "1,231", "1,531", "+300", "+24.37", "16,800"),
        )
        rows = shintakane.convert_kabutan_pts_html(html)
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "1"  # ランク
        assert "446A" in row[1]  # コード+銘柄名
        assert "ノースサンド" in row[1]
        assert row[2] == "東Ｇ"  # 市場
        assert row[3] == "セクター"
        assert row[4] == "1,531"  # PTS株価（通常終値ではない）
        assert row[5] == "+300"  # 前日比
        assert row[6] == "+24.37%"  # 前日比%
        assert row[7] == "16,800"  # 出来高

    def test_複数銘柄のパース(self):
        html = _make_kabutan_pts_table(
            ("446A", "ノースサンド", "東Ｇ", "1,231", "1,531", "+300", "+24.37", "16,800"),
            ("3441", "山王", "東Ｓ", "1,327", "1,627", "+300", "+22.61", "2,000"),
        )
        rows = shintakane.convert_kabutan_pts_html(html)
        assert len(rows) == 2
        assert rows[0][0] == "1"
        assert rows[1][0] == "2"
        assert "3441" in rows[1][1]

    def test_英数字コード(self):
        """英数字混在コード（例: 446A）のパース"""
        html = _make_kabutan_pts_table(
            ("446A", "ノースサンド", "東Ｇ", "1,231", "1,531", "+300", "+24.37", "16,800"),
        )
        rows = shintakane.convert_kabutan_pts_html(html)
        assert len(rows) == 1
        assert "446A" in rows[0][1]

    def test_下落銘柄もパースされる(self):
        """値下がりページの span class="down" でもマイナス符号付きで抽出される"""
        html = _make_kabutan_pts_table(
            ("1234", "テスト銘柄", "東Ｐ", "1,000", "950", "-50", "-5.00", "100,000"),
            direction="down",
        )
        rows = shintakane.convert_kabutan_pts_html(html)
        assert len(rows) == 1
        assert rows[0][5] == "-50"  # zenjitsuhi (マイナス符号付き)
        assert rows[0][6] == "-5.00%"  # zenjitsuhi_per (マイナス符号付き)

    def test_空テーブル(self):
        html = '<table class="stock_table st_market"></table>'
        rows = shintakane.convert_kabutan_pts_html(html)
        assert rows == []

    def test_出力カラム数(self):
        """出力行のカラム数が正しいこと（8カラム）"""
        html = _make_kabutan_pts_table(
            ("446A", "ノースサンド", "東Ｇ", "1,231", "1,531", "+300", "+24.37", "16,800"),
        )
        rows = shintakane.convert_kabutan_pts_html(html)
        assert len(rows[0]) == 8

    def test_max_rows制限(self):
        """max_rowsを超える銘柄は無視される"""
        data = [
            (f"{i:04d}", f"銘柄{i}", "東Ｐ", "1,000", "1,100", "+100", "+10.00", "10,000")
            for i in range(1000, 1030)
        ]
        html = _make_kabutan_pts_table(*data)
        rows = shintakane.convert_kabutan_pts_html(html, max_rows=5)
        assert len(rows) == 5


# ==================================================
# search_fromcsv_pts
# ==================================================
class TestSearchFromcsvPts:
    """PTS CSV読み込みテスト"""

    def test_originがptsになる(self, tmp_path):
        """CSV読み込み時にorigin='pts'がセットされること"""
        csv_file = tmp_path / "pts_260315.csv"
        csv_file.write_text('1,446A ノースサンド,東Ｇ,セクター,"1,531",+300,+24.37%,"16,800"\n', encoding="utf-8")
        result = shintakane.search_fromcsv_pts(str(csv_file))
        assert len(result) == 1
        assert result[0]["origin"] == "pts"
        assert result[0]["code_s"] == "446A"

    def test_存在しないファイル(self):
        """存在しないファイルは空リストを返す"""
        result = shintakane.search_fromcsv_pts("/nonexistent/file.csv")
        assert result == []


# ==================================================
# get_todays_pts 統合テスト
# ==================================================
def _make_pts_page_html(*rows_data, direction="up", date_str="2026年05月13日"):
    """日付ヘッダー付きの PTS ページ HTML を組み立てる"""
    table = _make_kabutan_pts_table(*rows_data, direction=direction)
    header = f'<div class="meigara_count">{date_str} 23:30現在 100銘柄</div>'
    return header + table


class TestGetTodaysPts:
    """get_todays_pts の統合テスト(複数ページ取得 + 値下がり統合 + rank連番)"""

    def test_値上がり2ページと値下がり1ページを統合してCSVに保存する(
        self, tmp_path, monkeypatch
    ):
        # 値上がり page=1 (15 銘柄)
        up_p1_data = [
            (f"{i:04d}", f"上1_{i}", "東Ｐ", "1,000", "1,100", "+100", "+10.00", "10,000")
            for i in range(1000, 1015)
        ]
        # 値上がり page=2 (15 銘柄)
        up_p2_data = [
            (f"{i:04d}", f"上2_{i}", "東Ｐ", "1,000", "1,100", "+50", "+5.00", "10,000")
            for i in range(2000, 2015)
        ]
        # 値下がり page=1 (10 銘柄、出来高 1000 以上で全件残る想定)
        down_p1_data = [
            (f"{i:04d}", f"下_{i}", "東Ｐ", "1,000", "900", "-100", "-10.00", "10,000")
            for i in range(3000, 3010)
        ]

        html_up_p1 = _make_pts_page_html(*up_p1_data, direction="up")
        html_up_p2 = _make_pts_page_html(*up_p2_data, direction="up")
        html_down_p1 = _make_pts_page_html(*down_p1_data, direction="down")

        # DATA_DIR を tmp_path に差し替え、today_stocks/html_cache を準備
        data_dir = tmp_path
        (data_dir / "today_stocks" / "html_cache").mkdir(parents=True)
        monkeypatch.setattr(shintakane, "DATA_DIR", str(data_dir))

        # キャッシュ判定スキップ用に latest_pts_fname を None にする
        monkeypatch.setattr(
            shintakane, "get_latest_pts_fname", lambda: (None, None)
        )
        # アーカイブ処理は対象外
        monkeypatch.setattr(shintakane, "_archive_old_csvs", lambda kind: None)

        # http_get_html を URL ごとに振り分けるモックに差し替え
        calls = []

        def fake_http_get_html(url, use_cache=False, cache_dir=None):
            calls.append(url)
            if "pts_night_price_increase" in url and "page=2" in url:
                return html_up_p2
            if "pts_night_price_increase" in url:
                return html_up_p1
            if "pts_night_price_decrease" in url:
                return html_down_p1
            raise AssertionError(f"unexpected url: {url}")

        monkeypatch.setattr(shintakane, "http_get_html", fake_http_get_html)

        shintakane.get_todays_pts(force=True)

        # 3 URL すべて取得されている
        assert any("pts_night_price_increase" in u and "page" not in u for u in calls)
        assert any("pts_night_price_increase" in u and "page=2" in u for u in calls)
        assert any("pts_night_price_decrease" in u for u in calls)

        # 出力 CSV を読み戻して検証
        csv_path = data_dir / "today_stocks" / "pts_260513.csv"
        assert csv_path.exists()
        import csv as _csv

        with open(csv_path, encoding="utf-8") as f:
            rows = list(_csv.reader(f))

        # 値上がり 30 + 値下がり 10 = 40 行
        assert len(rows) == 40

        # rank が 1..40 の連番
        assert [r[0] for r in rows] == [str(i) for i in range(1, 41)]

        # 値下がり行(rank 31..40)は zenjitsuhi_per がマイナス符号付き
        for r in rows[30:]:
            assert r[6].startswith("-"), f"値下がり行のはず: {r}"

        # 値上がり行(rank 1..30)は zenjitsuhi_per がプラス符号
        for r in rows[:30]:
            assert r[6].startswith("+"), f"値上がり行のはず: {r}"

    def test_値上がりpage1が薄商いで埋まらない場合page2で補完する(
        self, tmp_path, monkeypatch
    ):
        # page1: 15 件中 5 件のみ出来高 1000 以上、残り 10 件は薄商いで除外
        up_p1_data = [
            (f"{i:04d}", f"上1_{i}", "東Ｐ", "1,000", "1,100", "+100", "+10.00", "10,000")
            for i in range(1000, 1005)
        ] + [
            (f"{i:04d}", f"薄_{i}", "東Ｐ", "1,000", "1,100", "+100", "+10.00", "500")
            for i in range(1100, 1110)
        ]
        # page2: 全件出来高 1000 以上
        up_p2_data = [
            (f"{i:04d}", f"上2_{i}", "東Ｐ", "1,000", "1,100", "+50", "+5.00", "10,000")
            for i in range(2000, 2015)
        ]
        # 値下がりは空(値上がり補完シナリオに集中)
        down_p1_data = []

        html_up_p1 = _make_pts_page_html(*up_p1_data, direction="up")
        html_up_p2 = _make_pts_page_html(*up_p2_data, direction="up")
        html_down_p1 = _make_pts_page_html(*down_p1_data, direction="down")

        data_dir = tmp_path
        (data_dir / "today_stocks" / "html_cache").mkdir(parents=True)
        monkeypatch.setattr(shintakane, "DATA_DIR", str(data_dir))
        monkeypatch.setattr(
            shintakane, "get_latest_pts_fname", lambda: (None, None)
        )
        monkeypatch.setattr(shintakane, "_archive_old_csvs", lambda kind: None)

        def fake_http_get_html(url, use_cache=False, cache_dir=None):
            if "pts_night_price_increase" in url and "page=2" in url:
                return html_up_p2
            if "pts_night_price_increase" in url:
                return html_up_p1
            if "pts_night_price_decrease" in url:
                return html_down_p1
            raise AssertionError(f"unexpected url: {url}")

        monkeypatch.setattr(shintakane, "http_get_html", fake_http_get_html)

        shintakane.get_todays_pts(force=True)

        import csv as _csv

        csv_path = data_dir / "today_stocks" / "pts_260513.csv"
        with open(csv_path, encoding="utf-8") as f:
            rows = list(_csv.reader(f))

        # 値上がり: page1 から 5 + page2 から 15 = 20 件
        assert len(rows) == 20
        # rank 連番
        assert [r[0] for r in rows] == [str(i) for i in range(1, 21)]

    def test_force_True_は3ページ全てキャッシュをバイパスする(
        self, tmp_path, monkeypatch
    ):
        """force=True なら HTML キャッシュ判定をスキップし、必ず use_cache=False で HTTP 取得する"""
        up_p1_data = [
            (f"{i:04d}", f"上1_{i}", "東Ｐ", "1,000", "1,100", "+100", "+10.00", "10,000")
            for i in range(1000, 1015)
        ]
        up_p2_data = [
            (f"{i:04d}", f"上2_{i}", "東Ｐ", "1,000", "1,100", "+50", "+5.00", "10,000")
            for i in range(2000, 2002)
        ]
        down_p1_data = [
            (f"{i:04d}", f"下_{i}", "東Ｐ", "1,000", "900", "-100", "-10.00", "10,000")
            for i in range(3000, 3002)
        ]
        html_up_p1 = _make_pts_page_html(*up_p1_data, direction="up")
        html_up_p2 = _make_pts_page_html(*up_p2_data, direction="up")
        html_down_p1 = _make_pts_page_html(*down_p1_data, direction="down")

        data_dir = tmp_path
        cache_dir = data_dir / "today_stocks" / "html_cache"
        cache_dir.mkdir(parents=True)
        # 当日付の "古い HTML キャッシュ" を 3 URL 分すべて埋める
        # (force=False ならこれが使われてしまうが、force=True なら無視される)
        from shintakane import get_http_cachname
        stale_html = _make_pts_page_html(
            ("9999", "古いキャッシュ", "東Ｐ", "0", "0", "+0", "+0.0", "0"),
            direction="up",
        )
        for u in [
            "https://kabutan.jp/warning/pts_night_price_increase",
            "https://kabutan.jp/warning/pts_night_price_increase?page=2",
            "https://kabutan.jp/warning/pts_night_price_decrease",
        ]:
            (cache_dir / get_http_cachname(u)).write_text(stale_html, encoding="utf-8")

        monkeypatch.setattr(shintakane, "DATA_DIR", str(data_dir))
        monkeypatch.setattr(
            shintakane, "get_latest_pts_fname", lambda: (None, None)
        )
        monkeypatch.setattr(shintakane, "_archive_old_csvs", lambda kind: None)

        use_cache_calls = []

        def fake_http_get_html(url, use_cache=False, cache_dir=None):
            use_cache_calls.append((url, use_cache))
            if "pts_night_price_increase" in url and "page=2" in url:
                return html_up_p2
            if "pts_night_price_increase" in url:
                return html_up_p1
            if "pts_night_price_decrease" in url:
                return html_down_p1
            raise AssertionError(f"unexpected url: {url}")

        monkeypatch.setattr(shintakane, "http_get_html", fake_http_get_html)

        shintakane.get_todays_pts(force=True)

        # 3 URL すべて use_cache=False で取得されている
        assert len(use_cache_calls) == 3
        for url, use_cache in use_cache_calls:
            assert use_cache is False, f"force=True なのに use_cache=True: {url}"


# ==================================================
# _saved_latest_date (日次データのキャッシュ判定)
# ==================================================

class Test_saved_latest_date:
    """保存済み JSON の latest.date を読む共通ヘルパー。"""

    @pytest.mark.parametrize("content,expected", [
        ('{"latest": {"date": "2026-06-08", "new_high": 22}}', "2026-06-08"),  # 正常
        ('{"latest": {}}', None),          # latest はあるが date 無し
        ('{"history": []}', None),         # latest キー無し
        ('{"latest": null}', None),        # latest が null
        ('not a json {{{', None),          # 壊れた JSON
    ])
    def test_各種JSONからlatest日付を取り出す(self, tmp_path, content, expected):
        p = tmp_path / "daily.json"
        p.write_text(content, encoding="utf-8")
        assert shintakane._saved_latest_date(str(p)) == expected

    def test_ファイル無しはNone(self, tmp_path):
        assert shintakane._saved_latest_date(str(tmp_path / "missing.json")) is None


class Test_recent_weekday:
    """now から見た期待最新営業日 (17時カットオーバー + 土日補正) を返すヘルパー。"""

    @pytest.mark.parametrize("dt,expected", [
        (datetime(2026, 6, 11, 18), "2026-06-11"),  # 木17時後 → 当日
        (datetime(2026, 6, 11, 10), "2026-06-10"),  # 木17時前 → 前日 (退行解消)
        (datetime(2026, 6, 13, 12), "2026-06-12"),  # 土 → 前金曜
        (datetime(2026, 6, 14, 12), "2026-06-12"),  # 日 → 前々金曜
        (datetime(2026, 6, 8, 10), "2026-06-05"),   # 月17時前 → 前金曜
    ])
    def test_期待最新営業日(self, dt, expected):
        assert shintakane._recent_weekday(dt) == expected


class TestOriginToMark:
    @pytest.mark.parametrize(
        "origin, expected",
        [
            ("shintakane", "高"),
            ("dekidakaup", "出"),
            ("pts", "P"),
            ("shintakanedekidakauppts", "高出P"),
            ("", ""),
        ],
    )
    def test_origin_to_mark(self, origin, expected):
        assert shintakane._origin_to_mark(origin) == expected
