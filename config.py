"""
config.py
Carga la configuración y las credenciales desde el archivo .env
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Carpeta raíz del proyecto (donde vive este archivo)
BASE_DIR = Path(__file__).resolve().parent

# Cargar variables de entorno desde .env
load_dotenv(BASE_DIR / ".env")

# --- Credenciales de Facebook Marketing API ---
APP_ID = os.getenv("APP_ID", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
AD_ACCOUNT_ID = os.getenv("AD_ACCOUNT_ID", "").strip()

# Normalizar el AD_ACCOUNT_ID para que siempre tenga el prefijo "act_"
if AD_ACCOUNT_ID and not AD_ACCOUNT_ID.startswith("act_"):
    AD_ACCOUNT_ID = f"act_{AD_ACCOUNT_ID}"

# --- Rutas de archivos y persistencia ---
# STORAGE_ROOT es la carpeta donde viven los datos que DEBEN persistir entre
# redespliegues (base de datos SQLite y el Excel subido). En local es la carpeta
# del proyecto; en EasyPanel se apunta a un volumen montado, p. ej. /data.
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(BASE_DIR)))
try:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
except Exception:
    # Si no se puede crear (permisos), caemos a la carpeta del proyecto.
    STORAGE_ROOT = BASE_DIR

# Base de datos SQLite (persiste en STORAGE_ROOT)
DB_PATH = str(STORAGE_ROOT / os.getenv("DB_FILE", "datos.db"))

# Archivo Excel a monitorear (persiste en STORAGE_ROOT)
EXCEL_PATH = str(STORAGE_ROOT / os.getenv("EXCEL_FILE", "ventas.xlsx"))

# --- Parámetros de comportamiento ---
# Intervalo de polling a Facebook (segundos). Por defecto 5 minutos.
POLLING_INTERVAL_SEG = int(os.getenv("POLLING_INTERVAL_SEG", "300"))

# Usuario y contraseña de acceso a la app. Si la contraseña está vacía, la app
# queda abierta (no recomendado en un dominio público). El usuario por defecto
# es "admin" si no se define APP_USER.
APP_USER = os.getenv("APP_USER", "admin").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

# --- Supabase (2ª fuente de ventas) ---
# La URL del proyecto y la API key van como env vars (secretas). La tabla y el
# mapeo de columnas se configuran dentro de la app (Configuración → Supabase).
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()


def supabase_configurado() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)

# Minutos en un día (para el cálculo de gasto estimado)
MINUTOS_POR_DIA = 1440


def facebook_configurado() -> bool:
    """Devuelve True si todas las credenciales necesarias están presentes."""
    return all([APP_ID, APP_SECRET, ACCESS_TOKEN, AD_ACCOUNT_ID])


def resumen_config() -> dict:
    """Resumen seguro (sin exponer secretos completos) para mostrar en la UI."""
    def _mask(valor: str) -> str:
        if not valor:
            return "(vacío)"
        if len(valor) <= 6:
            return "*" * len(valor)
        return f"{valor[:4]}...{valor[-4:]}"

    return {
        "APP_ID": _mask(APP_ID),
        "APP_SECRET": _mask(APP_SECRET),
        "ACCESS_TOKEN": _mask(ACCESS_TOKEN),
        "AD_ACCOUNT_ID": AD_ACCOUNT_ID or "(vacío)",
        "STORAGE_ROOT": str(STORAGE_ROOT),
        "DB_PATH": DB_PATH,
        "EXCEL_PATH": EXCEL_PATH,
        "POLLING_INTERVAL_SEG": POLLING_INTERVAL_SEG,
    }
