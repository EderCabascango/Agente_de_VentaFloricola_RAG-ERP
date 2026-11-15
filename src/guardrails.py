"""Guardrail de entrada basado SOLO en reglas (sin llamada extra al LLM: costo cero y latencia ~0)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .erp_client import _normalizar

# Se aplican sobre el texto normalizado (minúsculas, sin tildes)
PATRONES_INYECCION = [
    r"\bignor\w*\b.{0,40}\b(instrucci|regla|politica|indicaci|restricci|validaci|control)",
    r"\bolvid\w*\b.{0,40}\b(instrucci|regla|anterior|politica)",
    r"\bsin\s+(la\s+|ninguna\s+)?(aprobacion|validacion|supervis|control|revision)",
    r"\b(salt\w*|omit\w*|evit\w*|desactiv\w*|bypass)\b.{0,30}\b(validaci|aprobaci|control|verificaci|regla|supervis)",
    r"(system|sistema)\s*prompt|prompt\s+del\s+sistema|instrucciones\s+(internas|originales|del\s+sistema)",
    r"\b(actua|finge|haz\s+de\s+cuenta|pretende)\b.{0,25}\b(como|que\s+eres)\b",
    r"modo\s+(desarrollador|developer|dios|admin|root)|jailbreak|\bdan\b",
    r"\b(borra\w*|elimina\w*|anula\w*|truncate|drop|delete)\b.{0,30}\b(pedidos|registros|base|tabla|auditoria|datos|inventario|todo)",
    r"\b(escrib\w*|insert\w*|registr\w*|guarda\w*)\b.{0,20}\bdirectamente\b",
    r"\b(apruebalo|apruebala|autoapru\w*)\b|\bapruebe?\s+tu\s+mismo",
    r"ignore\s+(all\s+|any\s+|previous\s+|your\s+|the\s+)*(instructions|rules)",
    r"<\s*/?\s*(system|instruction)s?\s*>",
]

PATRONES_FUERA_DE_ALCANCE = [
    r"\b(futbol|partido de|mundial|elecciones|presidente|politica nacional|receta|chiste|poema|cancion|horoscopo|"
    r"pelicula|serie de tv|novia|novio|bitcoin|criptomoneda|apuesta|tarea de|codigo (python|java)|"
    r"programa en python)\b",
]

PATRONES_DOMINIO = (
    r"pedido|producto|precio|stock|inventario|descuento|entrega|despacho|credito|factura|reclamo|sku|"
    r"tallo|bonche|caja|finca|cotiz|cliente|compra|envio|vuelo|flete|politica|temporada|valentin|"
    r"rosa|flor|clavel|gypsophila|hypericum|limonium|alstroemeria|crisantemo|girasol|florero|hidrat|"
    r"variedad|color|largo"
)

MAX_LARGO = 1500

RESPUESTA_INYECCION = (
    "No puedo hacer eso. Por seguridad, todo pedido pasa por validaciones automáticas y por la "
    "aprobación de un supervisor antes de llegar al ERP. Este intento quedó registrado en la auditoría. "
    "¿Le ayudo con una consulta o con un pedido de flores?"
)
RESPUESTA_FUERA_DE_ALCANCE = (
    "Solo puedo ayudarle con nuestras flores: variedades, disponibilidad, precios, envíos, cuidado "
    "y pedidos. ¿En qué le ayudo?"
)


@dataclass
class ResultadoGuardrail:
    permitido: bool
    categoria: str = "ok"          # ok | inyeccion | fuera_de_alcance | vacio | muy_largo
    patron: str = ""
    respuesta: str = ""


_VERBO_PEDIDO = re.compile(r"\b(pedir|pedido de|hacer un pedido|realizar un pedido|comprar|ordenar|encargar|confirm\w*|procedan|procede)\b")
_QUIERO = re.compile(r"\b(quiero|queremos|quisiera|quisieramos|necesito|necesitamos|deseo|deseamos|requerimos)\b")
_CONSULTA = re.compile(r"\?|\b(precio|cuanto|cuesta|cotiza\w*|aplica|saber)\b")
_COTIZA = re.compile(r"\b(descuento|precio|cuanto|cuesta|cotiza\w*)\b")


_AFIRMA = re.compile(r"^\W*(si+|claro|ok(ay)?|dale|listo|perfecto|de acuerdo|acepto|confirmo|confirmado|me parece|"
                     r"va|vale|adelante|procede|hagalo|esa|ese|eso|bueno|genial|excelente)\b")


def es_confirmacion(texto: str) -> bool:
    """Respuesta corta que acepta lo que el agente acaba de proponer ("sí, me parece", "dale", "esa")."""
    t = _normalizar(texto).strip()
    return len(t.split()) <= 12 and bool(_AFIRMA.search(t))


def detectar_intencion(texto: str) -> str | None:
    """Regla determinista: 'pedido', 'cotizacion' o None.

    Con 'pedido' el grafo obliga al LLM a enviar el borrador al validador (no lo decide el modelo);
    con 'cotizacion' lo obliga a usar calcular_cotizacion (las cuentas las hace el código).
    """
    t = _normalizar(texto)
    if not re.search(r"\d", t):
        return None
    if _VERBO_PEDIDO.search(t) or (_QUIERO.search(t) and not _CONSULTA.search(t)):
        return "pedido"
    if _COTIZA.search(t):
        return "cotizacion"
    return None


def revisar_entrada(texto: str) -> ResultadoGuardrail:
    if not texto or not texto.strip():
        return ResultadoGuardrail(False, "vacio", "", "¿En qué le puedo ayudar?")
    if len(texto) > MAX_LARGO:
        return ResultadoGuardrail(False, "muy_largo", f">{MAX_LARGO} caracteres",
                                  "El mensaje es demasiado largo. ¿Puede resumir su consulta?")
    t = _normalizar(texto)
    for p in PATRONES_INYECCION:
        if re.search(p, t):
            return ResultadoGuardrail(False, "inyeccion", p, RESPUESTA_INYECCION)
    if not re.search(PATRONES_DOMINIO, t):
        for p in PATRONES_FUERA_DE_ALCANCE:
            if re.search(p, t):
                return ResultadoGuardrail(False, "fuera_de_alcance", p, RESPUESTA_FUERA_DE_ALCANCE)
    return ResultadoGuardrail(True)
