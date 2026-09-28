"""掼蛋对局状态机：发牌、进贡/抗贡/还贡、出牌、升级、终局。

纯逻辑、无 IO。每次状态转移都产出事件写入 self.events，保证可重放复盘。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Sequence

from .cards import (
    BIG_JOKER,
    CARD_BY_ID,
    DECK,
    LEVEL_ORDER,
    NATURAL,
)
from .combos import Combo, beats, identify, interpretations, legal_moves as _legal_moves

PHASE_PLAY = "play"
PHASE_RETURN_TRIBUTE = "return_tribute"
PHASE_ROUND_END = "round_end"
PHASE_MATCH_END = "match_end"

TEAM_OF = {0: 0, 1: 1, 2: 0, 3: 1}


class IllegalMove(Exception):
    """非法操作（不是当前回合、牌型不对、压不过桌面等）。"""


def team_of(seat: int) -> int:
    return TEAM_OF[seat]


@dataclass
class Game:
    player_names: list[str] = field(default_factory=lambda: ["", "", "", ""])
    seed: int | None = None

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self.events: list[dict[str, Any]] = []
        self.levels: list[str] = ["2", "2"]  # 队伍 0(座位0/2)、队伍 1(座位1/3) 当前级数
        # 全桌统一的级牌：一局只有一个，取上一局胜方当前在打的级。
        # 比较牌力时所有人都按这个级牌抬升，避免「对手的2比我的A大」这种歧义。
        self.level_rank: str = "2"
        self.round_no: int = 0
        self.phase: str = PHASE_ROUND_END  # 尚未开局
        self.hands: list[list[int]] = [[], [], [], []]
        self.finished: list[int] = []  # 出完顺序
        self.current: int = 0
        self.table: Combo | None = None
        self.table_seat: int | None = None
        self.return_queue: list[tuple[int, int]] = []  # (还贡方, 受贡方)
        self.last_round: dict[str, Any] | None = None
        self.match_winner: int | None = None

    # ------------------------------------------------------------ 事件

    def _emit(self, type_: str, **data: Any) -> None:
        self.events.append({"type": type_, "round": self.round_no, **data})

    # ------------------------------------------------------------ 开局

    def start(self, first_leader: int = 0) -> None:
        """开始（或继续）新的一局。"""
        if self.phase == PHASE_MATCH_END:
            raise IllegalMove("对局已结束")
        if self.phase == PHASE_PLAY or self.phase == PHASE_RETURN_TRIBUTE:
            raise IllegalMove("上一局尚未结束")
        self.round_no += 1
        self._deal(first_leader)

    def _deal(self, first_leader: int) -> None:
        cards = list(range(len(DECK)))
        self._rng.shuffle(cards)
        self.hands = [sorted(cards[i * 27:(i + 1) * 27]) for i in range(4)]
        self.finished = []
        self.table = None
        self.table_seat = None
        self.return_queue = []
        self.phase = PHASE_PLAY
        # 级牌 = 上一局胜方当前在打的级；首局双方都在打 2
        if self.last_round:
            self.level_rank = self.levels[self.last_round["win_team"]]
        else:
            self.level_rank = "2"
        self._emit(
            "deal",
            hands=[list(h) for h in self.hands],
            levels=list(self.levels),
            level_rank=self.level_rank,
            player_names=list(self.player_names),
        )

        prev = self.last_round["finish_order"] if self.last_round else None
        if prev is None:
            self.current = first_leader
            self._emit("lead", seat=self.current, reason="first_round")
            return

        self._setup_tribute(prev, first_leader)

    # ------------------------------------------------------------ 进贡

    def _setup_tribute(self, prev_finish: Sequence[int], first_leader: int) -> None:
        head, second, third, last = prev_finish
        # 末游 → 头游；被双下时三游 → 二游
        pairs = [(last, head)]
        if team_of(head) == team_of(second):
            pairs.append((third, second))

        tributors = [p[0] for p in pairs]
        big_jokers = sum(
            1
            for s in tributors
            for c in self.hands[s]
            if CARD_BY_ID[c].rank == BIG_JOKER
        )
        if big_jokers >= 2:
            # 抗贡：进贡方合计两张大王，免进贡，由末游先出
            self._emit("tribute_denied", tributors=tributors, big_jokers=big_jokers)
            self.current = last
            self._emit("lead", seat=self.current, reason="tribute_denied")
            return

        for src, dst in pairs:
            card = self._largest_tribute_card(src)
            self.hands[src].remove(card)
            self.hands[dst].append(card)
            self.hands[dst].sort()
            self._emit("tribute", from_seat=src, to_seat=dst, card=card)

        # 还贡：受贡方回一张不大于 10 且非级牌的牌
        self.return_queue = []
        for src, dst in pairs:
            options = self._return_options(dst)
            if len(options) == 1:
                self._do_return(dst, src, options[0])
            else:
                self.return_queue.append((dst, src))

        if self.return_queue:
            self.phase = PHASE_RETURN_TRIBUTE
            self.current = self.return_queue[0][0]
            self._emit("await_return", seat=self.current)
        else:
            self.phase = PHASE_PLAY
            self.current = last
            self._emit("lead", seat=self.current, reason="after_tribute")

    def _largest_tribute_card(self, seat: int) -> int:
        """进贡：除大王外最大的一张（同点取 id 小者，确定性）。"""
        candidates = [
            c for c in self.hands[seat] if CARD_BY_ID[c].rank != BIG_JOKER
        ]
        return max(candidates, key=lambda c: (NATURAL[CARD_BY_ID[c].rank], CARD_BY_ID[c].suit or ""))

    def _return_options(self, seat: int) -> list[int]:
        level = self.level_rank
        ok = [
            c
            for c in self.hands[seat]
            if NATURAL[CARD_BY_ID[c].rank] <= 10 and CARD_BY_ID[c].rank != level
        ]
        if ok:
            return sorted(ok)
        # 兜底：手边没有 ≤10 的牌时，回最小的非级牌
        fallback = [
            c for c in self.hands[seat] if CARD_BY_ID[c].rank != level
        ]
        return sorted(fallback or self.hands[seat])[:1]

    def _do_return(self, src: int, dst: int, card: int) -> None:
        self.hands[src].remove(card)
        self.hands[dst].append(card)
        self.hands[dst].sort()
        self._emit("return_tribute", from_seat=src, to_seat=dst, card=card)

    def return_tribute(self, seat: int, card: int) -> None:
        if self.phase != PHASE_RETURN_TRIBUTE:
            raise IllegalMove("当前不处于还贡阶段")
        if not self.return_queue or self.return_queue[0][0] != seat:
            raise IllegalMove("还没轮到你还贡")
        src, dst = self.return_queue[0]
        options = self._return_options(src)
        if card not in options:
            raise IllegalMove("只能还不大于 10 且非级牌的牌")
        self._do_return(src, dst, card)
        self.return_queue.pop(0)
        if self.return_queue:
            self.current = self.return_queue[0][0]
            self._emit("await_return", seat=self.current)
        else:
            self.phase = PHASE_PLAY
            # 进贡后由上一局末游先出
            self.current = self.last_round["finish_order"][3] if self.last_round else 0
            self._emit("lead", seat=self.current, reason="after_tribute")

    def return_options(self, seat: int) -> list[int]:
        if self.phase != PHASE_RETURN_TRIBUTE:
            return []
        if not self.return_queue or self.return_queue[0][0] != seat:
            return []
        return self._return_options(seat)

    # ------------------------------------------------------------ 出牌

    def level_of(self, seat: int) -> str:
        """级牌全桌统一，与座位/队伍无关。"""
        return self.level_rank

    def interpretations(self, card_ids: Sequence[int]) -> list[dict]:
        """这组牌的全部打法解释（百搭可变时），供 UI 让玩家选。"""
        return interpretations(card_ids, self.level_rank)

    def legal_moves(self, seat: int) -> list[Combo]:
        if self.phase != PHASE_PLAY or self.current != seat:
            return []
        return _legal_moves(self.hands[seat], self.level_of(seat), self.table)

    def play(self, seat: int, card_ids: Sequence[int], prefer: dict | None = None) -> Combo:
        if self.phase != PHASE_PLAY:
            raise IllegalMove("当前不能出牌")
        if seat != self.current:
            raise IllegalMove("还没轮到你")
        cards = sorted(card_ids)
        if len(set(cards)) != len(cards):
            raise IllegalMove("重复的牌")
        hand = set(self.hands[seat])
        if any(c not in hand for c in cards):
            raise IllegalMove("含有不是你手里的牌")
        combo = identify(cards, self.level_of(seat), prefer=prefer)
        if combo is None:
            raise IllegalMove("不是合法牌型")
        if self.table is not None and self.table_seat == seat:
            raise IllegalMove("你已出过牌，需等其他人")
        if not beats(combo, self.table):
            raise IllegalMove("压不过桌面的牌")

        for c in cards:
            self.hands[seat].remove(c)
        self.table = combo
        self.table_seat = seat
        self._emit(
            "play",
            seat=seat,
            cards=list(combo.cards),   # 已按牌型结构排好
            kind=combo.kind,
            size=combo.size,
            main=combo.main,
        )

        if not self.hands[seat]:
            self.finished.append(seat)
            rank = len(self.finished)
            self._emit("finish", seat=seat, rank=rank)
            remaining = [s for s in range(4) if self.hands[s]]
            if len(self.finished) == 3:
                self.finished.append(remaining[0])
                self._emit("finish", seat=remaining[0], rank=4)
                self._end_round()
                return combo

        self.current = self._next_active(seat)
        return combo

    def pass_(self, seat: int) -> None:
        if self.phase != PHASE_PLAY:
            raise IllegalMove("当前不能过牌")
        if seat != self.current:
            raise IllegalMove("还没轮到你")
        if self.table is None:
            raise IllegalMove("你是本轮首家，必须出牌")
        self._emit("pass", seat=seat)

        nxt = self._next_active(seat)
        anchor = self._first_active_from(self.table_seat)  # type: ignore[arg-type]
        if nxt == anchor:
            # 其余还在手的玩家都过了 → 本轮由最后出牌者获胜
            leader = self._lead_after_win(self.table_seat)  # type: ignore[arg-type]
            self.table = None
            self.table_seat = None
            self.current = leader
            self._emit("lead", seat=leader, reason="trick_won")
        else:
            self.current = nxt

    def _next_active(self, seat: int) -> int:
        for i in range(1, 5):
            s = (seat + i) % 4
            if self.hands[s]:
                return s
        raise IllegalMove("没有可行动的玩家")

    def _lead_after_win(self, winner: int) -> int:
        """赢下这一轮后由谁领出。

        - 赢家还有牌：自己继续领出
        - 赢家已出完手牌：**接风**——让队友（对家）自由领出
        - 队友也已出完：才轮到下家
        """
        if self.hands[winner]:
            return winner
        partner = (winner + 2) % 4
        if self.hands[partner]:
            return partner
        return self._first_active_from(winner)

    def _first_active_from(self, seat: int) -> int:
        if self.hands[seat]:
            return seat
        return self._next_active(seat)

    # ------------------------------------------------------------ 结算

    def _end_round(self) -> None:
        order = list(self.finished)
        head = order[0]
        team = team_of(head)
        partner_seat = (head + 2) % 4
        partner_pos = order.index(partner_seat) + 1  # 名次 2/3/4
        # 头游+二游=双上升 3 级；一三游升 2 级；一四游升 1 级
        advance = {2: 3, 3: 2, 4: 1}[partner_pos]

        on_a = self.levels[team] == "A"
        passed_a = False
        if on_a:
            if partner_pos != 4:
                passed_a = True
                new_level = "A"
            else:
                new_level = "A"  # 打 A 失败，停留 A
        else:
            idx = LEVEL_ORDER.index(self.levels[team])
            new_level = LEVEL_ORDER[min(idx + advance, LEVEL_ORDER.index("A"))]
        self.levels[team] = new_level

        self.last_round = {
            "round": self.round_no,
            "finish_order": order,
            "win_team": team,
            "advance": advance,
            "levels": list(self.levels),
            "level_rank": self.level_rank,
        }
        self._emit(
            "round_end",
            finish_order=order,
            win_team=team,
            advance=advance,
            levels=list(self.levels),
            passed_a=passed_a,
        )

        if passed_a:
            self.match_winner = team
            self.phase = PHASE_MATCH_END
            self._emit("match_end", winner_team=team)
        else:
            self.phase = PHASE_ROUND_END

    # ------------------------------------------------------------ 视图

    def public_state(self) -> dict[str, Any]:
        return {
            "round_no": self.round_no,
            "phase": self.phase,
            "levels": list(self.levels),
            "level_rank": self.level_rank,
            "hands": [list(h) for h in self.hands],
            "finished": list(self.finished),
            "current": self.current,
            "table": (
                {
                    "seat": self.table_seat,
                    "kind": self.table.kind,
                    "size": self.table.size,
                    "cards": list(self.table.cards),
                }
                if self.table
                else None
            ),
            "return_queue": [list(p) for p in self.return_queue],
            "last_round": self.last_round,
            "match_winner": self.match_winner,
            "player_names": list(self.player_names),
        }
