#!/usr/bin/env python3
"""
過去決算メモ log → research_shelve.kessan_comments 移行スクリプト (issue #131)

入力: 手書き決算メモのプレーンテキスト (例: data/kessan_comments_log.txt)
出力: research_shelve (kessan_comments フィールドに upsert)

4 層構成 (migrate_research_from_csv.py を踏襲):
    1. 行読込層       (read_log_lines)
    2. トークナイズ層 (tokenize_lines)
    3. エントリ組立層 (build_entries_from_tokens)
    4. 実行層         (migrate_log_to_research_shelve + main)

トークナイザとビルダは純関数 (warnings を戻り値で返す)。
実行層のみが IO (shelve 書込・バックアップ) を持つ。

入力フォーマット (サンプル):

    <2026年>                                        ← 年ヘッダ
    [03/11]                                         ← 日付ヘッダ (MM/DD)
    ☆5032ＡＮＹＣＯＬＯＲ[3Q]                        ← 保有マーク + 全角コード + 名称 + [nQ]
    　←E: -15% 棚卸資産グッズ？評価損計上で         ← 事後行 (先頭全角空白 + ←)
    5031モイ[4Q]                                    ← 見通し・事後なし → スキップ
    ☆5572Ｒｉｄｇｅ－ｉ[2Q]: 衛星画像解析材料性ある  ← ": 見通し"
    　←C: +5% 衛星は大型終了で今後受注
    8142トーホー[4Q],6184鎌倉新書[4Q],...           ← カンマ区切り → スキップ
    ◯9556ＩＮＴＬＯＯＰ[2Q]: かなり安くなってるが    ← ◯(U+25EF) は pre_expectation
    　←E: -18% 人材採用前倒しで過剰に売られる

マッピング:
    ☆                   = 保有マーク (pre_expectation には入れない)
    ◎ / ○ / ◯ / ▲ / △ / ×  = pre_expectation (◯→○ 正規化)
    [nQ]                 = quarter (0〜4)
    ": xxx"              = pre_outlook
    "　←X: ±N% 本文"    = post_price_change="±N" (符号付き文字列、% なし)
                           post_comment="[X] ±N% 本文"
"""

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

# scripts/ を sys.path に追加 (直接実行時)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import research_shelve as rs  # noqa: E402
from research_shelve import _flock  # noqa: E402

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# ===========================================
# 定数・正規表現
# ===========================================

# 期待度マーカー (行頭1文字、☆ は除外)
# ◯ (U+25EF) は ○ (U+25CB) に正規化してから VALID_EXPECTATIONS と比較
PRE_EXPECTATION_CHARS = frozenset({"◎", "○", "◯", "▲", "△", "×"})
HOLDING_MARKER = "☆"

# 決算コメントのスキーマ由来定数 (research_shelve 側の実装と整合)
MAX_KESSAN_COMMENTS = rs.MAX_KESSAN_COMMENTS
VALID_EXPECTATIONS = rs.VALID_EXPECTATIONS

# 年ヘッダ: "<2026年>"
YEAR_HEADER = re.compile(r"^<\s*(\d{4})\s*年\s*>$")

# 日付ヘッダ: "[03/11]"
DATE_HEADER = re.compile(r"^\[(\d{1,2})/(\d{1,2})\]$")

# 銘柄行: [先頭 pre_expectation 記号?][☆?][コード][名称][/Q表記][: 見通し?]
# quarter は 0-4 (0=通期相当、webapp/helpers.py:476 の既存スキーマと整合)
STOCK_HEAD = re.compile(
    r"^([◎○◯▲△×])?(☆)?(\d{3}[A-Z]|\d{4})(.*?)\[([0-4])Q\](.*)$"
)

# コード単体検出 (複数銘柄行の判定用)
CODE_PATTERN = re.compile(r"(\d{3}[A-Z]|\d{4})")

# 事後行: "　←X: ±N% 本文" (先頭は全角空白または任意空白)
POST_LINE = re.compile(
    r"^[\u3000\s]+←\s*([A-Za-z])\s*:\s*"
    r"([+\-]?\d+(?:\.\d+)?)\s*%\s*(.*)$"
)

# post_price_change バリデーション
POST_PRICE_CHANGE_RE = re.compile(r"^[+\-]?\d+(?:\.\d+)?$")


# ===========================================
# 1. 行読込層
# ===========================================

def read_log_lines(log_path: str) -> List[str]:
    """ログファイルを読み込み、行末 CR/LF を除去した文字列リストを返す。

    - CRLF / LF / 混在を許容 (rstrip("\\r\\n"))
    - 先頭の全角空白 (U+3000) は保持する (事後行の判定で必要)
    - 空行は保持する (トークン列の区切りとして `BlankToken` を出す)
    """
    with open(log_path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    # splitlines() は \u2028 等も区切りにしてしまうため、\r\n のみで分割
    lines = raw.split("\n")
    # 最後が "\n" で終わっている場合、split すると末尾に空文字が付く → 除去
    if lines and lines[-1] == "":
        lines.pop()
    return [line.rstrip("\r") for line in lines]


# ===========================================
# 2. トークナイズ層
# ===========================================

@dataclass
class YearToken:
    year: int
    line_no: int


@dataclass
class DateToken:
    month: int
    day: int
    line_no: int


@dataclass
class StockToken:
    pre_expectation: str   # "" または VALID_EXPECTATIONS の一要素
    code_s: str            # 半角化・大文字化済み ("5032" / "215A")
    quarter: int           # 0〜4
    pre_outlook: str       # ": xxx" 部分。なければ ""
    line_no: int


@dataclass
class PostToken:
    rating_letter: str     # "E" / "C" / ...
    price_change: str      # "-15" / "+5" / "-12.3"
    comment_body: str      # 本文
    line_no: int


@dataclass
class MultiStockToken:
    """カンマ区切り複数銘柄行。ビルダで常にスキップされる。"""
    line_no: int


@dataclass
class BlankToken:
    line_no: int


@dataclass
class UnknownToken:
    raw: str
    line_no: int


Token = Union[
    YearToken, DateToken, StockToken, PostToken,
    MultiStockToken, BlankToken, UnknownToken,
]


def _normalize_expectation(ch: Optional[str]) -> str:
    """期待度マーカーを正規化する (◯ → ○)。マーカーなしは ""。"""
    if not ch:
        return ""
    if ch == "◯":  # U+25EF
        return "○"  # U+25CB
    return ch


def tokenize_lines(lines: List[str]) -> Tuple[List[Token], List[Dict[str, Any]]]:
    """行リストをトークン列に変換する純関数。

    Returns:
        (tokens, warnings)
        warnings は {"line_no": int, "message": str} の list。
    """
    tokens: List[Token] = []
    warnings: List[Dict[str, Any]] = []

    for idx, raw in enumerate(lines, start=1):
        # 先頭の全角空白は POST_LINE の検出に必要なので NFKC する前に判定する
        if POST_LINE.match(raw):
            m = POST_LINE.match(raw)
            tokens.append(PostToken(
                rating_letter=m.group(1),
                price_change=m.group(2),
                comment_body=m.group(3).strip(),
                line_no=idx,
            ))
            continue

        # 空行 (全角空白のみ含む行は post 扱いで既に処理済み)
        if raw.strip() == "":
            tokens.append(BlankToken(line_no=idx))
            continue

        # NFKC で全角コード・名称を半角化
        normalized = unicodedata.normalize("NFKC", raw).strip()

        # 年ヘッダ
        m_year = YEAR_HEADER.match(normalized)
        if m_year:
            tokens.append(YearToken(year=int(m_year.group(1)), line_no=idx))
            continue

        # 日付ヘッダ
        m_date = DATE_HEADER.match(normalized)
        if m_date:
            tokens.append(DateToken(
                month=int(m_date.group(1)),
                day=int(m_date.group(2)),
                line_no=idx,
            ))
            continue

        # 複数銘柄行判定: カンマ + コードが 2 つ以上
        if "," in normalized and len(CODE_PATTERN.findall(normalized)) >= 2:
            tokens.append(MultiStockToken(line_no=idx))
            continue

        # 銘柄行 (◯→○ 正規化は STOCK_HEAD の char class でどちらも受けるが、
        #          pre_expectation 出力時に正規化する)
        m_stock = STOCK_HEAD.match(normalized)
        if m_stock:
            pre_exp_raw = m_stock.group(1)
            pre_exp = _normalize_expectation(pre_exp_raw)
            code_s = m_stock.group(3).upper()
            quarter = int(m_stock.group(5))
            tail = m_stock.group(6)  # "[nQ]" 以降の残り
            # 先頭の ": " または ":" があれば pre_outlook、なければ ""
            pre_outlook = ""
            tail_stripped = tail.strip()
            if tail_stripped.startswith(":"):
                pre_outlook = tail_stripped[1:].strip()
            elif tail_stripped:
                # コロンなしで続きがある場合は warning として無視
                warnings.append({
                    "line_no": idx,
                    "message": f"銘柄行の末尾形式不明 (': xxx' を期待): {tail_stripped!r}",
                })
            tokens.append(StockToken(
                pre_expectation=pre_exp,
                code_s=code_s,
                quarter=quarter,
                pre_outlook=pre_outlook,
                line_no=idx,
            ))
            continue

        # 未知行
        tokens.append(UnknownToken(raw=raw, line_no=idx))
        warnings.append({
            "line_no": idx,
            "message": f"未知の行形式: {raw!r}",
        })

    return tokens, warnings


# ===========================================
# 3. エントリ組立層
# ===========================================

@dataclass
class ParsedEntry:
    code_s: str
    kessanbi: str          # "YYYY/MM/DD"
    quarter: int
    pre_expectation: str
    pre_outlook: str
    post_price_change: str  # 符号付き文字列、% なし。post なしは ""
    post_comment: str       # post なしは ""
    source_line_no: int = 0  # 診断用


def _format_post_comment(rating_letter: str, price_change: str, body: str) -> str:
    """post_comment を '[X] ±N% 本文' 形式で組み立てる。"""
    # price_change に既に符号がない場合は正数として扱う
    if price_change and not price_change.startswith(("+", "-")):
        price_change = "+" + price_change
    return f"[{rating_letter}] {price_change}% {body}".rstrip()


def build_entries_from_tokens(
    tokens: List[Token],
    *,
    default_year: Optional[int] = None,
) -> Tuple[List[ParsedEntry], List[Dict[str, Any]]]:
    """トークン列から ParsedEntry のリストを組み立てる純関数。

    状態機械:
        - current_year: <YYYY年> を見るたび更新
        - current_date: [MM/DD] を見るたび更新
        - pending_stock: StockToken を保留 (直後が PostToken ならアタッチ)

    スキップ規則:
        - MultiStockToken: 常にスキップ
        - pre_outlook=="" かつ post なしの StockToken: スキップ

    Returns:
        (entries, warnings)
    """
    entries: List[ParsedEntry] = []
    warnings: List[Dict[str, Any]] = []

    current_year: Optional[int] = default_year
    current_month: Optional[int] = None
    current_day: Optional[int] = None
    pending: Optional[StockToken] = None
    pending_post: Optional[PostToken] = None

    def flush_pending():
        """保留中の StockToken を (post があれば attach して) entries に積む。"""
        nonlocal pending, pending_post
        if pending is None:
            return
        # コンテキスト不足チェック
        if current_year is None:
            warnings.append({
                "line_no": pending.line_no,
                "message": f"年ヘッダ未設定のため {pending.code_s} をスキップ",
            })
            pending = None
            pending_post = None
            return
        if current_month is None or current_day is None:
            warnings.append({
                "line_no": pending.line_no,
                "message": f"日付ヘッダ未設定のため {pending.code_s} をスキップ",
            })
            pending = None
            pending_post = None
            return

        # スキップ判定: 見通しなし & post なし
        if pending.pre_outlook == "" and pending_post is None:
            pending = None
            pending_post = None
            return

        # エントリ組立
        kessanbi = f"{current_year:04d}/{current_month:02d}/{current_day:02d}"
        if pending_post is not None:
            post_price_change = pending_post.price_change
            post_comment = _format_post_comment(
                pending_post.rating_letter,
                pending_post.price_change,
                pending_post.comment_body,
            )
        else:
            post_price_change = ""
            post_comment = ""

        entries.append(ParsedEntry(
            code_s=pending.code_s,
            kessanbi=kessanbi,
            quarter=pending.quarter,
            pre_expectation=pending.pre_expectation,
            pre_outlook=pending.pre_outlook,
            post_price_change=post_price_change,
            post_comment=post_comment,
            source_line_no=pending.line_no,
        ))
        pending = None
        pending_post = None

    for tok in tokens:
        if isinstance(tok, YearToken):
            flush_pending()
            current_year = tok.year
        elif isinstance(tok, DateToken):
            flush_pending()
            current_month = tok.month
            current_day = tok.day
        elif isinstance(tok, StockToken):
            flush_pending()
            pending = tok
        elif isinstance(tok, PostToken):
            if pending is None:
                warnings.append({
                    "line_no": tok.line_no,
                    "message": "孤立した post 行 (直前に銘柄行なし): "
                               f"[{tok.rating_letter}] {tok.price_change}% ...",
                })
            else:
                pending_post = tok
                # post は 1 銘柄に 1 回のみ attach、即 flush する
                flush_pending()
        elif isinstance(tok, MultiStockToken):
            # flush はしない (カンマ区切り行は context を消費しない)
            continue
        elif isinstance(tok, BlankToken):
            # 空行: flush せず、pending を維持 (post 行が空行を挟む可能性に備える)
            continue
        elif isinstance(tok, UnknownToken):
            # 既に tokenize で warning 済み。flush して保留を手放す
            flush_pending()
        else:
            continue

    # EOF で残った pending を flush
    flush_pending()

    return entries, warnings


# ===========================================
# 4. 実行層
# ===========================================

def _validate_entry(entry: ParsedEntry) -> None:
    """エントリのハード検証。不正があれば ValueError を投げる。"""
    rs.validate_code_s(entry.code_s)
    if not (0 <= entry.quarter <= 4):
        raise ValueError(f"quarter は 0-4: got {entry.quarter}")
    if entry.pre_expectation not in VALID_EXPECTATIONS:
        raise ValueError(f"pre_expectation 不正: {entry.pre_expectation!r}")
    # kessanbi は YYYY/MM/DD として strptime で検証
    from datetime import datetime
    try:
        datetime.strptime(entry.kessanbi, "%Y/%m/%d")
    except ValueError:
        raise ValueError(f"kessanbi は YYYY/MM/DD 形式: got {entry.kessanbi!r}")
    # post_price_change は "" または符号付き数値文字列
    if entry.post_price_change and not POST_PRICE_CHANGE_RE.match(entry.post_price_change):
        raise ValueError(f"post_price_change 不正: {entry.post_price_change!r}")


def _upsert_kessan_comment_local(
    entry: ParsedEntry,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """entry を research_shelve に upsert する (本スクリプト専用のローカル実装)。

    排他制御: `_flock(db_path)` で read-modify-write 全体を囲む。
    webapp 側の `save_kessan_comment` (helpers.py:511) と同じロックを共有するため、
    並行書込しても後勝ち上書きは発生しない。

    未登録レコード時の挙動:
      - 本番 DB (db_path=None): webapp.helpers.add_stock() を呼び、
        stocks_shelve の情報からレコード + 初期スナップショットを生成する。
        (webapp の save_kessan_comment と同じ挙動)
      - カスタム DB (db_path 指定): add_stock は db_path を受けず本番側を
        汚染してしまうため呼ばない。代わりに create_research_record() で
        最小レコード (stock_name="") を作って当該 db_path に登録する。
        検証 (--db-path /tmp/...) やテスト時に本番 DB が影響を受けない。

    Returns:
        保存したエントリ dict。
    """
    normalized = rs.normalize_code_s(entry.code_s)

    saved_entry: Dict[str, Any] = {
        "kessanbi": entry.kessanbi,
        "quarter": entry.quarter,
        "pre_expectation": entry.pre_expectation,
        "pre_outlook": entry.pre_outlook,
        "post_price_change": entry.post_price_change,
        "post_comment": entry.post_comment,
    }

    with _flock(db_path):
        record = rs.get_research_record(normalized, db_path=db_path)
        if record is None:
            if db_path is None:
                # 本番 DB: webapp の save_kessan_comment と同じパターン
                # add_stock は内部で _flock() を取るがリエントラントなので OK
                from webapp.helpers import add_stock
                add_stock(normalized)
            else:
                # カスタム DB: add_stock は本番DBを汚染するため呼ばない。
                # 最小レコードを当該 db_path に直接登録する。
                minimal = rs.create_research_record(normalized, "")
                rs.upsert_research_record(minimal, db_path=db_path)
            record = rs.get_research_record(normalized, db_path=db_path)
            if record is None:
                raise ValueError(f"レコード登録失敗: {normalized}")

        comments: List[Dict[str, Any]] = list(record.get("kessan_comments") or [])
        # (kessanbi, quarter) で重複判定
        target_idx = None
        for i, existing in enumerate(comments):
            if (
                existing.get("kessanbi") == entry.kessanbi
                and int(existing.get("quarter", 0) or 0) == entry.quarter
            ):
                target_idx = i
                break

        if target_idx is not None:
            comments[target_idx] = saved_entry
        else:
            comments.append(saved_entry)

        # 昇順ソート + 12 件超の最古削除
        comments = _sort_kessan_comments(comments)
        if len(comments) > MAX_KESSAN_COMMENTS:
            comments = comments[-MAX_KESSAN_COMMENTS:]

        record["kessan_comments"] = comments
        rs.upsert_research_record(record, db_path=db_path)

    return saved_entry


def _sort_kessan_comments(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """kessan_comments を kessanbi 昇順に安定ソート。"""
    from datetime import date, datetime

    def _key(entry):
        k = entry.get("kessanbi", "")
        try:
            return datetime.strptime(k, "%Y/%m/%d").date()
        except ValueError:
            return date.min
    return sorted(comments, key=_key)


def migrate_log_to_research_shelve(
    log_path: str,
    *,
    db_path: Optional[str] = None,
    dry_run: bool = False,
    default_year: Optional[int] = None,
    show_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """ログファイル全体を研究DBに移行する。

    手順:
        1. read_log_lines → tokenize_lines → build_entries_from_tokens
        2. dry_run=False ならバックアップ
        3. 各エントリを _upsert_kessan_comment_local で upsert
           - 失敗時は log_warning + failed_entries に追加、継続
        4. show_codes が指定されたら format_record_full で stdout に出力
        5. サマリ dict を返す
    """
    log_print(f"[migrate_kessan] 読み込み: {log_path}")
    lines = read_log_lines(log_path)
    tokens, tokenize_warnings = tokenize_lines(lines)
    entries, build_warnings = build_entries_from_tokens(
        tokens, default_year=default_year,
    )
    parse_warnings: List[Dict[str, Any]] = tokenize_warnings + build_warnings

    total = len(entries)
    log_print(f"[migrate_kessan] 有効エントリ: {total}")

    # バックアップ
    backup_paths: List[str] = []
    if not dry_run:
        try:
            backup_paths = rs.backup_research_db(db_path=db_path)
            if backup_paths:
                log_print(f"[migrate_kessan] バックアップ(実行前): {backup_paths}")
            else:
                log_print("[migrate_kessan] バックアップ(実行前): 既存 DB なし、スキップ")
        except Exception as e:
            log_warning(f"[migrate_kessan] バックアップ失敗(継続): {e}")

    if dry_run:
        log_print("[migrate_kessan] dry_run=True: DB に書き込まず検証のみ実行")

    # 実行
    failed_entries: List[Dict[str, Any]] = []
    succeeded = 0

    log_print("[migrate_kessan] 移行開始...")
    for idx, entry in enumerate(entries, start=1):
        try:
            _validate_entry(entry)
            if not dry_run:
                _upsert_kessan_comment_local(entry, db_path=db_path)
            succeeded += 1
        except (ValueError, TypeError) as e:
            log_warning(
                f"[migrate_kessan] エントリ失敗 "
                f"(line {entry.source_line_no}, code={entry.code_s!r}, "
                f"kessanbi={entry.kessanbi!r}, quarter={entry.quarter}): {e}"
            )
            failed_entries.append({
                "line_no": entry.source_line_no,
                "code_s": entry.code_s,
                "kessanbi": entry.kessanbi,
                "quarter": entry.quarter,
                "error": f"{type(e).__name__}: {e}",
            })

    log_print(
        f"[migrate_kessan] 完了: 成功 {succeeded} 件、失敗 {len(failed_entries)} 件"
        f" (パース warning {len(parse_warnings)} 件)"
    )

    if failed_entries:
        log_print("[migrate_kessan] 失敗エントリ一覧:")
        for fe in failed_entries:
            log_print(
                f"  - line {fe['line_no']} {fe['code_s']} "
                f"{fe['kessanbi']} Q{fe['quarter']}: {fe['error']}"
            )

    if parse_warnings and len(parse_warnings) <= 30:
        log_print("[migrate_kessan] パース warning 一覧:")
        for w in parse_warnings:
            log_print(f"  - line {w['line_no']}: {w['message']}")
    elif parse_warnings:
        log_print(
            f"[migrate_kessan] パース warning が {len(parse_warnings)} 件あります"
            "(30 件超のため個別表示は省略)"
        )

    # --show 対応
    if show_codes:
        for code in show_codes:
            code = code.strip()
            if not code:
                continue
            log_print(f"[migrate_kessan] --show {code} ----------------------------")
            try:
                record = rs.get_research_record(code, db_path=db_path)
            except ValueError as e:
                log_warning(f"[migrate_kessan] --show: 不正コード {code!r}: {e}")
                continue
            if record is None:
                log_warning(f"[migrate_kessan] --show: レコード未登録: {code}")
                continue
            print(rs.format_record_full(record))

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": len(failed_entries),
        "failed_entries": failed_entries,
        "parse_warnings": parse_warnings,
        "dry_run": dry_run,
        "backup_paths": backup_paths,
    }


# ===========================================
# CLI エントリポイント
# ===========================================

def main() -> int:
    """CLI エントリポイント。

    usage: python migrate_kessan_comments_from_log.py <log_path>
                   [--dry-run] [--db-path PATH] [--show CODE[,CODE...]]
                   [--year YYYY]
    """
    parser = argparse.ArgumentParser(
        description="過去決算メモ log を research_shelve.kessan_comments に移行する"
    )
    parser.add_argument(
        "log_path",
        help="入力ログファイルのパス (例: data/kessan_comments_log.txt)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB に書き込まずパース結果のみ検証 (バックアップも取らない)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="書き込み先 DB パス (デフォルト: 本番 RESEARCH_SHELVE)",
    )
    parser.add_argument(
        "--show",
        default=None,
        help=(
            "検証用に代表銘柄の format_record_full 出力を stdout に流す。"
            "カンマ区切りで複数指定可 (例: --show 5032,5572,9556)"
        ),
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="<YYYY年> ヘッダが先頭に無い場合の default 年 (edge case)",
    )
    args = parser.parse_args()

    show_codes: Optional[List[str]] = None
    if args.show:
        show_codes = [c.strip() for c in args.show.split(",") if c.strip()]

    summary = migrate_log_to_research_shelve(
        args.log_path,
        db_path=args.db_path,
        dry_run=args.dry_run,
        default_year=args.year,
        show_codes=show_codes,
    )

    return 1 if summary["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
