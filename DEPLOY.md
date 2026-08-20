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
APP_ID=<tu_app_id_central>
APP_SECRET=<tu_app_secret_central>
APP_PASSWORD=<una_clave_para_entrar_a_la_app>
STORAGE_ROOT=/data
POLLING_INTERVAL_SEG=300
```

> **Multi-Business (tu caso):** NO pongas tokens aquí. Los tokens de cada Business
> se agregan **dentro de la app**, en la sección **🔌 Conexiones**, y se guardan en
> el volumen `/data`. `APP_ID`/`APP_SECRET` son la app central (una sola).
>
> **Una sola cuenta (alternativa):** si prefieres, agrega también
> `ACCESS_TOKEN=<token>` y `AD_ACCOUNT_ID=act_<id>` y te saltas la sección Conexiones.
>
> Los tokens necesitan permisos **`ads_management`, `ads_read`, `business_management`**.
> `APP_PASSWORD` pone un candado de acceso (recomendado en dominio público).

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

> ⚠️ **Puerto:** EasyPanel inyecta la variable `PORT` (normalmente **80**) y la app
> la respeta, así que Streamlit escucha en ese puerto. Revisa a qué puerto apunta
> el **dominio por defecto** que crea EasyPanel (en Dominios verás algo como
> `→ http://<servicio>:80/`) y usa **ese mismo puerto** en tu dominio propio.
> En la práctica: **usa el puerto `80`** (no 8501) salvo que el dominio por
> defecto muestre otro.

1. Pestaña **Domains** → **Add Domain**:
   - Host: `anuncios.datibot.lat`
   - **Port: `80`** (el mismo que usa el dominio por defecto de EasyPanel)
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

1. Entra con tu `APP_PASSWORD`.
2. Ve a **🔌 Conexiones** (abajo del todo) → **Agregar conexión**: pega el token
   de Usuario del Sistema de cada Business (uno por Business), ponle un alias y
   **Probar y descubrir cuentas** para confirmar. Guarda cada uno.
3. Barra lateral → **📥 Recargar anuncios de Facebook**: trae los anuncios de
   **todas las cuentas de todas las conexiones** y abre un período por cada uno.
4. Verifica en la barra lateral: **Conexiones / Cuentas / Anuncios activos**.
5. Filtra la tabla por **Cuenta / Business** y por **Activos / Apagados**.
6. Registra ventas de dos formas:
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
