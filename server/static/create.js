(() => {
  const SRD = window.SRD;
  const ABN = window.ABILITY_NAMES;
  const SPELLS = window.SPELLS || {};
  const SPELL_RULES = window.SPELL_RULES || {counts: {}, slots: {}, recommended: {}};
  const ITEMS = window.ITEMS || {};
  const EQUIP = window.EQUIP_SPECS || {};
  const CHARGEN = SRD.chargen || {};
  const HERITAGES = SRD.heritages || {};
  const TALENTS = SRD.talents || {};
  const ABILITIES = Object.keys(ABN);
  const PB_COSTS = Object.fromEntries(Object.entries(CHARGEN.pointbuy_costs || {8: 0}).map(([k, v]) => [Number(k), v]));
  const PB_TOTAL = CHARGEN.pointbuy_total || 32;
  const STD = CHARGEN.standard_array || window.STANDARD_ARRAY || [15, 14, 13, 12, 10, 8];
  const $ = (sel) => document.querySelector(sel);

  const st = {
    method: "pointbuy",
    pb: {str: 8, dex: 8, con: 8, int: 8, wis: 8, cha: 8},
    stdAssign: {},
    pool: null,
    poolAssign: {},
    rollPlus2: "",
    rollPlus1: "",
    classSkills: [],
    bgSkills: [],
    humanSkill: "",
    bgTalent: "",
    freeTalent: "",
    talentSchool: "",
    adaptation: "",
    adaptationSpellAbility: "",
    equip: {},
    spellMode: "class",
    spellCantrips: [],
    spellMain: [],
    spellRituals: [],
  };

  const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const mod = (score) => Math.floor((score - 10) / 2);
  const fmtMod = (m) => (m >= 0 ? `+${m}` : `${m}`);
  const skillTip = (id) => ((SRD.skills[id] && SRD.skills[id].desc) || "");
  const talentTip = (id) => {
    const t = TALENTS[id];
    if (!t) return "";
    const lines = [`${t.name}（${t.name_en}）`, `类别：${({magic: "魔法", martial: "武艺", technical: "技术"})[t.list] || t.list || "—"}`];
    if (t.prerequisite_zh) lines.push(`先决：${t.prerequisite_zh}`);
    if (t.repeatable) lines.push("可重复选择");
    if (t.desc_zh) lines.push("", t.desc_zh);
    const benefits = Array.isArray(t.benefits_zh) ? t.benefits_zh : [];
    if (benefits.length) lines.push("", ...benefits.map((b) => "· " + b));
    return lines.join("\n");
  };
  const spellTip = (s) => {
    const level = s.level === 0 ? "戏法" : s.level + " 环";
    const lines = [
      `${s.name}（${s.name_en}）· ${level} · ${s.school}`,
      `施法时间：${s.casting_time} · 射程：${s.range}`,
      `成分：${(s.components || []).join("、")}${s.material ? "（" + s.material + "）" : ""}`,
      `持续：${s.duration}${s.concentration ? " · 需专注" : ""}${s.ritual ? " · 可仪式" : ""}`,
    ];
    if (s.damage) lines.push(`伤害/数值：${JSON.stringify(s.damage)}`);
    if (s.desc_zh || s.desc) lines.push("", s.desc_zh || s.desc);
    if (s.higher_zh || s.higher_level) lines.push("", "升环施法：" + (s.higher_zh || s.higher_level));
    return lines.join("\n");
  };
  const raceTip = (id) => {
    const r = SRD.races[id];
    if (!r) return "";
    return [`${r.name} · 速度 ${r.speed} 尺`, "", r.traits_desc || r.traits || ""].join("\n");
  };
  const heritageTip = (id) => {
    const h = HERITAGES[id];
    if (!h) return "";
    return [`${h.name}传承`, "", h.traits_desc || (h.traits || []).join("；")].join("\n");
  };
  const currentRace = () => SRD.races[$("#f-race").value];
  const currentHeritage = () => HERITAGES[$("#f-heritage").value];
  const currentClass = () => SRD.classes[$("#f-class").value];
  const currentBackground = () => SRD.backgrounds[$("#f-background").value];
  const talentName = (id) => (TALENTS[id] ? TALENTS[id].name : id);

  function init() {
    $("#f-race").innerHTML = Object.entries(SRD.races)
      .map(([id, race]) => `<option value="${id}">${esc(race.name)}</option>`)
      .join("");
    $("#f-heritage").innerHTML = Object.entries(HERITAGES)
      .map(([id, h]) => `<option value="${id}">${esc(h.name)}</option>`)
      .join("");
    $("#f-class").innerHTML = Object.entries(SRD.classes)
      .map(([id, klass]) => `<option value="${id}">${esc(klass.name)}（d${klass.hit_die}）</option>`)
      .join("");
    $("#f-background").innerHTML = Object.entries(SRD.backgrounds)
      .map(([id, bg]) => `<option value="${id}">${esc(bg.name)}</option>`)
      .join("");
    $("#f-race").value = "human";
    $("#f-heritage").value = Object.keys(HERITAGES)[0] || "";
    $("#f-class").value = "fighter";
    $("#f-background").value = "soldier";
    document.querySelectorAll(".tab[data-method]").forEach((tab) => {
      tab.onclick = () => {
        st.method = tab.dataset.method;
        renderMethods();
        renderAbilities();
        renderPreview();
      };
    });
    $("#f-race").onchange = () => {
      renderDescriptions();
      renderAdaptation();
      renderSkills();
      renderTalents();
      renderAbilities();
      renderPreview();
    };
    $("#f-heritage").onchange = () => {
      renderDescriptions();
      renderPreview();
    };
    $("#f-adaptation").onchange = () => {
      st.adaptation = $("#f-adaptation").value;
      renderAdaptation();
      renderPreview();
    };
    $("#f-adaptation-ability").onchange = () => {
      st.adaptationSpellAbility = $("#f-adaptation-ability").value;
      renderPreview();
    };
    $("#f-class").onchange = () => {
      const klass = currentClass();
      st.classSkills = [];
      st.spellCantrips = [];
      st.spellMain = [];
      st.spellRituals = [];
      st.equip[$("#f-class").value] = null;
      renderDescriptions();
      renderAbilities();
      renderSkills();
      renderTalents();
      renderSpells();
      renderEquipment();
      renderPreview();
    };
    $("#f-background").onchange = () => {
      st.classSkills = st.classSkills.filter((id) => !(currentBackground().skill_choices || []).includes(id));
      st.bgSkills = [];
      st.bgTalent = "";
      renderDescriptions();
      renderSkills();
      renderTalents();
      renderPreview();
    };
    $("#f-bg-talent").onchange = () => {
      st.bgTalent = $("#f-bg-talent").value;
      renderSchoolField();
      renderPreview();
    };
    $("#f-free-talent").onchange = () => {
      st.freeTalent = $("#f-free-talent").value;
      renderSchoolField();
      renderPreview();
    };
    $("#f-talent-school").onchange = () => {
      st.talentSchool = $("#f-talent-school").value;
      renderPreview();
    };
    $("#f-name").oninput = renderPreview;
    $("#btn-submit").onclick = submit;
    $("#btn-recommend").onclick = recommendAllocation;
    $("#btn-spell-recommend").onclick = applyRecommendedSpells;
    document.querySelectorAll("[data-spellmode]").forEach((tab) => {
      tab.onclick = () => {
        st.spellMode = tab.dataset.spellmode;
        renderSpells();
      };
    });
    renderMethods();
    renderDescriptions();
    renderAdaptation();
    renderAbilities();
    renderSkills();
    renderTalents();
    renderSpells();
    renderEquipment();
    renderPreview();
  }

  function pointBuyCost() {
    return ABILITIES.reduce((sum, key) => sum + PB_COSTS[st.pb[key]], 0);
  }

  function renderMethods() {
    document.querySelectorAll(".tab[data-method]").forEach((tab) => tab.classList.toggle("active", tab.dataset.method === st.method));
    const area = $("#method-area");
    if (st.method === "pointbuy") {
      area.innerHTML = `<p class="dim">剩余点数：<strong>${PB_TOTAL - pointBuyCost()}</strong> / ${PB_TOTAL}</p>`;
    } else if (st.method === "standard") {
      area.innerHTML = `<p class="dim">把 ${STD.join(" / ")} 各分配给一项属性。</p>`;
    } else if (!st.pool) {
      area.innerHTML = `<button class="btn" id="btn-roll-abilities">开始掷骰</button> <span class="dim">4d6 取三高 × 6 组</span>`;
    } else {
      area.innerHTML = `<p class="dim">结果：${st.pool.map((p, i) => `第${i + 1}组 ${p.total}`).join("　")}（每组 = 4d6 去最低）</p>
        <p class="dim">BFRD 掷骰法：分配后，再选一项 ≤16 的属性 +2、一项 ≤17 的属性 +1。</p>`;
    }
    const rollBtn = $("#btn-roll-abilities");
    if (rollBtn) rollBtn.onclick = rollAbilities;
  }

  function progressionTable(klassId, highlightLevel) {
    const klass = SRD.classes[klassId];
    const prog = klass && klass.progression;
    if (!prog || !prog.columns || !prog.rows) return "";
    const cols = prog.columns;
    const head = cols.map((c) => `<th>${esc(c.label)}</th>`).join("");
    const rows = Object.entries(prog.rows)
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .map(([level, vals]) => {
        const tds = cols.map((c) => {
          if (c.key === "level") return `<td>${level}</td>`;
          const v = vals[c.key];
          if (c.key === "features") return `<td>${esc((v || []).join("、"))}</td>`;
          if (v === undefined || v === null || v === "" || v === 0 || v === "-") return "<td>—</td>";
          return `<td>${esc(String(v))}</td>`;
        }).join("");
        const cls = highlightLevel && Number(level) === Number(highlightLevel)
          ? ' class="hl"'
          : (highlightLevel && Number(level) === Number(highlightLevel) + 1 ? ' class="next"' : "");
        return `<tr${cls}>${tds}</tr>`;
      }).join("");
    return `<table class="prog-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  }

  function renderAdaptation() {
    const race = currentRace();
    const adaptations = (race && race.adaptations) || [];
    const field = $("#adaptation-field");
    field.classList.toggle("hidden", !adaptations.length);
    if (!adaptations.length) {
      st.adaptation = "";
      st.adaptationSpellAbility = "";
      $("#adaptation-desc").textContent = "";
      $("#adaptation-ability-field").classList.add("hidden");
      return;
    }
    if (!st.adaptation || !adaptations.some((a) => a.name === st.adaptation)) {
      st.adaptation = adaptations[0].name;
    }
    $("#f-adaptation").innerHTML = adaptations
      .map((a) => `<option value="${esc(a.name)}" ${a.name === st.adaptation ? "selected" : ""}>${esc(a.name)}</option>`)
      .join("");
    const chosen = adaptations.find((a) => a.name === st.adaptation) || adaptations[0];
    const label = $("#adaptation-label");
    if (label) label.dataset.tip = `自然适应（${chosen.name}${chosen.name_en ? "，" + chosen.name_en : ""}）\n\n${chosen.desc || ""}`;
    $("#adaptation-desc").textContent = `自然适应（${chosen.name}）：${chosen.desc || ""}`;
    const needAbility = st.adaptation === "侏儒";
    $("#adaptation-ability-field").classList.toggle("hidden", !needAbility);
    if (needAbility) {
      if (!st.adaptationSpellAbility) st.adaptationSpellAbility = "wis";
      $("#f-adaptation-ability").innerHTML = [["int", "智力"], ["wis", "感知"], ["cha", "魅力"]]
        .map(([key, name]) => `<option value="${key}" ${key === st.adaptationSpellAbility ? "selected" : ""}>${name}</option>`)
        .join("");
      const abilityLabel = $("#adaptation-ability-label");
      if (abilityLabel) abilityLabel.dataset.tip = "侏儒自然适应：你学会次级幻影戏法，并以此属性作为它的施法属性";
    } else {
      st.adaptationSpellAbility = "";
    }
  }

  function renderDescriptions() {
    const race = currentRace();
    const heritage = currentHeritage();
    const klass = currentClass();
    const bg = currentBackground();
    $("#race-desc").textContent = `血统特性（${race.name} · 速度 ${race.speed} 尺）：\n${race.traits_desc || race.traits}`;
    const raceLabel = $("#race-label");
    if (raceLabel) raceLabel.dataset.tip = raceTip($("#f-race").value);
    const heritageLabel = $("#heritage-label");
    if (heritageLabel) heritageLabel.dataset.tip = heritageTip($("#f-heritage").value);
    $("#heritage-desc").textContent = heritage
      ? `传承（${heritage.name}）：${heritage.traits_desc || (heritage.traits || []).join("；")}`
      : "";
    $("#class-desc").textContent = `推荐属性优先级：${klass.primary.map((key) => ABN[key]).join(" > ")}。${klass.hint || ""}`;
    const feats = Object.values(klass.features || {})
      .filter((f) => f.name && f.desc)
      .sort((a, b) => Number(a.level || 0) - Number(b.level || 0));
    const classFeatLine = feats.length
      ? `职业特性（悬停看全文）：` + feats.map((f) => `<span class="tip" data-tip="${esc(`${f.name}（${f.name_en || ""}）· ${f.level} 级\n\n${f.desc}`)}">${esc(f.name)}</span>（${f.level} 级）`).join("、")
      : "";
    const subs = Object.values(klass.subclasses || {}).filter((s) => s.name);
    const subLine = subs.length
      ? `　·　3 级子职业：` + subs.map((s) => {
          const feats = Object.values(s.features || {})
            .sort((a, b) => Number(a.level || 0) - Number(b.level || 0))
            .map((f) => `${f.level} 级 ${f.name}`)
            .join("、");
          const tip = `${s.name}${s.name_en ? "（" + s.name_en + "）" : ""}\n\n${s.desc || ""}${feats ? "\n\n特性：" + feats : ""}`;
          return `<span class="tip" data-tip="${esc(tip)}">${esc(s.name)}</span>`;
        }).join(" / ")
      : "";
    $("#feature-desc").innerHTML = classFeatLine ? `<span>${classFeatLine}${subLine}</span>` : subLine;
    const progDetails = $("#prog-details");
    if (progDetails) {
      const table = progressionTable($("#f-class").value, null);
      progDetails.classList.toggle("hidden", !table);
      progDetails.innerHTML = table ? `<summary>完整进阶表（1-20 级，含职业专属列）</summary><div class="prog-wrap">${table}</div>` : "";
    }
    const bgSkills = (bg.skill_choices || []).map((id) => SRD.skills[id].name).join("、");
    $("#bg-desc").textContent = `背景（${bg.name}）：${bg.additional_zh || ""}\n背景技能池：${bgSkills}\n背景装备：${bg.equipment_zh || ""}`;
  }

  function renderTalents() {
    const bg = currentBackground();
    const choices = (bg.talent_choices || []).map((id) => ({id, name: talentName(id)}));
    if (!st.bgTalent || !choices.some((c) => c.id === st.bgTalent)) st.bgTalent = choices[0] ? choices[0].id : "";
    $("#f-bg-talent").innerHTML = choices
      .map((c) => `<option value="${c.id}" ${c.id === st.bgTalent ? "selected" : ""}>${esc(c.name)}</option>`)
      .join("");
    const label = $("#bg-talent-label");
    if (label) label.dataset.tip = st.bgTalent ? talentTip(st.bgTalent) : "";
    const isHuman = $("#f-race").value === "human";
    $("#free-talent-field").classList.toggle("hidden", !isHuman);
    if (isHuman) {
      if (!st.freeTalent) st.freeTalent = Object.keys(TALENTS)[0] || "";
      $("#f-free-talent").innerHTML = Object.entries(TALENTS)
        .map(([id, t]) => `<option value="${id}" ${id === st.freeTalent ? "selected" : ""}>${esc(t.name)}</option>`)
        .join("");
      const fl = $("#free-talent-label");
      if (fl) fl.dataset.tip = talentTip(st.freeTalent);
    }
    const bgDesc = $("#bg-talent-desc");
    if (bgDesc) bgDesc.textContent = st.bgTalent ? "背景天赋全文：" + talentTip(st.bgTalent) : "";
    const freeDesc = $("#free-talent-desc");
    if (freeDesc) freeDesc.textContent = isHuman && st.freeTalent ? "自选天赋全文：" + talentTip(st.freeTalent) : "";
    renderSchoolField();
  }

  function renderSchoolField() {
    const needed = st.bgTalent === "school-specialization" || ($("#f-race").value === "human" && st.freeTalent === "school-specialization");
    $("#school-field").classList.toggle("hidden", !needed);
    if (needed) {
      if (!st.talentSchool) st.talentSchool = (window.SPELL_SCHOOLS || [])[0] || "";
      $("#f-talent-school").innerHTML = (window.SPELL_SCHOOLS || [])
        .map((s) => `<option value="${s}" ${s === st.talentSchool ? "selected" : ""}>${esc(s)}</option>`)
        .join("");
    }
  }

  function weaponPool(prompt) {
    return Object.entries(ITEMS)
      .filter(([, w]) => {
        const cat = String(w.weapon_category_zh || "");
        if (!cat.startsWith(prompt.category)) return false;
        if (prompt.melee && cat.includes("远程")) return false;
        return true;
      })
      .sort((a, b) => String(a[1].name || "").localeCompare(String(b[1].name || ""), "zh"));
  }

  function weaponOptionLabel(w) {
    const dmg = w.damage ? `（${w.damage} ${w.damage_type_zh || ""}）` : "";
    return `${w.name}${dmg}`;
  }

  function currentEquipState() {
    const klassId = $("#f-class").value;
    const specs = EQUIP[klassId] || [];
    if (!st.equip[klassId]) {
      st.equip[klassId] = specs.map((spec) => ({index: spec.index, key: spec.choices[0].key, weapons: []}));
    }
    return {specs, picks: st.equip[klassId]};
  }

  function renderEquipment() {
    const area = $("#equip-area");
    if (!area) return;
    const {specs, picks} = currentEquipState();
    if (!specs.length) {
      area.innerHTML = '<p class="dim">该职业没有需要选择的初始装备。</p>';
      return;
    }
    let html = "";
    specs.forEach((spec, i) => {
      const pick = picks[i];
      html += `<div class="equip-line"><div class="dim">装备位 ${i + 1}</div>`;
      html += spec.choices.map((c) => `
        <label class="radio tip"><input type="radio" name="equip-${i}" data-ei="${i}" data-key="${esc(c.key)}" ${pick.key === c.key ? "checked" : ""}>
        ${esc(c.text)}</label>`).join("");
      const side = spec.choices.find((c) => c.key === pick.key) || spec.choices[0];
      const slots = [];
      (side.weapon_picks || []).forEach((p) => {
        for (let n = 0; n < p.count; n++) slots.push(p);
      });
      slots.forEach((prompt, s) => {
        const pool = weaponPool(prompt);
        const chosen = pool.some(([id]) => id === pick.weapons[s]) ? pick.weapons[s] : (pool[0] ? pool[0][0] : "");
        pick.weapons[s] = chosen;
        html += `<div class="field"><label>选择武器：${esc(prompt.label)}</label><select class="equip-weapon" data-ei="${i}" data-si="${s}">
          ${pool.map(([id, w]) => `<option value="${esc(id)}" ${id === chosen ? "selected" : ""}>${esc(weaponOptionLabel(w))}</option>`).join("")}
        </select></div>`;
      });
      html += "</div>";
    });
    area.innerHTML = html;
    area.querySelectorAll("input[type=radio][data-ei]").forEach((radio) => {
      radio.onchange = () => {
        const i = parseInt(radio.dataset.ei, 10);
        picks[i].key = radio.dataset.key;
        picks[i].weapons = [];
        renderEquipment();
        renderPreview();
      };
    });
    area.querySelectorAll(".equip-weapon").forEach((sel) => {
      sel.onchange = () => {
        const i = parseInt(sel.dataset.ei, 10);
        const s = parseInt(sel.dataset.si, 10);
        picks[i].weapons[s] = sel.value;
        renderPreview();
      };
    });
  }

  function recommendAllocation() {
    const primary = currentClass().primary || [];
    const order = [...primary, ...ABILITIES.filter((key) => !primary.includes(key))];
    if (st.method === "pointbuy") {
      const pattern = [15, 15, 14, 13, 10, 8];      // BFRD 32 点买：推荐花满 32 点
      order.forEach((key, i) => {
        st.pb[key] = pattern[i] === undefined ? 8 : pattern[i];
      });
    } else if (st.method === "standard") {
      st.stdAssign = {};
      order.forEach((key, i) => {
        st.stdAssign[key] = STD[i] === undefined ? 8 : STD[i];
      });
    } else if (st.pool) {
      const ranked = st.pool
        .map((item, i) => ({total: item.total, i}))
        .sort((a, b) => b.total - a.total || a.i - b.i);
      st.poolAssign = {};
      order.forEach((key, i) => {
        st.poolAssign[key] = ranked[i].i;
      });
    } else {
      alert("请先点「开始掷骰」，再按推荐分配");
      return;
    }
    renderMethods();
    renderAbilities();
    renderPreview();
  }

  async function rollAbilities() {
    const res = await fetch("/api/roll_abilities", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    const out = await res.json();
    if (!out.ok) {
      alert(out.error || "掷骰失败");
      return;
    }
    st.pool = out.results;
    st.poolAssign = {};
    st.rollPlus2 = "";
    st.rollPlus1 = "";
    renderMethods();
    renderAbilities();
    renderPreview();
  }

  function assignedFromPool() {
    if (!st.pool || ABILITIES.some((key) => st.poolAssign[key] === undefined)) return null;
    return Object.fromEntries(ABILITIES.map((key) => [key, st.pool[st.poolAssign[key]].total]));
  }

  function baseScores() {
    if (st.method === "pointbuy") return {...st.pb};
    if (st.method === "standard") {
      if (ABILITIES.some((key) => st.stdAssign[key] === undefined)) return null;
      return Object.fromEntries(ABILITIES.map((key) => [key, st.stdAssign[key]]));
    }
    const assigned = assignedFromPool();
    if (!assigned) return null;
    const out = {...assigned};
    if (st.rollPlus2 && out[st.rollPlus2] !== undefined) out[st.rollPlus2] += 2;
    if (st.rollPlus1 && out[st.rollPlus1] !== undefined) out[st.rollPlus1] += 1;
    return out;
  }

  function renderRollBonuses() {
    const area = $("#roll-bonus-area");
    if (!area) return;
    if (st.method !== "roll" || !st.pool) {
      area.innerHTML = "";
      return;
    }
    const assigned = assignedFromPool();
    if (!assigned) {
      area.innerHTML = '<p class="dim">先把六个结果分配完，再选择掷骰奖励。</p>';
      return;
    }
    const opts = (filterFn, current) => ABILITIES
      .map((key) => `<option value="${key}" ${key === current ? "selected" : ""} ${filterFn(assigned[key]) ? "" : "disabled"}>${ABN[key]}（${assigned[key]}）</option>`)
      .join("");
    area.innerHTML = `<div class="grid3">
      <div class="field"><label>掷骰奖励 +2（需 ≤16）</label><select id="f-roll-plus2"><option value="">—</option>${opts((v) => v <= 16, st.rollPlus2)}</select></div>
      <div class="field"><label>掷骰奖励 +1（需 ≤17）</label><select id="f-roll-plus1"><option value="">—</option>${opts((v) => v <= 17, st.rollPlus1)}</select></div>
    </div>`;
    $("#f-roll-plus2").onchange = (e) => {
      st.rollPlus2 = e.target.value;
      renderPreview();
    };
    $("#f-roll-plus1").onchange = (e) => {
      st.rollPlus1 = e.target.value;
      renderPreview();
    };
  }

  function renderAbilities() {
    const base = baseScores();
    const area = $("#ability-area");
    area.innerHTML = ABILITIES.map((key) => {
      let control = "";
      if (st.method === "pointbuy") {
        control = `<div class="row"><button class="btn small" data-pb="${key}" data-delta="-1">−</button><span class="ability-final">${st.pb[key]}</span><button class="btn small" data-pb="${key}" data-delta="1">＋</button></div>`;
      } else if (st.method === "standard") {
        const used = Object.entries(st.stdAssign).filter(([k]) => k !== key).map(([, v]) => v);
        control = `<select data-std="${key}"><option value="">—</option>${STD.map((v) => `<option value="${v}" ${used.includes(v) ? "disabled" : ""} ${st.stdAssign[key] === v ? "selected" : ""}>${v}</option>`).join("")}</select>`;
      } else if (st.pool) {
        const used = Object.entries(st.poolAssign).filter(([k]) => k !== key).map(([, v]) => v);
        control = `<select data-pool="${key}"><option value="">—</option>${st.pool.map((p, i) => `<option value="${i}" ${used.includes(i) ? "disabled" : ""} ${st.poolAssign[key] === i ? "selected" : ""}>第${i + 1}组（${p.total}）</option>`).join("")}</select>`;
      } else {
        control = `<span class="dim">请先掷骰</span>`;
      }
      const rollBonus = st.method === "roll" ? (st.rollPlus2 === key ? 2 : 0) + (st.rollPlus1 === key ? 1 : 0) : 0;
      let finalTxt = "—";
      if (base) {
        finalTxt = `${base[key]}（${fmtMod(mod(base[key]))}）`;
      }
      const rank = (currentClass().primary || []).indexOf(key);
      const badge = rank === 0 ? '<span class="tag primary">核心</span>' : rank > 0 && rank < 3 ? '<span class="tag">推荐</span>' : "";
      const bonusTxt = rollBonus ? ` <span class="dim">掷骰 +${rollBonus}</span>` : "";
      return `<div class="ability-row"><div>${ABN[key]}${badge}</div><div>${control}</div><div class="ability-final">${finalTxt}${bonusTxt}</div></div>`;
    }).join("");
    area.querySelectorAll("[data-pb]").forEach((btn) => {
      btn.onclick = () => {
        const delta = parseInt(btn.dataset.delta, 10);
        const next = st.pb[btn.dataset.pb] + delta;
        if (next < 8 || next > 18) return;
        const trial = {...st.pb, [btn.dataset.pb]: next};
        const cost = ABILITIES.reduce((sum, key) => sum + PB_COSTS[trial[key]], 0);
        if (delta > 0 && cost > PB_TOTAL) return;
        st.pb = trial;
        renderMethods();
        renderAbilities();
        renderPreview();
      };
    });
    area.querySelectorAll("[data-std]").forEach((sel) => {
      sel.onchange = () => {
        if (sel.value === "") delete st.stdAssign[sel.dataset.std];
        else st.stdAssign[sel.dataset.std] = parseInt(sel.value, 10);
        renderAbilities();
        renderPreview();
      };
    });
    area.querySelectorAll("[data-pool]").forEach((sel) => {
      sel.onchange = () => {
        if (sel.value === "") delete st.poolAssign[sel.dataset.pool];
        else st.poolAssign[sel.dataset.pool] = parseInt(sel.value, 10);
        renderAbilities();
        renderRollBonuses();
        renderPreview();
      };
    });
    renderRollBonuses();
    renderSpells();   // 属性变化会影响准备施法者的准备上限
  }

  function renderSkills() {
    const klass = currentClass();
    const bg = currentBackground();
    const allowed = klass.skill_choices === "any" ? Object.keys(SRD.skills) : klass.skill_choices;
    const limit = klass.skill_count;
    st.classSkills = st.classSkills.filter((id) => allowed.includes(id));
    if (st.classSkills.length > limit) st.classSkills = st.classSkills.slice(0, limit);
    $("#skill-hint").textContent = `职业可选 ${limit} 项`;
    const area = $("#skill-area");
    area.innerHTML = allowed.map((id) => {
      const chosen = st.classSkills.includes(id);
      const disabled = !chosen && st.classSkills.length >= limit;
      return `<label class="skill-item tip ${chosen ? "chosen" : ""}" data-tip="${esc(skillTip(id))}">
        <input type="checkbox" data-skill="${id}" ${chosen ? "checked" : ""} ${disabled ? "disabled" : ""}>
        ${esc(SRD.skills[id].name)}<span class="dim">（${ABN[SRD.skills[id].ability]}）</span>
      </label>`;
    }).join("");
    area.querySelectorAll("[data-skill]").forEach((box) => {
      box.onchange = () => {
        const id = box.dataset.skill;
        if (box.checked) {
          if (st.classSkills.length >= limit) {
            box.checked = false;
            return;
          }
          st.classSkills.push(id);
        } else {
          st.classSkills = st.classSkills.filter((item) => item !== id);
        }
        renderSkills();
        renderPreview();
      };
    });

    const bgChoices = bg.skill_choices || [];
    const bgLimit = bg.skill_count || 2;
    st.bgSkills = st.bgSkills.filter((id) => bgChoices.includes(id));
    if (st.bgSkills.length > bgLimit) st.bgSkills = st.bgSkills.slice(0, bgLimit);
    $("#bg-skill-hint").textContent = `背景可选 ${bgLimit} 项（${bg.name}）`;
    const bgArea = $("#bg-skill-area");
    bgArea.innerHTML = bgChoices.map((id) => {
      const chosen = st.bgSkills.includes(id);
      const disabled = !chosen && st.bgSkills.length >= bgLimit;
      return `<label class="skill-item tip ${chosen ? "chosen" : ""}" data-tip="${esc(skillTip(id))}">
        <input type="checkbox" data-bgskill="${id}" ${chosen ? "checked" : ""} ${disabled ? "disabled" : ""}>
        ${esc(SRD.skills[id].name)}<span class="dim">（${ABN[SRD.skills[id].ability]}）</span>
      </label>`;
    }).join("");
    bgArea.querySelectorAll("[data-bgskill]").forEach((box) => {
      box.onchange = () => {
        const id = box.dataset.bgskill;
        if (box.checked) {
          if (st.bgSkills.length >= bgLimit) {
            box.checked = false;
            return;
          }
          st.bgSkills.push(id);
        } else {
          st.bgSkills = st.bgSkills.filter((item) => item !== id);
        }
        renderSkills();
        renderPreview();
      };
    });

    const isHuman = $("#f-race").value === "human";
    $("#human-skill-wrap").classList.toggle("hidden", !isHuman);
    if (isHuman) {
      const used = new Set([...st.classSkills, ...st.bgSkills]);
      if (st.humanSkill && used.has(st.humanSkill)) st.humanSkill = "";
      $("#human-skill-hint").textContent = "人类「雄心」：额外自选一项技能熟练";
      const hArea = $("#human-skill-area");
      hArea.innerHTML = Object.entries(SRD.skills).map(([id, s]) => {
        const chosen = st.humanSkill === id;
        const disabled = !chosen && used.has(id);
        return `<label class="skill-item tip ${chosen ? "chosen" : ""}" data-tip="${esc(skillTip(id))}">
          <input type="radio" name="human-skill" data-humanskill="${id}" ${chosen ? "checked" : ""} ${disabled ? "disabled" : ""}>
          ${esc(s.name)}<span class="dim">（${ABN[s.ability]}）</span>
        </label>`;
      }).join("");
      hArea.querySelectorAll("[data-humanskill]").forEach((radio) => {
        radio.onchange = () => {
          st.humanSkill = radio.dataset.humanskill;
          renderSkills();
          renderPreview();
        };
      });
    } else {
      st.humanSkill = "";
    }
  }

  function casterCaps() {
    const klassId = $("#f-class").value;
    const klass = SRD.classes[klassId];
    if (!klass || !klass.spell_ability) return null;
    const cantrips = Number((klass.cantrips_known || {})["1"] || 0);
    const known = Number((klass.spells_known || {})["1"] || 0);
    const rituals = Number((klass.rituals_known || {})["1"] || 0);
    const wizard = klassId === "wizard";
    const preparedTrack = ["cleric", "druid", "wizard"].includes(klassId);
    let prepared = 0;
    if (preparedTrack) {
      const base = baseScores();
      const ability = klass.spell_ability;
      const abilityMod = base ? Math.floor((base[ability] - 10) / 2) : 0;
      prepared = Math.max(1, abilityMod + 1);          // BFRD：调整值 + 等级（建卡 1 级）
    }
    const slotRow = (SPELL_RULES.slots && SPELL_RULES.slots[klassId] && SPELL_RULES.slots[klassId]["1"]) || [];
    let maxLevel = 0;
    slotRow.forEach((count, index) => { if (count > 0) maxLevel = index + 1; });
    return {
      klassId, klass, wizard, preparedTrack,
      cantrips, known, rituals, prepared, maxLevel,
      main: wizard ? 6 : (preparedTrack ? prepared : known),
      mainLabel: wizard ? "法术书（1 环至多 6 条）" : (preparedTrack ? "准备法术" : "已知法术"),
      sources: klass.sources || [],
    };
  }

  const spellEligible = (s, caps) => st.spellMode === "free" || !caps.sources.length || (s.sources || []).some((src) => caps.sources.includes(src));

  function renderSpellBucket(title, cap, list, chosen, key) {
    if (!cap) return "";
    const items = list.map(([sid, s]) => {
      const picked = chosen.includes(sid);
      const disabled = !picked && chosen.length >= cap;
      const off = st.spellMode === "free" && !(s.sources || []).some((src) => casterCaps().sources.includes(src)) ? "offclass" : "";
      return `<label class="skill-item tip ${picked ? "chosen" : ""} ${off}" data-tip="${esc(spellTip(s))}">
        <input type="checkbox" data-bucket="${key}" data-spellpick="${sid}" ${picked ? "checked" : ""} ${disabled ? "disabled" : ""}>
        ${esc(s.name)}<span class="dim">${esc(s.school)}</span></label>`;
    }).join("");
    return `<div class="spell-group">${esc(title)} ${chosen.length}/${cap}</div><div class="skill-grid">${items || '<span class="dim">（本栏位暂无可选法术）</span>'}</div>`;
  }

  function renderSpells() {
    const card = $("#spell-card");
    const caps = casterCaps();
    if (!caps) {
      card.classList.add("hidden");
      st.spellCantrips = [];
      st.spellMain = [];
      st.spellRituals = [];
      return;
    }
    card.classList.remove("hidden");
    document.querySelectorAll("[data-spellmode]").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.spellmode === st.spellMode);
    });
    $("#spell-hint").textContent =
      `戏法 ${st.spellCantrips.length}/${caps.cantrips} · ${caps.mainLabel} ${st.spellMain.length}/${caps.main} · 仪式 ${st.spellRituals.length}/${caps.rituals}；只选一部分也可以`;
    const cantripList = Object.entries(SPELLS).filter(([, s]) => s.level === 0 && spellEligible(s, caps));
    const mainList = Object.entries(SPELLS)
      .filter(([, s]) => s.level === 1 && spellEligible(s, caps))
      .slice()
      .sort((a, b) => a[1].name.localeCompare(b[1].name, "zh"));
    const ritualList = Object.entries(SPELLS)
      .filter(([, s]) => s.ritual && s.level === 1 && spellEligible(s, caps))
      .slice()
      .sort((a, b) => a[1].name.localeCompare(b[1].name, "zh"));
    $("#spell-area").innerHTML =
      renderSpellBucket("戏法", caps.cantrips, cantripList, st.spellCantrips, "cantrips")
      + renderSpellBucket(caps.mainLabel, caps.main, mainList, st.spellMain, "main")
      + renderSpellBucket("仪式（不耗法术位）", caps.rituals, ritualList, st.spellRituals, "rituals");
    $("#spell-area").querySelectorAll("[data-spellpick]").forEach((box) => {
      box.onchange = () => {
        const sid = box.dataset.spellpick;
        const key = box.dataset.bucket;
        const store = key === "cantrips" ? st.spellCantrips : key === "main" ? st.spellMain : st.spellRituals;
        const cap = key === "cantrips" ? caps.cantrips : key === "main" ? caps.main : caps.rituals;
        const index = store.indexOf(sid);
        if (box.checked) {
          if (index === -1) {
            if (store.length >= cap) { box.checked = false; return; }
            store.push(sid);
          }
        } else if (index !== -1) {
          store.splice(index, 1);
        }
        renderSpells();
        renderPreview();
      };
    });
  }

  function applyRecommendedSpells() {
    const caps = casterCaps();
    if (!caps) return;
    const rec = (SPELL_RULES.recommended && SPELL_RULES.recommended[caps.klassId]) || [];
    const cantrips = [];
    const main = [];
    const rituals = [];
    rec.forEach((sid) => {
      const s = SPELLS[sid];
      if (!s || !spellEligible(s, caps)) return;
      if (s.level === 0) cantrips.push(sid);
      else if (s.ritual && rituals.length < caps.rituals) rituals.push(sid);
      else main.push(sid);
    });
    st.spellCantrips = cantrips.slice(0, caps.cantrips);
    st.spellMain = main.slice(0, caps.main);
    st.spellRituals = rituals.slice(0, caps.rituals);
    renderSpells();
    renderPreview();
  }

  function wornArmorFromEquip() {
    const {specs, picks} = currentEquipState();
    let armorId = null;
    let shield = false;
    specs.forEach((spec, i) => {
      const pick = picks[i];
      const side = spec.choices.find((c) => c.key === pick.key) || spec.choices[0];
      (side.items || []).forEach((it) => {
        if (!it.id) return;
        const db = ITEMS[it.id] || {};
        if (db.is_shield) shield = true;
        else if (!armorId && db.category === "Armor" && db.ac_base !== undefined) armorId = it.id;
      });
    });
    return {armorId, shield};
  }

  function equipmentSummary() {
    const {specs, picks} = currentEquipState();
    const parts = [];
    specs.forEach((spec, i) => {
      const pick = picks[i];
      const side = spec.choices.find((c) => c.key === pick.key) || spec.choices[0];
      let text = side.text;
      if (side.weapon_picks && side.weapon_picks.length) {
        const names = pick.weapons.map((wid) => (ITEMS[wid] ? ITEMS[wid].name : wid)).filter(Boolean);
        if (names.length) text += `（选了：${names.join("、")}）`;
      }
      parts.push(text);
    });
    return parts;
  }

  function renderPreview() {
    const race = currentRace();
    const heritage = currentHeritage();
    const klass = currentClass();
    const bg = currentBackground();
    const base = baseScores();
    const rows = [];
    const name = $("#f-name").value.trim() || "（未命名）";
    rows.push(`<div class="preview-row"><span>角色</span><span>${esc(name)}</span></div>`);
    rows.push(`<div class="preview-row"><span>血统 / 传承 / 职业 / 背景</span><span>${esc(race.name)} / ${esc(heritage ? heritage.name : "—")} / ${esc(klass.name)} / ${esc(bg.name)}</span></div>`);
    if (st.adaptation) {
      const abilityName = {int: "智力", wis: "感知", cha: "魅力"}[st.adaptationSpellAbility] || "";
      rows.push(`<div class="preview-row"><span>自然适应</span><span>${esc(st.adaptation)}${abilityName ? `（次级幻影·${abilityName}）` : ""}</span></div>`);
    }
    const talentNames = [st.bgTalent && talentName(st.bgTalent), ($("#f-race").value === "human" && st.freeTalent && talentName(st.freeTalent))].filter(Boolean);
    rows.push(`<div class="preview-row"><span>天赋</span><span>${esc(talentNames.join("、")) || "—"}</span></div>`);
    if (!base) {
      rows.push(`<div class="dim">属性未完成，暂无法预览派生数值。</div>`);
    } else {
      const dexMod = mod(base.dex);
      const hp = klass.hit_die + mod(base.con);
      const worn = wornArmorFromEquip();
      const armorItem = worn.armorId ? ITEMS[worn.armorId] : null;
      let ac = armorItem
        ? (armorItem.ac_dex === "full" ? armorItem.ac_base + dexMod : armorItem.ac_base + Math.min(dexMod, Number(armorItem.ac_dex) || 0))
        : 10 + dexMod;
      if (worn.shield) ac += 2;
      rows.push(`<div class="preview-row"><span>属性</span><span>${ABILITIES.map((key) => `${ABN[key]} ${base[key]}`).join("　")}</span></div>`);
      rows.push(`<div class="preview-row"><span>HP / AC / 速度</span><span>${hp} / ${ac} / ${race.speed}尺</span></div>`);
      rows.push(`<div class="preview-row"><span>穿着（随装备选择自动）</span><span>${esc(armorItem ? armorItem.name : "无甲")}${worn.shield ? " + 盾" : ""}</span></div>`);
      const saves = klass.saves.map((key) => `${ABN[key]} ${fmtMod(mod(base[key]) + klass.prof_bonus)}`).join("　");
      rows.push(`<div class="preview-row"><span>豁免（熟练）</span><span>${saves}</span></div>`);
      const allSkillPicks = [...st.classSkills, ...st.bgSkills, ...(st.humanSkill ? [st.humanSkill] : [])];
      if (allSkillPicks.length) {
        const skillTxt = allSkillPicks
          .map((id) => `${SRD.skills[id].name} ${fmtMod(mod(base[SRD.skills[id].ability]) + klass.prof_bonus)}`)
          .join("　");
        rows.push(`<div class="preview-row"><span>技能</span><span>${skillTxt}</span></div>`);
      }
      const caps = casterCaps();
      if (caps) {
        const all = [...st.spellCantrips, ...st.spellMain, ...st.spellRituals]
          .map((sid) => (SPELLS[sid] ? SPELLS[sid].name : sid)).join("、");
        rows.push(`<div class="preview-row"><span>法术（戏法 ${st.spellCantrips.length}/${caps.cantrips} · ${caps.mainLabel} ${st.spellMain.length}/${caps.main} · 仪式 ${st.spellRituals.length}/${caps.rituals}）</span><span>${esc(all) || "（未选）"}</span></div>`);
      }
      const equipTxt = equipmentSummary().join("；");
      rows.push(`<div class="preview-row"><span>初始装备</span><span>${esc(equipTxt)}</span></div>`);
    }
    $("#preview").innerHTML = rows.join("");
  }

  async function submit() {
    const msg = $("#create-msg");
    const fail = (text) => {
      msg.style.color = "var(--bad)";
      msg.textContent = text;
    };
    const name = $("#f-name").value.trim();
    if (!name) return fail("请填写角色名");
    const base = baseScores();
    if (!base) return fail("请先完成属性分配");
    const klass = currentClass();
    if (st.method === "pointbuy" && pointBuyCost() > PB_TOTAL) return fail(`点买超支（${pointBuyCost()}/${PB_TOTAL}）`);
    if (st.method === "roll") {
      if (!st.rollPlus2 || !st.rollPlus1) return fail("掷骰法需要分配 +2 / +1 掷骰奖励");
      const assigned = assignedFromPool();
      if (assigned[st.rollPlus2] > 16) return fail("掷骰奖励 +2 只能给 16 或以下的属性");
      if (assigned[st.rollPlus1] > 17) return fail("掷骰奖励 +1 只能给 17 或以下的属性");
    }
    if (st.classSkills.length !== klass.skill_count) return fail(`请选择 ${klass.skill_count} 项职业技能`);
    const bg = currentBackground();
    if (st.bgSkills.length !== (bg.skill_count || 2)) return fail(`请选择 ${bg.skill_count || 2} 项背景技能`);
    if ($("#f-race").value === "human" && !st.humanSkill) return fail("人类「雄心」需要额外选一项技能");
    const {specs, picks} = currentEquipState();
    const choices = [];
    for (let i = 0; i < specs.length; i++) {
      const spec = specs[i];
      const pick = picks[i];
      const side = spec.choices.find((c) => c.key === pick.key) || spec.choices[0];
      const expect = (side.weapon_picks || []).reduce((sum, p) => sum + p.count, 0);
      const weapons = (pick.weapons || []).filter(Boolean);
      if (weapons.length !== expect) return fail(`装备位 ${i + 1}「${side.text}」还需要选择 ${expect - weapons.length} 件武器`);
      choices.push({index: spec.index, key: side.key, weapons});
    }
    const payload = {
      name,
      gender: $("#f-gender").value,
      race: $("#f-race").value,
      adaptation: st.adaptation,
      adaptation_spell_ability: st.adaptationSpellAbility,
      heritage: $("#f-heritage").value,
      klass: $("#f-class").value,
      background: $("#f-background").value,
      method: st.method,
      base,
      skills: st.classSkills,
      background_skills: st.bgSkills,
      background_talent: st.bgTalent,
      human_skill: $("#f-race").value === "human" ? st.humanSkill : "",
      talent: $("#f-race").value === "human" ? st.freeTalent : "",
      talent_school: (st.bgTalent === "school-specialization" || ($("#f-race").value === "human" && st.freeTalent === "school-specialization")) ? st.talentSchool : "",
      equipment_choices: choices,
      cantrips: st.spellCantrips,
      spells: st.spellMain,
      rituals: st.spellRituals,
      spell_mode: st.spellMode,
      backstory: $("#f-backstory").value,
    };
    msg.style.color = "var(--dim)";
    msg.textContent = "创建中…";
    const res = await fetch("/api/create", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const out = await res.json();
    if (!out.ok) return fail(out.error || "创建失败");
    location.href = "/play";
  }

  init();
})();
