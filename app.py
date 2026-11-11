"""UI de la demo: agente de ventas por WhatsApp de una florícola (Streamlit).

El chat imita WhatsApp: el cliente escribe desde su número y el agente responde con el formato
del canal. La conexión real con la API de WhatsApp se describe en el README.

Ejecutar:  streamlit run app.py
"""
from __future__ import annotations

import html
import json
import re
import time
import uuid
from datetime import datetime

import pandas as pd
import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from seed import reset_db
from src import config as cfg
from src.costs import CostTracker, costo_usd
from src.erp_client import ERPClient
from src.escenarios import CLIENTE_DEMO, ESCENARIOS
from src.graph import RETRY_HOOKS, LLMNoDisponible, build_graph, es_interno, pendiente_de_aprobacion, texto_de
from src.whatsapp import formatear_para_whatsapp, hilo_de

EMPRESA = "Rosaflor Andina"

st.set_page_config(page_title=f"{EMPRESA} · Agente de ventas", page_icon="🌹", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 1.2rem; padding-bottom: 1rem;}
.badge {display:inline-block; padding:2px 10px; border-radius:999px; font-size:0.72rem; font-weight:600; margin:4px 6px 0 0;}
.b-alta {background:#e6f4ea; color:#1e7b34;} .b-media {background:#fff4e0; color:#9a6200;}
.b-baja {background:#fdecea; color:#b3261e;} .b-info {background:#e8f0fe; color:#1a56c4;}

/* ---- chat estilo WhatsApp ---- */
.wa-header {display:flex; align-items:center; gap:12px; background:#075E54; color:#fff;
            padding:10px 14px; border-radius:12px 12px 0 0;}
.wa-avatar {width:40px; height:40px; border-radius:50%; background:#fff; display:flex;
            align-items:center; justify-content:center; font-size:22px; flex:none;}
.wa-name {font-weight:600; font-size:1rem; line-height:1.2;}
.wa-sub {font-size:0.78rem; opacity:0.85;}
.wa-row {display:flex; margin:4px 0;}
.wa-row.in {justify-content:flex-start;} .wa-row.out {justify-content:flex-end;}
.wa-bubble {max-width:82%; padding:6px 9px 4px 9px; border-radius:8px; font-size:0.93rem; line-height:1.38;
            color:#111b21; box-shadow:0 1px 0.5px rgba(11,20,26,.13); word-wrap:break-word;}
.wa-bubble.in {background:#ffffff; border-top-left-radius:0;}
.wa-bubble.out {background:#d9fdd3; border-top-right-radius:0;}
.wa-meta {display:block; text-align:right; font-size:0.68rem; color:#667781; margin-top:2px;}
.wa-tick {color:#53bdeb;}
.wa-fuente {display:block; font-size:0.72rem; color:#667781; margin-top:3px;}
.wa-dia {text-align:center; margin:6px 0 10px 0;}
.wa-dia span {background:#ffffff; color:#54656f; font-size:0.72rem; padding:4px 10px; border-radius:8px;
              box-shadow:0 1px 0.5px rgba(11,20,26,.13);}
.wa-cifrado {text-align:center; margin:0 auto 10px auto; max-width:85%;}
.wa-cifrado span {background:#ffeecd; color:#54656f; font-size:0.72rem; padding:5px 10px; border-radius:8px;
                  display:inline-block;}
/* fondo del contenedor del chat */
.st-key-wa_chat {background:#efeae2; border-radius:0 0 12px 12px; padding:6px 10px;}
</style>
""", unsafe_allow_html=True)

PROVEEDORES_DEMO = ["groq", "gemini", "ollama"]
RESPALDOS = ["gemini", "groq", "ollama", "ninguno"]

# Texto que ve el público mientras el agente trabaja (para narrar encima)
ACCION_TOOL = {
    "calcular_cotizacion": "Cotizando con precios del ERP…",
    "buscar_politicas": "Buscando en las políticas y guías de la florícola…",
    "consultar_inventario": "Consultando tallos disponibles por finca…",
    "consultar_cliente": "Consultando crédito del cliente…",
    "buscar_producto": "Buscando la variedad en el catálogo…",
    "preparar_borrador_pedido": "Preparando el borrador del pedido…",
}


def _siguiente_accion(nodo: str, upd: dict) -> str:
    """Qué está por hacer el agente, según el nodo que acaba de terminar."""
    upd = upd or {}
    if nodo == "guardrail_entrada":
        return "Revisión de seguridad lista" if upd.get("bloqueado") else f"{EMPRESA} está escribiendo…"
    if nodo == "agente":
        msgs = upd.get("messages") or []
        tcs = getattr(msgs[-1], "tool_calls", []) if msgs else []
        if tcs:
            return " · ".join(dict.fromkeys(ACCION_TOOL.get(tc["name"], tc["name"]) for tc in tcs))
        return "Verificando cifras y citas contra los datos…"
    if nodo == "herramientas":
        return ("Validando pedido: bonches, stock por finca, precio ERP, descuento y crédito…" if upd.get("borrador_nuevo")
                else f"{EMPRESA} está escribiendo…")
    if nodo == "validador_deterministico":
        v = upd.get("validacion") or {}
        return "Esperando aprobación del supervisor…" if v.get("aprobado") else "Explicando el rechazo y las alternativas…"
    if nodo == "aprobacion_humana":
        return "Registrando en el ERP…" if upd.get("siguiente") == "escribir" else "Procesando la decisión…"
    if nodo == "escribir_erp":
        return "Pedido registrado: armando la confirmación…"
    if nodo == "grounding_check":
        return ("Cifra sin respaldo detectada: regenerando…" if upd.get("siguiente") == "agente"
                else "Armando la respuesta final…")
    return "Procesando…"


NOMBRE_PASO = {"guardrail_entrada": "Guardrail", "agente": "Agente", "validador_deterministico": "Validador",
               "aprobacion_humana": "Aprobación humana", "escribir_erp": "ERP", "grounding_check": "Grounding",
               "respuesta_final": "Respuesta"}


# ------------------------------------------------------------------ estado
def _nuevo_hilo() -> None:
    # Un hilo por número de WhatsApp (como en producción) + sufijo para que "Reiniciar demo" empiece de cero.
    numero = st.session_state.get("numero", CLIENTE_DEMO["whatsapp"])
    st.session_state.thread_id = f"{hilo_de(numero)}-{uuid.uuid4().hex[:6]}"
    st.session_state.rf_por_turno = {}
    st.session_state.costos_turnos = []
    st.session_state.error = None
    st.session_state.tracker = CostTracker()
    st.session_state.prompt_pendiente = None
    st.session_state.panel = "✅ Aprobaciones"
    st.session_state.horas = {}
    st.session_state.pop("marca_pend", None)
    for k in [k for k in st.session_state if str(k).startswith("ed-")]:
        del st.session_state[k]


if "thread_id" not in st.session_state:
    st.session_state.numero = CLIENTE_DEMO["whatsapp"]
    _nuevo_hilo()
    st.session_state.proveedor = cfg.proveedor_actual() if cfg.proveedor_actual() in PROVEEDORES_DEMO else "groq"
    _r = cfg.proveedor_respaldo(st.session_state.proveedor) or "ninguno"
    st.session_state.respaldo = _r if _r in RESPALDOS else "ninguno"
    st.session_state.aprobador = "supervisor.maria"


@st.cache_resource(show_spinner=False)
def obtener_grafo(proveedor: str, respaldo: str):
    return build_graph(cfg.get_llm(proveedor), respaldo=cfg.get_llm_respaldo(proveedor, elegido=respaldo))


@st.cache_resource(show_spinner="Cargando índice de políticas (embeddings locales)…")
def precalentar_rag() -> bool:
    from src.rag import precalentar
    precalentar()
    return True


erp = ERPClient()
if not erp.db_path.exists():
    reset_db()

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown(f"### 🌹 {EMPRESA}")
    st.caption("Agente de ventas por WhatsApp · LangGraph · validación determinista · aprobación humana")

    clientes = erp._all("SELECT id, razon_social, whatsapp, pais FROM clientes ORDER BY id")
    etiquetas = {c["whatsapp"]: f"{c['razon_social']} · {c['whatsapp']}" for c in clientes}
    numeros = list(etiquetas)
    numero = st.selectbox("Cliente que escribe (su WhatsApp)", numeros,
                          index=numeros.index(st.session_state.numero) if st.session_state.numero in numeros else 0,
                          format_func=etiquetas.get,
                          help="El número identifica al cliente, como en WhatsApp: el agente no le pregunta quién es.")
    if numero != st.session_state.numero:
        st.session_state.numero = numero
        _nuevo_hilo()
        st.rerun()

    c1, c2 = st.columns(2)
    prov = c1.selectbox("LLM principal", PROVEEDORES_DEMO,
                        index=PROVEEDORES_DEMO.index(st.session_state.proveedor),
                        help="Groq es el más rápido. Ollama funciona sin internet.")
    resp_elegido = c2.selectbox("Respaldo", RESPALDOS, index=RESPALDOS.index(st.session_state.respaldo),
                                help="Toma la llamada si el principal falla o pide esperar más de "
                                     f"{cfg.FALLBACK_MAX_ESPERA:g} s.")
    if prov != st.session_state.proveedor or resp_elegido != st.session_state.respaldo:
        st.session_state.proveedor, st.session_state.respaldo = prov, resp_elegido
        _nuevo_hilo()
        st.rerun()
    _resp = cfg.proveedor_respaldo(prov, elegido=resp_elegido)
    st.caption(f"`{cfg.modelo_de(prov)}` · temperatura 0"
               + (f"  \nRespaldo: `{cfg.modelo_de(_resp)}`" if _resp else "  \nSin respaldo"))

    try:
        grafo = obtener_grafo(prov, resp_elegido)
    except Exception as e:  # noqa: BLE001
        grafo = None
        st.error(f"No se pudo iniciar {prov}: {e}")

    st.markdown("#### Escenarios de la demo")
    for esc in ESCENARIOS:
        if st.button(esc["titulo"], width="stretch", key=f"esc{esc['id']}"):
            st.session_state.prompt_pendiente = esc["mensaje"]
    st.divider()
    if st.button("Reiniciar demo", icon=":material/restart_alt:", width="stretch",
                 help="Deja el ERP y la auditoría en el estado inicial y limpia el chat"):
        reset_db()
        _nuevo_hilo()
        st.toast("ERP, auditoría y chat reiniciados", icon=":material/check_circle:")
        st.rerun()
    st.session_state.aprobador = st.text_input("Usuario aprobador", st.session_state.aprobador)

tid = st.session_state.thread_id
conf = {"configurable": {"thread_id": tid}, "callbacks": [st.session_state.tracker]}
estado = grafo.get_state({"configurable": {"thread_id": tid}}).values if grafo else {}
pendiente = pendiente_de_aprobacion(grafo, tid) if grafo else None
cliente_canal = erp.get_cliente(st.session_state.numero)


# ------------------------------------------------------------------ chat WhatsApp
_NEGRITA_WA = re.compile(r"\*([^*\n]+)\*")
_CURSIVA_WA = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")


def _html_whatsapp(texto: str) -> str:
    """Texto de WhatsApp → HTML seguro. Se escapa TODO antes de dar formato (texto del cliente y del LLM)."""
    t = html.escape(formatear_para_whatsapp(texto))
    t = _NEGRITA_WA.sub(r"<b>\1</b>", t)
    t = _CURSIVA_WA.sub(r"<i>\1</i>", t)
    # st.markdown procesa el HTML como markdown: se neutralizan $ (LaTeX), * y _ sobrantes y los saltos de línea.
    return (t.replace("$", "&#36;").replace("*", "&#42;").replace("_", "&#95;")
             .replace("\n", "<br>"))


def _hora(clave: str) -> str:
    return st.session_state.horas.setdefault(clave, datetime.now().strftime("%H:%M"))


def burbuja(rol: str, texto: str, clave: str, extra_html: str = "") -> None:
    lado = "out" if rol == "user" else "in"
    tick = " <span class='wa-tick'>✓✓</span>" if lado == "out" else ""
    st.markdown(f"<div class='wa-row {lado}'><div class='wa-bubble {lado}'>{_html_whatsapp(texto)}{extra_html}"
                f"<span class='wa-meta'>{_hora(clave)}{tick}</span></div></div>", unsafe_allow_html=True)


def _texto_paso(p: dict) -> str:
    nodo = p["nodo"]
    nombre = ("Herramienta " + nodo.split(":", 1)[1]) if nodo.startswith("tool:") else NOMBRE_PASO.get(nodo, nodo)
    detalle = p["detalle"] if len(p["detalle"]) <= 140 else p["detalle"][:140] + "…"
    return f"**{nombre}** — {detalle}".replace("$", r"\$")


def ejecutar(entrada, etiqueta: str, contenedor=None) -> None:
    """Corre el grafo mostrando EN VIVO, dentro del chat, el paso actual y la línea de tiempo."""
    tracker: CostTracker = st.session_state.tracker
    n0 = len(tracker.llamadas)
    st.session_state.error = None
    t0 = time.time()
    with (contenedor or st.container()), \
            st.status(f":shimmer[{etiqueta}]", type="compact", expanded=True) as s:
        def _aviso(n, seg, m):
            texto = ("Proveedor principal no disponible: usando el respaldo" if n == 0
                     else f"Límite de la API: reintentando ({n}) en {seg:.0f} s")
            s.update(label=f":shimmer[{texto}…]")
            st.status(texto, type="step", state="error")
        RETRY_HOOKS[tid] = _aviso
        try:
            for chunk in grafo.stream(entrada, conf, stream_mode="updates"):
                for nodo, upd in chunk.items():
                    if nodo == "__interrupt__":
                        s.update(label=":shimmer[Esperando aprobación del supervisor…]")
                        continue
                    for p in (upd or {}).get("traza", []):
                        st.status(_texto_paso(p), type="step", state="error" if p["ok"] is False else "complete")
                    s.update(label=f":shimmer[{_siguiente_accion(nodo, upd)}]")
            s.update(label=f"Listo en {time.time() - t0:.1f} s", state="complete", expanded=False)
        except LLMNoDisponible as e:
            st.session_state.error = f"{e}. Cambia el proveedor principal en la barra lateral."
            s.update(label="El proveedor de LLM no respondió", state="error")
    nuevas = tracker.llamadas[n0:]
    snap = grafo.get_state({"configurable": {"thread_id": tid}}).values
    if snap.get("respuesta_final"):
        st.session_state.rf_por_turno[snap["turno"]] = snap["respuesta_final"]
    if nuevas or isinstance(entrada, dict):
        ti = sum(c["tokens_entrada"] for c in nuevas)
        to = sum(c["tokens_salida"] for c in nuevas)
        if isinstance(entrada, dict):
            consulta = texto_de(entrada["messages"][-1])
            consulta = consulta if len(consulta) <= 60 else consulta[:60] + "…"
        else:
            consulta = f"Supervisor: {(entrada.resume or {}).get('accion', '')}"
        st.session_state.costos_turnos.append({
            "turno": snap.get("turno"), "consulta": consulta, "segundos": round(time.time() - t0, 1),
            "llamadas_llm": len(nuevas), "tokens_entrada": ti, "tokens_salida": to,
            "usd_gpt4o_mini": round(costo_usd(ti, to), 6), "usd_gpt4o": round(costo_usd(ti, to, "gpt-4o"), 6)})
    st.rerun()


def _extra_respuesta(rf: dict, tn: int) -> str:
    """Badges de confianza, tiempo y fuentes bajo la respuesta (solo valores generados por el código)."""
    conf_ = rf["nivel_confianza"]
    badges = f"<span class='badge b-{conf_}'>confianza {conf_}</span>"
    if rf.get("escalar_a_asesor"):
        badges += "<span class='badge b-baja'>escalado a asesor</span>"
    if rf.get("pedido_id"):
        badges += f"<span class='badge b-alta'>ERP: {html.escape(rf['pedido_id'])}</span>"
    del_turno = [c for c in st.session_state.costos_turnos if c["turno"] == tn]
    if del_turno:
        seg_t = sum(c.get("segundos", 0) for c in del_turno)
        tok_t = sum(c["tokens_entrada"] + c["tokens_salida"] for c in del_turno)
        badges += f"<span class='badge b-info'>⏱ {seg_t:.1f} s · {tok_t:,} tokens</span>"
    fuentes = ""
    if rf.get("fuentes"):
        fuentes = "<span class='wa-fuente'>📎 " + html.escape(" · ".join(rf["fuentes"])).replace("_", "&#95;") + "</span>"
    return f"<div>{badges}</div>{fuentes}"


st.markdown(f"## {EMPRESA} · agente de ventas <span class='badge b-info'>demo WhatsApp</span>",
            unsafe_allow_html=True)
col_chat, col_panel = st.columns([1.05, 1], gap="large")

with col_chat:
    nombre_cliente = html.escape(cliente_canal["razon_social"]) if cliente_canal else "Cliente"
    st.markdown(
        f"<div class='wa-header'><div class='wa-avatar'>🌹</div><div><div class='wa-name'>{EMPRESA} · Ventas</div>"
        f"<div class='wa-sub'>en línea · chateando con {nombre_cliente} ({html.escape(st.session_state.numero)})"
        f"</div></div></div>", unsafe_allow_html=True)
    caja = st.container(height=600, key="wa_chat")
    with caja:
        st.markdown("<div class='wa-dia'><span>HOY</span></div>"
                    "<div class='wa-cifrado'><span>🔒 Demo: el chat simula WhatsApp. Ningún mensaje sale de este "
                    "equipo.</span></div>", unsafe_allow_html=True)
        turno = 0
        mensajes = estado.get("messages", [])
        visibles = []
        for i, m in enumerate(mensajes):
            if es_interno(m):
                continue
            if isinstance(m, HumanMessage):
                turno += 1
                visibles.append(("user", texto_de(m), turno, m.id or str(i)))
            elif isinstance(m, AIMessage) and not m.tool_calls and texto_de(m).strip():
                visibles.append(("assistant", texto_de(m), turno, m.id or str(i)))
        ultimos_ai = {tn: clave for rol, _t, tn, clave in visibles if rol == "assistant"}
        if not visibles and not st.session_state.prompt_pendiente:
            burbuja("assistant", f"¡Hola! 🌹 Soy el asistente de ventas de {EMPRESA}. Puedo cotizarle flores, "
                                 "revisar disponibilidad, tomar su pedido y resolver dudas de envíos y cuidado.",
                    "bienvenida")
        for rol, texto, tn, clave in visibles:
            rf = st.session_state.rf_por_turno.get(tn)
            extra = _extra_respuesta(rf, tn) if rol == "assistant" and rf and ultimos_ai.get(tn) == clave else ""
            burbuja(rol, texto, clave, extra)
        if pendiente:
            burbuja("assistant", "Su pedido pasó las validaciones automáticas y está *pendiente de aprobación* "
                                 "de un supervisor. Le confirmo por aquí en cuanto quede registrado. 🌷",
                    f"pend-{estado.get('turno')}")
    if st.session_state.error:
        st.error(st.session_state.error)
    prompt = st.chat_input("Escribe un mensaje como el cliente…", disabled=bool(pendiente) or grafo is None)
    if pendiente:
        st.caption("⏸️ Hay un pedido esperando aprobación → pestaña **Aprobaciones**.")
    entrada_usuario = prompt or st.session_state.prompt_pendiente
    if entrada_usuario and grafo and not pendiente:
        st.session_state.prompt_pendiente = None
        with caja:
            burbuja("user", entrada_usuario, f"nuevo-{uuid.uuid4().hex[:6]}")
        msg = HumanMessage(content=entrada_usuario, id=f"wa-{uuid.uuid4().hex[:10]}")
        st.session_state.horas[msg.id] = datetime.now().strftime("%H:%M")
        ejecutar({"messages": [msg], "cliente_canal": cliente_canal}, f"{EMPRESA} está escribiendo…", caja)
    elif st.session_state.prompt_pendiente and pendiente:
        st.session_state.prompt_pendiente = None
        st.warning("Primero aprueba o rechaza el pedido pendiente.")


def _checks(checks: list[dict]) -> None:
    for c in checks:
        sku = f"**{c['sku']}** · " if c.get("sku") else ""
        linea = f"{c['nombre'].replace('_', ' ')} — {sku}{c['detalle']}".replace("$", r"\$")  # $ sin escapar = LaTeX en markdown
        st.markdown(f":green[:material/check_circle: {linea}]" if c["ok"] else f":red[:material/cancel: **{linea}**]")


def _tabla_lineas(v: dict) -> None:
    df = pd.DataFrame(v["lineas"])
    if not df.empty:
        df = df[["sku", "nombre", "cantidad", "bodega", "precio_unitario", "descuento_pct", "subtotal"]]
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"cantidad": st.column_config.NumberColumn("tallos", format="%d"),
                                    "bodega": st.column_config.TextColumn("finca"),
                                    "precio_unitario": st.column_config.NumberColumn("precio/tallo", format="$%.2f"),
                                    "descuento_pct": st.column_config.NumberColumn("desc. %", format="%.0f%%"),
                                    "subtotal": st.column_config.NumberColumn(format="$%.2f")})


with col_panel:
    PANELES = ["✅ Aprobaciones", "🧭 Traza", "💲 Costos", "🗄️ ERP"]
    # Cuando aparece un pedido pendiente, el panel salta solo a Aprobaciones.
    marca_pend = f"{tid}-{estado.get('turno')}-{bool(pendiente)}"
    if pendiente and st.session_state.get("marca_pend") != marca_pend:
        st.session_state.panel = PANELES[0]
    st.session_state.marca_pend = marca_pend
    if not st.session_state.get("panel"):
        st.session_state.panel = PANELES[0]
    panel = st.segmented_control("Panel", PANELES, key="panel", label_visibility="collapsed") or PANELES[0]

    # ---------------------------------------------------------- Aprobaciones
    if panel == "✅ Aprobaciones":
        if pendiente:
            v = pendiente["validacion"]
            st.markdown("##### ⏸️ Pendiente de aprobación")
            c = v["cliente"]
            st.markdown(f"**{c['razon_social']}** ({c['pais']}) · ID {c['id']} · segmento {c['segmento']}  \n"
                        f"Destino: {v.get('destino') or '—'} · Crédito disponible: \\${c['credito_disponible']:,.2f}")
            _tabla_lineas(v)
            st.markdown(f"**Total: \\${v['total']:,.2f}** (FOB Quito)")
            st.markdown("**Validaciones automáticas**")
            _checks(v["checks"])
            for corr in v.get("correcciones", []):
                st.warning(f"Corrección automática: {corr}".replace("$", r"\$"))
            b1, b2 = st.columns(2)
            if b1.button("✅ Aprobar", type="primary", width="stretch"):
                ejecutar(Command(resume={"accion": "aprobar", "usuario": st.session_state.aprobador}),
                         "Registrando en el ERP…", caja)
            if b2.button("✖️ Rechazar", width="stretch"):
                ejecutar(Command(resume={"accion": "rechazar", "usuario": st.session_state.aprobador}),
                         "Rechazando…", caja)
            with st.expander("✏️ Editar cantidad"):
                nuevas = {}
                for l in v["lineas"]:
                    nuevas[l["sku"]] = st.number_input(f"{l['sku']} · {l['nombre']} (tallos)", min_value=25,
                                                       value=int(l["cantidad"]), step=25, key=f"ed-{l['sku']}")
                if st.button("Revalidar con nuevas cantidades", width="stretch"):
                    ejecutar(Command(resume={"accion": "editar", "usuario": st.session_state.aprobador,
                                             "cantidades": nuevas}), "Revalidando…", caja)
        elif estado.get("validacion"):
            v = estado["validacion"]
            if estado.get("pedido_creado"):
                st.success(f"Pedido {estado['pedido_creado']['pedido_id']} registrado en el ERP "
                           f"(aprobado por {estado['pedido_creado']['aprobado_por']}).")
            elif not v["aprobado"]:
                st.error("El validador determinista bloqueó el último borrador. No llegó al supervisor ni al ERP.")
            st.markdown("**Validaciones del último borrador**")
            _tabla_lineas(v)
            _checks(v["checks"])
            if v.get("alternativas"):
                st.markdown("**Alternativas calculadas por el validador**")
                for a in v["alternativas"]:
                    st.markdown("- " + a.replace("$", r"\$"))
        else:
            st.info("No hay pedidos pendientes. Cuando el agente arme un borrador válido aparecerá aquí "
                    "con las validaciones en verde/rojo.")

    # ---------------------------------------------------------- Traza
    if panel == "🧭 Traza":
        traza = estado.get("traza", [])
        if not traza:
            st.info("Aquí se ven los pasos del grafo y las tools llamadas.")
        turnos = sorted({p["turno"] for p in traza}, reverse=True)
        for t in turnos:
            pasos = [p for p in traza if p["turno"] == t]
            with st.expander(f"Turno {t} · {len(pasos)} pasos", expanded=(t == turnos[0])):
                for p in pasos:
                    icono = {True: "✅", False: "⛔", None: "▫️"}[p["ok"]]
                    extra = f" · {p['segundos']} s" if p.get("segundos") else ""
                    if p.get("modelo"):
                        extra += f" · `{p['modelo']}`"
                    st.markdown(f"{icono} `{p['hora']}` **{p['nodo']}**{extra} — {p['detalle']}".replace("$", r"\$"))
                    if p.get("respuesta_descartada"):
                        st.caption("Respuesta descartada por grounding: "
                                   + p["respuesta_descartada"].replace("$", r"\$"))
        rf_ult = st.session_state.rf_por_turno.get(estado.get("turno"))
        if rf_ult and rf_ult.get("respuesta_whatsapp"):
            with st.expander("📱 Mensaje que se enviaría por la API de WhatsApp"):
                st.code(rf_ult["respuesta_whatsapp"], language=None, wrap_lines=True)

    # ---------------------------------------------------------- Costos
    if panel == "💲 Costos":
        tr: CostTracker = st.session_state.tracker
        st.caption(f"Tokens reales consumidos por **{prov}** (`{cfg.modelo_de(prov)}`, gratis en la demo).")
        m1, m2, m3 = st.columns(3)
        m1.metric("Llamadas al LLM", len(tr.llamadas))
        m2.metric("Tokens entrada", f"{tr.tokens_entrada:,}")
        m3.metric("Tokens salida", f"{tr.tokens_salida:,}")
        st.metric("Costo estimado en producción (Azure gpt-4o-mini)", f"${tr.costo('gpt-4o-mini'):.5f}",
                  help="$0.15 por millón de tokens de entrada y $0.60 por millón de salida.")
        consultas = [c for c in st.session_state.costos_turnos if c["llamadas_llm"]]
        if consultas:
            prom = sum(c["usd_gpt4o_mini"] for c in consultas) / len(consultas)
            prom_4o = sum(c["usd_gpt4o"] for c in consultas) / len(consultas)
            k1, k2, k3 = st.columns(3)
            k1.metric("Promedio por consulta", f"${prom:.5f}")
            k2.metric("2.000 chats/mes · gpt-4o-mini", f"${prom * 2000:,.2f}")
            k3.metric("2.000 chats/mes · GPT-4o", f"${prom_4o * 2000:,.2f}")
            st.dataframe(pd.DataFrame(st.session_state.costos_turnos), hide_index=True, width="stretch",
                         column_config={"usd_gpt4o_mini": st.column_config.NumberColumn("USD 4o-mini", format="$%.5f"),
                                        "usd_gpt4o": st.column_config.NumberColumn("USD 4o", format="$%.5f"),
                                        "consulta": st.column_config.TextColumn(width="medium")})
        st.caption("Controles de costo: modelo pequeño, temperatura 0, máximo 6 iteraciones, historial recortado "
                   "(turnos previos condensados), guardrails sin LLM, embeddings locales con caché y RAG solo cuando "
                   "hace falta. No incluye el costo por conversación de la API de WhatsApp.")

    # ---------------------------------------------------------- ERP
    if panel == "🗄️ ERP":
        pedidos = pd.DataFrame(erp.listar_pedidos())
        st.markdown("##### Pedidos en el ERP")
        if not pedidos.empty:
            vista = pedidos[["id", "fecha", "cliente", "estado", "total", "creado_por", "aprobado_por"]].copy()
            vista["fecha"] = vista["fecha"].str.slice(5, 16)  # MM-DD HH:MM: cabe a media pantalla
            st.dataframe(vista.style.apply(
                lambda r: ["background-color:rgba(46,160,67,0.35); font-weight:600" if r["creado_por"] == "agente-ia" else ""] * len(r), axis=1),
                hide_index=True, width="stretch",
                column_config={"total": st.column_config.NumberColumn(format="$%.2f"),
                               "cliente": st.column_config.TextColumn(width="small"),
                               "creado_por": st.column_config.TextColumn("creado por"),
                               "aprobado_por": st.column_config.TextColumn("aprobado por")})
            nuevos = pedidos[pedidos["creado_por"] == "agente-ia"]["id"].tolist()
            if nuevos:
                det = pd.DataFrame(erp.detalle_pedido(nuevos[0])).rename(columns={"bodega": "finca", "cantidad": "tallos"})
                st.caption(f"Detalle de {nuevos[0]} (creado por el agente, aprobado por un humano)")
                st.dataframe(det, hide_index=True, width="stretch")
        st.markdown("##### Inventario de las fincas (tallos)")
        inv = pd.DataFrame(erp.inventario_completo())
        if not inv.empty:
            tabla = inv.pivot_table(index=["sku", "nombre"], columns="bodega", values="stock_disponible").reset_index()
            st.dataframe(tabla, hide_index=True, width="stretch", height=200)
        st.markdown("##### Auditoría (más reciente primero)")
        aud = pd.DataFrame(erp.auditoria(30))
        if not aud.empty:
            aud["hora"] = aud["timestamp"].str.slice(11, 19)
            st.dataframe(aud[["id", "hora", "usuario", "accion", "resultado"]], hide_index=True,
                         width="stretch", height=280)
            with st.expander("Ver payload del último registro"):
                st.json(json.loads(aud.iloc[0]["payload_json"] or "{}"))

# Lo lento va al final: la interfaz ya está dibujada mientras se carga el modelo de embeddings (solo la 1.ª vez).
try:
    precalentar_rag()
except Exception as e:  # noqa: BLE001
    st.sidebar.warning(f"RAG no disponible: {e}")
