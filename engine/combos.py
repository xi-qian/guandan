"""掼蛋牌型识别、大小比较与合法出牌枚举。

牌型：
  普通 —— 单张 / 对子 / 三同张 / 三带二 / 顺子(5) / 三连对(6) / 钢板(6)
  炸弹 —— 四张及以上同点；同花顺(5 同花连)；天王炸(2 大王 + 2 小王)

比较规则（严格按 guandanguize.com）：
  普通牌型之间必须同类型同张数，比主点数；
  炸弹压任何普通牌型；炸弹之间先比张数再比点数；
  同花顺大于五炸、小于六炸；天王炸最大。
  顺子/连对/钢板里级牌按自然点数参与；其余比较级牌抬升到 A 之上。
  A 可作最小（A2345 / AA2233 / AAA222）或最大（10JQKA），不可跨头（JQKA2）。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence

from .cards import (
    BIG_JOKER,
    CARD_BY_ID,
    NATURAL,
    SMALL_JOKER,
    card_value,
)

# 牌型种类
SINGLE = "single"
PAIR = "pair"
TRIPLE = "triple"
TRIPLE_PAIR = "triple_pair"
STRAIGHT = "straight"
PAIR_RUN = "pair_run"
TRIPLE_RUN = "triple_run"
BOMB = "bomb"
STRAIGHT_FLUSH = "straight_flush"
ROCKET = "rocket"

NORMAL_KINDS = {SINGLE, PAIR, TRIPLE, TRIPLE_PAIR, STRAIGHT, PAIR_RUN, TRIPLE_RUN}
BOMB_KINDS = {BOMB, STRAIGHT_FLUSH, ROCKET}

# 炸弹层级：张数优先，同花顺介于五炸与六炸之间
_TIER_STRAIGHT_FLUSH = 55
_TIER_ROCKET = 1000



# ---------------------------------------------------------------- 逢人配（百搭）
# 当前级牌的红桃花色那张可当任意牌（不能当王）。两副牌共 2 张。

def is_wild(card_id: int, level: str) -> bool:
    """红桃级牌 = 百搭。"""
    c = CARD_BY_ID[card_id]
    return c.suit == "♥" and c.rank == level


def _split_wilds(card_ids: Sequence[int], level: str) -> tuple[list[int], list[int]]:
    """返回 (普通牌, 百搭牌)。"""
    normal, wilds = [], []
    for cid in card_ids:
        (wilds if is_wild(cid, level) else normal).append(cid)
    return normal, wilds


def _fits(counts: dict[str, list[int]], wilds: list[int], need: dict[str, int]) -> bool:
    """need 是目标 {点数: 张数}；普通牌必须全部用上，缺口恰好由百搭补齐。

    注意：百搭不能当王，所以 need 里不得出现 SJ/BJ 而普通牌里没有。
    """
    total_need = sum(need.values())
    total_have = sum(len(v) for v in counts.values()) + len(wilds)
    if total_need != total_have:
        return False
    gap = 0
    for r, k in need.items():
        have = len(counts.get(r, []))
        if have > k:
            return False          # 有的牌用不掉
        gap += k - have
    # 百搭不能凭空造王
    if any(r in (SMALL_JOKER, BIG_JOKER) and len(counts.get(r, [])) < k
           for r, k in need.items()):
        return False
    return gap == len(wilds)


def _take_subset(counts: dict[str, list[int]], wilds: list[int],
                 need: dict[str, int]) -> list[int] | None:
    """枚举用：按 need 取**子集**，缺口用百搭补（不必用完全部百搭）。"""
    out: list[int] = []
    pool = sorted(wilds)
    keys = sorted(need, key=lambda r: NATURAL[r])
    for r in keys:
        k = need[r]
        have = sorted(counts.get(r, []))
        out += have[:k]
        gap = k - min(len(have), k)
        if gap > 0:
            if len(pool) < gap:
                return None
            out += pool[:gap]
            pool = pool[gap:]
    return out


def _take(
    counts: dict[str, list[int]],
    wilds: list[int],
    need: dict[str, int],
    rank_order: Sequence[str] | None = None,
) -> list[int] | None:
    """按 need 取牌；不足的用百搭补。

    rank_order 决定输出的结构顺序（如三带二 = 三张在前、对子在后），
    不给则按点数升序。关键：**必须把所有牌都返回**，不能因为分组不整齐就丢牌。
    """
    out: list[int] = []
    pool = sorted(wilds)
    keys = list(rank_order) if rank_order else sorted(need, key=lambda r: NATURAL[r])
    keys += [r for r in need if r not in keys]

    for r in keys:
        k = need.get(r)
        if not k:
            continue
        have = sorted(counts.get(r, []))
        if len(have) > k:
            return None
        out += have
        gap = k - len(have)
        # 百搭放进它所填补的那一组，保持结构顺序
        if gap > 0:
            if len(pool) < gap:
                return None
            out += pool[:gap]
            pool = pool[gap:]
    if pool:
        return None          # 百搭没用完
    if len(out) != sum(need.values()):
        return None
    return out


def order_cards(card_ids: Sequence[int], kind: str) -> list[int]:
    """按牌型结构排列牌张，便于阅读。

    Combo.cards 在 identify 时已排好序，这里主要给历史/提示词兜底。
    **绝不丢牌**：分组不整齐（例如含百搭）时按点数升序补全。
    """
    ids = list(card_ids)
    if not ids:
        return []

    def key(cid: int) -> tuple:
        c = CARD_BY_ID[cid]
        return (NATURAL[c.rank], c.suit or "", c.id)

    if kind in (SINGLE, PAIR, TRIPLE, BOMB):
        return sorted(ids, key=key)

    counts: dict[str, list[int]] = {}
    for cid in ids:
        counts.setdefault(CARD_BY_ID[cid].rank, []).append(cid)

    if kind == TRIPLE_PAIR:
        out: list[int] = []
        used: set[int] = set()
        # 三张在前
        for r in sorted(counts, key=lambda r: NATURAL[r]):
            if len(counts[r]) == 3:
                out += sorted(counts[r], key=key)
                used.update(counts[r])
        # 对子在后
        for r in sorted(counts, key=lambda r: NATURAL[r]):
            if len(counts[r]) == 2:
                out += sorted(counts[r], key=key)
                used.update(counts[r])
        # 剩下的（含百搭导致的不整齐分组）补在最后
        out += sorted((c for c in ids if c not in used), key=key)
        return out

    if kind in (PAIR_RUN, TRIPLE_RUN, STRAIGHT, STRAIGHT_FLUSH):
        a_low = "A" in counts and set(counts) == {"A"} | {
            str(i) for i in range(2, len(counts) + 1)
        }

        def seq_key(r: str) -> int:
            return 1 if (a_low and r == "A") else NATURAL[r]

        out = []
        for r in sorted(counts, key=seq_key):
            out += sorted(counts[r], key=key)
        return out

    return sorted(ids, key=key)


@dataclass(frozen=True)
class Combo:
    kind: str
    size: int
    main: float  # 比较用主点数（普通牌型已按级牌抬升；顺子类为顶张自然点数）
    cards: tuple[int, ...]  # 牌 id，升序

    @property
    def is_bomb(self) -> bool:
        return self.kind in BOMB_KINDS

    def bomb_key(self) -> tuple[float, float]:
        if self.kind == ROCKET:
            return (_TIER_ROCKET, 0.0)
        if self.kind == STRAIGHT_FLUSH:
            return (_TIER_STRAIGHT_FLUSH, self.main)
        return (float(self.size) * 10, self.main)


def beats(a: Combo, b: Combo | None) -> bool:
    """a 能否压过桌面上的 b（b 为 None 表示自由出牌）。"""
    if b is None:
        return True
    if a.kind == ROCKET:
        return b.kind != ROCKET
    if b.kind == ROCKET:
        return False
    if a.is_bomb:
        if not b.is_bomb:
            return True
        return a.bomb_key() > b.bomb_key()
    if b.is_bomb:
        return False
    return a.kind == b.kind and a.size == b.size and a.main > b.main


# ---------------------------------------------------------------- 识别

def _rank_counts(card_ids: Sequence[int]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for cid in card_ids:
        r = CARD_BY_ID[cid].rank
        out.setdefault(r, []).append(cid)
    return out


def _is_consecutive(ranks: Sequence[str]) -> bool:
    """自然点数连续（不含绕回），A 只按 14。"""
    vals = sorted(NATURAL[r] for r in ranks)
    return all(vals[i + 1] - vals[i] == 1 for i in range(len(vals) - 1))


def _window_top(ranks: Sequence[str]) -> float:
    """连续窗口的顶张点数。A 作最小时顶为窗口内最大非 A 点（A2345 顶 5）。"""
    if "A" in ranks and set(ranks) == {"A"} | {str(i) for i in range(2, len(ranks) + 1)}:
        return float(max(NATURAL[r] for r in ranks if r != "A"))
    return float(max(NATURAL[r] for r in ranks))


def _straight_top(ranks: Sequence[str]) -> float | None:
    """5 张单牌是否成顺，返回顶张自然点数（A2345 顶为 5）。"""
    if len(ranks) != 5:
        return None
    if len(set(ranks)) != 5:
        return None
    if _is_consecutive(ranks):
        return _window_top(ranks)
    # A 作最小：A2345
    if set(ranks) == {"A", "2", "3", "4", "5"}:
        return 5.0
    return None


def _all_identify(cards: tuple[int, ...], level: str) -> list[Combo]:
    """列出这组牌能构成的全部牌型解释（百搭可有多种用法）。"""
    n = len(cards)
    if any(c not in CARD_BY_ID for c in cards):
        return []

    # 天王炸：四张王（百搭不能当王，故不能参与）
    if n == 4 and all(CARD_BY_ID[c].is_joker for c in cards):
        return [Combo(ROCKET, 4, 100.0, cards)]

    normal, wilds = _split_wilds(cards, level)
    w = len(wilds)
    counts = _rank_counts(normal)
    out: list[Combo] = []

    def try_build(kind: str, size: int, main: float, need: dict[str, int],
                  rank_order: Sequence[str] | None = None) -> None:
        got = _take(counts, wilds, need, rank_order=rank_order)
        if got:
            out.append(Combo(kind, size, main, tuple(got)))

    def fits(need: dict[str, int]) -> bool:
        return _fits(counts, wilds, need)

    ranks = list(counts) + ([level] if w else [])

    # 同点炸弹
    if n >= 4:
        for r in ranks:
            if r in (SMALL_JOKER, BIG_JOKER):
                continue
            need = {r: n}
            if fits(need):
                try_build(BOMB, n, card_value(r, level), need)

    # 单张 / 对子 / 三同张
    if n == 1:
        for r in (list(counts) or [level]):
            try_build(SINGLE, 1, card_value(r, level), {r: 1})
    elif n == 2:
        for r in ranks:
            if fits({r: 2}):
                try_build(PAIR, 2, card_value(r, level), {r: 2})
    elif n == 3:
        for r in ranks:
            if fits({r: 3}):
                try_build(TRIPLE, 3, card_value(r, level), {r: 3})

    # 三带二
    if n == 5:
        for tr in ranks:
            if tr in (SMALL_JOKER, BIG_JOKER):
                continue
            for pr in ranks:
                if tr == pr:
                    need, order = {tr: 5}, [tr]
                else:
                    need, order = {tr: 3, pr: 2}, [tr, pr]   # 三张在前
                if fits(need):
                    try_build(TRIPLE_PAIR, 5, card_value(tr, level), need, order)

    # 顺子 / 同花顺
    if n == 5:
        for window in _consecutive_windows(5):
            need = {r: 1 for r in window}
            if not fits(need):
                continue
            got = _take(counts, wilds, need, rank_order=window)
            if not got:
                continue
            real_suits = {CARD_BY_ID[c].suit for c in got if not is_wild(c, level)}
            same_suit = len(real_suits) == 1 and None not in real_suits
            if same_suit:
                # 百搭可以补成同花色（同花顺）；若用了百搭，也可当作补成异花色（顺子）
                out.append(Combo(STRAIGHT_FLUSH, 5, _window_top(window), tuple(got)))
                if w:
                    out.append(Combo(STRAIGHT, 5, _window_top(window), tuple(got)))
            else:
                out.append(Combo(STRAIGHT, 5, _window_top(window), tuple(got)))

    # 三连对 / 钢板
    if n == 6:
        for window in _consecutive_windows(3):
            need = {r: 2 for r in window}
            if fits(need):
                try_build(PAIR_RUN, 6, _window_top(window), need, window)
        for window in _consecutive_windows(2):
            need = {r: 3 for r in window}
            if fits(need):
                try_build(TRIPLE_RUN, 6, _window_top(window), need, window)

    return out


def identify(
    card_ids: Iterable[int],
    level: str,
    prefer: dict | None = None,
) -> Combo | None:
    """识别一组牌的牌型；非法组合返回 None。

    支持逢人配（红桃级牌当百搭）：可补任意点数/花色，但不能当王。
    同一组牌若有多种解释：
      - 给了 prefer（{"kind": ..., "main": ...}）就取匹配的那一种
      - 否则取最强的（炸弹优先、main 最大）
    prefer 存在但匹配不到 → 返回 None（说明玩家指定的打法不成立）。
    """
    cards = tuple(sorted(card_ids))
    if not cards:
        return None
    cands = _all_identify(cards, level)
    if not cands:
        return None
    if prefer:
        want_kind = prefer.get("kind")
        want_main = prefer.get("main")
        matched = [c for c in cands if c.kind == want_kind
                   and (want_main is None or abs(c.main - float(want_main)) < 1e-6)]
        if matched:
            return max(matched, key=lambda c: c.main)
        return None

    def rank_of_combo(c: Combo) -> tuple:
        return (1 if c.is_bomb else 0, c.bomb_key() if c.is_bomb else (0.0, c.main), c.main)

    return max(cands, key=rank_of_combo)


def interpretations(card_ids: Iterable[int], level: str) -> list[dict]:
    """这组牌的全部可用解释，供 UI/提示词让玩家挑选。

    每项: {"kind", "kind_label"由上层补, "main", "cards"(牌 id 升序), "size"}
    """
    cards = tuple(sorted(card_ids))
    seen: set[tuple] = set()
    out: list[dict] = []
    for c in _all_identify(cards, level):
        key = (c.kind, round(c.main, 3), c.cards)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": c.kind, "main": c.main,
                    "cards": list(c.cards), "size": c.size})
    out.sort(key=lambda d: (0 if d["kind"] in BOMB_KINDS else 1, -d["main"]))
    return out


# ---------------------------------------------------------------- 枚举

def _consecutive_windows(unit: int) -> list[list[str]]:
    """所有长度为 unit 的连续点数窗口（含 A 作最小的窗口）。

    unit=5 → 顺子：A2345 与 23456…10JQKA
    unit=3 → 三连对：AA2233 与 223344…QQKKAA
    unit=2 → 钢板：AAA222 与 222333…KKKAAA
    """
    rank_of = {v: r for r, v in NATURAL.items() if r not in (SMALL_JOKER, BIG_JOKER)}
    windows: list[list[str]] = []
    # A 作最小：A2…（不绕到 JQKA2）
    windows.append(["A"] + [rank_of[i] for i in range(2, unit + 1)])
    # 常规窗口：最低点数从 2 起，顶张直到 A
    for low in range(2, 15 - unit + 1):
        windows.append([rank_of[low + i] for i in range(unit)])
    return windows


def _pick(by_rank: dict[str, list[int]], rank: str, k: int) -> list[int] | None:
    have = by_rank.get(rank, [])
    return sorted(have)[:k] if len(have) >= k else None


def enumerate_combos(card_ids: Sequence[int], level: str) -> list[Combo]:
    """枚举手牌中全部合法牌型（含百搭的每种用法，每种结构取代表牌）。"""
    ids = tuple(sorted(card_ids))
    if not ids:
        return []
    normal, wilds = _split_wilds(ids, level)
    w = len(wilds)
    counts = _rank_counts(normal)
    all_ranks = [r for r in NATURAL if r not in (SMALL_JOKER, BIG_JOKER)]
    cand_ranks = sorted(set(counts) | (set(all_ranks) if w else set()),
                        key=lambda r: NATURAL[r])
    out: list[Combo] = []

    def emit(need: dict[str, int]) -> None:
        got = _take_subset(counts, wilds, need)
        if got:
            c = identify(got, level)
            if c is not None:
                out.append(c)

    # 单张 / 对子 / 三同张 / 同点炸弹
    for r in cand_ranks:
        avail = len(counts.get(r, [])) + w
        for k in (1, 2, 3):
            if k <= avail:
                emit({r: k})
        for k in range(4, avail + 1):
            emit({r: k})

    # 三带二
    triples = [r for r in cand_ranks if len(counts.get(r, [])) + w >= 3]
    pairs = [r for r in cand_ranks if len(counts.get(r, [])) + w >= 2]
    for tr in triples:
        for pr in pairs:
            emit({tr: 5} if tr == pr else {tr: 3, pr: 2})

    # 顺子（含同花顺）
    for window in _consecutive_windows(5):
        emit({r: 1 for r in window})

    # 三连对 / 钢板
    for window in _consecutive_windows(3):
        emit({r: 2 for r in window})
    for window in _consecutive_windows(2):
        emit({r: 3 for r in window})

    # 同花顺（按花色；百搭可当任意花色）
    by_suit: dict[str, dict[str, list[int]]] = {}
    for cid in normal:
        c = CARD_BY_ID[cid]
        if c.suit is None:
            continue
        by_suit.setdefault(c.suit, {}).setdefault(c.rank, []).append(cid)
    for _suit, rk in by_suit.items():
        for window in _consecutive_windows(5):
            got: list[int] = []
            gap = 0
            for r in window:
                have = sorted(rk.get(r, []))
                got += have[:1]
                gap += 1 - min(len(have), 1)
            if gap > w:
                continue
            got = sorted(got + sorted(wilds)[:gap])
            if len(got) == 5:
                c = identify(got, level)
                if c is not None and c.kind == STRAIGHT_FLUSH:
                    out.append(c)

    # 天王炸
    jokers = [c for c in ids if CARD_BY_ID[c].is_joker]
    if len(jokers) == 4:
        out.append(identify(sorted(jokers), level))

    seen: set[tuple] = set()
    uniq: list[Combo] = []
    for c in out:
        if c is None:
            continue
        key = (c.kind, round(c.main, 3), c.cards)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def legal_moves(
    card_ids: Sequence[int], level: str, table: Combo | None
) -> list[Combo]:
    """能压过桌面上 table 的全部出牌；table 为 None 时返回全部牌型。"""
    if table is None:
        return enumerate_combos(card_ids, level)
    moves = []
    for c in enumerate_combos(card_ids, level):
        if beats(c, table):
            moves.append(c)
    return moves
