"""html_sanitizer モジュールのユニットテスト。"""

import os
import sys

import pytest

# scripts/ を sys.path に追加
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from html_sanitizer import sanitize_html, strip_html_tags


class TestSanitizeHtml:
    """sanitize_html() のテスト。"""

    # --- 許可タグの通過 ---

    def test_bold_tag_passes(self):
        assert sanitize_html("<b>太字</b>") == "<b>太字</b>"

    def test_anchor_with_valid_href(self):
        result = sanitize_html('<a href="https://example.com" target="_blank">リンク</a>')
        assert result == '<a href="https://example.com" target="_blank">リンク</a>'

    def test_anchor_http(self):
        result = sanitize_html('<a href="http://example.com">link</a>')
        assert 'href="http://example.com"' in result

    def test_span_with_valid_color(self):
        result = sanitize_html('<span style="color:#cc0000">赤字</span>')
        assert result == '<span style="color:#cc0000">赤字</span>'

    def test_span_with_short_hex(self):
        result = sanitize_html('<span style="color:#f00">赤</span>')
        assert result == '<span style="color:#f00">赤</span>'

    # --- 不許可タグの除去 ---

    def test_script_tag_escaped(self):
        result = sanitize_html("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "&lt;script" in result

    def test_img_tag_escaped(self):
        result = sanitize_html('<img src="x" onerror="alert(1)">')
        assert "<img" not in result
        assert "&lt;img" in result

    def test_iframe_tag_escaped(self):
        result = sanitize_html('<iframe src="evil.com"></iframe>')
        assert "<iframe" not in result

    # --- 不許可属性の除去 ---

    def test_onclick_removed_from_anchor(self):
        result = sanitize_html('<a href="https://ok.com" onclick="evil()">text</a>')
        assert "onclick" not in result
        assert 'href="https://ok.com"' in result

    def test_javascript_uri_blocked(self):
        result = sanitize_html('<a href="javascript:alert(1)">click</a>')
        # javascript: URI は href パターンに一致しないので属性が除去される
        assert "javascript" not in result
        assert "<a>" in result  # href なしの a タグ

    def test_invalid_style_removed(self):
        result = sanitize_html('<span style="background:red">text</span>')
        assert "background" not in result
        # style 属性が除去された span タグ
        assert "<span>" in result

    # --- 混合コンテンツ ---

    def test_mixed_content(self):
        html = 'normal <b>bold</b> <span style="color:#ff0000">red</span> text'
        result = sanitize_html(html)
        assert "<b>bold</b>" in result
        assert '<span style="color:#ff0000">red</span>' in result
        assert "normal" in result

    def test_nested_tags(self):
        result = sanitize_html('<b><span style="color:#cc0000">bold red</span></b>')
        assert "<b>" in result
        assert '<span style="color:#cc0000">' in result

    # --- エッジケース ---

    def test_empty_string(self):
        assert sanitize_html("") == ""

    def test_none_returns_empty(self):
        assert sanitize_html(None) == ""

    def test_plain_text_unchanged(self):
        assert sanitize_html("普通のテキスト") == "普通のテキスト"

    def test_special_chars_escaped(self):
        result = sanitize_html("A < B & C > D")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_newlines_preserved(self):
        result = sanitize_html("line1\nline2\nline3")
        assert "line1\nline2\nline3" in result

    def test_markdown_tags_allowed_and_xss_still_blocked(self):
        """issue #165: theme-news 表示用に h3/ul/li 等を許可しつつ script は除去維持。"""
        html = (
            "<h3>見出し</h3><ul><li><strong>太字</strong></li></ul>"
            "<script>alert(1)</script><p onclick='x'>段落</p>"
        )
        result = sanitize_html(html)
        # 許可タグはそのまま残る
        assert "<h3>見出し</h3>" in result
        assert "<ul><li><strong>太字</strong></li></ul>" in result
        assert "<p>段落</p>" in result   # onclick は剥がされる
        # script タグはエスケープされる
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    @pytest.mark.parametrize(
        "html,must_contain,must_not_contain",
        [
            # 内部 anchor (#thn-src-N) と class="thn-footnote" は許可
            (
                '<a href="#thn-src-4" class="thn-footnote">[4]</a>',
                ['href="#thn-src-4"', 'class="thn-footnote"'],
                [],
            ),
            # <li id="thn-src-N"> も許可
            (
                '<li id="thn-src-3">[3] body</li>',
                ['id="thn-src-3"'],
                [],
            ),
            # 範囲外の id / class / href は剥がされる (XSS 防御維持)
            (
                '<a href="#other" class="other">x</a>'
                '<li id="other-id">y</li>'
                '<a href="javascript:alert(1)">z</a>',
                [],
                ["#other", 'class="other"', "other-id", "javascript:"],
            ),
        ],
    )
    def test_theme_news_footnote_anchors_allowed(self, html, must_contain, must_not_contain):
        """theme-news 脚注用の内部 anchor (#thn-src-N) と id="thn-src-N" / class="thn-footnote"
        は許可されるが、それ以外の id/class/href は剥がされる。"""
        result = sanitize_html(html)
        for needle in must_contain:
            assert needle in result, f"expected {needle!r} in {result!r}"
        for needle in must_not_contain:
            assert needle not in result, f"unexpected {needle!r} in {result!r}"


class TestStripHtmlTags:
    """strip_html_tags() のテスト。"""

    def test_strips_all_tags(self):
        result = strip_html_tags('<b>bold</b> <span style="color:#f00">red</span>')
        assert result == "bold red"

    def test_strips_anchor(self):
        result = strip_html_tags('<a href="https://example.com">link text</a>')
        assert result == "link text"

    def test_plain_text_unchanged(self):
        assert strip_html_tags("plain text") == "plain text"

    def test_empty_string(self):
        assert strip_html_tags("") == ""

    def test_none_returns_empty(self):
        assert strip_html_tags(None) == ""

    def test_preserves_newlines(self):
        result = strip_html_tags("<b>line1</b>\n<b>line2</b>")
        assert result == "line1\nline2"
