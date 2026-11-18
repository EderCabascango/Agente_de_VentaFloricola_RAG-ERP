"""Cada test usa su propia base SQLite temporal y un RAG simulado (rápido y sin descargar modelos)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Antes de importar src: la base de pruebas va a una carpeta temporal
_TMP = Path(tempfile.mkdtemp(prefix="erp_test_"))
os.environ["ERP_DB_PATH"] = str(_TMP / "erp_test.db")
os.environ["LLM_FALLBACK_PROVIDER"] = ""
os.environ["ERP_URL"] = ""      # los tests nunca tocan el ERP de la nube
os.environ["ERP_TOKEN"] = ""
os.environ["VARIEDADES_ARCHIVO"] = ""   # los tests usan el catálogo corto de demostración, no el del cliente

import pytest  # noqa: E402

from seed import reset_db  # noqa: E402
from src import config  # noqa: E402

FRAGMENTOS_FAKE = {
    "miami": [{"fuente": "tiempos_envio.md › Tiempos de entrega por destino",
               "texto": "Miami y Estados Unidos: 48 a 72 horas", "relevancia": 0.6}],
    "descuento": [{"fuente": "descuentos_volumen.md › Escala de descuentos por línea de pedido",
                   "texto": "5000 a 9999 tallos: 8%", "relevancia": 0.6}],
    "florero": [{"fuente": "cuidado_postcosecha.md › Vida en florero",
                 "texto": "Rosas: 10 a 14 días con agua limpia, cambiada cada 2 días.", "relevancia": 0.6}],
}


@pytest.fixture(autouse=True)
def erp_limpio(monkeypatch):
    reset_db(config.DB_PATH)

    def buscar_fake(pregunta: str, k: int = 3):
        p = pregunta.lower()
        res = []
        for clave, frags in FRAGMENTOS_FAKE.items():
            if clave in p:
                res += frags
        return res or FRAGMENTOS_FAKE["miami"]

    import src.rag
    monkeypatch.setattr(src.rag, "buscar", buscar_fake)
    yield config.DB_PATH


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _interruptor_aislado(tmp_path, monkeypatch):
    """Las pruebas nunca leen el interruptor real del agente (agente_apagado.flag)."""
    import webhook
    monkeypatch.setattr(webhook, "ARCHIVO_APAGADO", tmp_path / "agente_apagado.flag")
