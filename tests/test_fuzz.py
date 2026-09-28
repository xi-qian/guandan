"""随机对局压测：用随机合法出牌打完整场，验证状态机不会卡死、不变量恒成立。

这是规则引擎最强的回归网——任何轮转/结算/进贡 bug 都会在随机牌局里暴露。
"""
from __future__ import annotations

import random

import pytest

from engine.combos import identify
from engine.game import Game, IllegalMove

INVARIANT_MSG = "对局不变量被破坏"


def play_random_game(seed: int, max_actions: int = 5000) -> Game:
    rng = random.Random(seed)
    g = Game(player_names=["A", "B", "C", "D"], seed=seed)
    g.start(first_leader=rng.randrange(4))

    for _ in range(max_actions):
        if g.phase == "match_end":
            return g
        if g.phase == "round_end":
            g.start(first_leader=rng.randrange(4))
            continue

        if g.phase == "return_tribute":
            seat = g.return_queue[0][0]
            options = g.return_options(seat)
            assert options, "还贡必须有可选项"
            g.return_tribute(seat, rng.choice(options))
            continue

        seat = g.current
        moves = g.legal_moves(seat)
        if g.table is None:
            assert moves, "领出时必须有合法出牌"
            choice = rng.choice(moves)
            g.play(seat, list(choice.cards))
        else:
            passes_allowed = True
            plays = moves
            if plays and rng.random() < 0.6:
                choice = rng.choice(plays)
                g.play(seat, list(choice.cards))
            else:
                assert passes_allowed
                g.pass_(seat)

        # ---- 不变量
        all_cards = [c for h in g.hands for c in h]
        assert len(all_cards) == len(set(all_cards)), INVARIANT_MSG
        assert 0 <= g.current <= 3, INVARIANT_MSG
        assert len(g.finished) == len(set(g.finished)), INVARIANT_MSG
        # 真正打完的必须空手；双上按张数裁定的 3/4 名可能还有牌。
        # 事件跨局累积，只看最近一次发牌之后的。
        start = 0
        for i, e in enumerate(g.events):
            if e.get("type") == "deal":
                start = i
        out_by_play = [
            e["seat"] for e in g.events[start:]
            if e["type"] == "finish"
            and e.get("reason") not in ("double_up_by_card_count", "last_remaining")
        ]
        for s in out_by_play:
            assert not g.hands[s], f"已出完的座位 {s} 不应还有牌"
        if g.phase == "play":
            for s in range(4):
                if s in g.finished:
                    assert not g.hands[s], f"进行中已出完的座位 {s} 不应还有牌"
                else:
                    assert g.hands[s], f"未出完的座位 {s} 不应为空"
            assert g.current not in g.finished, "不该轮到已出完的玩家"
        else:
            assert len(g.finished) in (0, 4), "非出牌阶段名次应已定完"

    raise AssertionError(f"seed={seed} 未能在 {max_actions} 步内结束")


class TestRandomGames:
    @pytest.mark.parametrize("seed", range(40))
    def test_game_runs_to_completion(self, seed):
        g = play_random_game(seed)
        assert g.phase in ("round_end", "match_end")
        assert g.last_round is not None
        assert sorted(g.last_round["finish_order"]) == [0, 1, 2, 3]
        assert g.last_round["advance"] in (1, 2, 3)
        # 级数只在 2..A 之间
        for lv in g.levels:
            assert lv in ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")

    @pytest.mark.parametrize("seed", range(40, 60))
    def test_multi_round_match(self, seed):
        """连局：一直打到过 A 或打满 30 局。"""
        rng = random.Random(seed)
        g = Game(player_names=["A", "B", "C", "D"], seed=seed)
        g.start(first_leader=rng.randrange(4))
        for _ in range(40000):
            if g.phase == "match_end":
                break
            if g.phase == "round_end":
                g.start(first_leader=rng.randrange(4))
                continue
            if g.phase == "return_tribute":
                seat = g.return_queue[0][0]
                g.return_tribute(seat, rng.choice(g.return_options(seat)))
                continue
            seat = g.current
            moves = g.legal_moves(seat)
            if g.table is None:
                g.play(seat, list(rng.choice(moves).cards))
            elif moves and rng.random() < 0.6:
                g.play(seat, list(rng.choice(moves).cards))
            else:
                g.pass_(seat)
        else:
            pytest.fail(f"seed={seed} 连局未结束")

        assert g.phase == "match_end"
        assert g.match_winner in (0, 1)
        assert g.levels[g.match_winner] == "A"
        # 过 A 那一局：头游必须来自获胜队，且其队友非末游
        last = g.last_round
        assert last["win_team"] == g.match_winner
        head = last["finish_order"][0]
        partner = (head + 2) % 4
        assert last["finish_order"].index(partner) != 3

    def test_event_log_replays_exactly(self):
        """事件流重放必须复现终局手牌。"""
        g = play_random_game(12345)
        # 取最后一局的 deal 事件重放
        deal = [e for e in g.events if e["type"] == "deal"][-1]
        hands = [list(h) for h in deal["hands"]]
        start_idx = g.events.index(deal)
        for e in g.events[start_idx:]:
            if e["type"] == "play":
                for c in e["cards"]:
                    hands[e["seat"]].remove(c)
            elif e["type"] in ("tribute", "return_tribute"):
                hands[e["from_seat"]].remove(e["card"])
                hands[e["to_seat"]].append(e["card"])
        for s in range(4):
            assert sorted(hands[s]) == sorted(g.hands[s])

    def test_no_card_is_lost_or_duplicated(self):
        g = play_random_game(777)
        deal = [e for e in g.events if e["type"] == "deal"][-1]
        dealt = sorted(sum(deal["hands"], []))
        assert dealt == list(range(108))
        start_idx = g.events.index(deal)
        played = sorted(
            c
            for e in g.events[start_idx:]
            if e["type"] == "play"
            for c in e["cards"]
        )
        remaining = sorted(sum(g.hands, []))
        assert sorted(played + remaining) == dealt

    def test_illegal_actions_rejected_consistently(self):
        g = Game(seed=99)
        g.start()
        with pytest.raises(IllegalMove):
            g.play(1, [g.hands[1][0]])  # 不是你的回合
        with pytest.raises(IllegalMove):
            g.play(0, [])  # 空牌
        with pytest.raises(IllegalMove):
            g.play(0, [g.hands[0][0], g.hands[0][0]])  # 重复牌
        with pytest.raises(IllegalMove):
            g.pass_(0)  # 领出不能过
        # 出不是自己手里的牌
        foreign = next(c for s in (1, 2, 3) for c in g.hands[s])
        with pytest.raises(IllegalMove):
            g.play(0, [foreign])
        # 非法牌型
        a, b = g.hands[0][0], g.hands[0][1]
        if identify([a, b], g.level_of(0)) is None:
            with pytest.raises(IllegalMove):
                g.play(0, [a, b])
