"""Cliente que imita la API del ERP de la florícola sobre SQLite.

- `ERPClient`: SOLO LECTURA. Abre la base en modo `ro`, así que aunque alguien intente
  escribir con esta conexión, SQLite lo rechaza. Es lo único que usan las tools del agente.
- `registrar_pedido_aprobado` y `registrar_auditoria`: escritura. Solo las llaman nodos del
  grafo (escribir_erp tras la aprobación humana, y la auditoría). El agente no las importa.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .config import DB_PATH


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto).lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


_GUIONES = re.compile("[‐-―−]")
_STOPWORDS = {"de", "del", "la", "el", "los", "las", "para", "con", "en", "x", "y", "un", "una", "unidades",
              "tallo", "tallos", "bonche", "bonches", "cm"}


class ERPClient:
    """API de lectura del ERP simulado."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or DB_PATH)

    # -- conexión de solo lectura ------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"No existe {self.db_path}. Ejecuta: python seed.py")
        uri = f"file:{quote(self.db_path.resolve().as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    # -- productos -----------------------------------------------------------------
    def get_producto(self, sku: str) -> dict | None:
        sku = _GUIONES.sub("-", str(sku)).strip()  # gpt-oss escribe "ROS‑001" con guion no separable
        rows = self._all("SELECT * FROM productos WHERE upper(sku)=upper(?)", (sku,))
        return rows[0] if rows else None

    def listar_productos(self) -> list[dict]:
        return self._all("SELECT * FROM productos ORDER BY sku")

    def buscar_productos(self, texto: str, limite: int = 5) -> list[dict]:
        consulta = _normalizar(texto)
        tokens = [t for t in consulta.replace(",", " ").split() if t not in _STOPWORDS]
        resultados = []
        for p in self.listar_productos():
            if p["sku"].lower() == consulta.strip():
                return [p]
            campo = _normalizar(f"{p['sku']} {p['nombre']} {p['categoria']} {p['unidad']}")
            palabras = campo.split()
            aciertos = sum(
                1 for t in tokens
                if any(w.startswith(t) or t.startswith(w) and len(w) > 3 for w in palabras)
            )
            ratio = difflib.SequenceMatcher(None, consulta, _normalizar(p["nombre"])).ratio()
            score = (aciertos / max(len(tokens), 1)) + 0.3 * ratio
            if aciertos:
                resultados.append((score, p))
        resultados.sort(key=lambda x: -x[0])
        return [p for _, p in resultados[:limite]]

    def resolver_producto(self, sku_o_nombre: str) -> dict | None:
        """SKU exacto o, si no, la mejor coincidencia por nombre (determinista, sin LLM)."""
        prod = self.get_producto(sku_o_nombre)
        if prod:
            return prod
        res = self.buscar_productos(sku_o_nombre, limite=1)
        return res[0] if res else None

    def resolver_estricto(self, texto: str) -> tuple[dict | None, list[dict]]:
        """Como `resolver_producto`, pero NUNCA elige por el cliente cuando hay varias flores posibles.

        Devuelve (producto, []) si hay una sola coincidencia, o (None, opciones) si el texto es ambiguo
        (p. ej. "Freedom" existe en 3 largos). El código decide; el LLM solo pregunta cuál prefiere.
        """
        prod = self.get_producto(texto)
        if prod:
            return prod, []
        nq = _normalizar(texto).strip()
        tokens = [t for t in nq.replace(",", " ").split() if t not in _STOPWORDS]
        if not tokens:
            return None, []
        todos = self.listar_productos()
        for p in todos:
            if _normalizar(p["nombre"]) == nq:
                return p, []
        cands = []
        for p in todos:
            palabras = _normalizar(f"{p['sku']} {p['nombre']} {p['categoria']}").split()
            # t empieza con w solo si es plural (rosas/rosa, claveles/clavel): 'freedom' no debe coincidir con 'free'
            if all(any(w.startswith(t) or (t.startswith(w) and len(w) > 3 and len(t) - len(w) <= 2) for w in palabras)
                   for t in tokens):
                cands.append(p)
        if len(cands) == 1:
            return cands[0], []
        if len(cands) > 1:
            return None, cands[:8]
        res = self.buscar_productos(texto, limite=1)
        return (res[0], []) if res else (None, [])

    def catalogo_resumen(self, categoria: str | None = None) -> dict:
        """Catálogo compacto: sin categoría, un resumen por categoría; con categoría, sus flores con precio."""
        import re as _re
        todos = self.listar_productos()
        if categoria:
            nc = _normalizar(categoria).strip()
            sel = [p for p in todos if nc and (nc in _normalizar(p["categoria"]) or _normalizar(p["categoria"]) in nc)]
            return {"categoria": sel[0]["categoria"] if sel else categoria,
                    "flores": [f"{p['nombre']} · ${p['precio_unitario']:.2f} por tallo" for p in sel]}
        grupos: dict[str, list[dict]] = {}
        for p in todos:
            grupos.setdefault(p["categoria"], []).append(p)
        out = []
        for cat, ps in grupos.items():
            nombres = list(dict.fromkeys(_re.sub(r"\s+\d+\s*cm$", "", p["nombre"]) for p in ps))
            precios = [p["precio_unitario"] for p in ps]
            out.append({"categoria": cat, "variedades": nombres,
                        "precio_desde": min(precios), "precio_hasta": max(precios)})
        return {"categorias": out}

    # -- inventario ----------------------------------------------------------------
    def inventario(self, sku: str) -> list[dict]:
        return self._all(
            "SELECT bodega, stock_disponible, stock_reservado FROM inventario "
            "WHERE upper(sku)=upper(?) ORDER BY stock_disponible DESC",
            (sku.strip(),),
        )

    def inventario_completo(self) -> list[dict]:
        return self._all(
            "SELECT i.sku, p.nombre, i.bodega, i.stock_disponible, i.stock_reservado "
            "FROM inventario i JOIN productos p USING(sku) ORDER BY i.sku, i.bodega"
        )

    # -- clientes ------------------------------------------------------------------
    def get_cliente(self, id_ruc_o_nombre: str) -> dict | None:
        q = str(id_ruc_o_nombre).strip()
        rows = self._all(
            "SELECT * FROM clientes WHERE upper(id)=upper(?) OR ruc=?", (q, q)
        )
        if not rows:
            # número de WhatsApp (solo dígitos): así se identifica al cliente cuando escribe por el canal
            digitos = re.sub(r"\D", "", q)
            if len(digitos) >= 8:
                rows = [c for c in self._all("SELECT * FROM clientes") if re.sub(r"\D", "", c["whatsapp"]) == digitos]
        if not rows:
            # coincidencia por nombre (sin tildes)
            nq = _normalizar(q)
            rows = [c for c in self._all("SELECT * FROM clientes") if nq and nq in _normalizar(c["razon_social"])]
        if not rows:
            return None
        c = rows[0]
        c["credito_disponible"] = round(c["limite_credito"] - c["saldo_pendiente"], 2)
        return c

    # -- pedidos y auditoría ------------------------------------------------------
    def listar_pedidos(self) -> list[dict]:
        return self._all(
            "SELECT p.id, p.fecha, c.razon_social AS cliente, p.estado, p.total, p.creado_por, p.aprobado_por "
            "FROM pedidos p JOIN clientes c ON c.id=p.cliente_id ORDER BY p.fecha DESC, p.id DESC"
        )

    def detalle_pedido(self, pedido_id: str) -> list[dict]:
        return self._all("SELECT * FROM pedido_detalle WHERE pedido_id=?", (pedido_id,))

    def auditoria(self, limite: int = 50) -> list[dict]:
        return self._all(
            "SELECT id, timestamp, usuario, accion, resultado, payload_json FROM auditoria ORDER BY id DESC LIMIT ?",
            (limite,),
        )


# ======================= ESCRITURA (fuera del alcance del agente) =======================

def _rw_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ahora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar_auditoria(usuario: str, accion: str, payload: dict | None = None,
                        resultado: str = "", db_path: Path | str | None = None) -> None:
    conn = _rw_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO auditoria (timestamp, usuario, accion, payload_json, resultado) VALUES (?,?,?,?,?)",
            (_ahora(), usuario, accion, json.dumps(payload or {}, ensure_ascii=False, default=str), resultado),
        )
        conn.commit()
    finally:
        conn.close()


class ErrorEscrituraERP(RuntimeError):
    pass


def registrar_pedido_aprobado(validacion: dict, aprobado_por: str, creado_por: str = "agente-ia",
                              db_path: Path | str | None = None) -> dict:
    """Inserta el pedido, reserva stock y audita. Transaccional: o todo o nada.

    Solo debe llamarse desde el nodo `escribir_erp`, después de la aprobación humana.
    """
    if not validacion or not validacion.get("aprobado"):
        raise ErrorEscrituraERP("Se intentó escribir un pedido que no pasó la validación.")
    if not aprobado_por:
        raise ErrorEscrituraERP("Falta el usuario que aprobó el pedido.")

    conn = _rw_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0]
        pedido_id = f"PED-{n + 1:04d}"
        cliente_id = validacion["cliente"]["id"]
        total = round(sum(l["subtotal"] for l in validacion["lineas"]), 2)
        conn.execute(
            "INSERT INTO pedidos VALUES (?,?,?,?,?,?,?)",
            (pedido_id, cliente_id, "APROBADO", total, creado_por, aprobado_por, _ahora()),
        )
        for l in validacion["lineas"]:
            cur = conn.execute(
                "UPDATE inventario SET stock_disponible = stock_disponible - ?, "
                "stock_reservado = stock_reservado + ? "
                "WHERE sku=? AND bodega=? AND stock_disponible >= ?",
                (l["cantidad"], l["cantidad"], l["sku"], l["bodega"], l["cantidad"]),
            )
            if cur.rowcount != 1:
                raise ErrorEscrituraERP(f"Stock insuficiente al reservar {l['sku']} en {l['bodega']}.")
            conn.execute(
                "INSERT INTO pedido_detalle VALUES (?,?,?,?,?,?,?)",
                (pedido_id, l["sku"], l["bodega"], l["cantidad"], l["precio_unitario"],
                 l["descuento_pct"], l["subtotal"]),
            )
        conn.execute(
            "INSERT INTO auditoria (timestamp, usuario, accion, payload_json, resultado) VALUES (?,?,?,?,?)",
            (_ahora(), aprobado_por, "PEDIDO_ESCRITO_ERP",
             json.dumps({"pedido_id": pedido_id, "cliente_id": cliente_id, "total": total,
                         "lineas": validacion["lineas"], "creado_por": creado_por}, ensure_ascii=False),
             f"OK {pedido_id}"),
        )
        conn.commit()
        return {"pedido_id": pedido_id, "total": total, "cliente_id": cliente_id,
                "aprobado_por": aprobado_por, "estado": "APROBADO"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
