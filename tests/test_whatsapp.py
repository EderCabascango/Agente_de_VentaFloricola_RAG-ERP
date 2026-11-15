"""Capa del canal WhatsApp: formato de mensajes e identificación del cliente por su número."""
from src.erp_client import ERPClient
from src.whatsapp import LIMITE_WHATSAPP, formatear_para_whatsapp, hilo_de, normalizar_numero


def test_negrita_markdown_pasa_a_formato_whatsapp():
    assert formatear_para_whatsapp("Total **$1,235.00** y __8%__") == "Total *$1,235.00* y *8%*"


def test_titulos_vinetas_y_links():
    texto = "## Resumen\n- Rosa Freedom\n* Gypsophila\n[Catálogo](https://ejemplo.com/catalogo)"
    assert formatear_para_whatsapp(texto) == "*Resumen*\n• Rosa Freedom\n• Gypsophila\nCatálogo: https://ejemplo.com/catalogo"


def test_tablas_se_aplanan():
    texto = "| Finca | Tallos |\n|---|---|\n| CAY | 4000 |"
    assert formatear_para_whatsapp(texto) == "Finca · Tallos\nCAY · 4000"


def test_respeta_el_limite_de_la_api():
    assert len(formatear_para_whatsapp("rosa " * 2000)) <= LIMITE_WHATSAPP


def test_numero_a_hilo():
    assert normalizar_numero("+1 (305) 555-0142") == "13055550142"
    assert hilo_de("+1 305 555 0142") == hilo_de("13055550142") == "wa-13055550142"


def test_cliente_se_identifica_por_whatsapp():
    erp = ERPClient()
    assert erp.get_cliente("+33 6 55 50 01 23")["id"] == "C002"
    assert erp.get_cliente("+00 000 000 0000") is None


def test_negrita_anidada_se_aplana_y_las_separadas_se_respetan():
    assert formatear_para_whatsapp("*El descuento es del **8 %**.*") == "*El descuento es del 8 %.*"
    assert formatear_para_whatsapp("*Total:* $2,300 y *plazo:* 48 a 72 horas") == "*Total:* $2,300 y *plazo:* 48 a 72 horas"
