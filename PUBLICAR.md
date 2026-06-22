# 🌐 Publicar la Fábrica en internet (túnel Cloudflare)

Para entrar desde el **celular o la laptop** a tu app, que sigue corriendo en tu PC.
La PC debe estar **encendida y con la app publicada** mientras la uses.

## Preparación (una sola vez)

1. **Ponle contraseña.** Abre el archivo `.env` y escribe una contraseña:
   ```
   APP_USER=admin
   APP_PASSWORD=loquetuquieras
   ```
   > Sin contraseña, cualquiera con el link podría entrar y gastar tus créditos.

2. **Instala cloudflared.** Abre PowerShell y pega:
   ```
   winget install --id Cloudflare.cloudflared
   ```
   (Si no tienes `winget`, descarga `cloudflared.exe` y déjalo en esta carpeta.)

## Cada vez que quieras publicarla

1. **Doble clic en `publicar.bat`** (no abras la app por separado: este archivo
   la arranca y abre el túnel).
2. Aparecerá una línea como:
   ```
   https://algo-al-azar.trycloudflare.com
   ```
   **Copia esa dirección** y ábrela en tu celular o laptop.
3. Te pedirá **usuario y contraseña** (los de tu `.env`). Entras y listo:
   generas guiones y subes videos desde ahí.

## Apagar
Cierra la ventana del túnel **y** la ventana de la app.

## Notas
- La dirección `trycloudflare.com` **cambia cada vez** que la abres. Para tener
  una dirección fija (y que no haya que copiarla cada vez) se configura un
  "túnel con nombre" + un dominio propio — te ayudo cuando quieras.
- Subir videos desde el celular puede tardar según tu internet.
- Más adelante, si quieres que esté **siempre encendida** sin depender de tu PC,
  migramos a la nube (Render/Railway). Ese es el siguiente paso.
