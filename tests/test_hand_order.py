"""手牌编排逻辑测试：直接跑 app.js 里的纯函数（node 执行）。

用户的关切：手动排好顺序后，出牌了怎么办？
答案：仍在手的牌保序、新牌补末尾、组内被出掉的自动收敛——
这些只写在 JS 里，必须实测而不是靠眼看。
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None,
    reason="需要 node 执行 app.js 里的纯函数",
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _grab(js: str, name: str) -> str:
    i = js.index(f"  function {name}(")
    depth = 0
    started = False
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
            started = True
        elif js[j] == "}":
            depth -= 1
            if started and depth == 0:
                return js[i : j + 1]
    raise AssertionError(f"找不到函数 {name}")


def _run_case(body: str) -> None:
    js = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    fns = "\n".join(_grab(js, n) for n in ("cloneItem", "flattenOrder", "reconcileOrder"))
    harness = f"""
const state = {{ order: [], savedOrder: null, cardById: {{}} }};
{fns}
function hand(ids) {{ return ids.map((id) => ({{ id, rank: String(id), suit: "x" }})); }}
function eq(a, b, msg) {{
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A !== B) throw new Error(msg + "：期望 " + B + " 实得 " + A);
}}
{body}
console.log("OK");
"""
    path = "/tmp/_gd_hand_order.js"
    open(path, "w", encoding="utf-8").write(harness)
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


class TestHandOrderAfterPlay:
    def test_remaining_cards_keep_manual_order(self):
        """出牌后，剩下还在手的牌保住你排的顺序。"""
        _run_case(
            """
state.order = [{kind:"single",id:1},{kind:"single",id:2},
               {kind:"single",id:3},{kind:"single",id:4}];
reconcileOrder(hand([1,3,4]));                 // 出掉了 2
eq(flattenOrder(), [1,3,4], "出牌后顺序");
"""
        )

    def test_new_cards_appended_at_end(self):
        """进贡/还贡来的新牌补到末尾，不插乱已有顺序。"""
        _run_case(
            """
state.order = [{kind:"single",id:1},{kind:"single",id:3}];
reconcileOrder(hand([1,3,9]));
eq(flattenOrder(), [1,3,9], "新牌位置");
"""
        )

    def test_group_shrinks_when_member_played(self):
        """组里被出掉一张后，组自动收敛（剩 1 张时还原为单张）。"""
        _run_case(
            """
state.order = [{kind:"group",ids:[2,3]},{kind:"single",id:1}];
reconcileOrder(hand([2,1]));
eq(flattenOrder(), [2,1], "组收敛后顺序");
eq(state.order[0].kind, "single", "只剩一张应还原为单张");
"""
        )

    def test_intact_group_stays_grouped(self):
        _run_case(
            """
state.order = [{kind:"group",ids:[2,3]},{kind:"single",id:1}];
reconcileOrder(hand([2,3,1,5]));
eq(state.order[0].kind, "group", "组应保留");
eq(flattenOrder(), [2,3,1,5], "新牌仍补末尾");
"""
        )

    def test_restore_snapshot_drops_played_cards(self):
        """还原快照不能带回已经出掉的牌。"""
        _run_case(
            """
state.order = [{kind:"single",id:1},{kind:"single",id:2},{kind:"single",id:3}];
state.savedOrder = [{kind:"single",id:3},{kind:"single",id:2},{kind:"single",id:1}];
reconcileOrder(hand([1,3]));                   // 出掉了 2
eq(flattenOrder(state.savedOrder), [3,1], "快照里也要剔除 2");
"""
        )

    def test_new_round_resets_to_deal_order(self):
        """新一局牌 id 全变，按发牌顺序重排（快照也重置）。"""
        _run_case(
            """
state.order = [{kind:"group",ids:[1,2]},{kind:"single",id:3}];
state.savedOrder = [{kind:"single",id:1}];
reconcileOrder(hand([50,51,52,53]));
eq(flattenOrder(), [50,51,52,53], "新局按发牌顺序");
eq(flattenOrder(state.savedOrder), [50,51,52,53], "快照也重置");
"""
        )

    def test_group_split_across_play_keeps_relative_position(self):
        """组里出掉的牌不影响其他 item 的相对顺序。"""
        _run_case(
            """
state.order = [{kind:"single",id:1},{kind:"group",ids:[2,3]},
               {kind:"single",id:4},{kind:"group",ids:[5,6]}];
reconcileOrder(hand([1,2,4,5,6]));             // 出掉了 3
eq(flattenOrder(), [1,2,4,5,6], "相对顺序不变");
"""
        )
