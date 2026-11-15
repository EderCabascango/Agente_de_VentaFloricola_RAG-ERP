"""Validador determinista del borrador de pedido. Es código, NO el LLM.

Reglas (en este orden):
  1. el cliente existe
  2. cada variedad (SKU) existe
  3. la cantidad es entera y > 0, en bonches completos de 25 tallos, y no supera el stock de la finca
  4. el precio SIEMPRE sale del ERP (si el LLM puso otro, se sobrescribe y se registra)
  5. el descuento no supera el máximo del cliente ni la política por volumen
  6. el total no supera el crédito disponible
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .erp_client import ERPClient, _normalizar

# Política de descuento por volumen (por línea). Refleja data/policies/descuentos_volumen.md
POLITICA_DESCUENTO_VOLUMEN = [  # (tallos mínimos por línea, % descuento)
    (10000, 12.0),
    (5000, 8.0),
    (1000, 5.0),
    (1, 0.0),
]

TALLOS_POR_BONCHE = 25
TALLOS_POR_CAJA = 400  # caja completa = 16 bonches de 25 tallos (ver data/policies/presentaciones.md)
# Quien compra flor habla en cajas, bonches o tallos: el código convierte, el LLM no hace cuentas.
TALLOS_POR_UNIDAD = {"tallo": 1, "tallos": 1, "bonche": TALLOS_POR_BONCHE, "bonches": TALLOS_POR_BONCHE,
                     "ramo": TALLOS_POR_BONCHE, "ramos": TALLOS_POR_BONCHE,
                     "caja": TALLOS_POR_CAJA, "cajas": TALLOS_POR_CAJA}


def a_tallos(cantidad: float, unidad: Optional[str]) -> tuple[float, Optional[int]]:
    """(cantidad en tallos, tallos por unidad). El segundo valor es None si la unidad no se reconoce."""
    factor = TALLOS_POR_UNIDAD.get(_normalizar(unidad or "tallos").strip())
    return (cantidad * factor if factor else cantidad), factor

# Fincas con cuarto frío. Si el cliente no pide una, se despacha desde Cayambe.
FINCA_POR_NOMBRE = {"cayambe": "CAY", "tabacundo": "TAB", "latacunga": "LAT"}
FINCA_POR_DEFECTO = "CAY"


def descuento_por_volumen(cantidad: float) -> float:
    for minimo, pct in POLITICA_DESCUENTO_VOLUMEN:
        if cantidad >= minimo:
            return pct
    return 0.0


# ------------------------------- modelos ------------------------------------

class ItemBorrador(BaseModel):
    # El LLM ve este campo como "producto": llamarlo "sku" lo invitaba a inventar códigos (p. ej. "VAR-012").
    model_config = ConfigDict(populate_by_name=True)
    sku: str = Field(alias="producto", description="Flor tal como la nombró el cliente (variedad, color, largo)")
    cantidad: float = Field(description="Cantidad en la unidad que dijo el cliente")
    unidad: Optional[str] = Field(None, description="tallos (por defecto), bonches o cajas: la que dijo el cliente")
    descuento_pct: Optional[float] = Field(None, description="% pedido por el cliente")
    precio_unitario: Optional[float] = None
    bodega: Optional[str] = Field(None, description="Finca, solo si el cliente la pidió")


class BorradorPedido(BaseModel):
    cliente: str = Field(description="ID, identificación tributaria o razón social del cliente")
    items: list[ItemBorrador]
    destino: Optional[str] = Field(None, description="Ciudad o país de destino")


class Check(BaseModel):
    nombre: str
    ok: bool
    detalle: str
    sku: Optional[str] = None


class LineaValidada(BaseModel):
    solicitado: str = ""
    sku: str
    nombre: str
    unidad: str
    cantidad: int
    bodega: str
    precio_unitario: float
    descuento_pct: float
    subtotal: float


class ResultadoValidacion(BaseModel):
    aprobado: bool
    cliente: Optional[dict] = None
    destino: Optional[str] = None
    lineas: list[LineaValidada] = []
    total: float = 0.0
    checks: list[Check] = []
    correcciones: list[str] = []
    motivos: list[str] = []
    alternativas: list[str] = []


# ------------------------------ validador -----------------------------------

def _elegir_bodega(stock: list[dict], pedida: Optional[str], cantidad: float) -> str:
    bodegas = {s["bodega"]: s["stock_disponible"] for s in stock}
    pedida = FINCA_POR_NOMBRE.get(_normalizar(pedida or "").strip(), (pedida or "").upper())
    if pedida in bodegas:
        return pedida
    preferida = FINCA_POR_DEFECTO
    if bodegas.get(preferida, 0) >= cantidad:
        return preferida
    # la de mayor stock (stock viene ordenado desc)
    return stock[0]["bodega"] if stock else preferida


def validar_borrador(borrador: dict, erp: ERPClient | None = None) -> ResultadoValidacion:
    erp = erp or ERPClient()
    checks: list[Check] = []
    correcciones: list[str] = []
    alternativas: list[str] = []

    try:
        b = BorradorPedido.model_validate(borrador)
    except ValidationError as e:
        msg = f"Borrador con formato inválido: {e.errors()[0]['msg']}"
        return ResultadoValidacion(aprobado=False, checks=[Check(nombre="formato", ok=False, detalle=msg)],
                                   motivos=[msg])

    cliente = erp.get_cliente(b.cliente)
    if not cliente:
        msg = f"Cliente '{b.cliente}' no existe en el ERP."
        return ResultadoValidacion(aprobado=False, checks=[Check(nombre="cliente_existe", ok=False, detalle=msg)],
                                   motivos=[msg])
    checks.append(Check(nombre="cliente_existe", ok=True, detalle=f"{cliente['razon_social']} ({cliente['ruc']})"))

    if not b.items:
        msg = "El borrador no tiene productos."
        checks.append(Check(nombre="items", ok=False, detalle=msg))
        return ResultadoValidacion(aprobado=False, cliente=cliente, checks=checks, motivos=[msg])

    lineas: list[LineaValidada] = []
    total_solicitado = 0.0
    for it in b.items:
        prod, opciones = erp.resolver_estricto(it.sku)
        if opciones:
            lista = "; ".join(f"{o['nombre']} (${o['precio_unitario']:.2f} por tallo)" for o in opciones)
            checks.append(Check(nombre="producto_ambiguo", ok=False, sku=it.sku,
                                detalle=f"'{it.sku}' puede ser varias flores: {lista}. Falta que el cliente elija una."))
            alternativas.append(f"Preguntar al cliente cuál de estas flores quiere: {lista}.")
            continue
        if not prod:
            checks.append(Check(nombre="sku_existe", ok=False, sku=it.sku, detalle=f"Producto '{it.sku}' no existe en el ERP."))
            continue
        identificado = prod["nombre"] if prod["sku"].upper() == it.sku.strip().upper() else f"'{it.sku}' → {prod['nombre']}"
        checks.append(Check(nombre="sku_existe", ok=True, sku=prod["sku"], detalle=identificado))
        sku = prod["sku"]

        # Cantidad
        tallos, factor = a_tallos(it.cantidad, it.unidad)
        if factor is None:
            checks.append(Check(nombre="cantidad_valida", ok=False, sku=sku,
                                detalle=f"Unidad '{it.unidad}' no reconocida: use tallos, bonches o cajas."))
            continue
        cant_ok = it.cantidad > 0 and float(it.cantidad).is_integer()
        unidad_txt = _normalizar(it.unidad or "tallos").strip()
        if cant_ok and factor > 1:
            detalle_cant = f"{it.cantidad:g} {unidad_txt} = {int(tallos)} tallos"
        elif cant_ok:
            detalle_cant = f"{int(tallos)} tallos"
        else:
            detalle_cant = f"Cantidad inválida: {it.cantidad:g} (debe ser entera y > 0)"
        checks.append(Check(nombre="cantidad_valida", ok=cant_ok, sku=sku, detalle=detalle_cant))
        cantidad = int(tallos) if cant_ok else 0

        # Bonches completos: la flor se arma y se vende en bonches de 25 tallos
        if cant_ok:
            bonche_ok = cantidad % TALLOS_POR_BONCHE == 0
            abajo = cantidad - cantidad % TALLOS_POR_BONCHE
            arriba = abajo + TALLOS_POR_BONCHE
            checks.append(Check(
                nombre="bonche_completo", ok=bonche_ok, sku=sku,
                detalle=(f"{cantidad // TALLOS_POR_BONCHE} bonches de {TALLOS_POR_BONCHE} tallos" if bonche_ok
                         else f"{cantidad} tallos no completa bonches de {TALLOS_POR_BONCHE}"),
            ))
            if not bonche_ok:
                opciones = " o ".join(str(x) for x in (abajo, arriba) if x > 0)
                alternativas.append(f"{sku}: pedir {opciones} tallos (bonches completos de {TALLOS_POR_BONCHE}).")

        # Stock por finca
        stock = erp.inventario(sku)
        bodega = _elegir_bodega(stock, it.bodega, cantidad)
        disp = next((s["stock_disponible"] for s in stock if s["bodega"] == bodega), 0)
        stock_ok = cant_ok and cantidad <= disp
        resto = [s for s in stock if s["bodega"] != bodega]
        # El total de las otras fincas va ya sumado: si el LLM lo suma por su cuenta, el grounding lo rechaza.
        otras = (", ".join(f"{s['bodega']}: {s['stock_disponible']}" for s in resto)
                 + f" ({sum(s['stock_disponible'] for s in resto)} tallos entre las otras fincas)")
        checks.append(Check(
            nombre="stock_suficiente", ok=stock_ok, sku=sku,
            detalle=(f"Finca {bodega}: {disp} tallos disponibles" if stock_ok
                     else f"Stock insuficiente en finca {bodega}: pide {cantidad} tallos, hay {disp}. Otras fincas: {otras}"),
        ))
        if cant_ok and not stock_ok:
            total_disp = sum(s["stock_disponible"] for s in stock)
            # La alternativa va con sus cifras ya calculadas: el LLM no debe hacer cuentas.
            pct_alt = min(descuento_por_volumen(disp), float(cliente["descuento_max"]))
            total_alt = round(disp * float(prod["precio_unitario"]) * (1 - pct_alt / 100), 2)
            entra = "dentro" if total_alt <= float(cliente["credito_disponible"]) else "fuera"
            alternativas.append(
                f"{sku}: máximo {disp} {prod['unidad']} desde la finca {bodega}, con {pct_alt:g}% de descuento = "
                f"${total_alt:,.2f} ({entra} del crédito disponible).")
            if total_disp > disp:
                pct_comb = min(descuento_por_volumen(total_disp), float(cliente["descuento_max"]))
                total_comb = round(total_disp * float(prod["precio_unitario"]) * (1 - pct_comb / 100), 2)
                detalle_bod = " + ".join(f"{s['bodega']} {s['stock_disponible']}" for s in stock)
                entra_c = "dentro" if total_comb <= float(cliente["credito_disponible"]) else "fuera"
                alternativas.append(
                    f"{sku}: combinando fincas, máximo {total_disp} {prod['unidad']} ({detalle_bod}), con "
                    f"{pct_comb:g}% de descuento = ${total_comb:,.2f} ({entra_c} del crédito disponible).")
            sustitutos = [
                p for p in erp.listar_productos()
                if p["categoria"] == prod["categoria"] and p["sku"] != sku and p["unidad"] == prod["unidad"]
                and max((s["stock_disponible"] for s in erp.inventario(p["sku"])), default=0) >= cantidad
            ]
            for s in sustitutos[:2]:
                alternativas.append(f"Sustituto con stock suficiente: {s['sku']} {s['nombre']} a ${s['precio_unitario']:.2f}.")

        # Precio: siempre del ERP
        precio = float(prod["precio_unitario"])
        if it.precio_unitario is not None and abs(float(it.precio_unitario) - precio) > 0.005:
            correcciones.append(f"{sku}: precio propuesto ${it.precio_unitario:.2f} reemplazado por precio ERP ${precio:.2f}.")
        checks.append(Check(nombre="precio_erp", ok=True, sku=sku, detalle=f"${precio:.2f} por tallo (fuente: ERP)"))

        # Descuento
        pct_vol = descuento_por_volumen(cantidad)
        permitido = min(pct_vol, float(cliente["descuento_max"]))
        if it.descuento_pct is None or it.descuento_pct == 0:
            # Sin descuento solicitado: se aplica el que da la política (derecho del cliente)
            aplicado = permitido
            checks.append(Check(nombre="descuento_permitido", ok=True, sku=sku,
                                detalle=f"{aplicado:g}% aplicado según política (volumen {pct_vol:g}%, máx. cliente {cliente['descuento_max']:g}%)"))
        else:
            desc_ok = float(it.descuento_pct) <= permitido + 1e-9 and it.descuento_pct >= 0
            aplicado = float(it.descuento_pct) if desc_ok else permitido
            checks.append(Check(
                nombre="descuento_permitido", ok=desc_ok, sku=sku,
                detalle=(f"{it.descuento_pct:g}% dentro del máximo {permitido:g}%" if desc_ok
                         else f"Descuento {it.descuento_pct:g}% supera el máximo permitido {permitido:g}% "
                              f"(política por volumen {pct_vol:g}%, máximo del cliente {cliente['descuento_max']:g}%)"),
            ))
            # Si además falta stock, las alternativas de stock ya traen su descuento correcto: otra alternativa
            # "con el descuento máximo" invitaba al LLM a recalcular totales por su cuenta (y equivocarse).
            if not desc_ok and stock_ok:
                alternativas.append(f"{sku}: descuento máximo aplicable {permitido:g}%.")

        subtotal = round(cantidad * precio * (1 - aplicado / 100), 2)
        # Lo que el cliente pidió (cantidad y descuento solicitados), para el chequeo de crédito
        desc_pedido = float(it.descuento_pct) if it.descuento_pct else aplicado
        total_solicitado += cantidad * precio * (1 - desc_pedido / 100)
        lineas.append(LineaValidada(solicitado=it.sku, sku=sku, nombre=prod["nombre"], unidad=prod["unidad"], cantidad=cantidad,
                                    bodega=bodega, precio_unitario=precio, descuento_pct=aplicado, subtotal=subtotal))

    # Crédito: se evalúa sobre lo que el cliente pidió
    total = round(sum(l.subtotal for l in lineas), 2)
    if lineas:
        total_pedido = round(total_solicitado, 2)
        credito = float(cliente["credito_disponible"])
        cred_ok = total_pedido <= credito
        checks.append(Check(
            nombre="credito_disponible", ok=cred_ok,
            detalle=(f"Total ${total_pedido:,.2f} dentro del crédito disponible ${credito:,.2f}" if cred_ok
                     else f"Total ${total_pedido:,.2f} supera el crédito disponible ${credito:,.2f}"),
        ))
        if not cred_ok:
            alternativas.append(f"Reducir el pedido a máximo ${credito:,.2f} o gestionar ampliación de crédito con un asesor.")

    motivos = [c.detalle for c in checks if not c.ok]
    return ResultadoValidacion(
        aprobado=not motivos and bool(lineas), cliente=cliente, destino=b.destino,
        lineas=lineas, total=total, checks=checks, correcciones=correcciones,
        motivos=motivos, alternativas=alternativas,
    )
