"""sentiment.py のユニットテスト (掲示板 HTML のパース)"""

import json

import pytest

import sentiment


def _forum_html(posts):
    """掲示板ページと同じ埋め込み形式 (`self.__next_f.push`) の HTML を作る"""
    payload = '5:{"preloadedStore":{"bbsComment":{"threadId":"x","bbs":%s}}}' % json.dumps(
        posts, ensure_ascii=False)
    return "<html><script>self.__next_f.push([1,%s])</script></html>" % json.dumps(payload)


class TestParseForumPosts:
    def test_投稿を取り出して整形する(self):
        html = _forum_html([
            {"part": 12, "postDate": "2026/10/7 18:05", "good": 2, "bad": 4,
             "feelLabel": "strongest",
             "body": '下がったら<br />\n<a href="https://example.com">買増し</a>&hellip;'},
            {"part": 11, "postDate": "2026/10/6 9:00", "good": 0, "bad": 0,
             "feelLabel": "$undefined", "body": "topix外された"},
            {"part": 10, "postDate": "2026/10/5 9:00", "feelLabel": "$undefined", "body": ""},
        ])
        posts = sentiment.parse_forum_posts(html)
        # 本文が空の投稿は落とす。タグと実体参照は外す
        assert [p["part"] for p in posts] == [12, 11]
        assert posts[0]["body"] == "下がったら 買増し …"
        assert (posts[0]["good"], posts[0]["bad"], posts[0]["feel_label"]) == (2, 4, "strongest")
        assert posts[1]["feel_label"] is None

    @pytest.mark.parametrize("html", [
        "",
        "<html><body>メンテナンス中</body></html>",
        '<script>self.__next_f.push([1,"5:{\\"other\\":1}"])</script>',
    ])
    def test_形式が違えば空リスト(self, html):
        assert sentiment.parse_forum_posts(html) == []
