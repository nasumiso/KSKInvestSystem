#!/usr/bin/env python3
"""決算短信・決算説明資料を収集し、ページ単位のテキストを保存する。"""

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import html
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
import unicodedata
from urllib.parse import urljoin, urlparse

from pypdf import PdfReader
import requests

import disclosure
import portfolio
from ks_util import (
    DATA_DIR,
    USER_AGENT_CHROME,
    get_price_day,
    log_debug,
    log_error,
    log_print,
    log_warning,
)


IR_DOCS_DIR = Path(DATA_DIR) / "ir_docs"
KABUTAN_URL = "https://kabutan.jp/stock/news?code={code_s}&b=disclose&page={page}"
PDF_URL_RE = re.compile(
    r"https?://kabutan\.jp/disclosures/pdf/(\d{8})/(\d+)/?"
)
SETSUMEI_RE = re.compile(r"決算(補足)?説明(会)?資料|決算短信補足")
TANSHIN_RE = re.compile(r"決算短信")
# グロース市場の年次開示「事業計画及び成長可能性に関する事項」を中計として扱う。
CHUKI_PLAN_RE = re.compile(r"成長可能性に関する|事業計画(及び|並びに)成長可能性")
EXCLUDE_RE = re.compile(r"書き起こし|動画|開催")
REVISION_RE = re.compile(r"訂正|修正版|再表示|期中レビューの完了")
DEPTH_DAYS = {"latest": None, "1y": 365, "2y": 730}
DEPTH_RANK = {"latest": 0, "1y": 1, "2y": 2}
DEPTH_MONTHS = {"latest": 0, "1y": 12, "2y": 24}
REQUEST_INTERVAL = 1.0
IR_PAGE_SOURCE = "corporate_ir_page"
# 会社IRページ上の資料種別キーワード (#457)。リンク文言か URL に一致させる。
IR_PAGE_KEYWORDS = {
    "chuki_plan": re.compile(r"中期経営計画|中期事業方針|中期計画|中長期|Mid-?term", re.I),
    "setsumei": re.compile(
        r"決算(補足)?説明(会)?資料|決算短信補足|決算説明会|説明会資料|プレゼンテーション|presentation", re.I
    ),
}
# 策定の案内文や常設の会社案内は資料本体ではないため候補から外す。
IR_PAGE_EXCLUDE_RE = re.compile(r"お知らせ|会社案内|書き起こし|動画")
# 会社トップから IR トップへのリンク。開始URLが会社トップの銘柄で1回だけ辿る。
IR_TOP_LINK_RE = re.compile(
    r"^(IR|IR情報|投資家情報|株主・投資家(の皆様へ|情報)?|投資家の皆様へ|Investors?( Relations)?)$",
    re.I,
)
IR_TOP_HREF_RE = re.compile(r"/ir/?$|/ir\.html$|/investors?(-relations)?(\.html|/)?$", re.I)
ANCHOR_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
PDF_HREF_RE = re.compile(r"\.pdf($|[?#])", re.I)


def classify_heading(heading):
    """開示見出しを対象資料種別へ分類する。対象外は None を返す。"""
    normalized = unicodedata.normalize("NFKC", html.unescape(heading or ""))
    normalized = re.sub(r"<[^>]+>", "", normalized)
    if EXCLUDE_RE.search(normalized):
        return None
    # 「決算短信補足資料」は短信ではなく説明資料として扱う。
    if SETSUMEI_RE.search(normalized):
        doc_type = "setsumei"
    elif CHUKI_PLAN_RE.search(normalized):
        doc_type = "chuki_plan"
    elif TANSHIN_RE.search(normalized):
        doc_type = "tanshin"
    else:
        return None

    # 訂正資料そのものは「一部訂正について」でも収集する。
    # 「お知らせ」と明記された案内文や、訂正でない掲載案内は除外する。
    # 成長可能性資料は「〜に関する事項について」という見出しで本体が出るため除外しない。
    if "お知らせ" in normalized:
        return None
    if "について" in normalized and doc_type != "chuki_plan" and not REVISION_RE.search(normalized):
        return None
    return doc_type


def extract_period(heading):
    """見出しから会計期間と四半期を正規化して返す。"""
    normalized = unicodedata.normalize("NFKC", html.unescape(heading or ""))
    period_match = re.search(r"(\d{4})年(\d{1,2})月期", normalized)
    fiscal_period = None
    if period_match:
        fiscal_period = f"{period_match.group(1)}年{int(period_match.group(2))}月期"

    quarter_match = re.search(r"第\s*([1-4])\s*四半期", normalized)
    if quarter_match:
        quarter = f"Q{quarter_match.group(1)}"
    elif "中間" in normalized:
        quarter = "Q2"
    else:
        quarter = "FY"
    return fiscal_period, quarter


def classify_text_quality(page_texts):
    """抽出文字の量と文字種からテキスト品質を判定する。"""
    text = "".join(page_texts)
    page_count = len(page_texts)
    total_chars = len(text)
    average = total_chars / page_count if page_count else 0
    if average < 100:
        return "image_based"

    cjk_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", text))
    hangul_count = len(re.findall(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", text))
    garble_base = cjk_count + hangul_count
    garble_ratio = hangul_count / garble_base if garble_base else 0
    cjk_ratio = cjk_count / total_chars if total_chars else 0
    if garble_ratio > 0.3 or cjk_ratio < 0.06:
        return "garbled"
    return "ok"


def _pdf_url(source_url):
    match = PDF_URL_RE.search(source_url or "")
    if not match:
        raise ValueError(f"開示URLをPDF URLへ変換できません: {source_url}")
    date, doc_id = match.groups()
    return f"https://tdnet-pdf.kabutan.jp/{date}/{doc_id}.pdf", doc_id


def _candidate_from_record(record):
    doc_type = classify_heading(record.get("heading"))
    if record.get("type") != "kaiji" or not doc_type:
        return None
    pdf_url, doc_id = _pdf_url(record.get("url"))
    heading = re.sub(r"<[^>]+>", "", html.unescape(record.get("heading", ""))).strip()
    fiscal_period, quarter = extract_period(heading)
    return {
        "doc_id": doc_id,
        "doc_type": doc_type,
        "date": record["date"],
        "heading": heading,
        "fiscal_period": fiscal_period,
        "quarter": quarter,
        "url": pdf_url,
    }


class _RateLimiter:
    """同一収集処理内のHTTPリクエスト間隔を保証する。"""

    def __init__(self, interval=REQUEST_INTERVAL):
        self.interval = interval
        self.last_requested_at = None

    def wait(self):
        if self.last_requested_at is not None:
            remaining = self.interval - (time.monotonic() - self.last_requested_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_requested_at = time.monotonic()


def _get(session, url, limiter):
    limiter.wait()
    response = session.get(url, headers={"User-Agent": USER_AGENT_CHROME}, timeout=20)
    response.raise_for_status()
    return response


def _get_pdf(session, url, limiter):
    response = _get(session, url, limiter)
    pdf_bytes = response.content
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" not in content_type and not pdf_bytes.startswith(b"%PDF"):
        raise ValueError(f"PDF以外の応答です: {content_type or 'unknown'}")
    return pdf_bytes


def collect_candidates(code_s, depth="1y", session=None, limiter=None, now=None):
    """株探の開示一覧を新しいページから辿り、対象資料を返す。"""
    if depth not in DEPTH_DAYS:
        raise ValueError(f"未対応の収集深度です: {depth}")
    session = session or requests.Session()
    limiter = limiter or _RateLimiter()
    now = now or datetime.now()
    cutoff = None
    if DEPTH_DAYS[depth] is not None:
        cutoff = (get_price_day(now) - timedelta(days=DEPTH_DAYS[depth])).strftime("%Y%m%d")

    candidates = {}
    errors = []
    seen_records = set()
    reached_page_limit = True
    for page in range(1, 101):
        url = KABUTAN_URL.format(code_s=code_s, page=page)
        try:
            response = _get(session, url, limiter)
            records = disclosure.parse_disclosure_html(response.text)
        except Exception as exc:
            errors.append({"doc_id": None, "stage": "page_scan", "reason": str(exc)})
            reached_page_limit = False
            break
        if not records:
            reached_page_limit = False
            break

        page_keys = {
            (record.get("date"), record.get("url"), record.get("heading"))
            for record in records
        }
        if page_keys and page_keys.issubset(seen_records):
            reached_page_limit = False
            break
        seen_records.update(page_keys)

        dates = []
        for record in records:
            date = record.get("date", "")
            if re.fullmatch(r"\d{8}", date):
                dates.append(date)
            if cutoff and date < cutoff:
                continue
            try:
                candidate = _candidate_from_record(record)
            except ValueError as exc:
                if classify_heading(record.get("heading")):
                    errors.append({"doc_id": None, "stage": "url_convert", "reason": str(exc)})
                continue
            if candidate:
                candidates[candidate["doc_id"]] = candidate

        if depth == "latest" and candidates:
            reached_page_limit = False
            break
        if cutoff and dates and min(dates) < cutoff:
            reached_page_limit = False
            break

    if reached_page_limit:
        errors.append({"doc_id": None, "stage": "page_scan", "reason": "一覧の最大100ページに到達しました"})

    ordered = sorted(candidates.values(), key=lambda item: (item["date"], item["doc_id"]), reverse=True)
    if depth == "latest":
        ordered = ordered[:1]
    if not ordered:
        errors.append({"doc_id": None, "stage": "target_discovery", "reason": "対象資料が見つかりません"})
    return ordered, errors


def _slug(heading):
    value = unicodedata.normalize("NFKC", heading)
    value = re.sub(r"[\\/:*?\"<>|\s]+", "_", value).strip("_.")
    return value[:60] or "document"


def _clean_text(text):
    """pypdf がサロゲートのまま返す文字を直す。

    ペアは1文字に結合し、相方のいないサロゲートは置換文字にする。残したままだと
    UTF-8 で JSON を書けずに保存が失敗する (5138)。
    """
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _extract_pdf(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [_clean_text(page.extract_text() or "") for page in reader.pages]


def _write_json(path, value):
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as file_obj:
        json.dump(value, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")
    os.replace(tmp_path, path)


def _load_index(index_path):
    if not index_path.exists():
        return {
            "last_collected_at": None,
            "collected_depth": None,
            "last_requested_depth": None,
            "collected_months": 0,
            "collection_errors": {},
            "documents": [],
        }
    with open(index_path, encoding="utf-8") as file_obj:
        value = json.load(file_obj)
    value.setdefault("collection_errors", {})
    value.setdefault("documents", [])
    return value


def _link_revisions(documents):
    """明示的な訂正版だけを同種・同会計期間・同四半期の直前資料へ関連付ける。

    会社IRページ由来の資料は日付が推定値で訂正関係を判定できないうえ、
    is_latest は mark_superseded の手動操作だけで変えるため対象外とする。
    """
    documents = [item for item in documents if item.get("source") != IR_PAGE_SOURCE]
    for document in documents:
        document["is_latest"] = True
        document["supersedes"] = None
        document["superseded_by"] = None

    previous_by_key = {}
    for document in sorted(documents, key=lambda item: (item["date"], item["doc_id"])):
        key = (document.get("doc_type"), document.get("fiscal_period"), document.get("quarter"))
        previous_documents = previous_by_key.get(key, []) if all(key) else []
        previous = None
        if previous_documents and REVISION_RE.search(document.get("heading", "")):
            revised_heading = _comparable_heading(document["heading"])
            previous = max(
                previous_documents,
                key=lambda item: (
                    SequenceMatcher(
                        None, revised_heading, _comparable_heading(item["heading"])
                    ).ratio(),
                    item["date"],
                    item["doc_id"],
                ),
            )
            document["supersedes"] = previous["doc_id"]
            previous["superseded_by"] = document["doc_id"]
            previous["is_latest"] = False
        if all(key):
            previous_by_key.setdefault(key, []).append(document)


def _comparable_heading(heading):
    """訂正版と原本の比較用に見出しの改訂表現・装飾を除く。"""
    value = unicodedata.normalize("NFKC", heading or "")
    value = REVISION_RE.sub("", value)
    return re.sub(r"[\W_]+", "", value)


def _update_depth_state(index, requested_depth, errors):
    index["last_requested_depth"] = requested_depth
    collection_errors = index.setdefault("collection_errors", {})
    if errors:
        collection_errors[requested_depth] = errors
        return

    current = index.get("collected_depth")
    if current is None or DEPTH_RANK[requested_depth] > DEPTH_RANK[current]:
        index["collected_depth"] = requested_depth
    for depth in list(collection_errors):
        if DEPTH_RANK.get(depth, 999) <= DEPTH_RANK[requested_depth]:
            del collection_errors[depth]
    index["collected_months"] = DEPTH_MONTHS[index["collected_depth"]]


def _error(doc_id, stage, exc, at):
    return {"doc_id": doc_id, "stage": stage, "reason": str(exc), "at": at}


def _save_document(stock_dir, candidate, pdf_bytes, page_texts, now):
    """PDFとページ単位テキストを保存し、index 用のメタデータを返す。"""
    basename = f"{candidate['date']}_{candidate['doc_id']}_{_slug(candidate['heading'])}"
    pdf_name = basename + ".pdf"
    text_name = basename + ".json"
    pdf_path = stock_dir / pdf_name
    pdf_tmp_path = pdf_path.with_suffix(".pdf.tmp")
    with open(pdf_tmp_path, "wb") as file_obj:
        file_obj.write(pdf_bytes)
    os.replace(pdf_tmp_path, pdf_path)
    _write_json(
        stock_dir / text_name,
        {
            "doc_id": candidate["doc_id"],
            "pages": [
                {"page": page_number, "text": text}
                for page_number, text in enumerate(page_texts, start=1)
            ],
        },
    )
    return {
        **candidate,
        "pdf_path": pdf_name,
        "text_path": text_name,
        "downloaded_at": now,
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pages": len(page_texts),
        "total_chars": sum(len(text) for text in page_texts),
        "text_quality": classify_text_quality(page_texts),
        "is_latest": True,
        "supersedes": None,
        "superseded_by": None,
    }


def download_ir_docs(
    code_s,
    depth="1y",
    force=False,
    dry_run=False,
    output_dir=None,
    session=None,
    limiter=None,
):
    """指定銘柄のIR資料を収集し、今回の対象メタデータを返す。"""
    code_s = str(code_s).upper()
    if not re.fullmatch(r"\d[0-9A-Z]\d[0-9A-Z]", code_s):
        raise ValueError(f"不正な銘柄コードです: {code_s}")
    if depth == "2y":
        raise ValueError("2y収集は初回運用の対象外です")

    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    stock_dir = root / code_s
    index_path = stock_dir / "index.json"
    limiter = limiter or _RateLimiter()
    now = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")

    session_context = nullcontext(session) if session is not None else requests.Session()
    with session_context as session:
        candidates, scan_errors = collect_candidates(
            code_s, depth=depth, session=session, limiter=limiter
        )
        if dry_run:
            for candidate in candidates:
                log_print(f"{candidate['date']} {candidate['doc_type']} {candidate['heading']}")
            for item in scan_errors:
                log_warning(
                    f"IR資料dry-run走査失敗: {code_s} {item['stage']} {item['reason']}"
                )
            if scan_errors and not candidates:
                log_error(f"IR資料の候補を取得できませんでした: {code_s}")
            return candidates

        stock_dir.mkdir(parents=True, exist_ok=True)
        index = _load_index(index_path)
        documents_by_id = {item["doc_id"]: item for item in index["documents"]}
        errors = [_error(item.get("doc_id"), item["stage"], item["reason"], now) for item in scan_errors]

        latest_doc_id = candidates[0]["doc_id"] if candidates else None
        for candidate in candidates:
            existing = documents_by_id.get(candidate["doc_id"])
            if existing and not force:
                pdf_path = stock_dir / existing.get("pdf_path", "")
                text_path = stock_dir / existing.get("text_path", "")
                if pdf_path.is_file() and text_path.is_file():
                    log_debug(f"IR資料をスキップ: {code_s} {candidate['doc_id']}")
                    continue

            try:
                pdf_bytes = _get_pdf(session, candidate["url"], limiter)
            except Exception as exc:
                errors.append(_error(candidate["doc_id"], "pdf_download", exc, now))
                message = f"IR資料PDF取得失敗: {code_s} {candidate['doc_id']} {exc}"
                if candidate["doc_id"] == latest_doc_id:
                    log_error(message)
                else:
                    log_warning(message)
                continue

            try:
                page_texts = _extract_pdf(pdf_bytes)
            except Exception as exc:
                errors.append(_error(candidate["doc_id"], "text_extract", exc, now))
                message = f"IR資料テキスト抽出失敗: {code_s} {candidate['doc_id']} {exc}"
                if candidate["doc_id"] == latest_doc_id:
                    log_error(message)
                else:
                    log_warning(message)
                continue

            document = _save_document(stock_dir, candidate, pdf_bytes, page_texts, now)
            pdf_name = document["pdf_path"]
            documents_by_id[candidate["doc_id"]] = document
            log_print(f"IR資料を保存: {code_s} {pdf_name}")

    documents = sorted(
        documents_by_id.values(), key=lambda item: (item["date"], item["doc_id"]), reverse=True
    )
    _link_revisions(documents)
    index["documents"] = documents
    index["last_collected_at"] = now
    _update_depth_state(index, depth, errors)
    _write_json(index_path, index)

    if errors and depth == "latest":
        log_error(f"直近IR資料の収集に失敗: {code_s}")
    elif errors:
        log_warning(f"IR資料の{depth}収集に取りこぼし: {code_s} ({len(errors)}件)")
    return [documents_by_id[item["doc_id"]] for item in candidates if item["doc_id"] in documents_by_id]


def download_all(depth="1y", force=False, dry_run=False, output_dir=None):
    """保有・ウォッチ銘柄のIR資料を収集する。"""
    watch_codes, possess_codes = portfolio.parse_my_portforio()
    codes = sorted(set(watch_codes + possess_codes))
    results = {}
    limiter = _RateLimiter()
    with requests.Session() as session:
        for code_s in codes:
            try:
                results[code_s] = download_ir_docs(
                    code_s,
                    depth=depth,
                    force=force,
                    dry_run=dry_run,
                    output_dir=output_dir,
                    session=session,
                    limiter=limiter,
                )
            except Exception as exc:
                log_error(f"IR資料収集失敗: {code_s} {exc}")
                results[code_s] = []
    return results


def list_ir_docs(code_s, output_dir=None, doc_type=None):
    """保存済みIR資料をログへ一覧表示して返す。"""
    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    index = _load_index(root / str(code_s).upper() / "index.json")
    documents = [
        item for item in index["documents"] if doc_type is None or item["doc_type"] == doc_type
    ]
    for document in documents:
        latest = "latest" if document.get("is_latest", True) else "superseded"
        log_print(
            f"{document['date']} {document['doc_type']} {document['text_quality']} "
            f"{latest} {document['doc_id']} {document['heading']}"
        )
    return documents


def _check_public_url(url):
    """http(s) かつ解決先が全て公開アドレスの URL だけを許す。

    WebApp から任意 URL を取得させるため、localhost や LAN を叩かせない。
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"http(s) の URL ではありません: {url}")
    infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise ValueError(f"公開されていないアドレスは取得できません: {url}")


def _page_links(session, url, limiter):
    """ページを取得し、(絶対URL, リンク文言) の一覧を返す。"""
    _check_public_url(url)
    response = _get(session, url, limiter)
    response.encoding = response.apparent_encoding
    links = []
    for href, text in ANCHOR_RE.findall(response.text):
        text = re.sub(r"<[^>]+>|\s+", " ", html.unescape(text)).strip()
        # /ir → /ir/ のようなリダイレクト後は、到達先を相対リンクの基準にする
        links.append((urljoin(response.url, html.unescape(href)), text))
    return links


def _ir_page_doc_type(url, text):
    for doc_type, pattern in IR_PAGE_KEYWORDS.items():
        if pattern.search(text) or pattern.search(url):
            return doc_type
    return None


def guess_ir_top_url(code_s):
    """会社HP (上書き前の既定URL) のトップから IR トップの URL を推測する。無ければ None。"""
    from db_shelve import STOCKS_SHELVE, ShelveDB

    with ShelveDB(STOCKS_SHELVE, read_only=True) as db:
        top = ((db.get(str(code_s).upper()) or {}).get("corporate_url") or "").strip()
    if not top:
        return None
    return find_ir_top_link(_page_links(requests.Session(), top, _RateLimiter()), top)


def resolve_ir_start_url(code_s):
    """候補抽出の開始URL。会社HP上書き (#208) を優先し、無ければ会社HPを使う。"""
    import research_shelve
    from db_shelve import STOCKS_SHELVE, ShelveDB

    record = research_shelve.get_research_record(code_s) or {}
    override = (record.get("corporate_url_override") or "").strip()
    if override:
        return override
    with ShelveDB(STOCKS_SHELVE, read_only=True) as db:
        return ((db.get(code_s) or {}).get("corporate_url") or "").strip()


def _days_between(date_a, date_b):
    to_date = lambda value: datetime.strptime(value, "%Y%m%d")
    return abs((to_date(date_a) - to_date(date_b)).days)


def _mark_ir_page_candidates(candidates, documents, today):
    """候補に「古い」「TDnet 取得済みの可能性」の目印を付ける。

    古い: 推定日付が2年より前。IRページは新しい順に並ぶため、同じページで
    古い候補が出たら、日付を持たない後続の候補も古いとみなす。
    TDnet 取得済み: 見出しの会計期間・四半期が一致するか、期間が取れなければ
    推定日付が開示日の前後7日以内。PDF を取らずに判定するため確実ではない
    (同一PDFの重複保存は fetch_ir_page_doc の sha256 一致で防ぐ)。
    """
    cutoff = (datetime.strptime(today, "%Y%m%d") - timedelta(days=730)).strftime("%Y%m%d")
    tdnet_documents = [item for item in documents if item.get("source") != IR_PAGE_SOURCE]
    old_pages = set()
    for candidate in candidates:
        estimated = _estimate_date(candidate["heading"], candidate["url"], today)
        if estimated and estimated < cutoff:
            old_pages.add(candidate["source_page"])
        candidate["old"] = candidate["source_page"] in old_pages

        fiscal_period, quarter = extract_period(candidate["heading"])
        candidate["maybe_tdnet"] = next(
            (
                item["doc_id"] for item in tdnet_documents
                if item.get("doc_type") == candidate["doc_type"] and (
                    (item.get("fiscal_period"), item.get("quarter")) == (fiscal_period, quarter)
                    if fiscal_period
                    else estimated and _days_between(estimated, item["date"]) <= 7
                )
            ),
            None,
        )


def _same_site(url, base_url):
    """www. を除いたホストが一致するか、一方が他方のサブドメインなら同じサイトとみなす。"""
    host = (urlparse(url).hostname or "").removeprefix("www.")
    base = (urlparse(base_url).hostname or "").removeprefix("www.")
    return host == base or host.endswith("." + base) or base.endswith("." + host)


def find_ir_top_link(links, start_url):
    """会社トップのリンク一覧から IR トップらしいリンクを返す。無ければ None。

    文言「IR情報」はメニュー開閉用のダミーリンクに付いていることがあり (6227: isSmp?)、
    パス /ir.html は IR 問い合わせページのことがある (7729: contact/ir.html) ため、
    文言とパスの両方で絞り込み、一致の強い順に選ぶ。
    """
    links = [
        (url, text, urlparse(url).path) for url, text in links
        if url.rstrip("/") != start_url.rstrip("/")
    ]
    ranked = (
        # 文言とパスの両方が一致
        (url for url, text, path in links
         if IR_TOP_LINK_RE.search(text) and IR_TOP_HREF_RE.search(path)),
        # 文言が一致し、パスが IR 配下
        (url for url, text, path in links
         if IR_TOP_LINK_RE.search(text) and re.search(r"(^|/)(ir|investors?)\b", path, re.I)),
        # パスだけ一致
        (url for url, _, path in links if IR_TOP_HREF_RE.search(path)),
    )
    return next((url for group in ranked for url in group), None)


def _scan_start_page(links, start_url, add):
    """開始ページの PDF 候補を登録し、資料種別ごとのサブページ (先頭1件) を返す。

    サブページは同じサイト内に限る。会社トップのニュース欄にある外部記事
    (決算説明会の書き起こし記事など) を辿ると IR トップへ進めなくなるため。
    """
    subpages = {}
    for url, text in links:
        doc_type = _ir_page_doc_type(url, text)
        if not doc_type:
            continue
        if PDF_HREF_RE.search(url):
            add(url, text, doc_type, start_url)
        elif (
            doc_type not in subpages
            and url.rstrip("/") != start_url.rstrip("/")
            and _same_site(url, start_url)
        ):
            subpages[doc_type] = url
    return subpages


def find_ir_page_candidates(code_s, start_url, session=None, limiter=None, output_dir=None):
    """会社IRページから中計・決算説明資料のPDF候補を返す。DLはしない。

    開始ページの PDF リンクに加え、資料種別ごとに一致するサブページを1件だけ辿る。
    開始ページに候補もサブページも無ければ (会社トップの場合)、IR トップへの
    リンクを1回だけ辿ってそこを開始ページとみなす。リクエストは最大4回。
    """
    code_s = str(code_s).upper()
    session = session or requests.Session()
    limiter = limiter or _RateLimiter()
    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    documents = _load_index(root / code_s / "index.json")["documents"]
    downloaded_urls = {item.get("url") for item in documents}

    candidates = {}

    def add(url, text, doc_type, source_page):
        if url in candidates or IR_PAGE_EXCLUDE_RE.search(text):
            return
        candidates[url] = {
            "url": url,
            "heading": text or Path(urlparse(url).path).name,
            "doc_type": doc_type,
            "source_page": source_page,
            "downloaded": url in downloaded_urls,
        }

    links = _page_links(session, start_url, limiter)
    subpages = _scan_start_page(links, start_url, add)
    if not candidates and not subpages:
        ir_top = find_ir_top_link(links, start_url)
        if ir_top:
            log_debug(f"IRトップを辿る: {code_s} {ir_top}")
            start_url = ir_top
            links = _page_links(session, start_url, limiter)
            subpages = _scan_start_page(links, start_url, add)
    start_urls = {url for url, _ in links}

    for doc_type, page_url in subpages.items():
        try:
            sub_links = _page_links(session, page_url, limiter)
        except Exception as exc:
            log_warning(f"IRサブページ取得失敗: {code_s} {page_url} {exc}")
            continue
        for url, text in sub_links:
            # 資料種別ページ内の PDF は表記揺れがあるため、キーワード不一致でも候補に出す。
            # ただし開始ページにもあるものはヘッダー・フッター等のサイト共通リンクなので除く。
            if not PDF_HREF_RE.search(url):
                continue
            matched = _ir_page_doc_type(url, text)
            if matched or url not in start_urls:
                add(url, text, matched or doc_type, page_url)

    if not candidates:
        log_warning(f"IRページに資料候補がありません: {code_s} {start_url}")
    result = list(candidates.values())
    _mark_ir_page_candidates(result, documents, get_price_day(datetime.now()).strftime("%Y%m%d"))
    return result


def _estimate_date(heading, url, today, first_page=""):
    """資料1ページ目・見出し・ファイル名の順に YYYYMMDD を推定する。取れなければ None。

    1ページ目は表紙の日付 (「2024年3月21日」) だけを見る。月までの表記は
    対象期間の可能性があるため使わない。見出し・ファイル名は月までなら月初とする。
    「2027年4月通期 第1四半期」のような決算期末は公表日より先になるため、
    today より後の日付は捨てて次の候補を見る。
    """
    for value in _date_candidates(heading, url, first_page):
        if value <= today:
            return value
    return None


def _date_candidates(heading, url, first_page):
    cover = re.sub(r"\s+", "", unicodedata.normalize("NFKC", first_page or ""))[:500]
    match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", cover)
    if match and 1 <= int(match.group(2)) <= 12 and 1 <= int(match.group(3)) <= 31:
        yield f"{match.group(1)}{int(match.group(2)):02d}{int(match.group(3)):02d}"
    for text in (unicodedata.normalize("NFKC", heading or ""), Path(urlparse(url).path).name):
        # 見出しの「2026.5.14」「2026/5/14」「2026年08月04日」形式 (6134, 9270, 7729)
        match = re.search(r"(?<!\d)(20\d{2})(?:[./]|年)(\d{1,2})(?:[./]|月)(\d{1,2})(?!\d)", text)
        if match and 1 <= int(match.group(2)) <= 12 and 1 <= int(match.group(3)) <= 31:
            yield f"{match.group(1)}{int(match.group(2)):02d}{int(match.group(3)):02d}"
        match = re.search(r"(20\d{2})年\s*(\d{1,2})月", text)
        if match and 1 <= int(match.group(2)) <= 12:
            yield f"{match.group(1)}{int(match.group(2)):02d}01"
        # ファイル名は日付の後ろに時刻や連番が続くことがある (20260520181351871s.pdf)
        match = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})?", text)
        if match and 1 <= int(match.group(2)) <= 12:
            day = match.group(3) if match.group(3) and 1 <= int(match.group(3)) <= 31 else "01"
            yield f"{match.group(1)}{match.group(2)}{day}"


def fetch_ir_page_doc(
    code_s, url, doc_type, heading=None, source_page=None,
    output_dir=None, session=None, limiter=None,
):
    """会社IRページ上のPDFを1件取得して index.json に追加する。

    日付は推定値のため常に date_estimated=True とし、is_latest は自動で落とさない。
    TDnet 経路の網羅性を表す last_collected_at 等は更新しない。
    (資料メタデータ, 新規保存したか) を返す。同一PDFが保存済みなら既存を返す。
    """
    code_s = str(code_s).upper()
    if not re.fullmatch(r"\d[0-9A-Z]\d[0-9A-Z]", code_s):
        raise ValueError(f"不正な銘柄コードです: {code_s}")
    if doc_type not in IR_PAGE_KEYWORDS:
        raise ValueError(f"未対応の資料種別です: {doc_type}")
    _check_public_url(url)

    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    stock_dir = root / code_s
    index_path = stock_dir / "index.json"
    now_dt = datetime.now(timezone(timedelta(hours=9)))
    now = now_dt.isoformat(timespec="seconds")
    session = session or requests.Session()
    pdf_bytes = _get_pdf(session, url, limiter or _RateLimiter())
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    index = _load_index(index_path)
    for existing in index["documents"]:
        if existing.get("sha256") == sha256:
            log_print(f"同一PDFが保存済みのためスキップ: {code_s} {existing['doc_id']}")
            return existing, False

    page_texts = _extract_pdf(pdf_bytes)
    heading = (heading or "").strip() or Path(urlparse(url).path).name
    fiscal_period, quarter = extract_period(heading) if doc_type == "setsumei" else (None, None)
    candidate = {
        "doc_id": sha256[:16],
        "doc_type": doc_type,
        "date": _estimate_date(
            heading, url, now_dt.strftime("%Y%m%d"), page_texts[0] if page_texts else ""
        ) or now_dt.strftime("%Y%m%d"),
        "date_estimated": True,
        "heading": heading,
        "fiscal_period": fiscal_period,
        "quarter": quarter,
        "url": url,
        "source": IR_PAGE_SOURCE,
        "source_page": source_page,
    }
    stock_dir.mkdir(parents=True, exist_ok=True)
    document = _save_document(stock_dir, candidate, pdf_bytes, page_texts, now)
    index["documents"] = sorted(
        index["documents"] + [document], key=lambda item: (item["date"], item["doc_id"]), reverse=True
    )
    _write_json(index_path, index)
    log_print(f"IR資料を保存: {code_s} {document['pdf_path']}")
    return document, True


def mark_superseded(code_s, doc_id, output_dir=None):
    """会社IRページ由来の資料を手動で旧版にする。"""
    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    index_path = root / str(code_s).upper() / "index.json"
    index = _load_index(index_path)
    document = next((item for item in index["documents"] if item["doc_id"] == doc_id), None)
    if document is None:
        raise ValueError(f"資料が見つかりません: {code_s} {doc_id}")
    if document.get("source") != IR_PAGE_SOURCE:
        raise ValueError(f"適時開示由来の資料は訂正版で自動判定するため対象外です: {doc_id}")
    document["is_latest"] = False
    _write_json(index_path, index)
    log_print(f"IR資料を旧版にしました: {code_s} {doc_id} {document['heading']}")
    return document


def pending_candidates(candidates, doc_type=None):
    """一括走査用に、取りに行く価値のある候補だけを残す。

    古い・取得済み・TDnet取得済みの可能性がある候補を除き、同じ見出しは1件にまとめる
    (6890 のように同じ資料が別URLで2系統並ぶサイトがある)。
    """
    seen = set()
    result = []
    for item in candidates:
        if item["old"] or item["downloaded"] or item["maybe_tdnet"]:
            continue
        if doc_type and item["doc_type"] != doc_type:
            continue
        key = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", item["heading"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _print_pending_candidates(statuses, doc_type=None):
    """指定ステータスの銘柄を走査し、未取得の直近候補を銘柄ごとに表示する。"""
    import portfolio_shelve

    codes = sorted({
        record["code_s"]
        for status in statuses
        for record in portfolio_shelve.list_records(status=status.strip())
    })
    session = requests.Session()
    limiter = _RateLimiter()
    total = 0
    for code_s in codes:
        start_url = resolve_ir_start_url(code_s)
        if not start_url:
            continue
        try:
            candidates = find_ir_page_candidates(code_s, start_url, session, limiter)
        except Exception as exc:
            log_warning(f"IRページ取得失敗: {code_s} {exc}")
            continue
        items = pending_candidates(candidates, doc_type)
        if items:
            log_print(f"## {code_s} {start_url}")
        for item in items:
            log_print(f"  {item['doc_type']} {item['heading']} {item['url']}")
        total += len(items)
    log_print(f"未取得の候補: {total}件 / {len(codes)}銘柄 (取得は fetch-page <code_s> <url>)")


def tdnet_setsumei_missing(code_s, output_dir=None):
    """株探 (TDnet) に決算説明資料を出していない銘柄なら、会社HP由来の最新説明資料の日付を返す。

    株探から1年分収集済みで、短信はあるのに説明資料が0件の銘柄が対象。こうした会社は
    説明資料を会社HPにしか置かない (保有28銘柄中11銘柄)。対象外なら None、対象で
    会社HP由来の説明資料も無ければ空文字を返す。ローカルの index.json だけを見る。
    """
    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    index = _load_index(root / str(code_s).upper() / "index.json")
    if DEPTH_RANK.get(index.get("collected_depth"), -1) < DEPTH_RANK["1y"]:
        return None
    tdnet = [item for item in index["documents"] if item.get("source") != IR_PAGE_SOURCE]
    if not any(item["doc_type"] == "tanshin" for item in tdnet):
        return None
    if any(item["doc_type"] == "setsumei" for item in tdnet):
        return None
    dates = [
        item["date"] for item in index["documents"]
        if item.get("source") == IR_PAGE_SOURCE and item["doc_type"] == "setsumei"
    ]
    return max(dates, default="")


def group_ir_docs(code_s, output_dir=None):
    """詳細画面のIR資料モーダル用に、保存済み資料を中計・期ごと・期不明に分けて返す (issue #473)。

    期ごとの行は (fiscal_period, quarter) でまとめ、行内の最新日付の降順に並べる。
    各資料は index のエントリそのまま。count は旧版を除いた件数。
    """
    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    index = _load_index(root / str(code_s).upper() / "index.json")
    documents = sorted(index["documents"], key=lambda item: item["date"], reverse=True)
    chuki, unknown, rows = [], [], {}
    for document in documents:
        if document["doc_type"] == "chuki_plan":
            chuki.append(document)
            continue
        key = (document.get("fiscal_period"), document.get("quarter"))
        if not all(key):
            unknown.append(document)
            continue
        row = rows.setdefault(key, {
            "fiscal_period": key[0], "quarter": key[1], "tanshin": [], "setsumei": [],
        })
        row[document["doc_type"]].append(document)
    return {
        "chuki": chuki,
        # 資料は日付降順に走査しているので、行の作成順がそのまま行内最新日付の降順になる
        "periods": list(rows.values()),
        "unknown": unknown,
        "last_collected_at": index.get("last_collected_at"),
        "count": sum(1 for item in documents if item.get("is_latest", True)),
    }


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    download_parser = subparsers.add_parser("download", help="単一銘柄を収集")
    download_parser.add_argument("code_s")
    download_parser.add_argument("--depth", choices=DEPTH_DAYS, default="1y")
    download_parser.add_argument("--force", action="store_true")
    download_parser.add_argument("--dry-run", action="store_true")

    all_parser = subparsers.add_parser("download_all", help="保有・ウォッチ銘柄を収集")
    all_parser.add_argument("--depth", choices=DEPTH_DAYS, default="1y")
    all_parser.add_argument("--force", action="store_true")
    all_parser.add_argument("--dry-run", action="store_true")

    list_parser = subparsers.add_parser("list", help="保存済み資料を表示")
    list_parser.add_argument("code_s")
    list_parser.add_argument("--doc-type", choices=["tanshin", "setsumei", "chuki_plan"])

    candidates_parser = subparsers.add_parser(
        "page-candidates", help="会社IRページから中計・説明資料の候補を表示 (DLしない)"
    )
    candidates_parser.add_argument("code_s", nargs="?")
    candidates_parser.add_argument("--url", help="開始URL (省略時は会社HPの上書き→会社HP)")
    candidates_parser.add_argument(
        "--status", help="保有ステータスで一括走査 (例: 1保,2準)。未取得の直近候補だけを表示"
    )
    candidates_parser.add_argument("--doc-type", choices=IR_PAGE_KEYWORDS)

    fetch_parser = subparsers.add_parser("fetch-page", help="会社IRページのPDFを1件取得")
    fetch_parser.add_argument("code_s")
    fetch_parser.add_argument("url")
    fetch_parser.add_argument("--doc-type", choices=IR_PAGE_KEYWORDS, required=True)
    fetch_parser.add_argument("--heading")

    superseded_parser = subparsers.add_parser(
        "mark-superseded", help="会社IRページ由来の資料を旧版にする"
    )
    superseded_parser.add_argument("code_s")
    superseded_parser.add_argument("doc_id")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "download":
        download_ir_docs(args.code_s, args.depth, args.force, args.dry_run)
    elif args.command == "download_all":
        download_all(args.depth, args.force, args.dry_run)
    elif args.command == "list":
        list_ir_docs(args.code_s, doc_type=args.doc_type)
    elif args.command == "page-candidates":
        if args.status:
            _print_pending_candidates(args.status.split(","), args.doc_type)
            return
        if not args.code_s:
            log_error("銘柄コードか --status を指定してください")
            return
        code_s = args.code_s.upper()
        start_url = args.url or resolve_ir_start_url(code_s)
        if not start_url:
            log_error(f"会社HPのURLがありません: {code_s}")
            return
        for item in find_ir_page_candidates(code_s, start_url):
            if args.doc_type and item["doc_type"] != args.doc_type:
                continue
            mark = "取得済" if item["downloaded"] else "未取得"
            if item["maybe_tdnet"]:
                mark += "(TDnet取得済?)"
            if item["old"]:
                mark += "(古い)"
            log_print(f"{item['doc_type']} {mark} {item['heading']} {item['url']}")
    elif args.command == "fetch-page":
        fetch_ir_page_doc(args.code_s, args.url, args.doc_type, args.heading)
    else:
        mark_superseded(args.code_s, args.doc_id)


if __name__ == "__main__":
    main()
