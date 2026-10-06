import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEED_PATH = BASE_DIR / "data" / "seed_items.json"

API_KEY = os.environ.get("APP_API_KEY", "")  # key of the Ivanovo warehouse (kept for backward compatibility)

app = FastAPI(title="Repairkit Sklad API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Warehouses (one per city). The API key decides which city you work in; every
# city has its own SQLite file, so nomenclature, stock, orders and charges are
# completely separate.
#
#   APP_API_KEY   - key of the Ivanovo warehouse (as before)
#   WAREHOUSES    - extra cities, format  id:Название:ключ;id2:Название2:ключ2
# ---------------------------------------------------------------------------

def _safe_id(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", (s or "").lower())


def load_warehouses() -> dict:
    wh = {}
    for part in os.environ.get("WAREHOUSES", "").split(";"):
        bits = [b.strip() for b in part.strip().split(":", 2)]
        if len(bits) != 3:
            continue
        cid, name, key = _safe_id(bits[0]), bits[1], bits[2]
        if cid and key:
            wh[cid] = {"id": cid, "name": name or cid, "key": key}
    if API_KEY and "ivanovo" not in wh:
        wh["ivanovo"] = {"id": "ivanovo", "name": "Иваново", "key": API_KEY}
    if not wh:  # dev mode: nothing configured -> one open warehouse
        wh["ivanovo"] = {"id": "ivanovo", "name": "Иваново", "key": ""}
    return wh


WAREHOUSES = load_warehouses()


def db_path_for(city_id: str) -> Path:
    # Ivanovo keeps the original file name so the existing database continues to work
    return DATA_DIR / ("repairkit.db" if city_id == "ivanovo" else f"{city_id}.db")


class Ctx:
    def __init__(self, wh: dict):
        self.id = wh["id"]
        self.name = wh["name"]
        self.db = db_path_for(wh["id"])


def auth(x_api_key: Optional[str] = Header(default=None)) -> Ctx:
    for w in WAREHOUSES.values():
        if w["key"] == "":  # dev mode
            return Ctx(w)
    given = (x_api_key or "").encode("utf-8")
    for w in WAREHOUSES.values():
        if given and hmac.compare_digest(w["key"].encode("utf-8"), given):
            return Ctx(w)
    raise HTTPException(status_code=401, detail="invalid_api_key")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@contextmanager
def get_conn(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.create_function("PYLOWER", 1, lambda s: s.lower() if isinstance(s, str) else s)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm_ts(s: str) -> str:
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="bad_date")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(x) -> float:
    return round(float(x) + 0.0, 2)


# key, name, price, unit, group, mode, basis, note
SERVICES = [
    ("pickup", "Забор и доставка груза по городу", 700, "рейс", "Логистика до склада", "manual", "", "при более 2 коробов"),
    ("receive", "Приёмка и размещение товара на складе фулфилмента", 5, "шт", "Приёмка и размещение", "auto", "", ""),
    ("register", "Регистрация новой номенклатуры (однократно)", 2, "позиция", "Дополнительные услуги к приёмке", "auto", "", "списывается при первой приёмке позиции"),
    ("label_58x40", "Печать этикетки 58х40", 3, "шт", "Дополнительные услуги к приёмке", "auto", "", ""),
    ("label_75x120", "Печать этикетки 75х120", 5, "шт", "Дополнительные услуги к приёмке", "auto", "", ""),
    ("storage", "Хранение 1 куб.м/день", 50, "куб.м·день", "Хранение", "manual", "", "10 коробов 60х40х40"),
    ("fbs_wb", "FBS сборка и комплектация товаров в заказы WB", 13, "заказ", "FBS сборка и отгрузка", "auto", "order", "+ короб"),
    ("fbs_ozon", "FBS сборка и комплектация товаров в заказы Ozon", 13, "заказ", "FBS сборка и отгрузка", "auto", "order", "+ пакет"),
    ("fbs_ym", "FBS сборка и комплектация товаров в заказы Яндекс-Маркет", 13, "заказ", "FBS сборка и отгрузка", "auto", "order", "+ пакет"),
    ("deliv_wb", "Доставка FBS до СЦ WB (коробка 310х210х165)", 390, "коробка", "FBS доставка до СЦ", "manual", "", ""),
    ("deliv_ozon", "Доставка FBS до СЦ Ozon (1 л)", 4, "литр", "FBS доставка до СЦ", "manual", "", ""),
    ("deliv_ym", "Доставка FBS до СЦ Яндекс-Маркет (1 л)", 4, "литр", "FBS доставка до СЦ", "manual", "", ""),
    ("pack_bag", "Пакет 250х350 с клеевым клапаном", 2, "шт", "Упаковочные материалы", "auto", "", ""),
    ("pack_box", "Коробка 310х210х165", 30, "шт", "Упаковочные материалы", "auto", "", ""),
]
MP_NAMES = {"wb": "WB", "ozon": "Ozon", "ym": "Яндекс-Маркет"}
LABEL_KEYS = ("label_58x40", "label_75x120")
PACK_KEYS = ("pack_bag", "pack_box")


def init_db(path: Path, city_id: str):
    with get_conn(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                sku TEXT PRIMARY KEY,
                article TEXT DEFAULT '',
                name TEXT DEFAULT '',
                barcode_ozn TEXT DEFAULT '',
                barcode2 TEXT DEFAULT '',
                stock INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT '',
                location TEXT DEFAULT ''
            )
            """
        )
        item_cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
        if "location" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN location TEXT DEFAULT ''")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_batches (
                id TEXT PRIMARY KEY,
                marketplace TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                uploaded_at TEXT DEFAULT '',
                part_index INTEGER DEFAULT 1,
                parts_total INTEGER DEFAULT 1,
                columns_json TEXT DEFAULT '[]',
                rows_json TEXT DEFAULT '[]',
                meta_json TEXT DEFAULT '{}'
            )
            """
        )
        batch_cols = [r["name"] for r in conn.execute("PRAGMA table_info(order_batches)").fetchall()]
        if "meta_json" not in batch_cols:
            conn.execute("ALTER TABLE order_batches ADD COLUMN meta_json TEXT DEFAULT '{}'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_uploaded ON order_batches(uploaded_at)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tariff (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL DEFAULT 0,
                unit TEXT DEFAULT '',
                grp TEXT DEFAULT '',
                mode TEXT DEFAULT 'manual',
                basis TEXT DEFAULT '',
                note TEXT DEFAULT '',
                sort INTEGER DEFAULT 0
            )
            """
        )
        for i, (key, name, price, unit, grp, mode, basis, note) in enumerate(SERVICES):
            conn.execute(
                "INSERT OR IGNORE INTO tariff (key, name, price, unit, grp, mode, basis, note, sort) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, name, price, unit, grp, mode, basis, note, i),
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                ref TEXT DEFAULT '',
                marketplace TEXT DEFAULT '',
                payload TEXT DEFAULT '{}',
                total REAL DEFAULT 0,
                undone INTEGER DEFAULT 0,
                undone_ts TEXT DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_kind_ref ON ops(kind, ref, marketplace)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS charges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                service_key TEXT DEFAULT '',
                service_name TEXT DEFAULT '',
                unit TEXT DEFAULT '',
                qty REAL DEFAULT 0,
                unit_price REAL DEFAULT 0,
                amount REAL DEFAULT 0,
                ref_type TEXT DEFAULT '',
                ref TEXT DEFAULT '',
                marketplace TEXT DEFAULT '',
                sku TEXT DEFAULT '',
                item_name TEXT DEFAULT '',
                comment TEXT DEFAULT '',
                op_id INTEGER,
                storno_of INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_charges_ts ON charges(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_charges_op ON charges(op_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_charges_reg ON charges(service_key, sku)")

        # one-time seed of the Ivanovo catalogue (stock 0) when the table is empty
        if city_id == "ivanovo":
            count = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
            if count == 0 and SEED_PATH.exists():
                seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
                now = _now_iso()
                conn.executemany(
                    """
                    INSERT INTO items (sku, article, name, barcode_ozn, barcode2, stock, updated_at)
                    VALUES (:sku, :article, :name, :barcodeOzn, :barcode2, :stock, :updated_at)
                    ON CONFLICT(sku) DO NOTHING
                    """,
                    [{**row, "updated_at": now} for row in seed],
                )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ItemIn(BaseModel):
    article: str = ""
    name: str = ""
    barcodeOzn: str = ""
    barcode2: str = ""
    stock: int = 0
    location: str = ""


class ItemPatch(BaseModel):
    article: Optional[str] = None
    name: Optional[str] = None
    barcodeOzn: Optional[str] = None
    barcode2: Optional[str] = None
    location: Optional[str] = None


class LocationRow(BaseModel):
    sku: str
    location: str = ""


class LocationsIn(BaseModel):
    rows: List[LocationRow]


class StockDelta(BaseModel):
    delta: Optional[int] = None
    stock: Optional[int] = None  # absolute set, if provided takes priority


class OrderBatchIn(BaseModel):
    marketplace: str
    fileName: str = ""
    columns: List[str] = []
    rows: List[dict] = []
    meta: dict = {}


class ReceiveIn(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=100000)
    label: Optional[str] = None  # label_58x40 | label_75x120 (one label per received piece)


class WriteoffIn(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=100000)
    comment: str = ""


class ShipLine(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=100000)


class PackIn(BaseModel):
    key: str
    qty: int = Field(ge=0, le=10000)


class ShipIn(BaseModel):
    order_no: str
    marketplace: str
    lines: List[ShipLine]
    packaging: List[PackIn] = []
    comment: str = ""


class TariffItemIn(BaseModel):
    key: str
    price: float = Field(ge=0, le=10_000_000)
    basis: Optional[str] = None


class TariffIn(BaseModel):
    items: List[TariffItemIn]


class ManualChargeIn(BaseModel):
    service_key: Optional[str] = None
    name: str = ""
    unit: str = ""
    unit_price: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    qty: float = Field(gt=0, le=10_000_000)
    comment: str = ""
    ts: Optional[str] = None


# ---------------------------------------------------------------------------
# Who am I
# ---------------------------------------------------------------------------

@app.get("/api/whoami")
def whoami(ctx: Ctx = Depends(auth)):
    return {"city": ctx.id, "name": ctx.name}


# ---------------------------------------------------------------------------
# Items endpoints
# ---------------------------------------------------------------------------

def row_to_item(row) -> dict:
    return {
        "sku": row["sku"],
        "article": row["article"] or "",
        "name": row["name"] or "",
        "barcodeOzn": row["barcode_ozn"] or "",
        "barcode2": row["barcode2"] or "",
        "stock": row["stock"] or 0,
        "location": row["location"] or "",
        "updatedAt": row["updated_at"] or "",
    }


@app.get("/api/items")
def list_items(ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY name COLLATE NOCASE").fetchall()
    return [row_to_item(r) for r in rows]


@app.put("/api/items/{sku}")
def upsert_item(sku: str, item: ItemIn, ctx: Ctx = Depends(auth)):
    now = _now_iso()
    with get_conn(ctx.db) as conn:
        existing = conn.execute("SELECT stock FROM items WHERE sku = ?", (sku,)).fetchone()
        stock = existing["stock"] if existing is not None else item.stock
        conn.execute(
            """
            INSERT INTO items (sku, article, name, barcode_ozn, barcode2, stock, location, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                article=excluded.article,
                name=excluded.name,
                barcode_ozn=CASE WHEN excluded.barcode_ozn != '' THEN excluded.barcode_ozn ELSE items.barcode_ozn END,
                barcode2=CASE WHEN excluded.barcode2 != '' THEN excluded.barcode2 ELSE items.barcode2 END,
                location=CASE WHEN excluded.location != '' THEN excluded.location ELSE items.location END,
                updated_at=excluded.updated_at
            """,
            (sku, item.article, item.name, item.barcodeOzn, item.barcode2, stock, item.location.strip(), now),
        )
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
    return row_to_item(row)


@app.patch("/api/items/{sku}")
def patch_item(sku: str, patch: ItemPatch, ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        fields = patch.dict(exclude_unset=True)
        if not fields:
            return row_to_item(row)
        col_map = {"article": "article", "name": "name", "barcodeOzn": "barcode_ozn", "barcode2": "barcode2", "location": "location"}
        if fields.get("location") is not None:
            fields["location"] = fields["location"].strip()
        sets = ", ".join(f"{col_map[k]} = ?" for k in fields if k in col_map)
        values = [v for k, v in fields.items() if k in col_map]
        values.append(_now_iso())
        values.append(sku)
        conn.execute(f"UPDATE items SET {sets}, updated_at = ? WHERE sku = ?", values)
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
    return row_to_item(row)


@app.post("/api/items/{sku}/stock")
def adjust_stock(sku: str, body: StockDelta, ctx: Ctx = Depends(auth)):
    """Raw stock correction / bulk import. Does NOT create charges (use /api/receive for a real receipt)."""
    with get_conn(ctx.db) as conn:
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        current = row["stock"] or 0
        if body.stock is not None:
            new_stock = body.stock
        elif body.delta is not None:
            new_stock = max(0, current + body.delta)
        else:
            raise HTTPException(status_code=400, detail="delta_or_stock_required")
        now = _now_iso()
        conn.execute("UPDATE items SET stock = ?, updated_at = ? WHERE sku = ?", (new_stock, now, sku))
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
    return row_to_item(row)


@app.delete("/api/items/{sku}")
def delete_item(sku: str, ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        conn.execute("DELETE FROM items WHERE sku = ?", (sku,))
    return {"deleted": sku}


@app.post("/api/items/locations")
def bulk_set_locations(body: LocationsIn, ctx: Ctx = Depends(auth)):
    now = _now_iso()
    updated = 0
    with get_conn(ctx.db) as conn:
        for r in body.rows:
            cur = conn.execute(
                "UPDATE items SET location = ?, updated_at = ? WHERE sku = ?",
                (r.location.strip(), now, r.sku),
            )
            updated += cur.rowcount
    return {"updated": updated, "received": len(body.rows)}


@app.post("/api/items/reset-stock")
def reset_all_stock(ctx: Ctx = Depends(auth)):
    now = _now_iso()
    with get_conn(ctx.db) as conn:
        conn.execute("UPDATE items SET stock = 0, updated_at = ?", (now,))
        count = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    return {"reset": True, "count": count}


# ---------------------------------------------------------------------------
# Tariff
# ---------------------------------------------------------------------------

def row_to_tariff(r) -> dict:
    return {
        "key": r["key"], "name": r["name"], "price": r["price"], "unit": r["unit"],
        "group": r["grp"], "mode": r["mode"], "basis": r["basis"], "note": r["note"],
    }


def get_tariff(conn) -> dict:
    return {r["key"]: r for r in conn.execute("SELECT * FROM tariff ORDER BY sort").fetchall()}


@app.get("/api/tariff")
def read_tariff(ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        return [row_to_tariff(r) for r in conn.execute("SELECT * FROM tariff ORDER BY sort").fetchall()]


@app.put("/api/tariff")
def update_tariff(body: TariffIn, ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        tar = get_tariff(conn)
        for it in body.items:
            if it.key not in tar:
                raise HTTPException(status_code=400, detail="unknown_service")
            if it.basis is not None:
                if it.basis not in ("order", "piece"):
                    raise HTTPException(status_code=400, detail="bad_basis")
                if not it.key.startswith("fbs_"):
                    raise HTTPException(status_code=400, detail="basis_not_applicable")
                conn.execute("UPDATE tariff SET basis = ? WHERE key = ?", (it.basis, it.key))
            conn.execute("UPDATE tariff SET price = ? WHERE key = ?", (_money(it.price), it.key))
        rows = conn.execute("SELECT * FROM tariff ORDER BY sort").fetchall()
    return [row_to_tariff(r) for r in rows]


# ---------------------------------------------------------------------------
# Charges / operations
# ---------------------------------------------------------------------------

def add_charge(conn, ts, svc, qty, *, ref_type, ref, op_id=None, mp="", sku="", item_name="",
               comment="", price=None, force=False):
    """Insert one charge line using the tariff price at this very moment (price is snapshotted)."""
    unit_price = svc["price"] if price is None else price
    amount = _money(qty * unit_price)
    if unit_price <= 0 and not force:
        return None
    cur = conn.execute(
        """
        INSERT INTO charges (ts, service_key, service_name, unit, qty, unit_price, amount, ref_type, ref,
                             marketplace, sku, item_name, comment, op_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (ts, svc["key"], svc["name"], svc["unit"], qty, unit_price, amount, ref_type, ref, mp, sku, item_name, comment, op_id),
    )
    return {"id": cur.lastrowid, "service": svc["name"], "qty": qty, "unitPrice": unit_price, "amount": amount}


def row_to_charge(r) -> dict:
    return {
        "id": r["id"], "ts": r["ts"], "serviceKey": r["service_key"], "serviceName": r["service_name"],
        "unit": r["unit"], "qty": r["qty"], "unitPrice": r["unit_price"], "amount": r["amount"],
        "refType": r["ref_type"], "ref": r["ref"], "marketplace": r["marketplace"], "sku": r["sku"],
        "itemName": r["item_name"], "comment": r["comment"], "opId": r["op_id"],
        "stornoOf": r["storno_of"], "stornoed": bool(r["stornoed"]) if "stornoed" in r.keys() else False,
    }


def _new_op(conn, ts, kind, ref, mp, payload):
    cur = conn.execute(
        "INSERT INTO ops (ts, kind, ref, marketplace, payload) VALUES (?,?,?,?,?)",
        (ts, kind, ref, mp, json.dumps(payload, ensure_ascii=False)),
    )
    return cur.lastrowid


@app.post("/api/receive")
def receive(body: ReceiveIn, ctx: Ctx = Depends(auth)):
    if body.label is not None and body.label not in LABEL_KEYS:
        raise HTTPException(status_code=400, detail="bad_label")
    now = _now_iso()
    with get_conn(ctx.db) as conn:
        it = conn.execute("SELECT * FROM items WHERE sku = ?", (body.sku,)).fetchone()
        if it is None:
            raise HTTPException(status_code=404, detail="not_found")
        tar = get_tariff(conn)
        conn.execute("UPDATE items SET stock = ?, updated_at = ? WHERE sku = ?", ((it["stock"] or 0) + body.qty, now, body.sku))
        op_id = _new_op(conn, now, "receive", body.sku, "", {"lines": [{"sku": body.sku, "qty": body.qty}]})
        common = dict(ref_type="item", ref=body.sku, op_id=op_id, sku=body.sku, item_name=it["name"] or "")
        charges = []
        charges.append(add_charge(conn, now, tar["receive"], body.qty, **common))
        registered = conn.execute(
            """SELECT COUNT(*) AS c FROM charges c WHERE c.service_key='register' AND c.sku=?
               AND c.storno_of IS NULL AND NOT EXISTS (SELECT 1 FROM charges s WHERE s.storno_of = c.id)""",
            (body.sku,),
        ).fetchone()["c"]
        if not registered:
            charges.append(add_charge(conn, now, tar["register"], 1, force=True, **common))
        if body.label:
            charges.append(add_charge(conn, now, tar[body.label], body.qty, **common))
        charges = [c for c in charges if c]
        total = _money(sum(c["amount"] for c in charges))
        conn.execute("UPDATE ops SET total = ? WHERE id = ?", (total, op_id))
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (body.sku,)).fetchone()
    return {"item": row_to_item(row), "opId": op_id, "total": total, "charges": charges}


@app.post("/api/writeoff")
def writeoff(body: WriteoffIn, ctx: Ctx = Depends(auth)):
    now = _now_iso()
    with get_conn(ctx.db) as conn:
        it = conn.execute("SELECT * FROM items WHERE sku = ?", (body.sku,)).fetchone()
        if it is None:
            raise HTTPException(status_code=404, detail="not_found")
        have = it["stock"] or 0
        taken = min(have, body.qty)
        conn.execute("UPDATE items SET stock = ?, updated_at = ? WHERE sku = ?", (have - taken, now, body.sku))
        op_id = _new_op(conn, now, "writeoff", body.sku, "",
                        {"lines": [{"sku": body.sku, "qty": body.qty, "taken": taken}], "comment": body.comment})
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (body.sku,)).fetchone()
    return {"item": row_to_item(row), "opId": op_id, "taken": taken}


@app.post("/api/shipments")
def ship_order(body: ShipIn, ctx: Ctx = Depends(auth)):
    mp = body.marketplace
    if mp not in MP_NAMES:
        raise HTTPException(status_code=400, detail="bad_marketplace")
    order_no = body.order_no.strip()
    if not order_no:
        raise HTTPException(status_code=400, detail="order_no_required")
    if not body.lines:
        raise HTTPException(status_code=400, detail="no_lines")
    for p in body.packaging:
        if p.key not in PACK_KEYS:
            raise HTTPException(status_code=400, detail="bad_packaging")
    merged = {}
    for ln in body.lines:
        merged[ln.sku] = merged.get(ln.sku, 0) + ln.qty
    now = _now_iso()
    with get_conn(ctx.db) as conn:
        dup = conn.execute(
            "SELECT id FROM ops WHERE kind='ship' AND ref=? AND marketplace=? AND undone=0", (order_no, mp)
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="already_shipped")
        items = {}
        for sku in merged:
            r = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
            if r is None:
                raise HTTPException(status_code=404, detail=f"item_not_found:{sku}")
            items[sku] = r
        tar = get_tariff(conn)
        lines, short = [], []
        for sku, qty in merged.items():
            have = items[sku]["stock"] or 0
            taken = min(have, qty)
            if taken < qty:
                short.append({"sku": sku, "need": qty, "had": have})
            conn.execute("UPDATE items SET stock = ?, updated_at = ? WHERE sku = ?", (have - taken, now, sku))
            lines.append({"sku": sku, "qty": qty, "taken": taken})
        units = sum(merged.values())
        op_id = _new_op(conn, now, "ship", order_no, mp, {"lines": lines, "comment": body.comment})
        common = dict(ref_type="order", ref=order_no, op_id=op_id, mp=mp)
        svc = tar[f"fbs_{mp}"]
        by_piece = svc["basis"] == "piece"
        comment = f"{len(lines)} поз., {units} шт" + (f". {body.comment.strip()}" if body.comment.strip() else "")
        charges = [add_charge(conn, now, svc, units if by_piece else 1, comment=comment, **common)]
        for p in body.packaging:
            if p.qty > 0:
                charges.append(add_charge(conn, now, tar[p.key], p.qty, **common))
        charges = [c for c in charges if c]
        total = _money(sum(c["amount"] for c in charges))
        conn.execute("UPDATE ops SET total = ? WHERE id = ?", (total, op_id))
    return {"opId": op_id, "total": total, "charges": charges, "short": short}


@app.get("/api/ops")
def list_ops(kind: Optional[str] = None, limit: int = Query(30, ge=1, le=200), ctx: Ctx = Depends(auth)):
    sql, args = "SELECT * FROM ops", []
    if kind:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn(ctx.db) as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"] or "{}")
        out.append({
            "id": r["id"], "ts": r["ts"], "kind": r["kind"], "ref": r["ref"], "marketplace": r["marketplace"],
            "total": r["total"], "undone": bool(r["undone"]), "lines": payload.get("lines", []),
        })
    return out


@app.get("/api/shipped")
def shipped_keys(ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        rows = conn.execute(
            "SELECT marketplace, ref FROM ops WHERE kind='ship' AND undone=0 ORDER BY id DESC LIMIT 50000"
        ).fetchall()
    return {"keys": [f"{r['marketplace']}|{r['ref']}" for r in rows]}


@app.post("/api/ops/{op_id}/undo")
def undo_op(op_id: int, ctx: Ctx = Depends(auth)):
    now = _now_iso()
    with get_conn(ctx.db) as conn:
        op = conn.execute("SELECT * FROM ops WHERE id = ?", (op_id,)).fetchone()
        if op is None:
            raise HTTPException(status_code=404, detail="not_found")
        if op["undone"]:
            raise HTTPException(status_code=409, detail="already_undone")
        lines = json.loads(op["payload"] or "{}").get("lines", [])
        if op["kind"] == "receive":
            for ln in lines:  # goods that already left cannot be "un-received"
                r = conn.execute("SELECT stock FROM items WHERE sku = ?", (ln["sku"],)).fetchone()
                if r is None or (r["stock"] or 0) < ln["qty"]:
                    raise HTTPException(status_code=409, detail="not_enough_stock")
        for ln in lines:
            r = conn.execute("SELECT stock FROM items WHERE sku = ?", (ln["sku"],)).fetchone()
            if r is None:
                continue
            delta = -ln["qty"] if op["kind"] == "receive" else ln.get("taken", ln["qty"])
            conn.execute("UPDATE items SET stock = ?, updated_at = ? WHERE sku = ?",
                         (max(0, (r["stock"] or 0) + delta), now, ln["sku"]))
        originals = conn.execute(
            """SELECT * FROM charges c WHERE c.op_id = ? AND c.storno_of IS NULL
               AND NOT EXISTS (SELECT 1 FROM charges s WHERE s.storno_of = c.id)""", (op_id,)
        ).fetchall()
        for c in originals:
            conn.execute(
                """INSERT INTO charges (ts, service_key, service_name, unit, qty, unit_price, amount, ref_type, ref,
                                        marketplace, sku, item_name, comment, op_id, storno_of)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now, c["service_key"], c["service_name"], c["unit"], -c["qty"], c["unit_price"], -c["amount"],
                 c["ref_type"], c["ref"], c["marketplace"], c["sku"], c["item_name"], "Отмена операции", op_id, c["id"]),
            )
        conn.execute("UPDATE ops SET undone = 1, undone_ts = ? WHERE id = ?", (now, op_id))
    return {"undone": op_id}


def _charge_filters(frm, to, service, q):
    where, args = [], []
    if frm:
        where.append("c.ts >= ?")
        args.append(_norm_ts(frm))
    if to:
        where.append("c.ts < ?")
        args.append(_norm_ts(to))
    if service:
        if service == "__other":
            where.append("c.service_key = ''")
        else:
            where.append("c.service_key = ?")
            args.append(service)
    if q:
        like = "%" + q.lower() + "%"
        where.append("(PYLOWER(c.ref) LIKE ? OR PYLOWER(c.item_name) LIKE ? OR PYLOWER(c.service_name) LIKE ? "
                     "OR PYLOWER(c.comment) LIKE ? OR PYLOWER(c.sku) LIKE ?)")
        args += [like] * 5
    return ((" WHERE " + " AND ".join(where)) if where else ""), args


@app.get("/api/charges")
def list_charges(frm: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                 service: Optional[str] = None, q: Optional[str] = None,
                 limit: int = Query(300, ge=1, le=50000), offset: int = Query(0, ge=0),
                 ctx: Ctx = Depends(auth)):
    where, args = _charge_filters(frm, to, service, q)
    with get_conn(ctx.db) as conn:
        agg = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(c.amount),0) AS s FROM charges c{where}", args).fetchone()
        rows = conn.execute(
            f"""SELECT c.*, EXISTS(SELECT 1 FROM charges s WHERE s.storno_of = c.id) AS stornoed
                FROM charges c{where}
                ORDER BY c.ts DESC, c.id DESC LIMIT ? OFFSET ?""",
            args + [limit, offset],
        ).fetchall()
    return {"count": agg["n"], "total": _money(agg["s"]), "rows": [row_to_charge(r) for r in rows]}


@app.get("/api/charges/summary")
def charges_summary(frm: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                    ctx: Ctx = Depends(auth)):
    where, args = _charge_filters(frm, to, None, None)
    with get_conn(ctx.db) as conn:
        order = {k: r["sort"] for k, r in get_tariff(conn).items()}
        rows = conn.execute(
            f"""SELECT c.service_key, c.service_name, c.unit, SUM(c.qty) AS qty, SUM(c.amount) AS amount
                FROM charges c{where} GROUP BY c.service_key, c.service_name, c.unit
                HAVING ABS(SUM(c.qty)) > 0.000001 OR ABS(SUM(c.amount)) > 0.000001""",
            args,
        ).fetchall()
    out = [{"serviceKey": r["service_key"], "serviceName": r["service_name"], "unit": r["unit"],
            "qty": round(r["qty"], 4), "amount": _money(r["amount"])} for r in rows]
    out.sort(key=lambda x: (order.get(x["serviceKey"], 999), x["serviceName"]))
    return {"total": _money(sum(x["amount"] for x in out)), "rows": out}


@app.post("/api/charges/manual")
def manual_charge(body: ManualChargeIn, ctx: Ctx = Depends(auth)):
    ts = _norm_ts(body.ts) if body.ts else _now_iso()
    with get_conn(ctx.db) as conn:
        if body.service_key:
            tar = get_tariff(conn)
            if body.service_key not in tar:
                raise HTTPException(status_code=400, detail="unknown_service")
            svc = dict(tar[body.service_key])
            price = body.unit_price if body.unit_price is not None else svc["price"]
        else:
            if not body.name.strip() or body.unit_price is None:
                raise HTTPException(status_code=400, detail="name_and_price_required")
            svc = {"key": "", "name": body.name.strip(), "unit": body.unit.strip(), "price": body.unit_price}
            price = body.unit_price
        c = add_charge(conn, ts, svc, body.qty, ref_type="manual", ref="", comment=body.comment.strip(),
                       price=price, force=True)
    return c


@app.post("/api/charges/{charge_id}/storno")
def storno_charge(charge_id: int, ctx: Ctx = Depends(auth)):
    now = _now_iso()
    with get_conn(ctx.db) as conn:
        c = conn.execute("SELECT * FROM charges WHERE id = ?", (charge_id,)).fetchone()
        if c is None:
            raise HTTPException(status_code=404, detail="not_found")
        if c["ref_type"] != "manual" or c["storno_of"] is not None:
            raise HTTPException(status_code=400, detail="only_manual_charges")
        if conn.execute("SELECT 1 FROM charges WHERE storno_of = ?", (charge_id,)).fetchone():
            raise HTTPException(status_code=409, detail="already_stornoed")
        conn.execute(
            """INSERT INTO charges (ts, service_key, service_name, unit, qty, unit_price, amount, ref_type, ref,
                                    marketplace, sku, item_name, comment, op_id, storno_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, c["service_key"], c["service_name"], c["unit"], -c["qty"], c["unit_price"], -c["amount"],
             "manual", c["ref"], c["marketplace"], c["sku"], c["item_name"], "Отмена начисления", None, charge_id),
        )
    return {"stornoed": charge_id}


# ---------------------------------------------------------------------------
# Order batches endpoints
# ---------------------------------------------------------------------------

def row_to_batch(row) -> dict:
    return {
        "id": row["id"],
        "marketplace": row["marketplace"] or "",
        "fileName": row["file_name"] or "",
        "uploadedAt": row["uploaded_at"] or "",
        "partIndex": row["part_index"] or 1,
        "partsTotal": row["parts_total"] or 1,
        "columns": json.loads(row["columns_json"] or "[]"),
        "rows": json.loads(row["rows_json"] or "[]"),
        "meta": json.loads(row["meta_json"] or "{}"),
    }


@app.get("/api/orders")
def list_orders(ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        rows = conn.execute(
            "SELECT * FROM order_batches ORDER BY uploaded_at DESC LIMIT 300"
        ).fetchall()
    return [row_to_batch(r) for r in rows]


@app.post("/api/orders")
def create_order_batch(batch: OrderBatchIn, ctx: Ctx = Depends(auth)):
    now = _now_iso()
    CHUNK = 400
    rows = batch.rows or []
    chunks = [rows[i : i + CHUNK] for i in range(0, len(rows), CHUNK)] or [[]]
    ids = []
    with get_conn(ctx.db) as conn:
        for idx, chunk in enumerate(chunks, start=1):
            bid = str(uuid.uuid4())
            ids.append(bid)
            conn.execute(
                """
                INSERT INTO order_batches (id, marketplace, file_name, uploaded_at, part_index, parts_total,
                                           columns_json, rows_json, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bid,
                    batch.marketplace,
                    batch.fileName,
                    now,
                    idx,
                    len(chunks),
                    json.dumps(batch.columns, ensure_ascii=False),
                    json.dumps(chunk, ensure_ascii=False),
                    json.dumps(batch.meta or {}, ensure_ascii=False),
                ),
            )
    return {"ids": ids, "rowCount": len(rows)}


@app.delete("/api/orders/{batch_id}")
def delete_order_batch(batch_id: str, ctx: Ctx = Depends(auth)):
    with get_conn(ctx.db) as conn:
        conn.execute("DELETE FROM order_batches WHERE id = ?", (batch_id,))
    return {"deleted": batch_id}


# ---------------------------------------------------------------------------
# Health + static frontend
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "time": _now_iso()}


for _w in WAREHOUSES.values():
    init_db(db_path_for(_w["id"]), _w["id"])

STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest")
