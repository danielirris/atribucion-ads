# Imagen para desplegar en EasyPanel (build desde GitHub)
FROM python:3.11-slim

# Evita prompts y buffers; salida de logs inmediata
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Instalar dependencias primero (mejor cacheo de capas)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copiar el resto del código
COPY . .

# Carpeta de datos persistente (montar un volumen de EasyPanel aquí)
ENV STORAGE_ROOT=/data
RUN mkdir -p /data

EXPOSE 8501

# Healthcheck de Streamlit
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8501\")}/_stcore/health')" || exit 1

# Honrar la variable PORT si EasyPanel la define; por defecto 8501
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true"]
