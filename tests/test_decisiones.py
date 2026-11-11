"""Decisiones de ingeniería que reemplazaron criterio del LLM por reglas deterministas."""
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mock_llm import ScriptedChatModel, llamar, responder, tool_call
from src.escenarios import ESCENARIOS
from src.graph import _aplanar_para_responder, build_graph, recortar_historial
from src.grounding import verificar_grounding
from src.guardrails import detectar_intencion
from src.validators import validar_borrador


@pytest.mark.parametrize("texto,esperado", [
    (ESCENARIOS[0]["mensaje"], "cotizacion"),
    (ESCENARIOS[1]["mensaje"], "pedido"),
    (ESCENARIOS[2]["mensaje"], "pedido"),
    (ESCENARIOS[3]["mensaje"], None),
    (ESCENARIOS[4]["mensaje"], None),
    ("¿Cuánto cuesta 1000 tallos de gypsophila?", "cotizacion"),
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
    assert "$2,280.00" in alts           # 4000 × 0.60 × 0.95 (solo Cayambe)
    assert "$4,140.00" in alts           # 7500 × 0.60 × 0.92 (combinando las 3 fincas)


def test_producto_por_nombre_se_resuelve_sin_llm():
    r = validar_borrador({"cliente": "C001", "items": [{"sku": "gypsophila blanca", "cantidad": 25}]})
    assert r.aprobado and r.lineas[0].sku == "GYP-001"
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
        responder("Hay 4000 tallos en Cayambe: con 5% el total es $2,280.00. ¿Confirma?"),
    ])
    g = build_graph(llm)
    conf = {"configurable": {"thread_id": uuid.uuid4().hex}}
    g.invoke({"messages": [HumanMessage(content=ESCENARIOS[2]["mensaje"])]}, conf)
    assert vistos == [False, False], "la respuesta tras el rechazo va con contexto aplanado (sin ToolMessages)"
    assert g.get_state(conf).values["grounding"]["ok"]
