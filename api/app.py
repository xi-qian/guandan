"""掼蛋 HTTP 服务：房间管理、身份令牌、状态查询、出牌接口。

纯 HTTP 轮询，无 WebSocket。每个玩家用自己的 token 查询自己视角的状态。
"""
from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.cards import CARD_BY_ID, RANK_LABEL
from engine.combos import Combo
from engine.game import Game, IllegalMove

STATIC_DIR = __file__.rsplit("/", 2)[0] + "/static"

# 闲置多久的房间自动清理（秒）。正在打的桌会被轮询不断刷新，不会被误删。
IDLE_TTL_SECONDS = 600


app = FastAPI(title="掼蛋服务", version="0.1.0")


# ---------------------------------------------------------------- 房间

@dataclass
class Player:
    name: str
    token: str
    seat: int
    left: bool = False   # 主动离席；牌局中保留座位供同名重入


@dataclass
class Room:
    id: str
    name: str
    players: dict[str, Player] = field(default_factory=dict)  # token -> Player
    seats: list[Player | None] = field(default_factory=lambda: [None, None, None, None])
    game: Game | None = None
    first_leader: int = 0
    last_activity: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_activity = time.time()

    def idle_seconds(self) -> float:
        return time.time() - self.last_activity

    def by_token(self, token: str) -> Player:
        p = self.players.get(token)
        if p is None:
            raise HTTPException(401, "无效的 token")
        return p

    def by_seat(self, seat: int) -> Player | None:
        return self.seats[seat]

    def full(self) -> bool:
        return all(s is not None for s in self.seats)

    def started(self) -> bool:
        return self.game is not None


rooms: dict[str, Room] = {}


def sweep_stale_rooms(ttl: float = IDLE_TTL_SECONDS) -> list[str]:
    """清掉超过 ttl 没有任何请求的房间，返回被清掉的 id。

    正在打的桌会被客户端轮询不断 touch，所以不会被误删；
    bot 被 kill 掉的孤儿桌则会在 ttl 后消失。
    """
    stale = [rid for rid, r in rooms.items() if r.idle_seconds() > ttl]
    for rid in stale:
        del rooms[rid]
    return stale


def get_room(room_id: str, touch: bool = True) -> Room:
    sweep_stale_rooms()
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, "房间不存在")
    if touch:
        room.touch()
    return room


def auth(room: Room, authorization: str | None) -> Player:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <token>")
    return room.by_token(authorization.split(" ", 1)[1].strip())


# ---------------------------------------------------------------- 序列化

KIND_LABEL = {
    "single": "单张",
    "pair": "对子",
    "triple": "三同张",
    "triple_pair": "三带二",
    "straight": "顺子",
    "pair_run": "三连对",
    "triple_run": "钢板",
    "bomb": "炸弹",
    "straight_flush": "同花顺",
    "rocket": "天王炸",
}


def card_json(card_id: int) -> dict[str, Any]:
    c = CARD_BY_ID[card_id]
    return {"id": c.id, "rank": c.rank, "suit": c.suit, "label": c.label}


def combo_json(combo: Combo | None, seat: int | None) -> dict[str, Any] | None:
    if combo is None:
        return None
    return {
        "seat": seat,
        "kind": combo.kind,
        "kind_label": KIND_LABEL.get(combo.kind, combo.kind),
        "size": combo.size,
        "cards": [card_json(c) for c in combo.cards],
    }


RANK_TOTAL = {r: 8 for r in (
    "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"
)}
RANK_TOTAL["SJ"] = 2
RANK_TOTAL["BJ"] = 2


def _round_plays(g: Game) -> list[dict[str, Any]]:
    """本局（最近一次发牌起）的出牌与过牌记录，旧→新。"""
    start = 0
    for i, e in enumerate(g.events):
        if e.get("type") == "deal":
            start = i
    out: list[dict[str, Any]] = []
    for e in g.events[start:]:
        t = e.get("type")
        if t == "play":
            out.append({
                "seat": e["seat"],
                "kind": e["kind"],
                "kind_label": KIND_LABEL.get(e["kind"], e["kind"]),
                "size": e["size"],
                "cards": [card_json(c) for c in e["cards"]],
                "passed": False,
            })
        elif t == "pass":
            out.append({"seat": e["seat"], "passed": True, "size": 0,
                        "kind": "pass", "kind_label": "过牌", "cards": []})
    return out


def _seen_counts(g: Game, seat: int) -> dict[str, dict[str, int]]:
    """记牌：已见 = 你手上的 + 本局已打出的；总数为两副牌的张数。"""
    seen: dict[str, int] = {r: 0 for r in RANK_TOTAL}
    for c in g.hands[seat]:
        seen[CARD_BY_ID[c].rank] += 1
    for e in g.events:
        if e.get("type") != "play":
            continue
        for c in e["cards"]:
            seen[CARD_BY_ID[c].rank] += 1
    return {
        r: {"seen": seen.get(r, 0), "total": RANK_TOTAL[r]}
        for r in RANK_TOTAL
    }


def state_for(room: Room, me: Player) -> dict[str, Any]:
    g = room.game
    if g is None:
        return {
            "room_id": room.id,
            "room_name": room.name,
            "phase": "waiting",
            "seats": [
                {"seat": i, "name": p.name if p else None, "ready": p is not None}
                for i, p in enumerate(room.seats)
            ],
            "you": {"seat": me.seat, "name": me.name},
            "can_start": room.full() and me.seat == 0,
        }

    finished_rank = {s: i + 1 for i, s in enumerate(g.finished)}
    players = []
    for i, p in enumerate(room.seats):
        players.append(
            {
                "seat": i,
                "name": p.name if p else f"座位{i}",
                "hand_size": len(g.hands[i]),
                "finished_rank": finished_rank.get(i),
                "is_you": i == me.seat,
                "team": i % 2,
            }
        )

    out: dict[str, Any] = {
        "room_id": room.id,
        "room_name": room.name,
        "phase": g.phase,
        "round_no": g.round_no,
        "levels": [
            {"team": 0, "level": g.levels[0], "seats": [0, 2]},
            {"team": 1, "level": g.levels[1], "seats": [1, 3]},
        ],
        "level_rank": g.level_rank,
        "you": {
            "seat": me.seat,
            "name": me.name,
            "hand": [card_json(c) for c in sorted(g.hands[me.seat], key=lambda x: CARD_BY_ID[x].sort_key)],
            "hand_size": len(g.hands[me.seat]),
            "team": me.seat % 2,
        },
        "players": players,
        "current_seat": g.current,
        "your_turn": g.phase == "play" and g.current == me.seat,
        "table": combo_json(g.table, g.table_seat),
        "last_round": g.last_round,
        "match_winner": g.match_winner,
        "events": g.events[-40:],
        "round_plays": _round_plays(g),
        "seen_counts": _seen_counts(g, me.seat),
    }

    if g.phase == "return_tribute" and g.return_queue:
        waiting, recipient = g.return_queue[0]
        out["tribute"] = {
            "action": "return",
            "waiting_seat": waiting,
            "to_seat": recipient,
            "options": [card_json(c) for c in g.return_options(me.seat)] if waiting == me.seat else [],
        }
    else:
        out["tribute"] = None

    if g.phase == "play" and g.current == me.seat:
        out["legal_moves"] = [
            {
                "cards": [card_json(c) for c in m.cards],
                "card_ids": list(m.cards),
                "kind": m.kind,
                "kind_label": KIND_LABEL.get(m.kind, m.kind),
                "size": m.size,
            }
            for m in g.legal_moves(me.seat)
        ]
        out["can_pass"] = g.table is not None
    else:
        out["legal_moves"] = []
        out["can_pass"] = False

    return out


# ---------------------------------------------------------------- 接口

class CreateRoom(BaseModel):
    name: str = "掼蛋桌"


class JoinRoom(BaseModel):
    name: str


class PlayBody(BaseModel):
    cards: list[int]


class ReturnTribute(BaseModel):
    card: int


class StartBody(BaseModel):
    # 客户端已知的局数；用于「我想开第 N+1 局」的幂等判断
    after_round: Optional[int] = None


@app.post("/api/rooms")
def create_room(body: CreateRoom) -> dict[str, Any]:
    rid = uuid.uuid4().hex[:8]
    room = Room(id=rid, name=body.name)
    rooms[rid] = room
    return {"room_id": rid, "name": room.name}


@app.get("/api/rooms")
def list_rooms() -> list[dict[str, Any]]:
    sweep_stale_rooms()
    return [
        {
            "room_id": r.id,
            "name": r.name,
            "seats_taken": sum(1 for s in r.seats if s),
            "started": r.started(),
            "phase": r.game.phase if r.game else "waiting",
            "idle_seconds": int(r.idle_seconds()),
        }
        for r in rooms.values()
    ]


@app.post("/api/rooms/{room_id}/leave")
def leave_room(
    room_id: str, authorization: Optional[str] = Header(default=None)
) -> dict[str, Any]:
    """主动离席。

    未开局：直接释放座位与名字，可以重新加入。
    已开局：座位保留、标记离席，可用同名重入续接（免得半路退出就永远进不来）。
    """
    room = get_room(room_id)
    me = auth(room, authorization)
    if room.started() and room.game.phase in ("play", "return_tribute"):
        me.left = True
        return {"ok": True, "seat_held": True, "note": "牌局进行中，座位已保留，可同名重入"}
    # 未开局或已结束：彻底释放
    room.seats[me.seat] = None
    room.players.pop(me.token, None)
    if not room.players:
        rooms.pop(room_id, None)
        return {"ok": True, "seat_held": False, "room_removed": True}
    return {"ok": True, "seat_held": False}


@app.post("/api/rooms/{room_id}/close")
def close_room(room_id: str) -> dict[str, Any]:
    """手动关闭房间。本服务无用户系统，任何能访问服务的人都可以关。

    正在打的桌需要带该桌玩家的 token 才能关，避免误杀。
    """
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, "房间不存在")
    active = room.idle_seconds() < 30 and room.started()
    if active:
        raise HTTPException(400, "对桌正在使用中，暂不自动清理；如确要关闭请稍后再试")
    del rooms[room_id]
    return {"ok": True, "closed": room_id}


@app.post("/api/rooms/{room_id}/join")
def join_room(room_id: str, body: JoinRoom) -> dict[str, Any]:
    room = get_room(room_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    # 同名重入：若该名字的玩家主动离席过，换新 token 回到原座位
    for old_token, p in list(room.players.items()):
        if p.name == name:
            if p.left:
                token = secrets.token_hex(16)
                p.left = False
                p.token = token
                del room.players[old_token]
                room.players[token] = p
                room.seats[p.seat] = p
                return {"token": token, "seat": p.seat, "room_id": room_id,
                        "resumed": True}
            raise HTTPException(400, "该名字已被使用")

    free = next((i for i, s in enumerate(room.seats) if s is None), None)
    if free is None:
        raise HTTPException(400, "座位已满")
    token = secrets.token_hex(16)
    p = Player(name=name, token=token, seat=free)
    room.players[token] = p
    room.seats[free] = p
    return {"token": token, "seat": free, "room_id": room_id}


@app.post("/api/rooms/{room_id}/start")
def start_match(
    room_id: str,
    authorization: Optional[str] = Header(default=None),
    body: Optional[StartBody] = None,
) -> dict[str, Any]:
    room = get_room(room_id)
    auth(room, authorization)
    if not room.full():
        raise HTTPException(400, "需要 4 名玩家才能开局")

    if room.started() and room.game.phase == "round_end":
        # 连局：进贡并开始下一局
        try:
            room.game.start(first_leader=room.first_leader)
        except IllegalMove as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "round_no": room.game.round_no, "match": False}

    if room.started() and room.game.phase in ("play", "return_tribute"):
        # 多个客户端同时看到 round_end 去开局：第一个成功，其余幂等返回
        if body is not None and body.after_round is not None \
                and room.game.round_no > body.after_round:
            return {"ok": True, "already_started": True, "round_no": room.game.round_no}
        raise HTTPException(400, "对局进行中")

    # 新开一桌（含 match_end 后重开）
    names = [p.name if p else f"座位{i}" for i, p in enumerate(room.seats)]
    room.game = Game(player_names=names)
    try:
        room.game.start(first_leader=room.first_leader)
    except IllegalMove as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "round_no": room.game.round_no, "match": True}


@app.get("/api/rooms/{room_id}/state")
def get_state(
    room_id: str, authorization: Optional[str] = Header(default=None)
) -> dict[str, Any]:
    room = get_room(room_id)
    me = auth(room, authorization)
    return state_for(room, me)


@app.get("/api/rooms/{room_id}/legal-moves")
def get_legal_moves(
    room_id: str, authorization: Optional[str] = Header(default=None)
) -> list[dict[str, Any]]:
    room = get_room(room_id)
    me = auth(room, authorization)
    if not room.started():
        return []
    g = room.game
    assert g is not None
    moves = [
        {
            "card_ids": list(m.cards),
            "kind": m.kind,
            "kind_label": KIND_LABEL.get(m.kind, m.kind),
            "size": m.size,
            "cards": [card_json(c) for c in m.cards],
        }
        for m in g.legal_moves(me.seat)
    ]
    if g.phase == "play" and g.current == me.seat and g.table is not None:
        moves.append({"pass": True, "card_ids": [], "kind": "pass", "kind_label": "过牌"})
    return moves


@app.post("/api/rooms/{room_id}/play")
def play_cards(
    room_id: str, body: PlayBody, authorization: Optional[str] = Header(default=None)
) -> dict[str, Any]:
    room = get_room(room_id)
    me = auth(room, authorization)
    if not room.started():
        raise HTTPException(400, "对局未开始")
    g = room.game
    assert g is not None
    try:
        combo = g.play(me.seat, body.cards)
    except IllegalMove as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "played": combo_json(combo, me.seat), "state": state_for(room, me)}


@app.post("/api/rooms/{room_id}/pass")
def pass_turn(
    room_id: str, authorization: Optional[str] = Header(default=None)
) -> dict[str, Any]:
    room = get_room(room_id)
    me = auth(room, authorization)
    if not room.started():
        raise HTTPException(400, "对局未开始")
    g = room.game
    assert g is not None
    try:
        g.pass_(me.seat)
    except IllegalMove as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "state": state_for(room, me)}


@app.post("/api/rooms/{room_id}/return-tribute")
def return_tribute(
    room_id: str, body: ReturnTribute, authorization: Optional[str] = Header(default=None)
) -> dict[str, Any]:
    room = get_room(room_id)
    me = auth(room, authorization)
    if not room.started():
        raise HTTPException(400, "对局未开始")
    g = room.game
    assert g is not None
    try:
        g.return_tribute(me.seat, body.card)
    except IllegalMove as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "state": state_for(room, me)}


@app.get("/api/rooms/{room_id}/events")
def get_events(
    room_id: str, authorization: Optional[str] = Header(default=None), since: int = 0
) -> dict[str, Any]:
    """事件流（供后续复盘/分析使用）。"""
    room = get_room(room_id)
    auth(room, authorization)
    if not room.started():
        return {"events": []}
    g = room.game
    assert g is not None
    return {"events": g.events[since:], "total": len(g.events)}


# ---------------------------------------------------------------- 静态资源

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR + "/index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
