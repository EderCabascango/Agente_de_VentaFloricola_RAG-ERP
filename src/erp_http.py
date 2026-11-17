"""ERP en la nube (Cloudflare D1) visto desde el agente, por HTTP.

Cuando hay ERP_URL y ERP_TOKEN en el .env, `ERPClient()` devuelve este cliente en vez de abrir `erp.db`.
Las lecturas son las mismas (productos, inventario, clientes); las escrituras que hace el agente son pocas
y explícitas: registrar un cliente nuevo, dejar un pedido PENDIENTE, anotar auditoría y mensajes.
Aceptar o rechazar un pedido NO se hace desde aquí: lo hace una persona en la plataforma.
"""
from __future__ import annotations

import functools
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from . import config as cfg

log = logging.getLogger("erp")

TTL_PRODUCTOS_S = 300     # el catálogo casi no cambia: se guarda en memoria 5 minutos
TIMEOUT_S = 20

# Catálogo en memoria, compartido por todos los clientes (cada validación crea uno nuevo)
_CACHE: dict = {"productos": [], "indice": {}, "t": 0.0}
_CANDADO = threading.Lock()


class ERPNoDisponible(RuntimeError):
    """El ERP en la nube no respondió o rechazó la petición."""

    def __init__(self, mensaje: str, estado: Optional[int] = None):
        super().__init__(mensaje)
        self.estado = estado


def llamar(metodo: str, ruta: str, params: Optional[dict] = None, cuerpo: Optional[dict] = None) -> dict:
    if not cfg.ERP_URL or not cfg.ERP_TOKEN:
        raise ERPNoDisponible("Falta ERP_URL o ERP_TOKEN en el .env.")
    url = cfg.ERP_URL + ruta + (("?" + urllib.parse.urlencode(params)) if params else "")
    datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo, headers={
        "Authorization": f"Bearer {cfg.ERP_TOKEN}", "Content-Type": "application/json",
        "User-Agent": "agente-floricola/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error") or f"HTTP {e.code}"
        except Exception:  # noqa: BLE001
            msg = f"HTTP {e.code}"
        raise ERPNoDisponible(msg, e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ERPNoDisponible(f"No hay conexión con el ERP: {e}") from None


@functools.lru_cache(maxsize=1)
def crear_cliente_http():
    from .erp_client import ERPClient

    class ERPClientHTTP(ERPClient):
        """Misma interfaz de lectura que `ERPClient`, contra la API del ERP en la nube."""

        def __init__(self, db_path=None):  # db_path se ignora: los datos están en la nube
            pass

        @staticmethod
        def _refrescar() -> None:
            with _CANDADO:
                if _CACHE["productos"] and time.monotonic() - _CACHE["t"] < TTL_PRODUCTOS_S:
                    return
                productos = llamar("GET", "/api/erp/productos")["productos"]
                _CACHE.update(productos=productos, indice={p["sku"].upper(): p for p in productos}, t=time.monotonic())

        def listar_productos(self) -> list[dict]:
            self._refrescar()
            return list(_CACHE["productos"])

        def get_producto(self, sku: str) -> dict | None:
            from .erp_client import _GUIONES
            self._refrescar()
            return _CACHE["indice"].get(_GUIONES.sub("-", str(sku)).strip().upper())

        def inventario(self, sku: str) -> list[dict]:
            return llamar("GET", "/api/erp/inventario", {"sku": sku.strip()})["inventario"]

        def get_cliente(self, id_ruc_o_nombre: str) -> dict | None:
            q = str(id_ruc_o_nombre or "").strip()
            return llamar("GET", "/api/erp/clientes", {"q": q})["cliente"] if q else None

        # Lo usaba solo el panel de Streamlit (demo local con SQLite); en la nube se ve en la plataforma.
        def _all(self, sql: str, params: tuple = ()) -> list[dict]:
            raise NotImplementedError("Con el ERP en la nube, esto se consulta en la plataforma.")

    return ERPClientHTTP
