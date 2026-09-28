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

    forecasts = [r for r in rows if r["is_forecast"]]
    fiscal_month_changed = False
    if latest_actual is not None:
        fiscal_month = latest_actual["sort_key"][1]
        forecasts = [r for r in forecasts if r["sort_key"] > latest_actual["sort_key"]]
        same_month = [r for r in forecasts if r["sort_key"][1] == fiscal_month]
        if same_month:
            forecasts = same_month
        else:
            # 決算期変更の初年度は旧決算月の実績と比較できない。一方、予想行が
            # 1件だけでは中間期予想との区別が付かないため、同じ新決算月の予想が
            # 連続2期以上ある場合にだけ決算期変更として受け入れる。
            by_month = {}
            for row in forecasts:
                by_month.setdefault(row["sort_key"][1], []).append(row)
            changed = next(
                (
                    items for items in by_month.values()
                    if len(items) >= 2
                    and items[1]["sort_key"][0] == items[0]["sort_key"][0] + 1
                ),
                None,
            )
            if changed is None:
                raise ValueError(
                    f"直前実績 ({latest_actual['label']}) と同じ決算月の予想行が"
                    "ありません。中間期予想だけを貼り付けていないか確認してください。"
                )
            forecasts = changed
            fiscal_month_changed = True
    if not forecasts:
        raise ValueError(
            "予想通期行 (例: 連27.3予) が見つかりません。"
            "楽天証券の業績テーブルをそのまま貼り付けてください。"
        )

    # 今季 = 直前実績の次に来る予想期。予想が3期以上並んでいても位置ではなく
    # 期で選ぶ (forecasts[-2] だと今季を飛ばして来季・再来季を掴む)。
    if latest_actual is not None:
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
        # 決算期変更の初年度は 9か月等の変則期になりうるため、旧決算月の実績との
        # 成長率は計算しない。次年度以降は同じ新決算月の予想どうしで計算できる。
        "this_year": _to_entry(this_row, None if fiscal_month_changed else prev_row),
        "next_year": _to_entry(next_row, this_row) if next_row else None,
        # 保存する原文は strip 済みに正規化する (呼び出し元ごとに差が出ないように)
        "raw_text": text.strip(),
        "updated_at": date.today().isoformat(),
    }
