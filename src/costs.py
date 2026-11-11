"""Contador de tokens y costo estimado (callback de LangChain).

La demo usa LLMs gratuitos, pero el costo se calcula como si se usara Azure OpenAI gpt-4o-mini
(la opción de producción), para que la gerencia vea el número real que pagaría.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

# USD por 1 millón de tokens (entrada, salida)
PRECIOS_USD_POR_MILLON = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}
MODELO_REFERENCIA = "gpt-4o-mini"


def costo_usd(tokens_entrada: int, tokens_salida: int, modelo: str = MODELO_REFERENCIA) -> float:
    pin, pout = PRECIOS_USD_POR_MILLON[modelo]
    return tokens_entrada / 1e6 * pin + tokens_salida / 1e6 * pout


def _uso(response: Any) -> tuple[int, int, str]:
    entrada = salida = 0
    modelo = ""
    for gens in getattr(response, "generations", []) or []:
        for g in gens:
            msg = getattr(g, "message", None)
            um = getattr(msg, "usage_metadata", None) if msg is not None else None
            if um:
                entrada += int(um.get("input_tokens", 0) or 0)
                salida += int(um.get("output_tokens", 0) or 0)
            meta = getattr(msg, "response_metadata", {}) or {}
            modelo = modelo or meta.get("model_name") or meta.get("model") or ""
    if not (entrada or salida):
        lo = getattr(response, "llm_output", None) or {}
        tu = lo.get("token_usage") or lo.get("usage") or {}
        entrada = int(tu.get("prompt_tokens", 0) or tu.get("input_tokens", 0) or 0)
        salida = int(tu.get("completion_tokens", 0) or tu.get("output_tokens", 0) or 0)
    return entrada, salida, modelo


class CostTracker(BaseCallbackHandler):
    """Acumula tokens por conversación. Se pasa en config={'callbacks': [tracker]}."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.llamadas: list[dict] = []

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        entrada, salida, modelo = _uso(response)
        with self._lock:
            self.llamadas.append({
                "hora": datetime.now().strftime("%H:%M:%S"),
                "modelo": modelo,
                "tokens_entrada": entrada,
                "tokens_salida": salida,
                "costo_usd_gpt4o_mini": round(costo_usd(entrada, salida), 6),
            })

    # -- totales -----------------------------------------------------------------
    @property
    def tokens_entrada(self) -> int:
        return sum(c["tokens_entrada"] for c in self.llamadas)

    @property
    def tokens_salida(self) -> int:
        return sum(c["tokens_salida"] for c in self.llamadas)

    @property
    def total_tokens(self) -> int:
        return self.tokens_entrada + self.tokens_salida

    def costo(self, modelo: str = MODELO_REFERENCIA) -> float:
        return costo_usd(self.tokens_entrada, self.tokens_salida, modelo)

    def resumen(self) -> dict:
        return {
            "llamadas_llm": len(self.llamadas),
            "tokens_entrada": self.tokens_entrada,
            "tokens_salida": self.tokens_salida,
            "total_tokens": self.total_tokens,
            "costo_usd_gpt4o_mini": round(self.costo("gpt-4o-mini"), 6),
            "costo_usd_gpt4o": round(self.costo("gpt-4o"), 6),
        }

    def reset(self) -> None:
        with self._lock:
            self.llamadas.clear()
