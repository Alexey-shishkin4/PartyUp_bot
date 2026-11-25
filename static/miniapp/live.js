"use strict";

const el = id => document.getElementById(id);
const tabs = [...document.querySelectorAll("[data-tab]")];
const tg = window.Telegram?.WebApp;
let tab = "feed";
let current;
let busy = false;
const trail = [];
let profileMode = "card";
let ownProfile;
let ownProfileAt = 0;
const photos = new WeakMap();
let viewVersion = 0;

function node(tag, text, cls) {
  const out = document.createElement(tag);
  out.textContent = text;
  if (cls) out.className = cls;
  return out;
}

async function api(path, method = "GET", data) {
  const file = data instanceof Blob;
  const response = await fetch("/miniapp/api/" + path, {
    method, headers: { Authorization: "tma " + tg.initData, "Content-Type": file ? data.type : "application/json" },
    body: file ? data : (data === undefined ? undefined : JSON.stringify(data)),
    signal: method === "GET" ? AbortSignal.timeout?.(15000) : undefined
  });
  if (!response.ok) {
    throw new Error(response.status >= 500 ? "Сервис временно недоступен. Попробуй ещё раз."
      : (await response.text()) || "Не удалось выполнить запрос.");
  }
  if (path === "feed") el("undo-button").hidden = response.headers.get("X-Can-Undo") !== "1";
  return path.startsWith("photo/") ? response.blob() : response.json();
}

function backButton() {
  const inner = trail.length > 0 || profileMode === "edit" || el("mutual").open;
  el("go-back").textContent = inner ? "← Назад" : "← В чат";
  if (tg?.isVersionAtLeast("6.1")) {
    if (inner) tg.BackButton.show(); else tg.BackButton.hide();
  }
}

function goBack() {
  if (busy) return;
  if (el("mutual").open) el("mutual").close();
  else if (tab === "profile" && profileMode === "edit") navigate("profile", false);
  else if (trail.length) navigate(trail.pop(), false);
  else tg?.close();
}

function showTags(id, games, empty) {
  el(id).replaceChildren(...(games.length ? games.map(name => node("span", name, "tag"))
    : [node("span", empty, "hint")]));
}

async function loadPhoto(img, card, version) {
  if (photos.has(img)) {
    URL.revokeObjectURL(photos.get(img));
    photos.delete(img);
  }
  const portrait = img.closest(".portrait");
  const retry = portrait.querySelector(".retry-photo");
  retry.hidden = true;
  retry.onclick = () => loadPhoto(img, card, version);
  img.hidden = true;
  img.removeAttribute("src");
  portrait.setAttribute("aria-busy", String(card.has_photo));
  let photoFailed = false;
  if (card.has_photo) {
    try {
      const image = await api("photo/" + card.id);
      if (version !== viewVersion) return;
      const src = URL.createObjectURL(image);
      try {
        img.src = src;
        await img.decode();
        if (version !== viewVersion) {
          URL.revokeObjectURL(src);
          return;
        }
        photos.set(img, src);
      } catch (error) {
        URL.revokeObjectURL(src);
        throw error;
      }
    } catch {
      if (version !== viewVersion) return;
      photoFailed = true;
    }
  }
  if (version !== viewVersion) return;
  if (photoFailed) img.removeAttribute("src");
  if (!card.has_photo) {
    img.src = "/miniapp/avatar";
    try {
      await img.decode();
    } catch {
      if (version !== viewVersion) return;
      img.removeAttribute("src");
    }
  }
  if (version !== viewVersion) return;
  img.hidden = !img.hasAttribute("src");
  retry.hidden = !photoFailed;
  portrait.setAttribute("aria-busy", "false");
}

async function loadFeed(version, restored, initial) {
  current = undefined;
  el("player").hidden = true;
  el("empty").hidden = true;
  const card = restored || (initial ? initial.feed : await api("feed"))[0];
  if (version !== viewVersion) return;
  if (initial) el("undo-button").hidden = !initial.can_undo;
  current = card;
  el("empty").hidden = Boolean(card);
  if (!card) return;
  el("name").textContent = card.name + ", " + card.age;
  el("bio").textContent = card.bio || "Описание пока не заполнено.";
  el("position").hidden = !card.is_test;
  el("position").textContent = "Тестовая анкета";
  const hour = n => String(n % 24).padStart(2, "0") + ":00";
  el("schedule").textContent = card.timezone + (card.schedule_set ?
    " · " + hour(card.start_hour) + "–" + hour(card.end_hour) : "");
  showTags("common", card.common_games, "Пока нет общих игр");
  showTags("games", [...card.games, ...card.additional_games], "Игры ещё не выбраны");
  el("player").hidden = false;
  void loadPhoto(el("photo"), card, version);
}

async function loadMatches(version) {
  el("match-list").replaceChildren();
  el("match-empty").hidden = true;
  const matches = await api("matches");
  if (version !== viewVersion) return;
  el("match-empty").hidden = matches.length > 0;
  for (const match of matches) {
    const li = node("li", "");
    const contact = node("button", "Показать контакт");
    contact.onclick = async () => {
      contact.disabled = true;
      try {
        const { username } = await api("contact/" + match.id);
        if (username) {
          const link = node("a", "@" + username);
          link.href = "https://t.me/" + username;
          link.onclick = event => { event.preventDefault(); tg.openTelegramLink(link.href); };
          contact.replaceWith(link);
        } else contact.replaceWith(node("small", "У игрока пока нет @username."));
      } catch (e) { el("status").textContent = e.message; contact.disabled = false; }
    };
    li.append(node("strong", match.profile.name + ", " + match.profile.age), contact);
    el("match-list").append(li);
  }
}

function editor(profile) {
  profileMode = profile.registered ? "edit" : "create";
  el("own-card").hidden = true;
  el("editor").hidden = false;
  el("profile-title").textContent = profile.registered ? "Изменить анкету" : "Создать анкету";
  el("save-profile").textContent = profile.registered ? "Сохранить анкету" : "Опубликовать анкету";
  el("consent-line").hidden = profile.registered;
  el("consent").required = !profile.registered;
  el("consent").checked = false;
  el("avatar-file").value = "";
  el("game-search").value = "";
  el("age").value = profile.age || "";
  el("zone").value = profile.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Moscow";
  el("purpose").value = profile.purpose || "both";
  el("nickname").value = profile.name;
  el("about").value = profile.bio || "";
  el("share").checked = profile.share_username;
  el("choices").replaceChildren(...profile.catalog.map(game => {
    const label = node("label", "", "choice");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.value = game.id;
    check.checked = (profile.game_ids || []).includes(game.id);
    label.append(check, node("span", game.name));
    return label;
  }));
  backButton();
}

async function showProfile(version, initialProfile) {
  el("editor").hidden = true;
  el("own-card").hidden = true;
  const profile = initialProfile || (Date.now() - ownProfileAt < 15000 ? ownProfile : await api("me"));
  if (version !== viewVersion) return;
  if (profile !== ownProfile) ownProfileAt = Date.now();
  ownProfile = profile;
  document.querySelector("nav").hidden = !profile.registered;
  if (!profile.registered) { editor(profile); return; }
  el("profile-title").textContent = "Моя анкета";
  el("own-card").hidden = false;
  el("own-name").textContent = profile.name + ", " + profile.age;
  el("own-zone").textContent = profile.timezone;
  el("own-purpose").textContent = profile.purpose_label;
  el("own-bio").textContent = profile.bio || "Описание пока не заполнено.";
  showTags("own-games", [...profile.games, ...profile.additional_games], "Игры ещё не выбраны");
  void loadPhoto(el("own-photo"), profile, version);
}

async function navigate(next, remember = true, initial) {
  if (remember && next !== tab) trail.push(tab);
  profileMode = "card";
  tab = next;
  const version = ++viewVersion;
  for (const button of tabs) {
    const selected = next === button.dataset.tab;
    el(button.dataset.tab).hidden = !selected;
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  el("status").textContent = "";
  backButton();
  window.scrollTo(0, 0);
  el("section-loading").hidden = Boolean(initial);
  try {
    if (tab === "feed") await loadFeed(version, null, initial);
    else if (tab === "matches") await loadMatches(version);
    else await showProfile(version, initial?.profile);
  } catch (e) {
    if (version === viewVersion) el("status").textContent = e.message;
  } finally {
    if (version === viewVersion) el("section-loading").hidden = true;
  }
}

async function action(task) {
  if (busy) return;
  busy = true;
  const controls = [...document.querySelectorAll("nav button, .actions button, #editor button, #again")];
  controls.forEach(button => { button.disabled = true; });
  try { await task(); }
  catch (e) { el("status").textContent = e.message; }
  finally { busy = false; controls.forEach(button => { button.disabled = false; }); }
}

function react(kind) {
  return action(async () => {
    if (!current) return;
    const { matched } = await api("react", "POST", { target: current.id, kind });
    const name = current.name;
    await navigate("feed");
    if (matched) {
      el("match-message").textContent = "У вас с игроком «" + name + "» взаимная симпатия.";
      el("mutual").showModal();
      backButton();
    }
  });
}

async function start() {
  tabs.forEach(button => { button.disabled = true; });
  if (!tg?.initData) {
    el("startup-spinner").hidden = true;
    el("loading").querySelector("p").textContent = "Открой PartyUp через /miniapp в личном чате с ботом.";
    el("preview").hidden = false;
    return;
  }
  tg.ready();
  tg.expand();
  el("retry").hidden = true;
  el("startup-spinner").hidden = false;
  el("loading").querySelector("p").textContent = "Открываем PartyUp…";
  try {
    const initial = await api("bootstrap");
    ownProfile = initial.profile;
    ownProfileAt = Date.now();
    el("loading").hidden = true;
    tabs.forEach(button => { button.disabled = false; });
    el("go-back").hidden = false;
    await navigate(initial.profile.registered ? "feed" : "profile", false, initial);
  } catch (e) {
    el("startup-spinner").hidden = true;
    el("loading").querySelector("p").textContent = e.name === "TimeoutError" ? "PartyUp долго отвечает. Попробуй ещё раз." : e.message;
    el("retry").hidden = false;
  }
}

el("empty").querySelector("p").textContent = "Новые анкеты появятся здесь. Поиск можно обновить.";
el("again").textContent = "Обновить подбор";
el("editor").querySelector(".hint").textContent = "Выбери до 10 игр. Эту анкету увидят в боте и в Mini App.";
el("details").hidden = el("avatar-tools").hidden = el("game-search").hidden = false;
el("age").required = el("zone").required = true;
el("zones").replaceChildren(...(Intl.supportedValuesOf?.("timeZone") || ["Europe/Moscow", "Asia/Novosibirsk"])
  .map(zone => node("option", zone)));
el("edit-profile").onclick = () => editor(ownProfile);
el("go-back").onclick = goBack;
el("undo-button").textContent = "Вернуть пропуск";
el("undo-button").disabled = false;
el("undo-button").hidden = true;
el("game-search").oninput = event => {
  const query = event.target.value.trim().toLowerCase();
  for (const label of el("choices").children) label.hidden = !label.textContent.toLowerCase().includes(query);
};
el("sharing").hidden = false;
tabs.forEach(button => { button.onclick = () => navigate(button.dataset.tab); });
el("retry").onclick = start;
el("again").onclick = () => navigate("feed");
el("pass").onclick = () => react("skip");
el("like").onclick = () => react("like");
el("undo-button").onclick = () => action(async () => {
  const result = await api("undo", "POST");
  el("undo-button").hidden = true;
  await loadFeed(++viewVersion, result.card);
  el("status").textContent = result.card ? "Предыдущая анкета возвращена." : "Этот пропуск больше нельзя вернуть.";
});
el("editor").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const creating = profileMode === "create";
    const ids = [...el("choices").querySelectorAll(":checked")].map(input => Number(input.value));
    if (ids.length > 10 || (creating && !ids.length)) throw new Error("Выбери от 1 до 10 игр.");
    const file = el("avatar-file").files[0];
    if (file && (file.size > 10 * 1024 * 1024 || !["image/jpeg", "image/png"].includes(file.type))) {
      throw new Error("Выбери фото JPEG или PNG до 10 МБ.");
    }
    await api("me", creating ? "POST" : "PUT", {
      name: el("nickname").value.trim(), bio: el("about").value.trim(), age: Number(el("age").value),
      timezone: el("zone").value.trim(), purpose: el("purpose").value, game_ids: ids,
      share_username: el("share").checked, ...(creating ? {consent: el("consent").checked} : {})
    });
    let message = creating ? "Анкета опубликована." : "Анкета сохранена.";
    if (file) {
      try { await api("me/photo", "PUT", file); }
      catch (error) { message += " Фото не изменилось. " + error.message; }
    }
    ownProfileAt = 0;
    await navigate("profile", false);
    el("status").textContent = message;
  });
};
el("continue").onclick = () => el("mutual").close();
el("show-matches").onclick = () => { el("mutual").close(); navigate("matches"); };
el("mutual").addEventListener("close", backButton);
if (tg?.isVersionAtLeast("6.1")) tg.BackButton.onClick(goBack);
start();
