"""Capa del canal WhatsApp: formato de los mensajes e identificación del cliente.

La demo simula el chat en Streamlit, pero el agente ya habla "en WhatsApp":
- `formatear_para_whatsapp` convierte el markdown del LLM al formato de WhatsApp
  (*negrita*, _cursiva_, sin títulos ni tablas) y respeta el límite de 4.096 caracteres.
- `hilo_de` convierte el número del cliente en el thread_id de LangGraph: cada número
  tiene su propia conversación con memoria.

Para conectar el canal real (WhatsApp Cloud API de Meta) solo falta un webhook que reciba el
mensaje, llame al grafo con `hilo_de(numero)` y envíe `respuesta_final["respuesta_whatsapp"]`.
Ver la sección "Camino a WhatsApp" del README.
"""
from __future__ import annotations

import re

LIMITE_WHATSAPP = 4096  # caracteres por mensaje de texto en la API de WhatsApp

_NEGRITA_MD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
# un * precedido de espacio abre; si aparece dentro de otra negrita, es una negrita anidada
_NEGRITA_ANIDADA = re.compile(r"\*([^*\s][^*\n]*\s)\*([^*\n]+?)\*([^*\s][^*\n]*)?\*")
_TITULO_MD = re.compile(r"(?m)^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_LINK_MD = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_VINETA = re.compile(r"(?m)^\s*[-*•]\s+")
_SEPARADOR_TABLA = re.compile(r"(?m)^\s*\|?\s*:?-{2,}.*$\n?")
_FILA_TABLA = re.compile(r"(?m)^\s*\|(.+)\|\s*$")
_LINEAS_VACIAS = re.compile(r"\n{3,}")


def formatear_para_whatsapp(texto: str) -> str:
    """Markdown del LLM → texto de WhatsApp."""
    t = (texto or "").strip()
    t = _SEPARADOR_TABLA.sub("", t)
    t = _FILA_TABLA.sub(lambda m: " · ".join(c.strip() for c in m.group(1).split("|") if c.strip()), t)
    t = _TITULO_MD.sub(lambda m: f"*{m.group(1)}*", t)
    t = _NEGRITA_MD.sub(lambda m: f"*{m.group(1) or m.group(2)}*", t)
    # Negrita dentro de negrita ("*Total del *8 %*.*"): WhatsApp la muestra rota, se deja solo la exterior.
    previo = None
    while previo != t:
        previo, t = t, _NEGRITA_ANIDADA.sub(lambda m: f"*{m.group(1)}{m.group(2)}{m.group(3) or ''}*", t)
    t = _LINK_MD.sub(lambda m: f"{m.group(1)}: {m.group(2)}", t)
    t = _VINETA.sub("• ", t)
    t = _LINEAS_VACIAS.sub("\n\n", t)
    if len(t) > LIMITE_WHATSAPP:
        t = t[: LIMITE_WHATSAPP - 1].rsplit(" ", 1)[0] + "…"
    return t


def normalizar_numero(numero: str) -> str:
    """'+1 (305) 555-0142' → '13055550142' (formato wa_id de la API de WhatsApp)."""
    return re.sub(r"\D", "", numero or "")


def hilo_de(numero: str) -> str:
    """thread_id de LangGraph para un número: una conversación con memoria por cliente."""
    return f"wa-{normalizar_numero(numero)}"


def resumen_pedido_pendiente(v: dict, pedido_id: str | None = None) -> str:
    """Lo que ve el cliente cuando su pedido pasó las validaciones: precio, cantidad y total.

    Se arma con el resultado del validador (precios del ERP, cuentas hechas por el código): el modelo no escribe cifras.
    """
    lineas = []
    for l in v["lineas"]:
        desc = f", {l['descuento_pct']:g}% de descuento" if l["descuento_pct"] else ""
        lineas.append(f"• {l['cantidad']} tallos ({l['cantidad'] // 25} bonches) {l['nombre']}: "
                      f"${l['precio_unitario']:.2f} c/u{desc} = ${l['subtotal']:,.2f}")
    etiqueta = f" {pedido_id}" if pedido_id else ""
    return (f"Su pedido{etiqueta} pasó las validaciones y quedó *pendiente de aprobación*:\n" + "\n".join(lineas)
            + f"\n*Total: ${v['total']:,.2f}*\nLe confirmo por aquí apenas lo aprueben. 🌷")
