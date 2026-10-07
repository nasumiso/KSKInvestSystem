#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Yahoo Finance 掲示板の投稿を取得する (issue #62)。

取得した投稿は Claude Code スキル `/sentiment` が読み、感情ラベルを付けて要約する。
このモジュールは取得とパースだけを行い、分析・保存はしない。

使い方:
    python sentiment.py 3496             # 新しい順に50件
    python sentiment.py 3496 --limit 20
"""

import argparse
import html as html_lib
import json
import os
import re
from typing import Dict, List, Optional

from ks_util import (
    DATA_DIR,
    http_get_html,
    log_print,
    log_warning,
    setup_logger,
)

URL_FORUM = "https://finance.yahoo.co.jp/quote/%s.T/forum"
CACHE_DIR_FORUM = os.path.join(DATA_DIR, "stock_data", "yahoo", "forum")
DEFAULT_LIMIT = 50

# Next.js が埋め込む `self.__next_f.push([1,"..."])` の文字列部分
_RE_NEXT_F = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_RE_TAG = re.compile(r"<[^>]+>")


def _clean_body(body: str) -> str:
    """投稿本文の HTML タグ・実体参照を外して1行にする"""
    text = _RE_TAG.sub(" ", body or "")
    return " ".join(html_lib.unescape(text).split())


def parse_forum_posts(html: str) -> List[Dict]:
    """
    掲示板 HTML から投稿リストを取り出す (新しい順)。

    投稿は `preloadedStore.bbsComment.bbs` 配列に入っている。形式が変わって
    取り出せないときは例外にせず空リストを返す (呼び出し側でエラー表示する)。
    """
    try:
        payload = "".join(json.loads(c) for c in _RE_NEXT_F.findall(html))
        start = payload.index('"bbs":', payload.index('"bbsComment"'))
        raw_posts, _ = json.JSONDecoder().raw_decode(payload[start + len('"bbs":'):])
    except ValueError:
        return []

    posts = []
    for raw in raw_posts:
        body = _clean_body(raw.get("body", ""))
        if not body:
            continue
        # 未設定の項目は "$undefined" という文字列で入っている
        feel = raw.get("feelLabel")
        posts.append({
            "part": raw.get("part"),
            "post_date": raw.get("postDate"),
            "body": body,
            "good": raw.get("good") or 0,
            "bad": raw.get("bad") or 0,
            "feel_label": None if feel in (None, "$undefined") else feel,
        })
    return posts


def get_forum_posts(code_s: str, limit: Optional[int] = DEFAULT_LIMIT) -> List[Dict]:
    """
    指定銘柄の掲示板の投稿を取得する (新しい順、最大 limit 件)。

    掲示板は刻々と変わるのでキャッシュは読まない (取得した HTML は調査用に残る)。
    取得・パースに失敗したら空リストを返す。
    """
    os.makedirs(CACHE_DIR_FORUM, exist_ok=True)
    url = URL_FORUM % code_s
    html, status = http_get_html(
        url, use_cache=False, cache_dir=CACHE_DIR_FORUM, with_status=True
    )
    if not 200 <= status < 300:
        log_warning("掲示板を取得できませんでした (HTTP %d): %s" % (status, url))
        return []
    posts = parse_forum_posts(html)
    if not posts:
        log_warning("掲示板の投稿を取り出せませんでした (HTML形式の変更の可能性): %s" % url)
    return posts[:limit] if limit else posts


def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance 掲示板の投稿を表示する")
    parser.add_argument("code_s", help="銘柄コード (例: 3496, 285A)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="表示する件数 (新しい順、既定 %d)" % DEFAULT_LIMIT)
    args = parser.parse_args()

    posts = get_forum_posts(args.code_s, args.limit)
    if not posts:
        log_print("投稿を取得できませんでした: %s" % args.code_s)
        return
    log_print("掲示板 %s: %d件 (%s 〜 %s)" % (
        args.code_s, len(posts), posts[-1]["post_date"], posts[0]["post_date"]))
    for p in posts:
        feel = " 投稿者の選択:%s" % p["feel_label"] if p["feel_label"] else ""
        log_print("[%s] %s そう思う:%d そう思わない:%d%s" % (
            p["part"], p["post_date"], p["good"], p["bad"], feel))
        log_print("  " + p["body"])


if __name__ == "__main__":
    setup_logger("sentiment")
    main()
