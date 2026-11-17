"""Cliente mínimo de la WhatsApp Cloud API de Meta (Graph API) y lectura del webhook.

- `WhatsAppAPI`: envía texto, botones de respuesta y marca mensajes como leídos.
- `firma_valida`: verifica la cabecera X-Hub-Signature-256 (HMAC-SHA256 con el App Secret).
- `extraer_mensajes`: convierte el JSON del webhook en `MensajeEntrante`.

Sin dependencias externas (urllib), para que el canal no agregue librerías al proyecto.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("whatsapp")

LIMITE_CUERPO_BOTONES = 1024   # caracteres del cuerpo de un mensaje interactivo
LIMITE_TITULO_BOTON = 20       # caracteres del título de cada botón


class ErrorWhatsApp(RuntimeError):
    """La Graph API respondió con error (p. ej. token vencido o fuera de la ventana de 24 h)."""

    def __init__(self, mensaje: str, codigo: Optional[int] = None):
        super().__init__(mensaje)
        self.codigo = codigo


@dataclass
class AjustesWhatsApp:
    token: str
    phone_number_id: str
    verify_token: str
    app_secret: str
    version: str = "v24.0"
    supervisores: frozenset[str] = frozenset()      # wa_id (solo dígitos) que pueden aprobar pedidos
    clientes: dict[str, str] | None = None           # wa_id -> ID de cliente del ERP (testers del piloto)
    cliente_por_defecto: str = ""                    # ID de cliente para números no registrados ("" = registrarlos)
    registrar_nuevos: bool = True                    # False: los números desconocidos esperan a un asesor
    max_mensajes_minuto: int = 15                    # tope por número: protege la cuota del LLM (0 = sin tope)

    @classmethod
    def desde_entorno(cls) -> "AjustesWhatsApp":
        def digitos(x: str) -> str:
            return "".join(c for c in x if c.isdigit())

        faltan = [v for v in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
                              "WHATSAPP_APP_SECRET") if not os.getenv(v, "").strip()]
        if faltan:
            raise RuntimeError("Faltan variables en .env para el canal WhatsApp: " + ", ".join(faltan))
        clientes = {}
        for par in os.getenv("WHATSAPP_CLIENTES", "").split(","):
            if "=" in par:
                numero, cid = par.split("=", 1)
                clientes[digitos(numero)] = cid.strip()
        return cls(
            token=os.environ["WHATSAPP_TOKEN"].strip(),
            phone_number_id=os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip(),
            verify_token=os.environ["WHATSAPP_VERIFY_TOKEN"].strip(),
            app_secret=os.environ["WHATSAPP_APP_SECRET"].strip(),
            version=os.getenv("WHATSAPP_API_VERSION", "v24.0").strip(),
            supervisores=frozenset(digitos(n) for n in os.getenv("WHATSAPP_SUPERVISORES", "").split(",") if digitos(n)),
            clientes=clientes,
            cliente_por_defecto=os.getenv("WHATSAPP_CLIENTE_POR_DEFECTO", "").strip(),
            registrar_nuevos=os.getenv("WHATSAPP_REGISTRAR_NUEVOS", "1").strip().lower() not in ("0", "no", "false"),
            max_mensajes_minuto=int(os.getenv("WHATSAPP_MAX_MENSAJES_MINUTO", "15") or 0),
        )


class WhatsAppAPI:
    def __init__(self, ajustes: AjustesWhatsApp, timeout: float = 15):
        self.ajustes = ajustes
        self.timeout = timeout
        self.url = f"https://graph.facebook.com/{ajustes.version}/{ajustes.phone_number_id}/messages"

    def _post(self, payload: dict) -> dict:
        datos = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=datos, method="POST", headers={
            "Authorization": f"Bearer {self.ajustes.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(cuerpo).get("error", {})
                raise ErrorWhatsApp(f"{err.get('code')}: {err.get('message', cuerpo)[:300]}", err.get("code")) from e
            except json.JSONDecodeError:
                raise ErrorWhatsApp(f"HTTP {e.code}: {cuerpo[:300]}") from e

    def enviar_texto(self, para: str, texto: str) -> dict:
        return self._post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": para,
                           "type": "text", "text": {"body": texto[:4096], "preview_url": False}})

    def enviar_botones(self, para: str, texto: str, botones: list[tuple[str, str]]) -> dict:
        """Hasta 3 botones de respuesta: [(id, título)]. El id vuelve en el webhook al tocar el botón."""
        return self._post({
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": para, "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": texto[:LIMITE_CUERPO_BOTONES]},
                "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": t[:LIMITE_TITULO_BOTON]}}
                                       for i, t in botones[:3]]},
            },
        })

    def marcar_leido(self, message_id: str) -> None:
        try:
            self._post({"messaging_product": "whatsapp", "status": "read", "message_id": message_id})
        except ErrorWhatsApp as e:  # no es crítico: el mensaje igual se atiende
            log.warning("No se pudo marcar como leído %s: %s", message_id, e)


def firma_valida(app_secret: str, cuerpo: bytes, cabecera: Optional[str]) -> bool:
    """Meta firma cada POST del webhook con el App Secret: así nadie más puede inyectar mensajes."""
    if not cabecera or not cabecera.startswith("sha256="):
        return False
    esperada = hmac.new(app_secret.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, cabecera.split("=", 1)[1])


@dataclass
class MensajeEntrante:
    id: str
    de: str                     # wa_id del remitente (solo dígitos, con código de país)
    tipo: str                   # text | interactive | image | audio | ...
    texto: str = ""
    boton_id: str = ""          # id del botón tocado (mensajes interactive)
    nombre: str = ""            # nombre de perfil de WhatsApp


def extraer_mensajes(payload: dict) -> list[MensajeEntrante]:
    """Mensajes de usuarios dentro del webhook. Ignora los avisos de estado (enviado, entregado, leído)."""
    out = []
    for entrada in payload.get("entry", []) or []:
        for cambio in entrada.get("changes", []) or []:
            valor = cambio.get("value", {}) or {}
            nombres = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in valor.get("contacts", []) or []}
            for m in valor.get("messages", []) or []:
                tipo = m.get("type", "")
                texto = (m.get("text") or {}).get("body", "") if tipo == "text" else ""
                boton = ""
                if tipo == "interactive":
                    inter = m.get("interactive") or {}
                    boton = (inter.get("button_reply") or inter.get("list_reply") or {}).get("id", "")
                elif tipo == "button":  # respuesta a un botón de plantilla
                    boton = (m.get("button") or {}).get("payload", "")
                out.append(MensajeEntrante(id=m.get("id", ""), de=m.get("from", ""), tipo=tipo, texto=texto,
                                           boton_id=boton, nombre=nombres.get(m.get("from"), "")))
    return out
