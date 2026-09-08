FROM python:3.10-slim

WORKDIR /app

# libeccodes0/libeccodes-data are the runtime shared library + definition tables cfgrib's
# Python bindings dlopen at import time (confirmed: pip's eccodes package installs cleanly
# without them, but raises "Cannot find the ecCodes library" on import) — GFS GRIB2 decoding
# stayed unavailable in this image until they were added here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libeccodes0 libeccodes-data \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-api.txt requirements-full.txt ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements-full.txt

COPY . .

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
