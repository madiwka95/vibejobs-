#!/usr/bin/env python3
"""VibeJobs bake — вшить слепок РЕАЛЬНЫХ вакансий в index.html.

После этого страница показывает вакансии даже без запущенного сервера
(двойной клик по index.html): фильтры работают локально по слепку,
а при запущенном сервере подтягиваются свежие данные.

Запуск:
  python bake.py
  python bake.py --queries "python junior" "javascript junior" --limit 60
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collector

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE_DIR, "index.html")
TG_FILE = os.path.join(BASE_DIR, "telegram_vacancies.json")
MARKER = "const SNAPSHOT = null; // __BAKED__"


def main():
    ap = argparse.ArgumentParser(description="Вшить слепок реальных вакансий в index.html")
    ap.add_argument("--queries", nargs="+",
                    default=["python junior", "javascript junior", "telegram bot junior"])
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    all_jobs, seen = [], set()
    for q in args.queries:
        print(f"[*] запрос: {q!r}")
        for j in collector.collect(q, tg_path=TG_FILE):
            if j["url"] and j["url"] in seen:
                continue
            seen.add(j["url"])
            all_jobs.append(j)
    all_jobs.sort(key=lambda j: (-j["vibe_score"], j["title"]))
    all_jobs = all_jobs[:args.limit]

    demo_left = [j for j in all_jobs
                 if j["id"].startswith("demo-") or "example.com" in j["url"]]
    if demo_left:
        print(f"[!] ОТКАЗ: в выборке {len(demo_left)} демо-записей, слепок не вшиваю")
        return 2

    date = datetime.date.today().isoformat()
    payload = json.dumps({"date": date, "count": len(all_jobs), "jobs": all_jobs},
                         ensure_ascii=False)
    payload = payload.replace("</", "<\\/")  # чтобы не порвать <script>
    with open(INDEX, encoding="utf-8") as fh:
        html = fh.read()
    if MARKER not in html and "// __BAKED__" not in html:
        print("[!] в index.html нет места под слепок (ищите __BAKED__)")
        return 2
    # заменяем любой предыдущий слепок на свежий
    start = html.find("const SNAPSHOT =")
    end = html.find("// __BAKED__")
    new_const = f"const SNAPSHOT = {payload}; // __BAKED__ {date}"
    html = html[:start] + new_const + html[end + len("// __BAKED__"):]
    with open(INDEX, "w", encoding="utf-8") as fh:
        fh.write(html)
    srcs = {}
    for j in all_jobs:
        srcs[j["source"]] = srcs.get(j["source"], 0) + 1
    print(f"[+] вшито {len(all_jobs)} реальных вакансий от {date}: {srcs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
