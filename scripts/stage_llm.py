"""週足チャートを LLM に読ませ、ミネルヴィニのステージを第二意見として残す (issue #580)。

ステージを決めるのは人で、ここで読んだ値は人のラベル (memo.stage) を書き換えない。
保有銘柄を、新しい週足が確定したら1回読む (日次バッチから毎日呼び、読んだ週は飛ばす)。

- チャートは週足キャッシュから自前で描く。銘柄コード・銘柄名・人のラベルは渡さない
- `claude -p` (Sonnet、Read のみ) を、チャートだけを置いた一時フォルダで起動する。
  リポジトリを cwd にしないのは、CLAUDE.md を読ませず、他のファイルを開かせないため

使い方:
    python stage_llm.py run              # 保有のうち、まだ読んでいない週の銘柄を読む
    python stage_llm.py run 6134 2681    # 指定銘柄を読む (同じ週でも読み直す)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ks_util import log_debug, log_print, log_warning

import portfolio_shelve as ps

MODEL = "claude-sonnet-5-5"
# 1枚は実測で約6秒。混んでいるときの余裕を見る
CLAUDE_TIMEOUT_SEC = 120
PARALLEL = 4
# ma40 と、その4週前 (price.calc_trend_template と同じ) を描けるだけの週数
MIN_WEEKS = 44
CHART_FILE = "chart.png"

# 実測 (36件、計画書) で使った文面。境目の見方は本人から聞き取った傾向で、規則ではない。
# 「迷ったら遷移に逃げない」を足すと遷移を使わなくなり外れが増えたので入れない
PROMPT = """このフォルダの chart.png は、ある銘柄の週足チャート画像です。Read で開き、ミネルヴィニのステージ分析で、右端 (現在) のステージを判定してください。

チャートの見方: 上段は週足ローソク足 (赤=陽線、青=陰線) と 10・30・40週移動平均線。下段は出来高。終値は左端を100とした指数、縦軸は対数。

次の9つから1つだけ選んでください:
1S / 2S / 3S / 4S / 1Sor3S / 1S~2S / 2S~3S / 3S~4S / 4S~1S
(A~B は A から B へ移行中。1Sor3S は横ばいで、底固めか天井圏か決めきれない場合)

境目の見方の参考 (この投資家の傾向。絶対の規則ではない):
- 4S は、30・40週線が下を向くのを待たない。高値から戻りを挟まず一直線に落ちて移動平均線をまとめて割っていれば、線がまだ上向きでも 4S とする。
- ただし、下落しても直近の安値からの下げ幅がまだ小さいなら 4S にせず 3S とする。
- 上昇のあとの横ばいでも、もみ合いの期間が長ければ天井圏 (3S) ではなく新しいベース (1S) とみる。

根拠を2〜3行で述べ、最終行に `STAGE: <値>` の形で答えてください。他のファイルは開かないでください。"""

_STAGE_LINE_RE = re.compile(r"^\s*`?STAGE:\s*`?\s*([0-9A-Za-z~]+)`?\s*$", re.MULTILINE)
_STAGE_NUM_RE = re.compile(r"[1-4]S")


# ===========================================
# 判定 (画面とアクション行が使う)

def stage_numbers(stage: str) -> Set[str]:
    """ステージ文字列に含まれるステージ番号の集合。"1Sor3S" は {1S, 3S}、"2S(3B)~3S" は {2S, 3S}。"""
    return set(_STAGE_NUM_RE.findall(stage or ""))


def is_mismatch(human_stage: str, llm: Optional[Dict[str, Any]],
                human_updated: Optional[date]) -> bool:
    """人のラベルと LLM の最新の読み取りが重ならず、人がその後に見直していないか。

    human_updated は人のステージ更新日 (last_research_update を日付にしたもの)。
    人のラベルが空なら、知らせる対象にしない。
    """
    if not llm or not stage_numbers(human_stage):
        return False
    if stage_numbers(human_stage) & stage_numbers(llm.get("stage", "")):
        return False
    try:
        run_date = date.fromisoformat(llm.get("run_date", ""))
    except ValueError:
        return False
    return human_updated is None or human_updated < run_date


def parse_stage(result_text: str) -> Optional[Dict[str, str]]:
    """モデルの本文から、最後の `STAGE: <値>` と、それより前の根拠を取り出す。

    値が select の選択肢 (STAGE_OPTIONS) に無ければ None。
    """
    matches = list(_STAGE_LINE_RE.finditer(result_text or ""))
    if not matches:
        return None
    m = matches[-1]
    stage = m.group(1)
    if stage not in ps.STAGE_OPTIONS:
        return None
    return {"stage": stage, "rationale": result_text[:m.start()].strip()}


# ===========================================
# チャート

def _weekly_rows(code_s: str) -> List[tuple]:
    """週足キャッシュ (新しい週が先頭、確定週のみ)。無ければ空。"""
    import price  # WebApp は判定関数だけを使うので、yfinance を読み込ませない
    _, rows = price._load_yfinance_cache(price.YFINANCE_WEEKLY_CACHE_FNAME % code_s)
    return [tuple(r) for r in rows or []]


def _latest_week(rows: List[tuple]) -> Optional[str]:
    import price
    d = price._parse_weekly_cache_date(rows[0][0]) if rows else None
    return d.isoformat() if d else None


def render_chart(rows: List[tuple], path: str) -> None:
    """週足チャートを描く。実測と同じ描き方に固定する (変えると読み方も変わりうる)。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def num(x):
        return float(str(x).replace(",", ""))

    rows = rows[::-1]  # 古い順
    o, h, l, c, v = ([num(r[i]) for r in rows] for i in (1, 2, 3, 4, 7))
    k = 100 / c[0]
    o, h, l, c = ([a * k for a in s] for s in (o, h, l, c))
    n = len(c)

    def ma(w):
        return [sum(c[j - w + 1:j + 1]) / w if j >= w - 1 else None for j in range(n)]

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6), dpi=90, sharex=True,
                                 gridspec_kw={"height_ratios": [3, 1], "hspace": 0.03})
    for j in range(n):
        col = "#d33" if c[j] >= o[j] else "#27c"
        a1.plot([j, j], [l[j], h[j]], color=col, lw=0.8)
        a1.add_patch(plt.Rectangle((j - 0.35, min(o[j], c[j])), 0.7, abs(c[j] - o[j]) or 0.01, color=col))
        a2.bar(j, v[j], color=col, width=0.7)
    for w, col in ((10, "#e90"), (30, "#090"), (40, "#909")):
        a1.plot(range(n), ma(w), color=col, lw=1.3, label="MA%d" % w)
    a1.set_yscale("log")
    a1.legend(loc="upper left", fontsize=8)
    a1.grid(alpha=0.3, which="both")
    a1.set_title("weekly, close indexed to 100 at left edge (log)", fontsize=10)
    a2.grid(alpha=0.3)
    a2.set_xlabel("weeks")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ===========================================
# claude -p

def _run_claude(workdir: str) -> Optional[str]:
    """claude -p を workdir で起動し、モデルの本文 (result) を返す。失敗は None。"""
    cmd = ["claude", "-p", PROMPT, "--model", MODEL, "--allowed-tools", "Read",
           "--output-format", "json"]
    # claude CLI は ~/.local/bin にあるが、launchd の PATH には含めていないことがある
    env = {**os.environ, "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"}
    try:
        result = subprocess.run(cmd, cwd=workdir, env=env, timeout=CLAUDE_TIMEOUT_SEC,
                                check=False, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        log_warning(f"[stage-llm] claude -p タイムアウト ({CLAUDE_TIMEOUT_SEC}s)")
        return None
    except FileNotFoundError:
        log_warning("[stage-llm] claude CLI が見つかりません")
        return None
    if result.returncode != 0:
        log_warning(f"[stage-llm] claude -p 異常終了: rc={result.returncode} "
                    f"stderr={(result.stderr or '')[-300:]!r} stdout={(result.stdout or '')[-300:]!r}")
        return None
    try:
        return str(json.loads(result.stdout).get("result", ""))
    except (ValueError, TypeError, AttributeError):
        log_warning(f"[stage-llm] claude -p の出力が JSON として読めない: {(result.stdout or '')[-300:]!r}")
        return None


def read_stage(code_s: str, rows: List[tuple]) -> Optional[Dict[str, str]]:
    """1銘柄のチャートを描いて読ませる。失敗は None (1銘柄の失敗で残りを止めない)。"""
    with tempfile.TemporaryDirectory(prefix="stage_llm_") as workdir:
        try:
            render_chart(rows, os.path.join(workdir, CHART_FILE))
        except Exception as e:  # 週足の欠損値・終値0・描画の失敗など
            log_warning(f"[stage-llm] {code_s}: チャートを描けない: {e!r}")
            return None
        text = _run_claude(workdir)
    if text is None:
        return None
    parsed = parse_stage(text)
    if parsed is None:
        log_warning(f"[stage-llm] {code_s}: STAGE 行が読めない: {text[-200:]!r}")
    return parsed


# ===========================================
# 実行

def select_targets(records: List[Dict[str, Any]], latest: Dict[str, Dict[str, Any]],
                   force: bool = False) -> List[Dict[str, Any]]:
    """読む銘柄と、その週足を選ぶ。

    週足が MIN_WEEKS に満たない銘柄は読まない。force でなければ、保存済みの最新の
    week が週足の先頭と同じ銘柄も読まない (同じ日に2回走っても読み直さない)。
    """
    targets = []
    for r in records:
        code_s = r["code_s"]
        rows = _weekly_rows(code_s)
        if len(rows) < MIN_WEEKS:
            log_debug(f"[stage-llm] {code_s}: 週足 {len(rows)} 週で不足、読まない")
            continue
        week = _latest_week(rows)
        if week is None:
            continue
        if not force and (latest.get(code_s) or {}).get("week") == week:
            continue
        targets.append({"record": r, "rows": rows, "week": week})
    return targets


def run(codes: Optional[List[str]] = None) -> int:
    """対象を読み、結果を追記する。読めた件数を返す。"""
    latest = ps.latest_stage_llm_map()
    if codes:
        records = [rec for c in codes if (rec := ps.get_record(c)) is not None]
    else:
        records = ps.list_records("1保")
    targets = select_targets(records, latest, force=bool(codes))
    log_print(f"[stage-llm] 対象 {len(targets)} 件 (候補 {len(records)} 件)")
    if not targets:
        return 0

    def work(t):
        return t, read_stage(t["record"]["code_s"], t["rows"])

    ok = 0
    run_date = datetime.now().date().isoformat()
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        for t, parsed in ex.map(work, targets):
            if parsed is None:
                continue
            code_s = t["record"]["code_s"]
            ps.append_stage_llm(code_s, {
                "week": t["week"],
                "run_date": run_date,
                "stage": parsed["stage"],
                "rationale": parsed["rationale"],
                "model": MODEL,
                "human_stage": (t["record"].get("memo") or {}).get("stage") or "",
            })
            ok += 1
            log_debug(f"[stage-llm] {code_s}: {parsed['stage']}")
    log_print(f"[stage-llm] 完了: 読めた {ok} 件 / 失敗 {len(targets) - ok} 件")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="週足チャートを LLM に読ませる")
    p_run.add_argument("codes", nargs="*", help="銘柄コード (省略時は保有のうち未読の週)")
    args = parser.parse_args()
    if args.cmd == "run":
        run(args.codes or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
