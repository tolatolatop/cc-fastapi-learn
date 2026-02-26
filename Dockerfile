FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi>=0.116.0,<0.117.0" \
    "uvicorn>=0.35.0,<0.36.0" \
    "sqlalchemy>=2.0.43,<3.0.0" \
    "pydantic-settings>=2.10.1,<3.0.0" \
    "claude-agent-sdk>=0.1.44,<0.2.0" \
    "pymysql>=1.1.1,<2.0.0"

COPY src ./src

RUN mkdir -p /app/data

EXPOSE 8000

# uvicorn cc_fastapi.main:app --host 0.0.0.0 --port 8000 --app-dir src
CMD ["uvicorn", "cc_fastapi.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src"]
