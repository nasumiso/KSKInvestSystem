"""銘柄詳細と一覧の間で表示条件を引き継ぐ。"""

from urllib.parse import urlencode, urlsplit, urlunsplit

from flask import redirect, request, url_for

_RETURN_LABELS = {
    "/": "検索結果へ戻る",
    "/portfolio": "銘柄一覧へ戻る",
    "/portfolio/charts": "チャート一覧へ戻る",
    "/portfolio/shikiho": "四季報一覧へ戻る",
    "/portfolio/themes/summary": "テーマサマリーへ戻る",
}


def get_return_link(value):
    """許可したアプリ内一覧だけを戻り先として受け付ける。"""
    if not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or parts.fragment or parts.path not in _RETURN_LABELS:
        return None
    # // や /// から始まるURLはブラウザでの解釈が変わりうるため受け付けない。
    if not value.startswith("/") or value.startswith("//"):
        return None
    return (value, _RETURN_LABELS[parts.path])


def listing_return_url():
    """現在の一覧URLを返す。検索は1件直行を抑えて戻り先を表示する。"""
    if request.path == "/":
        params = [(k, v) for k, v in request.args.items(multi=True) if k != "show_results"]
        params.append(("show_results", "1"))
        return urlunsplit(("", "", "/", urlencode(params), ""))
    return request.full_path.rstrip("?")


def stock_detail_url(code_s):
    """元の一覧URL付きの銘柄詳細URLを生成する。"""
    return url_for("detail.stock_detail", code_s=code_s, return_to=listing_return_url())


def redirect_stock_detail(code_s, **values):
    """詳細画面からの通常POSTでも検証済みの戻り先を維持する。"""
    return_link = get_return_link(request.form.get("detail_return_to"))
    if return_link:
        values["return_to"] = return_link[0]
    return redirect(url_for("detail.stock_detail", code_s=code_s, **values))
