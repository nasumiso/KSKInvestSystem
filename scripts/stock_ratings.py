"""銘柄評価台帳 (ChatGPT で採点している投資判断スナップショット) の正本。

正本は KS_DATA_DIR/stock_ratings/ の JSON 1ファイル、変更履歴は JSONL (追記のみ)。
KS_DATA_DIR は Google Drive のミラー同期フォルダなので、置くだけで Drive に同期される。
書き込みはこのモジュール経由のみ (flock で排他し、一時ファイル → os.replace で置き換える)。

使い方:
    python stock_ratings.py show 3697
    python stock_ratings.py list [--status Active]
    python stock_ratings.py set 3697 --fund 36 --mispricing 17 --reason "2Q決算反映"
    python stock_ratings.py migrate --csv <シートから書き出したCSV>
"""

import argparse
import csv
import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ks_util import DATA_DIR, log_print, log_warning
from research_shelve import CODE_S_PATTERN

RATINGS_DIR = Path(DATA_DIR) / "stock_ratings"
RATINGS_FILENAME = "stock_ratings.json"
HISTORY_FILENAME = "stock_ratings_history.jsonl"
LOCK_FILENAME = ".stock_ratings.lock"

SCHEMA_VERSION = 1
RUBRIC_VERSION = "2.0"

# 各軸の上限 (下限はすべて0)
SCORE_MAX = {"fund": 40, "mispricing": 20, "momentum": 20, "valuation": 20}
CONFIDENCE_VALUES = ("A", "B", "C")
STATUS_VALUES = ("Active", "Watch", "Archive")
TEXT_FIELDS = (
    "name", "role", "thesis", "mispricing_note", "risks", "checkpoints", "note",
)
# 新規作成時に必須の項目 (scores は4軸すべて必須)
REQUIRED_FIELDS = ("name", "confidence", "status")

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

    allowed = set(TEXT_FIELDS) | {"scores", "confidence", "status", "updated_at"}
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
        if key not in SCORE_MAX:
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


def _with_total(code_s, record):
    return dict(record, code_s=code_s, total=sum(record["scores"].values()))


def get_rating(code_s, ratings_dir=None):
    """1銘柄の評価を返す (code_s と total を足した dict)。未登録なら None。"""
    _, json_path, _ = _paths(ratings_dir)
    record = _load(json_path)["stocks"].get(code_s)
    return _with_total(code_s, record) if record else None


def list_ratings(status=None, ratings_dir=None):
    """評価の一覧を総合点の高い順に返す。status を指定するとその Status だけ。"""
    _, json_path, _ = _paths(ratings_dir)
    stocks = _load(json_path)["stocks"]
    rows = [
        _with_total(code_s, record)
        for code_s, record in stocks.items()
        if status is None or record["status"] == status
    ]
    return sorted(rows, key=lambda r: (-r["total"], r["code_s"]))


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


def update_rating(code_s, fields, reason, source, ratings_dir=None):
    """1銘柄を部分更新する (未登録なら新規作成)。

    fields は平らな dict で、4軸は fund / mispricing / momentum / valuation のキーで渡す。
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
            if key in SCORE_MAX:
                after["scores"][key] = value
            else:
                after[key] = value

        errors = validate_record(code_s, after)
        if "updated_at" in fields:
            errors.append("updated_at: 書き込み時に自動で入るので指定できない")
        if errors:
            raise RatingValidationError(errors)

        changes = _diff(before, after)
        if not changes:
            return {}
        now = _now()
        after["updated_at"] = now.strftime("%Y-%m-%d")
        data["stocks"][code_s] = after
        # 履歴を先に書く (JSON の置き換え前に落ちても変更が履歴から漏れない)
        _append_history(history_path, [{
            "at": now.isoformat(timespec="seconds"),
            "code_s": code_s,
            "source": source,
            "reason": reason,
            "changes": changes,
        }])
        _write_json(json_path, data)
    return changes


# ===========================================
# 移行 (シート → JSON、一度きり)
# ===========================================

# シートの列名 → JSON の項目名。順位・総合・保有は移行しない
_CSV_COLUMNS = {
    "銘柄": "name",
    "ファンダ /40": "fund",
    "未織込 /20": "mispricing",
    "モメンタム /20": "momentum",
    "Valuation /20": "valuation",
    "Confidence": "confidence",
    "役割": "role",
    "投資仮説": "thesis",
    "主要リスク": "risks",
    "次の格上げ/確認条件": "checkpoints",
    "最終レビュー": "updated_at",
    "更新メモ": "note",
    "Status": "status",
}


def _record_from_csv_row(row):
    values = {key: (row.get(column) or "").strip() for column, key in _CSV_COLUMNS.items()}
    scores = {
        key: int(values[key]) if values[key].lstrip("-").isdigit() else values[key]
        for key in SCORE_MAX
    }
    # Drive 上で人が読みやすいよう、銘柄名 → 判断 → 根拠の順に並べる
    record = {"name": values["name"], "status": values["status"], "role": values["role"],
              "scores": scores, "confidence": values["confidence"]}
    for key in ("thesis", "mispricing_note", "risks", "checkpoints", "note", "updated_at"):
        record[key] = values.get(key, "")
    return record


def migrate_from_csv(csv_path, ratings_dir=None):
    """シートから書き出した CSV を JSON に移行する。

    JSON が既にあれば中断する。全行を検証し、エラーが1行でもあれば何も書かずに
    RatingValidationError を出す。怪しい値は警告のリストとして返す (移行はする)。
    戻り値は (移行した銘柄数, 警告のリスト)。
    """
    root, json_path, history_path = _paths(ratings_dir)
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    stocks, errors, warnings = {}, [], []
    for line_no, row in enumerate(rows, start=2):
        code_s = (row.get("コード") or "").strip()
        record = _record_from_csv_row(row)
        row_errors = validate_record(code_s, record)
        if code_s in stocks:
            row_errors.append("コードが重複している")
        if row_errors:
            errors.extend(f"{line_no}行目 {code_s}: {e}" for e in row_errors)
            continue
        stocks[code_s] = record

        total = sum(record["scores"].values())
        sheet_total = (row.get("総合 /100") or "").strip()
        if sheet_total != str(total):
            warnings.append(f"{code_s}: 総合 {sheet_total} と4軸合計 {total} が一致しない")
        if DATE_PATTERN.match(record["note"]):
            warnings.append(f"{code_s}: 更新メモが日付だけ ({record['note']})")
        extra = (row.get("") or "").strip()
        if extra:
            warnings.append(f"{code_s}: Status の右の列に値がある ({extra})。読まずに捨てる")

    if errors:
        raise RatingValidationError(errors)

    with _flock(root):
        if json_path.exists():
            raise FileExistsError(f"{json_path} が既にあるため移行を中断した")
        at = _now().isoformat(timespec="seconds")
        _append_history(history_path, [
            {
                "at": at,
                "code_s": code_s,
                "source": "migration",
                "reason": "スプレッドシートから移行",
                "changes": _diff({}, record),
            }
            for code_s, record in sorted(stocks.items())
        ])
        _write_json(json_path, {
            "schema_version": SCHEMA_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "stocks": stocks,
        })
    return len(stocks), warnings


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


def _cmd_list(status):
    for r in list_ratings(status):
        s = r["scores"]
        print(
            f"{r['code_s']:<5} {r['total']:>3} "
            f"({s['fund']:>2}/{s['mispricing']:>2}/{s['momentum']:>2}/{s['valuation']:>2}) "
            f"{r['confidence']} {r['status']:<7} {r['updated_at']} {r['name']}"
        )
    return 0


def _cmd_set(args):
    fields = {
        key: getattr(args, key)
        for key in (*SCORE_MAX, *TEXT_FIELDS, "confidence", "status")
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


def _cmd_migrate(csv_path):
    try:
        count, warnings = migrate_from_csv(csv_path)
    except RatingValidationError as exc:
        for error in exc.errors:
            log_warning(f"stock_ratings migrate: {error}")
        log_warning("stock_ratings migrate: 検証エラーのため何も書かずに中断した")
        return 2
    except FileExistsError as exc:
        log_warning(f"stock_ratings migrate: {exc}")
        return 2
    for warning in warnings:
        log_warning(f"stock_ratings migrate: {warning}")
    log_print(f"stock_ratings migrate: {count} 銘柄を移行した (警告 {len(warnings)} 件)")
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
    set_parser.add_argument("--confidence")
    set_parser.add_argument("--status")

    migrate_parser = subparsers.add_parser("migrate", help="シートの CSV から一度だけ移行")
    migrate_parser.add_argument("--csv", required=True)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "show":
        return _cmd_show(args.code_s)
    if args.command == "list":
        return _cmd_list(args.status)
    if args.command == "set":
        return _cmd_set(args)
    return _cmd_migrate(args.csv)


if __name__ == "__main__":
    import sys

    sys.exit(main())
