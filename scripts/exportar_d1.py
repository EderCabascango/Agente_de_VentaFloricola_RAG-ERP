"""Genera el SQL de datos para el ERP en la nube (Cloudflare D1) con el mismo catálogo que usa el agente.

Uso (desde la carpeta del proyecto):
    python scripts/exportar_d1.py --salida "C:/ruta/PagFuyu/erp/semilla.sql"

Usa data/variedades_cfg.txt si existe (las 203 variedades reales); si no, el catálogo corto de demostración.
Los precios (entre $0,18 y $0,39 por tallo) y el stock son inventados. El esquema está en erp/esquema.sql de la landing.
El archivo resultante lleva datos del cliente: no se sube al repositorio.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from seed import PRECIO_MAX, PRECIO_MIN, reset_db  # noqa: E402

# tabla -> columnas que existen en el esquema de D1 (las demás quedan en NULL)
TABLAS = {
    "productos": ["sku", "nombre", "categoria", "precio_unitario", "unidad"],
    "inventario": ["sku", "bodega", "stock_disponible", "stock_reservado"],
    "clientes": ["id", "razon_social", "ruc", "pais", "whatsapp", "limite_credito", "saldo_pendiente", "segmento", "descuento_max"],
    "pedidos": ["id", "cliente_id", "estado", "total", "creado_por", "aprobado_por", "fecha"],
    "pedido_detalle": ["pedido_id", "sku", "bodega", "cantidad", "precio_unitario", "descuento_pct", "subtotal"],
    "auditoria": ["timestamp", "usuario", "accion", "payload_json", "resultado"],
}


def literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", required=True)
    salida = Path(ap.parse_args().salida)
    with tempfile.TemporaryDirectory() as tmp:
        base = reset_db(Path(tmp) / "erp.db")
        con = sqlite3.connect(base)
        lineas = ["-- Datos de demostración del ERP (generado por scripts/exportar_d1.py). No subir al repositorio."]
        resumen = {}
        for tabla, cols in TABLAS.items():
            filas = con.execute(f"SELECT {', '.join(cols)} FROM {tabla}").fetchall()
            resumen[tabla] = len(filas)
            for i in range(0, len(filas), 100):
                bloque = ",\n".join("(" + ", ".join(literal(v) for v in f) + ")" for f in filas[i:i + 100])
                lineas.append(f"INSERT INTO {tabla} ({', '.join(cols)}) VALUES\n{bloque};")
        precios = [r[0] for r in con.execute("SELECT precio_unitario FROM productos")]
        assert all(PRECIO_MIN <= p <= PRECIO_MAX for p in precios), "hay precios fuera de $0,18 a $0,39"
        con.close()
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print(f"Escrito {salida} ({salida.stat().st_size // 1024} KB). Filas: {resumen}")


if __name__ == "__main__":
    main()
