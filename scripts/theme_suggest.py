"""業態テーマ自動提案 (issue #297)。

銘柄の事業内容テキスト (四季報特色・四季報コメント・株探概要) を軽量 LLM
(`claude -p` Haiku) に渡し、既定の業態テーママスターから最も合うテーマを
1〜2 件提案する。

- LLM は「マスターのリストから選ぶ分類」のみを行う。リスト外のテーマを返しても
  呼び出し側でフィルタ除去する (ハルシネーション防止)。
- `claude -p` の起動方法は run_theme_news.py を踏襲 (新規依存・API キー不要)。
- LLM の失敗 (タイムアウト・異常終了・パース失敗) は空リストにフォールバックする。
"""
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ks_util import log_debug, log_print, log_warning, log_error

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# claude -p のタイムアウト (秒)。Haiku の分類は通常数秒で返る。
# Flask ワーカースレッドを過度にブロックしないよう短めに設定。
DEFAULT_TIMEOUT_SEC = 45

# 提案するテーマの最大件数 (業態テーマのスロット数に合わせる)。
DEFAULT_MAX_SUGGEST = 2


def build_business_text(
    research_overview: Optional[str],
    shikiho_comments: Optional[List[Dict[str, Any]]],
    stocks_overview: Optional[str],
) -> str:
    """3 ソースを結合して 1 つの事業説明テキストにする。

    - research_overview: 四季報の特色・企業概要 (research_shelve.overview)
    - shikiho_comments: 四季報コメント [{"period", "comment"}, ...]
    - stocks_overview: 株探の概要 1 行 (stocks_shelve.overview)

    全ソースが空なら "" を返す (呼び出し側で LLM を起動しない判定に使う)。
    """
    parts: List[str] = []

    ro = (research_overview or "").strip()
    if ro:
        parts.append(f"【企業概要(四季報特色)】\n{ro}")

    comments = shikiho_comments or []
    comment_lines: List[str] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        text = (item.get("comment") or "").strip()
        if not text:
            continue
        period = (item.get("period") or "").strip()
        comment_lines.append(f"({period}) {text}" if period else text)
    if comment_lines:
        parts.append("【四季報コメント】\n" + "\n".join(comment_lines))

    so = (stocks_overview or "").strip()
    if so:
        parts.append(f"【概要(株探)】\n{so}")

    return "\n\n".join(parts)


def _build_prompt(business_text: str, theme_names: List[str], max_suggest: int) -> str:
    """LLM へ渡すプロンプトを組み立てる。

    マスターのテーマ一覧から「のみ」選ばせ、JSON 配列で返させる。
    """
    theme_list = "\n".join(f"- {name}" for name in theme_names)
    return (
        "あなたは日本株の業態分類アシスタントです。"
        "以下の事業内容に最も合致する「業態テーマ」を、後述のテーマ一覧の中から"
        f"最大{max_suggest}件選んでください。\n\n"
        "厳守事項:\n"
        "- 必ずテーマ一覧に存在する文字列だけを、一字一句そのまま出力すること。\n"
        "- 一覧に無いテーマを新しく作ってはいけない。\n"
        "- 合致するものが無ければ空配列を返すこと。\n"
        '- 出力は JSON 配列のみ。例: ["半導体", "AI"]。説明文は不要。\n\n'
        f"# テーマ一覧\n{theme_list}\n\n"
        f"# 事業内容\n{business_text}\n"
    )


def _extract_json_array(text: str) -> List[str]:
    """LLM 出力テキストから最初の JSON 配列を抽出して list[str] にする。

    ```json フェンスや前後の説明文が付いていても配列部分だけを拾う。
    パース失敗時は [] を返す。
    """
    if not text:
        return []
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    return [str(x).strip() for x in arr if str(x).strip()]


def _run_claude(prompt: str, timeout_sec: int) -> str:
    """claude -p (Haiku) を起動し、result テキストを返す。

    失敗時は "" を返す。ツールは使わせない (分類のみ、Web 検索等は不要)。
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        "haiku",
        "--output-format",
        "json",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            timeout=timeout_sec,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        log_error(f"[theme-suggest] claude -p タイムアウト ({timeout_sec}s)")
        return ""
    except FileNotFoundError:
        log_error("[theme-suggest] claude CLI が見つかりません")
        return ""

    if result.returncode != 0:
        log_warning(f"[theme-suggest] claude -p 異常終了: rc={result.returncode}")
        log_warning(f"stderr: {(result.stderr or '')[-500:]}")
        return ""

    # --output-format json の stdout は {"result": "...", "usage": {...}, ...}
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        log_warning("[theme-suggest] claude -p の出力が JSON としてパースできない")
        log_debug(f"stdout 末尾: {(result.stdout or '')[-300:]!r}")
        return ""
    return str(payload.get("result", ""))


def suggest_gyoutai_themes(
    business_text: str,
    theme_names: List[str],
    *,
    max_suggest: int = DEFAULT_MAX_SUGGEST,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> List[str]:
    """事業内容テキストから業態テーマを最大 max_suggest 件提案する。

    theme_names (マスター登録テーマ) の中からのみ選ぶ。LLM がリスト外を返しても
    フィルタ除去する。空入力や LLM 失敗時は [] を返す。
    """
    business_text = (business_text or "").strip()
    if not business_text:
        return []
    if not theme_names:
        return []

    prompt = _build_prompt(business_text, theme_names, max_suggest)
    log_debug(f"[theme-suggest] prompt:\n{prompt}")

    raw = _run_claude(prompt, timeout_sec)
    candidates = _extract_json_array(raw)
    log_debug(f"[theme-suggest] LLM 候補: {candidates}")

    # マスター内のみ・重複除去・順序保持・max_suggest 件で打ち切り
    valid = set(theme_names)
    seen: set = set()
    result: List[str] = []
    for name in candidates:
        if name in valid and name not in seen:
            seen.add(name)
            result.append(name)
        if len(result) >= max_suggest:
            break

    log_print(f"[theme-suggest] 提案テーマ: {result}")
    return result
