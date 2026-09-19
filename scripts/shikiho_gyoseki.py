"""四季報業績予想テキスト (楽天証券コピペ) のパーサー (issue #346)。

2期先の業績予想は四季報くらいしか出典がなく、信頼度の面から定量指標としては
扱えない。**AI 分析用の定性情報**として保持し、スコアリング (make_stock_db の
業績ポイント等) からは一切参照しない。
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional

# 期ラベル: 会計基準の接頭辞 (連/単/◇/※) + YY.M + サフィックス (予/*/中 など)。
# 例: "連27.3予", "連22.3*", "単26.3", "26.9中"
_PERIOD_RE = re.compile(r"^[連単◇※]?\s*(\d{2})\.(\d{1,2})(.*)$")

# 中間期・四半期行を示すサフィックス。通期のみ扱うため除外する。
# レンジ記号は累計行 (例: "連25.9～5" = 9月〜5月の3Q累計) を表す。決算月と
# 累計開始月が衝突する銘柄 (8月決算の 連25.9～5 等) では月での除外が効かず、
# 累計行を通期実績と誤認して決算月ごと取り違えるため、記号自体で弾く。
_NON_ANNUAL_SUFFIXES = ("中", "四", "～", "~", "-")


def _parse_number(token: str) -> Optional[float]:
    """数値セルをパースする。カンマ除去。数値でなければ None。"""
    cleaned = token.replace(",", "").replace("△", "-").replace("▲", "-").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _split_columns(line: str) -> List[str]:
    """行を列へ分割する。タブ優先、無ければ連続空白。"""
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    return line.split()


def _parse_row(line: str) -> Optional[Dict[str, Any]]:
    """1行を期ラベル + 売上高 + 営業利益へパースする。対象外の行は None。"""
    columns = _split_columns(line)
    if not columns:
        return None

    match = _PERIOD_RE.match(columns[0])
    if match is None:
        return None
    year, month, suffix = int(match.group(1)), int(match.group(2)), match.group(3)
    if not 1 <= month <= 12:
        return None
    if any(s in suffix for s in _NON_ANNUAL_SUFFIXES):
        return None  # 中間期・四半期行は対象外 (通期のみ)

    # 売上高・営業利益は期ラベルの次の2列に固定。「数値としてパースできた順」に
    # 拾うと、営業利益が "-" (非開示) の銘柄で経常利益を営業利益として取り込んで
    # しまい、もっともらしい誤値が DB・AI 分析まで流れる。列位置で固定し、
    # 数値でないセルは None (= 成長率を出さない) とする。
    cells = columns[1:]
    return {
        "label": columns[0],
        "sort_key": (year, month),
        "is_forecast": "予" in suffix,
        "sales": _parse_number(cells[0]) if len(cells) >= 1 else None,
        "op_profit": _parse_number(cells[1]) if len(cells) >= 2 else None,
    }


def _growth(new: Optional[float], old: Optional[float]) -> Optional[float]:
    """前期比の成長率 (%) を返す。

    分母が None / 0 / 負値のときは None。赤字からの回復などは率で語れないため。
    """
    if new is None or old is None or old <= 0:
        return None
    return round((new - old) / old * 100, 1)


def _to_entry(row: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """パース済み行を保存用 dict へ整形する。prev があれば成長率を付ける。"""
    entry = {
        "label": row["label"],
        "sales": row["sales"],
        "op_profit": row["op_profit"],
    }
    if prev is not None:
        entry["sales_growth"] = _growth(row["sales"], prev["sales"])
        entry["op_growth"] = _growth(row["op_profit"], prev["op_profit"])
    else:
        entry["sales_growth"] = None
        entry["op_growth"] = None
    return entry


def parse_shikiho_gyoseki(text: str) -> Dict[str, Any]:
    """楽天証券の四季報業績テーブル貼り付けテキストをパースする。

    直前実績の次の決算期を今季、そのさらに次を来季とする。数値の単位は
    四季報表記のまま (百万円) で変換しない。

    Raises:
        ValueError: 予想通期行が1件も無い、または予想がすべて直前実績より
            過去の期のとき (古い四季報を貼っている)
    """
    rows = [r for r in (_parse_row(line) for line in text.splitlines()) if r]
    rows.sort(key=lambda r: r["sort_key"])

    actual_rows = [r for r in rows if not r["is_forecast"]]
    latest_actual = actual_rows[-1] if actual_rows else None

    # 決算月は最新実績の月を正とする。四季報の予想行には本決算のほかに中間期
    # 予想が混じることがあり (例: 連27.9予)、月で弾かないと半期の値を通期として
    # 保存してしまう (成長率もMCP出力も誤る)。
    #
    # 同月の予想が無いとき「決算期変更」と「貼付範囲に中間期予想しか入って
    # いない」を貼付テキストから判別する方法は無い (過去の決算月変更の履歴が
    # 残っている会社では、実績行の月が複数あることも判断材料にならない)。
    # 誤って半期を通期として保存するより拒否する方が安全なので、推測しない。
    # 決算期変更後は新しい決算月の実績行がいずれ載るため、その時点で通る。
    forecasts = [r for r in rows if r["is_forecast"]]
    if latest_actual is not None:
        fiscal_month = latest_actual["sort_key"][1]
        forecasts = [r for r in forecasts if r["sort_key"][1] == fiscal_month]
        if not forecasts:
            raise ValueError(
                f"直前実績 ({latest_actual['label']}) と同じ決算月の予想行が"
                "ありません。中間期予想だけを貼り付けていないか確認してください。"
            )
    if not forecasts:
        raise ValueError(
            "予想通期行 (例: 連27.3予) が見つかりません。"
            "楽天証券の業績テーブルをそのまま貼り付けてください。"
        )

    # 今季 = 直前実績の次に来る予想期。予想が3期以上並んでいても位置ではなく
    # 期で選ぶ (forecasts[-2] だと今季を飛ばして来季・再来季を掴む)。
    if latest_actual is not None:
        forecasts = [r for r in forecasts if r["sort_key"] > latest_actual["sort_key"]]
        if not forecasts:
            # 予想が全て実績より過去 = 古い四季報を貼っている。ここで古い期を
            # 「今季予想」として保存すると、AI 分析に陳腐化した予想が流れる。
            raise ValueError(
                f"予想がすべて直前実績 ({latest_actual['label']}) より過去の期です。"
                "最新の四季報の業績テーブルを貼り付けてください。"
            )
    this_row = forecasts[0]
    next_row = forecasts[1] if len(forecasts) >= 2 else None

    # 直前実績: this_year より前の実績通期行のうち最新。
    actuals = [r for r in actual_rows if r["sort_key"] < this_row["sort_key"]]
    prev_row = actuals[-1] if actuals else None

    return {
        "prev_year": (
            {
                "label": prev_row["label"],
                "sales": prev_row["sales"],
                "op_profit": prev_row["op_profit"],
            }
            if prev_row
            else None
        ),
        "this_year": _to_entry(this_row, prev_row),
        "next_year": _to_entry(next_row, this_row) if next_row else None,
        # 保存する原文は strip 済みに正規化する (呼び出し元ごとに差が出ないように)
        "raw_text": text.strip(),
        "updated_at": date.today().isoformat(),
    }
