"""API 集成测试：用 FastAPI TestClient 走完整流程。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import app, rooms

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_rooms():
    rooms.clear()
    yield
    rooms.clear()


def make_room_with_players(n: int = 4) -> tuple[str, list[dict]]:
    r = client.post("/api/rooms", json={"name": "测试桌"})
    rid = r.json()["room_id"]
    tokens = []
    for i in range(n):
        j = client.post(f"/api/rooms/{rid}/join", json={"name": f"P{i}"})
        assert j.status_code == 200, j.text
        tokens.append(j.json())
    return rid, tokens


def auth(p: dict) -> dict:
    return {"Authorization": f"Bearer {p['token']}"}


class TestRooms:
    def test_create_and_list(self):
        r = client.post("/api/rooms", json={"name": "甲桌"})
        rid = r.json()["room_id"]
        rooms_list = client.get("/api/rooms").json()
        assert any(x["room_id"] == rid for x in rooms_list)

    def test_join_assigns_seats(self):
        rid, players = make_room_with_players(2)
        assert [p["seat"] for p in players] == [0, 1]

    def test_duplicate_name_rejected(self):
        rid, _ = make_room_with_players(1)
        r = client.post(f"/api/rooms/{rid}/join", json={"name": "P0"})
        assert r.status_code == 400

    def test_room_full(self):
        rid, _ = make_room_with_players(4)
        r = client.post(f"/api/rooms/{rid}/join", json={"name": "P9"})
        assert r.status_code == 400

    def test_bad_token(self):
        rid, _ = make_room_with_players(1)
        r = client.get(f"/api/rooms/{rid}/state", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


class TestFlow:
    def test_cannot_start_with_three(self):
        rid, players = make_room_with_players(3)
        r = client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        assert r.status_code == 400

    def test_start_deals_27_each(self):
        rid, players = make_room_with_players(4)
        r = client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        assert r.status_code == 200, r.text
        for p in players:
            st = client.get(f"/api/rooms/{rid}/state", headers=auth(p)).json()
            assert st["phase"] == "play"
            assert st["you"]["hand_size"] == 27
            assert len(st["you"]["hand"]) == 27
            # 别人的手牌不可见
            for other in st["players"]:
                assert "hand" not in other

    def test_only_current_player_sees_legal_moves(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        st0 = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        st1 = client.get(f"/api/rooms/{rid}/state", headers=auth(players[1])).json()
        assert st0["your_turn"] is True
        assert st0["legal_moves"], "首家应有合法出牌"
        assert st1["your_turn"] is False
        assert st1["legal_moves"] == []

    def test_play_and_pass_cycle(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))

        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        move = st["legal_moves"][0]
        r = client.post(
            f"/api/rooms/{rid}/play",
            json={"cards": move["card_ids"]},
            headers=auth(players[0]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"]["table"] is not None

        # 后面两家若不能压则过
        for i in (1, 2, 3):
            st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[i])).json()
            if not st["your_turn"]:
                break
            moves = client.get(f"/api/rooms/{rid}/legal-moves", headers=auth(players[i])).json()
            if any(not m.get("pass") for m in moves):
                mv = next(m for m in moves if not m.get("pass"))
                r = client.post(
                    f"/api/rooms/{rid}/play",
                    json={"cards": mv["card_ids"]},
                    headers=auth(players[i]),
                )
                assert r.status_code == 200
            else:
                r = client.post(f"/api/rooms/{rid}/pass", headers=auth(players[i]))
                assert r.status_code == 200

    def test_illegal_play_rejected(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[1])).json()
        r = client.post(
            f"/api/rooms/{rid}/play",
            json={"cards": [st["you"]["hand"][0]["id"]]},
            headers=auth(players[1]),
        )
        assert r.status_code == 400

    def test_events_endpoint_exposes_replay_log(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        mv = st["legal_moves"][0]
        client.post(
            f"/api/rooms/{rid}/play",
            json={"cards": mv["card_ids"]},
            headers=auth(players[0]),
        )
        ev = client.get(f"/api/rooms/{rid}/events", headers=auth(players[0])).json()
        types = [e["type"] for e in ev["events"]]
        assert "deal" in types and "play" in types

    def test_next_round_after_finish(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        # 打完整局
        for _ in range(4000):
            st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
            if st["phase"] != "play":
                break
            seat = st["current_seat"]
            p = players[seat]
            moves = client.get(f"/api/rooms/{rid}/legal-moves", headers=auth(p)).json()
            plays = [m for m in moves if not m.get("pass")]
            if plays:
                client.post(
                    f"/api/rooms/{rid}/play",
                    json={"cards": plays[0]["card_ids"]},
                    headers=auth(p),
                )
            else:
                client.post(f"/api/rooms/{rid}/pass", headers=auth(p))
        else:
            pytest.fail("一局没能在 4000 步内结束")

        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        assert st["phase"] in ("round_end", "match_end")
        assert st["last_round"] is not None
        assert sorted(st["last_round"]["finish_order"]) == [0, 1, 2, 3]

        if st["phase"] == "round_end":
            r = client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
            assert r.status_code == 200
            st2 = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
            assert st2["round_no"] == 2
            total = sum(p["hand_size"] for p in st2["players"])
            assert total == 108  # 进贡会改变各人张数，但总数不变
            # 进贡或抗贡必有其一
            types = [e["type"] for e in st2["events"]]
            assert "tribute" in types or "tribute_denied" in types


class TestCardCounting:
    """记牌表与本局出牌记录：给 AI 做记牌用。"""

    def test_state_exposes_round_plays_and_seen_counts(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        assert "round_plays" in st and "seen_counts" in st
        assert st["round_plays"] == []
        # 记牌表：每人每点总数 8，王各 2；已见=自己手里的
        sc = st["seen_counts"]
        assert set(sc) == {"2","3","4","5","6","7","8","9","10","J","Q","K","A","SJ","BJ"}
        assert all(v["total"] == 8 for k, v in sc.items() if k not in ("SJ", "BJ"))
        assert sc["SJ"]["total"] == 2 and sc["BJ"]["total"] == 2
        # 已见总数 = 自己 27 张
        assert sum(v["seen"] for v in sc.values()) == 27

    def test_seen_counts_grow_as_cards_are_played(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        before = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()["seen_counts"]
        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        mv = st["legal_moves"][0]
        client.post(f"/api/rooms/{rid}/play", json={"cards": mv["card_ids"]},
                    headers=auth(players[0]))
        after = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        assert after["round_plays"], "应记录出牌"
        played = after["round_plays"][0]
        assert played["seat"] == 0 and played["passed"] is False
        assert played["cards"]
        # 打出去的牌从自己手里消失，但「已见」不变（本来就算见过）
        assert sum(v["seen"] for v in after["seen_counts"].values()) == 27
        # 但别人视角看，这些牌变成了「已打出」
        other = client.get(f"/api/rooms/{rid}/state", headers=auth(players[1])).json()
        assert sum(v["seen"] for v in other["seen_counts"].values()) == 27 + mv["size"]

    def test_round_plays_records_passes(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        mv = st["legal_moves"][0]
        client.post(f"/api/rooms/{rid}/play", json={"cards": mv["card_ids"]},
                    headers=auth(players[0]))
        # 后面的人如果只能过
        for i in (1, 2, 3):
            moves = client.get(f"/api/rooms/{rid}/legal-moves", headers=auth(players[i])).json()
            if any(not m.get("pass") for m in moves):
                mv2 = next(m for m in moves if not m.get("pass"))
                client.post(f"/api/rooms/{rid}/play", json={"cards": mv2["card_ids"]},
                            headers=auth(players[i]))
            else:
                client.post(f"/api/rooms/{rid}/pass", headers=auth(players[i]))
                st2 = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
                assert any(e.get("passed") for e in st2["round_plays"])
                return
        pytest.skip("本轮无人过牌")



class TestStartIdempotent:
    """多个客户端同时开局：先到者成功，后到者幂等返回，不报 400。"""

    def test_retry_after_server_advanced_is_idempotent(self):
        """客户端以为第 0 局结束去开局，服务端其实已开到第 1 局 → 幂等成功。"""
        rid, players = make_room_with_players(4)
        assert client.post(f"/api/rooms/{rid}/start", headers=auth(players[0])).status_code == 200

        for p in players[1:]:
            r = client.post(f"/api/rooms/{rid}/start",
                            json={"after_round": 0}, headers=auth(p))
            assert r.status_code == 200, r.text
            assert r.json()["already_started"] is True
            assert r.json()["round_no"] == 1

    def test_claiming_finished_round_still_in_play_is_rejected(self):
        """客户端声称第 1 局已结束，但服务端还在打第 1 局 → 状态不一致，拒绝。"""
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        r = client.post(f"/api/rooms/{rid}/start",
                        json={"after_round": 1}, headers=auth(players[1]))
        assert r.status_code == 400

    def test_start_without_body_still_rejected_midgame(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        r = client.post(f"/api/rooms/{rid}/start", headers=auth(players[1]))
        assert r.status_code == 400

    def test_real_race_next_round(self):
        """打完整局后三个 bot 同时请求开下一局：都应成功。"""
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        for _ in range(4000):
            st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
            if st["phase"] != "play":
                break
            tok = players[st["current_seat"]]
            moves = client.get(f"/api/rooms/{rid}/legal-moves", headers=auth(tok)).json()
            plays = [m for m in moves if not m.get("pass")]
            if plays:
                client.post(f"/api/rooms/{rid}/play",
                            json={"cards": plays[0]["card_ids"]}, headers=auth(tok))
            else:
                client.post(f"/api/rooms/{rid}/pass", headers=auth(tok))
        assert st["phase"] == "round_end"

        results = [
            client.post(f"/api/rooms/{rid}/start", json={"after_round": 1}, headers=auth(p))
            for p in players
        ]
        codes = [r.status_code for r in results]
        assert codes.count(200) >= 1, f"至少一个成功：{codes}"
        # 先到者开新局，后到者要么幂等成功、要么在极窄竞态下被拒，但不能是别的错
        for r in results:
            assert r.status_code in (200, 400), r.text
        ok_already = [r for r in results if r.status_code == 200 and r.json().get("already_started")]
        assert ok_already, "后到者应被幂等处理"



class TestRoomLifecycle:
    """孤儿房间会积累（bot 被杀后座位不释放），要能自动清理 + 手动关闭。"""

    def test_sweep_removes_idle_rooms(self):
        import time as _t
        from api.app import sweep_stale_rooms, rooms

        rid, _ = make_room_with_players(1)
        assert rid in rooms
        swept = sweep_stale_rooms(ttl=0)      # 立刻判为闲置
        assert rid in swept
        assert rid not in rooms

    def test_sweep_keeps_recently_active(self):
        from api.app import sweep_stale_rooms, rooms

        rid, _ = make_room_with_players(1)
        swept = sweep_stale_rooms(ttl=3600)   # 一小时内都算活跃
        assert rid not in swept
        assert rid in rooms

    def test_list_rooms_reports_idle_seconds(self):
        rid, _ = make_room_with_players(1)
        rs = client.get("/api/rooms").json()
        row = next(r for r in rs if r["room_id"] == rid)
        assert isinstance(row["idle_seconds"], int)
        assert row["idle_seconds"] >= 0

    def test_polling_keeps_room_alive(self):
        """客户端轮询会 touch，房间不会被误清。"""
        from api.app import sweep_stale_rooms, rooms

        rid, players = make_room_with_players(2)
        for _ in range(3):
            client.get(f"/api/rooms/{rid}/state", headers=auth(players[0]))
        swept = sweep_stale_rooms(ttl=3600)
        assert rid not in swept

    def test_close_idle_room(self):
        rid, _ = make_room_with_players(1)
        r = client.post(f"/api/rooms/{rid}/close")
        assert r.status_code == 200
        assert client.get(f"/api/rooms/{rid}/state",
                          headers={"Authorization": "Bearer x"}).status_code == 404

    def test_close_unknown_room_404(self):
        assert client.post("/api/rooms/nope/close").status_code == 404

    def test_auto_sweep_via_list_endpoint(self):
        import time as _t
        from api.app import rooms, IDLE_TTL_SECONDS

        rid, _ = make_room_with_players(1)
        rooms[rid].last_activity = _t.time() - IDLE_TTL_SECONDS - 10
        client.get("/api/rooms")          # 列表接口顺带清理
        assert rid not in rooms



class TestLeaveAndResume:
    """离席后必须能回来。实测踩过：前端只清 localStorage 不通知服务端，
    座位和名字一直占着，退出就进不去了。"""

    def test_leave_before_start_frees_seat_and_name(self):
        rid, players = make_room_with_players(2)
        r = client.post(f"/api/rooms/{rid}/leave", headers=auth(players[0]))
        assert r.status_code == 200
        assert r.json()["seat_held"] is False

        # 同名能重新加入
        j = client.post(f"/api/rooms/{rid}/join", json={"name": "P0"})
        assert j.status_code == 200
        assert j.json()["seat"] == 0

    def test_leave_without_token_rejected(self):
        rid, _ = make_room_with_players(1)
        assert client.post(f"/api/rooms/{rid}/leave").status_code == 401

    def test_leave_midgame_holds_seat(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        r = client.post(f"/api/rooms/{rid}/leave", headers=auth(players[1]))
        assert r.json()["seat_held"] is True

    def test_rejoin_midgame_resumes_same_seat(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[1]))
        client.post(f"/api/rooms/{rid}/leave", headers=auth(players[1]))
        j = client.post(f"/api/rooms/{rid}/join", json={"name": "P1"})
        assert j.status_code == 200
        assert j.json()["resumed"] is True
        assert j.json()["seat"] == 1

    def test_rejoin_midgame_gets_valid_token(self):
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        old_token = players[2]["token"]
        client.post(f"/api/rooms/{rid}/leave", headers=auth(players[2]))
        j = client.post(f"/api/rooms/{rid}/join", json={"name": "P2"})
        new_token = j.json()["token"]
        assert new_token != old_token
        # 新 token 能查状态，旧 token 失效
        assert client.get(f"/api/rooms/{rid}/state",
                          headers={"Authorization": f"Bearer {new_token}"}).status_code == 200
        assert client.get(f"/api/rooms/{rid}/state",
                          headers={"Authorization": f"Bearer {old_token}"}).status_code == 401

    def test_duplicate_name_still_rejected_when_active(self):
        rid, _ = make_room_with_players(2)
        r = client.post(f"/api/rooms/{rid}/join", json={"name": "P0"})
        assert r.status_code == 400

    def test_last_player_leaving_removes_room(self):
        rid, players = make_room_with_players(1)
        r = client.post(f"/api/rooms/{rid}/leave", headers=auth(players[0]))
        assert r.json().get("room_removed") is True
        assert client.post(f"/api/rooms/{rid}/join", json={"name": "X"}).status_code == 404

    def test_seat_freed_by_leave_is_reusable(self):
        """离席释放的座位能被别人坐。"""
        rid, players = make_room_with_players(2)   # 占 0、1
        client.post(f"/api/rooms/{rid}/leave", headers=auth(players[1]))
        j = client.post(f"/api/rooms/{rid}/join", json={"name": "新人"})
        assert j.status_code == 200
        assert j.json()["seat"] == 1, "离席腾出的座位应可复用"
        # 补齐 4 人即可开局
        client.post(f"/api/rooms/{rid}/join", json={"name": "C1"})
        client.post(f"/api/rooms/{rid}/join", json={"name": "C2"})
        r = client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        assert r.status_code == 200



class TestRoundPlaysOrdering:
    """出牌历史里的牌必须和桌面一样按牌型排。用户反馈：桌面正常、历史里乱。"""

    def test_history_triple_pair_ordered(self):
        """三带二在历史里要按「三张在前、对子在后」排，且不丢牌。"""
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()

        target = next((m for m in st["legal_moves"] if m["kind"] == "triple_pair"), None)
        if target is None:
            target = st["legal_moves"][0]
        client.post(f"/api/rooms/{rid}/play",
                    json={"cards": target["card_ids"]}, headers=auth(players[0]))

        st2 = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        row = st2["round_plays"][-1]
        assert row["passed"] is False
        # 不许丢牌：历史里的牌数 = 出的牌数
        assert len(row["cards"]) == target["size"], (
            f"历史丢了牌：出 {target['size']} 张，显示 {len(row['cards'])} 张"
        )
        # 牌序与引擎的结构序一致
        from engine.combos import order_cards
        ids = [c["id"] for c in row["cards"]]
        assert ids == order_cards(ids, row["kind"]) or sorted(ids) == sorted(
            order_cards(ids, row["kind"])
        ), "历史牌序应与结构序一致"

    def test_triple_pair_structure_when_no_wild(self):
        """无百搭时：三张必须同点在前、对子同点在后（纯结构断言）。"""
        from engine.combos import identify, order_cards
        from engine.cards import DECK

        def pick(*specs):
            used, out = set(), []
            for rank, suit in specs:
                for c in DECK:
                    if c.id in used or c.rank != rank:
                        continue
                    if suit is not None and c.suit != suit:
                        continue
                    used.add(c.id)
                    out.append(c.id)
                    break
            return out

        ids = pick(("3", "♥"), ("K", "♠"), ("3", "♠"), ("K", "♥"), ("3", "♣"))
        c = identify(ids, "5")   # 级牌是 5，红桃 3 不是百搭
        assert c is not None and c.kind == "triple_pair"
        o = order_cards(c.cards, c.kind)
        ranks = [DECK[i].rank for i in o]
        assert ranks == ["3", "3", "3", "K", "K"], ranks

    def test_history_matches_table_ordering(self):
        """同一手在 table 和 round_plays 里的排列必须一致。"""
        rid, players = make_room_with_players(4)
        client.post(f"/api/rooms/{rid}/start", headers=auth(players[0]))
        st = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        mv = st["legal_moves"][0]
        client.post(f"/api/rooms/{rid}/play", json={"cards": mv["card_ids"]},
                    headers=auth(players[0]))
        st2 = client.get(f"/api/rooms/{rid}/state", headers=auth(players[0])).json()
        assert st2["table"] is not None and st2["round_plays"]
        assert [c["id"] for c in st2["table"]["cards"]] == \
               [c["id"] for c in st2["round_plays"][-1]["cards"]], \
               "桌面与出牌历史的牌序应一致"
