#!/usr/bin/env python3
"""
保有銘柄管理DB (portfolio_shelve) の基盤モジュール。

保有銘柄のステータス・手動メモ・アクションログを永続化するための
shelve ベースのラッパー。

既存の stocks_shelve / research_shelve とは別DBとして分離運用する:
- stocks_shelve: 揮発性キャッシュ (常に最新値で上書き)
- research_shelve: 不可逆な蓄積資産 (時系列履歴 + 調査メモ)
- portfolio_shelve: 保有状態 + 売買判断メモ + アクションログ

依存は一方向: portfolio_shelve が他の2DBを参照する形。
他のDBのコードは変更しない。

キー名前空間:
- record:<code_s>            -> 保有レコード本体
- action_log:<code_s>:<seq>  -> アクションログ (削除後も残る)
- _seq:<code_s>              -> アクションログの連番カウンタ

ライフサイクル:
- 追加: (新規) -> 3監 (1保/2準への直接登録は禁止)
- ステータス変更: 3監 <-> 2準 <-> 1保 / 3監 <-> 1保
- 売却: 1保 -> 2準 (アクションログ種別「売却」で記録)
- 削除: 3監 のみ (1保/2準 から直接削除は禁止、レコードは物理削除) ※現在 UI 経路なし
- ユニバース除外: 3監 のみ。`excluded=True` フラグで論理削除し、メモ・ログを保持。
  add_to_watch で同コード再投入すると excluded=False に戻して復活する
"""

import fcntl
import glob
import hashlib
import math
import os
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from db_shelve import PORTFOLIO_SHELVE, ShelveDB

try:
    from ks_util import DATA_DIR, backup_file, log_print, log_warning
except ImportError:
    import shutil as _shutil

    DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)

    def backup_file(fname, day=0, overwrite=False):  # type: ignore[override]
        if not os.path.exists(fname):
            return None
        backup_fname = fname + ".bak_fallback"
        _shutil.copy2(fname, backup_fname)
        return backup_fname


# ===========================================
# スキーマ定数
# ===========================================

# 銘柄コードの正規表現 (CLAUDE.md 規約: "0001"〜"9999" または "215A" 形式)
CODE_S_PATTERN = re.compile(r"^(?:\d{4}|\d{3}[A-Z])$")

# ステータスの許容値
VALID_STATUSES = frozenset({"1保", "2準", "3監"})

# アクションログ種別
VALID_ACTION_TYPES = frozenset(
    {"初回登録", "ステータス変更", "売却", "削除", "ユニバース除外", "株数変更"}
)

# issue #397: action_log の反映元。手入力 (デフォルト) / CSV取込を区別する。
VALID_ACTION_SOURCES = frozenset({"manual", "csv_import"})

# キー名前空間プレフィックス
KEY_RECORD_PREFIX = "record:"
KEY_ACTION_LOG_PREFIX = "action_log:"
KEY_SEQ_PREFIX = "_seq:"
KEY_THEME_PREFIX = "theme:"
KEY_TRADE_IDEA_PREFIX = "trade_idea:"
# issue #360 Phase2: 楽天CSV 由来の約定事実 (fill レイヤー、イミュータブル)
KEY_FILL_PREFIX = "fill:"
KEY_FILL_SEQ_PREFIX = "_fill_seq:"
# issue #387 Phase2: fill 建玉ラウンド (エピソード) 単位の振り返りメモ。
# fill は再取込で作り直されるため、メモは別レイヤーに独立保存し
# エピソードキー (code_s|kind|open_date|close_date) で紐付ける。
KEY_FILL_MEMO_PREFIX = "fill_memo:"
# issue #397: 証券会社ポートフォリオCSV 由来の保有残高スナップショット。
# (broker, account, kind, code_s) 単位で最新のみ保持 (上書き、fill と違い履歴を持たない)。
KEY_POSITION_PREFIX = "position:"
# ソース単位 (broker, account, kind) の取込メタ。covered 判定 (全ソース同一 as_of) に使う。
KEY_POSITION_SOURCE_PREFIX = "position_source:"
# issue #397 Phase2: CSV取込で検出したが trade_idea 未設定のため 1保 に上げられない
# 新規保有の保留キュー (code_s 単位、1件のみ保持)。
KEY_PENDING_IN_PREFIX = "pending_in:"
# issue #398: 株式分割・併合の換算比率 (yfinance corporate actions 由来のキャッシュ)。
# fill 本体は不変のまま、エピソード再構成時にのみ適用する派生情報として分離保存する。
KEY_SPLIT_ADJ_PREFIX = "split_adj:"
# --check-splits で検知したが split_adj 未登録の銘柄コード (拒否リスト)。
# build_fill_episodes は yfinance を呼ばないため、単価ジャンプが無く保有中の
# 総当たりチェックでのみ見つかるケースを検知できない。ここに記録して埋める。
KEY_SPLIT_PENDING_REVIEW_PREFIX = "split_pending_review:"
# --reject-split で分割・併合ではないと判断したイベント (再検出抑止リスト)。
KEY_SPLIT_REJECTED_REVIEW_PREFIX = "split_rejected_review:"

# テーママスター (issue #282)
THEME_FIELDS = frozenset({"name", "description", "created_at"})
THEME_NAME_MAX_LEN = 30  # UI バッジを崩さない上限
# URL に含めると曖昧になる文字 + HTML/JS リテラルを破壊する文字を name に許可しない
# (`/portfolio/themes/<name>/...` ルートの破綻防止 + テンプレ展開時の注入予防)
_THEME_NAME_FORBIDDEN_RE = re.compile(r"[\/\?\#\&\%\+\<\>\"\'\\\x00-\x1F]")

# PF 全体で 1 つだけ保持するメタキー (どこかの銘柄で qty が変化したら更新)
KEY_QTY_GLOBAL_UPDATED_AT = "_meta:qty_global_updated_at"

# shelve は .dat/.dir/.bak の3点セットで構成される (dbm.dumb)
_SHELVE_EXTENSIONS = (".dat", ".dir", ".bak")

# レコードの既知フィールド (銘柄名は持たない: 表示時に stocks_shelve / research_shelve から都度取得する)
RECORD_FIELDS = frozenset(
    {
        "code_s",
        "status",
        "registered_at",
        "updated_at",
        "memo",
        "excluded",
        "qty",
    }
)

# 旧スキーマ由来で許容するが扱わないフィールド (新スキーマでは未使用、過去データ互換のため warning しない)
LEGACY_RECORD_FIELDS = frozenset({"stock_name"})

MEMO_FIELDS = frozenset(
    {
        "gyoutai_theme",          # 旧: 業態・テーマ (str/改行区切り、移行期間中のみ残す。issue #187)
        "gyoutai_themes",         # 新: 業態・テーマ (list[str]、UI では最大2件)
        "watch_in_reason",        # ウォッチ・IN理由
        "trade_idea",             # 売買戦略 (旧: 投資売買アイデア)
        "inago_origin",           # イナゴ元・きっかけ
        "takaichi_sensitivity",   # 売買メモ (旧: 高市感応度)
        "last_research_update",   # 銘柄調査スプシでの更新日 (M/D 形式、年なし)
        "stage",                  # ステージ評価 (例: "1S", "2S(3T)", "3S")
        "jukyu_chart",            # 需給チャートメモ (例: "月足低位ブレイク CWH")
    }
)

# list[str] として扱う memo フィールド (str 系とバリデーションを分ける)
MEMO_LIST_FIELDS = frozenset({"gyoutai_themes"})

# gyoutai_themes の UI スロット上限 (issue #187)
GYOUTAI_THEMES_MAX_SLOTS = 2

# trade_idea (売買戦略) の定型リスト (issue #327: 自由記述から単一選択式へ移行)。
# 空文字 = 未分類。リスト外の既存値 (旧自由記述) は update_memo で保持を許可する (救済)。
# CSV 移行 (create_record 経由) はこのチェックを通らないため自由記述のまま格納される。
# 時間軸は戦略名に内包する (中期テーマ等)。各戦略の定義・決算またぎ可否は編集画面 issue で管理予定。
# 各戦略の説明 (UI の tooltip 用)。値は戦略の時間軸・狙いを 1 行で表す。
TRADE_IDEA_DESCRIPTIONS = {
    "GARP": "中長期: 安定成長株を押し目・一時的売り込まれ局面で拾う",
    "ピーターリンチ": "中長期: 身近な実感で好印象の銘柄を持つ",
    "中期テーマ": "中期: 相場の物色テーマの波に乗る",
    "中期モメンタム": "中期: 新高値ブレイク順張り 2〜3ヶ月保有 (メイン戦略)",
    "短期イベント": "短期: 決算・材料・政策などカタリスト狙い",
    "底値リバ": "短期: 下げすぎリバ。サイズ小・機械的利確損切り",
    "夢枠": "長期: 2〜3年の夢に乗る。現物放置",
    "大型高配当": "恒常: PF安定・信用代用",
}
TRADE_IDEA_OPTIONS = tuple(TRADE_IDEA_DESCRIPTIONS.keys())

# 戦略マスター (issue #335: 定数 shelve 移行・編集画面)
VALID_TIME_HORIZONS = ("短期", "中期", "中長期", "長期", "恒常", "")

_TRADE_IDEA_SEED = [
    {"name": "GARP",           "description": "安定成長株を押し目・一時的売り込まれ局面で拾う。業績成長シナリオが崩れたら売る",    "time_horizon": "中長期", "over_earnings": True},
    {"name": "ピーターリンチ", "description": "身近な実感で好印象の銘柄を持つ。身近な実感・好印象が消えたら売る",                  "time_horizon": "中長期", "over_earnings": False},
    {"name": "中期テーマ",     "description": "相場の物色テーマの波に乗る。物色が他テーマへ移ったら売る",                          "time_horizon": "中期",   "over_earnings": False},
    {"name": "中期モメンタム", "description": "新高値ブレイク順張り 2〜3ヶ月保有。トレンド（チャート）が崩れたら売る",             "time_horizon": "中期",   "over_earnings": False},
    {"name": "短期イベント",   "description": "決算・材料・政策などカタリスト狙い。イベント通過で手仕舞い",                        "time_horizon": "短期",   "over_earnings": True},
    {"name": "底値リバ",       "description": "下げすぎリバ取り。MAタッチ/10%で機械的利確。サイズ小・損切り事前設定",             "time_horizon": "短期",   "over_earnings": False},
    {"name": "夢枠",           "description": "2〜3年の夢に乗る。現物放置。売らない",                                              "time_horizon": "長期",   "over_earnings": True},
    {"name": "大型高配当",     "description": "PF安定・信用代用を兼ねる。恒常保有。売らない",                                      "time_horizon": "恒常",   "over_earnings": True},
]

# issue #344: stage / jukyu_chart を自由記述から選択式へ寄せるための定型候補。
# DB スキーマは変えず、UI 側で分解入力した値を route 層で 1 本の文字列に畳み込む。
STAGE_OPTIONS = (
    "1S", "2S", "3S", "4S", "1Sor3S",
    "1S~2S", "2S~3S", "3S~4S", "4S~1S",
)
STAGE_T_OPTIONS = ("1", "2", "3", "4", "5")
CHART_STYLE_OPTIONS = (
    "週足低位", "週足CWH", "週足VCP", "週足高値",
    "月足低位", "月足CWH", "月足VCP", "月足高値",
)
CHART_STATE_OPTIONS = ("形成", "ブレイク", "ブレイク済み", "ブレイク失敗", "再ブレイク")

ACTION_LOG_FIELDS = frozenset(
    {
        "code_s",
        "seq",
        "timestamp",
        "action_type",
        "status_from",
        "status_to",
        "reason",
        "review_memo",
        "qty",
        # issue #361: 売買イベント日の終値プロキシ。実約定価格 (#360 Phase2) が
        # 入れば price_source="actual" で上書きできる構造。旧ログは list 側で None 補完。
        "price_proxy",   # イベント日 (直前営業日) の終値 (int) or None
        "price_source",  # "close" (終値プロキシ) / 将来 "actual" (実約定)
        "post_sell_returns",
        # issue #397: 反映元。旧ログは list 側で "manual" 補完 (後方互換)。
        "source",         # "manual" (既定) / "csv_import"
        "source_detail",  # 取込元の識別子 (例 "楽天/信用/2026-08-10")
    }
)

# issue #360 Phase2: fill レコードの既知フィールド。
# CSV 由来の約定事実 (価格・株数・約定日) の真実源。イミュータブルに追記のみ。
FILL_FIELDS = frozenset(
    {
        "code_s",
        "seq",
        "trade_date",     # 約定日 "YYYY-MM-DD"
        "side",           # "buy" / "sell" (建玉方向を正規化)
        "qty",            # 数量[株] (>0)
        "price",          # 単価[円] (>0, float)
        "amount",         # 受渡金額[円] (楽天符号のまま int)
        "trade_kind",     # 元の取引区分 (現物 / 信用新規 / 信用返済 / 現物(単元未満))
        "dedup_key",      # 冪等取込用ハッシュ
        "imported_at",    # 取込時刻 ISO8601
        "broker",         # 取込元証券会社 ("楽天" / "SBI")。issue #387 Phase3
        "settle_pl",      # 決済損益[円] (SBI 信用返済行のみ。無ければ None)。issue #387 Phase3
        "tate_date",      # 建約定日 "YYYY-MM-DD" (楽天 信用返済行のみ)。issue #387 Phase4b
        "tate_price",     # 建単価[円] (楽天 信用返済行のみ、float)。issue #387 Phase4b
    }
)

# 建玉方向の正規化 (楽天 売買区分 → side)
SIDE_BUY = "buy"
SIDE_SELL = "sell"


# 許可されるステータス遷移 (status_from, status_to)
# (None, "3監") は新規追加。それ以外は (from, to) の組
ALLOWED_TRANSITIONS = frozenset(
    {
        (None, "3監"),     # 新規追加 (1保/2準 への直接登録禁止)
        ("3監", "2準"),
        ("2準", "3監"),
        ("2準", "1保"),
        ("1保", "2準"),    # = 売却
        ("3監", "1保"),
        ("1保", "3監"),
    }
)


# ===========================================
# バリデーション・正規化
# ===========================================

# JST タイムゾーン (timestamp の付与に使う)
JST = timezone(timedelta(hours=9))


def normalize_code_s(code_s: Any) -> str:
    """銘柄コード文字列を正規化する。

    - 文字列型でない場合は TypeError
    - 前後の空白を除去
    - 英字部分を大文字化
    """
    if not isinstance(code_s, str):
        raise TypeError(f"code_s must be str, got {type(code_s).__name__}")
    return code_s.strip().upper()


def validate_code_s(code_s: Any) -> None:
    """銘柄コードを検証する。"""
    normalized = normalize_code_s(code_s)
    if not CODE_S_PATTERN.match(normalized):
        raise ValueError(
            f"invalid code_s: {code_s!r} (正規化後={normalized!r}、"
            "期待形式は4文字の数字または3桁数字+大文字1文字)"
        )


_etf_codes_cache: Optional[frozenset] = None


def load_etf_code_set() -> frozenset:
    """ETF コード集合を `DATA_DIR/ETF_code.txt` から読む (issue #387)。

    ファイルはタブ区切り (`1357\t日経Ｄインバ`) でコードは先頭列。
    株式売買の分析対象外なので、fill 取込時に除外するのに使う。
    プロセス内でキャッシュする (取込ループから毎行呼ばれるため)。
    """
    global _etf_codes_cache
    if _etf_codes_cache is None:
        codes = set()
        try:
            with open(os.path.join(DATA_DIR, "ETF_code.txt"), "r") as f:
                for line in f:
                    code = line.strip().split("\t")[0].strip()
                    if code:
                        codes.add(normalize_code_s(code))
        except OSError as e:
            log_warning(f"ETF_code.txt を読めませんでした (ETF除外をスキップ): {e}")
        _etf_codes_cache = frozenset(codes)
    return _etf_codes_cache


def is_etf_code(code_s: Any) -> bool:
    """銘柄コードが ETF (ETF_code.txt 掲載) かどうか。"""
    return normalize_code_s(code_s) in load_etf_code_set()


def validate_status(status: Any) -> None:
    """ステータスを検証する。"""
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status: {status!r} (許容値: {sorted(VALID_STATUSES)})"
        )


def validate_action_type(action_type: Any) -> None:
    """アクションログ種別を検証する。"""
    if action_type not in VALID_ACTION_TYPES:
        raise ValueError(
            f"invalid action_type: {action_type!r} "
            f"(許容値: {sorted(VALID_ACTION_TYPES)})"
        )


def validate_action_source(source: Any) -> None:
    """アクションログ反映元 (issue #397) を検証する。"""
    if source not in VALID_ACTION_SOURCES:
        raise ValueError(
            f"invalid source: {source!r} (許容値: {sorted(VALID_ACTION_SOURCES)})"
        )


def _validate_qty(qty: Any) -> None:
    """保有株数を検証する (issue #269)。

    - 整数型 (bool は除外: True/False が 1/0 として通るのを防ぐ)
    - 0 以上
    """
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise TypeError(f"qty must be int, got {type(qty).__name__}")
    if qty < 0:
        raise ValueError(f"qty must be >= 0, got {qty}")


def validate_transition(status_from: Optional[str], status_to: str) -> None:
    """ステータス遷移を検証する。

    status_from=None は新規追加を表す。
    """
    if status_from is not None:
        validate_status(status_from)
    validate_status(status_to)
    if (status_from, status_to) not in ALLOWED_TRANSITIONS:
        raise ValueError(
            f"invalid transition: {status_from!r} -> {status_to!r} "
            f"(許可されていない遷移)"
        )


def now_iso() -> str:
    """現在時刻を ISO 8601 文字列 (JST) で返す。"""
    return datetime.now(JST).isoformat()


_ACTION_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_as_of(as_of: str) -> None:
    """ポジション取込の基準日を実在する当日以前の YYYY-MM-DD として検証する。"""
    if not isinstance(as_of, str) or not _ACTION_DATE_RE.fullmatch(as_of):
        raise ValueError(f"as_of は YYYY-MM-DD 形式で指定してください: {as_of!r}")
    try:
        parsed = date.fromisoformat(as_of)
    except ValueError as e:
        raise ValueError(f"as_of のパースに失敗: {as_of!r} ({e})") from e
    today = datetime.now(JST).date()
    if parsed > today:
        raise ValueError(
            f"as_of に未来日は指定できません: {as_of} (今日={today.isoformat()})"
        )


def _parse_action_date_to_iso(action_date: str) -> str:
    """YYYY-MM-DD を JST 12:00 の ISO 8601 文字列に変換する。

    issue #220: UI 側 <input type="date"> から受け取る日付を action_log の
    timestamp として保存する。00:00 だと JST 表示時に前日扱いになる縁起問題を
    避けるため固定 12:00 を当てる。

    未来日 (`datetime.now(JST).date()` 基準) は ValueError。
    `ks_util.get_price_day()` の業務日は使わない (17:00 前に前日を返すため、
    実カレンダーの今日入力を誤って弾いてしまう)。
    """
    if not isinstance(action_date, str) or not _ACTION_DATE_RE.match(action_date):
        raise ValueError(
            f"action_date は YYYY-MM-DD 形式で指定してください: {action_date!r}"
        )
    try:
        parsed = datetime.strptime(action_date, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"action_date のパースに失敗: {action_date!r} ({e})") from e
    today = datetime.now(JST).date()
    if parsed > today:
        raise ValueError(
            f"action_date に未来日は指定できません: {action_date} (今日={today.isoformat()})"
        )
    return datetime(parsed.year, parsed.month, parsed.day, 12, 0, 0, tzinfo=JST).isoformat()


# ===========================================
# issue #361: 売買日の営業日正規化 + 終値プロキシ取得
# ===========================================

def _weekday_date(d: date) -> date:
    """土(weekday=5)/日(6)なら直前金曜に丸める。平日はそのまま。

    ks_util.recent_weekday は内部の get_price_day が 17:00 境界で 12:00 の
    timestamp を前日化してしまうため使わず、date だけを見て土日を丸める。
    祝日は考慮しない (終値は _close_on_or_before が直前営業日を返すため概算許容)。
    """
    wd = d.weekday()
    if wd == 5:
        return d - timedelta(days=1)
    if wd == 6:
        return d - timedelta(days=2)
    return d


def _normalize_weekend_iso(iso_timestamp: str) -> str:
    """timestamp の日付が土日なら直前金曜の JST 12:00 ISO 文字列を返す。平日は元のまま。

    timestamp = ユーザーが意図した「売買日」。土日値は入力ミスであり、営業日への
    正規化は改ざんではなく訂正 (issue #361)。新規記録・バックフィルで共有する。
    """
    try:
        d = date.fromisoformat(iso_timestamp[:10])
    except (ValueError, TypeError):
        return iso_timestamp
    fixed = _weekday_date(d)
    if fixed == d:
        return iso_timestamp
    return datetime(fixed.year, fixed.month, fixed.day, 12, 0, 0, tzinfo=JST).isoformat()


def _close_on_or_before(price_log: list, target_dt: date) -> Optional[int]:
    """price_log から target_dt 以下の最新営業日終値を返す。無ければ None。

    price_log: [(date, int終値), ...] (順序非依存)。
    webapp.helpers._split_log_around_kessanbi と同ロジック (逆依存を避け内製)。
    """
    if not price_log:
        return None
    try:
        sorted_log = sorted(price_log, key=lambda x: x[0])  # 昇順
    except (TypeError, IndexError):
        return None
    before: Optional[int] = None
    for entry in sorted_log:
        try:
            entry_dt, entry_pr = entry[0], entry[1]
        except (IndexError, TypeError):
            continue
        if not isinstance(entry_dt, date):
            continue
        if entry_dt <= target_dt:
            before = entry_pr  # 昇順なので最後に上書きされた値が「以下の最新営業日」
    return before


def _fetch_price_proxy(code_s: str, iso_timestamp: str) -> Optional[int]:
    """stocks_shelve の price_log から timestamp 日付以下の最新営業日終値を引く。

    price_log 窓外 (直近30営業日より古い) ・未取得 (当日終値未確定) なら None。
    DB 無し等の例外は握りつぶして None。循環回避のため遅延 import。
    """
    try:
        target_dt = date.fromisoformat(iso_timestamp[:10])
    except (ValueError, TypeError):
        return None
    try:
        from db_shelve import STOCKS_SHELVE
        with ShelveDB(STOCKS_SHELVE) as db:
            rec = db.get(normalize_code_s(code_s))
        price_log = (rec or {}).get("price_log") or []
    except Exception:
        return None
    return _close_on_or_before(price_log, target_dt)


def _needs_price_proxy(action_type: Optional[str], status_to: Optional[str]) -> bool:
    """終値プロキシ付与・土日補正の対象イベントか判定する。

    対象 = 売買日の意味を持つ 1保 (保有開始) / 株数変更 / 売却。
    初回登録(3監)・監視系ステータス変更・ユニバース除外は対象外。
    """
    if status_to == "1保":
        return True
    return action_type in {"株数変更", "売却"}


# ===========================================
# キー組立
# ===========================================

def _record_key(code_s: str) -> str:
    return f"{KEY_RECORD_PREFIX}{code_s}"


def _action_log_key(code_s: str, seq: int) -> str:
    return f"{KEY_ACTION_LOG_PREFIX}{code_s}:{seq:06d}"


def _seq_key(code_s: str) -> str:
    return f"{KEY_SEQ_PREFIX}{code_s}"


def _action_log_prefix_for(code_s: str) -> str:
    return f"{KEY_ACTION_LOG_PREFIX}{code_s}:"


def _fill_key(code_s: str, seq: int) -> str:
    return f"{KEY_FILL_PREFIX}{code_s}:{seq:06d}"


def _fill_seq_key(code_s: str) -> str:
    return f"{KEY_FILL_SEQ_PREFIX}{code_s}"


def _fill_prefix_for(code_s: str) -> str:
    return f"{KEY_FILL_PREFIX}{code_s}:"


# ===========================================
# プロセス間排他制御
# ===========================================

_flock_holder = threading.local()


def _lock_path_for(db_path: Optional[str] = None) -> str:
    base = db_path if db_path is not None else PORTFOLIO_SHELVE
    return base + ".lock"


@contextmanager
def _flock(db_path: Optional[str] = None):
    """portfolio_shelve 書き込み用の排他ロック。

    research_shelve と同じパターン。同一スレッドのリエントラントは深さで管理。
    """
    if getattr(_flock_holder, "depth", 0) > 0:
        _flock_holder.depth += 1
        try:
            yield
        finally:
            _flock_holder.depth -= 1
        return

    lock_file = _lock_path_for(db_path)
    lock_dir = os.path.dirname(lock_file)
    if lock_dir and not os.path.exists(lock_dir):
        os.makedirs(lock_dir, exist_ok=True)
    fd = open(lock_file, "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _flock_holder.depth = 1
        try:
            yield
        finally:
            _flock_holder.depth = 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _resolve_db_path(db_path: Optional[str]) -> str:
    return db_path if db_path is not None else PORTFOLIO_SHELVE


# ===========================================
# ファクトリ
# ===========================================

def create_memo(
    *,
    gyoutai_theme: str = "",
    gyoutai_themes: Optional[List[str]] = None,
    watch_in_reason: str = "",
    trade_idea: str = "",
    inago_origin: str = "",
    takaichi_sensitivity: str = "",
    last_research_update: str = "",
    stage: str = "",
    jukyu_chart: str = "",
) -> Dict[str, Any]:
    """手動メモ dict を生成する。"""
    return {
        "gyoutai_theme": gyoutai_theme,
        "gyoutai_themes": list(gyoutai_themes) if gyoutai_themes else [],
        "watch_in_reason": watch_in_reason,
        "trade_idea": trade_idea,
        "inago_origin": inago_origin,
        "takaichi_sensitivity": takaichi_sensitivity,
        "last_research_update": last_research_update,
        "stage": stage,
        "jukyu_chart": jukyu_chart,
    }


def _normalize_loaded_memo(memo: Any) -> Dict[str, Any]:
    """shelve から読み込んだ memo に新フィールドのデフォルトを補完する。

    旧データに `gyoutai_themes` がない場合 `[]` を埋めて、UI/集計ヘルパーが
    KeyError を踏まないようにする (issue #187 後方互換)。
    """
    if not isinstance(memo, dict):
        return memo
    result = dict(memo)
    if "gyoutai_themes" not in result:
        result["gyoutai_themes"] = []
    return result


def _normalize_loaded_record(record: Any) -> Any:
    """shelve から読み込んだ record の memo / qty を正規化する (issue #187, #269)。"""
    if not isinstance(record, dict):
        return record
    result = dict(record)
    if "memo" in result:
        result["memo"] = _normalize_loaded_memo(result.get("memo"))
    # 旧データに qty が無ければ 0 を補完 (issue #269)
    result.setdefault("qty", 0)
    return result


def create_record(
    code_s: str,
    *,
    status: str = "3監",
    memo: Optional[Dict[str, str]] = None,
    registered_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    qty: int = 0,
) -> Dict[str, Any]:
    """portfolio レコード dict を生成する。

    銘柄名はこのレコードには保存しない (要件 §4: 指標データは保存せず stocks_shelve から
    都度参照する原則を銘柄名にも適用)。

    - code_s は normalize_code_s で大文字化される
    - status はデフォルト "3監" (新規追加用)
    - memo が None なら空メモで埋める
    - registered_at / updated_at が None なら現在時刻を埋める
    - qty: 保有株数 (issue #269、1保 のときのみ意味を持つ、デフォルト 0)
    """
    validate_code_s(code_s)
    normalized_code = normalize_code_s(code_s)
    validate_status(status)
    if memo is None:
        memo = create_memo()
    elif not isinstance(memo, dict):
        raise TypeError(f"memo must be dict, got {type(memo).__name__}")
    _validate_qty(qty)
    timestamp = registered_at or now_iso()
    return {
        "code_s": normalized_code,
        "status": status,
        "registered_at": timestamp,
        "updated_at": updated_at or timestamp,
        "memo": dict(memo),
        "excluded": False,
        "qty": int(qty),
    }


# ===========================================
# CRUD: レコード
# ===========================================

def get_record(
    code_s: str,
    *,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """1銘柄の保有レコードを取得する。存在しなければ None。"""
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        record = db.get(_record_key(normalized))
    return _normalize_loaded_record(record) if record is not None else None


def list_records(
    status: Optional[str] = None,
    *,
    include_excluded: bool = False,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """保有レコードを一覧取得する。

    - status: None で全件、"1保"/"2準"/"3監" 指定で絞り込み
    - include_excluded: False (既定) なら excluded=True のレコードを除外。
      True なら除外フラグ無視で全件返す (DB 整合性チェックや fallback 判定用)
    - 結果は code_s 昇順
    """
    if status is not None:
        validate_status(status)
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_RECORD_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            if status is not None and value.get("status") != status:
                continue
            if not include_excluded and value.get("excluded", False):
                continue
            results.append(_normalize_loaded_record(value))
    results.sort(key=lambda r: r.get("code_s", ""))
    return results


def _next_seq(db: ShelveDB, code_s: str) -> int:
    """指定銘柄の次のアクションログ seq を返し、カウンタを進める。

    db は既にオープン済みの ShelveDB。呼び出し側で flock 保持を前提とする。
    """
    seq_k = _seq_key(code_s)
    current = db.get(seq_k, 0)
    nxt = int(current) + 1
    db[seq_k] = nxt
    return nxt


def append_action_log(
    code_s: str,
    action_type: str,
    *,
    status_from: Optional[str] = None,
    status_to: Optional[str] = None,
    reason: str = "",
    review_memo: str = "",
    qty: Optional[int] = None,
    timestamp: Optional[str] = None,
    source: str = "manual",
    source_detail: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """アクションログを1件追加する。

    内部利用および移行スクリプトからの直接利用を想定。
    transition_status / add_to_watch / delete_record からも呼ばれる。
    レコードを物理削除した後でもログ追記は可能 (ログだけ残す要件のため)。

    source: 反映元 ("manual" 既定 / "csv_import"、issue #397)。

    Returns: 追記したログエントリ
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    validate_action_type(action_type)
    if status_from is not None:
        validate_status(status_from)
    if status_to is not None:
        validate_status(status_to)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")
    if not isinstance(review_memo, str):
        raise TypeError(f"review_memo must be str, got {type(review_memo).__name__}")
    validate_action_source(source)
    if not isinstance(source_detail, str):
        raise TypeError(f"source_detail must be str, got {type(source_detail).__name__}")
    ts = timestamp or now_iso()

    # issue #361: 売買日イベント (1保/株数変更/売却) は終値プロキシを自動付与する。
    # 土日 timestamp は入力ミスとして直前営業日に正規化 (新規記録・バックフィルで統一)。
    extra: Dict[str, Any] = {}
    if _needs_price_proxy(action_type, status_to):
        ts = _normalize_weekend_iso(ts)
        extra["price_proxy"] = _fetch_price_proxy(normalized, ts)
        extra["price_source"] = "close"

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            seq = _next_seq(db, normalized)
            entry = {
                "code_s": normalized,
                "seq": seq,
                "timestamp": ts,
                "action_type": action_type,
                "status_from": status_from,
                "status_to": status_to,
                "reason": reason,
                "review_memo": review_memo,
                "qty": qty,
                "source": source,
                "source_detail": source_detail,
                **extra,
            }
            db[_action_log_key(normalized, seq)] = entry
    log_print(
        "portfolio_shelve: action_log 追記",
        normalized,
        action_type,
        f"seq={seq}",
    )
    return entry


def list_action_logs(
    code_s: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """アクションログを取得する。

    - code_s 指定時はその銘柄のみ、None で全銘柄
    - 結果は (code_s, seq) 昇順
    """
    if code_s is not None:
        validate_code_s(code_s)
        normalized = normalize_code_s(code_s)
        prefix = _action_log_prefix_for(normalized)
    else:
        prefix = KEY_ACTION_LOG_PREFIX
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(prefix):
                continue
            if not isinstance(value, dict):
                continue
            value.setdefault("review_memo", "")
            value.setdefault("qty", None)
            # issue #361: 旧ログの後方互換補完 (物理スキーマ変更なし・マイグレーション不要)
            value.setdefault("price_proxy", None)
            value.setdefault("price_source", None)
            value.setdefault("post_sell_returns", {})
            # issue #397: 旧ログは手入力扱い
            value.setdefault("source", "manual")
            value.setdefault("source_detail", "")
            results.append(value)
    results.sort(
        key=lambda r: (r.get("code_s", ""), r.get("seq", 0)),
    )
    return results


def update_action_log_review_memo(
    code_s: str,
    seq: int,
    review_memo: str,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """アクションログエントリの review_memo フィールドを上書きする (issue #351)。

    - 対象エントリが存在しない場合は KeyError
    - review_memo は str のみ許容
    Returns: 更新後のログエントリ
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(review_memo, str):
        raise TypeError(f"review_memo must be str, got {type(review_memo).__name__}")
    key = _action_log_key(normalized, seq)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            entry = db.get(key)
            if entry is None:
                raise KeyError(f"action_log not found: code_s={code_s!r}, seq={seq}")
            entry["review_memo"] = review_memo
            db[key] = entry
    return entry


def update_action_log_post_sell_returns(
    code_s: str,
    seq: int,
    returns: Dict[str, float],
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """売却ログの確定済み売却後騰落率を保存する (issue #366)。"""
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(returns, dict) or not all(
        key in {"5d", "20d"} and isinstance(value, (int, float))
        for key, value in returns.items()
    ):
        raise TypeError("returns must be a dict of 5d/20d numeric values")
    key = _action_log_key(normalized, seq)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            entry = db.get(key)
            if entry is None:
                raise KeyError(f"action_log not found: code_s={code_s!r}, seq={seq}")
            if entry.get("action_type") != "売却":
                raise ValueError("post_sell_returns can only be saved for sell logs")
            saved = entry.get("post_sell_returns") or {}
            saved.update(returns)
            entry["post_sell_returns"] = saved
            db[key] = entry
    return entry


def backfill_price_proxies(
    *,
    overwrite: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """全 action_log の売買日イベント (1保/株数変更/売却) に終値プロキシを一括付与する。

    同時に土日 timestamp を直前営業日に正規化する (issue #361)。
    終値は既存 DB の price_log (直近30営業日) の範囲のみ。窓外は price_proxy=None のまま。

    overwrite=False: price_proxy が既に非 None のイベントはスキップ (冪等)。None のみ再取得。
    overwrite=True:  price_source != "actual" のイベントを全て再取得。
    実約定 (price_source="actual", #360 Phase2) は overwrite でも timestamp/proxy とも触らない。

    Returns: {"updated", "skipped", "no_price", "date_fixed"}
    """
    path = _resolve_db_path(db_path)
    stats = {"updated": 0, "skipped": 0, "no_price": 0, "date_fixed": 0}
    with _flock(db_path):
        with ShelveDB(path) as db:
            for key in [k for k in db.keys() if k.startswith(KEY_ACTION_LOG_PREFIX)]:
                entry = db[key]
                if not isinstance(entry, dict):
                    continue
                if not _needs_price_proxy(entry.get("action_type"), entry.get("status_to")):
                    continue
                # 実約定は不可侵 (プロキシが実約定に負ける構造)
                if entry.get("price_source") == "actual":
                    stats["skipped"] += 1
                    continue
                # 冪等: overwrite でなければ既に埋まっているものはスキップ
                if not overwrite and entry.get("price_proxy") is not None:
                    stats["skipped"] += 1
                    continue

                ts = entry.get("timestamp", "")
                fixed_ts = _normalize_weekend_iso(ts)
                date_changed = fixed_ts != ts
                proxy = _fetch_price_proxy(entry.get("code_s", ""), fixed_ts)

                if not date_changed and proxy == entry.get("price_proxy") \
                        and entry.get("price_source") == "close":
                    # 変化なし (窓外で None のまま等)
                    if proxy is None:
                        stats["no_price"] += 1
                    else:
                        stats["skipped"] += 1
                    continue

                entry["timestamp"] = fixed_ts
                entry["price_proxy"] = proxy
                entry["price_source"] = "close"
                db[key] = entry
                if date_changed:
                    stats["date_fixed"] += 1
                if proxy is None:
                    stats["no_price"] += 1
                else:
                    stats["updated"] += 1
    log_print(
        "portfolio_shelve: backfill_price_proxies",
        f"updated={stats['updated']}",
        f"skipped={stats['skipped']}",
        f"no_price={stats['no_price']}",
        f"date_fixed={stats['date_fixed']}",
    )
    return stats


# ===========================================
# issue #360 Phase2: fill レイヤー (楽天CSV 由来の約定事実)
# ===========================================

# 楽天 売買区分 → side 正規化テーブル。買付/買建=buy、売付/売埋=sell。
_SIDE_BY_BUY_SELL = {
    "買付": SIDE_BUY,
    "買建": SIDE_BUY,
    "売付": SIDE_SELL,
    "売埋": SIDE_SELL,
}


def normalize_side(baibai_kubun: str) -> str:
    """楽天 売買区分文字列を side ("buy"/"sell") に正規化する。

    未知の区分は ValueError (取込時に弾いて確認リスト行きにする)。
    """
    side = _SIDE_BY_BUY_SELL.get((baibai_kubun or "").strip())
    if side is None:
        raise ValueError(f"未知の売買区分: {baibai_kubun!r}")
    return side


def make_dedup_key(
    *,
    trade_date: str,
    code_s: str,
    trade_kind: str,
    baibai_kubun: str,
    qty: int,
    price: float,
    amount: int,
    occurrence: int,
) -> str:
    """fill の冪等取込用ハッシュを生成する。

    楽天CSVには注文番号列が無いため、行の内容 + 同一CSV内の出現順 (occurrence) で
    一意キーを作る。同日同単価の別注文 (正当な重複) は occurrence 違いで別 fill になり、
    再ダウンロードでは順序が保たれ同じ occurrence になるため冪等。
    """
    raw = "|".join(
        [
            trade_date,
            code_s,
            trade_kind,
            baibai_kubun,
            str(qty),
            f"{price:.4f}",
            str(amount),
            str(occurrence),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def create_fill(
    code_s: str,
    *,
    trade_date: str,
    side: str,
    qty: int,
    price: float,
    amount: int,
    trade_kind: str,
    dedup_key: str,
    broker: Optional[str] = None,
    settle_pl: Optional[int] = None,
    tate_date: Optional[str] = None,
    tate_price: Optional[float] = None,
) -> Dict[str, Any]:
    """fill レコード dict を生成する (バリデーション付き)。

    broker: 取込元証券会社 ("楽天"/"SBI")。settle_pl: 決済損益[円] (SBI 信用返済のみ)。
    tate_date/tate_price: 建約定日/建単価 (楽天 信用返済のみ)。issue #387 Phase4b。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if side not in (SIDE_BUY, SIDE_SELL):
        raise ValueError(f"invalid side: {side!r} (許容値: {SIDE_BUY!r}/{SIDE_SELL!r})")
    if not _ACTION_DATE_RE.match(trade_date):
        raise ValueError(f"trade_date は YYYY-MM-DD 形式で指定してください: {trade_date!r}")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise ValueError(f"qty must be a positive int, got {qty!r}")
    if not isinstance(price, (int, float)) or price <= 0:
        raise ValueError(f"price must be > 0, got {price!r}")
    return {
        "code_s": normalized,
        "seq": None,          # append_fill で採番
        "trade_date": trade_date,
        "side": side,
        "qty": int(qty),
        "price": float(price),
        "amount": int(amount),
        "trade_kind": trade_kind,
        "dedup_key": dedup_key,
        "imported_at": now_iso(),
        "broker": broker,
        "settle_pl": int(settle_pl) if settle_pl is not None else None,
        "tate_date": tate_date,
        "tate_price": float(tate_price) if tate_price is not None else None,
    }


def _next_fill_seq(db: ShelveDB, code_s: str) -> int:
    """指定銘柄の次の fill seq を返し、カウンタを進める (action_log とは別系統)。"""
    seq_k = _fill_seq_key(code_s)
    current = db.get(seq_k, 0)
    nxt = int(current) + 1
    db[seq_k] = nxt
    return nxt


# 再取込時に既存 fill へ後付けで埋めてよいフィールド (dedup_key に含まれず、
# 古い取込では欠けている派生情報)。値が None→非None のときだけ埋める。issue #387 Phase4b
_FILL_BACKFILL_FIELDS = ("tate_date", "tate_price", "settle_pl", "broker")


def append_fill(
    fill: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> tuple:
    """fill を1件追記する (dedup_key で冪等)。

    同一 code_s に同じ dedup_key の fill が既にあればスキップ (取込済み)。ただし
    tate_date/tate_price/settle_pl/broker が既存で None かつ新 fill で埋まっていれば
    既存レコードに後付けする (Phase4b で列を追加したための移行、None→非None のみ)。
    新規なら _fill_seq を採番して保存する。

    Returns: (fill, is_new: bool) — is_new=False は重複スキップ (既存 fill を返す)
    """
    if not isinstance(fill, dict):
        raise TypeError(f"fill must be dict, got {type(fill).__name__}")
    code_s = normalize_code_s(fill["code_s"])
    dedup_key = fill["dedup_key"]
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            prefix = _fill_prefix_for(code_s)
            for key, value in db.items():
                if not key.startswith(prefix):
                    continue
                if isinstance(value, dict) and value.get("dedup_key") == dedup_key:
                    # 既存 fill に欠けた派生情報を後付け (None→非None のみ)
                    updated = False
                    for f in _FILL_BACKFILL_FIELDS:
                        if value.get(f) is None and fill.get(f) is not None:
                            value[f] = fill[f]
                            updated = True
                    if updated:
                        db[key] = value
                    return value, False  # 既に取込済み (冪等)
            seq = _next_fill_seq(db, code_s)
            stored = dict(fill)
            stored["seq"] = seq
            db[_fill_key(code_s, seq)] = stored
    return stored, True


def list_fills(
    code_s: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """fill を取得する。code_s 指定でその銘柄のみ、None で全銘柄。(code_s, seq) 昇順。"""
    if code_s is not None:
        validate_code_s(code_s)
        prefix = _fill_prefix_for(normalize_code_s(code_s))
    else:
        prefix = KEY_FILL_PREFIX
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(prefix):
                continue
            if not isinstance(value, dict):
                continue
            results.append(value)
    results.sort(key=lambda r: (r.get("code_s", ""), r.get("seq", 0)))
    return results


# ===========================================
# fill 建玉ラウンド (エピソード) 単位の振り返りメモ (issue #387 Phase2)
# ===========================================

def fill_episode_key(code_s: str, kind: str, first_seq: int) -> str:
    """fill エピソード (建玉ラウンド) を一意に識別するキーを組み立てる。

    キーは 銘柄+口座種別+ラウンド先頭 fill の seq。first_seq は建玉開始時に
    確定し、その後の買い増し・部分売り・返済・売却で fill が増えても不変。
    これにより:
      - 保有中に付けたメモが売却後 (close_date 確定) も同じキーで追える。
      - 同一銘柄・同一区分で同日に複数回ラウンドトリップしても、各ラウンドの
        先頭 seq は異なるためキーが衝突しない。
    fill の seq は銘柄内で単調増加する固有値 (append_fill が採番)。
    """
    normalized = normalize_code_s(code_s)
    return f"{normalized}|{kind}|{first_seq}"


def _fill_memo_storage_key(episode_key: str) -> str:
    return f"{KEY_FILL_MEMO_PREFIX}{episode_key}"


def get_fill_memo(episode_key: str, *, db_path: Optional[str] = None) -> str:
    """fill エピソードの振り返りメモを取得する。未設定は空文字。"""
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        value = db.get(_fill_memo_storage_key(episode_key))
    if isinstance(value, dict):
        return value.get("review_memo", "") or ""
    return ""


def set_fill_memo(episode_key: str, review_memo: str, *,
                  db_path: Optional[str] = None) -> None:
    """fill エピソードの振り返りメモを上書き保存する。空文字は削除扱い。"""
    if not isinstance(review_memo, str):
        raise TypeError(f"review_memo must be str, got {type(review_memo).__name__}")
    path = _resolve_db_path(db_path)
    storage_key = _fill_memo_storage_key(episode_key)
    with _flock(db_path):
        with ShelveDB(path) as db:
            if review_memo.strip() == "":
                if storage_key in db:
                    del db[storage_key]
            else:
                db[storage_key] = {
                    "episode_key": episode_key,
                    "review_memo": review_memo,
                    "updated_at": now_iso(),
                }
    log_print("portfolio_shelve: fill_memo 更新", episode_key)


def list_fill_memos(*, db_path: Optional[str] = None) -> Dict[str, str]:
    """全 fill エピソードメモを {episode_key: review_memo} で一括取得する。"""
    path = _resolve_db_path(db_path)
    results: Dict[str, str] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_FILL_MEMO_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            memo = value.get("review_memo", "") or ""
            if memo:
                results[value.get("episode_key", key[len(KEY_FILL_MEMO_PREFIX):])] = memo
    return results


# ===========================================
# issue #397: ポートフォリオCSV由来の保有スナップショット (position レイヤー)
# ===========================================

# 期待するソース一式 (broker, kind)。1ファイルがその (broker, kind) の
# 全口座区分を網羅する前提 (楽天現物は実データで確認済み、SBI現物はユーザー確認済み)。
EXPECTED_POSITION_SOURCES = (
    ("楽天", "現物"),
    ("楽天", "信用"),
    ("SBI", "現物"),
    ("SBI", "信用"),
)

POSITION_KINDS = frozenset({"現物", "信用", "信用売建"})


def _position_key(broker: str, account: str, kind: str, code_s: str) -> str:
    return f"{KEY_POSITION_PREFIX}{broker}:{account}:{kind}:{normalize_code_s(code_s)}"


def _position_source_key(broker: str, account: str, kind: str) -> str:
    return f"{KEY_POSITION_SOURCE_PREFIX}{broker}:{account}:{kind}"


def upsert_position(
    broker: str,
    account: str,
    kind: str,
    code_s: str,
    qty: int,
    *,
    avg_price: Optional[float] = None,
    as_of: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """CSV由来の保有残高スナップショットを (broker, account, kind, code_s) 単位で上書き保存する。

    fill と異なり履歴を持たない (最新のみ)。kind="信用売建" は merged_qty の
    集計対象から除外される (issue #397 §2-0: 空売りは検出のみ)。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if kind not in POSITION_KINDS:
        raise ValueError(f"invalid kind: {kind!r} (許容値: {sorted(POSITION_KINDS)})")
    if not isinstance(qty, int) or qty < 0:
        raise ValueError(f"qty must be int >= 0, got {qty!r}")
    validate_as_of(as_of)
    entry = {
        "code_s": normalized,
        "broker": broker,
        "account": account,
        "kind": kind,
        "qty": qty,
        "avg_price": float(avg_price) if avg_price is not None else None,
        "as_of": as_of,
        "imported_at": now_iso(),
    }
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            db[_position_key(broker, account, kind, normalized)] = entry
    return entry


def list_positions(
    code_s: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """position を取得する。code_s 指定でその銘柄のみ、None で全件。"""
    if code_s is not None:
        validate_code_s(code_s)
    normalized = normalize_code_s(code_s) if code_s is not None else None
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_POSITION_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            if normalized is not None and value.get("code_s") != normalized:
                continue
            results.append(value)
    results.sort(key=lambda r: (r.get("code_s", ""), r.get("broker", ""), r.get("account", ""), r.get("kind", "")))
    return results


def delete_positions_for_source(
    broker: str,
    kind: str,
    *,
    db_path: Optional[str] = None,
) -> int:
    """指定 broker/kind の前回 position スナップショットを削除する。"""
    target_kinds = {kind}
    if kind == "信用":
        target_kinds.add("信用売建")
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            keys = [
                key for key, value in db.items()
                if key.startswith(KEY_POSITION_PREFIX)
                and isinstance(value, dict)
                and value.get("broker") == broker
                and value.get("kind") in target_kinds
            ]
            for key in keys:
                del db[key]
    return len(keys)


def delete_position_sources_for_source(
    broker: str,
    kind: str,
    *,
    db_path: Optional[str] = None,
) -> int:
    """指定 broker/kind の前回 position_source メタデータを全口座分削除する。"""
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            keys = [
                key for key, value in db.items()
                if key.startswith(KEY_POSITION_SOURCE_PREFIX)
                and isinstance(value, dict)
                and value.get("broker") == broker
                and value.get("kind") == kind
            ]
            for key in keys:
                del db[key]
    return len(keys)


def upsert_position_source(
    broker: str,
    account: str,
    kind: str,
    *,
    as_of: str,
    row_count: int,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """ソース単位 (broker, account, kind) の取込メタを上書き保存する。

    covered 判定 (§5-2) の材料。銘柄が0件のソース (全部売った口座) も
    row_count=0 で記録できる。
    """
    validate_as_of(as_of)
    entry = {
        "broker": broker,
        "account": account,
        "kind": kind,
        "as_of": as_of,
        "row_count": row_count,
        "imported_at": now_iso(),
    }
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            db[_position_source_key(broker, account, kind)] = entry
    return entry


def list_position_sources(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """全 position_source を取得する。"""
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_POSITION_SOURCE_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            results.append(value)
    results.sort(key=lambda r: (r.get("broker", ""), r.get("account", ""), r.get("kind", "")))
    return results


def compute_merged_qty(
    code_s: str,
    *,
    db_path: Optional[str] = None,
) -> int:
    """指定銘柄の position を合算した総保有株数を返す (issue #397 §5-2)。

    kind="信用売建" (空売り) は合算から除外する。
    """
    total = 0
    for pos in list_positions(code_s, db_path=db_path):
        if pos.get("kind") == "信用売建":
            continue
        total += pos.get("qty", 0)
    return total


def is_covered(
    code_s: str,
    *,
    expected_sources: tuple = EXPECTED_POSITION_SOURCES,
    db_path: Optional[str] = None,
) -> bool:
    """指定銘柄が covered (4ソース全てが取込済み) かどうかを判定する。

    - 期待する (broker, kind) それぞれについて position_source が存在すること
    - 当該銘柄が「信用売建」の position を持つ場合は強制的に False
      (空売りは自動更新対象外、issue #397 §2-0)

    position_source の as_of は一致を要求しない (issue #397 Phase3b:
    楽天だけ今回更新し SBI は前回分を引き継ぐ、といった部分更新を許容するため)。
    位置情報の有無 (position) 自体はソース側に銘柄が無い (=保有ゼロ) 場合も
    正常なので判定に使わない。判定材料は position_source (ソースが取り込まれたか) のみ。
    """
    positions = list_positions(code_s, db_path=db_path)
    if any(p.get("kind") == "信用売建" for p in positions):
        return False

    sources = list_position_sources(db_path=db_path)
    source_map = {(s["broker"], s["kind"]): s for s in sources}
    for broker, kind in expected_sources:
        if (broker, kind) not in source_map:
            return False
    return True


def _pending_in_key(code_s: str) -> str:
    return f"{KEY_PENDING_IN_PREFIX}{normalize_code_s(code_s)}"


def upsert_pending_in(
    code_s: str,
    qty: int,
    as_of: str,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """CSV取込で検出したが trade_idea 未設定の新規保有を保留キューに積む (issue #397 Phase2)。

    同一銘柄は最新の qty/as_of で上書き (複数回の取込で同じ銘柄が検出され続けても重複しない)。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    entry = {
        "code_s": normalized,
        "qty": int(qty),
        "as_of": as_of,
        "detected_at": now_iso(),
    }
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            db[_pending_in_key(normalized)] = entry
    return entry


def list_pending_in(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """保留キュー全件を返す (issue #397 Phase2)。"""
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_PENDING_IN_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            results.append(value)
    results.sort(key=lambda r: r.get("code_s", ""))
    return results


def remove_pending_in(code_s: str, *, db_path: Optional[str] = None) -> bool:
    """保留キューから1件削除する (確定して 1保 に遷移したとき、または CSV側で
    merged_qty が0に戻ったときに呼ぶ)。存在しなければ False。"""
    validate_code_s(code_s)
    key = _pending_in_key(code_s)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            if key not in db:
                return False
            del db[key]
    return True
# 株式分割・併合の換算比率キャッシュ (issue #398)
# ===========================================

def _split_adj_storage_key(code_s: str) -> str:
    return f"{KEY_SPLIT_ADJ_PREFIX}{normalize_code_s(code_s)}"


def get_split_adjustments(code_s: str, *, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """銘柄の分割・併合イベントを ex_date 昇順で返す (未登録なら空リスト)。

    各要素は {"ex_date": "YYYY-MM-DD", "ratio": float}。ratio は新株数/旧株数
    (0.05 = 20株->1株併合、2.0 = 1株->2株分割)。
    """
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        value = db.get(_split_adj_storage_key(code_s))
    if not isinstance(value, dict):
        return []
    events = value.get("events") or []
    return sorted(events, key=lambda e: e["ex_date"])


def list_all_split_adjustments(*, db_path: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """全銘柄の分割・併合イベントを {code_s: [events...]} で一括取得する。

    build_fill_episodes の全銘柄ループで銘柄ごとに DB を開き直す N+1 を避けるため。
    """
    path = _resolve_db_path(db_path)
    results: Dict[str, List[Dict[str, Any]]] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_SPLIT_ADJ_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            events = value.get("events") or []
            if events:
                code_s = value.get("code_s", key[len(KEY_SPLIT_ADJ_PREFIX):])
                results[code_s] = sorted(events, key=lambda e: e["ex_date"])
    return results


def add_split_adjustment(code_s: str, ex_date: str, ratio: float, *,
                         db_path: Optional[str] = None) -> Dict[str, Any]:
    """分割・併合イベントを1件追加する (同一 ex_date は上書き、dedup)。

    登録すると同銘柄の split_pending_review (未登録の疑いマーク) も解除する。
    """
    if not _ACTION_DATE_RE.match(ex_date):
        raise ValueError(f"ex_date は YYYY-MM-DD 形式で指定してください: {ex_date!r}")
    if not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
        raise ValueError(f"ratio must be > 0, got {ratio!r}")
    path = _resolve_db_path(db_path)
    storage_key = _split_adj_storage_key(code_s)
    pending_key = _split_pending_review_storage_key(code_s)
    with _flock(db_path):
        with ShelveDB(path) as db:
            value = db.get(storage_key)
            events = list(value.get("events", [])) if isinstance(value, dict) else []
            events = [e for e in events if e["ex_date"] != ex_date]
            events.append({"ex_date": ex_date, "ratio": float(ratio)})
            events.sort(key=lambda e: e["ex_date"])
            stored = {
                "code_s": normalize_code_s(code_s),
                "events": events,
                "updated_at": now_iso(),
                "source": "yfinance",
            }
            db[storage_key] = stored
            # pending は銘柄単位ではなくイベント日単位で解除する。同一銘柄に複数の
            # 未登録イベントがある状態で1件だけ登録した場合、残りのイベントの
            # pending フラグは消さない (PRレビュー #405 P1 対応)。"unknown" は
            # yfinance 取得失敗時に ex_date 不明のまま積まれたマーカーで、以後
            # ex_date が判明して登録できた時点で「不明だった」状態は解消したとみなし
            # 合わせて解除する (PRレビュー対応: unknown は ex_date と一致しないため
            # 残り続け、以後も保有中エピソードの残高・損益が非表示のままになる)。
            pending_value = db.get(pending_key)
            if isinstance(pending_value, dict):
                remaining = [d for d in pending_value.get("ex_dates", [])
                            if d != ex_date and d != "unknown"]
                if remaining:
                    pending_value["ex_dates"] = remaining
                    db[pending_key] = pending_value
                else:
                    del db[pending_key]
    log_print("portfolio_shelve: split_adj 登録", code_s, ex_date, ratio)
    return stored


def _split_pending_review_storage_key(code_s: str) -> str:
    return f"{KEY_SPLIT_PENDING_REVIEW_PREFIX}{normalize_code_s(code_s)}"


def _split_rejected_review_storage_key(code_s: str) -> str:
    return f"{KEY_SPLIT_REJECTED_REVIEW_PREFIX}{normalize_code_s(code_s)}"


def mark_split_pending_review(code_s: str, *, reason: str, ex_date: Optional[str] = None,
                              db_path: Optional[str] = None) -> None:
    """--check-splits の (a)/(b) 検知結果を、webapp からも見える形で残す。

    build_fill_episodes は yfinance を呼ばないため、単価ジャンプが無く
    エピソード期間総当たりチェックでのみ見つかったケース (9252 相当) を検知できない。
    この拒否リストに載せておけば、次回 webapp 表示時にも split_suspect を
    付与できる (yfinance 呼び出しなし)。

    ex_date (yfinance が示す権利落ち日) が分かれば ex_dates リストに積む。
    add_split_adjustment はイベント単位で ex_dates から解除するため、同一銘柄に
    複数の未登録イベントがあっても、登録済みの日付だけが解除される
    (PRレビュー #405 P1 対応: 銘柄単位で丸ごと解除すると、他の未登録イベントの
    警告が消えてしまう)。ex_date 不明 (単価ジャンプ検知は yfinance 未参照でも
    呼ばれる) の場合は "unknown" として積み、build_fill_episodes 側は
    ex_dates の有無に関わらず銘柄が pending なら警告する (安全側)。
    """
    path = _resolve_db_path(db_path)
    pending_key = _split_pending_review_storage_key(code_s)
    with _flock(db_path):
        with ShelveDB(path) as db:
            value = db.get(pending_key)
            ex_dates = list(value.get("ex_dates", [])) if isinstance(value, dict) else []
            marker = ex_date or "unknown"
            if marker not in ex_dates:
                ex_dates.append(marker)
            db[pending_key] = {
                "code_s": normalize_code_s(code_s),
                "reason": reason,
                "ex_dates": ex_dates,
                "marked_at": now_iso(),
            }


def mark_split_rejected_review(code_s: str, *, reason: str, ex_date: Optional[str] = None,
                               db_path: Optional[str] = None) -> None:
    """分割・併合ではないと判断した検知結果を再検出抑止用に保存する。"""
    if ex_date is not None and ex_date != "unknown" and not _ACTION_DATE_RE.match(ex_date):
        raise ValueError(f"ex_date は YYYY-MM-DD 形式で指定してください: {ex_date!r}")
    path = _resolve_db_path(db_path)
    rejected_key = _split_rejected_review_storage_key(code_s)
    with _flock(db_path):
        with ShelveDB(path) as db:
            value = db.get(rejected_key)
            ex_dates = list(value.get("ex_dates", [])) if isinstance(value, dict) else []
            marker = ex_date or "unknown"
            if marker not in ex_dates:
                ex_dates.append(marker)
            db[rejected_key] = {
                "code_s": normalize_code_s(code_s),
                "reason": reason,
                "ex_dates": ex_dates,
                "marked_at": now_iso(),
            }


def clear_split_pending_review(code_s: str, *, ex_date: Optional[str] = None,
                               db_path: Optional[str] = None) -> bool:
    """split_pending_review を手動解除する。

    ex_date 指定時はその日付だけを解除し、未指定なら同銘柄の pending を全解除する。
    分割・併合ではないと判断した誤検知を解除するための操作。
    """
    if ex_date is not None and ex_date != "unknown" and not _ACTION_DATE_RE.match(ex_date):
        raise ValueError(f"ex_date は YYYY-MM-DD 形式で指定してください: {ex_date!r}")
    path = _resolve_db_path(db_path)
    pending_key = _split_pending_review_storage_key(code_s)
    with _flock(db_path):
        with ShelveDB(path) as db:
            value = db.get(pending_key)
            if not isinstance(value, dict):
                return False
            if ex_date is None:
                del db[pending_key]
                return True
            remaining = [d for d in value.get("ex_dates", []) if d != ex_date]
            if len(remaining) == len(value.get("ex_dates", [])):
                return False
            if remaining:
                value["ex_dates"] = remaining
                db[pending_key] = value
            else:
                del db[pending_key]
            return True


def reject_split_pending_review(code_s: str, *, ex_date: Optional[str] = None,
                                reason: str = "分割ではないと判断",
                                db_path: Optional[str] = None) -> bool:
    """pending_review を却下し、同じイベントを再検出しないよう保存する。"""
    if ex_date is not None:
        mark_split_rejected_review(code_s, reason=reason, ex_date=ex_date, db_path=db_path)
        clear_split_pending_review(code_s, ex_date=ex_date, db_path=db_path)
        return True

    pending_events = list_pending_review_events(db_path=db_path).get(normalize_code_s(code_s), [])
    if pending_events:
        for marker in pending_events:
            mark_split_rejected_review(code_s, reason=reason, ex_date=marker, db_path=db_path)
    else:
        mark_split_rejected_review(code_s, reason=reason, db_path=db_path)
        return True
    return clear_split_pending_review(code_s, db_path=db_path)


def list_pending_review_codes(*, db_path: Optional[str] = None) -> List[str]:
    """split_pending_review が付いている銘柄コードの一覧を返す。"""
    path = _resolve_db_path(db_path)
    codes = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if key.startswith(KEY_SPLIT_PENDING_REVIEW_PREFIX) and isinstance(value, dict):
                codes.append(value.get("code_s", key[len(KEY_SPLIT_PENDING_REVIEW_PREFIX):]))
    return codes


def list_pending_review_events(*, db_path: Optional[str] = None) -> Dict[str, List[str]]:
    """split_pending_review の未登録イベント日を銘柄別に返す。

    ex_date 不明時の "unknown" もそのまま含める。呼び出し側は日付範囲で
    絞れるものだけ絞り、不明分は従来どおり安全側で扱う。
    """
    path = _resolve_db_path(db_path)
    results: Dict[str, List[str]] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_SPLIT_PENDING_REVIEW_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            code_s = value.get("code_s", key[len(KEY_SPLIT_PENDING_REVIEW_PREFIX):])
            results[code_s] = list(value.get("ex_dates", []))
    return results


def list_rejected_review_events(*, db_path: Optional[str] = None) -> Dict[str, List[str]]:
    """却下済みの分割・併合検知イベント日を銘柄別に返す。"""
    path = _resolve_db_path(db_path)
    results: Dict[str, List[str]] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_SPLIT_REJECTED_REVIEW_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            code_s = value.get("code_s", key[len(KEY_SPLIT_REJECTED_REVIEW_PREFIX):])
            results[code_s] = list(value.get("ex_dates", []))
    return results


# ===========================================
# 高レベル操作
# ===========================================

def add_to_watch(
    code_s: str,
    *,
    memo: Optional[Dict[str, str]] = None,
    reason: str = "",
    action_date: Optional[str] = None,
    source: str = "manual",
    source_detail: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """銘柄を 3監 として登録、または除外済みレコードをユニバース復活させる。

    銘柄名は持たない (表示時に stocks_shelve / research_shelve から都度取得)。

    挙動:
    - 既存レコードなし → 新規追加 (3監)。「初回登録」ログを reason 引数で記録
    - 既存レコードあり & excluded=True → 復活 (excluded=False に戻す)。
      memo / status は既存値を保持。「ユニバース除外」ログを reason="復活" で記録
      (復活時は reason 引数は無視される)
    - 既存レコードあり & excluded=False → ValueError (重複登録防止)
    - action_date (YYYY-MM-DD) を指定すると、action_log の timestamp を
      その日の JST 12:00 に固定する (issue #220)。未指定なら現在時刻
    - source: 反映元 ("manual" 既定 / "csv_import"、issue #397)

    Returns: 追加または復活したレコード
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    action_ts = _parse_action_date_to_iso(action_date) if action_date else None
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            existing = db.get(key)
            if existing is not None and isinstance(existing, dict):
                if existing.get("excluded", False):
                    existing["excluded"] = False
                    existing["updated_at"] = now_iso()
                    db[key] = existing
                    revived_record = existing
                    revived = True
                else:
                    raise ValueError(
                        f"portfolio_shelve: {normalized} は既に登録済みです"
                    )
            else:
                record = create_record(normalized, status="3監", memo=memo)
                db[key] = record
                revived_record = record
                revived = False
        if revived:
            # 復活時は明示的に reason="復活" を記録 (除外ログとの判別用、reason 引数は無視)
            append_action_log(
                normalized,
                "ユニバース除外",
                reason="復活",
                timestamp=action_ts,
                source=source,
                source_detail=source_detail,
                db_path=db_path,
            )
            log_print("portfolio_shelve: ユニバース復活", normalized)
        else:
            append_action_log(
                normalized,
                "初回登録",
                status_from=None,
                status_to="3監",
                reason=reason,
                timestamp=action_ts,
                source=source,
                source_detail=source_detail,
                db_path=db_path,
            )
            log_print("portfolio_shelve: 3監 追加", normalized)
    return revived_record


def upsert_record(
    record: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> None:
    """レコードを追加または上書きする (移行スクリプト用)。

    add_to_watch / transition_status と異なりアクションログは追記しない。
    呼び出し側で必要なら append_action_log を別途呼ぶこと。
    """
    if not isinstance(record, dict):
        raise TypeError(f"record must be dict, got {type(record).__name__}")
    if "code_s" not in record:
        raise ValueError("record に code_s が必須です")
    validate_code_s(record["code_s"])
    if "status" in record:
        validate_status(record["status"])
    normalized = normalize_code_s(record["code_s"])
    stored = dict(record)
    stored["code_s"] = normalized

    unknown = set(stored.keys()) - RECORD_FIELDS - LEGACY_RECORD_FIELDS
    if unknown:
        log_warning(
            "portfolio_shelve: 未知のレコードフィールドを保存します:",
            sorted(unknown),
        )

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            existed = _record_key(normalized) in db
            db[_record_key(normalized)] = stored
    if existed:
        log_print("portfolio_shelve: レコード更新", normalized)
    else:
        log_print("portfolio_shelve: レコード追加", normalized)


def update_qty(
    code_s: str,
    qty: int,
    *,
    reason: str = "",
    action_date: Optional[str] = None,
    log_action: bool = True,
    source: str = "manual",
    source_detail: str = "",
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """既存レコードの保有株数を更新する (issue #269)。

    - 差分なしは no-op で early return (戻り値も現レコード)
    - レコード未登録は KeyError
    - 不正値 (非整数 / 負数) は TypeError / ValueError
    - action_date (YYYY-MM-DD) を指定すると、action_log の timestamp を
      JST 12:00 の ISO 8601 文字列に変換して記録する
    - log_action=False のときは action_log を追記しない (1保遷移の初回セット用)
    - source: 反映元 ("manual" 既定 / "csv_import"、issue #397)

    Returns: 更新後のレコード。差分なしの場合は現レコードをそのまま返す。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    _validate_qty(qty)
    qty_int = int(qty)
    action_ts = _parse_action_date_to_iso(action_date) if action_date else None

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                raise KeyError(
                    f"portfolio_shelve: {normalized} はレコード未登録です"
                )
            record = db[key]
            current_qty = record.get("qty", 0)
            if current_qty == qty_int:
                return _normalize_loaded_record(record)
            record["qty"] = qty_int
            db[key] = record
            db[KEY_QTY_GLOBAL_UPDATED_AT] = now_iso()
    log_print("portfolio_shelve: 株数更新", normalized, f"{current_qty} -> {qty_int}")
    if log_action:
        append_action_log(
            code_s,
            "株数変更",
            reason=f"{current_qty} → {qty_int}" + (f" ({reason})" if reason else ""),
            timestamp=action_ts,
            source=source,
            source_detail=source_detail,
            db_path=db_path,
        )
    return _normalize_loaded_record(record)


def get_qty_global_updated_at(
    *,
    db_path: Optional[str] = None,
) -> Optional[str]:
    """PF 全体で最後に qty が変化した ISO 8601 タイムスタンプを返す。

    どの銘柄でも qty が変化していない (初期状態) なら None。
    保有銘柄リストの更新タイミングを「PF 全体で 1 つの時刻」として可視化するためのメタ情報。
    """
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        return db.get(KEY_QTY_GLOBAL_UPDATED_AT)


def transition_status(
    code_s: str,
    new_status: str,
    *,
    reason: str = "",
    action_date: Optional[str] = None,
    qty: Optional[int] = None,
    source: str = "manual",
    source_detail: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """既存レコードのステータスを変更する。

    - 1保 -> 2準 は内部的に「売却」として action_type=売却 で記録
    - それ以外の遷移は action_type=ステータス変更
    - 遷移バリデーション (ALLOWED_TRANSITIONS) を満たさなければ ValueError
    - レコードが存在しない場合は KeyError
    - action_date (YYYY-MM-DD) を指定すると、action_log の timestamp を
      その日の JST 12:00 に固定する (issue #220)。未指定なら現在時刻
    - qty: 1保遷移時のIN株数をログに記録する (issue #357)。売却時は record["qty"] を使用
    - source: 反映元 ("manual" 既定 / "csv_import"、issue #397)

    Returns: 更新後のレコード
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    validate_status(new_status)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    action_ts = _parse_action_date_to_iso(action_date) if action_date else None

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                raise KeyError(
                    f"portfolio_shelve: {normalized} はレコード未登録です"
                )
            record = db[key]
            old_status = record.get("status")
            if old_status == new_status:
                # 同一ステータスへの遷移は no-op (バリデーション前に判定)
                log_print(
                    "portfolio_shelve: 同一ステータスのため遷移スキップ",
                    normalized,
                    new_status,
                )
                return record
            validate_transition(old_status, new_status)
            record["status"] = new_status
            record["updated_at"] = now_iso()
            db[key] = record
        # アクションログ種別: 1保→2準 は売却、それ以外はステータス変更
        action_type = "売却" if old_status == "1保" and new_status == "2準" else "ステータス変更"
        # 1保遷移は引数 qty を優先（update_qty より先に呼ばれるため record["qty"] は旧値）
        # 売却時は record["qty"]（保有株数）をログに記録 (issue #357)
        if new_status == "1保":
            log_qty = qty
        elif new_status == "2準":
            log_qty = record.get("qty")
        else:
            log_qty = None
        # 売却時: 直前の1保ログに入力済みの振り返りメモを売却ログへ引き継ぐ
        inherited_memo = ""
        if action_type == "売却":
            hold_logs = list_action_logs(normalized, db_path=db_path)
            hold_log = next(
                (l for l in reversed(hold_logs) if l.get("status_to") == "1保"),
                None,
            )
            if hold_log:
                inherited_memo = hold_log.get("review_memo", "")
        append_action_log(
            normalized,
            action_type,
            status_from=old_status,
            status_to=new_status,
            reason=reason,
            review_memo=inherited_memo,
            qty=log_qty,
            timestamp=action_ts,
            source=source,
            source_detail=source_detail,
            db_path=db_path,
        )
        if new_status == "1保":
            remove_pending_in(normalized, db_path=db_path)
    log_print(
        "portfolio_shelve: ステータス変更",
        normalized,
        f"{old_status} -> {new_status}",
    )
    return record


def delete_record(
    code_s: str,
    *,
    reason: str = "",
    db_path: Optional[str] = None,
) -> bool:
    """レコードを物理削除する (3監 のみ可能)。

    - 1保/2準 を削除しようとすると ValueError
    - レコードが存在しない場合は False を返す
    - 削除に成功した場合は アクションログ「削除」を 1 件記録 (ログは残る)

    Returns: 削除に成功すれば True、未存在なら False
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                return False
            record = db[key]
            current_status = record.get("status")
            if current_status != "3監":
                raise ValueError(
                    f"portfolio_shelve: {normalized} は status={current_status!r} のため "
                    "削除できません (3監 のみ削除可能、先に 3監 へ遷移してください)"
                )
            del db[key]
        append_action_log(
            normalized,
            "削除",
            status_from="3監",
            status_to=None,
            reason=reason,
            db_path=db_path,
        )
    log_print("portfolio_shelve: レコード削除", normalized)
    return True


EXCLUDABLE_STATUSES = frozenset({"2準", "3監"})


def exclude_from_universe(
    code_s: str,
    *,
    reason: str = "",
    db_path: Optional[str] = None,
) -> bool:
    """2準/3監 レコードをユニバースから除外する (物理削除はしない)。

    - 1保 を除外しようとすると ValueError (保有中銘柄の誤除外を防ぐ)
    - レコードが存在しない場合は False を返す
    - 既に除外済みなら no-op で False を返す
    - 成功時はアクションログ「ユニバース除外」を 1 件記録

    Returns: 除外を新規に行った場合 True、未存在/既除外なら False
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                return False
            record = db[key]
            if record.get("excluded", False):
                return False
            current_status = record.get("status")
            if current_status not in EXCLUDABLE_STATUSES:
                raise ValueError(
                    f"portfolio_shelve: {normalized} は status={current_status!r} のため "
                    "ユニバース除外できません (2準/3監 のみ除外可能)"
                )
            record["excluded"] = True
            record["updated_at"] = now_iso()
            db[key] = record
        append_action_log(
            normalized,
            "ユニバース除外",
            reason=reason,
            db_path=db_path,
        )
    log_print("portfolio_shelve: ユニバース除外", normalized)
    return True


# ===========================================
# テーママスター (issue #282)
# ===========================================

def _theme_key(name: str) -> str:
    return f"{KEY_THEME_PREFIX}{name}"


def validate_theme_name(name: Any) -> str:
    """テーマ name を検証して正規化済み name を返す。

    - str でない → TypeError
    - strip() 後に空 → ValueError
    - 長さ > THEME_NAME_MAX_LEN → ValueError
    - URL 禁止文字 (/ ? # & % + 制御文字) を含む → ValueError

    正規化: strip() のみ (大文字小文字は維持、内部空白も維持)
    """
    if not isinstance(name, str):
        raise TypeError(f"theme name must be str, got {type(name).__name__}")
    normalized = name.strip()
    if not normalized:
        raise ValueError("theme name は空にできません")
    if len(normalized) > THEME_NAME_MAX_LEN:
        raise ValueError(
            f"theme name は {THEME_NAME_MAX_LEN} 文字以内: {name!r} ({len(normalized)} 文字)"
        )
    m = _THEME_NAME_FORBIDDEN_RE.search(normalized)
    if m:
        raise ValueError(
            f"theme name に使用できない文字が含まれています: {name!r} (禁止: / ? # & % + 制御文字)"
        )
    return normalized


def _validate_theme_description(description: Any) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise TypeError(
            f"theme description must be str or None, got {type(description).__name__}"
        )
    return description


def list_themes(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """テーママスターを name 昇順で取得する。"""
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_THEME_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            results.append(dict(value))
    results.sort(key=lambda r: r.get("name", ""))
    return results


def get_theme(name: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """1件取得 (存在しなければ None)。"""
    normalized = validate_theme_name(name)
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        value = db.get(_theme_key(normalized))
    return dict(value) if isinstance(value, dict) else None


def create_theme(
    name: str,
    description: str = "",
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """テーマを新規作成する。重複は ValueError。"""
    normalized = validate_theme_name(name)
    desc = _validate_theme_description(description)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _theme_key(normalized)
            if key in db:
                raise ValueError(f"theme {normalized!r} は既に存在します")
            record = {
                "name": normalized,
                "description": desc,
                "created_at": now_iso(),
            }
            db[key] = record
    log_print("portfolio_shelve: theme 作成", normalized)
    return dict(record)


def update_theme(
    name: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """テーマを更新する (リネーム / 説明文編集)。

    - new_name が現行と異なれば旧キー削除 + 新キー作成 + 全 record の memo[gyoutai_themes] 書き換え
    - description が None でなければその値で上書き
    - 同 _flock + 同 ShelveDB セッション内で完結 (途中失敗で不整合を残さない)

    戻り値: 更新後の theme レコード
    """
    normalized = validate_theme_name(name)
    new_normalized = validate_theme_name(new_name) if new_name is not None else None
    desc_value = _validate_theme_description(description) if description is not None else None

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            old_key = _theme_key(normalized)
            if old_key not in db:
                raise KeyError(f"theme {normalized!r} は存在しません")
            current = dict(db[old_key])

            renaming = new_normalized is not None and new_normalized != normalized
            if renaming:
                new_key = _theme_key(new_normalized)
                if new_key in db:
                    raise ValueError(f"theme {new_normalized!r} は既に存在します")
                current["name"] = new_normalized
            if desc_value is not None:
                current["description"] = desc_value

            if renaming:
                # 旧キー削除 + 新キー作成 + 銘柄側書き換え
                del db[old_key]
                db[new_key] = current
                affected_codes = _rewrite_theme_in_records(
                    db, normalized, new_normalized
                )
            else:
                db[old_key] = current
                affected_codes = []

    if renaming:
        log_print(
            "portfolio_shelve: theme リネーム",
            f"{normalized} -> {new_normalized}",
            f"affected={len(affected_codes)}",
        )
    else:
        log_print("portfolio_shelve: theme 更新", normalized)
    return dict(current)


def delete_theme(name: str, *, db_path: Optional[str] = None) -> int:
    """テーマを削除する。全 record から該当 name を除去。

    戻り値: 影響を受けた銘柄数
    """
    normalized = validate_theme_name(name)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _theme_key(normalized)
            if key not in db:
                raise KeyError(f"theme {normalized!r} は存在しません")
            del db[key]
            affected_codes = _rewrite_theme_in_records(db, normalized, None)
    log_print(
        "portfolio_shelve: theme 削除",
        normalized,
        f"affected={len(affected_codes)}",
    )
    return len(affected_codes)


def count_theme_usage(*, db_path: Optional[str] = None) -> Dict[str, int]:
    """name -> 使用銘柄数 を返す (編集画面表示用)。"""
    path = _resolve_db_path(db_path)
    counts: Dict[str, int] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_RECORD_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            memo = value.get("memo") or {}
            for theme in (memo.get("gyoutai_themes") or []):
                if isinstance(theme, str) and theme:
                    counts[theme] = counts.get(theme, 0) + 1
    return counts


def _rewrite_theme_in_records(
    db: ShelveDB,
    old_name: str,
    new_name: Optional[str],
) -> List[str]:
    """全 record:* を走査し、memo[gyoutai_themes] 内の old_name を new_name に置換する。

    - new_name=None なら除去 (削除)
    - new_name 指定で同一スロットに新 name が既に存在する場合は重複除去
    - 変更があった record は updated_at を now_iso() に更新
    - 戻り値: 変更があった code_s のリスト (action_log 追記用)

    呼び出し側で _flock + ShelveDB セッションを保持していること前提。
    """
    affected: List[str] = []
    record_keys = [k for k in db.keys() if k.startswith(KEY_RECORD_PREFIX)]
    for key in record_keys:
        record = db[key]
        if not isinstance(record, dict):
            continue
        memo = record.get("memo") or {}
        themes = memo.get("gyoutai_themes") or []
        if not isinstance(themes, list) or old_name not in themes:
            continue
        new_themes: List[str] = []
        for t in themes:
            if t == old_name:
                if new_name is None:
                    continue
                if new_name in new_themes:
                    continue  # リネーム後に重複する場合は除去
                new_themes.append(new_name)
            else:
                if t in new_themes:
                    continue
                new_themes.append(t)
        if new_themes == list(themes):
            continue
        new_memo = dict(memo)
        new_memo["gyoutai_themes"] = new_themes
        new_record = dict(record)
        new_record["memo"] = new_memo
        new_record["updated_at"] = now_iso()
        db[key] = new_record
        affected.append(record.get("code_s") or key[len(KEY_RECORD_PREFIX):])
    return affected


# ===========================================
# 戦略マスター CRUD (issue #335)
# ===========================================

def _trade_idea_key(name: str) -> str:
    return f"{KEY_TRADE_IDEA_PREFIX}{name}"


def validate_strategy_name(name: Any) -> str:
    """戦略 name を検証して正規化済み name を返す (validate_theme_name と同じルール)。"""
    try:
        return validate_theme_name(name)
    except TypeError as e:
        raise TypeError(str(e).replace("theme name", "strategy name")) from e
    except ValueError as e:
        raise ValueError(str(e).replace("theme name", "strategy name")) from e


def list_trade_ideas(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """戦略マスターを name 昇順で取得する。"""
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_TRADE_IDEA_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            results.append(dict(value))
    results.sort(key=lambda r: r.get("name", ""))
    return results


def get_trade_idea(name: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """1件取得 (存在しなければ None)。"""
    normalized = validate_strategy_name(name)
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        value = db.get(_trade_idea_key(normalized))
    return dict(value) if isinstance(value, dict) else None


def _validate_trade_idea_fields(
    time_horizon: Any,
    over_earnings: Any,
) -> tuple:
    """time_horizon / over_earnings の型・値チェック。正規化後の (time_horizon, over_earnings) を返す。"""
    if time_horizon is None:
        time_horizon = ""
    if not isinstance(time_horizon, str):
        raise TypeError(f"time_horizon must be str or None, got {type(time_horizon).__name__}")
    if time_horizon not in VALID_TIME_HORIZONS:
        raise ValueError(f"time_horizon {time_horizon!r} は無効です (許容値: {list(VALID_TIME_HORIZONS)})")
    if over_earnings is None:
        over_earnings = False
    if not isinstance(over_earnings, bool):
        raise TypeError(f"over_earnings must be bool or None, got {type(over_earnings).__name__}")
    return time_horizon, over_earnings


def create_trade_idea(
    name: str,
    description: str = "",
    time_horizon: str = "",
    over_earnings: bool = False,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """戦略を新規作成する。重複は ValueError。"""
    normalized = validate_strategy_name(name)
    desc = _validate_theme_description(description)
    th, oe = _validate_trade_idea_fields(time_horizon, over_earnings)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _trade_idea_key(normalized)
            if key in db:
                raise ValueError(f"strategy {normalized!r} は既に存在します")
            record = {
                "name": normalized,
                "description": desc,
                "time_horizon": th,
                "over_earnings": oe,
                "created_at": now_iso(),
            }
            db[key] = record
    log_print("portfolio_shelve: strategy 作成", normalized)
    return dict(record)


def update_trade_idea(
    name: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    time_horizon: Optional[str] = None,
    over_earnings: Optional[bool] = None,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """戦略を更新する (リネーム / 説明文 / 時間軸 / 決算またぎ編集)。

    - new_name が現行と異なれば旧キー削除 + 新キー作成 + 全 record の memo[trade_idea] 書き換え
    - 同 _flock + 同 ShelveDB セッション内で完結

    戻り値: 更新後の strategy レコード
    """
    normalized = validate_strategy_name(name)
    new_normalized = validate_strategy_name(new_name) if new_name is not None else None
    desc_value = _validate_theme_description(description) if description is not None else None
    if time_horizon is not None or over_earnings is not None:
        th_value, oe_value = _validate_trade_idea_fields(
            time_horizon if time_horizon is not None else "",
            over_earnings if over_earnings is not None else False,
        )
    else:
        th_value, oe_value = None, None

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            old_key = _trade_idea_key(normalized)
            if old_key not in db:
                raise KeyError(f"strategy {normalized!r} は存在しません")
            current = dict(db[old_key])

            renaming = new_normalized is not None and new_normalized != normalized
            if renaming:
                new_key = _trade_idea_key(new_normalized)
                if new_key in db:
                    raise ValueError(f"strategy {new_normalized!r} は既に存在します")
                current["name"] = new_normalized
            if desc_value is not None:
                current["description"] = desc_value
            if time_horizon is not None:
                current["time_horizon"] = th_value
            if over_earnings is not None:
                current["over_earnings"] = oe_value

            if renaming:
                del db[old_key]
                db[new_key] = current
                affected_codes = _rewrite_trade_idea_in_records(db, normalized, new_normalized)
            else:
                db[old_key] = current
                affected_codes = []

    if renaming:
        log_print(
            "portfolio_shelve: strategy リネーム",
            f"{normalized} -> {new_normalized}",
            f"affected={len(affected_codes)}",
        )
    else:
        log_print("portfolio_shelve: strategy 更新", normalized)
    return dict(current)


def delete_trade_idea(name: str, *, db_path: Optional[str] = None) -> int:
    """戦略を削除する。全 record の memo[trade_idea] を空文字にリセット。

    戻り値: 影響を受けた銘柄数
    """
    normalized = validate_strategy_name(name)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _trade_idea_key(normalized)
            if key not in db:
                raise KeyError(f"strategy {normalized!r} は存在しません")
            del db[key]
            affected_codes = _rewrite_trade_idea_in_records(db, normalized, None)
    log_print(
        "portfolio_shelve: strategy 削除",
        normalized,
        f"affected={len(affected_codes)}",
    )
    return len(affected_codes)


def count_trade_idea_usage(*, db_path: Optional[str] = None) -> Dict[str, int]:
    """name -> 使用銘柄数 を返す (編集画面表示用)。"""
    path = _resolve_db_path(db_path)
    counts: Dict[str, int] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_RECORD_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            memo = value.get("memo") or {}
            idea = memo.get("trade_idea") or ""
            if idea:
                counts[idea] = counts.get(idea, 0) + 1
    return counts


def seed_trade_ideas(*, db_path: Optional[str] = None) -> int:
    """戦略マスターが空のときのみシードデータを投入する（冪等）。投入数を返す。"""
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            existing = [k for k in db.keys() if k.startswith(KEY_TRADE_IDEA_PREFIX)]
            if existing:
                return 0
            for seed in _TRADE_IDEA_SEED:
                record = {
                    "name": seed["name"],
                    "description": seed["description"],
                    "time_horizon": seed["time_horizon"],
                    "over_earnings": seed["over_earnings"],
                    "created_at": now_iso(),
                }
                db[_trade_idea_key(seed["name"])] = record
    log_print("portfolio_shelve: strategy シード投入", f"{len(_TRADE_IDEA_SEED)} 件")
    return len(_TRADE_IDEA_SEED)


def _rewrite_trade_idea_in_records(
    db: ShelveDB,
    old_name: str,
    new_name: Optional[str],
) -> List[str]:
    """全 record:* を走査し、memo[trade_idea] が old_name の銘柄を new_name に書き換える。

    - new_name=None なら空文字にリセット (削除時)
    - 変更があった record は updated_at を更新
    - 戻り値: 変更があった code_s のリスト

    呼び出し側で _flock + ShelveDB セッションを保持していること前提。
    """
    affected: List[str] = []
    record_keys = [k for k in db.keys() if k.startswith(KEY_RECORD_PREFIX)]
    for key in record_keys:
        record = db[key]
        if not isinstance(record, dict):
            continue
        memo = record.get("memo") or {}
        if memo.get("trade_idea") != old_name:
            continue
        new_memo = dict(memo)
        new_memo["trade_idea"] = new_name if new_name is not None else ""
        new_record = dict(record)
        new_record["memo"] = new_memo
        new_record["updated_at"] = now_iso()
        db[key] = new_record
        affected.append(record.get("code_s") or key[len(KEY_RECORD_PREFIX):])
    return affected


def _append_action_log_inner(
    db: ShelveDB,
    code_s: str,
    action_type: str,
    *,
    reason: str = "",
) -> None:
    """既にオープン済みの ShelveDB に action_log を直接書き込む (flock 内専用)。

    append_action_log は内部で _flock + ShelveDB を再オープンするため、
    リネーム/削除のような巨大トランザクション内ではこちらを使って同一セッションに収める。
    """
    validate_action_type(action_type)
    normalized = normalize_code_s(code_s)
    seq = _next_seq(db, normalized)
    entry = {
        "code_s": normalized,
        "seq": seq,
        "timestamp": now_iso(),
        "action_type": action_type,
        "status_from": None,
        "status_to": None,
        "reason": reason,
    }
    db[_action_log_key(normalized, seq)] = entry


def update_memo(
    code_s: str,
    fields: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """既存レコードの memo フィールドを部分更新する。

    部分更新セマンティクス:
    - fields に含まれるキーのみ更新する。fields に存在しないキーは現行値を保持
    - 値 "" を明示的に渡した場合は「メモ削除」として "" に上書き
    - 値 None は "" に正規化 (空文字送信と同じ扱い)

    バリデーション:
    - fields のキーは MEMO_FIELDS のサブセットでなければ ValueError
    - 値は str (または None) のみ許容、それ以外は TypeError
    - レコード未登録なら KeyError
    - 排他制御は transition_status と同じ _flock パターン

    差分判定:
    - fields の各 key について現行値と完全一致すれば no-op
      (action_log 追記なし、updated_at 据え置き)
    - 1 つでも変更があれば updated_at を更新 (action_log は記録しない)

    Returns: 更新後のレコード dict (no-op 時も現行 record を返す)
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(fields, dict):
        raise TypeError(f"fields must be dict, got {type(fields).__name__}")

    unknown_keys = set(fields.keys()) - MEMO_FIELDS
    if unknown_keys:
        raise ValueError(
            f"portfolio_shelve: 未知の memo フィールド {sorted(unknown_keys)} "
            f"(許容値: {sorted(MEMO_FIELDS)})"
        )

    normalized_fields: Dict[str, Any] = {}
    for k, v in fields.items():
        if k in MEMO_LIST_FIELDS:
            # list[str] 専用フィールド (issue #187: gyoutai_themes)
            if not isinstance(v, list):
                raise TypeError(
                    f"portfolio_shelve: memo[{k!r}] must be list[str], "
                    f"got {type(v).__name__}"
                )
            if not all(isinstance(e, str) for e in v):
                raise TypeError(
                    f"portfolio_shelve: memo[{k!r}] must contain only str"
                )
            normalized_fields[k] = list(v)
        elif v is None:
            normalized_fields[k] = ""
        elif isinstance(v, str):
            normalized_fields[k] = v
        else:
            raise TypeError(
                f"portfolio_shelve: memo[{k!r}] must be str or None, "
                f"got {type(v).__name__}"
            )

    def _current_default(k: str) -> Any:
        return [] if k in MEMO_LIST_FIELDS else ""

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                raise KeyError(
                    f"portfolio_shelve: {normalized} はレコード未登録です"
                )
            record = db[key]
            current_memo = record.get("memo", {}) or {}

            # gyoutai_themes (list[str]) のマスター整合性チェック (issue #282)
            # - 空文字は除去 (スロットクリア)
            # - マスター登録済み name は採用
            # - 未登録 name で現行レコードに既に存在する場合は保持を許可 (移行漏れ救済)
            # - 純新規の未登録 name は ValueError
            if "gyoutai_themes" in normalized_fields:
                cleaned: List[str] = []
                current_themes = current_memo.get("gyoutai_themes") or []
                master_names = {
                    k[len(KEY_THEME_PREFIX):]
                    for k in db.keys()
                    if k.startswith(KEY_THEME_PREFIX)
                }
                for raw in normalized_fields["gyoutai_themes"]:
                    t = raw.strip()
                    if not t:
                        continue
                    if t in master_names or t in current_themes:
                        if t not in cleaned:
                            cleaned.append(t)
                        continue
                    raise ValueError(
                        f"portfolio_shelve: theme {t!r} はマスター未登録のため新規付与できません"
                    )
                normalized_fields["gyoutai_themes"] = cleaned

            # trade_idea の値チェック (issue #335: shelve マスター参照に移行)
            # - 空文字 (未分類) は常に許容
            # - 現行レコードに既に入っている値は保持を許可 (旧自由記述の救済)
            # - 純新規でマスター未登録の値は ValueError
            if "trade_idea" in normalized_fields:
                idea = normalized_fields["trade_idea"]
                current_idea = current_memo.get("trade_idea", "")
                if idea and idea != current_idea:
                    master_names = {
                        k[len(KEY_TRADE_IDEA_PREFIX):]
                        for k in db.keys()
                        if k.startswith(KEY_TRADE_IDEA_PREFIX)
                    }
                    if idea not in master_names:
                        raise ValueError(
                            f"portfolio_shelve: trade_idea {idea!r} はマスター未登録のため"
                            f"新規付与できません"
                        )

            changed = any(
                current_memo.get(k, _current_default(k)) != v
                for k, v in normalized_fields.items()
            )
            if not changed:
                log_print(
                    "portfolio_shelve: メモ更新スキップ (差分なし)",
                    normalized,
                )
                return record
            record["memo"] = {**current_memo, **normalized_fields}
            record["updated_at"] = now_iso()
            db[key] = record
    log_print(
        "portfolio_shelve: メモ更新",
        normalized,
        f"keys={sorted(normalized_fields.keys())}",
    )
    return record


# ===========================================
# バックアップ層
# ===========================================

def backup_portfolio_db(
    *, db_path: Optional[str] = None, generations: int = 14
) -> List[str]:
    """portfolio_shelve の実体ファイルを日付付きで世代バックアップする。"""
    path = _resolve_db_path(db_path)
    if generations < 1:
        raise ValueError("generations は1以上を指定してください")

    created: List[str] = []
    log_print("portfolio_shelve: バックアップ開始", path)
    with _flock(path):
        for ext in _SHELVE_EXTENSIONS:
            target = path + ext
            if not os.path.exists(target):
                continue
            backup_fname = backup_file(target, 0, overwrite=True)
            if backup_fname:
                created.append(backup_fname)
            backups = sorted(
                glob.glob(f"{path}_[0-9][0-9][0-9][0-9][0-9][0-9]{ext}"),
                reverse=True,
            )
            for old_backup in backups[generations:]:
                os.remove(old_backup)
    log_print("portfolio_shelve: バックアップ完了", f"files={len(created)}")
    return created


# ===========================================
# my_watch_list.txt 一方向同期
# ===========================================

def _resolve_stock_names(code_list: List[str]) -> Dict[str, str]:
    """code_s ごとの銘柄名を解決する (stocks_shelve → research_shelve → "" の優先順)。

    portfolio_shelve は銘柄名を持たないため、表示や txt 同期で必要なら都度こちらを呼ぶ。
    両 shelve とも未登録なら空文字。
    """
    from db_shelve import STOCKS_SHELVE, RESEARCH_SHELVE  # 遅延 import (循環回避)

    result: Dict[str, str] = {c: "" for c in code_list}
    if not code_list:
        return result

    try:
        with ShelveDB(STOCKS_SHELVE) as db:
            for c in code_list:
                rec = db.get(c)
                if rec and rec.get("stock_name"):
                    result[c] = rec["stock_name"]
    except Exception:
        # stocks_shelve が無い等は無視 (research_shelve fallback に進む)
        pass

    missing = [c for c, n in result.items() if not n]
    if missing:
        try:
            with ShelveDB(RESEARCH_SHELVE) as db:
                for c in missing:
                    rec = db.get(c)
                    if rec and rec.get("stock_name"):
                        result[c] = rec["stock_name"]
        except Exception:
            pass

    return result


def sync_to_my_watch_list_txt(
    *,
    txt_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """portfolio_shelve の現在状態を my_watch_list.txt に書き出す。

    一方向同期 (shelve → txt)。txt 廃止 issue (将来) で sync 自体を停止する想定。
    旧コードと既存運用との互換のため、Phase 3 完了後も同期は有効のまま残す。

    excluded=True のレコードは出力しない (`list_records` のデフォルトで除外される)。

    銘柄名は portfolio_shelve には保存されていないため stocks_shelve / research_shelve から
    都度引く (どちらにも無ければ code のみ書き出す)。

    フォーマット (現行 my_watch_list.txt 互換):
    - 1保 → "H<code_s><stock_name>" (H 接頭辞)
    - 2準 → "<code_s><stock_name>" (txt は 2 値しかないので 3監 扱い)
    - 3監 → "<code_s><stock_name>"

    出力順:
    - 1保 を先頭にまとめる (H 付きが先頭にある現行 txt の見た目を維持)
    - 各グループ内は code_s 昇順
    - 1保 と 3監/2準 の間に空行を 1 行入れる (現行 txt の見た目互換)

    Args:
        txt_path: 出力 txt パス。None なら ${DATA_DIR}/my_watch_list.txt
        db_path: portfolio_shelve のパス上書き (テスト用)

    Returns: 書き出した txt のパス
    """
    if txt_path is None:
        txt_path = os.path.join(DATA_DIR, "my_watch_list.txt")
    records = list_records(db_path=db_path)

    holds = sorted(
        (r for r in records if r.get("status") == "1保"),
        key=lambda r: r.get("code_s", ""),
    )
    others = sorted(
        (r for r in records if r.get("status") in ("2準", "3監")),
        key=lambda r: r.get("code_s", ""),
    )

    name_map = _resolve_stock_names([r.get("code_s", "") for r in holds + others])

    lines: List[str] = []
    for r in holds:
        code = r.get("code_s", "")
        lines.append(f"H{code}{name_map.get(code, '')}")
    if holds and others:
        lines.append("")
    for r in others:
        code = r.get("code_s", "")
        lines.append(f"{code}{name_map.get(code, '')}")

    txt_dir = os.path.dirname(txt_path)
    if txt_dir and not os.path.exists(txt_dir):
        os.makedirs(txt_dir, exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")

    log_print(
        f"portfolio_shelve: my_watch_list.txt 同期完了 holds={len(holds)} "
        f"others={len(others)} path={txt_path}"
    )
    return txt_path
