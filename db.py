"""
db.py
Todas las funciones de acceso a SQLite.

Se abre una conexión nueva por operación (con check_same_thread=False y un
lock global) para que sea seguro escribir desde varios hilos:
el hilo de Streamlit, el hilo de polling de Facebook y el hilo del watchdog.

Todos los timestamps se guardan como texto ISO 8601 con fecha + hora completa
(YYYY-MM-DDTHH:MM:SS) para soportar análisis de varios días.
"""
import sqlite3
import threading
from datetime import datetime
from typing import Optional

import config

# Lock global para serializar escrituras entre hilos.
_LOCK = threading.Lock()

# Formato canónico de timestamp usado en toda la app.
FMT = "%Y-%m-%dT%H:%M:%S"


def ahora() -> datetime:
    """Timestamp actual (sin microsegundos, para consistencia)."""
    return datetime.now().replace(microsecond=0)


def a_texto(dt: datetime) -> str:
    """Convierte un datetime a texto ISO para guardar en la DB."""
    return dt.strftime(FMT)


def a_fecha(texto: Optional[str]) -> Optional[datetime]:
    """Convierte texto de la DB a datetime. Tolera None y microsegundos."""
    if not texto:
        return None
    txt = str(texto).strip()
    for fmt in (FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# --------------------------------------------------------------------------- #
#  Inicialización del esquema
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Crea las tablas si no existen. Idempotente."""
    with _LOCK, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conexiones (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alias       TEXT,
                app_id      TEXT,
                app_secret  TEXT,
                token       TEXT NOT NULL,
                activo      INTEGER DEFAULT 1,
                ultimo_error TEXT,
                creado      TEXT
            );

            CREATE TABLE IF NOT EXISTS anuncios (
                ad_id                TEXT PRIMARY KEY,
                nombre               TEXT,
                adset_id             TEXT,
                activo               INTEGER DEFAULT 1,
                conexion_id          INTEGER,
                cuenta_id            TEXT,
                cuenta_nombre        TEXT,
                ultima_actualizacion TEXT
            );

            CREATE TABLE IF NOT EXISTS periodos (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id             TEXT NOT NULL,
                presupuesto       REAL NOT NULL,
                hora_inicio       TEXT NOT NULL,
                hora_fin          TEXT,
                duracion_minutos  REAL
            );

            CREATE TABLE IF NOT EXISTS ventas (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id         TEXT NOT NULL,
                valor_venta   REAL NOT NULL,
                hora_venta    TEXT NOT NULL,
                periodo_id    INTEGER,
                hoja_origen   TEXT,
                ext_id        TEXT,
                producto      TEXT
            );

            CREATE TABLE IF NOT EXISTS config_kv (
                clave TEXT PRIMARY KEY,
                valor TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_periodos_ad   ON periodos(ad_id);
            CREATE INDEX IF NOT EXISTS idx_ventas_ad     ON ventas(ad_id);
            CREATE INDEX IF NOT EXISTS idx_ventas_periodo ON ventas(periodo_id);
            """
        )
        conn.commit()

    # Migración defensiva: agrega columnas nuevas si la DB viene de versión previa.
    _asegurar_columna("anuncios", "adset_id", "TEXT")
    _asegurar_columna("anuncios", "fecha_creacion", "TEXT")
    _asegurar_columna("anuncios", "effective_status", "TEXT")
    _asegurar_columna("anuncios", "conexion_id", "INTEGER")
    _asegurar_columna("anuncios", "cuenta_id", "TEXT")
    _asegurar_columna("anuncios", "cuenta_nombre", "TEXT")
    _asegurar_columna("anuncios", "cuenta_pais", "TEXT")
    _asegurar_columna("anuncios", "cuenta_moneda", "TEXT")
    _asegurar_columna("anuncios", "campaign_id", "TEXT")
    _asegurar_columna("anuncios", "campaign_nombre", "TEXT")
    _asegurar_columna("anuncios", "adset_nombre", "TEXT")
    _asegurar_columna("anuncios", "budget_nivel", "TEXT")
    _asegurar_columna("anuncios", "budget_obj_id", "TEXT")
    _asegurar_columna("ventas", "ext_id", "TEXT")
    _asegurar_columna("ventas", "producto", "TEXT")
    _asegurar_columna("ventas", "pais", "TEXT")


def _asegurar_columna(tabla: str, columna: str, tipo: str) -> None:
    with _LOCK, _conn() as conn:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({tabla})")]
        if columna not in cols:
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")
            conn.commit()


# --------------------------------------------------------------------------- #
#  ANUNCIOS
# --------------------------------------------------------------------------- #
def upsert_anuncio(ad_id: str, nombre: str, adset_id: Optional[str] = None,
                   activo: int = 1, fecha_creacion: Optional[str] = None,
                   effective_status: Optional[str] = None,
                   conexion_id: Optional[int] = None, cuenta_id: Optional[str] = None,
                   cuenta_nombre: Optional[str] = None, cuenta_pais: Optional[str] = None,
                   campaign_id: Optional[str] = None, campaign_nombre: Optional[str] = None,
                   adset_nombre: Optional[str] = None, cuenta_moneda: Optional[str] = None,
                   budget_nivel: Optional[str] = None, budget_obj_id: Optional[str] = None) -> None:
    """Inserta o actualiza un anuncio."""
    with _LOCK, _conn() as conn:
        conn.execute(
            """
            INSERT INTO anuncios
                (ad_id, nombre, adset_id, activo, fecha_creacion, effective_status,
                 conexion_id, cuenta_id, cuenta_nombre, cuenta_pais, cuenta_moneda,
                 campaign_id, campaign_nombre, adset_nombre, budget_nivel, budget_obj_id,
                 ultima_actualizacion)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ad_id) DO UPDATE SET
                nombre               = excluded.nombre,
                adset_id             = COALESCE(excluded.adset_id, anuncios.adset_id),
                activo               = excluded.activo,
                fecha_creacion       = COALESCE(excluded.fecha_creacion, anuncios.fecha_creacion),
                effective_status     = excluded.effective_status,
                conexion_id          = COALESCE(excluded.conexion_id, anuncios.conexion_id),
                cuenta_id            = COALESCE(excluded.cuenta_id, anuncios.cuenta_id),
                cuenta_nombre        = COALESCE(excluded.cuenta_nombre, anuncios.cuenta_nombre),
                cuenta_pais          = excluded.cuenta_pais,
                cuenta_moneda        = COALESCE(excluded.cuenta_moneda, anuncios.cuenta_moneda),
                campaign_id          = COALESCE(excluded.campaign_id, anuncios.campaign_id),
                campaign_nombre      = COALESCE(excluded.campaign_nombre, anuncios.campaign_nombre),
                adset_nombre         = COALESCE(excluded.adset_nombre, anuncios.adset_nombre),
                budget_nivel         = COALESCE(excluded.budget_nivel, anuncios.budget_nivel),
                budget_obj_id        = COALESCE(excluded.budget_obj_id, anuncios.budget_obj_id),
                ultima_actualizacion = excluded.ultima_actualizacion
            """,
            (str(ad_id), nombre, adset_id, activo, fecha_creacion, effective_status,
             conexion_id, cuenta_id, cuenta_nombre, cuenta_pais, cuenta_moneda,
             campaign_id, campaign_nombre, adset_nombre, budget_nivel, budget_obj_id,
             a_texto(ahora())),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
#  CONEXIONES (multi-Business / multi-token)
# --------------------------------------------------------------------------- #
def agregar_conexion(alias: str, token: str, app_id: Optional[str] = None,
                     app_secret: Optional[str] = None) -> int:
    with _LOCK, _conn() as conn:
        cur = conn.execute(
            """INSERT INTO conexiones (alias, app_id, app_secret, token, activo, creado)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (alias, app_id, app_secret, token, a_texto(ahora())),
        )
        conn.commit()
        return cur.lastrowid


def obtener_conexiones(solo_activas: bool = False) -> list:
    with _conn() as conn:
        q = "SELECT * FROM conexiones"
        if solo_activas:
            q += " WHERE activo = 1"
        q += " ORDER BY id"
        return [dict(r) for r in conn.execute(q)]


def obtener_conexion(conexion_id: int) -> Optional[dict]:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM conexiones WHERE id = ?", (conexion_id,)).fetchone()
        return dict(r) if r else None


def actualizar_conexion(conexion_id: int, **campos) -> None:
    if not campos:
        return
    cols = ", ".join(f"{k} = ?" for k in campos)
    with _LOCK, _conn() as conn:
        conn.execute(f"UPDATE conexiones SET {cols} WHERE id = ?",
                     (*campos.values(), conexion_id))
        conn.commit()


def eliminar_conexion(conexion_id: int) -> None:
    with _LOCK, _conn() as conn:
        conn.execute("DELETE FROM conexiones WHERE id = ?", (conexion_id,))
        conn.commit()


def marcar_inactivos(ids_activos: list) -> None:
    """Marca como inactivos los anuncios que ya no están en la lista de activos."""
    if ids_activos is None:
        return
    with _LOCK, _conn() as conn:
        if ids_activos:
            placeholders = ",".join("?" for _ in ids_activos)
            conn.execute(
                f"UPDATE anuncios SET activo = 0 WHERE ad_id NOT IN ({placeholders})",
                [str(i) for i in ids_activos],
            )
        else:
            conn.execute("UPDATE anuncios SET activo = 0")
        conn.commit()


def set_estado_ads(ad_ids: list, activar: bool) -> None:
    """Marca varios anuncios como ACTIVE/PAUSED localmente (tras cambiarlos en FB)."""
    if not ad_ids:
        return
    estado = "ACTIVE" if activar else "PAUSED"
    activo = 1 if activar else 0
    with _LOCK, _conn() as conn:
        ph = ",".join("?" for _ in ad_ids)
        conn.execute(
            f"UPDATE anuncios SET activo = ?, effective_status = ? WHERE ad_id IN ({ph})",
            [activo, estado, *[str(a) for a in ad_ids]])
        conn.commit()


def obtener_anuncios(solo_activos: bool = False) -> list:
    with _conn() as conn:
        q = "SELECT * FROM anuncios"
        if solo_activos:
            q += " WHERE activo = 1"
        q += " ORDER BY nombre"
        return [dict(r) for r in conn.execute(q)]


def obtener_anuncio(ad_id: str) -> Optional[dict]:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM anuncios WHERE ad_id = ?", (str(ad_id),)).fetchone()
        return dict(r) if r else None


# --------------------------------------------------------------------------- #
#  PERIODOS
# --------------------------------------------------------------------------- #
def periodo_abierto(ad_id: str) -> Optional[dict]:
    """Devuelve el período activo (hora_fin IS NULL) más reciente de un anuncio."""
    with _conn() as conn:
        r = conn.execute(
            """
            SELECT * FROM periodos
            WHERE ad_id = ? AND hora_fin IS NULL
            ORDER BY hora_inicio DESC, id DESC LIMIT 1
            """,
            (str(ad_id),),
        ).fetchone()
        return dict(r) if r else None


def abrir_periodo(ad_id: str, presupuesto: float,
                  hora_inicio: Optional[datetime] = None) -> int:
    """Abre un período nuevo (hora_fin = NULL). Devuelve el id del período."""
    hi = hora_inicio or ahora()
    with _LOCK, _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO periodos (ad_id, presupuesto, hora_inicio, hora_fin, duracion_minutos)
            VALUES (?, ?, ?, NULL, NULL)
            """,
            (str(ad_id), float(presupuesto), a_texto(hi)),
        )
        conn.commit()
        return cur.lastrowid


def cerrar_periodo(ad_id: str, hora_fin: Optional[datetime] = None) -> Optional[int]:
    """
    Cierra el período abierto de un anuncio: guarda hora_fin y calcula
    duracion_minutos. Devuelve el id cerrado o None si no había período abierto.
    """
    hf = hora_fin or ahora()
    abierto = periodo_abierto(ad_id)
    if not abierto:
        return None
    inicio = a_fecha(abierto["hora_inicio"]) or hf
    duracion = max(0.0, (hf - inicio).total_seconds() / 60.0)
    with _LOCK, _conn() as conn:
        conn.execute(
            "UPDATE periodos SET hora_fin = ?, duracion_minutos = ? WHERE id = ?",
            (a_texto(hf), round(duracion, 4), abierto["id"]),
        )
        conn.commit()
    return abierto["id"]


def cambiar_periodo(ad_id: str, presupuesto_nuevo: float,
                    hora: Optional[datetime] = None) -> int:
    """
    Lógica central de cambio de presupuesto:
      1. Cierra el período anterior (si existe) a la hora indicada.
      2. Abre un período nuevo con el presupuesto nuevo a la misma hora.
    Devuelve el id del período nuevo.
    """
    h = hora or ahora()
    cerrar_periodo(ad_id, h)
    return abrir_periodo(ad_id, presupuesto_nuevo, h)


def asegurar_periodo_inicial(ad_id: str, presupuesto: float) -> dict:
    """
    Garantiza que un anuncio tenga un período abierto. Si no lo tiene, abre uno.
    Útil al cargar anuncios desde Facebook por primera vez.
    """
    abierto = periodo_abierto(ad_id)
    if abierto:
        return abierto
    abrir_periodo(ad_id, presupuesto)
    return periodo_abierto(ad_id)


def obtener_periodos(ad_id: str) -> list:
    with _conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM periodos WHERE ad_id = ? ORDER BY hora_inicio ASC, id ASC",
                (str(ad_id),),
            )
        ]


def periodo_para_hora(ad_id: str, hora: datetime) -> Optional[dict]:
    """
    Encuentra el período de presupuesto activo para un anuncio en una hora dada.
    Un período cubre [hora_inicio, hora_fin]; si hora_fin es NULL cubre hasta hoy.
    Se elige el período cuyo hora_inicio sea el mayor <= hora.
    """
    h = a_texto(hora)
    with _conn() as conn:
        r = conn.execute(
            """
            SELECT * FROM periodos
            WHERE ad_id = ?
              AND hora_inicio <= ?
              AND (hora_fin IS NULL OR hora_fin >= ?)
            ORDER BY hora_inicio DESC, id DESC
            LIMIT 1
            """,
            (str(ad_id), h, h),
        ).fetchone()
        return dict(r) if r else None


# --------------------------------------------------------------------------- #
#  VENTAS
# --------------------------------------------------------------------------- #
def insertar_venta(ad_id: str, valor_venta: float, hora_venta: datetime,
                   periodo_id: Optional[int], hoja_origen: str,
                   ext_id: Optional[str] = None, producto: Optional[str] = None,
                   pais: Optional[str] = None) -> int:
    with _LOCK, _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO ventas
                (ad_id, valor_venta, hora_venta, periodo_id, hoja_origen, ext_id, producto, pais)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(ad_id), float(valor_venta), a_texto(hora_venta), periodo_id,
             hoja_origen, ext_id, producto, pais),
        )
        conn.commit()
        return cur.lastrowid


def ventas_por_pais(cutoff: Optional[datetime] = None,
                    hasta: Optional[datetime] = None) -> dict:
    """Ingresos y # de ventas por país (solo ventas que traen país)."""
    cond = ["pais IS NOT NULL", "pais <> ''"]
    args = []
    if cutoff is not None:
        cond.append("hora_venta >= ?"); args.append(a_texto(cutoff))
    if hasta is not None:
        cond.append("hora_venta < ?"); args.append(a_texto(hasta))
    where = " WHERE " + " AND ".join(cond)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT pais, COUNT(*) AS num, COALESCE(SUM(valor_venta),0) AS total
                FROM ventas{where} GROUP BY pais""", args)
        return {r["pais"]: {"num": r["num"], "ingreso_nat": float(r["total"])} for r in rows}


def venta_existe(hoja_origen: str, ext_id: str) -> bool:
    """True si ya se importó una venta con ese ext_id desde esa fuente (dedup)."""
    if not ext_id:
        return False
    with _conn() as conn:
        r = conn.execute(
            "SELECT 1 FROM ventas WHERE hoja_origen = ? AND ext_id = ? LIMIT 1",
            (hoja_origen, str(ext_id)),
        ).fetchone()
        return r is not None


# --------------------------------------------------------------------------- #
#  Configuración clave-valor (mapeos de Supabase/Excel, etc.)
# --------------------------------------------------------------------------- #
def get_config(clave: str, defecto: Optional[str] = None) -> Optional[str]:
    with _conn() as conn:
        r = conn.execute("SELECT valor FROM config_kv WHERE clave = ?", (clave,)).fetchone()
        return r["valor"] if r else defecto


def set_config(clave: str, valor: str) -> None:
    with _LOCK, _conn() as conn:
        conn.execute(
            """INSERT INTO config_kv (clave, valor) VALUES (?, ?)
               ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor""",
            (clave, valor),
        )
        conn.commit()


def resumen_ventas_periodo(periodo_id: int) -> dict:
    """Devuelve {num_ventas, ingreso_total} de un período."""
    with _conn() as conn:
        r = conn.execute(
            """
            SELECT COUNT(*) AS num, COALESCE(SUM(valor_venta), 0) AS total
            FROM ventas WHERE periodo_id = ?
            """,
            (periodo_id,),
        ).fetchone()
        return {"num_ventas": r["num"], "ingreso_total": r["total"]}


def ventas_agg_por_ad(cutoff: Optional[datetime] = None,
                      hasta: Optional[datetime] = None) -> dict:
    """
    Agrega ventas por anuncio: {ad_id: {"num_ventas": n, "ingreso_total": x}}.
    `cutoff` = fecha inicial (hora_venta >= cutoff); `hasta` = fecha final (< hasta).
    """
    cond, args = [], []
    if cutoff is not None:
        cond.append("hora_venta >= ?")
        args.append(a_texto(cutoff))
    if hasta is not None:
        cond.append("hora_venta < ?")
        args.append(a_texto(hasta))
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT ad_id, COUNT(*) AS num, COALESCE(SUM(valor_venta),0) AS total
                FROM ventas{where} GROUP BY ad_id""", args)
        return {r["ad_id"]: {"num_ventas": r["num"], "ingreso_total": float(r["total"])}
                for r in rows}


def valores_venta_distintos() -> dict:
    """Devuelve {'origenes': [...], 'productos': [...]} para filtros de producto."""
    with _conn() as conn:
        orig = [r["hoja_origen"] for r in conn.execute(
            "SELECT DISTINCT hoja_origen FROM ventas WHERE hoja_origen IS NOT NULL")]
        prod = [r["producto"] for r in conn.execute(
            "SELECT DISTINCT producto FROM ventas WHERE producto IS NOT NULL AND producto <> ''")]
    genericos = {"Manual", "GoogleSheets", "Supabase", "Seed"}
    origenes = sorted({o for o in orig if o and o not in genericos})
    productos = sorted({p for p in prod if p})
    return {"origenes": origenes, "productos": productos}


def ventas_suma(ad_ids: list, desde: Optional[datetime] = None) -> dict:
    """Suma de ventas (num, ingreso nativo) de varios anuncios desde una fecha."""
    if not ad_ids:
        return {"num": 0, "ingreso_nat": 0.0}
    ph = ",".join("?" for _ in ad_ids)
    args = [str(a) for a in ad_ids]
    cond = ""
    if desde is not None:
        cond = " AND hora_venta >= ?"
        args.append(a_texto(desde))
    with _conn() as conn:
        r = conn.execute(
            f"""SELECT COUNT(*) num, COALESCE(SUM(valor_venta),0) total
                FROM ventas WHERE ad_id IN ({ph}){cond}""", args).fetchone()
        return {"num": r["num"], "ingreso_nat": float(r["total"])}


def ventas_diarias(ad_ids: list, desde: datetime) -> dict:
    """{'YYYY-MM-DD': {'num': n, 'ingreso_nat': x}} por día, desde 'desde'."""
    if not ad_ids:
        return {}
    ph = ",".join("?" for _ in ad_ids)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT substr(hora_venta,1,10) AS dia, COUNT(*) num,
                       COALESCE(SUM(valor_venta),0) total
                FROM ventas WHERE ad_id IN ({ph}) AND hora_venta >= ?
                GROUP BY dia ORDER BY dia""",
            [*[str(a) for a in ad_ids], a_texto(desde)])
        return {r["dia"]: {"num": r["num"], "ingreso_nat": float(r["total"])} for r in rows}


def obtener_ventas(ad_id: Optional[str] = None) -> list:
    with _conn() as conn:
        if ad_id:
            q = "SELECT * FROM ventas WHERE ad_id = ? ORDER BY hora_venta DESC"
            rows = conn.execute(q, (str(ad_id),))
        else:
            rows = conn.execute("SELECT * FROM ventas ORDER BY hora_venta DESC")
        return [dict(r) for r in rows]
