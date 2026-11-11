"""Crea (o regenera) la base SQLite que simula el ERP de la florícola.

Uso:  python seed.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.config import DB_PATH

SCHEMA = """
DROP TABLE IF EXISTS pedido_detalle;
DROP TABLE IF EXISTS pedidos;
DROP TABLE IF EXISTS inventario;
DROP TABLE IF EXISTS productos;
DROP TABLE IF EXISTS clientes;
DROP TABLE IF EXISTS auditoria;

CREATE TABLE productos (
    sku TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    categoria TEXT NOT NULL,
    precio_unitario REAL NOT NULL,
    unidad TEXT NOT NULL
);
CREATE TABLE inventario (
    sku TEXT NOT NULL REFERENCES productos(sku),
    bodega TEXT NOT NULL,            -- finca / cuarto frío de postcosecha
    stock_disponible INTEGER NOT NULL,
    stock_reservado INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sku, bodega)
);
CREATE TABLE clientes (
    id TEXT PRIMARY KEY,
    razon_social TEXT NOT NULL,
    ruc TEXT UNIQUE NOT NULL,        -- RUC o identificación tributaria del importador
    pais TEXT NOT NULL,
    whatsapp TEXT NOT NULL,
    limite_credito REAL NOT NULL,
    saldo_pendiente REAL NOT NULL,
    segmento TEXT NOT NULL,
    descuento_max REAL NOT NULL
);
CREATE TABLE pedidos (
    id TEXT PRIMARY KEY,
    cliente_id TEXT NOT NULL REFERENCES clientes(id),
    estado TEXT NOT NULL,
    total REAL NOT NULL,
    creado_por TEXT NOT NULL,
    aprobado_por TEXT,
    fecha TEXT NOT NULL
);
CREATE TABLE pedido_detalle (
    pedido_id TEXT NOT NULL REFERENCES pedidos(id),
    sku TEXT NOT NULL REFERENCES productos(sku),
    bodega TEXT NOT NULL,
    cantidad INTEGER NOT NULL,
    precio_unitario REAL NOT NULL,
    descuento_pct REAL NOT NULL,
    subtotal REAL NOT NULL
);
CREATE TABLE auditoria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    usuario TEXT NOT NULL,
    accion TEXT NOT NULL,
    payload_json TEXT,
    resultado TEXT
);
"""

# Precios FOB Quito, en USD por tallo. Se venden en bonches de 25 tallos.
PRODUCTOS = [
    # sku, nombre, categoria, precio por tallo (USD), unidad
    ("ROS-001", "Rosa Freedom roja 50 cm", "Rosas", 0.40, "tallos"),
    ("ROS-002", "Rosa Freedom roja 60 cm", "Rosas", 0.50, "tallos"),
    ("ROS-003", "Rosa Freedom roja 70 cm", "Rosas", 0.60, "tallos"),
    ("ROS-004", "Rosa Explorer roja 60 cm", "Rosas", 0.55, "tallos"),
    ("ROS-005", "Rosa Mondial blanca 60 cm", "Rosas", 0.50, "tallos"),
    ("ROS-006", "Rosa Playa Blanca 50 cm", "Rosas", 0.42, "tallos"),
    ("ROS-007", "Rosa Vendela crema 60 cm", "Rosas", 0.48, "tallos"),
    ("ROS-008", "Rosa Pink Floyd fucsia 60 cm", "Rosas", 0.52, "tallos"),
    ("ROS-009", "Rosa Hearts rosada 50 cm", "Rosas", 0.45, "tallos"),
    ("ROS-010", "Rosa Brighton amarilla 50 cm", "Rosas", 0.44, "tallos"),
    ("ROS-011", "Rosa Tiffany rosada 60 cm", "Rosas", 0.50, "tallos"),
    ("ROS-012", "Rosa Orange Crush naranja 60 cm", "Rosas", 0.50, "tallos"),
    ("ROS-013", "Rosa Free Spirit coral 60 cm", "Rosas", 0.62, "tallos"),
    ("ROS-014", "Rosa Country Blues lila 50 cm", "Rosas", 0.55, "tallos"),
    ("ROS-015", "Rosa spray blanca 50 cm", "Rosas", 0.38, "tallos"),
    ("GYP-001", "Gypsophila Xlence blanca 80 cm", "Rellenos", 0.30, "tallos"),
    ("GYP-002", "Gypsophila tinturada azul 80 cm", "Rellenos", 0.45, "tallos"),
    ("HYP-001", "Hypericum rojo 60 cm", "Rellenos", 0.35, "tallos"),
    ("LIM-001", "Limonium azul 70 cm", "Rellenos", 0.28, "tallos"),
    ("CLA-001", "Clavel rojo estándar 60 cm", "Claveles", 0.18, "tallos"),
    ("CLA-002", "Clavel blanco estándar 60 cm", "Claveles", 0.18, "tallos"),
    ("CLA-003", "Mini clavel surtido 60 cm", "Claveles", 0.15, "tallos"),
    ("ALS-001", "Alstroemeria surtida 70 cm", "Flores de verano", 0.25, "tallos"),
    ("CRI-001", "Crisantemo pompón blanco 70 cm", "Flores de verano", 0.22, "tallos"),
    ("GIR-001", "Girasol 70 cm", "Flores de verano", 0.55, "tallos"),
]

# Fincas con cuarto frío de postcosecha
BODEGAS = ["CAY", "TAB", "LAT"]  # Cayambe, Tabacundo, Latacunga

# Stock fijo (tallos) para los productos de los escenarios de la demo (CAY, TAB, LAT)
STOCK_FIJO = {
    "ROS-002": (12000, 6000, 3000),
    "ROS-003": (4000, 2500, 1000),
    "ROS-005": (8000, 4000, 2000),
    "GYP-001": (5000, 3000, 1500),
}

CLIENTES = [
    # id, razon_social, id tributario, pais, whatsapp, limite, saldo, segmento, descuento_max (%)
    # Todos los datos son ficticios.
    ("C001", "Bloom Imports LLC", "US-59-1234567", "Estados Unidos", "+1 305 555 0142", 12000, 4000, "Mayorista", 10),
    ("C002", "Fleurs du Marché SARL", "FR-40123456789", "Francia", "+33 6 55 50 01 23", 8000, 7200, "Floristería", 5),
    ("C003", "Tulp & Roos Trading B.V.", "NL-856789012B01", "Países Bajos", "+31 6 5550 1234", 120000, 30000, "Corporativo", 15),
    ("C004", "Tsvety Market OOO", "RU-7701234567", "Rusia", "+7 916 555 01 23", 60000, 12000, "Mayorista", 12),
    ("C005", "Floristería El Jardín", "1790012345001", "Ecuador", "+593 99 555 0123", 3000, 500, "Floristería", 3),
    ("C006", "Supermercados Florales del Norte S.A.", "0991234567001", "Ecuador", "+593 98 555 0456", 40000, 38000, "Supermercado", 10),
]


def _stock(idx: int, sku: str) -> tuple[int, int, int]:
    if sku in STOCK_FIJO:
        return STOCK_FIJO[sku]
    base = 25 * (40 + (idx * 37) % 160)  # determinista y en bonches completos de 25
    return base, 25 * round(base * 0.7 / 25), 25 * round(base * 0.3 / 25)


def reset_db(db_path: Path = DB_PATH) -> Path:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO productos VALUES (?,?,?,?,?)", PRODUCTOS)
        for i, (sku, *_rest) in enumerate(PRODUCTOS):
            for bodega, stock in zip(BODEGAS, _stock(i, sku)):
                conn.execute(
                    "INSERT INTO inventario VALUES (?,?,?,0)", (sku, bodega, stock)
                )
        conn.executemany("INSERT INTO clientes VALUES (?,?,?,?,?,?,?,?,?)", CLIENTES)

        # Pedidos históricos para que la tabla no esté vacía
        hoy = datetime.now()
        historicos = [
            ("PED-0001", "C003", "ENTREGADO", 4400.00, "vendedor.ana", "supervisor.luis", hoy - timedelta(days=12),
             [("ROS-005", "CAY", 10000, 0.50, 12.0, 4400.00)]),
            ("PED-0002", "C004", "DESPACHADO", 1250.00, "vendedor.ana", "supervisor.luis", hoy - timedelta(days=5),
             [("ROS-002", "CAY", 2000, 0.50, 5.0, 950.00), ("GYP-001", "TAB", 1000, 0.30, 5.0, 300.00)]),
            ("PED-0003", "C001", "ENTREGADO", 450.00, "vendedor.jorge", "supervisor.luis", hoy - timedelta(days=2),
             [("CLA-001", "LAT", 2500, 0.18, 0.0, 450.00)]),
        ]
        for pid, cid, estado, total, creado, aprobado, fecha, lineas in historicos:
            conn.execute(
                "INSERT INTO pedidos VALUES (?,?,?,?,?,?,?)",
                (pid, cid, estado, total, creado, aprobado, fecha.strftime("%Y-%m-%d %H:%M:%S")),
            )
            for sku, bodega, cant, precio, desc, sub in lineas:
                conn.execute(
                    "INSERT INTO pedido_detalle VALUES (?,?,?,?,?,?,?)",
                    (pid, sku, bodega, cant, precio, desc, sub),
                )
        conn.execute(
            "INSERT INTO auditoria (timestamp, usuario, accion, payload_json, resultado) VALUES (?,?,?,?,?)",
            (hoy.strftime("%Y-%m-%d %H:%M:%S"), "sistema", "SEED", "{}", "Base de datos regenerada"),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    path = reset_db()
    print(f"ERP simulado de la florícola creado en {path} con {len(PRODUCTOS)} productos y {len(CLIENTES)} clientes.")
