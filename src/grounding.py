"""Chequeo de grounding: toda cifra de la respuesta debe aparecer en los datos de la conversación
(resultados de tools o lo que dijo el propio cliente). Si no, se regenera o se escala.
"""
from __future__ import annotations

import re

_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_LISTA = re.compile(r"(?m)^\s*\d+[.)]\s")       # "1. " / "2) " al inicio de línea
_TRIVIALES = {0.0, 1.0, 2.0}                     # conteos ("2 opciones") que no son datos


def _interpretaciones(token: str) -> set[float]:
    """Un mismo texto puede ser 4.100 (cuatro mil cien) o 4.1. Devolvemos todas las lecturas."""
    out: set[float] = set()
    try:
        if "." in token and "," in token:
            if token.rfind(",") > token.rfind("."):   # 1.234,56
                out.add(float(token.replace(".", "").replace(",", ".")))
            else:                                       # 1,234.56
                out.add(float(token.replace(",", "")))
        elif "," in token:
            out.add(float(token.replace(",", "")))
            if token.count(",") == 1:
                out.add(float(token.replace(",", ".")))
        elif "." in token:
            out.add(float(token.replace(".", "")))
            if token.count(".") == 1:
                out.add(float(token))
        else:
            out.add(float(token))
    except ValueError:
        pass
    return out


# "3 910,00": gpt-oss separa miles con espacio fino (U+202F) u otros espacios no separables
_ESPACIO_MILES = re.compile(r"(?<=\d)[\u00a0\u202f\u2009\u2007](?=\d{3}\b)")


def extraer_numeros(texto: str) -> list[tuple[str, set[float]]]:
    texto = _ESPACIO_MILES.sub("", _LISTA.sub(" ", texto or ""))
    return [(m.group(0), _interpretaciones(m.group(0))) for m in _NUM.finditer(texto)]


def numeros_de_fuentes(textos: list[str]) -> set[float]:
    valores: set[float] = set()
    for t in textos:
        for _, lecturas in extraer_numeros(t):
            valores |= lecturas
    return valores


def _coincide(lecturas: set[float], fuente: set[float]) -> bool:
    for v in lecturas:
        if v in _TRIVIALES:
            return True
        for f in fuente:
            if abs(v - f) <= 0.011 or (v.is_integer() and round(f) == v and abs(v - f) < 1):
                return True
    return False


_CITA = re.compile(r"[\[【]([\w\-]+\.md)[^\]】]*[\]】]")  # [x.md] o 【x.md】


def verificar_citas(respuesta: str, archivos_consultados: set[str]) -> list[str]:
    """Devuelve las citas [archivo.md] que NO salieron de buscar_politicas (citas inventadas)."""
    return sorted({c for c in _CITA.findall(respuesta or "") if c not in archivos_consultados})


def verificar_grounding(respuesta: str, textos_fuente: list[str]) -> dict:
    fuente = numeros_de_fuentes(textos_fuente)
    revisados, sin_respaldo = [], []
    for token, lecturas in extraer_numeros(respuesta):
        revisados.append(token)
        if not _coincide(lecturas, fuente):
            sin_respaldo.append(token)
    return {"ok": not sin_respaldo, "numeros_revisados": revisados, "sin_respaldo": sin_respaldo}
