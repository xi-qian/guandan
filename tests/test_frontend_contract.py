"""前端契约测试：HTML 元素 id 与 app.js 的取用必须一致，避免渲染时找不到节点。"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def test_all_js_referenced_ids_exist_in_html():
    html = read("static/index.html")
    js = read("static/app.js")
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
    missing = sorted(js_ids - html_ids)
    assert not missing, f"app.js 引用了 HTML 里不存在的 id：{missing}"


def test_html_ids_unused_is_ok_but_spelling_suspects_flagged():
    html = read("static/index.html")
    js = read("static/app.js")
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
    # 允许 HTML 里有 JS 未用到的 id，但拼写接近的要提示
    unused = html_ids - js_ids
    for u in unused:
        for j in js_ids:
            assert u != j  # 同名必被用到，否则上面已报


def test_js_has_no_undeclared_dom_roots():
    js = read("static/app.js")
    assert 'getElementById' in js or '$(' in js
    assert "localStorage" in js  # 断线重连依赖
    assert "Authorization" in js  # 带 token 请求


def test_static_assets_are_served_paths():
    html = read("static/index.html")
    assert '/static/style.css' in html
    assert '/static/app.js' in html
    assert os.path.exists(os.path.join(ROOT, "static/style.css"))
    assert os.path.exists(os.path.join(ROOT, "static/app.js"))


def test_endpoints_used_by_frontend_exist_in_api():
    js = read("static/app.js")
    api = read("api/app.py")
    used = set(re.findall(r'api\(\s*[`"]([^`"\'?]+)', js))
    # 带路径参数的模板串
    used |= set(re.findall(r'`(/api/rooms/[^`]+)`', js))
    routes = set(re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"', api))
    # 把前端的 {state.roomId} 这类插值换成占位
    normalized = {re.sub(r"\$\{[^}]+\}", "{room_id}", u) for u in used}
    for u in normalized:
        if u.startswith("/api/rooms/{room_id}/"):
            assert any(r.startswith("/api/rooms/{room_id}/") for r in routes), u
        else:
            assert u in routes, f"前端调用了 API 里不存在的路由：{u}（可用：{sorted(routes)}）"



def test_close_endpoint_routed_and_used():
    js = read("static/app.js")
    api_src = read("api/app.py")
    assert "/close" in js, "前端应调用关闭接口"
    assert '@app.post("/api/rooms/{room_id}/close")' in api_src
    assert "idle_seconds" in api_src, "房间列表应带闲置秒数"
    assert "idle_seconds" in js, "前端应显示闲置时长"



def test_play_history_ui_present():
    html = read("static/index.html")
    js = read("static/app.js")
    assert 'id="history"' in html, "应有本轮出牌记录区"
    assert 'id="history-count"' in html
    assert "round_plays" in js, "前端应渲染 round_plays"
    assert "renderHistory" in js


def test_hand_sort_toggle_present():
    html = read("static/index.html")
    js = read("static/app.js")
    assert 'id="btn-sort"' in html, "应有手牌排序按钮"
    assert "sortByMode" in js
    assert "SORT_LABEL" in js
    for mode in ("rank", "suit", "group"):
        assert f'"{mode}"' in js, f"排序模式 {mode} 未实现"
    assert "localStorage.getItem(\"gd_sort\")" in js, "排序偏好应记住"


def test_combo_cards_ordered_server_side():
    """牌型排列只在服务端做一次（identify 时），下游一律不二次重排。

    含百搭时点数分组不整齐，二次重排会丢牌——踩过。
    """
    engine = read("engine/combos.py")
    game = read("engine/game.py")
    assert "def order_cards" in engine
    assert "rank_order" in engine, "identify 应按结构排牌"
    assert "combo.cards" in game, "出牌事件应存结构序，而不是排序后的原始牌"
    # 下游不得再重排
    assert "order_cards(" not in read("api/app.py"), "API 不应二次重排"
    assert "order_cards(" not in read("bot/llm_bot.py"), "提示词不应二次重排"



def test_room_list_distinguishes_live_from_stale():
    js = read("static/app.js")
    css = read("static/style.css")
    assert "tag-live" in js and "tag-stale" in js
    assert "stale" in js, "闲置房间应降权显示"
    assert "sort(" in js, "活跃房间应排前面"
    assert ".tag-live" in css and ".tag-stale" in css



def test_interpret_picker_wired():
    js = read("static/app.js")
    api = read("api/app.py")
    html = read("static/index.html")
    css = read("static/style.css")
    assert "/interpret" in js and "/interpret" in api
    assert "as_kind" in js and "as_kind" in api
    assert "showInterpretPicker" in js
    assert ".picker-overlay" in css


def test_bot_package_has_no_engine_dependency():
    """bot/ 是「外部 AI 接入样例」，只走 HTTP，不许 import engine——
    否则就证明不了「AI 方便接入」。"""
    import os
    for fname in ("client.py", "llm_client.py", "llm_bot.py", "rules_text.py"):
        src = read(f"bot/{fname}")
        assert "from engine" not in src, f"bot/{fname} 不应依赖 engine"
        assert "import engine" not in src, f"bot/{fname} 不应依赖 engine"


def test_hand_free_arrangement_wired():
    """手牌可自由拖动排序，选中多张可组合成一组整组拖动。"""
    js = read("static/app.js")
    html = read("static/index.html")
    css = read("static/style.css")
    for fn in ("reconcileOrder", "renderHand", "attachDrag", "groupSelected",
               "ungroupSelected", "commitDrop", "sortByMode"):
        assert fn in js, f"缺 {fn}"
    assert 'id="btn-group"' in html and 'id="btn-ungroup"' in html
    assert "pointerdown" in js, "拖动要用 pointer events（手机才好使）"
    assert "drag-ghost" in js and ".drag-ghost" in css
    assert ".group" in css and ".group-tag" in css
    assert "touch-action: none" in css, "手牌区要禁掉浏览器手势"


def test_hand_order_survives_server_sync():
    """服务端手牌变化时，本地编排要保留仍在手的牌的顺序，新牌放末尾。"""
    js = read("static/app.js")
    src = js[js.index("function reconcileOrder"):js.index("function sortByMode")]
    assert "live.has" in src, "要剔除已出掉的牌"
    assert "next.push({ kind: \"single\", id: c.id })" in src, "新牌要补进编排"
    assert "it.ids.filter" in src, "组合里被出掉的牌要从组里剔除"


def test_hand_reorder_not_interrupted_by_polling():
    """拖动中轮询重绘会把牌打乱，必须跳过。"""
    js = read("static/app.js")
    src = js[js.index("function renderHand"):js.index("function attachDrag")]
    assert "DRAG.active" in src or "DRAG.item" in src, "拖动中应跳过重绘"


def test_manual_order_restorable_after_sort():
    """排序后要能还原原来手动排的顺序。"""
    js = read("static/app.js")
    html = read("static/index.html")
    assert 'id="btn-restore"' in html, "应有还原按钮"
    assert "restoreManualOrder" in js
    assert "savedOrder" in js
    assert "cloneItem" in js, "快照要深拷贝，别被后续改动污染"

    # 排序前必须存快照（只在没有快照时存，避免覆盖原手动顺序）
    assert "if (!state.savedOrder) state.savedOrder" in js, "排序时要存快照"

    # 手动改动后快照作废
    assert js.count("state.savedOrder = null") >= 3, (
        "组合/拆开/拖动 三处手动改动都应作废快照"
    )


def test_saved_order_survives_hand_sync():
    """快照也要随手牌变化剔除已出掉的牌，否则还原会带回不存在的牌。"""
    js = read("static/app.js")
    src = js[js.index("function reconcileOrder"):js.index("function sortByMode")]
    assert "state.savedOrder" in src, "快照要跟着手牌对齐"
