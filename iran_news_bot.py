#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Телеграм-бот: новости по Ирану — ТОЛЬКО перемирие / атаки / заявления Трампа.

Что нового в этой версии:
  1. ФИЛЬТР ТЕМ: пропускаем новость, только если она про Иран И относится к одной
     из трёх тем — перемирие/сделка, атаки/удары, заявления Трампа.
  2. СКЛЕЙКА ИНФОПОВОДА: собираем новости со всех лент, группируем по одному и тому
     же событию (по совпадению ключевых слов заголовка) и шлём ОДНУ лучшую —
     от самого авторитетного источника. Больше не 10 сообщений про одно и то же.

Как работает (просто):
  - Раз в 2 минуты опрашивает ленты (Google News — агрегатор тысяч мировых СМИ
    на русском и английском + BBC, Al Jazeera, Guardian).
  - Новые события (которых ещё не отправлял) шлёт со ссылкой и пометкой темы:
    🕊 перемирие   💥 атака   🇺🇸 Трамп
  - Что уже слал — помнит в seen.json, повторов и дублей одного события не будет.

ЗАПУСК — см. шапку прошлой версии / ИНСТРУКЦИЯ_Oracle.md. Команды те же:
    python3 iran_news_bot.py --chatid   # узнать chat_id
    python3 iran_news_bot.py --test     # проверка связи
    python3 iran_news_bot.py            # рабочий режим
Только стандартная библиотека + curl.
"""
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

# ----------------------------- НАСТРОЙКИ -----------------------------------
# Токен и chat_id берутся ТОЛЬКО из окружения (в коде их нет — чтобы безопасно
# выкладывать в публичный репозиторий GitHub). Где задаются:
#   - на Mac: в файле автозапуска (LaunchAgent .plist, секция EnvironmentVariables);
#   - на GitHub Actions: в Settings -> Secrets (IRAN_BOT_TOKEN, IRAN_BOT_CHAT);
#   - вручную в терминале: export IRAN_BOT_TOKEN=...  export IRAN_BOT_CHAT=...
BOT_TOKEN = os.environ.get("IRAN_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("IRAN_BOT_CHAT", "")

CHECK_EVERY = 120          # секунд между опросами лент
MAX_PER_CYCLE = 12         # не слать больше N событий за один проход (защита от потопа)
EVENT_TTL = 72 * 3600      # сколько помнить отправленное событие (сек), чтобы не дублить
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

# Google News — запросы под наши три темы (when:1d = только за сутки)
GN_EN = ("https://news.google.com/rss/search?q=" +
         quote('Iran (ceasefire OR truce OR strike OR attack OR missile OR Trump) when:1d') +
         "&hl=en-US&gl=US&ceid=US:en")
GN_RU = ("https://news.google.com/rss/search?q=" +
         quote('Иран (перемирие OR сделка OR удар OR атака OR ракеты OR Трамп) when:1d') +
         "&hl=ru&gl=RU&ceid=RU:ru")

FEEDS = [
    ("GoogleNews-EN", GN_EN),
    ("GoogleNews-RU", GN_RU),
    ("BBC",       "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("AlJazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Guardian",  "https://www.theguardian.com/world/rss"),
]

# ----------------------------- ФИЛЬТР ТЕМ ----------------------------------
KW_IRAN = ("iran", "iranian", "tehran", "иран", "тегеран")
# три разрешённые темы:
KW_CEASE = ("ceasefire", "cease-fire", "truce", "peace deal", "peace talks", "deal",
            "agreement", "перемир", "прекращ", "соглашен", "мирн", "сделк",
            "переговор", "деэскал", "урегулир")
KW_ATTACK = ("strike", "attack", "missile", "drone", "bomb", "shelling", "airstrike",
             "air raid", "offensive", "killed", "explosion", "удар", "атак", "ракет",
             "обстрел", "бомб", "налет", "налёт", "беспилотник", "наступлен", "взрыв",
             "жертв", "погиб", "ранен")
KW_TRUMP = ("trump", "трамп")

CAT_EMOJI = {"cease": "🕊", "attack": "💥", "trump": "🇺🇸"}
CAT_NAME = {"cease": "перемирие", "attack": "атака", "trump": "Трамп"}

# Смысловые метки (язык-независимые): по ним русская и английская версии одного
# события склеиваются в один инфоповод, даже если в заголовках нет общих слов.
CONCEPT = {
    "trump":  ("trump", "трамп"),
    "cancel": ("cancel", "called off", "call off", "отмен", "отказал"),
    "deal":   ("ceasefire", "truce", "deal", "settlement", "agreement", "peace",
               "перемир", "сделк", "соглашен", "урегулир", "мирн"),
    "attack": ("strike", "attack", "missile", "bomb", "airstrike", "offensive",
               "удар", "атак", "ракет", "обстрел", "бомб", "налет", "налёт"),
    "talks":  ("talks", "negotiat", "переговор"),
    "nuclear": ("nuclear", "ядер"),
    "hormuz": ("hormuz", "ормуз"),
    "oil":    ("oil price", "crude", "нефт"),
    "threat": ("threat", "warns", "warning", "угроз", "предупред", "пригроз"),
}

# Авторитетность источника (меньше = лучше; из группы про одно событие берём лучший).
SOURCE_RANK = [
    (("reuters", "associated press", "ap news", "bloomberg", "bbc", "al jazeera",
      "financial times", "the guardian", "guardian", "wall street journal",
      "new york times", "washington post", "cnn", "axios", "politico"), 0),
    (("тасс", "интерфакс", "риа", "ria", "коммерсант", "рбк", "ведомости",
      "meduza", "медуза", "euronews", "the hill", "newsweek", "cnbc"), 1),
]


def source_rank(src):
    s = src.lower()
    for names, rank in SOURCE_RANK:
        if any(n in s for n in names):
            return rank
    return 5  # все прочие


# слова, которые НЕ помогают отличать события (выкидываем при склейке)
STOP = set((
    "и в во не на что он а то все она так его но да ты к у же вы за бы по только ее "
    "мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или "
    "ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может "
    "они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз "
    "тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь "
    "этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при "
    "наконец два об другой хоть после над больше тот через эти нас про всего них какая "
    "много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том "
    "нельзя такой им более всегда конечно всю между иран ирана иране ирану ираном "
    "сша заявил заявление сказал объявил видео новости the of to in a and on for at by "
    "with iran iranian about over says said after amid new news").split())


# ----------------------------- УТИЛИТЫ -------------------------------------
def http_get(url, timeout=25):
    r = subprocess.run(["curl", "-sL", "--max-time", str(timeout), url],
                       capture_output=True)
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""


def tg_api(method, **params):
    cmd = ["curl", "-s", "--max-time", "20",
           f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"]
    for k, v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True)
    try:
        return json.loads(r.stdout.decode("utf-8", errors="replace"))
    except Exception:
        return {"ok": False, "raw": r.stdout[:200]}


def send(text):
    resp = tg_api("sendMessage", chat_id=CHAT_ID, text=text,
                  parse_mode="HTML", disable_web_page_preview="false")
    if not resp.get("ok"):
        print(f"[!] телеграм не принял сообщение: {resp}", flush=True)
    return resp.get("ok", False)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def stem(w):
    """Грубое отбрасывание русских окончаний: удар/удары/ударам -> удар."""
    for s in ("ами", "ями", "ого", "ему", "ыми", "ому", "ах", "ях", "ов", "ев",
              "ам", "ям", "ом", "ем", "ой", "ый", "ий", "ая", "ую", "ые", "ие",
              "а", "я", "о", "е", "у", "ю", "ы", "и", "й", "ь"):
        if len(w) - len(s) >= 4 and w.endswith(s):
            return w[:-len(s)]
    return w


def keywords(title):
    """Значимые слова заголовка (для сравнения «то же событие или нет»)."""
    return {stem(w) for w in re.findall(r"[a-zа-яё0-9]+", title.lower())
            if len(w) >= 4 and w not in STOP}


def concept_tags(title):
    t = title.lower()
    return {tag for tag, ws in CONCEPT.items() if any(w in t for w in ws)}


def same_event(a, b):
    """a, b — события вида {'sig': set, 'tags': set, 'ts': float}. Одно событие, если:
       1) заголовки делят достаточно значимых слов (тот же язык, перефразировки), ИЛИ
       2) совпадает набор смысловых меток (кросс-язык) в пределах 6 часов."""
    sa, sb = a["sig"], b["sig"]
    if sa and sb:
        inter = len(sa & sb)
        if inter and (inter / len(sa | sb) >= 0.34 or inter / min(len(sa), len(sb)) >= 0.6):
            return True
    ta, tb = a["tags"], b["tags"]
    if len(ta) >= 2 and len(tb) >= 2 and abs(a["ts"] - b["ts"]) < 6 * 3600:
        if ta == tb or len(ta & tb) / len(ta | tb) >= 0.6:
            return True
    return False


def topic_of(title, desc):
    """Темы новости. Возвращает список из {'cease','attack','trump'} или []."""
    t = (title + " " + desc).lower()
    if not any(k in t for k in KW_IRAN):
        return []
    cats = []
    if any(k in t for k in KW_CEASE):
        cats.append("cease")
    if any(k in t for k in KW_ATTACK):
        cats.append("attack")
    if any(k in t for k in KW_TRUMP):
        cats.append("trump")
    return cats


def parse_feed(xml_text, name):
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        src = (it.findtext("source") or name).strip()
        if title.endswith(" - " + src):
            title = title[:-(len(src) + 3)].strip()
        desc = it.findtext("description") or ""
        cats = topic_of(title, desc)
        if not cats:
            continue
        ts = 0
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except Exception:
            pass
        out.append({"title": title, "link": link, "src": src, "ts": ts, "cats": cats,
                    "sig": keywords(title), "tags": concept_tags(title)})
    return out


def load_events():
    try:
        data = json.load(open(SEEN_FILE))
        ev = data.get("events", [])
        now = time.time()
        return [e for e in ev if now - e.get("ts", 0) < EVENT_TTL]
    except Exception:
        return []


def save_events(events):
    events = sorted(events, key=lambda e: e.get("ts", 0))[-1000:]
    json.dump({"events": events}, open(SEEN_FILE, "w"))


def cluster_and_pick(candidates, sent_events):
    """Из всех кандидатов выбираем по одному ЛУЧШЕМУ на каждое НОВОЕ событие.

    Лучший = самый авторитетный источник (при равенстве — свежее).
    Новое = не совпадает ни с уже отправленным событием, ни с выбранным в этом цикле.
    """
    # лучший источник первым: так представителем группы станет именно он
    cand = sorted(candidates, key=lambda x: (source_rank(x["src"]), -x["ts"]))
    sent = [{"sig": set(e["w"]), "tags": set(e.get("t", [])), "ts": e.get("ts", 0)}
            for e in sent_events]
    picked = []
    for it in cand:
        if any(same_event(it, s) for s in sent):
            continue                       # это событие уже отправляли раньше
        if any(same_event(it, p) for p in picked):
            continue                       # уже выбрали лучшую по этому событию
        picked.append(it)
    return picked


# ----------------------------- РЕЖИМЫ --------------------------------------
def mode_chatid():
    print("Напиши своему боту любое сообщение в телеграме, жду 60 секунд...")
    t0 = time.time()
    while time.time() - t0 < 60:
        resp = tg_api("getUpdates", timeout="5")
        for u in resp.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat", {})
            if chat.get("id"):
                who = chat.get("username") or chat.get("first_name", "")
                print(f"\nТвой CHAT_ID = {chat['id']}  ({who})")
                return
        time.sleep(2)
    print("Сообщений не пришло. Проверь токен и что написал боту, попробуй ещё раз.")


def mode_test():
    ok = send("✅ Бот на связи. Темы: 🕊 перемирие, 💥 атаки, 🇺🇸 заявления Трампа. "
              "Шлю по одной лучшей новости на каждый инфоповод.")
    print("отправлено" if ok else "НЕ отправлено — проверь BOT_TOKEN и CHAT_ID")


def gather():
    cands = []
    for name, url in FEEDS:
        xml_text = http_get(url)
        if not xml_text:
            print(f"[!] лента {name} не ответила", flush=True)
            continue
        cands += parse_feed(xml_text, name)
    return cands


def record(events, it, now):
    # время статьи (а не «сейчас») — иначе окно склейки дублей в след. цикле врёт
    events.append({"w": list(it["sig"]), "t": list(it["tags"]),
                   "ts": it["ts"] or now, "title": it["title"]})


def cycle(events, first_run):
    cands = gather()
    picks = cluster_and_pick(cands, events)
    now = time.time()
    if first_run:
        for it in picks:
            record(events, it, now)
        save_events(events)
        print(f"первый запуск: запомнил {len(picks)} текущих событий "
              f"(из {len(cands)} новостей), дальше шлю только новые", flush=True)
        return 0
    sent = 0
    for it in sorted(picks, key=lambda x: x["ts"]):     # старые -> новые
        if sent >= MAX_PER_CYCLE:
            print(f"[i] лимит {MAX_PER_CYCLE}/проход, остальное в следующий раз", flush=True)
            break
        tags = " ".join(f"{CAT_EMOJI[c]}" for c in it["cats"])
        names = "/".join(CAT_NAME[c] for c in it["cats"])
        txt = (f"{tags} <b>{esc(names)}</b> · {esc(it['src'])}\n"
               f"{esc(it['title'])}\n{it['link']}")
        if send(txt):
            sent += 1
            record(events, it, now)
            print(f"  -> [{names}] {it['src']}: {it['title'][:70]}", flush=True)
        time.sleep(1.2)
    if sent:
        save_events(events)
    return sent


def need_creds():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("Нет токена/chat_id. Задай переменные окружения IRAN_BOT_TOKEN и "
                 "IRAN_BOT_CHAT (на GitHub — в Secrets, на Mac — в .plist автозапуска).")


def mode_once():
    """Одна проверка и выход — для GitHub Actions (запуск по расписанию)."""
    need_creds()
    events = load_events()
    first_run = not events
    if first_run:
        cycle(events, True)
        send("✅ Бот запущен на GitHub. Темы: 🕊 перемирие, 💥 атаки, 🇺🇸 Трамп. "
             "Текущие новости запомнил, дальше — по одной лучшей на каждый новый инфоповод.")
        print("первый запуск завершён", flush=True)
        return
    n = cycle(events, False)
    print(f"проверка завершена, отправлено {n}", flush=True)


def mode_run():
    """Постоянная работа с опросом по таймеру — для Mac/сервера."""
    need_creds()
    events = load_events()
    first_run = not events
    print(f"старт: лент {len(FEEDS)}, опрос каждые {CHECK_EVERY} сек, "
          f"память: {len(events)} событий", flush=True)
    if first_run:
        cycle(events, True)
        send("✅ Бот запущен. Темы: 🕊 перемирие, 💥 атаки, 🇺🇸 Трамп. "
             "Текущие новости запомнил, дальше — по одной лучшей на каждый новый инфоповод.")
    while True:
        try:
            n = cycle(events, False)
            if n:
                print(f"[{time.strftime('%H:%M:%S')}] отправлено {n}", flush=True)
        except Exception as e:
            print(f"[!] ошибка цикла: {e}", flush=True)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    if "--chatid" in sys.argv:
        mode_chatid()
    elif "--test" in sys.argv:
        mode_test()
    elif "--once" in sys.argv:
        mode_once()
    else:
        mode_run()
