# 🚀 Despliegue en EasyPanel — `anuncios.datibot.lat`

Repositorio: **https://github.com/danielirris/atribucion-ads** (rama `main`)

EasyPanel construye la imagen desde el **Dockerfile** del repo. Cada vez que
hagas `git push` a `main`, vuelve a **Deploy** en EasyPanel para publicar los
cambios (el build usa siempre el último commit de GitHub).

---

## 1. Crear el servicio (App)

1. En tu proyecto de EasyPanel → **+ Service** → **App**.
2. Nombre sugerido: `anuncios`.
3. **Source** → GitHub:
   - Owner/Repo: `danielirris/atribucion-ads`
   - Branch: `main`
   - (Repo público: no requiere credenciales de GitHub.)
4. **Build** → **Dockerfile** (EasyPanel detecta el `Dockerfile` en la raíz).

## 2. Variables de entorno

En la pestaña **Environment** del servicio, agrega (tú pones los valores reales;
nunca se suben al repo):

```
APP_ID=<tu_app_id>
APP_SECRET=<tu_app_secret>
ACCESS_TOKEN=<tu_access_token_de_larga_duracion>
AD_ACCOUNT_ID=act_<tu_id_de_cuenta>
STORAGE_ROOT=/data
POLLING_INTERVAL_SEG=300
```

> El `ACCESS_TOKEN` necesita el permiso **`ads_management`** para leer/modificar
> presupuestos y duplicar anuncios. Usa un token de **larga duración**.

## 3. Volumen persistente (¡IMPORTANTE!)

Para que la base de datos (`datos.db`) y el Excel subido **NO se borren en cada
redeploy**:

- Pestaña **Mounts / Volumes** → **Add Volume**
  - Type: **Volume** (persistente)
  - Name: `datos-atribucion`
  - Mount Path: **`/data`**

Esto coincide con `STORAGE_ROOT=/data`. Sin este volumen, cada redeploy empieza
con la base de datos vacía.

## 4. Puerto y dominio

1. Pestaña **Domains** → **Add Domain**:
   - Host: `anuncios.datibot.lat`
   - **Port: `8501`** (puerto interno del contenedor / Streamlit)
   - **HTTPS / SSL: activado** (Let's Encrypt).
2. **DNS**: en tu proveedor de `datibot.lat`, crea un registro **A**:
   - `anuncios` → la **IP pública de tu VPS** de EasyPanel.
   - Espera a que propague (unos minutos) antes de emitir el certificado SSL.

> Streamlit usa **websockets**; el proxy de EasyPanel (Traefik) los soporta por
> defecto, así que no hay configuración extra. El `.streamlit/config.toml` ya
> desactiva CORS/XSRF para funcionar detrás del proxy.

## 5. Deploy

Pulsa **Deploy**. Cuando termine el build, abre `https://anuncios.datibot.lat`.

---

## 6. Primer arranque

1. Barra lateral → **📥 Recargar anuncios de Facebook** para traer tus anuncios
   activos y abrir un período inicial por cada uno.
2. Verifica en la barra lateral que **Facebook API: conectada**. Si dice “sin
   conexión”, revisa las variables de entorno y el permiso `ads_management`.
3. Registra ventas de dos formas:
   - **⬆️ Subir ventas.xlsx** (barra lateral): sube tu Excel; solo procesa las
     filas nuevas respecto a lo ya visto.
   - **🧾 Registrar venta**: captura una venta al instante (ideal en la nube).

---

## Actualizar la app (fix en la marcha)

```bash
git add -A
git commit -m "cambios"
git push
```

Luego, en EasyPanel, **Deploy** de nuevo. El volumen `/data` conserva tus datos.

---

## Notas de seguridad

- El repo es **público**: nunca subas `.env` ni `datos.db` (ya están en
  `.gitignore`). Las credenciales viven **solo** en las variables de entorno de
  EasyPanel.
- Si alguna vez expusiste un token en el repo, **revócalo** en Facebook y genera
  uno nuevo.
