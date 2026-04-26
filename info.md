# Madrileña Red de Gas

Integración de Home Assistant para lecturas bimestrales de **gas natural** del portal de [Madrileña Red de Gas](https://ov.madrilena.es/) (Madrid).

Funciona con un **bookmarklet** (favorito JavaScript) que pegas en Safari/Chrome/Firefox: tu navegador hace la descarga del HTML ya autenticado y POSTea a HA. Sin scraping desde HA, sin cookies que caducan, sin loop de OTP por email.

- Sensor de **lectura del contador** (m³, monotónico) por instalación.
- Sensores del **último periodo bimestral**: total, ACS (agua caliente sanitaria) y calefacción separados.
- Reparto **diario** del consumo bimestral usando temperatura exterior (HDD) y, opcionalmente, las horas de tus termostatos `climate.*`.
- Estadísticas externas para el **panel de Energía** (3 series: total / ACS / calefacción en m³).
- Entidades opcionales de **coste** (€/kWh) si las activas.

## Cómo empezar

1. Instala vía HACS y reinicia HA.
2. **Ajustes → Dispositivos y servicios → + Añadir integración → Madrileña Red de Gas**.
3. Indica nombre, URL HTTPS de tu HA, número de personas en la vivienda y (opcional) tus `climate.*` de calefacción + el `weather.*` o `sensor.*` de temperatura exterior.
4. Aparece una notificación con el bookmarklet — pégalo en favoritos del navegador.
5. Loguéate en la Oficina Virtual y pulsa el favorito. Sensores aparecen al instante.

## Requisitos

- Home Assistant accesible por **HTTPS** desde el navegador que va a pulsar el bookmarklet. Puede ser HTTPS **local** (p. ej. `https://192.168.1.50:8123` con NGINX SSL, o `https://homeassistant.local:8123` con certificado propio) o HTTPS **público** (DuckDNS + Let's Encrypt, Nabu Casa, Cloudflare Tunnel, Tailscale Funnel…). Sin HTTPS el navegador bloquea el `fetch()`.
- Cuenta activa en la Oficina Virtual de Madrileña Red de Gas.

## Hermana

Comparte arquitectura con la integración hermana para agua: [hass-canal-isabel-ii](https://github.com/alnavasa/hass-canal-isabel-ii).
