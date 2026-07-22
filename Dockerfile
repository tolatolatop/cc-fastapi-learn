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

COPY pyproject.toml README.md ./
COPY --chown=ccagent:ccagent src ./src
RUN python -m pip install --no-cache-dir .

COPY --chown=ccagent:ccagent config ./config
COPY docker-entrypoint.sh /usr/local/bin/cc-fastapi-entrypoint
COPY docker-admin-entrypoint.sh /usr/local/bin/cc-fastapi-admin

RUN chmod +x /usr/local/bin/cc-fastapi-entrypoint /usr/local/bin/cc-fastapi-admin \
    && mkdir -p /app/data \
    && chown -R ccagent:ccagent /app /home/ccagent

EXPOSE 8000

ENTRYPOINT ["cc-fastapi-entrypoint"]

# uvicorn cc_fastapi.main:app --host 0.0.0.0 --port 8000 --app-dir src
CMD ["uvicorn", "cc_fastapi.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src"]
