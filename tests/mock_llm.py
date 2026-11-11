"""LLM simulado SOLO para pytest: devuelve respuestas guionadas en orden.

No se usa en la app ni en la demo (la demo siempre corre con un LLM real).
"""
from __future__ import annotations

import itertools
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_ids = itertools.count(1)


def tool_call(nombre: str, **args) -> dict:
    return {"name": nombre, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def llamar(*tool_calls: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=list(tool_calls))


def responder(texto: str) -> AIMessage:
    return AIMessage(content=texto)


class ScriptedChatModel(BaseChatModel):
    """Cada invocación devuelve la siguiente respuesta del guion. Puede lanzar excepciones guionadas."""

    guion: list = []
    llamadas: int = 0
    repetir_ultimo: bool = False

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.llamadas >= len(self.guion):
            if self.repetir_ultimo and self.guion:
                item = self.guion[-1]
            else:
                raise AssertionError(f"El LLM simulado no tiene más respuestas (llamada {self.llamadas + 1}).")
        else:
            item = self.guion[self.llamadas]
        self.llamadas += 1
        if isinstance(item, Exception):
            raise item
        msg = item.model_copy(deep=True)
        if msg.tool_calls:  # ids únicos por llamada
            msg.tool_calls = [{**tc, "id": f"call_{next(_ids)}"} for tc in msg.tool_calls]
        msg.usage_metadata = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        return ChatResult(generations=[ChatGeneration(message=msg)])
