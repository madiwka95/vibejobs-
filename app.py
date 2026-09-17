#!/usr/bin/env python3
"""VibeJobs web — минимальное веб-приложение без pip-пакетов (только stdlib).

Запуск:
  python app.py
  python app.py --port 8000 --query "python junior"

Откройте: http://localhost:8000

Роуты:
  GET /                      -> index.html
  GET /api/jobs?...          -> JSON {count, jobs}
      params: q, stack, remote(1), junior(1), source, sort(vibe|fresh)
  GET /api/stacks            -> JSON {stacks: [...]}
  GET /api/refresh           -> пересобрать кэш принудительно
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)  # для embeddable-портабл Python

import collector

CACHE_FILE = os.path.join(BASE_DIR, "jobs_cache.json")
TG_FILE = os.path.join(BASE_DIR, "telegram_vacancies.json")
DEFAULT_QUERY = "python junior"

_cache = {"ts": 0, "query": "", "jobs": []}


def get_jobs(query, force=False):
    """Кэш 30 минут, иначе пересбор."""
    now = time.time()
    if (not force and _cache["jobs"]
            and _cache["query"] == query
            and now - _cache["ts"] < collector.CACHE_TTL_SEC):
        return _cache["jobs"]
    # пробуем дисковый кэш
    if not force and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as fh:
                disk = json.load(fh)
            if (disk.get("query") == query
                    and now - disk.get("ts", 0) < collector.CACHE_TTL_SEC
                    and disk.get("jobs")):
                _cache.update(ts=disk["ts"], query=query, jobs=disk["jobs"])
                return _cache["jobs"]
        except (OSError, json.JSONDecodeError):
            pass
    jobs = collector.collect(query, tg_path=TG_FILE)
    _cache.update(ts=now, query=query, jobs=jobs)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"query": query, "ts": now, "count": len(jobs), "jobs": jobs},
                      fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return jobs


class Handler(BaseHTTPRequestHandler):
    server_version = "VibeJobs/0.1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        one = lambda k, d="": qs.get(k, [d])[0]

        if parsed.path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html не найден", "text/plain; charset=utf-8")
            return

        if parsed.path == "/api/stacks":
            self._send(200, json.dumps({"stacks": sorted(collector.STACK_KEYWORDS)},
                                       ensure_ascii=False))
            return

        if parsed.path == "/api/refresh":
            q = one("q", self.server.default_query)
            jobs = get_jobs(q, force=True)
            self._send(200, json.dumps({"ok": True, "count": len(jobs)}, ensure_ascii=False))
            return

        if parsed.path == "/api/jobs":
            q = one("q", self.server.default_query)
            jobs = get_jobs(q or self.server.default_query)
            jobs = collector.filter_jobs(
                jobs,
                stack=one("stack", ""),
                remote_only=one("remote", "") == "1",
                junior_only=one("junior", "") == "1",
                source=one("source", ""),
                text=one("text", ""),
            )
            if one("sort", "vibe") == "fresh":
                jobs = sorted(jobs, key=lambda j: j.get("published", ""), reverse=True)
            else:
                jobs = sorted(jobs, key=lambda j: -j.get("vibe_score", 0))
            self._send(200, json.dumps({"count": len(jobs), "query": q, "jobs": jobs[:200]},
                                       ensure_ascii=False))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass  # тихо, без спама в консоль


def _env_port():
    try:
        return int(os.environ.get("PORT", "8000"))
    except ValueError:
        return 8000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                    help="0.0.0.0 — для хостинга (Render/Railway)")
    ap.add_argument("--port", type=int, default=_env_port(),
                    help="или env PORT (так дают порт хостинги)")
    ap.add_argument("--query", default=DEFAULT_QUERY)
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.default_query = args.query
    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"[*] VibeJobs: http://{shown_host}:{args.port}")
    print(f"[*] Базовый запрос: {args.query!r} (меняется параметром ?q= в API)")
    print("[*] Остановка: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Пока!")


if __name__ == "__main__":
    main()
