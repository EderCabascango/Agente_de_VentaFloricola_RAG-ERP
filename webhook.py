"""Canal WhatsApp en producción: webhook de la WhatsApp Cloud API que conecta los mensajes con el agente.

Flujo:
  cliente escribe → Meta → POST /webhook → (firma OK) → 200 inmediato → agente en segundo plano → respuesta
  pedido válido   → mensaje con botones Aprobar / Rechazar al WhatsApp del supervisor
  supervisor toca → se reanuda el grafo (interrupt) → pedido al ERP o rechazo → aviso al cliente y al supervisor

Ejecutar (ver docs/whatsapp_produccion.md):
  uvicorn webhook:crear_app --factory --port 8000
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import OrderedDict, defaultdict
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from src import config as cfg
from src.erp_client import ERPClient, registrar_auditoria
from src.graph import LLMNoDisponible, build_graph, pendiente_de_aprobacion, texto_de
from src.whatsapp import formatear_para_whatsapp, hilo_de, normalizar_numero
from src.whatsapp_api import (AjustesWhatsApp, ErrorWhatsApp, MensajeEntrante, WhatsAppAPI, extraer_mensajes,
                              firma_valida)

log = logging.getLogger("whatsapp")

EMPRESA = "Rosaflor Andina"
CONVERSACIONES_DB = cfg.ROOT / "conversaciones.db"

MSG_SOLO_TEXTO = "Por ahora solo puedo leer mensajes de texto 🌹. ¿Me escribe su consulta?"
MSG_NO_REGISTRADO = (f"¡Hola! Gracias por escribir a {EMPRESA} 🌹. Su número todavía no está registrado como "
                     "cliente; un asesor comercial le escribirá por aquí para atenderle.")
MSG_PENDIENTE = ("Su pedido pasó las validaciones automáticas y está *pendiente de aprobación* de un supervisor. "
                 "Le confirmo por aquí en cuanto quede registrado. 🌷")
MSG_YA_PENDIENTE = ("Todavía tiene un pedido *esperando la aprobación* del supervisor. Le escribo apenas lo revise; "
                    "después podemos seguir con su nueva consulta.")
MSG_ERROR = "Tuvimos una demora técnica. Un asesor revisará su mensaje y le responderá por aquí. 🙏"


def _resumen_para_supervisor(v: dict, wa_cliente: str) -> str:
    c = v["cliente"]
    lineas = "\n".join(
        f"• {l['sku']} {l['nombre']}: {l['cantidad']} tallos · finca {l['bodega']} · ${l['precio_unitario']:.2f} "
        f"· {l['descuento_pct']:g}% = ${l['subtotal']:,.2f}" for l in v["lineas"])
    ok = sum(1 for ch in v["checks"] if ch["ok"])
    texto = (f"🌹 *Pedido pendiente de aprobación*\n"
             f"Cliente: {c['razon_social']} ({c['pais']}) · ID {c['id']}\n"
             f"WhatsApp: +{wa_cliente}\nDestino: {v.get('destino') or '—'}\n\n{lineas}\n\n"
             f"*Total: ${v['total']:,.2f}* (FOB Quito)\n"
             f"Crédito disponible: ${c['credito_disponible']:,.2f}\n"
             f"Validaciones automáticas: {ok}/{len(v['checks'])} OK")
    for corr in v.get("correcciones", []):
        texto += f"\n⚠️ {corr}"
    return texto


class CanalWhatsApp:
    """Lógica del canal, separada de FastAPI para poder probarla sin red."""

    def __init__(self, grafo, api: WhatsAppAPI, ajustes: AjustesWhatsApp, erp: Optional[ERPClient] = None):
        self.grafo = grafo
        self.api = api
        self.ajustes = ajustes
        self.erp = erp or ERPClient()
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._vistos: OrderedDict[str, None] = OrderedDict()   # Meta reintenta: se ignoran ids repetidos
        self._lock_vistos = threading.Lock()

    # ------------------------------------------------------------------ utilidades
    def ya_visto(self, message_id: str) -> bool:
        with self._lock_vistos:
            if message_id in self._vistos:
                return True
            self._vistos[message_id] = None
            if len(self._vistos) > 2000:
                self._vistos.popitem(last=False)
            return False

    def _enviar(self, para: str, texto: str) -> None:
        try:
            self.api.enviar_texto(para, formatear_para_whatsapp(texto))
        except ErrorWhatsApp as e:
            log.error("No se pudo enviar a %s: %s", para, e)
            registrar_auditoria("canal-whatsapp", "ENVIO_FALLIDO", {"para": para, "error": str(e)[:300]}, "Error API")

    def cliente_de(self, wa_id: str) -> Optional[dict]:
        """El número identifica al cliente: mapa del piloto (.env), luego el ERP, luego el cliente por defecto."""
        cid = (self.ajustes.clientes or {}).get(wa_id)
        cliente = self.erp.get_cliente(cid) if cid else self.erp.get_cliente(wa_id)
        if not cliente and self.ajustes.cliente_por_defecto:
            cliente = self.erp.get_cliente(self.ajustes.cliente_por_defecto)
        return cliente

    def _conf(self, hilo: str) -> dict:
        return {"configurable": {"thread_id": hilo}}

    def _respuesta(self, hilo: str) -> str:
        st = self.grafo.get_state(self._conf(hilo)).values
        rf = st.get("respuesta_final") or {}
        if rf.get("respuesta_whatsapp"):
            return rf["respuesta_whatsapp"]
        ultimo = next((m for m in reversed(st.get("messages", []))
                       if isinstance(m, AIMessage) and not m.tool_calls and texto_de(m).strip()), None)
        return texto_de(ultimo) if ultimo else MSG_ERROR

    # ------------------------------------------------------------------ entrada
    def procesar(self, msg: MensajeEntrante) -> None:
        self.api.marcar_leido(msg.id)
        try:
            if msg.de in self.ajustes.supervisores and msg.boton_id.split(":", 1)[0] in ("aprobar", "rechazar"):
                self._decision_supervisor(msg)
            elif msg.de in self.ajustes.supervisores and msg.texto.strip().lower() in ("/reiniciar", "/reset"):
                self._reiniciar(msg)
            elif msg.tipo != "text" or not msg.texto.strip():
                self._enviar(msg.de, MSG_SOLO_TEXTO)
            else:
                self._mensaje_cliente(msg)
        except Exception:  # noqa: BLE001 — el webhook no debe caerse por un mensaje
            log.exception("Error procesando el mensaje %s", msg.id)
            self._enviar(msg.de, MSG_ERROR)

    def _reiniciar(self, msg: MensajeEntrante) -> None:
        """Comando del piloto: el supervisor borra su propia conversación (y un pedido pendiente) para probar de cero."""
        hilo = hilo_de(msg.de)
        with self._locks[hilo]:
            self.grafo.checkpointer.delete_thread(hilo)
        registrar_auditoria(f"supervisor +{msg.de}", "CONVERSACION_REINICIADA", {"hilo": hilo}, "Comando /reiniciar")
        self._enviar(msg.de, "Listo, reinicié su conversación de prueba. Escríbame como cliente para empezar de cero. 🌹")

    def _mensaje_cliente(self, msg: MensajeEntrante) -> None:
        cliente = self.cliente_de(msg.de)
        if not cliente:
            registrar_auditoria("canal-whatsapp", "NUMERO_NO_REGISTRADO", {"wa_id": msg.de, "nombre": msg.nombre},
                                "Derivado a asesor")
            self._enviar(msg.de, MSG_NO_REGISTRADO)
            return
        hilo = hilo_de(msg.de)
        with self._locks[hilo]:  # un mensaje a la vez por conversación
            if pendiente_de_aprobacion(self.grafo, hilo):
                self._enviar(msg.de, MSG_YA_PENDIENTE)
                return
            try:
                self.grafo.invoke({"messages": [HumanMessage(content=msg.texto, id=f"wa-{msg.id}")],
                                   "cliente_canal": cliente}, self._conf(hilo))
            except LLMNoDisponible as e:
                log.error("LLM no disponible: %s", e)
                self._enviar(msg.de, MSG_ERROR)
                return
            pend = pendiente_de_aprobacion(self.grafo, hilo)
            if pend:
                self._enviar(msg.de, MSG_PENDIENTE)
                self._notificar_supervisores(pend["validacion"], hilo, msg.de)
            else:
                self._enviar(msg.de, self._respuesta(hilo))

    def _notificar_supervisores(self, v: dict, hilo: str, wa_cliente: str) -> None:
        texto = _resumen_para_supervisor(v, wa_cliente)
        botones = [(f"aprobar:{hilo}", "✅ Aprobar"), (f"rechazar:{hilo}", "✖️ Rechazar")]
        avisados = 0
        for sup in sorted(self.ajustes.supervisores):
            try:
                self.api.enviar_botones(sup, texto, botones)
                avisados += 1
            except ErrorWhatsApp as e:
                # Típico: el supervisor no escribió al bot en las últimas 24 h (código 131047).
                log.error("No se pudo avisar al supervisor %s: %s", sup, e)
                registrar_auditoria("canal-whatsapp", "AVISO_SUPERVISOR_FALLIDO",
                                    {"supervisor": sup, "hilo": hilo, "error": str(e)[:300]}, "Revisar en el panel")
        if not avisados:
            log.warning("Ningún supervisor recibió el pedido de %s: queda pendiente en el panel de Streamlit", hilo)

    def _decision_supervisor(self, msg: MensajeEntrante) -> None:
        accion, hilo = msg.boton_id.split(":", 1)
        if not hilo.startswith("wa-"):
            return
        wa_cliente = hilo[3:]
        usuario = f"supervisor {msg.nombre or '+' + msg.de}".strip()
        with self._locks[hilo]:
            if not pendiente_de_aprobacion(self.grafo, hilo):
                self._enviar(msg.de, "Ese pedido ya fue procesado (o lo aprobó otro supervisor).")
                return
            try:
                self.grafo.invoke(Command(resume={"accion": accion, "usuario": usuario}), self._conf(hilo))
            except LLMNoDisponible as e:
                log.error("LLM no disponible al reanudar: %s", e)
            st = self.grafo.get_state(self._conf(hilo)).values
        self._enviar(wa_cliente, self._respuesta(hilo))
        pedido = st.get("pedido_creado")
        if pedido:
            self._enviar(msg.de, f"✅ Pedido *{pedido['pedido_id']}* registrado en el ERP por "
                                 f"${pedido['total']:,.2f}. Ya le avisé al cliente.")
        else:
            self._enviar(msg.de, "✖️ Pedido rechazado. Ya le avisé al cliente que un asesor lo contactará."
                         if accion == "rechazar" else "El pedido no se pudo registrar; revise la auditoría.")


# ====================================================================== FastAPI

def crear_app(grafo=None, api: Optional[WhatsAppAPI] = None, ajustes: Optional[AjustesWhatsApp] = None,
              procesar_en_segundo_plano: bool = True) -> FastAPI:
    """Fábrica de la app. Sin argumentos usa .env, el LLM configurado y conversaciones persistentes en SQLite."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ajustes = ajustes or AjustesWhatsApp.desde_entorno()
    api = api or WhatsAppAPI(ajustes)
    if grafo is None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(CONVERSACIONES_DB, check_same_thread=False)
        grafo = build_graph(cfg.get_llm(), checkpointer=SqliteSaver(conn), respaldo=cfg.get_llm_respaldo())
        from src.rag import precalentar
        precalentar()  # carga los embeddings antes del primer mensaje
    canal = CanalWhatsApp(grafo, api, ajustes)

    app = FastAPI(title=f"{EMPRESA} · webhook WhatsApp", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.canal = canal

    @app.get("/salud")
    def salud() -> dict:
        return {"ok": True, "proveedor": cfg.proveedor_actual(), "supervisores": len(ajustes.supervisores)}

    @app.get("/webhook", response_class=PlainTextResponse)
    def verificar(hub_mode: str = Query("", alias="hub.mode"), hub_token: str = Query("", alias="hub.verify_token"),
                  hub_challenge: str = Query("", alias="hub.challenge")) -> str:
        """Meta llama a esta URL una vez al configurar el webhook y espera el challenge de vuelta."""
        if hub_mode == "subscribe" and hub_token == ajustes.verify_token:
            return hub_challenge
        raise HTTPException(status_code=403, detail="Token de verificación inválido")

    @app.post("/webhook")
    async def recibir(request: Request, tareas: BackgroundTasks) -> dict:
        cuerpo = await request.body()
        if not firma_valida(ajustes.app_secret, cuerpo, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(status_code=401, detail="Firma inválida")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="JSON inválido") from None
        for msg in extraer_mensajes(payload):
            msg.de = normalizar_numero(msg.de)
            if not msg.id or canal.ya_visto(msg.id):
                continue
            # Meta espera la respuesta HTTP en pocos segundos: el agente corre después de responder 200.
            if procesar_en_segundo_plano:
                tareas.add_task(canal.procesar, msg)
            else:
                canal.procesar(msg)
        return {"ok": True}

    return app
