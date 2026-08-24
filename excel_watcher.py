"""
excel_watcher.py
Monitorea ventas.xlsx con watchdog (en un hilo aparte) y procesa filas nuevas.

- Lee TODAS las hojas del Excel.
- Columnas esperadas: ID_Anuncio, Valor_Venta, Hora_Venta.
- Si Hora_Venta viene vacía, se asigna el timestamp del momento de detección.
- Cada venta se atribuye al período de presupuesto activo del anuncio en esa hora.
- Se guarda en la tabla `ventas`.

Para no re-procesar filas ya vistas se lleva un conteo de filas procesadas por
(archivo, hoja). Solo se procesan las filas nuevas (append al final de la hoja).
"""
import os
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

import pandas as pd

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_DISPONIBLE = True
except Exception as e:  # pragma: no cover
    WATCHDOG_DISPONIBLE = False
    _WATCHDOG_ERROR = str(e)
    FileSystemEventHandler = object  # fallback

import config
import db

# Estado compartido con la UI.
_ESTADO_LOCK = threading.Lock()
ESTADO = {
    "activo": False,
    "ultima_lectura": None,
    "ultimo_error": None,
    "ventas_procesadas": 0,
    "mensajes": [],
}

# Cuántas filas ya procesamos por hoja: {nombre_hoja: n_filas}
_FILAS_VISTAS = {}
_PROC_LOCK = threading.Lock()

_OBSERVER = None
_WATCHER_THREAD = None
_STOP = threading.Event()

COLS = ["ID_Anuncio", "Valor_Venta", "Hora_Venta"]

# Alias aceptados para detectar columnas aunque se llamen distinto.
_ALIAS_ID = ["id_anuncio", "idanuncio", "id anuncio", "id del anuncio", "ad_id", "adid",
             "ad id", "post id", "post_id", "postid", "id_post", "id del post",
             "id", "anuncio", "id_ad", "id ad"]
_ALIAS_VALOR = ["valor_venta", "valor venta", "valor", "monto", "importe", "precio",
                "total", "venta", "amount", "value", "ingreso", "ingresos",
                "pago recibido", "pago_recibido", "recibido", "pago"]
_ALIAS_HORA = ["hora_venta", "hora venta", "hora", "fecha", "fecha_venta", "fecha venta",
               "fecha y hora", "timestamp", "date", "datetime", "fecha/hora"]
_ALIAS_PAIS = ["pais", "país", "country", "pais_venta", "país_venta"]
_ALIAS_PRODUCTO = ["producto", "product", "articulo", "artículo", "item", "sku",
                   "descripcion", "descripción", "concepto", "etiquetas", "etiqueta"]


def _norm(s):
    return str(s).strip().lower()


def limpiar_id(v):
    """
    Normaliza un ad_id: evita que un ID largo se convierta en notación científica
    o quede con '.0'. Ej: 1.2023682856562014e+17 -> 120236828565620140.
    """
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", ""):
        return ""
    # Notación científica o decimal -> entero exacto (con Decimal, sin perder dígitos).
    if "e" in s.lower() or "." in s:
        try:
            from decimal import Decimal
            return str(int(Decimal(s)))
        except Exception:
            pass
    return s


def detectar_columnas(columnas):
    """
    Detecta las columnas de ID de anuncio, valor, hora y país por nombre (flexible).
    Devuelve {"id","valor","hora","pais"} con el nombre real de la columna o None.
    """
    norm = {_norm(c): c for c in columnas}

    def buscar(aliases):
        # 1) match exacto por alias
        for a in aliases:
            if a in norm:
                return norm[a]
        # 2) match por "contiene" (ej. "ID del anuncio (FB)")
        for a in aliases:
            for k, real in norm.items():
                if a in k:
                    return real
        return None

    return {"id": buscar(_ALIAS_ID), "valor": buscar(_ALIAS_VALOR),
            "hora": buscar(_ALIAS_HORA), "pais": buscar(_ALIAS_PAIS),
            "producto": buscar(_ALIAS_PRODUCTO)}


def _log(msg: str) -> None:
    with _ESTADO_LOCK:
        ts = db.a_texto(db.ahora())
        ESTADO["mensajes"].insert(0, f"[{ts}] {msg}")
        del ESTADO["mensajes"][20:]


def _set_estado(**kwargs) -> None:
    with _ESTADO_LOCK:
        ESTADO.update(kwargs)


def obtener_estado() -> dict:
    with _ESTADO_LOCK:
        return dict(ESTADO)


# --------------------------------------------------------------------------- #
#  Parseo de una fila
# --------------------------------------------------------------------------- #
def _parse_hora(valor, deteccion: Optional[datetime] = None):
    """Interpreta Hora_Venta y devuelve un datetime, o `deteccion` si no se puede.

    IMPORTANTE: en importaciones masivas se llama con deteccion=None. Si la fecha
    viene vacía o ilegible devuelve None (la venta se marca 'sin fecha' y NO se
    cuenta como de hoy). Solo el registro manual/en vivo pasa deteccion=ahora."""
    if valor is None:
        return deteccion
    if isinstance(valor, datetime):
        return valor.replace(microsecond=0)
    try:
        if pd.isna(valor):
            return deteccion
    except Exception:
        pass
    txt = str(valor).strip()
    if txt == "" or txt.lower() in ("nan", "nat", "none"):
        return deteccion
    # Intentar parsear con pandas. Preferimos día/mes (formato de LatAm: 23/8/2026),
    # así fechas ambiguas como 5/8/2026 se leen 5-agosto y NO 8-mayo.
    try:
        ts = pd.to_datetime(txt, dayfirst=True, errors="coerce")
        if pd.isna(ts):
            ts = pd.to_datetime(txt, dayfirst=False, errors="coerce")
        if pd.isna(ts):
            return deteccion
        return ts.to_pydatetime().replace(microsecond=0)
    except Exception:
        return deteccion


def _parse_valor(valor) -> Optional[float]:
    """Extrae el número aunque venga con moneda o texto: '150.00 MXN', '$100',
    '1,234.56', '87 pesos' -> 150.0 / 100.0 / 1234.56 / 87.0."""
    import re
    try:
        if valor is None or pd.isna(valor):
            return None
    except Exception:
        pass
    txt = str(valor).strip()
    if txt == "" or txt.lower() in ("nan", "none", "nat"):
        return None
    txt = txt.replace(",", "")  # separador de miles
    m = re.search(r"-?\d+(?:\.\d+)?", txt)  # primer número (ignora 'MXN', '$', etc.)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _procesar_fila(ad_id: str, valor: float, hora: datetime, hoja: str,
                   pais: Optional[str] = None, ext_id: Optional[str] = None,
                   producto: Optional[str] = None) -> bool:
    """Atribuye y guarda una venta. Devuelve True si se insertó.
    Las ventas SIN ad_id también se guardan (ad_id="") para contarlas aparte."""
    ad_id = str(ad_id).strip()
    if ad_id.lower() in ("nan", "none"):
        ad_id = ""

    periodo = db.periodo_para_hora(ad_id, hora) if ad_id else None
    periodo_id = periodo["id"] if periodo else None

    db.insertar_venta(ad_id, valor, hora, periodo_id, hoja,
                      ext_id=ext_id, producto=producto, pais=pais)
    if periodo_id is None:
        _log(f"Venta de {ad_id} (${valor:.2f}) guardada SIN período "
             f"(no había período de presupuesto para esa hora).")
    else:
        _log(f"Venta de {ad_id} (${valor:.2f}) atribuida al período {periodo_id}.")
    return True


# --------------------------------------------------------------------------- #
#  Lectura del Excel
# --------------------------------------------------------------------------- #
def procesar_excel(path: Optional[str] = None, solo_nuevas: bool = True) -> int:
    """
    Lee todas las hojas del Excel y procesa las filas nuevas.
    Devuelve cuántas ventas nuevas se insertaron.

    Dedup ESTABLE por contenido: cada venta recibe un ext_id = huella del contenido
    (id|valor|hora|país) + un contador de ocurrencias por hoja. Así, re-subir el mismo
    archivo (aunque el servidor se haya reiniciado y perdido el conteo de filas en
    memoria) NO duplica ventas: las que ya están se saltan por su ext_id.
    El parámetro `solo_nuevas` se conserva por compatibilidad pero ya no afecta el
    dedup (siempre es seguro reprocesar).
    """
    import hashlib
    path = path or config.EXCEL_PATH
    if not os.path.exists(path):
        _set_estado(ultimo_error=f"No existe el archivo {path}")
        return 0

    deteccion = db.ahora()
    total = 0
    sin_fecha = 0
    try:
        # dtype=str: lee todo como texto para no perder precisión en IDs largos.
        hojas = pd.read_excel(path, sheet_name=None, engine="openpyxl", dtype=str)
    except Exception as e:
        _set_estado(ultimo_error=f"No se pudo leer el Excel: {e}")
        _log(f"Error leyendo Excel: {e}")
        return 0

    with _PROC_LOCK:
        for nombre_hoja, dff in hojas.items():
            if dff is None or dff.empty:
                continue

            # Normalizar nombres y detectar columnas de forma flexible.
            dff = dff.rename(columns=lambda c: str(c).strip())
            cmap = detectar_columnas(list(dff.columns))
            if not cmap["id"] or not cmap["valor"]:
                _log(f"Hoja '{nombre_hoja}' ignorada: no encontré columna de ID de anuncio "
                     f"y/o de valor. Columnas: {list(dff.columns)}")
                continue

            existentes = db.ext_ids_de_fuente(nombre_hoja)  # dedup estable (persistente)
            nuevos = set()
            firmas = {}  # contador de ocurrencias por huella (esta hoja)
            cols_firma = [c for c in (cmap.get("id"), cmap.get("valor"),
                                      cmap.get("hora"), cmap.get("pais")) if c]
            for _, fila in dff.iterrows():
                # Huella de contenido (crudo, no depende de la posición de la fila).
                partes = [str(fila.get(c)).strip() for c in cols_firma]
                firma = hashlib.md5("|".join(partes).encode("utf-8")).hexdigest()[:16]
                n = firmas.get(firma, 0)
                firmas[firma] = n + 1
                ext_id = f"{nombre_hoja}:h:{firma}:{n}"
                if ext_id in existentes or ext_id in nuevos:
                    continue  # ya importada: no duplicar

                valor = _parse_valor(fila.get(cmap["valor"]))
                if valor is None:
                    continue
                # Fecha ESTRICTA (deteccion=None): si no se puede leer, se salta la
                # venta (no se marca como "hoy"). Evita inflar el conteo del día.
                hora = _parse_hora(fila.get(cmap["hora"]) if cmap["hora"] else None)
                if hora is None:
                    sin_fecha += 1
                    continue
                pais = fila.get(cmap["pais"]) if cmap["pais"] else None
                pais = str(pais).strip() if pais is not None and str(pais).strip().lower() not in ("nan", "none", "") else None
                producto = fila.get(cmap["producto"]) if cmap.get("producto") else None
                producto = str(producto).strip() if producto is not None and str(producto).strip().lower() not in ("nan", "none", "") else None
                ok = _procesar_fila(limpiar_id(fila.get(cmap["id"])), valor, hora,
                                    nombre_hoja, pais=pais, ext_id=ext_id, producto=producto)
                if ok:
                    nuevos.add(ext_id)
                    total += 1

    if total:
        with _ESTADO_LOCK:
            ESTADO["ventas_procesadas"] += total
    _set_estado(ultima_lectura=deteccion, ultimo_error=None)
    if total or sin_fecha:
        _log(f"{total} venta(s) nueva(s) procesada(s)."
             + (f" {sin_fecha} sin fecha legible (no se importaron)." if sin_fecha else ""))
    return total


# --------------------------------------------------------------------------- #
#  Handler de watchdog
# --------------------------------------------------------------------------- #
class _ExcelHandler(FileSystemEventHandler):
    def __init__(self, path: str):
        super().__init__()
        self._path = os.path.abspath(path)
        self._ultimo = 0.0

    def on_any_event(self, event):
        try:
            if getattr(event, "is_directory", False):
                return
            ruta = os.path.abspath(getattr(event, "src_path", "") or "")
            dest = os.path.abspath(getattr(event, "dest_path", "") or "")
            # Excel suele guardar con archivos temporales; reaccionamos si toca nuestro archivo.
            objetivo = os.path.basename(self._path)
            if objetivo not in (os.path.basename(ruta), os.path.basename(dest)):
                return
            # Debounce: Excel dispara varios eventos por guardado.
            ahora = time.time()
            if ahora - self._ultimo < 1.0:
                return
            self._ultimo = ahora
            time.sleep(0.5)  # esperar a que Excel termine de escribir
            procesar_excel(self._path, solo_nuevas=True)
        except Exception as e:
            _set_estado(ultimo_error=str(e))
            _log(f"Error procesando evento: {e}")


# --------------------------------------------------------------------------- #
#  Arranque / parada del watcher
# --------------------------------------------------------------------------- #
def iniciar_watcher() -> None:
    """Arranca el observer de watchdog una sola vez por proceso."""
    global _OBSERVER, _WATCHER_THREAD

    if not WATCHDOG_DISPONIBLE:
        _set_estado(activo=False, ultimo_error=f"watchdog no disponible: {_WATCHDOG_ERROR}")
        return

    if _OBSERVER is not None:
        return  # ya iniciado

    carpeta = os.path.dirname(os.path.abspath(config.EXCEL_PATH)) or "."
    os.makedirs(carpeta, exist_ok=True)

    # Sincronizar el conteo inicial de filas SIN procesar históricas de golpe:
    # marcamos como "vistas" las filas ya existentes para no duplicar.
    _sincronizar_filas_existentes()

    handler = _ExcelHandler(config.EXCEL_PATH)
    _OBSERVER = Observer()
    _OBSERVER.schedule(handler, carpeta, recursive=False)
    _OBSERVER.daemon = True
    _OBSERVER.start()
    _set_estado(activo=True, ultimo_error=None)
    _log(f"Watchdog iniciado sobre {config.EXCEL_PATH}")

    # Hilo auxiliar: un barrido periódico por si algún evento se pierde.
    def _barrido():
        while not _STOP.wait(30):
            try:
                procesar_excel(config.EXCEL_PATH, solo_nuevas=True)
            except Exception:
                pass

    _WATCHER_THREAD = threading.Thread(target=_barrido, name="excel-barrido", daemon=True)
    _WATCHER_THREAD.start()


def _sincronizar_filas_existentes() -> None:
    """
    Al iniciar, cuenta las filas existentes y las marca como vistas SIN insertarlas,
    para no duplicar ventas históricas. Si prefieres importar lo existente,
    llama procesar_excel(solo_nuevas=False) manualmente desde la UI.
    """
    path = config.EXCEL_PATH
    if not os.path.exists(path):
        return
    try:
        hojas = pd.read_excel(path, sheet_name=None, engine="openpyxl", dtype=str)
        with _PROC_LOCK:
            for nombre_hoja, dff in hojas.items():
                clave = f"{os.path.abspath(path)}::{nombre_hoja}"
                _FILAS_VISTAS[clave] = 0 if dff is None else len(dff)
        _log("Filas existentes sincronizadas (no se re-importan las históricas).")
    except Exception as e:
        _log(f"No se pudo sincronizar filas existentes: {e}")


def detener_watcher() -> None:
    global _OBSERVER
    _STOP.set()
    if _OBSERVER is not None:
        try:
            _OBSERVER.stop()
            _OBSERVER.join(timeout=3)
        except Exception:
            pass
        _OBSERVER = None
    _set_estado(activo=False)


def importar_todo() -> int:
    """Reprocesa TODAS las filas del Excel (ignora el conteo de vistas)."""
    with _PROC_LOCK:
        _FILAS_VISTAS.clear()
    return procesar_excel(config.EXCEL_PATH, solo_nuevas=False)


# --------------------------------------------------------------------------- #
#  Modo online: subir Excel y registrar venta manual
# --------------------------------------------------------------------------- #
def guardar_excel_subido(contenido: bytes, reemplazar: bool = True) -> int:
    """
    Guarda un Excel subido desde la UI en EXCEL_PATH y procesa sus filas.

    - reemplazar=True: sobrescribe el archivo y procesa SOLO las filas nuevas
      respecto a lo ya visto (ideal cuando subes el mismo archivo con filas
      agregadas al final).
    Devuelve cuántas ventas nuevas se insertaron.
    """
    try:
        with open(config.EXCEL_PATH, "wb") as f:
            f.write(contenido)
    except Exception as e:
        _set_estado(ultimo_error=f"No se pudo guardar el Excel subido: {e}")
        _log(f"Error guardando Excel subido: {e}")
        return 0
    _log("Excel subido guardado. Procesando filas nuevas...")
    return procesar_excel(config.EXCEL_PATH, solo_nuevas=True)


def registrar_venta_manual(ad_id: str, valor: float,
                           hora: Optional[datetime] = None,
                           hoja_origen: str = "Manual") -> dict:
    """
    Registra una venta escrita a mano en la UI (útil en despliegue online,
    donde no siempre hay un Excel local que vigilar). Reutiliza la misma
    atribución por período. Devuelve {"ok": bool, "periodo_id": int|None, "error": str|None}.
    """
    ad_id = str(ad_id).strip()
    if not ad_id:
        return {"ok": False, "periodo_id": None, "error": "ID de anuncio vacío."}
    try:
        valor = float(valor)
    except Exception:
        return {"ok": False, "periodo_id": None, "error": "Valor de venta inválido."}

    h = hora or db.ahora()
    periodo = db.periodo_para_hora(ad_id, h)
    periodo_id = periodo["id"] if periodo else None
    db.insertar_venta(ad_id, valor, h, periodo_id, hoja_origen)
    with _ESTADO_LOCK:
        ESTADO["ventas_procesadas"] += 1
    _log(f"Venta manual de {ad_id} (${valor:.2f}) registrada "
         f"({'período ' + str(periodo_id) if periodo_id else 'sin período'}).")
    return {"ok": True, "periodo_id": periodo_id, "error": None}
