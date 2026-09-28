"""
fill 基準の売買エピソード・往復行・損益集計。

証券会社CSVの約定 (fill) から建玉ラウンド (エピソード) を組み立て、分割・併合の換算、
往復行への展開、銘柄別・戦略別の損益集計を行う。画面表示とは独立した計算処理で、
webapp の各画面と取込スクリプト (import_*_fills.py 等) から使う。
銘柄名・株価ログの一括取得は helpers のヘルパーを使う。
"""

from datetime import date
from typing import Any, Dict, List, Optional

from webapp import helpers

_SIDE_LABELS = {"buy": "買", "sell": "売"}


# ===========================================
# issue #387 Phase4b: fill 基準の建玉ラウンド・エピソード再構成
# ===========================================


def _episode_pl_from_round(rnd: dict) -> Optional[dict]:
    """クローズ済み建玉ラウンドから calc_trade_summary 互換の損益 dict を作る。

    現物: 平均取得単価法。買い (buy=買付/現引) で加重平均取得単価を積み、売り (sell) で
      実現損益 = Σ(sell_price - avg_cost) * sell_qty。amount = 総取得コスト (金額加重の重み)。
    信用: 各返済 fill が単独で損益確定。楽天=約定単価-建単価(tate_price)、SBI=settle_pl。
      amount = 建玉コスト (Σ tate_price*qty、無ければ約定金額)。

    保有日数は2レイヤーある:
      - ラウンド単位 (戻り値の hold_days): open_date〜close_date。現物・信用とも算出する。
      - fill 単位 (f["hold_days"]): 建日〜決済日。**信用のみ**。信用は返済 fill と建玉が
        tate_date/tate_price で1対1に対応するため個別に出せるが、現物は売却 fill が
        どの買いに対応するかCSVに情報が無く、平均取得単価法で損益を近似するため
        建日を紐付けられない。テンプレート側も現物行は日数列を空欄にする。

    Returns: {return_pct, hold_days, avg_cost, amount, profit_amount, ...} / 算出不能なら None
    """
    fills = rnd["fills"]
    if not fills:
        return None
    kind = rnd["kind"]
    total_cost = 0.0        # 取得コスト合計 (現物=買い金額, 信用=建玉金額) → 金額加重の重み
    realized = 0.0          # 実現損益額

    if kind == "現物":
        held_qty = 0
        avg_cost = 0.0
        cost_basis_total = 0.0  # 買いで積んだ延べ取得コスト (return_pct の分母 amount)
        for f in fills:
            qty = f["qty"]
            price = f["price"]
            if f["side"] == "buy":
                new_qty = held_qty + qty
                avg_cost = (avg_cost * held_qty + price * qty) / new_qty if new_qty else 0.0
                held_qty = new_qty
                cost_basis_total += price * qty
            else:  # sell
                sell_qty = min(qty, held_qty) if held_qty > 0 else qty
                if held_qty > 0 and avg_cost > 0:
                    fill_pl = (price - avg_cost) * sell_qty
                    f["fill_pl"] = round(fill_pl)
                    f["fill_return_pct"] = fill_pl / (avg_cost * sell_qty) * 100
                realized += (price - avg_cost) * sell_qty
                held_qty -= qty
        total_cost = cost_basis_total
    else:  # 信用
        # 買建は返済 sell で、売建 (空売り) は返済 buy で損益が確定する。
        # 売建は「高く売って安く買い戻す」ので損益の符号が買建と逆になる。
        is_short = rnd.get("is_short", False)
        settle_side = "buy" if is_short else "sell"
        open_side = "sell" if is_short else "buy"
        # 建約定日の無い返済でも、未決済ロットが1本だけなら建値は一意に復元できる。
        # 複数ロット候補が残る場合は従来どおり推測せず損益を伏せる。
        open_pool: List[Dict[str, Any]] = [
            {"fill": f, "remain": f["qty"]}
            for f in fills
            if (f["side"] == open_side
                and (f.get("trade_kind") or "").startswith("信用新規"))
        ]
        # 建情報のある返済を先に引き当てる。同日中の複数返済では、建情報なしの行が
        # 先に並んでいても、後続の建情報付き行を除外すれば残ロットが一意になる。
        for f in fills:
            if f["side"] == settle_side and f.get("tate_price") is not None:
                _consume_open_lots(open_pool, f["qty"], f.get("tate_date"),
                                   f["tate_price"], f.get("broker"))
        for f in fills:
            if f["side"] != settle_side:
                continue
            qty = f["qty"]
            price = f["price"]
            settle_pl = f.get("settle_pl")
            tate_price = f.get("tate_price")
            tate_date = f.get("tate_date")
            if tate_price is None and settle_pl is None:
                candidates = _match_open_lots(open_pool, None, None, f.get("broker"))
                if len(candidates) != 1 or candidates[0]["remain"] < qty:
                    return None
                opening = candidates[0]["fill"]
                tate_price = opening["price"]
                tate_date = opening.get("trade_date")
                _consume_open_lots(open_pool, qty, tate_date, tate_price, f.get("broker"))
            if settle_pl is not None:
                realized += settle_pl
                f["fill_pl"] = settle_pl
                total_cost += (tate_price or price) * qty
            elif tate_price is not None:
                # 売建は (建単価 - 買戻単価)、買建は (返済単価 - 建単価)
                fill_pl = ((tate_price - price) if is_short
                           else (price - tate_price)) * qty
                realized += fill_pl
                f["fill_pl"] = round(fill_pl)
                total_cost += tate_price * qty
            else:
                # 建単価も決済損益も無い → 損益不能
                return None
            if tate_price is not None:
                f["fill_return_pct"] = f["fill_pl"] / (tate_price * qty) * 100
            if tate_date:
                try:
                    f["hold_days"] = (date.fromisoformat(f["trade_date"]) - date.fromisoformat(tate_date)).days
                except (ValueError, TypeError):
                    pass

    if total_cost <= 0:
        return None
    return_pct = realized / total_cost * 100
    try:
        hold_days = (date.fromisoformat(rnd["close_date"])
                     - date.fromisoformat(rnd["open_date"])).days
    except (ValueError, TypeError, KeyError):
        hold_days = None
    # avg_cost = amount (取得/建玉コスト) ÷ その コストに対応する株数。
    # 現物は買い株数、信用は「返済した建玉」の株数で割る。信用の amount は返済 fill の
    # 建玉コストだけを積むため、現引で現物へ振り替えた分は amount に入らない。分母を
    # 建玉株数にすると現引がある銘柄で粒度が食い違い avg_cost が実態より低く出る。
    if kind == "信用":
        settle_side = "buy" if rnd.get("is_short") else "sell"
        open_qty = sum(f["qty"] for f in fills if f["side"] == settle_side)
    else:
        open_qty = sum(f["qty"] for f in fills if f["side"] == "buy")
    return {
        "return_pct": return_pct,
        "hold_days": hold_days if hold_days is not None else 0,
        "avg_cost": total_cost / max(open_qty, 1),
        "amount": total_cost,
        "profit_amount": round(realized),
    }


def _episode_open_pl(rnd: dict, current_price: Optional[float]) -> Optional[dict]:
    """保有中 (未クローズ) 建玉ラウンドの実現損益 (部分売り分) と含み損益を計算する。

    実現損益: ラウンド内で既に売った分の確定損益 (現物=平均取得単価法、信用=建単価/settle_pl)。
    含み損益: 残っている建玉 × (current_price - 取得基準単価)。current_price は price_log の
      直近終値。取得基準は 現物=平均取得単価、信用=平均建単価。current_price が無ければ
      含みは None (実現分は出す)。

    Returns: {realized, unrealized, held_qty, avg_cost, cost_basis_total, return_pct}
      / 建玉も売りも無ければ None。unrealized は current_price 不明なら None。

    return_pct は保有中ラウンドの暫定リターン = (実現 + 含み) / 延べ取得コスト。
    クローズ済みの pl.return_pct と分母の考え方を揃えてあり、同じ列に並べて比較できる。
    含みが出せない (現在値不明・建玉方向が交錯) 場合は None。
    """
    fills = rnd["fills"]
    if not fills:
        return None
    kind = rnd["kind"]

    cost_basis_total = 0.0  # 延べ取得コスト (return_pct の分母、クローズ済み amount と同義)
    if kind == "現物":
        held_qty = 0
        avg_cost = 0.0
        realized = 0.0
        for f in fills:
            qty = f["qty"]
            price = f["price"]
            if f["side"] == "buy":
                new_qty = held_qty + qty
                avg_cost = (avg_cost * held_qty + price * qty) / new_qty if new_qty else 0.0
                held_qty = new_qty
                cost_basis_total += price * qty
            else:  # sell (部分売り)
                sell_qty = min(qty, held_qty) if held_qty > 0 else qty
                if held_qty > 0 and avg_cost > 0:
                    fill_pl = (price - avg_cost) * sell_qty
                    f["fill_pl"] = round(fill_pl)
                    f["fill_return_pct"] = fill_pl / (avg_cost * sell_qty) * 100
                realized += (price - avg_cost) * sell_qty
                held_qty -= qty
        cost_basis = avg_cost
    else:  # 信用
        # 建玉側と決済側は買建/売建で逆になる。売建 (空売り) は新規売で建て返済買で閉じる。
        is_short = rnd.get("is_short", False)
        open_side = "sell" if is_short else "buy"
        settle_side = "buy" if is_short else "sell"
        # 建玉方向と逆の返済が混ざると held_qty が実態とずれ含み評価が不正確になるため、
        # そのラウンドは含みを算出しない (安全側)。売建ラウンドを分離した今、これは
        # 「売建が無いのに返済買がある」等の想定外パターンのみが該当する。
        has_reverse_settle = any(
            f["side"] == open_side and (f.get("trade_kind") or "").startswith("信用返済")
            for f in fills
        )
        held_qty = 0
        avg_cost = 0.0
        realized = 0.0
        for f in fills:
            qty = f["qty"]
            price = f["price"]
            if f["side"] == open_side and (f.get("trade_kind") or "").startswith("信用新規"):
                new_qty = held_qty + qty
                avg_cost = (avg_cost * held_qty + price * qty) / new_qty if new_qty else 0.0
                held_qty = new_qty
                cost_basis_total += price * qty
            elif (f.get("trade_kind") or "") == "現引" and not is_short:
                # 現引は建玉を現物へ振り替える。信用側では建玉が減るだけで損益は
                # 確定しない (取得原価ごと現物ラウンドへ持ち越す)。ここで減算しないと
                # 現引後も建玉が残るラウンドで held_qty が実態より多くなる。
                # 振り替えた分の取得コストも現物ラウンド側で積み直されるので、信用側の
                # 分母から抜く (抜かないと銘柄単位の通算で同じコストを二重計上する)。
                held_qty -= qty
                cost_basis_total -= avg_cost * qty
            elif f["side"] == settle_side:  # 信用返済 (部分返済)
                settle_pl = f.get("settle_pl")
                tate_price = f.get("tate_price")
                if settle_pl is not None:
                    realized += settle_pl
                    f["fill_pl"] = settle_pl
                elif tate_price is not None:
                    # 売建は (建単価 - 買戻単価)、買建は (返済単価 - 建単価)
                    fill_pl = ((tate_price - price) if is_short
                               else (price - tate_price)) * qty
                    realized += fill_pl
                    f["fill_pl"] = round(fill_pl)
                else:
                    # 建単価不明時は平均建単価で近似
                    fill_pl = ((avg_cost - price) if is_short
                               else (price - avg_cost)) * qty
                    realized += fill_pl
                    f["fill_pl"] = round(fill_pl)
                if tate_price is not None:
                    f["fill_return_pct"] = f["fill_pl"] / (tate_price * qty) * 100
                tate_date = f.get("tate_date")
                if tate_date:
                    try:
                        f["hold_days"] = (date.fromisoformat(f["trade_date"]) - date.fromisoformat(tate_date)).days
                    except (ValueError, TypeError):
                        pass
                held_qty -= qty
        cost_basis = avg_cost
        if has_reverse_settle:
            return {
                "realized": round(realized),
                "unrealized": None,  # 建玉方向が交錯し含み評価不能
                "held_qty": held_qty if held_qty > 0 else 0,
                "avg_cost": cost_basis,
                "cost_basis_total": cost_basis_total,
                "return_pct": None,  # 含みが出せないので暫定リターンも出せない
            }

    if _is_qty_closed(held_qty):
        # 保有中扱いだが実質建玉が残っていない (空売り等・分割換算後の丸め誤差含む) → 含み対象なし
        return {
            "realized": round(realized),
            "unrealized": None,
            "held_qty": held_qty if held_qty > 0 else 0,
            "avg_cost": cost_basis,
            "cost_basis_total": cost_basis_total,
            # 建玉が残っていないので実現分だけで暫定リターンが確定する
            "return_pct": (realized / cost_basis_total * 100) if cost_basis_total > 0 else None,
        }

    unrealized = None
    if current_price is not None and cost_basis > 0:
        # 売建 (空売り) は現在値が下がるほど含み益なので符号が逆になる
        diff = ((cost_basis - current_price) if rnd.get("is_short")
                else (current_price - cost_basis))
        unrealized = round(diff * held_qty)

    return_pct = None
    if unrealized is not None and cost_basis_total > 0:
        return_pct = (realized + unrealized) / cost_basis_total * 100

    return {
        "realized": round(realized),
        "unrealized": unrealized,
        "held_qty": held_qty,
        "avg_cost": cost_basis,
        "cost_basis_total": cost_basis_total,
        "return_pct": return_pct,
    }


# ===========================================
# issue #398: 株式分割・併合対応
# ===========================================

_SPLIT_PRICE_JUMP_RATIO = 3.0  # 隣接単価がこの倍数以上/以下に飛べば分割・併合の疑い
_SPLIT_QTY_ZERO_TOL = 1e-6  # 換算後 float qty のクローズ判定許容誤差


def _is_qty_closed(qty: float) -> bool:
    """建玉が0に戻ったとみなせるか判定する (qty <= 0 の許容誤差付き版)。

    分割・併合換算で qty が float になった場合、丸め誤差で厳密な0にならず
    5.55e-17 のような残差が残ってクローズ判定を取り逃す (issue #398)。
    整数 fill のみの既存経路では qty <= 0 と等価に振る舞う。
    """
    return qty <= _SPLIT_QTY_ZERO_TOL


def _is_fractional_residual(qty: float) -> bool:
    """分割・併合後の端株精算で消える想定の1株未満残高か判定する。"""
    return _SPLIT_QTY_ZERO_TOL < qty < 1.0


def _is_genbutsu_qty_closed(qty: float) -> bool:
    """現物残高が実質クローズ済みか判定する。

    分割・併合比率によって 0.3333 株のような端株が残る場合、証券会社CSVには
    整数株の売却だけが出て端株精算が fill として入らないことがある。残高上は
    クローズ扱いにするが、損益は精算額を確認できないため別途 suspect にする。
    """
    return _is_qty_closed(qty) or _is_fractional_residual(qty)


def _detect_price_jumps(fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """現物 fill を約定日順に見て、建玉が継続したまま隣接単価が3倍以上/1/3以下に
    飛ぶ箇所を検出する。

    分割・併合は取引として記録されないため、単価の断絶が唯一の痕跡になる。
    133件の実データで検証済み (誤検出0件、1491 中外鉱業のみ検出、issue #398)。

    建玉を一度売り切ってから (残高0) 数年後に買い直した場合、その間の株価変動は
    分割・併合と無関係な通常の値上がり・値下がりであり分割候補ではない
    (PRレビュー対応)。残高を追跡し、直前の fill で残高が0になっていた場合は
    ジャンプ判定をスキップする。

    Returns: [{"before_date", "before_price", "after_date", "after_price"}]
    """
    genbutsu = sorted(
        (f for f in fills if not (f.get("trade_kind") or "").startswith("信用")),
        key=lambda f: (f.get("trade_date") or "", f.get("seq") or 0),
    )
    jumps = []
    held_qty = 0.0
    for a, b in zip(genbutsu, genbutsu[1:]):
        held_qty += a["qty"] if a["side"] == "buy" else -a["qty"]
        pa, pb = a.get("price"), b.get("price")
        if pa and pb and not _is_genbutsu_qty_closed(held_qty):
            ratio = pb / pa
            if ratio >= _SPLIT_PRICE_JUMP_RATIO or ratio <= 1 / _SPLIT_PRICE_JUMP_RATIO:
                jumps.append({
                    "before_date": a.get("trade_date"),
                    "before_price": pa,
                    "after_date": b.get("trade_date"),
                    "after_price": pb,
                })
    return jumps


def _uncovered_jumps(jumps: List[Dict[str, Any]],
                     events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """検知した単価ジャンプのうち、登録済みイベントでカバーされていないものを返す。

    「登録済みイベントが1件でもあれば安全」という判定は粗く、同一銘柄で後日
    発生した別の分割・併合を見逃す (PRレビュー対応)。ジャンプの日付境界
    (before_date, after_date] に ex_date が入る登録イベントがあればカバー済み。
    """
    return [
        jump for jump in jumps
        if not any(jump["before_date"] < ev["ex_date"] <= jump["after_date"]
                   for ev in events)
    ]


def _jump_affects_episode(jump: Dict[str, Any], ep: Dict[str, Any]) -> bool:
    """未カバーの単価ジャンプが、このエピソードの残高・損益に影響するか。

    ジャンプの (before_date, after_date] がエピソードの期間 [open_date, close_date]
    (保有中は close_date なし=無期限) と重なる場合のみ影響する。分割前に完結した
    無関係なラウンドまで split_suspect で隠さないため (PRレビュー対応)。
    """
    close_date = ep.get("close_date") or "9999-12-31"  # 保有中は無期限
    return jump["before_date"] < close_date and jump["after_date"] >= ep["open_date"]


def _split_event_affects_episode(ex_date: str, ep: Dict[str, Any]) -> bool:
    """pending の ex_date がエピソード期間に含まれるか判定する。"""
    close_date = ep.get("close_date") or "9999-12-31"  # 保有中は無期限
    return ep["open_date"] < ex_date <= close_date


def _apply_split_adjustments(fills: List[Dict[str, Any]],
                             events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """現物 fill のうち各イベントの ex_date より前のものを比率換算したコピーを返す。

    events は ex_date 昇順。各 fill には、自身の trade_date より後の ex_date を
    持つ全イベントの比率を掛け合わせた累積比率を適用する (古いイベントから順に)。
    数量 = qty * cum_ratio、単価 = price / cum_ratio、amount は不変。
    信用 fill・現引は素通しする (元の fill dict は変更しない)。

    現引は「信用建玉の現物化」で、qty は信用新規側の shinyo_qty 減算と現物側の
    genbutsu_qty 加算の両方に同じ値で使われる (_build_code_episodes)。現引だけ
    換算すると信用新規(未換算)と現引(換算後)で株数基準がずれ、shinyo_qty が
    0に戻らずクローズを取り逃す。現引を除外し分割前基準のまま扱う (簡易な安全策)。

    既知の限界 (PRレビュー #405 で指摘、対応複雑度とのバランスで見送り): 現引後に
    現物のまま分割・併合をまたいで売却すると、現引 fill (未換算) と売却 fill (換算後)
    の株数基準がずれ、端数が誤って保有中に残る可能性がある。現時点の実データでは
    分割検知銘柄 (1491, 9252) に現引が絡むケースは無い。単価変化が3倍以上/1/3以下なら
    _detect_price_jumps が検知するが、それ未満の比率では split_suspect も付かず
    残高が誤ったまま表示されうる。再発したら現引 fill に現物側専用の換算済み
    qty/price を別キーで持たせ、_build_code_episodes 側で使い分ける対応が必要。
    """
    if not events:
        return fills
    adjusted = []
    for f in fills:
        tk = f.get("trade_kind") or ""
        if tk.startswith("信用") or tk == "現引":
            adjusted.append(f)
            continue
        trade_date = f.get("trade_date") or ""
        cum_ratio = 1.0
        for ev in events:
            if trade_date < ev["ex_date"]:
                cum_ratio *= ev["ratio"]
        if cum_ratio == 1.0:
            adjusted.append(f)
            continue
        g = dict(f)
        g["qty"] = f["qty"] * cum_ratio
        g["price"] = f["price"] / cum_ratio
        adjusted.append(g)
    return adjusted


def _build_code_episodes(code_s: str, stock_name: str,
                         fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """1銘柄の fill (信用+現物混在、約定日昇順) を建玉ラウンドのエピソードに分ける。

    信用ラウンドと現物ラウンドを並行管理し、**現引で信用→現物へ建玉を振り替える**:
      - 信用新規買 (buy): 信用建玉を積む
      - 信用返済売 (sell): 信用建玉を減らし、建玉0で信用ラウンドをクローズ (損益確定)
      - 現引 (buy): 信用建玉が残っていればその分を信用ラウンドから抜き (振替、信用側は
        損益計上しない=現物へ持ち越し)、現物ラウンドに現引 buy を積む。信用建玉が0に
        なれば信用ラウンドをクローズ
      - 現物買 (buy): 現物建玉を積む
      - 現物売 (sell): 現物建玉を減らし、建玉0で現物ラウンドをクローズ
    保有0に戻らず残った建玉は保有中エピソードになる。取込対象期間より前に建てた
    信用玉の返済は建約定日で判別し、当期の信用新規と相殺せず期首持越しとして分ける。
    """
    # 約定日昇順。同日内は建玉を作る側 (信用新規・現引・現物買) を先に、玉を減らす側
    # (売り・返済) を後に処理する。信用売建の新規売も先にし、同日の返済買より前に
    # 建玉を作る。現引で現物化してから同日に売るケースにも対応する (6366 相当)。
    # 現引は同日の信用新規の玉を振り替えるので、信用新規より後に処理する (9337 相当)。
    def _sort_key(f):
        tk = f.get("trade_kind") or ""
        if tk == "現引":
            order = 1
        elif (tk.startswith("信用新規")
              or (f["side"] == "buy" and not tk.startswith("信用返済"))):
            order = 0
        else:
            order = 2
        return (f.get("trade_date") or "", order, f.get("seq") or 0)
    fills = sorted(fills, key=_sort_key)
    episodes: List[Dict[str, Any]] = []

    shinyo_fills: List[Dict[str, Any]] = []  # 現ラウンドの信用 fill
    shinyo_qty = 0
    shinyo_peak = 0
    # 信用売建 (空売り) は買建とは別ラウンドで追跡する。新規売で建て、返済買で閉じる。
    # 同一銘柄で買建と売建を同時に持ちうる (両建て) ため、状態を分けないと
    # 売建が買建の建玉を打ち消してラウンドが誤って閉じる (issue #387 レビュー対応)。
    short_fills: List[Dict[str, Any]] = []
    short_qty = 0
    short_peak = 0
    genbutsu_fills: List[Dict[str, Any]] = []  # 現ラウンドの現物 fill
    genbutsu_qty = 0
    genbutsu_peak = 0
    genbutsu_fractional_residual = False
    # 取込済みの信用新規日。これに無い建約定日の返済は、取込前からの持越し玉である。
    shinyo_open_dates = {
        f.get("trade_date")
        for f in fills
        if (f.get("trade_kind") or "").startswith("信用新規")
        and f["side"] == "buy"
        and f.get("trade_date")
    }
    short_open_dates = {
        f.get("trade_date")
        for f in fills
        if (f.get("trade_kind") or "").startswith("信用新規")
        and f["side"] == "sell"
        and f.get("trade_date")
    }
    carry_over_shinyo: Dict[str, List[Dict[str, Any]]] = {}
    carry_over_short_by_tate: Dict[str, List[Dict[str, Any]]] = {}
    carry_over_short_unknown: List[List[Dict[str, Any]]] = []

    def close_shinyo():
        nonlocal shinyo_fills, shinyo_qty, shinyo_peak
        if shinyo_fills:
            episodes.append(_finalize_round(code_s, "信用", stock_name, shinyo_fills, shinyo_peak))
        shinyo_fills = []
        shinyo_qty = 0
        shinyo_peak = 0

    def close_short():
        nonlocal short_fills, short_qty, short_peak
        if short_fills:
            episodes.append(_finalize_round(
                code_s, "信用", stock_name, short_fills, short_peak, is_short=True))
        short_fills = []
        short_qty = 0
        short_peak = 0

    def close_genbutsu():
        nonlocal genbutsu_fills, genbutsu_qty, genbutsu_peak, genbutsu_fractional_residual
        if genbutsu_fills:
            ep = _finalize_round(code_s, "現物", stock_name, genbutsu_fills, genbutsu_peak)
            if genbutsu_fractional_residual:
                ep["split_fractional_residual"] = True
            episodes.append(ep)
        genbutsu_fills = []
        genbutsu_qty = 0
        genbutsu_peak = 0
        genbutsu_fractional_residual = False

    # 銘柄全体の同時保有ピーク (信用買建 + 現物)。ラウンド単位の qty_peak は口座ごと・
    # ラウンドごとの最大なので、信用と現物を同時に持つ銘柄の実際のピークを表せない。
    # 建玉を増減させる各分岐が continue を使うため、次の fill を処理する前に前回の
    # 反映結果を拾う (ループ末尾では拾えない)。
    code_qty_peak = 0

    for f in fills:
        code_qty_peak = max(code_qty_peak, shinyo_qty + genbutsu_qty)
        tk = f.get("trade_kind") or ""
        qty = f["qty"]
        if tk == "現引":
            # 信用建玉 → 現物へ振替。信用側は残っていれば現引分だけ減らす。
            if shinyo_qty > 0:
                # 現引を信用ラウンドの「終了イベント」として明細・日付に反映する。
                # 損益 (_episode_pl_from_round) は返済 sell のみ集計するので side=buy の
                # 現引を加えても損益は不変。close_date/last_trade_date が最後の信用新規日
                # ではなく現引日になる (P2 レビュー対応)。
                shinyo_fills.append(f)
                shinyo_qty -= qty
                if shinyo_qty <= 0:
                    close_shinyo()  # 現引で信用建玉が尽きたらクローズ (損益は現物へ)
            # 現物ラウンドに現引 buy を積む (price=実質取得原価)
            genbutsu_fills.append(f)
            genbutsu_qty += qty
            genbutsu_peak = max(genbutsu_peak, genbutsu_qty)
        elif tk.startswith("信用"):
            tate_date = f.get("tate_date")
            # 売建 (新規売) と、それを閉じる返済買は空売りラウンド側で処理する。
            # 対応する新規売が取込範囲に無い返済買は、期首持越しの売建を閉じたもの。
            if tk.startswith("信用返済") and f["side"] == "buy":
                # 建約定日があれば対応する新規売の有無で確定できる。SBI のように
                # 建約定日が無い場合は、買建が残っていれば従来どおり想定外の混在として
                # 買建側に残し含み評価を無効化する。両方の建玉が無いときだけ持越し売建。
                if ((tate_date and tate_date not in short_open_dates)
                        or (not tate_date and short_qty <= 0 and shinyo_qty <= 0)):
                    if tate_date:
                        carry_over_short_by_tate.setdefault(tate_date, []).append(f)
                    else:
                        # SBI は建約定日を持たないため、返済ごとに独立した期首持越しとする。
                        carry_over_short_unknown.append([f])
                    continue
            is_short_side = (
                (tk.startswith("信用新規") and f["side"] == "sell")
                or (tk.startswith("信用返済") and f["side"] == "buy" and short_qty > 0)
            )
            if is_short_side:
                short_fills.append(f)
                if f["side"] == "sell":
                    short_qty += qty      # 新規売 = 建てる
                else:
                    short_qty -= qty      # 返済買 = 閉じる
                short_peak = max(short_peak, short_qty)
                if short_qty <= 0:
                    close_short()
                continue
            if (tk.startswith("信用返済") and f["side"] == "sell"
                    and tate_date and tate_date not in shinyo_open_dates):
                # 当期に対応する信用新規が無い返済は、当期の建玉を減らしてはいけない。
                # 同一建約定日の分割返済は一つの期首持越しエピソードにまとめる。
                carry_over_shinyo.setdefault(tate_date, []).append(f)
                continue
            shinyo_fills.append(f)
            if f["side"] == "buy":
                shinyo_qty += qty
            else:
                shinyo_qty -= qty
            shinyo_peak = max(shinyo_peak, shinyo_qty)
            if shinyo_qty <= 0:
                close_shinyo()
        else:  # 現物 / 現物(単元未満)
            genbutsu_fills.append(f)
            if f["side"] == "buy":
                genbutsu_qty += qty
            else:
                genbutsu_qty -= qty
            genbutsu_peak = max(genbutsu_peak, genbutsu_qty)
            if _is_genbutsu_qty_closed(genbutsu_qty):
                genbutsu_fractional_residual = _is_fractional_residual(genbutsu_qty)
                close_genbutsu()

    code_qty_peak = max(code_qty_peak, shinyo_qty + genbutsu_qty)  # 最後の fill の反映分

    # 保有中 (残った建玉)
    if shinyo_fills:
        episodes.append(_finalize_round(code_s, "信用", stock_name, shinyo_fills, shinyo_peak, closed=False))
    if short_fills:
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, short_fills, short_peak, closed=False, is_short=True))
    if genbutsu_fills:
        episodes.append(_finalize_round(code_s, "現物", stock_name, genbutsu_fills, genbutsu_peak, closed=False))
    for tate_date, carry_over_fills in carry_over_shinyo.items():
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, carry_over_fills, 0,
            carry_over=True, open_date=tate_date,
        ))
    for tate_date, carry_over_fills in carry_over_short_by_tate.items():
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, carry_over_fills, 0,
            carry_over=True, open_date=tate_date, is_short=True,
        ))
    for carry_over_fills in carry_over_short_unknown:
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, carry_over_fills, 0,
            carry_over=True, is_short=True,
        ))

    # 銘柄単位ビューが使う同時保有ピーク。全エピソードで同じ値 (銘柄の属性)。
    for ep in episodes:
        ep["code_qty_peak"] = code_qty_peak

    return episodes


def fill_date_range_by_broker(db_path: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """証券会社別の取込済み fill の最古・最新約定日を返す (issue #387、取込タイミング参考)。

    Returns: {"楽天": {"first": "2026-01-05", "last": "2026-07-31"}, "SBI": {...}}。
    broker 未設定の既存 fill は「楽天」に寄せる (表示補完と整合)。
    取込 fill が無ければ空 dict。
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    ranges: Dict[str, Dict[str, str]] = {}
    for f in ps.list_fills(db_path=db_path):
        td = f.get("trade_date")
        if not td:
            continue
        broker = f.get("broker") or "楽天"
        r = ranges.setdefault(broker, {"first": td, "last": td})
        if td < r["first"]:
            r["first"] = td
        if td > r["last"]:
            r["last"] = td
    return ranges


def _episodes_for_code(code_s: str, stock_name: str, fills: List[Dict[str, Any]],
                       all_split_adj: Dict[str, List[Dict[str, Any]]],
                       pending_events: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """1銘柄分の fill を建玉ラウンドのエピソードに再構成し split_suspect を付ける。

    build_fill_episodes() の銘柄ループ本体。エピソード単位チャート (issue #366) は
    展開のたびに全銘柄を再構成すると重い (0.5秒/回) ため、この関数で1銘柄分だけ
    組み立てる。メモ・戦略・保有中の含み損益はここでは付けない。
    """
    events = all_split_adj.get(code_s, [])
    if events:
        fills = _apply_split_adjustments(fills, events)
    # ジャンプ検知は換算後の fills に対して行う (PRレビュー対応: 未換算のまま
    # 検知すると、登録済みイベントで残高の基準が変わった後の残高追跡が崩れ、
    # 別の未登録イベントのジャンプを見逃す)。
    jumps = _detect_price_jumps(fills)
    code_episodes = _build_code_episodes(code_s, stock_name, fills)
    uncovered = _uncovered_jumps(jumps, events)
    pending_dates = pending_events.get(code_s, [])
    for ep in code_episodes:
        if ep["kind"] == "信用" and any(
                _split_event_affects_episode(ev["ex_date"], ep) for ev in events):
            # 信用 fill は約定損益・建単価の基準を保つため換算しない。そのため、
            # 登録済みの分割・併合をまたぐ信用エピソードも集計から除外する。
            ep["split_suspect"] = True
        elif any(d != "unknown" and _split_event_affects_episode(d, ep)
                 for d in pending_dates):
            ep["split_suspect"] = True
        elif "unknown" in pending_dates and not ep["closed"]:
            ep["split_suspect"] = True
        elif ep["kind"] != "現物":
            continue
        elif ep.get("split_fractional_residual"):
            ep["split_suspect"] = True
        elif any(_jump_affects_episode(j, ep) for j in uncovered):
            ep["split_suspect"] = True
    return code_episodes


def build_fill_episodes(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """全 fill を建玉ラウンド単位のエピソードに再構成する (issue #387 Phase4b)。

    銘柄ごとに信用・現物を同一時系列で処理し、保有 (建玉) 株数が 0 → 建 → 0 に戻る
    1 サイクルを 1 エピソードとする。**現引は信用建玉を現物へ振り替える** (信用側の
    建玉を減らし現物側に取得原価で積む)。信用は返済 fill の建単価/決済損益で損益確定。

    各エピソード dict:
      code_s, stock_name, kind ("現物"/"信用"), open_date, close_date,
      last_trade_date (ラウンド内の最終約定日), qty_peak (最大建玉),
      closed (bool), fills (内部の個別 fill 明細リスト),
      carry_over (bool: 取込対象期間より前の信用建玉の返済),
      pl (クローズ済みのみ: _episode_pl_from_round の結果, 未クローズは None),
      open_pl (保有中のみ: realized/unrealized/held_qty/avg_cost),
      split_suspect (bool, 分割・併合の疑いで未換算、issue #398/#435。
        残高・損益が分割・併合未換算で誤っている可能性があるため画面上は数値を隠す)

    Returns: 最終約定日 (買い増し・部分売り含むラウンド内の最新の取引日) 降順の
    エピソードリスト。保有中エピソードも最後に約定した日で並ぶ。
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    all_fills = ps.list_fills(db_path=db_path)
    if not all_fills:
        return []

    # 銘柄コードでグループ化 (信用・現物を同一時系列で処理するため口座種別で分けない)
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for f in all_fills:
        by_code.setdefault(f["code_s"], []).append(f)

    names = helpers._bulk_resolve_stock_names(list(by_code.keys()))

    # issue #398: 分割・併合の疑いがある銘柄を検知し、登録済みイベントがあれば
    # 現物 fill を換算したコピーに差し替えてからエピソード再構成に渡す。
    # 未カバーのジャンプがあれば換算せず既存動作を維持し、その期間と重なる
    # エピソードにのみ split_suspect を付与する (PRレビュー対応:
    # 「登録済みイベントが1件でもあれば安全」という銘柄単位の判定は粗く、同一銘柄で
    # 後日発生した別の分割・併合を見逃す。また銘柄単位で全エピソードに付けると、
    # 分割前に完結した無関係なラウンドの正しい損益まで隠してしまう)。
    # pending_review は --check-splits の (a)単価ジャンプ/(b)エピソード期間総当たりの検知結果を
    # 拒否リストとして反映する (webapp は yfinance を呼ばないため (b) を自力では検知できない)。
    # pending_review の ex_dates がエピソード期間に含まれる場合は、クローズ済みでも
    # split_suspect を維持する。2:1 分割など単価ジャンプ閾値未満のイベントは
    # クローズ後に警告が外れると誤った実現損益が集計へ戻ってしまうため。
    all_split_adj = ps.list_all_split_adjustments(db_path=db_path)
    pending_events = ps.list_pending_review_events(db_path=db_path)
    episodes: List[Dict[str, Any]] = []
    for code_s, fills in by_code.items():
        episodes.extend(_episodes_for_code(
            code_s, names.get(code_s, ""), fills, all_split_adj, pending_events))

    # 保有中エピソードに実現損益 (部分売り分) と含み損益 (残玉評価) を付与 (issue #387 Phase4b)。
    # 含みは price_log の直近終値を現在値とする。銘柄をバルク取得して N+1 を避ける。
    open_codes = {e["code_s"] for e in episodes if not e["closed"]}
    latest_prices: Dict[str, Optional[float]] = {}
    if open_codes:
        price_logs = helpers._bulk_price_logs(list(open_codes))
        for code_s, log in price_logs.items():
            if log:
                latest = max(log, key=lambda x: x[0])  # (date, close) の最新
                latest_prices[code_s] = float(latest[1])
    for ep in episodes:
        if not ep["closed"]:
            # 往復行 (issue #421) の保有中ロットも現在値で含みを出すため保持する
            ep["current_price"] = latest_prices.get(ep["code_s"])
            ep["open_pl"] = _episode_open_pl(ep, ep["current_price"])

    # 建玉ラウンド単位の振り返りメモ (issue #387 Phase2) を一括で紐付ける。
    # メモは fill と独立レイヤーに保存され、エピソードキーで対応する。
    memos = ps.list_fill_memos(db_path=db_path)
    for ep in episodes:
        ep["episode_key"] = ps.fill_episode_key(
            ep["code_s"], ep["kind"], ep["first_seq"]
        )
        ep["review_memo"] = memos.get(ep["episode_key"], "")

    # エピソードに焼き付けた売買戦略 (issue #419) を紐付ける。
    # ここは純粋な読み取り経路なので DB には書かない (指紋の確定保存は
    # seal_episode_fingerprints / 付与時に行う)。
    strategies = ps.list_episode_strategies(db_path=db_path)
    for ep in episodes:
        record = strategies.get(ep["episode_key"])
        ep["trade_idea"] = record.get("trade_idea", "") if record else ""
        ep["strategy_source"] = record.get("source", "") if record else ""
        # 指紋不一致 = キーは生きているが中身が変わった (遡り取込でラウンドの
        # 区切りが変わった)。戦略が別の取引を指している可能性があるため、
        # 戦略別比較の母数から外して「要再確認」に退避する。
        #
        # 指紋はクローズ確定時にしか焼かない (保有中は買い増しで seq 列が伸びるのが
        # 正常動作なので None)。よって指紋があるのにクローズしていないエピソードは、
        # 遡り取込でラウンドが繋がり直して未決済に戻ったケース = ズレそのもの。
        saved_fp = record.get("fingerprint") if record else None
        ep["strategy_drift"] = bool(
            saved_fp
            and (not ep["closed"]
                 or saved_fp != ps.episode_fingerprint(ep["fills"]))
        )
        # 一括付与時の「保有期間がばらついている」警告に使う (テンプレートで日付
        # 計算をしないで済むよう、ここで出しておく)
        ep["hold_days_calc"] = episode_hold_days(ep)

    # 最終約定日 (最新の取引がある順) 降順、同日は銘柄コード昇順
    episodes.sort(key=lambda e: e["code_s"])
    episodes.sort(key=lambda e: e["last_trade_date"], reverse=True)
    return episodes


def summarize_by_strategy(episodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """戦略別の成績を集計する (issue #419)。

    母数定義は _summarize_fill_episodes と完全に揃える。すなわち split_suspect
    (分割・併合の疑いだが未換算, issue #398) は除外する。除外を継承しないと、
    損益が壊れていると既知のエピソードが戦略成績に再混入して比較表が壊れる。

    戦略が付いているものを上部の比較対象とし、下記2つは比較の母数から外して
    下部に分離する:
      - 未分類: まだ戦略が付いていない
      - 要再確認: 指紋不一致 (遡り取込でひもづけがずれた可能性がある)

    どちらも件数と損益合計は出す (実在する取引で損益は正しいため、全体の
    実現損益合計は既存サマリーと一致する)。勝率・平均損益は比較対象ではない。

    Returns: {"strategies": [(戦略名, part), ...], "unclassified": part,
              "drifted": part, "total_pl": 全体の実現損益, "unclassified_share": 0-1}

    unclassified_share はエピソード単位の絶対損益で測る。バケットの net を使うと
    利益と損失が相殺され、分類が進んでいないのにシェアが小さく見える。
    """
    valid = [ep for ep in episodes if not ep.get("split_suspect")]

    def _part(eps: List[Dict[str, Any]]) -> Dict[str, Any]:
        # 平均保有日数は件数・勝率・期待値と同じ母数 (損益を出せたクローズ済み) から
        # 計算する。母数を広く取ると「件数1」の行に2件以上を平均した保有日数が並び、
        # 成績1件あたりの平均と誤読される (建値や決済損益が欠けた期首持越しなど)
        priced = [ep for ep in eps if ep["closed"] and ep["pl"]]
        pls = [ep["pl"] for ep in priced]
        hold = [d for d in (episode_hold_days(ep) for ep in priced) if d is not None]
        return {
            "summary": calc_trade_summary(pls),
            "total_pl": sum(p["profit_amount"] for p in pls
                            if p["profit_amount"] is not None),
            # 分類状況の指標に使う絶対損益。バケット内で利益と損失が相殺されると
            # 規模を見失う (未分類に +100万/-100万があると 0 になる) ため、
            # エピソード単位の絶対値を積む
            "abs_pl": sum(abs(p["profit_amount"]) for p in pls
                          if p["profit_amount"] is not None),
            "priced_count": len(pls),
            "closed_count": sum(1 for ep in eps if ep["closed"]),
            "open_count": sum(1 for ep in eps if not ep["closed"]),
            "avg_hold_days": round(sum(hold) / len(hold)) if hold else None,
        }

    by_idea: Dict[str, List[Dict[str, Any]]] = {}
    unclassified: List[Dict[str, Any]] = []
    drifted: List[Dict[str, Any]] = []
    for ep in valid:
        if ep.get("strategy_drift"):
            drifted.append(ep)
        elif ep.get("trade_idea"):
            by_idea.setdefault(ep["trade_idea"], []).append(ep)
        else:
            unclassified.append(ep)

    strategies = [(name, _part(eps)) for name, eps in by_idea.items()]
    # 実現損益の大きい戦略から並べる (成績への寄与が大きい順)
    strategies.sort(key=lambda x: abs(x[1]["total_pl"]), reverse=True)

    unclassified_part = _part(unclassified)
    drifted_part = _part(drifted)
    total_abs = sum(p[1]["abs_pl"] for p in strategies) \
        + unclassified_part["abs_pl"] + drifted_part["abs_pl"]
    return {
        "strategies": strategies,
        "unclassified": unclassified_part,
        "drifted": drifted_part,
        "total_pl": (sum(p[1]["total_pl"] for p in strategies)
                     + unclassified_part["total_pl"] + drifted_part["total_pl"]),
        # 未分類の損益シェアが大きいうちは戦略別比較の精度が限定的
        "unclassified_share": (unclassified_part["abs_pl"] / total_abs
                               if total_abs else 0.0),
    }


def episode_hold_days(ep: Dict[str, Any]) -> Optional[int]:
    """エピソードの保有日数 (open_date〜close_date)。保有中・日付不正は None。"""
    return _round_trip_days(ep.get("open_date"), ep.get("close_date"))


def count_orphan_strategies(episodes: List[Dict[str, Any]],
                            db_path: Optional[str] = None) -> int:
    """現存エピソードに対応しない戦略ひもづけ (orphan) の件数 (issue #419)。

    遡り取込でラウンドの区切りが変わり、保存済みキーの参照先が消えた状態。
    指紋不一致 (strategy_drift) と違って印を立てる相手のエピソードが無いため、
    エピソードの属性ではなく画面レベルの警告として出す。
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    live_keys = {ep["episode_key"] for ep in episodes}
    strategies = ps.list_episode_strategies(db_path=db_path)
    return sum(1 for key in strategies if key not in live_keys)


def seal_episode_fingerprints(db_path: Optional[str] = None) -> Dict[str, int]:
    """クローズ確定したエピソードの指紋を焼き付ける (issue #419)。

    「保有中に戦略を付与 → 後でクローズ」の穴を埋める。保有中は指紋を持てない
    (買い増しで seq 列が伸びるのが正常) ため、クローズが確定した時点で初めて焼く。
    クローズ済みへの後付けは set_episode_strategy が付与時に焼くので、ここは
    fingerprint 未設定のものだけを拾えばよい。

    同時に、保有中は判定できなかった time_horizon 足切りをここで再評価する。
    素通しシードは「保留」であって「合格」ではないため、2日で閉じた中長期戦略の
    ような矛盾は未分類に戻す。人が確認した manual は再評価しない。

    fill 取込直後に呼ぶ。エピソードの姿が変わりうるのは fill が増えた時だけ。
    冪等 (既に指紋があるレコードは触らない)。

    戻り値: {"sealed": 指紋を確定した数, "dropped": 矛盾で未分類に戻した数}
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    episodes = build_fill_episodes(db_path=db_path)
    strategies = ps.list_episode_strategies(db_path=db_path)
    horizons = {t["name"]: t.get("time_horizon", "")
                for t in ps.list_trade_ideas(db_path=db_path)}

    sealed = dropped = 0
    for ep in episodes:
        record = strategies.get(ep["episode_key"])
        if not record or not ep["closed"] or record.get("fingerprint"):
            continue
        hold_days = episode_hold_days(ep)
        if record.get("source") == "seed" and not ps.is_hold_days_consistent(
            horizons.get(record["trade_idea"], ""), hold_days
        ):
            ps.set_episode_strategy(ep["episode_key"], "", db_path=db_path)
            dropped += 1
            continue
        ps.set_episode_strategy(
            ep["episode_key"], record["trade_idea"],
            source=record.get("source", "manual"),
            fingerprint=ps.episode_fingerprint(ep["fills"]),
            hold_days=hold_days,
            db_path=db_path,
        )
        sealed += 1
    if sealed or dropped:
        from ks_util import log_print

        log_print("episode_strategy 指紋確定",
                  f"sealed={sealed}", f"dropped={dropped}")
    return {"sealed": sealed, "dropped": dropped}


def _round_trip_days(open_date: Optional[str], close_date: Optional[str]) -> Optional[int]:
    """建日〜決済日の保有日数。どちらか欠けるか不正な日付なら None。"""
    if not open_date or not close_date:
        return None
    try:
        return (date.fromisoformat(close_date) - date.fromisoformat(open_date)).days
    except (ValueError, TypeError):
        return None


def _make_round_trip(open_fill: Optional[Dict[str, Any]], close_fill: Optional[Dict[str, Any]],
                     qty: float, open_date: Optional[str],
                     open_price: Optional[float]) -> Dict[str, Any]:
    """往復1行を組み立てる (issue #421)。

    open 側 (建て) と close 側 (決済) のどちらかが欠ける行もある:
      - close_fill=None: 未決済で残っている建玉 → 「保有中」行
      - open_fill=None:  建玉を特定できない決済 (建情報なしの信用返済、期首持越し)
                         → 売りのみ行
    損益は呼び出し側が close_fill の fill_pl / fill_return_pct から埋めるか、
    現物 FIFO のようにロット単位で計算した値を渡す。
    """
    close_date = close_fill.get("trade_date") if close_fill else None
    return {
        "open_date": open_date,
        "close_date": close_date,
        "qty": qty,
        "open_price": open_price,
        "close_price": close_fill.get("price") if close_fill else None,
        "hold_days": _round_trip_days(open_date, close_date),
        "broker": (close_fill or open_fill or {}).get("broker", ""),
        "closed": close_fill is not None,
        "genbiki": False,  # 現引による現物への振替 (決済ではないので損益を出さない)
        "unrealized": False,  # pl が含み損益 (未確定) かどうか
        "pl": None,
        "return_pct": None,
    }


def _match_open_lots(open_pool: List[Dict[str, Any]], tate_date: Optional[str],
                     tate_price: Optional[float],
                     broker: Optional[str] = None) -> List[Dict[str, Any]]:
    """建日・建単価に一致する建玉を古い順に返す (issue #421)。

    証券会社CSVの建玉情報 (tate_date/tate_price) で引き当てる。同じ建日に複数の
    新規がある場合は建単価でも絞る (4258 の 02-13 に 3,200/3,100 の2本があるなど)。

    **建情報が指定されているのに一致しなければ未照合として扱う** (空リストを返す)。
    別ロットへフォールバックすると、CSVが指定した建玉ではない玉が「決済済み」になり、
    実際に残っている建玉が逆になる (PRレビュー指摘)。建情報が無い場合のみ、残っている
    建玉を古い順に返す (FIFO フォールバック)。

    broker を渡すと同じ証券会社の建玉だけを対象にする。同一銘柄を複数社で同時保有
    しているとき (実データで22エピソード)、他社の建玉と突き合わせると誤った建値の
    リターンを出し、残っている証券会社も逆になる (PRレビュー指摘)。
    他社へのフォールバックはしない — 建玉が取込範囲外の決済が別の証券会社の建玉を
    消費すると、架空の損益を出したうえでその建玉の保有株数まで消えるため。
    """
    pool = [c for c in open_pool if c["remain"] > 0
            and (not broker or c["fill"].get("broker") == broker)]
    if tate_date is None and tate_price is None:
        return pool  # 建情報なし → FIFO で古い順に充当
    return [c for c in pool
            if (not tate_date or c["fill"].get("trade_date") == tate_date)
            and (tate_price is None or c["fill"].get("price") == tate_price)]


def _consume_open_lots(open_pool: List[Dict[str, Any]], qty: float,
                       tate_date: Optional[str] = None,
                       tate_price: Optional[float] = None,
                       broker: Optional[str] = None) -> List[tuple]:
    """建玉プールから qty 株を引き当て、[(建玉fill, 引当株数), ...] を返す (issue #421)。

    1本の返済が複数ロットにまたがる場合は順に按分する。先頭ロットから全数量を
    引くと残数が負になり、決済済みのロットが「保有中」として残る。
    引き当てきれなかった分 (取込範囲外の建玉など) は返り値に含めない。
    broker を渡すと同じ証券会社の建玉を優先して引き当てる。
    """
    taken: List[tuple] = []
    remain = qty
    for cand in _match_open_lots(open_pool, tate_date, tate_price, broker):
        if remain <= 0:
            break
        take = min(remain, cand["remain"])
        cand["remain"] -= take
        remain -= take
        taken.append((cand["fill"], take))
    if remain > 0 and (tate_date is not None or tate_price is not None):
        # 建情報に一致する建玉が取込範囲内で足りない。建玉の総数は釣り合っているのに
        # CSVの建情報が実際の建玉と対応しないことがある (4377: 建1000/返済900+現引100
        # で差0なのに、一部の返済の tate_date が別の建玉を指す)。ここで消費しないと
        # 決済済みの建玉が「保有中」として残る。**建値は CSV の値を使う**ので、
        # どのロットを消費したかは表示に影響しない。
        for cand in _match_open_lots(open_pool, None, None, broker):
            if remain <= 0:
                break
            take = min(remain, cand["remain"])
            cand["remain"] -= take
            remain -= take
    return taken


def _consume_exact_open_lots(open_pool: List[Dict[str, Any]], qty: float,
                             tate_date: Optional[str], tate_price: Optional[float],
                             broker: Optional[str] = None) -> None:
    """建日・建値に一致するロットだけを消費する（推定表示の候補絞り込み用）。"""
    remain = qty
    for cand in _match_open_lots(open_pool, tate_date, tate_price, broker):
        if remain <= 0:
            break
        take = min(remain, cand["remain"])
        cand["remain"] -= take
        remain -= take


def _build_shinyo_round_trips(ep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """信用エピソードの往復行を作る (issue #421)。

    信用返済 fill は証券会社CSV由来の tate_date / tate_price を持つため、
    **FIFO 等で推定せずこの対応をそのまま使う**。これが証券会社が実際に決済した
    建玉の対応であり、推定するとリターンが実態とずれる (4258 の 07/06 返済は
    04/08 建 → 89日 → +54.43% だが、FIFO では 06/16 の返済に当たってしまう)。

    建情報を持たない返済 (SBI 等で 17/487 本) は建玉を特定できないため、
    推測でペアを作らず売りのみ行として出す (誤った建値でリターンを出すほうが有害)。
    ただし建玉自体は消費するので、決済済みの玉が「保有中」行として残ることはない。
    """
    is_short = ep.get("is_short", False)
    settle_side = "buy" if is_short else "sell"
    # 建玉側 fill を建日ごとに残株数付きで保持し、返済の tate_date と突き合わせる。
    # 同じ建日に複数の新規がある場合は建単価でも絞る (4258 02/13 の 3,200/3,100 など)。
    open_pool: List[Dict[str, Any]] = []
    for f in ep["fills"]:
        if f["side"] != settle_side and f.get("trade_kind", "").startswith("信用新規"):
            open_pool.append({"fill": f, "remain": f["qty"]})

    # 建情報付きの後続返済をあらかじめ除外した照合用プール。建情報なし返済でも、
    # これで残ロットが1本に絞れる場合だけ明細の建日・建値を「推定」として表示する。
    inference_pool = [{"fill": c["fill"], "remain": c["remain"]} for c in open_pool]
    for f in ep["fills"]:
        if f.get("trade_kind") == "現引":
            _consume_exact_open_lots(inference_pool, f["qty"], f.get("tate_date"),
                                     f.get("tate_price"), f.get("broker"))
        elif f["side"] == settle_side and f.get("tate_price") is not None:
            _consume_exact_open_lots(inference_pool, f["qty"], f.get("tate_date"),
                                     f["tate_price"], f.get("broker"))

    rows: List[Dict[str, Any]] = []
    for f in ep["fills"]:
        # 現引は信用建玉を現物へ振り替える取引。side="buy" なので返済側の分岐に
        # 入らないが、建玉は消える。ここで建玉を減らさないと振替済みの玉が
        # 「保有中」行として残り続ける (4258 の 2025-03-19 建玉が該当)。
        # 損益は現物側へ持ち越すため、信用側では計上しない。
        if f.get("trade_kind") == "現引":
            # 現引もCSVの建玉情報を持つ (6366 の 05-11 現引は 04-22 建玉が対象)。
            # 先頭から機械的に消費すると別の建玉を消してしまい、実際に振り替えた
            # 玉が「保有中」行として残る。建情報があればそれで引き当てる。
            taken = _consume_open_lots(open_pool, f["qty"], f.get("tate_date"),
                                       f.get("tate_price"), f.get("broker"))
            for cf, take in taken:
                row = _make_round_trip(cf, f, take,
                                       f.get("tate_date") or cf.get("trade_date"),
                                       f.get("tate_price") or cf.get("price"))
                row["genbiki"] = True  # 決済ではなく現物への振替
                rows.append(row)
            remain = f["qty"] - sum(t for _, t in taken)
            if remain > 0:
                # 対応する建玉が取込範囲に無い現引 (期首持越し玉の現引)
                row = _make_round_trip(None, f, remain, f.get("tate_date"),
                                       f.get("tate_price"))
                row["genbiki"] = True
                rows.append(row)
            continue
        if f["side"] != settle_side:
            continue
        tate_date = f.get("tate_date")
        tate_price = f.get("tate_price")
        # 建情報なし返済は、後続の建情報付き返済を除いた残ロットが一意な場合だけ
        # 建日・建値を補完する。それ以外は従来どおり建玉不明として表示する。
        if tate_date is None and tate_price is None:
            candidates = _match_open_lots(inference_pool, None, None, f.get("broker"))
            inferred = None
            if len(candidates) == 1 and candidates[0]["remain"] >= f["qty"]:
                inferred = candidates[0]["fill"]
            if inferred is not None:
                tate_date = inferred.get("trade_date")
                tate_price = inferred.get("price")
                _consume_open_lots(inference_pool, f["qty"], tate_date, tate_price,
                                   f.get("broker"))
                matched = _consume_open_lots(open_pool, f["qty"], tate_date, tate_price,
                                              f.get("broker"))
                first = matched[0][0] if matched else inferred
                row = _make_round_trip(first, f, f["qty"], tate_date, tate_price)
                row["inferred_open"] = True
            else:
                # 建玉は消費するが建値は伏せる。消費しないと決済済みの玉が保有中に残る。
                _consume_open_lots(open_pool, f["qty"], broker=f.get("broker"))
                row = _make_round_trip(None, f, f["qty"], None, None)
            if "fill_pl" in f:
                row["pl"] = f["fill_pl"]
            if "fill_return_pct" in f:
                row["return_pct"] = f["fill_return_pct"]
            if "hold_days" in f:
                row["hold_days"] = f["hold_days"]
            rows.append(row)
            continue
        # 対応する建玉 fill を建日 (+建単価) で引き当て、残株数を減らす。
        # 表示上の建日はCSVの tate_date を正とする (建玉 fill が取込範囲外でも出せる)。
        # 同じ建日・建単価の新規が複数本あり返済が先頭ロットの残数を超える場合は、
        # 候補ロットへ順に按分する。先頭から全数量を引くと残数が負になり、後続ロットが
        # 決済済みなのに「保有中」として残る (PRレビュー指摘)。
        matched = _consume_open_lots(open_pool, f["qty"], tate_date, tate_price,
                                     f.get("broker"))
        first = matched[0][0] if matched else None
        open_price = tate_price if tate_price is not None else (
            first["price"] if first else None)
        open_date = tate_date or (first.get("trade_date") if first else None)
        row = _make_round_trip(first, f, f["qty"], open_date, open_price)
        # 損益は既存の fill 単位計算をそのまま使う (計算ロジックを二重化しない)。
        if "fill_pl" in f:
            row["pl"] = f["fill_pl"]
        if "fill_return_pct" in f:
            row["return_pct"] = f["fill_return_pct"]
        if "hold_days" in f:
            row["hold_days"] = f["hold_days"]
        rows.append(row)

    # 決済されずに残った建玉は「保有中」行
    for cand in open_pool:
        if cand["remain"] > 0:
            cf = cand["fill"]
            rows.append(_make_round_trip(cf, None, cand["remain"],
                                         cf.get("trade_date"), cf.get("price")))
    return rows


def _oldest_lot(lots: List[Dict[str, Any]],
                broker: Optional[str]) -> Optional[Dict[str, Any]]:
    """FIFO キューから充当対象のロットを選ぶ (issue #421)。

    同じ証券会社の最古ロットを返す。無ければ None (他社ロットへは充当しない)。
    他社へフォールバックすると、買付が取込範囲外の売却が別の証券会社の建玉を
    消費し、架空の損益を出したうえでその建玉の保有株数まで消える
    (楽天100株保有 + SBI売却のみ → 楽天の建値で +150,000円 と誤表示、PRレビュー指摘)。
    lots は買付順に並んでいる前提 (先頭が最古)。
    """
    for lot in lots:
        if not broker or lot["fill"].get("broker") == broker:
            return lot
    return None


def _build_genbutsu_round_trips(ep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """現物エピソードの往復行を FIFO (先入先出) で作る (issue #421)。

    現物の売り fill には建単価・建日が無く、どの買いに対応するかCSVに情報が無い。
    FIFO で古い買いから順に充当する (税務・証券会社の考え方に近く、ロットごとの
    リターンが実際の建値を反映するため)。

    **注意**: エピソードの実現損益 (_episode_pl_from_round) は平均取得単価法で
    計算されており、部分売却で建玉が残るラウンドでは往復行の損益合計と一致しない
    (100@100買→100@200買→100@150売100株 で FIFO +5,000 / 平均法 0)。
    全株売却されれば総額は一致する。振り返り目的では実際の建値を反映する FIFO を
    優先し、不一致は許容する方針 (issue #421)。
    """
    lots: List[Dict[str, Any]] = []  # FIFO キュー: {fill, remain}
    rows: List[Dict[str, Any]] = []
    for f in ep["fills"]:
        if f["side"] == "buy":
            lots.append({"fill": f, "remain": f["qty"]})
            continue
        # 売り: 古いロットから充当。1つの売りが複数ロットにまたがる場合は行を分ける。
        # 同一銘柄を複数社で同時保有していると、他社の買いロットと突き合わせて誤った
        # 建値のリターンを出してしまう (実データで7行該当)。同じ証券会社のロットを
        # 優先し、無ければ従来どおり最古のロットへ充当する (PRレビュー指摘)。
        remain = f["qty"]
        while remain > 0 and lots:
            lot = _oldest_lot(lots, f.get("broker"))
            if lot is None:
                break  # 同社の買いロットが無い → 建玉不明の売りとして下で処理
            take = min(remain, lot["remain"])
            bf = lot["fill"]
            row = _make_round_trip(bf, f, take, bf.get("trade_date"), bf.get("price"))
            cost = bf["price"] * take
            profit = (f["price"] - bf["price"]) * take
            row["pl"] = round(profit)
            row["return_pct"] = (profit / cost * 100) if cost else None
            rows.append(row)
            remain -= take
            lot["remain"] -= take
            if lot["remain"] <= 0:
                lots.remove(lot)
        if remain > 0:
            # 充当できる買いが無い (取込範囲外で取得した株の売却など)
            rows.append(_make_round_trip(None, f, remain, None, None))

    # 売られずに残ったロットは「保有中」行
    for lot in lots:
        if lot["remain"] > 0:
            bf = lot["fill"]
            rows.append(_make_round_trip(bf, None, lot["remain"],
                                         bf.get("trade_date"), bf.get("price")))
    return rows


def _set_unrealized(open_rows: List[Dict[str, Any]], current_price: Optional[float],
                    is_short: bool) -> None:
    """保有中ロットに含み損益を設定する (issue #421)。

    残数量 × (現在値 - 建値)。売建 (空売り) は「高く売って安く買い戻す」ので符号が逆。
    現在値が取れない、建値が不明 (取込範囲外の建玉) の行は None のままにする。
    エピソード行の含み損益 (_episode_open_pl) と定義を揃えてある。
    """
    if current_price is None:
        return
    for r in open_rows:
        if r["open_price"] is None or not r["open_price"]:
            continue
        diff = ((r["open_price"] - current_price) if is_short
                else (current_price - r["open_price"]))
        r["pl"] = round(diff * r["qty"])
        r["return_pct"] = diff / r["open_price"] * 100
        r["unrealized"] = True  # 確定損益ではなく評価額 (テンプレートで淡く出す)


def build_round_trips(ep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """エピソードの fill を「買→売」の往復1行に畳む (issue #421)。

    実際に下した売買判断の単位で明細を読めるようにするのが目的。
    信用は証券会社CSVの建玉対応 (tate_date/tate_price)、現物は FIFO で対応づける。

    並び順: 保有中 (未決済) を先頭に、続いて決済済みを決済日の降順 (最新が上)。
    保有中はまだ決済日が無く「最新」の側なので、決済済みより上に置く。
    保有中どうし・決済済みどうしは買付日/決済日の降順。

    保有中ロットには現在値 (ep["current_price"]) から含み損益を付ける。
    エピソード行の含み損益と定義を揃える (残数量 × (現在値 - 建値)、売建は符号が逆)。
    """
    if ep["kind"] == "信用":
        rows = _build_shinyo_round_trips(ep)
    else:
        rows = _build_genbutsu_round_trips(ep)
    closed = [r for r in rows if r["closed"]]
    open_rows = [r for r in rows if not r["closed"]]
    _set_unrealized(open_rows, ep.get("current_price"), ep.get("is_short", False))
    closed.sort(key=lambda r: (r["close_date"] or "", r["open_date"] or ""), reverse=True)
    open_rows.sort(key=lambda r: r["open_date"] or "", reverse=True)
    return open_rows + closed


def build_stock_rollups(episodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """建玉ラウンド・エピソードを銘柄単位に集約する (issue #391)。

    build_fill_episodes() の結果を code_s でグループ化し、銘柄ごとに畳んだ
    集約 dict を返す。損益は各エピソードが持つ pl / open_pl を足し合わせるだけで、
    fill からの再計算はしない (計算ロジックの二重化を避ける)。

    pl の対象は calc_trade_summary の母数定義 (ep["closed"] and ep["pl"]) と完全に
    一致させる。これにより実現損益合計・期待値がエピソード単位と銘柄単位で厳密に
    一致する (金額加重ゆえグループ化に依存しない)。
    """
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for ep in episodes:
        by_code.setdefault(ep["code_s"], []).append(ep)

    rollups: List[Dict[str, Any]] = []
    for code_s, eps in by_code.items():
        # split_suspect (分割・併合の疑いだが未換算) は残高・損益が誤っている可能性が
        # あるため、この銘柄の全集計から一貫して除外する (issue #398)。実現損益だけ
        # 除外して含み損益は含める、といった半端な状態にすると 9252 のように銘柄行の
        # 中で基準が食い違う。エピソード単位ビューも split_suspect の数値は — にする。
        agg_eps = [ep for ep in eps if not ep.get("split_suspect")]
        priced = [ep["pl"] for ep in agg_eps if ep["closed"] and ep["pl"]]
        if priced:
            amount = sum(p["amount"] for p in priced)
            profit_amount = sum(p["profit_amount"] for p in priced)
            pl = {
                "return_pct": profit_amount / amount * 100,
                "hold_days": sum(p["hold_days"] for p in priced),
                "amount": amount,
                "profit_amount": profit_amount,
            }
        else:
            pl = None

        open_pls = [ep["open_pl"] for ep in agg_eps
                    if not ep["closed"] and ep.get("open_pl")]
        open_realized = sum(op["realized"] for op in open_pls)
        unrealized_values = [op["unrealized"] for op in open_pls if op["unrealized"] is not None]
        if not open_pls or not unrealized_values:
            open_unrealized = None
        else:
            open_unrealized = sum(unrealized_values)
        open_unrealized_partial = bool(unrealized_values) and len(unrealized_values) < len(open_pls)
        held_qty = sum(op["held_qty"] for op in open_pls)

        # 保有中も含めた通算リターン。クローズ済み (pl) と保有中 (open_pl) の損益・
        # 分母を足して1つの % にする。エピソード単位の保有中リターンと定義を揃えてあり、
        # 銘柄単位でもリターン列が「残N株」で潰れず数値で読める。
        # 保有中エピソードは全件の含みが出せるときだけ通算に混ぜる。1件でも欠けると
        # その分の取得コストだけが分母に乗って過小評価になる (9252 のように価格が
        # 取れない銘柄が該当)。保有中が無い場合は 0 == 0 で成立し確定値と一致する。
        total_amount = (pl["amount"] if pl else 0.0) + sum(
            op.get("cost_basis_total") or 0.0 for op in open_pls)
        total_profit = (pl["profit_amount"] if pl else 0.0) + open_realized + sum(
            unrealized_values)
        if total_amount > 0 and len(unrealized_values) == len(open_pls):
            total_return_pct = total_profit / total_amount * 100
        else:
            total_return_pct = None

        eps_sorted = sorted(eps, key=lambda e: e["last_trade_date"], reverse=True)
        rollups.append({
            "code_s": code_s,
            "stock_name": eps[0]["stock_name"],
            "episodes": eps_sorted,
            "episode_count": len(eps),
            "kinds": sorted({ep["kind"] for ep in eps}),
            "first_open_date": min(ep["open_date"] for ep in eps),
            "last_trade_date": max(ep["last_trade_date"] for ep in eps),
            # 銘柄の同時保有ピーク (信用買建 + 現物)。_build_code_episodes が時系列
            # 走査中に実測した値を使う。ラウンド単位の qty_peak の max では、信用と
            # 現物を同時に持つ銘柄 (6890: 現物100+信用100) でピークを取り逃す。
            "qty_peak": max([ep.get("code_qty_peak") or ep["qty_peak"] for ep in eps]),
            "has_open": any(not ep["closed"] for ep in eps),
            "has_carry_over": any(ep.get("carry_over") for ep in eps),
            "memo_count": sum(1 for ep in eps if ep.get("review_memo")),
            "pl": pl,
            # 実現損益の総額 = クローズ済み確定分 + 保有中エピソードの部分売り確定分。
            # 消費側 (銘柄単位ビュー・CLI) がこの合算を各自で書くと、片方を足し忘れて
            # 過少表示になる (4258 が確定分のみで +103,847 円、6890 が — になっていた
            # バグ)。定義をここ1箇所に置く。確定分も部分売りも無ければ None。
            "realized_total": (
                (pl["profit_amount"] if pl else 0) + open_realized
                if (pl and pl["profit_amount"] is not None) or open_realized
                else None
            ),
            "open_realized": open_realized,
            "open_unrealized": open_unrealized,
            "open_unrealized_partial": open_unrealized_partial,
            "held_qty": held_qty,
            "total_return_pct": total_return_pct,
        })

    rollups.sort(key=lambda r: r["code_s"])
    rollups.sort(key=lambda r: r["last_trade_date"], reverse=True)
    return rollups


def _finalize_round(code_s: str, kind: str, stock_name: str,
                    round_fills: List[Dict[str, Any]], qty_peak: int,
                    closed: bool = True, carry_over: bool = False,
                    open_date: Optional[str] = None,
                    is_short: bool = False) -> Dict[str, Any]:
    """建玉ラウンドの fill リストからエピソード dict を組み立てる。

    is_short=True は信用売建 (空売り) のラウンド。建玉は新規売、決済は返済買で、
    損益の符号が買建と逆になる (_episode_pl_from_round で分岐)。
    """
    dates = [f["trade_date"] for f in round_fills if f.get("trade_date")]
    # ラウンド固有のキー用に先頭 fill の seq を取る (建玉開始時に確定し不変)。
    seqs = [f.get("seq") for f in round_fills if f.get("seq") is not None]
    first_seq = min(seqs) if seqs else 0
    ep = {
        "code_s": code_s,
        "stock_name": stock_name,
        "kind": kind,
        "first_seq": first_seq,
        "open_date": open_date or (min(dates) if dates else ""),
        "close_date": max(dates) if (dates and closed) else None,
        "last_trade_date": max(dates) if dates else "",  # ラウンド内の最新約定日 (並び順の基準)
        "qty_peak": qty_peak,
        "closed": closed,
        "carry_over": carry_over,
        "is_short": is_short,
        "fills": [
            {
                "trade_date": f.get("trade_date"),
                "side": f["side"],
                "side_label": _SIDE_LABELS.get(f["side"], f["side"]),
                "qty": f["qty"],
                "price": f["price"],
                "trade_kind": f.get("trade_kind", ""),
                # 既存の楽天取込 fill は broker 追加前で未設定 (None)。未設定は「楽天」で
                # 補完する (SBI取込は必ず broker="SBI" を持つ、P2 レビュー対応)。
                "broker": f.get("broker") or "楽天",
                "tate_price": f.get("tate_price"),
                "tate_date": f.get("tate_date"),
                "settle_pl": f.get("settle_pl"),
                # 銘柄全体の保有サイクル再生 (_current_hold_cycle) の dedup キーに必要。
                "seq": f.get("seq"),
                "dedup_key": f.get("dedup_key"),
            }
            for f in round_fills
        ],
    }
    ep["pl"] = _episode_pl_from_round(ep) if closed else None
    return ep


def calc_trade_summary(episode_pls: list) -> Optional[dict]:
    """エピソード損益 dict のリストから成績サマリーを算出する。

    勝ち = return_pct > 0、負け = return_pct <= 0 (0% は負け)。
    ペイオフレシオは金額加重: 勝ち群 Σ(return_pct×amount)/Σamount ÷ |負け群同値|。
    母数0 → None (サマリー非表示)。
    """
    if not episode_pls:
        return None

    wins = [p for p in episode_pls if p["return_pct"] > 0]
    loses = [p for p in episode_pls if p["return_pct"] <= 0]
    n_total = len(episode_pls)

    def _weighted_avg_return(group):
        total_amount = sum(p["amount"] for p in group)
        if total_amount <= 0:
            return None
        return sum(p["return_pct"] * p["amount"] for p in group) / total_amount

    def _avg_hold(group):
        return sum(p["hold_days"] for p in group) / len(group) if group else None

    win_weighted = _weighted_avg_return(wins)
    lose_weighted = _weighted_avg_return(loses)
    if win_weighted is None or lose_weighted is None or lose_weighted == 0:
        payoff_ratio = None
    else:
        payoff_ratio = win_weighted / abs(lose_weighted)

    # 期待値 = 1 トレードあたりの平均リターン% (全トレードの金額加重平均)。
    # 勝率とペイオフを統合した手法の総合的な優位性。プラスならトータルで優位。
    expectancy = _weighted_avg_return(episode_pls)

    return {
        "win_rate": len(wins) / n_total * 100,
        "payoff_ratio": payoff_ratio,
        "expectancy": expectancy,
        # 勝ち/負けの金額加重平均リターン% (ペイオフレシオの分子・分母)
        "avg_return_win": win_weighted,
        "avg_return_lose": lose_weighted,
        "avg_hold_win": _avg_hold(wins),
        "avg_hold_lose": _avg_hold(loses),
        "n_total": n_total,
        "n_win": len(wins),
        "n_lose": len(loses),
    }


def build_episode_for_key(episode_key: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """episode_key (code_s|kind|first_seq) の1エピソードだけを再構成して返す。

    チャートの遅延ロード (issue #366) 用。build_fill_episodes() は全 fill 再走査で
    実測 0.5 秒/回かかり、1行展開ごとに呼ぶと展開レイテンシが悪化するため、
    銘柄を絞って _episodes_for_code() を再利用する (ロジックは二重化しない)。
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    parts = episode_key.split("|")
    if len(parts) != 3:
        return None
    code_s, kind, _ = parts
    try:
        fills = ps.list_fills(code_s, db_path=db_path)
    except ValueError:
        return None
    if not fills:
        return None
    episodes = _episodes_for_code(
        code_s,
        helpers.resolve_stock_name(code_s) or "",
        fills,
        ps.list_all_split_adjustments(db_path=db_path),
        ps.list_pending_review_events(db_path=db_path),
    )
    for ep in episodes:
        if ps.fill_episode_key(ep["code_s"], ep["kind"], ep["first_seq"]) == episode_key:
            return ep
    return None
