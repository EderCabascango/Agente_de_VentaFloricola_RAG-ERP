"""El validador determinista: la barrera que evita pedidos erróneos en el ERP."""
from src.validators import descuento_por_volumen, validar_borrador

CLIENTE = "C001"  # Bloom Imports LLC: crédito disponible 8.000, descuento máx 10%


def _check(res, nombre, sku=None):
    return next(c for c in res.checks if c.nombre == nombre and (sku is None or c.sku == sku))


def test_pedido_valido_pasa_y_aplica_descuento_de_politica():
    r = validar_borrador({"cliente": CLIENTE, "destino": "Miami",
                          "items": [{"sku": "ROS-002", "cantidad": 2000}, {"sku": "ROS-004", "cantidad": 1000}]})
    assert r.aprobado
    assert r.total == 817.00  # 2000*0.28*0.95 + 1000*0.30*0.95
    assert {l.bodega for l in r.lineas} == {"CAY"}
    assert all(c.ok for c in r.checks)


def test_sku_inexistente_bloquea():
    r = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "XXX-999", "cantidad": 25}]})
    assert not r.aprobado
    assert not _check(r, "sku_existe").ok


def test_cantidad_cero_o_negativa_o_decimal_bloquea():
    for cant in (0, -25, 2.5):
        r = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "ROS-002", "cantidad": cant}]})
        assert not r.aprobado, cant
        assert not _check(r, "cantidad_valida").ok


def test_bonche_incompleto_bloquea_y_sugiere_cantidades():
    r = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "ROS-002", "cantidad": 110}]})
    assert not r.aprobado
    assert not _check(r, "bonche_completo").ok
    assert any("100 o 125" in a for a in r.alternativas)


def test_stock_insuficiente_bloquea_y_sugiere_fincas():
    r = validar_borrador({"cliente": CLIENTE, "destino": "Miami",
                          "items": [{"sku": "ROS-003", "cantidad": 20000}]})
    assert not r.aprobado
    chk = _check(r, "stock_suficiente")
    assert not chk.ok and "4000" in chk.detalle and "TAB" in chk.detalle
    assert any("máximo 4000" in a for a in r.alternativas)


def test_finca_pedida_por_nombre():
    r = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "ROS-002", "cantidad": 100, "bodega": "Tabacundo"}]})
    assert r.aprobado and r.lineas[0].bodega == "TAB"


def test_precio_siempre_sale_del_erp():
    r = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "ROS-002", "cantidad": 100, "precio_unitario": 0.10}]})
    assert r.aprobado
    assert r.lineas[0].precio_unitario == 0.28
    assert r.correcciones and "reemplazado" in r.correcciones[0]


def test_descuento_excesivo_bloquea():
    r = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "ROS-002", "cantidad": 6000, "descuento_pct": 25}]})
    assert not r.aprobado
    assert not _check(r, "descuento_permitido").ok
    assert any("8%" in a for a in r.alternativas)  # 6000 tallos → política 8%


def test_descuento_limitado_por_cliente():
    # 10000+ tallos da 12% por volumen, pero el cliente tiene tope 10%
    r = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "ROS-002", "cantidad": 10000, "descuento_pct": 12}]})
    assert not r.aprobado
    r2 = validar_borrador({"cliente": CLIENTE, "items": [{"sku": "ROS-002", "cantidad": 10000}]})
    assert r2.lineas[0].descuento_pct == 10


def test_credito_insuficiente_bloquea():
    # Fleurs du Marché: crédito disponible 800; 4000 tallos × 0.28 × 0.95 = 1064
    r = validar_borrador({"cliente": "C002", "items": [{"sku": "ROS-002", "cantidad": 4000}]})
    assert not r.aprobado
    assert not _check(r, "credito_disponible").ok


def test_cliente_inexistente_bloquea():
    r = validar_borrador({"cliente": "9999999999001", "items": [{"sku": "ROS-002", "cantidad": 25}]})
    assert not r.aprobado and r.motivos


def test_formato_invalido_bloquea():
    r = validar_borrador({"items": "no es lista"})
    assert not r.aprobado


def test_escala_de_descuentos():
    assert [descuento_por_volumen(q) for q in (25, 999, 1000, 4999, 5000, 9999, 10000)] == [0, 0, 5, 5, 8, 8, 12]
