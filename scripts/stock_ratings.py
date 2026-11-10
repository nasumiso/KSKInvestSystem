"""銘柄評価台帳 (ChatGPT で採点している投資判断スナップショット) の正本。

正本は KS_DATA_DIR/stock_ratings/ の JSON 1ファイル、変更履歴は JSONL (追記のみ)。
書き込みはこのモジュール経由のみ (flock で排他し、一時ファイル → os.replace で置き換える)。

ChatGPT が Drive コネクタから読めるよう、KS_DATA_DIR/stock_ratings_drive (運用機にだけ置く
Drive フォルダへの symlink) があれば、書き込みのたびと日次バッチでそこへ上書きコピーする。
Drive に出す JSON にだけ、システムが出す2軸 (Valuation とモメンタム、system) を埋め込む。

採点ルール Ver3 (#544): 総合点 = ファンダ 40 + 未織込 20 (AI が付ける) + Valuation 20 + モメンタム 20
(システムが基準日の終値から毎日出す)。AI は株価で変わらないものを判断し、株価で変わるものは
システムが出す。Valuation は AI が付けた適正株価 (fair_price) と基準日の終値から換算する。
Drive 上で os.replace するとファイル ID が変わり ChatGPT の参照が切れるため、symlink で正本を
Drive に置かず、既存ファイルへの上書き (shutil.copyfile) で ID を保つ。

使い方:
    python stock_ratings.py show 3697
    python stock_ratings.py list [--status Active]
    python stock_ratings.py set 3697 --fund 36 --mispricing 17 --fair-price 1800 --reason "2Q決算反映"
    python stock_ratings.py export_html
    python stock_ratings.py migrate_v3   # Ver2 → Ver3 (一度きり)
"""

import argparse
import fcntl
import html
import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ks_util import DATA_DIR, is_dev_data_dir, log_print, log_warning
from research_shelve import CODE_S_PATTERN

RATINGS_DIR = Path(DATA_DIR) / "stock_ratings"
DRIVE_MIRROR_DIR = Path(DATA_DIR) / "stock_ratings_drive"
RATINGS_FILENAME = "stock_ratings.json"
HISTORY_FILENAME = "stock_ratings_history.jsonl"
HTML_FILENAME = "stock_ratings.html"
LOCK_FILENAME = ".stock_ratings.lock"

SCHEMA_VERSION = 1
RUBRIC_VERSION = "3.0"

# AI が付ける軸の上限 (下限はすべて0)
SCORE_MAX = {"fund": 40, "mispricing": 20}
# Ver3 でシステムが出す軸。AI からは受け付けない (#544)
SYSTEM_AXES = ("momentum", "valuation")
CONFIDENCE_VALUES = ("A", "B", "C")
STATUS_VALUES = ("Active", "Watch", "Archive")
TEXT_FIELDS = (
    "name", "role", "thesis", "mispricing_note", "risks", "checkpoints", "note",
)
# 新規作成時に必須の項目 (scores は4軸すべて必須)
REQUIRED_FIELDS = ("name", "confidence", "status")
# Status ごとに空にできない項目 (#542)。台帳には一次資料で調べた銘柄だけを載せ、
# Watch は何を待っているか、Archive は外した理由 (見送りの記録) を残す。
# 適正株価が無いと Valuation を出せないので、Active / Watch では必須 (#544)
STATUS_REQUIRED = {
    "Active": ("thesis", "risks", "checkpoints", "fair_price"),
    "Watch": ("thesis", "risks", "checkpoints", "fair_price"),
    "Archive": ("note",),
}

# Status ごとの上限 (doc/spec/銘柄評価仕様.md の「Status」)。超過は決算シーズン明けにまとめて整理する
STATUS_LIMITS = {"Active": 20, "Watch": 30}
# 外す候補とみなす、最後の更新からの日数 (暦日)
CANDIDATE_STALE_DAYS = 90

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
JST = timezone(timedelta(hours=9))


class RatingValidationError(ValueError):
    """検証エラー。errors に項目ごとのメッセージを持つ。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _paths(ratings_dir=None):
    root = Path(ratings_dir) if ratings_dir else RATINGS_DIR
    return root, root / RATINGS_FILENAME, root / HISTORY_FILENAME


@contextmanager
def _flock(root):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / LOCK_FILENAME, "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _now():
    return datetime.now(JST)


def _load(json_path):
    if not json_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "stocks": {},
        }
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(json_path, data):
    """同じディレクトリの一時ファイルに書いて fsync し、os.replace で置き換える。"""
    data = dict(data, stocks=dict(sorted(data["stocks"].items())))
    tmp_path = json_path.with_suffix(json_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, json_path)


def _append_history(history_path, entries):
    with open(history_path, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def validate_record(code_s, record):
    """1銘柄分のレコードを検証し、エラーメッセージのリストを返す (空なら正常)。"""
    errors = []
    if not isinstance(code_s, str) or not CODE_S_PATTERN.match(code_s):
        errors.append(f"code_s: 不正な銘柄コード {code_s!r}")

    allowed = set(TEXT_FIELDS) | {"scores", "fair_price", "confidence", "status", "updated_at"}
    for key in record:
        if key not in allowed:
            errors.append(f"{key}: 未知のフィールド")
    for key in REQUIRED_FIELDS:
        if key not in record:
            errors.append(f"{key}: 必須項目がない")

    scores = record.get("scores", {})
    if not isinstance(scores, dict):
        errors.append("scores: dict でない")
        scores = {}
    for key in scores:
        if key in SYSTEM_AXES:
            errors.append(f"{key}: Ver3 ではシステムが算出するので渡せない (Valuation は fair_price から出す)")
        elif key not in SCORE_MAX:
            errors.append(f"scores.{key}: 未知の軸")
    for key, max_value in SCORE_MAX.items():
        if key not in scores:
            errors.append(f"scores.{key}: 必須項目がない")
            continue
        value = scores[key]
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"scores.{key}: 整数でない ({value!r})")
        elif not 0 <= value <= max_value:
            errors.append(f"scores.{key}: 0〜{max_value} の範囲外 ({value})")

    for key in TEXT_FIELDS:
        if key in record and not isinstance(record[key], str):
            errors.append(f"{key}: 文字列でない ({record[key]!r})")
    fair_price = record.get("fair_price")
    if fair_price is not None and (isinstance(fair_price, bool) or not isinstance(fair_price, int) or fair_price <= 0):
        errors.append(f"fair_price: 正の整数 (円) でない ({fair_price!r})")
    if "confidence" in record and record["confidence"] not in CONFIDENCE_VALUES:
        errors.append(f"confidence: {'/'.join(CONFIDENCE_VALUES)} のいずれかでない ({record['confidence']!r})")
    if "status" in record and record["status"] not in STATUS_VALUES:
        errors.append(f"status: {'/'.join(STATUS_VALUES)} のいずれかでない ({record['status']!r})")
    if "updated_at" in record and not DATE_PATTERN.match(str(record["updated_at"])):
        errors.append(f"updated_at: YYYY-MM-DD 形式でない ({record['updated_at']!r})")
    return errors


def _flatten(record):
    """差分を取るため scores を scores.fund 形式に展開する。"""
    flat = {k: v for k, v in record.items() if k not in ("scores", "updated_at")}
    for key, value in record.get("scores", {}).items():
        flat[f"scores.{key}"] = value
    return flat


def _diff(before, after):
    old, new = _flatten(before), _flatten(after)
    return {
        key: [old.get(key), new.get(key)]
        for key in sorted(set(old) | set(new))
        if old.get(key) != new.get(key)
    }


def _with_system(code_s, record, system):
    """code_s・システムの2軸 (system)・総合点 (total) を足した dict を返す。

    総合点 = AI の2軸 (fund・mispricing) + Valuation + モメンタム (#544)。
    """
    ai = sum(record["scores"].get(key, 0) for key in SCORE_MAX)
    return dict(record, code_s=code_s, system=system,
                total=ai + system["valuation"]["score"] + system["momentum"]["total"])


def _rows(stocks):
    system = compute_system_scores(stocks)
    return [_with_system(code_s, record, system[code_s]) for code_s, record in stocks.items()]


def get_rating(code_s, ratings_dir=None):
    """1銘柄の評価を返す (code_s・system・total を足した dict)。未登録なら None。"""
    _, json_path, _ = _paths(ratings_dir)
    record = _load(json_path)["stocks"].get(code_s)
    return _rows({code_s: record})[0] if record else None


def list_ratings(status=None, ratings_dir=None):
    """評価の一覧を総合点の高い順に返す。status を指定するとその Status だけ。

    status 未指定では Archive を含めない (Archive は比較の対象から外す、#542)。
    Archive は status="Archive" を指定したときだけ返す。
    """
    _, json_path, _ = _paths(ratings_dir)
    stocks = {
        code_s: record for code_s, record in _load(json_path)["stocks"].items()
        if record["status"] == status or (status is None and record["status"] != "Archive")
    }
    return sorted(_rows(stocks), key=lambda r: (-r["total"], r["code_s"]))


def get_history(code_s, limit=3, ratings_dir=None):
    """1銘柄の変更履歴を新しい順に最大 limit 件返す。

    書き込み途中で落ちて壊れた行は読み飛ばす。
    """
    _, _, history_path = _paths(ratings_dir)
    if not history_path.exists():
        return []
    entries = []
    with open(history_path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("code_s") == code_s:
                entries.append(entry)
    return entries[::-1][:limit]


def archive_candidates(ratings, held, kessan_dates, today):
    """上限を超えたときの整理で外す候補を、理由つきで返す。Status は変えない (決めるのは人)。

    ratings: Active・Watch の行 (list_ratings の戻り値)
    held: {code_s: 保有ステータス}。ポートフォリオに無い銘柄は含まない
    kessan_dates: {code_s: [決算発表日 (YYYY-MM-DD、昇順)]}
    戻り値: [{code_s, name, status, updated_at, reasons}]。理由の多い順、同じなら更新の古い順
    """
    candidates = []
    for r in ratings:
        status, updated = r["status"], r.get("updated_at", "")
        reasons = []
        if status == "Watch" and r["code_s"] not in held:
            reasons.append("Watch なのにポートフォリオに無い")
        elif status == "Active" and held.get(r["code_s"]) not in ("1保", "2準"):
            reasons.append(f"Active なのに{held.get(r['code_s']) or 'ポートフォリオに無い'}")
        if status == "Watch":
            after = [d for d in kessan_dates.get(r["code_s"], []) if updated < d <= today.isoformat()]
            if after:
                reasons.append(f"{int(after[0][5:7])}/{int(after[0][8:])} の決算の後に未更新")
        if updated and (today - datetime.strptime(updated, "%Y-%m-%d").date()).days >= CANDIDATE_STALE_DAYS:
            reasons.append(f"最後の更新が {updated}")
        if reasons:
            candidates.append({"code_s": r["code_s"], "name": r.get("name", ""), "status": status,
                               "updated_at": updated, "reasons": reasons})
    return sorted(candidates, key=lambda c: (-len(c["reasons"]), c["updated_at"], c["code_s"]))


def load_archive_candidates(today, ratings):
    """archive_candidates の入力を DB から読む (読み取りのみ。MCP から呼ぶので webapp は import しない)。

    ratings は Active・Watch の行 (呼び出し側で読んだ list_ratings() を渡す)。
    """
    import gyoseki
    import portfolio_shelve as ps

    held = {r["code_s"]: r.get("status") for r in ps.list_records()}
    dates = {r["code_s"]: gyoseki.load_kessan_dates(r["code_s"])["kessan"] for r in ratings}
    return archive_candidates(ratings, held, dates, today)


def update_rating(code_s, fields, reason, source, ratings_dir=None):
    """1銘柄を部分更新する (未登録なら新規作成)。

    fields は平らな dict で、AI の2軸は fund / mispricing のキーで渡す。momentum / valuation は
    システムが出すので渡すとエラー。Valuation の元になる適正株価は fair_price (円) で渡す。
    渡さなかった項目は変更しない。文字列項目を空にしたいときは "" を渡す。
    検証に失敗したら何も書かずに RatingValidationError を出す。
    戻り値は変更内容 {項目: [前の値, 新しい値]} (変更がなければ空 dict で、何も書かない)。
    """
    root, json_path, history_path = _paths(ratings_dir)
    with _flock(root):
        data = _load(json_path)
        before = data["stocks"].get(code_s, {})
        after = dict(before, scores=dict(before.get("scores", {})))
        for key, value in fields.items():
            if key in SCORE_MAX or key in SYSTEM_AXES:
                after["scores"][key] = value
            else:
                after[key] = value

        errors = validate_record(code_s, after)
        if "updated_at" in fields:
            errors.append("updated_at: 書き込み時に自動で入るので指定できない")
        # Status ごとの必須項目は書き込みの経路だけで見る (済んだ移行の検証には掛けない)
        for key in STATUS_REQUIRED.get(after.get("status"), ()):
            if not str(after.get(key) or "").strip():
                errors.append(f"{key}: {after['status']} では空にできない")
        if after.get("status") == "Archive" and before.get("status") != "Archive" and "note" not in fields:
            errors.append("note: Archive にするときは、外す理由を同じ更新で note に書く")
        if errors:
            raise RatingValidationError(errors)

        changes = _diff(before, after)
        if not changes:
            return {}
        now = _now()
        after["updated_at"] = now.strftime("%Y-%m-%d")
        data["stocks"][code_s] = after
        system = compute_system_scores(data["stocks"])
        # 履歴を先に書く (JSON の置き換え前に落ちても変更が履歴から漏れない)。
        # 採点したときのシステムの2軸 (基準日・終値・適正株価からの換算を含む) と採点ルールの版も残す。
        # AI が変えた値ではないので changes には入れない
        _append_history(history_path, [{
            "at": now.isoformat(timespec="seconds"),
            "code_s": code_s,
            "source": source,
            "reason": reason,
            "rubric_version": RUBRIC_VERSION,
            "changes": changes,
            "system": system[code_s],
        }])
        _write_json(json_path, data)
        _write_outputs(root, data, system)
    return changes


# ===========================================
# 移行 (Ver2 → Ver3、一度きり)
# ===========================================

def migrate_v3(ratings_dir=None):
    """採点ルール Ver2 の台帳を Ver3 にする (#544)。

    全銘柄 (Archive も) から AI が付けたモメンタムと Valuation の点数を外し、ファイルの版を 3.0 にする。
    外した値は銘柄ごとに変更履歴に残す。既に 3.0 なら何もせず FileExistsError を出す。
    適正株価 (fair_price) はここでは入れない。Ver3 での一括付け直しで ChatGPT が入れる。
    戻り値は移行した銘柄数。
    """
    root, json_path, history_path = _paths(ratings_dir)
    with _flock(root):
        data = _load(json_path)
        if data.get("rubric_version") == RUBRIC_VERSION:
            raise FileExistsError(f"{json_path} は既に採点ルール Ver{RUBRIC_VERSION} のため移行を中断した")
        at = _now().isoformat(timespec="seconds")
        entries = []
        for code_s, record in sorted(data["stocks"].items()):
            removed = {key: record["scores"].pop(key) for key in SYSTEM_AXES if key in record["scores"]}
            entries.append({
                "at": at,
                "code_s": code_s,
                "source": "migration",
                "reason": "Ver3 移行: モメンタムと Valuation をシステム算出に切り替え",
                "rubric_version": RUBRIC_VERSION,
                "changes": {f"scores.{key}": [value, None] for key, value in removed.items()},
            })
        _append_history(history_path, entries)
        data["rubric_version"] = RUBRIC_VERSION
        _write_json(json_path, data)
        # refresh_outputs はロックを取り直すので、ロックの中から直接書く
        _write_outputs(root, data)
    return len(entries)


# ===========================================
# システムが出す2軸: モメンタム (#543) と Valuation (#544)
# ===========================================

# 欠けた要素を埋める点 (モメンタムは各要素 0〜10 の中央、Valuation は 0〜20 の中央)
MOMENTUM_FILL = 5
VALUATION_FILL = 10


def _load_system_inputs(codes):
    """銘柄ごとの RS・最新の終値・参考値、ポートフォリオの業態テーマ、最新の業態テーマ順位と基準日を読む。

    テーマ順位は日次バッチが portfolio_shelve に記録したもの (WebApp の calc_theme_ranks は
    呼ばない。MCP サーバーに webapp を import させないため)。基準日はその記録日。
    """
    import db_shelve
    import portfolio_shelve as ps

    stocks = {}
    with db_shelve.ShelveDB(db_shelve.STOCKS_SHELVE, read_only=True) as db:
        for code_s in codes:
            s = db.get(code_s) or {}
            stocks[code_s] = {
                "momentum_pt": s.get("momentum_pt"),
                "price": (s.get("price_log") or [None])[0],  # (日付, 終値) の最新
                "rironkabuka": s.get("rironkabuka"),
                "rironkabuka_preceding": s.get("rironkabuka_preceding"),
                "per": (s.get("shihyo") or {}).get("PER"),
                "per_date": s.get("access_date_shihyo"),
            }
    themes = {r["code_s"]: (r.get("memo") or {}).get("gyoutai_themes") or [] for r in ps.list_records()}
    snapshots = ps.load_theme_rank_snapshots()
    day = max(snapshots) if snapshots else None
    return stocks, themes, (snapshots[day] if day else {}), day


def _momentum_of(pt, themes, ranks, fresh=True):
    """1銘柄のモメンタム。RS = momentum_pt / 10、テーマ = 順位を1位10点・最下位0点に線形で割り当てた平均。

    fresh が False (株価の更新日が基準日と違う) なら RS は使わず、rs_stale として欠けた扱いにする。
    """
    ranked = {t: v["rank"] for t, v in ranks.items() if v.get("rank") is not None}
    n = len(ranked)
    found = [(t, ranked[t]) for t in themes if t in ranked] if n >= 2 else []  # 2未満は比べられない
    has_pt = bool(pt and pt > 0)
    rs = round(pt / 10) if has_pt and fresh else None
    theme = round(sum(10 * (n - r) / (n - 1) for _, r in found) / len(found)) if found else None
    missing = []
    if rs is None:
        missing.append("rs_stale" if has_pt else "rs")
    if theme is None:
        missing.append("theme")
    rs = MOMENTUM_FILL if rs is None else rs
    theme = MOMENTUM_FILL if theme is None else theme
    return {"total": rs + theme, "rs": rs, "theme": theme, "missing": missing,
            "momentum_pt": pt, "themes": [{"name": t, "rank": r} for t, r in found], "theme_count": n}


def _valuation_of(fair_price, close):
    """1銘柄の Valuation。上昇余地 u = 適正株価 / 基準日の終値 − 1 を、−20%→0・+20%→10・+60%→20 の直線で換算。

    close は基準日の終値 (基準日のものでなければ None)。どちらかが無ければ中央の10点で欠けた扱い。
    """
    missing = [name for name, v in (("fair_price", fair_price), ("price_stale", close)) if not v]
    if missing:
        return {"score": VALUATION_FILL, "fair_price": fair_price, "close": close, "upside": None, "missing": missing}
    upside = fair_price / close - 1
    return {"score": max(0, min(20, round(25 * (upside + 0.2)))), "fair_price": fair_price,
            "close": close, "upside": round(upside, 4), "missing": []}


def _system_of(stock, fair_price, themes, ranks, day):
    price = stock.get("price")
    fresh = bool(day and price and price[0] == day)
    per_date = stock.get("per_date")
    return {
        "as_of": day.isoformat() if day else None,
        "momentum": _momentum_of(stock.get("momentum_pt"), themes, ranks, fresh),
        "valuation": _valuation_of(fair_price, price[1] if fresh else None),
        "reference": {
            "rironkabuka": stock.get("rironkabuka"),
            "rironkabuka_preceding": stock.get("rironkabuka_preceding"),
            "per": stock.get("per"),
            "per_date": per_date.strftime("%Y-%m-%d") if per_date else None,
        },
    }


def compute_system_scores(records):
    """台帳の銘柄 ({code_s: record}) について、システムが出す2軸を {code_s: {...}} で返す。

    基準日 (as_of) は業態テーマ順位の最新記録日で、RS と終値はその日のものだけを使う (#544)。
    渡した全銘柄について必ず値を返す。読み出しに失敗したら全銘柄を、1銘柄の計算に失敗したら
    その銘柄を、欠けた扱い (モメンタム 10・Valuation 10) にする。台帳の読み書きは止めない。
    """
    try:
        stocks, themes, ranks, day = _load_system_inputs(list(records))
    except Exception as exc:
        log_warning(f"stock_ratings: システムの2軸を読めなかった: {exc}")
        stocks, themes, ranks, day = {}, {}, {}, None
    result = {}
    for code_s, record in records.items():
        try:
            result[code_s] = _system_of(stocks.get(code_s, {}), record.get("fair_price"),
                                        themes.get(code_s, []), ranks, day)
        except Exception as exc:
            log_warning(f"stock_ratings: {code_s} のシステムの2軸を算出できなかった: {exc}")
            result[code_s] = _system_of({}, None, [], {}, None)
    return result


def refresh_outputs(ratings_dir=None):
    """表示用 HTML と Drive の写しを、今のシステムの2軸で作り直す (日次バッチから呼ぶ)。"""
    root, json_path, _ = _paths(ratings_dir)
    with _flock(root):
        if json_path.exists():
            _write_outputs(root, _load(json_path))


# ===========================================
# 表示用 HTML (JSON → HTML の一方向。編集は反映しない)
# ===========================================

# スマホのプレビューでは JavaScript が動かないことがあるため、CSS だけで組む
_HTML_STYLE = """
:root { --bg: #fff; --fg: #1f2328; --muted: #656d76; --line: #d0d7de; --head: #f6f8fa; --accent: #0969da; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #30363d; --head: #161b22; --accent: #4493f8; }
}
body { margin: 0 auto; max-width: 1100px; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
.meta { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; white-space: nowrap; }
th, td { border-bottom: 1px solid var(--line); padding: 4px 8px; text-align: left; }
th { background: var(--head); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.total { font-weight: 700; }
td.role { white-space: normal; min-width: 12em; color: var(--muted); }
a { color: var(--accent); text-decoration: none; }
details { border: 1px solid var(--line); border-radius: 6px; margin: 8px 0; padding: 6px 10px; }
details.archive { margin-top: 28px; }
summary { cursor: pointer; font-weight: 600; }
dl { margin: 8px 0 0; }
dt { color: var(--muted); font-size: 12px; margin-top: 8px; }
dd { margin: 0; white-space: pre-wrap; }
"""

SECTION_LABELS = (
    ("thesis", "投資仮説"),
    ("mispricing_note", "未織込"),
    ("risks", "主要リスク"),
    ("checkpoints", "次の格上げ/確認条件"),
    ("note", "更新メモ"),
)


def _cell(value, missing):
    return f'{value}{"欠" if missing else ""}'


def _yen(value):
    return f"{value:,}円" if value else "なし"


def momentum_detail(m):
    themes = "、".join(f'{t["name"]} {t["rank"]}位' for t in m["themes"]) or "なし"
    text = (f'{m["total"]} = RS {m["rs"]} (RSスコア {m["momentum_pt"] or "なし"}) + '
            f'テーマ {m["theme"]} ({themes} / {m["theme_count"]}テーマ)')
    if m["missing"]:
        text += f' — 欠けた要素 ({"・".join(m["missing"])}) は {MOMENTUM_FILL}点で埋めた'
    return text


def valuation_detail(v, ref):
    upside = f'{v["upside"]:+.0%}' if v["upside"] is not None else "—"
    text = f'{v["score"]} = 適正株価 {_yen(v["fair_price"])} / 基準日の終値 {_yen(v["close"])} (上昇余地 {upside})'
    if v["missing"]:
        text += f' — 欠けた値 ({"・".join(v["missing"])}) のため {VALUATION_FILL}点'
    per = f'{ref["per"]}倍 ({ref["per_date"]})' if ref.get("per") else "なし"
    return (text + f'。参考: 理論株価 {_yen(ref.get("rironkabuka"))}・先行 {_yen(ref.get("rironkabuka_preceding"))}'
            f'、PER {per}')


def render_html(data, generated_at, system=None):
    """台帳 JSON から表示用 HTML を組み立てる。Status ごとに総合点順で並べる。

    system はシステムの2軸 (compute_system_scores)。渡さなければここで算出する。
    """
    system = system if system is not None else compute_system_scores(data["stocks"])
    esc = html.escape
    rows = sorted(
        (_with_system(code_s, record, system[code_s]) for code_s, record in data["stocks"].items()),
        key=lambda r: (-r["total"], r["code_s"]),
    )
    # 順位は Active と Watch だけで付ける。Archive は比較の対象から外し、末尾に畳む (#542)
    ranked = [r for r in rows if r["status"] != "Archive"]
    rank = {r["code_s"]: i for i, r in enumerate(ranked, start=1)}
    as_of = next((r["system"]["as_of"] for r in rows if r["system"]["as_of"]), None)

    parts = [
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>銘柄評価台帳</title><style>{_HTML_STYLE}</style></head><body>",
        "<h1>銘柄評価台帳</h1>",
        f'<p class="meta">生成 {esc(generated_at)} / 採点ルール Ver{esc(data.get("rubric_version", ""))}'
        f' / Valuation とモメンタムの基準日 {esc(as_of or "なし")}'
        f" / {len(rows)} 銘柄。表示専用で、正本は stock_ratings.json</p>",
    ]
    for status in STATUS_VALUES:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        archive = status == "Archive"
        if archive:
            parts.append(f'<details class="archive"><summary>{status} ({len(group)}) — 比較の対象外</summary>')
        else:
            parts.append(f"<h2>{status} ({len(group)})</h2>")
        parts.append(
            '<div class="table-wrap"><table><tr><th class="num">順位</th><th>コード</th>'
            '<th>銘柄</th><th class="num">ファンダ</th><th class="num">未織込</th>'
            '<th class="num" title="適正株価と基準日の終値からシステムが出す">Val</th>'
            '<th class="num" title="RS と業態テーマ順位からシステムが出す">モメンタム</th><th class="num">総合</th>'
            "<th>C</th><th>更新日</th><th>役割</th></tr>"
        )
        for r in group:
            s, v, m = r["scores"], r["system"]["valuation"], r["system"]["momentum"]
            parts.append(
                f'<tr><td class="num">{rank.get(r["code_s"], "—")}</td><td>{esc(r["code_s"])}</td>'
                f'<td><a href="#s{esc(r["code_s"])}">{esc(r["name"])}</a></td>'
                f'<td class="num">{s.get("fund", "—")}</td><td class="num">{s.get("mispricing", "—")}</td>'
                f'<td class="num">{_cell(v["score"], v["missing"])}</td>'
                f'<td class="num">{_cell(m["total"], m["missing"])}</td>'
                f'<td class="num total">{r["total"]}</td><td>{esc(r["confidence"])}</td>'
                f'<td>{esc(r.get("updated_at", ""))}</td><td class="role">{esc(r.get("role", ""))}</td></tr>'
            )
        parts.append("</table></div>")
        for r in group:
            s, v, m = r["scores"], r["system"]["valuation"], r["system"]["momentum"]
            parts.append(
                f'<details id="s{esc(r["code_s"])}"><summary>{esc(r["code_s"])} {esc(r["name"])}'
                f' — {r["total"]} ({s.get("fund", "—")}/{s.get("mispricing", "—")}/{v["score"]}/{m["total"]})'
                f' {esc(r["confidence"])}</summary><dl>'
            )
            for key, label in SECTION_LABELS:
                if r.get(key):
                    parts.append(f"<dt>{label}</dt><dd>{esc(r[key])}</dd>")
            parts.append(f"<dt>Valuation (システム)</dt><dd>{esc(valuation_detail(v, r['system']['reference']))}</dd>")
            parts.append(f"<dt>モメンタム (システム)</dt><dd>{esc(momentum_detail(m))}</dd>")
            parts.append("</dl></details>")
        if archive:
            parts.append("</details>")
    parts.append("</body></html>\n")
    return "\n".join(parts)


def _write_outputs(root, data, system=None):
    """表示用 HTML と Drive の写しを書く。system を渡さなければここで算出する。"""
    if system is None:
        system = compute_system_scores(data["stocks"])
    _export_html_safely(root, data, system)
    _mirror_to_drive(root, data, system)


def _export_html_safely(root, data, system=None):
    """表示用 HTML を書き出す。失敗しても JSON の更新は成功扱いにする (警告だけ出す)。"""
    try:
        html_path = root / HTML_FILENAME
        tmp_path = html_path.with_suffix(html_path.suffix + ".tmp")
        tmp_path.write_text(
            render_html(data, _now().strftime("%Y-%m-%d %H:%M"), system), encoding="utf-8"
        )
        os.replace(tmp_path, html_path)
    except Exception as exc:  # 表示用なので、失敗で正本の更新を巻き戻さない
        log_warning(f"stock_ratings: HTML の書き出しに失敗した: {exc}")


def _mirror_to_drive(root, data=None, system=None):
    """正本を Drive へ上書きコピーする。ミラーが無い・開発用データ・テストなら何もしない。

    JSON だけは、各銘柄にシステムの2軸 (system) を埋め込んだ写しを出す (正本の JSON には入れない)。
    失敗しても正本の更新は成功扱いにする (警告だけ出す)。
    """
    if root != RATINGS_DIR or is_dev_data_dir() or not DRIVE_MIRROR_DIR.is_dir():
        return
    try:
        sources = {name: root / name for name in (RATINGS_FILENAME, HISTORY_FILENAME, HTML_FILENAME)}
        if data is not None and system:
            enriched = dict(data, stocks={
                code_s: dict(record, system=system.get(code_s))
                for code_s, record in data["stocks"].items()
            })
            sources[RATINGS_FILENAME] = root / f".drive_{RATINGS_FILENAME}"  # Drive に出す写し (正本ではない)
            _write_json(sources[RATINGS_FILENAME], enriched)
        for name, src in sources.items():
            if src.exists():
                shutil.copyfile(src, DRIVE_MIRROR_DIR / name)
    except Exception as exc:
        log_warning(f"stock_ratings: Drive へのコピーに失敗した: {exc}")


def export_html(ratings_dir=None):
    """現在の JSON から表示用 HTML を作り直す。"""
    root, json_path, _ = _paths(ratings_dir)
    with _flock(root):
        _write_outputs(root, _load(json_path))
    return root / HTML_FILENAME


# ===========================================
# CLI
# ===========================================

def _cmd_show(code_s):
    record = get_rating(code_s)
    if record is None:
        log_print(f"stock_ratings: {code_s} は未登録")
        return 1
    # 出力本体は標準出力へ直接書く (CLI表示の主目的)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


# 欠けた値の名前 → list の先頭行での呼び方
MISSING_LABELS = {
    "price_stale": "終値が基準日のものでない", "rs_stale": "RS が基準日のものでない",
    "fair_price": "適正株価なし", "rs": "RS なし", "theme": "業態テーマなし",
}


def _cmd_list(status):
    rows = list_ratings(status)
    # 基準日と欠けた値の件数を先頭に出す。日付がそろっているか・付け直しの進み具合をここで見る
    as_of = next((r["system"]["as_of"] for r in rows if r["system"]["as_of"]), None)
    counts = {}
    for r in rows:
        for key in r["system"]["valuation"]["missing"] + r["system"]["momentum"]["missing"]:
            counts[key] = counts.get(key, 0) + 1
    missing = "・".join(f"{MISSING_LABELS.get(k, k)} {n}" for k, n in counts.items()) or "なし"
    print(f"基準日 {as_of or 'なし'} / {len(rows)} 銘柄 / 欠: {missing}")
    for r in rows:
        s, v, m = r["scores"], r["system"]["valuation"], r["system"]["momentum"]
        print(
            f"{r['code_s']:<5} {r['total']:>3} "
            f"({s.get('fund', 0):>2}/{s.get('mispricing', 0):>2}/{_cell(v['score'], v['missing']):>3}/"
            f"{_cell(m['total'], m['missing']):>3}) "
            f"{r['confidence']} {r['status']:<7} {r['updated_at']} {r['name']}"
        )
    return 0


def _cmd_set(args):
    fields = {
        key: getattr(args, key)
        for key in (*SCORE_MAX, *TEXT_FIELDS, "fair_price", "confidence", "status")
        if getattr(args, key) is not None
    }
    try:
        changes = update_rating(args.code_s, fields, args.reason, source="cli")
    except RatingValidationError as exc:
        for error in exc.errors:
            log_warning(f"stock_ratings set: {error}")
        return 2
    if not changes:
        log_print(f"stock_ratings: {args.code_s} は変更なし")
    for key, (old, new) in changes.items():
        log_print(f"stock_ratings: {args.code_s} {key}: {old!r} → {new!r}")
    return 0


def _cmd_migrate_v3():
    try:
        count = migrate_v3()
    except FileExistsError as exc:
        log_warning(f"stock_ratings migrate_v3: {exc}")
        return 2
    log_print(f"stock_ratings migrate_v3: {count} 銘柄を採点ルール Ver{RUBRIC_VERSION} に移行した")
    return 0


def _cmd_export_html():
    log_print(f"stock_ratings: {export_html()} を書き出した")
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(description="銘柄評価台帳")
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="1銘柄を表示")
    show_parser.add_argument("code_s")

    list_parser = subparsers.add_parser("list", help="総合点順に一覧表示")
    list_parser.add_argument("--status", choices=STATUS_VALUES)

    set_parser = subparsers.add_parser("set", help="1銘柄を部分更新 (未登録なら新規作成)")
    set_parser.add_argument("code_s")
    set_parser.add_argument("--reason", required=True)
    for key in SCORE_MAX:
        set_parser.add_argument(f"--{key}", type=int)
    for key in TEXT_FIELDS:
        set_parser.add_argument(f"--{key.replace('_', '-')}", dest=key)
    set_parser.add_argument("--fair-price", dest="fair_price", type=int)
    set_parser.add_argument("--confidence")
    set_parser.add_argument("--status")

    subparsers.add_parser("export_html", help="表示用 HTML を作り直す")

    subparsers.add_parser("migrate_v3", help="採点ルール Ver2 → Ver3 に一度だけ移行")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "show":
        return _cmd_show(args.code_s)
    if args.command == "list":
        return _cmd_list(args.status)
    if args.command == "set":
        return _cmd_set(args)
    if args.command == "export_html":
        return _cmd_export_html()
    return _cmd_migrate_v3()


if __name__ == "__main__":
    import sys

    sys.exit(main())
