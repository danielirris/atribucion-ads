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

st.set_page_config(page_title="Ads Command Center", layout="wide")

# Marcador de versión: sirve para confirmar que el redeploy tomó el código nuevo.
APP_VERSION = "v20 · 2026-08-20"


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


def _roas_color(r):
    if r is None:
        return "#9ca3af"
    if r >= 2:
        return "#22c55e"
    if r >= 1:
        return "#eab308"
    return "#ef4444"


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


@st.cache_data(ttl=120, show_spinner=False)
def _insights_cache(date_preset: str, nivel: str = "ad",
                    since: str = "", until: str = ""):
    """Cachea los insights de Facebook 2 min (por rango y nivel)."""
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
def sidebar_estado():
    cta, ctb = st.sidebar.columns(2)
    if cta.button("Actualizar", use_container_width=True):
        try:
            _insights_cache.clear()
        except Exception:
            pass
        st.rerun()
    if ctb.button("Recargar", use_container_width=True,
                  help="Trae los anuncios de todas las conexiones de Facebook."):
        with st.spinner("Consultando Facebook (todas las conexiones)..."):
            r = fb.cargar_todo()
            try:
                _insights_cache.clear()
            except Exception:
                pass
        if r["num_anuncios"]:
            st.sidebar.success(f"{r['num_anuncios']} anuncios de {r['num_cuentas']} cuenta(s).")
        if r["errores"]:
            st.sidebar.error("Errores: " + "; ".join(r["errores"])[:300])
        st.rerun()

    sidebar_filtros()

    # Versión discreta al final (para confirmar el despliegue).
    st.sidebar.divider()
    st.sidebar.caption(f"Versión: {APP_VERSION}")


def _alias_conexion(conexion_id, conexiones):
    if conexion_id in (None, 0, "0"):
        return "ENV (.env)"
    for c in conexiones:
        if str(c["id"]) == str(conexion_id):
            return c.get("alias") or f"Conexión {conexion_id}"
    return f"Conexión {conexion_id}"


def sidebar_filtros():
    """Filtros globales que afectan al Dashboard (se guardan en session_state)."""
    st.sidebar.divider()
    st.sidebar.markdown("### Filtros")
    todos = db.obtener_anuncios(solo_activos=False)
    conexiones = db.obtener_conexiones()
    business = sorted({_alias_conexion(a.get("conexion_id"), conexiones) for a in todos})
    cuentas = sorted({(a.get("cuenta_nombre") or "—") for a in todos})
    paises = sorted({(a.get("cuenta_pais") or "—") for a in todos})

    st.sidebar.radio("Estado", ["Activos", "Apagados", "Todos"],
                     horizontal=True, key="f_estado")
    st.sidebar.multiselect("Business", business, key="f_business",
                           placeholder="Todos los Business")
    st.sidebar.multiselect("Cuentas publicitarias", cuentas, key="f_cuenta",
                           placeholder="Todas las cuentas")
    st.sidebar.selectbox("País", ["Todos"] + paises, key="f_pais")
    st.sidebar.selectbox("Rango de fechas",
                         ["Hoy", "Últimos 7 días", "Últimos 30 días", "Máximo", "Personalizado"],
                         key="f_rango")
    if st.session_state.get("f_rango") == "Personalizado":
        hoy = db.ahora().date()
        st.sidebar.date_input("Desde", value=hoy - timedelta(days=7), key="f_desde",
                              format="DD/MM/YYYY")
        st.sidebar.date_input("Hasta", value=hoy, key="f_hasta", format="DD/MM/YYYY")

    _resumen_pais(todos)


def _resumen_pais(todos):
    """Muestra gasto invertido por país (según insights del rango elegido)."""
    rango_lbl = st.session_state.get("f_rango", "Hoy")
    preset = {"Hoy": "today", "Últimos 7 días": "last_7d",
              "Últimos 30 días": "last_30d", "Máximo": "maximum"}.get(rango_lbl, "today")
    insights = _insights_cache(preset, "ad")
    if not insights:
        return
    gasto_pais = {}
    for a in todos:
        pais = a.get("cuenta_pais") or "—"
        sp = insights.get(str(a["ad_id"]), {}).get("spend")
        if sp:
            sp_usd = sp * fx.tasa_a_usd(a.get("cuenta_moneda") or "USD")  # -> USD
            gasto_pais[pais] = gasto_pais.get(pais, 0.0) + sp_usd
    if not gasto_pais:
        return
    st.sidebar.markdown("**Gasto por país (USD)**")
    for pais, g in sorted(gasto_pais.items(), key=lambda x: -x[1]):
        st.sidebar.caption(f"{pais}: {_usd(g)}")


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
    # Fuente de ventas del dashboard: 'propias' (Excel/Supabase/Sheets) o 'meta' (pixel).
    origen_ventas = db.get_config("origen_ventas", "propias")

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

        ins = insights.get(g["ins_key"], {}) if g["ins_key"] else {}

        # Fuente de ventas: propias (Excel/Supabase/Sheets) o Meta (compras del pixel).
        if origen_ventas == "meta":
            num = int(ins.get("compras") or 0)
            ingresos_nat = float(ins.get("compras_valor") or 0.0)
            ingresos = ingresos_nat * rate_c  # compras de Meta vienen en moneda de la cuenta
        else:
            num = sum(ventas_agg.get(x["ad_id"], {}).get("num_ventas", 0) for x in ads)
            ingresos_nat = sum(ventas_agg.get(x["ad_id"], {}).get("ingreso_total", 0.0) for x in ads)
            ingresos = ingresos_nat * rate_v  # -> USD

        if nivel == "campaign":
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
        else:
            gasto_nat = sum((calculos.metricas_periodo_actual(x["ad_id"], ahora) or {}).get(
                "gasto_estimado", 0.0) for x in ads)
        gasto = gasto_nat * rate_c

        cpm = (ins.get("cpm") * rate_c) if ins.get("cpm") is not None else None
        costo_conv_nat = ins.get("costo_conversacion")
        costo_conv = (costo_conv_nat * rate_c) if costo_conv_nat is not None else None

        roas = (ingresos / gasto) if gasto and gasto > 0 else 0.0
        activos = sum(1 for x in ads if _es_activo(x))
        rep = ads[0]
        # Objeto a prender/apagar según el nivel de la vista.
        if nivel == "campaign":
            status_obj = rep.get("campaign_id")
        elif nivel == "adset":
            status_obj = rep.get("adset_id")
        else:
            status_obj = rep["ad_id"]
        filas.append({
            "nombre": g["nombre"], "sub": g["sub"], "cuenta": g["cuenta"],
            "moneda": moneda_cuenta, "rate_c": rate_c,
            "activos": activos, "total": len(ads),
            "presupuesto": presupuesto, "presup_nat": presup_nat,
            "spend": spend, "gasto": gasto, "gasto_nat": gasto_nat,
            "pct": (spend / presupuesto * 100) if (spend is not None and presupuesto) else None,
            "cpm": cpm, "ctr": ins.get("ctr"),
            "num": num, "ingresos": ingresos, "ingresos_nat": ingresos_nat,
            "ganancia": ingresos - (gasto or 0.0), "roas": roas,
            "conv": ins.get("conversaciones"),
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
    if rango_lbl == "Personalizado" and st.session_state.get("f_desde") and st.session_state.get("f_hasta"):
        d, h = st.session_state["f_desde"], st.session_state["f_hasta"]
        return ("today", d.strftime("%Y-%m-%d"), h.strftime("%Y-%m-%d"),
                datetime.combine(d, datetime.min.time()),
                datetime.combine(h, datetime.min.time()) + timedelta(days=1))
    RANGO = {
        "Hoy": ("today", ahora.replace(hour=0, minute=0, second=0, microsecond=0)),
        "Últimos 7 días": ("last_7d", ahora - timedelta(days=7)),
        "Últimos 30 días": ("last_30d", ahora - timedelta(days=30)),
        "Máximo": ("maximum", None),
    }
    dp, cut = RANGO.get(rango_lbl, RANGO["Hoy"])
    return dp, "", "", cut, None


@st.cache_data(ttl=120, show_spinner=False)
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


def seccion_vista_general():
    ahora = db.ahora()

    # Control de nivel ARRIBA, bien visible (por defecto Conjuntos).
    nivel_lbl = st.segmented_control(
        "Ver por", ["Campaña", "Conjunto de anuncios", "Anuncio"],
        default=st.session_state.get("f_nivel", "Conjunto de anuncios"),
        key="f_nivel", label_visibility="collapsed") or "Conjunto de anuncios"
    nivel = _NIVELES.get(nivel_lbl, "adset")

    filtro = st.session_state.get("f_estado", "Activos")
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
    if pais_sel != "Todos":
        anuncios = [a for a in anuncios if (a.get("cuenta_pais") or "—") == pais_sel]

    resumen_cta = "todas" if not cuentas_sel else f"{len(cuentas_sel)} cuenta(s)"
    resumen_bus = "todos" if not business_sel else f"{len(business_sel)} business"
    origen = db.get_config("origen_ventas", "propias")
    origen_lbl = "Meta (pixel)" if origen == "meta" else "tus ventas"
    st.caption(f"Ver por: {nivel_lbl} · estado: {filtro} · business: {resumen_bus} · "
               f"cuentas: {resumen_cta} · país: {pais_sel} · rango: {rango_lbl} · "
               f"ventas: {origen_lbl} · valores en USD")

    if not anuncios:
        st.info("No hay datos para este filtro. En la barra lateral (o arriba) pulsa **Recargar** "
                "para traer los anuncios de Facebook.")
        return

    insights = _insights_cache(date_preset, nivel, since, until)
    ventas_agg = db.ventas_agg_por_ad(cutoff, hasta_dt)
    filas = _construir_filas(anuncios, nivel, insights, ventas_agg, ahora)

    if not insights:
        st.info("El **gasto**, CPM y conversaciones vienen de Facebook. Aún no se ven porque no "
                "hay conexión activa: agrega un token en **Configuración → Conexiones** y pulsa "
                "**Recargar**. Mientras tanto el gasto es estimado.")

    st.markdown(_TABLA_CSS, unsafe_allow_html=True)
    _render_lista_nativa(filas, nivel)


_GRID_TMPL = "2.9fr .7fr .9fr .95fr .95fr .85fr .5fr .95fr .95fr .55fr .6fr .8fr .4fr"


def _estado_cell(f, nivel):
    if f["activos"] and (nivel == "ad" or f["activos"] == f["total"]):
        return '<span class="pill pill-run">Corriendo</span>'
    if f["activos"]:
        return f'<span class="pill pill-run">{f["activos"]}/{f["total"]}</span>'
    return '<span class="pill pill-off">Pausado</span>'


def _render_lista_nativa(filas, nivel):
    esc = _html.escape
    _msg = st.session_state.pop("_estado_msg", None)
    if _msg:
        (st.success if _msg[0] == "ok" else st.error)(_msg[1])
    primera = {"ad": "Anuncio", "adset": "Conjunto", "campaign": "Campaña"}[nivel]
    labels = [primera, "Estado", "Cuenta", "Presupuesto", "Gasto", "CPM/CTR", "Ventas",
              "Ingresos", "Ganancia", "ROAS", "Conv.", "Costo/conv", "!"]
    st.markdown(
        f'<style>.gr{{display:grid;grid-template-columns:{_GRID_TMPL};gap:12px;'
        f'align-items:center;padding:6px 4px;}}'
        f'.gr .h2{{color:#8fd6db;font-size:11.5px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.04em;}}</style>', unsafe_allow_html=True)

    ACC = [0.6, 11.6, 0.7, 0.7]
    hc = st.columns(ACC)
    hc[0].markdown('<div class="h2" style="text-align:center">On/Off</div>', unsafe_allow_html=True)
    hc[1].markdown('<div class="gr">' + "".join(f'<div class="h2">{l}</div>' for l in labels)
                   + '</div>', unsafe_allow_html=True)
    hc[2].markdown('<div class="h2" style="text-align:center">Pres.</div>', unsafe_allow_html=True)
    hc[3].markdown('<div class="h2" style="text-align:center">Dup.</div>', unsafe_allow_html=True)

    for f in filas:
        g = f["ganancia"]
        gcls = "up" if g >= 0 else "down"
        cpm = _usd(f["cpm"]) if f["cpm"] is not None else "—"
        ctr = f'{f["ctr"]:.2f}%' if f["ctr"] is not None else "—"
        conv = int(f["conv"]) if f["conv"] is not None else "—"
        costoc = (_money_html(f["costo_conv"], f.get("costo_conv_nat"), f["moneda"])
                  if f.get("costo_conv") is not None else '<div class="big">—</div>')
        # Alerta: gastando y con ROAS bajo (< 1.5x)
        alerta = bool(f["gasto"] and f["gasto"] > 0 and f["roas"] < 1.5)
        if alerta:
            msg = (f"Alerta: ROAS {f['roas']:.2f}x por debajo de 1.5x. "
                   f"Gasto {_usd(f['gasto'])}, ingresos {_usd(f['ingresos'])}, "
                   f"{f['num']} venta(s), presupuesto {_usd(f['presupuesto'])}. "
                   f"Considera bajar o pausar.")
            alerta_cell = f'<span class="alert-badge" title="{esc(msg)}">!</span>'
        else:
            alerta_cell = '<span class="ok-dot" title="Sin alertas"></span>'
        nombre_html = (f'<div class="big" style="white-space:normal;word-break:break-word">'
                       f'{esc(str(f["nombre"]))[:90]}</div>'
                       f'<div class="sub">{esc(str(f["sub"]))[:22]}</div>')
        if alerta:
            nombre_html = f'<div class="name-alert">{nombre_html}</div>'
        cells = [
            nombre_html,
            _estado_cell(f, nivel),
            f'<div class="sub" style="font-size:12.5px">{esc(str(f["cuenta"]))[:18]}</div>',
            _money_html(f["presupuesto"], f["presup_nat"], f["moneda"], "m-peri"),
            _money_html(f["gasto"], f["gasto_nat"], f["moneda"])
            + ('<div class="sub">est.</div>' if f["spend"] is None else ''),
            f'<div class="big">{cpm}</div><div class="sub">{ctr} CTR</div>',
            f'<div class="big">{f["num"]}</div>',
            _money_html(f["ingresos"], f["ingresos_nat"], f["moneda"], "m-mint"),
            f'<div class="big {gcls}">{"+" if g>=0 else ""}{_usd(g)}</div>',
            f'<div class="big" style="color:{_roas_color(f["roas"])};font-size:16px">{f["roas"]:.2f}x</div>',
            f'<div class="big">{conv}</div>',
            costoc,
            alerta_cell,
        ]
        row_html = '<div class="gr">' + "".join(f'<div>{c}</div>' for c in cells) + '</div>'
        rc = st.columns(ACC, vertical_alignment="center")
        rc[0].toggle(" ", value=(f["activos"] > 0), key=f"tg_{f['sub']}",
                     on_change=_toggle_estado_cb, args=(f,),
                     label_visibility="collapsed",
                     help="Prender / Apagar en Facebook")
        rc[1].markdown(row_html, unsafe_allow_html=True)
        with rc[2].popover("", icon=":material/edit:"):
            _pop_presupuesto(f)
        with rc[3].popover("", icon=":material/content_copy:"):
            _pop_duplicar(f)
        st.markdown('<hr class="rowline">', unsafe_allow_html=True)


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
    st.caption(f"Presupuesto actual: {_usd(f['presupuesto'])}"
               + (f" ({_num(f['presup_nat'])} {f['moneda']})" if (f['moneda'] or 'USD').upper() != 'USD' else ""))
    nuevo = st.number_input("Nuevo presupuesto diario (USD)", min_value=0.0,
                            value=float(f["presupuesto"] or 0.0), step=1.0,
                            key=f"pres_{f['sub']}")
    if (f["moneda"] or "USD").upper() != "USD":
        st.caption(f"Se enviará a Facebook ≈ {_num(nuevo / (f['rate_c'] or 1))} {f['moneda']}")
    if st.button("Aplicar en Facebook", type="primary", key=f"presbtn_{f['sub']}",
                 use_container_width=True):
        r = _aplicar_presupuesto_grupo(f, nuevo)
        if r["ok"] and r.get("solo_local"):
            st.warning("Guardado localmente (sin conexión de Facebook).")
        elif r["ok"]:
            st.success("Presupuesto actualizado en Facebook.")
        else:
            st.error(f"Facebook rechazó el cambio: {r['error']}")
        time.sleep(1.0)
        st.rerun()


def _pop_duplicar(f):
    st.markdown(f"**Duplicar: {esc_nombre(f)}**")
    hay = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False
    if not hay:
        st.info("Necesitas una conexión de Facebook activa para duplicar.")
        return
    n = st.number_input("Número de copias", min_value=1, max_value=10, value=4, step=1,
                        key=f"dupn_{f['sub']}")
    presu = st.number_input("Presupuesto por copia (USD)", min_value=0.0,
                            value=float(f["presupuesto"] or 0.0), step=1.0, key=f"dupp_{f['sub']}")
    activar = st.toggle("Activar copias de inmediato", value=False, key=f"dupa_{f['sub']}")
    if st.button("Duplicar ahora", type="primary", key=f"dupbtn_{f['sub']}",
                 use_container_width=True):
        nativo = presu / (f["rate_c"] or 1)
        with st.spinner("Duplicando en Facebook..."):
            res = fb.duplicar_anuncio(f["ad_rep"], int(n), float(nativo), activar=bool(activar),
                                      conexion_id=f.get("conexion_id"), cuenta_id=f.get("cuenta_id"),
                                      cuenta_nombre=f.get("cuenta_nombre"))
        ok = res.get("exitosas", [])
        fail = res.get("fallidas", [])
        if res.get("error_global"):
            st.error(res["error_global"])
        if ok:
            st.success(f"{len(ok)} copia(s) creada(s).")
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
    st.header("Registrar venta")
    st.caption("Registra una venta al instante (se atribuye al período de presupuesto "
               "activo del anuncio). Alternativa al Excel para uso online.")
    anuncios = db.obtener_anuncios(solo_activos=False)
    opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}

    with st.form("form_venta_manual"):
        c1, c2, c3 = st.columns([3, 2, 2])
        with c1:
            if opciones:
                modo_libre = st.checkbox("Escribir ID manualmente", value=False)
                if modo_libre:
                    ad_id = st.text_input("ID_Anuncio")
                else:
                    sel = st.selectbox("Anuncio", list(opciones.keys()))
                    ad_id = opciones[sel]
            else:
                ad_id = st.text_input("ID_Anuncio (aún no hay anuncios cargados)")
        with c2:
            valor = st.number_input("Valor de la venta", min_value=0.0, value=0.0, step=1.0)
        with c3:
            usar_ahora = st.toggle("Usar hora actual", value=True)
        hora = None
        if not usar_ahora:
            cf1, cf2 = st.columns(2)
            with cf1:
                f = st.date_input("Fecha")
            with cf2:
                h = st.time_input("Hora")
            hora = datetime.combine(f, h)
        enviado = st.form_submit_button("Registrar venta", type="primary")

    if enviado:
        r = watcher.registrar_venta_manual(ad_id, valor, hora=hora)
        if r["ok"]:
            destino = f"período {r['periodo_id']}" if r["periodo_id"] else "sin período (no había presupuesto activo a esa hora)"
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

    st.divider()
    _bloque_duplicar(ad_id, anuncio, ahora)


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
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@500;600;700&family=Inter:wght@400;500&display=swap');
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"], header[data-testid="stHeader"]{ display:none !important; }
[data-testid="stAppViewContainer"]{ background:#0a0d13 !important; overflow:hidden; }
[data-testid="stAppViewContainer"] .main .block-container{ max-width:940px; padding-top:13vh; }
/* Marca a un costado (tipo Facebook) */
.login-side .login-logo{ margin:0 0 18px; }
.login-title-side{ font-family:'Geist',sans-serif;font-weight:700;font-size:32px;line-height:1.12;
    background:linear-gradient(100deg,#eafcff,#c7f6ee 45%,#d9cbff);
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;margin:0; }
.login-sub-side{ font-family:'Inter',sans-serif;color:#a7b6c4;font-size:14.5px;
    margin:12px 0 18px;max-width:380px;line-height:1.55; }
.login-feats{ list-style:none;padding:0;margin:0; }
.login-feats li{ font-family:'Inter',sans-serif;color:#c3ccd6;font-size:13.5px;
    margin:9px 0;padding-left:24px;position:relative; }
.login-feats li::before{ content:"";position:absolute;left:0;top:5px;width:12px;height:12px;
    border-radius:50%;background:linear-gradient(135deg,#8cd2d7,#c7c4ff);
    box-shadow:0 0 10px rgba(140,210,215,.5); }
.login-foot{ position:fixed; left:0; right:0; bottom:16px; text-align:center;
    font-family:'Inter',sans-serif; color:#7c8a99; font-size:12px; z-index:3; line-height:1.7; }
.login-foot .ver{ color:#5f6b7a; font-size:11px; }
.login-foot .hp{ color:#ff7a90; }
/* Matriz de puntos animada */
[data-testid="stAppViewContainer"]::before{
    content:""; position:fixed; inset:-25%;
    background-image:radial-gradient(rgba(140,210,215,.22) 1.5px, transparent 1.7px);
    background-size:26px 26px;
    animation:matrixPulse 5.5s ease-in-out infinite;
    z-index:0;
}
[data-testid="stAppViewContainer"]::after{
    content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
    background:
      radial-gradient(560px 560px at 18% 22%, rgba(191,242,226,.22), transparent 60%),
      radial-gradient(600px 600px at 84% 80%, rgba(230,213,255,.22), transparent 60%),
      radial-gradient(520px 520px at 55% 8%, rgba(199,196,255,.16), transparent 60%),
      radial-gradient(480px 480px at 8% 92%, rgba(140,210,215,.14), transparent 60%);
    animation:orbFloat 16s ease-in-out infinite alternate;
}
.login-feats li:nth-child(1)::before{ background:linear-gradient(135deg,#8cd2d7,#a8eff4); }
.login-feats li:nth-child(2)::before{ background:linear-gradient(135deg,#BFF2E2,#8cd2d7); }
.login-feats li:nth-child(3)::before{ background:linear-gradient(135deg,#C7C4FF,#E6D5FF); }
@keyframes matrixPulse{ 0%,100%{opacity:.35} 50%{opacity:.85} }
@keyframes orbFloat{ 0%{transform:translate(0,0)} 100%{transform:translate(-24px,26px)} }
/* Contenido por encima */
[data-testid="stAppViewContainer"] .main .block-container > *{ position:relative; z-index:2; }
.login-brand{ text-align:center; margin:2vh 0 0.5rem; }
.login-logo{ width:64px;height:64px;margin:0 auto 14px;border-radius:20px;
    background:linear-gradient(150deg,#a2e9ee,#8cd2d7 55%,#c4c1fb);
    display:flex;align-items:center;justify-content:center;
    box-shadow:0 10px 40px rgba(140,210,215,.35), inset 0 1px 0 rgba(255,255,255,.4); }
.login-logo svg{ width:34px;height:34px; }
.login-title{ font-family:'Geist',sans-serif;font-weight:700;font-size:23px;
    letter-spacing:.12em;color:#eafcff;margin:0; }
.login-sub{ font-family:'Inter',sans-serif;color:#9fb2c2;font-size:13.5px;margin-top:6px; }
/* Tarjeta glass */
div[data-testid="stVerticalBlockBorderWrapper"]{
    background:rgba(20,24,32,.62) !important; backdrop-filter:blur(18px);
    border:1px solid rgba(255,255,255,.09) !important; border-radius:20px !important;
    box-shadow:0 24px 60px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.05); }
.stTextInput input{ background:rgba(255,255,255,.04) !important; border-radius:12px !important;
    border:1px solid rgba(255,255,255,.12) !important; color:#eaf2f6 !important; }
.stTextInput input:focus{ border-color:#8cd2d7 !important; box-shadow:0 0 0 2px rgba(140,210,215,.25) !important; }
.stButton>button, .stFormSubmitButton>button{ border-radius:12px !important;
    background:linear-gradient(180deg,#a2e9ee,#8cd2d7) !important; color:#00373a !important;
    font-family:'Geist',sans-serif;font-weight:700;border:none !important;
    box-shadow:0 8px 26px rgba(140,210,215,.3) !important; }
</style>
"""

_LOGIN_HEADER_SIDE = """
<div class="login-side">
  <div class="login-logo">
    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Z" fill="#00373a" opacity=".18"/>
      <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Z" stroke="#00373a" stroke-width="1.3"/>
      <path d="M7 14l3-4 2 2.4L15 8l2 3" stroke="#00373a" stroke-width="1.6"
            stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    </svg>
  </div>
  <h1 class="login-title-side">Ads Command Center</h1>
  <p class="login-sub-side">Atribución de ventas y control de tus anuncios de Facebook.
     Todo en dólares, en un solo lugar.</p>
  <ul class="login-feats">
    <li>Campañas, conjuntos y anuncios en una sola vista</li>
    <li>ROAS, gasto y presupuesto de hoy</li>
    <li>Excel + Supabase unidos automáticamente</li>
  </ul>
</div>
"""


def _gate_password():
    if not config.APP_PASSWORD:
        return
    if st.session_state.get("_auth_ok"):
        return

    st.markdown(_LOGIN_CSS, unsafe_allow_html=True)
    colL, colR = st.columns([1.1, 0.9], gap="large", vertical_alignment="center")
    with colL:
        st.markdown(_LOGIN_HEADER_SIDE, unsafe_allow_html=True)
    with colR:
        with st.container(border=True):
            st.markdown("#### Iniciar sesión")
            usuario = st.text_input("Usuario", key="_user", placeholder="usuario")
            pwd = st.text_input("Contraseña", type="password", key="_pwd", placeholder="••••••••")
            entrar = st.button("Entrar", use_container_width=True, type="primary")
            if entrar:
                if usuario.strip() == config.APP_USER and pwd == config.APP_PASSWORD:
                    st.session_state["_auth_ok"] = True
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")

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
    @import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap');
    :root{
        --bg:#0d0f16; --surface:#171a22; --surface2:#20232c; --stroke:rgba(255,255,255,.07);
        --tint:#8cd2d7; --mint:#BFF2E2; --lav:#E6D5FF; --peri:#C7C4FF; --txt:#e6e7ee; --sub:#aeb8c6;
    }
    html, body, [data-testid="stAppViewContainer"]{ color:var(--txt); }
    [data-testid="stAppViewContainer"]{
        background:
          radial-gradient(1000px 640px at 92% -6%, rgba(140,210,215,.22), transparent 56%),
          radial-gradient(900px 660px at -10% 6%, rgba(230,213,255,.20), transparent 56%),
          radial-gradient(760px 760px at 50% 118%, rgba(199,196,255,.17), transparent 56%),
          radial-gradient(600px 600px at 30% 45%, rgba(191,242,226,.07), transparent 60%),
          linear-gradient(180deg,#0d0f16,#0b0d13) !important;
    }
    /* Matriz de puntos sutil + brillo suave, detrás del contenido */
    [data-testid="stAppViewContainer"]::before{
        content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
        background-image:radial-gradient(rgba(140,210,215,.10) 1px, transparent 1.4px);
        background-size:30px 30px; opacity:.5; animation:appPulse 8s ease-in-out infinite;
    }
    @keyframes appPulse{ 0%,100%{opacity:.30} 50%{opacity:.6} }
    [data-testid="stAppViewContainer"] .main .block-container{ position:relative; z-index:1; }

    body, .stMarkdown, p, label, input, textarea, button, li, td, th{ font-family:'Inter',sans-serif; }
    h1,.stMarkdown h1{ font-family:'Geist',sans-serif !important; letter-spacing:-.02em;
        background:linear-gradient(100deg,#eafcff,#c7f6ee 40%,#d9cbff);
        -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
    h2,.stMarkdown h2{ font-family:'Geist',sans-serif !important; letter-spacing:-.01em; color:#eef2f6; }
    h3,h4,.stMarkdown h3,.stMarkdown h4{ font-family:'Geist',sans-serif !important; color:var(--mint); }
    /* Restaurar la fuente de iconos de Material */
    [class*="material-symbols"], [class*="material-icons"], .material-icons,
    span[data-testid="stIconMaterial"], [data-testid="stIconMaterial"]{
        font-family:'Material Symbols Outlined','Material Symbols Rounded','Material Icons' !important;
        -webkit-text-fill-color:initial;
    }
    /* Sidebar con un toque de color */
    [data-testid="stSidebar"]{
        background:linear-gradient(180deg,#14161e,#0e1015);
        border-right:1px solid var(--stroke);
        box-shadow:inset -1px 0 0 rgba(140,210,215,.06);
    }
    [data-testid="stSidebar"] h3{ color:var(--tint); }

    /* Botones con BORDE de color (degradado) */
    .stButton>button, .stDownloadButton>button, .stFormSubmitButton>button{
        border-radius:12px; font-weight:600; color:var(--txt); transition:all .18s ease;
        border:1px solid transparent;
        background:linear-gradient(#181b23,#141720) padding-box,
                  linear-gradient(120deg, rgba(140,210,215,.65), rgba(199,196,255,.65)) border-box;
    }
    .stButton>button:hover, .stDownloadButton>button:hover{
        color:var(--mint); box-shadow:0 6px 22px rgba(140,210,215,.22);
        background:linear-gradient(#1c2029,#161a23) padding-box,
                  linear-gradient(120deg, #8cd2d7, #c7c4ff) border-box;
    }
    .stButton>button[kind="primary"], .stFormSubmitButton>button[kind="primary"]{
        background:linear-gradient(180deg,#a8eff4,#8cd2d7) !important; color:#00373a !important;
        border:none !important; box-shadow:0 8px 26px rgba(140,210,215,.30) !important;
    }
    /* Inputs */
    [data-baseweb="input"] input, [data-baseweb="textarea"] textarea, .stTextInput input,
    .stNumberInput input, [data-baseweb="select"]>div{
        border-radius:11px !important; background:rgba(255,255,255,.03) !important;
    }
    /* Tarjetas / contenedores / expanders (glass) */
    [data-testid="stExpander"], div[data-testid="stVerticalBlockBorderWrapper"]{
        background:rgba(23,26,34,.55) !important; backdrop-filter:blur(16px);
        border:1px solid var(--stroke) !important; border-radius:18px;
        box-shadow:0 10px 30px rgba(0,0,0,.25), inset 0 1px 0 rgba(255,255,255,.04);
    }
    /* Tabs con color activo */
    .stTabs [data-baseweb="tab-list"]{ gap:6px; border-bottom:1px solid var(--stroke); }
    .stTabs [data-baseweb="tab"]{ border-radius:12px 12px 0 0; color:var(--sub); }
    .stTabs [aria-selected="true"]{ color:var(--mint) !important; }
    .stTabs [data-baseweb="tab-highlight"]{ background:linear-gradient(90deg,#8cd2d7,#c7c4ff) !important; height:3px; }
    /* Alertas con vidrio */
    [data-testid="stAlert"]{ border-radius:14px; }
    [data-testid="stMetricValue"]{ font-family:'Geist',sans-serif; color:var(--mint); }
    hr{ border-color:var(--stroke) !important; }
    </style>
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
        if st.button("Guardar columnas manuales"):
            gs.set_overrides(ov_id, ov_val, ov_hora, ov_pais)
            st.success("Guardado. Vuelve a Sincronizar.")

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
                 help="Reprocesa todas las filas (útil la primera vez)."):
        n = watcher.importar_todo()
        st.success(f"{n} ventas importadas.")


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
    st.caption("De dónde salen las columnas Ventas e Ingresos: tus ventas (Excel/Supabase/"
               "Google Sheets) o las **compras que reporta Meta** (pixel).")
    origen_actual = db.get_config("origen_ventas", "propias")
    op = {"propias": "Mis ventas (Excel/Supabase/Sheets)", "meta": "Meta (compras del pixel)"}
    sel_o = st.radio("Fuente de ventas", list(op.keys()),
                     index=list(op.keys()).index(origen_actual) if origen_actual in op else 0,
                     format_func=lambda k: op[k], horizontal=True, key="sel_origen")
    if st.button("Guardar fuente de ventas", type="primary"):
        db.set_config("origen_ventas", sel_o)
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


def pagina_configuracion():
    st.title("Configuración")
    st.caption("Conecta tus Business, tus fuentes de ventas (Excel + Supabase), la moneda y "
               "revisa tus cuentas.")
    tabs = st.tabs(["Conexiones", "Excel", "Google Sheets", "Supabase", "Moneda", "Cuentas"])
    with tabs[0]:
        seccion_conexiones()
    with tabs[1]:
        _panel_excel()
    with tabs[2]:
        _panel_gsheets()
    with tabs[3]:
        _panel_supabase()
    with tabs[4]:
        _panel_moneda()
    with tabs[5]:
        _panel_cuentas()


# --------------------------------------------------------------------------- #
#  Página: Dashboard
# --------------------------------------------------------------------------- #
def pagina_dashboard():
    top1, top2 = st.columns([5, 1])
    with top1:
        st.title("Dashboard")
        st.caption(f"Última actualización: {db.a_texto(db.ahora())}")
    with top2:
        st.write("")
        st.write("")
        if st.button("Actualizar", use_container_width=True, type="primary"):
            try:
                _insights_cache.clear()
            except Exception:
                pass
            st.rerun()

    seccion_vista_general()
    st.divider()
    seccion_por_pais()
    st.divider()
    seccion_venta_manual()
    st.divider()
    seccion_detalle()
    st.divider()
    seccion_lote()
    st.divider()
    seccion_manual()

    # Actualización automática de la vista cada 15 minutos.
    _auto_refresco(900)


def _auto_refresco(segundos: int):
    st.components.v1.html(
        f"<script>setTimeout(function(){{window.parent.location.reload();}}, {segundos*1000});</script>",
        height=0,
    )


# --------------------------------------------------------------------------- #
#  Entrada principal (navegación por páginas)
# --------------------------------------------------------------------------- #
def main():
    _gate_password()
    _inject_css()
    sidebar_estado()
    nav = st.navigation([
        st.Page(pagina_dashboard, title="Dashboard", icon=":material/dashboard:", default=True),
        st.Page(pagina_configuracion, title="Configuración", icon=":material/settings:"),
    ])
    nav.run()


main()
