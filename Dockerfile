FROM python:3.10-slim

WORKDIR /app

# system deps for lightweight geospatial (no eccodes in slim — full via requirements-full.txt)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl && rm -rf /var/lib/apt/lists/*

COPY requirements-api.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements-api.txt

COPY . .

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
