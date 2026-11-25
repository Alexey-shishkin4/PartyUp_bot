"use strict";

const el = id => document.getElementById(id);
const tabs = [...document.querySelectorAll("[data-tab]")];
const tg = window.Telegram?.WebApp;
let tab = "feed";
let current;
let photoURL;
let busy = false;
let lastSkip = false;
let viewVersion = 0;

function node(tag, text, cls) {
  const out = document.createElement(tag);
  out.textContent = text;
  if (cls) out.className = cls;
  return out;
}

async function api(path, method = "GET", data) {
  const response = await fetch("/miniapp/api/" + path, {
    method, headers: { Authorization: "tma " + tg.initData, "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  if (!response.ok) {
    throw new Error(response.status >= 500 ? "Сервис временно недоступен. Попробуй ещё раз."
      : (await response.text()) || "Не удалось выполнить запрос.");
  }
  return path.startsWith("photo/") ? response.blob() : response.json();
}

function backButton() {
  if (!tg?.isVersionAtLeast("6.1")) return;
  if (tab === "feed" && !el("mutual").open) tg.BackButton.hide();
  else tg.BackButton.show();
}

function showTags(id, games, empty) {
  el(id).replaceChildren(...(games.length ? games.map(name => node("span", name, "tag"))
    : [node("span", empty, "hint")]));
}

async function loadFeed(version) {
  current = undefined;
  el("player").hidden = true;
  el("empty").hidden = true;
  const [card] = await api("feed");
  if (version !== viewVersion) return;
  current = card;
  el("undo-button").disabled = !lastSkip;
  el("empty").hidden = Boolean(card);
  if (!card) return;
  el("photo").src = "/miniapp/avatar";
  if (photoURL) { URL.revokeObjectURL(photoURL); photoURL = undefined; }
  el("name").textContent = card.name + ", " + card.age;
  el("bio").textContent = card.bio || "Описание пока не заполнено.";
  el("position").hidden = !card.is_test;
  el("position").textContent = "Тестовая анкета";
  const hour = n => String(n % 24).padStart(2, "0") + ":00";
  el("schedule").textContent = card.timezone + (card.schedule_set ?
    " · " + hour(card.start_hour) + "–" + hour(card.end_hour) : "");
  showTags("common", card.common_games, "Пока нет общих игр");
  showTags("games", card.games, "Игры ещё не выбраны");
  el("player").hidden = false;
  if (card.has_photo) {
    try {
      const image = await api("photo/" + card.id);
      if (viewVersion === version && current === card) {
        photoURL = URL.createObjectURL(image);
        el("photo").src = photoURL;
      }
    } catch { /* Keep the default avatar if Telegram cannot deliver the image. */ }
  }
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
  el("nickname").value = profile.name;
  el("about").value = profile.bio;
  el("share").checked = profile.share_username;
  el("choices").replaceChildren(...profile.catalog.map(game => {
    const label = node("label", "", "choice");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.value = game.id;
    check.checked = profile.game_ids.includes(game.id);
    label.append(check, node("span", game.name));
    return label;
  }));
}

async function navigate(next) {
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
  try {
    if (tab === "feed") await loadFeed(version);
    else if (tab === "matches") await loadMatches(version);
    else {
      el("editor").hidden = true;
      const profile = await api("me");
      if (version === viewVersion) { editor(profile); el("editor").hidden = false; }
    }
  } catch (e) {
    if (version === viewVersion) el("status").textContent = e.message;
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
    lastSkip = kind === "skip";
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
    el("loading").querySelector("p").textContent = "Открой PartyUp через /miniapp в личном чате с ботом.";
    el("preview").hidden = false;
    return;
  }
  tg.ready();
  tg.expand();
  try {
    await api("me");
    el("loading").hidden = true;
    tabs.forEach(button => { button.disabled = false; });
    await navigate("feed");
  } catch (e) {
    el("loading").querySelector("p").textContent = e.message;
    el("retry").hidden = false;
  }
}

el("empty").querySelector("p").textContent = "Новые анкеты появятся здесь. Поиск можно обновить.";
el("again").textContent = "Обновить подбор";
el("editor").querySelector(".hint").textContent = "Изменения появятся и в боте. Фото можно заменить через /photo.";
el("sharing").hidden = false;
tabs.forEach(button => { button.onclick = () => navigate(button.dataset.tab); });
el("retry").onclick = start;
el("again").onclick = () => navigate("feed");
el("pass").onclick = () => react("skip");
el("like").onclick = () => react("like");
el("undo-button").onclick = () => action(async () => {
  await api("undo", "POST");
  lastSkip = false;
  await navigate("feed");
});
el("editor").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    await api("me", "PUT", { name: el("nickname").value.trim(), bio: el("about").value.trim(),
      game_ids: [...el("choices").querySelectorAll(":checked")].map(input => Number(input.value)),
      share_username: el("share").checked });
    await navigate("feed");
    el("status").textContent = "Анкета сохранена.";
  });
};
el("continue").onclick = () => el("mutual").close();
el("show-matches").onclick = () => { el("mutual").close(); navigate("matches"); };
el("mutual").addEventListener("close", backButton);
if (tg?.isVersionAtLeast("6.1")) tg.BackButton.onClick(() => {
  if (el("mutual").open) el("mutual").close(); else navigate("feed");
});
start();
