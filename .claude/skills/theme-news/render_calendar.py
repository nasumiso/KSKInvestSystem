"""events.json と calendar_template.html から calendar.html を生成する。

theme-news スキルが events.json を編集した後、このスクリプトを実行して HTML を再生成する。
WebApp 側からも、events.json を直接読んで Jinja でレンダリングし直すことで再利用できる。

Usage:
    python render_calendar.py            # 通常: TODAY = ks_util.get_price_day() の今日
    python render_calendar.py --today 2026-05-19   # TODAY を明示指定 (テスト用)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
EVENTS_JSON = SKILL_DIR / "events.json"
TEMPLATE_HTML = SKILL_DIR / "calendar_template.html"
OUTPUT_HTML = SKILL_DIR / "calendar.html"


def resolve_today(today_arg: str | None) -> str:
    """TODAY (YYYY-MM-DD) を解決。明示指定 > ks_util.get_price_day > date.today。"""
    if today_arg:
        # フォーマット検証
        datetime.strptime(today_arg, "%Y-%m-%d")
        return today_arg
    # ks_util.get_price_day を試す (17:00前は前日扱いの CLAUDE.md 規約準拠)
    try:
        scripts_dir = SKILL_DIR.parent.parent.parent / "scripts"
        if scripts_dir.exists() and str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from ks_util import get_price_day  # type: ignore
        return get_price_day(datetime.now()).isoformat()
    except Exception:
        return date.today().isoformat()


def render(events_json: Path, template_html: Path, output_html: Path, today: str) -> None:
    events = json.loads(events_json.read_text())
    if not isinstance(events, list):
        raise ValueError(f"{events_json} のルートは配列である必要があります")

    # JSON を 2 スペースインデントで埋め込む (人間が後で読みやすいように)
    events_str = json.dumps(events, ensure_ascii=False, indent=2)

    template = template_html.read_text()
    rendered = (
        template
        .replace("{{LAST_UPDATED}}", today)
        .replace("{{TODAY}}", today)
        .replace("{{EVENTS_JSON}}", events_str)
    )
    output_html.write_text(rendered)
    print(f"wrote {output_html} (events: {len(events)}, today: {today})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render calendar.html from events.json")
    parser.add_argument("--today", help="TODAY を YYYY-MM-DD で明示指定")
    args = parser.parse_args()

    today = resolve_today(args.today)
    render(EVENTS_JSON, TEMPLATE_HTML, OUTPUT_HTML, today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
