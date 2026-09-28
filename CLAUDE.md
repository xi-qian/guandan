# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

使用者文档（规则摘要、HTTP 接口表、AI 接入示例、部署）见 `README.md`，此处不重复。

## 开发命令

```bash
python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8000   # 起服务

python3 -m pytest tests/ -q                                   # 全量（约 105 秒）
python3 -m pytest tests/test_combos.py -q                     # 单文件
python3 -m pytest tests/test_combos.py::TestWildcards -q      # 单用例
python3 -m pytest tests/ -q -k "Tribute or Jiefeng"           # 按关键字
python3 -m pytest tests/test_e2e_live.py -q                   # 起真实服务+4个bot进程

python3 scripts/smoke.py    # 浏览器式冒烟（建房/入座/开局/出牌/事件流）
node --check static/app.js  # 前端无构建步骤，语法检查即可
```

默认 `python3` 是 **3.9**：FastAPI 路由签名不能用 `X | None`（用 `Optional[X]`），
`f` 表达式里不能有反斜杠。改运行相关代码后跑一遍全量测试验证。

启动 bot 时用 `python3 -u` 或确保 `print(..., flush=True)`——stdout 重定向到文件是块缓冲，
否则日志一直为空、看起来像卡死。

## 必须保持的架构约束

这些是读多个文件才能看清的边界，破坏了会出隐蔽 bug：

**`engine/` 零 IO。** 不碰网络、文件、时间。判牌逻辑可独立单测。

**牌序在 `identify()` 时就定好**，因为只有那时知道百搭（逢人配）变成了什么。
`Combo.cards` 已是结构序（三带二=三张在前、连张按窗口顺序、百搭放进它填补的那一组）。
下游——API 序列化、出牌事件、LLM 提示词——**一律直接用，不许二次重排**
（含百搭时点数分组不整齐，重排会丢牌）。`order_cards()` 只是给历史兜底的，且绝不丢牌。

**`bot/` 不 import `engine/`。** 它是「外部 AI 如何接入」的可运行样例，只走 HTTP。
有测试 `test_bot_package_has_no_engine_dependency` 守着。

**AI 只能从合法动作里挑编号。** 服务端 `legal_moves` 算好列表，模型回 `{"index": N}`。
因此它出不了非法牌；解析失败/越界/超时一律回落 `bot/client.py` 的规则策略，牌局不卡住。
`LLMClient._post` 必须捕获 `socket.timeout`/`OSError` 并包成 `LLMError`——
`urlopen` 的超时不会被包成 `URLError`，漏掉会把 bot 进程打死。

**同一决策点最多打一次 LLM。** `LLMBot._decision_key()` 按局面签名缓存，动作失败重试时复用。
`run_loop` 只在 `needs_action` 为真时才进 `decide()`，纯轮询不碰 LLM。

**事件日志即真相。** `Game.events` 记录每次转移，重放必须复现终局状态（`test_fuzz.py` 有断言）。
要加复盘/分析就离线读事件文件，不要改引擎。

## 规则决策

和常见玩法有出入、被测试钉死的几条。改之前先看 `tests/test_rules_doc.py`：

- **级牌全桌统一** = 上一局胜方在打的级。不按队伍分别认定，否则会出现
  「对手的 2 比我的 A 大」这种歧义。
- **接风**：仅当「出牌者已出完」**且**「其余还在手的都过」，才让**队友**自由领出；
  队友也出完了才轮到下家。有人压过就不是接风。
  ⚠️ 「队友也出完」这条兜底实战中不可达——队友俩都出完即双上，会提前收局。
- **双上即收局**：一方包揽 1、2 名后立即结束。剩下两人按剩余张数定 3/4 名
  （张少者三游，同数按出牌顺序）。这类「裁定」出的名次手里还有牌，
  `finish` 事件带 `reason="double_up_by_card_count"` 或 `"last_remaining"`——
  断言「名次内玩家必须空手」时要排除它们（踩过）。
- **逢人配**：红桃级牌当任意牌，**不能当王**；打几就是几的红桃。
- **同一批牌可能有多种打法**（百搭可变）：`identify(cards, level, prefer=...)` 有 prefer 按指定，
  否则取最强。`interpretations()` 供 UI/提示词列全。
- **同花顺**强度在五炸与六炸之间，但**比五炸更常见**（31% vs 27%）——稀有度≠强度。

`bot/rules_text.py` 是给模型的规则说明，`tests/test_rules_doc.py` 逐条拿引擎校验。
**改规则必须同时改引擎和那份文本**，否则模型会被教错。文本里的数字（如稀有度表）
来自 30 万次发牌模拟，改动要重新算。

## 测试地图

| 文件 | 挡住什么 |
| --- | --- |
| `test_rules_doc.py` | 文档 ↔ 引擎一致性（规则只改一处会红） |
| `test_fuzz.py` | 随机对局不变量、事件重放（判牌/状态机 bug 先在这暴露） |
| `test_combos.py` | 牌型识别、比较、百搭、结构排牌 |
| `test_game.py` | 轮转、进贡/抗贡/还贡、升级与过 A |
| `test_e2e_live.py` | 真实 uvicorn + 4 bot 进程（慢，机器忙时可能到 300 秒上限） |
| `test_frontend_contract.py` | 前端 DOM id / API 路由 / 架构边界 |

`test_e2e_live.py` 用随机发牌，偶发失败先看是否超时而非逻辑错误。
