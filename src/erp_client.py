"""Cliente que imita la API del ERP de la florícola sobre SQLite.

- `ERPClient`: SOLO LECTURA. Abre la base en modo `ro`, así que aunque alguien intente
  escribir con esta conexión, SQLite lo rechaza. Es lo único que usan las tools del agente.
- `registrar_pedido_aprobado` y `registrar_auditoria`: escritura. Solo las llaman nodos del
  grafo (escribir_erp tras la aprobación humana, y la auditoría). El agente no las importa.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import config as cfg
from .config import DB_PATH
from .erp_http import ERPNoDisponible, llamar

log = logging.getLogger("erp")


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto).lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


_GUIONES = re.compile("[‐-―−]")
_STOPWORDS = {"de", "del", "la", "el", "los", "las", "para", "con", "en", "x", "y", "un", "una", "unidades",
              "tallo", "tallos", "bonche", "bonches", "cm"}


class ERPClient:
    """API de lectura del ERP simulado."""

    def __new__(cls, *args, **kwargs):
        # Con ERP_URL (ERP en la nube) se entrega el cliente HTTP; sin él, el de SQLite local.
        if cls is ERPClient and cfg.ERP_URL:
            from .erp_http import crear_cliente_http
            return object.__new__(crear_cliente_http())
        return object.__new__(cls)

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

    def catalogo_resumen(self, buscar: str | None = None) -> dict:
        """Catálogo de rosas, compacto: cuántas variedades hay, sus largos y el rango de precios por tallo.

        Con `buscar` (parte del nombre, o una sola letra para la inicial) devuelve las variedades que coinciden,
        con sus largos y precios. Nunca lista las 200 variedades de golpe: el agente pregunta cuál busca el cliente.
        """
        variedades: dict[str, dict] = {}
        for p in self.listar_productos():
            base = re.sub(r"^Rosa\s+", "", re.sub(r"\s*\d+\s*cm$", "", p["nombre"]), flags=re.I).strip()
            m = re.search(r"(\d+)\s*cm$", p["nombre"])
            d = variedades.setdefault(base, {"largos_cm": set(), "precios": []})
            if m:
                d["largos_cm"].add(int(m.group(1)))
            d["precios"].append(p["precio_unitario"])
        todos = [x for d in variedades.values() for x in d["precios"]]
        res = {"total_variedades": len(variedades),
               "largos_cm": sorted({l for d in variedades.values() for l in d["largos_cm"]}),
               "precio_desde": min(todos, default=0), "precio_hasta": max(todos, default=0),
               "moneda": "USD por tallo"}
        if buscar:
            nb = _normalizar(buscar).strip()
            sel = [v for v in variedades
                   if nb and (_normalizar(v).startswith(nb) if len(nb) == 1 else nb in _normalizar(v))]
            res["coincidencias"] = [{"variedad": v, "largos_cm": sorted(variedades[v]["largos_cm"]),
                                     "precio_desde": min(variedades[v]["precios"]),
                                     "precio_hasta": max(variedades[v]["precios"])} for v in sorted(sel)[:15]]
            res["total_coincidencias"] = len(sel)
        return res

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
    if cfg.ERP_URL and db_path is None:
        try:
            llamar("POST", "/api/erp/auditoria", cuerpo={"usuario": usuario, "accion": accion,
                                                          "payload": payload or {}, "resultado": resultado})
        except ERPNoDisponible as e:
            log.warning("No se pudo anotar la auditoría en el ERP: %s", e)
        return
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


# País según el prefijo del número de WhatsApp (los más probables para esta florícola)
_PREFIJOS_PAIS = [("593", "Ecuador"), ("57", "Colombia"), ("51", "Perú"), ("34", "España"), ("33", "Francia"),
                  ("31", "Países Bajos"), ("49", "Alemania"), ("44", "Reino Unido"), ("7", "Rusia"), ("1", "Estados Unidos")]


def _pais_de_numero(wa_id: str) -> str:
    return next((p for pref, p in _PREFIJOS_PAIS if wa_id.startswith(pref)), "Por confirmar")


def registrar_cliente_whatsapp(wa_id: str, nombre: str = "", db_path: Path | str | None = None,
                               limite_credito: float | None = None, descuento_max: float | None = None) -> dict:
    """Registra como cliente nuevo a un número de WhatsApp que no estaba en el ERP. Es idempotente.

    Lo llama el canal (webhook), nunca el agente. El cliente nuevo no tiene descuento propio y su
    crédito es un límite de prueba configurable; el supervisor ve que es nuevo al aprobar cada pedido.
    """
    from . import config as cfg

    digitos = re.sub(r"\D", "", wa_id or "")
    if len(digitos) < 8:
        raise ErrorEscrituraERP("Número de WhatsApp no válido.")
    limite = cfg.CLIENTE_NUEVO_LIMITE_CREDITO if limite_credito is None else limite_credito
    descuento = cfg.CLIENTE_NUEVO_DESCUENTO_MAX if descuento_max is None else descuento_max
    nombre = re.sub(r"\s+", " ", str(nombre or "")).strip()[:60] or "Cliente"
    cliente_id = f"W{digitos}"

    if cfg.ERP_URL and db_path is None:
        try:
            r = llamar("POST", "/api/erp/clientes/whatsapp", cuerpo={
                "wa_id": digitos, "nombre": nombre, "limite_credito": limite, "descuento_max": descuento,
                "pais": _pais_de_numero(digitos)})
        except ERPNoDisponible as e:
            raise ErrorEscrituraERP(str(e)) from e
        return {"id": r["id"], "nuevo": r["nuevo"]}

    conn = _rw_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ya = conn.execute("SELECT * FROM clientes WHERE id=?", (cliente_id,)).fetchone()
        if ya is None:
            conn.execute(
                "INSERT INTO clientes VALUES (?,?,?,?,?,?,?,?,?)",
                (cliente_id, f"{nombre} (WhatsApp +{digitos})", f"WA-{digitos}", _pais_de_numero(digitos),
                 f"+{digitos}", limite, 0.0, "Nuevo", descuento),
            )
            conn.execute(
                "INSERT INTO auditoria (timestamp, usuario, accion, payload_json, resultado) VALUES (?,?,?,?,?)",
                (_ahora(), "canal-whatsapp", "CLIENTE_REGISTRADO_WHATSAPP",
                 json.dumps({"id": cliente_id, "nombre": nombre, "limite_credito": limite,
                             "descuento_max": descuento}, ensure_ascii=False),
                 "Cliente nuevo atendido sin registro previo"),
            )
        conn.commit()
        return {"id": cliente_id, "nuevo": ya is None}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
            "INSERT INTO pedidos (id, cliente_id, estado, total, creado_por, aprobado_por, fecha) VALUES (?,?,?,?,?,?,?)",
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


def registrar_pedido_pendiente(validacion: dict, wa_id: str | None = None, creado_por: str = "agente-ia",
                               destino: str | None = None, db_path: Path | str | None = None) -> dict:
    """Deja un pedido ya validado como PENDIENTE (no mueve stock). Una persona lo acepta o rechaza en la plataforma.

    Con ERP_URL va al ERP en la nube, que vuelve a comprobar precios, stock y crédito antes de guardarlo.
    """
    if not validacion or not validacion.get("aprobado"):
        raise ErrorEscrituraERP("Se intentó guardar un pedido que no pasó la validación.")
    lineas = [{"sku": l["sku"], "bodega": l["bodega"], "cantidad": l["cantidad"],
               "precio_unitario": l["precio_unitario"], "descuento_pct": l["descuento_pct"]} for l in validacion["lineas"]]
    cliente_id = validacion["cliente"]["id"]
    destino = destino or validacion.get("destino")
    if cfg.ERP_URL and db_path is None:
        try:
            r = llamar("POST", "/api/erp/pedidos/pendiente", cuerpo={
                "cliente_id": cliente_id, "wa_id": wa_id, "destino": destino, "creado_por": creado_por, "lineas": lineas})
        except ERPNoDisponible as e:
            raise ErrorEscrituraERP(str(e)) from e
        return {"pedido_id": r["pedido_id"], "total": r["total"], "cliente_id": cliente_id, "estado": "PENDIENTE"}

    conn = _rw_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0]
        pedido_id = f"PED-{n + 1:04d}"
        total = round(sum(l["subtotal"] for l in validacion["lineas"]), 2)
        conn.execute(
            "INSERT INTO pedidos (id, cliente_id, estado, total, creado_por, aprobado_por, fecha, wa_id, destino) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pedido_id, cliente_id, "PENDIENTE", total, creado_por, None, _ahora(), wa_id, destino))
        for l in validacion["lineas"]:
            conn.execute("INSERT INTO pedido_detalle VALUES (?,?,?,?,?,?,?)",
                         (pedido_id, l["sku"], l["bodega"], l["cantidad"], l["precio_unitario"], l["descuento_pct"], l["subtotal"]))
        conn.execute(
            "INSERT INTO auditoria (timestamp, usuario, accion, payload_json, resultado) VALUES (?,?,?,?,?)",
            (_ahora(), creado_por, "PEDIDO_PENDIENTE", json.dumps({"pedido_id": pedido_id, "cliente_id": cliente_id, "total": total}),
             "Esperando aprobación en la plataforma"))
        conn.commit()
        return {"pedido_id": pedido_id, "total": total, "cliente_id": cliente_id, "estado": "PENDIENTE"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def registrar_mensaje(wa_id: str, rol: str, texto: str, traza: list[dict] | None = None) -> None:
    """Guarda un mensaje de la conversación para verla en la plataforma. Solo con el ERP en la nube; nunca rompe el chat."""
    if not cfg.ERP_URL:
        return
    try:
        llamar("POST", "/api/erp/mensajes", cuerpo={"wa_id": re.sub(r"\D", "", wa_id or ""), "rol": rol,
                                                   "texto": texto, "traza": traza or []})
    except ERPNoDisponible as e:
        log.warning("No se pudo guardar el mensaje en el ERP: %s", e)


def despachos_pendientes(wa_id: str) -> list[dict]:
    """Pedidos aceptados de este número que todavía no salen, con el estado de sus datos de despacho.

    Solo existe con el ERP en la nube. Si no responde, el chat sigue sin esta información (no se rompe).
    """
    if not cfg.ERP_URL or not wa_id:
        return []
    try:
        return llamar("GET", "/api/erp/despachos/pendientes", params={"wa": re.sub(r"\D", "", wa_id)}).get("despachos", [])
    except ERPNoDisponible as e:
        log.warning("No se pudo consultar los despachos pendientes: %s", e)
        return []


def guardar_datos_despacho(wa_id: str, pedido_id: str, datos: dict) -> dict:
    """Envía los datos de entrega y pago al ERP, que los valida. Lanza ERPNoDisponible con el motivo si los rechaza."""
    if not cfg.ERP_URL:
        raise ERPNoDisponible("El despacho solo funciona con el ERP en la nube.")
    return llamar("POST", "/api/erp/despachos", cuerpo={"wa_id": re.sub(r"\D", "", wa_id or ""), "pedido_id": pedido_id, **datos})
