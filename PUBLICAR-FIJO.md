# 🔗 URL fija con Cloudflare (túnel con nombre + tu dominio)

Para tener una dirección que **no cambie** (ej. `https://fabrica.tudominio.com`),
en vez de la `trycloudflare.com` que cambia cada vez.

> Requisito: haber comprado tu dominio en Cloudflare (o agregado tu dominio a tu
> cuenta de Cloudflare). Costo del dominio: ~$10/año el `.com`. El túnel es gratis.

---

## Parte A — En el navegador (puedes hacerlo desde el CELULAR)
1. Entra a **dash.cloudflare.com** → en el menú, **Zero Trust**.
   - La primera vez te pide elegir un plan: elige **Free** (gratis).
2. **Networks → Tunnels → Create a tunnel** → tipo **Cloudflared** → nómbralo
   `fabrica` → **Save**.
3. Te mostrará un **comando de instalación con un token largo**. Cópialo y guárdalo
   (lo usarás en la PC). Deja esa pantalla abierta.

## Parte B — En tu PC (una sola vez)
1. Abre **PowerShell como Administrador**.
2. Pega el comando que te dio Cloudflare (se ve así:
   `cloudflared service install eyJ......token......`) y Enter.
   - Eso instala el túnel como un **servicio** que arranca solo con Windows.

## Parte C — En el navegador otra vez (celular o PC)
1. En la pantalla del túnel → pestaña **Public Hostname** → **Add a public hostname**:
   - **Subdomain:** `fabrica`
   - **Domain:** tudominio.com
   - **Service / Type:** `HTTP`  →  **URL:** `localhost:8000`
   - **Save**
2. ¡Listo! Tu dirección fija es **https://fabrica.tudominio.com** (no cambia nunca).

---

## Cómo usarla cada día
1. En tu PC, doble clic en **`iniciar.bat`** (solo arranca la app; ya **no**
   necesitas `publicar.bat` ni copiar URLs).
2. En el celular abre **https://fabrica.tudominio.com** → te pide usuario y
   contraseña (los de tu `.env`) → entras.
3. Para apagar: cierra la ventana de la app.

## Notas
- La **contraseña** (`APP_PASSWORD` del `.env`) sigue protegiendo la entrada.
  Mientras la app esté apagada, no se expone nada.
- `publicar.bat` (el túnel rápido con URL que cambia) te sigue sirviendo como
  respaldo si algún día el dominio falla.
- Apagar el túnel del todo: en el dashboard borra el túnel, o en la PC corre
  `cloudflared service uninstall`.
