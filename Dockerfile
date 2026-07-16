FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/ccagent

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ccagent \
    && useradd --uid 10001 --gid ccagent --create-home --shell /usr/sbin/nologin ccagent

RUN pip install --no-cache-dir \
    "fastapi>=0.116.0,<0.117.0" \
    "uvicorn>=0.35.0,<0.36.0" \
    "sqlalchemy>=2.0.43,<3.0.0" \
    "pydantic-settings>=2.10.1,<3.0.0" \
    "claude-agent-sdk>=0.1.44,<0.2.0" \
    "jinja2>=3.1.5,<4.0.0" \
    "pyyaml>=6.0.2,<7.0.0" \
    "pymysql>=1.1.1,<2.0.0"

COPY --chown=ccagent:ccagent src ./src
COPY --chown=ccagent:ccagent config ./config
COPY docker-entrypoint.sh /usr/local/bin/cc-fastapi-entrypoint

RUN chmod +x /usr/local/bin/cc-fastapi-entrypoint \
    && mkdir -p /app/data \
    && chown -R ccagent:ccagent /app /home/ccagent

EXPOSE 8000

ENTRYPOINT ["cc-fastapi-entrypoint"]

# uvicorn cc_fastapi.main:app --host 0.0.0.0 --port 8000 --app-dir src
CMD ["uvicorn", "cc_fastapi.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src"]
