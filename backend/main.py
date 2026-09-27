import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "repairkit.db"
SEED_PATH = DATA_DIR / "seed_items.json"

API_KEY = os.environ.get("APP_API_KEY", "")  # if empty, auth is disabled (dev mode)

app = FastAPI(title="Repairkit Sklad API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                sku TEXT PRIMARY KEY,
                article TEXT DEFAULT '',
                name TEXT DEFAULT '',
                barcode_ozn TEXT DEFAULT '',
                barcode2 TEXT DEFAULT '',
                stock INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT ''
            )
            """
        )
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
                rows_json TEXT DEFAULT '[]'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_uploaded ON order_batches(uploaded_at)")

        # one-time seed from bundled seed_items.json, only if table is empty
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


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_key(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    return True


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ItemIn(BaseModel):
    article: str = ""
    name: str = ""
    barcodeOzn: str = ""
    barcode2: str = ""
    stock: int = 0


class ItemPatch(BaseModel):
    article: Optional[str] = None
    name: Optional[str] = None
    barcodeOzn: Optional[str] = None
    barcode2: Optional[str] = None


class StockDelta(BaseModel):
    delta: Optional[int] = None
    stock: Optional[int] = None  # absolute set, if provided takes priority


class OrderBatchIn(BaseModel):
    marketplace: str
    fileName: str = ""
    columns: List[str] = []
    rows: List[dict] = []


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
        "updatedAt": row["updated_at"] or "",
    }


@app.get("/api/items")
def list_items(_: bool = Depends(check_key)):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY name COLLATE NOCASE").fetchall()
    return [row_to_item(r) for r in rows]


@app.put("/api/items/{sku}")
def upsert_item(sku: str, item: ItemIn, _: bool = Depends(check_key)):
    now = _now_iso()
    with get_conn() as conn:
        existing = conn.execute("SELECT stock FROM items WHERE sku = ?", (sku,)).fetchone()
        stock = existing["stock"] if existing is not None else item.stock
        conn.execute(
            """
            INSERT INTO items (sku, article, name, barcode_ozn, barcode2, stock, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                article=excluded.article,
                name=excluded.name,
                barcode_ozn=CASE WHEN excluded.barcode_ozn != '' THEN excluded.barcode_ozn ELSE items.barcode_ozn END,
                barcode2=CASE WHEN excluded.barcode2 != '' THEN excluded.barcode2 ELSE items.barcode2 END,
                updated_at=excluded.updated_at
            """,
            (sku, item.article, item.name, item.barcodeOzn, item.barcode2, stock, now),
        )
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
    return row_to_item(row)


@app.patch("/api/items/{sku}")
def patch_item(sku: str, patch: ItemPatch, _: bool = Depends(check_key)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        fields = patch.dict(exclude_unset=True)
        if not fields:
            return row_to_item(row)
        col_map = {"article": "article", "name": "name", "barcodeOzn": "barcode_ozn", "barcode2": "barcode2"}
        sets = ", ".join(f"{col_map[k]} = ?" for k in fields if k in col_map)
        values = [v for k, v in fields.items() if k in col_map]
        values.append(_now_iso())
        values.append(sku)
        conn.execute(f"UPDATE items SET {sets}, updated_at = ? WHERE sku = ?", values)
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
    return row_to_item(row)


@app.post("/api/items/{sku}/stock")
def adjust_stock(sku: str, body: StockDelta, _: bool = Depends(check_key)):
    with get_conn() as conn:
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
def delete_item(sku: str, _: bool = Depends(check_key)):
    with get_conn() as conn:
        conn.execute("DELETE FROM items WHERE sku = ?", (sku,))
    return {"deleted": sku}


@app.post("/api/items/reset-stock")
def reset_all_stock(_: bool = Depends(check_key)):
    now = _now_iso()
    with get_conn() as conn:
        conn.execute("UPDATE items SET stock = 0, updated_at = ?", (now,))
        count = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    return {"reset": True, "count": count}


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
    }


@app.get("/api/orders")
def list_orders(_: bool = Depends(check_key)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM order_batches ORDER BY uploaded_at DESC LIMIT 300"
        ).fetchall()
    return [row_to_batch(r) for r in rows]


@app.post("/api/orders")
def create_order_batch(batch: OrderBatchIn, _: bool = Depends(check_key)):
    now = _now_iso()
    CHUNK = 400
    rows = batch.rows or []
    chunks = [rows[i : i + CHUNK] for i in range(0, len(rows), CHUNK)] or [[]]
    ids = []
    with get_conn() as conn:
        for idx, chunk in enumerate(chunks, start=1):
            bid = str(uuid.uuid4())
            ids.append(bid)
            conn.execute(
                """
                INSERT INTO order_batches (id, marketplace, file_name, uploaded_at, part_index, parts_total, columns_json, rows_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
    return {"ids": ids, "rowCount": len(rows)}


@app.delete("/api/orders/{batch_id}")
def delete_order_batch(batch_id: str, _: bool = Depends(check_key)):
    with get_conn() as conn:
        conn.execute("DELETE FROM order_batches WHERE id = ?", (batch_id,))
    return {"deleted": batch_id}


# ---------------------------------------------------------------------------
# Health + static frontend
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "time": _now_iso()}


init_db()

STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest")
