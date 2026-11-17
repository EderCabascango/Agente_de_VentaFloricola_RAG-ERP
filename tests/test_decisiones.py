"""Decisiones de ingeniería que reemplazaron criterio del LLM por reglas deterministas."""
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mock_llm import ScriptedChatModel, llamar, responder, tool_call
from src.escenarios import ESCENARIOS
from src.graph import _aplanar_para_responder, build_graph, pendiente_de_aprobacion, recortar_historial
from src.grounding import verificar_grounding
from src.guardrails import detectar_intencion
from src.validators import validar_borrador


@pytest.mark.parametrize("texto,esperado", [
    (ESCENARIOS[0]["mensaje"], "cotizacion"),
    (ESCENARIOS[1]["mensaje"], "pedido"),
    (ESCENARIOS[2]["mensaje"], "pedido"),
    (ESCENARIOS[3]["mensaje"], None),
    (ESCENARIOS[4]["mensaje"], None),
    ("¿Cuánto cuesta 1000 tallos de rosa Explorer?", "cotizacion"),
    ("Sí, confirmo 4000 tallos con 5%", "pedido"),
    ("Hola, buenos días", None),
])
def test_intencion_por_reglas(texto, esperado):
    assert detectar_intencion(texto) == esperado


def test_pedido_obliga_a_pasar_por_el_validador():
    """Con intención 'pedido', la 1.ª llamada solo puede usar preparar_borrador_pedido."""
    vistos = []

    class Espia(ScriptedChatModel):
        def bind_tools(self, tools, **kw):
            vistos.append(([t.name for t in tools], kw.get("tool_choice")))
            return self

    llm = Espia(guion=[llamar(tool_call("preparar_borrador_pedido", cliente="C001",
                                        items=[{"sku": "rosa freedom roja 60 cm", "cantidad": 100}]))])
    g = build_graph(llm)
    g.invoke({"messages": [HumanMessage(content=ESCENARIOS[1]["mensaje"])]},
             {"configurable": {"thread_id": uuid.uuid4().hex}})
    assert (["preparar_borrador_pedido"], "preparar_borrador_pedido") in vistos


def test_historial_condensa_turnos_previos():
    tc = {"name": "x", "args": {}, "id": "1"}
    ms = [HumanMessage("u1"), AIMessage("", tool_calls=[tc]), ToolMessage("dato", tool_call_id="1"),
          AIMessage("f1"), HumanMessage("u2"), AIMessage("f2"), HumanMessage("u3")]
    assert [m.content for m in recortar_historial(ms)] == ["u1", "f1", "u2", "f2", "u3"]
    # con exactamente 2 turnos previos no debe generar turnos vacíos (bug encontrado en pruebas)
    assert [m.content for m in recortar_historial(ms[:5])] == ["u1", "f1", "u2"]


def test_contexto_aplanado_no_tiene_tool_calls():
    tc = {"name": "x", "args": {}, "id": "1"}
    plano = _aplanar_para_responder([HumanMessage("u"), AIMessage("", tool_calls=[tc]),
                                     ToolMessage("1200", tool_call_id="1", name="x")])
    assert all(not getattr(m, "tool_calls", None) for m in plano)
    assert not any(isinstance(m, ToolMessage) for m in plano)
    assert "1200" in plano[-1].content


def test_validador_entrega_alternativas_ya_calculadas():
    r = validar_borrador({"cliente": "C001", "destino": "Miami",
                          "items": [{"sku": "rosas freedom rojas de 70 cm", "cantidad": 20000, "descuento_pct": 25}]})
    alts = " ".join(r.alternativas)
    assert "$1,254.00" in alts           # 4000 × 0.33 × 0.95 (solo Cayambe)
    assert "$2,277.00" in alts           # 7500 × 0.33 × 0.92 (combinando las 3 fincas)


def test_producto_por_nombre_se_resuelve_sin_llm():
    r = validar_borrador({"cliente": "C001", "items": [{"sku": "explorer roja", "cantidad": 25}]})
    assert r.aprobado and r.lineas[0].sku == "ROS-004"
    r2 = validar_borrador({"cliente": "C001", "items": [{"sku": "ROS‑005", "cantidad": 25}]})  # guion Unicode
    assert r2.aprobado and r2.lineas[0].sku == "ROS-005"


def test_grounding_entiende_espacio_fino_de_miles():
    assert verificar_grounding("Total USD 12 350,00", ['{"total_neto": 12350.0}'])["ok"]


def test_rechazo_del_validador_fuerza_respuesta_sin_tools():
    vistos = []

    class Espia(ScriptedChatModel):
        def invoke(self, mensajes, config=None, **kw):
            vistos.append(any(isinstance(m, ToolMessage) for m in mensajes))
            return super().invoke(mensajes, config=config, **kw)

    llm = Espia(guion=[
        llamar(tool_call("preparar_borrador_pedido", cliente="C001",
                         items=[{"sku": "ROS-003", "cantidad": 20000, "descuento_pct": 25}])),
        responder("Hay 4000 tallos en Cayambe: con 5% el total es $1,254.00. ¿Confirma?"),
    ])
    g = build_graph(llm)
    conf = {"configurable": {"thread_id": uuid.uuid4().hex}}
    g.invoke({"messages": [HumanMessage(content=ESCENARIOS[2]["mensaje"])]}, conf)
    assert vistos == [False, False], "la respuesta tras el rechazo va con contexto aplanado (sin ToolMessages)"
    assert g.get_state(conf).values["grounding"]["ok"]


# ------------------------------------------------------------------ mejoras de venta (conversación real por WhatsApp)
from src.erp_client import ERPClient
from src.guardrails import es_confirmacion
from src.validators import a_tallos


def test_cajas_y_bonches_se_convierten_a_tallos_en_codigo():
    assert a_tallos(60, "cajas") == (24000, 400)
    assert a_tallos(10, "bonches") == (250, 25)
    assert a_tallos(100, None) == (100, 1)
    assert a_tallos(5, "toneladas")[1] is None


def test_pedido_en_cajas_no_se_rechaza_por_bonche():
    """Falla real: '60 cajas de Mondial' se tomó como 60 tallos y se rechazó por no completar bonches."""
    r = validar_borrador({"cliente": "C001", "items": [{"sku": "Mondial", "cantidad": 10, "unidad": "cajas"}]})
    assert r.aprobado and r.lineas[0].cantidad == 4000 and r.lineas[0].sku == "ROS-005"
    assert any("10 cajas = 4000 tallos" in c.detalle for c in r.checks)


def test_pedido_en_bonches():
    r = validar_borrador({"cliente": "C001", "items": [{"sku": "Mondial", "cantidad": 8, "unidad": "bonches"}]})
    assert r.aprobado and r.lineas[0].cantidad == 200


def test_cajas_que_superan_el_stock_ofrecen_alternativas_calculadas():
    r = validar_borrador({"cliente": "C001", "items": [{"sku": "Mondial", "cantidad": 60, "unidad": "cajas"}]})
    assert not r.aprobado
    assert any("pide 24000 tallos" in m for m in r.motivos)
    assert any("máximo 8000 tallos" in a for a in r.alternativas)


def test_unidad_desconocida_se_rechaza():
    r = validar_borrador({"cliente": "C001", "items": [{"sku": "Mondial", "cantidad": 2, "unidad": "toneladas"}]})
    assert not r.aprobado and any("no reconocida" in m for m in r.motivos)


def test_producto_ambiguo_pregunta_en_vez_de_elegir():
    """'Freedom' existe en 50, 60 y 70 cm: el código no puede elegir por el cliente."""
    r = validar_borrador({"cliente": "C001", "items": [{"sku": "Freedom", "cantidad": 500}]})
    assert not r.aprobado and not r.lineas
    assert any("varias flores" in m for m in r.motivos)
    assert any("50 cm" in a and "60 cm" in a and "70 cm" in a for a in r.alternativas)
    # con el largo, ya no es ambiguo
    assert validar_borrador({"cliente": "C001", "items": [{"sku": "Freedom roja 60 cm", "cantidad": 500}]}).aprobado


def test_cotizacion_ambigua_devuelve_opciones_y_en_cajas_el_equivalente():
    from src.tools import calcular_cotizacion
    amb = calcular_cotizacion.invoke({"producto": "Freedom", "cantidad": 100})
    assert "opciones" in amb and len(amb["opciones"]) == 3
    r = calcular_cotizacion.invoke({"producto": "Mondial", "cantidad": 5, "unidad": "cajas", "cliente": "C001"})
    assert r["cantidad"] == 2000 and r["equivale"] == "5 cajas = 2000 tallos" and r["descuento_aplicable_pct"] == 5.0


def test_catalogo_para_la_pregunta_que_variedades_tienes():
    from src.tools import ver_catalogo
    resumen = ver_catalogo.invoke({})
    assert resumen["total_variedades"] == 12            # el catálogo corto de los tests; el real trae 203
    assert resumen["largos_cm"] == [50, 60, 70]
    assert resumen["precio_desde"] == 0.22 and resumen["precio_hasta"] == 0.35
    assert "coincidencias" not in resumen, "sin buscar no lista las variedades, solo cuántas hay"
    freedom = ver_catalogo.invoke({"buscar": "freedom"})
    assert freedom["total_coincidencias"] == 1
    assert freedom["coincidencias"][0]["largos_cm"] == [50, 60, 70]    # los 3 largos se agrupan
    assert (freedom["coincidencias"][0]["precio_desde"], freedom["coincidencias"][0]["precio_hasta"]) == (0.24, 0.33)
    assert ver_catalogo.invoke({"buscar": "f"})["total_coincidencias"] == 2   # una letra = inicial: Freedom y Free Spirit


def test_el_catalogo_solo_tiene_rosas():
    from src.tools import ver_catalogo
    erp = ERPClient()
    assert {p["categoria"] for p in erp.listar_productos()} == {"Rosas"}
    for otra in ("gypsophila", "clavel", "girasol", "hypericum", "limonium", "alstroemeria", "crisantemo"):
        assert ver_catalogo.invoke({"buscar": otra})["total_coincidencias"] == 0, otra
    assert all(0.18 <= p["precio_unitario"] <= 0.39 for p in erp.listar_productos()), "precios entre 18 y 39 ctvs"


def test_el_prompt_dice_cuantas_variedades_hay_y_que_solo_son_rosas():
    from src import config as cfg
    from src.graph import SYSTEM_PROMPT
    texto = SYSTEM_PROMPT.replace("{empresa}", cfg.EMPRESA).replace("{n}", "203")
    assert "203 variedades" in texto and "SOLO rosas" in texto and cfg.EMPRESA in texto
    assert "{n}" not in texto and "{empresa}" not in texto


@pytest.mark.parametrize("texto,esperado", [
    ("Si me parece", True), ("sí, dale", True), ("Perfecto, esa", True), ("ok", True), ("Claro que si", True),
    ("¿Cuánto cuesta la rosa?", False), ("No, mejor 100 tallos", False), ("Quiero 500 tallos de Freedom", False),
])
def test_confirmacion(texto, esperado):
    assert es_confirmacion(texto) is esperado


def _correr(llm, mensajes, hilo=None):
    g = build_graph(llm)
    conf = {"configurable": {"thread_id": hilo or uuid.uuid4().hex}}
    cli = ERPClient().get_cliente("C001")
    for m in mensajes:
        g.invoke({"messages": [HumanMessage(content=m)], "cliente_canal": cli}, conf)
    return g, conf


def test_aceptar_una_propuesta_arma_el_pedido_sin_volver_a_preguntar():
    """Falla real: tras proponer 'pedir 75', el cliente dijo 'sí me parece' y el bot se confundió."""
    llm = ScriptedChatModel(guion=[
        llamar(tool_call("preparar_borrador_pedido", cliente="C001", items=[{"producto": "Mondial", "cantidad": 60}])),
        responder("Con bonches de 25 puedo ofrecerle 50 o 75 tallos de Mondial. ¿Cuál prefiere?"),
        llamar(tool_call("preparar_borrador_pedido", cliente="C001", items=[{"producto": "Mondial", "cantidad": 75}])),
    ])
    g, conf = _correr(llm, ["Quiero 60 tallos de Mondial", "La de 75 porfa"])
    assert pendiente_de_aprobacion(g, conf["configurable"]["thread_id"])["validacion"]["lineas"][0]["cantidad"] == 75


def test_si_con_una_sola_opcion_fuerza_el_borrador():
    vistos = []

    class Espia(ScriptedChatModel):
        def bind_tools(self, tools, **kw):
            vistos.append((tuple(t.name for t in tools), kw.get("tool_choice")))
            return self

        def invoke(self, mensajes, config=None, **kw):
            vistos.append(("prompt", mensajes[0].content))
            return super().invoke(mensajes, config=config, **kw)

    llm = Espia(guion=[
        llamar(tool_call("preparar_borrador_pedido", cliente="C001", items=[{"producto": "Mondial", "cantidad": 3000}])),
        responder("En CAY hay 8000 tallos de Mondial, caben sus 3000... "),
    ])
    # 3000 tallos de Mondial sí hay: provocamos un rechazo de descuento con UNA alternativa
    llm.guion[0] = llamar(tool_call("preparar_borrador_pedido", cliente="C001",
                                    items=[{"producto": "Mondial", "cantidad": 6000, "descuento_pct": 30}]))
    llm.guion[1] = responder("Su descuento máximo es 8%. ¿Le parece bien?")
    llm.guion.append(llamar(tool_call("preparar_borrador_pedido", cliente="C001",
                                      items=[{"producto": "Mondial", "cantidad": 6000, "descuento_pct": 8}])))
    g, conf = _correr(llm, ["Quiero 6000 tallos de Mondial con 30% de descuento", "Si, dale"])
    assert pendiente_de_aprobacion(g, conf["configurable"]["thread_id"])
    assert (("preparar_borrador_pedido",), "preparar_borrador_pedido") in vistos
    assert any(v[0] == "prompt" and "responde a tu propuesta anterior" in v[1] for v in vistos)


def test_si_con_varias_opciones_no_fuerza_nada_y_pregunta():
    class Espia(ScriptedChatModel):
        forzados: list = []

        def bind_tools(self, tools, **kw):
            if kw.get("tool_choice"):
                Espia.forzados.append(kw["tool_choice"])
            return self

    Espia.forzados = []
    llm = Espia(guion=[
        llamar(tool_call("preparar_borrador_pedido", cliente="C001", items=[{"producto": "Mondial", "cantidad": 60}])),
        responder("Puedo ofrecerle 50 o 75 tallos de Mondial. ¿Cuál prefiere?"),
        responder("Perfecto, ¿50 o 75 tallos? Dígame cuál."),
    ])
    g, conf = _correr(llm, ["Quiero 60 tallos de Mondial", "Si me parece"])
    # el 'sí' no forzó preparar_borrador_pedido en el segundo mensaje (solo el primero lo forzó por 'quiero')
    assert Espia.forzados.count("preparar_borrador_pedido") >= 1
    assert llm.llamadas == 3
    assert pendiente_de_aprobacion(g, conf["configurable"]["thread_id"]) is None


@pytest.mark.parametrize("texto", ["No la quiero en 50cm", "ya no quiero 20 bonches", "mejor no, cancela los 500 tallos"])
def test_negaciones_no_son_pedido(texto):
    from src.guardrails import detectar_intencion
    assert detectar_intencion(texto) is None


def test_limpiar_largos_quita_largo_inventado():
    from langchain_core.messages import HumanMessage
    from src.graph import _limpiar_largos
    st = {"messages": [HumanMessage(content="Hola tienes Freedom? 40 bonches")]}
    assert _limpiar_largos({"producto": "Freedom 50cm"}, st) == {"producto": "Freedom"}
    st = {"messages": [HumanMessage(content="Freedom de 50cm")]}
    assert _limpiar_largos({"producto": "Freedom 50cm"}, st) == {"producto": "Freedom 50cm"}
