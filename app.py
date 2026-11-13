"""Мониторинг цен на СЗПТ — прототип для хакатона.
Запуск:  python app.py   ->  http://127.0.0.1:5000
"""
import csv
import io
import re
import sqlite3
import statistics
from datetime import datetime

from flask import Flask, g, redirect, render_template, request, url_for

DB_PATH = "prices.db"
DEV_FROM_MEDIAN = 0.20   # сигнал: отклонение от медианы по району больше 20 %
RISE_LIMIT = 0.10        # сигнал: рост в одной точке больше 10 % за 14 дней
ABSURD_RATIO = 3         # цена в 3 раза выше/ниже медианы — вероятна ошибка упаковки

UNIT_NAMES = {"kg": "кг", "l": "л", "sht": "шт"}

# Тестовые справочники (загружаются в пустую базу при первом запуске)
PRODUCTS = [
    ("Хлеб пшеничный", "kg"), ("Мука пшеничная 1 сорт", "kg"), ("Рис шлифованный", "kg"),
    ("Гречка", "kg"), ("Макароны", "kg"), ("Сахар", "kg"), ("Соль", "kg"),
    ("Картофель", "kg"), ("Лук репчатый", "kg"), ("Говядина", "kg"),
    ("Молоко 2,5%", "l"), ("Масло подсолнечное", "l"), ("Яйцо куриное С1", "sht"),
]
OUTLETS = [
    ("Магазин «Береке»", "магазин", "Центр"), ("Супермаркет «Арай»", "магазин", "Центр"),
    ("Центральный рынок", "рынок", "Центр"),
    ("Магазин «Достык»", "магазин", "Левый берег"), ("Минимаркет «Нұр»", "магазин", "Левый берег"),
    ("Рынок «Сырдария»", "рынок", "Левый берег"),
    ("Магазин «Жібек»", "магазин", "Тасбогет"), ("Супермаркет «Асыл»", "магазин", "Тасбогет"),
    ("Рынок «Тасбогет»", "рынок", "Тасбогет"),
    ("Магазин «Самал»", "магазин", "Титова"), ("Минимаркет «Дәм»", "магазин", "Титова"),
    ("Рынок «Титова»", "рынок", "Титова"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, base_unit TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outlets(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, type TEXT NOT NULL, district TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prices(
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  outlet_id INTEGER NOT NULL REFERENCES outlets(id),
  date TEXT NOT NULL,              -- дата наблюдения, ГГГГ-ММ-ДД
  pack_price REAL NOT NULL,        -- цена упаковки, тенге
  pack TEXT NOT NULL,              -- упаковка как ввели: «900 г»
  unit_price REAL NOT NULL,        -- цена за кг / л / шт
  UNIQUE(product_id, outlet_id, date));
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY,
  price_id INTEGER UNIQUE NOT NULL REFERENCES prices(id),
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'новый',
  conclusion TEXT NOT NULL DEFAULT '');
"""

app = Flask(__name__)


# ---------- база данных ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executemany("INSERT INTO products(name, base_unit) VALUES(?,?)", PRODUCTS)
        conn.executemany("INSERT INTO outlets(name, type, district) VALUES(?,?,?)", OUTLETS)
    conn.commit()
    conn.close()


# ---------- нормализация ----------
PACK_UNITS = {  # единица -> (базовая единица, множитель)
    "кг": ("kg", 1), "г": ("kg", 0.001), "гр": ("kg", 0.001),
    "л": ("l", 1), "мл": ("l", 0.001),
    "шт": ("sht", 1), "дес": ("sht", 10), "десяток": ("sht", 10),
}
PACK_RE = re.compile(r"(\d+(?:[.,]\d+)?)?\s*(кг|гр|г|мл|л|шт|десяток|дес)\.?")


def parse_pack(text):
    """'900 г' -> ('kg', 0.9);  '1,5л' -> ('l', 1.5);  'дес' -> ('sht', 10). Иначе None."""
    m = PACK_RE.fullmatch(text.strip().lower())
    if not m:
        return None
    qty = float(m.group(1).replace(",", ".")) if m.group(1) else 1.0
    base, k = PACK_UNITS[m.group(2)]
    return (base, qty * k) if qty > 0 else None


def parse_date(text):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def add_price(conn, date_s, outlet_s, product_s, price_s, pack_s):
    """Проверяет строку и сохраняет цену. Возвращает (id, None) или (None, 'причина ошибки')."""
    d = parse_date(date_s or "")
    if not d:
        return None, f"не распознана дата «{date_s}»"
    product = conn.execute("SELECT * FROM products WHERE lower(name)=lower(?)",
                           ((product_s or "").strip(),)).fetchone()
    if not product:
        return None, f"товара «{product_s}» нет в справочнике"
    outlet = conn.execute("SELECT * FROM outlets WHERE lower(name)=lower(?)",
                          ((outlet_s or "").strip(),)).fetchone()
    if not outlet:
        return None, f"торговой точки «{outlet_s}» нет в справочнике"
    try:
        price = float(str(price_s).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None, f"цена «{price_s}» не число"
    if price <= 0:
        return None, "цена должна быть больше нуля"
    pack = parse_pack(pack_s or "")
    if not pack:
        return None, f"не распознана упаковка «{pack_s}»"
    base, qty = pack
    if base != product["base_unit"]:
        return None, (f"упаковка «{pack_s}» не подходит: товар учитывается в "
                      f"{UNIT_NAMES[product['base_unit']]}")
    try:
        cur = conn.execute(
            "INSERT INTO prices(product_id, outlet_id, date, pack_price, pack, unit_price) "
            "VALUES(?,?,?,?,?,?)",
            (product["id"], outlet["id"], d, price, pack_s.strip(), round(price / qty, 2)))
    except sqlite3.IntegrityError:
        return None, "дубликат: цена этого товара в этой точке на эту дату уже есть"
    return cur.lastrowid, None


# ---------- сигналы для проверки ----------
def check_alerts(conn, price_ids):
    for pid in price_ids:
        r = conn.execute(
            "SELECT p.*, o.district FROM prices p JOIN outlets o ON o.id=p.outlet_id WHERE p.id=?",
            (pid,)).fetchone()
        reasons = []
        # 1. сравнение с медианой всех точек того же района (±3 дня)
        peers = [x[0] for x in conn.execute(
            "SELECT p.unit_price FROM prices p JOIN outlets o ON o.id=p.outlet_id "
            "WHERE p.product_id=? AND o.district=? "
            "AND abs(julianday(p.date)-julianday(?))<=3",
            (r["product_id"], r["district"], r["date"]))]
        if len(peers) >= 3:   # вместе с самой ценой: медиана из 3+ устойчива к одному выбросу
            med = statistics.median(peers)
            ratio = r["unit_price"] / med
            if ratio >= ABSURD_RATIO or ratio <= 1 / ABSURD_RATIO:
                reasons.append(f"цена в {ratio:.1f} раза отличается от медианы района "
                               f"({med:.0f} ₸) — возможна ошибка в упаковке или цене")
            elif abs(ratio - 1) > DEV_FROM_MEDIAN:
                reasons.append(f"отклонение {ratio - 1:+.0%} от медианы района ({med:.0f} ₸)")
        # 2. рост в той же точке
        prev = conn.execute(
            "SELECT unit_price, date FROM prices WHERE product_id=? AND outlet_id=? AND date<? "
            "AND julianday(?)-julianday(date)<=14 ORDER BY date DESC LIMIT 1",
            (r["product_id"], r["outlet_id"], r["date"], r["date"])).fetchone()
        if prev:
            rise = r["unit_price"] / prev["unit_price"] - 1
            if rise > RISE_LIMIT:
                reasons.append(f"рост {rise:+.0%} к цене от {prev['date']} ({prev['unit_price']:.0f} ₸)")
        if reasons:
            conn.execute("INSERT OR IGNORE INTO alerts(price_id, reason) VALUES(?,?)",
                         (pid, "; ".join(reasons)))


# ---------- страницы ----------
@app.route("/")
def compare():
    conn = db()
    products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    districts = [x[0] for x in conn.execute("SELECT DISTINCT district FROM outlets ORDER BY 1")]
    product_id = request.args.get("product", type=int) or (products[0]["id"] if products else 0)
    district = request.args.get("district", "")
    d_from = request.args.get("from", "")
    d_to = request.args.get("to", "")

    sql = ("SELECT p.date, p.unit_price, o.district, o.type FROM prices p "
           "JOIN outlets o ON o.id=p.outlet_id WHERE p.product_id=?")
    params = [product_id]
    if district:
        sql += " AND o.district=?"; params.append(district)
    if d_from:
        sql += " AND p.date>=?"; params.append(d_from)
    if d_to:
        sql += " AND p.date<=?"; params.append(d_to)
    rows = conn.execute(sql + " ORDER BY p.date", params).fetchall()

    def stats(values):
        return {"n": len(values), "min": min(values), "med": statistics.median(values),
                "max": max(values)}

    def group(key):
        groups = {}
        for r in rows:
            groups.setdefault(r[key], []).append(r["unit_price"])
        return {k: stats(v) for k, v in sorted(groups.items())}

    by_date = group("date")
    series = [{"date": d, **s} for d, s in by_date.items()]
    change = None
    if len(series) >= 2:
        change = series[-1]["med"] / series[0]["med"] - 1
    product = next((p for p in products if p["id"] == product_id), None)
    return render_template(
        "compare.html", products=products, districts=districts, product=product,
        district=district, d_from=d_from, d_to=d_to,
        total=stats([r["unit_price"] for r in rows]) if rows else None,
        by_district=group("district"), by_type=group("type"),
        series=series, change=change,
        unit=UNIT_NAMES[product["base_unit"]] if product else "")


@app.route("/input", methods=["GET", "POST"])
def input_prices():
    conn = db()
    report = None
    if request.method == "POST":
        accepted, errors = [], []
        if "file" in request.files and request.files["file"].filename:
            text = request.files["file"].read().decode("utf-8-sig")
            delimiter = ";" if text.count(";") >= text.count(",") else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            for n, row in enumerate(reader, start=2):
                pid, err = add_price(conn, row.get("date"), row.get("outlet"),
                                     row.get("product"), row.get("price"), row.get("pack"))
                if pid:
                    accepted.append(pid)
                else:
                    errors.append({"line": n, "row": row, "error": err})
        else:
            f = request.form
            pid, err = add_price(conn, f["date"], f["outlet"], f["product"], f["price"], f["pack"])
            if pid:
                accepted.append(pid)
            else:
                errors.append({"line": "—", "row": dict(f), "error": err})
        check_alerts(conn, accepted)
        conn.commit()
        n_alerts = 0
        if accepted:
            marks = ",".join("?" * len(accepted))
            n_alerts = conn.execute(
                f"SELECT COUNT(*) FROM alerts WHERE price_id IN ({marks})", accepted).fetchone()[0]
        report = {"accepted": len(accepted), "errors": errors, "alerts": n_alerts}
    return render_template(
        "input.html", report=report,
        products=conn.execute("SELECT * FROM products ORDER BY name").fetchall(),
        outlets=conn.execute("SELECT * FROM outlets ORDER BY name").fetchall(),
        last=conn.execute(
            "SELECT p.*, pr.name product, pr.base_unit, o.name outlet FROM prices p "
            "JOIN products pr ON pr.id=p.product_id JOIN outlets o ON o.id=p.outlet_id "
            "ORDER BY p.id DESC LIMIT 15").fetchall(),
        units=UNIT_NAMES)


@app.route("/alerts", methods=["GET", "POST"])
def alerts():
    conn = db()
    if request.method == "POST":
        conn.execute("UPDATE alerts SET status=?, conclusion=? WHERE id=?",
                     (request.form["status"], request.form["conclusion"].strip(),
                      request.form["id"]))
        conn.commit()
        return redirect(url_for("alerts", status=request.args.get("status", "")))
    status = request.args.get("status", "")
    sql = ("SELECT a.*, p.date, p.pack_price, p.pack, p.unit_price, pr.name product, "
           "pr.base_unit, o.name outlet, o.district FROM alerts a "
           "JOIN prices p ON p.id=a.price_id JOIN products pr ON pr.id=p.product_id "
           "JOIN outlets o ON o.id=p.outlet_id")
    params = []
    if status:
        sql += " WHERE a.status=?"; params.append(status)
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM alerts GROUP BY status").fetchall())
    return render_template("alerts.html", status=status, counts=counts, units=UNIT_NAMES,
                           alerts=conn.execute(sql + " ORDER BY p.date DESC, a.id DESC",
                                               params).fetchall())


@app.route("/refs", methods=["GET", "POST"])
def refs():
    conn = db()
    error = None
    if request.method == "POST":
        f = request.form
        try:
            if f["kind"] == "product":
                conn.execute("INSERT INTO products(name, base_unit) VALUES(?,?)",
                             (f["name"].strip(), f["base_unit"]))
            else:
                conn.execute("INSERT INTO outlets(name, type, district) VALUES(?,?,?)",
                             (f["name"].strip(), f["type"], f["district"].strip()))
            conn.commit()
            return redirect(url_for("refs"))
        except sqlite3.IntegrityError:
            error = f"«{f['name']}» уже есть в справочнике"
    return render_template(
        "refs.html", error=error, units=UNIT_NAMES,
        products=conn.execute("SELECT * FROM products ORDER BY name").fetchall(),
        outlets=conn.execute("SELECT * FROM outlets ORDER BY district, name").fetchall())


init_db()

if __name__ == "__main__":
    app.run(debug=True)
