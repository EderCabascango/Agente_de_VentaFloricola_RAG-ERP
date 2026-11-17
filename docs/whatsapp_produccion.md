# Conectar el agente a WhatsApp (piloto con número propio)

El agente corre en tu laptop y Meta le envía los mensajes por un túnel de ngrok. El ERP sigue siendo sintético (`erp.db`); el chat es WhatsApp de verdad.

```
Cliente (WhatsApp) ──► Meta Cloud API ──► https://TU-DOMINIO.ngrok-free.app/webhook ──► ngrok ──► laptop :8000
                                                                                          │
                       ◄──────────────── respuesta / botones al supervisor ◄──── webhook.py + agente (LangGraph)
```

Tiempo estimado: 1 hora de configuración, más la espera de Meta para verificar el número y aprobar el nombre visible (de horas a unos días).

> **Importante sobre tu número:** al registrarlo en la API deja de funcionar en la app de WhatsApp de tu celular, y su historial de chats se pierde. Para chatear con el bot como cliente necesitarás **otro número** (otro celular o el de un tester).
>
> **Mientras Meta verifica tu número**, puedes hacer todo el flujo con el número de prueba gratuito de Meta (paso 2). Cuando tu número esté listo solo cambias `WHATSAPP_PHONE_NUMBER_ID` en `.env`.

## 1. Cuenta de Meta Business

1. Entra a [business.facebook.com](https://business.facebook.com) y crea un portafolio comercial (nombre de la empresa, tu correo).
2. Opcional para el piloto, necesario para crecer: **verificación del negocio** (Configuración → Centro de seguridad). Sin verificar puedes atender clientes, con límites de volumen.

## 2. App de Meta con WhatsApp

1. Entra a [developers.facebook.com/apps](https://developers.facebook.com/apps) → **Crear app** → caso de uso **"Conectarte con clientes a través de WhatsApp"** → elige tu portafolio comercial.
2. En **WhatsApp → Configuración de la API** verás un **número de prueba**, su **Phone number ID** y un **token temporal** (dura 24 horas). Con esto ya puedes probar:
   - Agrega en "Para" el número desde el que escribirás como cliente (máximo 5 números de prueba) y verifícalo con el código que te llega.
3. En **Configuración de la app → Básica** copia el **App Secret** (clave secreta de la app).

## 3. Registrar tu número como número del bot

1. Si tu número está en la app de WhatsApp: en el celular, **Ajustes → Cuenta → Eliminar cuenta** (o, si es WhatsApp Business app, usa la migración que ofrece Meta). Respalda antes lo que quieras conservar.
2. En **WhatsApp → Configuración de la API → Agregar número de teléfono**:
   - Nombre visible: el de la florícola (Meta lo revisa; debe coincidir con la marca).
   - Categoría: Agricultura / Comercio.
   - Verifica el número con el código por SMS o llamada.
3. **Activa el número en la Cloud API** con un PIN de 6 dígitos que tú eliges (verificación en dos pasos). Reemplaza los valores y ejecútalo una vez:

```bash
curl -X POST "https://graph.facebook.com/v24.0/PHONE_NUMBER_ID/register" -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" -d "{\"messaging_product\":\"whatsapp\",\"pin\":\"123456\"}"
```

4. Copia el **Phone number ID** de tu número (no es el número de teléfono, es un ID largo).

## 4. Token permanente

El token temporal vence en 24 horas. Para el piloto crea uno permanente:

1. [business.facebook.com/settings](https://business.facebook.com/settings) → **Usuarios del sistema** → Agregar → rol **Administrador**.
2. **Asignar activos**: tu app (control total) y tu cuenta de WhatsApp (control total).
3. **Generar token** → tu app → permisos `whatsapp_business_messaging` y `whatsapp_business_management` → vencimiento "Nunca".
4. Guárdalo solo en `.env`. Si se filtra, revócalo desde el mismo lugar.

## 5. Variables en `.env`

```ini
WHATSAPP_TOKEN=el-token-permanente
WHATSAPP_PHONE_NUMBER_ID=el-id-de-tu-numero
WHATSAPP_VERIFY_TOKEN=una-frase-que-inventas
WHATSAPP_APP_SECRET=el-app-secret
WHATSAPP_SUPERVISORES=+593 98 765 4321
WHATSAPP_CLIENTES=+593 99 111 2233=C001
```

- **Supervisores:** números que reciben los pedidos con botones Aprobar / Rechazar. Solo ellos pueden aprobar.
- **Clientes:** el ERP es sintético, así que asigna a cada tester un cliente del ERP (`C001` Bloom Imports, `C003` Tulp & Roos, etc., ver `seed.py`). Así hereda su crédito y su descuento.
- **Cualquier número recibe atención.** Un número que no está en el ERP se registra solo como cliente nuevo (sin descuento propio y con un crédito de prueba), y su pedido igual pasa por el validador y la aprobación del supervisor, que lo ve marcado como "Cliente nuevo". `WHATSAPP_REGISTRAR_NUEVOS=0` lo desactiva: el desconocido recibe "un asesor le escribirá" y queda en la auditoría.
- `WHATSAPP_CLIENTE_POR_DEFECTO=C001` (opcional) atiende a los desconocidos como ese cliente del ERP, en vez de registrarlos. No lo recomiendo: todos compartirían su crédito y su descuento.
- `WHATSAPP_MAX_MENSAJES_MINUTO=15` limita los mensajes por número para proteger la cuota del LLM.

## 6. Túnel con ngrok (URL fija y gratis)

1. Crea una cuenta en [ngrok.com](https://ngrok.com) y copia tu authtoken. Una sola vez:

```bash
ngrok config add-authtoken TU_AUTHTOKEN
```

2. En el panel de ngrok → **Domains** → reclama tu **dominio estático gratuito** (por ejemplo `rosaflor-demo.ngrok-free.app`).

## 7. Arrancar

En una terminal, el webhook (o doble clic en `whatsapp.bat`):

```bash
python -m uvicorn webhook:crear_app --factory --host 127.0.0.1 --port 8000
```

En otra terminal, el túnel:

```bash
ngrok http --url=rosaflor-demo.ngrok-free.app 8000
```

Comprueba que responde: abre `https://rosaflor-demo.ngrok-free.app/salud` y debe mostrar `{"ok": true, ...}`.

## 8. Conectar el webhook en Meta

1. **WhatsApp → Configuración → Webhook → Editar**:
   - URL de devolución de llamada: `https://rosaflor-demo.ngrok-free.app/webhook`
   - Token de verificación: el mismo `WHATSAPP_VERIFY_TOKEN`.
   - Meta llama a la URL; si el webhook está corriendo, queda **verificado**.
2. En **Campos del webhook**, suscríbete a **`messages`**.
3. Para recibir mensajes de cualquier número (no solo de los de prueba), pon la app en modo **Activo / Live**: pide una URL de política de privacidad (puede ser una página simple de la florícola).

## 9. Probar

1. Desde el celular del tester escribe al número del bot: "¿Qué descuento aplica para 5000 tallos de rosa Freedom roja de 60 cm?"
2. Pide un pedido: "Queremos pedir 2000 tallos de rosa Freedom de 60 cm y 1000 de rosa Explorer de 60 cm, destino Miami".
3. El supervisor recibe el resumen con **✅ Aprobar** / **✖️ Rechazar**. Al aprobar, el cliente recibe el número de pedido.
4. El pedido aparece en el panel **ERP** de la demo de Streamlit (`demo.bat`), que usa la misma base.

## Reglas de WhatsApp que conviene saber

| Regla | Qué significa para el piloto |
|---|---|
| **Ventana de 24 horas** | El bot solo puede escribir texto libre a quien le escribió en las últimas 24 h. Los clientes siempre escriben primero, así que no hay problema. **El supervisor debe escribirle al bot (por ejemplo "hola") al empezar el día**; si no, el aviso con botones falla, queda en la auditoría (`AVISO_SUPERVISOR_FALLIDO`) y el pedido se puede aprobar en el panel de Streamlit. Para producción se crea una plantilla de "pedido pendiente". |
| **Costo** | Responder a mensajes que inicia el cliente, dentro de las 24 h, no tiene costo en la API. Las plantillas (mensajes que inicia el negocio) se cobran por mensaje. |
| **Reintentos de Meta** | Si el webhook no responde 200 a tiempo, Meta reenvía el mensaje. El webhook responde de inmediato y procesa en segundo plano, e ignora mensajes repetidos. |
| **Solo texto** | Audios, fotos y stickers reciben "por ahora solo leo texto". |

## Seguridad del canal

- Cada POST se verifica con la firma `X-Hub-Signature-256` y el App Secret: nadie más puede inyectar mensajes al webhook.
- El webhook escucha solo en `127.0.0.1`; desde internet solo se llega por el túnel.
- Solo los números en `WHATSAPP_SUPERVISORES` pueden aprobar, y un botón tocado dos veces no duplica el pedido.
- Las conversaciones se guardan en `conversaciones.db` (sobreviven a reinicios). Está en `.gitignore`, igual que `.env`.

## Si algo falla

| Síntoma | Causa probable |
|---|---|
| Meta no verifica la URL del webhook | El webhook o ngrok no están corriendo, o el verify token no coincide. Revisa `/salud`. |
| Los mensajes llegan (se ve en la terminal) pero no hay respuesta | Token vencido o Phone number ID equivocado: la terminal muestra el error de la Graph API. |
| El supervisor no recibe los botones | No le escribió al bot en las últimas 24 h (error 131047). |
| Solo responde a algunos números | La app sigue en modo desarrollo: solo atiende a los números de prueba. Pásala a Live. |
| El primer mensaje tarda ~20 s | Carga del modelo de embeddings al arrancar. Los siguientes tardan 1 a 4 s. |
