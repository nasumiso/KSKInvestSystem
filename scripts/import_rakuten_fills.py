#!/usr/bin/env python3
"""楽天証券 取引履歴CSV → portfolio_shelve fill レイヤー 取込 (issue #360 Phase2)。

二層モデル (GitHub コメント 2026-07-11 確定):
- fill レイヤー (本スクリプトが書く): CSV 由来の約定事実。価格・株数・約定日の真実源。
- 判断レイヤー (既存 action_log): ステータス・タグ・メモ。CSV では得られない情報。維持。

本スクリプトのスコープ (issue #360 Phase2 (a)+(b)):
- (a) fill 取込 + dedup 保存 (冪等)
- (b) --match 指定時、エピソードへ自動マッチして matched_seq を書き戻す

4 層構成 (migrate_portfolio_from_csv.py のパターン踏襲):
    1. CSV 読込層 (read_csv_rows)
    2. 行パース層 (parse_fill_row)
    3. 統合層 (import_csv_to_fills)
    4. 実行層 (main + argparse)

入力: 楽天 取引履歴CSV `tradehistory(JP)_YYYYMMDD.csv` (Shift-JIS, 28列, ヘッダあり)。
"""

import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# scripts/ を sys.path に追加 (直接実行時)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# ===========================================
# 定数 (楽天 取引履歴CSV フォーマット)
# ===========================================

CSV_ENCODING = "shift_jis"
EXPECTED_COL_COUNT = 28

# 列インデックス (0-indexed、確認済みフォーマット)
COL_TRADE_DATE = 0     # 約定日 (YYYY/M/D)
COL_CODE_S = 2         # 銘柄コード
COL_TRADE_KIND = 6     # 取引区分 (現物 / 信用新規 / 信用返済 / 現物(単元未満))
COL_BAIBAI = 7         # 売買区分 (買付 / 売付 / 買建 / 売埋)
COL_QTY = 10           # 数量[株]
COL_PRICE = 11         # 単価[円]
COL_AMOUNT = 16        # 受渡金額[円]

HEADER_FIRST_COL = "約定日"  # ヘッダ検証用


# ===========================================
# 1. CSV 読込層
# ===========================================

def read_csv_rows(csv_path: str) -> List[List[str]]:
    """楽天 取引履歴CSV を Shift-JIS で読み、ヘッダを除いたデータ行を返す。

    先頭行が既知ヘッダ (約定日...) でなければ ValueError。
    """
    with open(csv_path, "r", encoding=CSV_ENCODING, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"CSV が空です: {csv_path}")
    header = rows[0]
    if not header or header[0].strip() != HEADER_FIRST_COL:
        raise ValueError(
            f"想定外のヘッダです (先頭列={header[0]!r}, 期待={HEADER_FIRST_COL!r}): {csv_path}"
        )
    return rows[1:]


# ===========================================
# 2. 行パース層
# ===========================================

def _parse_num(raw: str) -> Optional[float]:
    """カンマ区切り数値文字列を float に。空欄/"-" は None。"""
    s = (raw or "").strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_trade_date(raw: str) -> Optional[str]:
    """約定日 "YYYY/M/D" を "YYYY-MM-DD" に正規化。パース不可なら None。"""
    s = (raw or "").strip()
    try:
        d = datetime.strptime(s, "%Y/%m/%d").date()
    except ValueError:
        return None
    return d.isoformat()


class RowSkip(Exception):
    """パース対象外の行 (無効コード・未知区分・数量欠落など)。理由を保持する。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_fill_row(row: List[str]) -> Dict[str, Any]:
    """CSV 1 行を fill 構築用の中間 dict に変換する。

    現物・信用の両方に対応。対象外行は RowSkip を投げる (呼び出し側でカウント)。
    dedup_key の occurrence は統合層で付与するため、ここでは素材のみ返す。
    """
    if len(row) < EXPECTED_COL_COUNT:
        raise RowSkip(f"列数不足 ({len(row)})")

    code_raw = (row[COL_CODE_S] or "").strip()
    try:
        ps.validate_code_s(code_raw)
    except (ValueError, TypeError):
        raise RowSkip(f"無効な銘柄コード: {code_raw!r}")
    code_s = ps.normalize_code_s(code_raw)

    trade_date = _normalize_trade_date(row[COL_TRADE_DATE])
    if trade_date is None:
        raise RowSkip(f"約定日パース不可: {row[COL_TRADE_DATE]!r}")

    baibai = (row[COL_BAIBAI] or "").strip()
    try:
        side = ps.normalize_side(baibai)
    except ValueError as e:
        raise RowSkip(str(e))

    qty_f = _parse_num(row[COL_QTY])
    price_f = _parse_num(row[COL_PRICE])
    if qty_f is None or qty_f <= 0:
        raise RowSkip(f"数量欠落/不正: {row[COL_QTY]!r}")
    if price_f is None or price_f <= 0:
        raise RowSkip(f"単価欠落/不正: {row[COL_PRICE]!r}")
    amount_f = _parse_num(row[COL_AMOUNT])

    trade_kind = (row[COL_TRADE_KIND] or "").strip()

    return {
        "code_s": code_s,
        "trade_date": trade_date,
        "side": side,
        "qty": int(qty_f),
        "price": price_f,
        "amount": int(amount_f) if amount_f is not None else 0,
        "trade_kind": trade_kind,
        "baibai": baibai,
    }


# ===========================================
# 3. 統合層
# ===========================================

def import_csv_to_fills(
    csv_path: str,
    *,
    dry_run: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """CSV を読み、各行を fill として冪等取込する。

    同一CSV内の同一 dedup 素材の出現順 (occurrence) を数えて dedup_key に含める
    (同日同単価の別注文を別 fill として扱いつつ、再取込は冪等)。

    Returns: {"rows", "imported", "skipped_dup", "skipped_invalid"}
    """
    rows = read_csv_rows(csv_path)
    stats = {"rows": len(rows), "imported": 0, "skipped_dup": 0, "skipped_invalid": 0}
    occurrence_counter: Dict[str, int] = {}
    samples: List[Dict[str, Any]] = []

    for row in rows:
        try:
            parsed = parse_fill_row(row)
        except RowSkip as e:
            stats["skipped_invalid"] += 1
            log_print("import_rakuten_fills: スキップ", e.reason)
            continue

        # occurrence: 同一CSV内で dedup 素材が同一な行の出現順
        occ_key = "|".join(
            [
                parsed["trade_date"],
                parsed["code_s"],
                parsed["trade_kind"],
                parsed["baibai"],
                str(parsed["qty"]),
                f"{parsed['price']:.4f}",
                str(parsed["amount"]),
            ]
        )
        occurrence = occurrence_counter.get(occ_key, 0)
        occurrence_counter[occ_key] = occurrence + 1

        dedup_key = ps.make_dedup_key(
            trade_date=parsed["trade_date"],
            code_s=parsed["code_s"],
            trade_kind=parsed["trade_kind"],
            baibai_kubun=parsed["baibai"],
            qty=parsed["qty"],
            price=parsed["price"],
            amount=parsed["amount"],
            occurrence=occurrence,
        )
        fill = ps.create_fill(
            parsed["code_s"],
            trade_date=parsed["trade_date"],
            side=parsed["side"],
            qty=parsed["qty"],
            price=parsed["price"],
            amount=parsed["amount"],
            trade_kind=parsed["trade_kind"],
            dedup_key=dedup_key,
        )

        if dry_run:
            if len(samples) < 3:
                samples.append(fill)
            stats["imported"] += 1
            continue

        _, is_new = ps.append_fill(fill, db_path=db_path)
        if is_new:
            stats["imported"] += 1
        else:
            stats["skipped_dup"] += 1

    if dry_run and samples:
        log_print("import_rakuten_fills: dry-run サンプル (先頭 3 件):")
        for s in samples:
            log_print(
                f"  {s['code_s']} {s['trade_date']} {s['side']} "
                f"qty={s['qty']} price={s['price']} kind={s['trade_kind']}"
            )
    return stats


# ===========================================
# 3b. (b) 自動マッチング — fill → action_log エピソード
# ===========================================

MATCH_WINDOW_DAYS = 3  # 約定日 ±3 暦日 (±5 は隣接エピソードを跨ぎやすいため縮小)


def _build_episodes(logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """action_log 群を trade_history.py と同じ走査でエピソード単位に再構築する。

    各エピソード: {code_s, hold_seq, hold_date, sell_seq, sell_date, has_qty_changes}。
    sell_seq/sell_date は未売却なら None。fill を「イベント」でなく「エピソードの
    hold/sell スロット」へ割り当てるための土台。
    """
    episodes: List[Dict[str, Any]] = []
    open_ep: Dict[str, Dict[str, Any]] = {}
    for log in logs:
        code_s = log["code_s"]
        if log.get("status_to") == "1保":
            if code_s in open_ep:
                episodes.append(open_ep.pop(code_s))  # 未クローズ再購入 (異常系) を先に確定
            open_ep[code_s] = {
                "code_s": code_s,
                "hold_seq": log["seq"],
                "hold_date": log["timestamp"][:10],
                "sell_seq": None,
                "sell_date": None,
                "has_qty_changes": False,
            }
        elif log.get("action_type") == "株数変更" and code_s in open_ep:
            open_ep[code_s]["has_qty_changes"] = True
        elif log.get("action_type") == "売却" and code_s in open_ep:
            ep = open_ep.pop(code_s)
            ep["sell_seq"] = log["seq"]
            ep["sell_date"] = log["timestamp"][:10]
            episodes.append(ep)
    episodes.extend(open_ep.values())
    return episodes


def _days_between(a: str, b: str) -> Optional[int]:
    try:
        return (date.fromisoformat(a) - date.fromisoformat(b)).days
    except (ValueError, TypeError):
        return None


def _candidate_slots(
    fill: Dict[str, Any],
    episodes: List[Dict[str, Any]],
    consumed: set,
) -> List[Tuple[int, int]]:
    """fill が区間整合する (距離, スロットの seq) 候補を返す (単一 IN のみ、未消費のみ)。

    区間整合 (codexレビュー指摘):
    - buy fill → hold スロット: |約定日 - hold_date| <= W (端点窓)。
      かつ 約定日 <= sell_date (このエピソードの sell を跨がない)。
      かつ 約定日 < 直後エピソードの hold_date (次サイクルに食い込まない)。
    - sell fill → sell スロット: |約定日 - sell_date| <= W (端点窓)。
      かつ 約定日 >= hold_date (このエピソードの hold より前でない)。
      かつ 約定日 > 直前エピソードの sell_date (前サイクルに食い込まない)。
    距離 = 約定日とスロット端点 (hold_date / sell_date) の暦日差の絶対値。
    """
    fd = fill["trade_date"]
    code_s = fill["code_s"]
    W = MATCH_WINDOW_DAYS
    # 同一コードのエピソードを hold_date 昇順に (前後境界の判定に使う)
    same = sorted(
        (e for e in episodes if e["code_s"] == code_s and not e["has_qty_changes"]),
        key=lambda e: e["hold_date"],
    )
    slots: List[Tuple[int, int]] = []
    for i, ep in enumerate(same):
        if fill["side"] == ps.SIDE_BUY:
            seq = ep["hold_seq"]
            if (code_s, seq) in consumed:
                continue
            lo = _days_between(fd, ep["hold_date"])  # 約定日 - hold
            if lo is None or abs(lo) > W:
                continue  # hold から W 日を超えるものは対象外 (端点窓)
            # このエピソードの sell_date (あれば) を跨がない
            if ep["sell_date"] is not None:
                hi = _days_between(fd, ep["sell_date"])
                if hi is not None and hi > 0:
                    continue  # sell を跨いだ買いは別サイクル
            # 直後エピソードの hold_date に食い込まない
            if i + 1 < len(same):
                nxt = _days_between(fd, same[i + 1]["hold_date"])
                if nxt is not None and nxt >= 0:
                    continue
            slots.append((abs(lo), seq))
        else:  # sell
            if ep["sell_date"] is None:
                continue
            seq = ep["sell_seq"]
            if (code_s, seq) in consumed:
                continue
            hi = _days_between(fd, ep["sell_date"])  # 約定日 - sell
            if hi is None or abs(hi) > W:
                continue  # sell から W 日を超えるものは対象外 (端点窓)
            # このエピソードの hold_date より前の売りは無効
            lo = _days_between(fd, ep["hold_date"])
            if lo is not None and lo < 0:
                continue
            # 直前エピソードの sell_date に食い込まない
            if i - 1 >= 0 and same[i - 1]["sell_date"] is not None:
                prev = _days_between(fd, same[i - 1]["sell_date"])
                if prev is not None and prev <= 0:
                    continue
            slots.append((abs(hi), seq))
    return slots


def match_fills_to_episodes(*, db_path: Optional[str] = None) -> Dict[str, int]:
    """未マッチ fill をエピソードの hold/sell スロットへ突合し matched_seq を書き戻す。

    安全側の設計 (曖昧さゼロのケースだけ自動反映):
    - 楽天CSVは注文番号を持たないため、同 (code_s, side, 約定日) の fill が 2 件以上ある
      コードは「同一注文の分割」か「別イベント」か区別できない。よってその (code_s, side)
      は丸ごと自動マッチ対象外 (全 fill 未マッチのまま)。
    - 残った 1 fill = 1 イベントを、エピソード区間で候補を固定した上で最近接一意マッチ。
      候補スロットのうち距離最小が一意 (2位と厳密に差) かつ未消費のときだけ確定。
    - 1 スロット (エピソードの hold or sell) は高々 1 fill (二重消費防止)。

    Returns: {"matched", "unmatched", "ambiguous"}
    """
    all_fills = ps.list_fills(db_path=db_path)
    episodes = _build_episodes(ps.list_action_logs(db_path=db_path))

    # (code_s, side, trade_date) ごとの件数 → 2 件以上は曖昧として除外
    group_count: Dict[Tuple[str, str, str], int] = {}
    for f in all_fills:
        key = (f["code_s"], f["side"], f["trade_date"])
        group_count[key] = group_count.get(key, 0) + 1

    stats = {"matched": 0, "unmatched": 0, "ambiguous": 0}
    # (code_s, slot_seq) 消費済みスロット。増分取込に備え、既にマッチ済みの fill が
    # 占有するスロットを先に投入する (別CSVの後日 fill が同一スロットへ再マッチするのを防ぐ)
    consumed: set = {
        (f["code_s"], f["matched_seq"])
        for f in all_fills
        if f.get("matched_seq") is not None
    }

    # 約定日昇順で処理 (早い約定を優先確定)
    for f in sorted(all_fills, key=lambda x: (x["code_s"], x["trade_date"], x["seq"])):
        if f.get("matched_seq") is not None:
            continue  # 既マッチは触らない (冪等)
        key = (f["code_s"], f["side"], f["trade_date"])
        if group_count.get(key, 0) >= 2:
            stats["ambiguous"] += 1
            log_print(
                "import_rakuten_fills: 曖昧 (同日同side複数) 未マッチ",
                f["code_s"], f["side"], f["trade_date"],
            )
            continue

        slots = _candidate_slots(f, episodes, consumed)
        slots.sort(key=lambda x: x[0])
        # 距離最小が一意 (2位と厳密に差がある) のときだけ確定
        if slots and (len(slots) == 1 or slots[0][0] < slots[1][0]):
            _, seq = slots[0]
            ps.set_fill_matched_seq(f["code_s"], f["seq"], seq, db_path=db_path)
            consumed.add((f["code_s"], seq))
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1
            log_print(
                "import_rakuten_fills: 未マッチ (候補 %d 件)" % len(slots),
                f["code_s"], f["side"], f["trade_date"],
            )
    return stats


# ===========================================
# 4. 実行層
# ===========================================

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="楽天 取引履歴CSV を fill レイヤーへ取込 (issue #360 Phase2)",
    )
    parser.add_argument("csv_path", help="楽天 取引履歴CSV のパス (Shift-JIS)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="読込・パースまで行うが DB へは書かない",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="portfolio_shelve のパス上書き (検証用)",
    )
    parser.add_argument(
        "--match",
        action="store_true",
        help="取込後にエピソードへの自動マッチを実行する",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not os.path.exists(args.csv_path):
        log_warning(f"CSV が見つかりません: {args.csv_path}")
        return 1

    stats = import_csv_to_fills(
        args.csv_path,
        dry_run=args.dry_run,
        db_path=args.db_path,
    )
    log_print(
        "import_rakuten_fills: 取込完了",
        f"rows={stats['rows']}",
        f"imported={stats['imported']}",
        f"skipped_dup={stats['skipped_dup']}",
        f"skipped_invalid={stats['skipped_invalid']}",
        "(dry-run)" if args.dry_run else "",
    )

    if args.match and not args.dry_run:
        match_stats = match_fills_to_episodes(db_path=args.db_path)
        log_print(
            "import_rakuten_fills: マッチ完了",
            f"matched={match_stats['matched']}",
            f"unmatched={match_stats['unmatched']}",
            f"ambiguous={match_stats['ambiguous']}",
        )
    elif args.match and args.dry_run:
        log_warning("--match は --dry-run と併用できません (書き込みが必要)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
