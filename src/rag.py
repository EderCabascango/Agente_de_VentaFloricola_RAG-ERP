"""RAG sobre las políticas comerciales: Chroma persistente + embeddings locales (gratis, sin API).

- Los documentos se indexan una sola vez; si cambian (hash distinto), se reindexan.
- Las consultas repetidas usan caché en memoria (no se vuelve a calcular el embedding).
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache

from .config import CHROMA_DIR, EMBEDDINGS_MODEL, POLICIES_DIR

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

_COLECCION = "politicas_comerciales"


def _hash_documentos() -> str:
    h = hashlib.sha256()
    for p in sorted(POLICIES_DIR.glob("*.md")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    h.update(EMBEDDINGS_MODEL.encode())
    return h.hexdigest()[:16]


@lru_cache(maxsize=1)
def _embeddings():
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(model_name=EMBEDDINGS_MODEL, encode_kwargs={"normalize_embeddings": True})


def _fragmentos() -> list:
    from langchain_core.documents import Document
    from langchain_text_splitters import MarkdownHeaderTextSplitter

    splitter = MarkdownHeaderTextSplitter([("#", "titulo"), ("##", "seccion")])
    docs = []
    for p in sorted(POLICIES_DIR.glob("*.md")):
        for d in splitter.split_text(p.read_text(encoding="utf-8")):
            seccion = d.metadata.get("seccion") or d.metadata.get("titulo", "")
            titulo = d.metadata.get("titulo", "")
            docs.append(Document(page_content=f"{titulo} — {seccion}\n{d.page_content}",
                                 metadata={"fuente": f"{p.name} › {seccion}", "archivo": p.name}))
    return docs


@lru_cache(maxsize=1)
def vectorstore():
    from langchain_chroma import Chroma
    from chromadb.config import Settings

    marca = CHROMA_DIR / "indice.json"
    actual = _hash_documentos()
    vs = Chroma(collection_name=_COLECCION, embedding_function=_embeddings(),
                persist_directory=str(CHROMA_DIR),
                client_settings=Settings(anonymized_telemetry=False, is_persistent=True,
                                         persist_directory=str(CHROMA_DIR)))
    previo = json.loads(marca.read_text())["hash"] if marca.exists() else None
    if previo != actual:
        try:
            vs.delete_collection()
        except Exception:  # noqa: BLE001
            pass
        vs = Chroma(collection_name=_COLECCION, embedding_function=_embeddings(),
                    persist_directory=str(CHROMA_DIR),
                    client_settings=Settings(anonymized_telemetry=False, is_persistent=True,
                                             persist_directory=str(CHROMA_DIR)))
        vs.add_documents(_fragmentos())
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        marca.write_text(json.dumps({"hash": actual}))
    return vs


@lru_cache(maxsize=256)
def _buscar_cache(pregunta: str, k: int) -> tuple:
    res = vectorstore().similarity_search_with_relevance_scores(pregunta, k=k)
    return tuple((d.metadata["fuente"], d.page_content, round(float(s), 3)) for d, s in res)


def _compactar(texto: str, max_chars: int = 420) -> str:
    """Quita el formato markdown de tablas y espacios sobrantes: menos tokens, mismo contenido."""
    lineas = [l.strip(" |") for l in texto.splitlines() if l.strip() and not set(l.strip()) <= set("|-: ")]
    t = " · ".join(l.replace(" | ", ": ", 1).replace(" | ", ", ") for l in lineas)
    return t if len(t) <= max_chars else t[:max_chars].rsplit(" ", 1)[0] + "…"


def buscar(pregunta: str, k: int = 3) -> list[dict]:
    return [{"fuente": f, "texto": _compactar(t)} for f, t, _s in _buscar_cache(pregunta.strip().lower(), k)]


def precalentar() -> None:
    """Carga el modelo de embeddings e índice antes de la demo (evita la espera en la 1.ª consulta)."""
    buscar("descuento por volumen", k=1)
