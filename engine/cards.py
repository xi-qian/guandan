"""掼蛋牌张模型：两副牌 108 张（2 大王 + 2 小王）。

点数自然顺序 2 < 3 < ... < K < A；大小王在自然顺序中高于 A。
级牌在单张/对子/三同张/炸弹比较中被临时抬到 A 之上、小王之下；
在顺子/连对/钢板里仍按自然点数参与。
"""
from __future__ import annotations

from dataclasses import dataclass

SUITS = ("♠", "♥", "♣", "♦")
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")
SMALL_JOKER = "SJ"
BIG_JOKER = "BJ"
JOKER_RANKS = (SMALL_JOKER, BIG_JOKER)

# 自然点数：2→2 … A→14，小王 15，大王 16
NATURAL = {r: i for i, r in enumerate(RANKS, start=2)}
NATURAL[SMALL_JOKER] = 15
NATURAL[BIG_JOKER] = 16

# 级牌抬升后位于 A(14) 与小王(15) 之间
LEVEL_VALUE = 14.5

RANK_LABEL = {
    **{r: r for r in RANKS},
    SMALL_JOKER: "小王",
    BIG_JOKER: "大王",
}

# 级数打级顺序 2 → A
LEVEL_ORDER = list(RANKS)


@dataclass(frozen=True)
class Card:
    id: int
    rank: str
    suit: str | None  # 王牌为 None

    @property
    def is_joker(self) -> bool:
        return self.suit is None

    @property
    def label(self) -> str:
        return RANK_LABEL[self.rank]

    @property
    def sort_key(self) -> tuple:
        """手牌排序：点数升序，同点数按花色。"""
        return (NATURAL[self.rank], self.suit or "")


def build_deck() -> list[Card]:
    """两副完整牌，共 108 张。id 从 0 到 107。"""
    cards: list[Card] = []
    cid = 0
    for _deck in range(2):
        for suit in SUITS:
            for rank in RANKS:
                cards.append(Card(id=cid, rank=rank, suit=suit))
                cid += 1
        cards.append(Card(id=cid, rank=SMALL_JOKER, suit=None))
        cid += 1
        cards.append(Card(id=cid, rank=BIG_JOKER, suit=None))
        cid += 1
    assert len(cards) == 108
    return cards


DECK: list[Card] = build_deck()
CARD_BY_ID: dict[int, Card] = {c.id: c for c in DECK}


def card_value(rank: str, level: str) -> float:
    """单张/对子/三同张/炸弹用的比较点数（级牌抬升）。"""
    if rank == level:
        return LEVEL_VALUE
    return NATURAL[rank]


def natural_value(rank: str) -> int:
    return NATURAL[rank]
