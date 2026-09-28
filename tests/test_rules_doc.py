"""规则文档与引擎实现的一致性测试。

bot/rules_text.py 是给大模型看的规则说明；如果它和 engine/ 的实际行为不一致，
模型就会被教错。这里把文本里的每条断言都拿引擎跑一遍。
"""
from __future__ import annotations

import pytest

from api.app import KIND_LABEL
from bot.llm_bot import SYSTEM_PROMPT
from bot.rules_text import RULES_TEXT
from engine.cards import DECK
from engine.combos import (
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
    identify,
)
from engine.game import Game


def cards(*specs) -> list[int]:
    used: set[int] = set()
    out: list[int] = []
    for rank, suit in specs:
        for c in DECK:
            if c.id in used or c.rank != rank:
                continue
            if suit is None and c.suit is not None:
                continue
            if suit is not None and c.suit != suit:
                continue
            used.add(c.id)
            out.append(c.id)
            break
        else:
            raise AssertionError(f"找不到牌 {(rank, suit)}")
    return out


# ---------------------------------------------------------------- 文本覆盖

class TestRulesTextCoverage:
    def test_system_prompt_carries_full_rules(self):
        assert RULES_TEXT in SYSTEM_PROMPT
        assert "只输出一个 JSON" in SYSTEM_PROMPT

    @pytest.mark.parametrize("kind,label", sorted(KIND_LABEL.items()))
    def test_every_card_type_is_documented(self, kind, label):
        assert label in RULES_TEXT, f"牌型「{label}」未写进规则文档"

    @pytest.mark.parametrize(
        "needle",
        [
            "108 张", "每人 27 张",  # 用牌
            "四炸 < 五炸 < **同花顺** < 六炸",  # 炸弹层级
            "天王炸", "压一切",
            "级牌", "A 之上", "小王之下",
            "JQKA2", "A2345", "10JQKA",  # 顺子两头
            "AA2233", "AAA222",  # 连对/钢板两头
            "双上", "升 3 级", "升 2 级", "升 1 级",
            "除大王外最大的牌", "抗贡", "不大于 10 且不是本局级牌",
            "头游", "队友不是末游",
        ],
    )
    def test_key_mechanics_are_documented(self, needle):
        assert needle in RULES_TEXT, f"规则要点「{needle}」未写进规则文档"

    def test_level_card_rule_mentions_natural_rank_in_runs(self):
        assert "顺子、三连对、钢板" in RULES_TEXT
        assert "自然点数" in RULES_TEXT

    def test_strategy_section_present(self):
        assert "策略要点" in SYSTEM_PROMPT
        assert "炸弹是稀缺资源" in SYSTEM_PROMPT


# ---------------------------------------------------------------- 文档 ↔ 引擎

class TestDocumentedRulesMatchEngine:
    """RULES_TEXT 里说的每一条，都要和引擎实际行为一致。"""

    LV2 = "2"  # 文档例子里用「打 2」说明级牌

    def test_rank_order_documented(self):
        # 2 < 3 < ... < A < 级牌 < 小王 < 大王
        lv = self.LV2
        chain = [
            ("3", "♠"), ("A", "♠"), ("2", "♠"), ("SJ", None), ("BJ", None),
        ]
        for low, high in zip(chain, chain[1:]):
            a = identify(cards(low), lv)
            b = identify(cards(high), lv)
            assert beats(b, a), f"{high} 应大于 {low}"

    def test_level_card_between_ace_and_small_joker(self):
        lv = identify(cards(("2", "♠")), "2")
        ace = identify(cards(("A", "♠")), "2")
        sj = identify(cards(("SJ", None)), "2")
        assert beats(lv, ace)
        assert beats(sj, lv)

    def test_documented_card_types_recognized(self):
        lv = self.LV2
        cases = {
            SINGLE: [cards(("9", "♠"))],
            PAIR: [cards(("7", "♠"), ("7", "♥"))],
            TRIPLE: [cards(("5", "♠"), ("5", "♥"), ("5", "♣"))],
            TRIPLE_PAIR: [
                cards(("8", "♠"), ("8", "♥"), ("8", "♣"), ("3", "♠"), ("3", "♥"))
            ],
            STRAIGHT: [cards(("3", "♠"), ("4", "♥"), ("5", "♣"), ("6", "♦"), ("7", "♠"))],
            PAIR_RUN: [
                cards(("5", "♠"), ("5", "♥"), ("6", "♣"), ("6", "♦"), ("7", "♠"), ("7", "♥"))
            ],
            TRIPLE_RUN: [
                cards(("4", "♠"), ("4", "♥"), ("4", "♣"), ("5", "♦"), ("5", "♠"), ("5", "♥"))
            ],
            BOMB: [cards(("6", "♠"), ("6", "♥"), ("6", "♣"), ("6", "♦"))],
            STRAIGHT_FLUSH: [
                cards(("3", "♥"), ("4", "♥"), ("5", "♥"), ("6", "♥"), ("7", "♥"))
            ],
            ROCKET: [cards(("SJ", None), ("SJ", None), ("BJ", None), ("BJ", None))],
        }
        for kind, groups in cases.items():
            for g in groups:
                c = identify(g, lv)
                assert c is not None and c.kind == kind, f"{kind} 识别失败"

    def test_ace_two_way_straights(self):
        lv = self.LV2
        assert identify(cards(("A", "♠"), ("2", "♥"), ("3", "♣"), ("4", "♦"), ("5", "♠")), lv).kind == STRAIGHT
        assert identify(cards(("10", "♠"), ("J", "♥"), ("Q", "♣"), ("K", "♦"), ("A", "♠")), lv).kind == STRAIGHT
        assert identify(cards(("J", "♠"), ("Q", "♥"), ("K", "♣"), ("A", "♦"), ("2", "♠")), lv) is None

    def test_ace_two_way_runs(self):
        lv = self.LV2
        assert identify(cards(("A", "♠"), ("A", "♥"), ("2", "♣"), ("2", "♦"), ("3", "♠"), ("3", "♥")), lv).kind == PAIR_RUN
        assert identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣"), ("2", "♦"), ("2", "♠"), ("2", "♥")), lv).kind == TRIPLE_RUN

    def test_level_card_uses_natural_rank_in_runs(self):
        # 文档：打 2 时 2 在顺子里还是 2，A2345 依然合法
        c = identify(cards(("A", "♠"), ("2", "♥"), ("3", "♣"), ("4", "♦"), ("5", "♠")), "2")
        assert c.kind == STRAIGHT and c.main == 5

    def test_bomb_hierarchy_as_documented(self):
        lv = self.LV2
        b4 = identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣"), ("A", "♦")), lv)
        b5 = identify(cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦"), ("3", "♠")), lv)
        sf = identify(cards(("3", "♥"), ("4", "♥"), ("5", "♥"), ("6", "♥"), ("7", "♥")), lv)
        b6 = identify(
            cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦"), ("3", "♠"), ("3", "♥")), lv
        )
        b7 = identify(
            cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦"), ("3", "♠"), ("3", "♥"), ("3", "♣")), lv
        )
        b8 = identify(
            cards(*[("9", s) for s in ("♠", "♥", "♣", "♦")]
                  + [("9", "♠"), ("9", "♥"), ("9", "♣"), ("9", "♦")]), lv
        )
        rocket = identify(cards(("SJ", None), ("SJ", None), ("BJ", None), ("BJ", None)), lv)

        ladder = [b4, b5, sf, b6, b7, b8, rocket]
        for i, low in enumerate(ladder):
            for high in ladder[i + 1:]:
                assert beats(high, low), f"{high.kind}/{high.size} 应大于 {low.kind}/{low.size}"

    def test_bomb_beats_any_normal(self):
        lv = self.LV2
        bomb = identify(cards(("4", "♠"), ("4", "♥"), ("4", "♣"), ("4", "♦")), lv)
        normal = [
            identify(cards(("A", "♠")), lv),
            identify(cards(("A", "♠"), ("A", "♥")), lv),
            identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣")), lv),
            identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣"), ("K", "♠"), ("K", "♥")), lv),
            identify(cards(("10", "♠"), ("J", "♥"), ("Q", "♣"), ("K", "♦"), ("A", "♠")), lv),
        ]
        for n in normal:
            assert beats(bomb, n)

    def test_normal_types_cannot_cross(self):
        lv = self.LV2
        pair = identify(cards(("3", "♠"), ("3", "♥")), lv)
        single = identify(cards(("A", "♠")), lv)
        assert not beats(pair, single)
        assert not beats(single, pair)

    def test_triple_pair_compares_by_triple(self):
        lv = self.LV2
        a = identify(cards(("9", "♠"), ("9", "♥"), ("9", "♣"), ("A", "♠"), ("A", "♥")), lv)
        b = identify(cards(("10", "♠"), ("10", "♥"), ("10", "♣"), ("3", "♠"), ("3", "♥")), lv)
        assert beats(b, a)

    def test_same_type_same_size_only(self):
        lv = self.LV2
        s5a = identify(cards(("3", "♠"), ("4", "♥"), ("5", "♣"), ("6", "♦"), ("7", "♠")), lv)
        s5b = identify(cards(("8", "♠"), ("9", "♥"), ("10", "♣"), ("J", "♦"), ("Q", "♠")), lv)
        assert beats(s5b, s5a)


class TestDocumentedUpgradesMatchEngine:
    """文档里的升级表与过 A 条件。"""

    def _finish(self, order):
        g = Game(seed=1)
        g.start()
        g.finished = list(order)
        g._end_round()
        return g

    def test_double_up_three_levels(self):
        g = self._finish([0, 2, 1, 3])
        assert g.levels[0] == "5" and g.last_round["advance"] == 3

    def test_one_three_two_levels(self):
        g = self._finish([0, 1, 2, 3])
        assert g.levels[0] == "4" and g.last_round["advance"] == 2

    def test_one_four_one_level(self):
        g = self._finish([0, 1, 3, 2])
        assert g.levels[0] == "3" and g.last_round["advance"] == 1

    def test_passing_a_needs_head_and_partner_not_last(self):
        g = Game(seed=1)
        g.start()
        g.levels = ["A", "2"]
        g.finished = [0, 1, 2, 3]  # 队友 2 是三游
        g._end_round()
        assert g.match_winner == 0

        g2 = Game(seed=1)
        g2.start()
        g2.levels = ["A", "2"]
        g2.finished = [0, 1, 3, 2]  # 队友 2 是末游
        g2._end_round()
        assert g2.match_winner is None and g2.levels[0] == "A"


class TestDocumentedTributeMatchesEngine:
    """文档里的进贡 / 抗贡 / 还贡。"""

    def test_tribute_is_biggest_except_big_joker(self):
        from engine.cards import NATURAL

        g = Game(seed=5)
        g.start()
        g.finished = [0, 2, 1, 3]
        g._end_round()
        g.start()
        ev = [e for e in g.events if e["type"] == "tribute"]
        assert ev, "应发生进贡"
        for e in ev:
            from engine.cards import CARD_BY_ID
            assert CARD_BY_ID[e["card"]].rank != "BJ", "文档说大王不能进贡"

    def test_two_big_jokers_anti_tribute(self):
        from engine.cards import DECK as D

        g = Game(seed=5)
        g.start()
        g.finished = [0, 2, 1, 3]
        g._end_round()
        g.start()
        bjs = [c.id for c in D if c.rank == "BJ"]
        for bj in bjs:
            for s in range(4):
                if bj in g.hands[s]:
                    g.hands[s].remove(bj)
        g.hands[3].extend(bjs)
        g.hands[3].sort()
        g.last_round = {"round": 1, "finish_order": [0, 2, 1, 3], "win_team": 0,
                        "advance": 3, "levels": ["2", "2"]}
        g._setup_tribute([0, 2, 1, 3], 0)
        assert any(e["type"] == "tribute_denied" for e in g.events)
        assert g.current == 3, "文档说抗贡后由末游先出"

    def test_return_tribute_at_most_ten_not_level(self):
        from engine.cards import CARD_BY_ID, NATURAL

        for seed in range(1, 60):
            g = Game(seed=seed)
            g.start()
            g.finished = [0, 2, 1, 3]
            g._end_round()
            g.start()
            if not any(e["type"] == "tribute" for e in g.events):
                continue
            while g.phase == "return_tribute":
                seat = g.return_queue[0][0]
                opts = g.return_options(seat)
                assert opts
                for c in opts:
                    assert NATURAL[CARD_BY_ID[c].rank] <= 10
                    assert CARD_BY_ID[c].rank != g.level_of(seat)
                g.return_tribute(seat, opts[0])
            return
        pytest.fail("找不到会发生进贡的种子")



class TestNewRulesDocumented:
    """级牌全桌统一、接风——这两条改过，文档必须写清，否则模型按旧直觉算。"""

    def test_level_card_is_table_wide(self):
        assert "全桌统一" in RULES_TEXT
        assert "级牌不看队伍" in RULES_TEXT
        assert "与你是哪队、谁出的牌都无关" in RULES_TEXT

    def test_level_card_has_counter_example(self):
        # 必须有反例，否则模型会以为对手的 2 也算级牌
        assert "对 A 可以压对 2" in RULES_TEXT
        assert "反过来若本局级牌是 2" in RULES_TEXT

    def test_level_card_source_documented(self):
        assert "上一局胜方当前在打的级" in RULES_TEXT

    def test_jiefeng_condition_is_conjunctive(self):
        # 接风 = 出完 AND 没人压，两个条件缺一不可
        assert "接风" in RULES_TEXT
        assert "出牌者已出完手牌" in RULES_TEXT
        assert "其余还在手的人都过" in RULES_TEXT
        assert "队友（对家）" in RULES_TEXT

    def test_jiefeng_spells_out_non_condition(self):
        assert "只要有人压过" in RULES_TEXT

    def test_jiefeng_fallback_documented(self):
        assert "队友也已出完" in RULES_TEXT
        assert "才轮到出牌者的下家" in RULES_TEXT

    def test_return_tribute_uses_table_level(self):
        assert "不是本局级牌" in RULES_TEXT
        assert "不看队伍" in RULES_TEXT

    def test_strategy_mentions_jiefeng(self):
        assert "接风意识" in SYSTEM_PROMPT

    def test_system_prompt_carries_all_of_the_above(self):
        for kw in ("全桌统一", "接风", "级牌不看队伍", "接风意识"):
            assert kw in SYSTEM_PROMPT



class TestNoSeatArithmeticInPrompt:
    """队友身份必须显式给出。曾因写「(你的座位+2)%4」逼模型自己算，
    结果它把对手当队友一直让牌。"""

    def test_no_modular_formula(self):
        assert "(你的座位+2)%4" not in SYSTEM_PROMPT
        assert "你的座位+2" not in SYSTEM_PROMPT

    def test_points_at_explicit_labels(self):
        assert "【队友】/【对手】标注" in SYSTEM_PROMPT
        assert "不要自己做座位运算" in SYSTEM_PROMPT



class TestStraightFlushValue:
    """同花顺的价值必须写清。实测踩过：模型按「先比张数」误以为同花顺=五炸档，
    把它当廉价炸弹压普通顺子。"""

    def test_ladder_says_straight_flush_beats_five_bomb(self):
        assert "五炸 < **同花顺** < 六炸" in RULES_TEXT or "五炸 < 同花顺 < 六炸" in RULES_TEXT

    def test_explicitly_spelled_out(self):
        assert "比任何五炸、任何四炸都大" in RULES_TEXT
        assert "价值接近六炸" in RULES_TEXT

    def test_count_rule_scoped_to_same_rank_bombs(self):
        assert "只适用于**同点炸弹之间**" in RULES_TEXT
        assert "不参与「按张数」的比较" in RULES_TEXT

    def test_calls_out_the_cheap_bomb_misreading(self):
        assert "同花顺不是「廉价炸弹」" in RULES_TEXT

    def test_engine_agrees_with_documented_ladder(self):
        lv = "2"
        b5 = identify(cards(("3", "♠"), ("3", "♥"), ("3", "♣"), ("3", "♦"), ("3", "♠")), lv)
        sf = identify(cards(("3", "♥"), ("4", "♥"), ("5", "♥"), ("6", "♥"), ("7", "♥")), lv)
        b4 = identify(cards(("A", "♠"), ("A", "♥"), ("A", "♣"), ("A", "♦")), lv)
        assert beats(sf, b5), "文档说同花顺大于五炸"
        assert beats(sf, b4), "文档说同花顺大于四炸"



class TestBombRarityTable:
    """稀有度表：20 万次模拟得出的 27 张手牌出现概率。"""

    TABLE = [
        ("四炸", "84.8%", "几乎人人都有"),
        ("五炸", "27.1%", "1/4"),
        ("同花顺", "31.3%", "1/3"),
        ("六炸", "3.8%", "1/26"),
        ("七炸", "0.27%", "1/376"),
        ("天王炸", "0.33%", "1/301"),
        ("八炸", "0.01%", "1/10345"),
    ]

    @pytest.mark.parametrize("name,prob,odds", TABLE)
    def test_each_bomb_listed_with_probability(self, name, prob, odds):
        assert name in RULES_TEXT
        assert prob in RULES_TEXT, f"{name} 的概率 {prob} 未写进文档"
        assert odds in RULES_TEXT

    def test_strength_and_rarity_are_distinct(self):
        assert "强度 ≠ 稀有度" in RULES_TEXT

    def test_four_bomb_not_treated_as_scarce(self):
        assert "几乎人人都有" in RULES_TEXT
        assert "不是稀缺资源" in RULES_TEXT

    def test_straight_flush_rarity_called_out(self):
        assert "稀有度只有 1/3" in RULES_TEXT
        assert "常见不等于廉价" in RULES_TEXT

    def test_real_bombs_flagged_as_scarce(self):
        assert "六炸以上才是真稀缺" in RULES_TEXT

    def test_straight_flush_commoner_than_five_bomb(self):
        """反直觉点：同花顺比五炸常见，但强度更高。必须写明免得模型据此低估。"""
        assert "比五炸还常见" in RULES_TEXT
        assert "常见不等于弱" in RULES_TEXT

    def test_rarity_table_ordered_by_strength(self):
        """稀有度表里的强度列必须从低到高，别把表写反。"""
        seg = RULES_TEXT[RULES_TEXT.index("各种炸弹的稀有度"):]
        idx = [seg.index(f"| {n} |") for n, _, _ in self.TABLE]
        assert idx == sorted(idx), "稀有度表应按强度从低到高排列"

    def test_probabilities_are_descending_in_rarity(self):
        """除同花顺/五炸顺序外，稀有度大体递减。"""
        probs = [float(p.rstrip("%")) for _, p, _ in self.TABLE]
        # 四炸 84.8 → 五炸 27.1 → 同花顺 31.2 → 六炸 3.8 → ...
        assert probs[0] > 50, "四炸应最常见"
        assert probs[-1] < 0.1, "八炸应最罕见"
        assert probs[2] > probs[3], "同花顺应比六炸常见"



class TestWildcardsDocumented:
    """逢人配必须写进规则，否则模型不知道红桃级牌是百搭。"""

    def test_section_present(self):
        assert "逢人配（百搭）" in RULES_TEXT
        assert "红桃级牌" in RULES_TEXT

    def test_two_wildcards(self):
        assert "2 张红桃级牌" in RULES_TEXT

    def test_cannot_be_joker(self):
        assert "不能当王" in RULES_TEXT

    def test_level_card_of_heart_is_wild(self):
        assert "打几就是几的红桃是百搭" in RULES_TEXT

    def test_multiple_interpretations_documented(self):
        assert "同一批牌可能有多种打法" in RULES_TEXT
        assert "444+33" in RULES_TEXT and "333+44" in RULES_TEXT

    def test_engine_matches_doc(self):
        from engine.combos import identify, interpretations
        # 打 2 时红桃 2 是百搭
        assert identify(cards(("2", "♥"), ("3", "♠")), "2").kind == PAIR
        # 打 3 时红桃 2 不是百搭
        assert identify(cards(("2", "♥"), ("3", "♠")), "3") is None
        # 百搭不能当王
        assert identify(cards(("BJ", None), ("2", "♥")), "2") is None
        # 多种解释
        h = cards(("2", "♥"), ("3", "♠"), ("3", "♥"), ("4", "♠"), ("4", "♥"))
        mains = {round(it["main"]) for it in interpretations(h, "2")}
        assert mains == {3, 4}
