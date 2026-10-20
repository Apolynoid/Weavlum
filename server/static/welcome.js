(() => {
  const $ = (sel) => document.querySelector(sel);
  const COLORS = window.PLAYER_COLORS || [];
  const st = {
    color: localStorage.getItem("weavlum-color") || "",
    avatar: "",
  };
  if (!COLORS.includes(st.color)) st.color = "";
  async function post(url, data) {
    const res = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data),
    });
    return res.json();
  }
  function showMsg(text, isError) {
    const el = $("#msg");
    el.textContent = text || "";
    el.style.color = isError ? "var(--bad)" : "var(--dim)";
  }
  function updateChip() {
    const chip = $("#player-chip");
    if (!chip) return;
    const nick = $("#nickname").value.trim();
    const img = st.avatar ? `<img class="avatar big" src="/avatars/${st.avatar}" alt="">` : "";
    const nameHTML = `<span${st.color ? ` style="color:${st.color}"` : ""}>${nick || "（未输入昵称）"}</span>`;
    chip.innerHTML = img + nameHTML;
  }
  const colorRow = $("#color-row");
  if (colorRow) {
    colorRow.innerHTML = COLORS
      .map((c) => `<span class="swatch${c === st.color ? " active" : ""}" data-color="${c}" style="background:${c}"></span>`)
      .join("");
    colorRow.querySelectorAll(".swatch").forEach((sw) => {
      sw.onclick = () => {
        st.color = sw.dataset.color === st.color ? "" : sw.dataset.color;
        localStorage.setItem("weavlum-color", st.color);
        colorRow.querySelectorAll(".swatch").forEach((x) => x.classList.toggle("active", x.dataset.color === st.color));
        updateChip();
      };
    });
  }
  async function ensureJoin() {
    const nickname = $("#nickname").value.trim();
    if (!nickname) {
      showMsg("请先输入昵称", true);
      return false;
    }
    const out = await post("/api/join", {nickname, color: st.color, avatar: st.avatar});
    if (!out.ok) {
      showMsg(out.error || "加入失败", true);
      return false;
    }
    localStorage.setItem("weavlum-nickname", nickname);
    return true;
  }
  const savedNickname = localStorage.getItem("weavlum-nickname");
  if (savedNickname) $("#nickname").value = savedNickname;
  $("#nickname").addEventListener("input", updateChip);
  updateChip();
  const avatarFile = $("#avatar-file");
  if (avatarFile) {
    avatarFile.onchange = async () => {
      const file = avatarFile.files[0];
      if (!file) return;
      if (!(await ensureJoin())) return;
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/avatar", {method: "POST", body: form});
      const out = await res.json();
      if (!out.ok) {
        showMsg(out.error || "头像上传失败", true);
        return;
      }
      st.avatar = out.avatar;
      updateChip();
      showMsg("头像已上传", false);
    };
  }
  $("#btn-create").onclick = async () => {
    if (await ensureJoin()) location.href = "/create";
  };
  document.querySelectorAll("[data-pick]").forEach((btn) => {
    btn.onclick = async () => {
      if (!(await ensureJoin())) return;
      const out = await post("/api/pick_character", {character_id: btn.dataset.pick});
      if (!out.ok) {
        showMsg(out.error || "入场失败", true);
        return;
      }
      location.href = "/play";
    };
  });
  document.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`删除角色卡「${btn.dataset.name}」？此操作不可恢复（存档与页面上的该角色都会消失）。`)) return;
      const out = await post("/api/delete_character", {character_id: btn.dataset.del});
      if (!out.ok) {
        showMsg(out.error || "删除失败", true);
        return;
      }
      location.reload();
    };
  });
  $("#btn-import").onclick = async () => {
    if (!(await ensureJoin())) return;
    const file = $("#import-file").files[0];
    if (!file) {
      showMsg("请选择角色卡文件", true);
      return;
    }
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/import_character", {method: "POST", body: form});
    const out = await res.json();
    if (!out.ok) {
      showMsg(out.error || "导入失败", true);
      return;
    }
    location.href = "/play";
  };
  const claimBtn = $("#btn-claim-dm");
  if (claimBtn) claimBtn.onclick = async () => {
    if (!(await ensureJoin())) return;
    const out = await post("/api/claim_dm", {});
    if (!out.ok) {
      showMsg(out.error || "接任失败", true);
      return;
    }
    location.href = "/play";
  };
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.onclick = async () => {
      const url = btn.dataset.copy;
      try {
        await navigator.clipboard.writeText(url);
        showMsg("已复制：" + url, false);
      } catch (err) {
        showMsg("复制失败，请手动复制：" + url, false);
      }
    };
  });
})();
