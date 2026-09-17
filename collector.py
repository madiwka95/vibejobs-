#!/usr/bin/env python3
"""VibeJobs collector — сбор вакансий со всех источников в единый формат.

Источники (только реальные, без демо-данных):
  1. hh.ru API (бесплатно, без ключа): https://api.hh.ru/vacancies
  2. Habr Career (парсинг карточек: заголовок, компания, грейд, город, навыки, зарплата)
  3. Telegram-каналы (импорт из telegram_vacancies.json — экспорт вручную)

Единый формат вакансии:
  {id, title, company, salary_from, salary_to, currency, city, remote,
   stack_tags[], source, url, published, desc, vibe_score, vibe_reasons, junior}

Запуск как CLI:
  python collector.py --query "python junior" --out jobs_cache.json
"""

import argparse
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "0.1.0"
UA = f"VibeJobs/{VERSION} (pet-project; contact: you@example.com)"
CACHE_TTL_SEC = 30 * 60

# --- стек: тег -> ключевые подстроки (в нижнем регистре) ---
STACK_KEYWORDS = {
    "python": ["python", "django", "fastapi", "flask", "aiogram"],
    "javascript": ["javascript", " js ", " js,", "node", "react", "vue"],
    "typescript": ["typescript", " ts ", "next.js", "nextjs", "nestjs"],
    "react": ["react", "next.js", "nextjs", "redux"],
    "vue": ["vue", "nuxt"],
    "node.js": ["node", "express", "nestjs"],
    "fastapi": ["fastapi"],
    "django": ["django"],
    "telegram-bots": ["telegram", "телеграм", "aiogram", "telebot",
                    " бот", "боты", "бота", "ботов", "боте", "ботом"],
    "ai/llm": [" llm", "openai", "chatgpt", "нейро", " ии", "ии-", "(ии", " ml",
               "ml-", "machine learning",
               "langchain", "rag", "prompt", " Muse", "gemini", "stable diffusion"],
    "flutter": ["flutter", "dart"],
    "go": ["golang", " go ", "gin-gonic", "fiber"],
    "php": ["php", "laravel", "wordpress", "bitrix"],
    "no-code": ["no-code", "nocode", "tilda", "bubble", "n8n", "zapier", "make.com", "airtable"],
    "next.js": ["next.js", "nextjs"],
}

VIBE_GOOD = [
    ("junior", 30, "junior-friendly: подходит новичку"),
    ("стаж", 30, "стажировка — хороший вход"),
    ("без опыта", 30, "можно без опыта"),
    ("trainee", 25, "trainee-позиция"),
    ("удален", 20, "удалёнка"),
    ("remote", 20, "remote"),
    ("гибрид", 10, "гибрид"),
    ("mvp", 15, "MVP — классика вайб-кодинга"),
    ("стартап", 12, "стартап"),
    ("лендинг", 10, "лендинг — быстрая задача"),
    ("телеграм", 10, "телеграм-бот"),
    ("бот", 8, "бот"),
    ("vibe", 15, "прямо пишут про vibe"),
    ("вайб", 15, "вайб-кодинг упоминается"),
    ("no-code", 12, "no-code friendly"),
    ("nocode", 12, "no-code friendly"),
    ("n8n", 10, "автоматизация n8n"),
    ("нейро", 8, "работа с нейросетями"),
    ("cursor", 8, "разрешают AI-инструменты"),
    ("copilot", 8, "разрешают AI-инструменты"),
    ("ai ", 5, "AI в стеке"),
    ("ии", 5, "AI в стеке"),
    ("частичная занятость", 10, "частичная занятость"),
    ("проектная", 8, "проектная работа"),
    ("фриланс", 8, "фриланс"),
]

VIBE_BAD = [
    ("senior", -25, "senior — не для старта"),
    ("сеньор", -25, "senior — не для старта"),
    ("lead", -20, "lead-позиция"),
    ("лид", -15, "lead-позиция"),
    ("5+ лет", -20, "требуют 5+ лет опыта"),
    ("5 лет", -15, "требуют много опыта"),
    ("3+ лет", -10, "требуют 3+ года"),
    ("архитектор", -15, "уровень архитектора"),
    ("teamlead", -20, "тимлид"),
    ("только офис", -10, "строго офис"),
    ("1с", -15, "1С — не вайб-стек"),
    ("cobol", -15, "legacy-стек"),
]


def fetch_url(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def detect_stack(text):
    t = " " + (text or "").lower() + " "
    tags = []
    for tag, keys in STACK_KEYWORDS.items():
        for k in keys:
            if k.lower() in t:
                tags.append(tag)
                break
    return sorted(set(tags))


def vibe_score(title, desc, remote=False, experience=""):
    text = f"{title}\n{desc}\n{experience}".lower()
    score = 50  # база
    reasons = []
    for kw, pts, why in VIBE_GOOD:
        if kw in text or (kw in ("удален",) and remote):
            score += pts
            reasons.append(f"+{pts}: {why}")
    # remote флаг отдельно (часто в API полем, а не текстом)
    if remote and "удалёнка" not in " ".join(reasons):
        score += 20
        reasons.append("+20: удалёнка")
    for kw, pts, why in VIBE_BAD:
        if kw in text:
            score += pts  # pts отрицательный
            reasons.append(f"{pts}: {why}")
    score = max(0, min(100, score))
    junior = any(s in text for s in ("junior", "стаж", "без опыта", "trainee", "начинающ", "jun"))
    return score, reasons, junior


def norm_job(**kw):
    tags = kw.get("stack_tags") or detect_stack(kw.get("title", "") + "\n" + kw.get("desc", ""))
    score, reasons, junior = vibe_score(kw.get("title", ""), kw.get("desc", ""),
                                        kw.get("remote", False), kw.get("experience", ""))
    # если теги уже есть — не пересчитывать junior дважды, просто вернуть
    return {
        "id": kw.get("id", ""),
        "title": kw.get("title", ""),
        "company": kw.get("company", ""),
        "salary_from": kw.get("salary_from"),
        "salary_to": kw.get("salary_to"),
        "currency": kw.get("currency", "RUR"),
        "city": kw.get("city", ""),
        "remote": bool(kw.get("remote", False)),
        "stack_tags": tags,
        "source": kw.get("source", "unknown"),
        "url": kw.get("url", ""),
        "published": kw.get("published", ""),
        "desc": (kw.get("desc", "") or "")[:600],
        "experience": kw.get("experience", ""),
        "vibe_score": score,
        "vibe_reasons": reasons[:6],
        "junior": junior,
    }


# ---------- 1. hh.ru ----------
def fetch_hh(query="python junior", per_page=30, timeout=10):
    """Бесплатный публичный API hh.ru, ключ не нужен."""
    jobs = []
    try:
        q = urllib.parse.urlencode({
            "text": query, "per_page": min(per_page, 50), "page": 0,
            "area": 113,  # Россия; уберите для всего мира
            "order_by": "publication_time",
        })
        data = json.loads(fetch_url("https://api.hh.ru/vacancies?" + q, timeout))
        for it in data.get("items", []):
            sal = it.get("salary") or {}
            snippet = it.get("snippet") or {}
            desc = " ".join(x for x in [snippet.get("requirement", ""), snippet.get("responsibility", "")] if x)
            desc = re.sub("<.*?>", " ", desc)
            exp = (it.get("experience") or {}).get("name", "")
            sched = (it.get("schedule") or {}).get("id", "")
            remote = sched == "remote"
            jobs.append(norm_job(
                id=f"hh-{it.get('id')}",
                title=html.unescape(it.get("name", "")),
                company=(it.get("employer") or {}).get("name", ""),
                salary_from=sal.get("from"), salary_to=sal.get("to"),
                currency=sal.get("currency", "RUR"),
                city=(it.get("area") or {}).get("name", ""),
                remote=remote, source="hh.ru",
                url=it.get("alternate_url", ""),
                published=it.get("published_at", "")[:10],
                desc=html.unescape(desc), experience=exp,
            ))
    except Exception as e:
        print(f"[!] hh.ru недоступен: {e} — пропускаю")
    return jobs


# ---------- 2. Habr Career (парсинг карточек, без pip-пакетов) ----------
GRADES = ("intern", "junior", "middle", "senior", "lead")
_CITY_SKIP = ("удален", "удалён", "офис", "гибрид", "полный", "день",
              "занятость", "стажировка", "можно", "опыт", "лет", "год")


def _num(s):
    try:
        return int(re.sub(r"\D", "", s or ""))
    except ValueError:
        return None


def _habr_salary(text):
    """Зарплаты Habr: 'от 75 000', 'от 170 000 до 195 000', 'примерно 238 000 - 325 000'."""
    m = re.search(r"от\s*([\d\s ]+?)\s*до\s*([\d\s ]+)", text)
    if m:
        return _num(m.group(1)), _num(m.group(2))
    m = re.search(r"от\s*([\d\s ]{4,})", text)
    if m:
        return _num(m.group(1)), None
    m = re.search(r"до\s*([\d\s ]{4,})", text)
    if m:
        return None, _num(m.group(1))
    m = re.search(r"(\d[\d\s ]*\d)\s*[—–-]\s*(\d[\d\s ]*\d)", text)
    if m:
        return _num(m.group(1)), _num(m.group(2))
    return None, None


def _habr_currency(text):
    if "€" in text or "EUR" in text:
        return "EUR"
    if "$" in text or "USD" in text:
        return "USD"
    return "RUR"


def fetch_habr(query="python", timeout=15, limit=25):
    """Реальные вакансии Habr Career: заголовок, компания, грейд, город, навыки, зарплата."""
    jobs = []
    try:
        q = urllib.parse.quote(query)
        page = fetch_url(f"https://career.habr.com/vacancies?q={q}&type=all", timeout)
        for block in re.split(r'<div class="vacancy-card[ "]', page)[1:]:
            if len(jobs) >= limit:
                break
            m_id = re.search(r'href="(/vacancies/\d+)"', block)
            m_title = re.search(r'class="vacancy-card__title-link"[^>]*>(.*?)</a>', block, re.S)
            if not m_id or not m_title:
                continue
            href = m_id.group(1)
            title = html.unescape(re.sub(r"\s+", " ", re.sub("<.*?>", "", m_title.group(1))).strip())
            if len(title) < 3:
                continue
            m_comp = re.search(r'vacancy-card__company">\s*<a[^>]*>(.*?)</a>', block, re.S)
            company = html.unescape(re.sub(r"\s+", " ", re.sub("<.*?>", "", m_comp.group(1))).strip()) if m_comp else ""
            m_date = re.search(r'<time[^>]*datetime="([^"]+)"', block)
            published = m_date.group(1)[:10] if m_date else ""
            chips = [html.unescape(re.sub(r"\s+", " ", c).strip())
                     for c in re.findall(r'chip-with-icon__text">([^<]+)</', block)]
            low = [c.lower() for c in chips]
            grade = next((c for c in chips if c.lower() in GRADES), "")
            remote = any("удален" in c or "удалён" in c for c in low)
            city = ""
            for c in chips:
                cl = c.lower()
                if cl in GRADES or any(k in cl for k in _CITY_SKIP):
                    continue
                city = c
                break
            skills = [html.unescape(s.strip()) for s in
                      re.findall(r'vacancy-card__skills-chip"[^>]*><div[^>]*>([^<]+)</', block)]
            sal_from, sal_to, currency = None, None, "RUR"
            i_sal = block.find('vacancy-card__salary">')
            if i_sal != -1:
                saltxt = re.sub(r"\s+", " ", re.sub("<.*?>", " ", block[i_sal:i_sal + 1500])).strip()
                sal_from, sal_to = _habr_salary(saltxt)
                currency = _habr_currency(saltxt)
            desc = ""
            if skills:
                desc = "Навыки: " + ", ".join(skills[:12]) + "."
            if grade:
                desc += f" Уровень: {grade}."
            jobs.append(norm_job(
                id=f"habr-{href.strip('/')}",
                title=title, company=company or "—",
                salary_from=sal_from, salary_to=sal_to, currency=currency,
                city=city, remote=remote, source="habr",
                url="https://career.habr.com" + href,
                desc=desc, published=published, experience=grade,
            ))
    except Exception as e:
        print(f"[!] Habr недоступен: {e} — пропускаю")
    return jobs


# ---------- 3. Telegram (ручной импорт) ----------
def load_telegram(path="telegram_vacancies.json"):
    jobs = []
    if not os.path.exists(path):
        return jobs
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for i, it in enumerate(raw if isinstance(raw, list) else raw.get("vacancies", [])):
            text = f"{it.get('title','')}\n{it.get('text','')}"
            jobs.append(norm_job(
                id=f"tg-{i}-{abs(hash(it.get('url',''))) % 10**6}",
                title=it.get("title", "Вакансия из Telegram"),
                company=it.get("channel", "telegram"),
                city=it.get("city", ""), remote=it.get("remote", True),
                source="telegram", url=it.get("url", ""),
                desc=it.get("text", "")[:600], published=it.get("date", ""),
            ))
    except Exception as e:
        print(f"[!] {path} битый: {e}")
    return jobs


def collect(query="python junior", tg_path="telegram_vacancies.json", per_page=30):
    """Только реальные источники: hh.ru API + Habr Career + Telegram-импорт. Без демо-данных."""
    short_q = (query.split() or ["python"])[0]
    tasks = [("hh", fetch_hh, (query,), {"per_page": per_page}),
             ("habr", fetch_habr, (short_q,), {})]
    if query.strip().lower() != short_q.lower():
        # вторым запросом добираем Habr шире: точное совпадение фразы тоже ищется
        tasks.append(("habr-full", fetch_habr, (query,), {}))
    tasks.append(("telegram", load_telegram, (tg_path,), {}))
    all_jobs = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fn, *a, **kw): name for name, fn, a, kw in tasks}
        for fut in as_completed(futs):
            try:
                all_jobs += fut.result()
            except Exception as e:
                print(f"[!] источник {futs[fut]} упал: {e}")
    if not all_jobs:
        print("[!] Все источники пустые: проверьте интернет и telegram_vacancies.json")
    # дедуп по url
    seen, uniq = set(), []
    for j in all_jobs:
        if j["url"] and j["url"] in seen:
            continue
        seen.add(j["url"])
        uniq.append(j)
    uniq.sort(key=lambda j: (-j["vibe_score"], j["title"]))
    return uniq


def filter_jobs(jobs, stack="", remote_only=False, junior_only=False, source="", text="", salary_min=0):
    text = (text or "").lower()
    out = []
    for j in jobs:
        if stack and stack.lower() not in [t.lower() for t in j["stack_tags"]]:
            # разрешаем подстрочный поиск: "next" найдёт "next.js"
            hay = " ".join(j["stack_tags"]).lower() + " " + (j["title"] + " " + j["desc"]).lower()
            if stack.lower() not in hay:
                continue
        if remote_only and not j["remote"]:
            continue
        if junior_only and not j["junior"]:
            continue
        if source and j["source"] != source:
            continue
        if salary_min:
            top = max(j.get("salary_from") or 0, j.get("salary_to") or 0)
            if top < salary_min:
                continue  # без зарплаты тоже отсеиваем при активном фильтре
        if text and text not in (j["title"] + " " + j["company"] + " " + j["desc"]).lower():
            continue
        out.append(j)
    return out


def main():
    ap = argparse.ArgumentParser(description="VibeJobs collector")
    ap.add_argument("--query", default="python junior")
    ap.add_argument("--out", default="jobs_cache.json")
    ap.add_argument("--per-page", type=int, default=30)
    ap.add_argument("--tg", default="telegram_vacancies.json")
    args = ap.parse_args()
    jobs = collect(args.query, tg_path=args.tg, per_page=args.per_page)
    payload = {"query": args.query, "ts": time.time(), "count": len(jobs), "jobs": jobs}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[+] собрано {len(jobs)} вакансий -> {args.out}")


if __name__ == "__main__":
    main()
