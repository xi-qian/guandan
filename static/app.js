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
    sortMode: localStorage.getItem("gd_sort") || "rank",
    // 手牌编排：有序 item 列表，每项是单张或一组
    order: [],
    // 排序前的手动顺序快照，「还原」用
    savedOrder: null,
    cardById: {},
  };

  const SORT_LABEL = { rank: "点数", suit: "花色", group: "牌型" };

  const SUIT_ORDER = { "♠": 0, "♥": 1, "♣": 2, "♦": 3 };
  const RANK_VALUE = {};
  ["2","3","4","5","6","7","8","9","10","J","Q","K","A"].forEach((r, i) => RANK_VALUE[r] = i + 2);
  RANK_VALUE["SJ"] = 15; RANK_VALUE["BJ"] = 16;

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

  let lastHistSig = "";

  function renderHistory(v, players) {
    const box = $("history");
    const count = $("history-count");
    if (!box) return;
    const hist = v.round_plays || [];
    const sig = hist.map((p) => `${p.seat}:${p.passed ? 0 : (p.cards || []).length}`).join(",")
      + "#" + (v.table ? v.table.cards.map((c) => c.id).join(",") : "");
    if (sig === lastHistSig) return;
    lastHistSig = sig;
    const plays = v.round_plays || [];
    box.innerHTML = "";
    if (count) count.textContent = plays.length ? `共 ${plays.length} 手` : "";
    if (!plays.length) {
      box.innerHTML = `<div class="muted">本局还没有出牌</div>`;
      return;
    }
    plays.forEach((p, i) => {
      const row = document.createElement("div");
      row.className = "row" + (p.seat === v.you?.seat ? " mine" : "") + (p.passed ? " pass-row" : "");
      const who = players[p.seat]?.name ?? `座位${p.seat}`;
      const cardsWrap = document.createElement("div");
      cardsWrap.className = "cards";
      (p.cards || []).forEach((c) => cardsWrap.appendChild(cardEl(c, { mini: true })));
      const label = p.passed ? "" : `${p.kind_label || ""} ${p.size || ""}张`;
      row.innerHTML = `<span class="who">${who}</span>`;
      row.appendChild(cardsWrap);
      const lab = document.createElement("span");
      lab.className = "label";
      lab.textContent = label;
      row.appendChild(lab);
      box.appendChild(row);

      // 在本轮最后一手后标出「当前桌面」
      if (i === plays.length - 1 && v.table) {
        const mark = document.createElement("div");
        mark.className = "turn-mark";
        mark.textContent = "── 当前桌面 ──";
        box.appendChild(mark);
      }
    });
    box.scrollTop = box.scrollHeight;
  }


  // ---------------------------------------------------------------- 手牌编排
  const DRAG = { active: false, item: null, index: -1, ghost: null, ph: null,
                 holder: null, startX: 0, startY: 0 };

  // item = {kind:'single', id} | {kind:'group', ids:[...]}

  function cloneItem(it) {
    return it.kind === "group" ? { kind: "group", ids: [...it.ids] }
                              : { kind: "single", id: it.id };
  }

  function restoreManualOrder() {
    if (!state.savedOrder) {
      toast("没有可还原的手动顺序");
      return;
    }
    state.order = state.savedOrder.map(cloneItem);
    renderHand(true);
    toast("已还原手动顺序");
  }

  function flattenOrder(order) {
    return (order || state.order).flatMap((it) => (it.kind === "group" ? it.ids : [it.id]));
  }

  function reconcileOrder(hand) {
    const live = new Set(hand.map((c) => c.id));
    state.cardById = {};
    hand.forEach((c) => (state.cardById[c.id] = c));

    const next = [];
    for (const it of state.order) {
      if (it.kind === "single") {
        if (live.has(it.id)) next.push(it);
      } else {
        const ids = it.ids.filter((id) => live.has(id));
        if (ids.length >= 2) next.push({ kind: "group", ids });
        else if (ids.length === 1) next.push({ kind: "single", id: ids[0] });
      }
    }
    const present = new Set(flattenOrder(next));
    for (const c of hand) {
      if (!present.has(c.id)) next.push({ kind: "single", id: c.id });
    }
    state.order = next;

    // 快照也要跟手牌对齐，否则还原会带回已出掉的牌
    if (state.savedOrder) {
      const live2 = live;
      const snap = [];
      for (const it of state.savedOrder) {
        if (it.kind === "single") {
          if (live2.has(it.id)) snap.push(it);
        } else {
          const ids = it.ids.filter((id) => live2.has(id));
          if (ids.length >= 2) snap.push({ kind: "group", ids });
          else if (ids.length === 1) snap.push({ kind: "single", id: ids[0] });
        }
      }
      const inSnap = new Set(flattenOrder(snap));
      for (const c of hand) if (!inSnap.has(c.id)) snap.push({ kind: "single", id: c.id });
      state.savedOrder = snap;
    }
  }

  function sortByMode(items) {
    const RANK_VALUE = {};
    ["2","3","4","5","6","7","8","9","10","J","Q","K","A"].forEach((r, i) => (RANK_VALUE[r] = i + 2));
    RANK_VALUE["SJ"] = 15; RANK_VALUE["BJ"] = 16;
    const SUIT_ORDER = { "♠": 0, "♥": 1, "♣": 2, "♦": 3 };
    const keyOf = (id) => {
      const c = state.cardById[id] || { rank: "2", suit: null, id };
      return [RANK_VALUE[c.rank] ?? 0, SUIT_ORDER[c.suit] ?? 9, c.id];
    };
    const cmp = (a, b) => {
      const ka = keyOf(a), kb = keyOf(b);
      return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2];
    };
    const out = items.map((it) =>
      it.kind === "group"
        ? { kind: "group", ids: [...it.ids].sort(cmp) }
        : { kind: "single", id: it.id }
    );
    if (state.sortMode === "group") {
      const cnt = {};
      flattenOrder(out).forEach((id) => {
        const c = state.cardById[id];
        if (c) cnt[c.rank] = (cnt[c.rank] || 0) + 1;
      });
      const headKey = (it) => {
        const c = state.cardById[it.kind === "group" ? it.ids[0] : it.id];
        return [-(cnt[c?.rank] || 0), RANK_VALUE[c?.rank] ?? 0, SUIT_ORDER[c?.suit] ?? 9];
      };
      out.sort((a, b) => {
        const ka = headKey(a), kb = headKey(b);
        return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2];
      });
    } else if (state.sortMode === "rank") {
      out.sort((a, b) => cmp(a.kind === "group" ? a.ids[0] : a.id, b.kind === "group" ? b.ids[0] : b.id));
    }
    return out;
  }

  function groupSelected() {
    const sel = [...state.selected];
    if (sel.length < 2) {
      toast("至少选两张才能组合");
      return;
    }
    const want = new Set(sel);
    const next = [];
    let placed = false;
    for (const it of state.order) {
      const ids = (it.kind === "group" ? it.ids : [it.id]).filter((id) => !want.has(id));
      if (ids.length === 0) continue;              // 整个被抽走
      if (ids.length === 1) next.push({ kind: "single", id: ids[0] });
      else next.push({ kind: "group", ids });
      if (!placed && (it.kind === "group" ? it.ids : [it.id]).some((id) => want.has(id))) {
        next.push({ kind: "group", ids: sel });
        placed = true;
      }
    }
    if (!placed) next.push({ kind: "group", ids: sel });
    state.order = next;
    state.savedOrder = null;      // 手动改动后快照作废
    state.selected.clear();
    toast(`已组合 ${sel.length} 张，可整组拖动`);
    renderHand(true);
  }

  function ungroupSelected() {
    const sel = new Set(state.selected);
    const next = [];
    let changed = 0;
    for (const it of state.order) {
      if (it.kind === "group" && it.ids.some((id) => sel.has(id))) {
        it.ids.forEach((id) => next.push({ kind: "single", id }));
        changed++;
      } else {
        next.push(it);
      }
    }
    if (!changed) {
      toast("选中的牌不在组合里");
      return;
    }
    state.order = next;
    state.savedOrder = null;
    state.selected.clear();
    toast(`已拆开 ${changed} 组`);
    renderHand(true);
  }


  let lastHandSig = "";

  function handSignature() {
    return state.order.map((it) =>
      it.kind === "group" ? `G${it.ids.join(",")}` : `S${it.id}`
    ).join("|") + "#" + [...state.selected].sort().join(",");
  }

  function renderHand(force) {
    const hand = $("hand");
    if (!hand) return;
    // 拖动中不重绘，否则轮询会把拖到一半的牌打乱
    if (DRAG.active || DRAG.item) return;
    // 内容没变就不重建 DOM——每次都重建会闪且拖慢响应
    const sig = handSignature();
    if (!force && sig === lastHandSig) {
      updateHandButtons();
      return;
    }
    lastHandSig = sig;
    hand.innerHTML = "";
    state.order.forEach((it, idx) => {
      const holder = document.createElement("div");
      holder.dataset.index = String(idx);
      if (it.kind === "group") {
        holder.className = "group";
        it.ids.forEach((id) => {
          const c = state.cardById[id];
          if (!c) return;
          holder.appendChild(cardEl(c, { selected: state.selected.has(id) }));
        });
        const tag = document.createElement("span");
        tag.className = "group-tag";
        tag.textContent = `${it.ids.length}张`;
        holder.appendChild(tag);
      } else {
        holder.className = "single";
        const c = state.cardById[it.id];
        if (c) holder.appendChild(cardEl(c, { selected: state.selected.has(it.id) }));
      }
      attachDrag(holder, it);
      hand.appendChild(holder);
    });
    updateHandButtons();
  }

  function updateHandButtons() {
    const selN = state.selected.size;
    const selInGroup = state.order.some(
      (it) => it.kind === "group" && it.ids.some((id) => state.selected.has(id))
    );
    const bg = $("btn-group"), bu = $("btn-ungroup"), br = $("btn-restore");
    if (bg) bg.hidden = selN < 2;
    if (bu) bu.hidden = !selInGroup;
    if (br) br.hidden = !state.savedOrder;
  }

  // ---- 指针拖动（桌面鼠标 + 手机触屏通用）
  function attachDrag(holder, item) {
    holder.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return;
      DRAG.item = item;
      DRAG.index = Number(holder.dataset.index);
      DRAG.startX = e.clientX;
      DRAG.startY = e.clientY;
      DRAG.holder = holder;
      DRAG.active = false;
      holder.setPointerCapture?.(e.pointerId);
    });

    holder.addEventListener("pointermove", (e) => {
      if (!DRAG.item) return;
      const dx = e.clientX - DRAG.startX;
      const dy = e.clientY - DRAG.startY;
      if (!DRAG.active) {
        if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
        startDrag(e);
      }
      moveGhost(e);
      updatePlaceholder(e);
    });

    const finish = (e) => {
      if (DRAG.active) {
        commitDrop();
      } else if (DRAG.item) {
        // 未达拖动阈值 → 视为点选
        const ids = item.kind === "group" ? item.ids : [item.id];
        const allIn = ids.every((id) => state.selected.has(id));
        ids.forEach((id) => (allIn ? state.selected.delete(id) : state.selected.add(id)));
        renderHand(true);
        const v = state.view;
        if (v) {
          $("btn-play").disabled = !(v.your_turn && state.selected.size > 0);
        }
      }
      DRAG.item = null;
      DRAG.active = false;
    };
    holder.addEventListener("pointerup", finish);
    holder.addEventListener("pointercancel", finish);
  }

  function startDrag(e) {
    DRAG.active = true;
    const ghost = DRAG.holder.cloneNode(true);
    ghost.className = "drag-ghost " + (DRAG.item.kind === "group" ? "group" : "single");
    ghost.style.width = DRAG.holder.offsetWidth + "px";
    document.body.appendChild(ghost);
    DRAG.ghost = ghost;
    DRAG.holder.classList.add("item-dragging");
    const ph = document.createElement("div");
    ph.className = "drag-placeholder";
    DRAG.ph = ph;
    moveGhost(e);
  }

  function moveGhost(e) {
    if (!DRAG.ghost) return;
    DRAG.ghost.style.left = e.clientX + "px";
    DRAG.ghost.style.top = e.clientY + "px";
  }

  function updatePlaceholder(e) {
    const hand = $("hand");
    if (!hand || !DRAG.ph) return;
    const items = [...hand.children].filter((el) => el !== DRAG.ph);
    let target = null;
    for (const el of items) {
      const r = el.getBoundingClientRect();
      if (e.clientX < r.left + r.width / 2) { target = el; break; }
    }
    if (target) hand.insertBefore(DRAG.ph, target);
    else hand.appendChild(DRAG.ph);
  }

  function commitDrop() {
    const hand = $("hand");
    if (!hand || !DRAG.ph) return cleanupDrag();
    const items = [...hand.children];
    const phIndex = items.indexOf(DRAG.ph);
    const order = [...state.order];
    order.splice(DRAG.index, 1);
    // 占位所在的新下标（去掉自己后）
    const cardsBefore = items.slice(0, phIndex).filter((el) => el !== DRAG.holder).length;
    const insertAt = Math.max(0, Math.min(order.length, cardsBefore));
    order.splice(insertAt, 0, DRAG.item);
    state.order = order;
    state.savedOrder = null;      // 手动拖动后快照作废
    cleanupDrag();
    renderHand(true);
  }

  function cleanupDrag() {
    DRAG.ghost?.remove();
    DRAG.ph?.remove();
    DRAG.holder?.classList.remove("item-dragging");
    DRAG.ghost = DRAG.ph = DRAG.holder = DRAG.item = null;
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

    // 本轮出牌记录
    renderHistory(v, players);

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

    // 手牌（按本地编排顺序渲染）
    reconcileOrder(v.you.hand || []);
    renderHand();

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
      rooms.sort((a, b) => (a.idle_seconds ?? 0) - (b.idle_seconds ?? 0));
      rooms.forEach((r) => {
        const li = document.createElement("li");
        const idle = r.idle_seconds ?? 0;
        const idleText = idle < 60
          ? "刚刚活跃"
          : idle < 3600
            ? `闲置 ${Math.floor(idle / 60)} 分钟`
            : `闲置 ${Math.floor(idle / 3600)} 小时`;
        const stale = idle >= 60;
        if (stale) li.classList.add("stale");
        const liveTag = stale
          ? `<span class="tag-stale">${idleText}</span>`
          : `<span class="tag-live">● 活跃</span>`;
        li.innerHTML = `<span>${r.name} · ${r.seats_taken}/4 人 · ${r.phase} ${liveTag}</span>`;

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
      if (joined.resumed) toast("已回到原来的座位");
      localStorage.setItem("gd_token", state.token);
      localStorage.setItem("gd_room", rid);
      localStorage.setItem("gd_name", name);
      startPolling();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  }

  let pollInFlight = false;

  async function poll() {
    if (!state.token || !state.roomId) return;
    // 去重：上一次还没回来就跳过，避免并发堆叠请求与重绘
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      const v = await api(`/api/rooms/${state.roomId}/state`);
      renderTable(v);
    } catch (e) {
      if (/token/i.test(e.message)) {
        leave();
        return;
      }
      // 网络抖动：忽略
    } finally {
      pollInFlight = false;
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
    // 先告诉服务端离席，否则座位和名字一直占着，进不去
    $("btn-sort").textContent = `排序：${SORT_LABEL[state.sortMode]}`;

  if (state.token && state.roomId) {
      api(`/api/rooms/${state.roomId}/leave`, { method: "POST" }).catch(() => {});
    }
    stopPolling();
    state.token = "";
    state.roomId = "";
    state.selected.clear();
    localStorage.removeItem("gd_token");
    localStorage.removeItem("gd_room");
    renderJoin();
  }

  async function submitPlay(cards, asKind, asMain) {
    const body = { cards };
    if (asKind) {
      body.as_kind = asKind;
      if (asMain != null) body.as_main = asMain;
    }
    await api(`/api/rooms/${state.roomId}/play`, { method: "POST", body: JSON.stringify(body) });
    state.selected.clear();
    state.hintIndex = 0;
    await poll();
  }

  function showInterpretPicker(list, onPick) {
    // 同一批牌有多种打法（百搭可变），让玩家选
    const overlay = document.createElement("div");
    overlay.className = "picker-overlay";
    const box = document.createElement("div");
    box.className = "picker-box";
    box.innerHTML = `<h3>这批牌有 ${list.length} 种打法</h3>
      <p class="muted">红桃级牌是百搭，可以当成不同牌。选你要出的那种：</p>`;
    list.forEach((it, i) => {
      const btn = document.createElement("button");
      btn.className = "picker-item";
      const mainTxt = it.main ? ` · 主点 ${Number(it.main).toFixed(it.main % 1 ? 1 : 0)}` : "";
      btn.innerHTML = `<strong>${it.kind_label}</strong><span class="muted">${it.size}张${mainTxt}</span>`;
      btn.addEventListener("click", () => {
        overlay.remove();
        onPick(it);
      });
      box.appendChild(btn);
    });
    const cancel = document.createElement("button");
    cancel.className = "ghost";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => overlay.remove());
    box.appendChild(cancel);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  }

  async function doPlay() {
    const cards = [...state.selected];
    if (!cards.length) return;
    try {
      // 百搭可能有多种用法：先问服务端有几种解释
      const opts = await api(`/api/rooms/${state.roomId}/interpret`, {
        method: "POST",
        body: JSON.stringify({ cards }),
      });
      if (!opts || opts.length === 0) {
        toast("这不是合法牌型");
        return;
      }
      if (opts.length === 1) {
        await submitPlay(cards, opts[0].kind, opts[0].main);
        return;
      }
      showInterpretPicker(opts, (it) => {
        submitPlay(cards, it.kind, it.main).catch((e) => toast(e.message));
      });
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
    renderHand();
    const v = state.view;
    if (v) $("btn-play").disabled = !(v.your_turn && state.selected.size > 0);
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
  $("btn-group").addEventListener("click", groupSelected);
  $("btn-restore").addEventListener("click", restoreManualOrder);
  $("btn-ungroup").addEventListener("click", ungroupSelected);
  $("btn-sort").addEventListener("click", () => {
    const modes = ["rank", "suit", "group"];
    state.sortMode = modes[(modes.indexOf(state.sortMode) + 1) % modes.length];
    localStorage.setItem("gd_sort", state.sortMode);
    $("btn-sort").textContent = `排序：${SORT_LABEL[state.sortMode]}`;
    // 存一份手动顺序，「还原」按钮可切回
    if (!state.savedOrder) state.savedOrder = state.order.map(cloneItem);
    state.order = sortByMode(state.order);
    renderHand(true);
    toast("已排序，点「还原手动顺序」可切回");
  });
  $("btn-start").addEventListener("click", doStart);

  // 回车提交
  for (const id of ["input-name", "input-room"]) {
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") join(false);
    });
  }

  // 断线重连
  $("btn-sort").textContent = `排序：${SORT_LABEL[state.sortMode]}`;

  if (state.token && state.roomId) {
    startPolling();
  } else {
    renderJoin();
  }
})();
