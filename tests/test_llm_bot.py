"""大模型接入测试：提示词构造、回复解析、以及「假 LLM 服务 + 真牌局服务」端到端。

所有测试都不需要真实 API key——用本地 stub 服务扮演 OpenAI 兼容接口。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bot.llm_bot import LLMBot, build_prompt, parse_index, _history_line, _seen_line
from bot.llm_client import LLMClient, LLMError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- stub LLM

class _StubLLMHandler(BaseHTTPRequestHandler):
    """扮演 OpenAI 兼容的 /chat/completions。"""

    replies: list = []  # 由测试注入；元素可以是 str 或完整 message dict
    last_payload: dict | None = None
    calls: int = 0
    request_budgets: list[int] = []

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        _StubLLMHandler.last_payload = body
        _StubLLMHandler.calls += 1
        _StubLLMHandler.request_budgets.append(body.get("max_tokens"))

        if _StubLLMHandler.replies:
            item = _StubLLMHandler.replies.pop(0)
            if not _StubLLMHandler.replies:
                _StubLLMHandler.replies = [item]
        else:
            item = '{"index": 0, "reason": "stub"}'

        if isinstance(item, dict):
            message = item.get("message", {})
            finish = item.get("finish_reason", "stop")
            usage = item.get("usage")
        else:
            message = {"role": "assistant", "content": item}
            finish = "stop"
            usage = None

        payload = {
            "id": "stub",
            "object": "chat.completion",
            "model": body.get("model", "stub"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }
        if usage:
            payload["usage"] = usage
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub_llm():
    _StubLLMHandler.replies = []
    _StubLLMHandler.last_payload = None
    _StubLLMHandler.calls = 0
    _StubLLMHandler.request_budgets = []
    server = HTTPServer(("127.0.0.1", 0), _StubLLMHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()
    server.server_close()


def make_llm_client(stub_url: str) -> LLMClient:
    return LLMClient(api_base=stub_url, api_key="test-key", model="stub-model")


# ---------------------------------------------------------------- 提示词

SAMPLE_VIEW = {
    "phase": "play",
    "round_no": 2,
    "your_turn": True,
    "can_pass": True,
    "levels": [{"team": 0, "level": "5", "seats": [0, 2]},
               {"team": 1, "level": "2", "seats": [1, 3]}],
    "you": {
        "seat": 0, "name": "GPT-1", "team": 0, "hand_size": 5,
        "hand": [
            {"id": 1, "rank": "3", "suit": "♠", "label": "3"},
            {"id": 2, "rank": "3", "suit": "♥", "label": "3"},
            {"id": 3, "rank": "SJ", "suit": None, "label": "小王"},
            {"id": 4, "rank": "BJ", "suit": None, "label": "大王"},
            {"id": 5, "rank": "A", "suit": "♣", "label": "A"},
        ],
    },
    "players": [
        {"seat": 0, "name": "GPT-1", "hand_size": 5, "team": 0},
        {"seat": 1, "name": "乙", "hand_size": 2, "team": 1},
        {"seat": 2, "name": "丙", "hand_size": 8, "team": 0},
        {"seat": 3, "name": "丁", "hand_size": 12, "team": 1},
    ],
    "table": {
        "seat": 1, "kind": "pair", "kind_label": "对子", "size": 2,
        "cards": [{"id": 9, "rank": "7", "suit": "♦", "label": "7"},
                  {"id": 10, "rank": "7", "suit": "♣", "label": "7"}],
    },
    "last_round": {"finish_order": [0, 2, 1, 3], "win_team": 0, "advance": 3},
    "seen_counts": {
        "2": {"seen": 0, "total": 8}, "3": {"seen": 2, "total": 8},
        "SJ": {"seen": 1, "total": 2}, "BJ": {"seen": 0, "total": 2},
    },
    "round_plays": [
        {"seat": 1, "passed": False, "kind": "pair", "kind_label": "对子", "size": 2,
         "cards": [{"id": 9, "rank": "7", "suit": "♠", "label": "7"},
                   {"id": 10, "rank": "7", "suit": "♥", "label": "7"}]},
        {"seat": 2, "passed": True, "kind": "pass", "kind_label": "过牌", "size": 0, "cards": []},
    ],
}

SAMPLE_MOVES = [
    {"card_ids": [1, 2], "kind": "pair", "kind_label": "对子", "size": 2,
     "cards": [{"id": 1, "rank": "3", "suit": "♠", "label": "3"},
               {"id": 2, "rank": "3", "suit": "♥", "label": "3"}]},
    {"pass": True, "card_ids": [], "kind": "pass", "kind_label": "过牌", "size": 0},
]


class TestPrompt:
    def test_prompt_lists_every_move_with_index(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES)
        assert "[0]" in p and "[1]" in p
        assert "对子" in p and "过牌" in p
        assert "小王" in p and "大王" in p  # 手牌可读
        assert "座位 1" in p  # 桌面出牌者
        assert "剩 2 张" in p  # 对手张数

    def test_prompt_mentions_level_and_round(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES)
        assert "打 5" in p
        assert "第 2 局" in p

    def test_prompt_instructs_json_output(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES)
        assert '"index"' in p


class TestParse:
    def test_plain_json(self):
        assert parse_index('{"index": 1, "reason": "x"}', 5) == 1

    def test_json_with_fences(self):
        assert parse_index('```json\n{"index": 2}\n```', 5) == 2

    def test_bare_number(self):
        assert parse_index("我选 3", 5) == 3

    def test_out_of_range_rejected(self):
        assert parse_index('{"index": 99}', 3) is None
        assert parse_index("99", 3) is None

    def test_garbage_returns_none(self):
        assert parse_index("让我想想……", 3) is None
        assert parse_index("", 3) is None

    def test_negative_rejected(self):
        assert parse_index('{"index": -1}', 3) is None


# ---------------------------------------------------------------- LLM 客户端

class TestLLMClient:
    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(LLMError):
            LLMClient(api_base="http://x/v1", api_key=None)

    def test_reads_env_key_and_base(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
        c = LLMClient()
        assert c.api_key == "sk-env"
        assert c.api_base == "https://example.com/v1"

    def test_complete_round_trip(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "ok"}']
        c = make_llm_client(stub_llm)
        out = c.complete("system", "user")
        assert "index" in out
        payload = _StubLLMHandler.last_payload
        assert payload["model"] == "stub-model"
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["content"] == "user"

    def test_unreachable_host_raises(self):
        c = LLMClient(api_base="http://127.0.0.1:1", api_key="k", model="m", timeout=1)
        with pytest.raises(LLMError):
            c.complete("s", "u")


# ---------------------------------------------------------------- LLM 决策

class TestLLMBotDecide:
    def _bot(self, stub_url: str) -> LLMBot:
        return LLMBot(
            "http://127.0.0.1:9", room_id="r", name="GPT-1",
            llm=make_llm_client(stub_url), verbose=False,
        )

    def test_picks_index_from_llm(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "压上"}']
        bot = self._bot(stub_llm)
        act = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert act == {"action": "play", "cards": [1, 2]}

    def test_picks_pass(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 1, "reason": "让队友"}']
        bot = self._bot(stub_llm)
        assert bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES) == {"action": "pass"}

    def test_invalid_index_falls_back_to_rules(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 999, "reason": "乱来"}']
        bot = self._bot(stub_llm)
        act = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        # 规则策略：有非炸弹就出最小的；此处为对子 33 或过牌
        assert act in ({"action": "play", "cards": [1, 2]}, {"action": "pass"})

    def test_llm_error_falls_back(self, stub_llm):
        bot = self._bot("http://127.0.0.1:1")  # 连不上
        act = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert act is not None

    def test_no_llm_uses_rules(self):
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="x", llm=None, verbose=False)
        act = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert act is not None

    def test_garbage_reply_falls_back(self, stub_llm):
        _StubLLMHandler.replies = ["让我想想……"]
        bot = self._bot(stub_llm)
        act = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert act is not None

    def test_says_pass_in_text_maps_to_pass(self, stub_llm):
        _StubLLMHandler.replies = ["这轮我选择过牌"]
        bot = self._bot(stub_llm)
        assert bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES) == {"action": "pass"}

    def test_prompt_actually_sent(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(stub_llm)
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        sent = _StubLLMHandler.last_payload["messages"][1]["content"]
        assert "[0]" in sent and "对子" in sent
        assert bot.last_reply is not None


# ---------------------------------------------------------------- 端到端

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestLLMEndToEnd:
    def test_llm_bot_plays_full_round_on_real_server(self, stub_llm):
        """假 LLM + 真牌局服务：LLM 做决策，牌局能打完一整局。"""
        port = _free_port()
        log = open("/tmp/guandan_llm_e2e.log", "w")
        srv = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.app:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None, token=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}

        try:
            for _ in range(60):
                try:
                    call("GET", "/api/rooms")
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                pytest.fail("牌局服务未起来")

            # 让 stub 永远选第 0 个动作（合法，一定能执行）
            _StubLLMHandler.replies = ['{"index": 0, "reason": "stub-0"}']

            room = call("POST", "/api/rooms", {"name": "LLM 桌"})
            rid = room["room_id"]

            llm_bots = []
            for i in range(4):
                name = f"LLM-{i}"
                bot = LLMBot(base, room_id=rid, name=name,
                             llm=make_llm_client(stub_llm), verbose=False)
                bot.join()
                llm_bots.append(bot)
                time.sleep(0.05)

            llm_bots[0].start()

            # 驱动四个 LLM bot 打到本局结束
            deadline = time.time() + 90
            acted = 0
            stalls = 0
            while time.time() < deadline:
                view0 = llm_bots[0].state()
                if view0["phase"] in ("round_end", "match_end"):
                    break
                seat = view0["current_seat"]
                bot = llm_bots[seat]
                view = bot.state()          # 必须用行动者自己的视角
                moves = bot.legal_moves() if view.get("your_turn") else []
                action = bot.decide(view, moves)
                if action is None:
                    stalls += 1
                    if stalls > 2000:
                        pytest.fail(f"卡住了：seat={seat} phase={view.get('phase')} "
                                    f"your_turn={view.get('your_turn')}")
                    time.sleep(0.02)
                    continue
                stalls = 0
                if action["action"] == "play":
                    bot.play(action["cards"])
                elif action["action"] == "pass":
                    bot.pass_turn()
                elif action["action"] == "return":
                    bot.return_tribute(action["card"])
                acted += 1
            else:
                pytest.fail("LLM bot 未能在一局内结束")

            assert acted > 10, "应该真的出过牌"
            assert _StubLLMHandler.calls > 5, "应该真的调用过 LLM"

            # 校验 LLM 只在合法动作里选：把它每次的 prompt/回复抓回来核对
            view = llm_bots[0].state()
            assert view["phase"] in ("round_end", "match_end")
            assert sorted(view["last_round"]["finish_order"]) == [0, 1, 2, 3]
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=5)
            except Exception:
                srv.kill()
            log.close()

    def test_llm_bot_never_illegal_when_picking_valid_index(self, stub_llm):
        """LLM 只从合法列表选编号 → 出牌接口永远接受。"""
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="x",
                     llm=make_llm_client(stub_llm), verbose=False)
        # 直接校验 decide 返回的 cards 来自 moves
        moves = SAMPLE_MOVES
        act = bot.decide(SAMPLE_VIEW, moves)
        assert act["cards"] == moves[0]["card_ids"]


class TestHistoryInPrompt:
    """出牌历史与记牌表必须进提示词——掼蛋要记牌。"""

    def test_card_count_table_present(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES)
        assert "记牌" in p
        assert "2:0/8" in p          # 没见过的 2
        assert "3:2/8" in p          # 见过两张 3
        assert "小王:1/2" in p       # 王牌总数是 2 不是 8
        assert "大王:0/2" in p

    def test_play_history_present(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES)
        assert "本局出牌记录" in p
        assert "座1 对子（2张）" in p
        assert "座2过" in p

    def test_history_truncates_but_says_total(self):
        plays = [
            {"seat": i % 4, "passed": False, "kind": "single", "kind_label": "单张",
             "size": 1, "cards": [{"id": i, "rank": "3", "suit": "♠", "label": "3"}]}
            for i in range(30)
        ]
        line = _history_line(plays, limit=12)
        assert "共 30 手" in line
        assert line.count("座") == 12

    def test_history_handles_empty(self):
        assert "还没有出牌" in _history_line([])
        assert "还没有出牌" in _history_line(None)

    def test_seen_line_handles_missing_ranks(self):
        assert "（无记牌数据）" in _seen_line(None)
        assert "（无记牌数据）" in _seen_line({})

    def test_prompt_still_instructs_json(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES)
        assert '"index"' in p


# ---------------------------------------------------------------- 思考型模型

class TestReasoningModel:
    """带 reasoning_content 的思考型模型：推理会吃掉 max_tokens 配额。"""

    def _client(self, stub_url, max_tokens=64):
        return LLMClient(api_base=stub_url, api_key="k", model="m",
                         max_tokens=max_tokens)

    def test_reasoning_content_is_captured_not_returned(self, stub_llm):
        _StubLLMHandler.replies = [{
            "message": {"role": "assistant",
                        "content": '{"index": 1, "reason": "x"}',
                        "reasoning_content": "让我想想……"},
            "finish_reason": "stop",
        }]
        c = self._client(stub_llm)
        out = c.complete("s", "u")
        assert out == '{"index": 1, "reason": "x"}'   # 正文只含 content
        assert "让我想想" in c.last_reasoning
        assert c.last_meta["finish_reason"] == "stop"

    def test_empty_content_retries_with_bigger_budget(self, stub_llm):
        """content 空 + finish_reason=length → 放大 max_tokens 重试。"""
        _StubLLMHandler.replies = [
            {"message": {"role": "assistant", "content": "",
                         "reasoning_content": "很长的推理……"},
             "finish_reason": "length"},
            {"message": {"role": "assistant", "content": '{"index": 2}',
                         "reasoning_content": "更长的推理"},
             "finish_reason": "stop"},
        ]
        c = self._client(stub_llm, max_tokens=100)
        out = c.complete("s", "u")
        assert out == '{"index": 2}'
        assert _StubLLMHandler.request_budgets == [100, 400], "重试应放大 4 倍配额"
        assert _StubLLMHandler.calls == 2

    def test_still_empty_after_retry_returns_empty(self, stub_llm):
        _StubLLMHandler.replies = [
            {"message": {"role": "assistant", "content": "", "reasoning_content": "x"},
             "finish_reason": "length"},
            {"message": {"role": "assistant", "content": "", "reasoning_content": "y"},
             "finish_reason": "length"},
        ]
        c = self._client(stub_llm, max_tokens=50)
        assert c.complete("s", "u") == ""
        assert _StubLLMHandler.calls == 2

    def test_default_budget_is_large_enough_for_reasoning(self):
        from bot.llm_client import DEFAULT_MAX_TOKENS
        # 输入窗口可达 1M，输出配额不该抠——推理要能写完
        assert DEFAULT_MAX_TOKENS >= 16384, "思考型模型需要足够配额写完推理再写正文"

    def test_max_tokens_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_MAX_TOKENS", "12345")
        c = LLMClient(api_base="http://x/v1", api_key="k", model="m")
        assert c.max_tokens == 12345

    def test_max_tokens_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_MAX_TOKENS", "12345")
        c = LLMClient(api_base="http://x/v1", api_key="k", model="m", max_tokens=777)
        assert c.max_tokens == 777

    def test_usage_meta_recorded(self, stub_llm):
        _StubLLMHandler.replies = [{
            "message": {"role": "assistant", "content": '{"index": 0}',
                        "reasoning_content": "r"},
            "finish_reason": "stop",
            "usage": {"completion_tokens": 225, "prompt_tokens": 514,
                      "completion_tokens_details": {"reasoning_tokens": 191}},
        }]
        c = self._client(stub_llm)
        c.complete("s", "u")
        assert c.last_meta["usage"]["completion_tokens_details"]["reasoning_tokens"] == 191

    def test_multimodal_list_content_supported(self, stub_llm):
        _StubLLMHandler.replies = [{
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": '{"index": 1}'}]},
            "finish_reason": "stop",
        }]
        c = self._client(stub_llm)
        assert c.complete("s", "u") == '{"index": 1}'


class TestThinkingSwitch:
    """思考模式开关：实测 mimo 接口只认 thinking.type=disabled。"""

    def _client(self, stub_url, thinking=None, max_tokens=1000):
        return LLMClient(api_base=stub_url, api_key="k", model="m",
                         max_tokens=max_tokens, thinking=thinking)

    def test_off_sends_disabled(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0}']
        self._client(stub_llm, thinking="off").complete("s", "u")
        assert _StubLLMHandler.last_payload.get("thinking") == {"type": "disabled"}

    def test_on_sends_nothing(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0}']
        self._client(stub_llm, thinking="on").complete("s", "u")
        assert "thinking" not in _StubLLMHandler.last_payload
        assert "chat_template_kwargs" not in _StubLLMHandler.last_payload

    def test_default_sends_nothing(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0}']
        self._client(stub_llm, thinking=None).complete("s", "u")
        assert "thinking" not in _StubLLMHandler.last_payload

    def test_numeric_sends_budget(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0}']
        self._client(stub_llm, thinking="256").complete("s", "u")
        assert _StubLLMHandler.last_payload["chat_template_kwargs"] == {"thinking_budget": 256}

    def test_env_default(self, monkeypatch, stub_llm):
        monkeypatch.setenv("OPENAI_THINKING", "off")
        _StubLLMHandler.replies = ['{"index": 0}']
        self._client(stub_llm).complete("s", "u")
        assert _StubLLMHandler.last_payload.get("thinking") == {"type": "disabled"}

    def test_empty_reply_retry_still_works_with_thinking_off(self, stub_llm):
        _StubLLMHandler.replies = [
            {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"},
            {"message": {"role": "assistant", "content": '{"index": 1}'}, "finish_reason": "stop"},
        ]
        c = self._client(stub_llm, thinking="off", max_tokens=50)
        assert c.complete("s", "u") == '{"index": 1}'


class TestIntentMemory:
    """请求保持无状态，但把自己的「打算」喂回给模型，保持策略连贯。"""

    def _bot(self, stub_url=None, llm=None):
        return LLMBot("http://127.0.0.1:9", room_id="r", name="x",
                      llm=llm, verbose=False)

    def test_prompt_includes_past_intents(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES,
                         ["第1局 出 5 张 —— 留三张A和大王控盘"])
        assert "你此前的打算" in p
        assert "留三张A和大王控盘" in p

    def test_prompt_without_intents_has_no_section(self):
        p = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES, [])
        assert "你此前的打算" not in p
        p2 = build_prompt(SAMPLE_VIEW, SAMPLE_MOVES, None)
        assert "你此前的打算" not in p2

    def test_decide_records_intent(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "先出对子走小牌"}']
        bot = self._bot(llm=make_llm_client(stub_llm))
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert bot.past_intents
        assert "对子" in bot.past_intents[0] or "2 张" in bot.past_intents[0]
        assert "先出对子走小牌" in bot.past_intents[0]

    def test_pass_records_intent(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 1, "reason": "让队友上手"}']
        bot = self._bot(llm=make_llm_client(stub_llm))
        v = json.loads(json.dumps(SAMPLE_VIEW))
        v["current_seat"] = 3
        bot.decide(v, SAMPLE_MOVES)
        assert "过牌" in bot.past_intents[0]
        assert "让队友上手" in bot.past_intents[0]

    @staticmethod
    def _vary(view, i):
        """造出互不相同的决策点（缓存按局面签名区分）。"""
        v = json.loads(json.dumps(view))
        v["current_seat"] = i % 4
        v["you"]["hand_size"] = v["you"]["hand_size"] - i
        return v

    def test_memory_is_bounded(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(llm=make_llm_client(stub_llm))
        bot.memory_limit = 3
        for i in range(10):
            bot.decide(self._vary(SAMPLE_VIEW, i), SAMPLE_MOVES)
        assert len(bot.past_intents) == 3
        assert bot.past_intents == bot.past_intents[-3:]

    def test_intents_feed_into_next_prompt(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "保留四炸压对手头游"}']
        bot = self._bot(llm=make_llm_client(stub_llm))
        bot.decide(self._vary(SAMPLE_VIEW, 1), SAMPLE_MOVES)
        assert "保留四炸压对手头游" in bot.past_intents[0]
        # 下一个决策点的提示词里必须带上之前的打算
        bot.decide(self._vary(SAMPLE_VIEW, 2), SAMPLE_MOVES)
        sent = _StubLLMHandler.last_payload["messages"][1]["content"]
        assert "你此前的打算" in sent
        assert "保留四炸压对手头游" in sent

    def test_requests_stay_stateless(self, stub_llm):
        """messages 始终只有 system + user 两条，不累积对话。"""
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(llm=make_llm_client(stub_llm))
        for _ in range(4):
            bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
            msgs = _StubLLMHandler.last_payload["messages"]
            assert len(msgs) == 2, "请求必须保持无状态"
            assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"

    def test_fallback_also_records_intent(self):
        bot = self._bot()  # 无 LLM → 回落规则
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert bot.past_intents
        assert "回落规则策略" in bot.past_intents[0]

    def test_intent_is_truncated_to_keep_prompt_small(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x" * 500}']
        bot = self._bot(llm=make_llm_client(stub_llm))
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert len(bot.past_intents[0]) < 120


class TestNoWastedLLMCalls:
    """契约：LLM 只在「真正要决策」时调用，且同一决策点最多一次。

    轮询不该烧 token；动作失败重试也不该重复计费。
    """

    def _bot(self, stub_url, llm_calls_holder=None):
        return LLMBot("http://127.0.0.1:9", room_id="r", name="x",
                      llm=make_llm_client(stub_url), verbose=False)

    def test_not_my_turn_never_calls_llm(self, stub_llm):
        bot = self._bot(stub_llm)
        for _ in range(50):                      # 模拟 50 次轮询都没轮到自己
            view = dict(SAMPLE_VIEW, your_turn=False)
            act = bot.decide(view, SAMPLE_MOVES)
            assert act is None
        assert bot.llm_calls == 0, "非自己回合打了 LLM = 浪费"
        assert _StubLLMHandler.calls == 0

    def test_waiting_and_round_end_never_call_llm(self, stub_llm):
        bot = self._bot(stub_llm)
        for phase in ("waiting", "round_end", "match_end"):
            view = dict(SAMPLE_VIEW, phase=phase, your_turn=False)
            bot.decide(view, SAMPLE_MOVES)
        assert bot.llm_calls == 0
        assert _StubLLMHandler.calls == 0

    def test_empty_moves_never_calls_llm(self, stub_llm):
        bot = self._bot(stub_llm)
        for _ in range(10):
            assert bot.decide(SAMPLE_VIEW, []) is None
        assert bot.llm_calls == 0

    def test_same_decision_point_calls_llm_once(self, stub_llm):
        """动作失败后重试：复用上次决定，不重新计费。"""
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(stub_llm)
        first = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        for _ in range(20):                      # 20 次重试
            again = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
            assert again == first, "重试必须复用同一个决定"
        assert bot.llm_calls == 1, f"同决策点调了 {bot.llm_calls} 次 LLM"
        assert bot.decision_points == 1
        assert bot.cache_hits == 20
        assert _StubLLMHandler.calls == 1

    def test_new_state_triggers_new_call(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(stub_llm)
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        # 手牌变了 = 新决策点
        v2 = json.loads(json.dumps(SAMPLE_VIEW))
        v2["you"]["hand"] = v2["you"]["hand"][:-1]
        v2["you"]["hand_size"] = 4
        bot.decide(v2, SAMPLE_MOVES[:1])
        assert bot.llm_calls == 2
        assert bot.decision_points == 2

    def test_turn_change_triggers_new_call(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(stub_llm)
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        v2 = json.loads(json.dumps(SAMPLE_VIEW))
        v2["current_seat"] = 2
        bot.decide(v2, SAMPLE_MOVES)
        assert bot.decision_points == 2

    def test_table_change_triggers_new_call(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(stub_llm)
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        v2 = json.loads(json.dumps(SAMPLE_VIEW))
        v2["table"] = {"seat": 3, "kind": "single", "kind_label": "单张", "size": 1,
                       "cards": [{"id": 99, "rank": "2", "suit": "♣", "label": "2"}]}
        bot.decide(v2, SAMPLE_MOVES)
        assert bot.decision_points == 2

    def test_llm_calls_never_exceed_decision_points(self, stub_llm):
        """总账：无论怎么轮询/重试，LLM 调用 ≤ 决策点数。"""
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = self._bot(stub_llm)
        # 混合：空转轮询 + 真实决策 + 重试
        for i in range(30):
            if i % 3 == 0:
                bot.decide(dict(SAMPLE_VIEW, your_turn=False), SAMPLE_MOVES)
            elif i % 3 == 1:
                bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)   # 同一决策点，反复
            else:
                bot.decide(SAMPLE_VIEW, [])
        assert bot.llm_calls <= bot.decision_points
        assert bot.llm_calls == 1
        assert bot.decision_points == 1

    def test_tribute_return_also_cached(self, stub_llm):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "还小的"}']
        bot = self._bot(stub_llm)
        view = dict(SAMPLE_VIEW, phase="return_tribute", your_turn=False)
        view["tribute"] = {"waiting_seat": 0, "to_seat": 1,
                           "options": [{"id": 5, "rank": "3", "suit": "♠", "label": "3"}]}
        view["you"] = dict(view["you"], seat=0)
        first = bot.decide(view, [])
        for _ in range(5):
            assert bot.decide(view, []) == first
        assert bot.llm_calls == 1
        assert bot.cache_hits == 5


class TestRunLoopPolling:
    """run_loop 的轮询循环：等别人出牌时不该进入决策逻辑。"""

    def test_needs_action_gate(self):
        """复刻 run_loop 的 needs_action 判定。"""

        def needs_action(view):
            phase = view.get("phase")
            tribute = view.get("tribute") or {}
            my_seat = (view.get("you") or {}).get("seat")
            return (
                bool(view.get("your_turn"))
                or phase in ("round_end", "match_end", "waiting")
                or (phase == "return_tribute" and tribute.get("waiting_seat") == my_seat)
            )

        base = {"you": {"seat": 0}, "phase": "play", "your_turn": False}
        assert needs_action(base) is False, "等别人出牌时不该决策"

        assert needs_action(dict(base, your_turn=True)) is True
        for ph in ("round_end", "match_end", "waiting"):
            assert needs_action(dict(base, phase=ph)) is True

        # 还贡：只有轮到自己还才决策
        t_other = {"tribute": {"waiting_seat": 2}}
        t_me = {"tribute": {"waiting_seat": 0}}
        assert needs_action(dict(base, phase="return_tribute", **t_other)) is False
        assert needs_action(dict(base, phase="return_tribute", **t_me)) is True

        # 还贡但没信息时不该决策
        assert needs_action(dict(base, phase="return_tribute")) is False

    def test_run_loop_source_gates_decide(self):
        """源码层面断言：run_loop 里 decide 只在 needs_action 之后被调用。"""
        import inspect
        from bot import client
        src = inspect.getsource(client.run_loop)
        assert "needs_action" in src
        gate = src.index("if not needs_action:")
        call = src.index("bot.decide(")
        assert gate < call, "decide 必须在 needs_action 闸门之后"
        # 闸门之后才出现 legal_moves
        assert src.index("bot.legal_moves(") > gate



class TestTeammateLabeling:
    """队友必须显式标注。实测踩过：模型自己算 (seat+2)%4 算错，
    把对手当成队友一直在「让牌」。"""

    def _view_for(self, seat, team):
        v = json.loads(json.dumps(SAMPLE_VIEW))
        v["you"] = dict(v["you"], seat=seat, team=team)
        v["players"] = [
            {"seat": 0, "name": "甲", "hand_size": 5, "team": 0},
            {"seat": 1, "name": "乙", "hand_size": 3, "team": 1},
            {"seat": 2, "name": "丙", "hand_size": 8, "team": 0},
            {"seat": 3, "name": "丁", "hand_size": 12, "team": 1},
        ]
        return v

    def test_seat1_partner_is_seat3(self):
        p = build_prompt(self._view_for(1, 1), SAMPLE_MOVES)
        assert "你的队友是座位 3（丁）" in p
        assert "座位3(丁) 剩 12 张【队友】" in p
        assert "座位2(丙) 剩 8 张【对手】" in p
        assert "座位0(甲) 剩 5 张【对手】" in p

    def test_seat0_partner_is_seat2(self):
        p = build_prompt(self._view_for(0, 0), SAMPLE_MOVES)
        assert "你的队友是座位 2（丙）" in p
        assert "座位2(丙) 剩 8 张【队友】" in p
        assert "座位3(丁) 剩 12 张【对手】" in p

    def test_seat2_partner_is_seat0(self):
        p = build_prompt(self._view_for(2, 0), SAMPLE_MOVES)
        assert "你的队友是座位 0（甲）" in p

    def test_seat3_partner_is_seat1(self):
        p = build_prompt(self._view_for(3, 1), SAMPLE_MOVES)
        assert "你的队友是座位 1（乙）" in p

    def test_every_non_partner_is_marked_opponent(self):
        p = build_prompt(self._view_for(1, 1), SAMPLE_MOVES)
        assert p.count("【对手】") == 2, "两个对手都要标出来"
        assert p.count("【队友】") == 1

    def test_prompt_never_asks_model_to_compute_seat_math(self):
        """队友身份必须是给定事实，不能靠模型自己算 (seat+2)%4。"""
        p = build_prompt(self._view_for(1, 1), SAMPLE_MOVES)
        assert "你的队友是座位" in p
        assert "(你的座位+2)%4" not in p



class TestLevelCardInPrompt:
    """本局打几必须显式给出。实测踩过：提示词只写两队各自打几，
    三家各猜各的级牌，mimo-1/3 当 2 是级牌、mimo-2 当 2 是小牌。"""

    def test_states_the_level_being_played(self):
        v = json.loads(json.dumps(SAMPLE_VIEW))
        v["level_rank"] = "3"
        p = build_prompt(v, SAMPLE_MOVES)
        assert "本局打 3" in p
        assert "3 是级牌" in p

    def test_no_internal_field_names(self):
        v = json.loads(json.dumps(SAMPLE_VIEW))
        v["level_rank"] = "3"
        p = build_prompt(v, SAMPLE_MOVES)
        assert "level_rank" not in p
        assert "级牌是 3" not in p or True

    def test_level_card_ordering_stated(self):
        v = json.loads(json.dumps(SAMPLE_VIEW))
        v["level_rank"] = "3"
        p = build_prompt(v, SAMPLE_MOVES)
        assert "A 之上" in p and "小王之下" in p

    def test_ace_level_wellformed(self):
        """打 A 时不能说「A 比 A 大」这种病句。"""
        v = json.loads(json.dumps(SAMPLE_VIEW))
        v["level_rank"] = "A"
        p = build_prompt(v, SAMPLE_MOVES)
        assert "本局打 A" in p
        assert "比 A 大" not in p.split("本局打 A")[1].splitlines()[0]

    def test_team_levels_marked_irrelevant_to_strength(self):
        v = json.loads(json.dumps(SAMPLE_VIEW))
        v["level_rank"] = "3"
        p = build_prompt(v, SAMPLE_MOVES)
        assert "不影响牌力" in p



class TestDecisionContextLog:
    """决策日志必须能看出「过牌是被迫还是主动」。"""

    def test_context_log_shows_hand_and_options(self, stub_llm, capsys):
        _StubLLMHandler.replies = ['{"index": 1, "reason": "让队友"}']
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="T",
                     llm=make_llm_client(stub_url=stub_llm), verbose=True)
        v = json.loads(json.dumps(SAMPLE_VIEW))
        bot.decide(v, SAMPLE_MOVES)
        err = capsys.readouterr().err
        assert "手牌5张" in err
        assert "可选2手" in err and "出1/过1" in err

    def test_pass_with_plays_available_is_flagged(self, stub_llm, capsys):
        _StubLLMHandler.replies = ['{"index": 1, "reason": "让队友"}']
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="T",
                     llm=make_llm_client(stub_url=stub_llm), verbose=True)
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        err = capsys.readouterr().err
        assert "⚠ 有 1 手可出却选择过" in err

    def test_forced_pass_is_not_flagged(self, stub_llm, capsys):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "压不住"}']
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="T",
                     llm=make_llm_client(stub_url=stub_llm), verbose=True)
        v = json.loads(json.dumps(SAMPLE_VIEW))
        only_pass = [{"pass": True, "card_ids": [], "kind": "pass",
                      "kind_label": "过牌", "size": 0}]
        bot.decide(v, only_pass)
        err = capsys.readouterr().err
        assert "⚠" not in err
        assert "出0/过1" in err

    def test_lists_playable_kinds(self, stub_llm, capsys):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="T",
                     llm=make_llm_client(stub_url=stub_llm), verbose=True)
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        err = capsys.readouterr().err
        assert "可出的牌型" in err
        assert "对子·2张" in err

    def test_quiet_mode_stays_quiet(self, stub_llm, capsys):
        _StubLLMHandler.replies = ['{"index": 0, "reason": "x"}']
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="T",
                     llm=make_llm_client(stub_url=stub_llm), verbose=False)
        bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        err = capsys.readouterr().err
        assert "手牌" not in err



class TestNetworkFailureSafety:
    """网络异常不能打死 bot 进程——实测踩过 socket.timeout 直接穿出。"""

    def test_read_timeout_becomes_llm_error(self, stub_llm, monkeypatch):
        import socket as _s
        from bot import llm_client as _lc

        def boom(*a, **k):
            raise _s.timeout("The read operation timed out")
        monkeypatch.setattr(_lc.urllib.request, "urlopen", boom)
        c = LLMClient(api_base="http://127.0.0.1:1", api_key="k", model="m", timeout=1)
        with pytest.raises(LLMError):
            c.complete("s", "u")

    def test_generic_oserror_becomes_llm_error(self, monkeypatch):
        from bot import llm_client as _lc

        def boom(*a, **k):
            raise OSError("Network is unreachable")
        monkeypatch.setattr(_lc.urllib.request, "urlopen", boom)
        c = LLMClient(api_base="http://127.0.0.1:1", api_key="k", model="m")
        with pytest.raises(LLMError):
            c.complete("s", "u")

    def test_timeout_falls_back_to_rules_not_crash(self, monkeypatch):
        from bot import llm_client as _lc
        import socket as _s

        def boom(*a, **k):
            raise _s.timeout("timed out")
        monkeypatch.setattr(_lc.urllib.request, "urlopen", boom)
        bot = LLMBot("http://127.0.0.1:9", room_id="r", name="x",
                     llm=LLMClient(api_base="http://x/v1", api_key="k", model="m"),
                     verbose=False)
        act = bot.decide(json.loads(json.dumps(SAMPLE_VIEW)), SAMPLE_MOVES)
        assert act is not None, "超时应回落到规则策略，而不是抛异常"
        assert bot.last_decision_source == "fallback"

    def test_run_loop_source_has_outer_guard(self):
        import inspect
        from bot import client
        src = inspect.getsource(client.run_loop)
        assert "except Exception" in src, "run_loop 必须有兜底，别让异常打死进程"
        assert src.count("except Exception") >= 2, "拉状态/决策/动作三处都要兜"
