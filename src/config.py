"""Configuración central: rutas, límites y selección del LLM por .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

DB_PATH = Path(os.getenv("ERP_DB_PATH", str(ROOT / "erp.db")))
EMPRESA = os.getenv("EMPRESA", "Rosas del Valle").strip() or "Rosas del Valle"
# ERP en la nube (Cloudflare D1, ver la plataforma de la landing). Sin ERP_URL se usa el ERP local (erp.db).
ERP_URL = os.getenv("ERP_URL", "").strip().rstrip("/")
ERP_TOKEN = os.getenv("ERP_TOKEN", "").strip()
# Cómo se aprueba un pedido: "erp" = queda PENDIENTE en el ERP y una persona lo acepta o rechaza en la plataforma;
# "interrupt" = el grafo se pausa y se aprueba con botones (demo de Streamlit o WhatsApp).
APROBACION = os.getenv("APROBACION", "erp" if ERP_URL else "interrupt").strip().lower()
POLICIES_DIR = ROOT / "data" / "policies"
CHROMA_DIR = ROOT / ".chroma"
EMBEDDINGS_MODEL = os.getenv(
    "EMBEDDINGS_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
MAX_ITERACIONES = int(os.getenv("MAX_ITERACIONES", "6"))
MAX_INTENTOS_VALIDACION = 3
# Si la API pide esperar más que esto (segundos) y hay respaldo, se pasa al respaldo en vez de esperar.
FALLBACK_MAX_ESPERA = float(os.getenv("FALLBACK_MAX_ESPERA", "3"))

# Clientes nuevos que escriben por WhatsApp sin estar en el ERP: se registran solos con condiciones
# conservadoras (sin descuento propio y un límite de crédito de prueba). El pedido igual necesita
# la aprobación de un supervisor, que ve que es un cliente nuevo.
CLIENTE_NUEVO_LIMITE_CREDITO = float(os.getenv("CLIENTE_NUEVO_LIMITE_CREDITO", "2000"))
CLIENTE_NUEVO_DESCUENTO_MAX = float(os.getenv("CLIENTE_NUEVO_DESCUENTO_MAX", "0"))

PROVEEDORES = ["gemini", "groq", "ollama", "azure"]
MODELOS_POR_DEFECTO = {
    "gemini": "gemini-3.5-flash",
    "groq": "openai/gpt-oss-120b",
    "ollama": "qwen2.5:7b",
    "azure": "gpt-4o-mini",
}
_ENV_MODELO = {
    "gemini": "GEMINI_MODEL",
    "groq": "GROQ_MODEL",
    "ollama": "OLLAMA_MODEL",
    "azure": "AZURE_OPENAI_DEPLOYMENT",
}


class ConfigError(RuntimeError):
    """Falta una clave o el proveedor no es válido."""


def proveedor_actual() -> str:
    return os.getenv("LLM_PROVIDER", "groq").strip().lower()


def modelo_de(proveedor: str) -> str:
    return os.getenv(_ENV_MODELO[proveedor], MODELOS_POR_DEFECTO[proveedor]).strip()


def _requiere(var: str, proveedor: str) -> str:
    valor = os.getenv(var, "").strip()
    if not valor:
        raise ConfigError(f"Falta {var} en .env para usar LLM_PROVIDER={proveedor}.")
    return valor


def proveedor_respaldo(principal: str | None = None, elegido: str | None = None) -> str | None:
    """Proveedor de respaldo (el elegido en la UI o LLM_FALLBACK_PROVIDER), si es distinto del principal."""
    p = (elegido if elegido is not None else os.getenv("LLM_FALLBACK_PROVIDER", "")).strip().lower()
    if p == "ninguno":
        return None
    if not p or p == (principal or proveedor_actual()):
        return None
    return p


def get_llm_respaldo(principal: str | None = None, elegido: str | None = None):
    """LLM de respaldo o None si no hay uno configurado (o le falta la clave)."""
    p = proveedor_respaldo(principal, elegido)
    if not p:
        return None
    try:
        return get_llm(p)
    except ConfigError:
        return None


def get_llm(proveedor: str | None = None):
    """Devuelve el chat model configurado. Temperatura 0 en todos los proveedores.

    Los reintentos ante 429/timeout los maneja el grafo (backoff exponencial con aviso en la UI),
    por eso aquí se desactivan los reintentos internos de cada librería.
    """
    p = (proveedor or proveedor_actual()).lower()
    modelo = modelo_de(p) if p in _ENV_MODELO else None

    if p == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs = dict(
            model=modelo,
            google_api_key=_requiere("GOOGLE_API_KEY", p),
            temperature=0,
            max_retries=0,
            timeout=60,
        )
        # "Thinking" bajo: menos latencia y tokens. La familia 2.5 usa presupuesto; la 3.x usa nivel.
        if modelo.startswith("gemini-2.5"):
            kwargs["thinking_budget"] = int(os.getenv("GEMINI_THINKING_BUDGET", "0"))
        else:
            kwargs["thinking_level"] = os.getenv("GEMINI_THINKING_LEVEL", "low")
        return ChatGoogleGenerativeAI(**kwargs)

    if p == "groq":
        from langchain_groq import ChatGroq

        extra = {}
        if "gpt-oss" in modelo:  # razonamiento bajo: menos tokens (límite gratuito ~8K tokens/min)
            extra["reasoning_effort"] = os.getenv("GROQ_REASONING_EFFORT", "low")
        return ChatGroq(
            model=modelo,
            api_key=_requiere("GROQ_API_KEY", p),
            temperature=0,
            max_retries=0,
            timeout=60,
            **extra,
        )

    if p == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=modelo,
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            temperature=0,
        )

    if p == "azure":
        # Opción de producción (documentada, no usada en la demo gratuita).
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_endpoint=_requiere("AZURE_OPENAI_ENDPOINT", p),
            api_key=_requiere("AZURE_OPENAI_API_KEY", p),
            azure_deployment=modelo,
            api_version=os.getenv("OPENAI_API_VERSION", "2024-10-21"),
            temperature=0,
            max_retries=0,
            timeout=60,
        )

    raise ConfigError(f"LLM_PROVIDER desconocido: {p!r}. Usa uno de {PROVEEDORES}.")
