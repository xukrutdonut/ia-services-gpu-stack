"""
lm-chat — lightweight web UI for LM Studio with MCP tool integration.
Serves a PWA-ready single-page app, proxies to LM Studio, persists chats in SQLite.
"""

import base64, binascii, gzip, hashlib, hmac, html as html_mod, io, json, logging, math, os, re, secrets, signal, sqlite3, struct, subprocess, sys, threading, time, uuid, urllib.request, urllib.error
import http.cookies
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from logging.handlers import RotatingFileHandler
from qr import generate_qr_svg

VERSION = "0.5.9"
LMSTUDIO = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")
LMSTUDIO_TOKEN = os.environ.get("LMSTUDIO_TOKEN", "")
OPENVINO_URL = os.environ.get("OPENVINO_URL", "")  # e.g. http://localhost:8006 — OpenVINO GenAI server (OpenAI-compatible API)
OPENVINO_TOKEN = os.environ.get("OPENVINO_TOKEN", "")
PORT = int(os.environ.get("PORT", "3001"))
DB_PATH = os.environ.get("LM_CHAT_DB", os.path.join(os.path.dirname(__file__), "chats.db"))
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
AUTH_ENABLED = os.environ.get("LM_CHAT_AUTH", "true").lower() not in ("0", "false", "no")
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
def _get_or_create_signing_key():
    """Get signing key from env, or persist a generated one to .lm_chat_secret."""
    env_key = os.environ.get("LM_CHAT_SECRET")
    if env_key:
        return env_key
    secret_file = os.path.join(os.path.dirname(DB_PATH) or os.path.dirname(os.path.abspath(__file__)), ".lm_chat_secret")
    try:
        with open(secret_file) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        try:
            fd = os.open(secret_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, key.encode())
            os.close(fd)
        except FileExistsError:
            with open(secret_file) as f:
                return f.read().strip()
        return key

TOTP_SIGNING_KEY = _get_or_create_signing_key()
MAX_BODY_SIZE = 50 * 1024 * 1024  # 50 MB (base64 images can be large)

SESSION_EXPIRY = 30 * 86400        # 30 days
RATE_LIMIT_WINDOW = 900            # 15 minutes
RATE_LIMIT_MAX_ATTEMPTS = 5
PARTIAL_TOKEN_EXPIRY = 300         # 5 minutes
TOTP_SETUP_EXPIRY = 600            # 10 minutes
COMPACT_MIN_TURNS = 10
COMPACT_MAX_CHARS = 6000
SEARCH_MAX_RESULTS = 5000
TITLE_MAX_LENGTH = 500
MODEL_MAX_LENGTH = 200

# --- First-visitor bootstrap protection ---
# When the server starts with no users in the DB, /api/auth/setup is reachable
# by anyone — first request through wins admin.  Operators exposing lm-chat
# on a public URL set this env so the first POST must include a matching
# ``setup_token`` field; without it the endpoint returns 401.
_SETUP_TOKEN = os.environ.get("LM_CHAT_SETUP_TOKEN", "").strip()

# --- Session policy ---
# **Single-session is the default** as of 0.5.7 (ASVS V3.3.3 — session re-id
# Default is multi-device: every new tab on a personal-use deployment
# otherwise triggers a re-login (the new tab's login rotates the cookie,
# invalidating the old tab's session).  Operators with a stricter threat
# model (public exposure, shared devices) opt in with
# ``LM_CHAT_SINGLE_SESSION=true``.  The 0.5.7 default-ON flip was too
# aggressive for the dominant personal-use case and shipped without an
# operator opt-in — reverted in 0.5.9.
_SINGLE_SESSION = os.environ.get("LM_CHAT_SINGLE_SESSION", "").strip().lower() in ("1", "true", "on", "yes")

# --- Reverse-proxy trust ---
# When set, the server trusts ``X-Forwarded-For`` / ``X-Real-IP`` /
# ``X-Forwarded-Proto`` headers from the upstream proxy.  Leave UNSET (the
# default) when lm-chat is exposed to the internet directly — otherwise any
# client can spoof their IP to bypass per-IP rate limits or forge
# ``X-Forwarded-Proto: https`` to harvest a ``Secure``-gated cookie over
# plain HTTP.  Set this to ``true`` only when there is an actual trusted
# reverse proxy (nginx, Caddy, Cloudflare, fly.io, etc.) in front.
_TRUSTED_PROXY = os.environ.get("LM_CHAT_TRUSTED_PROXY", "").strip().lower() in ("1", "true", "on", "yes")

# --- HSTS ---
# Opt-in.  Empty / "false" / "off" → no header.  "true"/"1"/"on" → standard
# directive.  "preload" → adds the preload token for HSTS preload list inclusion.
# HSTS is only meaningful over HTTPS; we only emit it when LM_CHAT_HTTPS=1 or
# the request arrived through a reverse proxy with X-Forwarded-Proto: https.
_HSTS_MODE = os.environ.get("LM_CHAT_HSTS", "").strip().lower()
try:
    _HSTS_MAX_AGE = max(0, int(os.environ.get("LM_CHAT_HSTS_MAX_AGE", "63072000")))
except (TypeError, ValueError):
    _HSTS_MAX_AGE = 63072000  # 2 years — RFC 6797 / OWASP recommendation

# --- Structured Logging with Rotation ---

LOG_DIR = os.environ.get("LM_CHAT_LOGS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
LOG_BACKUP_COUNT = 5               # keep 5 rotated files (25 MB total max)

def _setup_logger():
    """Configure rotating file logger + optional console output.
    Falls back to console-only if the log directory is not writable (e.g. read-only container)."""
    logger = logging.getLogger("lm-chat")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    # Rotating file handler — active when log dir is writable
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(LOG_DIR, "lm-chat.log"),
            maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)
    except OSError:
        pass  # read-only filesystem — console-only logging
    # Console handler — only INFO+ unless debug mode is on
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    ch.setLevel(logging.DEBUG if os.environ.get("LM_CHAT_DEBUG") else logging.INFO)
    ch.name = "console"
    logger.addHandler(ch)
    return logger

log = _setup_logger()

def set_debug_mode(enabled):
    """Toggle debug verbosity on the console handler at runtime."""
    for h in log.handlers:
        if getattr(h, "name", None) == "console":
            h.setLevel(logging.DEBUG if enabled else logging.INFO)
    log.info(f"Debug mode: {'ON' if enabled else 'OFF'}")


def _token_overlap(a, b):
    """Simple Jaccard token overlap ratio between two strings."""
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


_DEFAULT_USER_MIGRATION_TABLES = (
    "chats", "messages", "users", "sessions", "embeddings",
    "user_settings", "rate_limits", "user_insights", "shared_chats",
    "message_feedback", "pins",
)


def bootstrap_admin_if_needed(db, admin_user=None, admin_pass=None, announce=True):
    """If no non-default users exist, create one from env / passed args.

    Same logic used to live inlined in ``__main__``; lifted here so the test
    suite can call it directly without re-implementing the credential read
    + default-user data migration.

    Args:
        db: active sqlite connection.
        admin_user: override LM_CHAT_ADMIN_USER (defaults to "admin").
        admin_pass: override LM_CHAT_ADMIN_PASS.  If empty AND no env value,
                    a random password is generated and printed to stderr.
        announce: when True (production), print the generated password to
                  stderr so the operator can read it once.  Tests pass
                  ``announce=False`` to keep stderr quiet.

    Returns: ``(admin_id, was_created)``.  When ``was_created`` is False the
    DB already had at least one non-default user and nothing was changed.
    """
    count = db.execute(
        "SELECT COUNT(*) FROM users WHERE username != 'default'"
    ).fetchone()[0]
    if count > 0:
        return None, False

    admin_user = admin_user or os.environ.get("LM_CHAT_ADMIN_USER", "admin")
    admin_pass = admin_pass or os.environ.get("LM_CHAT_ADMIN_PASS", "")
    generated = False
    if not admin_pass:
        admin_pass = secrets.token_urlsafe(12)
        generated = True

    if announce and generated:
        print(f"\n{'='*50}", file=sys.stderr)
        print("  Admin account created", file=sys.stderr)
        print(f"  Username: {admin_user}", file=sys.stderr)
        print(f"  Password: {admin_pass}", file=sys.stderr)
        print("  (set LM_CHAT_ADMIN_PASS to use your own)", file=sys.stderr)
        print(f"{'='*50}\n", file=sys.stderr)
    elif announce:
        log.info(f"Admin account created: {admin_user}")

    admin_id = uuid.uuid4().hex
    pw_hash, salt = hash_password(admin_pass)
    db.execute(
        "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (admin_id, admin_user, pw_hash, salt, admin_user, 1, time.time()),
    )

    # Promote any rows owned by the auth-disabled "default" user to the new
    # admin so they don't get orphaned when auth flips on.  Loop tolerates
    # tables added in future migrations as long as they keep user_id.
    migrated = 0
    for (tname,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        if tname not in _DEFAULT_USER_MIGRATION_TABLES:
            continue
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({tname})").fetchall()]
        if "user_id" in cols:
            r = db.execute(
                f"UPDATE {tname} SET user_id=? WHERE user_id='default'",
                (admin_id,),
            )
            migrated += r.rowcount
    if announce and migrated:
        log.info(f"Migrated {migrated} rows from default user to {admin_user}")
    db.commit()
    return admin_id, True


def _apply_pragmas(db: sqlite3.Connection) -> None:
    """Connection-level tuning.  Idempotent; safe to call on every open."""
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-64000")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA journal_size_limit=67108864")


# Tables, indexes, FTS5 virtual tables, and triggers.  Every statement is
# IF NOT EXISTS so first-run and upgrade paths share the same code.  Listed
# in (roughly) the order they were added to lm-chat — newest tables sink
# to the bottom so the diff stays small as the schema grows.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # Core conversation tables.  Columns added across 0.2.x — 0.3.x are
    # inlined here so fresh installs get the current shape in one go; the
    # ``_MIGRATIONS`` list below covers upgrades from older DBs.
    """CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT 'New chat',
        model TEXT,
        response_id TEXT,
        updated_at REAL NOT NULL,
        user_id TEXT,
        summary TEXT,
        summary_up_to INTEGER,
        pinned INTEGER DEFAULT 0,
        folder TEXT DEFAULT '',
        settings TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT,
        name TEXT,
        args TEXT,
        output TEXT,
        created_at REAL NOT NULL,
        token_count INTEGER,
        status TEXT
    )""",
    # Auth — totp_* columns added in 0.1.3 but inlined for fresh installs.
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        display_name TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        totp_secret TEXT,
        totp_enabled INTEGER DEFAULT 0,
        last_totp_counter INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""",
    # Memory / embeddings
    """CREATE TABLE IF NOT EXISTS embeddings (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        vector BLOB NOT NULL,
        model_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY(user_id, key)
    )""",
    """CREATE TABLE IF NOT EXISTS rate_limits (
        ip TEXT PRIMARY KEY,
        attempts INTEGER DEFAULT 0,
        first_attempt REAL
    )""",
    """CREATE TABLE IF NOT EXISTS user_insights (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'context',
        origin_chat_id TEXT,
        weight REAL DEFAULT 1.0,
        created_at REAL NOT NULL,
        last_used REAL NOT NULL,
        use_count INTEGER DEFAULT 0,
        state TEXT DEFAULT 'active',
        replaced_by TEXT,
        ups REAL DEFAULT 0,
        downs REAL DEFAULT 0,
        last_feedback_at REAL,
        pinned INTEGER DEFAULT 0,
        content_hash TEXT,
        FOREIGN KEY (origin_chat_id) REFERENCES chats(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS insight_activations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        insight_id  TEXT    NOT NULL REFERENCES user_insights(id) ON DELETE CASCADE,
        created_at  REAL    NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS message_feedback (
        message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        user_id     TEXT    NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
        rating      INTEGER NOT NULL CHECK(rating IN (-1, 1)),
        created_at  REAL    NOT NULL,
        PRIMARY KEY (message_id, user_id)
    )""",
    # Pins + FTS index — 0.3.0
    """CREATE TABLE IF NOT EXISTS pins (
        id          TEXT    PRIMARY KEY,
        user_id     TEXT    NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
        message_id  INTEGER          REFERENCES messages(id)  ON DELETE SET NULL,
        chat_id     TEXT             REFERENCES chats(id)     ON DELETE SET NULL,
        chat_title  TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        pin_title   TEXT,
        pinned_at   REAL    NOT NULL
    )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS pins_fts USING fts5(
        content,
        chat_title,
        pin_title,
        content='pins',
        content_rowid='rowid'
    )""",
    # Sharing — 0.1.3
    """CREATE TABLE IF NOT EXISTS shared_chats (
        share_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        messages TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    # Per-model parameter blacklist — survives restarts so a fresh
    # container doesn't re-burn one 400-roundtrip per (model, param) on
    # every deploy.  Seeded proactively from /api/v1/models capabilities
    # where available, and additively from runtime 400 retries.
    """CREATE TABLE IF NOT EXISTS model_param_blacklist (
        model_key TEXT NOT NULL,
        param     TEXT NOT NULL,
        source    TEXT NOT NULL,
        added_at  REAL NOT NULL,
        PRIMARY KEY (model_key, param)
    )""",
    # Indexes — keep adjacent to the table they cover so the diff stays
    # easy to read when adding new columns.
    "CREATE INDEX IF NOT EXISTS idx_messages_chat_id    ON messages(chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_chat_role  ON messages(chat_id, role)",
    "CREATE INDEX IF NOT EXISTS idx_chats_user_id       ON chats(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_embeddings_message_id ON embeddings(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_insights_user_state ON user_insights(user_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_insights_user_cat   ON user_insights(user_id, category)",
    "CREATE INDEX IF NOT EXISTS idx_activations_message ON insight_activations(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_activations_insight ON insight_activations(insight_id)",
    "CREATE INDEX IF NOT EXISTS idx_pins_user           ON pins(user_id, pinned_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pins_chat           ON pins(user_id, chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_pins_message        ON pins(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_shared_chats_chat_id ON shared_chats(chat_id)",
    # FTS5 sync triggers — keep last so the pins table + pins_fts virtual
    # table they reference already exist.
    """CREATE TRIGGER IF NOT EXISTS pins_ai AFTER INSERT ON pins BEGIN
        INSERT INTO pins_fts(rowid, content, chat_title, pin_title)
        VALUES (new.rowid, new.content, new.chat_title, new.pin_title);
    END""",
    """CREATE TRIGGER IF NOT EXISTS pins_ad AFTER DELETE ON pins BEGIN
        INSERT INTO pins_fts(pins_fts, rowid, content, chat_title, pin_title)
        VALUES ('delete', old.rowid, old.content, old.chat_title, old.pin_title);
    END""",
    """CREATE TRIGGER IF NOT EXISTS pins_au AFTER UPDATE ON pins BEGIN
        INSERT INTO pins_fts(pins_fts, rowid, content, chat_title, pin_title)
        VALUES ('delete', old.rowid, old.content, old.chat_title, old.pin_title);
        INSERT INTO pins_fts(rowid, content, chat_title, pin_title)
        VALUES (new.rowid, new.content, new.chat_title, new.pin_title);
    END""",
)


# Column-level migrations for upgrades from pre-0.2.x DBs.  Each entry is
# ``(table, column, definition)``.  ``ALTER TABLE ADD COLUMN`` is a no-op
# (raises ``OperationalError`` and we swallow) when the column already
# exists, so on fresh installs every row here is dead weight but cheap.
# When you add a new column to a long-lived table, append it here AND to
# the CREATE TABLE so fresh installs get the column too.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("chats",         "user_id",           "TEXT"),
    ("chats",         "summary",           "TEXT"),
    ("chats",         "summary_up_to",     "INTEGER"),
    ("users",         "totp_secret",       "TEXT"),
    ("users",         "totp_enabled",      "INTEGER DEFAULT 0"),
    ("users",         "last_totp_counter", "INTEGER DEFAULT 0"),
    ("messages",      "token_count",       "INTEGER"),
    ("chats",         "pinned",            "INTEGER DEFAULT 0"),
    ("chats",         "folder",            "TEXT DEFAULT ''"),
    ("chats",         "settings",          "TEXT"),
    ("user_insights", "ups",               "REAL DEFAULT 0"),
    ("user_insights", "downs",             "REAL DEFAULT 0"),
    ("user_insights", "last_feedback_at",  "REAL"),
    # Lifecycle marker for assistant rows.
    # ENUM (extend additively, never repurpose a value):
    #   NULL          — completed (healthy turn, default)
    #   "interrupted" — stream cut before chat.end; SPA renders retry UI
    # Convo-context builders MUST filter out anything other than NULL with
    # ``(status IS NULL OR status != 'interrupted')`` — interrupted rows
    # are SPA UI markers, not model-context content.
    ("messages",      "status",            "TEXT"),
    # Memory system Tier-1 columns (spec v3, 2026-05-16):
    # - embeddings.model_id: which embedding model produced this vector.
    #   Mixing vectors from different model versions in cosine retrieval
    #   is mathematically meaningless — different spaces.  Retrieval
    #   filters by current model; legacy NULL rows excluded until user
    #   opts into re-index.
    ("embeddings",    "model_id",          "TEXT"),
    # - user_insights.pinned: must-remember flag.  Pinned insights inject
    #   unconditionally up to _PINNED_INSIGHTS_LIMIT (=5).
    ("user_insights", "pinned",            "INTEGER DEFAULT 0"),
    # - user_insights.content_hash: MD5 of normalised content.
    #   UNIQUE (user_id, content_hash) prevents byte-identical re-distills.
    #   Backfill + unique-index creation happen post-migration in init_db.
    ("user_insights", "content_hash",      "TEXT"),
)


def _create_schema(db: sqlite3.Connection) -> None:
    """Run every CREATE statement in ``_SCHEMA_STATEMENTS``.  Idempotent."""
    for stmt in _SCHEMA_STATEMENTS:
        db.execute(stmt)


def _run_migrations(db: sqlite3.Connection) -> None:
    """Apply column-level migrations.  Silently skips columns that already
    exist (the ``OperationalError`` swallow) so fresh installs are no-ops."""
    for table, col, typedef in _MIGRATIONS:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass


def _load_param_blacklist(db: sqlite3.Connection) -> dict:
    """Return ``{model_key: set[param]}`` from the persisted blacklist.

    Called once at startup to seed ``Handler._unsupported_params`` so a
    fresh container doesn't re-burn 400 round-trips against LM Studio
    for every (model, param) pair it had already learned about before
    the restart.
    """
    out: dict = {}
    try:
        for model_key, param in db.execute(
            "SELECT model_key, param FROM model_param_blacklist"
        ):
            out.setdefault(model_key, set()).add(param)
    except sqlite3.OperationalError:
        # Table missing on a pre-migration upgrade path — _create_schema
        # adds it idempotently on the same init_db() call so this never
        # actually fires at runtime; the guard is defensive.
        pass
    return out


def _persist_param_blacklist(model_key: str, param: str, source: str) -> None:
    """Idempotent INSERT into ``model_param_blacklist``.

    Called from the retry-on-400 block once we've seen LM Studio reject
    a (model, param) pair, and from the proactive seed in ``_get_models``
    when ``capabilities.reasoning.allowed_options`` excludes the user's
    setting.  ``source`` is informational (``"reactive_400"`` or
    ``"capability_gate"``) — debugging only, not used for behaviour.
    """
    if not model_key or not param:
        return
    try:
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO model_param_blacklist "
            "(model_key, param, source, added_at) VALUES (?,?,?,?)",
            (model_key, param, source, time.time()),
        )
        db.commit()
    except Exception as e:
        log.warning(f"could not persist param blacklist {model_key!r}/{param!r}: {e}")


def _content_hash_text(content: str) -> str:
    """Normalize whitespace + case, MD5 hash.  Matches Mem0's dedup
    precedent.  Stable across re-runs.  Lives at module scope so the
    backfill helper (also module scope) can use it without a Handler
    instance.
    """
    norm = " ".join((content or "").lower().split())
    return hashlib.md5(norm.encode("utf-8"), usedforsecurity=False).hexdigest()


def _backfill_content_hashes(db: sqlite3.Connection) -> None:
    """One-time backfill of user_insights.content_hash for legacy rows.

    Runs in a single transaction — partial state on any exception is
    rolled back so a fresh `init_db` retry sees the pre-backfill state.
    Collapses any legacy duplicates by keeping the older row (lower
    `created_at`) and deleting the rest.

    Called from `init_db` AFTER `_run_migrations` (so `content_hash`
    column exists) and BEFORE creating the UNIQUE index (so that
    duplicates can be resolved without failing the index build).
    """
    rows = db.execute(
        "SELECT id, user_id, content, created_at FROM user_insights "
        "WHERE content_hash IS NULL"
    ).fetchall()
    if not rows:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        # (user_id, hash) -> (winning_row_id, winning_created_at)
        seen: dict = {}
        deletes: set = set()
        updates: list = []  # (hash, row_id)
        for row_id, user_id, content, created_at in rows:
            h = _content_hash_text(content or "")
            key = (user_id, h)
            if key in seen:
                existing_id, existing_created = seen[key]
                if created_at > existing_created:
                    deletes.add(row_id)
                else:
                    deletes.add(existing_id)
                    seen[key] = (row_id, created_at)
                    updates.append((h, row_id))
            else:
                seen[key] = (row_id, created_at)
                updates.append((h, row_id))
        for d in deletes:
            db.execute("DELETE FROM user_insights WHERE id = ?", (d,))
        for h, row_id in updates:
            if row_id in deletes:
                continue
            db.execute(
                "UPDATE user_insights SET content_hash = ? WHERE id = ?",
                (h, row_id),
            )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise


def _seed_default_user(db: sqlite3.Connection) -> None:
    """Ensure the ``default`` user row exists.

    Required for FK integrity in auth-disabled mode, where every chat/pin
    row is owned by ``user_id = 'default'``.  Empty hash/salt make the
    account unusable as a login target — auth-disabled mode bypasses login
    anyway.
    """
    db.execute(
        """INSERT OR IGNORE INTO users (id, username, password_hash, salt, display_name, is_admin, created_at)
           VALUES ('default', 'default', '', '', 'User', 1, ?)""",
        (time.time(),),
    )


def init_db():
    """Run once at startup: pragmas → schema → migrations → seed →
    backfill → late indexes."""
    db = sqlite3.connect(DB_PATH)
    try:
        _apply_pragmas(db)
        _create_schema(db)
        _run_migrations(db)
        _seed_default_user(db)
        # Backfill content_hash for any legacy user_insights rows BEFORE
        # the UNIQUE index is created — otherwise the index build would
        # fail on legacy duplicates.
        _backfill_content_hashes(db)
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_insights_user_content_hash "
            "ON user_insights(user_id, content_hash)"
        )
        db.commit()
    finally:
        db.close()


_thread_local = threading.local()

def get_db():
    """Get a database connection, cached per thread. Call init_db() first at startup."""
    db = getattr(_thread_local, 'db', None)
    if db is not None:
        try:
            db.execute("SELECT 1")
            return db
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-64000")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA foreign_keys=ON")
    db.create_function("ln", 1, math.log)
    db.create_function("exp", 1, math.exp)
    _thread_local.db = db
    return db


# --- Password helpers ---

# --- Password policy ---
#
# Upper bound: scrypt processes the full byte string, so a 50MB body would
# burn ~10s of CPU on M3-class silicon (the only request-side limit before
# this was MAX_BODY_SIZE).  Cap at 256 chars — beyond reasonable human use
# and password-manager output, well below the DoS threshold.
PASSWORD_MAX_LENGTH = 256
PASSWORD_MIN_LENGTH = 8

# scrypt cost parameters — OWASP password storage cheat sheet, 2024 update.
# Pre-0.5.0 used (n=16384, r=8, p=1) which is the 2017 floor; modern silicon
# pushes that into "tractable for offline cracking" territory.  131072 is the
# current OWASP minimum and costs ~50–80 ms per call on M-class hardware.
#
# The stored hash carries its own (n,r,p) so older hashes verify against the
# parameters they were originally created with — no forced reset needed when
# operators upgrade.  On the first successful login of any user whose stored
# cost is below ``SCRYPT_PARAMS_CURRENT``, the hash is silently re-computed.
SCRYPT_PARAMS_CURRENT = (
    int(os.environ.get("LM_CHAT_SCRYPT_N", "131072")),
    int(os.environ.get("LM_CHAT_SCRYPT_R", "8")),
    int(os.environ.get("LM_CHAT_SCRYPT_P", "1")),
)
# Format string for new hashes; legacy bare-hex hashes are detected by the
# absence of this prefix and decoded with the 2017 defaults.
_SCRYPT_PREFIX = "scrypt$"
_SCRYPT_LEGACY_PARAMS = (16384, 8, 1)


def _scrypt_hex(password: str, salt: bytes, n: int, r: int, p: int) -> str:
    # Memory footprint of scrypt is ~128 * N * r bytes; OpenSSL's default
    # ``maxmem`` is 32 MB, which trips on the OWASP 2024 floor (N=131072,
    # r=8 → ~128 MB).  Bump the ceiling with a comfortable safety margin so
    # operators can tune cost upward without surprise ``ValueError``s.
    needed = 128 * n * r
    maxmem = max(needed * 2, 64 * 1024 * 1024)
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=64, maxmem=maxmem,
    ).hex()


def _parse_password_hash(stored: str) -> tuple[int, int, int, str]:
    """Decode a stored hash string into (n, r, p, raw_hex).

    Accepts both the new composite form (``scrypt$n=N$r=R$p=P$HEX``) and
    bare-hex legacy hashes from pre-0.5.0 installs, which are assumed to use
    the original parameters in ``_SCRYPT_LEGACY_PARAMS``.

    Raises ``ValueError`` if the prefix is present but malformed.
    """
    if not stored.startswith(_SCRYPT_PREFIX):
        n, r, p = _SCRYPT_LEGACY_PARAMS
        return n, r, p, stored
    body = stored[len(_SCRYPT_PREFIX):]
    parts = body.split("$")
    if len(parts) != 4:
        raise ValueError("malformed scrypt hash")
    params: dict[str, int] = {}
    for piece in parts[:3]:
        k, _, v = piece.partition("=")
        if k not in ("n", "r", "p") or not v.isdigit():
            raise ValueError("malformed scrypt param")
        params[k] = int(v)
    if {"n", "r", "p"} != set(params):
        raise ValueError("missing scrypt param")
    return params["n"], params["r"], params["p"], parts[3]


def hash_password(password: str, *, params: tuple[int, int, int] | None = None) -> tuple[str, str]:
    """Compute (stored_hash, salt_hex) for ``password``.

    ``params`` overrides the default cost — used by tests that need cheap
    hashes, and by rehash-on-login when we want to embed the legacy params
    in a regression test.
    """
    if not isinstance(password, str) or len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError("password length out of bounds")
    n, r, p = params or SCRYPT_PARAMS_CURRENT
    salt = os.urandom(16)
    raw = _scrypt_hex(password, salt, n, r, p)
    return f"{_SCRYPT_PREFIX}n={n}$r={r}$p={p}${raw}", salt.hex()


def verify_password(password: str, hash_stored: str, salt_hex: str) -> bool:
    """Constant-time verify against either legacy or composite hashes."""
    if not isinstance(password, str) or len(password) > PASSWORD_MAX_LENGTH:
        return False
    try:
        n, r, p, expected_hex = _parse_password_hash(hash_stored)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    actual_hex = _scrypt_hex(password, salt, n, r, p)
    return hmac.compare_digest(actual_hex, expected_hex)


def password_needs_rehash(hash_stored: str) -> bool:
    """True if the stored hash was computed with weaker parameters than current.

    Drives the rehash-on-login flow: when a user successfully authenticates
    against a legacy hash we silently bump them to current cost so the DB
    drifts towards the new floor without forcing a password reset campaign.
    """
    try:
        n, r, p, _ = _parse_password_hash(hash_stored)
    except (ValueError, TypeError):
        # Malformed hash — treat as "needs rehash" so the operator's reset
        # flow has a chance to overwrite it.
        return True
    return (n, r, p) != SCRYPT_PARAMS_CURRENT


# --- Session helpers ---

def _hash_token(token: str) -> str:
    """Hash session token for storage (SHA-256). Token has 256 bits of entropy so fast hash is safe."""
    return hashlib.sha256(token.encode()).hexdigest()


# --- Symmetric authenticated encryption for at-rest secrets ---
#
# We need to protect ``lm_apikey`` and ``remote_mcps.auth`` from a DB-only
# compromise (filesystem read, leaked backup, sloppy volume mount).  Stdlib
# doesn't ship a real AEAD primitive — adding ``cryptography`` would break
# lm-chat's "zero-dependency" install promise.  Instead we compose primitives
# already in the standard library:
#
#   - HKDF-SHA256 to derive distinct stream/MAC keys from ``LM_CHAT_SECRET``
#   - SHAKE-256 (NIST SP 800-185 XOF) as a stream cipher: keystream =
#     SHAKE256(stream_key || nonce).digest(len(plaintext))
#   - HMAC-SHA256 over (nonce || ciphertext) for tamper resistance
#
# This is Encrypt-then-MAC.  Security depends on (a) nonce uniqueness — we
# use 16 random bytes per encryption, so the birthday bound is ~2^64 writes
# before reuse becomes likely; (b) keys never being reused for unrelated
# purposes — HKDF's ``info`` parameter binds keys to a per-purpose context.
#
# Storage format: ``enc$v1$<base64(nonce(16) || ciphertext || mac(32))>``.
# Any value missing the ``enc$v1$`` prefix is treated as legacy plaintext
# and the next write re-encrypts it; this gives a transparent upgrade for
# DBs created before the encryption rollout.

_ENC_PREFIX = "enc$v1$"
_ENC_NONCE_LEN = 16
_ENC_MAC_LEN = 32  # SHA-256 digest


def _hkdf_sha256(secret: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF using SHA-256.  ``salt`` fixed at 32 zero bytes since
    ``LM_CHAT_SECRET`` is already high-entropy (64 hex chars from secrets.token_hex)."""
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    t = b""
    okm = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        counter += 1
    return okm[:length]


def _enc_keys(context: str) -> tuple[bytes, bytes]:
    """Derive (stream_key, mac_key) bound to ``context`` from the signing key.

    The same ``context`` always yields the same pair, which is required so
    that decryption later picks up the same keys.  Different contexts (one
    per logical table column) keep the keys segregated.
    """
    secret = TOTP_SIGNING_KEY.encode()
    stream_key = _hkdf_sha256(secret, ("lm-chat-enc-stream:" + context).encode(), 32)
    mac_key    = _hkdf_sha256(secret, ("lm-chat-enc-mac:"    + context).encode(), 32)
    return stream_key, mac_key


def encrypt_at_rest(plaintext: str, context: str) -> str:
    """Encrypt ``plaintext`` for storage with ``context``-bound keys.

    Empty input returns empty output — there's nothing useful to encrypt
    and several call sites store "" to mean "unset", which we preserve.
    """
    if not plaintext:
        return ""
    stream_key, mac_key = _enc_keys(context)
    nonce = secrets.token_bytes(_ENC_NONCE_LEN)
    pt_bytes = plaintext.encode("utf-8")
    keystream = hashlib.shake_256(stream_key + nonce).digest(len(pt_bytes))
    ct = bytes(p ^ k for p, k in zip(pt_bytes, keystream))
    mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    blob = nonce + ct + mac
    return _ENC_PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_at_rest(stored: str, context: str) -> str | None:
    """Decrypt a value previously produced by ``encrypt_at_rest``.

    Returns the original plaintext, an empty string for empty input, or
    ``None`` if the value was tampered with or encrypted under a different
    secret.  Legacy plaintext (no prefix) is returned unchanged so the next
    write transparently upgrades it to the encrypted form.
    """
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored  # legacy plaintext — caller re-encrypts on next write
    try:
        blob = base64.b64decode(stored[len(_ENC_PREFIX):], validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(blob) < _ENC_NONCE_LEN + _ENC_MAC_LEN:
        return None
    nonce = blob[:_ENC_NONCE_LEN]
    mac = blob[-_ENC_MAC_LEN:]
    ct = blob[_ENC_NONCE_LEN:-_ENC_MAC_LEN]
    _, mac_key = _enc_keys(context)
    expected_mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        return None
    stream_key, _ = _enc_keys(context)
    keystream = hashlib.shake_256(stream_key + nonce).digest(len(ct))
    try:
        return bytes(c ^ k for c, k in zip(ct, keystream)).decode("utf-8")
    except UnicodeDecodeError:
        return None

def create_session(
    db: sqlite3.Connection,
    user_id: str,
    *,
    commit: bool = True,
    rotate: bool | None = None,
) -> str:
    """Insert a new session row and return the plaintext token.

    ``rotate=True`` deletes every other session for the same user as part
    of the same transaction, which is what fresh-login + change-password
    flows want.  Defaults to the ``LM_CHAT_SINGLE_SESSION`` env-driven
    policy when not specified.
    """
    token = secrets.token_hex(32)
    token_hash = _hash_token(token)
    expires = time.time() + SESSION_EXPIRY
    db.execute("INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
               (token_hash, user_id, time.time(), expires))
    if rotate is None:
        rotate = _SINGLE_SESSION
    if rotate:
        # Done in the same transaction so a crash can't leave the user with
        # two simultaneously-valid sessions.
        db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token != ?",
            (user_id, token_hash),
        )
    if commit:
        db.commit()
    return token  # return plaintext to client; only hash is stored

def get_session_user(db: sqlite3.Connection, token: str) -> dict | None:
    token_hash = _hash_token(token)
    row = db.execute(
        "SELECT s.user_id,s.expires_at,u.id,u.username,u.display_name,u.is_admin,u.totp_enabled "
        "FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=?", (token_hash,)
    ).fetchone()
    if not row or row["expires_at"] < time.time():
        if row and row["expires_at"] < time.time():
            db.execute("DELETE FROM sessions WHERE token=?", (token_hash,))
            db.commit()
        return None
    # Sliding window: only extend if past 50% of session lifetime
    remaining = row["expires_at"] - time.time()
    if remaining < SESSION_EXPIRY * 0.5:
        db.execute("UPDATE sessions SET expires_at=? WHERE token=?",
                   (time.time() + SESSION_EXPIRY, token_hash))
        db.commit()
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "is_admin": row["is_admin"], "totp_enabled": row["totp_enabled"] or 0}


# --- Rate limiting (SQLite-backed) ---

def check_rate_limit(ip: str) -> bool:
    """Check rate limit AND record a failed attempt atomically. Returns True if allowed."""
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        row = db.execute("SELECT attempts, first_attempt FROM rate_limits WHERE ip=?", (ip,)).fetchone()
        if not row:
            db.execute("INSERT INTO rate_limits (ip, attempts, first_attempt) VALUES (?, 1, ?)", (ip, now))
            db.execute("COMMIT")
            return True
        count, first = row
        if now - first > RATE_LIMIT_WINDOW:
            db.execute("UPDATE rate_limits SET attempts=1, first_attempt=? WHERE ip=?", (now, ip))
            db.execute("COMMIT")
            return True
        if count >= RATE_LIMIT_MAX_ATTEMPTS:
            db.execute("COMMIT")
            return False
        db.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE ip=?", (ip,))
        db.execute("COMMIT")
        return True
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise

def clear_rate_limit(ip: str) -> None:
    db = get_db()
    db.execute("DELETE FROM rate_limits WHERE ip=?", (ip,))
    db.commit()

def cleanup_expired_sessions(db: sqlite3.Connection) -> None:
    now = time.time()
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.execute("DELETE FROM rate_limits WHERE first_attempt < ?", (now - RATE_LIMIT_WINDOW,))
    db.commit()

# --- In-memory API rate limiter (token bucket) ---

class TokenBucketLimiter:
    """Per-key rate limiter using token bucket algorithm."""
    def __init__(self, rate, capacity):
        self.rate = rate          # tokens per second
        self.capacity = capacity
        self._buckets = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = [self.capacity - 1, now]
                return True
            tokens, last = self._buckets[key]
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[key] = [tokens, now]
                return False
            self._buckets[key] = [tokens - 1, now]
            return True

    def cleanup(self):
        now = time.monotonic()
        with self._lock:
            stale = [k for k, (_, ts) in self._buckets.items() if now - ts > 600]
            for k in stale:
                del self._buckets[k]

_api_limiter = TokenBucketLimiter(rate=2, capacity=60)     # 60 burst, 2/sec sustained
_stream_limiter = TokenBucketLimiter(rate=0.5, capacity=5)  # 5 burst, 1 per 2sec

# Periodic cleanup: run at most once per hour
_last_cleanup = 0.0
_cleanup_lock = threading.Lock()
CLEANUP_INTERVAL = 3600  # 1 hour

def maybe_cleanup_sessions():
    """Run session cleanup at most once per hour (thread-safe)."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < CLEANUP_INTERVAL:
        return
    with _cleanup_lock:
        if now - _last_cleanup < CLEANUP_INTERVAL:
            return
        _last_cleanup = now
        try:
            db = get_db()
            cleanup_expired_sessions(db)
            _api_limiter.cleanup()
            _stream_limiter.cleanup()
        except Exception as e:
            log.debug(f"Session cleanup failed (non-fatal): {e}")

# --- Server-side TOTP setup storage (C3: secret never in client token) ---

_pending_totp = {}
_pending_totp_lock = threading.Lock()

def store_totp_setup(user_id, secret):
    """Store TOTP secret server-side, return opaque setup token."""
    setup_id = secrets.token_urlsafe(32)
    now = time.time()
    with _pending_totp_lock:
        # Cleanup expired entries
        expired = [k for k, v in _pending_totp.items() if now - v["ts"] > TOTP_SETUP_EXPIRY]
        for k in expired:
            del _pending_totp[k]
        _pending_totp[setup_id] = {"user_id": user_id, "secret": secret, "ts": now}
    return setup_id

def get_totp_setup(setup_id, user_id):
    """Retrieve and validate a pending TOTP setup. Returns secret or None."""
    with _pending_totp_lock:
        pending = _pending_totp.get(setup_id)
    if not pending or pending["user_id"] != user_id:
        return None
    if time.time() - pending["ts"] > TOTP_SETUP_EXPIRY:
        with _pending_totp_lock:
            _pending_totp.pop(setup_id, None)
        return None
    return pending["secret"]

def consume_totp_setup(setup_id):
    """Remove a used TOTP setup token."""
    with _pending_totp_lock:
        _pending_totp.pop(setup_id, None)

# --- Validation ---

USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,32}$')

def validate_username(username: str | None) -> bool:
    return bool(USERNAME_RE.match(username or ""))

def validate_password(password: str | None) -> bool:
    return (
        isinstance(password, str)
        and PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH
    )


# --- TOTP helpers (RFC 6238) ---

def generate_totp_secret() -> str:
    """Generate 20-byte random secret, return as base32 (no padding)."""
    return base64.b32encode(os.urandom(20)).decode().rstrip('=')

def verify_totp(secret: str, code: str, window: int = 1) -> int | None:
    """Verify 6-digit TOTP code. Returns the counter value on success, None on failure."""
    padded = secret + '=' * (-len(secret) % 8)
    key = base64.b32decode(padded)
    now = int(time.time()) // 30
    for offset in range(-window, window + 1):
        counter = now + offset
        msg = struct.pack('>Q', counter)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        o = h[-1] & 0x0F
        truncated = struct.unpack('>I', h[o:o+4])[0] & 0x7FFFFFFF
        otp = str(truncated % 1000000).zfill(6)
        if hmac.compare_digest(otp, code):
            return counter
    return None

def make_totp_uri(username: str, secret: str) -> str:
    """Build otpauth:// URI for QR code."""
    return f"otpauth://totp/lm-chat:{username}?secret={secret}&issuer=lm-chat"

def sign_partial_token(user_id: str) -> str:
    """Create HMAC-signed partial token for 2FA login (5 min expiry). Base64-encoded to avoid exposing user_id."""
    ts = str(int(time.time()))
    msg = f"{user_id}:{ts}"
    sig = hmac.new(TOTP_SIGNING_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{user_id}:{ts}:{sig}".encode()).decode()

def verify_partial_token(token_str: str) -> str | None:
    """Verify and extract user_id from partial token. Returns user_id or None."""
    try:
        decoded = base64.urlsafe_b64decode(token_str.encode()).decode()
    except Exception:
        return None
    parts = decoded.split(':')
    if len(parts) != 3:
        return None
    user_id, ts, sig = parts
    try:
        if time.time() - int(ts) > PARTIAL_TOKEN_EXPIRY:
            return None
    except ValueError:
        return None
    expected = hmac.new(TOTP_SIGNING_KEY.encode(), f"{user_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        int(user_id, 16)  # Validate it's a valid hex string (UUID)
        return user_id
    except ValueError:
        return None

# --- Context management ---

_cached_embed_model = None

def _resolve_embed_model(token: str | None = None) -> str:
    global _cached_embed_model
    if _cached_embed_model:
        return _cached_embed_model
    headers = {"Content-Type": "application/json"}
    auth_token = token or LMSTUDIO_TOKEN or "sk-lm-lmchat01:akelarreakelarre1234"
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        req = urllib.request.Request(f"{LMSTUDIO}/v1/models", headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = [m.get("id", "") for m in data.get("data", [])]
            for m in models:
                if any(kw in m.lower() for kw in ("embed", "nomic", "bge", "gte", "minilm")):
                    _cached_embed_model = m
                    return m
            if models:
                _cached_embed_model = models[0]
                return models[0]
    except Exception:
        pass
    return "text-embedding-nomic-embed-text-v1.5"

def get_embedding(text: str, token: str | None = None) -> list[float] | None:
    """Get embedding vector from LM Studio. Returns list of floats or None."""
    auth_token = token or LMSTUDIO_TOKEN or "sk-lm-lmchat01:akelarreakelarre1234"
    model_name = _resolve_embed_model(auth_token)
    payload = {"model": model_name, "input": text[:2000]}
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        req = urllib.request.Request(
            f"{LMSTUDIO}/v1/embeddings",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        # Validate response shape — LM Studio occasionally returns a
        # well-formed 200 with empty ``data`` if the embedding model
        # crashed; surface that as a None return without an IndexError
        # crash but log so the caller can correlate against the silent
        # degradation symptom.
        items = data.get("data") if isinstance(data, dict) else None
        if not items or not isinstance(items, list) or not items[0].get("embedding"):
            log.warning(f"get_embedding: empty/malformed response from {LMSTUDIO}/v1/embeddings")
            return None
        return items[0]["embedding"]
    except Exception as e:
        log.warning(f"get_embedding failed: {e}")
        return None


class PooledHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a bounded thread pool instead of unbounded threads."""
    _pool = ThreadPoolExecutor(max_workers=64)
    daemon_threads = True
    def process_request(self, request, client_address):
        self._pool.submit(self.process_request_thread, request, client_address)
    def server_close(self):
        self._pool.shutdown(wait=True)
        super().server_close()

class Handler(BaseHTTPRequestHandler):
    timeout = 30  # seconds — prevents slow-loris and thread leaks

    # --- Request lifecycle hooks ---

    def setup(self):
        # Generate a short correlation ID for every connection so access logs,
        # error logs, and (eventually) SSE error frames can all be cross-
        # referenced.  12 hex chars = 48 bits — enough to make collisions on
        # an in-memory dashboard effectively impossible.
        super().setup()
        self.request_id = uuid.uuid4().hex[:12]
        self._req_started_at = time.monotonic()

    def log_request(self, code="-", size="-"):
        # ``log_request`` is called by ``send_response`` once per response,
        # which is the right place for an access log.  We emit at INFO so
        # the default console handler shows it; the rotating file handler
        # captures it at any level.  Output is structured: a reviewer can
        # grep ``req_id=...`` and follow a single request through every
        # other log line.
        try:
            latency_ms = int((time.monotonic() - self._req_started_at) * 1000)
        except AttributeError:
            latency_ms = 0
        log.info(
            "req_id=%s ip=%s method=%s path=%s status=%s latency_ms=%d",
            getattr(self, "request_id", "?"),
            self._client_ip(),
            self.command,
            self.path,
            code,
            latency_ms,
        )

    # --- Auth middleware ---

    def _parse_session_cookie(self):
        cookie_str = self.headers.get("Cookie", "")
        c = http.cookies.SimpleCookie()
        try:
            c.load(cookie_str)
        except Exception:
            return None
        # Check __Host- prefixed cookie first (HTTPS), fall back to unprefixed (HTTP)
        morsel = c.get("__Host-lm_session") or c.get("lm_session")
        return morsel.value if morsel else None

    def _get_user(self):
        """Returns user dict or None. Does NOT send 401."""
        if not AUTH_ENABLED:
            return {"id": "default", "username": "default", "display_name": "User", "is_admin": 1}
        token = self._parse_session_cookie()
        if not token:
            return None
        db = get_db()
        user = get_session_user(db, token)
        return user

    def _check_csrf(self):
        """Check CSRF header on mutating requests. Returns True if OK, sends 403 and returns False if not."""
        if self.command in ("POST", "PATCH", "DELETE") and self.headers.get("X-Requested-With") != "lm-chat":
            self._error(403, "missing CSRF header")
            return False
        return True

    def _require_auth(self, rate_limit=True):
        """Returns user dict or sends 401/429 and returns None."""
        if not self._check_csrf():
            return None
        # Periodic session cleanup
        maybe_cleanup_sessions()
        user = self._get_user()
        if not user:
            self._error(401, "unauthorized")
            return None
        # API rate limiting (per-user)
        if rate_limit and not _api_limiter.allow(f"api:{user['id']}"):
            self._error(429, "too many requests")
            return None
        return user

    def _verify_chat_owner(self, db, chat_id, user_id):
        """Returns True if chat exists and belongs to user. Sends 404 if not.

        Ownership is checked unconditionally — the previous version skipped
        the user_id comparison when AUTH was disabled, which leaked every
        chat in the DB when an operator with multi-user history toggled auth
        off.  The startup gate in ``__main__`` refuses that configuration
        outright, but this is the defence-in-depth backstop: the moment a
        request lands here, we enforce ownership regardless of mode.
        """
        row = db.execute("SELECT user_id FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            self._error(404, "chat not found")
            return False
        if row[0] != user_id:
            self._error(404, "chat not found")
            return False
        return True

    _CHAT_SETTINGS_ALLOWLIST = {
        "system_prompt":    (str,   lambda v: isinstance(v, str) and len(v) <= 8000),
        "temperature":      (float, lambda v: isinstance(v, (int, float)) and 0.0 <= v <= 2.0),
        "top_p":            (float, lambda v: isinstance(v, (int, float)) and 0.0 <= v <= 1.0),
        "top_k":            (int,   lambda v: isinstance(v, int) and 0 <= v <= 500),
        "min_p":            (float, lambda v: isinstance(v, (int, float)) and 0.0 <= v <= 1.0),
        "repeat_penalty":   (float, lambda v: isinstance(v, (int, float)) and 0.0 <= v <= 3.0),
        "max_output_tokens":(int,   lambda v: isinstance(v, int) and (v == -1 or 1 <= v <= 32768)),
        "reasoning":        (str,   lambda v: v in ("off", "low", "medium", "high", "on")),
        "sc_enabled":       (bool,  lambda v: isinstance(v, bool)),
        "cove_enabled":     (bool,  lambda v: isinstance(v, bool)),
    }

    def _get_chat_settings(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        row = db.execute(
            "SELECT settings FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user["id"])
        ).fetchone()
        if row is None:
            return self._error(404, "chat not found")
        try:
            settings = json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            settings = {}
        self._json_response(200, settings)

    def _save_chat_settings(self, chat_id, body):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        row = db.execute(
            "SELECT settings FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user["id"])
        ).fetchone()
        if row is None:
            return self._error(404, "chat not found")
        try:
            existing = json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            existing = {}
        # Merge: null values remove keys; non-null values are validated
        for key, value in body.items():
            if value is None:
                existing.pop(key, None)  # explicit null removes the key
            elif key not in self._CHAT_SETTINGS_ALLOWLIST:
                return self._error(400, f"unknown setting: {key}")
            else:
                _, validator = self._CHAT_SETTINGS_ALLOWLIST[key]
                if not validator(value):
                    return self._error(400, f"invalid value for {key}")
                existing[key] = value
        # Write NULL if empty, not '{}'
        new_json = json.dumps(existing) if existing else None
        db.execute(
            "UPDATE chats SET settings = ? WHERE id = ? AND user_id = ?",
            (new_json, chat_id, user["id"])
        )
        db.commit()
        self._json_response(200, existing)

    def _delete_chat_settings(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        result = db.execute(
            "UPDATE chats SET settings = NULL WHERE id = ? AND user_id = ?",
            (chat_id, user["id"])
        )
        if result.rowcount == 0:
            return self._error(404, "chat not found")
        db.commit()
        self._json_response(200, {})

    def _pin_message(self, message_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        row = db.execute("""
            SELECT m.content, m.role, c.id, c.title
            FROM messages m
            JOIN chats c ON m.chat_id = c.id
            WHERE m.id = ? AND c.user_id = ? AND m.role = 'assistant'
        """, (message_id, user["id"])).fetchone()
        if not row:
            return self._error(404, "message not found or not an assistant message")
        content, _, chat_id, chat_title = row
        if not content:
            return self._error(400, "message has no content to pin")
        existing = db.execute(
            "SELECT id FROM pins WHERE message_id = ? AND user_id = ?",
            (message_id, user["id"])
        ).fetchone()
        if existing:
            return self._json_response(200, {"id": existing[0], "already_pinned": True})
        pin_id = uuid.uuid4().hex
        now = time.time()
        db.execute(
            """INSERT INTO pins (id, user_id, message_id, chat_id, chat_title, content, pin_title, pinned_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
            (pin_id, user["id"], message_id, chat_id, chat_title, content, now)
        )
        db.commit()
        t = threading.Thread(
            target=self._generate_pin_title,
            args=(pin_id, content, user["id"], chat_id),
            daemon=True
        )
        t.start()
        self._json_response(201, {
            "id": pin_id, "message_id": message_id, "chat_id": chat_id,
            "chat_title": chat_title, "pin_title": None, "pinned_at": now
        })

    def _delete_pin(self, pin_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        result = db.execute(
            "DELETE FROM pins WHERE id = ? AND user_id = ?",
            (pin_id, user["id"])
        )
        if result.rowcount == 0:
            return self._error(404, "pin not found")
        db.commit()
        self._json_response(200, {"ok": True})

    def _list_mcp_servers(self):
        """Return MCP server names from ~/.lmstudio/mcp.json as integration IDs.
        Only exposes server names — no keys, commands, or env vars.
        """
        mcp_path = os.environ.get(
            "LMSTUDIO_MCP_JSON",
            os.path.join(os.path.expanduser("~"), ".lmstudio", "mcp.json")
        )
        try:
            with open(mcp_path) as f:
                data = json.load(f)
            servers = data.get("mcpServers", {})
            result = []
            for name in servers:
                # Integration ID format: mcp/{name}
                # Display name: capitalize words, replace hyphens/underscores with spaces
                display = name.replace("-", " ").replace("_", " ").title()
                result.append({"id": f"mcp/{name}", "name": display})
            self._json_response(200, {"servers": result})
        except FileNotFoundError:
            self._json_response(200, {"servers": []})
        except Exception as e:
            log.warning(f"Could not read mcp.json: {e}")
            self._json_response(200, {"servers": []})

    def _list_pins(self):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute(
            """SELECT id, message_id, chat_id, chat_title, pin_title, pinned_at,
                      substr(content, 1, 200) as preview
               FROM pins WHERE user_id = ?
               ORDER BY pinned_at DESC""",
            (user["id"],)
        ).fetchall()
        pins = [
            {
                "id": r[0], "message_id": r[1], "chat_id": r[2],
                "chat_title": r[3], "pin_title": r[4], "pinned_at": r[5],
                "preview": r[6]
            }
            for r in rows
        ]
        self._json_response(200, pins)

    def _get_chat_pins(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute(
            """SELECT id, pin_title, message_id, pinned_at, substr(content, 1, 40) as fallback
               FROM pins
               WHERE user_id = ? AND chat_id = ?
               ORDER BY message_id IS NULL ASC, message_id ASC, pinned_at ASC""",
            (user["id"], chat_id)
        ).fetchall()
        pins = [
            {
                "id": r[0],
                "pin_title": r[1] or r[4],
                "message_id": r[2],
                "pinned_at": r[3]
            }
            for r in rows
        ]
        self._json_response(200, pins)

    def _update_pin_title(self, pin_id, body):
        user = self._require_auth()
        if not user:
            return
        title = (body.get("title") or "").strip()[:80]
        if not title:
            return self._error(400, "title required")
        db = get_db()
        result = db.execute(
            "UPDATE pins SET pin_title = ? WHERE id = ? AND user_id = ?",
            (title, pin_id, user["id"])
        )
        if result.rowcount == 0:
            return self._error(404, "pin not found")
        db.commit()
        self._json_response(200, {"ok": True, "title": title})

    def _generate_pin_title(self, pin_id, content, user_id, chat_id):
        """Background thread — must open its own DB connection via get_db()."""
        try:
            db = get_db()
            row = db.execute("SELECT model FROM chats WHERE id = ?", (chat_id,)).fetchone()
            model = row[0] if row else None
            if not model:
                return
            prompt = f"Summarize this in 5-7 words as a short navigation label. No punctuation.\n\n{content[:500]}"
            payload = {
                "model": model,
                "input": prompt,
                "max_output_tokens": 20,
                "temperature": 0.3,
                "store": False,
                "integrations": []
            }
            data = self._lmstudio_chat(payload, user_id, timeout=15)
            title = self._extract_content(data).strip()[:80]
            if title:
                db.execute("UPDATE pins SET pin_title = ? WHERE id = ?", (title, pin_id))
                db.commit()
        except Exception as e:
            log.warning(f"Pin title generation failed for pin {pin_id}: {e}")

    def _resolve_chat_settings(self, chat_id, user_id, body):
        """Merge per-chat DB settings into the request body as fallback values.
        Layering (first non-null wins, per parameter):
          client body → chat DB settings → global localStorage defaults → LM Studio instance config
        DB settings only fill keys that the client body omitted or sent as None.
        """
        if not chat_id:
            return
        db = get_db()
        row = db.execute(
            "SELECT settings FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()
        if not row or not row[0]:
            return
        try:
            chat_settings = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return
        for key, value in chat_settings.items():
            if key not in body or body[key] is None:
                body[key] = value

    def _client_ip(self):
        """Return the originating client IP.

        Trusts ``X-Forwarded-For`` (leftmost address) and ``X-Real-IP`` only
        when ``LM_CHAT_TRUSTED_PROXY`` is set — otherwise the headers are
        attacker-controlled and would let any client spoof their IP for the
        per-IP rate limiter.  Falls back to the raw TCP peer address.
        """
        if _TRUSTED_PROXY:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
            xri = self.headers.get("X-Real-IP")
            if xri:
                return xri.strip()
        return self.client_address[0]

    def _secure_flag(self):
        """Return '; Secure' if running behind HTTPS, else empty string.

        ``X-Forwarded-Proto`` is honored only when an explicit trusted-proxy
        env is set; otherwise any client could send the header and trick the
        server into emitting a ``Secure``-flagged cookie that never reaches
        the browser over plain HTTP.
        """
        if os.environ.get("LM_CHAT_HTTPS"):
            return "; Secure"
        if _TRUSTED_PROXY and self.headers.get("X-Forwarded-Proto") == "https":
            return "; Secure"
        return ""

    def _set_session_cookie(self, token):
        # Cookie name is ``lm_session`` regardless of scheme.  The
        # earlier ``__Host-lm_session`` prefix was a hardening that
        # depended on Secure + Path=/ + no Domain, but its interaction
        # with reverse-proxy schemes caused first-request races where
        # the browser had the cookie set but failed to send it back,
        # producing "Failed to load your settings — unauthorized"
        # banners that disappeared on a manual refresh.  Dropping the
        # prefix removes the ambiguity; ``Secure`` + ``HttpOnly`` +
        # ``SameSite=Strict`` continue to apply when the request was
        # HTTPS (or a trusted proxy reports so).
        secure = self._secure_flag()
        flags = f"HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_EXPIRY}{secure}"
        self.send_header("Set-Cookie", f"lm_session={token}; {flags}")

    def _clear_session_cookie(self):
        secure = self._secure_flag()
        self.send_header("Set-Cookie", f"lm_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{secure}")

    def _send_security_headers(self, csp=None, referrer="strict-origin-when-cross-origin"):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or CSP)
        self.send_header("Referrer-Policy", referrer)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()")
        # HSTS — only valid over HTTPS per RFC 6797.  _secure_flag is truthy
        # when LM_CHAT_HTTPS is set or the upstream proxy forwarded HTTPS.
        # Browsers ignore Strict-Transport-Security on plain HTTP responses,
        # but we still gate emission to avoid leaking the directive on
        # mixed-deploy configs and to keep header counts low on health pings.
        if _HSTS_MODE in ("true", "1", "on", "preload") and self._secure_flag():
            directives = [f"max-age={_HSTS_MAX_AGE}", "includeSubDomains"]
            if _HSTS_MODE == "preload":
                directives.append("preload")
            self.send_header("Strict-Transport-Security", "; ".join(directives))

    def _json_response_with_cookie(self, code, data, cookie_token=None, clear_cookie=False):
        """Like _json_response but can set/clear cookie before end_headers."""
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self._send_security_headers()
        if cookie_token:
            self._set_session_cookie(cookie_token)
        if clear_cookie:
            self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(data)

    # --- Routing ---

    _GET_ROUTES = [
        (re.compile(r'^/$|^/index\.html$'),                                    lambda s,m,b: s._serve_file("index.html","text/html")),
        (re.compile(r'^/manifest\.json$'),                                     lambda s,m,b: s._serve_file("manifest.json","application/json")),
        (re.compile(r'^/sw\.js$'),                                             lambda s,m,b: s._serve_file("sw.js","application/javascript")),
        (re.compile(r'^/lm-chat-logo\.svg$'),                                  lambda s,m,b: s._serve_file("lm-chat-logo.svg","image/svg+xml")),
        (re.compile(r'^/style\.css$'),                                         lambda s,m,b: s._serve_file("style.css","text/css")),
        (re.compile(r'^/app\.js$'),                                            lambda s,m,b: s._serve_file("app.js","application/javascript")),
        (re.compile(r'^/highlight\.min\.js$'),                                 lambda s,m,b: s._serve_file("highlight.min.js","application/javascript")),
        (re.compile(r'^/highlight\.min\.css$'),                                lambda s,m,b: s._serve_file("highlight.min.css","text/css")),
        (re.compile(r'^/api/health$'),                                         lambda s,m,b: s._health_check()),
        (re.compile(r'^/share/(?P<id>[^/]+)$'),                                lambda s,m,b: s._serve_shared(m.group("id"))),
        (re.compile(r'^/api/debug$'),                                          lambda s,m,b: s._get_debug()),
        (re.compile(r'^/api/auth/me$'),                                        lambda s,m,b: s._auth_me()),
        (re.compile(r'^/api/auth/users$'),                                     lambda s,m,b: s._auth_list_users()),
        (re.compile(r'^/api/auth/settings$'),                                  lambda s,m,b: s._get_settings()),
        (re.compile(r'^/api/models$'),                                         lambda s,m,b: s._get_models()),
        (re.compile(r'^/api/chats$'),                                          lambda s,m,b: s._list_chats()),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/messages$'),                   lambda s,m,b: s._get_messages(m.group("id"))),
        (re.compile(r'^/api/insights$'),                                       lambda s,m,b: s._list_insights()),
        (re.compile(r'^/api/insights/settings$'),                              lambda s,m,b: s._get_insight_settings()),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/settings$'),                   lambda s,m,b: s._get_chat_settings(m.group("id"))),
        (re.compile(r'^/api/pins$'),                                           lambda s,m,b: s._list_pins()),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/pins$'),                       lambda s,m,b: s._get_chat_pins(m.group("id"))),
        (re.compile(r'^/api/mcp/servers$'),                                    lambda s,m,b: s._list_mcp_servers()),
    ]

    _POST_ROUTES = [
        (re.compile(r'^/api/auth/setup$'),                                     lambda s,m,b: s._auth_setup(b)),
        (re.compile(r'^/api/auth/login$'),                                     lambda s,m,b: s._auth_login(b)),
        (re.compile(r'^/api/auth/logout$'),                                    lambda s,m,b: s._auth_logout()),
        (re.compile(r'^/api/auth/invite$'),                                    lambda s,m,b: s._auth_invite(b)),
        (re.compile(r'^/api/auth/change-password$'),                           lambda s,m,b: s._auth_change_password(b)),
        (re.compile(r'^/api/auth/totp/setup$'),                                lambda s,m,b: s._totp_setup()),
        (re.compile(r'^/api/auth/totp/verify$'),                               lambda s,m,b: s._totp_verify(b)),
        (re.compile(r'^/api/auth/totp/disable$'),                              lambda s,m,b: s._totp_disable(b)),
        (re.compile(r'^/api/auth/totp/login$'),                                lambda s,m,b: s._totp_login(b)),
        (re.compile(r'^/api/auth/settings$'),                                  lambda s,m,b: s._save_settings(b)),
        (re.compile(r'^/api/debug$'),                                          lambda s,m,b: s._post_debug(b)),
        (re.compile(r'^/api/chat$'),                                           lambda s,m,b: s._handle_chat(b)),
        (re.compile(r'^/api/chat/stream$'),                                    lambda s,m,b: s._handle_chat_stream(b)),
        (re.compile(r'^/api/chats$'),                                          lambda s,m,b: s._create_chat(b)),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/messages/truncate$'),          lambda s,m,b: s._truncate_messages(m.group("id"),b)),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/fork$'),                       lambda s,m,b: s._fork_chat(m.group("id"),b)),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/compact$'),                    lambda s,m,b: s._compact_chat(m.group("id"),b)),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/pin$'),                        lambda s,m,b: s._toggle_pin(m.group("id"))),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/folder$'),                     lambda s,m,b: s._set_folder(m.group("id"),b)),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/share$'),                      lambda s,m,b: s._share_chat(m.group("id"))),
        (re.compile(r'^/api/search$'),                                         lambda s,m,b: s._search_messages(b)),
        (re.compile(r'^/api/insights/distill$'),                               lambda s,m,b: s._distill_insights(b)),
        (re.compile(r'^/api/insights/refine$'),                                lambda s,m,b: s._refine_insights(b)),
        (re.compile(r'^/api/insights/reindex$'),                               lambda s,m,b: s._reindex_embeddings()),
        (re.compile(r'^/api/insights$'),                                       lambda s,m,b: s._add_insight(b)),
        (re.compile(r'^/api/insights/(?P<id>[^/]+)/edit$'),                    lambda s,m,b: s._edit_insight(m.group("id"),b)),
        (re.compile(r'^/api/messages/(?P<id>\d+)/feedback$'),                  lambda s,m,b: s._post_message_feedback(int(m.group("id")),b)),
        (re.compile(r'^/api/messages/(?P<id>\d+)/pin$'),                       lambda s,m,b: s._pin_message(int(m.group("id")))),
    ]

    _DELETE_ROUTES = [
        (re.compile(r'^/api/auth/users/(?P<id>[^/]+)$'),                       lambda s,m,b: s._auth_delete_user(m.group("id"))),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/messages/last$'),              lambda s,m,b: s._delete_last_response(m.group("id"))),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/share$'),                      lambda s,m,b: s._unshare_chat(m.group("id"))),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/settings$'),                   lambda s,m,b: s._delete_chat_settings(m.group("id"))),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)$'),                            lambda s,m,b: s._delete_chat(m.group("id"))),
        (re.compile(r'^/api/insights$'),                                       lambda s,m,b: s._clear_insights()),
        (re.compile(r'^/api/insights/(?P<id>[^/]+)$'),                         lambda s,m,b: s._delete_insight(m.group("id"))),
        (re.compile(r'^/api/pins/(?P<id>[^/]+)$'),                             lambda s,m,b: s._delete_pin(m.group("id"))),
    ]

    _PATCH_ROUTES = [
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/settings$'),                   lambda s,m,b: s._save_chat_settings(m.group("id"),b)),
        (re.compile(r'^/api/chats/(?P<id>[^/]+)/title$'),                      lambda s,m,b: s._update_title(m.group("id"),b)),
        (re.compile(r'^/api/auth/profile$'),                                   lambda s,m,b: s._auth_update_profile(b)),
        (re.compile(r'^/api/insights/settings$'),                              lambda s,m,b: s._save_insight_settings(b)),
        (re.compile(r'^/api/pins/(?P<id>[^/]+)/title$'),                       lambda s,m,b: s._update_pin_title(m.group("id"),b)),
    ]

    _ROUTES = {"GET": _GET_ROUTES, "POST": _POST_ROUTES, "DELETE": _DELETE_ROUTES, "PATCH": _PATCH_ROUTES}

    def _health_check(self):
        status = {"ok": True, "version": VERSION, "db": False, "lmstudio": False}
        try:
            db = get_db()
            db.execute("SELECT 1")
            status["db"] = True
        except Exception as e:
            log.warning(f"Health check DB failed: {e}")
        try:
            req = urllib.request.Request(f"{LMSTUDIO}/api/v1/models", method="GET")
            # Centralised: env var first, else any stored user key.
            # Goes through ``decrypt_at_rest`` — no raw row[0] reads of
            # encrypted columns (root fix for 2026-05-16 enc$v1$ leak).
            token = self._get_any_user_lm_apikey()
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
            status["lmstudio"] = True
        except Exception:
            pass
        status["ok"] = status["db"] and status["lmstudio"]
        code = 200 if status["ok"] else 503
        self._json_response(code, status)

    def _get_debug(self):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin only")
        # Return current debug state + log file info
        console_level = "DEBUG"
        for h in log.handlers:
            if getattr(h, "name", None) == "console":
                console_level = logging.getLevelName(h.level)
        log_files = []
        if os.path.isdir(LOG_DIR):
            for f in sorted(os.listdir(LOG_DIR)):
                fp = os.path.join(LOG_DIR, f)
                if os.path.isfile(fp):
                    log_files.append({"name": f, "size": os.path.getsize(fp)})
        self._json_response(200, {
            "enabled": console_level == "DEBUG",
            "log_dir": LOG_DIR,
            "log_files": log_files,
            "max_bytes": LOG_MAX_BYTES,
            "backup_count": LOG_BACKUP_COUNT,
        })

    def _post_debug(self, body):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin only")
        enabled = body.get("enabled", True)
        set_debug_mode(enabled)
        self._json_response(200, {"enabled": enabled})

    def _dispatch(self, method, body=None):
        path = self.path.split("?")[0]
        routes = self._ROUTES.get(method)
        if routes is None:
            self.send_error(405)
            return
        for pattern, handler in routes:
            m = pattern.match(path)
            if m:
                handler(self, m, body)
                return
        # SPA-fallback: a deep-linked URL like /chat/:id or /settings/:tab
        # (Phase 6 routing) reaches the server on page reload because the
        # browser GETs the path before the SPA's history API kicks in.
        # Anything that isn't an /api/* call AND isn't a static asset is
        # treated as an SPA route — serve index.html so the SPA can read
        # window.location.pathname and apply the route client-side.  POST
        # / DELETE / etc. still 404 — only GET gets the fallback.
        if (method == "GET"
                and not path.startswith("/api/")
                and not path.startswith("/share/")
                and "." not in path.rsplit("/", 1)[-1]):
            return self._serve_file("index.html", "text/html")
        self.send_error(404)

    def do_GET(self):
        self._dispatch("GET")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "86400")
        self._send_security_headers()
        self.end_headers()

    def _read_json_body(self):
        """Read and JSON-decode the request body, applying MAX_BODY_SIZE.

        Returns the parsed body on success.  On any framing/size/parse error
        it calls ``self._error`` with the appropriate status and returns the
        sentinel ``None`` so callers can short-circuit dispatch.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                self._error(400, "invalid content-length")
                return None
            if length > MAX_BODY_SIZE:
                self._error(413, "request too large")
                return None
            return json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, json.JSONDecodeError):
            self._error(400, "invalid request body")
            return None

    def do_POST(self):
        body = self._read_json_body()
        if body is None:
            return
        self._dispatch("POST", body)

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        body = self._read_json_body()
        if body is None:
            return
        self._dispatch("PATCH", body)

    # --- Auth endpoints ---

    def _auth_me(self):
        if not AUTH_ENABLED:
            return self._json_response(200, {"auth_enabled": False})
        db = get_db()
        user_count = db.execute("SELECT COUNT(*) FROM users WHERE username != 'default'").fetchone()[0]
        user = self._get_user()
        if not user:
            return self._json_response(200, {
                "auth_enabled": True, "user": None, "needs_setup": user_count == 0
            })
        return self._json_response(200, {
            "auth_enabled": True, "user": user, "needs_setup": False
        })

    def _auth_setup(self, body):
        if not self._check_csrf():
            return
        if not AUTH_ENABLED:
            return self._error(400, "auth not enabled")
        ip = self._client_ip()
        if not check_rate_limit(ip):
            return self._error(429, "too many attempts, try again later")
        # Operator-set setup token gates first-visitor-wins on public URLs.
        # Constant-time compare prevents timing oracles on the token value;
        # body["setup_token"] is allowed to be absent only when the env is unset.
        if _SETUP_TOKEN:
            supplied = (body.get("setup_token") or "")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, _SETUP_TOKEN):
                return self._error(401, "setup token required")
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        display_name = (body.get("display_name") or "").strip() or username
        if not validate_username(username):
            return self._error(400, "username must be 3-32 chars, alphanumeric + underscore")
        if not validate_password(password):
            return self._error(400, f"password must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters")
        user_id = uuid.uuid4().hex
        pw_hash, salt = hash_password(password)
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM users WHERE username != 'default'").fetchone()[0]
            if count > 0:
                db.rollback()
                return self._error(400, "setup already complete")
            db.execute(
                "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, username, pw_hash, salt, display_name, 1, time.time()),
            )
            db.execute("UPDATE chats SET user_id=? WHERE user_id IS NULL", (user_id,))
            token = create_session(db, user_id, commit=False)
            db.commit()
        except Exception as e:
            db.rollback()
            log.error(f"Auth setup failed: {e}", exc_info=True)
            return self._error(500, "setup failed")
        user = {"id": user_id, "username": username, "display_name": display_name, "is_admin": 1}
        self._json_response_with_cookie(200, {"user": user}, cookie_token=token)

    def _auth_login(self, body):
        if not self._check_csrf():
            return
        if not AUTH_ENABLED:
            return self._error(400, "auth not enabled")
        ip = self._client_ip()
        if not check_rate_limit(ip):
            return self._error(429, "too many login attempts, try again in 15 minutes")
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        db = get_db()
        cleanup_expired_sessions(db)
        row = db.execute(
            "SELECT id,username,password_hash,salt,display_name,is_admin,totp_enabled FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if not row or not verify_password(password, row["password_hash"], row["salt"]):

            return self._error(401, "invalid username or password")
        # Silently upgrade the stored hash if it was computed with weaker
        # parameters than the current floor (e.g. pre-0.5.0 legacy hashes).
        # Done after a successful verify so we know ``password`` is correct.
        if password_needs_rehash(row["password_hash"]):
            try:
                new_hash, new_salt = hash_password(password)
                db.execute(
                    "UPDATE users SET password_hash=?, salt=? WHERE id=?",
                    (new_hash, new_salt, row["id"]),
                )
                db.commit()
            except Exception as e:
                # Non-fatal — log and continue; the user is authenticated.
                log.warning(f"password rehash failed for user {row['id']}: {e}")
        # Check if 2FA is enabled
        if row["totp_enabled"]:
            partial = sign_partial_token(row["id"])
            return self._json_response(200, {"needs_totp": True, "partial_token": partial})
        # Only clear rate limit after FULL authentication (no 2FA pending)
        clear_rate_limit(ip)
        token = create_session(db, row["id"])
        user = {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "is_admin": row["is_admin"]}
        self._json_response_with_cookie(200, {"user": user}, cookie_token=token)

    def _auth_logout(self):
        if not self._check_csrf():
            return
        token = self._parse_session_cookie()
        if token:
            db = get_db()
            db.execute("DELETE FROM sessions WHERE token=?", (_hash_token(token),))
            db.commit()
        self._json_response_with_cookie(200, {"ok": True}, clear_cookie=True)

    def _auth_invite(self, body):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin required")
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        display_name = (body.get("display_name") or "").strip() or username
        if not validate_username(username):
            return self._error(400, "username must be 3-32 chars, alphanumeric + underscore")
        if not validate_password(password):
            return self._error(400, f"password must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters")
        db = get_db()
        user_id = uuid.uuid4().hex
        pw_hash, salt = hash_password(password)
        try:
            db.execute(
                "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, username, pw_hash, salt, display_name, 0, time.time()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return self._error(409, "username already taken")
        new_user = {"id": user_id, "username": username, "display_name": display_name, "is_admin": 0}
        self._json_response(200, {"user": new_user})

    def _auth_delete_user(self, target_id):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin required")
        if target_id == user["id"]:
            return self._error(400, "cannot delete yourself")
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT id FROM users WHERE id=?", (target_id,)).fetchone()
            if not target:
                db.rollback()
                return self._error(404, "user not found")
            db.execute("DELETE FROM chats WHERE user_id=?", (target_id,))  # messages cascade via FK
            db.execute("DELETE FROM shared_chats WHERE user_id=?", (target_id,))
            db.execute("DELETE FROM user_settings WHERE user_id=?", (target_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
            db.execute("DELETE FROM users WHERE id=?", (target_id,))
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            log.error(f"Failed to delete user: {e}", exc_info=True)
            return self._error(500, "failed to delete user")
        self._json_response(200, {"ok": True})

    def _auth_change_password(self, body):
        user = self._require_auth()
        if not user:
            return
        current_password = body.get("current_password") or ""
        new_password = body.get("new_password") or ""
        if not validate_password(new_password):
            return self._error(400, f"new password must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters")
        db = get_db()
        row = db.execute("SELECT password_hash,salt FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not verify_password(current_password, row["password_hash"], row["salt"]):
            return self._error(401, "current password is incorrect")
        pw_hash, salt = hash_password(new_password)
        db.execute("UPDATE users SET password_hash=?,salt=? WHERE id=?", (pw_hash, salt, user["id"]))
        # Invalidate all existing sessions
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        # Create fresh session
        token = create_session(db, user["id"])
        self._json_response_with_cookie(200, {"ok": True}, cookie_token=token)

    def _auth_update_profile(self, body):
        user = self._require_auth()
        if not user:
            return
        display_name = (body.get("display_name") or "").strip()
        if not display_name:
            return self._error(400, "display name required")
        if len(display_name) > 100:
            return self._error(400, "display name too long (max 100)")
        db = get_db()
        db.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user["id"]))
        db.commit()
        self._json_response(200, {"ok": True})

    def _auth_list_users(self):
        user = self._require_auth()
        if not user:
            return
        if not user.get("is_admin"):
            return self._error(403, "admin required")
        db = get_db()
        # Exclude the synthetic ``default`` user — it exists for FK
        # integrity in auth-disabled mode, not as a real account, and
        # has no password to log in with.  Surfacing it in the admin
        # users list (with a Delete button) was a polish-pass defect.
        rows = db.execute(
            "SELECT id,username,display_name,is_admin,created_at FROM users "
            "WHERE username != 'default' "
            "ORDER BY created_at"
        ).fetchall()
        users = [{"id": r[0], "username": r[1], "display_name": r[2], "is_admin": r[3], "created_at": r[4]} for r in rows]
        self._json_response(200, users)

    # --- TOTP 2FA ---

    def _totp_setup(self):
        """Generate TOTP secret and return QR SVG. Does NOT enable 2FA yet."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        existing = db.execute("SELECT totp_enabled FROM users WHERE id=?", (user["id"],)).fetchone()
        if existing and existing[0]:
            return self._error(400, "2FA already enabled, disable first")
        secret = generate_totp_secret()
        uri = make_totp_uri(user["username"], secret)
        qr_svg = generate_qr_svg(uri) or ""
        # Store secret server-side; client gets opaque token (secret never in transport token)
        setup_token = store_totp_setup(user["id"], secret)
        self._json_response(200, {"secret": secret, "qr_svg": qr_svg, "setup_token": setup_token})

    def _totp_verify(self, body):
        """Verify TOTP code and enable 2FA."""
        user = self._require_auth()
        if not user:
            return
        code = (body.get("code") or "").strip()
        setup_token = body.get("setup_token") or ""
        if not code or len(code) != 6:
            return self._error(400, "6-digit code required")
        # Look up secret from server-side storage (never in the token)
        tok_secret = get_totp_setup(setup_token, user["id"])
        if not tok_secret:
            return self._error(400, "invalid or expired setup, try again")
        # Verify TOTP code against the stored secret
        counter = verify_totp(tok_secret, code)
        if counter is None:
            return self._error(400, "invalid code, try again")
        # Consume the setup token and persist the secret
        consume_totp_setup(setup_token)
        db = get_db()
        # TOTP shared secret is a long-lived credential — encrypt before write.
        # ``decrypt_at_rest`` returns legacy plaintext unchanged, so any rows
        # written by older releases continue to verify until the user
        # re-enrolls (or runs through the rotate-on-read code path).
        db.execute(
            "UPDATE users SET totp_secret=?, totp_enabled=1, last_totp_counter=? WHERE id=?",
            (encrypt_at_rest(tok_secret, "users.totp_secret"), counter, user["id"]),
        )
        db.commit()
        self._json_response(200, {"ok": True})

    def _totp_disable(self, body):
        """Disable 2FA after verifying current TOTP code."""
        user = self._require_auth()
        if not user:
            return
        code = (body.get("code") or "").strip()
        db = get_db()
        row = db.execute("SELECT totp_secret,totp_enabled FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not row["totp_enabled"]:
            return self._error(400, "2FA not enabled")
        stored_secret = decrypt_at_rest(row["totp_secret"], "users.totp_secret")
        if not stored_secret:
            return self._error(400, "2FA not enabled")
        counter = verify_totp(stored_secret, code)
        if counter is None:
            return self._error(400, "invalid code")
        db.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (user["id"],))
        db.commit()
        self._json_response(200, {"ok": True})

    def _totp_login(self, body):
        """Complete 2FA login with partial token + TOTP code."""
        if not self._check_csrf():
            return
        ip = self._client_ip()
        if not check_rate_limit(ip):
            return self._error(429, "too many attempts, try again later")
        partial = body.get("partial_token") or ""
        code = (body.get("code") or "").strip()
        user_id = verify_partial_token(partial)
        if not user_id:
            return self._error(401, "expired or invalid token, please log in again")
        if not code or len(code) != 6:
            return self._error(400, "6-digit code required")
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT totp_secret,username,display_name,is_admin,last_totp_counter FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return self._error(401, "invalid or reused code")
            stored_secret = decrypt_at_rest(row["totp_secret"], "users.totp_secret")
            if not stored_secret:
                db.execute("ROLLBACK")
                return self._error(401, "invalid or reused code")
            counter = verify_totp(stored_secret, code)
            if counter is None or counter <= (row["last_totp_counter"] or 0):
                db.execute("ROLLBACK")
                return self._error(401, "invalid or reused code")
            db.execute("UPDATE users SET last_totp_counter=? WHERE id=?", (counter, user_id))
            db.execute("COMMIT")
        except Exception as e:
            db.execute("ROLLBACK")
            log.error(f"TOTP login error: {e}", exc_info=True)
            return self._error(500, "login error")
        token = create_session(db, user_id)
        clear_rate_limit(ip)  # Clear rate limit after full 2FA success
        user = {"id": user_id, "username": row["username"], "display_name": row["display_name"], "is_admin": row["is_admin"]}
        self._json_response_with_cookie(200, {"user": user}, cookie_token=token)

    # --- User Settings API (H4: server-side secrets) ---

    ALLOWED_SETTINGS = {"lm_apikey", "remote_mcps"}

    def _get_settings(self):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute("SELECT key, value FROM user_settings WHERE user_id=?", (user["id"],)).fetchall()
        result = {}
        for key, value in rows:
            if key == "lm_apikey":
                # Decrypt the stored token only to test whether it's set —
                # the boolean is the only thing exposed to the client.
                plain = decrypt_at_rest(value, "user_settings.lm_apikey")
                result[key] = bool(plain)
            elif key == "remote_mcps":
                plain = decrypt_at_rest(value, "user_settings.remote_mcps")
                try:
                    mcps = json.loads(plain) if plain else []
                    result[key] = [{"label": m.get("label", ""), "url": m.get("url", ""),
                                    "on": m.get("on", True), "has_auth": bool(m.get("auth")),
                                    "allowed_tools": m.get("allowed_tools") or []}
                                   for m in mcps]
                except (json.JSONDecodeError, TypeError):
                    result[key] = []
            else:
                result[key] = value
        self._json_response(200, result)

    def _save_settings(self, body):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        saved = {}
        for key, value in body.items():
            if key not in self.ALLOWED_SETTINGS:
                continue
            if key == "lm_apikey":
                str_val = str(value).strip() if value else ""
                stored = encrypt_at_rest(str_val, "user_settings.lm_apikey")
                db.execute(
                    "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                    (user["id"], key, stored),
                )
                saved[key] = bool(str_val)
            elif key == "remote_mcps":
                existing = []
                row = db.execute(
                    "SELECT value FROM user_settings WHERE user_id=? AND key=?",
                    (user["id"], key),
                ).fetchone()
                if row and row[0]:
                    plain = decrypt_at_rest(row[0], "user_settings.remote_mcps")
                    try:
                        existing = json.loads(plain) if plain else []
                    except (json.JSONDecodeError, TypeError):
                        existing = []
                existing_auth = {(m.get("label"), m.get("url")): m.get("auth", "")
                                 for m in existing}
                new_mcps = []
                for m in (value if isinstance(value, list) else []):
                    entry = {"label": m.get("label", ""), "url": m.get("url", ""),
                             "on": m.get("on", True)}
                    if "auth" in m:
                        entry["auth"] = m["auth"]
                    else:
                        entry["auth"] = existing_auth.get(
                            (entry["label"], entry["url"]), "")
                    # Optional per-integration tool whitelist — empty list
                    # / missing means "all tools the server exposes."
                    at = m.get("allowed_tools") or []
                    if isinstance(at, list):
                        entry["allowed_tools"] = [str(t) for t in at if t]
                    new_mcps.append(entry)
                stored = encrypt_at_rest(
                    json.dumps(new_mcps), "user_settings.remote_mcps"
                )
                db.execute(
                    "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                    (user["id"], key, stored),
                )
                saved[key] = [{"label": m["label"], "url": m["url"],
                               "on": m["on"], "has_auth": bool(m.get("auth")),
                               "allowed_tools": m.get("allowed_tools") or []}
                              for m in new_mcps]
            else:
                str_val = str(value).strip() if value else ""
                db.execute(
                    "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                    (user["id"], key, str_val),
                )
                saved[key] = str_val
        db.commit()
        self._json_response(200, saved)

    def _get_user_lm_apikey(self, user_id):
        """Get stored LM Studio API key for a user (decrypts at-rest value).

        IMPORTANT: this is one of only THREE legitimate read paths for
        encrypted user_settings columns.  The others are
        ``_get_any_user_lm_apikey`` (server-internal contexts with no
        active user, like health-check) and ``_get_user_remote_mcps``.
        Every other read of ``user_settings.value`` for ``lm_apikey`` or
        ``remote_mcps`` is a bug — the stored value is ciphertext of
        format ``enc$v1$...`` and LM Studio rejects it as malformed.
        See: 2026-05-16 incident where the health-check read row[0]
        directly and shipped the envelope as a Bearer header.
        """
        db = get_db()
        row = db.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key='lm_apikey'",
            (user_id,),
        ).fetchone()
        if not row or not row[0]:
            return ""
        plain = decrypt_at_rest(row[0], "user_settings.lm_apikey")
        # ``None`` means tamper or wrong key — fail closed (no apikey).
        return plain or ""

    # =====================================================================
    # Embedding-model identity cache (Memory spec v3, Change 1).
    # Server-wide TTL cache for the currently-loaded LM Studio embedding
    # model id.  Each row in the ``embeddings`` table is tagged with the
    # model that produced its vector; retrieval filters by current model
    # so old vectors from a since-superseded model don't poison ranking.
    # Embedding-model selection is server-scoped, not per-user — a single
    # cache entry covers all users.
    # =====================================================================
    _embed_model_cache: tuple = (0.0, None)
    _embed_model_cache_lock: threading.Lock = threading.Lock()

    def _current_embedding_model_id(self) -> str | None:
        """Return the id of the currently-loaded embedding model.

        60-second TTL cache.  Returns ``None`` if LM Studio is offline or
        no embedding model is loaded.  On lookup failure, falls back to
        the last successful value so a flaky LM Studio doesn't wipe the
        cache mid-session.
        """
        now = time.monotonic()
        with Handler._embed_model_cache_lock:
            ts, cached = Handler._embed_model_cache
            if now - ts < 60.0:
                return cached
        try:
            token = self._get_any_user_lm_apikey()
            req = urllib.request.Request(f"{LMSTUDIO}/api/v1/models")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read())
        except Exception as e:
            # Promoted debug → warning in 0.5.9 sweep: this is a capability
            # degradation (memory semantic-recall falls back to text grep
            # without an embedding model), worth surfacing in default logs.
            log.warning(f"embedding model lookup failed: {e}")
            # Preserve last known value on transient failure.
            return Handler._embed_model_cache[1]
        chosen = None
        for m in payload.get("models", []) or []:
            if m.get("type") == "embedding" and (m.get("loaded_instances") or []):
                chosen = m.get("key") or m.get("id")
                break
        with Handler._embed_model_cache_lock:
            Handler._embed_model_cache = (now, chosen)
        return chosen

    def _get_any_user_lm_apikey(self):
        """Decrypted LM Studio API key for non-user contexts (health-check,
        startup probes).  Returns the env-var token first, else the first
        non-empty stored apikey decrypted on read.  Returns ``""`` if no
        token is available.  Centralised so every "any-user" lookup goes
        through ONE decrypt path — no more raw row[0] leaks.
        """
        if LMSTUDIO_TOKEN:
            return LMSTUDIO_TOKEN
        try:
            db = get_db()
            row = db.execute(
                "SELECT value FROM user_settings "
                "WHERE key='lm_apikey' AND value != '' LIMIT 1"
            ).fetchone()
        except Exception as e:
            log.warning(f"any-user lm_apikey lookup failed: {e}")
            return ""
        if not row or not row[0]:
            return ""
        return decrypt_at_rest(row[0], "user_settings.lm_apikey") or ""

    def _get_user_remote_mcps(self, user_id):
        """Get stored remote MCP configs (with auth) for a user.  Decrypts at-rest."""
        db = get_db()
        row = db.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key='remote_mcps'",
            (user_id,),
        ).fetchone()
        if not row or not row[0]:
            return []
        plain = decrypt_at_rest(row[0], "user_settings.remote_mcps")
        if not plain:
            return []
        try:
            return json.loads(plain)
        except (json.JSONDecodeError, TypeError):
            return []

    def _inject_mcp_auth(self, integrations, user_id):
        """Inject server-side auth headers into ephemeral_mcp integrations."""
        # Short-circuit: skip DB lookup if no ephemeral MCPs in the list
        if not any(isinstance(i, dict) and i.get("type") == "ephemeral_mcp" for i in integrations):
            return integrations
        stored_mcps = self._get_user_remote_mcps(user_id)
        auth_lookup = {(m.get("label"), m.get("url")): m.get("auth", "")
                       for m in stored_mcps}
        result = []
        for item in integrations:
            if isinstance(item, dict) and item.get("type") == "ephemeral_mcp":
                key = (item.get("server_label"), item.get("server_url"))
                auth = auth_lookup.get(key, "")
                new_item = {k: v for k, v in item.items() if k != "headers"}
                if auth:
                    new_item["headers"] = {"Authorization": auth}
                result.append(new_item)
            else:
                result.append(item)
        return result

    # --- Chat API ---

    def _handle_chat(self, body):
        user = self._require_auth()
        if not user:
            return
        if len(body.get("model", "")) > MODEL_MAX_LENGTH:
            return self._error(400, f"model too long (max {MODEL_MAX_LENGTH})")

        body["integrations"] = self._inject_mcp_auth(
            body.get("integrations", []), user["id"])

        self._resolve_chat_settings(body.get("chat_id"), user["id"], body)
        payload = self._build_lmstudio_payload(body)

        try:
            data = self._lmstudio_chat(payload, user["id"], timeout=300)

            chat_id = body.get("chat_id")
            is_incognito = body.get("incognito", False)
            if chat_id and not is_incognito:
                db = get_db()
                if not self._verify_chat_owner(db, chat_id, user["id"]):
                    return
                # Extract tool calls from output
                tool_calls = []
                if isinstance(data.get("output"), list):
                    for item in data["output"]:
                        if item.get("type") == "tool_call":
                            tool_calls.append(item)
                content = self._extract_content(data)
                self._persist_chat_messages(
                    db, chat_id, body.get("input", ""), content, tool_calls,
                    data.get("response_id"), data.get("usage") or {},
                )
            self._json_response(200, data)
        except urllib.error.HTTPError as e:
            # Wrap upstream errors in a JSON envelope.  Real LM Studio returns
            # JSON, but a misconfigured proxy or a generic 5xx page could send
            # HTML — we still set Content-Type: application/json upstream, so
            # forward unwrapped bytes would lie about the body shape and the
            # SPA's error parser would choke.  Truncate at 500 chars to keep
            # giant HTML error pages from blowing up the response.
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                upstream = json.loads(err_body)
                if isinstance(upstream, dict) and isinstance(upstream.get("error"), dict):
                    msg = upstream["error"].get("message") or err_body
                elif isinstance(upstream, dict) and "error" in upstream:
                    msg = upstream.get("error") or err_body
                else:
                    msg = err_body
            except (json.JSONDecodeError, ValueError):
                msg = err_body[:500]
            self._json_response(e.code, {"error": f"LM Studio error: {msg}"})
        except Exception as e:
            log.error(f"chat completion: {e}")
            self._error(502, "upstream service unavailable")

    def _inject_memory(self, user: dict, system_prompt: str, is_incognito: bool) -> tuple:
        """Inject user memory insights into system_prompt.
        Returns (enriched_prompt, injected_ids).
        """
        if is_incognito:
            return system_prompt, []
        injected_ids = []
        try:
            mem_db = get_db()
            mem_enabled = mem_db.execute(
                "SELECT value FROM user_settings WHERE user_id=? AND key='memory_enabled'",
                (user["id"],)
            ).fetchone()
            if mem_enabled is None or mem_enabled[0] != "false":
                max_inject_row = mem_db.execute(
                    "SELECT value FROM user_settings WHERE user_id=? AND key='memory_max_inject'",
                    (user["id"],)
                ).fetchone()
                try:
                    max_inject = int(max_inject_row[0]) if max_inject_row and max_inject_row[0] else 30
                except (ValueError, TypeError):
                    max_inject = 30
                insights = self._get_top_insights(mem_db, user["id"], limit=max_inject)
                injected_ids = [i["id"] for i in insights]
                if insights:
                    memory_block = self._format_insights_for_prompt(insights)
                    system_prompt = (system_prompt + "\n\n" + memory_block) if system_prompt else memory_block
                    self._touch_insights(mem_db, [i["id"] for i in insights])
        except Exception as e:
            log.error(f"memory injection: {e}")
        return system_prompt, injected_ids

    def _collect_stream(self, resp, is_incognito: bool, chat_id: str = "") -> tuple:
        """Proxy SSE stream to client, collecting data for persistence.
        Returns (content_parts, reasoning_parts, tool_calls, response_id, stream_usage, stream_complete).

        When ``chat_id`` is non-empty AND we are not in incognito mode, the
        ``response_id`` carried by ``chat.end`` is committed to
        ``chats.response_id`` inline — before the rest of the message is
        persisted — so a post-chat.end exception (resp.close raising,
        thread-cleanup error, etc.) cannot break the conversation chain.
        Losing the message body is recoverable (the user re-asks); losing
        the chain ID is not, because LM Studio's response-store keeps
        moving forward.
        """
        content_parts = []
        reasoning_parts = []
        tool_calls = []
        # Open tool calls, in arrival order.  A single-variable
        # ``current_tool`` worked for serial wire format (which is all
        # current LM Studio emits per /tmp/probe-tool2.txt) but would
        # corrupt state under any future interleave — 0.3.19's changelog
        # says "Improved handling of parallel tool calls via the
        # streaming API" but does not promise serial events, and a $0
        # defensive structure here protects us if LM Studio interleaves
        # later.  Match events to opens by ``data.id`` when present;
        # fall back to most-recent-open for the no-id wire format we see
        # today.
        open_tools: list = []
        def _match_open(tc_id):
            if tc_id:
                for t in reversed(open_tools):
                    if t.get("id") == tc_id:
                        return t
            return open_tools[-1] if open_tools else None
        def _ensure_open(tc_id):
            t = _match_open(tc_id)
            if t is None:
                t = {"id": tc_id or "", "tool": "", "arguments": "",
                     "output": None, "provider": ""}
                open_tools.append(t)
            return t
        response_id = None
        event_type = ""
        stream_usage = {}
        stream_complete = False

        lines_seen = 0
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace")
                self.wfile.write(raw_line)
                self.wfile.flush()
                lines_seen += 1

                stripped = line.strip()
                if stripped.startswith("event:"):
                    event_type = stripped[6:].strip()
                elif stripped.startswith("data:"):
                    data_str = stripped[5:].strip()
                    try:
                        data = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        data = {}

                    if event_type != "message.delta" and not is_incognito:
                        # Tool-call events: log the full data payload so we
                        # can see exactly what LM Studio is emitting per
                        # call — sizes are tiny and this is the only way
                        # to debug tool-rendering issues from the logs
                        # without replaying the whole stream.
                        if event_type.startswith("tool_call."):
                            log.info(f"SSE {event_type}: {data!r}")
                        else:
                            log.debug(f"SSE {event_type}")

                    if event_type == "message.delta":
                        content_parts.append(data.get("content") or "")
                    elif event_type == "reasoning.delta":
                        reasoning_parts.append(data.get("content") or "")
                    # Tool-call events — real LM Studio wire shape (verified
                    # live 2026-05-15 against qwen3.6 + searxng MCP):
                    #   tool_call.start     {"type":"tool_call.start"}
                    #     — empty marker; one such event per tool call.
                    #   tool_call.name      {"tool_name":"…","provider_info":{
                    #                          "type":"plugin","plugin_id":"…"}}
                    #     — sets the tool name (the "id" we previously hoped
                    #     for on tool_call.start doesn't exist in current
                    #     LM Studio).
                    #   tool_call.arguments {"tool":"…","arguments":{…},
                    #                        "provider_info":{…}}
                    #     — COMPLETE arguments snapshot per call (dict, not
                    #     a string delta); same call can fire several times
                    #     before success when the model refines its plan,
                    #     but each event carries the final args for its call.
                    #   tool_call.success   {"tool":"…","arguments":{…},
                    #                        "output":"…","provider_info":{…}}
                    #     — full record at completion; we use these as the
                    #     authoritative source for the persisted call.
                    #   tool_call.failure   {"tool":"…","arguments":{…},
                    #                        "error":"…","provider_info":{…}}
                    elif event_type == "tool_call.start":
                        open_tools.append({
                            "id":     data.get("id") or "",
                            "tool":   data.get("tool") or data.get("tool_name") or "",
                            "arguments": "",
                            "output": None,
                            "provider": "",
                        })
                    elif event_type == "tool_call.name":
                        t = _ensure_open(data.get("id"))
                        t["tool"] = data.get("tool_name") or t["tool"]
                        prov = data.get("provider_info") or {}
                        if isinstance(prov, dict):
                            t["provider"] = prov.get("plugin_id") or ""
                    elif event_type == "tool_call.arguments":
                        # ``arguments`` is opaque: current LM Studio sends a
                        # dict snapshot per event; older builds and the
                        # 0.3.17 changelog ("tokens streamed as generated")
                        # suggest some models stream string deltas instead.
                        # If the running buffer is a string AND the incoming
                        # field is a string, append (delta semantics); in
                        # every other case overwrite (snapshot semantics).
                        # ``argumentsDelta`` covers an older field name.
                        t = _ensure_open(data.get("id"))
                        incoming = data.get("arguments")
                        if incoming is None:
                            incoming = data.get("argumentsDelta")
                        if isinstance(incoming, str) and isinstance(t.get("arguments"), str) and t["arguments"]:
                            t["arguments"] = t["arguments"] + incoming
                        elif incoming is not None:
                            t["arguments"] = incoming
                        if data.get("tool") and not t.get("tool"):
                            t["tool"] = data["tool"]
                    elif event_type == "tool_call.success":
                        t = _ensure_open(data.get("id"))
                        # Final record carries everything — prefer its fields
                        # over the running state when present.
                        if data.get("tool"):
                            t["tool"] = data["tool"]
                        if data.get("arguments") is not None:
                            t["arguments"] = data["arguments"]
                        t["output"] = data.get("output")
                        try:
                            open_tools.remove(t)
                        except ValueError:
                            pass
                        tool_calls.append(t)
                    elif event_type == "tool_call.failure":
                        t = _ensure_open(data.get("id"))
                        if data.get("tool"):
                            t["tool"] = data["tool"]
                        if data.get("arguments") is not None:
                            t["arguments"] = data["arguments"]
                        t["output"] = data.get("error") or "Tool call failed"
                        try:
                            open_tools.remove(t)
                        except ValueError:
                            pass
                        tool_calls.append(t)
                    elif event_type == "chat.end":
                        result = data.get("result", {})
                        response_id = result.get("response_id")
                        stats = result.get("stats", {})
                        stream_usage = data.get("usage") or {
                            "input_tokens": stats.get("input_tokens", 0),
                            "output_tokens": stats.get("total_output_tokens", 0),
                        }
                        stream_complete = True
                        # Commit the chain ID immediately so a later
                        # exception in this function or its caller cannot
                        # orphan the upstream chain.  ``_persist_chat_messages``
                        # will redundantly UPDATE the same column at the
                        # end of the request; that no-op is the cost of
                        # this defence.
                        if response_id and chat_id and not is_incognito:
                            try:
                                _db_resp = get_db()
                                _db_resp.execute(
                                    "UPDATE chats SET response_id=?, updated_at=? WHERE id=?",
                                    (response_id, time.time(), chat_id),
                                )
                                _db_resp.commit()
                            except Exception as _e:
                                log.warning(
                                    f"could not eagerly persist response_id "
                                    f"for chat {chat_id!r}: {_e}"
                                )
                        if not is_incognito:
                            log.debug(f"RESP chat.end resp_id={response_id} usage={stream_usage}")
                            log.debug(f"RESP content: [{len(''.join(content_parts))} chars]")
                            if tool_calls:
                                log.debug(f"RESP tools: {len(tool_calls)} calls")
                elif stripped and lines_seen <= 5 and not is_incognito:
                    # Log first few unrecognised lines to diagnose unexpected LM Studio responses
                    log.warning(f"SSE unexpected line: {stripped[:200]!r}")
        except (BrokenPipeError, ConnectionResetError):
            log.debug("Stream aborted by client")
        except TimeoutError:
            log.warning("Stream timed out waiting for LM Studio response")
        except OSError as e:
            log.warning(f"Stream OS error: {e}")
        except Exception as e:
            log.error(f"Stream unexpected error: {e}", exc_info=True)
        finally:
            # ``resp.close()`` is the last action in the only finally
            # block on this path; if it ever raised it would propagate
            # past every except above and out of this function, defeating
            # the post-loop persistence in the caller.  Swallow.
            try:
                resp.close()
            except Exception:
                pass

        if not stream_complete and not is_incognito:
            log.warning(f"Stream ended without chat.end (lines_seen={lines_seen})")

        return content_parts, reasoning_parts, tool_calls, response_id, stream_usage, stream_complete

    def _handle_chat_stream(self, body):
        user = self._require_auth()
        if not user:
            return
        # Stream-specific rate limiting (stricter than general API)
        if not _stream_limiter.allow(f"stream:{user['id']}"):
            return self._error(429, "too many requests — please wait")
        if len(body.get("model", "")) > MODEL_MAX_LENGTH:
            return self._error(400, f"model too long (max {MODEL_MAX_LENGTH})")

        # Incognito mode: skip persistence, logging, and memory
        is_incognito = body.get("incognito", False)

        # Verify chat ownership before streaming
        chat_id = body.get("chat_id")
        db = get_db() if chat_id else None
        if chat_id and AUTH_ENABLED:
            if not self._verify_chat_owner(db, chat_id, user["id"]):
                return

        # Send SSE headers before memory injection so SC/CoVe can emit status events
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._send_security_headers()
        self.end_headers()

        def emit_status(text):
            msg = f"event: status\ndata: {json.dumps({'text': text})}\n\n"
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass

        # Context: LM Studio manages history via response_id chaining.
        # Summary injection is redundant (model sees summary AND full history).
        # Manual /compact still works via _compact_chat for explicit user action.
        system_prompt = body.get("system_prompt") or ""

        # Memory: inject user insights into system prompt (skip in incognito)
        system_prompt, injected_insight_ids = self._inject_memory(user, system_prompt, is_incognito)
        # Instruction sandwich: append core instruction reminder at end of system prompt.
        # Local LLMs (Qwen, Llama, Mistral) have strong recency bias — instructions at
        # the end of the system prompt get more attention than those in the middle.
        if system_prompt:
            system_prompt += "\n\n## REMINDERS\n- Lead with the answer, then elaborate.\n- Use your tools when they add value.\n- When uncertain about a fact, say so clearly rather than guessing. It is better to be honest about uncertainty than to sound confident and be wrong."

        body["integrations"] = self._inject_mcp_auth(
            body.get("integrations", []), user["id"])

        self._resolve_chat_settings(body.get("chat_id"), user["id"], body)
        payload = self._build_lmstudio_payload(body, system_prompt=system_prompt or None, stream=True)

        # SC/CoVe interception
        sc_enabled = body.get("sc_enabled") or False
        cove_enabled = body.get("cove_enabled") or False

        if sc_enabled or cove_enabled:
            result = None
            try:
                if cove_enabled and sc_enabled:
                    cove_result = self._chain_of_verification(payload, user["id"], emit_status)
                    if isinstance(cove_result, dict):
                        result = self._self_consistency(cove_result, user["id"], emit_status)
                    else:
                        result = cove_result
                elif cove_enabled:
                    result = self._chain_of_verification(payload, user["id"], emit_status)
                elif sc_enabled:
                    result = self._self_consistency(payload, user["id"], emit_status)
            except Exception as e:
                log.error(f"SC/CoVe failed: {e}")
                result = None

            if isinstance(result, str) and not result.strip():
                result = None
            if isinstance(result, str):
                text = result
                delta = json.dumps({"content": text})
                try:
                    self.wfile.write(f"event: message.delta\ndata: {delta}\n\n".encode())
                    self.wfile.write(b"event: chat.end\ndata: {}\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                if chat_id and not is_incognito:
                    db = get_db()
                    self._persist_chat_messages(
                        db, chat_id,
                        user_input=body.get("input", ""),
                        content=text,
                        tool_calls=[],
                        response_id=None,
                        usage={},
                    )
                return
            elif isinstance(result, dict):
                payload = result
            # None: fall through to normal request

        # Debug logging (skip content in incognito mode)
        if is_incognito:
            log.debug(f"REQ [incognito] model={payload.get('model')}")
        else:
            log.debug(f"REQ model={payload.get('model')} stream={payload.get('stream')} ctx={payload.get('context_length')} prev_resp_id={payload.get('previous_response_id')}")
            log.debug(f"REQ system_prompt: [{len(payload.get('system_prompt') or '')} chars]")
            inp = payload.get('input', '')
            log.debug(f"REQ input: [{len(str(inp)) if isinstance(inp, list) else len(inp or '')} chars]")
            params = {k: payload[k] for k in ('temperature','top_p','top_k','min_p','repeat_penalty','max_output_tokens','reasoning') if k in payload}
            if params:
                log.debug(f"REQ params: {params}")

        headers = {"Content-Type": "application/json"}
        token = self._get_lmstudio_token(user["id"])
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(
            f"{LMSTUDIO}/api/v1/chat",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )

        # Open LM Studio connection in a background thread so the main thread can
        # send SSE keep-alive comments every 10 s.  Without this, the 30-second
        # Handler.timeout fires on the idle client socket while LM Studio spends
        # 18-24 s connecting its MCP integrations before sending the first byte.
        # [resp, exc] — mutable slot the background thread writes into.
        # Annotated as ``list[Any]`` so the type checker accepts the
        # Exception or response-object assignments below.
        _result: list = [None, None]
        _ready  = threading.Event()

        def _do_open():
            try:
                _result[0] = urllib.request.urlopen(req, timeout=300)
            except Exception as exc:
                _result[1] = exc
            _ready.set()

        threading.Thread(target=_do_open, daemon=True).start()

        while not _ready.wait(timeout=10):
            try:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
            except Exception:
                return  # client disconnected while waiting

        exc = _result[1]
        if exc is not None:
            if isinstance(exc, urllib.error.HTTPError):
                err_body = exc.read().decode("utf-8", errors="replace")
                try:
                    err_obj = json.loads(err_body).get("error", {})
                    if not isinstance(err_obj, dict):
                        err_obj = {"message": str(err_obj)}
                except Exception:
                    err_obj = {"message": err_body}
                err_detail = err_obj.get("message") or err_body
                # Universal "model rejects this param" detection.  LM Studio
                # returns the offending parameter name in ``error.param`` and
                # the message phrasing "does not (support|expose) ...
                # configuration" — that combination signals the model has no
                # API surface for the param at all (as opposed to "value out
                # of range" which uses the same ``code: invalid_value`` but
                # different message wording).
                #
                # Verified shapes against LM Studio (2026-05):
                #   {"error": {"param": "reasoning", "code": "invalid_value",
                #              "message": "Model '...' does not expose reasoning configuration."}}
                # Older builds may omit ``param``; we fall back to a known
                # message-substring list keyed by param name.
                err_lower = err_detail.lower() if isinstance(err_detail, str) else ""
                err_code = err_obj.get("code") if isinstance(err_obj.get("code"), str) else ""
                bad_param = err_obj.get("param") if isinstance(err_obj.get("param"), str) else None
                if not bad_param:
                    # Legacy phrasings without ``error.param``.  Extend this
                    # map as new ones surface — keep the mapping centralised
                    # so the retry stays universal.
                    legacy_patterns = {
                        "reasoning": ("does not support reasoning", "does not expose reasoning"),
                        "previous_response_id": ("could not find stored response", "previous_response_not_found"),
                    }
                    for p, markers in legacy_patterns.items():
                        if any(marker in err_lower for marker in markers):
                            bad_param = p
                            break
                # Two distinct "drop the param and retry" scenarios from LM Studio:
                #
                #   (a) model_unsupports_param — model rejects the param
                #       entirely (e.g. ``reasoning`` on a fixed-depth model).
                #       Recovery: cache (model, param) so we strip it up
                #       front on every subsequent request.
                #
                #   (b) ephemeral_param_evicted — server-side state the
                #       param references is no longer available (e.g.
                #       ``previous_response_id`` after LM Studio's response
                #       store evicted the chain).  Recovery: strip the param
                #       AND clear our stored copy so subsequent turns don't
                #       keep sending the same dead reference.  Verified
                #       shape: ``{"param":"previous_response_id","code":"previous_response_not_found"}``
                #       per lmstudio-ai/lmstudio-bug-tracker#1188.
                model_unsupports_param = (
                    exc.code == 400
                    and bad_param is not None
                    and bad_param in payload
                    and ("does not support" in err_lower or "does not expose" in err_lower
                         or "not configurable" in err_lower)
                )
                ephemeral_param_evicted = (
                    exc.code == 400
                    and bad_param is not None
                    and bad_param in payload
                    and (err_code == "previous_response_not_found"
                         or "not found" in err_lower
                         or "no longer available" in err_lower)
                )
                should_retry_without_param = False
                if model_unsupports_param and bad_param:
                    model_id = payload.get("model") or ""
                    if model_id:
                        with Handler._unsupported_params_lock:
                            Handler._unsupported_params.setdefault(
                                model_id, set()
                            ).add(bad_param)
                        # Persist so a restart doesn't re-learn this pair.
                        _persist_param_blacklist(model_id, bad_param, "reactive_400")
                        log.info(
                            f"unsupported_params: cached {{{model_id!r}: {bad_param!r}}} "
                            f"(LM Studio said: {err_detail!r})"
                        )
                    payload.pop(bad_param, None)
                    should_retry_without_param = True
                elif ephemeral_param_evicted and bad_param:
                    # Don't cache — the next turn's reference will be valid
                    # again.  But do clear our DB copy so we stop sending
                    # the dead one.
                    if bad_param == "previous_response_id" and chat_id and not is_incognito:
                        try:
                            _db_clear = get_db()
                            _db_clear.execute(
                                "UPDATE chats SET response_id=NULL WHERE id=?", (chat_id,),
                            )
                            _db_clear.commit()
                            log.info(
                                f"previous_response_id evicted from LM Studio "
                                f"for chat {chat_id!r}; cleared from DB and retrying without"
                            )
                        except Exception as e:
                            log.warning(
                                f"could not clear stale previous_response_id "
                                f"for {chat_id!r}: {e}"
                            )
                    payload.pop(bad_param, None)
                    should_retry_without_param = True
                if should_retry_without_param:
                    req2 = urllib.request.Request(
                        f"{LMSTUDIO}/api/v1/chat",
                        data=json.dumps(payload).encode(),
                        headers=headers,
                        method="POST",
                    )
                    _result2: list = [None, None]
                    _ready2  = threading.Event()
                    def _do_open2():
                        try:
                            _result2[0] = urllib.request.urlopen(req2, timeout=300)
                        except Exception as exc2:
                            _result2[1] = exc2
                        _ready2.set()
                    threading.Thread(target=_do_open2, daemon=True).start()
                    _ready2.wait(timeout=300)
                    if _result2[1] is None and _result2[0] is not None:
                        exc = None
                        _result[0] = _result2[0]
                    else:
                        exc = _result2[1] or Exception("retry failed")
                if exc is not None:
                    # exc.code only exists on HTTPError; narrow via isinstance
                    # so the type checker accepts the attribute access.
                    if isinstance(exc, urllib.error.HTTPError):
                        prefix = f"{exc.code}: "
                    else:
                        prefix = ""
                    err_msg = json.dumps({"type": "error", "error": {"message": f"LM Studio error {prefix}{err_detail}"}})
                else:
                    err_msg = ""  # retry succeeded — won't be written below
            else:
                log.error(f"chat stream: {exc}")
                err_msg = json.dumps({"type": "error", "error": {"message": "upstream service unavailable"}})
            if exc is not None:
                try:
                    self.wfile.write(f"event: error\ndata: {err_msg}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return

        resp = _result[0]

        try:
            content_parts, reasoning_parts, tool_calls, response_id, stream_usage, stream_complete = \
                self._collect_stream(resp, is_incognito, chat_id=body.get("chat_id") or "")
        except Exception as e:
            log.error(f"chat stream collect: {e}", exc_info=True)
            try:
                err_msg = json.dumps({"type": "error", "error": {"message": "Stream error — please try again"}})
                # Named event so app.js:processSSEBlock actually delivers it.
                # Bare data-only frames are silently dropped by the client.
                self.wfile.write(f"event: error\ndata: {err_msg}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return

        # If stream ended without chat.end (LM Studio didn't respond), tell the client
        if not stream_complete and not content_parts:
            try:
                err_msg = json.dumps({"type": "error", "error": {"message": "No response from model — it may be busy, crashed, or unable to reach a tool server"}})
                self.wfile.write(f"event: error\ndata: {err_msg}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

        # Only persist complete responses (skip if client disconnected mid-stream)
        chat_id = body.get("chat_id")
        if chat_id and stream_complete and not is_incognito:
            db = get_db()
            reasoning = "".join(reasoning_parts).strip()
            content = ("".join(content_parts))
            if reasoning:
                content = f"<think>{reasoning}</think>{content}"
            self._persist_chat_messages(
                db, chat_id, body.get("input", ""), content, tool_calls,
                response_id, stream_usage, injected_insight_ids=injected_insight_ids,
            )
        elif chat_id and not is_incognito:
            # Stream cut before chat.end — synthesise an "interrupted"
            # assistant row so the chat history reflects what happened.
            # Fires even when content_parts AND tool_calls are empty (LM
            # Studio timed out before any token); without that, a
            # zero-token cut leaves the user wondering whether their turn
            # was sent at all.  The SPA renders interrupted rows with a
            # Retry affordance regardless of whether content is present.
            try:
                db = get_db()
                reasoning = "".join(reasoning_parts).strip()
                content = ("".join(content_parts))
                if reasoning:
                    content = f"<think>{reasoning}</think>{content}"
                self._persist_chat_messages(
                    db, chat_id, body.get("input", ""), content, tool_calls,
                    response_id, stream_usage,
                    injected_insight_ids=injected_insight_ids,
                    assistant_status="interrupted",
                )
            except Exception as e:
                log.warning(f"failed to persist interrupted stream stub: {e}")

        # Index embeddings in background thread (truly non-blocking).  Fires
        # for BOTH completion paths above — normal stream_complete AND
        # interrupted — so semantic search has content to search regardless
        # of how the turn ended.  Previously buried inside the `elif`
        # interrupted branch, which meant normal completions never got
        # indexed and semantic search silently degraded to LIKE.
        if chat_id and not is_incognito:
            _embed_token = self._get_lmstudio_token(user["id"])
            def _index_embeddings(cid, tok2):
                try:
                    db2 = get_db()
                    # Tag every new embedding with the model that produced
                    # it.  Retrieval filters by current model so old
                    # vectors don't poison ranking after a model swap.
                    embed_model = self._current_embedding_model_id() or "unknown"
                    for role_to_embed in ("user", "assistant"):
                        rows = db2.execute(
                            "SELECT m.id, m.content FROM messages m LEFT JOIN embeddings e ON m.id = e.message_id WHERE m.chat_id=? AND m.role=? AND e.message_id IS NULL ORDER BY m.id DESC LIMIT 2",
                            (cid, role_to_embed)
                        ).fetchall()
                        for mid, emb_content in rows:
                            if emb_content:
                                vec = get_embedding(emb_content, tok2)
                                if vec:
                                    blob = struct.pack(f'{len(vec)}f', *vec)
                                    db2.execute("INSERT OR IGNORE INTO embeddings (message_id, vector, model_id) VALUES (?, ?, ?)", (mid, blob, embed_model))
                    db2.commit()
                except Exception:
                    log.debug("Background embedding indexing failed", exc_info=True)
            threading.Thread(target=_index_embeddings, args=(chat_id, _embed_token), daemon=True).start()

    def _create_chat(self, body):
        user = self._require_auth()
        if not user:
            return
        title = body.get("title", "New chat")
        model = body.get("model", "")
        if len(title) > TITLE_MAX_LENGTH:
            return self._error(400, f"title too long (max {TITLE_MAX_LENGTH})")
        if len(model) > MODEL_MAX_LENGTH:
            return self._error(400, f"model too long (max {MODEL_MAX_LENGTH})")
        db = get_db()
        chat_id = f"c{uuid.uuid4().hex[:12]}"
        now = time.time()
        db.execute(
            "INSERT INTO chats (id,title,model,updated_at,user_id) VALUES (?,?,?,?,?)",
            (chat_id, title, model, now, user["id"]),
        )
        db.commit()
        self._json_response(200, {"id": chat_id, "title": title})

    def _list_chats(self):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        where, params = self._user_filter(user)
        rows = db.execute(
            f"SELECT id,title,model,response_id,updated_at,pinned,folder FROM chats {where} ORDER BY pinned DESC, updated_at DESC",
            params,
        ).fetchall()
        chats = [{"id": r[0], "title": r[1], "model": r[2], "response_id": r[3], "updated_at": r[4], "pinned": r[5] or 0, "folder": r[6] or ""} for r in rows]
        self._json_response(200, chats)

    def _toggle_pin(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        row = db.execute("SELECT pinned FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            return self._error(404, "chat not found")
        new_val = 0 if row[0] else 1
        db.execute("UPDATE chats SET pinned=? WHERE id=?", (new_val, chat_id))
        db.commit()
        self._json_response(200, {"pinned": new_val})

    def _set_folder(self, chat_id, body):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        folder = str(body.get("folder", "")).strip()[:50]
        db.execute("UPDATE chats SET folder=? WHERE id=?", (folder, chat_id))
        db.commit()
        self._json_response(200, {"folder": folder})

    def _get_messages(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        rows = db.execute("""
            SELECT m.id, m.role, m.content, m.name, m.args, m.output, m.token_count,
                   m.status, mf.rating
            FROM messages m
            LEFT JOIN message_feedback mf ON m.id = mf.message_id AND mf.user_id = ?
            WHERE m.chat_id = ?
            ORDER BY m.created_at
        """, (user["id"], chat_id)).fetchall()
        msgs = []
        for r in rows:
            m = {"id": r[0], "role": r[1]}
            if r[2]: m["content"] = r[2]
            if r[3]: m["name"] = r[3]
            if r[4]:
                try: m["args"] = json.loads(r[4])
                except (json.JSONDecodeError, ValueError, TypeError): m["args"] = r[4]
            if r[5]:
                try: m["output"] = json.loads(r[5])
                except (json.JSONDecodeError, ValueError, TypeError): m["output"] = r[5]
            if r[6]: m["token_count"] = r[6]
            if r[7]: m["status"]   = r[7]  # NULL means "completed"
            if r[8] is not None: m["feedback"] = r[8]  # rating from message_feedback
            msgs.append(m)
        self._json_response(200, msgs)

    def _delete_last_response(self, chat_id):
        """Delete last assistant message + preceding tool calls, return last user content + previous response_id."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        rows = db.execute(
            "SELECT id,role,content FROM messages WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        ).fetchall()

        # Find last assistant, collect tool calls before it, find and delete the user message too
        # (resendText will re-insert the user message via /api/chat/stream)
        to_delete = []
        user_content = None
        for r in rows:
            mid, role, content = r
            if role == "assistant" and not to_delete:
                to_delete.append(mid)
            elif role == "tool" and to_delete and user_content is None:
                to_delete.append(mid)
            elif role == "user":
                user_content = content
                to_delete.append(mid)
                break
            else:
                break

        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", to_delete)
            db.execute("UPDATE chats SET response_id=NULL, updated_at=? WHERE id=?", (time.time(), chat_id))
        db.commit()
        self._json_response(200, {"user_content": user_content})

    def _truncate_messages(self, chat_id, body):
        """Delete all messages with id >= from_message_id."""
        user = self._require_auth()
        if not user:
            return
        from_id = body.get("from_message_id")
        if not from_id:
            self._error(400, "from_message_id required")
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("DELETE FROM messages WHERE chat_id=? AND id>=?", (chat_id, from_id))
        db.execute("UPDATE chats SET response_id=NULL, updated_at=? WHERE id=?", (time.time(), chat_id))
        db.commit()
        self._json_response(200, {"ok": True})

    def _fork_chat(self, source_id, body):
        user = self._require_auth()
        if not user:
            return
        up_to = body.get("up_to_message_id")
        if not up_to:
            self._error(400, "up_to_message_id required")
            return
        db = get_db()
        if not self._verify_chat_owner(db, source_id, user["id"]):
            return
        # Get source chat
        src = db.execute("SELECT title, response_id FROM chats WHERE id=?", (source_id,)).fetchone()
        if not src:
            self._error(404, "chat not found")
            return
        # Determine if we're forking at the last message; if so, carry the response_id
        last_msg = db.execute(
            "SELECT id FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (source_id,)
        ).fetchone()
        src_response_id = src[1] if (last_msg and last_msg[0] == up_to) else None
        # Create new chat
        new_id = f"c{uuid.uuid4().hex[:12]}"
        new_title = f"Fork: {src[0]}"
        now = time.time()
        db.execute(
            "INSERT INTO chats (id,title,model,response_id,updated_at,user_id) VALUES (?,?,?,?,?,?)",
            (new_id, new_title, body.get("model", ""), src_response_id, now, user["id"]),
        )
        # Copy messages up to and including the specified message id.
        # Filter ``status='interrupted'`` stubs — they're SPA-only UI rows
        # and forking them just propagates the broken-stream marker.
        rows = db.execute(
            "SELECT role,content,name,args,output,created_at FROM messages "
            "WHERE chat_id=? AND id<=? "
            "AND (status IS NULL OR status != 'interrupted') "
            "ORDER BY id",
            (source_id, up_to),
        ).fetchall()
        for r in rows:
            db.execute(
                "INSERT INTO messages (chat_id,role,content,name,args,output,created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id, r[0], r[1], r[2], r[3], r[4], r[5]),
            )
        db.commit()
        self._json_response(200, {"id": new_id, "title": new_title, "response_id": src_response_id})

    def _compact_chat(self, chat_id, body):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        # Fetch all messages — exclude ``status='interrupted'`` stubs so
        # the compacted summary doesn't include role=assistant/content=""
        # turns that would confuse LM Studio's downstream context build.
        msg_rows = db.execute(
            "SELECT id, role, content, name, args, output FROM messages "
            "WHERE chat_id=? AND (status IS NULL OR status != 'interrupted') "
            "ORDER BY id",
            (chat_id,),
        ).fetchall()
        db_messages = []
        for r in msg_rows:
            m = {"id": r[0], "role": r[1]}
            if r[2]: m["content"] = r[2]
            if r[3]: m["name"] = r[3]
            if r[4]: m["args"] = r[4]
            if r[5]: m["output"] = r[5]
            db_messages.append(m)

        # Exclude pinned messages from compaction — they must survive regardless of window
        pinned_ids = {r[0] for r in db.execute(
            "SELECT message_id FROM pins WHERE chat_id = ? AND message_id IS NOT NULL",
            (chat_id,)
        )}
        db_messages = [m for m in db_messages if m['id'] not in pinned_ids]
        # Recompute half from compactable messages only (pinned don't count against the window)

        turn_count = sum(1 for m in db_messages if m["role"] in ("user", "assistant"))
        if turn_count < COMPACT_MIN_TURNS:
            return self._error(400, f"Conversation too short to compact (need at least {COMPACT_MIN_TURNS} turns).")

        # Build conversation text for summarization
        convo_snippet = self._build_convo_snippet(db_messages, max_chars=COMPACT_MAX_CHARS)

        summary_payload = {
            "model": body.get("model", ""),
            "input": (
                "Summarize this entire conversation in 3-5 sentences. "
                "Capture the key topics, decisions, code discussed, and any important context "
                "that would be needed to continue the conversation:\n\n"
                + convo_snippet
            ),
            "stream": False,
            "store": False,
            "integrations": [],
        }
        try:
            sum_data = self._lmstudio_chat(summary_payload, user["id"], timeout=120)
            summary = self._extract_content(sum_data)
            if not summary:
                self._error(500, "Failed to generate summary.")
                return
            last_id = db_messages[-1]["id"]
            half = len(db_messages) // 2

            # Single transaction: update summary + delete compacted messages
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE chats SET summary=?, summary_up_to=? WHERE id=?",
                    (summary, last_id, chat_id),
                )
                ids_to_delete = [m["id"] for m in db_messages[:half]]
                if ids_to_delete:
                    placeholders = ",".join("?" * len(ids_to_delete))
                    db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids_to_delete)
                db.commit()
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                log.error(f"Compact failed: {e}")
                self._error(500, "compact failed")
                return

            # Distill insights from messages being compacted (non-blocking, separate)
            try:
                convo_for_distill = self._build_convo_snippet(db_messages[:half], max_chars=3000)
                existing = db.execute(
                    "SELECT content FROM user_insights WHERE user_id=? AND state='active'",
                    (user["id"],),
                ).fetchall()
                known = "\n".join(f"- {r[0]}" for r in existing) if existing else "(none yet)"

                raw = self._run_llm_distill(
                    self.DISTILL_PROMPT.format(
                        known_context=known, conversation=convo_for_distill,
                    ),
                    user["id"],
                    model=body.get("model", ""),
                    temperature=0.3,
                    timeout=30,
                )
                new = self._parse_and_store_insights(db, user["id"], raw, chat_id)
                if new:
                    log.debug(f"memory: distilled {len(new)} insights during compaction of {chat_id}")
            except Exception as e:
                log.warning(f"memory: compaction distillation failed (non-fatal): {e}")

            self._json_response(200, {
                "summary": summary,
                "messages_summarized": len(db_messages),
                "messages_deleted": half,
            })
        except Exception as e:
            log.error(f"Compact failed: {e}")
            self._error(500, "compact failed")

    def _update_title(self, chat_id, body):
        user = self._require_auth()
        if not user:
            return
        title = body.get("title", "")
        if len(title) > TITLE_MAX_LENGTH:
            return self._error(400, f"title too long (max {TITLE_MAX_LENGTH})")
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        db.commit()
        self._json_response(200, {"ok": True})

    def _delete_chat(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("DELETE FROM shared_chats WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM chats WHERE id=?", (chat_id,))  # messages cascade via FK
        db.commit()
        self._json_response(200, {"ok": True})

    # --- Share endpoints ---

    def _share_chat(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        # Check if already shared
        existing = db.execute("SELECT share_id FROM shared_chats WHERE chat_id=? AND user_id=?",
                              (chat_id, user["id"])).fetchone()
        if existing:
            return self._json_response(200, {"share_id": existing[0], "url": f"/share/{existing[0]}"})
        # Build message snapshot (text only — no system prompts, no base64
        # images, max 500 messages, no ``status='interrupted'`` stubs).
        rows = db.execute(
            "SELECT role, content, name, args, output FROM messages "
            "WHERE chat_id=? AND (status IS NULL OR status != 'interrupted') "
            "ORDER BY id LIMIT 500",
            (chat_id,)
        ).fetchall()
        snapshot = []
        for role, content, name, args, output in rows:
            if role == "system":
                continue
            msg = {"role": role}
            if content:
                # Strip base64 image data from content
                if isinstance(content, str) and content.startswith("data:image"):
                    msg["content"] = "[image]"
                else:
                    msg["content"] = content
            if name:
                msg["name"] = name
            if args:
                msg["args"] = args
            if output:
                msg["output"] = output
            snapshot.append(msg)
        # Get chat title
        chat_row = db.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
        title = chat_row[0] if chat_row else "Shared Chat"
        share_id = secrets.token_urlsafe(12)
        now = time.time()
        db.execute(
            "INSERT INTO shared_chats (share_id, chat_id, user_id, title, messages, created_at) VALUES (?,?,?,?,?,?)",
            (share_id, chat_id, user["id"], title, json.dumps(snapshot), now)
        )
        db.commit()
        self._json_response(200, {"share_id": share_id, "url": f"/share/{share_id}"})

    def _unshare_chat(self, chat_id):
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        if not self._verify_chat_owner(db, chat_id, user["id"]):
            return
        db.execute("DELETE FROM shared_chats WHERE chat_id=? AND user_id=?", (chat_id, user["id"]))
        db.commit()
        self._json_response(200, {"ok": True})

    def _serve_shared(self, share_id):
        """Serve a read-only shared conversation page. NO auth required."""
        if not share_id:
            return self._serve_share_404()
        db = get_db()
        row = db.execute("SELECT title, messages, created_at FROM shared_chats WHERE share_id=?",
                         (share_id,)).fetchone()
        if not row:
            return self._serve_share_404()
        title, messages_json, created_at = row
        messages = json.loads(messages_json)
        html = self._build_share_html(title, messages, created_at)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_security_headers(csp="default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; base-uri 'none'", referrer="no-referrer")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_share_404(self):
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Not Found — LM Chat</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,system-ui,sans-serif;background:#0B1117;color:#E2E8F0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.c{text-align:center}h1{font-size:48px;font-weight:700;color:#4A5568;margin-bottom:8px}p{color:#8494A7;font-size:15px}a{color:#C084FC;text-decoration:none}a:hover{text-decoration:underline}</style>
</head><body><div class="c"><h1>404</h1><p>This shared conversation doesn't exist or has been deleted.</p><p style="margin-top:12px"><a href="/">Go to LM Chat</a></p></div></body></html>"""
        data = html.encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_security_headers(csp="default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'", referrer="no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _build_share_html(self, title, messages, created_at):
        date_str = datetime.fromtimestamp(created_at).strftime("%B %d, %Y")
        # HTML-escape the title
        safe_title = html_mod.escape(title)
        msg_html = []
        def _md(text):
            """Minimal markdown: bold, italic, inline code, code blocks."""
            s = html_mod.escape(text)
            # Fenced code blocks: ```...```
            s = re.sub(r'```(\w*)\n(.*?)```', lambda m: f'<pre><code>{m.group(2)}</code></pre>', s, flags=re.DOTALL)
            # Inline code: `...`
            s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
            # Bold: **...**
            s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
            # Italic: *...*
            s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
            # Paragraphs and line breaks
            s = s.replace("\n\n", "</p><p>").replace("\n", "<br>")
            return s
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "") or ""
            safe = _md(content)
            if role == "user":
                msg_html.append(f'<div class="msg user"><div class="role">You</div><div class="content"><p>{safe}</p></div></div>')
            elif role == "assistant":
                msg_html.append(f'<div class="msg asst"><div class="role">Assistant</div><div class="content"><p>{safe}</p></div></div>')
            elif role == "tool":
                name = html_mod.escape(m.get("name") or "tool")
                output = m.get("output", "") or ""
                safe_output = _md(output)
                if safe_output:
                    msg_html.append(f'<div class="msg tool"><div class="role">Tool: {name}</div><div class="content tool-out"><p>{safe_output}</p></div></div>')
                else:
                    msg_html.append(f'<div class="msg tool"><div class="role">Tool: {name}</div></div>')

        return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title} — LM Chat</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,'Segoe UI',sans-serif;background:#0B1117;color:#E2E8F0;line-height:1.65;padding:2rem 1rem}}
.container{{max-width:720px;margin:0 auto}}
h1{{font-size:1.5rem;font-weight:600;margin-bottom:4px;color:#E2E8F0}}
.meta{{font-size:0.8rem;color:#8494A7;margin-bottom:1.75rem}}
.msg{{margin-bottom:1rem;padding:0.875rem 1.125rem;border-radius:10px;border:1px solid rgba(255,255,255,.06)}}
.msg.user{{background:#131B24}}
.msg.asst{{background:#1B2530}}
.msg.tool{{background:#0D1319;font-size:0.75rem;color:#8494A7}}
.role{{font-size:0.6875rem;text-transform:uppercase;letter-spacing:.6px;color:#8494A7;margin-bottom:5px;font-weight:600}}
.content p{{margin-bottom:0.5rem}}
.content p:last-child{{margin-bottom:0}}
.tool-out{{font-size:0.75rem;color:#8494A7;margin-top:4px}}
pre{{background:#0D1319;padding:0.75rem;border-radius:8px;overflow-x:auto;font-size:0.8rem;margin:0.5rem 0;border:1px solid rgba(255,255,255,.06)}}
code{{font-family:'SF Mono',Menlo,Consolas,monospace}}
.footer{{margin-top:2rem;padding-top:1rem;border-top:1px solid rgba(255,255,255,.06);font-size:0.75rem;color:#4A5568;text-align:center}}
a{{color:#C084FC;text-decoration:none}}a:hover{{text-decoration:underline}}
</style>
</head><body>
<div class="container">
<h1>{safe_title}</h1>
<div class="meta">Shared on {date_str} via LM Chat</div>
{''.join(msg_html)}
<div class="footer">Shared from <a href="/">LM Chat</a> — local AI, your way</div>
</div>
</body></html>"""

    def _search_messages(self, body):
        user = self._require_auth()
        if not user:
            return
        query = body.get("query", "").strip()
        if not query:
            return self._error(400, "query required")

        tok = self._get_lmstudio_token(user["id"])
        query_vec = get_embedding(query, tok)
        if not query_vec:
            # Embedding endpoint returned nothing — could be no model loaded,
            # wrong token, or LM Studio unreachable.  Surface the degradation
            # so the SPA can warn instead of silently treating LIKE as semantic.
            return self._search_messages_like(
                user, query,
                fallback_reason="embedding endpoint failed — verify an embedding model is loaded in LM Studio",
            )

        db = get_db()
        where, params = self._user_filter(user)
        # Filter embeddings by current model so vectors from a previous
        # model don't mix with the query's space — cosine across spaces
        # is mathematically meaningless.  Legacy NULL / "unknown" rows
        # are excluded until the user opts into /api/insights/reindex.
        current_model = self._current_embedding_model_id()
        if not current_model:
            # /api/v1/models reports no embedding-type model — fall back to
            # LIKE search and tell the SPA why.
            return self._search_messages_like(
                user, query,
                fallback_reason="no embedding model loaded in LM Studio",
            )
        rows = db.execute(f"""
            SELECT e.message_id, e.vector, m.content, m.role, m.chat_id, chats.title
            FROM embeddings e
            JOIN messages m ON e.message_id = m.id
            JOIN chats ON m.chat_id = chats.id
            {where}
            AND e.model_id = ?
            ORDER BY m.created_at DESC
            LIMIT ?
        """, (*params, current_model, SEARCH_MAX_RESULTS)).fetchall()
        results = []
        q_norm = math.sqrt(sum(x*x for x in query_vec))
        for mid, blob, content, role, chat_id, title in rows:
            vec = list(struct.unpack(f'{len(blob)//4}f', blob))
            dot = sum(a*b for a, b in zip(query_vec, vec))
            v_norm = math.sqrt(sum(x*x for x in vec))
            sim = dot / (q_norm * v_norm) if q_norm and v_norm else 0
            results.append({"message_id": mid, "score": round(sim, 4), "content": (content or "")[:200], "role": role, "chat_id": chat_id, "chat_title": title})

        results.sort(key=lambda x: x["score"], reverse=True)
        results = results[:20]  # cap semantic results before appending pins

        # Also search pins FTS
        try:
            pin_rows = db.execute("""
                SELECT p.id, p.pin_title, p.chat_title, p.pinned_at,
                       substr(p.content, 1, 300) as preview, p.chat_id, p.message_id
                FROM pins p
                JOIN (SELECT rowid FROM pins_fts WHERE pins_fts MATCH ?) fts ON p.rowid = fts.rowid
                WHERE p.user_id = ?
                ORDER BY p.pinned_at DESC
                LIMIT 10
            """, (query, user["id"])).fetchall()
            for r in pin_rows:
                results.append({
                    "type": "pin",
                    "id": r[0], "pin_title": r[1], "chat_title": r[2],
                    "pinned_at": r[3], "preview": r[4],
                    "chat_id": r[5], "message_id": r[6]
                })
        except Exception as e:
            log.warning(f"Pin search failed: {e}")  # non-fatal

        self._json_response(200, {"results": results, "mode": "semantic"})

    def _search_messages_like(self, user, query, fallback_reason=None):
        """SQL LIKE search.  ``fallback_reason`` is set by the semantic-search
        path when it's degrading to LIKE because the embedding pipeline is
        unavailable — emitted as ``mode: "text_fallback"`` + a ``reason``
        string so the SPA can show a toast instead of silently mis-labelling
        results as text-mode-by-choice.  When unset (direct text-mode
        invocation), behaviour is unchanged."""
        db = get_db()
        # Escape LIKE wildcards in user input
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        where, params = self._user_filter(user)
        content_filter = "AND" if where else "WHERE"
        rows = db.execute(f"""
            SELECT m.id, m.content, m.role, m.chat_id, chats.title
            FROM messages m
            JOIN chats ON m.chat_id = chats.id
            {where}
            {content_filter} m.content LIKE ? ESCAPE '\\' AND m.role IN ('user', 'assistant')
            ORDER BY m.created_at DESC
            LIMIT 20
        """, (*params, like_pattern)).fetchall()
        results = []
        for mid, content, role, chat_id, title in rows:
            results.append({"message_id": mid, "score": 1.0, "content": (content or "")[:200], "role": role, "chat_id": chat_id, "chat_title": title})
        body = {"results": results, "mode": "text_fallback" if fallback_reason else "text"}
        if fallback_reason:
            body["reason"] = fallback_reason
        self._json_response(200, body)

    def _user_filter(self, user, table="chats"):
        """Return (where_clause, params) for user-scoped queries.

        Always scoped — the same ownership leak that hit ``_verify_chat_owner``
        was present here.  In auth-disabled mode every request comes from
        ``user["id"] == "default"`` so the WHERE clause correctly returns only
        the rows that belong to the default user.
        """
        return f"WHERE {table}.user_id = ?", (user["id"],)

    # --- Helpers ---

    # Legacy phrase fallback for when LM Studio's 400 doesn't carry
    # ``error.param``.  Mirrors the inline map at _handle_chat_stream:2828 —
    # keep these two in sync.
    _LEGACY_PARAM_PHRASES = {
        "reasoning": ("does not support reasoning", "does not expose reasoning"),
        "previous_response_id": ("could not find stored response", "previous_response_not_found"),
    }

    def _lmstudio_chat(self, payload, user_id, timeout=60, max_strip_iterations=2):
        """Send a chat completion request to LM Studio.

        Universal strip + harvest + retry-loop: every direct caller (CoVe,
        title generation, ``/compact`` summary, memory distill, non-stream
        ``_handle_chat``) gets the same param-cache protection that the
        streaming chat path has at _handle_chat_stream:2851-2925.

        Strip-up-front handles the warm-cache case.  Harvest-on-400 +
        retry-once handles the cold-cache case so the first call after a
        restart isn't broken (the bug that silently killed CoVe Step 2
        for qwen3.6 — reasoning param rejected, cache empty, no retry,
        pipeline aborted).

        max_strip_iterations defaults to 2 — one strip pass is enough in
        practice (LM Studio rejects at most one param at a time in our
        observed surface).  The cap exists as a safety belt, not a
        budget: wall-clock = iterations × timeout, so a high cap on a
        300s-timeout caller is a foot-gun.
        """
        payload = dict(payload)  # isolate caller from our mutations
        headers = {"Content-Type": "application/json"}
        token = self._get_lmstudio_token(user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # Strip known-bad params up front.
        model_id = payload.get("model") or ""
        if model_id:
            with Handler._unsupported_params_lock:
                rejected = set(Handler._unsupported_params.get(model_id, ()))
            for p in rejected:
                payload.pop(p, None)

        def _send():
            req = urllib.request.Request(
                f"{LMSTUDIO}/api/v1/chat",
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())

        for _ in range(max_strip_iterations):
            try:
                return _send()
            except urllib.error.HTTPError as exc:
                if exc.code != 400:
                    raise
                # Read the body ONCE — urllib.HTTPError.read() consumes the
                # underlying file.  Stash the bytes back as a fresh BytesIO
                # so callers (e.g. _handle_chat's error envelope at line
                # 2378) can still .read() the upstream JSON when we re-raise.
                # Without this, every non-retryable 400 surfaced to the SPA
                # as ``{"error": "LM Studio error: "}`` — empty msg.
                err_bytes = exc.read()
                exc.fp = io.BytesIO(err_bytes)
                try:
                    err = json.loads(err_bytes).get("error") or {}
                except Exception:
                    raise exc from None
                bad_param = err.get("param")
                err_msg = (err.get("message") or "").lower()
                err_code = err.get("code")
                if not bad_param:
                    for p, markers in self._LEGACY_PARAM_PHRASES.items():
                        if any(m in err_msg for m in markers):
                            bad_param = p
                            break
                # Must be a param WE sent — guards against arbitrary user
                # content being mis-interpreted as a param name.
                if not bad_param or bad_param not in payload:
                    raise exc from None
                model_unsupports = (
                    "does not support" in err_msg
                    or "does not expose" in err_msg
                    or "not configurable" in err_msg
                )
                ephemeral_evicted = (
                    err_code == "previous_response_not_found"
                    or "not found" in err_msg
                    or "no longer available" in err_msg
                )
                if not (model_unsupports or ephemeral_evicted):
                    raise exc from None
                if model_unsupports and model_id:
                    with Handler._unsupported_params_lock:
                        Handler._unsupported_params.setdefault(model_id, set()).add(bad_param)
                    _persist_param_blacklist(model_id, bad_param, "reactive_400")
                    log.info(
                        f"unsupported_params: cached ({model_id!r}, {bad_param!r}) via direct call"
                    )
                payload.pop(bad_param, None)
                # Loop to retry without the bad param.  Sanity guard:
                # ``bad_param in payload`` fails on the next iteration if
                # the same param keeps coming back, so no infinite loop.
        raise RuntimeError(
            f"LM Studio: exhausted {max_strip_iterations} param-strip iterations"
        )

    def _run_llm_distill(self, prompt, user_id, *, model="", temperature=0.3, timeout=60):
        """One-shot LLM call for distillation / refinement / compaction prompts.

        Same call pattern repeated at three sites (``_compact_chat``,
        ``_distill_insights``, ``_refine_insights``): build a non-streaming
        payload with no integrations and a cool-ish temperature, fire it at
        LM Studio, pull the content out with ``_extract_content``.

        Returns the parsed text (or empty string when the response can't be
        parsed) — does NOT raise; callers handle empty results their own way.
        """
        payload = {
            "model": model,
            "input": prompt,
            "stream": False,
            "store": False,
            "integrations": [],
            "temperature": temperature,
        }
        data = self._lmstudio_chat(payload, user_id, timeout=timeout)
        return self._extract_content(data) or ""

    def _get_lmstudio_token(self, user_id=None):
        """Get LM Studio auth token from server-side user settings, fall back to env var.
        No longer reads from client Authorization header (H4 security fix)."""
        if user_id:
            stored = self._get_user_lm_apikey(user_id)
            if stored:
                return stored
        return LMSTUDIO_TOKEN

    def _extract_content(self, data):
        if data.get("content"): return data["content"]
        if data.get("output_text"): return data["output_text"]
        if isinstance(data.get("output"), str): return data["output"]
        if isinstance(data.get("output"), list):
            t = "\n".join(i.get("content", "") for i in data["output"] if i.get("type") == "message")
            if t: return t
        if data.get("choices"):
            return data["choices"][0].get("message", {}).get("content", "")
        return ""

    def _error(self, code, msg):
        """Send a JSON error response.

        Includes the per-request ``req_id`` so a user can quote it when
        opening a bug report and the operator can grep the corresponding
        access log + any subsequent stack traces with a single token.
        """
        self._json_response(code, {"error": msg, "req_id": getattr(self, "request_id", None)})

    # Mixed-content input parts we know how to forward.  Anything outside
    # this set is logged-and-passed-through so a future LM Studio release
    # adding (e.g.) "audio" or "video" parts doesn't silently drop them
    # before the upstream sees them.
    _KNOWN_INPUT_PART_TYPES = {"text", "message", "image"}

    @staticmethod
    def _normalize_input(raw):
        """Validate and normalise the ``input`` field.

        Accepts ``str`` (returned as-is) or a list of mixed-content
        dicts.  Malformed items (non-dict, missing ``type``, missing
        the type-appropriate payload) are dropped with a warning so the
        upstream POST stays well-formed and the SPA gets a clean error
        on the next round-trip if everything was dropped.  Unknown
        types are forwarded untouched — only deliberate malformations
        are stripped.
        """
        if not isinstance(raw, list):
            return raw
        out = []
        for item in raw:
            if not isinstance(item, dict):
                log.warning(f"input: dropping non-dict part {type(item).__name__}")
                continue
            t = item.get("type")
            if not t:
                log.warning("input: dropping part with no 'type' field")
                continue
            if t in ("text", "message"):
                # LM Studio's native ``/api/v1/chat`` requires the body in
                # the ``content`` field on text parts and rejects ``text``
                # with ``Unrecognized key(s) in object: 'text'``.  Some
                # older SPA builds / OpenAI-compat clients send ``text``;
                # rewrite to the canonical key at the wire boundary so
                # every upstream call sees the same shape (universal
                # fix, not client-specific).
                body_val = item.get("content")
                if body_val is None:
                    body_val = item.get("text")
                if not body_val:
                    log.warning(f"input: dropping {t!r} part with empty body")
                    continue
                item = {**item, "content": body_val}
                item.pop("text", None)
            elif t == "image":
                if not (item.get("data_url") or item.get("url")):
                    log.warning("input: dropping image part with no data_url/url")
                    continue
            # Unknown ``type`` — forward; LM Studio's parser will 400
            # if it really cannot handle it, and our universal error
            # branch surfaces that with a useful message.
            out.append(item)
        return out

    # --- OpenVINO GenAI endpoint helpers ---
    # OpenVINO GenAI server exposes an OpenAI-compatible API (/v1/chat/completions,
    # /v1/models) instead of LM Studio's native /api/v1/chat.  We detect OpenVINO
    # models by the "-int4-ov" suffix and route accordingly.

    @staticmethod
    def _is_openvino_model(model_id):
        """True if the model is served by the OpenVINO GenAI endpoint."""
        if not model_id:
            return False
        return model_id.endswith("-int4-ov")

    def _openvino_base_url(self):
        """Return the OpenVINO GenAI base URL, or empty string if not configured."""
        return OPENVINO_URL.rstrip("/") if OPENVINO_URL else ""

    def _openvino_headers(self, user_id=None):
        """Build headers for OpenVINO GenAI requests."""
        headers = {"Content-Type": "application/json"}
        token = OPENVINO_TOKEN
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _build_openvino_messages(self, body, system_prompt=None):
        """Build the messages[] array for the OpenAI-compatible API.
        
        OpenVINO GenAI doesn't support LM Studio's previous_response_id /
        response-store chaining, so we reconstruct the conversation history
        from the DB (user + assistant messages) and append the current input.
        """
        messages = []
        # System prompt
        sp = system_prompt or body.get("system_prompt")
        if sp:
            messages.append({"role": "system", "content": sp})
        
        # Reconstruct history from DB if we have a chat_id
        chat_id = body.get("chat_id")
        if chat_id and not body.get("incognito") and body.get("store") is not False:
            try:
                db = get_db()
                rows = db.execute(
                    "SELECT role, content FROM messages WHERE chat_id=? AND role IN ('user','assistant') AND content != '' ORDER BY created_at",
                    (chat_id,),
                ).fetchall()
                for r in rows:
                    role, content = r
                    # Skip thinking blocks — OpenVINO can't process them
                    if role == "assistant" and content.startswith("<think>"):
                        # Extract content after </think>
                        idx = content.find("</think>")
                        if idx >= 0:
                            content = content[idx + len("</think>"):].strip()
                        else:
                            continue  # malformed thinking block, skip
                    if content:
                        messages.append({"role": role, "content": content})
            except Exception as e:
                log.warning(f"openvino: failed to load history for chat {chat_id}: {e}")
        
        # Current user input
        user_input = self._normalize_input(body.get("input", ""))
        if isinstance(user_input, str):
            messages.append({"role": "user", "content": user_input})
        elif isinstance(user_input, list):
            # Mixed-content input: extract text parts
            text_parts = []
            for item in user_input:
                if isinstance(item, dict):
                    if item.get("type") in ("text", "message"):
                        text_parts.append(item.get("text") or item.get("content") or "")
                    elif item.get("type") == "image":
                        text_parts.append("[image attached]")
            messages.append({"role": "user", "content": "\n".join(text_parts)})
        
        return messages

    def _build_openvino_payload(self, body, system_prompt=None, stream=False):
        """Build an OpenAI-compatible payload for the OpenVINO GenAI server."""
        messages = self._build_openvino_messages(body, system_prompt)
        payload = {
            "model": body.get("model", ""),
            "messages": messages,
            "stream": stream,
        }
        if body.get("temperature") is not None:
            payload["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            payload["top_p"] = body["top_p"]
        if body.get("max_output_tokens") is not None and body["max_output_tokens"] > 0:
            payload["max_tokens"] = body["max_output_tokens"]
        if body.get("repeat_penalty") is not None:
            payload["frequency_penalty"] = body["repeat_penalty"]
        return payload

    def _collect_openvino_stream(self, resp, is_incognito: bool, chat_id: str = "") -> tuple:
        """Parse OpenAI SSE stream from OpenVINO GenAI and re-emit as LM Studio SSE.
        
        OpenVINO emits standard OpenAI chunks:
            data: {"choices":[{"delta":{"content":"token"}}]}
            data: {"choices":[{"delta":{"content":"token"},"finish_reason":"stop"}]}
            data: [DONE]
        
        We translate to LM Studio's event format so the frontend (app.js) 
        can parse it without modification:
            event: message.delta
            data: {"content":"token"}
            event: chat.end
            data: {"result":{"response_id":"","stats":{}},"usage":{...}}
        """
        content_parts = []
        reasoning_parts = []
        tool_calls = []
        response_id = None
        stream_usage = {}
        stream_complete = False
        accumulated_content = ""

        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace")
                stripped = line.strip()

                if not stripped.startswith("data:"):
                    # Forward non-data lines (comments, keepalives) as-is
                    if stripped:
                        self.wfile.write(raw_line)
                        self.wfile.flush()
                    continue

                data_str = stripped[5:].strip()
                if data_str == "[DONE]":
                    # Emit chat.end event
                    end_data = json.dumps({
                        "type": "chat.end",
                        "result": {"response_id": response_id or "", "stats": {}},
                        "usage": stream_usage,
                    })
                    self.wfile.write(f"event: chat.end\ndata: {end_data}\n\n".encode())
                    self.wfile.flush()
                    stream_complete = True
                    break

                try:
                    chunk = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Extract usage from the final chunk if present
                if chunk.get("usage"):
                    stream_usage = {
                        "input_tokens": chunk["usage"].get("prompt_tokens", 0),
                        "output_tokens": chunk["usage"].get("completion_tokens", 0),
                    }

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                delta = choices[0].get("delta", {})

                # Content delta
                content_delta = delta.get("content")
                if content_delta:
                    content_parts.append(content_delta)
                    accumulated_content += content_delta
                    msg_data = json.dumps({"content": content_delta})
                    self.wfile.write(f"event: message.delta\ndata: {msg_data}\n\n".encode())
                    self.wfile.flush()

                # Reasoning delta (if model supports it)
                reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    rdata = json.dumps({"content": reasoning_delta})
                    self.wfile.write(f"event: reasoning.delta\ndata: {rdata}\n\n".encode())
                    self.wfile.flush()

                # Finish reason
                finish_reason = choices[0].get("finish_reason")
                if finish_reason:
                    if not stream_usage:
                        stream_usage = {"input_tokens": 0, "output_tokens": len(content_parts)}
                    end_data = json.dumps({
                        "type": "chat.end",
                        "result": {"response_id": response_id or "", "stats": {}},
                        "usage": stream_usage,
                    })
                    self.wfile.write(f"event: chat.end\ndata: {end_data}\n\n".encode())
                    self.wfile.flush()
                    stream_complete = True
                    break

        except (BrokenPipeError, ConnectionResetError):
            log.debug("OpenVINO stream aborted by client")
        except TimeoutError:
            log.warning("OpenVINO stream timed out")
        except OSError as e:
            log.warning(f"OpenVINO stream OS error: {e}")
        except Exception as e:
            log.error(f"OpenVINO stream unexpected error: {e}", exc_info=True)
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if not stream_complete and not is_incognito:
            log.warning(f"OpenVINO stream ended without completion")

        return content_parts, reasoning_parts, tool_calls, response_id, stream_usage, stream_complete

    def _build_lmstudio_payload(self, body, system_prompt=None, stream=False):
        """Build the common LM Studio API payload."""
        integrations = body.get("integrations", [])
        payload = {
            "model": body.get("model", ""),
            "input": self._normalize_input(body.get("input", "")),
            "integrations": integrations,
            "stream": stream,
        }
        if body.get("previous_response_id"):
            payload["previous_response_id"] = body["previous_response_id"]
        if system_prompt:
            payload["system_prompt"] = system_prompt
        elif body.get("system_prompt"):
            payload["system_prompt"] = body["system_prompt"]
        if body.get("temperature") is not None:
            payload["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            payload["top_p"] = body["top_p"]
        if body.get("top_k") is not None:
            payload["top_k"] = body["top_k"]
        if body.get("min_p") is not None:
            payload["min_p"] = body["min_p"]
        if body.get("repeat_penalty") is not None:
            payload["repeat_penalty"] = body["repeat_penalty"]
        if body.get("max_output_tokens") is not None and body["max_output_tokens"] > 0:
            payload["max_output_tokens"] = body["max_output_tokens"]
        if body.get("reasoning"):
            payload["reasoning"] = body["reasoning"]

        # Universal capability gate — strip any parameter the model has
        # previously rejected with "does not (support|expose)".  Populated
        # by the retry block in ``_handle_chat_stream`` once we've seen the
        # structured error from LM Studio.  Lookup is O(1) and runs after
        # every other payload-building step so it catches any param we
        # might add in future code paths without needing per-param coupling.
        #
        # ``user_set_params`` from the client is a one-shot bypass: any
        # param the user JUST set via an inline UI control (e.g. the
        # reasoning cycle button) is exempt from the gate for this turn.
        # The user explicitly asked to try this value; let LM Studio
        # answer authoritatively rather than silently dropping the param.
        # If LM Studio still rejects, the reactive retry path catches it
        # and adds the (model, param) pair to the blacklist as usual.
        model_id = payload.get("model") or body.get("model") or ""
        user_overrides = set(body.get("user_set_params") or [])
        if model_id:
            with Handler._unsupported_params_lock:
                rejected = set(Handler._unsupported_params.get(model_id, ()))
            for p in rejected:
                if p in user_overrides:
                    continue  # explicit user request — let LM Studio decide
                payload.pop(p, None)
        # context_length omitted — it's a load-time parameter in LM Studio.
        # Sending it per-request triggers JIT model reloads.
        # ``store=False`` fires for incognito (system-level) OR for an
        # explicit per-chat "Stateless turns" preference (user-level).
        # Both ask LM Studio to skip allocating response-store state.
        if body.get("incognito") or body.get("store") is False:
            payload["store"] = False
        return payload

    def _build_convo_snippet(self, messages, max_chars=4000):
        """Build conversation text for summarization."""
        convo_text = []
        for m in messages:
            role = m.get("role", "")
            text = m.get("content") or ""
            if role == "tool":
                text = f"[tool: {m.get('name', '')}] {(m.get('output') or '')[:200]}"
            if text:
                convo_text.append(f"{role}: {text}")
        return "\n".join(convo_text)[:max_chars]

    def _build_usc_synthesis_prompt(self, original_question, candidates, is_factual=False):
        """Build the USC synthesis prompt from N candidates."""
        max_per = 4000  # chars; generous for most models
        trimmed = [c[:max_per] for c in candidates]
        sections = "\n\n".join(
            f"--- Response {i+1} ---\n{t}"
            for i, t in enumerate(trimmed)
        )
        question_str = original_question if isinstance(original_question, str) else str(original_question)[:500]
        prompt = (
            f'The user asked: "{question_str}"\n\n'
            f"Here are {len(candidates)} independent responses generated for this question:\n\n"
            f"{sections}\n\n"
            "Review all responses. Return the single response that is most consistent with the "
            "majority of the others — the one that best represents the central, agreed-upon answer. "
            "Return only the selected response text, verbatim. Do not add commentary or explain your choice."
        )
        if is_factual:
            prompt += (
                "\n\nPrefer the response that is most specific and factually detailed "
                "while still being consistent with the majority position."
            )
        return prompt

    def _self_consistency(self, payload, user_id, emit_status=None, n=3, temperature=0.7):
        """USC self-consistency: N parallel candidates → synthesis.
        Returns bare str (early exit or failure) | dict (synthesis payload) | None (all failed).
        emit_status: optional callable(text) to send SSE status events to the client.
        """
        if emit_status:
            emit_status(f"Generating response 1 of {n}...")

        # ``base`` is the shared shape for BOTH candidates AND the synthesis
        # call below.  Synthesis doesn't need tools (it's combining text
        # outputs), so default ``integrations=[]``.  Candidates DO need
        # tools — they're generating real responses to the user's question;
        # stripping tools here was the bug that made "use the firecrawl
        # tool to fetch X" with SC enabled produce N hallucinated answers
        # which then synthesised into one hallucinated answer.
        original_integrations = list(payload.get("integrations") or [])
        base = {
            **payload,
            "store": False,
            "integrations": [],
            "temperature": temperature,
            "stream": False,
        }
        # Candidate payload: same as base but keeps the user's integrations
        # so each parallel generation can actually call tools.
        candidate_payload = {**base, "integrations": original_integrations}

        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(self._lmstudio_chat, candidate_payload, user_id, 60) for _ in range(n)]
            candidates = []
            completed = 0
            for f in as_completed(futures):
                completed += 1
                if emit_status and completed < n:
                    emit_status(f"Generating response {completed + 1} of {n}...")
                try:
                    candidates.append(self._extract_content(f.result()))
                except Exception as e:
                    log.warning(f"SC candidate failed: {e}")

        if len(candidates) < 2:
            log.warning(f"SC: only {len(candidates)} candidates succeeded, falling back")
            return candidates[0] if candidates else None

        # Early exit: first two candidates agree closely
        if _token_overlap(candidates[0], candidates[1]) > 0.80:
            if emit_status:
                emit_status("✓ Consistent answer found")
            return candidates[0]

        if emit_status:
            emit_status("Selecting most consistent response...")

        original_q = payload.get("input", "")
        question_str = original_q if isinstance(original_q, str) else ""
        factual_keywords = {"who", "when", "where", "born", "died", "founded", "invented", "created"}
        is_factual = bool(factual_keywords & set(question_str.lower().split()))

        synthesis_input = self._build_usc_synthesis_prompt(original_q, candidates, is_factual)
        result_payload = {
            **base,
            "input": synthesis_input,
            "temperature": 0.0,
            "stream": True,
        }
        result_payload.pop("previous_response_id", None)
        return result_payload

    def _build_vq_extraction_prompt(self, original_question, draft):
        """Build the verification question extraction prompt for CoVe Step 2."""
        q_str = original_question if isinstance(original_question, str) else str(original_question)[:500]
        return {
            "system_prompt": (
                "You are a fact-checker. Given a question and a draft answer, generate "
                "targeted verification questions to check the factual claims.\n\n"
                "Rules:\n"
                "- Each question must be independently answerable without seeing the draft\n"
                "- Each question targets a single specific claim\n"
                "- Phrase as standalone questions, not confirmations\n"
                "- Maximum 4 questions\n"
                "- If there are no specific factual claims to verify, respond with: NONE"
            ),
            "input": f"Question: {q_str}\nDraft answer: {draft}\n\nGenerate verification questions:"
        }

    def _build_cove_synthesis_prompt(self, original_question, draft, vq_answers):
        """Build the CoVe Step 4 synthesis input."""
        q_str = original_question if isinstance(original_question, str) else str(original_question)[:500]
        vq_section = "\n\n".join(
            f"Q: {vq}\nA: {ans}"
            for vq, ans in vq_answers.items()
        )
        return (
            f"Original question: {q_str}\n\n"
            f"Initial draft answer (may contain errors):\n{draft}\n\n"
            f"Verification results:\n{vq_section}\n\n"
            "Using the verified answers, write the final response. Where verification "
            "answers contradict the draft, use the verified information. Acknowledge "
            "uncertainty where verification answers were inconclusive. Do not mention "
            "that this is a verification process — just provide the accurate answer."
        )

    def _parse_verification_questions(self, text):
        """Extract numbered verification questions from LLM output. Max 4."""
        if "NONE" in text.upper():
            return []
        lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
        questions = []
        for line in lines:
            cleaned = re.sub(r'^[\d\-\.\)]+\s*', '', line).strip()
            if cleaned.endswith('?') and len(cleaned) > 10:
                questions.append(cleaned)
            if len(questions) >= 4:
                break
        return questions

    def _chain_of_verification(self, payload, user_id, emit_status=None):
        """4-step CoVe pipeline.
        Returns bare str (no verifiable claims — draft) | dict (synthesis streaming payload) | None (failure).
        Same return contract as _self_consistency: bare str | dict | None.
        """
        original_reasoning = payload.get("reasoning")

        # ``base_silent`` is the shared shape for Step 2 (VQ extraction —
        # different system prompt, no tools needed), Step 3 (VQ answers —
        # spawned independently with their own clean payload), and Step 4
        # (synthesis — combines text outputs, no tools needed).  Default
        # ``integrations=[]`` is correct for those.
        # Step 1 (draft) is the user's actual answer and MUST keep their
        # integrations or the whole pipeline fact-checks a hallucinated
        # draft (the bug that made "use firecrawl to fetch X" with CoVe
        # enabled produce "I don't have access to firecrawl" → VQ
        # extraction on that → synthesis → user sees a non-answer).
        original_integrations = list(payload.get("integrations") or [])
        base_silent = {
            **payload,
            "store": False,
            "integrations": [],
            "reasoning": "off",
            "stream": False,
        }

        try:
            # Step 1: Draft — uses the user's actual integrations so tools fire.
            if emit_status:
                emit_status("Drafting response...")
            draft_payload = {**base_silent, "integrations": original_integrations}
            draft_data = self._lmstudio_chat(draft_payload, user_id, timeout=60)
            draft = self._extract_content(draft_data)
            if not draft.strip():
                draft = None
            if not draft:
                log.warning("CoVe Step 1: empty draft, falling back")
                return None

            # Step 2: Extract verification questions
            if emit_status:
                emit_status("Identifying claims to verify...")
            vq_prompt_parts = self._build_vq_extraction_prompt(payload.get("input", ""), draft)
            vq_payload = {
                **base_silent,
                "system_prompt": vq_prompt_parts["system_prompt"],
                "input": vq_prompt_parts["input"],
                "temperature": 0.0,
                "max_output_tokens": 350,
            }
            vq_payload.pop("previous_response_id", None)
            vq_data = self._lmstudio_chat(vq_payload, user_id, timeout=30)
            vqs = self._parse_verification_questions(self._extract_content(vq_data))

            if not vqs:
                if emit_status:
                    emit_status("No factual claims to verify")
                    emit_status("Responding...")
                return draft

            if emit_status:
                emit_status(f"Verifying {len(vqs)} fact{'s' if len(vqs) != 1 else ''}...")

            # Step 3: Answer VQs independently in parallel
            def answer_vq(vq):
                clean_payload = {
                    "model": payload["model"],
                    "input": vq,
                    "system_prompt": "Answer the following question directly and accurately. Be concise.",
                    "temperature": 0.1,
                    "max_output_tokens": 300,
                    "store": False,
                    "integrations": [],
                    "reasoning": "off",
                    "stream": False,
                }
                try:
                    return vq, self._extract_content(self._lmstudio_chat(clean_payload, user_id, timeout=30))
                except Exception as e:
                    log.warning(f"CoVe VQ answer failed: {e}")
                    return vq, None

            vq_answers = {}
            with ThreadPoolExecutor(max_workers=min(len(vqs), 4)) as ex:
                futures = {ex.submit(answer_vq, vq): vq for vq in vqs}
                for f in as_completed(futures):
                    vq, answer = f.result()
                    if answer is not None:
                        vq_answers[vq] = answer

            if not vq_answers:
                log.warning("CoVe Step 3: all VQ answers failed, returning draft")
                return draft

            # Step 4: Synthesis
            if emit_status:
                emit_status("Finalizing verified response...")
            synthesis_input = self._build_cove_synthesis_prompt(
                payload.get("input", ""), draft, vq_answers
            )
            synthesis_payload = {
                **base_silent,
                "input": synthesis_input,
                "system_prompt": "You are a careful and accurate assistant.",
                "stream": True,
            }
            synthesis_payload.pop("previous_response_id", None)
            if original_reasoning is not None:
                synthesis_payload["reasoning"] = original_reasoning
            else:
                synthesis_payload.pop("reasoning", None)
            return synthesis_payload

        except Exception as e:
            log.warning(f"CoVe pipeline failed: {e}")
            return None

    # --- Memory: Insight scoring and retrieval ---

    CATEGORY_WEIGHTS = {
        "identity": 2.0, "preference": 2.0, "opinion": 1.5,
        "skill": 1.0, "project": 1.0, "context": 1.0,
    }
    _LAPLACE_ALPHA = 1.0  # Bayesian Laplace smoothing for insight feedback scoring
    _LAPLACE_BETA  = 2.0
    # Maximum number of pinned insights injected unconditionally.
    # Larger values risk blowing the system-prompt budget with pinned
    # rows alone.  5 was minimax's stress-test recommendation; raise
    # with caution.
    _PINNED_INSIGHTS_LIMIT = 5

    @staticmethod
    def _content_hash(content: str) -> str:
        """Per-spec dedup hash.  Delegates to the module-level helper so
        the backfill (which runs without a Handler instance) can use the
        exact same normalization.
        """
        return _content_hash_text(content)

    def _score_insights(self, db, user_id):
        """Update freshness scores for all active insights. Pure SQL, no LLM."""
        db.execute("""
            UPDATE user_insights SET state = 'faded'
            WHERE user_id = ? AND state = 'active'
            AND (1.0 / (1.0 + (julianday('now') - julianday(last_used, 'unixepoch')) / 30.0))
                * (1.0 + 0.3 * ln(1 + use_count)) < 0.1
        """, (user_id,))
        db.commit()

    def _get_top_insights(self, db, user_id, limit=30, max_tokens=500):
        """Retrieve insights for injection.

        Pinned insights (capped at ``_PINNED_INSIGHTS_LIMIT``) inject
        first, unconditionally, in insertion order.  If pinned alone
        consumes the char budget, the unpinned loop never runs.
        Otherwise top-scored unpinned rows fill the remainder.
        """
        self._score_insights(db, user_id)
        char_budget = max_tokens * 4

        # 1. Pinned (capped + oldest first so the set is stable across reranks).
        pinned_rows = db.execute("""
            SELECT id, content, category FROM user_insights
            WHERE user_id = ? AND state = 'active' AND pinned = 1
            ORDER BY created_at
            LIMIT ?
        """, (user_id, self._PINNED_INSIGHTS_LIMIT)).fetchall()
        result = [{"id": r[0], "content": r[1], "category": r[2],
                   "score": float("inf"), "pinned": True}
                  for r in pinned_rows]
        chars = sum(len(p["content"]) + 20 for p in result)
        # Pinned takes priority — if it alone exceeds budget, stop here.
        if chars >= char_budget:
            return result

        # 2. Scored unpinned.
        rows = db.execute("""
            SELECT id, content, category, ups, downs,
                (1.0 / (1.0 + (julianday('now') - julianday(last_used, 'unixepoch')) / 30.0))
                * (1.0 + 0.3 * ln(1 + use_count)) AS base_score
            FROM user_insights
            WHERE user_id = ? AND state = 'active' AND pinned = 0
            ORDER BY base_score DESC
            LIMIT ?
        """, (user_id, limit * 2)).fetchall()

        weighted = []
        for r in rows:
            cat_w = self.CATEGORY_WEIGHTS.get(r[2], 1.0)
            ups, downs = r[3] or 0.0, r[4] or 0.0
            bayesian = (ups + self._LAPLACE_ALPHA) / (ups + downs + self._LAPLACE_BETA)
            weighted.append({"id": r[0], "content": r[1], "category": r[2],
                             "score": r[5] * cat_w * bayesian, "pinned": False})
        weighted.sort(key=lambda x: x["score"], reverse=True)

        remaining = max(0, limit - len(result))
        for w in weighted[:remaining]:
            next_chars = chars + len(w["content"]) + 20
            if next_chars > char_budget:
                break
            chars = next_chars
            result.append(w)
        return result

    def _format_insights_for_prompt(self, insights):
        """Format insights as a system prompt section."""
        if not insights:
            return ""
        lines = ["## About the user"]
        for ins in insights:
            lines.append(f"- {ins['content']} [{ins['category']}]")
        return "\n".join(lines)

    def _touch_insights(self, db, insight_ids):
        """Bump last_used and use_count for retrieved insights."""
        if not insight_ids:
            return
        placeholders = ",".join("?" * len(insight_ids))
        db.execute(f"""
            UPDATE user_insights SET last_used = ?, use_count = use_count + 1
            WHERE id IN ({placeholders})
        """, [time.time()] + insight_ids)
        db.commit()

    # --- Memory: Insight parsing ---

    VALID_INSIGHT_CATEGORIES = {"identity", "preference", "skill", "project", "opinion", "context"}

    def _parse_insight_line(self, line):
        """Parse a single insight line. Returns (content, category) or (None, None)."""
        line = line.lstrip("•-* ").strip()
        if not line:
            return None, None
        category = "context"
        for cat in self.VALID_INSIGHT_CATEGORIES:
            if f"[{cat}]" in line.lower():
                category = cat
                line = re.sub(r'\[' + cat + r'\]', '', line, flags=re.IGNORECASE).strip()
                break
        return line, category

    def _parse_and_store_insights(self, db, user_id, raw_text, chat_id=None):
        """Parse LLM distillation output and store insights. Returns list of new insights."""
        if not raw_text or raw_text.strip().lower() == "none":
            return []
        new_insights = []
        now = time.time()
        for raw_line in raw_text.strip().split("\n"):
            raw_line = raw_line.strip()
            if raw_line.lower().lstrip("- \u2022").startswith("[correction]"):
                raw_line = raw_line.lstrip("- \u2022")[len("[correction]"):].strip()
            line, category = self._parse_insight_line(raw_line)
            if not line or len(line) < 5:
                continue
            insight_id = uuid.uuid4().hex[:12]
            chash = self._content_hash(line)
            try:
                db.execute(
                    """INSERT INTO user_insights
                       (id, user_id, content, category, origin_chat_id,
                        created_at, last_used, content_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (insight_id, user_id, line, category, chat_id, now, now, chash),
                )
            except sqlite3.IntegrityError:
                # UNIQUE (user_id, content_hash) hit \u2014 byte-identical
                # re-distill.  Skip silently; refine pass handles
                # legitimate re-categorisation.
                log.debug(f"insight dedup: {line[:50]!r}")
                continue
            new_insights.append({"id": insight_id, "content": line, "category": category})
        if new_insights:
            db.commit()
        return new_insights

    # --- Memory: Insight distillation ---

    DISTILL_PROMPT = """You are a personal context system. Given this conversation, distill NEW insights about the user.

Rules:
- Only insights about the USER (preferences, identity, work, skills, opinions, style)
- One insight per line, concise (under 20 words)
- Tag each: [identity] [preference] [skill] [project] [opinion] [context]
- Skip anything generic or already known
- If correcting a previous insight, prefix with [correction]

Known context (do not repeat):
{known_context}

Conversation:
{conversation}

Distill insights (or respond "none" if nothing new):"""

    def _reindex_embeddings(self):
        """POST /api/insights/reindex — re-embed messages whose stored
        vectors were produced by a different embedding model than the
        one currently loaded on LM Studio.  Opt-in, idempotent.

        Atomic per call: all updates commit together; any exception
        triggers ROLLBACK and returns 500 with no changes applied.
        Per-row embedding failures (rate limit, transient) are counted
        and reported in the response — does NOT abort the batch.
        """
        user = self._require_auth()
        if not user:
            return
        current = self._current_embedding_model_id()
        if not current:
            return self._error(503, "no embedding model loaded on LM Studio")
        db = get_db()
        rows = db.execute("""
            SELECT m.id, m.content
            FROM messages m
            JOIN embeddings e ON e.message_id = m.id
            JOIN chats c ON m.chat_id = c.id
            WHERE c.user_id = ? AND (e.model_id IS NULL OR e.model_id != ?)
        """, (user["id"], current)).fetchall()
        if not rows:
            return self._json_response(200, {
                "updated": 0, "skipped": 0, "total_seen": 0,
                "model_id": current,
            })
        token = self._get_lmstudio_token(user["id"])
        updated = 0
        failed = 0
        db.execute("BEGIN IMMEDIATE")
        try:
            for mid, content in rows:
                if not content:
                    continue
                vec = get_embedding(content, token)
                if not vec:
                    failed += 1
                    continue
                blob = struct.pack(f"{len(vec)}f", *vec)
                db.execute(
                    "UPDATE embeddings SET vector=?, model_id=? WHERE message_id=?",
                    (blob, current, mid),
                )
                updated += 1
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            log.error(f"reindex failed: {e}")
            return self._error(500, "reindex failed; no changes applied")
        self._json_response(200, {
            "updated":     updated,
            "skipped":     failed,
            "total_seen":  len(rows),
            "model_id":    current,
        })

    def _distill_insights(self, body):
        """Extract user insights from a chat conversation via LLM call."""
        user = self._require_auth()
        if not user:
            return

        chat_id = body.get("chat_id")
        model = body.get("model", "")
        if not chat_id:
            return self._error(400, "chat_id required")

        with Handler._distill_lock:
            if chat_id in Handler._distilling:
                return self._json_response(409, {"error": "distillation already in progress"})
            Handler._distilling.add(chat_id)
        try:
            db = get_db()
            if not self._verify_chat_owner(db, chat_id, user["id"]):
                return

            # Fetch conversation — skip interrupted stubs so distillation
            # doesn't pull "the model said nothing" as a real signal.
            msg_rows = db.execute(
                "SELECT role, content, name, output FROM messages "
                "WHERE chat_id=? AND (status IS NULL OR status != 'interrupted') "
                "ORDER BY id",
                (chat_id,),
            ).fetchall()
            if not msg_rows:
                return self._json_response(200, {"insights": [], "message": "empty chat"})

            convo_lines = []
            for r in msg_rows:
                role, content = r[0], r[1] or ""
                if role == "tool":
                    name = r[2] or "tool"
                    output = r[3] or ""
                    convo_lines.append(f"[Tool: {name}] {output[:200]}")
                elif content:
                    convo_lines.append(f"{role}: {content}")
            conversation = "\n".join(convo_lines)[-3000:]  # last 3000 chars

            # Fetch existing insights for dedup
            existing = db.execute(
                "SELECT content FROM user_insights WHERE user_id=? AND state='active'",
                (user["id"],),
            ).fetchall()
            known_context = "\n".join(f"- {r[0]}" for r in existing) if existing else "(none yet)"

            # LLM call
            prompt = self.DISTILL_PROMPT.format(
                known_context=known_context, conversation=conversation
            )
            try:
                raw_text = self._run_llm_distill(
                    prompt, user["id"], model=model, temperature=0.3, timeout=60,
                )
            except Exception as e:
                log.error(f"LLM distillation failed: {e}")
                return self._error(502, "LLM distillation failed")

            try:
                new_insights = self._parse_and_store_insights(db, user["id"], raw_text, chat_id)
            except Exception as e:
                log.error(f"Failed to store insights: {e}")
                return self._error(500, "failed to store insights")
            log.debug(f"memory: distilled {len(new_insights)} insights from chat {chat_id}")
            self._json_response(200, {"insights": new_insights})
        finally:
            with Handler._distill_lock:
                Handler._distilling.discard(chat_id)

    # --- Memory: CRUD endpoints ---

    def _list_insights(self):
        """GET /api/insights — list all insights for current user."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute(
            """SELECT id, content, category, origin_chat_id, weight,
                      created_at, last_used, use_count, state, pinned
               FROM user_insights WHERE user_id=? AND state != 'removed'
               ORDER BY pinned DESC, created_at DESC""",
            (user["id"],),
        ).fetchall()
        insights = [
            {"id": r[0], "content": r[1], "category": r[2], "origin_chat_id": r[3],
             "weight": r[4], "created_at": r[5], "last_used": r[6],
             "use_count": r[7], "state": r[8], "pinned": r[9] or 0}
            for r in rows
        ]
        self._json_response(200, insights)

    def _add_insight(self, body):
        """POST /api/insights — manually add an insight."""
        user = self._require_auth()
        if not user:
            return
        content = (body.get("content") or "").strip()
        if not content:
            return self._error(400, "content required")
        category = body.get("category", "context")
        if category not in self.VALID_INSIGHT_CATEGORIES:
            category = "context"
        now = time.time()
        insight_id = uuid.uuid4().hex[:12]
        chash = self._content_hash(content)
        db = get_db()
        try:
            db.execute(
                """INSERT INTO user_insights (id, user_id, content, category, created_at, last_used, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (insight_id, user["id"], content, category, now, now, chash),
            )
        except sqlite3.IntegrityError:
            existing = db.execute(
                "SELECT id FROM user_insights WHERE user_id=? AND content_hash=?",
                (user["id"], chash),
            ).fetchone()
            return self._error(409, {
                "error":       "duplicate_content",
                "existing_id": existing[0] if existing else None,
            })
        db.commit()
        self._json_response(201, {"id": insight_id, "content": content, "category": category})

    # Per spec v3: PATCH-style fields the SPA is allowed to update on
    # an insight.  Anything outside this set is ignored silently.
    ALLOWED_INSIGHT_UPDATE_FIELDS = {"content", "category", "pinned"}

    @staticmethod
    def _coerce_bool_flag(value, field_name: str):
        """Accept 0/1, True/False, "0"/"1"/"true"/"false" (case-insensitive),
        return 0 or 1.  Raises ValueError on garbage so the caller can
        return a real 400 instead of silently defaulting to 0 (which was
        the v2 bug minimax flagged as N3+N5).
        """
        truthy = {1}
        falsy  = {0, None}
        if value in truthy:
            return 1
        if value in falsy:
            return 0
        s = str(value).strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return 1
        if s in {"0", "false", "no", "off", ""}:
            return 0
        raise ValueError(f"invalid {field_name} value: {value!r}")

    def _edit_insight(self, insight_id, body):
        """POST /api/insights/:id/edit — edit an insight.

        Allowed fields: content, category, pinned (see
        ALLOWED_INSIGHT_UPDATE_FIELDS).  Validates per-field:
        - content: stripped non-empty; recomputes content_hash; rejects
          409 if the new hash collides with another row.
        - category: must be in VALID_INSIGHT_CATEGORIES, else 'context'.
        - pinned: coerced via _coerce_bool_flag; enforces
          _PINNED_INSIGHTS_LIMIT on the user's active pinned count.
        """
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        row = db.execute(
            "SELECT user_id FROM user_insights WHERE id=?", (insight_id,)
        ).fetchone()
        if not row or row[0] != user["id"]:
            return self._error(404, "insight not found")
        updates = []
        params = []
        new_pinned_val = None
        for key, value in (body or {}).items():
            if key not in self.ALLOWED_INSIGHT_UPDATE_FIELDS:
                continue
            if key == "content":
                stripped = (value or "").strip()
                if not stripped:
                    return self._error(400, "content cannot be empty")
                new_hash = self._content_hash(stripped)
                collision = db.execute(
                    "SELECT id FROM user_insights "
                    "WHERE user_id=? AND content_hash=? AND id != ?",
                    (user["id"], new_hash, insight_id),
                ).fetchone()
                if collision:
                    return self._error(409, {
                        "error":       "duplicate_content",
                        "existing_id": collision[0],
                    })
                updates.append("content=?")
                params.append(stripped)
                updates.append("content_hash=?")
                params.append(new_hash)
            elif key == "category":
                cat = value if value in self.VALID_INSIGHT_CATEGORIES else "context"
                updates.append("category=?")
                params.append(cat)
            elif key == "pinned":
                try:
                    coerced = self._coerce_bool_flag(value, "pinned")
                except ValueError as e:
                    return self._error(400, str(e))
                new_pinned_val = coerced
                updates.append("pinned=?")
                params.append(coerced)
        if not updates:
            return self._error(400, "nothing to update")
        # Pin-limit gate: only check when actually pinning (not unpinning).
        if new_pinned_val == 1:
            cur_pinned = db.execute(
                "SELECT COUNT(*) FROM user_insights "
                "WHERE user_id=? AND state='active' AND pinned=1 AND id != ?",
                (user["id"], insight_id),
            ).fetchone()[0]
            if cur_pinned >= self._PINNED_INSIGHTS_LIMIT:
                return self._error(409, {
                    "error": "pin_limit",
                    "limit": self._PINNED_INSIGHTS_LIMIT,
                })
        params.append(insight_id)
        db.execute(f"UPDATE user_insights SET {','.join(updates)} WHERE id=?", params)
        db.commit()
        self._json_response(200, {"ok": True})

    def _delete_insight(self, insight_id):
        """DELETE /api/insights/:id — hard delete an insight."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        row = db.execute(
            "SELECT user_id FROM user_insights WHERE id=?", (insight_id,)
        ).fetchone()
        if not row or row[0] != user["id"]:
            return self._error(404, "insight not found")
        db.execute("DELETE FROM user_insights WHERE id=?", (insight_id,))
        db.commit()
        self._json_response(200, {"ok": True})

    def _clear_insights(self):
        """DELETE /api/insights — clear all insights for current user."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        db.execute("DELETE FROM user_insights WHERE user_id=?", (user["id"],))
        db.commit()
        self._json_response(200, {"ok": True})

    def _get_insight_settings(self):
        """GET /api/insights/settings — get memory preferences."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        rows = db.execute(
            "SELECT key, value FROM user_settings WHERE user_id=? AND key LIKE 'memory_%'",
            (user["id"],),
        ).fetchall()
        settings = {r[0]: r[1] for r in rows}
        settings.setdefault("memory_enabled", "true")
        settings.setdefault("memory_max_inject", "30")
        self._json_response(200, settings)

    def _save_insight_settings(self, body):
        """PATCH /api/insights/settings — update memory preferences."""
        user = self._require_auth()
        if not user:
            return
        db = get_db()
        allowed = {"memory_enabled", "memory_max_inject"}
        for key, value in body.items():
            if key not in allowed:
                continue
            val = str(value)
            if key == "memory_enabled" and val not in ("true", "false"):
                continue
            if key == "memory_max_inject":
                try:
                    n = int(val)
                    if n < 1 or n > 100:
                        continue
                    val = str(n)
                except (ValueError, TypeError):
                    continue
            db.execute(
                "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                (user["id"], key, val),
            )
        db.commit()
        self._json_response(200, {"ok": True})

    REFINE_PROMPT = """You are a personal context curator. Review these user insights and:

1. Merge duplicates (keep the more specific version)
2. Resolve contradictions (newer insight wins, note the update)
3. Group related insights by theme
4. Remove insights that are clearly session-specific and not worth keeping

Current insights:
{insights}

Return ONLY the curated list. One insight per line with category tag.
Prefix removed items with [drop] and state the reason briefly.
Prefix merged items with [merged] and include all relevant detail.
Keep items unchanged if they're fine.

Curated list:"""

    def _refine_insights(self, body):
        """POST /api/insights/refine — merge, dedup, prune via LLM."""
        user = self._require_auth()
        if not user:
            return
        model = body.get("model", "")

        db = get_db()
        rows = db.execute(
            "SELECT id, content, category, created_at FROM user_insights WHERE user_id=? AND state='active' ORDER BY created_at",
            (user["id"],),
        ).fetchall()

        if len(rows) < 3:
            return self._json_response(200, {"message": "too few insights to refine", "count": len(rows)})

        insight_lines = []
        for r in rows:
            date_str = time.strftime("%Y-%m-%d", time.localtime(r[3]))
            insight_lines.append(f"- {r[1]} [{r[2]}] (added {date_str})")

        prompt = self.REFINE_PROMPT.format(insights="\n".join(insight_lines))

        try:
            raw_text = self._run_llm_distill(
                prompt, user["id"], model=model, temperature=0.2, timeout=60,
            )
        except Exception as e:
            return self._error(502, f"LLM refinement failed: {e}")

        dropped, merged, kept = 0, 0, 0
        now = time.time()

        old_ids = [r[0] for r in rows]

        new_insights = []
        for raw_line in raw_text.strip().split("\n"):
            raw_line = raw_line.strip()
            if not raw_line or len(raw_line.lstrip("- •")) < 5:
                continue

            if raw_line.lstrip("- •").lower().startswith("[drop]"):
                dropped += 1
                continue

            is_merged = raw_line.lstrip("- •").lower().startswith("[merged]")
            if is_merged:
                raw_line = raw_line.lstrip("- •")[len("[merged]"):].strip()
                merged += 1
            else:
                kept += 1

            line, category = self._parse_insight_line(raw_line)

            # Strip date annotations the LLM might echo back
            line = re.sub(r'\(added \d{4}-\d{2}-\d{2}\)', '', line).strip() if line else ""

            if line and len(line) >= 5:
                new_insights.append({"content": line, "category": category})

        # Sanity check: abort if LLM returned too few insights (likely garbage)
        if len(new_insights) == 0:
            return self._error(502, "Refinement produced no usable insights — aborted to protect existing data")
        if len(new_insights) < len(old_ids) * 0.3:
            return self._error(502, f"Refinement produced too few insights ({len(new_insights)} vs {len(old_ids)}) — aborted")

        # Replace: mark old as removed, insert new in a single transaction
        try:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" * len(old_ids))
            db.execute(f"UPDATE user_insights SET state='removed' WHERE id IN ({placeholders})", old_ids)
            for ins in new_insights:
                insight_id = uuid.uuid4().hex[:12]
                db.execute(
                    """INSERT INTO user_insights (id, user_id, content, category, created_at, last_used)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (insight_id, user["id"], ins["content"], ins["category"], now, now),
                )
            db.execute("COMMIT")
        except Exception as e:
            db.execute("ROLLBACK")
            log.error(f"Refinement DB write failed: {e}")
            return self._error(500, "refinement failed")
        result = {"dropped": dropped, "merged": merged, "kept": kept, "total": len(new_insights)}
        log.debug(f"memory: refined: {result}")
        self._json_response(200, result)

    # Per-row size caps for tool-call persistence.  MCP search results
    # commonly run 30-80 KB each; without a cap a multi-turn conversation
    # with parallel searches inflates SQLite by tens of MB per chat.  Caps
    # are large enough to preserve any "small" result intact and any
    # "large" result as a truncated preview the SPA can still render.
    _TOOL_ARGS_PERSIST_CAP   = 8 * 1024
    _TOOL_OUTPUT_PERSIST_CAP = 32 * 1024

    @staticmethod
    def _cap_for_persist(value, cap: int):
        """Serialise ``value`` to JSON and truncate to ``cap`` bytes if
        oversize, returning a sentinel JSON envelope on truncation.

        The envelope keeps the round-trip parseable: SPA reads JSON, sees
        ``{"truncated": true, ...}``, renders the preview + size marker.
        """
        if value is None:
            return None
        try:
            blob = json.dumps(value)
        except (TypeError, ValueError):
            blob = json.dumps(str(value))
        if len(blob) <= cap:
            return blob
        return json.dumps({
            "truncated":     True,
            "preview":       blob[: max(1, cap - 256)],
            "original_size": len(blob),
        })

    def _persist_chat_messages(self, db, chat_id, user_input, content, tool_calls, response_id, usage, now=None, injected_insight_ids=None, assistant_status=None):
        """Persist user message, tool calls, and assistant response.

        ``assistant_status`` defaults to NULL (= "completed") and is set
        to "interrupted" by the orphan-stream recovery path so the SPA
        can render a retry affordance on partial turns.
        """
        if now is None:
            now = time.time()
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        # Extract text + structured markers from array input.  We don't
        # store base64 image/audio payloads in SQLite — they'd inflate
        # the DB by orders of magnitude — but we DO emit one marker per
        # part so the history view can render "image attached" /
        # "audio attached" affordances rather than silently losing them
        # (which is what the prior code did for any type the count
        # ignored, e.g. a future "audio").  Unknown types get a generic
        # marker so a `git log` of this column shows up the gap.
        if isinstance(user_input, list):
            persist_chunks: list[str] = []
            for item in user_input:
                if not isinstance(item, dict):
                    continue
                t = item.get("type") or ""
                if t in ("text", "message"):
                    txt = item.get("text") or item.get("content") or ""
                    if txt:
                        persist_chunks.append(txt)
                elif t == "image":
                    persist_chunks.append("[image attached]")
                elif t:
                    persist_chunks.append(f"[{t} attached]")
            persist_text = "\n".join(persist_chunks)
        else:
            persist_text = user_input
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO messages (chat_id,role,content,token_count,created_at) VALUES (?,?,?,?,?)",
                (chat_id, "user", persist_text, prompt_tokens or None, now),
            )
            for tc in tool_calls:
                db.execute(
                    "INSERT INTO messages (chat_id,role,name,args,output,created_at) VALUES (?,?,?,?,?,?)",
                    (chat_id, "tool", tc.get("tool", ""),
                     self._cap_for_persist(tc.get("arguments"), self._TOOL_ARGS_PERSIST_CAP),
                     self._cap_for_persist(tc.get("output"),    self._TOOL_OUTPUT_PERSIST_CAP),
                     now),
                )
            # Persist the assistant row even on empty content WHEN the
            # stream was interrupted — the row's mere presence (with status
            # marker) tells the SPA to render a Retry affordance instead of
            # silently swallowing the failed turn.  Healthy turns require
            # non-empty content as before.
            if content or assistant_status:
                assistant_message_id = db.execute(
                    "INSERT INTO messages (chat_id,role,content,token_count,created_at,status) VALUES (?,?,?,?,?,?)",
                    (chat_id, "assistant", content or "",
                     completion_tokens or None, now, assistant_status),
                ).lastrowid
                if injected_insight_ids:
                    db.executemany(
                        "INSERT INTO insight_activations (message_id, insight_id, created_at) VALUES (?,?,?)",
                        [(assistant_message_id, iid, now) for iid in injected_insight_ids]
                    )
            db.execute(
                "UPDATE chats SET response_id=?, updated_at=? WHERE id=?",
                (response_id, now, chat_id),
            )
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            log.error(f"Failed to persist chat messages: {e}")

    def _apply_feedback(self, db, message_id, user_id, rating):
        """Upsert feedback and adjust insight weights atomically.
        rating: 1 (up), -1 (down), 0 (remove)
        Returns new rating on success, None on auth failure.
        """
        try:
            db.execute("BEGIN IMMEDIATE")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise
        try:
            ok = db.execute("""
                SELECT 1 FROM messages m
                JOIN chats c ON m.chat_id = c.id
                WHERE m.id = ? AND c.user_id = ?
            """, (message_id, user_id)).fetchone()
            if not ok:
                db.execute("ROLLBACK")
                return None  # 403

            prev = db.execute(
                "SELECT rating FROM message_feedback WHERE message_id = ? AND user_id = ?",
                (message_id, user_id)
            ).fetchone()
            prev_rating = prev[0] if prev else 0

            if rating == 0:
                db.execute(
                    "DELETE FROM message_feedback WHERE message_id = ? AND user_id = ?",
                    (message_id, user_id)
                )
            else:
                db.execute("""
                    INSERT INTO message_feedback (message_id, user_id, rating, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(message_id, user_id) DO UPDATE
                      SET rating=excluded.rating, created_at=excluded.created_at
                """, (message_id, user_id, rating, time.time()))

            delta = rating - prev_rating
            if delta != 0:
                activations = db.execute(
                    "SELECT insight_id FROM insight_activations WHERE message_id = ?",
                    (message_id,)
                ).fetchall()
                for (insight_id,) in activations:
                    if delta > 0:
                        db.execute(
                            "UPDATE user_insights SET ups = MAX(ups + 0.5, 0), last_feedback_at = ? WHERE id = ?",
                            (time.time(), insight_id)
                        )
                    else:
                        db.execute(
                            "UPDATE user_insights SET downs = MAX(downs + 0.5, 0), last_feedback_at = ? WHERE id = ?",
                            (time.time(), insight_id)
                        )
            db.execute("COMMIT")
            return rating
        except Exception:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def _post_message_feedback(self, message_id, body):
        user = self._require_auth()
        if not user:
            return
        rating = body.get("rating")
        if rating not in (-1, 0, 1):
            return self._error(400, "rating must be -1, 0, or 1")
        db = get_db()
        result = self._apply_feedback(db, message_id, user["id"], rating)
        if result is None:
            return self._error(403, "not your message")
        self._json_response(200, {"ok": True, "rating": result})

    def _json_response(self, code, data):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str): data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    _file_cache: dict = {}  # {filename: (raw_bytes, gzipped_bytes, mtime, etag)}
    _file_cache_lock: threading.Lock = threading.Lock()
    _distill_lock: threading.Lock = threading.Lock()
    _distilling: set = set()  # chat_ids currently being distilled

    # Per-model record of LM Studio chat parameters the model rejects with
    # "does not (support|expose) ... configuration".  Populated lazily from
    # the first 400 response per model+param pair; subsequent requests for
    # the same model strip those params up front so we never burn another
    # round-trip on a known-rejection.  Kept as ``{model_key: set[param]}``
    # rather than a reasoning-specific scalar so the same mechanism handles
    # any future param LM Studio gates per model (vision-only models that
    # reject ``images``, tool-call-only models that reject ``integrations``,
    # etc.).  Also surfaced via /api/models so the SPA can disable matching
    # UI controls — no more dropdowns whose value the server silently drops.
    _unsupported_params: dict = {}
    _unsupported_params_lock: threading.Lock = threading.Lock()

    def _serve_file(self, filename, content_type):
        path = os.path.join(os.path.dirname(__file__), filename)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self.send_error(404)
            return
        # Either pull (raw, gz, etag) from the in-memory cache when the
        # mtime matches, or compute them fresh and store.  Branches assign
        # the same three names so the rest of the method can use them
        # unconditionally.
        with self._file_cache_lock:
            entry = self._file_cache.get(filename)
            fresh = entry is not None and entry[2] == st.st_mtime
        if fresh:
            raw, gz, _mtime, etag = entry  # type: ignore[misc]
        else:
            with open(path, "rb") as f:
                raw = f.read()
            gz = gzip.compress(raw, compresslevel=6)
            etag = hashlib.md5(raw, usedforsecurity=False).hexdigest()
            with self._file_cache_lock:
                self._file_cache[filename] = (raw, gz, st.st_mtime, etag)
        # ETag: return 304 if client has current version
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self._send_security_headers()
            self.end_headers()
            return
        accept_enc = self.headers.get("Accept-Encoding", "")
        use_gz = "gzip" in accept_enc and len(gz) < len(raw)
        data = gz if use_gz else raw
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # Upstream HTTP wrapper (class fix for the 2026-05-16 login-bounce bug).
    # ALL future proxy paths to LM Studio MUST go through this — never call
    # ``urllib.request.urlopen`` against LM Studio directly from a request
    # handler.  The wrapper centralises:
    #   - Upstream 401/403 translation to 502 (so the SPA's ``apiFetch``
    #     interceptor never sees an HTTP 401 it didn't itself originate,
    #     which prevented an infinite login-bounce loop when our LM Studio
    #     token was missing or rejected).
    #   - Network errors translate to a uniform 502 body.
    #   - 4xx other than 401/403 propagate verbatim (the universal
    #     capability gate depends on structured 400s flowing through).
    #
    # Returns (response_bytes, error_dict).  Exactly one is None.  Error
    # dict shape: ``{"code": int, "message": str}``.  Caller decides
    # whether to write the upstream bytes verbatim or parse them first.
    # ------------------------------------------------------------------
    def _proxy_lmstudio(self, req: urllib.request.Request, *, timeout: int = 10):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                hint = ("LM Studio rejected our API token.  Open Settings → "
                        "LM Studio API key and paste a valid one, or unset "
                        "LM Studio's API-key requirement.")
                log.warning(f"_proxy_lmstudio: upstream {e.code} — {hint}")
                return None, {"code": 502, "message": hint}
            # Pass through other 4xx (structured 400s, 404 etc.) so the
            # caller can decide what to do — the capability gate parses
            # 400 bodies, /models prefers verbatim, etc.
            try:
                body = e.read()
            except Exception:
                body = b""
            return None, {"code": e.code, "message": body.decode("utf-8", errors="replace")
                                                 if body else f"upstream HTTP {e.code}"}
        except Exception as e:
            log.error(f"_proxy_lmstudio: {e}")
            return None, {"code": 502, "message": "upstream service unavailable"}

    def _proxy_get(self, path, user_id=None):
        headers = {}
        token = self._get_lmstudio_token(user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{LMSTUDIO}{path}", headers=headers)
        body, err = self._proxy_lmstudio(req, timeout=10)
        if err:
            return self._error(err["code"], err["message"])
        # Guard against `None` AND `b""` — both would land the SPA in
        # ``JSON.parse("")`` territory.  Locks ``_proxy_lmstudio``'s
        # success contract (non-empty bytes) at the call site.
        if not body:
            return self._error(502, "upstream returned empty response")
        self._json_response(200, body)

    def _get_models(self):
        """Fetch LM Studio's /api/v1/models and augment each LLM entry with
        ``capabilities.unsupported_params`` — the list of chat parameters
        we've empirically observed the model rejects.

        LM Studio doesn't (yet) expose per-model param capabilities in its
        own metadata — the feature request is open at
        https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1659.
        We populate a server-side cache lazily on the first 400 response
        per (model, param) pair (see the retry block in
        ``_handle_chat_stream``) and surface it here so the SPA can disable
        matching UI controls — no more dropdowns whose value the server
        silently drops on every subsequent request.
        """
        user = self._get_user() or {}
        user_id = user.get("id")
        headers = {}
        token = self._get_lmstudio_token(user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{LMSTUDIO}/api/v1/models", headers=headers)
        body, err = self._proxy_lmstudio(req, timeout=10)
        if err:
            return self._error(err["code"], err["message"])
        if not body:
            return self._error(502, "upstream returned empty response")
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as e:
            log.error(f"proxy /api/v1/models: malformed JSON: {e}")
            return self._error(502, "upstream returned malformed response")

        # Proactive seed: any LLM whose ``capabilities.reasoning`` object
        # is absent cannot accept a ``reasoning`` parameter at all — LM
        # Studio returns the documented "does not expose reasoning
        # configuration" 400.  Mark it in the cache now (and persist) so
        # the very first chat against that model goes out without the
        # reasoning param, no retry round-trip needed.  This is the
        # capability gate the operator asked for — runtime cache stays as
        # the fallback for params LM Studio doesn't yet expose flags for
        # (top_p, top_k, etc.).
        newly_seeded: list = []
        for m in payload.get("models", []) or []:
            if m.get("type") != "llm":
                continue
            caps = m.get("capabilities") or {}
            if "reasoning" not in caps:
                model_key = m.get("key") or ""
                if not model_key:
                    continue
                with Handler._unsupported_params_lock:
                    existing = Handler._unsupported_params.setdefault(model_key, set())
                    if "reasoning" not in existing:
                        existing.add("reasoning")
                        newly_seeded.append(model_key)
        for k in newly_seeded:
            _persist_param_blacklist(k, "reasoning", "capability_gate")
        if newly_seeded:
            log.info(
                f"unsupported_params: capability-seeded reasoning for "
                f"{len(newly_seeded)} model(s) lacking capabilities.reasoning"
            )

        # Snapshot the cache once so the augmentation is consistent across
        # all entries in the response.
        with Handler._unsupported_params_lock:
            snapshot = {k: list(v) for k, v in Handler._unsupported_params.items()}

        for m in payload.get("models", []) or []:
            if m.get("type") != "llm":
                continue
            caps = m.setdefault("capabilities", {})
            caps["unsupported_params"] = sorted(snapshot.get(m.get("key"), []))

        self._json_response(200, payload)

    def log_message(self, format, *args):
        # Suppress the default ``"GET / HTTP/1.1" 200 -`` line — our
        # ``log_request`` override above emits a structured ``req_id=...``
        # line at INFO that covers the same ground.  Keep this method as a
        # silent override so BaseHTTPRequestHandler's ``log_error`` fallback
        # (called by ``send_error``) doesn't double-log over stderr.
        pass


def _pid_file():
    """Return PID file path next to the database, scoped by port."""
    return os.path.join(os.path.dirname(DB_PATH) or ".", f".lm_chat_{PORT}.pid")

def _kill_stale_server():
    """If a previous lm-chat is still on our port, kill it."""
    pidfile = _pid_file()
    # Check PID file first
    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile).read().strip())
            if old_pid != os.getpid():
                # Verify PID is actually server.py before killing
                try:
                    cmdline = subprocess.check_output(
                        ["ps", "-p", str(old_pid), "-o", "command="], text=True
                    )
                    if "server.py" not in cmdline:
                        log.info(f"Stale PID file (PID {old_pid} is not server.py) — removing")
                        try:
                            os.unlink(pidfile)
                        except OSError:
                            pass
                        raise OSError("not our process")  # skip to fallback
                except subprocess.CalledProcessError:
                    # Process doesn't exist — stale PID file
                    try:
                        os.unlink(pidfile)
                    except OSError:
                        pass
                    raise OSError("process gone") from None  # skip to fallback
                os.kill(old_pid, signal.SIGTERM)
                log.info(f"Stopped previous lm-chat (PID {old_pid})")
                for _ in range(20):  # wait up to 2s
                    time.sleep(0.1)
                    try:
                        os.kill(old_pid, 0)
                    except OSError:
                        break
                else:
                    os.kill(old_pid, signal.SIGKILL)
                    log.warning(f"Force-killed stale lm-chat (PID {old_pid})")
        except (ValueError, OSError):
            pass  # stale file, process already gone
    # Fallback: check if anything is on our port (covers non-PID-file cases)
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1)
        s.connect(("127.0.0.1", PORT))
        s.close()
        # Something is listening — only kill it if it's actually server.py
        try:
            out = subprocess.check_output(["lsof", "-ti", f":{PORT}"], text=True).strip()
            for pid_str in out.splitlines():
                pid = int(pid_str)
                if pid == os.getpid():
                    continue
                try:
                    cmdline = open(f"/proc/{pid}/cmdline").read().replace("\x00", " ")
                except FileNotFoundError:
                    # macOS: use ps
                    cmdline = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
                if "server.py" in cmdline:
                    os.kill(pid, signal.SIGTERM)
                    log.info(f"Stopped process {pid} on port {PORT}")
                    time.sleep(0.5)
                else:
                    log.warning(f"Port {PORT} is in use by non-lm-chat process {pid} — not killing it")
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            log.warning(f"Port {PORT} is in use but couldn't identify the process — start may fail")
    except (ConnectionRefusedError, OSError):
        pass  # port is free
    finally:
        s.close()

def _write_pid():
    """Write current PID to file."""
    try:
        with open(_pid_file(), "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        log.warning(f"Could not write PID file: {e}")

def _remove_pid():
    """Remove PID file on shutdown."""
    try:
        os.remove(_pid_file())
    except OSError:
        pass

if __name__ == "__main__":
    init_db()
    # Restore the per-(model, param) blacklist learned in previous runs so
    # we don't waste a 400-roundtrip per pair after every restart.  Done
    # before serve_forever() so the very first request after boot sees a
    # warm cache.
    try:
        _boot_db = get_db()
        Handler._unsupported_params = _load_param_blacklist(_boot_db)
        if Handler._unsupported_params:
            _pair_count = sum(len(v) for v in Handler._unsupported_params.values())
            log.info(
                f"unsupported_params: loaded {_pair_count} pair(s) across "
                f"{len(Handler._unsupported_params)} model(s) from DB"
            )
    except Exception as e:
        log.warning(f"could not load param blacklist at boot: {e}")
    _kill_stale_server()
    server = PooledHTTPServer(("0.0.0.0", PORT), Handler)
    _write_pid()
    log.info(f"lm-chat running on http://localhost:{PORT}")
    log.info(f"Proxying to LM Studio at {LMSTUDIO}")
    log.info(f"Chat DB: {DB_PATH}")
    log.info(f"Logs: {LOG_DIR} (max {LOG_MAX_BYTES // 1024 // 1024}MB x {LOG_BACKUP_COUNT} files)")
    if AUTH_ENABLED:
        log.info("Authentication: ENABLED (set LM_CHAT_AUTH=false to disable)")
        db = get_db()
        bootstrap_admin_if_needed(db)
    else:
        # Safety gate: auth-disabled mode is only safe on a single-user DB.
        # If anyone has been writing data under a non-default user_id (i.e.
        # auth was previously enabled and real users existed), turning auth
        # off would expose all of their chats/insights/pins to whoever hits
        # the server.  Refuse to start in that configuration.
        _db_safety = get_db()
        _multiuser_count = _db_safety.execute(
            "SELECT COUNT(*) FROM users WHERE username != 'default'"
        ).fetchone()[0]
        if _multiuser_count > 0:
            log.error("=" * 60)
            log.error("REFUSING TO START: LM_CHAT_AUTH=false on a multi-user DB")
            log.error(
                f"  Found {_multiuser_count} non-default users in {DB_PATH}."
            )
            log.error("  Auth-disabled mode shows the same data to every visitor.")
            log.error("  Either set LM_CHAT_AUTH=true, point LM_CHAT_DB at a")
            log.error("  fresh path, or delete the existing DB and re-create it.")
            log.error("=" * 60)
            sys.exit(2)
        log.info("Authentication: disabled (set LM_CHAT_AUTH=true to enable)")

    def shutdown_handler(signum, frame):
        log.info("Shutting down gracefully...")
        # Set the internal flag directly — server.shutdown() deadlocks when
        # called from a signal handler in the same thread as serve_forever().
        # The attribute name is BaseServer's name-mangled private member;
        # type checkers don't see through the mangling.
        server._BaseServer__shutdown_request = True  # type: ignore[attr-defined]

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _remove_pid()
        log.info("Server stopped.")
