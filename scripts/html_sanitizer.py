#!/usr/bin/env python3
"""
許可タグベースの HTML サニタイザ。

reimport_rich_text.py が生成する HTML 片と、Webapp のユーザー入力を
安全にフィルタリングする。外部依存なし（Python 標準ライブラリのみ）。

許可タグ:
    <b>          — 太字
    <a href target> — ハイパーリンク (http/https のみ)
    <span style>   — 色付きテキスト (color:#XXXXXX のみ)

    issue #165 で theme-news markdown 表示のため以下を追加:
    <h2/h3/h4>, <ul/ol/li>, <p>, <strong>, <em>, <code>, <br>,
    <table/thead/tbody/tr/th/td>
"""

import re
from html.parser import HTMLParser

# 許可タグと属性の定義
ALLOWED_TAGS = frozenset({
    "b", "a", "span",
    # theme-news 表示用 (markdown 生成タグ + Sources 折りたたみ + 改行ヒント)
    "h2", "h3", "h4", "ul", "ol", "li", "p", "strong", "em", "code", "br",
    "table", "thead", "tbody", "tr", "th", "td",
    "details", "summary", "wbr",
})

# 属性値の許可パターン
# href は外部リンク (http/https) と theme-news 脚注の内部 anchor (#thn-src-N) を許可。
_HREF_PATTERN = re.compile(r"^(?:https?://|#thn-src-\d{1,2}$)")
_TARGET_PATTERN = re.compile(r"^_blank$")
_REL_PATTERN = re.compile(r"^noopener$")
_STYLE_COLOR_PATTERN = re.compile(r"^color:#[0-9a-fA-F]{3,6}$")
# theme-news Sources セクション折りたたみ用に <details class="sources"> のみ許可。
# 他クラス名は除去される (XSS 経路にはならないが、混乱回避のため厳密に絞る)。
_DETAILS_CLASS_PATTERN = re.compile(r"^sources$")
# theme-news 脚注リンク用の class と Sources <li> の id を専用パターンで許可。
_A_CLASS_PATTERN = re.compile(r"^thn-footnote$")
_LI_ID_PATTERN = re.compile(r"^thn-src-\d{1,2}$")

ALLOWED_ATTRS = {
    "a": {
        "href": _HREF_PATTERN,
        "target": _TARGET_PATTERN,
        "rel": _REL_PATTERN,
        "class": _A_CLASS_PATTERN,
    },
    "span": {"style": _STYLE_COLOR_PATTERN},
    "details": {"class": _DETAILS_CLASS_PATTERN},
    "li": {"id": _LI_ID_PATTERN},
}


class _SanitizingParser(HTMLParser):
    """許可タグのみ通す HTMLParser サブクラス。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            # 不許可タグはエスケープして出力
            self._parts.append(self._escape_tag(tag, attrs))
            return
        # 許可属性のみフィルタ
        allowed = ALLOWED_ATTRS.get(tag, {})
        safe_attrs = []
        for name, value in attrs:
            pattern = allowed.get(name)
            if pattern and value and pattern.match(value):
                safe_attrs.append((name, value))
        # タグを再構築
        attr_str = ""
        for name, value in safe_attrs:
            attr_str += f' {name}="{_escape_attr(value)}"'
        self._parts.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag):
        if tag in ALLOWED_TAGS:
            self._parts.append(f"</{tag}>")
        # 不許可タグの閉じタグは無視（開始タグをエスケープ済みなので対応不要）

    def handle_data(self, data):
        self._parts.append(_escape_text(data))

    def handle_entityref(self, name):
        self._parts.append(f"&{name};")

    def handle_charref(self, name):
        self._parts.append(f"&#{name};")

    def _escape_tag(self, tag, attrs):
        """不許可タグをエスケープして無害なテキストにする。"""
        attr_str = ""
        for name, value in attrs:
            attr_str += f' {name}="{_escape_attr(value)}"'
        return _escape_text(f"<{tag}{attr_str}>")

    def get_result(self):
        return "".join(self._parts)


def _escape_text(text):
    """テキスト部分の HTML エスケープ。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(value):
    """属性値のエスケープ。"""
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def sanitize_html(html_str):
    """許可タグ・属性のみ残し、それ以外をエスケープする。

    Args:
        html_str: サニタイズ対象の HTML 文字列

    Returns:
        安全な HTML 文字列
    """
    if not html_str:
        return html_str or ""
    parser = _SanitizingParser()
    parser.feed(html_str)
    return parser.get_result()


class _TagStripper(HTMLParser):
    """全 HTML タグを除去してプレーンテキストを抽出する。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_result(self):
        return "".join(self._parts)


def strip_html_tags(html_str):
    """全タグを除去してプレーンテキストを返す。

    キーワード検索で HTML タグがノイズになるのを防ぐ。

    Args:
        html_str: タグ除去対象の文字列

    Returns:
        プレーンテキスト
    """
    if not html_str:
        return html_str or ""
    parser = _TagStripper()
    parser.feed(html_str)
    return parser.get_result()
