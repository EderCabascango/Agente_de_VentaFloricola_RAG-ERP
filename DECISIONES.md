# Decisiones de ingeniería

Este agente parte de un agente B2B de pedidos que ya había pasado por muchas pruebas con LLM reales. Aquí están las decisiones que se heredaron, las que se agregaron para la florícola y las fallas que aparecieron al adaptarlo.

Casi todas siguen un mismo principio:

> **Lo que se puede decidir con una regla, no lo decide el LLM.** El modelo redacta y elige herramientas; el código valida, calcula, resuelve productos y decide los pasos críticos.

## Resultado

| | Valor (2 pasadas seguidas con Groq `gpt-oss-120b`) |
|---|---|
| Escenarios limpios | **6 de 6, dos veces seguidas** |
| Tiempo por escenario | **0,5 a 3,7 s** |
| Tokens por escenario | **900 a 3.200** |
| Llamadas al LLM por escenario | **0 a 3** |

## Decisiones nuevas para la florícola

| # | Decisión | Por qué |
|---|---|---|
| 1 | **Bonches completos de 25 tallos** como regla del validador, con alternativas ya calculadas ("pedir 100 o 125 tallos"). | En flor de corte no se vende un tallo suelto. Si el cliente pide 110, el código lo detecta; el LLM no tiene que saberlo. |
| 2 | **Fincas en lugar de bodegas** (CAY, TAB, LAT). Se despacha desde Cayambe y, si no alcanza, desde la finca con más stock. El cliente puede pedir una finca por nombre. | El precio es FOB Quito: el destino (Miami, Ámsterdam) no decide la finca, a diferencia de una distribuidora local. |
| 3 | **El número de WhatsApp identifica al cliente.** El canal pone el cliente en el estado (`cliente_canal`) y el prompt le dice al LLM qué ID usar. | En WhatsApp nadie escribe su RUC en cada mensaje. Que el LLM lo pregunte o lo adivine era un riesgo: lo resuelve el canal, no el modelo. |
| 4 | **Formato del canal en código** (`src/whatsapp.py`): `**negrita**` → `*negrita*`, tablas aplanadas, títulos y links convertidos, límite de 4.096 caracteres. | El LLM escribe markdown aunque se le pida otra cosa. Convertirlo con reglas es gratis y seguro. El grounding revisa el texto original, así que el formato no afecta la verificación. |
| 5 | **Chat que escapa todo el texto** (`html.escape`) antes de dibujar las burbujas. | El chat estilo WhatsApp usa HTML. Sin escapar, un cliente podría inyectar HTML en la pantalla del supervisor. |
| 6 | **Escenario de cuidado de la flor** (solo RAG). | Es la pregunta más común después de la venta, y muestra el RAG con cita sin tocar el ERP. |

## Fallas encontradas al adaptarlo y cómo se corrigieron

| # | Falla observada | Cómo se detectó | Corrección |
|---|---|---|---|
| 7 | **El LLM sumó por su cuenta el stock de las otras fincas** ("3.500 en otras fincas") al explicar un rechazo. La cifra era correcta, pero no estaba en ningún dato. | El grounding la rechazó y la respuesta terminó escalando a un asesor. | El validador entrega ese total ya calculado: "TAB: 2500, LAT: 1000 (3500 tallos entre las otras fincas)". |
| 8 | **El LLM inventó una tercera alternativa** "con el descuento máximo" y calculó un total ($3.825) que nadie le dio. | Grounding. | Si falta stock, el validador ya no ofrece la alternativa suelta de "descuento máximo": las alternativas de stock traen su descuento correcto. |
| 9 | El calificador automático marcó como error "10‑14 días" porque gpt-oss usa un guion no separable (U+2011). | Revisión de la evidencia. | El calificador acepta cualquier guion Unicode. Es el mismo problema que ya se había resuelto para los SKU ("ROS‑001"). |
| 10 | La validación decía "$0.50 por tallos". | Prueba visual de la UI. | El texto usa "por tallo". |

## Decisiones heredadas del agente base

### Precisión: el agente no puede inventar

| Decisión | Motivo |
|---|---|
| El **validador entrega las alternativas ya calculadas** y el prompt dice "no calcules ni muestres operaciones". | El LLM hacía cuentas, y a veces mal. |
| El campo del borrador se llama `producto`, no `sku`, y el **código resuelve el nombre al SKU**. | El nombre `sku` invitaba al LLM a inventar códigos. |
| El grounding también **verifica citas**: solo valen archivos que devolvió `buscar_politicas`, con `[]` o `【】`. | El LLM citaba archivos que no había consultado. |
| Tras un rechazo del validador, el agente **responde sin tools** y con los datos como texto. | Al explicar un rechazo inventaba cifras. |
| Se normalizan espacios finos de miles ("2 300") y guiones Unicode. | gpt-oss los usa y daban falsos positivos. |

### Operaciones: el pedido siempre pasa por el validador

| Decisión | Motivo |
|---|---|
| Una **regla detecta la intención** (pedido / cotización) y el grafo **obliga** a usar la tool que corresponde en la primera llamada. | El LLM decidía solo que un pedido "no procedía" y lo trataba como cotización. |
| En la llamada forzada se expone **solo esa tool**. | Groq no restringe la generación con `tool_choice` si hay varias tools. |
| Descuento 0 o vacío = se aplica la política. | El LLM enviaba 0% y el pedido salía sin el descuento al que el cliente tiene derecho. |
| El validador **siempre** usa el precio del ERP y registra la corrección. | El LLM podía proponer otro precio. |

### Costo y velocidad (Groq gratis: 8.000 tokens/minuto)

| Decisión | Motivo |
|---|---|
| Tools que aceptan el **nombre del producto**, descripciones compactas y RAG con 2 fragmentos sin formato de tabla. | Con tools encadenadas se gastaban 11.000 a 15.000 tokens por escenario. |
| **Historial condensado**: de los turnos previos solo van la pregunta y la respuesta final. | El historial completo viajaba en cada llamada. |
| Precarga del modelo de embeddings después de dibujar la UI. | La primera búsqueda tardaba 20 s. |
| La última iteración se hace sin tools. | El límite de iteraciones cortaba sin responder al cliente. |

### Disponibilidad de proveedores

| Decisión | Motivo |
|---|---|
| **Groq principal y Gemini como respaldo automático**, con modelos configurables en `.env`. | Gemini gratis tiene 20 solicitudes/día y tarda 10 a 30 s por llamada. |
| Se respeta el "try again in Xs" de la API; si supera 3 s y hay respaldo, la llamada pasa al respaldo. La cuota diaria agotada no se reintenta. | Los reintentos a ciegas alargaban la espera sin resultado. |

## Cómo verificarlo

```bash
python scripts/run_scenarios.py --sin-respaldo --pasadas 2
```

Corre los 6 escenarios dos veces, como en la presentación (una sola conversación por pasada, con pausa de narración entre escenarios), y califica cada uno automáticamente: citas inventadas, cifras sin respaldo, tool equivocada, "decidió solo que no procede", ERP que no cambia, etc.

```bash
pytest -q
```

71 tests, incluidos los de estas decisiones (`tests/test_decisiones.py`, `tests/test_whatsapp.py`).
