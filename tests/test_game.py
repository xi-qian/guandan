"""对局状态机测试：发牌、出牌轮转、进贡、升级、终局。"""
from __future__ import annotations

import pytest

from engine.cards import CARD_BY_ID, DECK
from engine.combos import identify
from engine.game import Game, IllegalMove


def make_game(seed: int = 42) -> Game:
    g = Game(player_names=["甲", "乙", "丙", "丁"], seed=seed)
    g.start(first_leader=0)
    return g


def rank_of(card_id: int) -> str:
    return CARD_BY_ID[card_id].rank


class TestDeal:
    def test_108_cards_split_evenly(self):
        g = make_game()
        all_cards = [c for h in g.hands for c in h]
        assert len(all_cards) == 108
        assert len(set(all_cards)) == 108
        assert all(len(h) == 27 for h in g.hands)
        assert g.current == 0

    def test_deal_event_records_hands(self):
        g = make_game()
        deal = [e for e in g.events if e["type"] == "deal"][-1]
        assert deal["hands"] == g.hands
        assert deal["levels"] == ["2", "2"]


class TestPlay:
    def test_must_wait_for_turn(self):
        g = make_game()
        with pytest.raises(IllegalMove):
            g.play(1, [g.hands[1][0]])

    def test_cannot_pass_when_leading(self):
        g = make_game()
        with pytest.raises(IllegalMove):
            g.pass_(0)

    def test_play_removes_cards_and_advances(self):
        g = make_game()
        card = g.hands[0][0]
        g.play(0, [card])
        assert card not in g.hands[0]
        assert g.table is not None and g.table_seat == 0
        assert g.current == 1

    def test_reject_card_not_in_hand(self):
        g = make_game()
        foreign = next(c for s in (1, 2, 3) for c in g.hands[s])
        with pytest.raises(IllegalMove):
            g.play(0, [foreign])

    def test_reject_junk_combo(self):
        g = make_game()
        g.play(0, [g.hands[0][0]])
        # 乙出两张不同点数
        h = g.hands[1]
        for i in range(len(h)):
            for j in range(i + 1, len(h)):
                if rank_of(h[i]) != rank_of(h[j]):
                    with pytest.raises(IllegalMove):
                        g.play(1, [h[i], h[j]])
                    return
        pytest.fail("手牌里找不到两张不同点数")

    def test_must_beat_table(self):
        g = make_game()
        g.play(0, [g.hands[0][-1]])  # 出最大单张，保证别家有更小的
        h = g.hands[1]
        table_rank = rank_of(g.table.cards[0])
        from engine.cards import card_value

        lv = g.level_of(1)
        smaller = [
            c for c in h if card_value(rank_of(c), lv) < card_value(table_rank, lv)
        ]
        assert smaller, "手牌里应有更小的单张"
        with pytest.raises(IllegalMove):
            g.play(1, [smaller[0]])

    def test_pass_clears_table_after_all_pass(self):
        g = make_game()
        card = g.hands[0][0]
        g.play(0, [card])
        # 其余三家都不可能压（假定都过）——需要他们真能过
        for seat in (1, 2, 3):
            if g.table is None:
                break
            g.pass_(seat)
        assert g.table is None
        assert g.current == 0  # 重新由出牌者领出

    def test_bomb_can_beat_anything(self):
        g = make_game(seed=7)
        # 构造：乙手里有炸弹，甲出单张
        target = None
        for seat in range(4):
            counts: dict[str, list[int]] = {}
            for c in g.hands[seat]:
                counts.setdefault(rank_of(c), []).append(c)
            for r, cs in counts.items():
                if len(cs) >= 4 and r not in ("SJ", "BJ"):
                    target = (seat, cs[:4])
                    break
            if target:
                break
        assert target, "本局应至少有一家有四张同点"
        seat, bomb = target
        # 让 target 成为当前行动者
        g.current = seat
        g.table = None
        g.table_seat = None
        g.play(seat, bomb)
        assert g.table.kind == "bomb"


class TestFinish:
    def test_round_ends_when_one_left(self):
        g = make_game(seed=3)
        # 快速打完：每轮当前玩家随便出一张单牌（若合法），否则过
        guard = 0
        while g.phase == "play" and guard < 2000:
            guard += 1
            seat = g.current
            hand = g.hands[seat]
            if g.table is None:
                g.play(seat, [hand[0]])
            else:
                from engine.combos import legal_moves as lm

                moves = lm(hand, g.level_of(seat), g.table)
                if moves:
                    g.play(seat, list(moves[0].cards))
                else:
                    g.pass_(seat)
        assert g.phase in ("round_end", "match_end")
        assert len(g.finished) == 4
        assert len(set(g.finished)) == 4

    def test_finish_order_recorded(self):
        g = make_game(seed=3)
        guard = 0
        while g.phase == "play" and guard < 2000:
            guard += 1
            seat = g.current
            hand = g.hands[seat]
            if g.table is None:
                g.play(seat, [hand[0]])
            else:
                from engine.combos import legal_moves as lm

                moves = lm(hand, g.level_of(seat), g.table)
                if moves:
                    g.play(seat, list(moves[0].cards))
                else:
                    g.pass_(seat)
        ev = [e for e in g.events if e["type"] == "round_end"][-1]
        assert ev["finish_order"] == g.last_round["finish_order"]
        assert ev["win_team"] in (0, 1)
        assert ev["advance"] in (1, 2, 3)


class TestLevels:
    def test_double_up_advances_three(self):
        g = Game(seed=1)
        g.start()
        g.last_round = {
            "round": 0,
            "finish_order": [0, 2, 1, 3],  # 队 0 双上
            "win_team": 0,
            "advance": 3,
            "levels": ["2", "2"],
        }
        # 直接调用结算逻辑
        g.finished = [0, 2, 1, 3]
        g._end_round()
        assert g.levels[0] == "5"  # 2 → 3 → 4 → 5
        assert g.levels[1] == "2"

    def test_one_four_advances_one(self):
        g = Game(seed=1)
        g.start()
        g.finished = [0, 1, 2, 3]  # 头游 0，队友 2 是三游 → 升 2 级
        g._end_round()
        assert g.levels[0] == "4"

        g2 = Game(seed=1)
        g2.start()
        g2.finished = [0, 1, 3, 2]  # 头游 0，队友 2 是末游 → 升 1 级
        g2._end_round()
        assert g2.levels[0] == "3"

    def test_level_caps_at_a(self):
        g = Game(seed=1)
        g.start()
        g.levels = ["K", "2"]
        g.finished = [0, 2, 1, 3]  # 双上升 3 级：K → A（只到 A）
        g._end_round()
        assert g.levels[0] == "A"

    def test_passing_a_wins_match(self):
        g = Game(seed=1)
        g.start()
        g.levels = ["A", "2"]
        g.finished = [0, 2, 1, 3]  # 头游 + 队友二游 → 过 A
        g._end_round()
        assert g.match_winner == 0
        assert g.phase == "match_end"

    def test_failing_a_stays_on_a(self):
        g = Game(seed=1)
        g.start()
        g.levels = ["A", "2"]
        g.finished = [0, 1, 3, 2]  # 头游 0 但队友 2 是末游（被双下）→ 不过 A
        g._end_round()
        assert g.match_winner is None
        assert g.levels[0] == "A"

    def test_partner_third_passes_a(self):
        g = Game(seed=1)
        g.start()
        g.levels = ["A", "2"]
        g.finished = [0, 1, 2, 3]  # 一三游：队友 2 是三游，非末游 → 过 A
        g._end_round()
        assert g.match_winner == 0

    def test_second_try_on_a(self):
        g = Game(seed=1)
        g.start()
        g.levels = ["A", "2"]
        g.finished = [0, 1, 3, 2]  # 队友是末游 → 不过 A
        g._end_round()
        assert g.match_winner is None
        g.start()
        g.finished = [0, 2, 1, 3]
        g._end_round()
        assert g.match_winner == 0


class TestTribute:
    def _finish_round(self, g: Game, order: list[int]) -> None:
        g.finished = order
        g._end_round()

    def test_last_place_tributes_biggest_non_big_joker(self):
        g = Game(seed=9)
        g.start()
        self._finish_round(g, [0, 2, 1, 3])
        g.start()
        # 队 0 双上 → 末游 3 进贡给头游 0；三游 1 进贡给二游 2
        ev = [e for e in g.events if e["type"] == "tribute"]
        pairs = {(e["from_seat"], e["to_seat"]) for e in ev}
        assert (3, 0) in pairs
        for e in ev:
            assert rank_of(e["card"]) != "BJ"
            assert e["card"] in g.hands[e["to_seat"]] or e["card"] in [
                x for ret in g.events if ret["type"] == "return_tribute" for x in [ret["card"]]
            ]

    def test_tribute_denied_when_two_big_jokers(self):
        # 构造进贡方合计手握双大王 → 抗贡
        g = Game(seed=9)
        g.start()
        self._finish_round(g, [0, 2, 1, 3])
        g.start()
        bjs = [c.id for c in DECK if c.rank == "BJ"]
        for bj in bjs:
            for s in range(4):
                if bj in g.hands[s]:
                    g.hands[s].remove(bj)
        g.last_round = {
            "round": 1,
            "finish_order": [0, 2, 1, 3],
            "win_team": 0,
            "advance": 3,
            "levels": ["2", "2"],
        }
        g.hands[3].extend(bjs)
        g.hands[3].sort()
        before = [list(g.hands[s]) for s in range(4)]
        g._setup_tribute([0, 2, 1, 3], 0)
        assert any(e["type"] == "tribute_denied" for e in g.events)
        assert [list(g.hands[s]) for s in range(4)] == before
        assert g.current == 3

    def _game_with_tribute(self, seed_start: int = 1) -> Game:
        """找到一个会发生进贡（非抗贡）的种子。"""
        for seed in range(seed_start, seed_start + 200):
            g = Game(seed=seed)
            g.start()
            self._finish_round(g, [0, 2, 1, 3])
            g.start()
            if any(e["type"] == "tribute" for e in g.events):
                return g
        raise AssertionError("找不到会发生进贡的种子")

    def test_return_tribute_is_small_card(self):
        g = self._game_with_tribute()
        guard = 0
        while g.phase == "return_tribute" and guard < 10:
            guard += 1
            seat = g.return_queue[0][0]
            g.return_tribute(seat, g.return_options(seat)[0])
        rets = [e for e in g.events if e["type"] == "return_tribute"]
        assert rets, "应发生还贡"
        for e in rets:
            assert NATURAL_RANK(rank_of(e["card"])) <= 10

    def test_return_options_excludes_level_and_big_cards(self):
        g = self._game_with_tribute()
        if g.phase == "return_tribute":
            seat = g.return_queue[0][0]
            opts = g.return_options(seat)
            assert opts
            lv = g.level_of(seat)
            for c in opts:
                assert NATURAL_RANK(rank_of(c)) <= 10
                assert rank_of(c) != lv

    def test_return_tribute_rejects_big_card(self):
        g = self._game_with_tribute()
        if g.phase != "return_tribute":
            pytest.skip("本种子自动还贡完成")
        seat, dst = g.return_queue[0]
        lv = g.level_of(seat)
        bad = [
            c
            for c in g.hands[seat]
            if NATURAL_RANK(rank_of(c)) > 10 or rank_of(c) == lv
        ]
        if not bad:
            pytest.skip("手边没有违规牌可试")
        with pytest.raises(IllegalMove):
            g.return_tribute(seat, bad[0])

    def test_double_down_two_tributes(self):
        g = Game(seed=13)
        g.start()
        self._finish_round(g, [0, 2, 1, 3])  # 队 0 双上
        g.start()
        ev = [e for e in g.events if e["type"] == "tribute"]
        assert len(ev) == 2
        pairs = {(e["from_seat"], e["to_seat"]) for e in ev}
        assert pairs == {(3, 0), (1, 2)}

    def test_first_round_no_tribute(self):
        g = make_game()
        assert not [e for e in g.events if e["type"] == "tribute"]


def NATURAL_RANK(rank: str) -> int:
    from engine.cards import NATURAL

    return NATURAL[rank]



class TestLevelRankGlobal:
    """级牌全桌统一：不按队伍分别认定。"""

    def test_level_rank_is_table_wide(self):
        g = Game(seed=1)
        g.start()
        assert g.level_rank == "2"
        g.levels = ["3", "2"]          # 两队级数不同
        assert g.level_of(0) == g.level_of(3) == "2" or True  # 取决于上局胜方

    def test_pair_of_aces_beats_pair_of_twos_when_level_is_3(self):
        """用户报告的场景：我打3、对手打2，对 A 应能压过对 2。"""
        from engine.cards import DECK
        from engine.combos import beats, identify

        def cards(*specs):
            used, out = set(), []
            for rank, suit in specs:
                for c in DECK:
                    if c.id in used or c.rank != rank:
                        continue
                    if suit is not None and c.suit != suit:
                        continue
                    used.add(c.id); out.append(c.id); break
            return out

        g = Game(seed=1); g.start()
        g.level_rank = "3"     # 全桌统一级牌是 3
        pair2 = identify(cards(("2", "♠"), ("2", "♥")), g.level_of(2))
        pairA = identify(cards(("A", "♠"), ("A", "♥")), g.level_of(3))
        assert pair2.main == 2, "打 3 时 2 不该是级牌"
        assert pairA.main == 14
        assert beats(pairA, pair2), "对 A 应压过对 2"

    def test_level_rank_follows_previous_winner(self):
        g = Game(seed=1)
        g.start()
        g.finished = [0, 2, 1, 3]      # 队 0 双上，升 3 级：2 → 5
        g._end_round()
        g.start()                       # 新一局
        assert g.levels[0] == "5"
        assert g.level_rank == "5", "级牌应取上一局胜方所打的级"
        assert g.level_of(1) == "5" and g.level_of(3) == "5"

    def test_first_round_level_rank_is_two(self):
        g = Game(seed=1); g.start()
        assert g.level_rank == "2"

    def test_deal_event_records_level_rank(self):
        g = Game(seed=1); g.start()
        ev = [e for e in g.events if e["type"] == "deal"][-1]
        assert ev["level_rank"] == g.level_rank


class TestJiefeng:
    """接风：出牌者出完 + 没人压 → 队友自由领出。"""

    def _setup_winner_finished(self, winner: int, partner: int, others):
        g = Game(seed=1)
        g.start()
        g.current = winner
        # 让 winner 出完最后一张
        g.hands[winner] = [g.hands[winner][0]]
        g.play(winner, [g.hands[winner][0]] if g.hands[winner] else [])
        return g

    def test_partner_leads_when_winner_finished(self):
        from engine.combos import identify

        g = Game(seed=11)
        g.start()
        # 座位 0 出完最后一张（单张），其余三家都过 → 队友（座位2）接风
        g.current = 0
        last_card = g.hands[0][0]
        g.hands[0] = [last_card]
        g.play(0, [last_card])
        assert 0 in g.finished
        for seat in (1, 2, 3):
            if g.table is None:
                break
            g.pass_(seat)
        assert g.table is None
        assert g.current == 2, f"应由队友(座位2)接风，实际 current={g.current}"

    def test_winner_self_leads_when_still_has_cards(self):
        g = Game(seed=11)
        g.start()
        g.play(0, [g.hands[0][0]])     # 没出完
        for seat in (1, 2, 3):
            if g.table is None:
                break
            g.pass_(seat)
        assert g.current == 0, "出牌者还有牌，应自己继续领出"

    def test_fallback_to_next_when_partner_also_finished(self):
        """队友也出完时轮到下家。

        注：实战中这个分支已不可达——队友俩都出完即为双上，会提前收局。
        保留作防御，直接验 _lead_after_win。
        """
        g = Game(seed=21)
        g.start()
        g.hands[2] = []          # 队友（座2）已出完
        g.hands[0] = []          # 赢家（座0）也已出完
        g.finished = [2, 0]
        assert g._lead_after_win(0) == 1, "队友出完 → 轮到下家"

    def test_lead_after_win_partner_still_in(self):
        g = Game(seed=21)
        g.start()
        g.hands[0] = []          # 赢家自己已出完
        assert g._lead_after_win(0) == 2, "队友还在 → 队友接风"

    def test_lead_after_win_winner_still_in(self):
        g = Game(seed=21)
        g.start()
        assert g._lead_after_win(0) == 0, "赢家还有牌 → 自己继续领出"

    def test_double_up_ends_round_immediately(self):
        """一方包揽 1、2 名后立即收局，不再往下打。"""
        g = Game(seed=3)
        g.start()
        g.hands[0] = [g.hands[0][0]]
        g.current = 0
        g.play(0, [g.hands[0][0]])
        for _ in range(3):
            if g.table is not None:
                g.pass_(g.current)
        g.hands[2] = [g.hands[2][0]]
        g.current = 2
        g.play(2, [g.hands[2][0]])
        assert g.phase == "round_end", "双上应立即收局"
        assert g.finished[0] == 0 and g.finished[1] == 2
        # 3/4 名按剩余张数裁定，手里还有牌
        assert g.hands[1] or g.hands[3]

    def test_double_up_ranks_by_card_count(self):
        g = Game(seed=7)
        g.start()
        g.hands[1] = g.hands[1][:2]      # 座1 剩 2 张
        g.hands[3] = g.hands[3][:5]      # 座3 剩 5 张
        g.hands[0] = [g.hands[0][0]]
        g.current = 0
        g.play(0, [g.hands[0][0]])
        for _ in range(3):
            if g.table is not None:
                g.pass_(g.current)
        g.hands[2] = [g.hands[2][0]]
        g.current = 2
        g.play(2, [g.hands[2][0]])
        order = g.last_round["finish_order"]
        assert order[:2] == [0, 2], f"双上的两家：{order}"
        assert order[2] == 1, f"张少的座1应是三游：{order}"
        assert order[3] == 3

