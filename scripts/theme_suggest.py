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

# この confidence (0-100) 以上のマスター内テーマだけを select にプリセットする。
# 未満は「低確信」として参考表示のみ。
SUGGEST_CONFIDENCE_THRESHOLD = 60


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


def _build_prompt(business_text: str, theme_names: List[str]) -> str:
    """LLM へ渡すプロンプトを組み立てる。

    マスター一覧から確信度 (0-100) 付きで挙げさせ、合致が弱い場合のみ
    投資テーマとして適切な粒度の新テーマを提案させる。
    """
    theme_list = "\n".join(f"- {name}" for name in theme_names)
    return (
        "あなたは日本株の業態分類アシスタントです。"
        "以下の事業内容に合致する「業態テーマ」を判定してください。\n\n"
        "判定方針:\n"
        "- 後述のテーマ一覧の各候補について、事業内容との合致度を confidence (0-100) で評価する。\n"
        "- confidence の基準 (厳格に適用すること):\n"
        "  * 80-100: そのテーマが売上の柱・主力事業そのものと言える。\n"
        "  * 60-79:  主力事業の重要な一部だが中核とまでは言えない。\n"
        "  * 40-59:  関連はするが副次的・補完的な位置づけ。\n"
        "  * 0-39:   将来計画・一言の言及程度、または無関係に近い。\n"
        "  迷ったら低めに付ける。安易に高スコアを付けない。\n"
        "- 無理に件数を埋めない。合致しないテーマは挙げないか、低い confidence を付ける。\n"
        "- マスター一覧に適切なテーマが無い/弱い場合に限り、一覧に無い新しい業態テーマを提案してよい。\n"
        "  新テーマの粒度は「投資テーマ」として括れる広さにする:\n"
        "  特定すぎる例 (× SMSサービス) も汎用すぎる例 (× ITサービス) も避け、\n"
        "  中間の投資テーマ粒度 (○ 認証ソリューション 等) にすること。\n\n"
        "厳守事項:\n"
        "- matched に挙げる name は、必ずテーマ一覧に存在する文字列を一字一句そのまま使う。\n"
        "- new に挙げる name は、テーマ一覧に存在しない新テーマだけにする。\n"
        "- 出力は次の JSON のみ。説明文やフェンス外の文章は不要。\n"
        '  {"matched": [{"name": "半導体", "confidence": 80}],'
        ' "new": [{"name": "認証ソリューション", "confidence": 75, "reason": "..."}]}\n\n'
        f"# テーマ一覧\n{theme_list}\n\n"
        f"# 事業内容\n{business_text}\n"
    )


def _extract_json_object(text: str) -> Dict[str, Any]:
    """LLM 出力テキストから最初の JSON オブジェクトを抽出して dict にする。

    ```json フェンスや前後の説明文が付いていてもオブジェクト部分だけを拾う。
    パース失敗時は {} を返す。
    """
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _clamp_confidence(value: Any) -> Optional[int]:
    """confidence を 0-100 の int に正規化する。不正値は None。"""
    try:
        n = int(round(float(value)))
    except (ValueError, TypeError):
        return None
    return max(0, min(100, n))


def _parse_entries(raw_list: Any) -> List[Dict[str, Any]]:
    """LLM の matched/new 配列を [{name, confidence, reason?}] に正規化する。

    name 空・confidence 不正のエントリはスキップ。
    """
    if not isinstance(raw_list, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        conf = _clamp_confidence(item.get("confidence"))
        if not name or conf is None:
            continue
        entry: Dict[str, Any] = {"name": name, "confidence": conf}
        reason = str(item.get("reason", "")).strip()
        if reason:
            entry["reason"] = reason
        out.append(entry)
    return out


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


def _empty_result() -> Dict[str, List[Dict[str, Any]]]:
    return {"preset": [], "low": [], "new": []}


def suggest_gyoutai_themes(
    business_text: str,
    theme_names: List[str],
    *,
    max_preset: int = DEFAULT_MAX_SUGGEST,
    threshold: int = SUGGEST_CONFIDENCE_THRESHOLD,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, List[Dict[str, Any]]]:
    """事業内容テキストから業態テーマを確信度付きで提案する。

    戻り値:
      {
        "preset": [{"name", "confidence"}, ...],  # マスター内 & confidence>=threshold
        "low":    [{"name", "confidence"}, ...],  # マスター内だが confidence<threshold
        "new":    [{"name", "confidence", "reason"?}, ...],  # マスター外の新テーマ提案
      }
    preset のみ select 反映対象。low/new は参考表示用。
    空入力や LLM 失敗時は全て空の dict を返す。
    """
    business_text = (business_text or "").strip()
    if not business_text or not theme_names:
        return _empty_result()

    prompt = _build_prompt(business_text, theme_names)
    log_debug(f"[theme-suggest] prompt:\n{prompt}")

    raw = _run_claude(prompt, timeout_sec)
    obj = _extract_json_object(raw)
    matched = _parse_entries(obj.get("matched"))
    new_entries = _parse_entries(obj.get("new"))
    log_debug(f"[theme-suggest] matched={matched} new={new_entries}")

    valid = set(theme_names)
    # matched: マスター内のみ採用 (ハルシネーション防止)、重複除去、confidence 降順
    seen: set = set()
    in_master: List[Dict[str, Any]] = []
    for e in matched:
        if e["name"] in valid and e["name"] not in seen:
            seen.add(e["name"])
            in_master.append(e)
    in_master.sort(key=lambda e: e["confidence"], reverse=True)

    preset = [e for e in in_master if e["confidence"] >= threshold][:max_preset]
    preset_names = {e["name"] for e in preset}
    low = [e for e in in_master if e["name"] not in preset_names]

    # new: マスター外のものだけ残す (既存名を新テーマ扱いしない)、重複除去、confidence 降順
    new_seen: set = set()
    new_list: List[Dict[str, Any]] = []
    for e in new_entries:
        if e["name"] not in valid and e["name"] not in new_seen:
            new_seen.add(e["name"])
            new_list.append(e)
    new_list.sort(key=lambda e: e["confidence"], reverse=True)

    result = {"preset": preset, "low": low, "new": new_list}
    log_print(
        f"[theme-suggest] preset={[e['name'] for e in preset]} "
        f"low={[e['name'] for e in low]} new={[e['name'] for e in new_list]}"
    )
    return result
