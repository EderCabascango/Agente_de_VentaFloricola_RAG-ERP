"""Tools del agente. Todas son de SOLO LECTURA sobre el ERP.

`preparar_borrador_pedido` no escribe nada: devuelve un acuse y el grafo guarda el borrador
en el estado, donde lo revisa el validador determinista.

Descripciones y salidas compactas a propósito: el esquema de las tools viaja en CADA llamada al LLM
y el plan gratuito de Groq permite ~8K tokens por minuto.
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from .erp_client import ERPClient
from .validators import BorradorPedido, ItemBorrador, descuento_por_volumen

_erp = ERPClient()


@tool
def buscar_politicas(pregunta: str) -> dict:
    """Políticas: descuentos, envíos y tiempos por destino, pedidos especiales, temporadas, reclamos de calidad, cuidado de la flor."""
    from .rag import buscar

    return {"fragmentos": buscar(pregunta, k=2)}


@tool
def buscar_producto(texto: str) -> dict:
    """Busca flores por variedad, color, largo o SKU."""
    res = _erp.buscar_productos(texto, limite=3)
    return {"resultados": [{k: p[k] for k in ("sku", "nombre", "precio_unitario", "unidad")} for p in res]}


@tool
def consultar_inventario(producto: str) -> dict:
    """Tallos disponibles por finca (CAY Cayambe, TAB Tabacundo, LAT Latacunga). Acepta SKU o nombre."""
    prod = _erp.resolver_producto(producto)
    if not prod:
        return {"error": f"Producto '{producto}' no existe."}
    stock = {s["bodega"]: s["stock_disponible"] for s in _erp.inventario(prod["sku"])}
    return {"sku": prod["sku"], "nombre": prod["nombre"], "stock": stock, "total": sum(stock.values())}


@tool
def consultar_cliente(id_o_ruc: str) -> dict:
    """Cliente por ID, identificación tributaria o nombre: crédito disponible y descuento máximo."""
    c = _erp.get_cliente(id_o_ruc)
    if not c:
        return {"error": f"Cliente {id_o_ruc} no encontrado."}
    return {"id": c["id"], "razon_social": c["razon_social"], "credito_disponible": c["credito_disponible"],
            "descuento_max_pct": c["descuento_max"]}


@tool
def calcular_cotizacion(producto: str, cantidad: int, cliente: Optional[str] = None) -> dict:
    """Precio ERP por tallo, descuento aplicable y total. cantidad = tallos. Acepta SKU o nombre; cliente = ID o identificación."""
    prod = _erp.resolver_producto(producto)
    if not prod:
        return {"error": f"Producto '{producto}' no existe."}
    pct_vol = descuento_por_volumen(cantidad)
    pct, tope = pct_vol, None
    if cliente:
        c = _erp.get_cliente(cliente)
        if c:
            tope = c["descuento_max"]
            pct = min(pct_vol, tope)
    bruto = round(prod["precio_unitario"] * cantidad, 2)
    desc = round(bruto * pct / 100, 2)
    res = {"sku": prod["sku"], "nombre": prod["nombre"], "cantidad": cantidad, "unidad": prod["unidad"],
           "precio_unitario": prod["precio_unitario"], "descuento_volumen_pct": pct_vol}
    if tope is not None:
        res["descuento_max_cliente_pct"] = tope
    res.update({"descuento_aplicable_pct": pct, "subtotal": bruto, "descuento_usd": desc,
                "total_neto": round(bruto - desc, 2),
                "stock": {s["bodega"]: s["stock_disponible"] for s in _erp.inventario(prod["sku"])}})
    return res


@tool(args_schema=BorradorPedido)
def preparar_borrador_pedido(cliente: str, items: list[ItemBorrador], destino: Optional[str] = None) -> dict:
    """Borrador de pedido con lo que pidió el cliente (no escribe en el ERP). El sistema lo valida."""
    return {"estado": "BORRADOR_RECIBIDO"}


TOOLS = [buscar_politicas, calcular_cotizacion, preparar_borrador_pedido, consultar_inventario,
         consultar_cliente, buscar_producto]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
