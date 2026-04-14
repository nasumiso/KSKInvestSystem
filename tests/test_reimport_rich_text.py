"""reimport_rich_text モジュールのユニットテスト。"""

import os
import sys
import tempfile

import pytest

# scripts/ を sys.path に追加
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from reimport_rich_text import (
    _build_segments,
    _color_to_hex,
    _get_run_color,
    api_row_to_text_row,
    apply_formatting_to_ir_blocks,
    textFormatRuns_to_html,
)


# ===========================================
# 2. HTML 変換層テスト
# ===========================================

class TestColorToHex:
    """_color_to_hex() のテスト。"""

    def test_red(self):
        assert _color_to_hex({"red": 1.0, "green": 0.0, "blue": 0.0}) == "#ff0000"

    def test_black_returns_none(self):
        assert _color_to_hex({"red": 0.0, "green": 0.0, "blue": 0.0}) is None

    def test_near_black_returns_none(self):
        assert _color_to_hex({"red": 0.05, "green": 0.05, "blue": 0.05}) is None

    def test_none_returns_none(self):
        assert _color_to_hex(None) is None

    def test_empty_dict_returns_none(self):
        assert _color_to_hex({}) is None

    def test_custom_color(self):
        result = _color_to_hex({"red": 0.8, "green": 0.2, "blue": 0.0})
        assert result == "#cc3300"


class TestBuildSegments:
    """_build_segments() のテスト。"""

    def test_single_run_from_zero(self):
        runs = [{"startIndex": 0, "format": {"bold": True}}]
        segments = _build_segments("hello", runs)
        assert segments == [(0, 5, {"bold": True})]

    def test_multiple_runs(self):
        runs = [
            {"startIndex": 0, "format": {}},
            {"startIndex": 5, "format": {"bold": True}},
        ]
        segments = _build_segments("hello world", runs)
        assert len(segments) == 2
        assert segments[0] == (0, 5, {})
        assert segments[1] == (5, 11, {"bold": True})

    def test_gap_before_first_run(self):
        runs = [{"startIndex": 5, "format": {"bold": True}}]
        segments = _build_segments("hello world", runs)
        assert len(segments) == 2
        assert segments[0] == (0, 5, {})
        assert segments[1] == (5, 11, {"bold": True})

    def test_implicit_start_index(self):
        """先頭 run の startIndex が省略された場合"""
        runs = [{"format": {"bold": True}}]
        segments = _build_segments("hello", runs)
        assert segments == [(0, 5, {"bold": True})]


class TestApiRowToTextRow:
    """api_row_to_text_row() のテスト。"""

    def test_full_row(self):
        """17列以上のセルから17列テキストリストを返す"""
        cells = [{"formattedValue": f"col{i}"} for i in range(17)]
        result = api_row_to_text_row(cells)
        assert len(result) == 17
        assert result[0] == "col0"
        assert result[16] == "col16"

    def test_short_row_padded(self):
        """列数不足は空文字でパディングされる"""
        cells = [{"formattedValue": "code"}, {"formattedValue": "name"}]
        result = api_row_to_text_row(cells)
        assert len(result) == 17
        assert result[0] == "code"
        assert result[2] == ""

    def test_empty_cells(self):
        """空セルは空文字になる"""
        cells = [{}] * 5
        result = api_row_to_text_row(cells)
        assert all(v == "" for v in result)

    def test_none_formatted_value(self):
        """formattedValue が None のセルは空文字"""
        cells = [{"formattedValue": None}]
        result = api_row_to_text_row(cells)
        assert result[0] == ""


class TestTextFormatRunsToHtml:
    """textFormatRuns_to_html() のテスト。"""

    def test_no_formatting(self):
        """書式なし → プレーンテキスト（HTMLエスケープ）"""
        result = textFormatRuns_to_html("Hello <world>", [], None, {})
        assert result == "Hello &lt;world&gt;"

    def test_empty_value(self):
        assert textFormatRuns_to_html("", [], None, {}) == ""

    def test_bold_only(self):
        runs = [{"startIndex": 0, "format": {"bold": True}}]
        result = textFormatRuns_to_html("太字テスト", runs, None, {})
        assert result == "<b>太字テスト</b>"

    def test_bold_not_doubled_when_default(self):
        """セルのデフォルトが bold の場合、run の bold は二重化しない"""
        runs = [{"startIndex": 0, "format": {"bold": True}}]
        result = textFormatRuns_to_html(
            "text", runs, None, {"bold": True}
        )
        assert "<b>" not in result
        assert result == "text"

    def test_red_color(self):
        runs = [
            {
                "startIndex": 0,
                "format": {
                    "foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}
                },
            }
        ]
        result = textFormatRuns_to_html("赤字テスト", runs, None, {})
        assert '<span style="color:#ff0000">' in result
        assert "赤字テスト" in result

    def test_hyperlink(self):
        runs = [
            {
                "startIndex": 0,
                "format": {"link": {"uri": "https://example.com"}},
            }
        ]
        result = textFormatRuns_to_html("link text", runs, None, {})
        assert '<a href="https://example.com" target="_blank">' in result
        assert "link text" in result

    def test_cell_level_hyperlink(self):
        """runs なし、セルレベルリンクのみ"""
        result = textFormatRuns_to_html(
            "click here", [], "https://example.com", {}
        )
        assert '<a href="https://example.com" target="_blank">' in result

    def test_mixed_formatting(self):
        """太字 + 色 + リンク混在"""
        runs = [
            {"startIndex": 0, "format": {}},
            {"startIndex": 6, "format": {"bold": True}},
            {
                "startIndex": 10,
                "format": {"link": {"uri": "https://ex.com"}},
            },
        ]
        result = textFormatRuns_to_html(
            "normal bold link here", runs, None, {}
        )
        assert "normal" in result
        assert "<b>" in result
        assert '<a href="https://ex.com"' in result

    def test_html_special_chars_escaped(self):
        """テキスト内の特殊文字がエスケープされる"""
        runs = [{"startIndex": 0, "format": {"bold": True}}]
        result = textFormatRuns_to_html("<script>", runs, None, {})
        assert "<b>&lt;script&gt;</b>" == result

    def test_newlines_preserved(self):
        """改行が保持される"""
        result = textFormatRuns_to_html("line1\nline2", [], None, {})
        assert "line1\nline2" in result

    def test_bold_and_color_combined(self):
        """太字 + 色の同時適用"""
        runs = [
            {
                "startIndex": 0,
                "format": {
                    "bold": True,
                    "foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0},
                },
            }
        ]
        result = textFormatRuns_to_html("text", runs, None, {})
        # bold が内側、color が外側
        assert '<span style="color:#ff0000"><b>text</b></span>' == result


# ===========================================
# 3. IR 列特殊処理層テスト
# ===========================================

class TestApplyFormattingToIrBlocks:
    """apply_formatting_to_ir_blocks() のテスト。"""

    def test_single_block_no_formatting(self):
        """書式なしの IR テキスト → プレーンテキスト per ブロック"""
        text = "企業概要テスト\n26.4[増収増益]\nIRコメント内容"
        overview_html, blocks = apply_formatting_to_ir_blocks(
            text, [], None, {}
        )
        assert "企業概要テスト" in overview_html
        assert "26.4" in blocks
        assert "IRコメント内容" in blocks["26.4"]

    def test_multiple_blocks(self):
        """複数日付ブロック"""
        text = "概要\n26.4[定量]\nコメント4\n26.1[定量]\nコメント1"
        _, blocks = apply_formatting_to_ir_blocks(text, [], None, {})
        assert "26.4" in blocks
        assert "26.1" in blocks
        assert "コメント4" in blocks["26.4"]
        assert "コメント1" in blocks["26.1"]

    def test_formatting_applied_to_correct_block(self):
        """書式が正しいブロックの ir_comment に適用される"""
        text = "概要\n26.4[定量]\n太字テスト"
        # "太字テスト" は offset 15 から始まる
        overview_len = len("概要") + 1  # +1 for \n
        date_line_len = len("26.4[定量]") + 1
        comment_start = overview_len + date_line_len
        runs = [
            {"startIndex": comment_start, "format": {"bold": True}},
        ]
        _, blocks = apply_formatting_to_ir_blocks(text, runs, None, {})
        assert "<b>太字テスト</b>" == blocks["26.4"]

    def test_overview_excluded_from_blocks(self):
        """overview 行はブロックに含まれない"""
        text = "企業概要行\n26.4[定量]\nコメント"
        overview_html, blocks = apply_formatting_to_ir_blocks(
            text, [], None, {}
        )
        assert "企業概要行" in overview_html
        assert "企業概要行" not in blocks.get("26.4", "")

    def test_empty_text(self):
        overview, blocks = apply_formatting_to_ir_blocks("", [], None, {})
        assert overview == ""
        assert blocks == {}

    def test_overview_only(self):
        """日付ブロックがなく overview のみの場合"""
        text = "概要のみのテキスト"
        overview_html, blocks = apply_formatting_to_ir_blocks(
            text, [], None, {}
        )
        assert "概要のみのテキスト" in overview_html
        assert blocks == {}


# ===========================================
# 4. 実行層テスト
# ===========================================

class TestApplyRichTextToRecord:
    """_apply_rich_text_to_record() のテスト。"""

    def test_patches_memo_and_openwork(self):
        """memo と openwork が HTML 化される"""
        from reimport_rich_text import _apply_rich_text_to_record

        record = {
            "code_s": "3496",
            "memo": "plain memo",
            "openwork": "plain ow",
            "snapshots": [],
        }
        api_row = [
            {},  # col 0: code
            {}, {}, {}, {},  # col 1-4
            {},  # col 5: IR (空)
            {}, {},  # col 6-7
            {  # col 8: openwork — bold
                "formattedValue": "bold ow",
                "textFormatRuns": [
                    {"startIndex": 0, "format": {"bold": True}}
                ],
            },
            {  # col 9: memo — リンク付き
                "formattedValue": "see link",
                "textFormatRuns": [
                    {
                        "startIndex": 0,
                        "format": {"link": {"uri": "https://example.com"}},
                    }
                ],
            },
        ]
        _apply_rich_text_to_record(record, api_row)
        assert "<b>bold ow</b>" == record["openwork"]
        assert "https://example.com" in record["memo"]

    def test_ir_comment_html_in_snapshots(self):
        """スナップショットの ir_comment が HTML 化される"""
        from reimport_rich_text import _apply_rich_text_to_record

        record = {
            "code_s": "3496",
            "overview": "old overview",
            "memo": "",
            "openwork": "",
            "snapshots": [
                {"date_yy_m": "26.4", "ir_comment": "plain comment"},
            ],
        }
        ir_text = "概要\n26.4[定量]\n太字コメント"
        overview_len = len("概要") + 1
        date_line_len = len("26.4[定量]") + 1
        comment_start = overview_len + date_line_len

        api_row = [
            {},  # col 0
            {}, {}, {}, {},  # col 1-4
            {  # col 5: IR
                "formattedValue": ir_text,
                "textFormatRuns": [
                    {"startIndex": comment_start, "format": {"bold": True}},
                ],
            },
        ]
        _apply_rich_text_to_record(record, api_row)
        assert "<b>太字コメント</b>" == record["snapshots"][0]["ir_comment"]

    def test_missing_api_columns_no_error(self):
        """API 行のカラム数が少なくてもエラーにならない"""
        from reimport_rich_text import _apply_rich_text_to_record

        record = {
            "code_s": "1234",
            "memo": "original",
            "openwork": "original",
            "snapshots": [],
        }
        _apply_rich_text_to_record(record, [{}])  # col 0 のみ
        # 変更されない
        assert record["memo"] == "original"
        assert record["openwork"] == "original"
