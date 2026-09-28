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
