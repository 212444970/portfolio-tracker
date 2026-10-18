import json
import logging
import math
import os
import secrets
import sqlite3
import uuid
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_file
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Load .env for local development (ignored if not installed / no .env file)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

USE_FIREBASE = bool(os.environ.get("FIREBASE_CREDENTIALS"))


# ---------- Firebase init (pouze pokud jsou credentials) ----------

if USE_FIREBASE:
    import firebase_admin
    from firebase_admin import credentials, firestore

    def _init_firebase():
        cred_json = os.environ.get("FIREBASE_CREDENTIALS")
        cred = credentials.Certificate(json.loads(cred_json))
        firebase_admin.initialize_app(cred)
        return firestore.client()

    _fs = _init_firebase()
    COLL = "positions"


# ---------- SQLite fallback ----------

DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(
    os.path.dirname(__file__), "portfolio.db"
)

@contextmanager
def _sqlite_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

REQUIRED_COLS = {"id", "ticker", "name", "position_type", "shares",
                 "buy_price", "buy_currency", "currency", "added_at"}

def _sqlite_init():
    with _sqlite_conn() as conn:
        needs_recreate = False
        try:
            cols = {row[1]: row[2].upper()
                    for row in conn.execute("PRAGMA table_info(positions)")}
            if not REQUIRED_COLS.issubset(cols.keys()) or cols.get("id") != "TEXT":
                needs_recreate = True
        except sqlite3.OperationalError:
            needs_recreate = True

        if needs_recreate:
            conn.execute("DROP TABLE IF EXISTS positions")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id           TEXT PRIMARY KEY,
                ticker       TEXT NOT NULL,
                name         TEXT NOT NULL,
                position_type TEXT NOT NULL DEFAULT 'own',
                shares       REAL NOT NULL DEFAULT 0,
                buy_price    REAL NOT NULL DEFAULT 0,
                buy_currency TEXT NOT NULL DEFAULT 'USD',
                currency     TEXT NOT NULL DEFAULT 'USD',
                quote_type   TEXT NOT NULL DEFAULT 'EQUITY',
                added_at     TEXT NOT NULL
            )
        """)
        try:
            conn.execute("ALTER TABLE positions ADD COLUMN quote_type TEXT NOT NULL DEFAULT 'EQUITY'")
        except Exception:
            pass

if not USE_FIREBASE:
    _sqlite_init()


# ---------- Auth ----------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if not APP_PASSWORD or request.form.get("password") == APP_PASSWORD:
            session["ok"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Nesprávné heslo"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- FX rate cache ----------

_fx_cache: dict[str, float] = {}
_fx_ts: dict[str, float] = {}
FX_TTL = 3600


def get_fx_rate(from_currency: str, to_currency: str = "CZK") -> float:
    if from_currency == to_currency:
        return 1.0
    key = f"{from_currency}{to_currency}"
    now = datetime.now().timestamp()
    if key in _fx_cache and now - _fx_ts.get(key, 0) < FX_TTL:
        return _fx_cache[key]
    tk = yf.Ticker(f"{from_currency}{to_currency}=X")
    rate = None
    try:
        info = tk.info
        rate = info.get("regularMarketPrice") or info.get("bid") or info.get("ask")
    except Exception as exc:
        logger.warning("FX info failed %s: %s", key, exc)
    if not rate:
        try:
            rate = tk.fast_info.last_price
        except Exception as exc:
            logger.warning("FX fast_info failed %s: %s", key, exc)
    if rate:
        logger.info("FX rate %s = %.4f", key, rate)
        _fx_cache[key] = float(rate)
        _fx_ts[key] = now
        return float(rate)
    fallback = _fx_cache.get(key, 1.0)
    logger.error("FX rate fetch failed for %s, using %.4f", key, fallback)
    return fallback


# ---------- Storage helpers (Firebase nebo SQLite) ----------

def get_positions() -> list[dict]:
    if USE_FIREBASE:
        try:
            docs = _fs.collection(COLL).order_by(
                "added_at", direction=firestore.Query.DESCENDING
            ).stream()
            result = []
            for doc in docs:
                p = doc.to_dict()
                p["id"] = doc.id
                if hasattr(p.get("added_at"), "isoformat"):
                    p["added_at"] = p["added_at"].isoformat()
                result.append(p)
            logger.info("Loaded %d positions from Firestore", len(result))
            return result
        except Exception as exc:
            logger.exception("Firestore read failed: %s", exc)
            return []
    else:
        with _sqlite_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions ORDER BY added_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def add_position_doc(data: dict) -> None:
    if USE_FIREBASE:
        data["added_at"] = firestore.SERVER_TIMESTAMP
        _fs.collection(COLL).add(data)
    else:
        data["id"] = str(uuid.uuid4())
        data["added_at"] = datetime.now().isoformat()
        with _sqlite_conn() as conn:
            conn.execute("""
                INSERT INTO positions
                    (id, ticker, name, position_type, shares, buy_price, buy_currency, currency, quote_type, added_at)
                VALUES
                    (:id, :ticker, :name, :position_type, :shares, :buy_price, :buy_currency, :currency, :quote_type, :added_at)
            """, data)


def update_name_doc(doc_id: str, name: str) -> None:
    if USE_FIREBASE:
        _fs.collection(COLL).document(doc_id).update({"name": name})
    else:
        with _sqlite_conn() as conn:
            conn.execute("UPDATE positions SET name=? WHERE id=?", (name, doc_id))


def update_type_doc(doc_id: str, quote_type: str) -> None:
    if USE_FIREBASE:
        _fs.collection(COLL).document(doc_id).update({"quote_type": quote_type})
    else:
        with _sqlite_conn() as conn:
            conn.execute("UPDATE positions SET quote_type=? WHERE id=?", (quote_type, doc_id))


def update_position_doc(doc_id: str, shares: float, buy_price: float) -> None:
    if USE_FIREBASE:
        _fs.collection(COLL).document(doc_id).update({
            "shares": shares,
            "buy_price": buy_price,
        })
    else:
        with _sqlite_conn() as conn:
            conn.execute(
                "UPDATE positions SET shares=?, buy_price=? WHERE id=?",
                (shares, buy_price, doc_id),
            )


def find_existing_own(ticker: str) -> dict | None:
    """Vrátí první 'own' pozici se shodným tickerem, nebo None."""
    for p in get_positions():
        if p["ticker"] == ticker and p.get("position_type", "own") == "own":
            return p
    return None


def delete_position_doc(doc_id: str) -> None:
    if USE_FIREBASE:
        _fs.collection(COLL).document(doc_id).delete()
    else:
        with _sqlite_conn() as conn:
            conn.execute("DELETE FROM positions WHERE id = ?", (doc_id,))


def _migration_authorized() -> bool:
    token = os.environ.get("PORTFOLIO_MIGRATION_TOKEN")
    auth = request.authorization
    return bool(
        token
        and auth
        and auth.username == "portfolio-migration"
        and secrets.compare_digest(auth.password or "", token)
    )


def _migration_auth_error():
    if not os.environ.get("PORTFOLIO_MIGRATION_TOKEN"):
        return jsonify({"error": "Portfolio migration is not configured"}), 503
    response = jsonify({"error": "Migration credentials required"})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Basic realm="Portfolio migration"'
    return response


@app.route("/migration/export")
def export_portfolio():
    if not _migration_authorized():
        return _migration_auth_error()
    if USE_FIREBASE:
        return jsonify({"error": "SQLite export is unavailable when Firebase is enabled"}), 409

    payload = {
        "format_version": 1,
        "positions": get_positions(),
    }
    return send_file(
        BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name="portfolio-export.json",
    )


@app.route("/migration/import", methods=["GET", "POST"])
def import_portfolio():
    if not _migration_authorized():
        return _migration_auth_error()
    if USE_FIREBASE:
        return jsonify({"error": "SQLite import is unavailable when Firebase is enabled"}), 409
    if request.method == "GET":
        return render_template("migration.html")

    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "Upload the export as a file field named 'file'"}), 400
    try:
        payload = json.load(upload.stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify({"error": "Invalid JSON export"}), 400
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        return jsonify({"error": "Unsupported portfolio export format"}), 400
    positions = payload.get("positions")
    if not isinstance(positions, list):
        return jsonify({"error": "Export must contain a positions list"}), 400

    required = {
        "id", "ticker", "name", "position_type", "shares", "buy_price",
        "buy_currency", "currency", "added_at",
    }
    allowed = required | {"quote_type"}
    rows = []
    for position in positions:
        if not isinstance(position, dict) or not required.issubset(position):
            return jsonify({"error": "An exported position is missing required fields"}), 400
        row = {key: position[key] for key in allowed if key in position}
        if (
            not all(isinstance(row[key], str) and row[key] for key in ("id", "ticker", "name", "added_at"))
            or row["position_type"] not in ("own", "watch")
            or not isinstance(row["shares"], (int, float))
            or isinstance(row["shares"], bool)
            or not math.isfinite(row["shares"])
            or not isinstance(row["buy_price"], (int, float))
            or isinstance(row["buy_price"], bool)
            or not math.isfinite(row["buy_price"])
        ):
            return jsonify({"error": "An exported position contains invalid data"}), 400
        row.setdefault("quote_type", "EQUITY")
        rows.append(row)

    try:
        with _sqlite_conn() as conn:
            if conn.execute("SELECT 1 FROM positions LIMIT 1").fetchone():
                return jsonify({"error": "Target portfolio is not empty; import was not applied"}), 409
            conn.executemany(
                """
                INSERT INTO positions
                    (id, ticker, name, position_type, shares, buy_price, buy_currency,
                     currency, quote_type, added_at)
                VALUES
                    (:id, :ticker, :name, :position_type, :shares, :buy_price, :buy_currency,
                     :currency, :quote_type, :added_at)
                """,
                rows,
            )
    except sqlite3.IntegrityError:
        logger.exception("Portfolio migration import failed due to a database constraint")
        return jsonify({"error": "Import failed because the export contains duplicate or invalid records"}), 400

    logger.info("Imported %d portfolio positions", len(rows))
    return jsonify({"ok": True, "imported": len(rows)})


# ---------- Fundamentals / recommendation ----------

def compute_recommendation(info: dict, hist) -> dict:
    """Score fundamental & technical signals; return recommendation dict."""
    score = 0
    signals: list[dict] = []

    def add(name: str, present, display: str, pts: int, note: str, short: str = None):
        nonlocal score
        if present is None:
            return
        score += pts
        raw = present if isinstance(present, (int, float)) else 0
        signals.append({"name": name, "value": display, "pts": pts, "note": note, "raw": raw,
                         "short": short if short is not None else display})

    # ── P/E (trailing) ──
    pe = info.get("trailingPE")
    if pe is not None:
        if pe < 0:
            add("P/E", pe, f"{pe:.1f}×", -2, "Záporný zisk")
        elif pe < 15:
            add("P/E", pe, f"{pe:.1f}×", +2, "Podhodnoceno")
        elif pe < 25:
            add("P/E", pe, f"{pe:.1f}×", +1, "Přiměřené")
        elif pe < 40:
            add("P/E", pe, f"{pe:.1f}×",  0, "Mírně drahé")
        else:
            add("P/E", pe, f"{pe:.1f}×", -1, "Nadhodnoceno")

    # ── Forward P/E ──
    fpe = info.get("forwardPE")
    if fpe and fpe > 0:
        if fpe < 15:
            add("Forw. P/E", fpe, f"{fpe:.1f}×", +2, "Atraktivní výhled zisku")
        elif fpe < 25:
            add("Forw. P/E", fpe, f"{fpe:.1f}×", +1, "Přiměřený výhled")
        else:
            add("Forw. P/E", fpe, f"{fpe:.1f}×", -1, "Drahý výhled")

    # ── P/B ──
    pb = info.get("priceToBook")
    if pb is not None:
        if pb < 1:
            add("P/B", pb, f"{pb:.2f}×", +1, "Pod účetní hodnotou")
        elif pb < 3:
            add("P/B", pb, f"{pb:.2f}×",  0, "Přiměřené")
        elif pb < 7:
            add("P/B", pb, f"{pb:.2f}×",  0, "Drahé")
        else:
            add("P/B", pb, f"{pb:.2f}×", -1, "Výrazně nadhodnoceno")

    # ── ROE ──
    roe = info.get("returnOnEquity")
    if roe is not None:
        p = roe * 100
        if p > 20:
            add("ROE", roe, f"{p:.1f}%", +2, "Výborná rentabilita")
        elif p > 10:
            add("ROE", roe, f"{p:.1f}%", +1, "Dobrá rentabilita")
        elif p >= 0:
            add("ROE", roe, f"{p:.1f}%",  0, "Slabá rentabilita")
        else:
            add("ROE", roe, f"{p:.1f}%", -2, "Záporná rentabilita")

    # ── Profit margin ──
    pm = info.get("profitMargins")
    if pm is not None:
        p = pm * 100
        if p > 20:
            add("Zisk. marže", pm, f"{p:.1f}%", +2, "Výborná marže")
        elif p > 10:
            add("Zisk. marže", pm, f"{p:.1f}%", +1, "Dobrá marže")
        elif p >= 0:
            add("Zisk. marže", pm, f"{p:.1f}%",  0, "Slabá marže")
        else:
            add("Zisk. marže", pm, f"{p:.1f}%", -2, "Záporná marže")

    # ── Revenue growth ──
    rg = info.get("revenueGrowth")
    if rg is not None:
        p = rg * 100
        if p > 20:
            add("Růst tržeb", rg, f"{p:+.1f}%", +2, "Rychlý růst")
        elif p > 5:
            add("Růst tržeb", rg, f"{p:+.1f}%", +1, "Solidní růst")
        elif p >= 0:
            add("Růst tržeb", rg, f"{p:+.1f}%",  0, "Pomalý růst")
        else:
            add("Růst tržeb", rg, f"{p:+.1f}%", -1, "Pokles tržeb")

    # ── Earnings growth ──
    eg = info.get("earningsGrowth")
    if eg is not None:
        p = eg * 100
        if p > 20:
            add("Růst zisku", eg, f"{p:+.1f}%", +2, "Rychlý růst zisku")
        elif p > 5:
            add("Růst zisku", eg, f"{p:+.1f}%", +1, "Solidní růst zisku")
        elif p >= 0:
            add("Růst zisku", eg, f"{p:+.1f}%",  0, "Pomalý růst zisku")
        else:
            add("Růst zisku", eg, f"{p:+.1f}%", -1, "Pokles zisku")

    # ── Debt / Equity ──
    de = info.get("debtToEquity")
    if de is not None:
        ratio = de / 100  # yfinance returns percentage-form (e.g. 155 → 1.55)
        if ratio < 0.3:
            add("D/E ratio", de, f"{ratio:.2f}",  +1, "Nízká zadluženost")
        elif ratio < 1.0:
            add("D/E ratio", de, f"{ratio:.2f}",   0, "Přiměřená zadluženost")
        elif ratio < 2.0:
            add("D/E ratio", de, f"{ratio:.2f}",  -1, "Vysoká zadluženost")
        else:
            add("D/E ratio", de, f"{ratio:.2f}",  -2, "Velmi vysoká zadluženost")

    # ── Analyst target price ──
    cur_price = info.get("currentPrice") or info.get("regularMarketPrice")
    target = info.get("targetMeanPrice")
    n_anal = info.get("numberOfAnalystOpinions") or 0
    if cur_price and target and n_anal >= 3:
        upside = (target - cur_price) / cur_price * 100
        lbl = f"{target:.2f} ({upside:+.0f}%, {n_anal} anal.)"
        short = f"{upside:+.0f}%"
        if upside > 20:
            add("Cíl analytiků", target, lbl, +2, "Výrazný potenciál růstu", short)
        elif upside > 5:
            add("Cíl analytiků", target, lbl, +1, "Mírný potenciál růstu", short)
        elif upside > -5:
            add("Cíl analytiků", target, lbl,  0, "Blízko cílové ceny", short)
        else:
            add("Cíl analytiků", target, lbl, -1, "Nad cílovou cenou", short)

    # ── Analyst consensus (1=Strong Buy … 5=Strong Sell) ──
    rec_mean = info.get("recommendationMean")
    if rec_mean is not None and n_anal >= 3:
        labels = {1: "Silné Koupit", 2: "Koupit", 3: "Držet", 4: "Prodat", 5: "Silné Prodat"}
        txt = labels.get(round(rec_mean), "—")
        lbl = f"{rec_mean:.1f}/5 — {txt} ({n_anal} anal.)"
        if rec_mean <= 1.8:
            add("Konsensus", rec_mean, lbl, +2, "Analytici: Silné Koupit")
        elif rec_mean <= 2.5:
            add("Konsensus", rec_mean, lbl, +1, "Analytici: Koupit")
        elif rec_mean <= 3.5:
            add("Konsensus", rec_mean, lbl,  0, "Analytici: Držet")
        else:
            add("Konsensus", rec_mean, lbl, -1, "Analytici: Prodat/Prodat")

    # ── RSI 14-day (from price history) ──
    if hist is not None and not hist.empty and len(hist) >= 20:
        close_series = None
        if "Close" in hist.columns:
            close_series = hist["Close"]
        elif "close" in hist.columns:
            close_series = hist["close"]

        if close_series is not None and len(close_series) >= 20:
            recent = list(close_series.iloc[-20:])
            deltas = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
            gains  = [d if d > 0 else 0 for d in deltas[-14:]]
            losses = [-d if d < 0 else 0 for d in deltas[-14:]]
            avg_g  = sum(gains) / 14
            avg_l  = sum(losses) / 14
            rsi = 100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l))
            if rsi <= 30:
                add("RSI 14d", rsi, f"{rsi:.0f}", +2, "Přeprodáno — nákupní příležitost")
            elif rsi <= 45:
                add("RSI 14d", rsi, f"{rsi:.0f}", +1, "Mírně přeprodáno")
            elif rsi <= 60:
                add("RSI 14d", rsi, f"{rsi:.0f}",  0, "Neutrální")
            elif rsi <= 70:
                add("RSI 14d", rsi, f"{rsi:.0f}",  0, "Mírně překoupeno")
            else:
                add("RSI 14d", rsi, f"{rsi:.0f}", -1, "Překoupeno — prodejní signál")

    # ── Beta (informační) ──
    beta = info.get("beta")
    if beta is not None:
        note = ("Nízká volatilita" if beta < 0.8
                else "Průměrná volatilita" if beta < 1.3
                else "Vysoká volatilita")
        add("Beta", beta, f"{beta:.2f}", 0, note)

    # ── 52týdenní pozice ──
    w52h = info.get("fiftyTwoWeekHigh")
    w52l = info.get("fiftyTwoWeekLow")
    if w52h and w52l and cur_price and w52h != w52l:
        pos52 = (cur_price - w52l) / (w52h - w52l) * 100
        if pos52 <= 20:
            add("Pozice 52t", pos52, f"{pos52:.0f}% od dna",  +1, f"Blízko min. ({w52l:.2f})")
        elif pos52 >= 80:
            add("Pozice 52t", pos52, f"{pos52:.0f}% od dna",  -1, f"Blízko max. ({w52h:.2f})")
        else:
            add("Pozice 52t", pos52, f"{pos52:.0f}% od dna",   0, f"Rozsah: {w52l:.2f} – {w52h:.2f}")

    # ── Dividendový výnos (informační) ──
    dy = info.get("dividendYield")
    if dy and dy > 0:
        p = dy * 100
        pts = 1 if p > 4 else 0
        note = ("Výborný dividendový výnos" if p > 4
                else "Dobrý dividendový výnos" if p > 2
                else "Mírný dividendový výnos")
        add("Dividenda", dy, f"{p:.2f}%", pts, note)

    # ── Výsledné doporučení ──
    decisive = [s for s in signals if s["name"] != "Beta"]
    if not decisive:
        rec, rec_cls = "N/A", "neutral"
    elif score >= 4:
        rec, rec_cls = "Nakoupit", "buy"
    elif score >= 0:
        rec, rec_cls = "Držet", "hold"
    else:
        rec, rec_cls = "Prodat", "sell"

    return {
        "score": score,
        "n_signals": len(decisive),
        "recommendation": rec,
        "rec_class": rec_cls,
        "signals": signals,
        "signals_map": {s["name"]: s for s in signals},
    }


# ---------- Market data ----------

_market_cache: dict[str, dict] = {}
_market_cache_ts: dict[str, float] = {}
MARKET_TTL = 900  # 15 minut


def fetch_market_data(ticker: str) -> dict | None:
    now = datetime.now().timestamp()
    if ticker in _market_cache and now - _market_cache_ts.get(ticker, 0) < MARKET_TTL:
        return _market_cache[ticker]

    result = _fetch_uncached(ticker)
    if result:
        _market_cache[ticker] = result
        _market_cache_ts[ticker] = now
    return result


def _fetch_uncached(ticker: str) -> dict | None:
    tk = yf.Ticker(ticker)

    # ── 1. Cena + měna ──────────────────────────────────────────────────────
    price = None
    currency = "USD"
    name = ticker
    info = {}
    try:
        info = tk.info
        price = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("navPrice")
        )
        currency = info.get("currency", "USD") or "USD"
        name = (info.get("longName") or info.get("shortName") or ticker).strip()
    except Exception as exc:
        logger.warning("tk.info failed for %s: %s", ticker, exc)

    # Záložní cesta přes fast_info
    if not price:
        try:
            fi = tk.fast_info
            price = fi.last_price
            currency = getattr(fi, "currency", None) or currency
        except Exception as exc:
            logger.warning("fast_info failed for %s: %s", ticker, exc)

    if not price:
        logger.error("No price found for %s, skipping", ticker)
        return None

    price = float(price)

    # ── 2. Historická data ───────────────────────────────────────────────────
    closes = None
    hist = None
    for _period in ("4y", "2y", "1y", "6mo"):
        try:
            hist = tk.history(period=_period, auto_adjust=True)
            if not hist.empty:
                closes = hist["Close"].dropna()
                if closes.index.tz is not None:
                    closes.index = closes.index.tz_convert("UTC").tz_localize(None)
                logger.info("History OK for %s: %d rows (period=%s)", ticker, len(closes), _period)
                break
            logger.warning("Empty history for %s (period=%s)", ticker, _period)
        except Exception as exc:
            logger.warning("history failed for %s (period=%s): %s", ticker, _period, exc)
    else:
        logger.error("All history attempts failed for %s", ticker)

    def price_n_days_ago(n: int) -> float | None:
        if closes is None:
            return None
        target = datetime.now() - timedelta(days=n)
        past = closes[closes.index <= target]
        return float(past.iloc[-1]) if not past.empty else None

    if closes is not None and len(closes) >= 2:
        prev_close = price_n_days_ago(1) or float(closes.iloc[-2])
    else:
        prev_close = price

    logger.info("Fetched %s: price=%.2f %s, history_rows=%s",
                ticker, price, currency, len(closes) if closes is not None else 0)
    return {
        "price": price,
        "currency": currency,
        "name": name,
        "prev_close": prev_close,
        "price_48h": price_n_days_ago(2),
        "price_7d":  price_n_days_ago(7),
        "price_30d": price_n_days_ago(30),
        "price_6m":  price_n_days_ago(180),
        "price_1y":  price_n_days_ago(365),
        "price_3y":  price_n_days_ago(3 * 365),
        "price_5y":  price_n_days_ago(5 * 365),
        "fundamentals": compute_recommendation(info, hist),
    }


def enrich_position(pos: dict) -> dict:
    pos.setdefault("position_type", "own")
    data = fetch_market_data(pos["ticker"])
    if not data:
        pos["error"] = True
        return pos

    price = data["price"]
    stock_cur = data["currency"]
    stock_to_czk = get_fx_rate(stock_cur)

    def pct(ref):
        if ref is None:
            return None
        return (price - ref) / ref * 100

    pos.update({
        "name": pos.get("name") or data["name"],
        "currency": stock_cur,
        "current_price": price,
        "pct_24h": pct(data["prev_close"]),
        "pct_48h": pct(data["price_48h"]),
        "pct_7d":  pct(data["price_7d"]),
        "pct_30d": pct(data["price_30d"]),
        "pct_6m":  pct(data["price_6m"]),
        "pct_1y":  pct(data["price_1y"]),
        "pct_3y":  pct(data["price_3y"]),
        "pct_5y":  pct(data["price_5y"]),
        "fundamentals": data.get("fundamentals"),
        "error": False,
    })

    if pos["position_type"] == "watch":
        return pos

    shares = pos.get("shares") or 0
    buy = pos.get("buy_price") or 0
    buy_cur = pos.get("buy_currency") or "USD"
    buy_to_czk = get_fx_rate(buy_cur)

    current_value = shares * price
    cost_basis = shares * buy
    current_value_czk = current_value * stock_to_czk
    cost_basis_czk = cost_basis * buy_to_czk
    total_gain_czk = current_value_czk - cost_basis_czk
    total_gain_czk_pct = (total_gain_czk / cost_basis_czk * 100) if cost_basis_czk else 0

    def gain_czk(ref):
        return None if ref is None else (price - ref) * shares * stock_to_czk

    pos.update({
        "buy_currency": buy_cur,
        "current_value": current_value,
        "current_value_czk": current_value_czk,
        "cost_basis": cost_basis,
        "cost_basis_czk": cost_basis_czk,
        "total_gain_czk": total_gain_czk,
        "total_gain_czk_pct": total_gain_czk_pct,
        "gain_24h_czk": gain_czk(data["prev_close"]),
        "gain_48h_czk": gain_czk(data["price_48h"]),
        "gain_7d_czk":  gain_czk(data["price_7d"]),
        "gain_30d_czk": gain_czk(data["price_30d"]),
        "gain_6m_czk":  gain_czk(data["price_6m"]),
        "gain_1y_czk":  gain_czk(data["price_1y"]),
        "gain_3y_czk":  gain_czk(data["price_3y"]),
        "gain_5y_czk":  gain_czk(data["price_5y"]),
        "buy_to_czk": buy_to_czk,
        "stock_to_czk": stock_to_czk,
    })
    return pos


# ---------- Template filters ----------

@app.template_filter("czk")
def czk_fmt(v):
    return f"{v:,.0f}"

@app.template_filter("czk_signed")
def czk_signed_fmt(v):
    return f"{v:+,.0f}"

@app.template_filter("pct")
def pct_fmt(v):
    if v is None:
        return "—"
    return f"{v:+.2f}%"


# ---------- Routes ----------

@app.route("/")
@login_required
def index():
    raw = get_positions()
    logger.info("index: enriching %d positions", len(raw))
    all_pos = [enrich_position(p) for p in raw]
    own   = [p for p in all_pos if p.get("position_type", "own") == "own"]
    watch = [p for p in all_pos if p.get("position_type") == "watch"]

    gain_keys = ["gain_24h_czk", "gain_48h_czk", "gain_7d_czk", "gain_30d_czk",
                 "gain_6m_czk", "gain_1y_czk", "gain_3y_czk"]
    total: dict | None = None
    valid_own = [p for p in own if not p.get("error")]
    if valid_own:
        total = {
            "value": sum(p["current_value_czk"] for p in valid_own),
            "cost":  sum(p["cost_basis_czk"] for p in valid_own),
        }
        for k in gain_keys:
            total[k] = sum(p.get(k) or 0 for p in valid_own)
        total["total_gain"] = total["value"] - total["cost"]
        total["total_gain_pct"] = (total["total_gain"] / total["cost"] * 100) if total["cost"] else 0

        def safe_pct(part, whole):
            return round(part / whole * 100) if whole else 0

        ASSET_LABELS = {
            "EQUITY": "Akcie", "ETF": "ETF", "MUTUALFUND": "ETF",
            "FUTURE": "Komodity", "INDEX": "Index", "CRYPTOCURRENCY": "Krypto",
        }
        by_type: dict[str, dict] = {}
        for p in valid_own:
            label = ASSET_LABELS.get(p.get("quote_type", "EQUITY"), "Akcie")
            if label not in by_type:
                by_type[label] = {"value": 0, "total_gain": 0}
                for k in gain_keys:
                    by_type[label][k] = 0
            by_type[label]["value"]      += p["current_value_czk"]
            by_type[label]["total_gain"] += p["total_gain_czk"]
            for k in gain_keys:
                by_type[label][k] += p.get(k) or 0

        for bt in by_type.values():
            bt["value_pct"]      = safe_pct(bt["value"], total["value"])
            bt["total_gain_pct"] = safe_pct(bt["total_gain"], total["total_gain"]) if total["total_gain"] else 0
            for k in gain_keys:
                bt[f"{k}_pct"] = safe_pct(bt[k], total[k]) if total[k] else 0

        total["by_type"] = dict(sorted(by_type.items(), key=lambda x: -x[1]["value"]))

    return render_template("index.html", own=own, watch=watch, total=total)


@app.route("/add", methods=["POST"])
@login_required
def add_position():
    ticker = request.form.get("ticker", "").strip().upper()
    position_type = request.form.get("position_type", "own").strip()
    if position_type not in ("own", "watch"):
        position_type = "own"
    if not ticker:
        return redirect(url_for("index"))

    shares = 0.0
    buy_price = 0.0
    buy_currency = "USD"
    if position_type == "own":
        try:
            shares = float(request.form.get("shares", ""))
            buy_price = float(request.form.get("buy_price", ""))
        except ValueError:
            return redirect(url_for("index"))
        buy_currency = request.form.get("buy_currency", "USD").upper()
        if buy_currency not in ("CZK", "EUR", "USD"):
            buy_currency = "USD"

    quote_type = request.form.get("quote_type", "EQUITY").strip().upper()
    if quote_type not in ("EQUITY", "ETF", "MUTUALFUND", "FUTURE", "INDEX", "CRYPTOCURRENCY"):
        quote_type = "EQUITY"

    data = fetch_market_data(ticker)

    # ── Agregace: pokud stejný ticker (own) už existuje, přepočítej vážený průměr ──
    if position_type == "own":
        existing = find_existing_own(ticker)
        if existing:
            old_shares = existing.get("shares", 0) or 0
            old_price  = existing.get("buy_price", 0) or 0
            old_cur    = existing.get("buy_currency", buy_currency)
            # Sjednotit měnu: pokud se liší, převést nový nákup na měnu existující pozice
            if old_cur != buy_currency:
                rate_old = get_fx_rate(old_cur)
                rate_new = get_fx_rate(buy_currency)
                buy_price_conv = buy_price * rate_new / rate_old if rate_old else buy_price
            else:
                buy_price_conv = buy_price
            total_shares = old_shares + shares
            avg_price = (old_price * old_shares + buy_price_conv * shares) / total_shares if total_shares else 0
            update_position_doc(existing["id"], round(total_shares, 6), round(avg_price, 6))
            return redirect(url_for("index"))

    add_position_doc({
        "ticker": ticker,
        "name": data["name"] if data else ticker,
        "position_type": position_type,
        "shares": shares,
        "buy_price": buy_price,
        "buy_currency": buy_currency,
        "currency": data["currency"] if data else "USD",
        "quote_type": quote_type,
    })
    return redirect(url_for("index"))


@app.route("/delete/<doc_id>", methods=["POST"])
@login_required
def delete_position(doc_id: str):
    delete_position_doc(doc_id)
    return redirect(url_for("index"))


@app.route("/update-name/<doc_id>", methods=["POST"])
@login_required
def update_name(doc_id: str):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "empty"}), 400
    try:
        update_name_doc(doc_id, name)
        logger.info("update_name: doc=%s name=%s", doc_id, name)
    except Exception as exc:
        logger.exception("update_name failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/update-type/<doc_id>", methods=["POST"])
@login_required
def update_type(doc_id: str):
    data = request.get_json(silent=True) or {}
    qt = data.get("quote_type", "EQUITY")
    valid = ("EQUITY", "ETF", "MUTUALFUND", "FUTURE", "INDEX", "CRYPTOCURRENCY")
    if qt not in valid:
        return jsonify({"error": "invalid type"}), 400
    try:
        update_type_doc(doc_id, qt)
        logger.info("update_type: doc=%s qt=%s", doc_id, qt)
    except Exception as exc:
        logger.exception("update_type failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/search")
@login_required
def search_ticker():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        results = yf.Search(q, news_count=0, max_results=8)
        quotes, seen = [], set()
        for r in results.quotes:
            symbol = r.get("symbol", "")
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            qtype = r.get("quoteType", "")
            if qtype not in ("EQUITY", "ETF", "MUTUALFUND", "INDEX", "FUTURE", "CRYPTOCURRENCY"):
                continue
            quotes.append({
                "symbol": symbol,
                "name": (r.get("longname") or r.get("shortname") or symbol).strip(),
                "type": qtype,
                "exchange": r.get("exchange", ""),
            })
        return jsonify(quotes)
    except Exception:
        logger.exception("Ticker search failed for %r", q)
        return jsonify([])


def _get_price_and_currency(ticker: str) -> tuple[float, str] | None:
    tk = yf.Ticker(ticker)
    try:
        fi = tk.fast_info
        price = fi.last_price
        currency = getattr(fi, "currency", None) or "USD"
        if price:
            return round(float(price), 2), currency
    except Exception:
        pass
    try:
        info = tk.info
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("navPrice")
        currency = info.get("currency", "USD") or "USD"
        if price:
            return round(float(price), 2), currency
    except Exception:
        pass
    return None


@app.route("/prices")
@login_required
def get_prices():
    raw = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()][:10]
    result = {}
    for sym in symbols:
        try:
            pc = _get_price_and_currency(sym)
            if pc:
                result[sym] = {"price": pc[0], "currency": pc[1]}
        except Exception:
            pass
    return jsonify(result)


@app.route("/price/<ticker>")
@login_required
def get_price(ticker: str):
    ticker = ticker.strip().upper()
    try:
        pc = _get_price_and_currency(ticker)
        if pc:
            return jsonify({"price": pc[0], "currency": pc[1]})
        return jsonify({"error": "no price"}), 404
    except Exception:
        return jsonify({"error": "failed"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
