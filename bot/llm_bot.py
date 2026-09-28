"""用大模型决策的掼蛋 AI：给一个 OpenAI 兼容地址 + key 即可接入。

决策流程：
  1. 把「当前局面 + 编号后的合法出牌」交给模型，让它选一个编号
  2. 校验编号合法（服务端给的合法出牌里必须有这一手）
  3. 模型返回非法 / 超时 / 报错 → 回落到内置的规则策略，保证牌局不卡死

模型只做「从合法动作里挑一个」，所以它永远不会出非法牌，也不会破坏规则引擎。
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

from .client import GuandanClient
from .llm_client import LLMClient, LLMError
from .rules_text import RULES_TEXT, SYSTEM_PROMPT
from engine.combos import order_cards


def _card_str(c: dict[str, Any]) -> str:
    if c.get("suit") is None:
        return "大王" if c.get("rank") == "BJ" else "小王"
    return f"{c['label']}{c['suit']}"


def _hand_str(cards: list[dict[str, Any]] | None) -> str:
    if not cards:
        return "（无）"
    return " ".join(_card_str(c) for c in cards)


def _move_str(m: dict[str, Any]) -> str:
    if m.get("pass"):
        return "过牌"
    cards = m.get("cards") or []
    if cards and m.get("kind") and not m.get("pass"):
        try:
            ids = [c["id"] for c in cards]
            ordered = {c["id"]: c for c in cards}
            cards = [ordered[i] for i in order_cards(ids, m["kind"])]
        except Exception:
            pass
    return f"{m.get('kind_label', m.get('kind', '?'))}（{m.get('size')}张）：{_hand_str(cards)}"


RANK_ORDER = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "SJ", "BJ"]
RANK_LABEL_CN = {
    **{r: r for r in ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")},
    "SJ": "小王", "BJ": "大王",
}


def _seen_line(seen_counts: dict[str, Any] | None) -> str:
    """把记牌表压成一行：`2:3/8` 表示 2 已见 3 张、外面还可能有 5 张。"""
    if not seen_counts:
        return "（无记牌数据）"
    parts = []
    for r in RANK_ORDER:
        d = seen_counts.get(r)
        if not d:
            continue
        parts.append(f"{RANK_LABEL_CN[r]}:{d['seen']}/{d['total']}")
    return "  ".join(parts)


def _history_line(round_plays: list[dict[str, Any]] | None, limit: int = 12) -> str:
    """本局出牌记录，旧→新，最多显示 limit 手。"""
    if not round_plays:
        return "（本局还没有出牌）"
    seq = round_plays[-limit:]
    parts = []
    for p in seq:
        if p.get("passed"):
            parts.append(f"座{p['seat']}过")
        else:
            parts.append(f"座{p['seat']} {_move_str(p)}")
    prefix = f"（共 {len(round_plays)} 手，只列最近 {len(seq)} 手）" if len(round_plays) > limit else ""
    return prefix + " → ".join(parts)


def build_prompt(
    view: dict[str, Any],
    moves: list[dict[str, Any]],
    past_intents: list[str] | None = None,
) -> str:
    you = view["you"]
    my_seat = you.get("seat", 0)
    my_team = you.get("team", my_seat % 2)
    partner_seat = (my_seat + 2) % 4
    players_by_seat = {p["seat"]: p for p in view.get("players", [])}
    others = [p for p in view.get("players", []) if p["seat"] != my_seat]
    levels = " / ".join(
        f"队伍{t['team']} 打 {t['level']}" for t in view.get("levels", [])
    )
    table = view.get("table")
    table_line = (
        f"座位 {table['seat']} 出了 {table['kind_label']}（{table['size']}张）："
        f"{_hand_str(table.get('cards'))}"
        if table
        else "本轮自由出牌（你是首家）"
    )
    last_round = view.get("last_round")
    last_line = (
        f"上一局名次顺序={last_round['finish_order']}，"
        f"获胜队伍={last_round['win_team']}，升 {last_round['advance']} 级"
        if last_round
        else "（本桌第一局）"
    )

    lv = view.get("level_rank")
    level_line = (
        f"★ 本局打 {lv}：{lv} 是级牌，排在 A 之上、小王之下，"
        f"比任何非级牌都大；其它点数按正常大小。"
        if lv else "★ 本局打几未知"
    )
    lines = [
        f"局面：第 {view.get('round_no', 1)} 局，阶段={view.get('phase')}。",
        level_line,
        f"各队进度（只关升级，不影响牌力）：{levels}；你的队伍={you.get('team')}。",
        f"你的手牌（{you.get('hand_size')} 张）：{_hand_str(you.get('hand'))}",
        f"桌面：{table_line}",
        f"你的队友是座位 {partner_seat}（{players_by_seat.get(partner_seat, {}).get('name', '?')}）；"
        f"对手是另外两家。",
        "其他玩家："
        + "、".join(
            f"座位{p['seat']}({p['name']}) 剩 {p['hand_size']} 张"
            f"【{'队友' if p['seat'] == partner_seat else '对手'}】"
            for p in others
        ),
        "",
        f"记牌（已见=你手上的+本局已打出的；每点共 8 张，大小王各 2 张）：",
        "  " + _seen_line(view.get("seen_counts")),
        f"本局出牌记录：{_history_line(view.get('round_plays'))}",
        "",
        f"历史：{last_line}",
    ]
    if past_intents:
        lines += [
            "",
            "你此前的打算（保持策略连贯，不要自相矛盾）：",
            *[f"  - {x}" for x in past_intents],
        ]
    lines += [
        "",
        f"现在轮到你。可以从下面 {len(moves)} 个合法动作里选 1 个（编号 0 到 {len(moves) - 1}）：",
    ]
    for i, m in enumerate(moves):
        lines.append(f"  [{i}] {_move_str(m)}")
    lines.append("")
    lines.append('只输出 JSON：{"index": <编号>, "reason": "<一句话>"}')
    return "\n".join(lines)


def parse_index(text: str, n_moves: int) -> int | None:
    """从模型输出里解析出动作编号；解析不出返回 None。"""
    if not text:
        return None
    text = text.strip()

    # 1) 直接是 JSON，或正文里嵌了 JSON
    for match in re.finditer(r"\{[^{}]*\}", text, re.S):
        try:
            obj = json.loads(match.group(0))
        except Exception:
            continue
        if isinstance(obj, dict) and "index" in obj:
            try:
                idx = int(obj["index"])
            except (TypeError, ValueError):
                continue
            # 模型明确给了 index：合法就用；越界判定为无效回答，
            # 不再回退到裸数字（避免把 -1 误读成 1）
            return idx if 0 <= idx < n_moves else None

    # 2) 裸数字（不把负号后面的数字当成编号）
    m = re.search(r"(?<![-\d])(\d+)\b", text)
    if m:
        idx = int(m.group(1))
        return idx if 0 <= idx < n_moves else None

    return None


class LLMBot(GuandanClient):
    """在 GuandanClient 之上加一层大模型决策，失败时回落到规则策略。"""

    def __init__(
        self,
        base: str,
        room_id: str | None = None,
        name: str = "llm",
        llm: LLMClient | None = None,
        verbose: bool = True,
    ) -> None:
        super().__init__(base, room_id=room_id, name=name)
        self.llm = llm
        self.verbose = verbose
        self.last_prompt: str | None = None
        self.last_reply: str | None = None
        # 自己近期的决策意图：请求仍无状态，但把「为什么这么打」喂回去
        self.past_intents: list[str] = []
        self.memory_limit = 6
        # 决策缓存：同一个决策点只打一次 LLM，动作失败重试时直接复用
        self._decision_cache: dict[tuple, dict[str, Any] | None] = {}
        self.llm_calls = 0          # 实际打到 LLM 的次数
        self.last_decision_source = "none"   # llm | fallback | cache | rules
        self.hand_no = 0
        self.decision_points = 0    # 不同决策点的个数
        self.cache_hits = 0

    # ------------------------------------------------------------ 决策

    def decide(self, view: dict[str, Any], moves: list[dict[str, Any]]) -> dict[str, Any] | None:
        # 还贡：交给模型在可选项里挑一张
        tribute = view.get("tribute") or {}
        if (
            view.get("phase") == "return_tribute"
            and tribute.get("waiting_seat") == view.get("you", {}).get("seat")
        ):
            options = tribute.get("options") or []
            if options:
                return self._decide_return(view, options)

        # 局末 / 等待 / 非自己回合：沿用规则策略（它只负责推进流程）
        if view.get("phase") in ("round_end", "match_end", "waiting") or not view.get(
            "your_turn"
        ):
            return super().decide(view, moves)

        if not moves:
            return None

        # 同一个决策点只算一次：动作失败重试时复用上次的决定，不重复计费
        key = self._decision_key(view, moves)
        if key in self._decision_cache:
            self.cache_hits += 1
            self.last_decision_source = "cache"
            self._log(f"#{self.hand_no} 命中决策缓存（同一局面重试，不重复调用 LLM）")
            return self._decision_cache[key]

        self.decision_points += 1
        self.hand_no += 1
        self._log_context(view, moves)
        idx = self._ask_index(view, moves)
        if idx is None:
            self.last_decision_source = "fallback"
            self._log(f"#{self.hand_no} [fallback→规则] 模型未能给出合法编号")
            act = super().decide(view, moves)
            self._remember(view, act, reason="（回落规则策略）")
            self._decision_cache[key] = act
            return act

        self.last_decision_source = "llm"
        chosen = moves[idx]
        if chosen.get("pass"):
            plays = sum(1 for m in moves if not m.get("pass"))
            warn = f"  ⚠ 有 {plays} 手可出却选择过" if plays else ""
            self._log(f"#{self.hand_no} [LLM] 选了 [{idx}] 过牌 —— {self._last_reason()}{warn}")
            act = {"action": "pass"}
        else:
            self._log(f"#{self.hand_no} [LLM] 选了 [{idx}] {_move_str(chosen)} —— {self._last_reason()}")
            act = {"action": "play", "cards": list(chosen["card_ids"])}
        self._remember(view, act, reason=self._last_reason())
        self._decision_cache[key] = act
        return act

    def _decide_return(
        self, view: dict[str, Any], options: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        moves = [
            {
                "card_ids": [c["id"]],
                "cards": [c],
                "kind": "return",
                "kind_label": "还贡",
                "size": 1,
            }
            for c in options
        ]
        intent_block = ""
        if self.past_intents:
            intent_block = (
                "\n你此前的打算（保持策略连贯）：\n"
                + "\n".join(f"  - {x}" for x in self.past_intents)
            )
        prompt = (
            f"你收到进贡，需要还一张牌给对手（不大于 10 的非级牌）。\n"
            f"你的手牌（{view['you']['hand_size']} 张）：{_hand_str(view['you'].get('hand'))}\n"
            f"可还的牌，选 1 个：\n"
            + "\n".join(f"  [{i}] {_move_str(m)}" for i, m in enumerate(moves))
            + intent_block
            + "\n只输出 JSON：{\"index\": <编号>, \"reason\": \"<一句话>\"}"
        )
        key = self._decision_key(view, moves)
        if key in self._decision_cache:
            self.cache_hits += 1
            self.last_decision_source = "cache"
            return self._decision_cache[key]

        self.decision_points += 1
        self.hand_no += 1
        self._log_context(view, moves, prefix="还贡")
        idx = self._ask_index(view, moves, prompt_override=prompt)
        if idx is None:
            self.last_decision_source = "fallback"
            best = min(options, key=lambda c: (c.get("rank") not in ("BJ", "SJ"), c["id"]))
            act: dict[str, Any] | None = {"action": "return", "card": best["id"]}
            self._log(f"#{self.hand_no} [fallback→规则] 还贡取最小 {act['card']}")
        else:
            self.last_decision_source = "llm"
            act = {"action": "return", "card": moves[idx]["card_ids"][0]}
            self._log(f"#{self.hand_no} [LLM] 还贡选了 [{idx}] —— {self._last_reason()}")
        self._decision_cache[key] = act
        return act

    def _ask_index(
        self,
        view: dict[str, Any],
        moves: list[dict[str, Any]],
        prompt_override: str | None = None,
    ) -> int | None:
        if self.llm is None:
            return None
        prompt = prompt_override or build_prompt(view, moves, self.past_intents)
        self.last_prompt = prompt
        before = self.llm_calls
        try:
            reply = self.llm.complete(SYSTEM_PROMPT, prompt)
            self.llm_calls += 1
        except LLMError as e:
            self.llm_calls += 1  # 失败也是一次真实计费
            self._log(f"[LLM 调用失败] {e} → 将回落规则策略")
            return None
        self.last_reply = reply
        idx = parse_index(reply, len(moves))
        if idx is not None:
            return idx
        # 模型说"过牌"但没给编号时，找到 pass 那一手
        if reply and ("过牌" in reply or re.search(r"\bpass\b", reply, re.I)):
            for i, m in enumerate(moves):
                if m.get("pass"):
                    return i
        return None

    @staticmethod
    def _decision_key(view: dict[str, Any], moves: list[dict[str, Any]]) -> tuple:
        """一个决策点 = 局数 + 阶段 + 轮到谁 + 桌面牌 + 手牌 + 可选动作集合。

        这些任一变化都意味着局面变了，需要重新决策；
        全部相同则说明是同一次决策的重试，直接复用结果。
        """
        you = view.get("you") or {}
        table = view.get("table") or {}
        hand = tuple(sorted(c.get("id") for c in (you.get("hand") or [])))
        table_cards = tuple(sorted(c.get("id") for c in (table.get("cards") or [])))
        move_sig = tuple(sorted(
            (m.get("pass", False), tuple(sorted(m.get("card_ids") or ())))
            for m in moves
        ))
        return (
            view.get("round_no"),
            view.get("phase"),
            view.get("current_seat"),
            table_cards,
            hand,
            move_sig,
        )

    def _log_context(self, view: dict[str, Any], moves: list[dict[str, Any]],
                     prefix: str = "") -> None:
        """打出手牌与可选动作摘要，便于判断「过牌是被迫还是主动」。"""
        if not self.verbose:
            return
        hand = (view.get("you") or {}).get("hand") or []
        plays = [m for m in moves if not m.get("pass")]
        passes = [m for m in moves if m.get("pass")]
        tag = f"[{prefix}] " if prefix else ""
        self._log(
            f"#{self.hand_no} {tag}手牌{len(hand)}张[{_hand_str(hand)}] "
            f"可选{len(moves)}手（出{len(plays)}/过{len(passes)}）"
        )
        if plays:
            brief = "、".join(
                f"{m.get('kind_label', m.get('kind'))}·{m.get('size')}张"
                for m in plays[:8]
            )
            more = f" 等{len(plays)}种" if len(plays) > 8 else ""
            self._log(f"#{self.hand_no}   可出的牌型：{brief}{more}")

    def _last_reason(self) -> str:
        """从模型回复里抠出 reason 字段。"""
        if not self.last_reply:
            return ""
        for match in re.finditer(r"\{[^{}]*\}", self.last_reply, re.S):
            try:
                obj = json.loads(match.group(0))
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("reason"):
                return str(obj["reason"])[:80]
        return self.last_reply.strip()[:80]

    def _remember(self, view: dict[str, Any], act: dict[str, Any] | None,
                  reason: str = "") -> None:
        """记下自己这一手的意图，供后续轮次保持策略连贯。"""
        if act is None:
            return
        if act.get("action") == "play":
            cards = act.get("cards") or []
            desc = f"出 {len(cards)} 张（{cards}）"
        elif act.get("action") == "pass":
            desc = "过牌"
        elif act.get("action") == "return":
            desc = f"还贡 {act.get('card')}"
        else:
            return
        line = f"第{view.get('round_no', 1)}局 {desc}"
        if reason:
            line += f" —— {reason}"
        self.past_intents.append(line)
        if len(self.past_intents) > self.memory_limit:
            self.past_intents = self.past_intents[-self.memory_limit:]

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{self.name}] {msg}", file=sys.stderr, flush=True)
