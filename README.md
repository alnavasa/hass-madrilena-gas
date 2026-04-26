# Madrileña Red de Gas — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square)](https://github.com/hacs/integration)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=alnavasa&repository=hass-madrilena-gas&category=integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)

Integración de Home Assistant que importa las **lecturas bimestrales de consumo de gas natural** del portal de [Madrileña Red de Gas](https://ov.madrilena.es/) (Madrid) al **panel de Energía → Gas** y a sensores nativos por contador, **separando ACS (agua caliente) de calefacción** y repartiendo el consumo bimestral en una curva diaria.

## ¿Cómo funciona?

El portal `ov.madrilena.es` tiene **2FA por email** en cada login y la cookie de sesión Laravel **caduca rápido** sin ofrecer "recuérdame". Cualquier scraper desde HA acabaría pidiendo un OTP por email cada poco. La integración usa el truco de la integración hermana de Canal de Isabel II:

> **Tu navegador, ya autenticado, descarga las páginas de `/consumos` mediante un *bookmarklet* (favorito JavaScript).** HA publica un endpoint HTTP donde recibe el HTML; el coordinador parsea, distribuye los m³ del bimestre por día, y empuja todo al panel de Energía.

```
┌──────────────────────┐       click       ┌───────────────────────────────┐
│ Tu navegador (logado │ ───────────────►  │ ov.madrilena.es                │
│ en la OV de gas)     │                   │ - tabla bimestral              │
└──────────┬───────────┘                   └───────────────────────────────┘
           │ POST HTML (cookies del usuario, mismo navegador)
           ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Home Assistant /api/madrilena_gas/ingest/<entry_id>                    │
│ - valida Bearer token                                                  │
│ - parsea HTML → list[Reading]                                          │
│ - reparto diario: ACS (m³/persona·día) + calefacción (HDD × climates) │
│ - persiste en .storage + empuja estadísticas externas → panel Energía │
└────────────────────────────────────────────────────────────────────────┘
```

Ventajas del modelo:

- **Sin OTP recurrente.** El usuario hace login en la web una vez al mes (o cuando le apetezca) y pulsa el bookmarklet. HA no toca la web jamás.
- **Sin scraping permanente.** Cero procesos en background, cero RAM gastada entre clicks.
- **Sin cookies en HA.** La cookie de sesión es HttpOnly y nunca sale del navegador del usuario.
- **Funciona en cualquier variante de HA** — Container, Core, OS y Supervised.
- Una **URL de bookmarklet** que pegas en favoritos de Safari/Chrome (PC, Mac, iOS, Android) y pulsas cuando quieras refrescar.

## ¿Qué resuelve para el usuario?

Madrileña entrega **una lectura cada ~60 días**, así que el panel de Energía nativo de HA se queda con un único punto bimestral — inútil para entender consumo. Esta integración:

1. **Distribuye el bimestre por días** usando temperatura exterior (HDD base 18 °C) y, si se lo pides, las horas que tus `climate.*` (o `binary_sensor.*` de demanda — útil en Airzone) estuvieron pidiendo calor. Días fríos consumen más, días templados menos.
2. **Separa ACS de calefacción.** Mira tus periodos de verano (sin calefacción) y deduce los m³/persona·día de agua caliente. Resta esa baseline de cada periodo invernal y reparte el resto como heating.
3. **Empuja 3 series cumulativas** al recorder: `total`, `acs`, `heating` (en m³). El panel de Energía las ve como cualquier otro contador.
4. **Backfill de temperatura** vía Open-Meteo Archive (gratis, sin API key) para los días que tu HA aún no tenía instalado.

## Requisitos

- Home Assistant accesible por **HTTPS** desde el **navegador** donde vayas a pulsar el bookmarklet. HTTPS es obligatorio porque la página del portal (`https://ov.madrilena.es`) es HTTPS y los navegadores bloquean `fetch()` HTTP por *mixed content*. **Puede ser HTTPS local o HTTPS público** — ambos funcionan, elige según tu caso:

  | Modo | URL típica | Funciona cuando | Privacidad |
  |------|------------|-----------------|-----------|
  | **HTTPS local** | `https://192.168.1.50:8123`, `https://homeassistant.local:8123` | El dispositivo desde el que pulsas el bookmarklet está en la LAN de HA (cable, WiFi doméstico, VPN al router). | Máxima — HA no sale a internet. |
  | **HTTPS público** | `https://micasa.duckdns.org`, `https://abc123.ui.nabu.casa`, `https://hass.midominio.com` | Desde cualquier sitio (4G, oficina, otro WiFi). | Menor — HA expuesto a internet. |

- Cuenta activa en la Oficina Virtual de Madrileña Red de Gas.
- Navegador con favoritos (cualquiera moderno).
- HACS (recomendado, para auto-update).

> **Lo que NO funciona:** `http://192.168.x.x:8123`, `http://homeassistant.local:8123`, o cualquier URL HTTP sin certificado. Es cosa del navegador, no de la integración.

## Instalación

### 1. Instalar la integración (HACS)

1. HACS → menú `⋮` → **Repositorios personalizados**.
2. URL `https://github.com/alnavasa/hass-madrilena-gas`, categoría **Integration**.
3. Cierra el modal, busca **"Madrileña Red de Gas"** → **Descargar** → última versión.
4. **Reinicia Home Assistant**.

Manual: copia `custom_components/madrilena_gas/` a `<config>/custom_components/madrilena_gas/` y reinicia.

### 2. Añadir la integración

**Ajustes → Dispositivos y servicios → + Añadir integración → *Madrileña Red de Gas***.

El asistente pide:

| Campo | Qué poner | Ejemplo |
|---|---|---|
| **Nombre instalación** | Etiqueta libre. Aparece como nombre del dispositivo y prefijo de los sensores. | `Casa principal` |
| **URL de tu HA** | URL HTTPS (local o pública). Default: `external_url` / `internal_url` configurada en HA. | `https://192.168.1.50:8123` o `https://micasa.duckdns.org` |
| **Personas** | Cuántas personas viven en la casa. Sirve para estimar el consumo de agua caliente sanitaria. | `4` |
| **Calefacción** | `climate.*` (cuenta cuando `hvac_action == heating`) y/o `binary_sensor.*` (cuenta cuando `on`). Multi-select. En setups Airzone con suelo radiante por gas + aire eléctrico, los `binary_sensor.*demanda_de_suelo*` aíslan el gas mejor que los climates (que también marcan `heating` mientras el aire eléctrico está empujando). Vacío = solo HDD. | `climate.salon`, `binary_sensor.despacho_demanda_de_suelo` |
| **Sensor de temperatura exterior** | Cualquier `sensor.*` o `weather.*` (p. ej. `weather.met_no` por defecto). Si lo dejas vacío, se usa Open-Meteo Archive siempre. | `weather.met_no` |
| **Temperatura base HDD** | 18 °C es el estándar para España (REE / IDAE). Solo bájala si tienes la casa muy fría. | `18.0` |
| **Calcular precio (€)** | Casilla opt-in. Si la marcas, aparece un paso extra que pide `kWh/m³` (PCS, ~11.70 en España) y `€/kWh` (de tu factura). | (sin marcar) |

Si has elegido al menos un `climate.*` o `binary_sensor.*`, aparece un **segundo paso "Metros² por zona"** donde indicas los m² de cada uno. Si lo dejas todo en `1`, el reparto cuenta cuántas zonas estuvieron pidiendo calor cada hora (mejor que la v0.1.1 que sólo miraba "alguna zona sí/no", pero sin distinguir tamaños). Si pones m² reales, el reparto es proporcional al gas que realmente quema cada zona — recomendado para Airzone u otros multi-zona desiguales (un salón de 30 m² consume ~6× más que un baño de 5 m²).

Al pulsar **Enviar**:

1. Se crea la entry y se genera un **token único** (192 bits, `secrets.token_hex(24)`).
2. Aparece un modal corto "Éxito" → pulsa **Terminar**.
3. Inmediatamente se publica una **notificación persistente** (campana de la barra lateral) con el bookmarklet listo para copiar.
4. Los sensores **aún no existen** — se crean en el primer POST exitoso del bookmarklet. El dispositivo aparece vacío hasta entonces.

> **Si cerraste la notificación sin querer**: ejecuta la acción
> `madrilena_gas.show_bookmarklet` desde **Herramientas para desarrolladores →
> Acciones** y la notificación vuelve a aparecer con el mismo contenido.

### 3. Pegar el bookmarklet en el navegador

1. Pulsa la campana en la barra lateral de HA y abre la notificación **"Bookmarklet listo"**.
2. Pulsa **Abrir página de instalación** dentro de la notificación. Se abre una página HTML servida por HA con el bookmarklet listo para arrastrar / copiar.
3. La página te ofrece **dos formas** de instalar el favorito:

   | Cómo | Cuándo | Qué hacer |
   |---|---|---|
   | **Madrileña → HA** (enlace estilo botón) | Escritorio (Safari Mac, Chrome, Firefox, Edge) | **Arrástralo** a la barra de favoritos. *No lo pulses* — un click suelto bloquea la ejecución (no tiene sentido en HA, sin sesión de Madrileña). |
   | **Copiar bookmarklet** (botón) | Móvil (iOS Safari, Chrome Android) y cualquier navegador | Copia al portapapeles. Crea un favorito cualquiera, edita su URL y pega. |

### 4. Pulsar el bookmarklet desde el portal

1. Abre [ov.madrilena.es](https://ov.madrilena.es).
2. Login con DNI + contraseña + el código que te llega por email (2FA).
3. Entra en **Histórico de lecturas**.
4. Pulsa el favorito. Verás un alert con el resumen.
5. Vuelve a HA — los sensores y la estadística para el panel de Energía se rellenan solos.

## Entidades que crea

Una vez vinculado el contador (primer POST), por cada instalación aparecen:

| Entidad | Tipo | Notas |
|---|---|---|
| `sensor.<nombre>_lectura_del_contador` | total_increasing, m³ | La cifra que casa con la factura. |
| `sensor.<nombre>_ultima_lectura` | timestamp | Fecha de la última lectura del portal. |
| `sensor.<nombre>_tipo_ultima_lectura` | string | `Real`, `Estimada`, `Revisada`, `Facilitada`. |
| `sensor.<nombre>_ultimo_periodo_total` | total, m³ | Total bimestral del último periodo cerrado. |
| `sensor.<nombre>_ultimo_periodo_acs` | total, m³ | Parte ACS del último bimestre. |
| `sensor.<nombre>_ultimo_periodo_calefaccion` | total, m³ | Parte calefacción del último bimestre. |
| `sensor.<nombre>_baseline_acs` | measurement, m³/persona·día | Diagnóstico — la cifra deducida de tus veranos. |
| `sensor.<nombre>_ultima_actualizacion` | timestamp | Cuándo el bookmarklet POSTeó por última vez. |
| `sensor.<nombre>_dias_desde_ultima_lectura` | measurement, días | Recordatorio de cuándo refrescar. |

Adicionalmente, **3 series de estadísticas externas** que el panel de Energía consume directamente:

- `madrilena_gas:total_<meter>`
- `madrilena_gas:acs_<meter>`
- `madrilena_gas:heating_<meter>`

## Conectar al panel de Energía

**Ajustes → Tableros → Energía → Añadir consumo de gas:**

| Campo | Qué meter |
|---|---|
| **Consumo de gas** | Estadística externa **`madrilena_gas:total_<meter>`** (m³). El `<meter>` es el número del contador que ves en tu factura. |
| **Caudal de gas** | Vacío. Madrileña sólo da factura bimensual; no hay caudal en tiempo real. |
| **Costes** | Marca **"Usar un precio estático"** y mete tu **€/m³** = `kwh_per_m3 × price_eur_kwh` (ejemplo: 10.541 × 0.0870 = **0.917 €/m³**). |

> Si activaste el bloque de coste en la configuración de la integración, los valores `kwh_per_m3` y `price_eur_kwh` están en tu factura (apartado *Detalle de la facturación*).

## Configuración post-install

**Ajustes → Dispositivos y servicios → Madrileña Red de Gas → Configurar.**

Puedes editar:

- Personas en la vivienda (recalcula ACS hacia atrás).
- Climates / sensor exterior / HDD base.
- **Metros² por zona** (segundo paso, sólo si hay al menos un `climate.*` o `binary_sensor.*` marcado). Pondera el reparto entre zonas — deja `1` en todas para reparto por cuenta de zonas.
- Override manual del baseline ACS (en m³/persona·día), si tu uso es atípico.
- Activar/desactivar entidades de coste y editar `kWh/m³` y `€/kWh`.

Los cambios se aplican en el siguiente refresco del coordinador (1 h o cuando llegue una lectura nueva).

## Acciones (servicios) disponibles

| Acción | Descripción |
|---|---|
| `madrilena_gas.refresh` | Reprocesa el cache local. **No** descarga del portal — para eso pulsa el bookmarklet. |
| `madrilena_gas.show_bookmarklet` | Vuelve a mostrar la notificación con el bookmarklet — útil si la cerraste. |

## Arquitectura

Documentación técnica en [DESIGN.md](DESIGN.md).

## Hermana

Comparte arquitectura (bookmarklet, ingest endpoint, storage layout) con la integración hermana para agua de Canal de Isabel II: [hass-canal-isabel-ii](https://github.com/alnavasa/hass-canal-isabel-ii).

## Licencia

MIT — ver [LICENSE](LICENSE).
