"""示例 AI 客户端：只通过 HTTP 与服务端交互，不依赖引擎代码。

用法:
    python -m bot.client --room <房间号> --name <玩家名> [--base http://127.0.0.1:8000]
    python -m bot.client --create --name 电脑A      # 自己开一桌
    python -m bot.client --room <房间号> --name 电脑B

策略是「最小合法牌」：能走就走最小的，走不了就过。
真实 AI 可以把 decide() 换成任意决策（包括调用大模型）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


class GuandanClient:
    def __init__(self, base: str, room_id: str | None = None, name: str = "bot"):
        self.base = base.rstrip("/")
        self.room_id = room_id
        self.name = name
        self.token: str | None = None

    # ------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = resp.read().decode()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                msg = json.loads(detail).get("detail", detail)
            except Exception:
                msg = detail
            raise RuntimeError(f"{method} {path} -> {e.code}: {msg}") from None

    # ------------------------------------------------------------ 大厅

    def create_room(self) -> str:
        r = self._request("POST", "/api/rooms", {"name": f"{self.name} 的牌桌"})
        self.room_id = r["room_id"]
        return self.room_id

    def join(self) -> int:
        assert self.room_id, "缺少房间号"
        r = self._request("POST", f"/api/rooms/{self.room_id}/join", {"name": self.name})
        self.token = r["token"]
        return r["seat"]

    def state(self) -> dict:
        return self._request("GET", f"/api/rooms/{self.room_id}/state")

    def legal_moves(self) -> list[dict]:
        return self._request("GET", f"/api/rooms/{self.room_id}/legal-moves")

    def play(self, cards: list[int]) -> dict:
        return self._request("POST", f"/api/rooms/{self.room_id}/play", {"cards": cards})

    def pass_turn(self) -> dict:
        return self._request("POST", f"/api/rooms/{self.room_id}/pass")

    def return_tribute(self, card: int) -> dict:
        return self._request(
            "POST", f"/api/rooms/{self.room_id}/return-tribute", {"card": card}
        )

    def start(self, after_round: int | None = None) -> dict:
        body = {} if after_round is None else {"after_round": after_round}
        return self._request("POST", f"/api/rooms/{self.room_id}/start", body)

    # ------------------------------------------------------------ 决策

    def decide(self, view: dict, moves: list[dict]) -> dict | None:
        """返回要执行的动作；None 表示暂不动作。"""
        phase = view.get("phase")

        if phase == "return_tribute" and view.get("tribute", {}) and view["tribute"].get("waiting_seat") == view["you"]["seat"]:
            options = view["tribute"].get("options") or []
            if options:
                # 还最小的牌
                best = min(options, key=lambda c: (c["rank"] not in ("BJ", "SJ"), c["id"]))
                return {"action": "return", "card": best["id"]}
            return None

        if phase == "round_end":
            return {"action": "start"}

        if phase == "match_end":
            return None

        if phase == "waiting":
            return {"action": "start"} if view.get("can_start") else None

        if phase != "play" or not view.get("your_turn"):
            return None

        if not moves:
            return None

        passes = [m for m in moves if m.get("pass")]
        plays = [m for m in moves if not m.get("pass")]

        if not plays:
            return {"action": "pass"} if passes else None

        # 策略：优先出非炸弹里最小的；只有当手牌很少或是唯一出路时才用炸弹
        normal = [m for m in plays if m["kind"] not in ("bomb", "straight_flush", "rocket")]
        hand_size = view["you"]["hand_size"]
        pool = normal if normal else plays

        # 手牌只剩一手时，直接走最大可能收尾
        if hand_size <= max((m["size"] for m in pool), default=0):
            choice = max(pool, key=lambda m: m["size"])
        else:
            # 最小的主点数：用牌张里最后一张的排序近似（服务端已给牌面）
            def score(m):
                cards = m.get("cards") or []
                ranks = [c.get("label", "") for c in cards]
                return (m["size"], ranks)

            choice = min(pool, key=score)

        # 队友已出完领先时保留炸弹：若只剩炸弹可走且手牌多，选择过
        if not normal and passes and hand_size > 6:
            return {"action": "pass"}

        return {"action": "play", "cards": choice["card_ids"]}


def _print_stats(bot: GuandanClient) -> None:
    """收尾时打印决策来源统计，便于判断 LLM 参与度。"""
    pts = getattr(bot, "decision_points", None)
    if pts is None:
        return
    print(
        f"[{bot.name}] 决策统计：决策点 {pts}  "
        f"LLM 调用 {getattr(bot, 'llm_calls', 0)}  "
        f"缓存命中 {getattr(bot, 'cache_hits', 0)}  "
        f"回落规则 {pts - getattr(bot, 'llm_calls', 0)}",
        flush=True,
    )


def run_loop(
    bot: GuandanClient,
    poll_interval: float = 0.4,
    max_steps: int = 100000,
    max_rounds: int | None = None,
) -> None:
    """轮询主循环。任何单次异常都不能打死进程——bot 崩了座位还占着，牌局会卡死。"""
    steps = 0
    done_rounds = 0
    steps_since_beat = [0]
    consecutive_errors = 0

    while steps < max_steps:
        steps += 1

        # ---- 拉状态
        try:
            view = bot.state()
            consecutive_errors = 0
        except RuntimeError as e:
            consecutive_errors += 1
            print(f"[{bot.name}] 状态获取失败(#{consecutive_errors}): {e}", flush=True)
            if consecutive_errors >= 20:
                print(f"[{bot.name}] 连续失败过多，退出", flush=True)
                return
            time.sleep(2)
            continue
        except Exception as e:
            consecutive_errors += 1
            print(f"[{bot.name}] 拉状态异常(#{consecutive_errors}): "
                  f"{type(e).__name__}: {e}", flush=True)
            if consecutive_errors >= 20:
                print(f"[{bot.name}] 连续失败过多，退出", flush=True)
                return
            time.sleep(2)
            continue

        phase = view.get("phase")
        if phase == "match_end":
            w = view.get("match_winner")
            print(f"[{bot.name}] 对局结束，获胜队伍={w}，本桌级数={view.get('levels')}", flush=True)
            _print_stats(bot)
            return

        if phase in ("round_end", "match_end"):
            done_rounds = view.get("round_no", done_rounds)
            if max_rounds is not None and done_rounds >= max_rounds:
                print(f"[{bot.name}] 已打满 {done_rounds} 局，退出", flush=True)
                return

        # 只在「确实需要行动」时才走决策逻辑。
        # 纯轮询（等别人出牌）不调 decide，从根本上杜绝轮询触发 LLM 计费。
        moves: list[dict] = []
        tribute = view.get("tribute") or {}
        my_seat = (view.get("you") or {}).get("seat")
        needs_action = (
            view.get("your_turn")
            or phase in ("round_end", "match_end", "waiting")
            or (phase == "return_tribute" and tribute.get("waiting_seat") == my_seat)
        )
        if not needs_action:
            steps_since_beat[0] += 1
            if steps_since_beat[0] >= 30:
                steps_since_beat[0] = 0
                print(f"[{bot.name}] 心跳 phase={phase} 等待中…", flush=True)
            time.sleep(poll_interval)
            continue

        # ---- 决策
        try:
            if view.get("your_turn"):
                moves = bot.legal_moves()
            action = bot.decide(view, moves)
        except Exception as e:
            consecutive_errors += 1
            print(f"[{bot.name}] 决策异常(#{consecutive_errors}): "
                  f"{type(e).__name__}: {e}", flush=True)
            if consecutive_errors >= 20:
                print(f"[{bot.name}] 连续失败过多，退出", flush=True)
                return
            time.sleep(1)
            continue

        if action is None:
            time.sleep(poll_interval)
            continue

        # ---- 执行动作
        try:
            if action["action"] == "play":
                bot.play(action["cards"])
                print(f"[{bot.name}] 出牌 {action['cards']}", flush=True)
            elif action["action"] == "pass":
                bot.pass_turn()
                print(f"[{bot.name}] 过牌", flush=True)
            elif action["action"] == "return":
                bot.return_tribute(action["card"])
                print(f"[{bot.name}] 还贡 {action['card']}", flush=True)
            elif action["action"] == "start":
                if max_rounds is not None and view.get("round_no", 0) >= max_rounds:
                    print(f"[{bot.name}] 已打满 {view.get('round_no')} 局，退出", flush=True)
                    return
                r = bot.start(after_round=view.get("round_no"))
                if r.get("already_started"):
                    print(f"[{bot.name}] 开局（别人已开，跳过）", flush=True)
                else:
                    print(f"[{bot.name}] 开局", flush=True)
            consecutive_errors = 0
        except RuntimeError as e:
            consecutive_errors += 1
            print(f"[{bot.name}] 动作失败(#{consecutive_errors}): {e}", flush=True)
            time.sleep(1)
        except Exception as e:
            consecutive_errors += 1
            print(f"[{bot.name}] 动作异常(#{consecutive_errors}): "
                  f"{type(e).__name__}: {e}", flush=True)
            time.sleep(1)

        time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="掼蛋示例 AI 客户端")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="掼蛋服务地址")
    ap.add_argument("--room", default=None, help="房间号；不给则新建")
    ap.add_argument("--name", default="bot", help="玩家名")
    ap.add_argument("--create", action="store_true", help="自己创建房间")
    ap.add_argument("--interval", type=float, default=0.3, help="轮询间隔（秒）")
    ap.add_argument("--max-rounds", type=int, default=None, help="打满 N 局后退出")

    g = ap.add_argument_group("大模型决策（可选，不填则用规则策略）")
    g.add_argument("--llm", action="store_true", help="用大模型决策")
    g.add_argument("--api-base", default=None,
                   help="OpenAI 兼容地址，如 https://api.openai.com/v1 "
                        "（也可用环境变量 OPENAI_BASE_URL）")
    g.add_argument("--api-key", default=None,
                   help="API key（也可用环境变量 OPENAI_API_KEY）")
    g.add_argument("--model", default=None, help="模型名，默认 gpt-4o-mini")
    g.add_argument("--temperature", type=float, default=0.2)
    g.add_argument("--max-tokens", type=int, default=None,
                   help="输出配额（含思考型模型的推理），默认 128000，"
                        "也可用环境变量 OPENAI_MAX_TOKENS")
    g.add_argument("--thinking", default=None,
                   help="思考模式：on=全开（默认，慢但更强）；off=关闭（约 12 倍提速）；"
                        "或给数字作为推理 token 预算。也可用环境变量 OPENAI_THINKING")
    args = ap.parse_args(argv)

    if args.llm:
        from .llm_bot import LLMBot
        from .llm_client import LLMClient, LLMError

        try:
            llm = LLMClient(
                api_base=args.api_base,
                api_key=args.api_key,
                model=args.model,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                thinking=args.thinking,
            )
        except LLMError as e:
            print(f"无法初始化大模型：{e}", file=sys.stderr, flush=True)
            return 2
        bot: GuandanClient = LLMBot(
            args.base, room_id=args.room, name=args.name, llm=llm
        )
        print(f"[{bot.name}] 大模型决策：{llm.model} @ {llm.api_base}", flush=True)
    else:
        bot = GuandanClient(args.base, room_id=args.room, name=args.name)

    if args.create or not bot.room_id:
        rid = bot.create_room()
        print(f"[{bot.name}] 创建房间 {rid}", flush=True)
    seat = bot.join()
    print(f"[{bot.name}] 已坐下 seat={seat} room={bot.room_id}", flush=True)
    run_loop(bot, poll_interval=args.interval, max_rounds=args.max_rounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
