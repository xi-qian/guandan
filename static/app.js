/* 掼蛋前端：纯 HTTP 轮询，无框架。 */
(() => {
  const $ = (id) => document.getElementById(id);

  const state = {
    token: localStorage.getItem("gd_token") || "",
    roomId: localStorage.getItem("gd_room") || "",
    name: localStorage.getItem("gd_name") || "",
    view: null,
    selected: new Set(),
    lastEventCount: 0,
    timer: null,
    hintIndex: 0,
    lastMoves: [],
  };

  // ---------------------------------------------------------------- API

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const res = await fetch(path, { ...options, headers });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      /* ignore */
    }
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || `请求失败 ${res.status}`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  // ---------------------------------------------------------------- 渲染

  const SUIT_COLOR = { "♥": "red", "♦": "red", "♠": "", "♣": "" };

  function cardEl(card, { mini = false, selected = false, placeholder = false } = {}) {
    const div = document.createElement("div");
    if (placeholder) {
      div.className = "card placeholder mini";
      div.textContent = "?";
      return div;
    }
    const red = card.suit ? SUIT_COLOR[card.suit] === "red" : false;
    const joker = card.suit == null;
    div.className = `card${mini ? " mini" : ""}${red ? " red" : ""}${joker ? " joker" : ""}${selected ? " selected" : ""}`;
    div.dataset.id = card.id;
    div.innerHTML = `<div class="rank">${card.label}</div><div class="suit">${card.suit ?? (card.rank === "BJ" ? "★" : "☆")}</div>`;
    return div;
  }

  function toast(msg, ms = 2200) {
    const el = $("toast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => (el.hidden = true), ms);
  }

  function renderJoin() {
    $("screen-join").hidden = false;
    $("screen-table").hidden = true;
    if (state.name) $("input-name").value = state.name;
    if (state.roomId) $("input-room").value = state.roomId;
    refreshRooms();
  }

  function renderTable(v) {
    state.view = v;
    $("screen-join").hidden = true;
    $("screen-table").hidden = false;

    $("table-room-name").textContent = v.room_name || "掼蛋桌";
    $("table-room-id").textContent = ` · 房间 ${v.room_id}`;
    $("table-round").textContent = v.round_no ? `第 ${v.round_no} 局` : "等待开始";

    // 级数
    const lv = $("table-levels");
    lv.innerHTML = "";
    if (v.level_rank) {
      const chip = document.createElement("div");
      chip.className = "level-chip mine";
      chip.textContent = `级牌 ${v.level_rank}`;
      lv.appendChild(chip);
    }
    (v.levels || []).forEach((t) => {
      const chip = document.createElement("div");
      chip.className = "level-chip" + (t.team === v.you?.team ? " mine" : "");
      chip.textContent = `队伍${t.team === 0 ? "①" : "②"} 打 ${t.level}`;
      lv.appendChild(chip);
    });

    // 座位
    const seats = $("seats");
    seats.innerHTML = "";
    const players = v.players || (v.seats || []).map((s) => ({
      seat: s.seat,
      name: s.name || `座位${s.seat}`,
      hand_size: null,
      is_you: s.seat === v.you?.seat,
    }));
    players.forEach((p) => {
      const div = document.createElement("div");
      div.className = "seat";
      if (v.current_seat === p.seat && v.phase === "play") div.classList.add("active");
      if (p.finished_rank) div.classList.add("done");
      const badges = [];
      if (p.is_you) badges.push("你");
      if (p.finished_rank) badges.push(`第${p.finished_rank}名`);
      if (p.seat % 2 === v.you?.team) badges.push("队友");
      div.innerHTML = `
        <div class="name">${p.name}${badges.length ? ` <span class="badge">${badges.join(" · ")}</span>` : ""}</div>
        <div class="meta"><span>${p.hand_size == null ? "" : `剩 ${p.hand_size} 张`}</span><span>${p.seat === v.current_seat && v.phase === "play" ? "出牌中" : ""}</span></div>`;
      seats.appendChild(div);
    });

    // 桌面
    const box = $("table-combo");
    box.innerHTML = "";
    if (v.table) {
      v.table.cards.forEach((c) => box.appendChild(cardEl(c, { mini: true })));
      const label = document.createElement("div");
      label.className = "muted";
      label.textContent = ` ${v.table.kind_label} · ${players[v.table.seat]?.name ?? ""}`;
      box.appendChild(label);
    } else {
      box.innerHTML = `<div class="muted">${v.phase === "play" ? "本轮自由出牌" : ""}</div>`;
    }

    // 状态栏
    const status = $("status-line");
    if (v.phase === "waiting") {
      status.textContent = "等待玩家加入（需要 4 人）";
    } else if (v.phase === "match_end") {
      const w = v.match_winner === v.you?.team ? "我方" : "对方";
      status.textContent = `对局结束，${w}获胜！`;
    } else if (v.phase === "round_end") {
      status.textContent = "本局结束，点击「开始 / 下一局」继续";
    } else if (v.phase === "return_tribute") {
      const w = v.tribute?.waiting_seat;
      const who = players[w]?.name ?? "对手";
      status.textContent = w === v.you.seat ? "请还贡" : `还贡中，等待 ${who}…`;
    } else if (v.your_turn) {
      status.textContent = "轮到你出牌";
    } else {
      const cur = players[v.current_seat];
      status.textContent = `等待 ${cur ? cur.name : "对手"} 出牌…`;
    }

    // 进贡：只在「轮到你还贡」时才展开那一排；还完即收起
    const tb = $("tribute-bar");
    const myReturn = v.phase === "return_tribute" && v.tribute
      && v.tribute.waiting_seat === v.you.seat;
    if (myReturn) {
      tb.hidden = false;
      tb.innerHTML = `<strong>请还贡</strong><span class="muted">选一张不大于 10 的非级牌还给 ${players[v.tribute.to_seat]?.name ?? "对手"}</span>`;
      const wrap = document.createElement("div");
      wrap.style.display = "flex";
      wrap.style.gap = "8px";
      wrap.style.flexWrap = "wrap";
      v.tribute.options.forEach((c) => {
        const el = cardEl(c, { mini: true });
        el.addEventListener("click", async () => {
          try {
            await api(`/api/rooms/${state.roomId}/return-tribute`, {
              method: "POST",
              body: JSON.stringify({ card: c.id }),
            });
            state.selected.clear();
            await poll();
          } catch (e) {
            toast(e.message);
          }
        });
        wrap.appendChild(el);
      });
      tb.appendChild(wrap);
    } else {
      // 不管是还完了、还是在等别人还，都不占屏
      tb.hidden = true;
      tb.innerHTML = "";
    }

    // 手牌
    const hand = $("hand");
    hand.innerHTML = "";
    (v.you.hand || []).forEach((c) => {
      const el = cardEl(c, { selected: state.selected.has(c.id) });
      el.addEventListener("click", () => {
        if (state.selected.has(c.id)) state.selected.delete(c.id);
        else state.selected.add(c.id);
        renderTable(v);
      });
      hand.appendChild(el);
    });

    // 按钮
    $("btn-play").disabled = !(v.your_turn && state.selected.size > 0);
    $("btn-pass").hidden = !(v.your_turn && v.can_pass);
    $("btn-hint").hidden = !v.your_turn;
    const canStart =
      v.phase === "waiting"
        ? !!v.can_start
        : v.phase === "round_end" || v.phase === "match_end";
    $("btn-start").hidden = !canStart;
    $("btn-start").textContent =
      v.phase === "waiting" ? "开始对局" : v.phase === "match_end" ? "再来一场" : "下一局";

    // 提示用合法牌
    if (v.your_turn) {
      state.lastMoves = v.legal_moves || [];
    } else {
      state.lastMoves = [];
    }
    if (!v.your_turn) state.hintIndex = 0;
  }

  // ---------------------------------------------------------------- 交互

  async function refreshRooms() {
    try {
      const rooms = await api("/api/rooms");
      const ul = $("room-list");
      ul.innerHTML = "";
      if (!rooms.length) {
        ul.innerHTML = `<li class="muted">暂无房间，点「新建房间」开始</li>`;
        return;
      }
      rooms.forEach((r) => {
        const li = document.createElement("li");
        const idle = r.idle_seconds ?? 0;
        const idleText = idle < 60
          ? "刚刚活跃"
          : idle < 3600
            ? `闲置 ${Math.floor(idle / 60)} 分钟`
            : `闲置 ${Math.floor(idle / 3600)} 小时`;
        li.innerHTML = `<span>${r.name} · ${r.seats_taken}/4 人 · ${r.phase} · <span class="muted">${idleText}</span></span>`;

        const joinBtn = document.createElement("button");
        joinBtn.className = "primary small";
        joinBtn.textContent = "加入";
        joinBtn.addEventListener("click", () => {
          $("input-room").value = r.room_id;
          join();
        });

        const closeBtn = document.createElement("button");
        closeBtn.className = "ghost small";
        closeBtn.textContent = "关闭";
        closeBtn.addEventListener("click", async () => {
          if (!confirm(`关闭房间「${r.name}」？`)) return;
          try {
            await api(`/api/rooms/${r.room_id}/close`, { method: "POST" });
            toast(`已关闭 ${r.room_id}`);
            refreshRooms();
          } catch (e) {
            toast(e.message);
          }
        });

        li.appendChild(joinBtn);
        li.appendChild(closeBtn);
        ul.appendChild(li);
      });
    } catch (e) {
      /* 列表失败不影响主流程 */
    }
  }

  async function join(create = false) {
    const name = $("input-name").value.trim();
    const roomId = $("input-room").value.trim();
    const err = $("join-error");
    err.hidden = true;
    if (!name) {
      err.textContent = "请先填名字";
      err.hidden = false;
      return;
    }
    try {
      let rid = roomId;
      if (create || !rid) {
        const created = await api("/api/rooms", {
          method: "POST",
          body: JSON.stringify({ name: `${name} 的牌桌` }),
        });
        rid = created.room_id;
      }
      const joined = await api(`/api/rooms/${rid}/join`, {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      state.token = joined.token;
      state.roomId = rid;
      state.name = name;
      localStorage.setItem("gd_token", state.token);
      localStorage.setItem("gd_room", rid);
      localStorage.setItem("gd_name", name);
      startPolling();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  }

  async function poll() {
    if (!state.token || !state.roomId) return;
    try {
      const v = await api(`/api/rooms/${state.roomId}/state`);
      renderTable(v);
    } catch (e) {
      if (/token/i.test(e.message)) {
        leave();
        return;
      }
      // 网络抖动：忽略
    }
  }

  function startPolling() {
    stopPolling();
    poll();
    state.timer = setInterval(poll, 1200);
  }

  function stopPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  function leave() {
    stopPolling();
    state.token = "";
    state.roomId = "";
    state.selected.clear();
    localStorage.removeItem("gd_token");
    localStorage.removeItem("gd_room");
    renderJoin();
  }

  async function doPlay() {
    const cards = [...state.selected];
    if (!cards.length) return;
    try {
      await api(`/api/rooms/${state.roomId}/play`, {
        method: "POST",
        body: JSON.stringify({ cards }),
      });
      state.selected.clear();
      state.hintIndex = 0;
      await poll();
    } catch (e) {
      toast(e.message);
    }
  }

  async function doPass() {
    try {
      await api(`/api/rooms/${state.roomId}/pass`, { method: "POST" });
      state.selected.clear();
      await poll();
    } catch (e) {
      toast(e.message);
    }
  }

  function doHint() {
    if (!state.lastMoves.length) {
      toast("没有可出的牌，请过牌");
      return;
    }
    const m = state.lastMoves[state.hintIndex % state.lastMoves.length];
    state.hintIndex += 1;
    state.selected = new Set(m.card_ids || []);
    renderTable(state.view);
  }

  async function doStart() {
    try {
      await api(`/api/rooms/${state.roomId}/start`, { method: "POST" });
      state.selected.clear();
      await poll();
    } catch (e) {
      toast(e.message);
    }
  }

  // ---------------------------------------------------------------- 绑定

  $("btn-join").addEventListener("click", () => join(false));
  $("btn-create").addEventListener("click", () => join(true));
  $("btn-refresh-rooms").addEventListener("click", refreshRooms);
  $("btn-leave").addEventListener("click", leave);
  $("btn-play").addEventListener("click", doPlay);
  $("btn-pass").addEventListener("click", doPass);
  $("btn-hint").addEventListener("click", doHint);
  $("btn-start").addEventListener("click", doStart);

  // 回车提交
  for (const id of ["input-name", "input-room"]) {
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") join(false);
    });
  }

  // 断线重连
  if (state.token && state.roomId) {
    startPolling();
  } else {
    renderJoin();
  }
})();
