"""教訓Top10 (doc/投資メモ分析/教訓Top10.md) の見出しと、出す場面を読む (issue #590, #591)。

正本は md のまま。四半期の入れ替えで書き写さないよう、表示のたびに読む。
"""

import re
from pathlib import Path
from typing import Dict, List, Tuple

LESSONS_PATH = Path(__file__).resolve().parents[2] / "doc" / "投資メモ分析" / "教訓Top10.md"
LESSONS_URL = "https://github.com/nasumiso/KSKInvestSystem/blob/main/doc/投資メモ分析/教訓Top10.md"

# "## 1. 見出し *(2026-10 新設)*" → 番号と見出し (末尾の注記は外す)
_HEADING_RE = re.compile(r"^## (\d+)\. (.+?)(?:\s*\*\(.*\)\*)?\s*$")
# 教訓の節の中の "- **出す場面**: 買い・売り"
_SCENES_RE = re.compile(r"^- \*\*出す場面\*\*:\s*(.+?)\s*$")


def load_lessons() -> List[Dict]:
    """[{"num", "title", "scenes"}] を md の順で返す。md が無ければ空。"""
    try:
        text = LESSONS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    lessons, current = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            m = _HEADING_RE.match(line)
            current = {"num": m.group(1), "title": m.group(2), "scenes": []} if m else None
            if current:
                lessons.append(current)
        elif current and (m := _SCENES_RE.match(line)):
            current["scenes"] = [s.strip() for s in m.group(1).split("・") if s.strip()]
    return lessons


def lessons_by_scene(lessons: List[Dict]) -> Dict[str, List[Tuple[str, str]]]:
    """場面ごとの [(番号, 見出し)]。"""
    scenes: Dict[str, List[Tuple[str, str]]] = {}
    for lesson in lessons:
        for scene in lesson["scenes"]:
            scenes.setdefault(scene, []).append((lesson["num"], lesson["title"]))
    return scenes
