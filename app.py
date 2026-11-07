import json
import os
import secrets
import socket
import string
import time
from urllib.parse import urlparse

import psycopg2
import redis
from flask import Flask, abort, jsonify, redirect, render_template_string, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration (all from environment — no hardcoded secrets)
# ---------------------------------------------------------------------------
REDIS_HOST  = os.environ.get("REDIS_HOST", "redis")
DB_HOST     = os.environ.get("DB_HOST", "db")
DB_NAME     = os.environ.get("DB_NAME", "shortener")
DB_USER     = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
BASE_URL    = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
CACHE_TTL   = int(os.environ.get("CACHE_TTL", "300"))    # seconds
RATE_LIMIT  = int(os.environ.get("RATE_LIMIT", "30"))    # /shorten calls per minute per IP
RATE_WINDOW = 60

# ---------------------------------------------------------------------------
# Redis — retry on startup, but never crash the app if it's briefly down
# ---------------------------------------------------------------------------
cache = None
for _ in range(10):
    try:
        cache = redis.Redis(
            host=REDIS_HOST, port=6379, db=0,
            socket_connect_timeout=2, decode_responses=True,
        )
        cache.ping()
        break
    except redis.RedisError:
        cache = None
        time.sleep(2)

# ---------------------------------------------------------------------------
# PostgreSQL — lazy connect with reconnect-on-demand
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id         SERIAL PRIMARY KEY,
    code       VARCHAR(12) UNIQUE NOT NULL,
    url        TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS clicks (
    id         BIGSERIAL PRIMARY KEY,
    code       VARCHAR(12) NOT NULL,
    clicked_at TIMESTAMP DEFAULT NOW(),
    user_agent TEXT,
    referrer   TEXT
);
CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks (code);
"""

_conn = None


def get_db():
    """Return a live connection, reconnecting (and re-initialising schema) if needed."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
        )
        with _conn.cursor() as cur:
            cur.execute(SCHEMA)
        _conn.commit()
    return _conn


# Try to connect at startup, but tolerate a slow database (compose waits anyway)
for _ in range(10):
    try:
        get_db()
        break
    except psycopg2.Error:
        time.sleep(2)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ALPHABET = string.ascii_letters + string.digits
RESERVED = {"health", "shorten", "stats", "links", "api", "top", "favicon.ico", "static"}


def generate_code(length: int = 7) -> str:
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if code not in RESERVED:
            return code


def is_valid_url(url: str) -> bool:
    if not url or len(url) > 2048:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")


def rate_limited(key: str, limit: int, window: int = RATE_WINDOW):
    """Fixed-window counter in Redis. Fails open if Redis is unavailable."""
    if cache is None:
        return True
    try:
        bucket = f"rl:{key}"
        current = cache.incr(bucket)
        if current == 1:
            cache.expire(bucket, window)
        return current <= limit
    except redis.RedisError:
        return True


def get_link(code: str):
    """Cache-aside lookup. Returns (link_dict | None, served_from_cache: bool)."""
    key = f"link:{code}"

    if cache is not None:
        try:
            cached = cache.get(key)
            if cached:
                return json.loads(cached), True
        except redis.RedisError:
            pass

    with get_db().cursor() as cur:
        cur.execute("SELECT code, url, created_at FROM links WHERE code = %s", (code,))
        row = cur.fetchone()

    if row is None:
        return None, False

    link = {"code": row[0], "url": row[1], "created_at": row[2].isoformat()}
    if cache is not None:
        try:
            cache.set(key, json.dumps(link), ex=CACHE_TTL)
        except redis.RedisError:
            pass
    return link, False


def record_click(code: str) -> None:
    """Durable click log in Postgres + fast leaderboard update in Redis."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clicks (code, user_agent, referrer) VALUES (%s, %s, %s)",
                (
                    code,
                    (request.headers.get("User-Agent") or "")[:300],
                    (request.headers.get("Referer") or "")[:300],
                ),
            )
        conn.commit()
    except psycopg2.Error:
        try:
            get_db().rollback()
        except psycopg2.Error:
            pass

    if cache is not None:
        try:
            cache.zincrby("top_links", 1, code)
        except redis.RedisError:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM links")
        total_links = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM clicks")
        total_clicks = cur.fetchone()[0]
        cur.execute(
            """
            SELECT l.code, l.url, COUNT(c.id) AS clicks
            FROM links l
            LEFT JOIN clicks c ON c.code = l.code
            GROUP BY l.id, l.code, l.url
            ORDER BY clicks DESC, l.created_at DESC
            LIMIT 10
            """
        )
        top = [
            {"code": r[0], "url": r[1], "clicks": r[2]}
            for r in cur.fetchall()
        ]

    cached_entries = 0
    if cache is not None:
        try:
            cached_entries = sum(1 for _ in cache.scan_iter(match="link:*", count=100))
        except redis.RedisError:
            pass

    return render_template_string(
        DASHBOARD_HTML,
        base=BASE_URL,
        total_links=total_links,
        total_clicks=total_clicks,
        cached_entries=cached_entries,
        top=top,
    )


@app.route("/shorten", methods=["POST"])
def shorten():
    if not rate_limited(f"shorten:{client_ip()}", RATE_LIMIT):
        return jsonify({"error": "rate limit exceeded", "retry_after": RATE_WINDOW}), 429

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not is_valid_url(url):
        return jsonify({"error": "a valid http(s) URL is required"}), 400

    conn = get_db()
    with conn.cursor() as cur:
        # Idempotent: the same URL always returns the same code.
        cur.execute("SELECT code FROM links WHERE url = %s LIMIT 1", (url,))
        row = cur.fetchone()
        if row:
            code = row[0]
        else:
            code = None
            for _ in range(5):
                candidate = generate_code()
                try:
                    cur.execute("INSERT INTO links (code, url) VALUES (%s, %s)", (candidate, url))
                    conn.commit()
                    code = candidate
                    break
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()   # collision — try another code
            if code is None:
                return jsonify({"error": "could not allocate a code, try again"}), 500

    return jsonify({"code": code, "short_url": f"{BASE_URL}/{code}", "url": url}), 201


@app.route("/<code>")
def follow(code):
    link, from_cache = get_link(code)
    if link is None:
        abort(404)
    record_click(code)
    resp = redirect(link["url"], code=302)
    resp.headers["X-Cache"] = "HIT" if from_cache else "MISS"
    return resp


@app.route("/stats/<code>")
def stats(code):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT code, url, created_at FROM links WHERE code = %s", (code,))
        row = cur.fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        cur.execute(
            "SELECT COUNT(*), MAX(clicked_at) FROM clicks WHERE code = %s", (code,)
        )
        total, last = cur.fetchone()

    cached = False
    if cache is not None:
        try:
            cached = bool(cache.exists(f"link:{code}"))
        except redis.RedisError:
            pass

    return jsonify({
        "code": row[0],
        "url": row[1],
        "created_at": row[2].isoformat(),
        "total_clicks": total,
        "last_clicked": last.isoformat() if last else None,
        "cached": cached,
    })


@app.route("/links/<code>", methods=["DELETE"])
def delete_link(code):
    """Demonstrates cache invalidation — the hard part of caching."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM clicks WHERE code = %s", (code,))
        cur.execute("DELETE FROM links WHERE code = %s RETURNING code", (code,))
        deleted = cur.fetchone()
    conn.commit()

    if deleted is None:
        return jsonify({"error": "not found"}), 404

    if cache is not None:
        try:
            cache.delete(f"link:{code}")
            cache.zrem("top_links", code)
        except redis.RedisError:
            pass

    return jsonify({"deleted": code}), 200


@app.route("/api/top")
def api_top():
    """Leaderboard straight from the Redis sorted set (fast, derived data)."""
    if cache is None:
        return jsonify({"error": "cache unavailable"}), 503
    try:
        rows = cache.zrevrange("top_links", 0, 9, withscores=True)
    except redis.RedisError:
        return jsonify({"error": "cache unavailable"}), 503
    return jsonify([{"code": c, "clicks": int(s)} for c, s in rows])


@app.route("/health")
def health():
    cache_ok = False
    if cache is not None:
        try:
            cache.ping()
            cache_ok = True
        except Exception:
            pass

    db_ok = False
    try:
        with get_db().cursor() as cur:
            cur.execute("SELECT 1 FROM links LIMIT 1")
        db_ok = True
    except Exception:
        pass

    ok = cache_ok and db_ok
    return jsonify({
        "status": "ok" if ok else "degraded",
        "cache": cache_ok,
        "database": db_ok,
        "hostname": socket.gethostname(),
    }), (200 if ok else 503)


# ---------------------------------------------------------------------------
# Dashboard (inline template keeps the image to two files)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shortly — URL shortener</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
         background:#0f172a; color:#e2e8f0; margin:0; padding:3rem 1.25rem; }
  main { max-width: 760px; margin: 0 auto; }
  h1 { font-size:1.7rem; margin:0 0 .25rem; letter-spacing:-.02em; }
  .sub { color:#94a3b8; margin:0 0 2rem; font-size:.95rem; }
  .stats { display:flex; gap:1rem; margin-bottom:2rem; flex-wrap:wrap; }
  .card { flex:1 1 140px; background:#1e293b; border:1px solid #334155;
          border-radius:10px; padding:1rem; }
  .card .n { font-size:1.7rem; font-weight:700; }
  .card .l { color:#94a3b8; font-size:.72rem; text-transform:uppercase;
             letter-spacing:.06em; margin-top:.15rem; }
  form { display:flex; gap:.5rem; margin-bottom:.75rem; flex-wrap:wrap; }
  input[type=url] { flex:1 1 320px; padding:.8rem 1rem; border-radius:8px;
                    border:1px solid #334155; background:#1e293b; color:#e2e8f0;
                    font-size:.95rem; }
  input[type=url]:focus { outline:2px solid #6366f1; border-color:transparent; }
  button { padding:.8rem 1.3rem; border:0; border-radius:8px; background:#6366f1;
           color:#fff; font-weight:600; cursor:pointer; font-size:.95rem; }
  button:hover { background:#4f46e5; }
  #out { min-height:1.4rem; color:#a5b4fc; font-size:.9rem; }
  h2 { font-size:1.05rem; margin:2rem 0 .75rem; }
  table { width:100%; border-collapse:collapse; font-size:.9rem; }
  th, td { text-align:left; padding:.6rem .4rem; border-bottom:1px solid #1e293b; }
  th { color:#94a3b8; font-weight:600; font-size:.78rem; text-transform:uppercase;
       letter-spacing:.05em; }
  a { color:#818cf8; text-decoration:none; word-break:break-all; }
  a:hover { text-decoration:underline; }
  code { background:#1e293b; padding:.1rem .35rem; border-radius:4px; }
</style>
</head>
<body>
<main>
  <h1>Shortly</h1>
  <p class="sub">A Dockerized URL shortener — Flask · Redis · PostgreSQL</p>

  <div class="stats">
    <div class="card"><div class="n">{{ total_links }}</div><div class="l">Links</div></div>
    <div class="card"><div class="n">{{ total_clicks }}</div><div class="l">Clicks</div></div>
    <div class="card"><div class="n">{{ cached_entries }}</div><div class="l">Cached entries</div></div>
  </div>

  <form id="f">
    <input type="url" id="url" placeholder="https://example.com/a/very/long/path" required>
    <button type="submit">Shorten</button>
  </form>
  <p id="out"></p>

  <h2>Top links</h2>
  <table>
    <thead><tr><th>Short</th><th>Destination</th><th>Clicks</th></tr></thead>
    <tbody>
      {% for l in top %}
      <tr>
        <td><a href="{{ base }}/{{ l.code }}">{{ base }}/{{ l.code }}</a></td>
        <td><a href="{{ l.url }}">{{ l.url|truncate(58) }}</a></td>
        <td>{{ l.clicks }}</td>
      </tr>
      {% else %}
      <tr><td colspan="3" class="sub">No links yet — shorten one above.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</main>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const out = document.getElementById('out');
  const res = await fetch('/shorten', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: document.getElementById('url').value})
  });
  const data = await res.json();
  out.textContent = res.ok ? 'Created → ' + data.short_url : 'Error: ' + data.error;
  if (res.ok) setTimeout(() => location.reload(), 900);
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
