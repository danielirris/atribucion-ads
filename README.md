# 📊 Atribución de ventas a Facebook Ads por período de presupuesto

Aplicación en **Python + Streamlit** para atribuir ventas a anuncios de Facebook
Ads y medir el rendimiento **desde cada cambio de presupuesto**. Pensada para
escalar anuncios de forma agresiva durante el día y decidir, en tiempo real, si
subir o bajar el presupuesto según el ROAS del período actual.

---

## ¿Qué hace?

- **Monitorea `ventas.xlsx`** con `watchdog`. Lee **todas las hojas**. Cuando
  aparece una fila nueva, si `Hora_Venta` viene vacía le asigna el timestamp del
  momento de detección, atribuye la venta al **período de presupuesto activo** de
  ese anuncio y la guarda en SQLite.
- **Se conecta a la Facebook Marketing API** (`facebook-business`) para cargar
  los anuncios activos y hace **polling cada 5 minutos** buscando cambios de
  `daily_budget`. Cada cambio **cierra el período anterior** y **abre uno nuevo**.
- **Calcula por período**: número de ventas, ingreso total, gasto estimado, ROAS,
  costo por venta y estado (ACTIVO/CERRADO).
- **Dashboard** con vista general (semáforo 🟢🟡🔴), detalle por anuncio (tabla,
  gráfica de ROAS y línea de tiempo), alertas, registro manual y cambios en lote.
- **Modifica presupuestos en Facebook** (a nivel de **Adset**), **duplica anuncios**
  (`/{ad-id}/copies` con `deep_copy`) y registra todo localmente.

### Fórmulas por período

```
gasto_estimado  = (presupuesto_diario / 1440) * duracion_en_minutos
roas            = ingreso_total / gasto_estimado
costo_por_venta = gasto_estimado / numero_ventas
```

---

## Estructura del proyecto

```
proyecto-atribucion/
├── app.py                 # Entrada principal de Streamlit (dashboard)
├── config.py              # Carga el .env
├── db.py                  # Todas las funciones de SQLite
├── facebook_api.py        # Conexión, polling, duplicación y cambio de presupuesto
├── excel_watcher.py       # Watchdog del Excel
├── calculos.py            # Lógica de períodos y ROAS
├── crear_excel_ejemplo.py # (Opcional) genera un ventas.xlsx de prueba
├── requirements.txt       # Dependencias
├── .env.example           # Plantilla de credenciales
├── .env                   # Tus credenciales (NO subir a git)
└── README.md
```

---

## Instalación paso a paso

### 1. Requisitos

- Python 3.9 o superior.

### 2. Crear entorno virtual (recomendado)

```bash
cd proyecto-atribucion
python3 -m venv .venv
source .venv/bin/activate      # En Windows: .venv\Scripts\activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. Configurar credenciales

Copia la plantilla y edítala con tus datos reales:

```bash
cp .env.example .env
```

Abre `.env` y completa:

| Variable        | Descripción                                                        |
|-----------------|--------------------------------------------------------------------|
| `APP_ID`        | ID de tu app de Facebook Developers                                |
| `APP_SECRET`    | Secret de tu app                                                   |
| `ACCESS_TOKEN`  | Token de acceso de **larga duración** con permiso `ads_management` |
| `AD_ACCOUNT_ID` | ID de la cuenta publicitaria (con o sin prefijo `act_`)            |

> El token necesita el permiso **`ads_management`** para poder leer presupuestos,
> modificarlos y duplicar anuncios.

### 5. (Opcional) Crear un Excel de prueba

```bash
python crear_excel_ejemplo.py
```

Esto crea `ventas.xlsx` con dos hojas y las columnas correctas
(`ID_Anuncio`, `Valor_Venta`, `Hora_Venta`).

### 6. Ejecutar la app

```bash
streamlit run app.py
```

Se abrirá en el navegador (por defecto `http://localhost:8501`).

---

## Uso

1. Al iniciar, la app arranca **dos hilos en segundo plano**: el polling de
   Facebook y el watchdog del Excel. También crea `datos.db` si no existe.
2. Pulsa **"Recargar anuncios de Facebook"** (barra lateral) para traer tus
   anuncios activos y abrir un período inicial por cada uno.
3. Cada vez que registres una venta en `ventas.xlsx` (nueva fila en cualquier
   hoja), la app la detecta y la atribuye al período de presupuesto activo.
4. Cuando cambies un presupuesto —desde Facebook, desde el polling, o desde la
   propia app— se **cierra el período anterior** y se **abre uno nuevo**, de modo
   que el ROAS siempre se mide **desde el último cambio de presupuesto**.

### El Excel (`ventas.xlsx`)

- Columnas obligatorias: **`ID_Anuncio`**, **`Valor_Venta`**, **`Hora_Venta`**.
- Puede tener **varias hojas**; se leen todas.
- Si `Hora_Venta` está vacía, la app usa el momento de detección.
- Las filas históricas existentes al arrancar **no** se reimportan
  automáticamente (para no duplicar). Usa **"Importar TODO el Excel"** en la barra
  lateral si quieres cargarlas.

### Secciones del dashboard

1. **Vista general** — tabla de anuncios activos con presupuesto, tiempo en ese
   presupuesto, ventas y ROAS del período actual, y semáforo (🟢 ROAS > 2,
   🟡 entre 1 y 2, 🔴 < 1). Incluye edición de presupuesto por anuncio (envía a Facebook).
2. **Alertas** — avisa si un período lleva **+30 min activo y ROAS < 1.5x**.
3. **Detalle por anuncio** — tabla de todos los períodos del día, gráfica de barras
   de ROAS por período (Plotly), línea de tiempo de cambios de presupuesto, y el
   botón **⚡ Duplicar anuncio**.
4. **Cambios rápidos en lote** — tabla editable con todos los anuncios y un botón
   para aplicar todos los cambios a Facebook de una vez.
5. **Registro manual** — formulario para cambiar el presupuesto de un anuncio
   (también lo envía a Facebook).

El dashboard tiene un botón **"Actualizar ahora"** y además se **refresca solo
cada 2 minutos**.

---

## Cambios de presupuesto y Facebook (importante)

- El presupuesto se modifica **sobre el Adset**, no sobre el Ad. Por eso la app
  guarda el `adset_id` de cada anuncio (columna en la tabla `anuncios`).
- La app llama a la API con `daily_budget` en **centavos** (`monto * 100`).
- **Solo si Facebook confirma el cambio** se cierra el período anterior y se abre
  uno nuevo en SQLite. Si Facebook devuelve error, el período **no** se toca y se
  muestra el mensaje exacto de Facebook.
- Si no hay API disponible (sin credenciales o sin `adset_id`), el cambio se
  registra **solo localmente** y se avisa en pantalla.

---

## Duplicación de anuncios

En **Detalle por anuncio → ⚡ Duplicar anuncio**:

- Eliges número de copias (por defecto 4, máx. 10) y el presupuesto por copia.
- Usa `/{ad-id}/copies` con `deep_copy: true` (copia adset + creativo).
- Las copias se crean **PAUSED** salvo que actives el toggle
  *"Activar copias inmediatamente"* (**ACTIVE**).
- Cada copia se guarda en `anuncios` con nombre `... - Copia N`, se le fija el
  presupuesto y se le abre un período nuevo.
- Se muestra el resultado: cuántas se crearon, sus IDs y nombres, y los errores
  de las que fallaron (con el mensaje de Facebook).

---

## Base de datos (`datos.db`)

Persiste entre reinicios. Tablas:

- **`anuncios`**: `ad_id`, `nombre`, `adset_id`, `activo`, `ultima_actualizacion`
- **`periodos`**: `id`, `ad_id`, `presupuesto`, `hora_inicio`, `hora_fin` (NULL si
  está abierto), `duracion_minutos`
- **`ventas`**: `id`, `ad_id`, `valor_venta`, `hora_venta`, `periodo_id`, `hoja_origen`

Todos los timestamps se guardan con **fecha y hora completas** (ISO 8601).

---

## Manejo de errores

- Si la Facebook API no responde, la app **sigue funcionando**: muestra el estado
  y el último error en la barra lateral, y las secciones locales (ventas,
  períodos, cálculos) siguen operando.
- Si `facebook-business` o `watchdog` no están instalados, la app lo indica en la
  barra lateral en lugar de caerse.

---

## Notas

- No subas `.env` ni `datos.db` a git (ya están en `.gitignore`).
- El polling corre en su propio hilo (`threading`), igual que el watchdog, para no
  bloquear la UI de Streamlit.
```
