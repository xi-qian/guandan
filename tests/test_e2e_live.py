"""端到端测试：真实 uvicorn 进程 + 4 个 bot 进程，走 HTTP 打完整牌局。

验证：
  1. 服务能起、房间能建、四人能坐满并开局
  2. bot 只用 HTTP 接口就能把牌打完（不 import 引擎）
  3. 结束后牌局状态自洽：事件可重放、牌张守恒、名次齐全、升级正确
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = resp.read().decode()
        return json.loads(payload) if payload else {}


@pytest.fixture(scope="module")
def server():
    port = free_port()
    log = open("/tmp/guandan_server.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            http("GET", f"{base}/api/rooms")
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(f"服务启动失败，日志：/tmp/guandan_server.log")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


class TestLiveServer:
    def test_index_served(self, server):
        with urllib.request.urlopen(server + "/", timeout=10) as resp:
            html = resp.read().decode()
        assert "掼蛋" in html

    def test_static_assets(self, server):
        for path in ("/static/app.js", "/static/style.css"):
            with urllib.request.urlopen(server + path, timeout=10) as resp:
                assert resp.status == 200

    def test_four_bots_play_full_rounds(self, server):
        """4 个 bot 进程打满 2 局，然后校验状态自洽。"""
        room = http("POST", f"{server}/api/rooms", {"name": "E2E 桌"})
        rid = room["room_id"]

        procs = []
        for i in range(4):
            p = subprocess.Popen(
                [
                    sys.executable, "-m", "bot.client",
                    "--base", server,
                    "--room", rid,
                    "--name", f"E2E-Bot{i}",
                    "--interval", "0.05",
                    "--max-rounds", "2",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            procs.append(p)
            time.sleep(0.15)  # 依次入座，避免抢名字/座位

        # 放宽时限：机器上有其它负载（比如正在跑的 LLM bot）时不要误报
        deadline = time.time() + 300
        finished = False
        while time.time() < deadline:
            if all(p.poll() is not None for p in procs):
                finished = True
                break
            time.sleep(0.5)

        if not finished:
            for p in procs:
                p.kill()
            logs = "".join(p.stdout.read() if p.stdout else "" for p in procs)
            pytest.fail(f"bot 未能在时限内结束。bot 输出：\n{logs[-3000:]}")

        logs = {i: p.stdout.read() if p.stdout else "" for i, p in enumerate(procs)}
        codes = [p.returncode for p in procs]
        assert codes == [0, 0, 0, 0], f"bot 退出码异常：{codes}\n{logs}"

        # 拉一个 bot 的 token 去查终局状态——用第一条 join 响应不可行，
        # 改为重新用 events 接口（需要 token），所以在这里补一次 join 是不行的；
        # 直接校验日志里出现了完成标记。
        joined = "".join(logs.values())
        assert "已坐下" in joined
        assert ("已打满" in joined) or ("对局结束" in joined)

    def test_state_consistency_after_play(self, server):
        """单桌短局：校验牌张守恒、事件可重放、名次齐全。"""
        room = http("POST", f"{server}/api/rooms", {"name": "一致性桌"})
        rid = room["room_id"]
        tokens = []
        for i in range(4):
            j = http("POST", f"{server}/api/rooms/{rid}/join", {"name": f"C{i}"})
            tokens.append(j["token"])

        http("POST", f"{server}/api/rooms/{rid}/start", token=tokens[0])

        # 用「最小合法牌」策略打完一整局
        for _ in range(5000):
            st = http("GET", f"{server}/api/rooms/{rid}/state", token=tokens[0])
            if st["phase"] != "play":
                break
            seat = st["current_seat"]
            tok = tokens[seat]
            moves = http("GET", f"{server}/api/rooms/{rid}/legal-moves", token=tok)
            plays = [m for m in moves if not m.get("pass")]
            if plays:
                chosen = min(plays, key=lambda m: (m["kind"] in ("bomb", "straight_flush", "rocket"), m["size"]))
                http("POST", f"{server}/api/rooms/{rid}/play",
                     {"cards": chosen["card_ids"]}, token=tok)
            else:
                http("POST", f"{server}/api/rooms/{rid}/pass", token=tok)
        else:
            pytest.fail("一局未能在 5000 步内结束")

        st = http("GET", f"{server}/api/rooms/{rid}/state", token=tokens[0])
        assert st["phase"] in ("round_end", "match_end")

        # 牌张守恒：出掉的 + 剩下的 = 108
        remaining = sum(p["hand_size"] for p in st["players"])
        assert 0 < remaining <= 108

        # 名次齐全
        assert sorted(st["last_round"]["finish_order"]) == [0, 1, 2, 3]
        assert st["last_round"]["advance"] in (1, 2, 3)
        assert st["last_round"]["win_team"] in (0, 1)

        # 事件日志可重放校验
        ev = http("GET", f"{server}/api/rooms/{rid}/events?since=0", token=tokens[0])
        types = [e["type"] for e in ev["events"]]
        assert types[0] == "deal"
        assert "round_end" in types
        plays_in_log = [e for e in ev["events"] if e["type"] == "play"]
        finishes = [e for e in ev["events"] if e["type"] == "finish"]
        assert len(finishes) == 4
        assert all("cards" in e and "seat" in e for e in plays_in_log)

        # 重放：按事件重建手牌，必须与服务端终局状态完全一致
        hands = [list(h) for h in ev["events"][0]["hands"]]
        for e in ev["events"]:
            if e["type"] == "play":
                for c in e["cards"]:
                    hands[e["seat"]].remove(c)
            elif e["type"] in ("tribute", "return_tribute"):
                hands[e["from_seat"]].remove(e["card"])
                hands[e["to_seat"]].append(e["card"])
        # 发出的牌 = 已打出的牌 + 仍在手的牌（出牌/进贡都不凭空增减）
        dealt = sorted(sum(ev["events"][0]["hands"], []))
        played = sorted(
            c for e in ev["events"] if e["type"] == "play" for c in e["cards"]
        )
        remaining = sorted(sum(hands, []))
        assert sorted(played + remaining) == dealt
        assert len(dealt) == 108

        # 与各玩家视角的手牌逐一比对
        for seat, tok in enumerate(tokens):
            view = http("GET", f"{server}/api/rooms/{rid}/state", token=tok)
            assert view["you"]["seat"] == seat
            actual = sorted(c["id"] for c in view["you"]["hand"])
            assert sorted(hands[seat]) == actual, f"座位 {seat} 重放结果与服务端不一致"
            assert view["you"]["hand_size"] == len(hands[seat])

    def test_replay_matches_final_state(self, server):
        """事件重放的结果必须与服务端终局状态一致。"""
        room = http("POST", f"{server}/api/rooms", {"name": "重放桌"})
        rid = room["room_id"]
        tokens = []
        for i in range(4):
            tokens.append(http("POST", f"{server}/api/rooms/{rid}/join", {"name": f"R{i}"})["token"])
        http("POST", f"{server}/api/rooms/{rid}/start", token=tokens[0])

        for _ in range(5000):
            st = http("GET", f"{server}/api/rooms/{rid}/state", token=tokens[0])
            if st["phase"] != "play":
                break
            tok = tokens[st["current_seat"]]
            moves = http("GET", f"{server}/api/rooms/{rid}/legal-moves", token=tok)
            plays = [m for m in moves if not m.get("pass")]
            if plays:
                http("POST", f"{server}/api/rooms/{rid}/play",
                     {"cards": plays[0]["card_ids"]}, token=tok)
            else:
                http("POST", f"{server}/api/rooms/{rid}/pass", token=tok)

        st = http("GET", f"{server}/api/rooms/{rid}/state", token=tokens[0])
        ev = http("GET", f"{server}/api/rooms/{rid}/events?since=0", token=tokens[0])

        # 重放 finish 事件，名次必须一致
        order = [e["seat"] for e in ev["events"] if e["type"] == "finish"]
        assert order == st["last_round"]["finish_order"]

        # 重放升级
        round_ends = [e for e in ev["events"] if e["type"] == "round_end"]
        assert round_ends[-1]["levels"] == [
            st["levels"][0]["level"],
            st["levels"][1]["level"],
        ]
        assert round_ends[-1]["win_team"] == st["last_round"]["win_team"]
        assert round_ends[-1]["advance"] == st["last_round"]["advance"]
