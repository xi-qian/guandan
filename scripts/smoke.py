"""浏览器式冒烟：按前端实际请求序列走一遍，校验状态字段与 app.js 一致。"""
import json, os, re, socket, subprocess, sys, time, urllib.request

ROOT = "/Users/a1/MyProjects/game"
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

port = free_port()
log = open("/tmp/guandan_smoke.log", "w")
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.app:app",
                        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{port}"

def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}

try:
    for _ in range(60):
        try:
            call("GET", "/api/rooms"); break
        except Exception: time.sleep(0.2)
    else:
        raise SystemExit("服务未起来")

    # 1) 首页与静态资源
    html = urllib.request.urlopen(base + "/", timeout=10).read().decode()
    assert "掼蛋" in html and 'id="hand"' in html, "首页缺关键元素"
    for p in ("/static/app.js", "/static/style.css"):
        assert urllib.request.urlopen(base + p, timeout=10).status == 200
    print("✓ 首页与静态资源")

    # 2) app.js 里用到的每个 $(\"id\") 都在 HTML 中
    js = urllib.request.urlopen(base + "/static/app.js", timeout=10).read().decode()
    js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    missing = js_ids - html_ids
    assert not missing, f"前端 DOM 缺失: {missing}"
    print(f"✓ DOM 契约（{len(js_ids)} 个节点全部存在）")

    # 3) 建房 + 四人入座（前端的 join 请求形状）
    room = call("POST", "/api/rooms", {"name": "冒烟桌"})
    rid = room["room_id"]
    tokens = []
    for i in range(4):
        j = call("POST", f"/api/rooms/{rid}/join", {"name": f"玩家{i}"})
        assert set(j) >= {"token", "seat", "room_id"}
        tokens.append(j["token"])
    print(f"✓ 房间 {rid} 四人入座")

    # 4) 未开局时的状态形状（前端 waiting 分支）
    st = call("GET", f"/api/rooms/{rid}/state", token=tokens[0])
    for k in ("room_id", "room_name", "phase", "you", "seats", "can_start"):
        assert k in st, f"waiting 状态缺字段 {k}"
    assert st["phase"] == "waiting"
    print("✓ waiting 状态形状")

    # 5) 开局后状态形状（前端 play 分支）
    call("POST", f"/api/rooms/{rid}/start", token=tokens[0])
    st = call("GET", f"/api/rooms/{rid}/state", token=tokens[0])
    for k in ("phase", "round_no", "levels", "you", "players", "current_seat",
              "your_turn", "table", "last_round", "match_winner", "events",
              "tribute", "legal_moves", "can_pass"):
        assert k in st, f"play 状态缺字段 {k}"
    assert st["your_turn"] is True and st["legal_moves"]
    assert "hand" in st["you"] and "hand_size" in st["you"]
    assert all("hand" not in p for p in st["players"]), "不该泄露别人手牌"
    mv = st["legal_moves"][0]
    for k in ("card_ids", "kind", "kind_label", "size", "cards"):
        assert k in mv, f"legal_moves 缺字段 {k}"
    print(f"✓ play 状态形状（合法出牌 {len(st['legal_moves'])} 个）")

    # 6) 出一手牌，确认 table 结构
    r = call("POST", f"/api/rooms/{rid}/play", {"cards": mv["card_ids"]}, token=tokens[0])
    assert r["ok"] is True and r["played"]["cards"]
    st2 = call("GET", f"/api/rooms/{rid}/state", token=tokens[1])
    assert st2["your_turn"] is True
    assert st2["table"] and st2["table"]["seat"] == 0
    print(f"✓ 出牌生效：{r['played']['kind_label']} {r['played']['size']} 张")

    # 7) 过牌与回合推进
    moves = call("GET", f"/api/rooms/{rid}/legal-moves", token=tokens[1])
    assert any(m.get("pass") for m in moves), "跟牌时应可过牌"
    call("POST", f"/api/rooms/{rid}/pass", token=tokens[1])
    st3 = call("GET", f"/api/rooms/{rid}/state", token=tokens[2])
    assert st3["current_seat"] == 2
    print("✓ 过牌与回合推进")

    # 8) 事件流可读
    ev = call("GET", f"/api/rooms/{rid}/events?since=0", token=tokens[0])
    types = [e["type"] for e in ev["events"]]
    assert types[0] == "deal" and "play" in types and "pass" in types
    print(f"✓ 事件流 {len(ev['events'])} 条，可用于复盘")

    print("\n冒烟全部通过 ✅")
finally:
    srv.terminate()
    try: srv.wait(timeout=5)
    except Exception: srv.kill()
    log.close()
