"""migrate_research_from_csv.py のユニットテスト (issue #92)"""

import csv
import os

import pytest

import migrate_research_from_csv as mig
import research_shelve as rs


# ==================================================
# fixtures
# ==================================================
@pytest.fixture
def db_path(tmp_path):
    """テスト用一時 DB パスを返す"""
    return str(tmp_path / "test_research_shelve")


def _write_csv(path: str, rows: list):
    """ヘッダ + rows を書き出すヘルパ"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        # 1 行目は実 CSV と同じ(先頭セル 6360、その他列名)
        w.writerow([
            "6360", "銘柄名", "分析日", "決算日", "総合評価",
            "IR分析", "クォリティ指標", "機関投資家", "OpenWork",
            "メモ・総括", "ジムクレイマー",
            "四季報コメント", "四季報コメント2", "四季報コメント3",
            "四季報コメント4", "四季報コメント5", "",
        ])
        for r in rows:
            w.writerow(r)


def _minimal_row(code_s: str, stock_name: str = "テスト") -> list:
    """最小限の 17 列行(他の列は空文字)を返すヘルパ"""
    return [
        code_s, stock_name, "", "", "",     # code, 銘柄名, 分析日, 決算日, 総合評価
        "", "", "", "", "", "",              # IR, クォリティ, 機関, OpenWork, メモ, ジム
        "", "", "", "", "", "",              # 四季報1-5 + 末尾
    ]


# ==================================================
# TestReadCsvRows
# ==================================================
class TestReadCsvRows:
    """CSV 読込層のテスト"""

    def test_header_row_skipped(self, tmp_path):
        """1 行目(ヘッダ)がスキップされる"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [_minimal_row("3496", "アズーム")])
        rows = mig.read_csv_rows(csv_path)
        assert len(rows) == 1
        assert rows[0][0] == "3496"

    def test_empty_row_skipped(self, tmp_path):
        """全列空の行がスキップされる"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [
            _minimal_row("3496", "アズーム"),
            [""] * 17,  # 全列空
            _minimal_row("9999", "テスト"),
        ])
        rows = mig.read_csv_rows(csv_path)
        assert len(rows) == 2
        codes = [r[0] for r in rows]
        assert codes == ["3496", "9999"]

    def test_short_row_padded(self, tmp_path):
        """列数が 17 未満の行は末尾を空文字でパディング"""
        csv_path = str(tmp_path / "test.csv")
        # csv.writer 経由で 16 列だけ書き出すと、csv.reader 側は 16 列で返す
        # 直接 16 列を書いてもヘッダ行との列数不一致は問題ない
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "6360", "銘柄名", "分析日", "決算日", "総合評価",
                "IR分析", "クォリティ指標", "機関投資家", "OpenWork",
                "メモ・総括", "ジムクレイマー",
                "四季報コメント", "四季報コメント2", "四季報コメント3",
                "四季報コメント4", "四季報コメント5", "",
            ])
            w.writerow(["3496", "アズーム"] + [""] * 14)  # 16 列
        rows = mig.read_csv_rows(csv_path)
        assert len(rows) == 1
        assert len(rows[0]) == 17  # パディングで 17 列に

    def test_long_row_truncated(self, tmp_path, caplog):
        """列数が 17 を超える行は末尾を切り詰め + warning"""
        csv_path = str(tmp_path / "test.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "6360", "銘柄名", "分析日", "決算日", "総合評価",
                "IR分析", "クォリティ指標", "機関投資家", "OpenWork",
                "メモ・総括", "ジムクレイマー",
                "四季報コメント", "四季報コメント2", "四季報コメント3",
                "四季報コメント4", "四季報コメント5", "",
            ])
            w.writerow(_minimal_row("3496", "アズーム") + ["extra1", "extra2"])  # 19 列
        rows = mig.read_csv_rows(csv_path)
        assert len(rows) == 1
        assert len(rows[0]) == 17
        # extra1/extra2 は切り詰められている(17 列目は空)
        assert rows[0][16] == ""


# ==================================================
# TestParseIrColumn
# ==================================================
class TestParseIrColumn:
    """IR 分析列パーサのテスト"""

    def test_basic_single_block(self):
        """概要 + 1 ブロックの基本形"""
        text = "概要\n26.1[A]26%,21%[Q]25%,25%\t[P]1Q21%(22%),20%(19%)"
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert overview == "概要"
        assert "26.1" in blocks
        assert blocks["26.1"]["ir_quant"] == "[A]26%,21%[Q]25%,25%\t[P]1Q21%(22%),20%(19%)"
        assert blocks["26.1"]["ir_comment"] == ""
        assert warnings == []

    def test_multiple_blocks(self):
        """複数ブロック"""
        text = (
            "概要\n"
            "26.1[A]26%,21%\n"
            "25.11[A]27%,22%\n"
            "25.7[A]28%,23%"
        )
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert overview == "概要"
        assert set(blocks.keys()) == {"26.1", "25.11", "25.7"}
        assert blocks["26.1"]["ir_quant"] == "[A]26%,21%"
        assert blocks["25.11"]["ir_quant"] == "[A]27%,22%"
        assert blocks["25.7"]["ir_quant"] == "[A]28%,23%"
        assert warnings == []

    def test_comment_attached_to_previous_block(self):
        """ブロック内の ・コメント が ir_comment に入る"""
        text = (
            "概要\n"
            "25.11[A]26%,21%[Q]27%,59%\t[P]0Q\n"
            "・新中経~30 CAGR35%(つよい)"
        )
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert blocks["25.11"]["ir_comment"] == "・新中経~30 CAGR35%(つよい)"

    def test_multiple_comments_joined(self):
        """1 ブロックに複数のコメント行があれば \\n 連結"""
        text = (
            "25.11[A]26%,21%\n"
            "・コメント1\n"
            "・コメント2\n"
            "・コメント3"
        )
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert blocks["25.11"]["ir_comment"] == "・コメント1\n・コメント2\n・コメント3"

    def test_confusing_date_not_boundary(self):
        """22.9.1Q決算説明資料 はブロック境界にならず前ブロックのコメントに入る"""
        text = (
            "25.1[A]19%,37%\n"
            "22.9.1Q決算説明資料"
        )
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert set(blocks.keys()) == {"25.1"}  # 22.9 はブロックにならない
        assert blocks["25.1"]["ir_comment"] == "22.9.1Q決算説明資料"
        assert warnings == []  # 正常パターンなので warning なし

    def test_out_of_range_month_warning(self):
        """月範囲外(26.13)は warning に記録し、ブロックにしない"""
        text = "26.13[A]26%,21%"
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert blocks == {}
        assert len(warnings) == 1
        assert warnings[0]["column"] == "ir"
        assert "不正日付" in warnings[0]["message"]

    def test_empty_input(self):
        """空入力"""
        overview, blocks, warnings = mig.parse_ir_column("")
        assert overview == ""
        assert blocks == {}
        assert warnings == []

    def test_no_overview(self):
        """先頭から日付行"""
        text = "26.1[A]26%,21%"
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert overview == ""
        assert "26.1" in blocks

    def test_block_without_quant(self):
        """定量なしブロック: 日付 + 空白終わり + コメント行"""
        text = "25.5 \n・コメントだけ"
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert "25.5" in blocks
        # 日付直後が空白のみ → ir_quant は空白込みで原文保持
        # ( lookahead \\s のため、"25.5" の後に空白が入った rest 部分)
        assert blocks["25.5"]["ir_comment"] == "・コメントだけ"

    def test_real_data_3496_prefix(self):
        """実データ 3496 の先頭 3 ブロック相当"""
        text = (
            "オフィスビル等の駐車場のサブリースが主力\n"
            "26.1[A]26%,21%[Q]25%,25%\t[P]1Q21%(22%),20%(19%)\n"
            "25.11[A]26%,21%[Q]27%,59%\t[P]0Q\n"
            "・新中経~30 CAGR35%(つよい)\n"
            "25.7[A]27%,37%[Q]28%,31%\t[P]3Q72%(71%),67%(68%)\n"
            "・増配"
        )
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert overview == "オフィスビル等の駐車場のサブリースが主力"
        assert set(blocks.keys()) == {"26.1", "25.11", "25.7"}
        assert blocks["26.1"]["ir_quant"] == "[A]26%,21%[Q]25%,25%\t[P]1Q21%(22%),20%(19%)"
        assert blocks["26.1"]["ir_comment"] == ""
        assert blocks["25.11"]["ir_comment"] == "・新中経~30 CAGR35%(つよい)"
        assert blocks["25.7"]["ir_comment"] == "・増配"
        assert warnings == []

    def test_trailing_unrelated_text(self):
        """末尾の決算無関係な自由テキストが最後のブロックのコメントに入る"""
        text = (
            "26.1[A]26%,21%\n"
            "2020年9月決算説明会\n"
            "・駐車場サブリース"
        )
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert "26.1" in blocks
        # 2020年9月... と ・駐車場... が 26.1 のコメントに入る
        assert "2020年9月決算説明会" in blocks["26.1"]["ir_comment"]
        assert "・駐車場サブリース" in blocks["26.1"]["ir_comment"]

    def test_leading_zero_year_preserved(self):
        """YY の leading zero が保持される (09.7 → "09.7" not "9.7")"""
        text = "09.7[A]10%,20%"
        overview, blocks, warnings = mig.parse_ir_column(text)
        # "09.7" キーで保存(int 化で "9.7" にならない)
        assert "09.7" in blocks
        assert "9.7" not in blocks
        assert blocks["09.7"]["ir_quant"] == "[A]10%,20%"


# ==================================================
# TestParseQualityColumn
# ==================================================
class TestParseQualityColumn:
    """クォリティ指標列パーサのテスト"""

    def test_basic(self):
        """1 ブロック"""
        text = "26.1\n555億 PER27 PBR9.3\n配当2.8 ROE36"
        blocks, warnings = mig.parse_quality_column(text)
        assert blocks == {"26.1": "555億 PER27 PBR9.3\n配当2.8 ROE36"}
        assert warnings == []

    def test_multiple_blocks(self):
        """複数ブロック"""
        text = (
            "26.1\n"
            "555億 PER27\n"
            "25.11\n"
            "579億 PER31\n"
            "25.7\n"
            "510億 PER29"
        )
        blocks, warnings = mig.parse_quality_column(text)
        assert set(blocks.keys()) == {"26.1", "25.11", "25.7"}
        assert blocks["26.1"] == "555億 PER27"
        assert blocks["25.11"] == "579億 PER31"
        assert blocks["25.7"] == "510億 PER29"
        assert warnings == []

    def test_empty_input(self):
        """空入力"""
        blocks, warnings = mig.parse_quality_column("")
        assert blocks == {}
        assert warnings == []

    def test_pbr_missing_format(self):
        """PBR 欠落フォーマットをそのまま原文保持"""
        text = (
            "22.11\n"
            "260億 PER34 PSR3.5 配当0.4\n"
            "ROE46 利益率15%"
        )
        blocks, warnings = mig.parse_quality_column(text)
        assert blocks["22.11"] == "260億 PER34 PSR3.5 配当0.4\nROE46 利益率15%"

    def test_preamble_garbage_warning(self):
        """冒頭の非境界行は warning + 破棄"""
        text = "ゴミ行\n26.1\n内容"
        blocks, warnings = mig.parse_quality_column(text)
        assert blocks == {"26.1": "内容"}
        assert len(warnings) == 1
        assert warnings[0]["column"] == "quality"
        assert "冒頭" in warnings[0]["message"]

    def test_out_of_range_month_warning(self):
        """月範囲外 → warning + 破棄"""
        text = "26.13\n内容"
        blocks, warnings = mig.parse_quality_column(text)
        assert blocks == {}
        # 月範囲外 warning (破棄) + 冒頭非境界 warning (内容) の両方
        # もしくは実装方針に応じて 1-2 件
        assert len(warnings) >= 1
        assert any("不正日付" in w["message"] for w in warnings)

    def test_invalid_block_does_not_contaminate_previous(self):
        """月範囲外ブロックの本文が前ブロックに混入しない (regression for codex finding)"""
        text = "25.11\nVALID\n26.13\nSHOULD_DROP"
        blocks, warnings = mig.parse_quality_column(text)
        # 25.11 は VALID だけ、SHOULD_DROP は捨てられる
        assert blocks == {"25.11": "VALID"}
        assert any("26.13" in w["message"] for w in warnings)


# ==================================================
# TestParseInstitutionalColumn
# ==================================================
class TestParseInstitutionalColumn:
    """機関投資家列パーサのテスト"""

    def test_same_line_pattern(self):
        """同一行パターン (主流)"""
        text = "26.1 75%(-%)|243%,-91%"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert comment == ""
        assert kairi == {"26.1": "75%(-%)|243%,-91%"}
        assert warnings == []

    def test_comment_only(self):
        """コメントのみ"""
        text = "あまりいない\n個人多い"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert comment == "あまりいない\n個人多い"
        assert kairi == {}
        assert warnings == []

    def test_leading_comment_and_date(self):
        """先頭コメント + 日付行"""
        text = "あまりいない\n26.1 75%(-%)|243%,-91%"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert comment == "あまりいない"
        assert kairi == {"26.1": "75%(-%)|243%,-91%"}

    def test_trailing_comment_mixed(self):
        """3021 パターン: 日付行の後にコメント末尾混在"""
        text = "26.1 -5%(6%)|69%,-79%\n外資少し\n社長半分"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert comment == "外資少し\n社長半分"
        assert kairi == {"26.1": "-5%(6%)|69%,-79%"}

    def test_triangle_mark(self):
        """△ 付き行の原文保持"""
        text = "26.1 △-31%(-30%)|30%,-93%"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert kairi == {"26.1": "△-31%(-30%)|30%,-93%"}

    def test_empty_input(self):
        """空入力"""
        comment, kairi, warnings = mig.parse_institutional_column("")
        assert comment == ""
        assert kairi == {}
        assert warnings == []

    def test_out_of_range_month(self):
        """月範囲外の日付行はコメントに降格 + warning"""
        text = "26.13 foo"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert comment == "26.13 foo"
        assert kairi == {}
        assert len(warnings) == 1
        assert warnings[0]["column"] == "institutional"

    def test_multi_line_pattern_3445(self):
        """別行パターン (3445 準拠): 日付単独行 + 次行に値"""
        text = (
            "23.6\n"
            "127%(131%)|320%,-65%\n"
            "23.2\n"
            "△70%(-%)|179%,-39%"
        )
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert comment == ""
        assert kairi == {
            "23.6": "127%(131%)|320%,-65%",
            "23.2": "△70%(-%)|179%,-39%",
        }
        assert warnings == []

    def test_mixed_same_and_multi_line(self):
        """7309 パターン: 同一行と別行の混在"""
        text = (
            "23.6 -29%(-24%)|12%,-70%\n"
            "22.12 86%(94%)|242%,-71%\n"
            "22.9\n"
            "56%(60%)|186%,-74%"
        )
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert kairi == {
            "23.6": "-29%(-24%)|12%,-70%",
            "22.12": "86%(94%)|242%,-71%",
            "22.9": "56%(60%)|186%,-74%",
        }

    def test_date_alone_followed_by_another_date(self):
        """日付単独行の次行が別の日付行 (1301 簡略): 単独行は破棄 + warning"""
        text = "25.5\n25.5 93%(115%)|205%,-18%"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        # 1 行目 "25.5" は値なしで破棄、2 行目 "25.5 93%..." が採用
        assert kairi == {"25.5": "93%(115%)|205%,-18%"}
        assert len(warnings) == 1
        assert "日付単独行" in warnings[0]["message"]

    def test_date_alone_followed_by_blank_then_data(self):
        """日付単独行の次に空行 + 実値(空行スキップして紐付け)"""
        text = "25.5\n\n93%(115%)|205%,-18%"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        # 空行スキップして次行が非日付非空なので紐付け
        assert kairi == {"25.5": "93%(115%)|205%,-18%"}

    def test_date_alone_at_end(self):
        """日付単独行が末尾 → 値なしで破棄 + warning"""
        text = "26.1"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert kairi == {}
        assert len(warnings) == 1
        assert "末尾" in warnings[0]["message"]


# ==================================================
# TestParseShikihoColumns
# ==================================================
class TestParseShikihoColumns:
    """四季報コメント列パーサのテスト"""

    def test_all_6_cols_filled(self):
        """col 11-16 全埋まり"""
        row = [""] * 11 + ["A", "B", "C", "D", "E", "F"]
        result = mig.parse_shikiho_columns(row)
        assert result == ["A", "B", "C", "D", "E", "F"]

    def test_only_col_11(self):
        """col 11 のみ埋まり"""
        row = [""] * 11 + ["最新コメント", "", "", "", "", ""]
        result = mig.parse_shikiho_columns(row)
        assert result == ["最新コメント"]

    def test_empty_cells_skipped(self):
        """空白のみのセルはスキップ"""
        row = [""] * 11 + ["A", "  ", "B", "", "C", ""]
        result = mig.parse_shikiho_columns(row)
        assert result == ["A", "B", "C"]

    def test_order_preserved(self):
        """col 11 が先頭(最新)、col 16 が最後"""
        row = [""] * 11 + ["latest", "second", "", "", "", "oldest"]
        result = mig.parse_shikiho_columns(row)
        # 順序は col 11 → 16 のまま
        assert result == ["latest", "second", "oldest"]


# ==================================================
# TestBuildRecordFromRow
# ==================================================
def _make_row(
    code_s="3496",
    stock_name="アズーム",
    analysis_date="",
    kessan_date="",
    rating="",
    ir="",
    quality="",
    inst="",
    openwork="",
    memo="",
    cramer="",
    shikiho=("", "", "", "", "", ""),
) -> list:
    """統合層テスト用の 17 列行を作成するヘルパ"""
    return [
        code_s, stock_name, analysis_date, kessan_date, rating,
        ir, quality, inst, openwork, memo, cramer,
        shikiho[0], shikiho[1], shikiho[2], shikiho[3], shikiho[4], shikiho[5],
    ]


class TestBuildRecordFromRow:
    """統合層のテスト"""

    def test_minimal_row(self):
        """code_s と stock_name のみ"""
        row = _make_row()
        record, warnings = mig.build_record_from_row(row)
        assert record["code_s"] == "3496"
        assert record["stock_name"] == "アズーム"
        assert record["snapshots"] == []
        assert record["analysis_date_raw"] == ""
        assert record["kessan_date_raw"] == ""
        assert warnings == []

    def test_union_of_dates(self):
        """3 列の日付和集合がスナップショットになる"""
        row = _make_row(
            ir="26.1[A]26%,21%\n25.7[A]27%,37%",
            quality="26.1\n555億\n25.4\n466億",
        )
        record, warnings = mig.build_record_from_row(row)
        # 日付セット: {26.1, 25.7, 25.4}
        snap_dates = [s["date_yy_m"] for s in record["snapshots"]]
        assert snap_dates == ["26.1", "25.7", "25.4"]
        # 部分欠落は空文字
        snap_map = {s["date_yy_m"]: s for s in record["snapshots"]}
        assert snap_map["26.1"]["ir_quant"] == "[A]26%,21%"
        assert snap_map["26.1"]["quality_indicators"] == "555億"
        assert snap_map["25.7"]["ir_quant"] == "[A]27%,37%"
        assert snap_map["25.7"]["quality_indicators"] == ""
        assert snap_map["25.4"]["ir_quant"] == ""
        assert snap_map["25.4"]["quality_indicators"] == "466億"

    def test_real_data_3496_like(self):
        """3496 準拠: 概要 + 複数ブロック"""
        row = _make_row(
            rating="S",
            ir=(
                "オフィスビル等の駐車場のサブリースが主力\n"
                "26.1[A]26%,21%[Q]25%,25%\n"
                "25.11[A]26%,21%[Q]27%,59%\n"
                "・新中経~30 CAGR35%"
            ),
            quality="26.1\n555億 PER27\n25.11\n579億 PER31",
            inst="あまりいない\n26.1 75%(-%)|243%,-91%\n25.11 -20%(-%)|42%,-84%",
        )
        record, warnings = mig.build_record_from_row(row)
        assert record["overall_rating"] == "S"
        assert "駐車場" in record["overview"]
        assert record["institutional_comment"] == "あまりいない"
        # snapshots は最新順
        snap_dates = [s["date_yy_m"] for s in record["snapshots"]]
        assert snap_dates == ["26.1", "25.11"]
        snap_map = {s["date_yy_m"]: s for s in record["snapshots"]}
        assert snap_map["26.1"]["ir_quant"] == "[A]26%,21%[Q]25%,25%"
        assert snap_map["26.1"]["rironkabuka_kairi"] == "75%(-%)|243%,-91%"
        assert snap_map["26.1"]["quality_indicators"] == "555億 PER27"
        assert snap_map["25.11"]["ir_comment"] == "・新中経~30 CAGR35%"
        assert snap_map["25.11"]["data_source"] == "migration"

    def test_code_s_normalization(self):
        """135a → 135A"""
        row = _make_row(code_s="135a", stock_name="テスト")
        record, warnings = mig.build_record_from_row(row)
        assert record["code_s"] == "135A"

    def test_invalid_code_s_raises(self):
        """不正 code_s で ValueError"""
        row = _make_row(code_s="123", stock_name="テスト")
        with pytest.raises(ValueError):
            mig.build_record_from_row(row)

    def test_invalid_rating_raises(self):
        """不正 rating で ValueError"""
        row = _make_row(rating="F")
        with pytest.raises(ValueError):
            mig.build_record_from_row(row)

    def test_warnings_propagated_with_code_s(self):
        """IR 列の warning に code_s が付与される"""
        row = _make_row(ir="26.13[A]26%,21%")
        record, warnings = mig.build_record_from_row(row)
        assert len(warnings) == 1
        assert warnings[0]["code_s"] == "3496"
        assert warnings[0]["column"] == "ir"

    def test_analysis_kessan_date_raw_preserved(self):
        """分析日・決算日が原文保持される(主流 MM/DD 形式)"""
        row = _make_row(analysis_date="11/13", kessan_date="01/30")
        record, warnings = mig.build_record_from_row(row)
        assert record["analysis_date_raw"] == "11/13"
        assert record["kessan_date_raw"] == "01/30"

    def test_analysis_kessan_date_raw_variant_formats(self):
        """分析日・決算日の異形もそのまま格納"""
        row = _make_row(analysis_date="22/2/10", kessan_date="22四季報春")
        record, warnings = mig.build_record_from_row(row)
        assert record["analysis_date_raw"] == "22/2/10"
        assert record["kessan_date_raw"] == "22四季報春"


# ==================================================
# TestMigrateCsvToResearchShelve (実行層の最小統合テスト)
# ==================================================
class TestMigrateCsvToResearchShelve:
    """実行層のテスト"""

    def test_full_flow(self, tmp_path, db_path):
        """CSV → DB ラウンドトリップ"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [
            _make_row(
                code_s="3496",
                stock_name="アズーム",
                analysis_date="11/13",
                kessan_date="01/30",
                rating="S",
                ir="26.1[A]26%,21%",
            ),
            _make_row(code_s="9999", stock_name="テスト", rating="B"),
        ])
        summary = mig.migrate_csv_to_research_shelve(csv_path, db_path=db_path)
        assert summary["total"] == 2
        assert summary["succeeded"] == 2
        assert summary["failed"] == 0

        # DB に入っているか確認
        rec = rs.get_research_record("3496", db_path=db_path)
        assert rec is not None
        assert rec["stock_name"] == "アズーム"
        assert rec["overall_rating"] == "S"
        assert rec["analysis_date_raw"] == "11/13"
        assert rec["kessan_date_raw"] == "01/30"
        assert len(rec["snapshots"]) == 1
        assert rec["snapshots"][0]["date_yy_m"] == "26.1"
        assert rec["snapshots"][0]["data_source"] == "migration"

    def test_dry_run_no_db_write(self, tmp_path, db_path):
        """dry_run=True で DB に書き込まれない"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [_make_row(code_s="3496", stock_name="アズーム")])
        summary = mig.migrate_csv_to_research_shelve(
            csv_path, db_path=db_path, dry_run=True
        )
        assert summary["dry_run"] is True
        assert summary["succeeded"] == 1
        # DB に書き込まれていない
        rec = rs.get_research_record("3496", db_path=db_path)
        assert rec is None

    def test_dry_run_no_backup(self, tmp_path, db_path, monkeypatch):
        """dry_run=True でバックアップも呼ばれない(monkeypatch 検証)"""
        # 事前にレコードを入れておく(空 DB ベースのダミー検証にならないため)
        rs.upsert_research_record(
            rs.create_research_record("1111", "既存"), db_path=db_path,
        )

        call_count = {"count": 0}

        def mock_backup(*, db_path=None):
            call_count["count"] += 1
            return []

        monkeypatch.setattr(mig.rs, "backup_research_db", mock_backup)

        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [_make_row(code_s="3496", stock_name="アズーム")])
        mig.migrate_csv_to_research_shelve(
            csv_path, db_path=db_path, dry_run=True
        )
        assert call_count["count"] == 0

    def test_non_dry_run_backup_called_once(self, tmp_path, db_path, monkeypatch):
        """dry_run=False で移行前バックアップが 1 回呼ばれる"""
        # 事前にレコードを入れておく
        rs.upsert_research_record(
            rs.create_research_record("1111", "既存"), db_path=db_path,
        )

        call_count = {"count": 0}

        def mock_backup(*, db_path=None):
            call_count["count"] += 1
            return ["mock_backup.dat"]

        monkeypatch.setattr(mig.rs, "backup_research_db", mock_backup)

        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [_make_row(code_s="3496", stock_name="アズーム")])
        mig.migrate_csv_to_research_shelve(
            csv_path, db_path=db_path, dry_run=False
        )
        assert call_count["count"] == 1

    def test_invalid_code_s_recorded_as_failed(self, tmp_path, db_path):
        """不正 code_s を含む行は failed_rows に記録、他行は成功"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [
            _make_row(code_s="3496", stock_name="アズーム"),
            _make_row(code_s="123", stock_name="不正"),  # 3 桁は不正
            _make_row(code_s="9999", stock_name="テスト"),
        ])
        summary = mig.migrate_csv_to_research_shelve(csv_path, db_path=db_path)
        assert summary["total"] == 3
        assert summary["succeeded"] == 2
        assert summary["failed"] == 1
        assert len(summary["failed_rows"]) == 1
        assert summary["failed_rows"][0]["code_s"] == "123"

    def test_failed_row_has_error_info(self, tmp_path, db_path):
        """failed_rows の dict にエラーメッセージが含まれる"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [_make_row(code_s="abc", stock_name="不正")])
        summary = mig.migrate_csv_to_research_shelve(csv_path, db_path=db_path)
        assert summary["failed"] == 1
        fr = summary["failed_rows"][0]
        assert "error" in fr
        assert "ValueError" in fr["error"] or "code_s" in fr["error"]

    def test_show_codes_outputs_format(self, tmp_path, db_path, capsys):
        """show_codes で代表銘柄の format_record_full 出力"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [
            _make_row(code_s="3496", stock_name="アズーム", rating="S"),
        ])
        mig.migrate_csv_to_research_shelve(
            csv_path, db_path=db_path, show_codes=["3496"],
        )
        captured = capsys.readouterr()
        assert "3496" in captured.out
        assert "アズーム" in captured.out

    def test_show_codes_nonexistent_no_crash(self, tmp_path, db_path, capsys):
        """存在しない code_s を指定しても TypeError にならない"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [_make_row(code_s="3496", stock_name="アズーム")])
        mig.migrate_csv_to_research_shelve(
            csv_path, db_path=db_path, show_codes=["0000"],
        )
        # クラッシュせず完了することが検証のポイント
        # log_warning が呼ばれているはず(stdout/stderr どちらか)

    def test_dry_run_and_show_codes_combined(self, tmp_path, db_path):
        """dry_run=True かつ show_codes 併用でも TypeError にならない"""
        csv_path = str(tmp_path / "test.csv")
        _write_csv(csv_path, [_make_row(code_s="3496", stock_name="アズーム")])
        # クラッシュせず完了すれば OK(dry_run で DB に書き込まれないので
        # get_research_record は None を返すが、None チェック済みなので安全)
        summary = mig.migrate_csv_to_research_shelve(
            csv_path, db_path=db_path, dry_run=True, show_codes=["3496"],
        )
        assert summary["dry_run"] is True
        assert summary["succeeded"] == 1


# ==================================================
# TestCrlfRobustness (防御的テスト)
# ==================================================
class TestCrlfRobustness:
    """CRLF 入力に対する防御テスト"""

    def test_ir_column_crlf(self):
        """CRLF 入力でも IR 分析列がパースできる"""
        text = "概要\r\n26.1[A]26%,21%\r\n・コメント"
        overview, blocks, warnings = mig.parse_ir_column(text)
        assert overview == "概要"
        assert "26.1" in blocks
        assert blocks["26.1"]["ir_quant"] == "[A]26%,21%"
        assert blocks["26.1"]["ir_comment"] == "・コメント"

    def test_quality_column_crlf(self):
        """CRLF 入力でもクォリティ指標列がパースできる"""
        text = "26.1\r\n555億 PER27\r\n配当2.8"
        blocks, warnings = mig.parse_quality_column(text)
        assert blocks == {"26.1": "555億 PER27\n配当2.8"}

    def test_institutional_column_crlf(self):
        """CRLF 入力でも機関投資家列がパースできる、\\r は混入しない"""
        text = "26.1 75%(-%)|243%,-91%\r\n25.11 -20%(-%)|42%,-84%"
        comment, kairi, warnings = mig.parse_institutional_column(text)
        assert kairi == {
            "26.1": "75%(-%)|243%,-91%",
            "25.11": "-20%(-%)|42%,-84%",
        }
        # 値に \r が含まれていない
        assert "\r" not in kairi["26.1"]
        assert "\r" not in kairi["25.11"]
