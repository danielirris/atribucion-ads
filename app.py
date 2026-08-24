"""
app.py
Dashboard de atribución de ventas a Facebook Ads con seguimiento por período
de presupuesto.

Ejecutar con:
    streamlit run app.py
"""
import time
import html as _html
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config
import db
import calculos
import facebook_api as fb
import excel_watcher as watcher
import supabase_source as supa
import gsheets as gs
import fx
import ia

st.set_page_config(page_title="Ads Command Center", layout="wide")

# Marcador de versión: sirve para confirmar que el redeploy tomó el código nuevo.
APP_VERSION = "v79 · 2026-08-23"


# --------------------------------------------------------------------------- #
#  Arranque único de servicios en segundo plano (una vez por proceso)
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def arrancar_servicios():
    """
    Inicializa la DB y lanza los hilos de background UNA sola vez por proceso.
    st.cache_resource garantiza que no se reinicien en cada rerun de Streamlit.
    """
    db.init_db()
    fb.inicializar_api()
    fb.iniciar_polling()      # hilo de polling de Facebook
    watcher.iniciar_watcher() # hilo de watchdog del Excel
    return {"iniciado_en": db.a_texto(db.ahora())}


servicios = arrancar_servicios()


# --------------------------------------------------------------------------- #
#  Helpers de UI
# --------------------------------------------------------------------------- #
def _fmt_money(v):
    try:
        return f"${v:,.2f}"
    except Exception:
        return str(v)


def _usd(v):
    """Formato tipo '1.234,56 US$' (coma decimal), como la herramienta de referencia."""
    if v is None:
        return "—"
    try:
        s = f"{float(v):,.2f}"            # 1,234.56
        s = s.replace(",", "@").replace(".", ",").replace("@", ".")
        return f"{s} US$"
    except Exception:
        return str(v)


def _norm_txt(s) -> str:
    """minúsculas y sin acentos (para búsquedas tolerantes)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.lower().strip()


def _roas_color(r):
    if r is None:
        return "#9ca3af"
    if r >= 2:
        return "#22c55e"
    if r >= 1:
        return "#eab308"
    return "#ef4444"


def _roas_pill(r):
    """Badge tipo pill para el ROAS: verde alto (brillo extra si >5x), amarillo, rojo."""
    r = r or 0.0
    if r >= 5:
        bg, c, glow = "rgba(16,185,129,.18)", "#34f5b0", ";box-shadow:0 0 12px rgba(16,185,129,.45)"
    elif r > 2:
        bg, c, glow = "rgba(16,185,129,.15)", "#10b981", ""
    elif r >= 1:
        bg, c, glow = "rgba(245,196,81,.15)", "#f5c451", ""
    else:
        bg, c, glow = "rgba(239,68,68,.15)", "#ff8b84", ""
    return (f'<span style="display:inline-block;padding:3px 9px;border-radius:8px;'
            f'background:{bg};color:{c};font-weight:700;font-size:13px{glow}">{r:.2f}x</span>')


def _spark_svg(serie, color="#22c55e", w=78, h=26):
    """Mini-gráfica (polyline SVG) a partir de una lista de valores."""
    pts = [x for x in (serie or []) if x is not None]
    if len(pts) < 2:
        return f'<svg width="{w}" height="{h}"></svg>'
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    n = len(pts)
    coords = []
    for i, v in enumerate(pts):
        x = (i / (n - 1)) * (w - 4) + 2
        y = h - 2 - ((v - lo) / rng) * (h - 4)
        coords.append(f"{x:.1f},{y:.1f}")
    path = " ".join(coords)
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline points="{path}" fill="none" stroke="{color}" '
            f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>')


def _fecha_corta(iso):
    """'2026-08-04T10:00:00-0700' -> '04 ago 2026'."""
    if not iso:
        return "—"
    meses = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
    d = db.a_fecha(str(iso)[:19].replace("T", " ")) if "T" in str(iso) else db.a_fecha(iso)
    if not d:
        return str(iso)[:10]
    return f"{d.day:02d} {meses[d.month - 1]} {d.year}"


@st.cache_data(ttl=900, show_spinner=False)
def _insights_cache(date_preset: str, nivel: str = "ad",
                    since: str = "", until: str = ""):
    """Cachea los insights de Facebook 10 min (por rango y nivel).
    Los datos base ya se refrescan cada 30 min en segundo plano, así que no hace
    falta llamar cada 2 min mientras miras el Dashboard (evita el rate limit)."""
    tr = {"since": since, "until": until} if (since and until) else None
    return fb.obtener_insights(date_preset, nivel, time_range=tr)


def cambio_presupuesto_completo(ad_id: str, nuevo_monto: float) -> dict:
    """
    Flujo Parte 8: envía el cambio a Facebook y SOLO si Facebook confirma,
    cierra el período anterior y abre uno nuevo en SQLite.
    Devuelve {"ok": bool, "error": str|None, "solo_local": bool}.
    """
    anuncio = db.obtener_anuncio(ad_id)
    if not anuncio:
        return {"ok": False, "error": "Anuncio no encontrado.", "solo_local": False}

    conexion_id = anuncio.get("conexion_id")
    # El presupuesto puede vivir en el conjunto (adset) o en la campaña (CBO).
    obj_id = anuncio.get("budget_obj_id") or anuncio.get("adset_id")
    nivel = anuncio.get("budget_nivel") or "adset"
    hay_conexion = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False

    # Si hay API/conexión disponible, intentamos el cambio en Facebook.
    if fb.SDK_DISPONIBLE and hay_conexion and obj_id:
        r = fb.actualizar_presupuesto(obj_id, nivel, nuevo_monto, conexion_id)
        if not r["ok"]:
            # NO cerramos el período en SQLite.
            return {"ok": False, "error": r["error"], "solo_local": False}
        db.cambiar_periodo(ad_id, nuevo_monto)
        return {"ok": True, "error": None, "solo_local": False}

    # Sin API disponible: registro local solamente (advertencia).
    db.cambiar_periodo(ad_id, nuevo_monto)
    motivo = "sin conjunto/campaña" if not obj_id else "Facebook no disponible"
    return {"ok": True, "error": None, "solo_local": True, "motivo": motivo}


# --------------------------------------------------------------------------- #
#  Barra lateral: estado de servicios
# --------------------------------------------------------------------------- #
def _recargar_facebook():
    """Trae datos nuevos de Facebook (se usa desde el botón del top-right)."""
    with st.spinner("Consultando Facebook (todas las conexiones)..."):
        r = fb.cargar_todo()
        try:
            _insights_cache.clear()
        except Exception:
            pass
    if r["num_anuncios"]:
        st.toast(f"{r['num_anuncios']} anuncios de {r['num_cuentas']} cuenta(s).", icon="✅")
    if r["errores"]:
        st.error("Errores: " + "; ".join(r["errores"])[:300])
    st.rerun()


_BRAND_HTML = """
<div class="acc-brand">
  <div class="acc-logo">
    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Z" fill="#fff" opacity=".14"/>
      <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Z" stroke="#fff" stroke-width="1.3"/>
      <path d="M7 14l3-4 2 2.4L15 8l2 3" stroke="#fff" stroke-width="1.7"
            stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    </svg>
  </div>
  <div class="acc-name">Ads Command Center<small>ACC · FACEBOOK ADS</small></div>
</div>
"""


def sidebar_estado(pagina: str = "dashboard"):
    # Marca arriba a la izquierda (en todas las páginas).
    st.sidebar.markdown(_BRAND_HTML, unsafe_allow_html=True)
    # Los filtros solo aplican al Dashboard (los botones de datos van arriba a la derecha).
    if pagina == "dashboard":
        sidebar_filtros()


# Páginas de la app (para la navegación propia en el pie de la barra lateral).
_PAGINAS = {
    "dashboard": ("Dashboard", ":material/dashboard:"),
    "tutoriales": ("Tutoriales", ":material/school:"),
    "configuracion": ("Configuración", ":material/settings:"),
}


def _sidebar_nav(pagina: str):
    """Navegación al PIE de la barra lateral (Configuración abajo, como se pidió)."""
    st.sidebar.divider()
    for key, (label, icon) in _PAGINAS.items():
        if st.sidebar.button(label, icon=icon, use_container_width=True,
                             type="primary" if key == pagina else "secondary",
                             key=f"nav_{key}"):
            st.session_state["_pagina"] = key
            st.rerun()
    st.sidebar.caption(f"Versión: {APP_VERSION}")


def _alias_conexion(conexion_id, conexiones):
    if conexion_id in (None, 0, "0"):
        return "ENV (.env)"
    for c in conexiones:
        if str(c["id"]) == str(conexion_id):
            return c.get("alias") or f"Conexión {conexion_id}"
    return f"Conexión {conexion_id}"


# Palabras que NO son producto (estructura de la campaña, formatos, países...).
_PROD_STOP = {
    "CP", "CONJUNTO", "CONJUNTOS", "AD", "ADS", "ADSET", "CAMPANA", "CAMPAÑA", "CBO",
    "ABO", "RETARGETING", "COMPRADORES", "COMPRADOR", "VIDEO", "VIDEOS", "UGC",
    "TESTIMONIO", "LARGO", "CORTO", "INTERESES", "INTERES", "LOOKALIKE", "LAL",
    "ABIERTO", "ADVANTAGE", "COPY", "NUEVO", "NUEVA", "TEST", "PRUEBA", "IMAGEN",
    "CARRUSEL", "ANUNCIO", "ANUNCIOS", "FRIO", "FRÍO", "CALIENTE", "REMARKETING",
    "DIA", "DÍA", "DIAS", "DÍAS", "NUEVOS", "NUEVAS", "PROSPECTING",
}
_PROD_PAISES = {"BR", "MX", "CO", "AR", "CL", "PE", "US", "USA", "EC", "VE", "BO",
                "PY", "UY", "GT", "DO", "PA", "CR", "HN", "SV", "NI", "ES"}


def _extraer_productos(campanas) -> list:
    """Saca nombres de PRODUCTO de los nombres de campaña.
    Separa por | - / y descarta números, países y palabras de estructura."""
    import re
    cand = {}
    for nombre in campanas:
        for parte in re.split(r"[|/\-–—]", str(nombre)):
            parte = re.sub(r"#\s*[\d.]+", " ", parte)   # quita "#2", "AD6.1"
            parte = re.sub(r"[\d.]+", " ", parte)         # quita números sueltos
            palabras = [w for w in parte.strip().split()
                        if len(w) >= 3 and w.upper() not in _PROD_STOP
                        and w.upper() not in _PROD_PAISES]
            if palabras:
                clave = " ".join(palabras).strip().title()
                cand[clave] = cand.get(clave, 0) + 1
    # ordenados por frecuencia (los productos más usados primero)
    return [k for k, _ in sorted(cand.items(), key=lambda x: (-x[1], x[0]))]


def sidebar_filtros():
    """Filtros globales que afectan al Dashboard (se guardan en session_state).
    En el sidebar: Buscar, Rango de fechas y un expander 'Filtros' con el resto.
    (Ver por / Estado están arriba en el Dashboard.)"""
    st.sidebar.divider()
    st.sidebar.text_input("🔎 Buscar", key="f_buscar", placeholder="nombre o ID…",
                          help="Busca por fragmento en el nombre del anuncio, conjunto, "
                               "campaña o el ID. Ej: 'BOLIS' encuentra '[18/8] p6 BOLIS'.")
    st.sidebar.selectbox("📅 Rango de fechas",
                         ["Hoy", "Ayer", "Últimos 7 días", "Últimos 30 días", "Este mes",
                          "Máximo", "Personalizado (calendario)"], key="f_rango")
    if st.session_state.get("f_rango") == "Personalizado (calendario)":
        _hoy = db.ahora().date()
        _defv = st.session_state.get("f_rango_pers") or (_hoy, _hoy)
        st.sidebar.date_input(
            "Elige el día (o un rango: clic en inicio y luego en fin)",
            value=_defv, format="DD/MM/YYYY", key="f_rango_pers",
            help="Para UN solo día: haz clic dos veces en el mismo día. "
                 "Para un rango: clic en el día inicial y luego en el final.")
        _rp = st.session_state.get("f_rango_pers")
        if isinstance(_rp, (list, tuple)) and len(_rp) == 1:
            st.sidebar.caption("Elige también el día final (o repite el mismo para un solo día).")

    todos = db.obtener_anuncios(solo_activos=False)
    conexiones = db.obtener_conexiones()
    business = sorted({_alias_conexion(a.get("conexion_id"), conexiones) for a in todos})
    cuentas = sorted({(a.get("cuenta_nombre") or "—") for a in todos})
    paises = sorted({(a.get("cuenta_pais") or "—") for a in todos})
    campanas = sorted({a.get("campaign_nombre") for a in todos if a.get("campaign_nombre")})
    # Productos: se extraen del NOMBRE de las campañas (más los de las ventas).
    vd = db.valores_venta_distintos()
    productos = _extraer_productos(campanas)
    extra = [p for p in (set(vd["origenes"]) | set(vd["productos"])) if p]
    for p in sorted(extra):
        if p.upper() not in {x.upper() for x in productos}:
            productos.append(p)

    # Cuenta cuántos filtros hay activos, para mostrarlo en el título del expander.
    n_act = sum(bool(st.session_state.get(k)) for k in
                ("f_business", "f_cuenta", "f_campana", "f_producto")) \
        + (1 if st.session_state.get("f_pais", "Todos") not in ("Todos", None) else 0)
    titulo = f"🎚️ Filtros ({n_act})" if n_act else "🎚️ Filtros"
    with st.sidebar.expander(titulo, expanded=False):
        st.multiselect("Business", business, key="f_business",
                       placeholder="Todos los Business",
                       help="Puedes elegir VARIOS: haz clic en cada Business (queda como chip).")
        st.multiselect("Cuentas publicitarias", cuentas, key="f_cuenta",
                       placeholder="Todas las cuentas",
                       help="Puedes elegir VARIAS: haz clic en cada cuenta, una por una "
                            "(cada una queda como chip). Deja vacío para ver todas.")
        st.multiselect("Campaña", campanas, key="f_campana",
                       placeholder="Todas las campañas",
                       help="Elige una campaña y pon 'Ver por: Conjunto de anuncios' "
                            "para ver sus conjuntos.")
        st.multiselect("Producto", productos, key="f_producto",
                       placeholder=("Todos los productos" if productos
                                    else "Sin productos aún — pulsa Recargar"),
                       help="Se saca del NOMBRE de la campaña (p. ej. BOLIS, LAVADORAS, "
                            "EDUCARTE). Puedes elegir varios. Filtra las campañas/conjuntos "
                            "que contengan ese producto.")
        st.selectbox("País", ["Todos"] + paises, key="f_pais")
        _resumen_pais(todos)


def _resumen_pais(todos):
    """Gasto por país en el sidebar. REUSA el mismo caché de 'Rendimiento por país'
    (mismos argumentos) para no hacer ni un llamado extra a Facebook."""
    ahora = db.ahora()
    date_preset, since, until, _c, _h = _rango_actual(ahora)
    filas = _gasto_pais_cache(date_preset, since, until)
    if not filas:
        return
    gasto_pais = {}
    for r in filas:
        nombre = fb.pais_nombre(r["country"])
        gasto_pais[nombre] = gasto_pais.get(nombre, 0.0) + r["spend"] * fx.tasa_a_usd(r["moneda"])
    if not gasto_pais:
        return
    # Usa el contenedor actual (va dentro del expander de Filtros), no la raíz del sidebar.
    st.markdown("**Gasto por país (USD)**")
    for pais, g in sorted(gasto_pais.items(), key=lambda x: -x[1]):
        st.caption(f"{pais}: {_usd(g)}")


# --------------------------------------------------------------------------- #
#  Sección 1 — Vista general de anuncios activos
# --------------------------------------------------------------------------- #
def _es_activo(a: dict) -> bool:
    est = a.get("effective_status")
    if est:
        return est == "ACTIVE"
    return a.get("activo") == 1


_NIVELES = {"Anuncio": "ad", "Conjunto de anuncios": "adset", "Campaña": "campaign"}


def _presupuesto_ad(ad_id: str):
    abierto = db.periodo_abierto(ad_id)
    if abierto:
        return float(abierto["presupuesto"])
    ps = db.obtener_periodos(ad_id)
    return float(ps[-1]["presupuesto"]) if ps else None


def _agregar_insights(ad_ids, insights):
    """Suma los insights (a nivel anuncio) de un grupo de anuncios; recalcula CPM,
    CTR y costo por conversación. Devuelve un dict como el de un anuncio."""
    spend = imp = clk = conv = compras = compras_val = 0.0
    hay = False
    for aid in ad_ids:
        m = insights.get(str(aid))
        if not m:
            continue
        hay = True
        spend += m.get("spend") or 0.0
        imp += m.get("impressions") or 0.0
        clk += m.get("clicks") or 0.0
        conv += m.get("conversaciones") or 0.0
        compras += m.get("compras") or 0.0
        compras_val += m.get("compras_valor") or 0.0
    if not hay:
        return {}
    return {
        "spend": spend, "impressions": imp, "clicks": clk,
        "cpm": (spend / imp * 1000) if imp else None,
        "ctr": (clk / imp * 100) if imp else None,
        "conversaciones": conv,
        "costo_conversacion": (spend / conv) if conv else None,
        "compras": compras, "compras_valor": compras_val,
    }


def _construir_filas(anuncios, nivel, insights, ventas_agg, ahora):
    # Tipo de cambio a USD (todo se muestra en dólares).
    _rate_cache = {}

    def _rate(moneda):
        moneda = (moneda or "USD").upper()
        if moneda not in _rate_cache:
            _rate_cache[moneda] = fx.tasa_a_usd(moneda)
        return _rate_cache[moneda]

    # Moneda en la que vienen las ventas (Excel/Supabase): 'auto' = la de la cuenta.
    moneda_ventas_cfg = db.get_config("moneda_ventas", "auto")
    # Fuente de ventas del dashboard: excel / gsheets / supabase / todas / meta.
    fuente_ventas = db.get_config("fuente_ventas", "todas")
    # ¿Hay conexión activa a Facebook? Si la hay, el gasto es el REAL de Meta y un
    # anuncio sin entrega en el período gasta 0 (no se estima, para no inflarlo).
    try:
        hay_conexion = bool(fb.SDK_DISPONIBLE and fb._conexiones_efectivas())
    except Exception:
        hay_conexion = False

    grupos = {}
    for a in anuncios:
        if nivel == "adset":
            gkey = a.get("adset_id") or a["ad_id"]
            nombre = a.get("adset_nombre") or gkey
            ins_key = str(a.get("adset_id") or "")
        elif nivel == "campaign":
            gkey = a.get("campaign_id") or a["ad_id"]
            nombre = a.get("campaign_nombre") or gkey
            ins_key = str(a.get("campaign_id") or "")
        else:
            gkey = a["ad_id"]
            nombre = a["nombre"]
            ins_key = str(a["ad_id"])
        g = grupos.setdefault(gkey, {"nombre": nombre, "sub": gkey,
                                     "cuenta": a.get("cuenta_nombre") or "—",
                                     "ins_key": ins_key, "ads": []})
        g["ads"].append(a)

    filas = []
    for gkey, g in grupos.items():
        ads = g["ads"]
        # Moneda de la cuenta del grupo y tipo de cambio a USD.
        moneda_cuenta = ads[0].get("cuenta_moneda") or "USD"
        rate_c = _rate(moneda_cuenta)
        moneda_v = moneda_cuenta if moneda_ventas_cfg == "auto" else moneda_ventas_cfg
        rate_v = _rate(moneda_v)

        # Insights vienen SIEMPRE a nivel anuncio (una sola llamada) y se SUMAN por
        # grupo (conjunto/campaña) aquí en Python -> cambiar de vista es instantáneo
        # y no dispara llamados nuevos a Facebook.
        ins = _agregar_insights([x["ad_id"] for x in ads], insights)

        # Fuente de ventas: tus fuentes (ya filtradas en ventas_agg) o Meta (pixel).
        if fuente_ventas == "meta":
            num = int(ins.get("compras") or 0)
            ingresos_nat = float(ins.get("compras_valor") or 0.0)
            ingresos = ingresos_nat * rate_c  # compras de Meta vienen en moneda de la cuenta
        else:
            num = sum(ventas_agg.get(x["ad_id"], {}).get("num_ventas", 0) for x in ads)
            ingresos_nat = sum(ventas_agg.get(x["ad_id"], {}).get("ingreso_total", 0.0) for x in ads)
            ingresos = ingresos_nat * rate_v  # -> USD

        if nivel == "campaign":
            # ¿CBO? (presupuesto a nivel CAMPAÑA). Si lo es, el presupuesto es UNO
            # solo (el pote de la campaña), NO se suma por conjunto.
            es_cbo = any((x.get("budget_nivel") == "campaign") for x in ads)
            if es_cbo:
                presup_nat = _presupuesto_ad(ads[0]["ad_id"])  # todos comparten el mismo
            else:
                # Presupuesto por conjunto: sumar el de cada conjunto (adset) único.
                vistos, presup_nat, hay = set(), 0.0, False
                for x in ads:
                    asid = x.get("adset_id")
                    if asid in vistos:
                        continue
                    vistos.add(asid)
                    p = _presupuesto_ad(x["ad_id"])
                    if p is not None:
                        presup_nat += p
                        hay = True
                presup_nat = presup_nat if hay else None
        else:
            presup_nat = _presupuesto_ad(ads[0]["ad_id"])
        presupuesto = (presup_nat * rate_c) if presup_nat is not None else None

        spend_nat = ins.get("spend")
        spend = (spend_nat * rate_c) if spend_nat is not None else None
        if spend_nat is not None:
            gasto_nat = spend_nat
        elif hay_conexion:
            # Con conexión activa: sin fila de insights = sin entrega = gasto 0.
            # (Antes se estimaba desde el presupuesto e inflaba el total.)
            gasto_nat = 0.0
        else:
            # Sin conexión a Facebook: mostramos un gasto estimado desde el presupuesto.
            gasto_nat = sum((calculos.metricas_periodo_actual(x["ad_id"], ahora) or {}).get(
                "gasto_estimado", 0.0) for x in ads)
        gasto = gasto_nat * rate_c

        cpm = (ins.get("cpm") * rate_c) if ins.get("cpm") is not None else None
        costo_conv_nat = ins.get("costo_conversacion")
        costo_conv = (costo_conv_nat * rate_c) if costo_conv_nat is not None else None

        roas = (ingresos / gasto) if gasto and gasto > 0 else 0.0
        costo_venta = (gasto / num) if num else 0.0
        activos = sum(1 for x in ads if _es_activo(x))
        rep = ads[0]
        # Objeto a prender/apagar según el nivel de la vista.
        if nivel == "campaign":
            status_obj = rep.get("campaign_id")
        elif nivel == "adset":
            status_obj = rep.get("adset_id")
        else:
            status_obj = rep["ad_id"]

        # Datos "desde la última modificación de presupuesto" (período abierto del rep).
        ab = db.periodo_abierto(rep["ad_id"])
        ad_ids_grupo = [x["ad_id"] for x in ads]
        if ab:
            inicio_mod = db.a_fecha(ab["hora_inicio"])
            mins = max(0.0, (ahora - inicio_mod).total_seconds() / 60.0) if inicio_mod else 0.0
            gmod_nat = (float(ab["presupuesto"]) / config.MINUTOS_POR_DIA) * mins
            vs_mod = db.ventas_suma(ad_ids_grupo, inicio_mod)
            imod_nat = vs_mod["ingreso_nat"]
            ventas_mod = int(vs_mod.get("num", 0))
            roas_mod = (imod_nat / gmod_nat) if gmod_nat > 0 else 0.0
        else:
            inicio_mod, gmod_nat, imod_nat, roas_mod, ventas_mod = None, 0.0, 0.0, 0.0, 0
        # Presupuesto ANTERIOR (período previo al último cambio), en USD.
        _periodos = db.obtener_periodos(rep["ad_id"])
        presup_anterior = ((float(_periodos[-2]["presupuesto"]) * rate_c)
                           if len(_periodos) >= 2 else None)

        filas.append({
            "nombre": g["nombre"], "sub": g["sub"], "cuenta": g["cuenta"],
            "moneda": moneda_cuenta, "rate_c": rate_c,
            "activos": activos, "total": len(ads),
            "presupuesto": presupuesto, "presup_nat": presup_nat,
            "presup_anterior": presup_anterior,
            "spend": spend, "gasto": gasto, "gasto_nat": gasto_nat,
            "pct": (spend / presupuesto * 100) if (spend is not None and presupuesto) else None,
            "cpm": cpm, "ctr": ins.get("ctr"), "costo_venta": costo_venta,
            "num": num, "ingresos": ingresos, "ingresos_nat": ingresos_nat,
            "ganancia": ingresos - (gasto or 0.0), "roas": roas,
            "creado": rep.get("fecha_creacion"), "ult_mod": inicio_mod,
            "roas_mod": roas_mod, "ingresos_mod": imod_nat * rate_v,
            "gasto_mod": gmod_nat * rate_c, "ventas_mod": ventas_mod,
            "conv": ins.get("conversaciones"),
            "impresiones": ins.get("impressions"), "clics": ins.get("clicks"),
            "spend_nat": spend_nat, "compras_meta": ins.get("compras"),
            "compras_valor_meta": ins.get("compras_valor"),
            "costo_conv": costo_conv, "costo_conv_nat": costo_conv_nat,
            # datos para acciones (modificar presupuesto / duplicar / prender-apagar)
            "ad_rep": rep["ad_id"], "ad_ids": [x["ad_id"] for x in ads],
            "adset_rep": rep.get("adset_id"),
            "conexion_id": rep.get("conexion_id"),
            "budget_obj_id": rep.get("budget_obj_id") or rep.get("adset_id"),
            "budget_nivel": rep.get("budget_nivel") or "adset",
            "cuenta_id": rep.get("cuenta_id"), "cuenta_nombre": rep.get("cuenta_nombre"),
            "status_obj_id": status_obj, "status_nivel": nivel,
        })
    filas.sort(key=lambda f: f["roas"], reverse=True)
    return filas


def _num(v):
    """Número con coma decimal, sin símbolo (para la moneda original)."""
    try:
        return f"{float(v):,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
    except Exception:
        return "—"


def _money_html(usd, nat, moneda, cls=""):
    """USD grande + moneda original pequeña abajo."""
    big = f'<div class="big {cls}">{_usd(usd)}</div>'
    if nat is not None and (moneda or "USD").upper() != "USD":
        return big + f'<div class="sub">{_num(nat)} {moneda}</div>'
    return big


def _aplicar_presupuesto_grupo(fila, nuevo_usd):
    """Cambia el presupuesto (en USD, se convierte a la moneda de la cuenta)."""
    rate = fila.get("rate_c") or 1.0
    nativo = (nuevo_usd / rate) if rate else nuevo_usd
    hay = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False
    if fb.SDK_DISPONIBLE and hay and fila.get("budget_obj_id"):
        r = fb.actualizar_presupuesto(fila["budget_obj_id"], fila.get("budget_nivel", "adset"),
                                      nativo, fila.get("conexion_id"))
        if not r["ok"]:
            return {"ok": False, "error": r["error"]}
        for aid in fila["ad_ids"]:
            db.cambiar_periodo(aid, nativo)
        return {"ok": True, "solo_local": False, "nativo": nativo}
    for aid in fila["ad_ids"]:
        db.cambiar_periodo(aid, nativo)
    return {"ok": True, "solo_local": True, "nativo": nativo}


def _rango_actual(ahora):
    """Devuelve (date_preset, since, until, cutoff, hasta) según el filtro de rango."""
    rango_lbl = st.session_state.get("f_rango", "Hoy")
    if rango_lbl == "Personalizado (calendario)":
        rp = st.session_state.get("f_rango_pers")
        d = h = None
        if isinstance(rp, (list, tuple)):
            if len(rp) == 2 and rp[0] and rp[1]:
                d, h = rp[0], rp[1]
            elif len(rp) == 1 and rp[0]:      # un solo día elegido
                d = h = rp[0]
        elif rp:                               # date_input devolvió una sola fecha
            d = h = rp
        if d and h:
            return ("today", d.strftime("%Y-%m-%d"), h.strftime("%Y-%m-%d"),
                    datetime.combine(d, datetime.min.time()),
                    datetime.combine(h, datetime.min.time()) + timedelta(days=1))
    medianoche = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    if rango_lbl == "Ayer":
        # cutoff = ayer 00:00, hasta = hoy 00:00; date_preset 'yesterday' usa la zona
        # horaria de la cuenta en Facebook (gasto más exacto).
        return "yesterday", "", "", medianoche - timedelta(days=1), medianoche
    if rango_lbl == "Este mes":
        return "this_month", "", "", medianoche.replace(day=1), None
    RANGO = {
        "Hoy": ("today", medianoche),
        "Últimos 7 días": ("last_7d", ahora - timedelta(days=7)),
        "Últimos 30 días": ("last_30d", ahora - timedelta(days=30)),
        "Máximo": ("maximum", None),
    }
    dp, cut = RANGO.get(rango_lbl, RANGO["Hoy"])
    return dp, "", "", cut, None


@st.cache_data(ttl=900, show_spinner=False)
def _gasto_pais_cache(date_preset: str, since: str = "", until: str = ""):
    tr = {"since": since, "until": until} if (since and until) else None
    return fb.gasto_por_pais(date_preset, tr)


def seccion_por_pais():
    """Gasto (de Facebook) y facturación (si las ventas traen país) por país."""
    ahora = db.ahora()
    date_preset, since, until, cutoff, hasta = _rango_actual(ahora)

    # Gasto por país (breakdown de Facebook) -> USD
    gasto = {}
    for r in _gasto_pais_cache(date_preset, since, until):
        nombre = fb.pais_nombre(r["country"])
        gasto[nombre] = gasto.get(nombre, 0.0) + r["spend"] * fx.tasa_a_usd(r["moneda"])

    # Facturación por país (solo ventas que traen país) -> USD
    mv = db.get_config("moneda_ventas", "auto")
    if mv == "auto":
        monedas = [a.get("cuenta_moneda") for a in db.obtener_anuncios() if a.get("cuenta_moneda")]
        mv = max(set(monedas), key=monedas.count) if monedas else "USD"
    rate_v = fx.tasa_a_usd(mv)
    ingresos, ventas_n = {}, {}
    for pais_raw, d in db.ventas_por_pais(cutoff, hasta).items():
        nombre = fb.pais_nombre(pais_raw) if len(str(pais_raw)) <= 3 else str(pais_raw).strip().title()
        ingresos[nombre] = ingresos.get(nombre, 0.0) + d["ingreso_nat"] * rate_v
        ventas_n[nombre] = ventas_n.get(nombre, 0) + d["num"]

    st.subheader("Rendimiento por país")
    paises = sorted(set(gasto) | set(ingresos))
    if not paises:
        st.caption("El **gasto por país** aparece al conectar Facebook y pulsar Recargar. "
                   "La **facturación por país** aparece si tus ventas traen una columna de país "
                   "(configúrala en Configuración → Supabase, o agrega una columna 'Pais' al Excel).")
        return
    filas = []
    for p in paises:
        g = gasto.get(p, 0.0)
        ing = ingresos.get(p, 0.0)
        filas.append({"País": p, "Gasto (USD)": round(g, 2),
                      "Facturación (USD)": round(ing, 2),
                      "Ventas": ventas_n.get(p, 0),
                      "ROAS": round((ing / g), 2) if g > 0 else 0.0,
                      "Ganancia (USD)": round(ing - g, 2)})
    df = pd.DataFrame(filas).sort_values("Gasto (USD)", ascending=False)
    st.dataframe(df, use_container_width=True, hide_index=True, column_config={
        "Gasto (USD)": st.column_config.NumberColumn(format="$%.2f"),
        "Facturación (USD)": st.column_config.NumberColumn(format="$%.2f"),
        "Ganancia (USD)": st.column_config.NumberColumn(format="$%.2f"),
        "ROAS": st.column_config.NumberColumn(format="%.2f x"),
    })
    if not ingresos:
        st.caption("Solo se ve el gasto por país. Para ver la **facturación por país**, agrega el "
                   "país a tus ventas (columna 'Pais' en el Excel o mapea la columna en Supabase).")


def _seccion_ia(filas, contexto):
    """Asistente de IA tipo chat, en la barra lateral izquierda."""
    with st.sidebar.expander("🤖 Asistente IA", expanded=False):
        if not ia.disponible():
            st.info("Actívalo en **Configuración → IA**: elige el proveedor "
                    "(OpenAI o Anthropic) y pega tu API key. (También sirve poner "
                    "OPENAI_API_KEY / ANTHROPIC_API_KEY como variable de entorno.)")
            return
        st.caption(f"Proveedor: **{ia.proveedor()}** · modelo `{ia.modelo()}`. Analiza los "
                   "anuncios del rango/filtros actuales. Ej.: «¿Cuáles activos tienen ROAS por "
                   "debajo del promedio?» · «¿Qué anuncios no tienen ni una venta?» · «Top 5 por "
                   "utilidad» · «¿Cuáles conviene pausar y por qué?».")
        hist = st.session_state.setdefault("ia_hist", [])
        for m in hist:
            with st.chat_message("user" if m["role"] == "user" else "assistant"):
                st.markdown(m["texto"])
        preg = st.text_input("Escribe tu pregunta", key="ia_preg",
                             placeholder="¿Cuáles anuncios no tienen ni una venta?",
                             label_visibility="collapsed")
        c1, c2 = st.columns([2, 1])
        if c1.button("Preguntar a la IA", type="primary", key="ia_btn", use_container_width=True):
            if not preg.strip():
                st.warning("Escribe una pregunta.")
            else:
                with st.spinner("Analizando tus anuncios con IA…"):
                    try:
                        hmodel = [{"role": m["role"], "content": m["texto"]} for m in hist]
                        resp = ia.preguntar(preg, filas, contexto, historial=hmodel)
                        hist.append({"role": "user", "texto": preg})
                        hist.append({"role": "assistant", "texto": resp})
                        st.rerun()
                    except Exception as e:
                        st.error(f"No pude responder: {e}")
        if c2.button("Limpiar chat", key="ia_clear", use_container_width=True):
            st.session_state["ia_hist"] = []
            st.rerun()


def seccion_vista_general():
    ahora = db.ahora()

    # Ver por + Estado ARRIBA (Buscar, Rango y demás filtros están en el sidebar).
    ct1, ct2 = st.columns([2.2, 2.0])
    with ct1:
        nivel_lbl = st.segmented_control(
            "Ver por", ["Campaña", "Conjunto de anuncios", "Anuncio"],
            default=st.session_state.get("f_nivel", "Conjunto de anuncios"),
            key="f_nivel") or "Conjunto de anuncios"
    with ct2:
        st.markdown('<span class="estado-anchor"></span>', unsafe_allow_html=True)
        st.segmented_control(
            "Estado", ["Activos", "Apagados", "Todos"],
            default=st.session_state.get("f_estado", "Activos"), key="f_estado")
        _est = st.session_state.get("f_estado") or "Activos"
        _bg, _bd = {
            "Activos": ("rgba(16,185,129,.2)", "rgba(16,185,129,.5)"),
            "Apagados": ("rgba(239,68,68,.2)", "rgba(239,68,68,.5)"),
            "Todos": ("rgba(255,255,255,.12)", "rgba(255,255,255,.25)"),
        }.get(_est, ("rgba(255,255,255,.12)", "rgba(255,255,255,.25)"))
        st.markdown(
            '<style>[data-testid="stElementContainer"]:has(.estado-anchor) + '
            '[data-testid="stElementContainer"] '
            'button[data-variant="segmented_control"][data-selected="true"]{'
            f'background:{_bg} !important;border:1px solid {_bd} !important;'
            'color:#fff !important;}</style>', unsafe_allow_html=True)
    nivel = _NIVELES.get(nivel_lbl, "adset")

    filtro = st.session_state.get("f_estado") or "Activos"
    rango_lbl = st.session_state.get("f_rango", "Hoy")
    cuentas_sel = st.session_state.get("f_cuenta", []) or []
    business_sel = st.session_state.get("f_business", []) or []
    pais_sel = st.session_state.get("f_pais", "Todos")

    date_preset, since, until, cutoff, hasta_dt = _rango_actual(ahora)

    todos = db.obtener_anuncios(solo_activos=False)
    if filtro == "Activos":
        anuncios = [a for a in todos if _es_activo(a)]
    elif filtro == "Apagados":
        anuncios = [a for a in todos if not _es_activo(a)]
    else:
        anuncios = todos
    if business_sel:
        conexiones = db.obtener_conexiones()
        anuncios = [a for a in anuncios
                    if _alias_conexion(a.get("conexion_id"), conexiones) in business_sel]
    if cuentas_sel:
        anuncios = [a for a in anuncios if (a.get("cuenta_nombre") or "—") in cuentas_sel]
    campanas_sel = st.session_state.get("f_campana", []) or []
    if campanas_sel:
        anuncios = [a for a in anuncios if a.get("campaign_nombre") in campanas_sel]
    productos_sel = st.session_state.get("f_producto", []) or []
    if productos_sel:
        pl = [p.lower() for p in productos_sel]

        def _tiene_producto(a):
            texto = ((a.get("campaign_nombre") or "") + " " + (a.get("adset_nombre") or "")
                     + " " + (a.get("nombre") or "")).lower()
            return any(p in texto for p in pl)
        anuncios = [a for a in anuncios if _tiene_producto(a)]
    if pais_sel != "Todos":
        anuncios = [a for a in anuncios if (a.get("cuenta_pais") or "—") == pais_sel]

    # Búsqueda: sobre los anuncios crudos, en TODOS los nombres (anuncio, conjunto,
    # campaña, cuenta) y el ID; insensible a acentos y por palabras (todas deben estar).
    q = (st.session_state.get("f_buscar") or "").strip()
    if q:
        palabras = [_norm_txt(w) for w in q.split() if w]

        def _match_busqueda(a):
            blob = _norm_txt(" ".join(str(a.get(k) or "") for k in
                             ("nombre", "adset_nombre", "campaign_nombre",
                              "cuenta_nombre", "ad_id", "adset_id", "campaign_id")))
            return all(w in blob for w in palabras)
        anuncios = [a for a in anuncios if _match_busqueda(a)]

    fuente = db.get_config("fuente_ventas", "todas")

    if not anuncios:
        st.info("No hay datos para este filtro. En la barra lateral (o arriba) pulsa **Recargar** "
                "para traer los anuncios de Facebook.")
        return

    # Insights SIEMPRE a nivel anuncio (una sola llamada, reutilizable en todas las
    # vistas); se agregan por conjunto/campaña en Python.
    insights = _insights_cache(date_preset, "ad", since, until)
    fuente = db.get_config("fuente_ventas", "todas")
    ventas_agg = db.ventas_agg_por_ad(cutoff, hasta_dt,
                                      fuente if fuente != "meta" else "todas")
    filas = _construir_filas(anuncios, nivel, insights, ventas_agg, ahora)

    # Ventas realmente SIN ad_id (POST ID vacío), SIEMPRE dentro del rango de fechas
    # (ventas_agg ya viene filtrado por cutoff/hasta). Se suman al total. Las ventas
    # con un ID que no coincide con ningún anuncio (huérfanas) NO se cuentan aquí:
    # esas se explican en el "Cuadre de ventas" para no inflar el número.
    sin_adid = None
    if fuente != "meta":
        moneda_v = db.get_config("moneda_ventas", "auto")
        if moneda_v and moneda_v != "auto":
            rate_sa = fx.tasa_a_usd(moneda_v)
        else:
            from collections import Counter
            monc = Counter((a.get("cuenta_moneda") or "USD") for a in todos)
            rate_sa = fx.tasa_a_usd(monc.most_common(1)[0][0] if monc else "USD")
        sa_num, sa_ing_nat = 0, 0.0
        for ad_id, v in ventas_agg.items():
            aid = str(ad_id).strip().lower()
            if aid in ("", "nan", "none"):   # SOLO vacías; no huérfanas
                sa_num += int(v.get("num_ventas") or 0)
                sa_ing_nat += float(v.get("ingreso_total") or 0.0)
        if sa_num:
            sin_adid = {"num": sa_num, "ingreso_usd": sa_ing_nat * rate_sa}

    _render_totales(filas, sin_adid)
    _cuadre_ventas(todos, ventas_agg, filas, rango_lbl)

    _seccion_ia(filas, f"Vista por: {nivel_lbl} · estado: {filtro} · rango: {rango_lbl} · "
                       f"{len(filas)} elementos · montos en USD")

    if not insights:
        st.info("El **gasto**, CPM y conversaciones vienen de Facebook. Aún no se ven porque no "
                "hay conexión activa: agrega un token en **Configuración → Conexiones** y pulsa "
                "**Recargar**. Mientras tanto el gasto es estimado.")

    filas = _ordenar_filas(filas)
    st.markdown(_TABLA_CSS, unsafe_allow_html=True)
    cta_a, _ = st.columns([1.6, 4])
    cta_a.toggle("Nombres más anchos", key="f_ancho_nombre",
                 help="Ensancha la columna del nombre para leer completos los nombres "
                      "largos de conjuntos/anuncios (a costa de un poco de las otras columnas).")
    _barra_acciones_conjunto(filas)
    _render_lista_nativa(filas, nivel)


# Columnas ordenables: etiqueta -> (clave del dato, es_texto)
_COLS_SORT = {
    "nombre": ("nombre", True), "presupuesto": ("presupuesto", False),
    "gasto": ("gasto", False), "cpm": ("cpm", False), "num": ("num", False),
    "ingresos": ("ingresos", False), "ganancia": ("ganancia", False),
    "roas": ("roas", False), "conv": ("conv", False), "costo_conv": ("costo_conv", False),
    "costo_venta": ("costo_venta", False), "cuenta": ("cuenta", True),
}


def _ordenar_filas(filas):
    sort = st.query_params.get("sort", "roas")
    direc = st.query_params.get("dir", "desc")
    if sort not in _COLS_SORT:
        sort = "roas"
    clave, es_texto = _COLS_SORT[sort]

    def keyf(f):
        v = f.get(clave)
        if es_texto:
            return str(v or "").lower()
        return (v is None, v if v is not None else 0)
    filas.sort(key=keyf, reverse=(direc == "desc"))
    return filas


def _cuadre_ventas(todos, ventas_agg, filas, rango_lbl):
    """Desglose de las ventas de ESTA FECHA (ventas_agg ya viene filtrado por el rango):
    total en la base = mostradas + ocultas por filtro + sin ad id + huérfanas.
    Todo se cuenta SOLO dentro de la fecha del filtro."""
    ids_conocidos = {str(a.get("ad_id")) for a in todos}
    total_rango = sum(int(v.get("num_ventas") or 0) for v in ventas_agg.values())
    mostradas = sum(int(f["num"] or 0) for f in filas)
    sin_id, huerfanas, huerf_detalle = 0, 0, []
    for ad_id, v in ventas_agg.items():
        n = int(v.get("num_ventas") or 0)
        aid = str(ad_id).strip()
        if aid.lower() in ("", "nan", "none"):
            sin_id += n
        elif aid not in ids_conocidos:
            huerfanas += n
            huerf_detalle.append((aid, n))
    ocultas = max(0, total_rango - mostradas - sin_id - huerfanas)
    huerf_detalle.sort(key=lambda x: -x[1])
    cuadra = (ocultas == 0 and huerfanas == 0)
    titulo = f"🧮 Cuadre de ventas — {total_rango} en «{rango_lbl}»"
    with st.sidebar.expander(titulo, expanded=(not cuadra)):
        st.markdown(
            f"**{total_rango}** venta(s) registradas en la base para **«{rango_lbl}»** "
            "(solo esta fecha). Se reparten así:\n\n"
            f"- ✅ **{mostradas}** se muestran en la tabla (anuncios visibles).\n"
            f"- 🏷️ **{sin_id}** sin AD ID (fila vacía) — sí van en el total.\n"
            f"- 🔁 **{ocultas}** ocultas por filtros (Estado/País/Cuenta/Buscar): existen "
            "pero no pasan el filtro. Pon **Estado = Todos** y **País = Todos**.\n"
            f"- ❓ **{huerfanas}** huérfanas: su ID no coincide con ningún anuncio cargado "
            "(el anuncio es de una cuenta/token que no está conectado, o fue borrado). "
            "Conecta/recarga esa cuenta para atribuirlas.")
        if huerf_detalle:
            st.caption("IDs huérfanos (más ventas primero):")
            st.dataframe(
                pd.DataFrame([{"ad_id": a, "ventas": n} for a, n in huerf_detalle[:25]]),
                hide_index=True, use_container_width=True)
        st.caption("Si el total aquí ya es menor que en tu Excel/Sheet, esas ventas no se "
                   "importaron (valor 0 ignorado o duplicado). Revisa en "
                   "**Configuración → Fuentes de ventas → Probar**.")


def _render_totales(filas, sin_adid=None):
    gasto = sum(f["gasto"] or 0 for f in filas)
    ingresos = sum(f["ingresos"] or 0 for f in filas)
    num = sum(f["num"] or 0 for f in filas)
    conv = sum((f["conv"] or 0) for f in filas)
    # Ventas sin ad_id (o no atribuibles): se SUMAN al conteo/ingresos totales.
    sa_num = int((sin_adid or {}).get("num", 0) or 0)
    sa_ing = float((sin_adid or {}).get("ingreso_usd", 0.0) or 0.0)
    num += sa_num
    ingresos += sa_ing
    roas = (ingresos / gasto) if gasto > 0 else 0.0
    ganancia = ingresos - gasto
    costo_venta = (gasto / num) if num > 0 else 0.0
    costo_conv = (gasto / conv) if conv > 0 else 0.0

    # Sumas en moneda local (solo si todo el conjunto es una misma moneda).
    monedas = {(f.get("moneda") or "USD") for f in filas}
    mon = monedas.pop() if len(monedas) == 1 else None
    if mon and mon != "USD":
        gn = sum(f.get("gasto_nat") or 0 for f in filas)
        inn = sum(f.get("ingresos_nat") or 0 for f in filas)
        def loc(v):
            return f"{_num(v)} {mon}"
        nat = {
            "Gasto total": loc(gn), "Ingresos": loc(inn),
            "Costo/venta": loc(gn / num) if num > 0 else "", "Ganancia": loc(inn - gn),
            "Costo/conv": loc(gn / conv) if conv > 0 else "",
        }
    else:
        nat = {}

    tarjetas = [
        ("Gasto total", _usd(gasto), "#C7C4FF"),
        ("Ventas", f"{num:,}".replace(",", "."), "#e6e7ee"),
        ("Ingresos", _usd(ingresos), "#BFF2E2"),
        ("ROAS", f"{roas:.2f}x", _roas_color(roas)),
        ("Costo/venta", _usd(costo_venta), "#e6e7ee"),
        ("Costo/conv", _usd(costo_conv), "#e6e7ee"),
        ("Ganancia", ("+" if ganancia >= 0 else "") + _usd(ganancia),
         "#5ee7a0" if ganancia >= 0 else "#ff8b84"),
    ]
    cards = "".join(
        f'<div class="tcard"><div class="tlbl">{t}</div>'
        f'<div class="tval" style="color:{c}">{v}</div>'
        + (f'<div class="tnat">{nat.get(t)}</div>' if nat.get(t) else '')
        + '</div>'
        for t, v, c in tarjetas)
    st.markdown(
        '<style>.trow{display:flex;gap:12px;flex-wrap:wrap;margin:2px 0 10px;}'
        '.tcard{flex:1;min-width:120px;background:rgba(255,255,255,.03);backdrop-filter:blur(12px);'
        'border:1px solid rgba(255,255,255,.07);border-radius:16px;padding:14px 16px 13px;}'
        '.tlbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;font-weight:600;}'
        '.tval{font-family:Geist,sans-serif;font-weight:800;font-size:28px;margin-top:5px;line-height:1.1;}'
        '.tnat{font-family:Inter,sans-serif;font-size:12px;font-weight:500;'
        'color:#475569;margin-top:4px;}</style>'
        f'<div class="trow">{cards}</div>', unsafe_allow_html=True)

    # Fila explícita de ventas sin ad_id (no atribuibles a ningún anuncio).
    if sa_num:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;margin:-4px 0 10px;'
            'padding:8px 14px;border:1px dashed rgba(245,196,81,.5);border-radius:12px;'
            'background:rgba(245,196,81,.08);">'
            '<span style="font-size:13px;color:#f5c451;font-weight:700;">🏷️ VENTAS SIN AD ID</span>'
            f'<span style="font-size:13px;color:#e6e7ee;">{sa_num} venta(s) · {_usd(sa_ing)}</span>'
            '<span style="font-size:11.5px;color:#94a3b8;">— sí están incluidas en el total de '
            'arriba; no se pudieron atribuir a un anuncio (fila sin POST ID o ID que no coincide).'
            '</span></div>', unsafe_allow_html=True)


_GRID_TMPL = ("3.2fr .9fr .8fr .9fr .55fr .85fr .8fr .55fr .85fr .95fr .85fr .6fr")


def _estado_cell(f, nivel):
    if f["activos"] and (nivel == "ad" or f["activos"] == f["total"]):
        return '<span class="pill pill-run">Corriendo</span>'
    if f["activos"]:
        return f'<span class="pill pill-run">{f["activos"]}/{f["total"]}</span>'
    return '<span class="pill pill-off">Pausado</span>'


def _hace_amigable(dt, ahora) -> str:
    """Tiempo transcurrido en formato humano: minutos → horas → días → fecha.
    (Evita cosas como 'hace 191 horas'.)"""
    if not dt:
        return "—"
    seg = int((ahora - dt).total_seconds())
    if seg < 60:
        return "hace un momento"
    mins = seg // 60
    if mins < 60:
        return f"hace {mins} min"
    horas = mins // 60
    if horas < 24:
        return f"hace {horas} h"
    dias = horas // 24
    if dias <= 13:
        return f"hace {dias} día" + ("s" if dias != 1 else "")
    return _fecha_corta(dt.isoformat())


def _perf_help_text(f, ahora) -> str:
    """Texto (markdown) del tooltip del botón ⓘ: rendimiento desde el último cambio."""
    umod = f.get("ult_mod")
    if not umod:
        return ("Sin cambios de presupuesto registrados.\n\n"
                "Haz clic para ver el detalle y la gráfica de 7 días.")
    roas = f.get("roas_mod") or 0.0
    emoji = "🟢" if roas > 2 else "🟡" if roas >= 1 else "🔴"
    ventas = int(f.get("ventas_mod") or 0)
    ingreso = f.get("ingresos_mod") or 0.0
    gasto_mod = f.get("gasto_mod") or 0.0
    # El "$" activa el modo fórmula (LaTeX) del tooltip -> hay que escaparlo.
    def _d(v):
        return _usd(v).replace("$", "\\$")
    # Presupuesto: anterior → actual.
    p_act = f.get("presupuesto")
    p_ant = f.get("presup_anterior")
    if p_ant is not None and p_act is not None:
        presup_line = f"Presupuesto: **{_d(p_ant)}** → **{_d(p_act)}**  \n"
    elif p_act is not None:
        presup_line = f"Presupuesto: **{_d(p_act)}** (sin cambios previos)  \n"
    else:
        presup_line = ""
    return (
        f"**Rendimiento desde el último cambio** · {_hace_amigable(umod, ahora)}\n\n"
        f"{presup_line}"
        f"Ventas: **{ventas}**  \n"
        f"Gasto publicitario: **{_d(gasto_mod)}**  \n"
        f"Ingreso: **{_d(ingreso)}**  \n"
        f"ROAS: **{roas:.2f}x** {emoji}\n\n"
        f"Haz clic para ver el detalle y la gráfica de 7 días.")


def _perf_block_html(f, ahora) -> str:
    """Bloque 'Rendimiento desde el último cambio de presupuesto' (usado en la
    card de detalle). Datos en tiempo real desde SQLite."""
    umod = f.get("ult_mod")
    if not umod:
        return ('<div class="perf-block"><div class="perf-line">'
                '<span class="perf-time">Sin cambios de presupuesto registrados</span>'
                '</div></div>')
    mins = max(0, int((ahora - umod).total_seconds() // 60))
    hace = _hace_amigable(umod, ahora)
    roas = f.get("roas_mod") or 0.0
    ventas = int(f.get("ventas_mod") or 0)
    ingreso = f.get("ingresos_mod") or 0.0
    if roas > 2:
        col, emoji = "#5ee7a0", "🟢"
    elif roas >= 1:
        col, emoji = "#f5c451", "🟡"
    else:
        col, emoji = "#ff8b84", "🔴"
    pct = max(0.0, min(roas / 3.0, 1.0)) * 100.0
    alerta = mins > 30 and roas < 1.5
    cls = "perf-block perf-alert" if alerta else "perf-block"
    warn = '<span class="perf-warn">⚠️</span>' if alerta else ''
    return (
        f'<div class="{cls}">'
        f'<div class="perf-line">{warn}'
        f'<span class="perf-time">{hace}</span> <span class="perf-sep">|</span> '
        f'<b>{ventas}</b> vtas <span class="perf-sep">|</span> '
        f'<b>{_usd(ingreso)}</b> <span class="perf-sep">|</span> '
        f'ROAS <b style="color:{col}">{roas:.2f}x {emoji}</b></div>'
        f'<div class="perf-bar">'
        f'<div class="perf-fill" style="width:{pct:.0f}%"></div>'
        f'<div class="perf-marker"></div></div></div>')


def _limpiar_seleccion(filas):
    for f in filas:
        st.session_state.pop(f"sel_{f['sub']}", None)


def _bulk_estado(sel, activar):
    ok, err = 0, []
    for f in sel:
        r = fb.cambiar_estado(f["status_obj_id"], f.get("status_nivel", "ad"),
                              activar, f.get("conexion_id"))
        if r.get("ok"):
            db.set_estado_ads(f["ad_ids"], activar)
            ok += 1
        else:
            err.append(r.get("error", ""))
    verbo = "activado(s)" if activar else "pausado(s)"
    st.session_state["_estado_msg"] = ("ok" if ok else "err",
        f"{ok} {verbo}." + (f" {len(err)} con error." if err else ""))


def _barra_acciones_conjunto(filas):
    """Barra de acciones para modificar VARIOS anuncios a la vez (los seleccionados)."""
    sel = [f for f in filas if st.session_state.get(f"sel_{f['sub']}")]
    if not sel:
        st.caption("💡 Marca la casilla ☐ (izquierda) de varios anuncios para modificarlos "
                   "en conjunto: bajarles el presupuesto, pausarlos o activarlos a la vez.")
        return
    with st.container(border=True):
        st.markdown(f"**{len(sel)} seleccionado(s)** — acción en conjunto")
        modo = st.radio("Presupuesto:", ["Porcentaje % (sobre el de cada uno)",
                                         "Monto fijo (USD, igual para todos)"],
                        horizontal=True, key="bulk_modo")
        c1, c2, c3, c4, c5 = st.columns([1.4, 1.1, 0.9, 0.9, 1])
        if modo.startswith("Porcentaje"):
            accion = c1.radio("Acción", ["Reducir", "Aumentar"], horizontal=True,
                              key="bulk_acc")
            pct = c1.number_input("% de cambio", min_value=0.0, max_value=100.0,
                                  value=35.0, step=5.0, key="bulk_pct")
            factor = (1 - pct / 100.0) if accion == "Reducir" else (1 + pct / 100.0)
            btn_lbl = f"{accion} {pct:g}%"
        else:
            nuevo = c1.number_input("Nuevo presupuesto c/u (USD)", min_value=0.0,
                                    value=4.0, step=1.0, key="bulk_presup")
            factor = None
            btn_lbl = "Aplicar presupuesto"
        if c2.button(btn_lbl, type="primary", key="bulk_pres_btn",
                     use_container_width=True):
            hay = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False
            for f in sel:
                rate = f.get("rate_c") or 1.0
                # % -> se aplica al presupuesto propio de cada anuncio; fijo -> mismo monto.
                monto_usd = round(float(f["presupuesto"] or 0.0) * factor, 2) \
                    if factor is not None else nuevo
                nativo = (monto_usd / rate) if rate else monto_usd
                for aid in f["ad_ids"]:
                    db.cambiar_periodo(aid, nativo)
                if fb.SDK_DISPONIBLE and hay and f.get("budget_obj_id"):
                    fb.actualizar_presupuesto_async(
                        f["budget_obj_id"], f.get("budget_nivel", "adset"), nativo,
                        f.get("conexion_id"), etiqueta=str(f.get("nombre", ""))[:40])
            resumen = btn_lbl if factor is not None else f"→ {_usd(nuevo)}"
            st.session_state["_estado_msg"] = ("ok",
                f"Presupuesto de {len(sel)} anuncio(s): {resumen}. "
                "Aplicándose en Facebook.")
            _limpiar_seleccion(filas)
            st.rerun()
        if c3.button("⏸ Pausar", key="bulk_pause", use_container_width=True):
            _bulk_estado(sel, activar=False)
            _limpiar_seleccion(filas)
            st.rerun()
        if c4.button("▶ Activar", key="bulk_act", use_container_width=True):
            _bulk_estado(sel, activar=True)
            _limpiar_seleccion(filas)
            st.rerun()
        if c5.button("Limpiar selección", key="bulk_clear", use_container_width=True):
            _limpiar_seleccion(filas)
            st.rerun()


def _render_lista_nativa(filas, nivel):
    esc = _html.escape
    ahora = db.ahora()
    _msg = st.session_state.pop("_estado_msg", None)
    if _msg:
        (st.success if _msg[0] == "ok" else st.error)(_msg[1])
    primera = {"ad": "Anuncio", "adset": "Conjunto", "campaign": "Campaña"}[nivel]
    # Encabezado y datos usan la MISMA rejilla de columnas nativas -> alineación exacta.
    labels = [primera, "Cuenta", "Presup.", "Gastado", "Conv.", "C/conv",
              "CPM", "Ventas", "C/venta", "Ingr.", "Util.", "ROAS"]
    keys = ["nombre", "cuenta", "presupuesto", "gasto", "conv", "costo_conv",
            "cpm", "num", "costo_venta", "ingresos", "ganancia", "roas"]
    # Ancho de la columna del conjunto/anuncio (ampliable con el toggle).
    name_w = 5.2 if st.session_state.get("f_ancho_nombre") else 3.0
    GRID_W = [name_w, 0.85, 0.8, 0.85, 0.5, 0.8, 0.72, 0.5, 0.8, 0.9, 0.85, 0.6]
    ayudas = {"Presup.": "Presupuesto diario", "Gastado": "Importe gastado",
              "C/conv": "Costo por conversación", "CPM": "CPM / CTR",
              "C/venta": "Costo por venta", "Ingr.": "Ingresos",
              "Util.": "Utilidad (ganancia)", "Conv.": "Conversaciones",
              "ROAS": "Retorno sobre la inversión"}
    st.markdown(
        '<style>.meta-mini{font-size:9.5px;color:#7f8b9c;line-height:1.25;margin-top:1px;}'
        '.meta-mini b{color:#9fb0c2;font-weight:600;}'
        # Datos centrados en cada columna (igual que sus títulos).
        '.gcell,.gcell .big,.gcell .sub,.gcell .meta-mini{text-align:center !important;}'
        # Botones de encabezado (ordenar) CENTRADOS, igual que los valores, sin recuadro.
        '.stButton button[kind="tertiary"]{padding:0 !important;min-height:0 !important;'
        'color:#8fd6db !important;letter-spacing:0;line-height:1.1;'
        'justify-content:center !important;text-align:center !important;'
        'border:none !important;box-shadow:none !important;background:transparent !important;}'
        '.stButton button[kind="tertiary"]:focus,.stButton button[kind="tertiary"]:active,'
        '.stButton button[kind="tertiary"]:focus-visible{box-shadow:none !important;'
        'outline:none !important;border:none !important;color:#8fd6db !important;}'
        '.stButton button[kind="tertiary"] div[data-testid="stMarkdownContainer"]'
        '{width:100% !important;text-align:center !important;}'
        '.stButton button[kind="tertiary"] p{font-size:10px !important;font-weight:700 !important;'
        'text-transform:uppercase;margin:0 !important;white-space:nowrap;text-align:center !important;}'
        '.stButton button[kind="tertiary"]:hover p{color:#BFF2E2 !important;}</style>',
        unsafe_allow_html=True)

    # Encabezados clicables (botones nativos): reordenan SIN recargar la página.
    sort_c = st.query_params.get("sort", "roas")
    dir_c = st.query_params.get("dir", "desc")

    ACC = [0.5, 11.0, 0.55, 0.55]
    # Marcador para fijar (sticky) la fila de títulos al hacer scroll.
    st.markdown('<span class="tbl-hdr-anchor"></span>', unsafe_allow_html=True)
    hc = st.columns(ACC, vertical_alignment="center")
    hc[0].markdown('<div class="h2" style="text-align:center">On/Off</div>', unsafe_allow_html=True)
    with hc[1]:
        bcols = st.columns(GRID_W, gap="small", vertical_alignment="center")
        for col, label, key in zip(bcols, labels, keys):
            arrow = (" ▼" if dir_c == "desc" else " ▲") if sort_c == key else ""
            if col.button(f"{label}{arrow}", key=f"sort_{key}", type="tertiary",
                          help=ayudas.get(label), use_container_width=True):
                nd = "asc" if (sort_c == key and dir_c == "desc") else "desc"
                st.query_params["sort"] = key
                st.query_params["dir"] = nd
                st.rerun()
    hc[2].markdown('<div class="h2" style="text-align:center">Info</div>', unsafe_allow_html=True)
    hc[3].markdown('<div class="h2" style="text-align:center">Pres.</div>', unsafe_allow_html=True)
    st.markdown('<hr class="rowline">', unsafe_allow_html=True)

    for f in filas:
        g = f["ganancia"]
        gcls = "up" if g >= 0 else "down"
        cpm = _usd(f["cpm"]) if f["cpm"] is not None else "—"
        ctr = f'{f["ctr"]:.2f}%' if f["ctr"] is not None else "—"
        conv = int(f["conv"]) if f["conv"] is not None else "—"
        costoc = (_money_html(f["costo_conv"], f.get("costo_conv_nat"), f["moneda"])
                  if f.get("costo_conv") is not None else '<div class="big">—</div>')
        costov = (_money_html(f["costo_venta"], None, "USD")
                  if f.get("costo_venta") else '<div class="big">—</div>')
        # Color del nombre según estado: verde=activo, rojo=apagado, amarillo=mixto.
        a_n, t_n = f["activos"], f["total"]
        if t_n and a_n == t_n:
            est_col = "#10b981"
        elif a_n == 0:
            est_col = "#ff8b84"
        else:
            est_col = "#f5c451"
        # Fecha de creación (se mantiene bajo Cuenta).
        creado = _fecha_corta(f.get("creado"))
        creado_mini = f'<div class="meta-mini" title="Fecha de creación">Creado <b>{creado}</b></div>'
        # El rendimiento "desde el último cambio" ya NO va fijo en la fila (ruido
        # visual); sale en el tooltip del botón ⓘ (abajo) y en la card de detalle.
        # Degradado rojo SOLO para los que van muy mal: gastan y su ROAS < 1 (pierden).
        muy_mal = bool(f["gasto"] and f["gasto"] > 0 and (f["roas"] or 0) < 1.0)
        roas_alto = bool(f["roas"] and f["roas"] > 5)
        # Nombre coloreado por ESTADO (verde activo / rojo apagado / amarillo mixto);
        # acento verde a la izquierda si ROAS > 5.
        borde = "border-left:3px solid #10b981;padding-left:8px;" if roas_alto else ""
        nombre_html = (f'<div class="ad-name" style="{borde}color:{est_col};font-weight:700;'
                       f'font-size:16.5px;line-height:1.25;white-space:normal;word-break:break-word">'
                       f'{esc(str(f["nombre"]))[:120]}</div>')
        if muy_mal:
            nombre_html = (f'<div class="name-alert" title="Rinde muy mal: ROAS por '
                           f'debajo de 1x con gasto. Considera pausar o ajustar.">'
                           f'{nombre_html}</div>')
        gastado_cell = (_money_html(f["gasto"], f["gasto_nat"], f["moneda"])
                        + ('<div class="sub">est.</div>' if f["spend"] is None else ''))
        cells = [
            nombre_html,
            f'<div class="sub" style="font-size:12.5px">{esc(str(f["cuenta"]))[:18]}</div>'
            + creado_mini,
            _money_html(f["presupuesto"], f["presup_nat"], f["moneda"], "m-peri"),
            gastado_cell,
            f'<div class="big">{conv}</div>',
            costoc,
            f'<div class="big">{cpm}</div><div class="sub">{ctr} CTR</div>',
            f'<div class="big">{f["num"]}</div>',
            costov,
            _money_html(f["ingresos"], f["ingresos_nat"], f["moneda"], "m-mint"),
            f'<div class="big" style="color:{"#10b981" if g>=0 else "#ff8b84"};font-weight:700">'
            f'{"+" if g>=0 else ""}{_usd(g)}</div>',
            _roas_pill(f["roas"] or 0.0),
        ]
        est_txt = ("Activo" if est_col == "#10b981"
                   else "Apagado" if est_col == "#ff8b84" else "Mixto (unos activos)")
        rc = st.columns(ACC, vertical_alignment="center")
        rc[0].checkbox(" ", key=f"sel_{f['sub']}", label_visibility="collapsed",
                       help="Seleccionar para modificar en conjunto (varios a la vez)")
        rc[0].markdown(
            f'<div title="{est_txt}" style="text-align:center;color:{est_col};'
            f'font-size:15px;line-height:1;margin-bottom:-6px">●</div>',
            unsafe_allow_html=True)
        rc[0].toggle(" ", value=(f["activos"] > 0), key=f"tg_{f['sub']}",
                     on_change=_toggle_estado_cb, args=(f,),
                     label_visibility="collapsed",
                     help="Prender / Apagar en Facebook")
        with rc[1]:
            dcols = st.columns(GRID_W, gap="small", vertical_alignment="top")
            for col, cell in zip(dcols, cells):
                col.markdown(f'<div class="gcell">{cell}</div>', unsafe_allow_html=True)
        if rc[2].button("", icon=":material/info:", key=f"info_{f['sub']}",
                        help=_perf_help_text(f, ahora)):
            st.session_state["info_row"] = f
            _dialog_info()
        # Presupuesto y Duplicar: botones (no popovers) que abren un diálogo —
        # los popovers no se renderizaban en columnas angostas.
        if rc[3].button("", icon=":material/edit:", key=f"presb_{f['sub']}",
                        help="Modificar presupuesto"):
            st.session_state["pres_row"] = f
            _dialog_presupuesto()
        st.markdown('<hr class="rowline">', unsafe_allow_html=True)


@st.dialog("Detalle del anuncio", width="large")
def _dialog_info():
    f = st.session_state.get("info_row")
    if not f:
        return
    st.markdown(f"#### {f['nombre']}")
    st.caption(f"{f.get('cuenta','')} · ID del anuncio: {f.get('sub','')}")

    # Bloque "Rendimiento desde el último cambio" (mismo de la fila), en tiempo real.
    st.markdown(_TABLA_CSS, unsafe_allow_html=True)
    st.markdown("**Rendimiento desde el último cambio**", unsafe_allow_html=True)
    st.markdown(_perf_block_html(f, db.ahora()), unsafe_allow_html=True)

    rate = f.get("rate_c") or 1.0
    a = db.obtener_anuncio(f["ad_rep"]) or {}
    creado = _fecha_corta(a.get("fecha_creacion"))
    periodos = db.obtener_periodos(f["ad_rep"])
    ab = db.periodo_abierto(f["ad_rep"])
    if ab and db.a_fecha(ab["hora_inicio"]):
        ult_mod = _fecha_corta(db.a_fecha(ab["hora_inicio"]).isoformat())
    else:
        ult_mod = "—"
    # Presupuesto actual y el ANTERIOR (el período previo al cambio), en USD.
    presup_actual = f.get("presupuesto")
    presup_ant = (float(periodos[-2]["presupuesto"]) * rate) if len(periodos) >= 2 else None
    delta_presup = None
    if presup_ant is not None and presup_actual is not None:
        d = presup_actual - presup_ant
        delta_presup = f"{'+' if d >= 0 else ''}{_usd(d)} vs. anterior"

    st.markdown("&nbsp;", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Creado", creado)
    c2.metric("Presupuesto actual",
              _usd(presup_actual) if presup_actual is not None else "—", delta=delta_presup)
    c3.metric("Presupuesto anterior",
              _usd(presup_ant) if presup_ant is not None else "Sin cambios")
    c4.metric("Último cambio", ult_mod)

    # Métricas para verificar contra Facebook Ads (en un desplegable para no saturar)
    with st.expander("Métricas del rango (para verificar con Facebook Ads)"):
        ver = {
            "Gasto (USD)": _usd(f["gasto"]),
            "Gasto (moneda cuenta)": (f"{_num(f.get('spend_nat'))} {f['moneda']}"
                                      if f.get("spend_nat") is not None else "—"),
            "Impresiones": (f"{int(f['impresiones']):,}".replace(",", ".")
                            if f.get("impresiones") is not None else "—"),
            "Clics": (f"{int(f['clics']):,}".replace(",", ".")
                      if f.get("clics") is not None else "—"),
            "CPM (USD)": _usd(f["cpm"]) if f["cpm"] is not None else "—",
            "CTR": f'{f["ctr"]:.2f}%' if f["ctr"] is not None else "—",
            "Compras (Meta)": (int(f["compras_meta"]) if f.get("compras_meta") is not None else "—"),
            "Valor compras (Meta, USD)": (_usd((f.get("compras_valor_meta") or 0) * rate)
                                          if f.get("compras_valor_meta") is not None else "—"),
            "Conversaciones": int(f["conv"]) if f["conv"] is not None else "—",
            "Ventas (tus fuentes)": f["num"],
            "Ingresos (tus fuentes, USD)": _usd(f["ingresos"]),
        }
        st.table(pd.DataFrame(list(ver.items()), columns=["Métrica", "Valor"]))

    # Gráfica de los últimos 7 días: gasto (Facebook) e ingresos (tus ventas).
    ahora = db.ahora()
    dias = [(ahora - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    serie = fb.serie_diaria(f.get("status_obj_id"), f.get("status_nivel", "ad"),
                            f.get("conexion_id"))
    gasto_por_dia = {s["date"]: s["spend"] * rate for s in serie if s.get("date")}
    vd = db.ventas_diarias(f["ad_ids"], ahora - timedelta(days=7))
    ing_por_dia = {d: v["ingreso_nat"] * rate for d, v in vd.items()}

    gastos = [round(gasto_por_dia.get(d, 0.0), 2) for d in dias]
    ingresos = [round(ing_por_dia.get(d, 0.0), 2) for d in dias]
    etiquetas = [d[5:] for d in dias]  # MM-DD

    fig = go.Figure()
    fig.add_trace(go.Bar(x=etiquetas, y=gastos, name="Gasto (USD)", marker_color="#7c3aed"))
    fig.add_trace(go.Scatter(x=etiquetas, y=ingresos, name="Ingresos (USD)", mode="lines+markers",
                             line=dict(color="#10b981", width=3)))
    fig.update_layout(height=280, margin=dict(t=10, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=1.15),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color="#c3ccd6"))
    st.plotly_chart(fig, use_container_width=True)
    if not serie:
        st.caption("El gasto por día se llena cuando hay conexión de Facebook. Los ingresos "
                   "salen de tus ventas importadas.")


@st.dialog("Modificar presupuesto")
def _dialog_presupuesto():
    f = st.session_state.get("pres_row")
    if f:
        _pop_presupuesto(f)


@st.dialog("Duplicar")
def _dialog_duplicar():
    f = st.session_state.get("dup_row")
    if f:
        _pop_duplicar(f)


def _toggle_estado_cb(f):
    """Prende/apaga en Facebook al mover el toggle."""
    key = f"tg_{f['sub']}"
    activar = bool(st.session_state.get(key))
    r = fb.cambiar_estado(f["status_obj_id"], f.get("status_nivel", "ad"),
                          activar, f.get("conexion_id"))
    if r["ok"]:
        db.set_estado_ads(f["ad_ids"], activar)
        st.session_state["_estado_msg"] = ("ok",
            f"{f['nombre']}: {'activado' if activar else 'pausado'} en Facebook.")
    else:
        st.session_state[key] = not activar  # revertir el toggle
        st.session_state["_estado_msg"] = ("err", f"No se pudo cambiar el estado: {r['error']}")


def _pop_presupuesto(f):
    st.markdown(f"**{esc_nombre(f)}**")
    actual = float(f["presupuesto"] or 0.0)
    st.caption(f"Presupuesto actual: {_usd(actual)}"
               + (f" ({_num(f['presup_nat'])} {f['moneda']})" if (f['moneda'] or 'USD').upper() != 'USD' else ""))
    modo = st.radio("¿Cómo lo cambias?", ["Monto fijo (USD)", "Porcentaje %"],
                    horizontal=True, key=f"presmodo_{f['sub']}")
    if modo == "Porcentaje %":
        cpa, cpb = st.columns([1, 1])
        accion = cpa.radio("Acción", ["Reducir", "Aumentar"], horizontal=True,
                           key=f"presacc_{f['sub']}")
        pct = cpb.number_input("% de cambio", min_value=0.0, max_value=100.0,
                               value=35.0, step=5.0, key=f"prespct_{f['sub']}")
        factor = (1 - pct / 100.0) if accion == "Reducir" else (1 + pct / 100.0)
        nuevo = round(actual * factor, 2)
        signo = "−" if accion == "Reducir" else "+"
        st.info(f"{accion} {pct:g}%:  {_usd(actual)}  →  **{_usd(nuevo)}**  ({signo}{_usd(abs(nuevo-actual))})")
    else:
        nuevo = st.number_input("Nuevo presupuesto diario (USD)", min_value=0.0,
                                value=actual, step=1.0, key=f"pres_{f['sub']}")
    if (f["moneda"] or "USD").upper() != "USD":
        st.caption(f"Se enviará a Facebook ≈ {_num(nuevo / (f['rate_c'] or 1))} {f['moneda']}")
    if st.button("Aplicar", type="primary", key=f"presbtn_{f['sub']}",
                 use_container_width=True):
        # Rápido: aplica local al instante y empuja a Facebook en SEGUNDO PLANO.
        rate = f.get("rate_c") or 1.0
        nativo = (nuevo / rate) if rate else nuevo
        for aid in f["ad_ids"]:
            db.cambiar_periodo(aid, nativo)
        hay = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False
        if fb.SDK_DISPONIBLE and hay and f.get("budget_obj_id"):
            fb.actualizar_presupuesto_async(
                f["budget_obj_id"], f.get("budget_nivel", "adset"), nativo,
                f.get("conexion_id"), etiqueta=str(f.get("nombre", ""))[:40])
            st.session_state["_estado_msg"] = (
                "ok", f"Presupuesto de '{str(f['nombre'])[:35]}' → {_usd(nuevo)}. "
                "Aplicándose en Facebook en segundo plano.")
        else:
            st.session_state["_estado_msg"] = (
                "ok", "Presupuesto guardado localmente (sin conexión de Facebook).")
        st.rerun()


def _pop_duplicar(f):
    # Duplica según el NIVEL de la vista actual (anuncio / conjunto / campaña).
    nivel = f.get("status_nivel", "ad")
    obj_id = f.get("status_obj_id")
    lbl = {"ad": "anuncio", "adset": "conjunto", "campaign": "campaña"}.get(nivel, "anuncio")
    st.markdown(f"**Duplicar {lbl}: {esc_nombre(f)}**")
    st.caption(f"Estás en la vista **{lbl}**, así que se duplica el {lbl} completo "
               "en su misma campaña.")
    hay = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False
    if not hay:
        st.info("Necesitas una conexión de Facebook activa para duplicar.")
        return
    n = st.number_input("Número de copias", min_value=1, max_value=10, value=4, step=1,
                        key=f"dupn_{f['sub']}")
    presu = st.number_input(f"Presupuesto por copia (USD)", min_value=0.0,
                            value=4.0, step=1.0, key=f"dupp_{f['sub']}",
                            help="Por defecto 4 USD por copia.")
    activar = st.toggle("Activar copias de inmediato", value=False, key=f"dupa_{f['sub']}")
    reutilizar = True
    if nivel == "ad":
        reutilizar = st.toggle("Conservar interacciones (usar la misma publicación)",
                               value=True, key=f"dupr_{f['sub']}",
                               help="Fuerza a la copia a usar el MISMO post del original, así "
                                    "arrastra los likes, comentarios y compartidos.")
    else:
        st.caption("Las interacciones se conservan si los anuncios del "
                   f"{lbl} usan publicaciones existentes (deep copy de Facebook).")
    if st.button("Duplicar ahora", type="primary", key=f"dupbtn_{f['sub']}",
                 use_container_width=True):
        nativo = presu / (f["rate_c"] or 1)
        with st.spinner(f"Duplicando {lbl} en Facebook..."):
            res = fb.duplicar_objeto(nivel, obj_id, int(n), float(nativo),
                                     activar=bool(activar), conexion_id=f.get("conexion_id"),
                                     cuenta_id=f.get("cuenta_id"),
                                     cuenta_nombre=f.get("cuenta_nombre"),
                                     reutilizar_post=bool(reutilizar))
        ok = res.get("exitosas", [])
        fail = res.get("fallidas", [])
        if res.get("error_global"):
            st.error(res["error_global"])
        if ok:
            st.success(f"{len(ok)} copia(s) de {lbl} creada(s).")
            if nivel == "ad" and reutilizar:
                nrp = res.get("post_reutilizado", 0)
                if res.get("post_id") and nrp:
                    st.info(f"✅ {nrp} copia(s) usan la misma publicación "
                            f"({res['post_id']}) → conservan interacciones.")
                elif not res.get("post_id"):
                    st.warning("No pude leer la publicación del original (creativo especial); "
                               "las copias quedaron con su propio post.")
            if nivel != "ad":
                st.info("Pulsa **🔄 Recargar** (arriba a la derecha) para ver las copias "
                        "con sus anuncios en la tabla.")
        for x in fail:
            st.error(f"{x['nombre']}: {x['error']}")


def esc_nombre(f):
    return _html.escape(str(f.get("nombre", "")))[:52]


# --------------------------------------------------------------------------- #
#  Render de la tabla rica (HTML)
# --------------------------------------------------------------------------- #
_TABLA_CSS = """
<style>
.tbl-wrap { overflow-x:auto; border:1px solid rgba(255,255,255,.08); border-radius:18px;
    background:rgba(23,26,34,.55); backdrop-filter:blur(16px);
    box-shadow:0 12px 34px rgba(0,0,0,.28), inset 0 1px 0 rgba(255,255,255,.04); }
table.ads { width:100%; border-collapse:collapse; font-size:12px; color:#e6e7ee;
    background:transparent; min-width:1080px; }
table.ads thead th { text-align:left; font-weight:700; color:#8fd6db; font-size:10px;
    text-transform:uppercase; letter-spacing:.06em; padding:12px 12px; white-space:nowrap;
    background:linear-gradient(180deg, rgba(140,210,215,.08), transparent);
    border-bottom:1px solid rgba(140,210,215,.18); }
table.ads td { padding:11px 12px; border-bottom:1px solid rgba(255,255,255,.05); vertical-align:middle; }
table.ads tbody tr:hover td { background:linear-gradient(90deg, rgba(140,210,215,.07), rgba(199,196,255,.05)); }
.big { font-size:15px; font-weight:600; color:#f2f4f8; line-height:1.2; }
.sub { font-size:12px; color:#9aa7b6; }
.m-mint{ color:#BFF2E2; } .m-lav{ color:#E6D5FF; } .m-peri{ color:#C7C4FF; }
/* Alerta: solo el nombre, con difuminado rojo hacia la derecha */
.name-alert{ background:linear-gradient(90deg, rgba(239,68,68,.30), rgba(239,68,68,0) 88%);
    border-radius:8px; padding:3px 10px; margin:-3px -10px; }
.alert-badge{ display:inline-flex; align-items:center; justify-content:center;
    width:20px; height:20px; border-radius:50%; background:#ef4444; color:#fff;
    font-weight:800; font-size:12px; cursor:help; box-shadow:0 0 10px rgba(239,68,68,.4); }
.ok-dot{ display:inline-block; width:8px; height:8px; border-radius:50%;
    background:rgba(94,231,160,.5); }
.pill { display:inline-flex; align-items:center; gap:6px; padding:3px 10px;
    border-radius:999px; font-size:10.5px; font-weight:600; }
.pill-run { background:rgba(74,222,128,.14); color:#7ef0a9; box-shadow:0 0 12px rgba(74,222,128,.15); }
.pill-off { background:rgba(148,163,184,.14); color:#b6c0cd; }
.dot { width:7px; height:7px; border-radius:50%; display:inline-block; }
.badge { display:inline-block; padding:2px 8px; border-radius:6px; font-size:10px;
    font-weight:700; color:#0b1220; }
.bar { height:5px; background:rgba(255,255,255,.08); border-radius:4px; margin-top:5px; overflow:hidden; }
.bar > span { display:block; height:100%; background:linear-gradient(90deg,#8cd2d7,#c7c4ff); }
.chip { display:inline-block; background:rgba(191,242,226,.14); color:#BFF2E2; border-radius:6px;
    padding:2px 8px; font-size:10px; font-weight:600; }
.up { color:#5ee7a0; } .down { color:#ff8b84; } .flat { color:#9ca3af; }
.hcol { color:#8fd6db; font-size:10px; font-weight:700; text-transform:uppercase;
    letter-spacing:.06em; padding:4px 0 2px; }
hr.rowline { margin:6px 0; border:none; border-top:1px solid rgba(255,255,255,.06); }
/* Fija (sticky) la fila de títulos de la tabla al hacer scroll hacia abajo.
   El hermano siguiente al marcador puede ser stLayoutWrapper o stHorizontalBlock. */
[data-testid="stElementContainer"]:has(.tbl-hdr-anchor) + *{
    position:sticky; top:0; z-index:30; background:#080810;
    box-shadow:0 6px 12px -8px rgba(0,0,0,.7); padding:6px 0 4px;
    border-bottom:1px solid rgba(255,255,255,.08);
}
/* Bloque "Rendimiento desde el último cambio" (fila + card) */
.perf-block{ background:rgba(255,255,255,.03); border-radius:8px; padding:6px 10px;
    margin-top:5px; border:1px solid transparent; }
.perf-block.perf-alert{ border:1px solid rgba(239,68,68,.4); }
.perf-line{ font-size:10.5px; color:#dbe2ea; line-height:1.35; }
.perf-line b{ color:#fff; font-weight:600; }
.perf-time{ color:#8a93a6; }
.perf-sep{ color:#5a6474; margin:0 1px; }
.perf-warn{ margin-right:5px; animation:perfBlink 1s steps(1) infinite; }
@keyframes perfBlink{ 50%{ opacity:.15; } }
.perf-bar{ position:relative; height:5px; border-radius:3px;
    background:rgba(255,255,255,.08); margin-top:5px; }
.perf-fill{ height:100%; border-radius:3px;
    background:linear-gradient(90deg,#7c3aed,#10b981); transition:width .4s ease; }
.perf-marker{ position:absolute; top:-1px; left:66.6%; width:2px; height:7px;
    background:#e6e7ee; opacity:.8; border-radius:1px; }
</style>
"""


def _c_estado(f):
    if f["activo"]:
        return ('<span class="pill pill-run"><span class="dot" style="background:#4ade80"></span>'
                'Corriendo</span>')
    return ('<span class="pill pill-off"><span class="dot" style="background:#9ca3af"></span>'
            'Pausado</span>')


def _c_presupuesto(f):
    return (f'<div class="big">{_usd(f["presupuesto"])}</div>'
            f'<div class="sub">ABO</div>')


def _c_gasto(f):
    if f["spend"] is None:
        return (f'<div class="big">{_usd(f["gasto"])}</div>'
                f'<div class="sub">estimado</div>')
    pct = f["pct"]
    pct_txt = f"{pct:.0f}%" if pct is not None else "—"
    ancho = min(100, max(0, pct)) if pct is not None else 0
    return (f'<div class="big">{_usd(f["spend"])}</div>'
            f'<div class="sub">{pct_txt}</div>'
            f'<div class="bar"><span style="width:{ancho:.0f}%"></span></div>')


def _c_cpm_ctr(f):
    cpm = _usd(f["cpm"]) if f["cpm"] is not None else "—"
    ctr = f'{f["ctr"]:.2f}% CTR' if f["ctr"] is not None else "— CTR"
    return f'<div class="big">{cpm} <span class="sub">CPM</span></div><div class="sub">{ctr}</div>'


def _c_ventas(f):
    return (f'<div class="big">{_usd(f["val_venta"])} <span class="sub">/ venta</span></div>'
            f'<div class="sub">{_usd(f["ingresos"])} ingresos</div>'
            f'<div class="chip">{f["num"]} venta{"s" if f["num"]!=1 else ""}</div>')


def _c_ganancia(f):
    g = f["ganancia"]
    cls = "up" if g >= 0 else "down"
    signo = "+" if g >= 0 else ""
    return f'<div class="big {cls}">{signo}{_usd(g)}</div>'


def _c_roas(f):
    col = _roas_color(f["roas"])
    return f'<div class="big" style="color:{col};font-size:14px">{f["roas"]:.2f}x</div>'


def _c_salud(f):
    b = f["badge"]
    spark = _spark_svg(f["serie"], color=b["color"])
    return (f'{spark}'
            f'<div><span class="badge" style="background:{b["color"]}">{b["texto"]}</span></div>')


def _c_conv(f):
    if f["conv"] is None:
        return '<div class="big">—</div>'
    cc = _usd(f["costo_conv"]) if f["costo_conv"] is not None else "—"
    return (f'<div class="big">{int(f["conv"])}</div>'
            f'<div class="sub">{cc} / conv.</div>')


def _c_costo_conv(f):
    if f.get("costo_conv") is None:
        return '<div class="big">—</div>'
    return f'<div class="big">{_usd(f["costo_conv"])}</div><div class="sub">por conversación</div>'


def _c_accion(f):
    ac = f["accion"]
    cls = {"up": "up", "down": "down"}.get(ac["direccion"], "flat")
    hace = f'<div class="sub">{_html.escape(ac["hace"])}</div>' if ac["hace"] else ""
    return f'<div class="{cls}" style="font-weight:600">{_html.escape(ac["texto"])}</div>{hace}'


def _celda_gasto(f):
    base = f'<div class="big">{_usd(f["gasto"])}</div>'
    if f["spend"] is None:
        return base + '<div class="sub">estimado</div>'
    pct = f["pct"]
    pct_txt = f"{pct:.0f}% del presup." if pct is not None else ""
    ancho = min(100, max(0, pct)) if pct is not None else 0
    return base + f'<div class="sub">{pct_txt}</div><div class="bar"><span style="width:{ancho:.0f}%"></span></div>'


def _celda_estado(f, nivel):
    if nivel == "ad":
        if f["activos"]:
            return '<span class="pill pill-run"><span class="dot" style="background:#4ade80"></span>Corriendo</span>'
        return '<span class="pill pill-off"><span class="dot" style="background:#9ca3af"></span>Pausado</span>'
    return f'<div class="big">{f["activos"]}/{f["total"]}</div><div class="sub">activos</div>'


def _render_tabla(filas, nivel):
    primera = {"ad": "Anuncio", "adset": "Conjunto de anuncios", "campaign": "Campaña"}[nivel]
    cols = [primera, "Estado", "Cuenta", "Presupuesto", "Gasto", "CPM / CTR",
            "Ventas", "Ingresos", "Ganancia", "ROAS", "Conversaciones", "Costo/conv."]
    ths = "".join(f"<th>{c}</th>" for c in cols)
    trs = []
    for f in filas:
        nombre = _html.escape(str(f["nombre"]))[:60]
        sub = _html.escape(str(f["sub"]))
        cuenta = _html.escape(str(f["cuenta"]))[:34]
        g = f["ganancia"]
        gcls = "up" if g >= 0 else "down"
        cpm = _usd(f["cpm"]) if f["cpm"] is not None else "—"
        ctr = f'{f["ctr"]:.2f}% CTR' if f["ctr"] is not None else "— CTR"
        val_venta = (f["ingresos"] / f["num"]) if f["num"] else 0.0
        conv = f'{int(f["conv"])}' if f["conv"] is not None else "—"
        costoc = _usd(f["costo_conv"]) if f.get("costo_conv") is not None else "—"
        celdas = [
            f'<div class="big">{nombre}</div><div class="sub">{sub}</div>',
            _celda_estado(f, nivel),
            f'<div class="sub">{cuenta}</div>',
            f'<div class="big m-peri">{_usd(f["presupuesto"])}</div><div class="sub">diario</div>',
            _celda_gasto(f),
            f'<div class="big">{cpm}</div><div class="sub">{ctr}</div>',
            f'<div class="big">{f["num"]}</div><div class="sub">{_usd(val_venta)}/venta</div>',
            f'<div class="big m-mint">{_usd(f["ingresos"])}</div>',
            f'<div class="big {gcls}">{"+" if g>=0 else ""}{_usd(g)}</div>',
            f'<div class="big" style="color:{_roas_color(f["roas"])};font-size:14px">{f["roas"]:.2f}x</div>',
            f'<div class="big">{conv}</div>',
            f'<div class="big">{costoc}</div>',
        ]
        tds = "".join(f"<td>{c}</td>" for c in celdas)
        trs.append(f"<tr>{tds}</tr>")
    return (f'<div class="tbl-wrap"><table class="ads"><thead><tr>{ths}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')


# --------------------------------------------------------------------------- #
#  Sección: Cambios rápidos de presupuesto en lote
# --------------------------------------------------------------------------- #
def seccion_lote():
    st.header("Cambios rápidos de presupuesto en lote")
    ahora = db.ahora()
    anuncios = db.obtener_anuncios(solo_activos=True)
    if not anuncios:
        st.info("No hay anuncios activos.")
        return

    filas = []
    for a in anuncios:
        m = calculos.metricas_periodo_actual(a["ad_id"], ahora)
        actual = m["presupuesto"] if m else 0.0
        filas.append({
            "ad_id": a["ad_id"],
            "Anuncio": a["nombre"],
            "Presupuesto actual": float(actual or 0.0),
            "Nuevo presupuesto": float(actual or 0.0),
        })
    df = pd.DataFrame(filas)

    editado = st.data_editor(
        df,
        use_container_width=True, hide_index=True, key="editor_lote",
        column_config={
            "ad_id": None,  # oculto
            "Anuncio": st.column_config.TextColumn(disabled=True),
            "Presupuesto actual": st.column_config.NumberColumn(format="$%.2f", disabled=True),
            "Nuevo presupuesto": st.column_config.NumberColumn(format="$%.2f", min_value=0.0),
        },
    )

    if st.button("Aplicar todos los cambios", type="primary"):
        cambios = editado[abs(editado["Nuevo presupuesto"] - editado["Presupuesto actual"]) > 0.01]
        if cambios.empty:
            st.info("No hay cambios que aplicar.")
        else:
            resultados = []
            for _, fila in cambios.iterrows():
                r = cambio_presupuesto_completo(fila["ad_id"], float(fila["Nuevo presupuesto"]))
                resultados.append({
                    "Anuncio": fila["Anuncio"],
                    "Nuevo": _fmt_money(fila["Nuevo presupuesto"]),
                    "Resultado": ("OK" if r["ok"] and not r.get("solo_local")
                                  else ("Solo local" if r.get("solo_local") else f"{r['error']}")),
                })
            st.dataframe(pd.DataFrame(resultados), use_container_width=True, hide_index=True)
            time.sleep(1.0)


# --------------------------------------------------------------------------- #
#  Registro rápido de venta manual (útil en despliegue online)
# --------------------------------------------------------------------------- #
def seccion_venta_manual():
    """Registrar venta manual, en la barra lateral izquierda."""
    with st.sidebar.expander("➕ Registrar venta", expanded=False):
        st.caption("Registra una venta al instante (se atribuye al período de presupuesto "
                   "activo del anuncio).")
        anuncios = db.obtener_anuncios(solo_activos=False)
        opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}

        with st.form("form_venta_manual"):
            if opciones:
                modo_libre = st.checkbox("Escribir ID manualmente", value=False)
                if modo_libre:
                    ad_id = st.text_input("ID_Anuncio")
                else:
                    sel = st.selectbox("Anuncio", list(opciones.keys()))
                    ad_id = opciones[sel]
            else:
                ad_id = st.text_input("ID_Anuncio (aún no hay anuncios cargados)")
            valor = st.number_input("Valor de la venta", min_value=0.0, value=0.0, step=1.0)
            usar_ahora = st.toggle("Usar hora actual", value=True)
            hora = None
            if not usar_ahora:
                f = st.date_input("Fecha")
                h = st.time_input("Hora")
                hora = datetime.combine(f, h)
            enviado = st.form_submit_button("Registrar venta", type="primary",
                                            use_container_width=True)

        if enviado:
            r = watcher.registrar_venta_manual(ad_id, valor, hora=hora)
            if r["ok"]:
                destino = (f"período {r['periodo_id']}" if r["periodo_id"]
                           else "sin período (no había presupuesto activo a esa hora)")
                st.success(f"Venta registrada y atribuida a {destino}.")
            else:
                st.error(f"{r['error']}")
            time.sleep(0.8)
            st.rerun()


# --------------------------------------------------------------------------- #
#  Sección 2 — Detalle por anuncio
# --------------------------------------------------------------------------- #
def seccion_detalle():
    st.header("2 · Detalle por anuncio")
    anuncios = db.obtener_anuncios(solo_activos=False)
    if not anuncios:
        st.info("Aún no hay anuncios en la base de datos.")
        return

    opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}
    sel = st.selectbox("Selecciona un anuncio", list(opciones.keys()), key="detalle_sel")
    ad_id = opciones[sel]
    anuncio = db.obtener_anuncio(ad_id)
    ahora = db.ahora()

    metricas = calculos.metricas_todos_los_periodos(ad_id, ahora)
    if not metricas:
        st.warning("Este anuncio no tiene períodos registrados todavía.")
    else:
        # Tabla de todos los períodos
        df = pd.DataFrame([{
            "Período": m["periodo_id"],
            "Estado": m["estado"],
            "Presupuesto": m["presupuesto"],
            "Inicio": db.a_texto(m["hora_inicio"]) if m["hora_inicio"] else "—",
            "Fin": db.a_texto(m["hora_fin"]) if m["hora_fin"] else "— (abierto)",
            "Duración (min)": m["duracion_minutos"],
            "Ventas": m["num_ventas"],
            "Ingreso": m["ingreso_total"],
            "Gasto estimado": m["gasto_estimado"],
            "ROAS": m["roas"],
            "Costo/venta": m["costo_por_venta"],
        } for m in metricas])

        st.subheader("Períodos del día")
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={
                "Presupuesto": st.column_config.NumberColumn(format="$%.2f"),
                "Ingreso": st.column_config.NumberColumn(format="$%.2f"),
                "Gasto estimado": st.column_config.NumberColumn(format="$%.2f"),
                "ROAS": st.column_config.NumberColumn(format="%.2f x"),
                "Costo/venta": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        c1, c2 = st.columns(2)

        # Gráfica de barras ROAS por período (Plotly)
        with c1:
            st.subheader("ROAS por período")
            df_bar = df.copy()
            df_bar["Etiqueta"] = df_bar["Período"].astype(str) + " ($" + df_bar["Presupuesto"].round(0).astype(int).astype(str) + ")"
            colores = ["#2ecc71" if r > 2 else "#f1c40f" if r >= 1 else "#e74c3c" for r in df_bar["ROAS"]]
            fig = go.Figure(go.Bar(x=df_bar["Etiqueta"], y=df_bar["ROAS"],
                                   marker_color=colores, text=df_bar["ROAS"].round(2)))
            fig.add_hline(y=1, line_dash="dot", line_color="gray")
            fig.add_hline(y=2, line_dash="dot", line_color="green")
            fig.update_layout(xaxis_title="Período (presupuesto)", yaxis_title="ROAS",
                              height=380, margin=dict(t=20, b=20))
            st.plotly_chart(fig, use_container_width=True)

        # Línea de tiempo de cambios de presupuesto
        with c2:
            st.subheader("Línea de tiempo de presupuesto")
            fig2 = go.Figure()
            xs, ys, textos = [], [], []
            for m in metricas:
                if not m["hora_inicio"]:
                    continue
                fin = m["hora_fin"] or ahora
                xs += [m["hora_inicio"], fin, None]
                ys += [m["presupuesto"], m["presupuesto"], None]
            fig2.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(width=3, color="#3498db"),
                                      name="Presupuesto"))
            # Marcadores en cada cambio (inicio de período)
            mx = [m["hora_inicio"] for m in metricas if m["hora_inicio"]]
            my = [m["presupuesto"] for m in metricas if m["hora_inicio"]]
            mt = [f"${m['presupuesto']:.0f}" for m in metricas if m["hora_inicio"]]
            fig2.add_trace(go.Scatter(x=mx, y=my, mode="markers+text", text=mt,
                                      textposition="top center",
                                      marker=dict(size=12, color="#e67e22"), name="Cambio"))
            fig2.update_layout(xaxis_title="Hora", yaxis_title="Presupuesto diario ($)",
                               height=380, margin=dict(t=20, b=20), showlegend=False)
            st.plotly_chart(fig2, use_container_width=True)


# --------------------------------------------------------------------------- #
#  Parte 7 — Duplicación de anuncios
# --------------------------------------------------------------------------- #
def _bloque_duplicar(ad_id, anuncio, ahora):
    st.subheader("Duplicar anuncio")
    m = calculos.metricas_periodo_actual(ad_id, ahora)
    presup_def = m["presupuesto"] if m else 0.0

    with st.expander("Abrir formulario de duplicación"):
        with st.form(f"form_dup_{ad_id}"):
            c1, c2, c3 = st.columns(3)
            with c1:
                n = st.number_input("Número de copias", min_value=1, max_value=10, value=4, step=1)
            with c2:
                presup = st.number_input("Presupuesto por copia", min_value=0.0,
                                         value=float(presup_def or 0.0), step=1.0)
            with c3:
                activar = st.toggle("Activar copias inmediatamente", value=False)
                st.caption("Si está apagado, las copias se crean en PAUSED.")

            enviado = st.form_submit_button("Duplicar ahora", type="primary")

        if enviado:
            hay_conexion = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False
            if not hay_conexion:
                st.error("No hay conexión de Facebook disponible. Agrega un token en "
                         "**Conexiones** o revisa el .env. No se puede duplicar.")
                return
            with st.spinner(f"Duplicando {n} veces en Facebook..."):
                res = fb.duplicar_anuncio(
                    ad_id, int(n), float(presup), activar=bool(activar),
                    conexion_id=anuncio.get("conexion_id") if anuncio else None,
                    cuenta_id=anuncio.get("cuenta_id") if anuncio else None,
                    cuenta_nombre=anuncio.get("cuenta_nombre") if anuncio else None)

            if res.get("error_global"):
                st.error(f"{res['error_global']}")

            exitosas = res.get("exitosas", [])
            fallidas = res.get("fallidas", [])

            if exitosas:
                st.success(f"{len(exitosas)} copia(s) creada(s) correctamente.")
                st.dataframe(
                    pd.DataFrame([{"Nombre": e["nombre"], "Nuevo ad_id": e["ad_id"],
                                   "adset_id": e.get("adset_id"), "Presupuesto": _fmt_money(e["presupuesto"])}
                                  for e in exitosas]),
                    use_container_width=True, hide_index=True,
                )
            if fallidas:
                st.error(f"{len(fallidas)} copia(s) fallaron:")
                for f in fallidas:
                    st.write(f"- **{f['nombre']}**: {f['error']}")
            if not exitosas and not fallidas and not res.get("error_global"):
                st.info("No se creó ninguna copia.")


# --------------------------------------------------------------------------- #
#  Sección 3 — Alertas
# --------------------------------------------------------------------------- #
def seccion_alertas():
    st.header("3 · Alertas")
    alertas = calculos.evaluar_alertas(db.ahora(), umbral_minutos=30, umbral_roas=1.5)
    if not alertas:
        st.success("Sin alertas. Ningún anuncio lleva +30 min con ROAS < 1.5x.")
        return
    for a in alertas:
        st.error(
            f"**{a['nombre']}** — presupuesto {_fmt_money(a['presupuesto'])}, "
            f"lleva **{a['antiguedad']}** en este presupuesto y su ROAS actual es "
            f"**{a['roas']:.2f}x** ({a['num_ventas']} ventas). Considera ajustar."
        )


# --------------------------------------------------------------------------- #
#  Sección 4 — Registro manual de cambio de presupuesto
# --------------------------------------------------------------------------- #
def seccion_manual():
    st.header("4 · Registro manual de cambio de presupuesto")
    st.caption("Envía el cambio a Facebook (si hay API) y abre un período nuevo. "
               "Si Facebook rechaza el cambio, el período NO se cierra.")
    anuncios = db.obtener_anuncios(solo_activos=False)
    if not anuncios:
        st.info("No hay anuncios registrados.")
        return

    opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}
    with st.form("form_manual"):
        sel = st.selectbox("Anuncio", list(opciones.keys()))
        monto = st.number_input("Nuevo monto (presupuesto diario)", min_value=0.0, value=0.0, step=1.0)
        enviado = st.form_submit_button("Registrar cambio", type="primary")

    if enviado:
        ad_id = opciones[sel]
        r = cambio_presupuesto_completo(ad_id, float(monto))
        if r["ok"] and r.get("solo_local"):
            st.warning(f"Registrado solo localmente ({r.get('motivo')}). Nuevo período iniciado.")
        elif r["ok"]:
            st.success("Presupuesto actualizado en Facebook Ads — nuevo período iniciado")
        else:
            st.error(f"Facebook rechazó el cambio: {r['error']}\n\nEl período NO se cerró.")
        time.sleep(1.0)
        st.rerun()


# --------------------------------------------------------------------------- #
#  Candado de contraseña
# --------------------------------------------------------------------------- #
_LOGIN_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@500;600;700;800&family=Inter:wght@400;500;600&display=swap');
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"], header[data-testid="stHeader"], [data-testid="stToolbar"]{ display:none !important; }
[data-testid="stAppViewContainer"], .stApp{ background:#080810 !important; overflow:hidden; }
/* Centrado perfecto: una sola columna en el centro exacto */
[data-testid="stMain"]{ display:flex !important; align-items:center !important; justify-content:center !important; min-height:100vh; }
[data-testid="stMainBlockContainer"], .block-container{ max-width:420px !important; width:100%;
    padding:2.5vh 1.1rem !important; position:relative; z-index:2; }

/* ---------- Fondo animado (aurora + grid + blobs + partículas) ---------- */
.login-bg{ position:fixed; inset:0; z-index:0; overflow:hidden; pointer-events:none; background:#080810; }
.login-bg .aurora{ position:absolute; inset:-40%;
    background:
      radial-gradient(45% 45% at 30% 30%, #1a0533 0%, transparent 60%),
      radial-gradient(40% 40% at 72% 55%, #020818 0%, transparent 60%),
      radial-gradient(50% 50% at 50% 80%, #14082e 0%, transparent 62%),
      radial-gradient(35% 35% at 82% 22%, #0a1a3a 0%, transparent 60%);
    filter:blur(40px); animation:aurora 22s ease-in-out infinite alternate; }
.login-bg .grid{ position:absolute; inset:0;
    background-image:linear-gradient(rgba(255,255,255,.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.03) 1px, transparent 1px);
    background-size:46px 46px;
    -webkit-mask:radial-gradient(circle at 50% 42%, #000 0%, transparent 78%);
    mask:radial-gradient(circle at 50% 42%, #000 0%, transparent 78%); }
.login-bg .blob{ position:absolute; width:440px; height:440px; border-radius:50%;
    filter:blur(80px); opacity:.15; }
.login-bg .blob-1{ background:#7c3aed; top:-130px; left:-120px; animation:blob1 20s ease-in-out infinite alternate; }
.login-bg .blob-2{ background:#2563eb; bottom:-140px; right:-120px; animation:blob2 26s ease-in-out infinite alternate; }
.login-bg .particles span{ position:absolute; width:3px; height:3px; border-radius:50%;
    background:#c9d3ff; opacity:.35; box-shadow:0 0 6px rgba(201,211,255,.6);
    animation:floaty linear infinite; }
@keyframes aurora{
    0%{ transform:translate(-4%,-3%) rotate(0deg) scale(1.1); }
    50%{ transform:translate(4%,3%) rotate(7deg) scale(1.28); }
    100%{ transform:translate(-2%,5%) rotate(-5deg) scale(1.16); } }
@keyframes blob1{ 0%{transform:translate(0,0) scale(1)} 100%{transform:translate(60px,50px) scale(1.15)} }
@keyframes blob2{ 0%{transform:translate(0,0) scale(1)} 100%{transform:translate(-50px,-40px) scale(1.1)} }
@keyframes floaty{ 0%{transform:translate(0,0); opacity:0}
    10%{opacity:.4} 90%{opacity:.4} 100%{transform:translate(14px,-120px); opacity:0} }

/* ---------- Logo + título ---------- */
.login-brand{ text-align:center; margin:0 0 22px; animation:fadeInDown .6s cubic-bezier(.2,.7,.3,1) both; }
.login-logo{ width:76px; height:76px; margin:0 auto 20px; border-radius:22px;
    background:linear-gradient(150deg,#7c3aed,#2563eb); display:flex; align-items:center; justify-content:center;
    animation:logoPulse 2.6s ease-in-out infinite; }
.login-logo svg{ width:40px; height:40px; }
.login-title{ font-family:'Geist',sans-serif; font-weight:800; font-size:33px; line-height:1.1; margin:0;
    background:linear-gradient(92deg,#ffffff 30%,#a855f7 100%);
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
.login-sub{ font-family:'Inter',sans-serif; color:#9aa3b8; font-size:14px; margin:10px 0 0; }
@keyframes logoPulse{
    0%,100%{ box-shadow:0 0 22px 4px rgba(124,58,237,.35), 0 0 44px 10px rgba(37,99,235,.18); }
    50%{ box-shadow:0 0 40px 10px rgba(124,58,237,.6), 0 0 70px 18px rgba(37,99,235,.38); } }

/* ---------- Tarjeta glass ---------- */
div[data-testid="stVerticalBlockBorderWrapper"]{
    position:relative; overflow:hidden;
    background:rgba(255,255,255,.03) !important; backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
    border:1px solid rgba(255,255,255,.08) !important; border-radius:20px !important;
    box-shadow:0 30px 80px rgba(0,0,0,.5);
    animation:fadeInUp .8s cubic-bezier(.2,.7,.3,1) .15s both; }
div[data-testid="stVerticalBlockBorderWrapper"]::before{
    content:""; position:absolute; top:0; left:0; right:0; height:2px;
    background:linear-gradient(90deg,#7c3aed,#2563eb); }
.login-cardtitle{ font-family:'Geist',sans-serif; font-weight:700; font-size:28px; color:#fff; margin:4px 0 14px; }

/* Campos */
.stTextInput{ animation:fadeInUp .5s ease both; }
.stTextInput:nth-of-type(1){ animation-delay:.30s; }
.stTextInput:nth-of-type(2){ animation-delay:.40s; }
.stTextInput label p, .stTextInput label{ color:#8a93a6 !important; font-family:'Inter',sans-serif;
    font-size:11px !important; font-weight:600 !important; text-transform:uppercase; letter-spacing:.09em; }
.stTextInput div[data-baseweb="input"], .stTextInput input{ background:rgba(255,255,255,.05) !important;
    border-radius:10px !important; }
.stTextInput input{ border:1px solid rgba(255,255,255,.1) !important; color:#fff !important;
    padding:13px 14px !important; font-family:'Inter',sans-serif; }
.stTextInput input:focus{ border-color:#7c3aed !important;
    box-shadow:0 0 0 3px rgba(124,58,237,.25), 0 0 18px rgba(124,58,237,.35) !important; }
.stTextInput div[data-baseweb="input"]{ border:none !important; }

/* Botón con shimmer */
.stButton{ animation:fadeInUp .5s ease .5s both; }
.stButton>button{ position:relative; overflow:hidden; width:100%; border:none !important; border-radius:10px !important;
    background:linear-gradient(90deg,#7c3aed,#2563eb) !important; color:#fff !important;
    font-family:'Geist',sans-serif; font-weight:700 !important; padding:12px 0 !important;
    box-shadow:0 8px 26px rgba(124,58,237,.35) !important; transition:box-shadow .25s, transform .1s; }
.stButton>button:hover{ box-shadow:0 0 34px rgba(124,58,237,.65) !important; transform:translateY(-1px); }
.stButton>button::after{ content:""; position:absolute; top:0; left:-70%; width:45%; height:100%;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,.4),transparent); transform:skewX(-20deg); }
.stButton>button:hover::after{ animation:shimmer .9s ease; }
@keyframes shimmer{ 0%{left:-70%} 100%{left:150%} }

/* Features abajo */
.login-feats2{ display:flex; align-items:center; justify-content:center; gap:14px; flex-wrap:wrap;
    margin:20px auto 0; animation:fadeInUp .6s ease .7s both; }
.login-feats2 .feat{ display:flex; align-items:center; gap:7px; color:#8a93a6;
    font-family:'Inter',sans-serif; font-size:11.5px; }
.login-feats2 .feat svg{ width:15px; height:15px; stroke:#a855f7; opacity:.9; }
.login-feats2 .sep{ width:1px; height:14px; background:rgba(255,255,255,.12); }
.login-foot{ position:fixed; left:0; right:0; bottom:14px; text-align:center;
    font-family:'Inter',sans-serif; color:#5a6478; font-size:11.5px; z-index:3; line-height:1.7; }
.login-foot .ver{ color:#4a5265; font-size:11px; }
.login-foot .hp{ color:#a855f7; }

@keyframes fadeInDown{ 0%{opacity:0; transform:translateY(-18px)} 100%{opacity:1; transform:translateY(0)} }
@keyframes fadeInUp{ 0%{opacity:0; transform:translateY(18px)} 100%{opacity:1; transform:translateY(0)} }
</style>
"""

_LOGIN_HEADER = """
<div class="login-brand">
  <div class="login-logo">
    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Z" fill="#fff" opacity=".14"/>
      <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Z" stroke="#fff" stroke-width="1.3"/>
      <path d="M7 14l3-4 2 2.4L15 8l2 3" stroke="#fff" stroke-width="1.7"
            stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    </svg>
  </div>
  <h1 class="login-title">Ads Command Center</h1>
  <p class="login-sub">Control total de tus campañas de Facebook Ads</p>
</div>
"""

_LOGIN_FEATS = """
<div class="login-feats2">
  <div class="feat">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
      <rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
    <span>Campañas, conjuntos y anuncios</span>
  </div>
  <div class="sep"></div>
  <div class="feat">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/></svg>
    <span>ROAS, gasto y presupuesto de hoy</span>
  </div>
  <div class="sep"></div>
  <div class="feat">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/>
      <path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>
    <span>Excel + Supabase unidos</span>
  </div>
</div>
"""

# Partículas flotantes (posiciones y duraciones fijas, deterministas).
_LOGIN_PARTICULAS = [(8, 78, 15, 0), (18, 40, 18, 3), (27, 88, 13, 6), (36, 20, 20, 1),
                     (45, 65, 16, 4), (54, 35, 19, 7), (63, 82, 14, 2), (72, 15, 21, 5),
                     (80, 58, 17, 8), (88, 30, 15, 3), (94, 72, 19, 6), (13, 55, 16, 9)]


def _login_bg_html() -> str:
    parts = "".join(
        f'<span style="left:{x}%;top:{y}%;animation-duration:{d}s;animation-delay:{dl}s"></span>'
        for (x, y, d, dl) in _LOGIN_PARTICULAS)
    return ('<div class="login-bg"><div class="aurora"></div><div class="grid"></div>'
            '<div class="blob blob-1"></div><div class="blob blob-2"></div>'
            f'<div class="particles">{parts}</div></div>')


def _auth_token() -> str:
    """Token de sesión (hash del usuario+contraseña). NO es la contraseña; sirve para
    recordar la sesión en la URL y no cerrarla al recargar el navegador."""
    import hashlib
    return hashlib.sha256(f"{config.APP_USER}:{config.APP_PASSWORD}".encode()).hexdigest()[:24]


def _gate_password():
    if not config.APP_PASSWORD:
        return
    if st.session_state.get("_auth_ok"):
        return
    # Sesión recordada en la URL (sobrevive el F5/recarga del navegador).
    if st.query_params.get("s") == _auth_token():
        st.session_state["_auth_ok"] = True
        return

    st.markdown(_LOGIN_CSS, unsafe_allow_html=True)
    st.markdown(_login_bg_html(), unsafe_allow_html=True)
    st.markdown(_LOGIN_HEADER, unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown('<div class="login-cardtitle">Iniciar sesión</div>', unsafe_allow_html=True)
        usuario = st.text_input("Usuario", key="_user", placeholder="usuario")
        pwd = st.text_input("Contraseña", type="password", key="_pwd", placeholder="••••••••")
        entrar = st.button("Entrar", use_container_width=True, type="primary")
        if entrar:
            if usuario.strip() == config.APP_USER and pwd == config.APP_PASSWORD:
                st.session_state["_auth_ok"] = True
                st.query_params["s"] = _auth_token()  # recuerda la sesión al recargar
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")
    st.markdown(_LOGIN_FEATS, unsafe_allow_html=True)
    st.markdown(
        f'<div class="login-foot"><span class="ver">{_html.escape(APP_VERSION)}</span><br>'
        f'Hecho con <span class="hp">&#10084;</span> por Daniel M</div>',
        unsafe_allow_html=True)
    st.stop()


# --------------------------------------------------------------------------- #
#  Sección — Conexiones (multi-Business / tokens)
# --------------------------------------------------------------------------- #
def seccion_conexiones():
    st.header("Conexiones (Business / tokens)")
    st.caption("Agrega un token de **Usuario del Sistema** por cada Business. La app "
               "descubre sus cuentas automáticamente. Los tokens se guardan solo en "
               "este servidor (volumen /data), nunca en el repositorio.")

    cons = db.obtener_conexiones()
    if cons:
        for c in cons:
            with st.container(border=True):
                col = st.columns([4, 2, 2, 2])
                tok = c.get("token") or ""
                col[0].markdown(f"**{c.get('alias') or 'Conexión'}**  ·  id {c['id']}")
                col[0].caption(f"token …{tok[-6:]}" + ("  ·  app propia" if c.get("app_id") else ""))
                if c.get("ultimo_error"):
                    col[0].caption(f"{c['ultimo_error']}")
                col[1].write("activa" if c["activo"] else "inactiva")
                if col[2].button("Probar", key=f"probar_{c['id']}"):
                    r = fb.probar_conexion(tok, c.get("app_id"), c.get("app_secret"))
                    if r["ok"]:
                        nombres = ", ".join(x["name"] for x in r["cuentas"][:12])
                        st.success(f"OK · {len(r['cuentas'])} cuenta(s): {nombres}")
                    else:
                        st.error(r["error"])
                if col[3].button("Eliminar", key=f"del_{c['id']}"):
                    db.eliminar_conexion(c["id"])
                    st.rerun()
    else:
        st.info("Aún no hay conexiones guardadas. Si definiste credenciales en el .env, "
                "se usa esa única cuenta por defecto.")

    st.subheader("Agregar conexión")
    with st.form("form_conexion"):
        alias = st.text_input("Alias (ej. BM Tienda MX)")
        token = st.text_area("Token de Usuario del Sistema", height=90,
                             help="Business Settings → Usuarios del sistema → Generar token")
        with st.expander("App propia (opcional, solo si no usas la app central del .env)"):
            app_id = st.text_input("APP_ID (opcional)")
            app_secret = st.text_input("APP_SECRET (opcional)", type="password")
        cc1, cc2 = st.columns(2)
        probar = cc1.form_submit_button("Probar y descubrir cuentas")
        guardar = cc2.form_submit_button("Guardar conexión", type="primary")

    if probar:
        if not token.strip():
            st.error("Pega primero el token.")
        else:
            r = fb.probar_conexion(token.strip(), app_id or None, app_secret or None)
            if r["ok"]:
                st.success(f"{len(r['cuentas'])} cuenta(s) encontradas:")
                st.dataframe(pd.DataFrame(r["cuentas"]), use_container_width=True, hide_index=True)
            else:
                st.error(f"{r['error']}")
    if guardar:
        if not token.strip():
            st.error("Pega el token.")
        else:
            cid = db.agregar_conexion(alias or "Business", token.strip(),
                                      app_id or None, app_secret or None)
            try:
                _insights_cache.clear()
            except Exception:
                pass
            st.success(f"Conexión #{cid} guardada. Pulsa **Recargar anuncios de "
                       "Facebook** (barra lateral) para cargar sus cuentas.")
            time.sleep(1.0)
            st.rerun()


# --------------------------------------------------------------------------- #
#  Layout principal
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  Tema visual (modo oscuro pulido)
# --------------------------------------------------------------------------- #
def _inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700;800&family=Inter:wght@400;500;600&display=swap');
    :root{
        --bg:#080810; --card:rgba(255,255,255,.03); --card-brd:rgba(255,255,255,.07);
        --txt:#ffffff; --sub:#94a3b8; --ter:#475569;
        --p1:#7c3aed; --p2:#2563eb; --ok:#10b981; --warn:#ef4444; --plight:#a78bfa;
    }
    html, body, .stApp, [data-testid="stAppViewContainer"]{ color:var(--txt); background:var(--bg) !important; }

    /* ---------- Fondo premium: aurora (30%) + blobs (10%) + grid ---------- */
    .app-bg{ position:fixed; inset:0; z-index:0; pointer-events:none; overflow:hidden; background:#080810; }
    .app-bg .aurora{ position:absolute; inset:-40%; opacity:.5;
        background:
          radial-gradient(46% 46% at 26% 24%, #2a0a55 0%, transparent 60%),
          radial-gradient(42% 42% at 72% 40%, #1a0533 0%, transparent 60%),
          radial-gradient(40% 40% at 78% 70%, #061336 0%, transparent 60%),
          radial-gradient(50% 50% at 50% 88%, #1c0940 0%, transparent 62%);
        filter:blur(50px); animation:dashAurora 26s ease-in-out infinite alternate; }
    .app-bg .grid{ position:absolute; inset:0;
        background-image:linear-gradient(rgba(255,255,255,.03) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,.03) 1px, transparent 1px);
        background-size:48px 48px;
        -webkit-mask:radial-gradient(circle at 50% 30%, #000, transparent 85%);
        mask:radial-gradient(circle at 50% 30%, #000, transparent 85%); }
    .app-bg .blob{ position:absolute; width:460px; height:460px; border-radius:50%;
        filter:blur(80px); opacity:.10; }
    .app-bg .b1{ background:#7c3aed; top:-150px; left:-130px; animation:dashBlob1 22s ease-in-out infinite alternate; }
    .app-bg .b2{ background:#2563eb; bottom:-160px; right:-130px; animation:dashBlob2 28s ease-in-out infinite alternate; }
    @keyframes dashAurora{ 0%{transform:translate(-3%,-2%) rotate(0) scale(1.1)} 100%{transform:translate(3%,4%) rotate(6deg) scale(1.22)} }
    @keyframes dashBlob1{ 0%{transform:translate(0,0)} 100%{transform:translate(50px,40px)} }
    @keyframes dashBlob2{ 0%{transform:translate(0,0)} 100%{transform:translate(-45px,-35px)} }
    [data-testid="stAppViewContainer"] .main .block-container{ position:relative; z-index:1; }
    [data-testid="stSidebar"]{ position:relative; z-index:1; }

    /* Scrollbar delgado púrpura */
    ::-webkit-scrollbar{ width:9px; height:9px; }
    ::-webkit-scrollbar-thumb{ background:rgba(124,58,237,.3); border-radius:8px; }
    ::-webkit-scrollbar-thumb:hover{ background:rgba(124,58,237,.5); }
    ::-webkit-scrollbar-track{ background:transparent; }

    body, .stMarkdown, p, label, input, textarea, button, li, td, th{ font-family:'Inter',sans-serif; }
    h1,.stMarkdown h1{ font-family:'Geist',sans-serif !important; letter-spacing:-.02em; font-weight:800;
        background:linear-gradient(92deg,#ffffff 35%,#a78bfa 100%);
        -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
    h2,.stMarkdown h2{ font-family:'Geist',sans-serif !important; letter-spacing:-.01em; color:#eef2f6; }
    h3,h4,.stMarkdown h3,.stMarkdown h4{ font-family:'Geist',sans-serif !important; color:var(--plight); }
    /* Restaurar la fuente de iconos de Material */
    [class*="material-symbols"], [class*="material-icons"], .material-icons,
    span[data-testid="stIconMaterial"], [data-testid="stIconMaterial"]{
        font-family:'Material Symbols Outlined','Material Symbols Rounded','Material Icons' !important;
        -webkit-text-fill-color:initial;
    }
    /* Sidebar */
    [data-testid="stSidebar"]{ background:#0a0a16 !important; border-right:1px solid rgba(255,255,255,.05); }
    /* Marca ACC arriba a la izquierda */
    .acc-brand{ display:flex; align-items:center; gap:11px; padding:2px 2px 8px; }
    .acc-logo{ width:42px; height:42px; border-radius:12px; flex:none;
        background:linear-gradient(150deg,#7c3aed,#2563eb); display:flex; align-items:center;
        justify-content:center; animation:accGlow 2.8s ease-in-out infinite; }
    .acc-logo svg{ width:23px; height:23px; }
    @keyframes accGlow{ 0%,100%{box-shadow:0 0 12px 2px rgba(124,58,237,.35)}
        50%{box-shadow:0 0 20px 5px rgba(124,58,237,.6)} }
    .acc-name{ font-family:'Geist',sans-serif; font-weight:800; font-size:15px; line-height:1.08;
        background:linear-gradient(92deg,#fff 40%,#a78bfa); -webkit-background-clip:text;
        background-clip:text; -webkit-text-fill-color:transparent; }
    .acc-name small{ display:block; font-family:'Inter',sans-serif; font-size:8.5px; font-weight:600;
        letter-spacing:.14em; color:#6b7488; -webkit-text-fill-color:#6b7488; margin-top:3px; }
    [data-testid="stSidebar"] h3{ color:var(--sub); text-transform:uppercase; font-size:12px;
        letter-spacing:.12em; font-weight:600; }
    [data-testid="stStatusWidget"]{ display:none !important; }

    /* Botones secundarios (glass con borde púrpura al hover) */
    .stButton>button, .stDownloadButton>button, .stFormSubmitButton>button{
        border-radius:10px; font-weight:600; color:#fff; transition:all .18s ease;
        background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08);
    }
    .stButton>button:hover, .stDownloadButton>button:hover{
        border-color:var(--p1); box-shadow:0 0 18px rgba(124,58,237,.3);
    }
    /* Botones primarios: gradiente púrpura→azul + shimmer */
    .stButton>button[kind="primary"], .stFormSubmitButton>button[kind="primary"]{
        background:linear-gradient(90deg,#7c3aed,#2563eb) !important; color:#fff !important;
        border:none !important; border-radius:10px !important; position:relative; overflow:hidden;
        box-shadow:0 8px 26px rgba(124,58,237,.35) !important;
    }
    .stButton>button[kind="primary"]:hover, .stFormSubmitButton>button[kind="primary"]:hover{
        box-shadow:0 0 30px rgba(124,58,237,.6) !important; transform:translateY(-1px);
    }
    .stButton>button[kind="primary"]::after, .stFormSubmitButton>button[kind="primary"]::after{
        content:""; position:absolute; top:0; left:-70%; width:45%; height:100%;
        background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent); transform:skewX(-20deg);
    }
    .stButton>button[kind="primary"]:hover::after{ animation:dashShimmer .9s ease; }
    @keyframes dashShimmer{ 0%{left:-70%} 100%{left:150%} }

    /* Inputs y selects */
    .stTextInput input, .stNumberInput input, [data-baseweb="textarea"] textarea{
        background:rgba(255,255,255,.04) !important; border:1px solid rgba(255,255,255,.08) !important;
        border-radius:10px !important; color:#fff !important;
    }
    .stTextInput input:focus, .stNumberInput input:focus{
        border-color:var(--p1) !important;
        box-shadow:0 0 0 3px rgba(124,58,237,.2), 0 0 16px rgba(124,58,237,.3) !important;
    }
    input::placeholder{ color:var(--ter) !important; }
    [data-baseweb="select"]>div{ background:rgba(255,255,255,.04) !important;
        border:1px solid rgba(255,255,255,.08) !important; border-radius:10px !important; }
    [data-baseweb="popover"] li:hover, [role="option"]:hover{ background:rgba(124,58,237,.12) !important; }

    /* Cards / contenedores / expanders (glass) */
    [data-testid="stExpander"], div[data-testid="stVerticalBlockBorderWrapper"]{
        background:rgba(255,255,255,.03) !important; backdrop-filter:blur(12px);
        border:1px solid rgba(255,255,255,.07) !important; border-radius:16px;
        box-shadow:0 10px 30px rgba(0,0,0,.3); transition:border-color .2s ease;
    }
    [data-testid="stExpander"]:hover, div[data-testid="stVerticalBlockBorderWrapper"]:hover{
        border-color:rgba(124,58,237,.4) !important;
    }

    /* Controles segmentados (Ver por / Estado): grupo glass, activo púrpura */
    [data-testid="stButtonGroup"]{ background:rgba(255,255,255,.03);
        border:1px solid rgba(255,255,255,.07); border-radius:10px; padding:3px; }
    button[data-variant="segmented_control"]{ color:#94a3b8 !important;
        background:transparent !important; border:1px solid transparent !important; border-radius:8px !important; }
    button[data-variant="segmented_control"]:hover{ color:#fff !important; background:rgba(124,58,237,.1) !important; }
    button[data-variant="segmented_control"][data-selected="true"]{
        background:rgba(124,58,237,.2) !important; border:1px solid rgba(124,58,237,.5) !important;
        color:#fff !important; }

    /* KPI cards: acento superior + hover glow (clase .tcard de _render_totales) */
    .tcard{ position:relative; overflow:hidden; transition:border-color .2s ease, box-shadow .2s ease; }
    .tcard::before{ content:""; position:absolute; top:0; left:0; right:0; height:2px;
        background:linear-gradient(90deg,#7c3aed,#2563eb); }
    .tcard:hover{ border-color:rgba(124,58,237,.45) !important; box-shadow:0 10px 30px rgba(124,58,237,.15); }

    /* Botones de acción de la tabla (Info/Pres/Dup): visibles y UNIFORMES.
       Los de Pres/Dup son popovers que antes salían casi invisibles. */
    [data-testid="stPopoverButton"], .stButton>button:has([data-testid="stIconMaterial"]):not(:has(p)):not([kind="tertiary"]){
        background:rgba(124,58,237,.12) !important; border:1px solid rgba(255,255,255,.14) !important;
        border-radius:9px !important; color:#fff !important; min-height:0 !important;
        padding:6px 8px !important; transition:all .15s ease;
    }
    [data-testid="stPopoverButton"]:hover, .stButton>button:has([data-testid="stIconMaterial"]):not(:has(p)):not([kind="tertiary"]):hover{
        background:rgba(124,58,237,.28) !important; border-color:#7c3aed !important;
        box-shadow:0 0 14px rgba(124,58,237,.3) !important;
    }
    [data-testid="stPopoverButton"] [data-testid="stIconMaterial"],
    .stButton>button:has([data-testid="stIconMaterial"]):not(:has(p)) [data-testid="stIconMaterial"]{
        color:#c4b5fd !important;
    }
    /* El popover de "Rango de fechas" NO debe verse como los mini-botones de acción:
       se muestra como un dropdown claro y visible (con su "📅 Hoy"). */
    [data-testid="stElementContainer"]:has(.rango-anchor) ~ * [data-testid="stPopoverButton"]{
        padding:9px 12px !important; min-height:38px !important; width:100% !important;
        justify-content:space-between !important; font-size:14px !important;
        background:rgba(255,255,255,.05) !important; border:1px solid rgba(255,255,255,.14) !important;
        color:#fff !important; border-radius:10px !important;
    }
    [data-testid="stElementContainer"]:has(.rango-anchor) ~ * [data-testid="stPopoverButton"]:hover{
        border-color:#7c3aed !important; box-shadow:0 0 14px rgba(124,58,237,.3) !important;
    }
    /* Toggle On/Off rediseñado: OFF gris, ON verde con glow (transición suave) */
    [data-testid="stCheckbox"] label > div:first-of-type{
        background:#374151 !important; transition:background .2s ease, box-shadow .2s ease;
    }
    [data-testid="stCheckbox"] label:has(input:checked) > div:first-of-type{
        background:#34d399 !important; box-shadow:0 0 9px rgba(52,211,153,.45) !important;
    }

    /* Tabs (Configuración) */
    .stTabs [data-baseweb="tab-list"]{ gap:6px; border-bottom:1px solid rgba(255,255,255,.07); }
    .stTabs [data-baseweb="tab"]{ border-radius:10px 10px 0 0; color:var(--sub); }
    .stTabs [aria-selected="true"]{ color:var(--plight) !important; }
    .stTabs [data-baseweb="tab-highlight"]{ background:linear-gradient(90deg,#7c3aed,#2563eb) !important; height:3px; }

    [data-testid="stAlert"]{ border-radius:14px; }
    [data-testid="stMetricValue"]{ font-family:'Geist',sans-serif; color:#fff; }
    hr{ border-color:rgba(255,255,255,.07) !important; }
    </style>
    <div class="app-bg"><div class="aurora"></div><div class="grid"></div>
    <div class="blob b1"></div><div class="blob b2"></div></div>
    """, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
#  Página: Configuración (Excel + Cuentas + Conexiones + Supabase)
# --------------------------------------------------------------------------- #
def _panel_gsheets():
    st.subheader("Google Sheets")
    st.caption("Lee TODAS las pestañas de un Google Sheets. Comparte el Sheet como "
               "**'Cualquiera con el enlace: Lector'** y pega la URL. Detecta la columna del "
               "ID del anuncio, valor, fecha y país aunque se llamen distinto.")
    url = st.text_input("URL del Google Sheets", value=gs.get_url(),
                        placeholder="https://docs.google.com/spreadsheets/d/...")
    c1, c2, c3 = st.columns(3)
    if c1.button("Guardar URL", use_container_width=True):
        gs.set_url(url)
        st.success("URL guardada.")
    if c2.button("Probar y ver pestañas", use_container_width=True):
        gs.set_url(url)
        r = gs.probar()
        if r["ok"]:
            st.success(f"Conectado. {len(r['hojas'])} pestaña(s):")
            ejemplos_ad = [a["ad_id"] for a in db.obtener_anuncios()][:3]
            for h in r["hojas"]:
                det = h["detectado"]
                sin = "" if det["id"] else "  ← NO detecté columna de ID"
                st.caption(f"• **{h['nombre']}** ({h['filas']} filas) — ID: {det['id'] or '—'} "
                           f"· valor: {det['valor'] or '—'} · fecha: {det['hora'] or '—'} "
                           f"· país: {det['pais'] or '—'}{sin}")
                if h.get("muestra_id"):
                    st.caption(f"   ▸ Ejemplo de ID en tu Sheet: `{h['muestra_id']}`")
            if ejemplos_ad:
                st.info("Compara: un **ad_id real** de tus anuncios se ve así: "
                        + ", ".join(f"`{x}`" for x in ejemplos_ad) +
                        ". El valor de tu columna debe ser **igual a estos** (el ID del "
                        "ANUNCIO). Si tu 'post id' es distinto, es el ID del post/creativo y "
                        "no va a coincidir.")
            else:
                st.info("Aún no hay anuncios cargados para comparar. Pulsa **Recargar** primero.")
        else:
            st.error(r["error"])
    if c3.button("Sincronizar ahora", type="primary", use_container_width=True):
        gs.set_url(url)
        r = gs.sincronizar()
        if r["ok"]:
            msg = f"{r['insertadas']} venta(s) importada(s) de Google Sheets."
            if r["sin_periodo"]:
                msg += f" ({r['sin_periodo']} sin período/anuncio a esa hora)."
            st.success(msg)
            for d in r.get("detalle", []):
                st.caption(f"• **{d['hoja']}**: {d['motivo']}")
        else:
            st.error(r["error"])

    with st.expander("Columnas (opcional — si el auto-detector se equivoca)"):
        st.caption("Deja en blanco para detección automática. Si el valor sale en 0 o toma la "
                   "columna equivocada, escribe aquí el nombre EXACTO de la columna de tu Sheet.")
        ov = gs.get_overrides()
        o1, o2 = st.columns(2)
        ov_id = o1.text_input("Columna del ID del anuncio", value=ov["id"], key="gs_ov_id")
        ov_val = o2.text_input("Columna del valor/ingreso", value=ov["valor"], key="gs_ov_val")
        o3, o4 = st.columns(2)
        ov_hora = o3.text_input("Columna de fecha/hora", value=ov["hora"], key="gs_ov_hora")
        ov_pais = o4.text_input("Columna de país", value=ov["pais"], key="gs_ov_pais")
        st.caption("**Antiduplicados:** DÉJALO EN BLANCO y la app usará una **huella por "
                   "contenido** (anuncio + valor + fecha + país) con contador de repetidos. "
                   "No necesitas ningún ID único: aguanta que tu software agregue filas al final, "
                   "que reordenes y hasta ventas idénticas. Solo escribe una columna aquí si de "
                   "verdad tienes un ID único por venta y prefieres usarlo.")
        ov_dedup = st.text_input("Columna de ID único (dedup) — opcional, mejor en blanco",
                                 value=gs.get_dedup(), key="gs_dedup")
        ig_cero = st.checkbox("Ignorar ventas con valor 0", value=gs.get_ignora_cero(),
                              key="gs_ig_cero")
        if st.button("Guardar columnas manuales"):
            gs.set_overrides(ov_id, ov_val, ov_hora, ov_pais)
            gs.set_dedup(ov_dedup)
            gs.set_ignora_cero(ig_cero)
            st.success("Guardado. Usa 'Borrar e importar de nuevo' para aplicar limpio.")
        st.divider()
        st.caption("Si ya tienes duplicados, borra las ventas de Google Sheets y vuelve a "
                   "importarlas limpio con la configuración actual:")
        if st.button("Borrar ventas de Google Sheets y re-importar", type="primary"):
            n = db.borrar_ventas(gs.HOJA)
            r = gs.sincronizar()
            if r["ok"]:
                st.success(f"Borradas {n}. Re-importadas {r['insertadas']} venta(s) limpias.")
            else:
                st.error(r["error"])

    # Reconciliación: cuadra con tu Sheet
    with st.expander("Reconciliación (cuadrar con tu Sheet)"):
        res = db.resumen_ventas_fuente(gs.HOJA)
        hoy = db.a_texto(db.ahora())[:10]
        hoy_row = next((d for d in res["por_dia"] if d["dia"] == hoy), None)
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Ventas importadas (total)", f"{res['num']:,}".replace(",", "."))
        cc2.metric("Ingreso total importado", _num(res["ingreso"]))
        cc3.metric(f"Ventas de HOY ({hoy})", hoy_row["num"] if hoy_row else 0)
        if not hoy_row:
            st.warning("No hay ventas de hoy importadas. Causas comunes: (1) Google todavía "
                       "servía una copia en caché — vuelve a **Sincronizar**; (2) las filas de "
                       "hoy en tu Sheet aún no tienen **valor** o **ID del anuncio**; (3) la "
                       "columna de **fecha** de esas filas está vacía o en otro formato.")
        st.caption("Ingreso en la MONEDA de tu Sheet (sin convertir), para comparar directo con tu cálculo.")
        if res["por_dia"]:
            st.markdown("**Por día (últimos 14):**")
            st.dataframe(pd.DataFrame([{"Día": d["dia"], "Ventas": d["num"],
                                        "Ingreso": round(d["ingreso"], 2)} for d in res["por_dia"]]),
                         use_container_width=True, hide_index=True)

    # Diagnóstico: ¿las ventas importadas coinciden con tus anuncios?
    with st.expander("Diagnóstico de ventas importadas (por qué no aparecen)"):
        ventas = [v for v in db.obtener_ventas() if v.get("hoja_origen") == gs.HOJA][:20]
        total_gs = len([v for v in db.obtener_ventas() if v.get("hoja_origen") == gs.HOJA])
        st.caption(f"Ventas importadas de Google Sheets en la base: **{total_gs}**")
        if not ventas:
            st.info("No hay ventas importadas de Google Sheets todavía. Pulsa **Sincronizar "
                    "ahora**. Si sigue en 0, revisa que el Sheet esté compartido como "
                    "'Cualquiera con el enlace: Lector' y que haya una columna con el ID del anuncio.")
        else:
            ids_anuncios = {a["ad_id"] for a in db.obtener_anuncios()}
            filas = [{"ad_id": v["ad_id"],
                      "coincide con anuncio": "Sí" if str(v["ad_id"]) in ids_anuncios else "NO",
                      "valor": v["valor_venta"], "hora": v["hora_venta"],
                      "período": v.get("periodo_id") or "—"} for v in ventas]
            st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)
            n_no = sum(1 for f in filas if f["coincide con anuncio"] == "NO")
            if n_no:
                st.warning(f"{n_no} de las últimas {len(filas)} ventas tienen un **ID que NO "
                           "coincide** con ningún anuncio cargado. Por eso no aparecen en la tabla. "
                           "Revisa que la columna del Sheet tenga el **ID del anuncio (ad_id)** de "
                           "Facebook, no el ID de campaña/conjunto ni otro código. Y pulsa "
                           "**Recargar** para traer los anuncios primero.")


def _panel_excel():
    st.subheader("Excel (ventas del socio)")
    st.caption("Sube un Excel (.xlsx). Lee **todas las hojas** y detecta la columna del ID del "
               "anuncio, valor, fecha y país aunque se llamen distinto (ID_Anuncio, Ad ID, id, "
               "Monto, Fecha, Pais…). Solo procesa las filas nuevas.")
    subido = st.file_uploader("Subir / reemplazar ventas.xlsx", type=["xlsx"], key="up_excel_cfg")
    c1, c2 = st.columns(2)
    if subido is not None and c1.button("Procesar Excel subido", use_container_width=True):
        n = watcher.guardar_excel_subido(subido.getvalue(), reemplazar=True)
        st.success(f"{n} venta(s) nueva(s) procesada(s).")
    if c2.button("Reimportar TODO el Excel", use_container_width=True,
                 help="Reprocesa todas las filas. Ahora es seguro: no duplica."):
        n = watcher.importar_todo()
        st.success(f"{n} ventas importadas (sin duplicar).")

    st.divider()
    st.markdown("**🧹 Arreglar ventas duplicadas**")
    st.caption("Si ves MÁS ventas de las que tienes en tu Excel, es por imports antiguos que "
               "reinsertaban todo. Esto lo arregla. Ya no volverá a pasar (el dedup ahora es "
               "estable). *Quita ventas de Excel repetidas; no toca Google Sheets ni Supabase.*")
    d1, d2 = st.columns(2)
    if d1.button("Quitar duplicados (recomendado)", use_container_width=True, type="primary",
                 help="No destructivo: deja una sola de cada venta idéntica."):
        borradas = db.deduplicar_ventas_excel()
        st.success(f"Listo: {borradas} venta(s) duplicada(s) eliminada(s).")
    if d2.button("Borrar TODO y re-subir", use_container_width=True,
                 help="Borra todas las ventas de Excel; luego vuelve a subir el archivo arriba."):
        borradas = db.borrar_ventas_excel()
        st.warning(f"Borradas {borradas} venta(s) de Excel. Ahora sube el archivo arriba "
                   "para re-importarlo limpio.")


def _panel_supabase():
    st.subheader("Supabase (tus ventas)")
    st.caption("Fuente 2: una tabla de Supabase con tus ventas. Se une con el Excel en la "
               "misma tabla de ventas. Las credenciales van en variables de entorno "
               "(SUPABASE_URL / SUPABASE_KEY).")
    if not config.supabase_configurado():
        st.warning("Faltan **SUPABASE_URL** y **SUPABASE_KEY** en las variables de entorno "
                   "de EasyPanel. Agrégalas y redespliega.")
    else:
        st.success("Credenciales de Supabase detectadas ")

    m = supa.get_mapeo()
    with st.form("form_supabase"):
        tabla = st.text_input("Nombre de la tabla", value=m[supa.K_TABLA])
        c1, c2, c3 = st.columns(3)
        col_ad = c1.text_input("Columna ID del anuncio", value=m[supa.K_ADID])
        col_val = c2.text_input("Columna valor de venta", value=m[supa.K_VALOR])
        col_hora = c3.text_input("Columna fecha/hora", value=m[supa.K_HORA])
        c4, c5, c6 = st.columns(3)
        col_prod = c4.text_input("Columna producto (opcional)", value=m[supa.K_PRODUCTO])
        col_id = c5.text_input("Columna id único (dedup)", value=m[supa.K_ID])
        col_pais = c6.text_input("Columna país (opcional)", value=m.get(supa.K_PAIS, ""),
                                 help="Para ver la facturación por país.")
        gcol1, gcol2 = st.columns(2)
        probar = gcol1.form_submit_button("Probar y ver columnas")
        guardar = gcol2.form_submit_button("Guardar mapeo", type="primary")

    if guardar:
        supa.guardar_mapeo(tabla, col_ad, col_val, col_hora, col_prod, col_id, col_pais)
        st.success("Mapeo guardado.")
    if probar:
        supa.guardar_mapeo(tabla, col_ad, col_val, col_hora, col_prod, col_id, col_pais)
        r = supa.probar_conexion()
        if r["ok"]:
            st.success(f"Conectado Columnas de tu tabla: {', '.join(r['columnas'])}")
            if r["muestra"]:
                st.dataframe(pd.DataFrame(r["muestra"]), use_container_width=True, hide_index=True)
        else:
            st.error(f"{r['error']}")

    if st.button("Sincronizar ventas de Supabase ahora", type="primary"):
        r = supa.sincronizar()
        if r["ok"]:
            msg = f"{r['insertadas']} venta(s) importada(s) de Supabase."
            if r["sin_periodo"]:
                msg += f" ({r['sin_periodo']} sin período/anuncio activo a esa hora)."
            st.success(msg)
        else:
            st.error(f"{r['error']}")


def _panel_ia():
    st.subheader("Asistente de IA")
    st.caption("Elige qué IA conectar. La clave se guarda en este servidor (volumen /data), "
               "no en el repositorio. También puedes usar variables de entorno "
               "(OPENAI_API_KEY / ANTHROPIC_API_KEY).")
    prov_actual = db.get_config(ia.K_PROV, "") or ""
    opciones = {"": "Automático (según la key disponible)",
                "openai": "OpenAI (ChatGPT)", "anthropic": "Anthropic (Claude)"}
    prov = st.selectbox("Proveedor de IA", list(opciones.keys()),
                        index=list(opciones.keys()).index(prov_actual) if prov_actual in opciones else 0,
                        format_func=lambda k: opciones[k])
    key_actual = db.get_config(ia.K_KEY, "") or ""
    nueva_key = st.text_input("API key", type="password",
                              placeholder=("•••• (ya guardada, deja vacío para no cambiarla)"
                                           if key_actual else "sk-..."),
                              help="OpenAI: platform.openai.com/api-keys · "
                                   "Anthropic: console.anthropic.com")
    modelo_def = "gpt-4o-mini" if prov == "openai" else ("claude-opus-5" if prov == "anthropic" else "")
    modelo = st.text_input("Modelo (opcional)", value=db.get_config(ia.K_MODELO, "") or "",
                           placeholder=modelo_def or "gpt-4o-mini / claude-sonnet-5 …",
                           help="OpenAI: gpt-4o-mini (barato), gpt-4o. "
                                "Anthropic: claude-opus-5, claude-sonnet-5, claude-haiku-4-5.")
    if st.button("Guardar configuración de IA", type="primary"):
        db.set_config(ia.K_PROV, prov)
        if nueva_key.strip():
            db.set_config(ia.K_KEY, nueva_key.strip())
        db.set_config(ia.K_MODELO, modelo.strip())
        st.success("Guardado. El asistente aparece en el Dashboard.")
        st.rerun()
    st.divider()
    if ia.disponible():
        st.success(f"IA activa · proveedor: **{ia.proveedor()}** · modelo: `{ia.modelo()}`")
    else:
        st.warning("Aún no hay IA configurada: elige proveedor y pega tu API key arriba.")
    if key_actual and st.button("Borrar API key guardada"):
        db.set_config(ia.K_KEY, "")
        st.rerun()


def _panel_cuentas():
    st.subheader("Cuentas publicitarias detectadas")
    todos = db.obtener_anuncios(solo_activos=False)
    if not todos:
        st.info("Aún no hay cuentas. Agrega una conexión y pulsa **Recargar** en la barra lateral.")
        return
    filas = {}
    for a in todos:
        c = a.get("cuenta_nombre") or "—"
        if c not in filas:
            filas[c] = {"Cuenta": c, "País (pauta)": a.get("cuenta_pais") or "—",
                        "Moneda": a.get("cuenta_moneda") or "—",
                        "Anuncios": 0, "Activos": 0}
        filas[c]["Anuncios"] += 1
        if _es_activo(a):
            filas[c]["Activos"] += 1
    st.dataframe(pd.DataFrame(list(filas.values())),
                 use_container_width=True, hide_index=True)


def _panel_moneda():
    st.subheader("Moneda y tipo de cambio")
    st.caption("Todo se muestra en **USD**. El gasto/presupuesto de Facebook vienen en la "
               "moneda de cada cuenta y se convierten con el tipo de cambio del día (automático).")

    monedas = sorted({(a.get("cuenta_moneda") or "—") for a in db.obtener_anuncios()} - {"—"})
    if monedas:
        st.markdown("**Tipo de cambio de hoy (a USD):**")
        for m in monedas:
            info = fx.info_tasa(m)
            st.caption(f"1 {m} = {info['tasa']:.5f} USD  ·  origen: {info['origen']}")
    else:
        st.info("Aún no se detecta la moneda de tus cuentas. Pulsa **Recargar** en la barra "
                "lateral para traerla de Facebook.")

    st.divider()
    st.markdown("**Fuente de las ventas del dashboard**")
    st.caption("De dónde salen Ventas e Ingresos. Elige **UNA sola** para no duplicar si las "
               "mismas ventas están en varias fuentes.")
    actual = db.get_config("fuente_ventas", "todas")
    op = {"excel": "Solo Excel", "gsheets": "Solo Google Sheets", "supabase": "Solo Supabase",
          "todas": "Todas mis fuentes (Excel + Sheets + Supabase)",
          "meta": "Meta (compras del pixel)"}
    sel_o = st.radio("Fuente de ventas", list(op.keys()),
                     index=list(op.keys()).index(actual) if actual in op else 3,
                     format_func=lambda k: op[k], key="sel_fuente")
    if st.button("Guardar fuente de ventas", type="primary"):
        db.set_config("fuente_ventas", sel_o)
        st.success("Guardado.")

    st.divider()
    st.markdown("**Moneda de las ventas (Excel/Supabase)**")
    st.caption("Si tus ventas ya están en USD, elige USD. Si están en la moneda de la cuenta "
               "(ej. MXN), deja 'Automático'.")
    actual = db.get_config("moneda_ventas", "auto")
    opciones = ["auto", "USD", "MXN", "COP", "EUR", "ARS", "BRL", "CLP", "PEN"]
    idx = opciones.index(actual) if actual in opciones else 0
    sel = st.selectbox("Moneda de las ventas", opciones, index=idx,
                       format_func=lambda x: "Automático (moneda de la cuenta)" if x == "auto" else x)
    if st.button("Guardar moneda de ventas", type="primary"):
        db.set_config("moneda_ventas", sel)
        st.success("Guardado.")

    st.divider()
    st.markdown("**Tipo de cambio manual (opcional)**")
    st.caption("Si prefieres fijar tú el tipo de cambio en vez del automático, escríbelo aquí.")
    c1, c2 = st.columns(2)
    m_manual = c1.text_input("Moneda (ej. MXN)", value="MXN")
    t_manual = c2.number_input("1 unidad = X USD", min_value=0.0, value=0.0, step=0.001, format="%.5f")
    cc1, cc2 = st.columns(2)
    if cc1.button("Fijar tipo de cambio manual") and m_manual.strip() and t_manual > 0:
        db.set_config(f"fx_manual_{m_manual.strip().upper()}", str(t_manual))
        st.success(f"Tipo de cambio manual fijado: 1 {m_manual.strip().upper()} = {t_manual:.5f} USD.")
    if cc2.button("Quitar manual (volver a automático)") and m_manual.strip():
        db.set_config(f"fx_manual_{m_manual.strip().upper()}", "")
        st.success("Se quitó el tipo de cambio manual; vuelve al automático.")


def pagina_tutoriales():
    st.title("Tutoriales")
    st.caption("Guías paso a paso para conectar Facebook (Business + cuentas publicitarias) "
               "y Supabase. Cualquier duda, sígueme preguntando.")

    with st.expander("① Conectar un Business de Facebook (token de Usuario del Sistema)",
                     expanded=True):
        st.markdown("""
Para leer tus anuncios, la app necesita un **token de Usuario del Sistema** de cada Business.
Es la forma segura y permanente (no caduca como los tokens normales).

**Paso a paso:**

1. Entra a **[business.facebook.com](https://business.facebook.com)** con el perfil dueño del Business.
2. Arriba a la derecha, abre **Configuración del negocio** (el ⚙️).
3. En el menú izquierdo: **Usuarios → Usuarios del sistema**.
4. Pulsa **Agregar**, ponle un nombre (ej. *"Reporte Ads"*) y rol **Administrador**. Crear.
5. Con el usuario del sistema seleccionado, pulsa **Asignar activos** →
   **Cuentas publicitarias** → marca **todas las cuentas** que quieras ver →
   dale permiso de **Administrar campañas** (control total). Guardar.
6. Ahora pulsa **Generar nuevo token**.
   - **App:** elige tu app (si no tienes, créala en el paso ②).
   - **Permisos (scopes):** marca **`ads_read`** y **`ads_management`**
     (y `business_management` si aparece).
   - Genera y **copia el token** (es largo).
7. Ve a esta app → **Configuración → Conexiones → Agregar conexión**, pégalo y guarda.
8. Vuelve al **Dashboard** y pulsa **Recargar**. Deberían aparecer tus anuncios.

> 🔁 **Repite** los pasos 3–7 **por cada Business** que tengas. Cada Business = una conexión.
> No importa que las cuentas estén en perfiles distintos: lo que manda es el Business.
        """)

    with st.expander("② ¿No tienes App de Facebook? Créala una vez (App ID y secreto)"):
        st.markdown("""
La **App** es solo el "contenedor" técnico que autoriza los llamados. Se crea **una vez** y
sirve para todos tus Business.

1. Entra a **[developers.facebook.com/apps](https://developers.facebook.com/apps)**.
2. **Crear app** → tipo **Negocio/Business** → ponle nombre → crear.
3. En **Configuración → Información básica** copia el **Identificador de la app (App ID)**
   y la **Clave secreta (App Secret)**.
4. En el producto **Marketing API** (Agregar producto) quedará habilitado el acceso a anuncios.
5. En esta app puedes poner ese App ID / App Secret al **agregar la conexión** (o dejar los
   del sistema si ya están configurados). El token del paso ① es lo esencial.

> Con **modo de desarrollo** basta para leer TUS propias cuentas. No necesitas revisión de
> Facebook mientras solo consultes tus anuncios (no publicas nada de cara a terceros).
        """)

    with st.expander("③ Conectar Supabase (segunda fuente de ventas)"):
        st.markdown("""
Si tú registras ventas en **Supabase** (y por ejemplo un socio las deja en Excel/Sheets),
la app une **todas** las fuentes en un mismo lugar.

1. Entra a tu proyecto en **[supabase.com](https://supabase.com)**.
2. **Project Settings (⚙️) → API**. Copia:
   - **Project URL** (algo como `https://xxxx.supabase.co`).
   - Una **API key**: usa la **`service_role`** (o una key con permiso de lectura de tu tabla).
3. Esas dos van como **variables de entorno** en EasyPanel del servicio:
   - `SUPABASE_URL` = tu Project URL
   - `SUPABASE_KEY` = la key
   Guarda y **redespliega**.
4. En esta app → **Configuración → Supabase**: escribe el **nombre de la tabla** y **mapea las
   columnas** (id del anuncio, valor de la venta, fecha/hora, y opcional producto/país e id único).
5. Pulsa **Probar conexión** para ver una muestra, y luego **Sincronizar**.

> Las ventas de Supabase se deduplican por su **id** de fila, así que no se repiten.
> En **Configuración → Moneda → Fuente de las ventas** puedes elegir contar *Todas* las
> fuentes o solo una (para no contar doble si la misma venta está en dos lados).
        """)

    with st.expander("④ Ver la facturación por país (campañas en varios países)"):
        st.markdown("""
La app saca el **país de cada anuncio del *targeting*** del conjunto (a quién se lo estás
mostrando), no del país de la cuenta. Así, si lanzas campañas a **otros países**, cada una
queda etiquetada con su país real.

- El **gasto por país** aparece solo con la conexión de Facebook activa (viene del desglose de Meta).
- La **facturación por país** aparece si tus **ventas traen el país**:
  - En **Excel/Sheets**: agrega una columna **`Pais`** (o `País`) en tus ventas.
  - En **Supabase**: mapea la **columna de país** en Configuración → Supabase.
- Míralo en el **Dashboard → sección "Rendimiento por país"** (gasto, facturación, ventas y ROAS por país).
        """)


def pagina_configuracion():
    st.title("Configuración")
    st.caption("Organizada por tipo: de dónde entran tus ventas, cómo conectas tus Business "
               "de Facebook, y cómo conectas la IA. Cada grupo tiene sus propias pestañas.")
    grupos = st.tabs(["📥 Fuentes de ventas", "🔗 Conectar Business",
                      "🤖 Inteligencia Artificial", "🛠️ Herramientas"])

    # --- Grupo 1: de dónde entran las ventas ---
    with grupos[0]:
        st.caption("Tus ventas pueden venir de varias fuentes a la vez. Aquí las conectas y "
                   "eliges en qué moneda vienen.")
        t = st.tabs(["Excel", "Google Sheets", "Supabase", "Moneda de las ventas"])
        with t[0]:
            _panel_excel()
        with t[1]:
            _panel_gsheets()
        with t[2]:
            _panel_supabase()
        with t[3]:
            _panel_moneda()

    # --- Grupo 2: conectar Facebook / Business ---
    with grupos[1]:
        st.caption("Conecta tus Business Managers (tokens) y revisa las cuentas publicitarias "
                   "que traen.")
        t = st.tabs(["Conexiones (tokens)", "Cuentas"])
        with t[0]:
            seccion_conexiones()
        with t[1]:
            _panel_cuentas()

    # --- Grupo 3: IA ---
    with grupos[2]:
        st.caption("Conecta el asistente de IA (OpenAI o Anthropic) que responde preguntas "
                   "sobre tus anuncios.")
        _panel_ia()

    # --- Grupo 4: herramientas / diagnóstico ---
    with grupos[3]:
        _panel_diagnostico_api()


def _panel_diagnostico_api():
    """Muestra el estado real de los llamados a Facebook (para ver si el freno es
    real o estaba 'pegado')."""
    st.subheader("Diagnóstico de la API de Facebook")
    if st.button("🔄 Actualizar diagnóstico"):
        st.rerun()
    est = fb.obtener_estado()
    ahora = db.ahora()
    c1, c2, c3, c4 = st.columns(4)
    up = est.get("ultimo_polling")
    c1.metric("Última carga FB", _fmt_hace(up, ahora) if up else "—")
    c2.metric("Llamados último ciclo", est.get("llamados_ciclo", 0))
    tts = est.get("throttled_ts")
    c3.metric("Último freno", _fmt_hace(tts, ahora) if tts else "nunca")
    reciente = fb._throttled_reciente()
    c4.metric("¿Frenado ahora?", "Sí" if reciente else "No")
    uso = est.get("uso_api")
    st.caption(f"Uso API reportado por Facebook: **{uso if uso is not None else '—'}%** · "
               f"Cuentas: {est.get('num_cuentas', 0)} · Conexiones: {est.get('num_conexiones', 0)}")
    if tts and not reciente:
        st.success("El último freno ya pasó (hace más de ~12 min). El aviso NO debería "
                   "estar saliendo; si lo ves, es que aún hay frenos recientes.")
    st.markdown("**Registro del sistema** (cada freno queda con su hora y cuenta — así ves "
                "si es real y cada cuánto pasa):")
    msgs = est.get("mensajes", [])
    if msgs:
        st.code("\n".join(msgs[:25]))
    else:
        st.caption("Sin mensajes todavía.")


# --------------------------------------------------------------------------- #
#  Página: Dashboard
# --------------------------------------------------------------------------- #
def _fmt_hace(dt, ahora):
    """'hace 3 min' / 'hace 1 h 5 min' a partir de dos datetimes."""
    if not dt:
        return "aún sin datos"
    seg = int((ahora - dt).total_seconds())
    if seg < 60:
        return "hace unos segundos"
    mins = seg // 60
    if mins < 60:
        return f"hace {mins} min"
    h, m = divmod(mins, 60)
    return f"hace {h} h {m} min" if m else f"hace {h} h"


@st.fragment(run_every=30)
def _timer_actualizacion():
    """Timer en vivo del último 'arrastre' de anuncios. Se refresca solo cada 30 s
    (rerun del fragmento, NO recarga de página → no cierra la sesión). Cuando el
    hilo de fondo trae datos nuevos, dispara un rerun completo para verlos."""
    ahora = db.ahora()
    ult = db.get_config("ultima_actualizacion", "") or ""
    dt = db.a_fecha(ult) if ult else None
    hace = _fmt_hace(dt, ahora)
    cuando = f"{dt.strftime('%d/%m %H:%M')} · {hace}" if dt else hace
    de_noche = ahora.hour >= 23 or ahora.hour < 6
    cad = "cada 1 h (modo noche 11 p.m.–6 a.m.)" if de_noche else "cada 30 min"
    # Indicador de uso del límite de la API de Facebook.
    try:
        est = fb.obtener_estado()
    except Exception:
        est = {}
    # Línea de estado como micro-badges (pills) separados por puntos.
    badges = [f"🔄 Anuncios: {cuando}", f"automática {cad}"]
    uso = est.get("uso_api")
    if uso:  # solo si es > 0 (0% no aporta y confunde)
        badges.append(f"Uso API {uso}%")
    if fb._throttled_reciente():  # se limpia solo cuando ya se recuperó
        badges.append("⚠️ Facebook frenó el ritmo (temporal)")
    vent_check = db.get_config("ultima_sync_ventas", "") or ""
    dtv = db.a_fecha(vent_check) if vent_check else None
    if dtv:
        badges.append(f"Ventas {_fmt_hace(dtv, ahora)}")
    pills = ('<span class="tb-dot">·</span>'.join(
        f'<span class="tb-pill">{b}</span>' for b in badges))
    st.markdown(
        '<style>.tb-row{display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin:2px 0 4px;}'
        '.tb-pill{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);'
        'border-radius:7px;padding:2px 9px;font-size:11px;color:#94a3b8;font-family:Inter,sans-serif;}'
        '.tb-dot{color:#475569;margin:0 2px;}</style>'
        f'<div class="tb-row">{pills}</div>', unsafe_allow_html=True)

    # Si llegaron datos nuevos (anuncios o ventas) desde el último render, refresca.
    vent_cambio = db.get_config("ventas_cambio", "") or ""
    firma = f"{ult}|{vent_cambio}"
    prev = st.session_state.get("_ult_sync_visto")
    st.session_state["_ult_sync_visto"] = firma
    if prev is not None and prev != firma:
        st.rerun()  # rerun de app (mantiene sesión), no recarga la página


def _panel_sync_ventas():
    """Diagnóstico y sincronización manual de ventas (Sheets/Supabase)."""
    import gsheets
    hilos = fb.estado_hilos()
    est = fb.obtener_estado()
    ahora = db.ahora()
    vs = db.get_config("ultima_sync_ventas", "")
    dtv = db.a_fecha(vs) if vs else None
    titulo = ("✅ Ventas al día" if hilos.get("ventas_vivo")
              else "⚠️ Sincronización de ventas detenida")
    with st.expander(f"{titulo} · diagnóstico y sincronización manual", expanded=False):
        c1, c2, c3 = st.columns(3)
        c1.metric("Hilo de ventas", "Activo" if hilos.get("ventas_vivo") else "Detenido")
        c2.metric("Última revisión", _fmt_hace(dtv, ahora) if dtv else "—")
        c3.metric("Sheet configurado", "Sí" if gsheets.get_url() else "No")
        if not gsheets.get_url():
            st.warning("No hay URL del Google Sheet. Pégala en **Configuración → "
                       "Google Sheets** y compártelo como 'Cualquiera con el enlace: Lector'.")
        # CAUSA FRECUENTE: el dashboard filtra por fuente y oculta las del Sheet.
        fuente = db.get_config("fuente_ventas", "todas")
        try:
            en_db = db.resumen_ventas_fuente(gsheets.HOJA).get("num", 0)
        except Exception:
            en_db = "?"
        st.caption(f"Ventas del Google Sheet guardadas en la base: **{en_db}** · "
                   f"Fuente del dashboard: **{fuente}**")
        if fuente not in ("todas", "gsheets"):
            st.error(
                f"⚠️ El dashboard está contando **solo '{fuente}'**, así que NO suma las "
                "ventas del Google Sheet aunque sí se sincronizan. Ve a **Configuración → "
                "Moneda → Fuente de las ventas** y elige **'Todas'** o **'Solo Google Sheets'**.")
        en_curso = fb.sync_ventas_en_curso()
        if en_curso:
            st.info("⏳ Sincronizando en segundo plano… el conteo se actualiza solo al terminar. "
                    "Puedes seguir usando la app.")
        b1, b2 = st.columns(2)
        if b1.button("🔄 Sincronizar ventas AHORA", type="primary",
                     use_container_width=True, disabled=en_curso):
            # NO bloquea la UI: corre en segundo plano y el timer refresca al terminar.
            if fb.sincronizar_ventas_async():
                st.success("Sincronización iniciada en segundo plano. El conteo se "
                           "actualizará solo en cuanto termine (unos segundos).")
            else:
                st.info("Ya hay una sincronización en curso.")
            st.rerun()
        if b2.button("♻️ Reiniciar hilos de fondo", use_container_width=True,
                     help="Vuelve a lanzar los hilos si se detuvieron."):
            fb.iniciar_polling()
            st.success("Hilos reiniciados.")
            st.rerun()
        msgs = est.get("mensajes", [])
        if msgs:
            st.caption("Últimos mensajes del sistema (errores de sync, etc.):")
            st.code("\n".join(msgs[:12]))


def pagina_dashboard():
    top1, top2 = st.columns([3.4, 1.6])
    with top1:
        st.title("Dashboard")
        _timer_actualizacion()
    with top2:
        st.write("")
        # Los DOS botones de datos, arriba a la derecha, mismo estilo (gradiente).
        b1, b2 = st.columns(2)
        if b1.button("🔄 Recargar", use_container_width=True, type="primary",
                     help="Trae datos NUEVOS de Facebook (anuncios, gasto, CPM). Es lo más "
                          "pesado; la app ya lo hace sola cada 30 min."):
            _recargar_facebook()
        if b2.button("↻ Actualizar", use_container_width=True, type="primary",
                     help="RÁPIDO: recalcula los números con lo ya guardado (no llama a Facebook)."):
            try:
                _insights_cache.clear()
            except Exception:
                pass
            st.rerun()

    # Paneles de la barra lateral (registrar venta arriba; IA y cuadre los agrega
    # seccion_vista_general una vez calculados los datos).
    seccion_venta_manual()
    seccion_vista_general()
    st.divider()
    seccion_por_pais()
    st.divider()
    seccion_detalle()
    st.divider()
    seccion_lote()
    st.divider()
    seccion_manual()


# --------------------------------------------------------------------------- #
#  Entrada principal (navegación por páginas)
# --------------------------------------------------------------------------- #
def main():
    _gate_password()
    _inject_css()
    pagina = st.session_state.get("_pagina", "dashboard")
    if pagina not in _PAGINAS:
        pagina = "dashboard"
    sidebar_estado(pagina)
    if pagina == "configuracion":
        pagina_configuracion()
    elif pagina == "tutoriales":
        pagina_tutoriales()
    else:
        pagina_dashboard()
    # La navegación va al FINAL para que quede al pie de la barra lateral,
    # debajo de los paneles (filtros, registrar venta, IA, cuadre).
    _sidebar_nav(pagina)


main()
