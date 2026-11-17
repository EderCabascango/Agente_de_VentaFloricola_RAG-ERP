"""Grafo del agente de ventas de la florícola (LangGraph).

guardrail_entrada -> agente <-> herramientas -> validador_deterministico -> aprobacion_humana (interrupt)
                  -> escribir_erp -> respuesta_final
agente (sin tool calls) -> grounding_check -> respuesta_final  (o vuelve al agente 1 vez)
"""
from __future__ import annotations

import json
import operator
import re
import time
from datetime import datetime, timedelta
from typing import Annotated, Any, Callable, Optional, TypedDict

from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage, RemoveMessage,
                                     SystemMessage, ToolMessage)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel

from . import config as cfg
from .erp_client import (ERPClient, despachos_pendientes, ErrorEscrituraERP, registrar_auditoria, registrar_pedido_aprobado,
                         registrar_pedido_pendiente)
from .grounding import verificar_citas, verificar_grounding
from .guardrails import detectar_intencion, es_confirmacion, revisar_entrada
from .tools import TOOLS, TOOLS_BY_NAME
from .validators import validar_borrador
from .whatsapp import formatear_para_whatsapp, resumen_pedido_pendiente

SYSTEM_PROMPT = """Asistente de ventas por WhatsApp de {empresa}, finca de rosas de Ecuador. Vendes SOLO rosas: {n} variedades, en largos de 40 a 80 cm. No hay claveles, gypsophila, hypericum, limonium, flores de verano ni otras flores: si preguntan por una, di con amabilidad que solo trabajan rosas y ofrece rosas. Se vende por tallo, en bonches de 25 tallos; una caja completa son 400 tallos. Reglas:
1. Precios, cantidades y totales salen SIEMPRE de las herramientas, nunca de tu memoria ni de tus cuentas: toda cifra que escribas debe aparecer tal cual en un resultado de herramienta. Para cotizar usa calcular_cotizacion y muestra siempre el precio por tallo, la cantidad (tallos y bonches) y el total neto. No muestres operaciones.
2. Si no hay dato (p. ej. precios futuros o de temporada), responde solo: "No tengo esa información, la derivo a un asesor."
3. Políticas, envíos, reclamos y cuidado de la flor: buscar_politicas. Cita solo archivos que devolvió, ej. [tiempos_envio.md].
4. Solo si el cliente pide comprar o hacer un pedido, llama de inmediato a preparar_borrador_pedido con exactamente lo que pidió: cada línea con su cantidad y su unidad tal como la dijo (tallos, bonches o cajas; no conviertas, el sistema convierte), descuento y rosa por nombre o SKU. No revises stock, bonches, crédito ni descuento: lo hace el validador. Nunca registras en el ERP. Una consulta de precio o descuento NO es un pedido: usa calcular_cotizacion (aplica la política y el tope del cliente).
5. Si el validador rechaza: explica sus motivos y ofrece sus alternativas con sus cifras exactas, sin calcular otras; pide confirmación.
6. Si preguntan qué variedades hay, usa ver_catalogo: di cuántas variedades de rosa tienes ({n}) y su rango de precios, y pregunta cuál busca o qué largo; no las listes todas. Si buscan una por nombre o inicial, usa ver_catalogo con buscar. Si una rosa existe en varios largos, pregunta cuál antes de cotizar.
7. Termina con una pregunta corta que avance la venta (variedad, largo o cantidad).
8. Nunca asumas el largo (cm): si el cliente no lo dijo, pregúntale cuál prefiere entre los disponibles. Llama en paralelo las herramientas independientes. Español, tono cordial de WhatsApp, máximo 90 palabras, sin tablas ni títulos; *negrita* con un asterisco, solo en cifras clave (nunca un mensaje entero). Precios en USD por tallo. 9. Si preguntan qué modelo, tecnología o empresa de IA usas, responde que eres el asistente virtual de ventas y no das detalles técnicos; nunca afirmes un proveedor."""

TOOL_FORZADA = {"pedido": "preparar_borrador_pedido", "cotizacion": "calcular_cotizacion"}

FRASE_ESCALAR = re.compile(r"deriv\w*\b.{0,30}\basesor", re.IGNORECASE | re.DOTALL)
USUARIO_SISTEMA = "agente-ia"


# ================================ estado =================================

class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    cliente_canal: Optional[dict]   # cliente identificado por su número de WhatsApp (lo pone el canal, no el LLM)
    oferta: Optional[dict]          # última propuesta rechazada por el validador (el cliente puede aceptarla: "sí, dale")
    acepta_oferta: bool
    turno: int
    iteraciones: int
    borrador: Optional[dict]
    borrador_call_id: Optional[str]
    borrador_nuevo: bool
    validacion: Optional[dict]
    intentos_validacion: int
    reintentos_grounding: int
    decision: Optional[dict]
    pedido_creado: Optional[dict]
    pedido_pendiente: Optional[dict]   # pedido dejado PENDIENTE en el ERP (modo "erp")
    wa_id: Optional[str]               # número de WhatsApp que escribe (lo pone el canal)
    escalar: bool
    motivo_escalar: str
    bloqueado: bool
    intencion: Optional[str]
    solo_responder: bool
    grounding: Optional[dict]
    siguiente: str
    traza: Annotated[list[dict], operator.add]
    respuesta_final: Optional[dict]


class RespuestaFinal(BaseModel):
    respuesta: str
    fuentes: list[str]
    datos_usados: list[str]
    nivel_confianza: str          # alta | media | baja
    escalar_a_asesor: bool = False
    pedido_id: Optional[str] = None
    respuesta_whatsapp: str = ""      # el mismo texto, listo para enviarlo por la API de WhatsApp


# ============================ utilidades =================================

class LLMNoDisponible(RuntimeError):
    pass


# Hooks de reintento por conversación (la UI registra uno para mostrar "reintentando...")
RETRY_HOOKS: dict[str, Callable[[int, float, str], None]] = {}

_REINTENTABLE = ("tool choice is required", "429", "rate limit", "rate_limit", "resource_exhausted", "resourceexhausted", "quota",
                 "timeout", "timed out", "503", "unavailable", "overloaded", "502", "deadline")


def _es_reintentable(e: Exception) -> bool:
    msg = f"{type(e).__name__} {e}".lower()
    if "per day" in msg or "perday" in msg or "requests per day" in msg:
        return False  # cuota diaria agotada: reintentar no sirve, mejor pasar al respaldo
    return any(k in msg for k in _REINTENTABLE)


_ESPERA_RE = re.compile(r"(?:retry|try again)\D{0,20}?(?:(\d+)m)?(\d+(?:\.\d+)?)(ms|s)\b", re.IGNORECASE)


def _espera_sugerida(e: Exception) -> Optional[float]:
    """Lee la espera que pide la API: Gemini ('retryDelay': '37s') o Groq ('try again in 1m2.5s')."""
    m = _ESPERA_RE.search(str(e))
    if not m:
        return None
    seg = float(m.group(2)) / (1000 if m.group(3).lower() == "ms" else 1) + 60 * int(m.group(1) or 0)
    return min(seg + 0.5, 60.0)


def invocar_con_reintentos(llm, mensajes, config, thread_id: str = "", esperas=(2, 4, 8, 16),
                           max_espera: float = 60.0):
    """Backoff exponencial ante 429 / timeout; avisa a la UI en cada reintento.

    max_espera: si la API pide esperar más que esto, se desiste (útil cuando hay un respaldo listo).
    """
    for intento in range(len(esperas) + 1):
        try:
            return llm.invoke(mensajes, config=config)
        except Exception as e:  # noqa: BLE001
            espera = _espera_sugerida(e) or (esperas[intento] if intento < len(esperas) else 0)
            if not _es_reintentable(e) or intento == len(esperas) or espera > max_espera:
                raise LLMNoDisponible(f"El proveedor de LLM no respondió: {type(e).__name__}: {str(e)[:300]}") from e
            hook = RETRY_HOOKS.get(thread_id)
            if hook:
                hook(intento + 1, espera, f"{type(e).__name__}: {str(e)[:120]}")
            time.sleep(espera)


_LARGO_RE = re.compile(r"\s*\b(\d{2,3})\s*cm\b", re.I)


def _limpiar_largos(args, state):
    """Quita un largo (p. ej. '50cm') del nombre de flor si el cliente nunca lo dijo: así la herramienta
    devuelve las medidas disponibles en vez de que el modelo escoja una por su cuenta."""
    dichos = {m for h in state["messages"] if isinstance(h, HumanMessage) and not es_interno(h)
              for m in re.findall(r"\b(\d{2,3})\s*(?:cm|centimetros|centímetros)?\b", texto_de(h), re.I)}

    def limpia(x):
        if isinstance(x, str):
            return _LARGO_RE.sub(lambda m: m.group(0) if m.group(1) in dichos else "", x).strip()
        if isinstance(x, dict):
            return {k: (limpia(v) if k in ("producto", "solicitado", "texto", "buscar") else limpia(v) if isinstance(v, (dict, list)) else v)
                    for k, v in x.items()}
        if isinstance(x, list):
            return [limpia(v) for v in x]
        return x
    return limpia(args)


def texto_de(msg: BaseMessage) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    partes = []
    for p in c or []:
        if isinstance(p, str):
            partes.append(p)
        elif isinstance(p, dict) and p.get("type") == "text":
            partes.append(p.get("text", ""))
    return "".join(partes)


def es_interno(msg: BaseMessage) -> bool:
    return bool((msg.additional_kwargs or {}).get("interno"))


def _paso(turno: int, nodo: str, detalle: str, ok: Optional[bool] = None, **extra) -> dict:
    return {"turno": turno, "hora": datetime.now().strftime("%H:%M:%S"), "nodo": nodo,
            "detalle": detalle, "ok": ok, **extra}


def _indices_usuario(mensajes: list[BaseMessage]) -> list[int]:
    return [i for i, m in enumerate(mensajes) if isinstance(m, HumanMessage) and not es_interno(m)]


def mensajes_del_turno(mensajes: list[BaseMessage]) -> list[BaseMessage]:
    idx = _indices_usuario(mensajes)
    return mensajes[idx[-1]:] if idx else mensajes


def recortar_historial(mensajes: list[BaseMessage], turnos_previos: int = 2) -> list[BaseMessage]:
    """Turno actual completo + los N turnos anteriores condensados (pregunta y respuesta final, sin
    resultados de tools). Ahorra tokens: el plan gratuito de Groq permite ~8K tokens por minuto."""
    idx = _indices_usuario(mensajes)
    if not idx:
        return mensajes
    actual = mensajes[idx[-1]:]
    previos = []
    inicios, fines = idx[:-1][-turnos_previos:], idx[1:][-turnos_previos:]
    for a, b in zip(inicios, fines):
        turno = mensajes[a:b]
        final = next((m for m in reversed(turno) if isinstance(m, AIMessage) and not m.tool_calls
                      and not es_interno(m) and texto_de(m).strip()), None)
        previos.append(HumanMessage(content=texto_de(turno[0])))
        if final is not None:
            previos.append(AIMessage(content=texto_de(final)))
    return previos + actual


def _resumir(resultado: Any, largo: int = 160) -> str:
    s = json.dumps(resultado, ensure_ascii=False, default=str)
    return s if len(s) <= largo else s[:largo] + "…"


def _texto_validacion(v: dict) -> str:
    if v["aprobado"]:
        lineas = "; ".join(
            f"{l['sku']} x{l['cantidad']} tallos desde finca {l['bodega']} a ${l['precio_unitario']:.2f} "
            f"con {l['descuento_pct']:g}% = ${l['subtotal']:.2f}" for l in v["lineas"])
        txt = (f"VALIDACIÓN AUTOMÁTICA: APROBADA. {lineas}. Total ${v['total']:.2f} (FOB Quito). "
               "Pendiente de aprobación del supervisor.")
    else:
        txt = ("VALIDACIÓN AUTOMÁTICA: RECHAZADA. Motivos: " + " | ".join(v["motivos"])
               + (". Alternativas: " + " | ".join(v["alternativas"]) if v["alternativas"] else "")
               + ". Explica al cliente y propone una alternativa.")
    if v.get("correcciones"):
        txt += " Correcciones: " + " | ".join(v["correcciones"])
    return txt


def _contexto_cliente(cliente: Optional[dict]) -> str:
    """El canal identifica al cliente por su número: el LLM no tiene que preguntarlo ni adivinarlo."""
    if not cliente:
        return ""
    return (f"\nCliente identificado por su número de WhatsApp: {cliente['razon_social']}, ID {cliente['id']}. "
            "Usa ese ID como cliente en las herramientas.")


def _contexto_despachos(wa_id: Optional[str]) -> tuple[str, bool]:
    """Pedidos aceptados de este cliente que aún no salen. Devuelve (texto para el prompt, si hay datos por pedir)."""
    pend = despachos_pendientes(wa_id or "")
    if not pend:
        return "", False
    ahora_ec = datetime.utcnow() - timedelta(hours=5)
    hoy, manana = ahora_ec.date().isoformat(), (ahora_ec + timedelta(days=1)).date().isoformat()
    lineas, pedir = [], False
    for d in pend:
        base = f"- {d['pedido_id']} (total ${d['total']:.2f}"
        base += f", destino {d['destino']})" if d.get("destino") else ")"
        if d["estado"] == "FALTAN_DATOS":
            pedir = True
            lineas.append(f"{base}: aceptado. FALTAN los datos de despacho.")
        elif d["estado"] == "DEVUELTO":
            pedir = True
            lineas.append(f"{base}: un asesor devolvió los datos. Debe corregir: {d.get('motivo_devolucion')}")
        elif d["estado"] == "POR_VALIDAR":
            lineas.append(f"{base}: ya tenemos los datos; un asesor los está validando.")
        else:
            lineas.append(f"{base}: datos validados, el despacho sale pronto.")
    texto = (f"\nFecha de hoy: {hoy}; mañana: {manana}."
             "\nPedidos aceptados de este cliente que todavía no salen (el primero es el más reciente):\n" + "\n".join(lineas))
    if pedir:
        texto += (
            f"\nPara los pedidos con datos por pedir: pídele en UN solo mensaje nombre y teléfono de quien recibe, dirección "
            f"y ciudad de entrega, fecha de entrega (hoy es {hoy}; conviértela a AAAA-MM-DD) y método de pago (transferencia, "
            "crédito, contra entrega o efectivo). Revisa TODOS los mensajes anteriores del cliente: lo que ya dijo (por ejemplo la ciudad dentro de la dirección) no se vuelve a pedir; pide solo lo que falte y nunca llames la herramienta con campos vacíos. "
            "Si hay varios pedidos por completar y el cliente no aclara cuál, usa el más reciente y pregúntaselo una vez. Cuando tengas TODOS, llama guardar_datos_despacho con el pedido_id exacto. "
            "No inventes ni completes datos: si falta alguno, pregúntalo. Si el sistema rechaza un dato, explícale el motivo "
            "y pídelo de nuevo. Tras guardar, dile que un asesor validará los datos y que le avisaremos por este chat.")
    return texto, pedir


# ============================ construcción ===============================

def _aplanar_para_responder(mensajes: list[BaseMessage]) -> list[BaseMessage]:
    """Para las llamadas de 'solo responder': los resultados de tools viajan como texto plano.
    Sin estructura de tool calls en el contexto, el modelo no intenta llamar otra herramienta."""
    out = []
    for m in mensajes:
        if isinstance(m, AIMessage) and m.tool_calls:
            continue
        if isinstance(m, ToolMessage):
            out.append(HumanMessage(content=f"[Datos del sistema · {m.name}] {texto_de(m)}"))
        else:
            out.append(m)
    return out


def _sanear_para_respaldo(mensajes: list[BaseMessage]) -> list[BaseMessage]:
    """Otro proveedor no entiende las partes propias de Gemini (firmas de 'thinking'): dejamos solo texto."""
    out = []
    for m in mensajes:
        if isinstance(m, AIMessage):
            out.append(AIMessage(content=texto_de(m), tool_calls=m.tool_calls, id=m.id))
        else:
            out.append(m)
    return out


def build_graph(llm=None, checkpointer=None, respaldo=None, aprobacion=None):
    """llm: modelo principal. respaldo: modelo de otro proveedor que toma la llamada si el principal falla.

    aprobacion: "erp" (el pedido queda PENDIENTE en el ERP y se acepta en la plataforma) o "interrupt"
    (el grafo se pausa y se aprueba con botones). Por defecto, lo que diga APROBACION en la configuración.
    """
    aprobacion = (aprobacion or cfg.APROBACION).lower()
    llm = llm if llm is not None else cfg.get_llm()
    llm_tools = llm.bind_tools(TOOLS)
    # El número de variedades sale del ERP, no está escrito a mano: si cambia el catálogo, cambia el prompt.
    try:
        n_variedades = str(ERPClient().catalogo_resumen()["total_variedades"])
    except Exception:  # noqa: BLE001 — sin ERP disponible el agente igual arranca
        n_variedades = "varias"
    prompt_base = SYSTEM_PROMPT.replace("{empresa}", cfg.EMPRESA).replace("{n}", n_variedades)
    respaldo_tools = respaldo.bind_tools(TOOLS) if respaldo is not None else None
    # Variantes que OBLIGAN a usar una tool: las elige una regla, no el LLM. Se expone SOLO esa tool
    # (Groq no restringe la generación con tool_choice si hay varias tools disponibles).
    forzados = {n: llm.bind_tools([TOOLS_BY_NAME[n]], tool_choice=n) for n in TOOL_FORZADA.values()}
    forzados_resp = ({n: respaldo.bind_tools([TOOLS_BY_NAME[n]], tool_choice=n) for n in TOOL_FORZADA.values()}
                     if respaldo is not None else {})

    def invocar(mensajes, config, thread_id, sin_tools=False, forzar=None):
        principal = llm if sin_tools else forzados.get(forzar, llm_tools)
        if respaldo is None:
            return invocar_con_reintentos(principal, mensajes, config, thread_id)
        try:
            return invocar_con_reintentos(principal, mensajes, config, thread_id, esperas=(2,), max_espera=cfg.FALLBACK_MAX_ESPERA)
        except LLMNoDisponible as e:
            hook = RETRY_HOOKS.get(thread_id)
            if hook:
                hook(0, 0, f"Proveedor principal no disponible → usando respaldo. ({str(e)[:90]})")
            return invocar_con_reintentos(respaldo if sin_tools else forzados_resp.get(forzar, respaldo_tools),
                                          _sanear_para_respaldo(mensajes), config, thread_id)

    # ---------------------------------------------------------------- nodos
    def guardrail_entrada(state: AgentState) -> dict:
        turno = state.get("turno", 0) + 1
        ultimo = state["messages"][-1]
        texto = texto_de(ultimo)
        r = revisar_entrada(texto)
        reset = {"turno": turno, "iteraciones": 0, "borrador": None, "borrador_call_id": None,
                 "borrador_nuevo": False, "validacion": None, "intentos_validacion": 0,
                 "reintentos_grounding": 0, "decision": None, "pedido_creado": None, "pedido_pendiente": None, "escalar": False,
                 "motivo_escalar": "", "bloqueado": False, "grounding": None, "respuesta_final": None,
                 "intencion": None, "solo_responder": False, "acepta_oferta": False}
        if r.permitido:
            intencion = detectar_intencion(texto)
            # El cliente responde a una propuesta reciente ("sí, me parece", "la de 75"): lo decide una regla.
            oferta = state.get("oferta")
            vigente = bool(oferta) and turno - oferta.get("turno", 0) <= 2
            acepta = vigente and intencion != "cotizacion" and (es_confirmacion(texto) or bool(re.search(r"\d", texto)))
            if acepta and (re.search(r"\d", texto) or oferta.get("opciones", 1) <= 1):
                intencion = "pedido"      # una sola opción o eligió con un número: arma el borrador de inmediato
            det = "Entrada permitida" + (f" · intención detectada: {intencion}" if intencion else "")
            if acepta:
                det += " · el cliente responde a la propuesta anterior"
            return {**reset, "intencion": intencion, "acepta_oferta": acepta,
                    "traza": [_paso(turno, "guardrail_entrada", det, True)]}
        registrar_auditoria("cliente-whatsapp", "GUARDRAIL_BLOQUEO",
                            {"mensaje": texto[:500], "categoria": r.categoria, "patron": r.patron},
                            f"Bloqueado: {r.categoria}")
        return {**reset, "bloqueado": True,
                "messages": [AIMessage(content=r.respuesta)],
                "traza": [_paso(turno, "guardrail_entrada", f"BLOQUEADO ({r.categoria}). Registrado en auditoría.",
                                False, patron=r.patron)]}

    def agente(state: AgentState, config) -> dict:
        turno = state["turno"]
        it = state.get("iteraciones", 0)
        if it > cfg.MAX_ITERACIONES:
            msg = "No pude completar su solicitud con los datos disponibles; la derivo a un asesor."
            return {"messages": [AIMessage(content=msg)], "escalar": True,
                    "motivo_escalar": f"Límite de {cfg.MAX_ITERACIONES} iteraciones", "siguiente": "final",
                    "traza": [_paso(turno, "agente", f"Límite de {cfg.MAX_ITERACIONES} iteraciones alcanzado → escala", False)]}
        prompt = prompt_base + _contexto_cliente(state.get("cliente_canal"))
        ctx_despacho, pedir_despacho = _contexto_despachos(state.get("wa_id"))
        prompt += ctx_despacho
        if state.get("acepta_oferta") and state.get("oferta"):
            of = state["oferta"]
            prompt += (f"\nEl cliente responde a tu propuesta anterior. Propuesta del sistema: {of['texto']} "
                       f"Borrador anterior: {json.dumps(of['borrador'], ensure_ascii=False)}. Si aceptó una opción clara, "
                       "llama a preparar_borrador_pedido con ese borrador ajustado a la opción (mismo cliente y flor). "
                       "Si hay varias opciones y no eligió, pregúntale cuál prefiere, sin llamar herramientas.")
        mensajes = [SystemMessage(content=prompt)] + [
            m for m in recortar_historial(state["messages"])]
        thread_id = (config or {}).get("configurable", {}).get("thread_id", "")
        # En la última iteración permitida el agente ya no puede llamar tools: debe responder con lo que tiene.
        # Sin tools en la última iteración permitida, o tras un rechazo del validador: el agente solo
        # puede explicar el veredicto (que ya trae las alternativas calculadas), no investigar ni calcular.
        sin_tools = it == cfg.MAX_ITERACIONES or bool(state.get("solo_responder"))
        # Primera llamada del turno con intención clara: la tool la decide una regla, no el modelo.
        forzar = TOOL_FORZADA.get(state.get("intencion")) if it == 0 and not pedir_despacho else None
        if forzar:
            mensajes[0] = SystemMessage(content=prompt + f"\nAhora llama a {forzar} con lo que pidió el cliente.")
        if sin_tools:
            mensajes = [SystemMessage(content=prompt + "\nResponde ahora al cliente solo con los datos del "
                                      "sistema que tienes; no hay herramientas disponibles.")] + \
                       _aplanar_para_responder(mensajes[1:])
        t0 = time.time()
        try:
            resp = invocar(mensajes, config, thread_id, sin_tools=sin_tools, forzar=forzar)
        except LLMNoDisponible as e:
            # El proveedor rechazó la herramienta obligatoria (el modelo prefirió responder): se reintenta libre.
            if forzar and "tool choice is required" in str(e).lower():
                libres = [SystemMessage(content=prompt)] + mensajes[1:]
                resp = invocar(libres, config, thread_id, sin_tools=False, forzar=None)
            else:
                raise
        seg = round(time.time() - t0, 2)
        modelo = (resp.response_metadata or {}).get("model_name") or (resp.response_metadata or {}).get("model", "")
        if resp.tool_calls:
            det = "Llama a: " + ", ".join(f"{tc['name']}({_resumir(tc['args'], 90)})" for tc in resp.tool_calls)
            sig = "herramientas"
        else:
            det = "Redacta respuesta al cliente"
            sig = "grounding"
        return {"messages": [resp], "iteraciones": it + 1, "siguiente": sig, "solo_responder": False,
                "traza": [_paso(turno, "agente", det, None, iteracion=it + 1, segundos=seg, modelo=modelo)]}

    def herramientas(state: AgentState) -> dict:
        turno = state["turno"]
        ultimo = state["messages"][-1]
        salida, pasos = [], []
        upd: dict = {"borrador_nuevo": False}
        for tc in ultimo.tool_calls:
            nombre, args = tc["name"], tc.get("args", {}) or {}
            herramienta = TOOLS_BY_NAME.get(nombre)
            try:
                if herramienta is None:
                    raise ValueError(f"Herramienta desconocida: {nombre}")
                args = _limpiar_largos(args, state)
                if nombre == "guardar_datos_despacho":
                    args = {**args, "wa_id": state.get("wa_id") or ""}  # siempre el número que escribe, no el que diga el modelo
                res = herramienta.invoke(args)
                ok = not (isinstance(res, dict) and "error" in res)
            except Exception as e:  # noqa: BLE001
                res, ok = {"error": f"{type(e).__name__}: {e}"}, False
            if nombre == "preparar_borrador_pedido" and ok:
                upd.update({"borrador": json.loads(json.dumps(args, default=lambda o: o.model_dump())),
                            "borrador_call_id": tc["id"], "borrador_nuevo": True})
            salida.append(ToolMessage(content=json.dumps(res, ensure_ascii=False, default=str),
                                      tool_call_id=tc["id"], name=nombre, id=f"tool-{tc['id']}"))
            pasos.append(_paso(turno, f"tool:{nombre}", _resumir(res, 220), ok, args=args))
        return {**upd, "messages": salida, "traza": pasos}

    def validador_deterministico(state: AgentState) -> dict:
        turno = state["turno"]
        intentos = state.get("intentos_validacion", 0) + 1
        v = validar_borrador(state["borrador"]).model_dump()
        for c in v["correcciones"]:
            registrar_auditoria(USUARIO_SISTEMA, "PRECIO_CORREGIDO", {"detalle": c}, "Precio ERP aplicado")
        registrar_auditoria(USUARIO_SISTEMA, "VALIDACION_OK" if v["aprobado"] else "VALIDACION_FALLIDA",
                            {"borrador": state["borrador"], "motivos": v["motivos"]},
                            "Pendiente de aprobación humana" if v["aprobado"] else "; ".join(v["motivos"])[:300])
        # El resultado reemplaza al acuse de la tool: así el LLM "ve" el veredicto del validador.
        call_id = state["borrador_call_id"]
        tool_msg = ToolMessage(content=_texto_validacion(v), tool_call_id=call_id,
                               name="preparar_borrador_pedido", id=f"tool-{call_id}")
        pasos = [_paso(turno, "validador_deterministico",
                       ("APROBADO: " if v["aprobado"] else "RECHAZADO: ")
                       + ("; ".join(v["motivos"]) if v["motivos"] else f"total ${v['total']:.2f}"),
                       v["aprobado"], checks=v["checks"])]
        upd = {"validacion": v, "intentos_validacion": intentos, "borrador_nuevo": False,
               "messages": [tool_msg], "traza": pasos}
        # Si se rechaza, queda guardada la propuesta: el cliente puede aceptarla en su próximo mensaje.
        upd["oferta"] = None if v["aprobado"] else {
            "turno": turno, "texto": _texto_validacion(v), "borrador": state["borrador"],
            "opciones": max(len(v["alternativas"]), 1)}
        if v["aprobado"]:
            upd["siguiente"] = "aprobacion"
        elif intentos >= cfg.MAX_INTENTOS_VALIDACION:
            upd.update({"siguiente": "final", "escalar": True,
                        "motivo_escalar": "El borrador falló la validación varias veces",
                        "messages": [tool_msg, AIMessage(content=(
                            "No logré armar un pedido que cumpla las políticas; la derivo a un asesor."))]})
        else:
            upd["siguiente"] = "agente"
            upd["solo_responder"] = True
        return upd

    def aprobacion_humana(state: AgentState) -> dict:
        v = state["validacion"]
        decision = interrupt({"tipo": "aprobacion_pedido", "validacion": v})
        accion = (decision or {}).get("accion", "rechazar")
        usuario = (decision or {}).get("usuario", "supervisor.demo")
        turno = state["turno"]
        if accion == "editar":
            nuevas = {k.upper(): v for k, v in (decision.get("cantidades") or {}).items()}
            b = json.loads(json.dumps(state["borrador"]))
            # se fija la finca y el descuento ya validados para no cambiar nada más que la cantidad
            bodegas = {l["sku"]: l["bodega"] for l in v["lineas"]}
            resuelto = {str(l.get("solicitado") or l["sku"]).upper(): l["sku"] for l in v["lineas"]}
            for item in b["items"]:
                pedido = str(item.pop("producto", None) or item.get("sku"))
                sku = resuelto.get(pedido.upper(), pedido.upper())
                item["sku"] = sku  # el borrador queda con el SKU ya resuelto
                if sku in nuevas:
                    item["cantidad"] = nuevas[sku]
                    item["unidad"] = "tallos"   # el supervisor edita siempre en tallos
                item.setdefault("bodega", bodegas.get(sku))
            registrar_auditoria(usuario, "CANTIDAD_EDITADA", {"cantidades": nuevas}, "Se vuelve a validar")
            return {"borrador": b, "decision": decision, "siguiente": "validador",
                    "traza": [_paso(turno, "aprobacion_humana", f"{usuario} editó cantidades {nuevas} → revalidar", None)]}
        if accion == "aprobar":
            return {"decision": decision, "siguiente": "escribir",
                    "traza": [_paso(turno, "aprobacion_humana", f"APROBADO por {usuario}", True)]}
        registrar_auditoria(usuario, "PEDIDO_RECHAZADO", {"validacion_total": v.get("total"),
                                                          "motivo": decision.get("motivo", "")}, "No se escribe en ERP")
        msg = ("El supervisor no aprobó este pedido, por lo que no se registró en el ERP. "
               "Un asesor comercial le escribirá por este chat para revisarlo.")
        return {"decision": decision, "siguiente": "final", "escalar": True,
                "motivo_escalar": "Pedido rechazado por supervisor",
                "messages": [AIMessage(content=msg)],
                "traza": [_paso(turno, "aprobacion_humana", f"RECHAZADO por {usuario}", False)]}

    def registrar_pendiente(state: AgentState) -> dict:
        """Modo "erp": el pedido ya validado queda PENDIENTE en el ERP y el cliente ve precio, cantidad y total.

        No se escribe stock: eso ocurre cuando una persona lo acepta en la plataforma.
        """
        v = state["validacion"]
        turno = state["turno"]
        try:
            r = registrar_pedido_pendiente(v, wa_id=state.get("wa_id"), destino=v.get("destino"))
        except Exception as e:  # noqa: BLE001 — si el ERP falla, el cliente no se queda sin respuesta
            registrar_auditoria(USUARIO_SISTEMA, "PENDIENTE_FALLIDO", {"error": str(e)[:300]}, "Derivado a un asesor")
            msg = "No pude dejar su pedido registrado por una falla técnica; un asesor lo revisará y le escribirá por aquí."
            return {"messages": [AIMessage(content=msg)], "escalar": True, "motivo_escalar": "No se pudo guardar el pedido pendiente",
                    "traza": [_paso(turno, "registrar_pendiente", f"ERROR: {str(e)[:160]}", False)]}
        texto = resumen_pedido_pendiente(v, r["pedido_id"])
        return {"pedido_pendiente": r, "messages": [AIMessage(content=texto)],
                "traza": [_paso(turno, "registrar_pendiente",
                                f"{r['pedido_id']} guardado PENDIENTE en el ERP por ${r['total']:,.2f}; se acepta en la plataforma", True)]}

    def escribir_erp(state: AgentState) -> dict:
        turno = state["turno"]
        decision = state.get("decision") or {}
        if decision.get("accion") != "aprobar":  # defensa en profundidad
            raise ErrorEscrituraERP("escribir_erp llamado sin aprobación humana")
        # Revalidar justo antes de escribir (el stock pudo cambiar mientras se esperaba la aprobación)
        v = validar_borrador(state["borrador"]).model_dump()
        if not v["aprobado"]:
            registrar_auditoria(USUARIO_SISTEMA, "ESCRITURA_CANCELADA", {"motivos": v["motivos"]}, "Revalidación fallida")
            return {"messages": [AIMessage(content="Las condiciones cambiaron antes de registrar el pedido ("
                                           + "; ".join(v["motivos"]) + "). Un asesor le contactará.")],
                    "escalar": True, "motivo_escalar": "Revalidación fallida",
                    "traza": [_paso(turno, "escribir_erp", "Cancelado: revalidación fallida", False)]}
        pedido = registrar_pedido_aprobado(v, aprobado_por=decision.get("usuario", "supervisor.demo"),
                                           creado_por=USUARIO_SISTEMA)
        bodegas = ", ".join(sorted({l["bodega"] for l in v["lineas"]}))
        msg = (f"¡Listo! Su pedido {pedido['pedido_id']} quedó registrado por ${pedido['total']:,.2f} (FOB Quito), "
               f"aprobado por {pedido['aprobado_por']}. La flor quedó reservada en la finca {bodegas}.")
        return {"pedido_creado": pedido, "messages": [AIMessage(content=msg)],
                "traza": [_paso(turno, "escribir_erp", f"Pedido {pedido['pedido_id']} insertado, stock reservado, auditado", True)]}

    def grounding_check(state: AgentState) -> dict:
        turno = state["turno"]
        ultimo = state["messages"][-1]
        texto = texto_de(ultimo)
        fuentes = [prompt_base, _contexto_despachos(state.get("wa_id"))[0]] + [texto_de(m) for m in state["messages"]
                   if isinstance(m, ToolMessage) or (isinstance(m, HumanMessage) and not es_interno(m))]
        g = verificar_grounding(texto, fuentes)
        # Citas: solo se pueden citar archivos que buscar_politicas devolvió en esta conversación
        archivos = set()
        for m in state["messages"]:
            if isinstance(m, ToolMessage) and m.name == "buscar_politicas":
                try:
                    archivos |= {f["fuente"].split(" ")[0] for f in json.loads(m.content).get("fragmentos", [])}
                except (json.JSONDecodeError, AttributeError, KeyError):
                    pass
        citas_falsas = verificar_citas(texto, archivos)
        if citas_falsas:
            g = {**g, "ok": False, "sin_respaldo": g["sin_respaldo"] + [f"cita [{c}]" for c in citas_falsas]}
        if g["ok"]:
            return {"grounding": g, "siguiente": "final",
                    "traza": [_paso(turno, "grounding_check",
                                    f"OK: {len(g['numeros_revisados'])} cifras verificadas contra datos de tools", True)]}
        intentos = state.get("reintentos_grounding", 0)
        registrar_auditoria(USUARIO_SISTEMA, "GROUNDING_FALLIDO", {"respuesta": texto[:500], "sin_respaldo": g["sin_respaldo"]},
                            "Se regenera" if intentos == 0 else "Se escala")
        if intentos == 0:
            nota = HumanMessage(
                content=(f"[VERIFICADOR AUTOMÁTICO] Tu respuesta tenía cifras sin respaldo en las herramientas: "
                         f"{', '.join(g['sin_respaldo'])}. Reescríbela usando solo cifras de las herramientas "
                         "tal como aparecen, sin calcular otras, o di que no tienes la información."),
                additional_kwargs={"interno": True})
            # La reescritura va sin tools: solo puede reformular con los datos que ya tiene.
            return {"grounding": g, "reintentos_grounding": 1, "siguiente": "agente", "solo_responder": True,
                    "messages": [RemoveMessage(id=ultimo.id), nota],
                    "traza": [_paso(turno, "grounding_check", f"FALLÓ: sin respaldo {g['sin_respaldo']} → regenerar", False,
                                    respuesta_descartada=texto)]}
        msg = "No puedo confirmar algunas cifras con los datos del sistema; derivo su consulta a un asesor."
        return {"grounding": g, "siguiente": "final", "escalar": True, "motivo_escalar": "Grounding fallido",
                "messages": [RemoveMessage(id=ultimo.id), AIMessage(content=msg)],
                "traza": [_paso(turno, "grounding_check", f"FALLÓ de nuevo: {g['sin_respaldo']} → escala", False,
                                respuesta_descartada=texto)]}

    def respuesta_final(state: AgentState) -> dict:
        turno = state["turno"]
        turno_msgs = mensajes_del_turno(state["messages"])
        ultimo_ai = next((m for m in reversed(turno_msgs) if isinstance(m, AIMessage) and not m.tool_calls), None)
        texto = (texto_de(ultimo_ai) if ultimo_ai else "").replace("【", "[").replace("】", "]")
        # Citas a nombres de tools ("[calcular_cotizacion]") no son fuentes para el cliente: se quitan.
        texto = re.sub(r"\[(?:" + "|".join(TOOLS_BY_NAME) + r")[^\]]*\]", "", texto)
        fuentes, datos = [], []
        llamadas = {tc["id"]: tc for m in turno_msgs if isinstance(m, AIMessage) for tc in m.tool_calls}
        for m in turno_msgs:
            if not isinstance(m, ToolMessage):
                continue
            tc = llamadas.get(m.tool_call_id, {})
            nombre = tc.get("name", m.name)
            datos.append(f"{nombre}({_resumir(tc.get('args', {}), 80)})")
            if nombre == "buscar_politicas":
                try:
                    for f in json.loads(m.content).get("fragmentos", []):
                        if f.get("fuente") and f["fuente"] not in fuentes:
                            fuentes.append(f["fuente"])
                except (json.JSONDecodeError, AttributeError):
                    pass
            elif nombre in ("buscar_producto", "consultar_inventario", "calcular_cotizacion"):
                if "ERP · productos/inventario" not in fuentes:
                    fuentes.append("ERP · productos/inventario")
            elif nombre == "consultar_cliente":
                if "ERP · clientes" not in fuentes:
                    fuentes.append("ERP · clientes")
            elif nombre == "preparar_borrador_pedido":
                if "Validador determinista" not in fuentes:
                    fuentes.append("Validador determinista")
        escalar = state.get("escalar", False)
        motivo = state.get("motivo_escalar", "")
        if not escalar and FRASE_ESCALAR.search(texto):
            escalar, motivo = True, "El agente no tenía datos para responder"
        if escalar:
            registrar_auditoria(USUARIO_SISTEMA, "ESCALADO_A_ASESOR", {"respuesta": texto[:300]}, motivo)
        if state.get("bloqueado") or state.get("pedido_creado") or state.get("pedido_pendiente"):
            confianza = "alta"
        elif escalar:
            confianza = "baja"
        elif datos and (state.get("grounding") or {}).get("ok", True):
            confianza = "alta"
        else:
            confianza = "media"
        rf = RespuestaFinal(respuesta=texto, fuentes=fuentes, datos_usados=datos, nivel_confianza=confianza,
                            escalar_a_asesor=escalar, respuesta_whatsapp=formatear_para_whatsapp(texto),
                            pedido_id=(state.get("pedido_creado") or state.get("pedido_pendiente") or {}).get("pedido_id"))
        upd = {"respuesta_final": rf.model_dump(), "escalar": escalar, "motivo_escalar": motivo,
               "traza": [_paso(turno, "respuesta_final",
                               f"confianza={confianza}" + (f", ESCALADO ({motivo})" if escalar else ""), True)]}
        if ultimo_ai is not None and texto != texto_de(ultimo_ai):  # el chat muestra la versión limpia
            upd["messages"] = [AIMessage(content=texto, id=ultimo_ai.id)]
        return upd

    # --------------------------------------------------------------- aristas
    g = StateGraph(AgentState)
    g.add_node("guardrail_entrada", guardrail_entrada)
    g.add_node("agente", agente)
    g.add_node("herramientas", herramientas)
    g.add_node("validador_deterministico", validador_deterministico)
    g.add_node("aprobacion_humana", aprobacion_humana)
    g.add_node("registrar_pendiente", registrar_pendiente)
    g.add_node("escribir_erp", escribir_erp)
    g.add_node("grounding_check", grounding_check)
    g.add_node("respuesta_final", respuesta_final)

    g.add_edge(START, "guardrail_entrada")
    g.add_conditional_edges("guardrail_entrada", lambda s: "respuesta_final" if s.get("bloqueado") else "agente",
                            ["agente", "respuesta_final"])
    g.add_conditional_edges("agente", lambda s: {"herramientas": "herramientas", "grounding": "grounding_check"}
                            .get(s.get("siguiente"), "respuesta_final"),
                            ["herramientas", "grounding_check", "respuesta_final"])
    g.add_conditional_edges("herramientas",
                            lambda s: "validador_deterministico" if s.get("borrador_nuevo") else "agente",
                            ["validador_deterministico", "agente"])
    destino_aprobacion = "registrar_pendiente" if aprobacion == "erp" else "aprobacion_humana"
    g.add_conditional_edges("validador_deterministico",
                            lambda s: {"aprobacion": destino_aprobacion, "agente": "agente"}
                            .get(s.get("siguiente"), "respuesta_final"),
                            ["aprobacion_humana", "registrar_pendiente", "agente", "respuesta_final"])
    g.add_edge("registrar_pendiente", "respuesta_final")
    g.add_conditional_edges("aprobacion_humana",
                            lambda s: {"escribir": "escribir_erp", "validador": "validador_deterministico"}
                            .get(s.get("siguiente"), "respuesta_final"),
                            ["escribir_erp", "validador_deterministico", "respuesta_final"])
    g.add_edge("escribir_erp", "respuesta_final")
    g.add_conditional_edges("grounding_check",
                            lambda s: "agente" if s.get("siguiente") == "agente" else "respuesta_final",
                            ["agente", "respuesta_final"])
    g.add_edge("respuesta_final", END)

    return g.compile(checkpointer=checkpointer or MemorySaver())


# ===================== helpers para la UI / scripts ======================

def pendiente_de_aprobacion(graph, thread_id: str) -> Optional[dict]:
    """Devuelve el payload del interrupt si el grafo espera aprobación humana."""
    snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    for intr in getattr(snap, "interrupts", ()) or ():
        return intr.value
    for task in snap.tasks or []:
        for intr in getattr(task, "interrupts", []) or []:
            return intr.value
    return None


def traza_resumida(traza: list[dict], turno: int) -> list[dict]:
    """Los pasos del turno que vale la pena mostrar en la plataforma: herramientas, validador y registro del pedido."""
    nodos = ("guardrail_entrada", "validador_deterministico", "registrar_pendiente", "grounding_check")
    return [{"nodo": p["nodo"], "detalle": str(p.get("detalle", ""))[:160], "ok": p.get("ok")}
            for p in traza or [] if p.get("turno") == turno and (str(p.get("nodo", "")).startswith("tool:") or p.get("nodo") in nodos)]
