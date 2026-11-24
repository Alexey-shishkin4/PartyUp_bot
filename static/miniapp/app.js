"use strict";

const byId = id => document.getElementById(id);
const tabs = [...document.querySelectorAll("[data-tab]")];
const storageKey = "partyup.miniapp.prototype.v1";
const defaults = { name: "Игрок", bio: "", games: ["ELDEN RING", "Phasmophobia", "Dota 2"] };
let cards = [];
let current;
let active = "feed";
let tg;
let profile = { ...defaults };
let reactions = [];

function restore() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
    if (!saved) return;
    if (saved.profile && typeof saved.profile.name === "string" && typeof saved.profile.bio === "string"
        && Array.isArray(saved.profile.games) && saved.profile.games.every(game => typeof game === "string")) {
      profile = saved.profile;
    }
    if (Array.isArray(saved.reactions)) {
      reactions = saved.reactions.filter(r => r && cards.some(c => c.slug === r.slug)
        && ["like", "pass"].includes(r.kind));
    }
  } catch { /* Use fresh in-memory state when storage is unavailable. */ }
}

function persist() {
  try {
    localStorage.setItem(storageKey, JSON.stringify({ profile, reactions }));
  } catch {
    byId("status").textContent = "Хранилище недоступно. Изменения сохранятся до закрытия экрана.";
  }
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function common(card) {
  return card.games.filter(game => profile.games.includes(game));
}

function timeLabel(card) {
  const offset = new Intl.DateTimeFormat("en", { timeZone: card.timezone, timeZoneName: "shortOffset" })
    .formatToParts(new Date()).find(part => part.type === "timeZoneName").value.replace("GMT", "UTC");
  const hour = value => String(value % 24).padStart(2, "0") + ":00";
  return offset + " · " + hour(card.start_hour) + "–" + hour(card.end_hour);
}

function renderFeed(preferredSlug) {
  const remaining = cards.filter(card => !reactions.some(r => r.slug === card.slug));
  remaining.sort((a, b) => common(b).length - common(a).length);
  current = remaining.find(card => card.slug === preferredSlug) || remaining[0];
  byId("player").hidden = !current;
  byId("empty").hidden = Boolean(current);
  byId("undo-button").disabled = reactions.length === 0;
  if (!current) return;
  byId("photo").src = current.photo;
  byId("name").textContent = current.name + ", " + current.age;
  byId("schedule").textContent = timeLabel(current);
  byId("position").textContent = (cards.length - remaining.length + 1) + " / " + cards.length;
  byId("bio").textContent = current.bio;
  const shared = common(current);
  byId("common").replaceChildren(...(shared.length ? shared.map(game => element("span", game, "tag"))
    : [element("span", "Пока нет пересечений", "hint")]));
  byId("games").replaceChildren(...current.games.map(game => element("span", game, "tag")));
}

function renderMatches() {
  const matched = reactions.filter(r => r.kind === "like")
    .map(r => cards.find(card => card.slug === r.slug)).filter(card => card?.incoming_like);
  byId("match-empty").hidden = matched.length > 0;
  byId("count").hidden = matched.length === 0;
  byId("count").textContent = matched.length;
  byId("match-list").replaceChildren(...matched.map(card => {
    const row = element("li", "");
    const photo = document.createElement("img");
    photo.alt = "";
    photo.src = card.photo;
    const details = element("div", "");
    details.append(element("strong", card.name + ", " + card.age),
      element("small", "@ник_игрока · пример контакта"));
    row.append(photo, details);
    return row;
  }));
}

function renderEditor() {
  byId("nickname").value = profile.name;
  byId("about").value = profile.bio;
  const games = [...new Set(cards.flatMap(card => card.games))].sort();
  byId("choices").replaceChildren(...games.map(game => {
    const label = element("label", "", "choice");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = game;
    checkbox.checked = profile.games.includes(game);
    label.append(checkbox, element("span", game));
    return label;
  }));
}

function syncBackButton() {
  if (!tg?.isVersionAtLeast("6.1")) return;
  if (active === "feed" && !byId("mutual").open) tg.BackButton.hide();
  else tg.BackButton.show();
}

function navigate(tab) {
  active = tab;
  for (const button of tabs) {
    const selected = button.dataset.tab === tab;
    byId(button.dataset.tab).hidden = !selected;
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (tab === "profile") renderEditor();
  byId("status").textContent = "";
  syncBackButton();
  window.scrollTo(0, 0);
}

function react(kind) {
  if (!current) return;
  const chosen = current;
  reactions.push({ slug: chosen.slug, kind });
  byId("status").textContent = kind === "like" ? "Лайк сохранён в макете." : "Анкета пропущена.";
  persist();
  renderFeed();
  renderMatches();
  window.scrollTo(0, 0);
  if (kind === "like" && chosen.incoming_like) {
    byId("match-message").textContent = "У тебя взаимный лайк с игроком «" + chosen.name + "». Теперь доступен пример Telegram-контакта.";
    byId("mutual").showModal();
    syncBackButton();
  }
}

async function start() {
  byId("retry").hidden = true;
  tabs.forEach(button => { button.disabled = true; });
  try {
    const response = await fetch("/miniapp/deck");
    if (!response.ok) throw new Error("Deck unavailable");
    cards = await response.json();
    restore();
    renderFeed();
    renderMatches();
    byId("loading").hidden = true;
    tabs.forEach(button => { button.disabled = false; });
  } catch {
    byId("loading").querySelector("p").textContent = "Не удалось загрузить анкеты.";
    byId("retry").hidden = false;
  }
}

// A browser preview also works offline from Telegram's script host.
function connectTelegram() {
  const launch = new URLSearchParams(location.hash.slice(1));
  if (!launch.has("tgWebAppVersion") && !launch.has("tgWebAppData")) return;
  const script = document.createElement("script");
  script.src = "https://telegram.org/js/telegram-web-app.js";
  script.onload = () => {
    tg = window.Telegram?.WebApp;
    if (!tg) return;
    tg.ready();
    tg.expand();
    if (tg.isVersionAtLeast("6.1")) {
      tg.BackButton.onClick(() => {
        if (byId("mutual").open) byId("mutual").close();
        else navigate("feed");
      });
      syncBackButton();
    }
  };
  document.head.append(script);
}

tabs.forEach(button => button.addEventListener("click", () => navigate(button.dataset.tab)));
byId("pass").onclick = () => react("pass");
byId("like").onclick = () => react("like");
byId("retry").onclick = start;
byId("undo-button").onclick = () => {
  const last = reactions.pop();
  persist();
  renderFeed(last?.slug);
  renderMatches();
  byId("status").textContent = "Последняя реакция отменена.";
};
byId("again").onclick = () => {
  reactions = [];
  persist();
  renderFeed();
  renderMatches();
  byId("status").textContent = "Лайки и взаимности сброшены. Начинаем новый просмотр.";
};
byId("mutual").addEventListener("close", syncBackButton);
byId("continue").onclick = () => byId("mutual").close();
byId("show-matches").onclick = () => { byId("mutual").close(); navigate("matches"); };
byId("editor").onsubmit = event => {
  event.preventDefault();
  const nickname = byId("nickname");
  if (!nickname.value.trim()) {
    nickname.setCustomValidity("Введите имя или ник.");
    nickname.reportValidity();
    return;
  }
  profile = { name: nickname.value.trim(), bio: byId("about").value.trim(),
    games: [...byId("choices").querySelectorAll(":checked")].map(input => input.value) };
  navigate("feed");
  renderFeed();
  byId("status").textContent = "Анкета сохранена. Подбор обновлён.";
  persist();
};
byId("nickname").oninput = event => event.target.setCustomValidity("");
connectTelegram();
start();
