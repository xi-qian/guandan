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


def order_cards(card_ids: Sequence[int], kind: str) -> list[int]:
    """按牌型结构排列牌张，便于阅读。

    三带二 → 三张在前、对子在后
    三连对 / 钢板 → 各组按点数递增，组内排在一起
    顺子 / 同花顺 → 按点数递增（A 低顺时 A 排最前）
    炸弹 / 单张 / 对子 / 三同张 → 按点数
    """
    from .cards import NATURAL as _NAT

    def key(cid: int) -> tuple:
        c = CARD_BY_ID[cid]
        return (_NAT[c.rank], c.suit or "", c.id)

    ids = list(card_ids)
    if kind in (SINGLE, PAIR, TRIPLE, BOMB):
        return sorted(ids, key=key)

    counts: dict[str, list[int]] = {}
    for cid in ids:
        counts.setdefault(CARD_BY_ID[cid].rank, []).append(cid)

    if kind == TRIPLE_PAIR:
        triple = [r for r, v in counts.items() if len(v) == 3]
        pair = [r for r, v in counts.items() if len(v) == 2]
        out = []
        for r in sorted(triple, key=lambda r: _NAT[r]):
            out += sorted(counts[r], key=key)
        for r in sorted(pair, key=lambda r: _NAT[r]):
            out += sorted(counts[r], key=key)
        return out

    if kind in (PAIR_RUN, TRIPLE_RUN, STRAIGHT, STRAIGHT_FLUSH):
        # A 低顺时 A 排最前（A2345 / AA2233 / AAA222）
        a_low = "A" in counts and set(counts) == {"A"} | {
            str(i) for i in range(2, len(counts) + 1)
        }
        def seq_key(r: str) -> int:
            return 1 if (a_low and r == "A") else _NAT[r]
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


def identify(card_ids: Iterable[int], level: str) -> Combo | None:
    """识别一组牌的牌型；非法组合返回 None。"""
    cards = tuple(sorted(card_ids))
    if not cards:
        return None
    if any(c not in CARD_BY_ID for c in cards):
        return None
    n = len(cards)
    counts = _rank_counts(cards)
    ranks = list(counts)

    # 天王炸：四张王（本牌只有 2 小 2 大，故即 2+2）
    if n == 4 and all(CARD_BY_ID[c].is_joker for c in cards):
        return Combo(ROCKET, 4, 100.0, cards)

    # 同点炸弹
    if len(counts) == 1 and n >= 4 and not CARD_BY_ID[cards[0]].is_joker:
        r = ranks[0]
        return Combo(BOMB, n, card_value(r, level), cards)

    if n == 1:
        r = ranks[0]
        return Combo(SINGLE, 1, card_value(r, level), cards)

    if n == 2 and len(counts) == 1:
        return Combo(PAIR, 2, card_value(ranks[0], level), cards)

    if n == 3 and len(counts) == 1:
        return Combo(TRIPLE, 3, card_value(ranks[0], level), cards)

    if n == 5:
        # 三带二
        if sorted(len(v) for v in counts.values()) == [2, 3]:
            triple_rank = next(r for r, v in counts.items() if len(v) == 3)
            return Combo(TRIPLE_PAIR, 5, card_value(triple_rank, level), cards)
        # 顺子 / 同花顺
        top = _straight_top(ranks)
        if top is not None:
            suits = {CARD_BY_ID[c].suit for c in cards}
            kind = STRAIGHT_FLUSH if len(suits) == 1 and None not in suits else STRAIGHT
            return Combo(kind, 5, top, cards)

    if n == 6:
        # 三连对（木板）
        if all(len(v) == 2 for v in counts.values()) and len(counts) == 3:
            if _is_consecutive(ranks) or set(ranks) == {"A", "2", "3"}:
                return Combo(PAIR_RUN, 6, _window_top(ranks), cards)
        # 钢板（三同连张）
        if all(len(v) == 3 for v in counts.values()) and len(counts) == 2:
            if _is_consecutive(ranks) or set(ranks) == {"A", "2"}:
                return Combo(TRIPLE_RUN, 6, _window_top(ranks), cards)

    return None


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
    """枚举手牌中全部合法牌型（每种结构取一张代表，炸弹/同花顺全枚举）。"""
    ids = sorted(card_ids)
    by_rank = _rank_counts(ids)
    out: list[Combo] = []

    def add(c: Combo | None):
        if c is not None:
            out.append(c)

    # 单张 / 对子 / 三同张 / 同点炸弹
    for r, have in by_rank.items():
        add(identify([have[0]], level))
        if len(have) >= 2:
            add(identify(sorted(have[:2]), level))
        if len(have) >= 3:
            add(identify(sorted(have[:3]), level))
        for k in range(4, len(have) + 1):
            add(identify(sorted(have[:k]), level))

    # 三带二
    triple_ranks = [r for r, v in by_rank.items() if len(v) >= 3 and not CARD_BY_ID[v[0]].is_joker]
    pair_ranks = [r for r, v in by_rank.items() if len(v) >= 2]
    for tr in triple_ranks:
        for pr in pair_ranks:
            if pr == tr and len(by_rank[tr]) < 5:
                continue
            cards = sorted(by_rank[tr][:3])
            if pr == tr:
                cards += sorted(by_rank[tr][3:5])
            else:
                cards += sorted(by_rank[pr][:2])
            add(identify(cards, level))

    # 顺子
    for window in _consecutive_windows(5):
        picked = [_pick(by_rank, r, 1) for r in window]
        if all(p is not None for p in picked):
            add(identify(sorted(x[0] for x in picked if x), level))

    # 三连对
    for window in _consecutive_windows(3):
        picked = [_pick(by_rank, r, 2) for r in window]
        if all(p is not None for p in picked):
            add(identify(sorted(x for p in picked if p for x in p), level))

    # 钢板
    for window in _consecutive_windows(2):
        picked = [_pick(by_rank, r, 3) for r in window]
        if all(p is not None for p in picked):
            add(identify(sorted(x for p in picked if p for x in p), level))

    # 同花顺
    by_suit: dict[str, dict[str, list[int]]] = {}
    for cid in ids:
        c = CARD_BY_ID[cid]
        if c.suit is None:
            continue
        by_suit.setdefault(c.suit, {}).setdefault(c.rank, []).append(cid)
    for _suit, ranks in by_suit.items():
        for window in _consecutive_windows(5):
            if all(r in ranks for r in window):
                cards = sorted(ranks[r][0] for r in window)
                add(identify(cards, level))

    # 天王炸
    jokers = [c for c in ids if CARD_BY_ID[c].is_joker]
    if len(jokers) == 4:
        add(identify(sorted(jokers), level))

    # 去重
    seen: set[tuple] = set()
    uniq: list[Combo] = []
    for c in out:
        if c.cards not in seen:
            seen.add(c.cards)
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
