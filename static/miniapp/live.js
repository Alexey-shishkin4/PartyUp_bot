"use strict";

const el = id => document.getElementById(id);
const tabs = [...document.querySelectorAll("[data-tab]")];
const tg = window.Telegram?.WebApp;
const localDebug = document.body.dataset.localDebug === "true";
let tab = "feed";
let current;
let busy = false;
const trail = [];
let profileMode = "card";
let ownProfile;
const photos = new WeakMap();
const contacts = new Map();
let defaultAvatar;
let matchRender = 0;

function node(tag, text, cls) {
  const out = document.createElement(tag);
  out.textContent = text;
  if (cls) out.className = cls;
  return out;
}

async function api(path, method = "GET", data) {
  const file = data instanceof Blob;
  const identity = localDebug ? { "X-PartyUp-Debug": "1" } : { Authorization: "tma " + tg.initData };
  const response = await fetch("/miniapp/api/" + path, {
    method, headers: { ...identity, "Content-Type": file ? data.type : "application/json" },
    body: file ? data : (data === undefined ? undefined : JSON.stringify(data)),
    signal: method === "GET" ? AbortSignal.timeout?.(/^(photo|contact)\//.test(path) ? 8000 : 15000) : undefined
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
  if (localDebug) el("go-back").hidden = !inner;
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

async function loadPhoto(img, card) {
  const previous = photos.get(img);
  if (previous?.url) URL.revokeObjectURL(previous.url);
  const loading = {};
  photos.set(img, loading);
  const portrait = img.closest(".portrait");
  const retry = portrait.querySelector(".retry-photo");
  retry.hidden = true;
  retry.onclick = () => loadPhoto(img, card);
  img.hidden = true;
  img.removeAttribute("src");
  portrait.setAttribute("aria-busy", String(card.has_photo));
  let photoFailed = false;
  try {
    if (!card.has_photo) {
      defaultAvatar ??= fetch("/miniapp/avatar", {signal: AbortSignal.timeout?.(8000)})
        .then(response => response.ok ? response.blob() : null).catch(() => null);
    }
    const image = card.has_photo ? await api("photo/" + card.id) : await defaultAvatar;
    if (photos.get(img) !== loading) return;
    if (image) {
      loading.url = URL.createObjectURL(image);
      img.src = loading.url;
      await img.decode();
    }
  } catch {
    if (photos.get(img) === loading) {
      if (loading.url) URL.revokeObjectURL(loading.url);
      delete loading.url;
      img.removeAttribute("src");
      photoFailed = card.has_photo;
    }
  }
  if (photos.get(img) !== loading) return;
  img.hidden = !img.hasAttribute("src");
  retry.hidden = !photoFailed;
  portrait.setAttribute("aria-busy", "false");
}

function renderFeed(card) {
  current = card;
  el("player").hidden = !card;
  el("empty").hidden = Boolean(card);
  if (!card) return;
  el("name").textContent = card.name + ", " + card.age;
  el("bio").textContent = card.bio || "Описание пока не заполнено.";
  el("position").hidden = !card.is_test;
  el("like-contact-note").hidden = card.is_test;
  el("position").textContent = "Тестовая анкета";
  const hour = n => String(n % 24).padStart(2, "0") + ":00";
  el("schedule").textContent = card.timezone + (card.schedule_set ?
    " · " + hour(card.start_hour) + "–" + hour(card.end_hour) : "");
  showTags("common", card.common_games, "Пока нет общих игр");
  showTags("games", [...card.games, ...card.additional_games], "Игры ещё не выбраны");
  el("player").hidden = false;
  return loadPhoto(el("photo"), card);
}

async function showContact(slot, id, refresh = false) {
  slot.textContent = "Загружаем контакт…";
  try {
    if (refresh || !contacts.has(id)) contacts.set(id, api("contact/" + id));
    const { username } = await contacts.get(id);
    if (!username) { slot.textContent = "У игрока пока нет @username."; return; }
    const link = node("a", "@" + username);
    link.href = "https://t.me/" + username;
    if (tg) link.onclick = event => { event.preventDefault(); tg.openTelegramLink(link.href); };
    else { link.target = "_blank"; link.rel = "noopener"; }
    slot.replaceChildren(link);
  } catch (error) {
    const retry = node("button", "Обновить контакт");
    retry.onclick = () => showContact(slot, id, true);
    slot.replaceChildren(node("small", error.message), retry);
  }
}

async function renderMatches(matches) {
  const version = ++matchRender;
  el("match-list").replaceChildren();
  el("match-empty").hidden = true;
  el("match-empty").hidden = matches.length > 0;
  const pending = matches.map(match => {
    const li = node("li", "");
    const contact = node("div", "Загружаем контакт…");
    li.append(node("strong", match.profile.name + ", " + match.profile.age), contact);
    el("match-list").append(li);
    return () => showContact(contact, match.id);
  });
  // Prepare the first visible contacts. Longer histories continue with four workers.
  const visible = pending.splice(0, 4);
  await Promise.all(visible.map(load => load()));
  async function nextContacts() {
    while (version === matchRender && pending.length) await pending.shift()();
  }
  const workers = Math.min(4, pending.length);
  for (let i = 0; i < workers; i++) void nextContacts();
}

function editor(profile) {
  profileMode = profile.registered ? "edit" : "create";
  el("own-card").hidden = true;
  el("editor").hidden = false;
  el("refresh-profile").hidden = true;
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

function showProfileCard() {
  if (!ownProfile.registered) { editor(ownProfile); return; }
  el("editor").hidden = true;
  el("own-card").hidden = false;
  el("refresh-profile").hidden = false;
  el("profile-title").textContent = "Моя анкета";
}

function renderProfile(profile) {
  ownProfile = profile;
  document.querySelector("nav").hidden = !profile.registered;
  if (!profile.registered) return;
  el("own-name").textContent = profile.name + ", " + profile.age;
  el("own-zone").textContent = profile.timezone;
  el("own-purpose").textContent = profile.purpose_label;
  el("own-bio").textContent = profile.bio || "Описание пока не заполнено.";
  showTags("own-games", [...profile.games, ...profile.additional_games], "Игры ещё не выбраны");
  return loadPhoto(el("own-photo"), profile);
}

function navigate(next, remember = true) {
  if (remember && next !== tab) trail.push(tab);
  profileMode = "card";
  tab = next;
  for (const button of tabs) {
    const selected = next === button.dataset.tab;
    el(button.dataset.tab).hidden = !selected;
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  el("status").textContent = "";
  if (tab === "profile") showProfileCard();
  backButton();
  window.scrollTo(0, 0);
}

async function action(task) {
  if (busy) return;
  busy = true;
  const controls = [...document.querySelectorAll("nav button, .actions button, #editor button, #again, #refresh-profile, #refresh-matches, #undo-button")];
  controls.forEach(button => { button.disabled = true; });
  try { await task(); }
  catch (e) { el("status").textContent = e.message; }
  finally { busy = false; controls.forEach(button => { button.disabled = false; }); }
}

function react(kind) {
  return action(async () => {
    if (!current) return;
    const { matched, match_id } = await api("react", "POST", { target: current.id, kind });
    const name = current.name;
    void renderFeed();
    if (matched) {
      void api("matches").then(renderMatches).catch(error => { el("status").textContent = error.message; });
      el("match-message").textContent = "У вас с игроком «" + name + "» взаимная симпатия.";
      el("mutual").showModal();
      const slot = node("div", "");
      el("mutual-contact").replaceChildren(slot);
      void showContact(slot, match_id);
      backButton();
    }
    const [next] = await api("feed");
    void renderFeed(next);
  });
}

async function prepareTabs() {
  const initial = await api("bootstrap");
  el("undo-button").hidden = !initial.can_undo;
  await Promise.all([
    renderProfile(initial.profile),
    renderFeed(initial.feed[0]),
    renderMatches(initial.matches)
  ]);
}

async function start() {
  tabs.forEach(button => { button.disabled = true; });
  if (!tg?.initData && !localDebug) {
    el("startup-animation").hidden = true;
    el("startup-spinner").hidden = true;
    el("loading").querySelector("p").textContent = "Открой PartyUp через /miniapp в личном чате с ботом.";
    el("preview").hidden = false;
    return;
  }
  tg?.ready();
  tg?.expand();
  el("retry").hidden = true;
  el("startup-animation").hidden = false;
  el("startup-spinner").hidden = false;
  el("loading").querySelector("p").textContent = "Открываем PartyUp…";
  try {
    await Promise.all([prepareTabs(), waitForStartup()]);
    el("loading").hidden = true;
    tabs.forEach(button => { button.disabled = false; });
    if (!localDebug) el("go-back").hidden = false;
    navigate(ownProfile.registered ? "feed" : "profile", false);
  } catch (e) {
    el("startup-animation").hidden = true;
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
tabs.forEach(button => { button.onclick = () => navigate(button.dataset.tab); });
el("retry").onclick = start;
el("again").onclick = () => action(async () => { const [card] = await api("feed"); await renderFeed(card); });
el("refresh-profile").onclick = () => action(async () => {
  await renderProfile(await api("me"));
  showProfileCard();
});
el("refresh-matches").onclick = () => action(async () => {
  const matches = await api("matches");
  contacts.clear();
  await renderMatches(matches);
});
el("pass").onclick = () => react("skip");
el("like").onclick = () => react("like");
el("undo-button").onclick = () => action(async () => {
  const result = await api("undo", "POST");
  el("undo-button").hidden = true;
  const card = result.card || (await api("feed"))[0];
  void renderFeed(card);
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
      ...(creating ? {consent: el("consent").checked} : {})
    });
    let message = creating ? "Анкета опубликована." : "Анкета сохранена.";
    if (file) {
      try { await api("me/photo", "PUT", file); }
      catch (error) { message += " Фото не изменилось. " + error.message; }
    }
    const [profile, [card]] = await Promise.all([api("me"), api("feed")]);
    void renderProfile(profile);
    void renderFeed(card);
    navigate("profile", false);
    el("status").textContent = message;
  });
};
el("continue").onclick = () => el("mutual").close();
el("show-matches").onclick = () => { el("mutual").close(); navigate("matches"); };
el("mutual").addEventListener("close", backButton);
if (tg?.isVersionAtLeast("6.1")) tg.BackButton.onClick(goBack);
start();
