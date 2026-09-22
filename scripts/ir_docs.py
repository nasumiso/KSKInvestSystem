#!/usr/bin/env python3
"""決算短信・決算説明資料を収集し、ページ単位のテキストを保存する。"""

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import time
import unicodedata

from pypdf import PdfReader
import requests

import disclosure
import portfolio
from ks_util import DATA_DIR, USER_AGENT_CHROME, log_debug, log_error, log_print, log_warning


IR_DOCS_DIR = Path(DATA_DIR) / "ir_docs"
KABUTAN_URL = "https://kabutan.jp/stock/news?code={code_s}&b=disclose&page={page}"
PDF_URL_RE = re.compile(
    r"https?://kabutan\.jp/disclosures/pdf/(\d{8})/(\d+)/?"
)
SETSUMEI_RE = re.compile(r"決算(補足)?説明(会)?資料|決算短信補足")
TANSHIN_RE = re.compile(r"決算短信")
EXCLUDE_RE = re.compile(r"お知らせ|書き起こし|動画|について|開催")
REVISION_RE = re.compile(r"訂正|修正版|再表示|期中レビューの完了")
DEPTH_DAYS = {"latest": None, "1y": 365, "2y": 730}
DEPTH_RANK = {"latest": 0, "1y": 1, "2y": 2}
DEPTH_MONTHS = {"latest": 0, "1y": 12, "2y": 24}
REQUEST_INTERVAL = 1.0


def classify_heading(heading):
    """開示見出しを対象資料種別へ分類する。対象外は None を返す。"""
    normalized = unicodedata.normalize("NFKC", html.unescape(heading or ""))
    normalized = re.sub(r"<[^>]+>", "", normalized)
    if EXCLUDE_RE.search(normalized):
        return None
    # 「決算短信補足資料」は短信ではなく説明資料として扱う。
    if SETSUMEI_RE.search(normalized):
        return "setsumei"
    if TANSHIN_RE.search(normalized):
        return "tanshin"
    return None


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


def collect_candidates(code_s, depth="1y", session=None, limiter=None, now=None):
    """株探の開示一覧を新しいページから辿り、対象資料を返す。"""
    if depth not in DEPTH_DAYS:
        raise ValueError(f"未対応の収集深度です: {depth}")
    session = session or requests.Session()
    limiter = limiter or _RateLimiter()
    now = now or datetime.now()
    cutoff = None
    if DEPTH_DAYS[depth] is not None:
        cutoff = (now.date() - timedelta(days=DEPTH_DAYS[depth])).strftime("%Y%m%d")

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


def _extract_pdf(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(page.extract_text() or "") for page in reader.pages]


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
    """明示的な訂正版だけを同種・同会計期間・同四半期の直前資料へ関連付ける。"""
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
                response = _get(session, candidate["url"], limiter)
                pdf_bytes = response.content
                content_type = response.headers.get("content-type", "").lower()
                if "pdf" not in content_type and not pdf_bytes.startswith(b"%PDF"):
                    raise ValueError(f"PDF以外の応答です: {content_type or 'unknown'}")
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
            document = {
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


def list_ir_docs(code_s, output_dir=None):
    """保存済みIR資料をログへ一覧表示して返す。"""
    root = Path(output_dir) if output_dir else IR_DOCS_DIR
    index = _load_index(root / str(code_s).upper() / "index.json")
    for document in index["documents"]:
        latest = "latest" if document.get("is_latest", True) else "superseded"
        log_print(
            f"{document['date']} {document['doc_type']} {document['text_quality']} "
            f"{latest} {document['heading']}"
        )
    return index["documents"]


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
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "download":
        download_ir_docs(args.code_s, args.depth, args.force, args.dry_run)
    elif args.command == "download_all":
        download_all(args.depth, args.force, args.dry_run)
    else:
        list_ir_docs(args.code_s)


if __name__ == "__main__":
    main()
