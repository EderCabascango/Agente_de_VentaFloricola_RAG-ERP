"""El ERP en la nube visto desde el agente: lecturas por HTTP, pedido PENDIENTE y mensajes para la plataforma.

No se usa red: `ERPFalso` hace de nube y responde las llamadas con el ERP SQLite de los tests.
El Worker real (Cloudflare D1) se prueba aparte, con wrangler en local.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from mock_llm import ScriptedChatModel, llamar as llamar_tool, responder, tool_call
from src import config as cfg
from src import erp_client, erp_http
from src.erp_client import ERPClient, registrar_cliente_whatsapp
from src.erp_http import ERPNoDisponible
from src.graph import build_graph, pendiente_de_aprobacion
from test_webhook import AJUSTES, CLIENTE_WA, APIFalsa, _payload, _post
from webhook import crear_app

WA = "593991112233"


class ERPFalso:
    def __init__(self):
        self.real = ERPClient()          # se crea antes de activar ERP_URL: es el de SQLite
        self.llamadas: list[tuple] = []
        self.cuerpos: dict[str, dict] = {}
        self.mensajes: list[dict] = []
        self.caido_en: str | None = None
        self.despachos: list[dict] = []          # lo que el ERP dice que está por despachar
        self.rechazo_despacho: str | None = None

    def __call__(self, metodo, ruta, params=None, cuerpo=None):
        self.llamadas.append((metodo, ruta))
        if self.caido_en and ruta == self.caido_en:
            raise ERPNoDisponible("El ERP no responde", 503)
        db = cfg.DB_PATH
        if cuerpo is not None:
            self.cuerpos[ruta] = cuerpo
        if ruta == "/api/erp/productos":
            return {"ok": True, "productos": self.real.listar_productos()}
        if ruta == "/api/erp/inventario":
            return {"ok": True, "inventario": self.real.inventario(params["sku"])}
        if ruta == "/api/erp/clientes":
            return {"ok": True, "cliente": self.real.get_cliente(params["q"])}
        if ruta == "/api/erp/clientes/whatsapp":
            r = registrar_cliente_whatsapp(cuerpo["wa_id"], cuerpo["nombre"], db_path=db,
                                           limite_credito=cuerpo["limite_credito"], descuento_max=cuerpo["descuento_max"])
            return {"ok": True, **r}
        if ruta == "/api/erp/pedidos/pendiente":
            lineas = [dict(l, subtotal=round(l["cantidad"] * l["precio_unitario"] * (1 - l["descuento_pct"] / 100), 2))
                      for l in cuerpo["lineas"]]
            v = {"aprobado": True, "cliente": {"id": cuerpo["cliente_id"]}, "destino": cuerpo.get("destino"), "lineas": lineas}
            r = erp_client.registrar_pedido_pendiente(v, wa_id=cuerpo.get("wa_id"), db_path=db)
            return {"ok": True, "pedido_id": r["pedido_id"], "total": r["total"]}
        if ruta == "/api/erp/auditoria":
            erp_client.registrar_auditoria(cuerpo["usuario"], cuerpo["accion"], cuerpo.get("payload"), cuerpo.get("resultado", ""), db_path=db)
            return {"ok": True}
        if ruta == "/api/erp/mensajes":
            self.mensajes.append(cuerpo)
            return {"ok": True}
        if ruta == "/api/erp/despachos/pendientes":
            return {"ok": True, "despachos": self.despachos}
        if ruta == "/api/erp/despachos":
            if self.rechazo_despacho:
                raise ERPNoDisponible(self.rechazo_despacho, 422)
            return {"ok": True, "pedido_id": cuerpo["pedido_id"], "estado": "POR_VALIDAR"}
        raise AssertionError(f"ruta no prevista: {ruta}")


@pytest.fixture
def nube(monkeypatch):
    falso = ERPFalso()
    monkeypatch.setattr(cfg, "ERP_URL", "http://nube.test")
    monkeypatch.setattr(cfg, "ERP_TOKEN", "t" * 40)
    monkeypatch.setattr(cfg, "APROBACION", "erp")
    monkeypatch.setattr(erp_http, "llamar", falso)
    monkeypatch.setattr(erp_client, "llamar", falso)
    erp_http._CACHE.update(productos=[], indice={}, t=0.0)
    return falso


def _filas(sql, *params):
    c = sqlite3.connect(cfg.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def _correr(nube, guion, mensaje="pedido", cliente="C001"):
    g = build_graph(ScriptedChatModel(guion=guion), aprobacion="erp")
    conf = {"configurable": {"thread_id": uuid.uuid4().hex}}
    g.invoke({"messages": [HumanMessage(content=mensaje)], "cliente_canal": nube.real.get_cliente(cliente), "wa_id": WA}, conf)
    return g, conf


def _guion_pedido():
    return [llamar_tool(tool_call("preparar_borrador_pedido", cliente="C001", destino="Quito",
                                  items=[{"sku": "ROS-002", "cantidad": 2000}, {"sku": "ROS-004", "cantidad": 1000}]))]


# ------------------------------------------------------------------ lecturas por HTTP
def test_el_cliente_http_lee_catalogo_inventario_y_clientes(nube):
    erp = ERPClient()
    assert type(erp).__name__ == "ERPClientHTTP"
    assert len(erp.listar_productos()) == 14 and erp.get_producto("ROS‑002")["precio_unitario"] == 0.28   # guion Unicode
    assert erp.inventario("ROS-002")[0]["bodega"] == "CAY"
    assert erp.get_cliente("C001")["credito_disponible"] == 8000 and erp.get_cliente("") is None
    ERPClient().listar_productos(); ERPClient().get_producto("ROS-001")
    assert nube.llamadas.count(("GET", "/api/erp/productos")) == 1, "el catálogo se guarda en memoria y se comparte"


def test_sin_erp_url_se_usa_el_erp_local():
    assert type(ERPClient()).__name__ == "ERPClient"


# ------------------------------------------------------------------ pedido pendiente
def test_pedido_valido_queda_pendiente_en_el_erp_y_el_cliente_ve_precio_cantidad_y_total(nube):
    g, conf = _correr(nube, _guion_pedido())
    assert pendiente_de_aprobacion(g, conf["configurable"]["thread_id"]) is None, "ya no se pausa para aprobar por WhatsApp"
    rf = g.get_state(conf).values["respuesta_final"]
    assert rf["pedido_id"] == "PED-0004"
    for esperado in ("PED-0004", "pendiente de aprobación", "2000 tallos (80 bonches)", "$0.28 c/u", "1000 tallos (40 bonches)",
                     "$0.30 c/u", "*Total: $817.00*"):
        assert esperado in rf["respuesta"], esperado
    (p,) = _filas("SELECT * FROM pedidos WHERE id='PED-0004'")
    assert p["estado"] == "PENDIENTE" and p["wa_id"] == WA and p["total"] == 817.0 and p["destino"] == "Quito"
    stock = {s["bodega"]: s for s in nube.real.inventario("ROS-002")}
    assert stock["CAY"]["stock_disponible"] == 12000 and stock["CAY"]["stock_reservado"] == 0, "pendiente no mueve stock"
    assert "PEDIDO_PENDIENTE" in [a["accion"] for a in nube.real.auditoria(20)]


def test_pedido_rechazado_por_el_validador_no_queda_pendiente(nube):
    guion = [
        llamar_tool(tool_call("preparar_borrador_pedido", cliente="C001", destino="Miami",
                              items=[{"producto": "rosa freedom roja 70 cm", "cantidad": 40000, "descuento_pct": 25}])),
        responder("El pedido no pasó la validación: en Cayambe hay 4000 tallos y su descuento máximo es 10%. "
                  "Le propongo 4000 tallos desde CAY con 5% por $1,254.00. ¿Confirma?"),
    ]
    _correr(nube, guion, mensaje="quiero 40000 tallos de rosa freedom roja 70 cm con 25% de descuento")
    assert _filas("SELECT * FROM pedidos WHERE estado='PENDIENTE'") == []


def test_si_el_erp_cae_el_cliente_igual_recibe_respuesta(nube):
    nube.caido_en = "/api/erp/pedidos/pendiente"
    g, conf = _correr(nube, _guion_pedido())
    st = g.get_state(conf).values
    assert "falla técnica" in st["respuesta_final"]["respuesta"] and st["respuesta_final"]["escalar_a_asesor"]
    assert _filas("SELECT * FROM pedidos WHERE estado='PENDIENTE'") == []


# ------------------------------------------------------------------ clientes y conversaciones
def test_cliente_nuevo_se_registra_en_la_nube_con_condiciones_de_prueba(nube):
    r = registrar_cliente_whatsapp("5491100000000", "Lucía")
    assert r == {"id": "W5491100000000", "nuevo": True}
    cuerpo = nube.cuerpos["/api/erp/clientes/whatsapp"]
    assert cuerpo["wa_id"] == "5491100000000" and cuerpo["limite_credito"] == 2000 and cuerpo["descuento_max"] == 0


def test_el_webhook_avisa_con_el_total_y_guarda_la_conversacion_para_la_plataforma(nube):
    api = APIFalsa()
    app = crear_app(grafo=build_graph(ScriptedChatModel(guion=_guion_pedido()), aprobacion="erp"), api=api, ajustes=AJUSTES)
    _post(TestClient(app), _payload(CLIENTE_WA, "Queremos pedir 2000 tallos de Freedom 60 y 1000 de Explorer 60"))
    (respuesta,) = api.textos_a(CLIENTE_WA)
    assert "PED-0004" in respuesta and "*Total: $817.00*" in respuesta
    assert not [e for e in api.enviados if e[0] == "botones"], "nadie recibe botones de aprobar por WhatsApp"
    cliente, agente = nube.mensajes
    assert cliente["rol"] == "cliente" and cliente["wa_id"] == CLIENTE_WA and "Freedom" in cliente["texto"]
    assert agente["rol"] == "agente" and "PED-0004" in agente["texto"]
    nodos = [t["nodo"] for t in agente["traza"]]
    assert "registrar_pendiente" in nodos and any(n.startswith("tool:") for n in nodos)


# ------------------------------------------------------------------ datos de despacho por WhatsApp
DATOS_DESPACHO = dict(pedido_id="PED-0007", contacto_nombre="Ana Pérez", contacto_telefono="0991234567",
                      direccion="Av. Amazonas N24-10 y Colón", ciudad="Quito", fecha_entrega="2030-01-15",
                      metodo_pago="transferencia")


def test_el_contexto_pide_los_datos_de_despacho_de_un_pedido_aceptado(nube):
    from src.graph import _contexto_despachos
    nube.despachos = [{"pedido_id": "PED-0007", "total": 120.0, "destino": "Quito", "estado": "FALTAN_DATOS"}]
    texto, pedir = _contexto_despachos(WA)
    assert pedir and "PED-0007" in texto and "guardar_datos_despacho" in texto and "AAAA-MM-DD" in texto
    nube.despachos = [{"pedido_id": "PED-0007", "total": 120.0, "destino": None, "estado": "DEVUELTO", "motivo_devolucion": "Falta el número de casa"}]
    texto, pedir = _contexto_despachos(WA)
    assert pedir and "Falta el número de casa" in texto
    nube.despachos = [{"pedido_id": "PED-0007", "total": 120.0, "destino": None, "estado": "POR_VALIDAR"}]
    texto, pedir = _contexto_despachos(WA)
    assert not pedir and "validando" in texto


def test_sin_pedidos_aceptados_no_hay_contexto_de_despacho(nube):
    from src.graph import _contexto_despachos
    assert _contexto_despachos(WA) == ("", False)
    assert _contexto_despachos(None) == ("", False)


def test_el_agente_guarda_los_datos_con_el_numero_que_escribe_no_con_el_que_diga_el_modelo(nube):
    nube.despachos = [{"pedido_id": "PED-0007", "total": 120.0, "destino": "Quito", "estado": "FALTAN_DATOS"}]
    guion = [llamar_tool(tool_call("guardar_datos_despacho", wa_id="593900000000", **DATOS_DESPACHO)),
             responder("Gracias, ya tenemos sus datos. Un asesor los validará y le avisaremos por este chat.")]
    g, conf = _correr(nube, guion, mensaje="Ana Pérez, 0991234567, Av. Amazonas N24-10 y Colón, Quito, el 2030-01-15, transferencia")
    cuerpo = nube.cuerpos["/api/erp/despachos"]
    assert cuerpo["wa_id"] == WA and cuerpo["pedido_id"] == "PED-0007" and cuerpo["metodo_pago"] == "transferencia"
    assert "validará" in g.get_state(conf).values["respuesta_final"]["respuesta"]


def test_si_el_erp_rechaza_los_datos_el_agente_recibe_el_motivo(nube):
    nube.rechazo_despacho = "La fecha de entrega no puede ser anterior a hoy."
    from src.tools import guardar_datos_despacho
    r = guardar_datos_despacho.invoke({**DATOS_DESPACHO, "wa_id": WA})
    assert r == {"error": "La fecha de entrega no puede ser anterior a hoy."}


def test_la_herramienta_no_acepta_campos_en_blanco_y_dice_cuales_faltan(nube):
    from src.tools import guardar_datos_despacho
    r = guardar_datos_despacho.invoke({**DATOS_DESPACHO, "direccion": "", "ciudad": " ", "wa_id": WA})
    assert "direccion" in r["error"] and "ciudad" in r["error"]
    assert "/api/erp/despachos" not in nube.cuerpos


def test_el_contexto_trae_el_enlace_del_formulario_y_manda_a_usarlo(nube):
    from src.graph import _contexto_despachos
    enlace = "https://fuyu.test/despacho/?p=PED-0007&t=1900000000.abc"
    nube.despachos = [{"pedido_id": "PED-0007", "total": 120.0, "destino": "Quito", "estado": "FALTAN_DATOS", "enlace": enlace}]
    texto, pedir = _contexto_despachos(WA)
    assert pedir and enlace in texto and "formulario" in texto.lower() and "uno por uno" in texto
