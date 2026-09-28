# 掼蛋（4 人两副牌 · 网页 + AI 可接入）

一个可自托管的掼蛋对局服务：**后端发牌与判牌、前端网页出牌、AI 通过 HTTP 以玩家身份入座**。
无用户系统，进入房间时自报名字即可；同一桌最多 4 人，可多桌并行、连局打级到过 A。

规则严格按 [guandanguize.com](https://guandanguize.com/)：
两副牌 108 张、每人 27 张、对家为队友；单张/对子/三同张/三带二/顺子/三连对/钢板 +
炸弹/同花顺/天王炸；级牌抬到 A 之上小王之下（顺子等连张里按自然点数）；
同花顺大于五炸小于六炸；双上升 3 级、一三游升 2 级、一四游升 1 级；
末游进贡（除大王外最大张）、双下双贡、双大王抗贡、还贡不大于 10 且非级牌；
打 A 需「头游 + 队友非末游」才过 A 获胜。

## 快速开始

```bash
pip install -r requirements.txt
python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

浏览器打开 <http://127.0.0.1:8000> ，填名字、建房/进房，凑满 4 人点「开始」。

### 用示例 AI 凑局

```bash
# 终端 1：开一桌
python3 -m bot.client --create --name 电脑A

# 终端 2/3/4：加入同一桌（房间号见终端 1 输出）
python3 -m bot.client --room <房间号> --name 电脑B
python3 -m bot.client --room <房间号> --name 电脑C
python3 -m bot.client --room <房间号> --name 电脑D

# 你也可以自己开一个网页加入，和 3 个电脑打
```

`bot/client.py` **不 import 引擎**，只走 HTTP——它就是外部 AI 的最小可运行样例。
把 `GuandanClient.decide()` 换成你自己的决策（规则搜索、启发式、或调用大模型）即可。

## 接大模型：一个地址 + 一个 key

加 `--llm` 就切换到大模型决策，只要服务实现 OpenAI 兼容的
`POST {api_base}/chat/completions`：

```bash
# OpenAI
export OPENAI_API_KEY=sk-...
python3 -m bot.client --llm --name GPT-1 --api-base https://api.openai.com/v1 --model gpt-4o-mini

# DeepSeek / Moonshot / 智谱 / Ollama / vLLM / 任意兼容网关
python3 -m bot.client --llm --name GPT-2 --room <房间号> \
  --api-base https://api.deepseek.com/v1 --api-key sk-xxx --model deepseek-chat

# 本地 Ollama
python3 -m bot.client --llm --name 本地 --api-base http://127.0.0.1:11434/v1 \
  --api-key ollama --model qwen2.5:14b
```

参数也可以用环境变量：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` /
`OPENAI_MAX_TOKENS` / `OPENAI_THINKING`。

### 思考型模型（带 reasoning_content）

mimo / DeepSeek-R1 / QwQ 这类模型会把「推理」写进 `reasoning_content`，
而**推理也吃 `max_tokens` 配额**。配额太小会出现「推理完没剩 token 写答案」
→ `content` 为空、`finish_reason=length`。本项目的处理：

| 问题 | 处理 |
| --- | --- |
| 配额不够导致空回复 | 默认 `max_tokens=128000`；空回复或截断时自动放大配额重试一次 |
| 推理太慢 | `--thinking off` 关掉推理（实测 mimo 约 **12 倍提速**） |
| 响应结构不同 | 兼容 `content` 为字符串或列表、忽略 `reasoning_content`、记录 `finish_reason` |

```bash
# 快速局：关掉思考
python3 -m bot.client --llm --thinking off --name 快手 ...

# 认真局：开满思考（慢但更强）
python3 -m bot.client --llm --thinking on  --name 认真 ...
```

实测 mimo-v2.6-flash（输入约 2300 字的牌局提示词）：

| 配置 | 单步延迟 | 推理 token | 空回复 |
| --- | --- | --- | --- |
| `max_tokens=400`（旧行为） | 9.5s | 被截断 | **53%** |
| `max_tokens=128k` 思考全开 | 17–31s | 557–3897 字 | 0% |
| `max_tokens=128k` `--thinking off` | **2.5s** | 0 | 0% |

注：该接口实测只认 `thinking={"type":"disabled"}`；
`reasoning_effort` 会被忽略，`chat_template_kwargs.thinking_budget` 仅部分生效。

**模型只做「从合法动作里挑一个」**：提示词里把服务端算好的合法出牌编号化
（`[0] 过牌`、`[1] 对子（2张）：3♠3♥` …），模型回一个 `{"index": 3, "reason": "..."}`。
所以它永远不会出非法牌；模型回复越界/胡说/超时/报错时自动回落到内置规则策略，
牌局不会卡住。还贡也是同样的编号选择。

相关代码：

| 文件 | 作用 |
| --- | --- |
| `bot/llm_client.py` | OpenAI 兼容 HTTP 客户端（纯 urllib，无第三方依赖），兼容思考型模型 |
| `bot/llm_bot.py` | 提示词构造（含记牌表与出牌历史）· 回复解析 · 合法性校验 · 失败回落 |
| `bot/rules_text.py` | 规则文本（系统提示词），`tests/test_rules_doc.py` 校验它与引擎一致 |
| `tests/test_llm_bot.py` | stub 一个假 LLM 服务做测试，不消耗真实 token |

## 架构

```
static/  网页前端（原生 HTML/CSS/JS，移动端自适应，HTTP 轮询）
api/     FastAPI：房间、令牌、每人视角序列化、静态托管
engine/  纯 Python 规则引擎：牌型 · 比较 · 合法出牌 · 状态机 · 事件日志
bot/     示例 AI 客户端（只用 HTTP）
tests/   单元 + 集成 + 随机压测 + 端到端
```

`engine/` 不做任何 IO。每次状态转移都往 `Game.events` 追加一条结构化事件，
所以**牌局天然可重放**：`GET /api/rooms/{id}/events` 返回事件流，从头重放即可复现整局
——这也是后续做「复盘分析」的入口，不需要改引擎。

## HTTP 接口

所有需要身份的接口都带请求头 `Authorization: Bearer <token>`。
`token` 由加入房间时返回，代表你在这一桌的座位。状态接口只返回**你自己的手牌**，
别人只暴露剩余张数与名次。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/rooms` | 建房 `{"name":"..."}` → `{room_id}` |
| GET | `/api/rooms` | 房间列表 |
| POST | `/api/rooms/{id}/join` | `{"name":"阿明"}` → `{token, seat}` |
| POST | `/api/rooms/{id}/start` | 需 4 人；局末再调则开下一局（自动进贡） |
| GET | `/api/rooms/{id}/state` | **你的视角**完整状态（含合法出牌列表） |
| GET | `/api/rooms/{id}/legal-moves` | 只要合法出牌（含 `{pass:true}`） |
| POST | `/api/rooms/{id}/play` | `{"cards":[id,...]}` |
| POST | `/api/rooms/{id}/pass` | 过牌 |
| POST | `/api/rooms/{id}/return-tribute` | `{"card":id}` 还贡 |
| GET | `/api/rooms/{id}/events?since=0` | 事件流（复盘/分析用） |

### 状态里有什么

```jsonc
{
  "phase": "play | return_tribute | round_end | match_end | waiting",
  "round_no": 3,
  "levels": [{"team":0,"level":"5","seats":[0,2]}, {"team":1,"level":"2","seats":[1,3]}],
  "you": {"seat":0, "name":"阿明", "team":0,
          "hand":[{"id":17,"rank":"K","suit":"♠","label":"K"}, ...],
          "hand_size": 21},
  "players": [{"seat":0,"name":"...","hand_size":21,"finished_rank":null,"is_you":true,"team":0}],
  "current_seat": 2,
  "your_turn": false,
  "table": {"seat":2,"kind":"pair_run","kind_label":"三连对","size":6,"cards":[...]},
  "tribute": null,          // return_tribute 阶段时给出待还贡信息与可选项
  "legal_moves": [          // 仅 your_turn 时非空
    {"card_ids":[3,16], "kind":"pair", "kind_label":"对子", "size":2, "cards":[...]}
  ],
  "can_pass": false,
  "last_round": {"round":2,"finish_order":[0,2,1,3],"win_team":0,"advance":3,"levels":["5","2"]},
  "match_winner": null,
  "events": [ {"type":"deal","round":3,...}, ... ]   // 最近 40 条
}
```

### AI 接入最小流程

```python
import urllib.request, json

def call(method, path, body=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:8000{path}",
                                data=json.dumps(body).encode() if body else None,
                                method=method)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"{}")

room = call("POST", "/api/rooms", {"name": "AI 桌"})
me   = call("POST", f"/api/rooms/{room['room_id']}/join", {"name": "GPT-1"})
token = me["token"]
call("POST", f"/api/rooms/{room['room_id']}/start", token=token)   # 4 人齐后

while True:
    st = call("GET", f"/api/rooms/{room['room_id']}/state", token=token)
    if st["phase"] == "match_end": break
    if st.get("your_turn"):
        moves = st["legal_moves"]                       # 服务端已算好，无需自己判牌
        pick = moves[0]                                 # ← 换成你的决策
        if pick.get("pass"):
            call("POST", f"/api/rooms/{room['room_id']}/pass", token=token)
        else:
            call("POST", f"/api/rooms/{room['room_id']}/play",
                 {"cards": pick["card_ids"]}, token=token)
```

**关键点**：合法出牌列表由服务端的规则引擎算好推给你，AI 不必自己实现牌型识别；
`events` 与 `legal-moves` 同源，方便做「这一步是不是最优」的复盘分析。

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖四个层次：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_combos.py` | 牌型识别、大小比较（含级牌抬升、同花顺层级、天王炸、A 两头顺） |
| `tests/test_game.py` | 发牌、轮转、出完判定、进贡/抗贡/还贡、升级与过 A |
| `tests/test_fuzz.py` | 40 个随机完整对局 + 20 场随机连局，不变量与事件重放一致性 |
| `tests/test_api.py` | 房间/令牌/出牌接口、视角隐私、事件流 |
| `tests/test_e2e_live.py` | **真实 uvicorn + 4 个 bot 进程**打完整牌局；事件重放与终局状态逐一比对 |
| `tests/test_llm_bot.py` | 大模型接入：提示词、回复解析、失败回落、**假 LLM 服务 + 真牌局服务**端到端 |
| `tests/test_frontend_contract.py` | 前端 DOM id 与 API 路由契约 |

## 后续扩展：复盘分析

事件日志已经是复盘的原料。要加分析，只需：

1. 把 `Game.events` 落盘（每桌一份 JSON/SQLite），接口已暴露 `/events`；
2. 写一个分析器，从事件序列还原每一步的「合法出牌集合」（调用 `engine.legal_moves`），
   对比实际出牌，就能算出漏掉的炸弹、错过的收尾机会等。

引擎的转移是纯函数，重放结果与当时状态必须完全一致（`test_fuzz.py` 里有断言），
所以分析器不需要读服务端内存，离线跑事件文件即可。
