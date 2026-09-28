"""牌型识别与比较的单元测试。"""
from __future__ import annotations

import pytest

from engine.cards import CARD_BY_ID, DECK
from engine.combos import (
    order_cards,
    BOMB,
    PAIR,
    PAIR_RUN,
    ROCKET,
    SINGLE,
    STRAIGHT,
    STRAIGHT_FLUSH,
    TRIPLE,
    TRIPLE_PAIR,
    TRIPLE_RUN,
    beats,
    enumerate_combos,
    identify,
    legal_moves,
)


def cards(*specs) -> list[int]:
    """按 (rank, suit) 取牌 id；suit=None 取王牌。同名取未用过的下一张。"""
    used: set[int] = set()
    out: list[int] = []
    for spec in specs:
        rank, suit = spec
        for c in DECK:
            if c.id in used:
                continue
            if c.rank != rank:
                continue
            if suit is None and c.suit is not None:
                continue
            if suit is not None and c.suit != suit:
                continue
            used.add(c.id)
            out.append(c.id)
            break
        else:
            raise AssertionError(f"找不到牌 {spec}")
    return out


LV2 = "2"  # 级牌是 2


class TestIdentify:
    def test_single(self):
        c = identify(cards(("9", "♠")), LV2)
        assert c.kind == SINGLE and c.size == 1

    def test_pair(self):
        c = identify(cards(("7", "♠"), ("7", "♥")), LV2)
        assert c.kind == PAIR

    def test_joker_pair(self):
        c = identify(cards(("SJ", None), ("SJ", None)), LV2)
        assert c.kind == PAIR

    def test_triple(self):
        c = identify(cards(("5", "♠"), ("5", "♥"), ("5", "♣")), LV2)
        assert c.kind == TRIPLE

    def test_triple_pair(self):
        c = identify(
            cards(("8", "♠"), ("8", "♥"), ("8", "♣"), ("3", "♠"), ("3", "♥")), LV2
        )
        assert c.kind == TRIPLE_PAIR and c.main == 8

    def test_straight_normal(self):
        c = identify(cards(("3", "♠"), ("4", "♥"), ("5", "♣"), ("6", "♦"), ("7", "♠")), LV2)
        assert c.kind == STRAIGHT and c.main == 7

    def test_straight_ace_high(self):
        c = identify(cards(("10", "♠"), ("J", "♥"), ("Q", "♣"), ("K", "♦"), ("A", "♠")), LV2)
        assert c.kind == STRAIGHT and c.main == 14

    def test_straight_ace_low(self):
        c = identify(cards(("A", "♠"), ("2", "♥"), ("3", "♣"), ("4", "♦"), ("5", "♠")), LV2)
        assert c.kind == STRAIGHT and c.main == 5

    def test_straight_no_wraparound(self):
        assert identify(cards(("J", "♠"), ("Q", "♥"), ("K", "♣"), ("A", "♦"), ("2", "♠")), LV2) is None

    def test_straight_not_six(self):
        assert (
            identify(
                cards(("3", "♠"), ("4", "♥"), ("5", "♣"), ("6", "♦"), ("7", "♠"), ("8", "♥")),
                LV2,
            )
            is None
        )

    def test_pair_run(self):
        c = identify(
            cards(("5", "♠"), ("5", "♥"), ("6", "♣"), ("6", "♦"), ("7", "♠"), ("7", "♥")), LV2
        )
        assert c.kind == PAIR_RUN and c.main == 7

    def test_pair_run_ace_low(self):
        c = identify(
            cards(("A", "♠"), ("A", "♥"), ("2", "♣"), ("2", "♦"), ("3", "♠"), ("3", "♥")), LV2
        )
        assert c.kind == PAIR_RUN and c.main == 3

    def test_triple_run(self):
        c = identify(
            cards(("4", "♠"), ("4", "♥"), ("4", "♣"), ("5", "♦"), ("5", "♠"), ("5", "♥")), LV2
        )
        assert c.kind == TRIPLE_RUN and c.main == 5

    def test_bomb_four(self):
        c = identify(cards(("6", "♠"), ("6", "♥"), ("6", "♣"), ("6", "♦")), LV2)
        assert c.kind == BOMB and c.size == 4

    def test_bomb_eight(self):
        cs = cards(*[("9", s) for s in ("♠", "♥", "♣", "♦")] + [("9", "♠"), ("9", "♥"), ("9", "♣"), ("9", "♦")])
        c = identify(cs, LV2)
        assert c.kind == BOMB and c.size == 8

    def test_straight_flush(self):
        c = identify(cards(("3", "♥"), ("4", "♥"), ("5", "♥"), ("6", "♥"), ("7", "♥")), LV2)
        assert c.kind == STRAIGHT_FLUSH

    def test_rocket(self):
        c = identify(cards(("SJ", None), ("SJ", None), ("BJ", None), ("BJ", None)), LV2)
        assert c.kind == ROCKET

    def test_junk_rejected(self):
        assert identify(cards(("3", "♠"), ("4", "♥")), LV2) is None
        assert identify(cards(("3", "♠"), ("3", "♥"), ("4", "♥")), LV2) is None
        assert identify(cards(("3", "♠"), ("4", "♥"), ("5", "♣"), ("6", "♦"), ("8", "♠")), LV2) is None


class TestBeats:
    def test_same_type_higher_rank(self):
        a = identify(cards(("K", "♠")), LV2)
        b = identify(cards(("Q", "♠")), LV2)
        assert beats(a, b) and not beats(b, a)

    def test_level_card_beats_ace(self):
        a = identify(cards(("2", "♠")), LV2)  # 级牌
        b = identify(cards(("A", "♠")), LV2)
        assert beats(a, b)

    def test_jokers_high(self):
        bj = identify(cards(("BJ", None)), LV2)
        sj = identify(cards(("SJ", None)), LV2)
        two = identify(cards(("2", "♠")), LV2)
        assert beats(bj, sj) and beats(sj, two)

    def test_kind_must_match(self):
        pair = identify(cards(("3", "♠"), ("3", "♥")), LV2)
        single = identify(cards(("A", "♠")), LV2)
        assert not beats(pair, single)
        assert not beats(single, pair)

    def test_size_must_match_for_straight(self):
        # 顺子固定 5 张，6 张不成顺子，已由 identify 拦截
        s1 = identify(cards(("3", "♠"), ("4", "♥"), ("5", "♣"), ("6", "♦"), ("7", "♠")), LV2)
        s2 = identify(cards(("8", "♠"), ("9", "♥"), ("10", "♣"), ("J", "♦"), ("Q", "♠")), LV2)
        assert beats(s2, s1)

    def test_bomb_beats_normal(self):
        bomb = identify(cards(("4", "♠"), ("4", "♥"), ("4", "♣"), ("4", "♦")), LV2)
        straight = identify(
            cards(("A", "♠"), ("K", "♥"), ("Q", "♣"), ("J", "♦"), ("10", "♠")), LV2
        )
        assert beats(bomb, straight)

    def test_bigger_bomb_beats_smaller(self):
        b4 = identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣"), ("A", "♦")), LV2)
        b5 = identify(cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦"), ("3", "♠")), LV2)
        assert beats(b5, b4)

    def test_bomb_rank_breaks_tie(self):
        low = identify(cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦")), LV2)
        high = identify(cards(("K", "♠"), ("K", "♥"), ("K", "♣"), ("K", "♦")), LV2)
        assert beats(high, low)

    def test_level_bomb_beats_ace_bomb(self):
        lv = identify(cards(("2", "♠"), ("2", "♥"), ("2", "♣"), ("2", "♦")), LV2)
        ac = identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣"), ("A", "♦")), LV2)
        assert beats(lv, ac)

    def test_straight_flush_between_5_and_6_bomb(self):
        sf = identify(cards(("3", "♥"), ("4", "♥"), ("5", "♥"), ("6", "♥"), ("7", "♥")), LV2)
        b5 = identify(cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦"), ("3", "♠")), LV2)
        b6 = identify(
            cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦"), ("3", "♠"), ("3", "♥")), LV2
        )
        assert beats(sf, b5)
        assert beats(b6, sf)

    def test_rocket_beats_all(self):
        rocket = identify(cards(("SJ", None), ("SJ", None), ("BJ", None), ("BJ", None)), LV2)
        b8 = identify(
            cards(*[("9", s) for s in ("♠", "♥", "♣", "♦")] + [("9", "♠"), ("9", "♥"), ("9", "♣"), ("9", "♦")]),
            LV2,
        )
        assert beats(rocket, b8)
        assert not beats(b8, rocket)

    def test_triple_pair_compares_by_triple(self):
        a = identify(cards(("9", "♠"), ("9", "♥"), ("9", "♣"), ("A", "♠"), ("A", "♥")), LV2)
        b = identify(cards(("10", "♠"), ("10", "♥"), ("10", "♣"), ("3", "♠"), ("3", "♥")), LV2)
        assert beats(b, a)


class TestEnumeration:
    def test_level_2_in_straight_uses_natural_rank(self):
        # 打 2 时 2 在顺子里仍是 2：A2345 合法，且主点数为 5
        hand = cards(("A", "♠"), ("2", "♥"), ("3", "♣"), ("4", "♦"), ("5", "♠"))
        c = identify(hand, "2")
        assert c is not None and c.kind == STRAIGHT and c.main == 5

    def test_leading_has_many_options(self):
        hand = cards(
            ("3", "♠"), ("3", "♥"), ("4", "♣"), ("4", "♦"), ("5", "♠"), ("5", "♥"),
            ("6", "♣"), ("7", "♦"), ("8", "♠"), ("J", "♥"), ("Q", "♣"), ("K", "♦"),
            ("A", "♠"), ("A", "♥"), ("SJ", None), ("BJ", None),
        )
        moves = enumerate_combos(hand, "2")
        kinds = {m.kind for m in moves}
        assert SINGLE in kinds and PAIR in kinds and STRAIGHT in kinds
        assert PAIR_RUN in kinds  # 334455
        assert TRIPLE not in kinds

    def test_legal_moves_following_must_beat(self):
        hand = cards(
            ("3", "♠"), ("3", "♥"), ("3", "♣"), ("8", "♦"), ("9", "♠"),
            ("K", "♥"), ("A", "♣"), ("A", "♦"), ("A", "♠"), ("SJ", None),
        )
        table = identify(cards(("10", "♠"), ("10", "♥")), "2")
        moves = legal_moves(hand, "2", table)
        assert moves
        for m in moves:
            assert m.kind == PAIR and m.main > 10 or m.is_bomb

    def test_no_legal_moves_returns_empty(self):
        hand = cards(("3", "♠"), ("4", "♥"), ("5", "♣"))
        table = identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣"), ("A", "♦")), "2")
        assert legal_moves(hand, "2", table) == []

    def test_all_enumerated_moves_are_valid(self):
        hand = cards(
            ("2", "♠"), ("2", "♥"), ("3", "♣"), ("3", "♦"), ("4", "♠"), ("4", "♥"),
            ("4", "♣"), ("5", "♦"), ("6", "♠"), ("6", "♥"), ("6", "♣"), ("6", "♦"),
            ("7", "♠"), ("8", "♥"), ("9", "♣"), ("10", "♦"), ("J", "♠"), ("Q", "♥"),
            ("K", "♣"), ("A", "♦"), ("A", "♠"), ("SJ", None), ("BJ", None), ("3", "♠"),
        )
        for m in enumerate_combos(hand, "4"):
            assert identify(m.cards, "4") is not None
            assert all(c in hand for c in m.cards)



class TestOrderCards:
    """出牌要按牌型结构排列，便于阅读。用户点名：三带二应 3 在前 2 在后。"""

    def test_triple_pair_triple_first(self):
        ids = cards(("8", "♠"), ("3", "♥"), ("8", "♥"), ("3", "♠"), ("8", "♣"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        ranks = [DECK[i].rank for i in o]
        assert ranks == ["8", "8", "8", "3", "3"], ranks

    def test_triple_pair_lows_last(self):
        ids = cards(("3", "♠"), ("K", "♥"), ("3", "♥"), ("K", "♠"), ("3", "♣"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert [DECK[i].rank for i in o] == ["3", "3", "3", "K", "K"]

    def test_straight_ascending(self):
        ids = cards(("7", "♠"), ("3", "♥"), ("5", "♣"), ("4", "♦"), ("6", "♠"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert [DECK[i].rank for i in o] == ["3", "4", "5", "6", "7"]

    def test_ace_low_straight_puts_ace_first(self):
        ids = cards(("5", "♠"), ("A", "♥"), ("3", "♣"), ("4", "♦"), ("2", "♠"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert [DECK[i].rank for i in o] == ["A", "2", "3", "4", "5"]

    def test_ace_high_straight_keeps_ace_last(self):
        ids = cards(("10", "♠"), ("A", "♥"), ("Q", "♣"), ("J", "♦"), ("K", "♠"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert [DECK[i].rank for i in o] == ["10", "J", "Q", "K", "A"]

    def test_pair_run_groups_pairs(self):
        ids = cards(("7", "♠"), ("5", "♥"), ("6", "♣"), ("5", "♦"), ("7", "♥"), ("6", "♠"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert [DECK[i].rank for i in o] == ["5", "5", "6", "6", "7", "7"]

    def test_triple_run_groups_triples(self):
        ids = cards(("5", "♠"), ("4", "♥"), ("5", "♣"), ("4", "♦"), ("5", "♥"), ("4", "♠"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert [DECK[i].rank for i in o] == ["4", "4", "4", "5", "5", "5"]

    def test_bomb_by_rank(self):
        ids = cards(("9", "♠"), ("9", "♥"), ("9", "♣"), ("9", "♦"))
        c = identify(ids, "2")
        assert len(order_cards(c.cards, c.kind)) == 4

    def test_straight_flush_ascending(self):
        ids = cards(("7", "♥"), ("3", "♥"), ("5", "♥"), ("4", "♥"), ("6", "♥"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert [DECK[i].rank for i in o] == ["3", "4", "5", "6", "7"]

    def test_preserves_card_identity(self):
        """排列只改顺序，不改牌。"""
        ids = cards(("8", "♠"), ("3", "♥"), ("8", "♥"), ("3", "♠"), ("8", "♣"))
        c = identify(ids, "2")
        o = order_cards(c.cards, c.kind)
        assert sorted(o) == sorted(c.cards)
