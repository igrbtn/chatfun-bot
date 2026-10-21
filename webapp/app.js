(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  var initData = (tg && tg.initData) || window.DEV_INIT_DATA || "";
  var S = {};
  var state = { me: null, chatId: null, chat: null, admin: null };
  var $ = function (id) { return document.getElementById(id); };

  function t(key, vars) {
    var s = S[key] || key;
    Object.keys(vars || {}).forEach(function (k) { s = s.split("{" + k + "}").join(vars[k]); });
    return s;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  var toastTimer;
  function toast(msg) {
    var box = $("toast");
    box.textContent = msg;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.hidden = true; }, 2600);
  }

  function haptic(kind) {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(kind);
  }

  function confirmDo(text, fn) {
    if (tg && tg.showConfirm && tg.platform !== "unknown") tg.showConfirm(text, function (ok) { if (ok) fn(); });
    else if (window.confirm(text)) fn();
  }

  function api(method, path, body) {
    return fetch("/api" + path, {
      method: method,
      headers: { "X-Init-Data": initData, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.error || "generic");
          err.code = data.error || "generic";
          throw err;
        }
        return data;
      });
    });
  }

  function showError(e) {
    haptic("error");
    toast(S["err_" + e.code] || t("err_generic"));
  }

  function applyTexts() {
    document.querySelectorAll("[data-t]").forEach(function (n) { n.textContent = t(n.dataset.t); });
    document.querySelectorAll("[data-tp]").forEach(function (n) { n.placeholder = t(n.dataset.tp); });
  }

  // --- tabs ---------------------------------------------------------

  var SECTIONS = ["hero", "stats", "dl", "admin"];

  function showTab(name) {
    document.querySelectorAll("#tabs button").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === name); });
    SECTIONS.forEach(function (s) { $(s).hidden = s !== name; });
    if (name === "admin") loadAdmin();
    if (name === "stats") loadStats();
  }

  $("tabs").addEventListener("click", function (ev) {
    var b = ev.target.closest("button");
    if (b) showTab(b.dataset.tab);
  });

  // --- hero ---------------------------------------------------------

  function ranking(list, rows, label, value, highlight) {
    list.textContent = "";
    if (!rows.length) list.appendChild(el("li", "empty", t("top_empty")));
    rows.forEach(function (row, i) {
      var li = el("li", highlight && highlight(row) ? "me" : "");
      var left = el("span", "name");
      left.appendChild(el("span", "place", String(i + 1)));
      left.appendChild(document.createTextNode(label(row)));
      li.appendChild(left);
      li.appendChild(el("span", "count", value(row)));
      list.appendChild(li);
    });
  }

  function isMe(row) { return state.me && row.id === state.me.user.id; }

  function renderChat(c) {
    state.chat = c;
    var f = c.features;
    $("toptitle").textContent = c.game_title + ": " + t("top_title").toLowerCase();
    $("todaylabel").textContent = t("today_of", { title: c.game_title });
    $("winner").textContent = c.today ? c.today.name : t("nobody_yet");
    $("roll").hidden = !c.hero_enabled || !!c.today;
    $("herooff").hidden = c.hero_enabled;
    $("meactive").checked = c.me.active;
    $("players").textContent = t("players", { n: c.players });
    ranking($("top"), c.top, function (r) { return r.name; }, function (r) { return String(r.count); }, isMe);

    var p = c.pair;
    $("paircard").hidden = !f.pair;
    $("pairsbox").hidden = !f.pair;
    $("pairlabel").textContent = t("today_of", { title: p.title });
    $("pairnames").textContent = p.today ? p.today[0].name + " + " + p.today[1].name : t("nobody_yet_pair");
    $("pairroll").hidden = !!p.today;
    $("pairstitle").textContent = p.title + ": " + t("pairs_top").toLowerCase();
    ranking($("pairs"), p.top, function (r) { return r.a.name + " + " + r.b.name; },
      function (r) { return String(r.count); });

    $("whocard").hidden = !f.random;
    $("tabs").querySelector("[data-tab=stats]").hidden = !f.stats && !f.duels;
    $("tabs").querySelector("[data-tab=admin]").hidden = !c.admin;
  }

  function loadChat() {
    if (state.chatId === null) return Promise.resolve();
    return api("GET", "/chats/" + state.chatId).then(renderChat).catch(showError);
  }

  $("roll").addEventListener("click", function () {
    var btn = $("roll");
    btn.disabled = true;
    btn.textContent = t("rolling");
    api("POST", "/chats/" + state.chatId + "/roll").then(function (r) {
      haptic("success");
      var w = $("winner");
      w.classList.remove("pop");
      void w.offsetWidth;
      w.classList.add("pop");
      return loadChat();
    }).catch(showError).then(function () {
      btn.disabled = false;
      btn.textContent = t("roll");
    });
  });

  $("pairroll").addEventListener("click", function () {
    var btn = $("pairroll");
    btn.disabled = true;
    api("POST", "/chats/" + state.chatId + "/pair/roll").then(function () {
      haptic("success");
      var n = $("pairnames");
      n.classList.remove("pop");
      void n.offsetWidth;
      n.classList.add("pop");
      return loadChat();
    }).catch(showError).then(function () { btn.disabled = false; });
  });

  $("whogo").addEventListener("click", function () {
    var q = $("whoinput").value.trim();
    if (!q) return;
    var btn = $("whogo");
    btn.disabled = true;
    api("POST", "/chats/" + state.chatId + "/who", { question: q }).then(function (r) {
      haptic("success");
      $("whoanswer").textContent = r.text;
      $("whoinput").value = "";
    }).catch(showError).then(function () { btn.disabled = false; });
  });

  // --- stats --------------------------------------------------------

  var period = "week";

  function loadStats() {
    if (state.chatId === null) return;
    api("GET", "/chats/" + state.chatId + "/stats?period=" + period).then(function (r) {
      var f = state.chat ? state.chat.features : { stats: true, duels: true };
      $("statsbox").hidden = !f.stats;
      $("duelsbox").hidden = !f.duels;
      $("statstitle").textContent = t("stats_total", { n: r.total });
      ranking($("statsrows"), r.rows, function (x) { return x.name; }, function (x) { return String(x.count); }, isMe);
      ranking($("duelrows"), r.duels, function (x) { return x.name; },
        function (x) { return x.wins + " / " + x.losses; }, isMe);
    }).catch(showError);
  }

  $("periods").addEventListener("click", function (ev) {
    var b = ev.target.closest("button");
    if (!b) return;
    period = b.dataset.period;
    document.querySelectorAll("#periods button").forEach(function (x) { x.classList.toggle("on", x === b); });
    loadStats();
  });

  $("meactive").addEventListener("change", function (ev) {
    api("POST", "/chats/" + state.chatId + "/me", { active: ev.target.checked }).then(loadChat).catch(function (e) {
      ev.target.checked = !ev.target.checked;
      showError(e);
    });
  });

  // --- download -----------------------------------------------------

  $("dlgo").addEventListener("click", function () {
    var url = $("url").value.trim();
    if (!url) return;
    $("dlgo").disabled = true;
    api("POST", "/download", { url: url }).then(function () {
      haptic("success");
      $("dlmsg").textContent = t("dl_queued");
      $("url").value = "";
    }).catch(function (e) {
      $("dlmsg").textContent = S["err_" + e.code] || t("err_generic");
      haptic("error");
    }).then(function () { $("dlgo").disabled = false; });
  });

  // --- admin --------------------------------------------------------

  var SETTING_KEYS = ["downloads", "hero", "auto_register", "drumroll", "tie_poll", "pair", "stats", "random",
    "duels", "chatter"];

  function switchRow(label, checked, onChange) {
    var row = el("label", "row");
    row.appendChild(el("span", "name", label));
    var box = el("input", "switch");
    box.type = "checkbox";
    box.checked = checked;
    box.addEventListener("change", function () { onChange(box); });
    row.appendChild(box);
    return row;
  }

  function presetChips(box, input, key) {
    box.textContent = "";
    (S[key] || []).forEach(function (name) {
      var chip = el("button", "", name);
      chip.type = "button";
      chip.addEventListener("click", function () { input.value = name; });
      box.appendChild(chip);
    });
  }

  function renderTones(a) {
    var box = $("tones");
    box.textContent = "";
    a.tones.forEach(function (tone) {
      var row = el("button", tone.id === a.tone ? "on" : "");
      row.type = "button";
      var text = el("span", "name");
      text.appendChild(el("b", "", tone.id + ". " + tone.name));
      text.appendChild(el("div", "sub", tone.sample));
      row.appendChild(text);
      row.appendChild(el("span", "check", tone.id === a.tone ? "v" : ""));
      row.addEventListener("click", function () {
        api("POST", "/chats/" + state.chatId + "/admin/tone", { tone: tone.id }).then(function (r) {
          a.tone = r.tone;
          haptic("success");
          toast(t("saved"));
          renderTones(a);
        }).catch(showError);
      });
      box.appendChild(row);
    });
  }

  function renderAdmin(a) {
    renderTones(a);
    $("pairtitleinput").value = a.pair_title;
    $("pairtitleinput").placeholder = t("pair_placeholder");
    presetChips($("pairpresets"), $("pairtitleinput"), "pair_presets");
    $("titleinput").value = a.hero_title;
    $("titleinput").placeholder = t("title_placeholder");
    $("playersinput").placeholder = t("add_placeholder");
    var presets = $("presets");
    presets.textContent = "";
    (S.title_presets || []).forEach(function (name) {
      var chip = el("button", "", name);
      chip.type = "button";
      chip.addEventListener("click", function () { $("titleinput").value = name; });
      presets.appendChild(chip);
    });
    var settings = $("settings");
    settings.textContent = "";
    SETTING_KEYS.forEach(function (key) {
      settings.appendChild(switchRow(t("set_" + key), a.settings[key], function (box) {
        var patch = {};
        patch[key] = box.checked;
        api("POST", "/chats/" + state.chatId + "/admin/settings", patch).then(function () {
          toast(t("saved"));
          loadChat();
        }).catch(function (e) { box.checked = !box.checked; showError(e); });
      }));
    });
    var members = $("members");
    members.textContent = "";
    if (!a.members.length) members.appendChild(el("div", "empty", t("members_empty")));
    a.members.forEach(function (m) {
      var row = switchRow(m.name, m.active, function (box) {
        api("POST", "/chats/" + state.chatId + "/admin/members/" + m.id, { active: box.checked })
          .then(loadChat)
          .catch(function (e) { box.checked = !box.checked; showError(e); });
      });
      if (m.manual) {
        row.querySelector(".name").appendChild(el("span", "tag", t("manual_tag")));
        var rm = el("button", "remove", "x");
        rm.type = "button";
        rm.addEventListener("click", function (ev) {
          ev.preventDefault();
          confirmDo(t("confirm_remove", { name: m.name }), function () {
            api("POST", "/chats/" + state.chatId + "/admin/members/" + m.id + "/remove")
              .then(function () { loadAdmin(); loadChat(); })
              .catch(showError);
          });
        });
        row.insertBefore(rm, row.lastChild);
      }
      members.appendChild(row);
    });
  }

  function loadAdmin() {
    if (state.chatId === null) return;
    api("GET", "/chats/" + state.chatId + "/admin").then(renderAdmin).catch(showError);
  }

  $("playersadd").addEventListener("click", function () {
    var text = $("playersinput").value.trim();
    if (!text) return;
    var btn = $("playersadd");
    btn.disabled = true;
    api("POST", "/chats/" + state.chatId + "/admin/players", { text: text }).then(function (r) {
      $("playersinput").value = "";
      haptic("success");
      toast(t("added_n", { a: r.added.length, s: r.skipped.length }));
      loadAdmin();
      loadChat();
    }).catch(showError).then(function () { btn.disabled = false; });
  });

  $("pairtitlesave").addEventListener("click", function () {
    var btn = $("pairtitlesave");
    btn.disabled = true;
    api("POST", "/chats/" + state.chatId + "/admin/title", { pair_title: $("pairtitleinput").value }).then(function (r) {
      $("pairtitleinput").value = r.pair_title;
      haptic("success");
      toast(t("saved"));
      return loadChat();
    }).catch(showError).then(function () { btn.disabled = false; });
  });

  $("titlesave").addEventListener("click", function () {
    var btn = $("titlesave");
    btn.disabled = true;
    api("POST", "/chats/" + state.chatId + "/admin/title", { title: $("titleinput").value }).then(function (r) {
      $("titleinput").value = r.hero_title;
      haptic("success");
      toast(t("saved"));
      return loadChat();
    }).catch(showError).then(function () { btn.disabled = false; });
  });

  $("reroll").addEventListener("click", function () {
    confirmDo(t("confirm_reroll"), function () {
      api("POST", "/chats/" + state.chatId + "/admin/reroll").then(function () {
        haptic("success");
        loadChat();
      }).catch(showError);
    });
  });

  $("reset").addEventListener("click", function () {
    confirmDo(t("confirm_reset"), function () {
      api("POST", "/chats/" + state.chatId + "/admin/reset").then(function () {
        toast(t("reset_done"));
        loadChat();
      }).catch(showError);
    });
  });

  // --- chat picker ------------------------------------------------

  function sortedChats() {
    return state.me.chats.slice().sort(function (a, b) {
      if (a.admin !== b.admin) return a.admin ? -1 : 1;
      return a.title.localeCompare(b.title);
    });
  }

  function renderChatList() {
    var q = $("chatsearch").value.trim().toLowerCase();
    var list = $("chatlist");
    list.textContent = "";
    var shown = sortedChats().filter(function (c) { return !q || c.title.toLowerCase().indexOf(q) >= 0; });
    if (!shown.length && state.me.chats.length) list.appendChild(el("div", "empty", t("nothing_found")));
    shown.forEach(function (c) {
      var row = el("button");
      row.type = "button";
      var name = el("span", "name");
      name.appendChild(el("div", "", c.title));
      if (c.id === state.chatId) name.appendChild(el("div", "sub", t("current_chat")));
      row.appendChild(name);
      if (c.admin && !state.me.global_admin) row.appendChild(el("span", "badge", t("badge_admin")));
      row.appendChild(el("span", "chev", ">"));
      row.addEventListener("click", function () { openChat(c.id); });
      list.appendChild(row);
    });
  }

  function showPicker() {
    $("chathead").hidden = true;
    SECTIONS.forEach(function (s) { $(s).hidden = true; });
    $("picker").hidden = false;
    $("pickertitle").textContent = t(state.me.global_admin ? "pick_chat_global" : "pick_chat");
    $("nochats").hidden = state.me.chats.length > 0;
    $("chatsearch").hidden = state.me.chats.length < 7;
    renderChatList();
    if (tg && tg.BackButton) tg.BackButton.hide();
    window.scrollTo(0, 0);
  }

  function openChat(id) {
    var c = state.me.chats.filter(function (x) { return x.id === id; })[0];
    state.chatId = id;
    $("picker").hidden = true;
    $("chathead").hidden = false;
    $("chattitle").textContent = c.title;
    $("chatrole").textContent = t(state.me.global_admin ? "role_global" : (c.admin ? "role_admin" : "role_member"));
    var many = state.me.chats.length > 1;
    $("switchchat").hidden = !many;
    if (tg && tg.BackButton) { if (many) tg.BackButton.show(); else tg.BackButton.hide(); }
    var current = document.querySelector("#tabs button.on");
    var tab = current ? current.dataset.tab : "hero";
    if (tab === "admin" && !c.admin) tab = "hero";
    showTab(tab);
    loadChat();
  }

  $("switchchat").addEventListener("click", showPicker);
  $("chatsearch").addEventListener("input", renderChatList);
  if (tg && tg.BackButton) tg.BackButton.onClick(showPicker);

  // --- boot ---------------------------------------------------------

  function boot(me) {
    state.me = me;
    var chats = me.chats;
    var start = chats.filter(function (c) { return c.id === me.start_chat; })[0];
    if (start) openChat(start.id);
    else if (chats.length === 1) openChat(chats[0].id);
    else showPicker();
  }

  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.platform !== "unknown") document.documentElement.classList.add("tg");
  }
  fetch($("strings").href).then(function (r) { return r.json(); }).then(function (strings) {
    S = strings;
    applyTexts();
    return api("GET", "/me");
  }).then(boot).catch(showError);
})();
