(() => {
  const $ = (sel) => document.querySelector(sel);
  const ABN = window.ABILITY_NAMES;
  const SKILLS = window.SKILLS;
  const SPELLS = window.SPELLS || {};
  const RACES = window.RACES || {};
  const TALENTS = window.TALENTS || {};
  const CLASS_META = window.CLASS_META || {};
  const FEATURES = window.FEATURES || {};
  const CONDITIONS = window.CONDITIONS || {};
  const ITEMS = window.ITEMS || {};
  const MONSTERS = window.MONSTERS || {};
  const st = {
    participants: [],
    chars: {},
    combat: null,
    aiMode: true,
    dmSid: null,
    meSid: window.SID,
    logRendered: false,
    sheetCid: null,
    checksReady: false,
    debug: false,
    campaign: null,
    clock: "",
    clockMinutes: 0,
    dmBrief: "",
    questFilter: localStorage.getItem("weavlum-quest-filter") || "in_progress",
  };
  let ws = null;
  let toastTimer = null;

  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  // 备注里的字面量 \n（用户手动输入）转成真实换行，展示时按多行处理
  const noteText = (raw) => String(raw || "").replace(/\\r\\n|\\n/g, "\n");
  // 固定掷骰：存在玩家档案里（按昵称持久化）；未设置时回退到默认三条
  const DEFAULT_QUICK_ROLLS = [
    {label: "d20", expr: "1d20"},
    {label: "2d6+3", expr: "2d6+3"},
    {label: "1d8+3", expr: "1d8+3"},
  ];
  let quickRolls = Array.isArray(window.QUICK_ROLLS) ? window.QUICK_ROLLS : DEFAULT_QUICK_ROLLS;
  const fmtMod = (m) => (m >= 0 ? `+${m}` : `${m}`);
  const skillTip = (id) => ((SKILLS[id] && SKILLS[id].desc) || "");
  const spellTip = (s) => {
    const level = s.level === 0 ? "戏法" : s.level + " 环";
    const lines = [
      `${s.name}（${s.name_en}）· ${level} · ${s.school}`,
      `施法时间：${s.casting_time} · 射程：${s.range}`,
      `成分：${(s.components || []).join("、")}${s.material ? "（" + s.material + "）" : ""}`,
      `持续：${s.duration}${s.concentration ? " · 需专注" : ""}${s.ritual ? " · 可仪式" : ""}`,
    ];
    if (s.damage) lines.push(`伤害/数值：${s.damage}`);
    if (s.desc_zh || s.desc) lines.push("", s.desc_zh || s.desc);
    if (s.higher_zh || s.higher_level) lines.push("", "升环施法：" + (s.higher_zh || s.higher_level));
    return lines.join("\n");
  };
  const raceTip = (cid) => {
    const r = RACES[cid];
    if (!r) return "";
    return [`${r.name} · 速度 ${r.speed} 尺`, "", r.traits_desc || r.traits || ""].join("\n");
  };
  const adaptationTip = (c) => {
    const race = RACES[c.race_id];
    const adapt = ((race && race.adaptations) || []).find((a) => a.name === c.adaptation);
    if (!adapt) return "";
    const ability = c.adaptation_spell_ability
      ? `（次级幻影施法属性：${{int: "智力", wis: "感知", cha: "魅力"}[c.adaptation_spell_ability]}）`
      : "";
    return [`自然适应：${adapt.name}${ability}`, "", adapt.desc || ""].join("\n");
  };
  const talentTip = (tid) => {
    const t = TALENTS[tid];
    if (!t) return "";
    const lines = [`${t.name}（${t.name_en}）`];
    if (t.prerequisite_zh) lines.push(`先决：${t.prerequisite_zh}`);
    if (t.desc_zh) lines.push("", t.desc_zh);
    const benefits = Array.isArray(t.benefits_zh) ? t.benefits_zh : [];
    if (benefits.length) lines.push("", ...benefits.map((b) => "· " + b));
    return lines.join("\n");
  };
  const moneyFmt = (gp) => {
    let cp = Math.round((Number(gp) || 0) * 100);
    const g = Math.floor(cp / 100); cp %= 100;
    const sp = Math.floor(cp / 10); const copper = cp % 10;
    const parts = [];
    if (g) parts.push(`${g} 金`);
    if (sp) parts.push(`${sp} 银`);
    if (copper || !parts.length) parts.push(`${copper} 铜`);
    return parts.join(" ");
  };
  const coinsText = (coins) => {
    const rows = [["pp", "铂"], ["gp", "金"], ["sp", "银"], ["cp", "铜"]];
    const parts = rows.filter(([k]) => (coins || {})[k]).map(([k, label]) => `${coins[k]} ${label}`);
    return parts.join(" ") || "0 铜";
  };
  const colorOfNick = (nick) => {
    const p = st.participants.find((x) => x.nickname === nick);
    return (p && p.color) || "";
  };
  const monsterLookup = (name) => {
    const clean = String(name || "").replace(/\s*\d+$/, "").trim();
    if (MONSTERS[clean]) return MONSTERS[clean];
    const list = Object.values(MONSTERS);
    return list.find((m) => m.name === clean) || list.find((m) => clean && clean.startsWith(m.name)) || null;
  };
  const monsterTip = (m) => {
    const lines = [`${m.name}（CR ${m.cr_label || m.cr} · XP ${m.xp}）`];
    const init = Number(m.init_bonus) || 0;
    lines.push(`AC ${m.ac} · HP ${m.hp} · 速度 ${m.speed} 尺 · 先攻 ${init >= 0 ? "+" : ""}${init}`);
    const a = m.attack || {};
    if (a.name) lines.push(`攻击：${a.name} +${a.to_hit}，伤害 ${a.damage}`);
    if (m.traits_zh && m.traits_zh.length) {
      lines.push("");
      let budget = 1400;
      for (const t of m.traits_zh) {
        const line = String(t).length > 200 ? String(t).slice(0, 200) + "…" : String(t);
        if (budget - line.length < 0) {
          lines.push("……（更多细节见 GM 查询）");
          break;
        }
        lines.push(line);
        budget -= line.length;
      }
    }
    if (m.desc) { lines.push(""); lines.push(m.desc); }
    return lines.join("\n");
  };
  const tokenTip = (c) => {
    if (c.kind === "monster") {
      const m = monsterLookup(c.name);
      if (m) return monsterTip(m);
      return `${c.name}\nHP ${c.hp}/${c.hp_max}${c.cr_label ? ` · CR ${c.cr_label}` : ""}`;
    }
    if (c.kind === "pc") {
      const ch = st.chars[c.id];
      const owner = ch && ch.owner_nick ? ` · 玩家 ${ch.owner_nick}` : "";
      const ac = c.ac || (ch ? ch.ac : "");
      return `${c.name}${owner}\nHP ${c.hp}/${c.hp_max}${ac ? ` · AC ${ac}` : ""} · 先攻 ${c.init}`;
    }
    return `${c.name}（同伴）\nHP ${c.hp}/${c.hp_max} · 先攻 ${c.init}`;
  };
  const itemTip = (id, name) => {
    const it = id ? ITEMS[id] : null;
    if (!it) return `${name}（自定义物品，无库内说明）`;
    const head = [`${it.name_zh}（${it.name_en}）· ${it.category_zh || ""}${it.rarity_zh ? " · " + it.rarity_zh : ""}`];
    const meta = [];
    if (it.cost_cp != null) meta.push(`价格：${moneyFmt(it.cost_cp / 100)}${it.price_note || ""}`);
    if (it.damage) meta.push(`伤害：${it.damage} ${it.damage_type_zh || ""}${it.versatile_damage ? `（双手 ${it.versatile_damage}）` : ""}`);
    if (it.range_text) meta.push(it.range_text);
    if (it.properties_zh && it.properties_zh.length) meta.push(`属性：${it.properties_zh.join("、")}`);
    if (it.ac_text) meta.push(it.ac_text);
    if (it.str_minimum) meta.push(`力量要求 ${it.str_minimum}`);
    if (it.stealth_disadvantage) meta.push("隐匿劣势");
    if (it.weight != null) meta.push(`重量 ${it.weight} 磅`);
    if (meta.length) head.push(meta.join(" · "));
    if (it.desc_zh) { head.push(""); head.push(it.desc_zh); }
    return head.join("\n");
  };
  function shopOptions() {
    const groups = {};
    Object.entries(ITEMS).forEach(([id, it]) => {
      if (!it.name_zh) return;
      const cat = it.category_zh || "其他";
      (groups[cat] = groups[cat] || []).push([id, it]);
    });
    return Object.entries(groups).map(([cat, list]) => {
      const opts = list
        .sort((a, b) => a[1].name_zh.localeCompare(b[1].name_zh, "zh"))
        .map(([id, it]) => {
          const price = it.cost_cp != null ? `（${moneyFmt(it.cost_cp / 100)}）` : "（无定价）";
          return `<option value="${esc(id)}">${esc(it.name_zh)}${price}</option>`;
        })
        .join("");
      return `<optgroup label="${esc(cat)}">${opts}</optgroup>`;
    }).join("");
  }
  function inventoryHTML(c, canEdit) {
    const items = c.items || [];
    const invRows = items.map((it) => {
      const key = it.id || it.name;
      const rawNote = String(it.note || "");      // 原样保存/定位用（保留字面量 \n）
      const note = noteText(rawNote);             // 仅展示时转成真实换行
      const tip = [itemTip(it.id, it.name), note ? "备注：" + note : ""].filter(Boolean).join("\n\n");
      const ctrls = canEdit
        ? `<button class="btn small" data-inv-note="${esc(key)}" data-old-note="${esc(rawNote)}">注</button><button class="btn small" data-inv-minus="${esc(key)}">−1</button><button class="btn small" data-inv-plus="${esc(key)}">+1</button>${it.id ? `<button class="btn small" data-inv-sell="${esc(key)}">卖 1</button>` : ""}<button class="btn small" data-inv-del="${esc(key)}" data-qty="${it.qty}">移除</button>`
        : "";
      const nameCell = `${esc(it.name)}${note ? `<div class="dim inv-note">${esc(note)}</div>` : ""}`;
      return `<tr><td class="tip" data-tip="${esc(tip)}">${nameCell}</td><td class="inv-qty">${it.qty}</td><td class="inv-controls">${ctrls}</td></tr>`;
    }).join("");
    const coins = c.coins || {};
    const totalCp = (coins.pp || 0) * 1000 + (coins.gp || 0) * 100 + (coins.sp || 0) * 10 + (coins.cp || 0);
    const coinEdit = canEdit
      ? `<p>铂 <input id="inv-coin-pp" type="number" min="0" value="${coins.pp || 0}" style="width:64px"> 金 <input id="inv-coin-gp" type="number" min="0" value="${coins.gp || 0}" style="width:64px"> 银 <input id="inv-coin-sp" type="number" min="0" value="${coins.sp || 0}" style="width:64px"> 铜 <input id="inv-coin-cp" type="number" min="0" value="${coins.cp || 0}" style="width:64px"> <button class="btn small" id="inv-coins-btn">保存</button></p>
        <div class="row">${[
          ["cp", "sp", 10, "10 铜→1 银"],
          ["sp", "cp", 1, "1 银→10 铜"],
          ["sp", "gp", 10, "10 银→1 金"],
          ["gp", "sp", 1, "1 金→10 银"],
          ["gp", "pp", 10, "10 金→1 铂"],
          ["pp", "gp", 1, "1 铂→10 金"],
        ].map(([f, t, n, label]) => `<button class="btn small" data-inv-fx="${f}:${t}:${n}" ${(coins[f] || 0) < n ? "disabled" : ""}>${label}</button>`).join("")}</div>`
      : "";
    const addRow = canEdit ? `
      <div class="row">
        <input id="inv-add-name" list="item-name-list" placeholder="添加物品（自由输入或从库选）" style="flex:1">
        <input id="inv-add-note" placeholder="描述 / 品质（可选）" style="flex:1">
        <input id="inv-add-qty" type="number" value="1" min="1" style="width:64px">
        <button class="btn small" id="inv-add-btn">添加</button>
      </div>
      <p class="dim hidden" id="inv-add-preview" style="white-space: pre-line;"></p>
      <div class="row"><input id="inv-shop-search" placeholder="搜索物品（名称 / 分类）…" style="flex:1"></div>
      <div class="row">
        <select id="inv-shop-item" style="flex:1">${shopOptions()}</select>
        <input id="inv-shop-qty" type="number" value="1" min="1" style="width:64px">
        <button class="btn small" id="inv-buy-btn">买入</button>
      </div>
      <p class="dim hidden" id="inv-shop-preview" style="white-space: pre-line;"></p>` : "";
    const carry = c.carry;
    const carryLine = carry
      ? `<p class="tip" data-tip="物品 ${carry.items_weight} 磅 · 硬币 ${carry.coins_weight} 磅（${carry.coin_count} 枚 · 按 50 枚/磅）">负重：<strong>${carry.weight.toFixed(1)}</strong> / ${carry.capacity} 磅 <span class="dim">（推、拉、举 ${carry.push} 磅 · ${esc(carry.label)}${carry.unknown_count ? ` · ${carry.unknown_count} 件自定义物品未计重` : ""}）</span>${carry.hint ? ` <span style="color:${carry.level === "over" ? "var(--bad)" : "var(--accent)"}">${esc(carry.hint)}</span>` : ""}</p>`
      : "";
    return `<h2>背包（信任制可手调）</h2>`
      + `<p class="tip" data-tip="总值 ${totalCp} 铜（10 铜=1 银 · 10 银=1 金 · 10 金=1 铂）">资产：<strong>${coinsText(coins)}</strong></p>`
      + coinEdit
      + carryLine
      + (invRows ? `<table class="sheet-table inv-table">${invRows}</table>` : '<p class="dim">（背包为空）</p>')
      + addRow;
  }

  function fmtMinutes(minutes) {
    if (minutes < 1) return `${Math.max(1, Math.round(minutes * 10))} 轮`;
    if (minutes < 60) return `${Math.round(minutes)} 分钟`;
    const hours = Math.floor(minutes / 60);
    const mins = Math.round(minutes % 60);
    if (hours < 24) return `${hours} 小时${mins ? " " + mins + " 分钟" : ""}`;
    const days = Math.floor(hours / 24);
    const rest = hours % 24;
    return `${days} 天${rest ? " " + rest + " 小时" : ""}`;
  }
  const me = () => st.participants.find((p) => p.sid === st.meSid) || {};
  const myChar = () => (me().char_id ? st.chars[me().char_id] : null);
  const canDM = () => Boolean(me().is_dm);

  function wsSend(obj) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  }

  function reportClient(event, data) {
    if (!st.debug) return;
    fetch("/api/debug/client", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({event, data}),
    }).catch(() => {});
  }

  function toast(text) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
  }

  function setThinking(on) {
    const el = $("#ai-thinking");
    if (el) el.classList.toggle("hidden", !on);
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      try {
        handle(JSON.parse(ev.data));
      } catch (err) {
        undefined;
      }
    };
    ws.onclose = (ev) => {
      reportClient("ws_closed", {code: ev.code});
      if (ev.code === 4401) {
        location.href = "/";
        return;
      }
      setTimeout(connect, 3000);
    };
  }

  function handle(msg) {
    if (msg.type === "snapshot") applySnapshot(msg.state);
    else if (msg.type === "log") appendLog(msg.entry);
    else if (msg.type === "ai_state") setThinking(Boolean(msg.thinking));
    else if (msg.type === "dm_brief") {
      st.dmBrief = msg.text || "";
      renderDMPanel();
    } else if (msg.type === "error") {
      toast(msg.error || "操作失败");
      renderBoard();
    }
  }

  function applySnapshot(state) {
    st.participants = state.participants || [];
    st.chars = state.characters || {};
    st.combat = state.combat || null;
    st.aiMode = state.ai_mode;
    st.dmSid = state.dm_sid;
    st.debug = Boolean(state.debug);
    st.campaign = state.campaign || null;
    st.clock = state.clock || "";
    st.clockMinutes = state.clock_minutes || 0;
    setThinking(Boolean(state.ai_thinking));
    renderParty();
    renderCombat();
    renderBoard();
    renderQuests();
    renderMeta();
    renderDMPanel();
    if (!st.checksReady) {
      $("#check-ability").innerHTML = Object.entries(ABN)
        .map(([key, label]) => `<option value="${key}">${esc(label)}</option>`)
        .join("");
      st.checksReady = true;
    }
    if (!st.logRendered) {
      $("#log").innerHTML = "";
      (state.log || []).forEach(appendLog);
      st.logRendered = true;
    }
    if (st.sheetCid && st.chars[st.sheetCid] && !$("#sheet-modal").classList.contains("hidden")) {
      openSheet(st.sheetCid);
    }
  }

  function appendLog(entry) {
    const log = $("#log");
    const nearBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 80;
    const div = document.createElement("div");
    div.className = `log-entry ${entry.kind || "chat"}`;
    const whoMap = {narrate: "AI GM", system: "系统"};
    const who = whoMap[entry.kind] || entry.who || "";
    const whoColor = (entry.kind === "chat" || entry.kind === "roll") ? colorOfNick(entry.who) : "";
    div.innerHTML = `<div class="who"${whoColor ? ` style="color:${esc(whoColor)}"` : ""}>${esc(who)}</div><div>${esc(entry.text).replace(/\n/g, "<br>")}</div>`;
    log.appendChild(div);
    while (log.children.length > 400) log.removeChild(log.firstChild);
    if (nearBottom) log.scrollTop = log.scrollHeight;
  }

  const pendingLabels = (c) => {
    const items = [];
    if (c.pending_subclass) items.push("选子职业");
    const asiCount = Number(c.pending_asi || 0);
    if (asiCount) items.push(`提升 ×${asiCount}`);
    const boonCount = (c.pending_boons || []).length;
    if (boonCount) items.push(`恩赐 ×${boonCount}`);
    return items;
  };

  function subclassPreviewHtml(classId, sid) {
    const sub = ((FEATURES[classId] || {}).subclasses || {})[sid];
    if (!sub) return "";
    const feats = (sub.features || [])
      .slice()
      .sort((a, b) => a.level - b.level)
      .map((f) => `<span class="tip" data-tip="${esc(`${f.name} · ${f.level} 级\n\n${f.desc || ""}`)}">${esc(f.name)}（${f.level} 级）</span>`)
      .join("、");
    return (
      `${esc(sub.name)}${sub.name_en ? "（" + esc(sub.name_en) + "）" : ""}\n${esc(sub.desc || "")}`
      + (feats ? `\n\n特性：${feats}` : "")
    );
  }

  function renderParty() {
    const area = $("#party");
    const playersHtml = st.participants.map((p) => {
      const char = p.char_id ? st.chars[p.char_id] : null;
      const hpBar = char
        ? `<div class="hp-bar"><span style="width:${Math.max(0, Math.round((char.hp.cur / char.hp.max) * 100))}%"></span></div>`
        : "";
      const av = p.avatar ? `<img class="avatar" src="/avatars/${esc(p.avatar)}" alt="">` : "";
      const pending = char ? pendingLabels(char) : [];
      return `<div class="party-item">
        <div class="charmeta">
          ${av}<strong${p.color ? ` style="color:${esc(p.color)}"` : ""}>${esc(p.nickname)}</strong>
          ${p.is_host ? '<span class="tag">主机</span>' : ""}
          ${p.is_dm ? '<span class="tag">GM</span>' : ""}
          ${p.connected ? "" : '<span class="tag">离线</span>'}
        </div>
        ${char ? `<div class="dim">${esc(char.name)} · ${esc(char.race)}${esc(char.klass)} · HP ${char.hp.cur}/${char.hp.max} · AC ${char.ac}</div>${hpBar}` : '<div class="dim">尚未创建角色</div>'}
        ${pending.length ? `<div><span class="tag primary">待办：${esc(pending.join("、"))}（开角色卡分配）</span></div>` : ""}
        ${char ? `<button class="btn small" data-sheet="${p.char_id}">角色卡</button>` : ""}
        ${(me().is_dm && !p.connected && p.sid !== st.meSid) ? `<button class="btn small" data-kick="${esc(p.sid)}">踢出</button>` : ""}
      </div>`;
    }).join("");
    const companions = Object.values(st.chars).filter((c) => c.companion && !c.owner_nick);
    const companionHtml = companions.map((c) => `<div class="party-item">
        <div class="charmeta"><strong>${esc(c.name)}</strong><span class="tag">同伴</span></div>
        <div class="dim">${esc(c.race)}${esc(c.klass)} · HP ${c.hp.cur}/${c.hp.max} · AC ${c.ac}</div>
        <button class="btn small" data-sheet="${c.id}">角色卡</button>
      </div>`).join("");
    area.innerHTML = (playersHtml || '<div class="dim">暂无玩家</div>')
      + (companionHtml ? `<div class="spell-group">同伴（AI 控制）</div>${companionHtml}` : "");
    area.querySelectorAll("[data-sheet]").forEach((btn) => {
      btn.onclick = () => openSheet(btn.dataset.sheet);
    });
    area.querySelectorAll("[data-kick]").forEach((btn) => {
      btn.onclick = () => {
        const target = st.participants.find((x) => x.sid === btn.dataset.kick);
        if (!target) return;
        if (!confirm(`将离线玩家 ${target.nickname} 移出房间？其角色会被释放，可被其他人选用。`)) return;
        wsSend({type: "kick_player", sid: target.sid});
      };
    });
    // GM 控制台待办一览（谁有待分配项）
    const dcPending = $("#dc-pending");
    if (dcPending) {
      const rows = Object.values(st.chars)
        .map((c) => ({name: c.name, items: pendingLabels(c)}))
        .filter((r) => r.items.length)
        .map((r) => `${r.name}（${r.items.join("、")}）`);
      dcPending.textContent = rows.length ? "待分配：" + rows.join("；") : "";
    }
  }

  function renderCombat() {
    const area = $("#combat");
    const combat = st.combat;
    if (!combat) {
      area.innerHTML = "";
      return;
    }
    let html = "";
    if (combat.active) {
      const current = combat.combatants[combat.turn];
      html += `<p class="dim">第 ${combat.round} 轮 · 当前：<strong>${esc(current ? current.name : "—")}</strong></p>`;
      html += combat.combatants
        .map((c, i) => {
          const tag = c.kind === "monster" ? `（怪${c.cr_label ? " CR " + c.cr_label : ""}）` : (c.kind === "ally" ? "（同伴）" : "");
          const hpCtl =
            c.kind === "monster" && canDM()
              ? ` <input class="btn small" data-hpinput="${c.id}" type="number" min="1" value="${esc(hpDrafts[c.id] || 5)}" style="width:56px;padding:2px 6px" title="调整量：−/+ 按此数值增减">
                  <button class="btn small" data-mhp="${c.id}" data-dir="-1">−</button><button class="btn small" data-mhp="${c.id}" data-dir="1">+</button>`
              : "";
          const hpText =
            c.kind === "monster" && canDM()
              ? `<span class="hp-num" data-hpset="${c.id}" title="点击设置精确 HP">${c.hp}/${c.hp_max}</span>`
              : `${c.hp}/${c.hp_max}`;
          const ownerChar = c.kind === "pc" ? st.chars[c.id] : null;
          const nameColor = ownerChar ? colorOfNick(ownerChar.owner_nick) : "";
          const mon = c.kind === "monster" ? monsterLookup(c.name) : null;
          const nameHTML = mon
            ? `<span class="tip" data-tip="${esc(monsterTip(mon))}">${esc(c.name)}</span>`
            : esc(c.name);
          return `<div class="init-row ${i === combat.turn ? "current" : ""}"><span${nameColor ? ` style="color:${esc(nameColor)}"` : ""}>${nameHTML}${tag}</span><span>先攻 ${c.init} · HP ${hpText}${hpCtl}</span></div>`;
        })
        .join("");
      if (canDM()) html += `<button class="btn" id="btn-next-turn">下一回合</button>`;
    } else {
      html += '<p class="dim">战斗未开始</p>';
      if (canDM()) html += '<button class="btn primary" id="btn-start-combat">开始战斗（自动掷先攻）</button>';
    }
    if (canDM()) {
      html += `<div class="row" style="margin-top:8px">
        <input id="mon-name" placeholder="怪物名" style="flex:1">
        <input id="mon-hp" type="number" value="10" title="HP" style="width:64px">
        <input id="mon-init" type="number" value="0" title="先攻加值" style="width:64px">
        <button class="btn" id="btn-add-monster">添加</button>
      </div>`;
    }
    area.innerHTML = html;
    area.querySelectorAll("[data-hpinput]").forEach((inp) => {
      inp.oninput = () => {
        hpDrafts[inp.dataset.hpinput] = inp.value;
      };
    });
    area.querySelectorAll("[data-mhp]").forEach((btn) => {
      btn.onclick = () => {
        const amount = Math.max(1, parseInt(hpDrafts[btn.dataset.mhp] || "5", 10) || 5);
        wsSend({type: "set_monster_hp", id: btn.dataset.mhp, delta: amount * parseInt(btn.dataset.dir, 10)});
      };
    });
    area.querySelectorAll("[data-hpset]").forEach((el) => {
      el.onclick = () => {
        const mon = combatants().find((x) => x.id === el.dataset.hpset);
        if (!mon) return;
        const raw = prompt(`设置「${mon.name}」的 HP（当前 ${mon.hp}/${mon.hp_max}）`, mon.hp);
        if (raw === null) return;
        const value = parseInt(raw, 10);
        if (!Number.isNaN(value)) wsSend({type: "set_monster_hp", id: el.dataset.hpset, value});
      };
    });
    const start = $("#btn-start-combat");
    if (start) start.onclick = () => wsSend({type: "start_combat"});
    const next = $("#btn-next-turn");
    if (next) next.onclick = () => wsSend({type: "next_turn"});
    const add = $("#btn-add-monster");
    if (add) {
      add.onclick = () => {
        wsSend({
          type: "add_combatant",
          name: $("#mon-name").value.trim() || "怪物",
          hp: parseInt($("#mon-hp").value || "10", 10),
          init_bonus: parseInt($("#mon-init").value || "0", 10),
        });
      };
    }
  }

  const BOARD_W = 120;
  const BOARD_H = 60;
  let boardDrag = null;
  let boardSelected = null;
  let hpDrafts = {};

  function combatants() {
    return st.combat && st.combat.active ? st.combat.combatants || [] : [];
  }

  function currentCombatant() {
    const list = combatants();
    const turn = st.combat ? st.combat.turn || 0 : 0;
    return list[turn] || null;
  }

  // ---- 战术板视图（缩放/平移）：统一比例（横竖同一"尺→像素"），滚轮缩放、空白拖动平移 ----
  let boardZoom = 1;
  let boardCenter = {x: BOARD_W / 2, y: BOARD_H / 2};
  let panDrag = null;
  let boardGrid = false;

  function boardScale() {
    const canvas = $("#board");
    if (!canvas) return 1;
    // 统一比例也顺带修复原先横竖比例不一致造成的视觉拉伸
    return Math.min(canvas.width / BOARD_W, canvas.height / BOARD_H) * boardZoom;
  }

  function toPx(pos) {
    const canvas = $("#board");
    const s = boardScale();
    return {x: (pos.x - boardCenter.x) * s + canvas.width / 2, y: (pos.y - boardCenter.y) * s + canvas.height / 2};
  }

  function pxFromEvent(event) {
    const canvas = $("#board");
    const rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) * (canvas.width / rect.width),
      y: (event.clientY - rect.top) * (canvas.height / rect.height),
    };
  }

  function clampBoardView() {
    const canvas = $("#board");
    const s = boardScale();
    const halfW = (canvas.width / 2) / s;
    const halfH = (canvas.height / 2) / s;
    if (halfW >= BOARD_W / 2) boardCenter.x = BOARD_W / 2;
    else boardCenter.x = Math.max(halfW, Math.min(BOARD_W - halfW, boardCenter.x));
    if (halfH >= BOARD_H / 2) boardCenter.y = BOARD_H / 2;
    else boardCenter.y = Math.max(halfH, Math.min(BOARD_H - halfH, boardCenter.y));
  }

  function setBoardZoom(next, anchorEvent) {
    const anchor = anchorEvent ? feetFromEvent(anchorEvent) : null;
    boardZoom = Math.max(1, Math.min(4, next));
    if (anchor) {
      // 以光标处为锚点缩放：该点的棋盘坐标保持不动
      const canvas = $("#board");
      const s = boardScale();
      const px = pxFromEvent(anchorEvent);
      boardCenter.x = anchor.x - (px.x - canvas.width / 2) / s;
      boardCenter.y = anchor.y - (px.y - canvas.height / 2) / s;
    }
    clampBoardView();
    const label = $("#board-zoom");
    if (label) label.textContent = `${Math.round(boardZoom * 100)}%`;
    renderBoard();
  }

  function feetFromEvent(event) {
    const canvas = $("#board");
    const px = pxFromEvent(event);
    const s = boardScale();
    return {
      x: (px.x - canvas.width / 2) / s + boardCenter.x,
      y: (px.y - canvas.height / 2) / s + boardCenter.y,
    };
  }

  function tokenAt(ftPos) {
    const list = combatants();
    for (let i = list.length - 1; i >= 0; i--) {
      const c = list[i];
      if (!c.pos) continue;
      if (Math.hypot(c.pos.x - ftPos.x, c.pos.y - ftPos.y) <= 3) return c;
    }
    return null;
  }

  function canDragToken(c) {
    if (!c) return false;
    if (canDM()) return true;
    return c.kind === "pc" && me().char_id === c.id;
  }

  function renderBoard() {
    const canvas = $("#board");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width;
    const H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#171a21";
    ctx.fillRect(0, 0, W, H);
    ctx.lineWidth = 1;
    const s0 = boardScale();
    const halfW = (W / 2) / s0;
    const halfH = (H / 2) / s0;
    const x0 = Math.max(0, boardCenter.x - halfW);
    const x1 = Math.min(BOARD_W, boardCenter.x + halfW);
    const y0 = Math.max(0, boardCenter.y - halfH);
    const y1 = Math.min(BOARD_H, boardCenter.y + halfH);
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    for (let ft = Math.floor(x0 / 10) * 10; ft <= x1; ft += 10) {
      const px = toPx({x: ft, y: 0}).x;
      ctx.beginPath();
      ctx.moveTo(px, 0);
      ctx.lineTo(px, H);
      ctx.stroke();
    }
    for (let ft = Math.floor(y0 / 10) * 10; ft <= y1; ft += 10) {
      const py = toPx({x: 0, y: ft}).y;
      ctx.beginPath();
      ctx.moveTo(0, py);
      ctx.lineTo(W, py);
      ctx.stroke();
    }
    if (boardGrid) {
      // 5 尺网格（开关）：读距离用
      ctx.strokeStyle = "rgba(201,163,92,0.16)";
      for (let ft = Math.floor(x0 / 5) * 5; ft <= x1; ft += 5) {
        const px = toPx({x: ft, y: 0}).x;
        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, H);
        ctx.stroke();
      }
      for (let ft = Math.floor(y0 / 5) * 5; ft <= y1; ft += 5) {
        const py = toPx({x: 0, y: ft}).y;
        ctx.beginPath();
        ctx.moveTo(0, py);
        ctx.lineTo(W, py);
        ctx.stroke();
      }
    }
    const list = combatants();
    if (!list.length) {
      ctx.fillStyle = "rgba(230,227,218,0.35)";
      ctx.font = "16px 'Microsoft YaHei', sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("战斗开始后，棋子会出现在战术板上", W / 2, H / 2);
      renderBoardInfo();
      return;
    }
    if (boardSelected) {
      const selected = list.find((c) => c.id === boardSelected);
      if (selected && selected.pos) {
        list.forEach((c) => {
          if (c.id === selected.id || !c.pos) return;
          const a = toPx(selected.pos);
          const b = toPx(c.pos);
          const dist = Math.round(Math.hypot(selected.pos.x - c.pos.x, selected.pos.y - c.pos.y));
          ctx.strokeStyle = "rgba(201,163,92,0.4)";
          ctx.setLineDash([5, 5]);
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = "#c9a35c";
          ctx.font = "12px sans-serif";
          ctx.textAlign = "center";
          ctx.fillText(`${dist}尺`, (a.x + b.x) / 2, (a.y + b.y) / 2 - 4);
        });
      }
    }
    list.forEach((c) => {
      if (!c.pos) return;
      const pos = boardDrag && boardDrag.id === c.id ? {x: boardDrag.x, y: boardDrag.y} : c.pos;
      const p = toPx(pos);
      const current = currentCombatant();
      const isCurrent = current && current.id === c.id;
      if (boardDrag && boardDrag.id === c.id && c.turn_start) {
        const a = toPx(c.turn_start);
        const dist = Math.hypot(c.turn_start.x - pos.x, c.turn_start.y - pos.y);
        const allowed = (c.speed || 30) * (c.dashing ? 2 : 1);
        ctx.strokeStyle = dist > allowed + 0.5 ? "#d16060" : "rgba(111,191,115,0.8)";
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = dist > allowed + 0.5 ? "#d16060" : "#6fbf73";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(`已移动 ${Math.round(dist)} / ${allowed} 尺`, (a.x + p.x) / 2, (a.y + p.y) / 2 - 8);
      }
      const R = 11 * Math.min(boardZoom, 3);
      ctx.beginPath();
      ctx.arc(p.x, p.y, R, 0, Math.PI * 2);
      const ownerChar = c.kind === "pc" ? st.chars[c.id] : null;
      const tokenColor = ownerChar ? colorOfNick(ownerChar.owner_nick) : "";
      ctx.fillStyle = c.kind === "monster" ? "#a84a42" : (c.kind === "ally" ? "#3f8fa8" : (tokenColor || "#3f6fa8"));
      ctx.fill();
      if (boardSelected === c.id || isCurrent) {
        ctx.lineWidth = 3;
        ctx.strokeStyle = boardSelected === c.id ? "#c9a35c" : "#e6e3da";
        ctx.stroke();
        ctx.lineWidth = 1;
      }
      ctx.fillStyle = "#e6e3da";
      const fs = Math.round(11 * Math.min(boardZoom, 1.8));
      ctx.font = `${fs}px 'Microsoft YaHei', sans-serif`;
      ctx.textAlign = "center";
      const tag = c.kind === "monster" && c.cr_label ? ` CR${c.cr_label}` : "";
      ctx.fillText(`${c.name}${tag} ${c.hp}/${c.hp_max}`, p.x, p.y + R + 15);
    });
    renderBoardInfo();
  }

  function renderBoardInfo() {
    const info = $("#board-info");
    const dashBtn = $("#btn-dash");
    if (!info) return;
    const list = combatants();
    if (!st.combat || !st.combat.active || !list.length) {
      info.textContent = "未进入战斗";
      if (dashBtn) dashBtn.classList.add("hidden");
      return;
    }
    const current = currentCombatant();
    const mine = current && me().char_id === current.id;
    let text = `第 ${st.combat.round} 轮 · 当前：${current ? current.name : "—"}`;
    if (mine && current.turn_start && current.pos) {
      const used = Math.round(Math.hypot(current.pos.x - current.turn_start.x, current.pos.y - current.turn_start.y));
      const allowed = (current.speed || 30) * (current.dashing ? 2 : 1);
      text += ` · 已移动 ${used}/${allowed} 尺`;
    }
    info.textContent = text;
    if (dashBtn) {
      dashBtn.classList.toggle("hidden", !mine);
      if (mine) dashBtn.textContent = current.dashing ? "取消冲刺" : "冲刺（双倍移动）";
    }
  }

  const QUEST_STATUS = {
    in_progress: {label: "进行中", cls: "qp"},
    completed: {label: "已完成", cls: "qc"},
    failed: {label: "失败", cls: "qf"},
    expired: {label: "已过期", cls: "qf"},
  };

  function renderQuests() {
    const card = $("#quest-card");
    const campaign = st.campaign;
    const quests = (campaign && campaign.quests) || [];
    if (!campaign || (!quests.length && !campaign.finale)) {
      card.classList.add("hidden");
      return;
    }
    card.classList.remove("hidden");
    $("#quest-milestone").textContent = campaign.milestone_hint || "";
    const pressure = campaign.pressure || {};
    const pressureEl = $("#quest-pressure");
    if (campaign.pressure_type === "threat_clock" && pressure.max) {
      pressureEl.innerHTML = `<span class="tip" data-tip="${esc(`威胁进度（0-${pressure.max}）：每完成一个任务自动 +1，GM 也可因世界恶化额外推进。满 ${pressure.max} 时终局降临（本战役：霜牙完全苏醒，终局变为逃生或绝望决战）。当前 ${pressure.level}/${pressure.max}。`)}">${esc(pressure.label || "威胁")}：${pressure.level}/${pressure.max}</span>`;
    } else {
      pressureEl.textContent = "";
    }
    const order = {in_progress: 0, completed: 1, failed: 2, expired: 3};
    const sorted = [...quests].sort(
      (a, b) => ((order[a.status] ?? 9) - (order[b.status] ?? 9)) || ((a.tier || 0) - (b.tier || 0))
    );
    const inProgress = sorted.filter((q) => q.status === "in_progress");
    const shown = st.questFilter === "all" ? sorted : inProgress;
    document.querySelectorAll("[data-qf]").forEach((btn) => {
      const active = btn.dataset.qf === st.questFilter;
      btn.classList.toggle("active", active);
      btn.textContent = `${btn.dataset.qf === "all" ? "全部" : "进行中"} ${btn.dataset.qf === "all" ? quests.length : inProgress.length}`;
    });
    let html = shown
      .map((q) => {
        const status = QUEST_STATUS[q.status] || {label: q.status || "", cls: ""};
        const notes = (q.notes || []).length ? `<div class="dim">进展：${q.notes.map(esc).join("；")}</div>` : "";
        return `<div class="quest-item ${status.cls}" data-quest="${q.id}">
          <div class="quest-head"><span class="quest-dot"></span><strong>${esc(q.title || "")}</strong><span class="tag">${status.label}</span></div>
          <div class="quest-body hidden">
            ${q.hook ? `<div class="dim">${esc(q.hook)}</div>` : ""}
            <div class="dim">目标：${esc(q.objective || "")}</div>
            ${q.location ? `<div class="dim">地点：${esc(q.location)}</div>` : ""}
            ${notes}
          </div>
        </div>`;
      })
      .join("");
    if (!shown.length) {
      html = `<div class="dim">${st.questFilter === "all" ? "还没有已发现的任务。" : "暂无进行中任务（可切到「全部」查看已发现的历史任务）。"}</div>`;
    }
    if (campaign.finale) {
      html += `<div class="quest-item finale">
        <div class="quest-head"><span class="quest-dot"></span><strong>终局：${esc(campaign.finale.location || "")}</strong><span class="tag">${campaign.finale_cleared ? "已达成" : "已开启"}</span></div>
        <div class="dim">${esc(campaign.finale.objective || "")}</div>
      </div>`;
    }
    $("#quests").innerHTML = html;
    $("#quests").querySelectorAll("[data-quest]").forEach((item) => {
      item.onclick = () => {
        const body = item.querySelector(".quest-body");
        if (body) body.classList.toggle("hidden");
      };
    });
  }

  function renderMeta() {
    const rows = [];
    const connected = st.participants.filter((p) => p.connected).length;
    const dm = st.dmSid ? st.participants.find((p) => p.sid === st.dmSid) : null;
    const presiding = dm ? `人类 GM（${dm.nickname}）` : (st.aiMode ? "AI 主持" : "无人主持");
    rows.push(`<div class="preview-row"><span>主持</span><span>${esc(presiding)}</span></div>`);
    rows.push(`<div class="preview-row"><span>游戏时间</span><span>${esc(st.clock || "第 1 天 00:00")}</span></div>`);
    rows.push(`<div class="preview-row"><span>在线玩家</span><span>${connected} / ${st.participants.length}</span></div>`);
    if (st.aiMode && !st.dmSid && connected <= 1) {
      rows.push('<div class="preview-row"><span>模式</span><span>单人测试</span></div>');
    }
    $("#meta").innerHTML = rows.join("");
  }

  function renderDMPanel() {
    const area = $("#dm-panel");
    const isDM = Boolean(me().is_dm);
    const restRow = isDM
      ? `<div class="row" style="margin-top:6px"><button class="btn" id="btn-short-rest">短休（1 小时）</button><button class="btn" id="btn-long-rest">长休（8 小时）</button><button class="btn" id="btn-end-combat">结束战斗</button><button class="btn" id="btn-release-dm">卸任 GM 席位</button></div>`
      : "";
    const dmHolder = st.dmSid ? st.participants.find((p) => p.sid === st.dmSid) : null;
    if (isDM) {
      area.innerHTML = `<p class="dim">你是本场 GM：下方有控制台与含剧透的「GM 秘闻」；想下场玩角色请先「卸任」，主持模式总闸在服务器管理页。</p>${restRow}`;
    } else if (dmHolder) {
      area.innerHTML = `<p class="dim">人类 GM 主持中（${esc(dmHolder.nickname)}）。</p>`;
    } else {
      const hint = me().is_host
        ? "你是服务器管理员：本页属于玩家界面；服务器配置与主持总闸在「服务器管理页」（启动终端里打印的管理网址）。"
        : (st.aiMode ? "AI 主持中。" : "当前无人主持。");
      area.innerHTML = `<p class="dim">${hint}</p><div class="row" style="margin-top:6px"><button class="btn" id="btn-claim-dm">接任 GM 席位</button></div>`;
    }
    const shortBtn = $("#btn-short-rest");
    if (shortBtn) shortBtn.onclick = () => wsSend({type: "rest", kind: "short"});
    const longBtn = $("#btn-long-rest");
    if (longBtn) longBtn.onclick = () => wsSend({type: "rest", kind: "long"});
    const endBtn = $("#btn-end-combat");
    if (endBtn) {
      endBtn.onclick = () => {
        const reason = prompt("结束战斗的原因（可留空）", "") || "手动结束";
        wsSend({type: "end_combat", reason});
      };
    }
    const releaseBtn = $("#btn-release-dm");
    if (releaseBtn) {
      releaseBtn.onclick = () => {
        if (!confirm("卸任 GM 席位？卸任后可以重新选择角色；若总闸开着 AI 将恢复主持。")) return;
        wsSend({type: "release_dm"});
      };
    }
    const claimBtn = $("#btn-claim-dm");
    if (claimBtn) {
      claimBtn.onclick = () => {
        const c = myChar();
        if (c && !confirm(`接任 GM 需要放下你当前的角色（${c.name}），确定？`)) return;
        wsSend({type: "claim_dm"});
      };
    }
    // GM 专属：GM 秘闻（含剧透的完整战役结构）与控制台
    const briefCard = $("#dm-brief-card");
    if (briefCard) {
      briefCard.classList.toggle("hidden", !(isDM && st.dmBrief));
      if (isDM && st.dmBrief) $("#dm-brief").textContent = st.dmBrief;
    }
    const consoleCard = $("#dm-console-card");
    if (consoleCard) consoleCard.classList.toggle("hidden", !isDM);
    const charSel = $("#dc-char");
    if (charSel && isDM) {
      const options = Object.values(st.chars)
        .map((c) => `<option value="${c.id}">${esc(c.name)}（${esc(c.klass)} ${c.level}级）</option>`)
        .join("");
      if (charSel.dataset.options !== options) {
        const current = charSel.value;
        charSel.innerHTML = options || '<option value="">（无角色）</option>';
        if (current) charSel.value = current;
        charSel.dataset.options = options;
      }
    }
    const rewardSel = $("#dc-reward-target");
    if (rewardSel && isDM) {
      const options = '<option value="@party">全队（上桌角色）</option>'
        + Object.values(st.chars)
          .map((c) => `<option value="${c.id}">${esc(c.name)}（${esc(c.klass)} ${c.level}级）</option>`)
          .join("");
      if (rewardSel.dataset.options !== options) {
        const current = rewardSel.value;
        rewardSel.innerHTML = options;
        if (current) rewardSel.value = current;
        rewardSel.dataset.options = options;
      }
    }
  }

  function sheetHTML(c) {
    const abilityRows = Object.entries(ABN)
      .map(([key, label]) => `<tr><td>${esc(label)}</td><td>${c.abilities[key]}</td><td>${fmtMod(c.mods[key])}</td><td>${fmtMod(c.saves[key])}</td></tr>`)
      .join("");
    const skillRows = Object.entries(c.skills)
      .map(([id, bonus]) => `<tr><td class="tip" data-tip="${esc(skillTip(id))}">${esc(SKILLS[id] ? SKILLS[id].name : id)}</td><td colspan="3">${fmtMod(bonus)}</td></tr>`)
      .join("");
    const spell = c.spellcasting
      ? `<p class="dim">施法：豁免 DC ${c.spellcasting.save_dc} · 攻击 ${fmtMod(c.spellcasting.attack_bonus)}（基于${esc(ABN[c.spellcasting.ability])}）</p>`
      : "";
    const slots = c.spell_slots || {};
    const slotRows = Object.keys(slots)
      .sort()
      .map((key) => {
        const slot = slots[key];
        const remaining = Math.max(0, (slot.max || 0) - (slot.used || 0));
        const dots = "●".repeat(remaining) + "○".repeat(Math.max(0, (slot.max || 0) - remaining));
        return `<tr><td>${key} 环</td><td colspan="3">${dots} ${remaining}/${slot.max} <button class="btn small" data-slotup="${key}">消耗</button><button class="btn small" data-slotdown="${key}">恢复</button></td></tr>`;
      })
      .join("");
    const spellRows = (c.spells || [])
      .map((s) => {
        const full = SPELLS[s.id];
        return `<tr><td class="tip" data-tip="${esc(full ? spellTip(full) : "")}">${esc(s.name)}</td><td colspan="3">${s.level === 0 ? "戏法" : s.level + " 环"}</td></tr>`;
      })
      .join("");
    const canEditSheet = canDM() || me().char_id === c.id;

    // ---- 法术分层管理器（BFRD：戏法 / 已知或准备 / 法术书 / 仪式）----
    function spellManagerHtml() {
      const meta = CLASS_META[c.class_id] || {};
      if (Number(c.spell_model || 1) < 2 || !c.spellcasting) {
        return spellRows ? `<h2>已知法术</h2><table class="sheet-table">${spellRows}</table>` : "";
      }
      const sources = meta.sources || [];
      const freeMode = c.spell_mode === "free";
      const circleMax = Math.max(0, ...Object.keys(c.spell_slots || {}).map(Number));
      const ability = meta.spell_ability || (c.spellcasting && c.spellcasting.ability);
      const abilityMod = ability ? Number((c.mods || {})[ability] || 0) : 0;
      const prow = ((meta.progression || {}).rows || {})[String(c.level || 1)] || {};
      const caps = {
        cantrips: Number(prow.cantrips || 0),
        known: Number(prow.spells || 0),
        rituals: Number(prow.rituals || 0),
        prepared: Math.max(1, abilityMod + Number(c.level || 1)),
      };
      const eligible = (s) => freeMode || !sources.length || (s.sources || []).some((src) => sources.includes(src));
      const ownedIds = new Set((c.spells || []).map((e) => e.id));          // 全部栏位并集（防跨栏重复）
      const preparedIds = new Set((c.prepared || []).map((e) => e.id));
      const isWizard = (c.class_id || "") === "wizard";                     // 法术书/入书仅法师可见
      const spellList = (pred) => Object.entries(SPELLS)
        .filter(([sid, s]) => pred(s, sid) && eligible(s))
        .sort((a, b) => a[1].level - b[1].level || a[1].name.localeCompare(b[1].name, "zh"));
      const listHtml = (bucket, rows) => rows.map((e, idx) => {
        const full = SPELLS[e.id];
        const remove = canEditSheet ? `<button class="btn small" data-spell-remove="${esc(e.id)}" data-bucket="${bucket}">✕</button>` : "";
        return `<tr><td class="tip" data-tip="${esc(full ? spellTip(full) : "")}">${esc(e.name)}</td><td>${e.level === 0 ? "戏法" : e.level + " 环"}</td><td colspan="2">${remove}</td></tr>`;
      }).join("");
      const addHtml = (bucket, label, options, count, cap) => {
        if (!canEditSheet || count >= cap) return "";
        const opts = options.map(([sid, s]) => {
          const short = String(s.desc_zh || s.desc || "").replace(/\s+/g, " ").slice(0, 70);
          return `<option value="${esc(sid)}" title="${esc(short)}">${esc(s.name)}（${s.level === 0 ? "戏法" : s.level + " 环"}）</option>`;
        }).join("");
        if (!opts) return "";
        return `<div class="row"><select id="spell-add-${bucket}">${opts}</select>
          <button class="btn small" data-spell-add="${bucket}">＋ ${esc(label)}</button></div>
          <div class="dim" id="spell-add-${bucket}-preview" style="margin-top:2px; white-space: pre-line;"></div>`;
      };
      let html = "";
      // 戏法
      if (caps.cantrips) {
        const rows = listHtml("cantrips", c.cantrips || []);
        html += `<h2>戏法 ${(c.cantrips || []).length}/${caps.cantrips}</h2>`;
        html += (rows || "") ? `<table class="sheet-table">${rows}</table>` : "";
        html += addHtml("cantrips", "戏法", spellList((s, sid) => s.level === 0 && !ownedIds.has(sid)), (c.cantrips || []).length, caps.cantrips);
      }
      // 已知 / 准备
      if ((c.class_id || "") in {bard: 1, sorcerer: 1, warlock: 1, paladin: 1, ranger: 1}) {
        const rows = listHtml("known", c.known || []);
        html += `<h2>已知法术 ${(c.known || []).length}/${caps.known}</h2>`;
        html += rows ? `<table class="sheet-table">${rows}</table>` : "";
        html += addHtml("known", "已知", spellList((s, sid) => s.level >= 1 && s.level <= circleMax && !s.ritual && !ownedIds.has(sid)), (c.known || []).length, caps.known);
        const swapCredits = Number(c.swap_credits || 0);
        if (canEditSheet && swapCredits > 0 && (c.known || []).length) {
          const oldOpts = (c.known || []).map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("");
          const newOpts = spellList((s, sid) => s.level >= 1 && s.level <= circleMax && !s.ritual && !ownedIds.has(sid))
            .map(([sid, s]) => `<option value="${esc(sid)}">${esc(s.name)}（${s.level} 环）</option>`).join("");
          html += `<div class="row"><span class="dim">替换名额 ×${swapCredits}</span>
            <select id="spell-swap-old">${oldOpts}</select><select id="spell-swap-new">${newOpts}</select>
            <button class="btn small" id="spell-swap-btn">替换</button></div>
            <div class="dim" id="spell-swap-preview" style="margin-top:2px; white-space: pre-line;"></div>`;
        }
      } else {
        const rows = listHtml("prepared", c.prepared || []);
        if (isWizard) {
          const bookRows = (c.spellbook || []).length ? `<details class="prog-details"><summary>法术书 ${(c.spellbook || []).length} 条（免费名额 ${Number(c.spellbook_credits || 0)}）</summary><table class="sheet-table">${listHtml("spellbook", c.spellbook || [])}</table></details>` : "";
          html += `<h2>准备法术 ${(c.prepared || []).length}/${caps.prepared}</h2>`;
          html += rows ? `<table class="sheet-table">${rows}</table>` : "";
          const preparedPool = spellList((s, sid) =>
            s.level >= 1 && s.level <= circleMax
            && (c.spellbook || []).some((e) => e.id === sid)      // 只能从法术书准备
            && !preparedIds.has(sid)                               // 已准备的不重复列出
          );
          html += addHtml("prepared", "准备", preparedPool, (c.prepared || []).length, caps.prepared);
          const bookPool = spellList((s, sid) => s.level >= 1 && s.level <= circleMax && !ownedIds.has(sid));
          html += addHtml("spellbook", "入书", bookPool, 0, 1);
          html += bookRows;
        } else {
          html += `<h2>准备法术 ${(c.prepared || []).length}/${caps.prepared}</h2>`;
          html += rows ? `<table class="sheet-table">${rows}</table>` : "";
          html += addHtml("prepared", "准备", spellList((s, sid) => s.level >= 1 && s.level <= circleMax && !ownedIds.has(sid)), (c.prepared || []).length, caps.prepared);
        }
      }
      // 仪式
      if (caps.rituals) {
        const rows = listHtml("rituals", c.rituals || []);
        html += `<h2>仪式 ${(c.rituals || []).length}/${caps.rituals}（不耗法术位）</h2>`;
        html += rows ? `<table class="sheet-table">${rows}</table>` : "";
        html += addHtml("rituals", "仪式", spellList((s, sid) => s.ritual && s.level >= 1 && s.level <= circleMax && !ownedIds.has(sid)), (c.rituals || []).length, caps.rituals);
      }
      return html;
    }
    const pendingAsi = Number(c.pending_asi || 0);
    const abilityOptions = Object.entries(ABN)
      .map(([k, n]) => `<option value="${k}">${esc(n)}</option>`)
      .join("");
    const meta = CLASS_META[c.class_id] || {};
    const subclassList = Object.entries(((FEATURES[c.class_id] || {}).subclasses || {}));
    const subclassOptions = subclassList
      .map(([id, sub]) => `<option value="${esc(id)}" title="${esc(`${sub.name}：${(sub.desc || "").slice(0, 60)}`)}">${esc(sub.name)}</option>`)
      .join("");
    const talentOptions = Object.entries(TALENTS)
      .filter(([, t]) => t.list && (meta.improvement_talents || []).includes(t.list))
      .map(([tid, t]) => `<option value="${esc(tid)}">${esc(t.name)}</option>`)
      .join("");
    const asiBlock = pendingAsi > 0
      ? (canEditSheet
        ? `<div class="asi-box">待分配提升（Improvement）×${pendingAsi}：
             <select id="asi-a">${abilityOptions}</select><button class="btn small" id="asi-plus2">+2</button>
             <select id="asi-b">${abilityOptions}</select><select id="asi-c">${abilityOptions}</select><button class="btn small" id="asi-split">各 +1</button>
             ${talentOptions ? `<br><select id="asi-d">${abilityOptions}</select><select id="asi-talent">${talentOptions}</select><button class="btn small" id="asi-talent-btn">+1 与天赋</button>` : ""}</div>`
        : `<p style="color:var(--accent)">待分配提升（Improvement）×${pendingAsi}（由角色本人或 GM 分配）</p>`)
      : "";
    const subclassBlock = c.pending_subclass
      ? (canEditSheet && subclassOptions
        ? `<div class="asi-box">3 级解锁子职业：<select id="subclass-pick">${subclassOptions}</select><button class="btn small" id="btn-choose-subclass">选择</button>
             <div class="dim" id="subclass-preview" style="margin-top:6px; white-space: pre-line;"></div></div>`
        : `<p style="color:var(--accent)">待选子职业（由角色本人或 GM 选择）</p>`)
      : "";
    const boonBlocks = (c.pending_boons || []).map((lv) => {
      const options = ((meta.boons || {})[String(lv)] || []).map((o) => `<option value="${esc(o.key)}">${esc(o.name)}</option>`).join("");
      const label = lv === 10 ? "英雄恩赐" : "史诗恩赐";
      return canEditSheet && options
        ? `<div class="asi-box">待选${label}（${lv} 级）：<select id="boon-${lv}">${options}</select><button class="btn small" data-boonlevel="${lv}">选择</button></div>`
        : `<p style="color:var(--accent)">待选${label}（由角色本人或 GM 选择）</p>`;
    }).join("");
    const effectRows = (c.effects || [])
      .map((e) => {
        const cond = CONDITIONS[e.name];
        const condTip = cond ? String(cond.desc_zh || cond.desc || "").replace(/\s+/g, " ") : "";
        const status = e.expires_at
          ? "剩 " + fmtMinutes(Math.max(0, e.expires_at - (st.clockMinutes || 0)))
          : "持续";
        const drop = canEditSheet ? `<button class="btn small" data-drop="${esc(e.name)}">解除</button>` : "";
        return `<tr><td class="${cond ? "tip" : ""}" data-tip="${esc(condTip)}">${esc(e.name)}</td><td colspan="2">${e.concentration ? "专注 · " : ""}${status}</td><td>${drop}</td></tr>`;
      })
      .join("");
    const classInfo = FEATURES[c.class_id] || {};
    const charLevel = Number(c.level || 1);
    const myFeats = (classInfo.features || []).filter((f) => f.level <= charLevel);
    const subInfo = c.subclass ? ((classInfo.subclasses || {})[c.subclass] || null) : null;
    const subFeats = subInfo ? (subInfo.features || []).filter((f) => f.level <= charLevel) : [];
    const featureRows = [
      ...myFeats.map((f) => `<tr><td class="tip" data-tip="${esc(f.desc)}">${esc(f.name)}</td><td colspan="3">${f.level} 级</td></tr>`),
      ...subFeats.map((f) => `<tr><td class="tip" data-tip="${esc(f.desc)}">${esc(subInfo.name)} · ${esc(f.name)}</td><td colspan="3">${f.level} 级</td></tr>`),
    ].join("");
    const featureBlock = featureRows ? `<h2>职业特性（悬停看全文）</h2><table class="sheet-table">${featureRows}</table>` : "";
    const prog = (CLASS_META[c.class_id] || {}).progression || {};
    let progBlock = "";
    if (prog.columns && prog.rows) {
      const head = prog.columns.map((col) => `<th>${esc(col.label)}</th>`).join("");
      const body = Object.entries(prog.rows)
        .sort((a, b) => Number(a[0]) - Number(b[0]))
        .map(([level, vals]) => {
          const tds = prog.columns.map((col) => {
            if (col.key === "level") return `<td>${level}</td>`;
            const v = vals[col.key];
            if (col.key === "features") return `<td>${esc((v || []).join("、"))}</td>`;
            if (v === undefined || v === null || v === "" || v === 0 || v === "-") return "<td>—</td>";
            return `<td>${esc(String(v))}</td>`;
          }).join("");
          const cls = Number(level) === charLevel ? ' class="hl"' : (Number(level) === charLevel + 1 ? ' class="next"' : "");
          return `<tr${cls}>${tds}</tr>`;
        }).join("");
      progBlock = `<details class="prog-details"><summary>职业进阶表（高亮=当前等级，虚线=下一级）</summary><div class="prog-wrap"><table class="prog-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></details>`;
    }
    return `
      <h2>${esc(c.name)}</h2>
      <p class="dim"><span class="tip" data-tip="${esc(raceTip(c.race_id))}">${esc(c.race)}</span>${c.adaptation ? `（<span class="tip" data-tip="${esc(adaptationTip(c))}">自然适应：${esc(c.adaptation)}</span>）` : ""}${c.heritage ? `（${esc(c.heritage)}传承）` : ""}${c.gender ? " · " + esc(c.gender) : ""} · ${esc(c.klass)}${c.subclass_name ? "·" + esc(c.subclass_name) : ""} ${c.level}级 · 背景：${esc(c.background)} · 玩家：${esc(c.owner_nick || "未上桌")}</p>
      ${c.alt_speed ? `<p class="dim">${esc(c.alt_speed)}速度 = 步行速度（${c.speed} 尺，自然适应）</p>` : ""}
      <p><strong>HP ${c.hp.cur}/${c.hp.max}</strong> · AC ${c.ac} · 速度 ${c.speed}尺 · 熟练 ${fmtMod(c.prof_bonus)} · 被动察觉 ${c.passive_perception}</p>
      ${c.dead ? `<p style="color:var(--bad)"><strong>已死亡</strong>——普通治疗无效，需复苏术 / 死者复活 / 复活术 / 真实复活术（引擎 revive 动作）</p>` : (c.stable ? `<p style="color:var(--accent)"><strong>稳定（昏迷）</strong>——无需死亡豁免，1d4 小时后自动苏醒（回复 1 HP），或由治疗提前救醒</p>` : (c.dying ? `<p style="color:var(--bad)"><strong>濒死（昏迷）</strong>——死亡豁免 ${(c.death_saves || {}).successes || 0} 成功 / ${(c.death_saves || {}).failures || 0} 失败（回合开始引擎自动掷；3 成功稳定、3 失败死亡）</p>` : ""))}
      ${Number(c.exhaustion) > 0 ? `<p style="color:var(--accent)">力竭 ${c.exhaustion} 级（1 检定劣势 / 2 速度减半 / 3 攻击豁免劣势 / 4 HP 上限减半 / 5 速度 0 / 6 死亡；长休降 1 级）</p>` : ""}
      ${(Number(c.contacts) || Number(c.clues)) ? `<p class="dim">人脉 ${Number(c.contacts) || 0} · 线索 ${Number(c.clues) || 0}${c.training ? ` · 训练中：${esc(c.training.goal || "")}（${c.training.progress_weeks || 0}/${c.training.required_weeks || 50} 周）` : ""}${c.crafting ? ` · 制作中：${esc(c.crafting.name || "")}（${Math.floor((c.crafting.progress_cp || 0) / 100)}/${Math.floor((c.crafting.price_cp || 1) / 100)} 金）` : ""}</p>` : ""}
      ${asiBlock}
      ${subclassBlock}
      ${boonBlocks}
      ${(c.talents && c.talents.length) ? `<p class="dim">天赋：${c.talents.map((tid) => `<span class="tip" data-tip="${esc(talentTip(tid))}">${esc((TALENTS[tid] || {}).name || tid)}</span>`).join("、")}${(c.talent_schools && c.talent_schools["school-specialization"]) ? `（学派专精：${esc(c.talent_schools["school-specialization"])}）` : ""}</p>` : ""}
      <p class="dim">幸运：${"●".repeat(Math.max(0, Math.min(5, Number(c.luck) || 0)))}${"○".repeat(5 - Math.max(0, Math.min(5, Number(c.luck) || 0)))}（${Number(c.luck) || 0}/5）${canEditSheet ? `<button class="btn small" id="luck-spend">消耗 1</button><button class="btn small" id="luck-gain">+1</button>` : ""}</p>
      <p class="dim">生命骰：${Math.max(0, Number(c.level || 1) - Number(c.hit_dice_used || 0))}/${Number(c.level || 1)}（d${Number(c.hit_die || 8)}；长休恢复一半）${canEditSheet ? `<button class="btn small" id="hd-spend">短休消耗 1 颗回血</button>` : ""}</p>
      ${(c.boons && c.boons.length) ? `<p class="dim">恩赐：${c.boons.map((b) => `<span class="tip" data-tip="${esc(b.desc || "")}">${esc(b.name)}</span>`).join("、")}</p>` : ""}
      ${spell}
      ${slotRows ? `<h2>法术位（信任制可手调）</h2><table class="sheet-table">${slotRows}</table>` : ""}
      ${spellManagerHtml()}
      ${effectRows ? `<h2>当前效果</h2><table class="sheet-table">${effectRows}</table>` : ""}
      <div class="row">
        <input id="sheet-hp-set" type="number" value="${c.hp.cur}" style="width:90px">
        <button class="btn" id="sheet-hp-btn">设置 HP</button>
      </div>
      ${featureBlock}
      ${progBlock}
      <table class="sheet-table"><tr><th>属性</th><th>值</th><th>调整</th><th>豁免</th></tr>${abilityRows}</table>
      ${skillRows ? `<h2>技能</h2><table class="sheet-table">${skillRows}</table>` : ""}
      <h2>护甲</h2>
      <p class="dim">当前：${esc(c.armor)}${c.shield ? " + 盾" : ""}（AC ${c.ac}）</p>
      ${canEditSheet ? (() => {
        const ownedArmors = (c.items || []).filter((it) => {
          const db = it.id ? ITEMS[it.id] : null;
          return db && db.category === "Armor" && db.ac_base !== undefined && !db.is_shield;
        });
        if (!ownedArmors.length) return "";
        const current = c.worn_armor || "none";
        const opts = [`<option value="none" ${current === "none" ? "selected" : ""}>无甲</option>`]
          .concat(ownedArmors.map((it) => `<option value="${esc(it.id)}" ${it.id === current ? "selected" : ""}>${esc(it.name)}（AC ${ITEMS[it.id].ac_base}${ITEMS[it.id].ac_dex === "full" ? "+敏捷" : ITEMS[it.id].ac_dex ? `+敏≤${ITEMS[it.id].ac_dex}` : ""}）</option>`));
        return `<div class="row"><select id="wear-armor-select">${opts.join("")}</select>
          <label class="radio tip" data-tip="盾牌 +2 AC（BFRD）"><input type="checkbox" id="wear-shield-box" ${c.shield ? "checked" : ""}> 持盾</label>
          <button class="btn small" id="wear-armor-btn">更换穿着</button></div>`;
      })() : ""}
      ${inventoryHTML(c, canEditSheet)}
      ${c.backstory ? `<h2>背景故事</h2><p class="dim">${esc(c.backstory)}</p>` : ""}
      <p><a class="btn" href="/export/character/${c.id}">导出角色卡 JSON</a>
      ${canEditSheet ? `<button class="btn" id="btn-delete-char" style="color:var(--bad); margin-left:6px;">删除角色卡（不可恢复）</button>` : ""}</p>`;
  }

  function openSheet(cid) {
    const char = st.chars[cid];
    if (!char) return;
    st.sheetCid = cid;
    $("#sheet").innerHTML = sheetHTML(char);
    $("#sheet-modal").classList.remove("hidden");
    const canEdit = canDM() || me().char_id === cid;
    const btn = $("#sheet-hp-btn");
    if (canEdit) {
      btn.onclick = () => {
        const value = parseInt($("#sheet-hp-set").value, 10);
        if (!Number.isNaN(value)) wsSend({type: "set_hp", character_id: cid, value});
      };
    } else {
      btn.disabled = true;
    }
    if (canEdit) {
      const coinsBtn = $("#inv-coins-btn");
      if (coinsBtn) coinsBtn.onclick = () => {
        const val = (sel) => Math.max(0, parseInt($(sel).value || "0", 10) || 0);
        wsSend({type: "inv_coins", character_id: cid, coins: {
          pp: val("#inv-coin-pp"), gp: val("#inv-coin-gp"), sp: val("#inv-coin-sp"), cp: val("#inv-coin-cp"),
        }});
      };
      $("#sheet").querySelectorAll("[data-inv-fx]").forEach((b) => {
        b.onclick = () => {
          const parts = b.dataset.invFx.split(":");
          wsSend({type: "inv_exchange", character_id: cid, from: parts[0], to: parts[1], amount: parseInt(parts[2], 10)});
        };
      });
      const addBtn = $("#inv-add-btn");
      if (addBtn) addBtn.onclick = () => {
        const name = $("#inv-add-name").value.trim();
        if (!name) return;
        wsSend({
          type: "inv_add",
          character_id: cid,
          item: name,
          qty: parseInt($("#inv-add-qty").value || "1", 10),
          note: $("#inv-add-note") ? $("#inv-add-note").value.trim() : "",
        });
      };
      $("#sheet").querySelectorAll("[data-inv-note]").forEach((b) => {
        b.onclick = () => {
          const current = b.dataset.oldNote || "";
          const next = prompt("备注（留空清除）", current);
          if (next === null) return;
          wsSend({type: "inv_note", character_id: cid, item: b.dataset.invNote, old_note: current, note: next});
        };
      });
      const buyBtn = $("#inv-buy-btn");
      if (buyBtn) buyBtn.onclick = () => {
        wsSend({type: "inv_buy", character_id: cid, item: $("#inv-shop-item").value, qty: parseInt($("#inv-shop-qty").value || "1", 10)});
      };
      $("#sheet").querySelectorAll("[data-inv-plus]").forEach((b) => {
        b.onclick = () => wsSend({type: "inv_add", character_id: cid, item: b.dataset.invPlus, qty: 1});
      });
      $("#sheet").querySelectorAll("[data-inv-minus]").forEach((b) => {
        b.onclick = () => wsSend({type: "inv_remove", character_id: cid, item: b.dataset.invMinus, qty: 1});
      });
      $("#sheet").querySelectorAll("[data-inv-del]").forEach((b) => {
        b.onclick = () => wsSend({type: "inv_remove", character_id: cid, item: b.dataset.invDel, qty: parseInt(b.dataset.qty || "999", 10)});
      });
      $("#sheet").querySelectorAll("[data-inv-sell]").forEach((b) => {
        b.onclick = () => wsSend({type: "inv_sell", character_id: cid, item: b.dataset.invSell, qty: 1});
      });
      const shopSel = $("#inv-shop-item");
      const shopPreview = $("#inv-shop-preview");
      if (shopSel && shopPreview) {
        const showShopPreview = () => {
          const it = ITEMS[shopSel.value];
          shopPreview.textContent = it ? itemTip(shopSel.value, it.name_zh) : "";
          shopPreview.classList.toggle("hidden", !it);
        };
        shopSel.onchange = showShopPreview;
        // 买入搜索：按名称/分类过滤下拉（保留当前选中项；无输入时还原完整列表）
        const shopSearch = $("#inv-shop-search");
        if (shopSearch) {
          const allItems = Object.entries(ITEMS)
            .filter(([, it]) => it.name_zh)
            .map(([id, it]) => ({id, name: it.name_zh, cat: it.category_zh || "其他", entry: it}))
            .sort((a, b) => a.name.localeCompare(b.name, "zh"));
          const rebuildShop = () => {
            const query = shopSearch.value.trim().toLowerCase();
            const hits = allItems.filter((x) => !query || x.name.toLowerCase().includes(query) || x.cat.toLowerCase().includes(query));
            const previous = shopSel.value;
            shopSel.innerHTML = hits.length
              ? hits.map((x) => {
                  const price = x.entry.cost_cp != null ? `（${moneyFmt(x.entry.cost_cp / 100)}）` : "（无定价）";
                  return `<option value="${esc(x.id)}">${esc(x.name)}${price}</option>`;
                }).join("")
              : '<option value="">（没有匹配的物品）</option>';
            if (hits.some((x) => x.id === previous)) shopSel.value = previous;
            showShopPreview();
          };
          shopSearch.oninput = rebuildShop;
          rebuildShop();
        }
        showShopPreview();
      }
      const addInput = $("#inv-add-name");
      const addPreview = $("#inv-add-preview");
      if (addInput && addPreview) {
        addInput.oninput = () => {
          const key = addInput.value.trim();
          const hit = Object.entries(ITEMS).find(([, it]) => it.name_zh === key);
          addPreview.textContent = hit ? itemTip(hit[0], hit[1].name_zh) : "";
          addPreview.classList.toggle("hidden", !hit);
        };
      }
      const asiPlus2 = $("#asi-plus2");
      if (asiPlus2) asiPlus2.onclick = () => {
        wsSend({type: "choose_asi", character_id: cid, asi: {[$("#asi-a").value]: 2}});
      };
      const asiSplit = $("#asi-split");
      if (asiSplit) asiSplit.onclick = () => {
        const a = $("#asi-b").value;
        const b = $("#asi-c").value;
        if (a === b) {
          toast("两项各 +1 请选择不同属性（同一属性请用 +2）");
          return;
        }
        wsSend({type: "choose_asi", character_id: cid, asi: {[a]: 1, [b]: 1}});
      };
      const asiTalentBtn = $("#asi-talent-btn");
      if (asiTalentBtn) asiTalentBtn.onclick = () => {
        wsSend({type: "choose_asi", character_id: cid, asi: {[$("#asi-d").value]: 1}, talent: $("#asi-talent").value});
      };
      const subclassBtn = $("#btn-choose-subclass");
      if (subclassBtn) subclassBtn.onclick = () => {
        wsSend({type: "choose_subclass", character_id: cid, subclass: $("#subclass-pick").value});
      };
      const subclassPick = $("#subclass-pick");
      if (subclassPick) {
        const previewBox = $("#subclass-preview");
        const refreshSubclassPreview = () => {
          if (previewBox) previewBox.innerHTML = subclassPreviewHtml(char.class_id, subclassPick.value);
        };
        subclassPick.onchange = refreshSubclassPreview;
        refreshSubclassPreview();
      }
      $("#sheet").querySelectorAll("[data-boonlevel]").forEach((btn) => {
        btn.onclick = () => {
          const lv = btn.dataset.boonlevel;
          const sel = document.getElementById(`boon-${lv}`);
          if (sel) wsSend({type: "choose_boon", character_id: cid, level: Number(lv), boon: sel.value});
        };
      });
      const luckGain = $("#luck-gain");
      if (luckGain) luckGain.onclick = () => wsSend({type: "luck_gain", character_id: cid, amount: 1, reason: "手动授予"});
      const luckSpend = $("#luck-spend");
      if (luckSpend) luckSpend.onclick = () => wsSend({type: "luck_spend", character_id: cid, amount: 1, reason: "手动消耗"});
      const hdSpend = $("#hd-spend");
      if (hdSpend) hdSpend.onclick = () => wsSend({type: "hit_die_spend", character_id: cid, count: 1});
      $("#sheet").querySelectorAll("[data-spell-add]").forEach((btn) => {
        btn.onclick = () => {
          const bucket = btn.dataset.spellAdd;
          const sel = document.getElementById(`spell-add-${bucket}`);
          if (sel && sel.value) wsSend({type: "spell_learn", character_id: cid, spell: sel.value, bucket});
        };
      });
      // 选法术预览：下拉选择即显示完整说明（与物品/子职业预览同一模式）
      $("#sheet").querySelectorAll('select[id^="spell-add-"]').forEach((sel) => {
        const box = document.getElementById(`${sel.id}-preview`);
        const update = () => {
          if (box) box.textContent = SPELLS[sel.value] ? spellTip(SPELLS[sel.value]) : "";
        };
        sel.onchange = update;
        update();
      });
      $("#sheet").querySelectorAll("[data-spell-remove]").forEach((btn) => {
        btn.onclick = () => wsSend({type: "spell_forget", character_id: cid, spell: btn.dataset.spellRemove, bucket: btn.dataset.bucket});
      });
      const spellSwapBtn = $("#spell-swap-btn");
      if (spellSwapBtn) spellSwapBtn.onclick = () => {
        const oldSel = $("#spell-swap-old");
        const newSel = $("#spell-swap-new");
        if (oldSel && newSel && newSel.value) {
          wsSend({type: "spell_swap", character_id: cid, old: oldSel.value, spell: newSel.value});
        }
      };
      const swapOld = $("#spell-swap-old");
      const swapNew = $("#spell-swap-new");
      if (swapOld && swapNew) {
        const swapBox = $("#spell-swap-preview");
        const updateSwap = () => {
          if (!swapBox) return;
          const oldFull = SPELLS[swapOld.value];
          const newFull = SPELLS[swapNew.value];
          const parts = [];
          if (oldFull) parts.push(`【换出】\n${spellTip(oldFull)}`);
          if (newFull) parts.push(`【换入】\n${spellTip(newFull)}`);
          swapBox.textContent = parts.join("\n\n");
        };
        swapOld.onchange = updateSwap;
        swapNew.onchange = updateSwap;
        updateSwap();
      }
      const deleteBtn = $("#btn-delete-char");
      if (deleteBtn) deleteBtn.onclick = async () => {
        if (!confirm(`删除角色卡「${char.name}」？此操作不可恢复（角色、装备与页面都会消失）。`)) return;
        const res = await fetch("/api/delete_character", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({character_id: cid}),
        });
        const out = await res.json();
        if (!out.ok) {
          toast(out.error || "删除失败");
          return;
        }
        if (me().char_id === cid) {
          location.href = "/";
          return;
        }
        $("#sheet-modal").classList.add("hidden");
      };
      const wearBtn = $("#wear-armor-btn");
      if (wearBtn) wearBtn.onclick = () => {
        wsSend({
          type: "wear_armor",
          character_id: cid,
          armor: $("#wear-armor-select").value,
          shield: $("#wear-shield-box").checked,
        });
      };
    }
    $("#sheet").querySelectorAll("[data-slotup]").forEach((slotBtn) => {
      slotBtn.onclick = () => {
        const slot = (char.spell_slots || {})[slotBtn.dataset.slotup];
        if (slot) wsSend({type: "set_slot", character_id: cid, level: slotBtn.dataset.slotup, used: (slot.used || 0) + 1});
      };
    });
    $("#sheet").querySelectorAll("[data-slotdown]").forEach((slotBtn) => {
      slotBtn.onclick = () => {
        const slot = (char.spell_slots || {})[slotBtn.dataset.slotdown];
        if (slot) wsSend({type: "set_slot", character_id: cid, level: slotBtn.dataset.slotdown, used: Math.max(0, (slot.used || 0) - 1)});
      };
    });
    $("#sheet").querySelectorAll("[data-drop]").forEach((dropBtn) => {
      dropBtn.onclick = () => {
        wsSend({type: "drop_effect", character_id: cid, name: dropBtn.dataset.drop});
      };
    });
    $("#btn-close-sheet").onclick = () => {
      $("#sheet-modal").classList.add("hidden");
      st.sheetCid = null;
    };
  }

  function sendChat() {
    const box = $("#chat-input");
    const text = box.value.trim();
    if (!text) return;
    wsSend({type: "chat", text});
    box.value = "";
  }

  $("#btn-send").onclick = sendChat;
  $("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendChat();
    }
  });
  function renderQuickRolls() {
    const area = $("#quick-rolls");
    if (!area) return;
    area.innerHTML = quickRolls.length
      ? quickRolls.map((r) => `<button class="btn" data-quick="${esc(r.expr)}" data-quick-label="${esc(r.label || r.expr)}">${esc(r.label || r.expr)}</button>`).join("")
      : '<span class="dim">还没有固定掷骰，点下方「自定义固定掷骰」添加。</span>';
    area.querySelectorAll("[data-quick]").forEach((btn) => {
      btn.onclick = () => {
        $("#roll-expr").value = btn.dataset.quick;
        // 自定义名称与表达式不同时，顺手填进原因，掷骰日志更清楚
        if (btn.dataset.quickLabel && btn.dataset.quickLabel !== btn.dataset.quick) {
          $("#roll-reason").value = btn.dataset.quickLabel;
        }
      };
    });
  }

  function quickEditRow(roll) {
    return `<div class="quick-edit-row">
      <input class="qr-label" maxlength="20" placeholder="名称（如 长剑）" value="${esc(roll.label || "")}">
      <input class="qr-expr" maxlength="40" placeholder="表达式（如 1d20+5）" value="${esc(roll.expr || "")}">
      <button class="btn small" data-qr-del>删除</button>
    </div>`;
  }

  function renderQuickEditor() {
    $("#quick-editor-rows").innerHTML = quickRolls.map(quickEditRow).join("");
  }

  async function saveQuickRolls() {
    const rows = [...$("#quick-editor-rows").querySelectorAll(".quick-edit-row")].map((row) => ({
      label: row.querySelector(".qr-label").value.trim(),
      expr: row.querySelector(".qr-expr").value.trim(),
    })).filter((row) => row.expr);
    if (rows.length > 8) {
      toast("固定掷骰最多 8 条");
      return;
    }
    let out;
    try {
      const res = await fetch("/api/quick_rolls", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rolls: rows}),
      });
      out = await res.json();
    } catch (err) {
      toast("保存失败：网络错误");
      return;
    }
    if (!out.ok) {
      toast(out.error || "保存失败");
      return;
    }
    quickRolls = out.rolls || [];
    renderQuickRolls();
    $("#quick-editor").classList.add("hidden");
    toast("固定掷骰已保存");
  }

  $("#btn-quick-edit").onclick = () => {
    renderQuickEditor();
    $("#quick-editor").classList.remove("hidden");
  };
  $("#quick-add-row").onclick = () => {
    if ($("#quick-editor-rows").children.length >= 8) {
      toast("固定掷骰最多 8 条");
      return;
    }
    $("#quick-editor-rows").insertAdjacentHTML("beforeend", quickEditRow({label: "", expr: ""}));
  };
  $("#quick-editor-rows").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-qr-del]");
    if (btn) btn.closest(".quick-edit-row").remove();
  });
  $("#quick-save").onclick = () => { saveQuickRolls(); };
  $("#quick-cancel").onclick = () => { $("#quick-editor").classList.add("hidden"); };
  $("#quick-reset").onclick = () => {
    $("#quick-editor-rows").innerHTML = DEFAULT_QUICK_ROLLS.map(quickEditRow).join("");
  };
  renderQuickRolls();
  $("#btn-roll").onclick = () => {
    const expr = $("#roll-expr").value.trim();
    if (!expr) {
      toast("请输入掷骰表达式");
      return;
    }
    wsSend({type: "roll", expr, reason: $("#roll-reason").value.trim()});
  };
  $("#btn-check").onclick = () => {
    const ability = $("#check-ability").value;
    const char = myChar();
    const modifier = char ? char.mods[ability] : 0;
    const dcRaw = $("#check-dc").value;
    wsSend({
      type: "check",
      modifier,
      dc: dcRaw === "" ? null : parseInt(dcRaw, 10),
      mode: $("#check-mode").value,
      reason: `${ABN[ability]}检定`,
    });
  };
  const boardCanvas = $("#board");
  if (boardCanvas) {
    boardCanvas.addEventListener("mousedown", (event) => {
      const token = tokenAt(feetFromEvent(event));
      if (!token) {
        if (boardZoom > 1) {
          // 放大后：空白处拖动平移视图
          panDrag = {pxX: event.clientX, pxY: event.clientY, cx: boardCenter.x, cy: boardCenter.y};
          boardCanvas.style.cursor = "move";
        } else {
          boardSelected = null;
          renderBoard();
        }
        return;
      }
      boardSelected = token.id;
      if (!canDragToken(token)) {
        renderBoard();
        return;
      }
      if (token.kind === "pc" && me().char_id === token.id && st.combat && st.combat.active) {
        const current = currentCombatant();
        if (!current || current.id !== token.id) {
          if (!canDM()) {
            toast(`还没轮到 ${token.name} 行动`);
            renderBoard();
            return;
          }
        }
      }
      boardDrag = {id: token.id, x: token.pos.x, y: token.pos.y};
      boardCanvas.style.cursor = "grabbing";
      renderBoard();
    });
    window.addEventListener("mousemove", (event) => {
      if (panDrag) {
        const s = boardScale();
        const rect = boardCanvas.getBoundingClientRect();
        const dx = (event.clientX - panDrag.pxX) * (boardCanvas.width / rect.width) / s;
        const dy = (event.clientY - panDrag.pxY) * (boardCanvas.height / rect.height) / s;
        boardCenter.x = panDrag.cx - dx;
        boardCenter.y = panDrag.cy - dy;
        clampBoardView();
        renderBoard();
        return;
      }
      if (!boardDrag) return;
      const ft = feetFromEvent(event);
      boardDrag.x = Math.max(0, Math.min(BOARD_W, ft.x));
      boardDrag.y = Math.max(0, Math.min(BOARD_H, ft.y));
      renderBoard();
    });
    boardCanvas.addEventListener("mousemove", (event) => {
      const tipEl = $("#board-tip");
      if (!tipEl) return;
      if (boardDrag || panDrag) {
        tipEl.classList.add("hidden");
        return;
      }
      const token = tokenAt(feetFromEvent(event));
      if (!token) {
        tipEl.classList.add("hidden");
        boardCanvas.style.cursor = "grab";
        return;
      }
      tipEl.textContent = tokenTip(token);
      tipEl.style.left = `${event.offsetX + 14}px`;
      tipEl.style.top = `${event.offsetY + 14}px`;
      tipEl.classList.remove("hidden");
      boardCanvas.style.cursor = "pointer";
    });
    boardCanvas.addEventListener("mouseleave", () => {
      const tipEl = $("#board-tip");
      if (tipEl) tipEl.classList.add("hidden");
    });
    boardCanvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
      setBoardZoom(boardZoom * factor, event);
    });
    window.addEventListener("mouseup", () => {
      if (panDrag) {
        panDrag = null;
        boardCanvas.style.cursor = boardZoom > 1 ? "move" : "grab";
        return;
      }
      if (!boardDrag) return;
      const drag = boardDrag;
      boardDrag = null;
      boardCanvas.style.cursor = "grab";
      wsSend({type: "move_token", id: drag.id, x: Math.round(drag.x * 2) / 2, y: Math.round(drag.y * 2) / 2});
      renderBoard();
    });
  }
  $("#btn-dash").onclick = () => wsSend({type: "toggle_dash"});
  $("#btn-zoom-in").onclick = () => setBoardZoom(boardZoom * 1.25);
  $("#btn-zoom-out").onclick = () => setBoardZoom(boardZoom / 1.25);
  $("#btn-zoom-reset").onclick = () => {
    boardZoom = 1;
    boardCenter = {x: BOARD_W / 2, y: BOARD_H / 2};
    const label = $("#board-zoom");
    if (label) label.textContent = "100%";
    renderBoard();
  };
  $("#btn-grid").onclick = () => {
    boardGrid = !boardGrid;
    $("#btn-grid").classList.toggle("primary", boardGrid);
    renderBoard();
  };
  function dmAction(name, args) {
    wsSend({type: "action", name, args});
  }
  const dcTimeBtn = $("#dc-time-btn");
  if (dcTimeBtn) {
    dcTimeBtn.onclick = () => dmAction("advance_time", {minutes: parseInt($("#dc-minutes").value || "10", 10), reason: $("#dc-time-reason").value.trim() || "GM 推进时间"});
    $("#dc-quest-btn").onclick = () => dmAction("update_quest", {quest: $("#dc-quest").value.trim(), status: $("#dc-status").value, note: $("#dc-note").value.trim()});
    $("#dc-note-btn").onclick = () => dmAction("add_world_note", {kind: $("#dc-note-kind").value, name: $("#dc-note-name").value.trim(), note: $("#dc-note-text").value.trim()});
    $("#dc-level-btn").onclick = () => dmAction("level_up", {character: $("#dc-char").value});
    $("#dc-hp-btn").onclick = () => dmAction("adjust_hp", {target: $("#dc-char").value, delta: parseInt($("#dc-hp").value || "-5", 10)});
    $("#dc-party-level-btn").onclick = () => {
      if (!confirm("为全队上桌角色（含同伴）各升 1 级？已达等级上限的会自动跳过。")) return;
      dmAction("level_up_party", {});
    };
    const dcOpenSheet = $("#dc-open-sheet");
    if (dcOpenSheet) dcOpenSheet.onclick = () => {
      const cid = $("#dc-char").value;
      if (cid) openSheet(cid);
    };
    const dcSpell = $("#dc-spell");
    dcSpell.innerHTML = Object.entries(window.SPELLS || {})
      .map(([sid, s]) => ({id: sid, name: s.name, level: s.level}))
      .sort((a, b) => a.level - b.level || a.name.localeCompare(b.name, "zh"))
      .map((s) => `<option value="${esc(s.id)}">${esc(s.name)}（${s.level === 0 ? "戏法" : s.level + " 环"}）</option>`)
      .join("");
    $("#dc-learn-btn").onclick = () => dmAction("learn_spell", {character: $("#dc-char").value, spell: dcSpell.value});
    $("#dc-forget-btn").onclick = () => dmAction("forget_spell", {character: $("#dc-char").value, spell: dcSpell.value});
    const dcSpellPreview = $("#dc-spell-preview");
    const updateDcSpellPreview = () => {
      if (dcSpellPreview) dcSpellPreview.textContent = SPELLS[dcSpell.value] ? spellTip(SPELLS[dcSpell.value]) : "";
    };
    dcSpell.onchange = updateDcSpellPreview;
    updateDcSpellPreview();
    const itemNameList = $("#item-name-list");
    if (itemNameList) {
      itemNameList.innerHTML = Object.values(ITEMS)
        .filter((it) => it.name_zh)
        .map((it) => `<option value="${esc(it.name_zh)}"></option>`)
        .join("");
    }
    const dcRewardItem = $("#dc-reward-item");
    if (dcRewardItem) dcRewardItem.innerHTML = shopOptions();
    const dcMon = $("#dc-mon");
    const dcMonPreview = $("#dc-mon-preview");
    const showMonPreview = () => {
      const m = MONSTERS[dcMon.value];
      if (dcMonPreview) {
        dcMonPreview.textContent = m ? monsterTip(m) : "";
        dcMonPreview.classList.toggle("hidden", !m);
      }
    };
    const crMinSel = $("#dc-mon-cr-min");
    const crMaxSel = $("#dc-mon-cr-max");
    const crRange = $("#dc-mon-range");
    const crLabel = (v) => {
      if (v === 0) return "CR 0";
      if (Math.abs(v - 0.125) < 1e-6) return "CR 1/8";
      if (Math.abs(v - 0.25) < 1e-6) return "CR 1/4";
      if (Math.abs(v - 0.5) < 1e-6) return "CR 1/2";
      return "CR " + Math.round(v);
    };
    const crValues = [...new Set(Object.values(MONSTERS).map((m) => parseFloat(m.cr) || 0))].sort((a, b) => a - b);
    const crOptions = '<option value="">全部</option>' + crValues.map((v) => `<option value="${v}">${crLabel(v)}</option>`).join("");
    crMinSel.innerHTML = crOptions;
    crMaxSel.innerHTML = crOptions;
    const fillDcMonsters = () => {
      const minV = crMinSel.value === "" ? null : parseFloat(crMinSel.value);
      const maxV = crMaxSel.value === "" ? null : parseFloat(crMaxSel.value);
      const picked = Object.entries(MONSTERS)
        .map(([id, m]) => ({id, name: m.name, label: m.cr_label || m.cr, crv: parseFloat(m.cr) || 0}))
        .filter((m) => (minV === null || m.crv >= minV) && (maxV === null || m.crv <= maxV))
        .sort((a, b) => a.crv - b.crv || a.name.localeCompare(b.name, "zh"));
      dcMon.innerHTML = picked
        .map((m) => `<option value="${esc(m.id)}">${esc(m.name)}（CR ${esc(m.label)}）</option>`)
        .join("");
      if (crRange) crRange.textContent = `共 ${picked.length} 种`;
      showMonPreview();
    };
    dcMon.onchange = showMonPreview;
    crMinSel.onchange = fillDcMonsters;
    crMaxSel.onchange = fillDcMonsters;
    fillDcMonsters();
    $("#dc-spawn-btn").onclick = () => {
      const id = dcMon.value;
      if (!id) return;
      const count = Math.max(1, Math.min(12, parseInt($("#dc-mon-count").value || "1", 10)));
      dmAction("spawn_monsters", {list: [{id, count}]});
    };
    $("#dc-next-turn-btn").onclick = () => dmAction("advance_turn", {});
    $("#dc-end-combat-btn").onclick = () => {
      const reason = prompt("结束战斗的原因（可留空）", "") || "GM 手动结束";
      dmAction("end_combat", {reason});
    };
    $("#dc-reward-item-btn").onclick = () => {
      const target = $("#dc-reward-target").value;
      const item = $("#dc-reward-item").value;
      if (!item) return;
      const qty = Math.max(1, parseInt($("#dc-reward-qty").value || "1", 10) || 1);
      if (target === "@party") dmAction("reward_party", {item, qty});
      else dmAction("add_item", {character: target, item, qty});
    };
    $("#dc-reward-gold-btn").onclick = () => {
      const target = $("#dc-reward-target").value;
      const gold = parseFloat($("#dc-reward-gold").value || "0");
      if (!gold) return;
      if (target === "@party") dmAction("reward_party", {gold});
      else dmAction("adjust_gold", {character: target, delta: gold});
    };
    $("#dc-narrate-btn").onclick = () => {
      const text = $("#dc-narrate").value.trim();
      if (text) {
        wsSend({type: "dm_narrate", text});
        $("#dc-narrate").value = "";
      }
    };
  }
  function setupCollapsibleCards() {
    document.querySelectorAll(".side .card").forEach((card) => {
      const head = card.querySelector(":scope > h2");
      if (!head || card.dataset.collapseReady) return;
      card.dataset.collapseReady = "1";
      head.classList.add("collapsible");
      const body = document.createElement("div");
      body.className = "card-body";
      let node = head.nextSibling;
      while (node) {
        const next = node.nextSibling;
        body.appendChild(node);
        node = next;
      }
      card.appendChild(body);
      const label = (head.childNodes[0] ? head.childNodes[0].textContent : head.textContent).trim().slice(0, 16);
      const key = "weavlum-collapse:" + label;
      if (localStorage.getItem(key) === "1") card.classList.add("collapsed");
      head.onclick = () => {
        card.classList.toggle("collapsed");
        localStorage.setItem(key, card.classList.contains("collapsed") ? "1" : "0");
      };
    });
  }
  setupCollapsibleCards();
  document.querySelectorAll("[data-qf]").forEach((btn) => {
    btn.onclick = () => {
      st.questFilter = btn.dataset.qf;
      localStorage.setItem("weavlum-quest-filter", st.questFilter);
      renderQuests();
    };
  });
  window.addEventListener("error", (event) => {
    reportClient("js_error", {message: String(event.message || ""), source: event.filename, line: event.lineno});
  });
  window.addEventListener("unhandledrejection", (event) => {
    reportClient("js_rejection", {message: String(event.reason || "")});
  });
  connect();
})();
