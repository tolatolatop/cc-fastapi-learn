FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY review_console ./review_console
RUN python -m pip install --no-cache-dir .
RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin reviewer \
    && mkdir -p /app/data && chown -R reviewer:reviewer /app
USER reviewer
EXPOSE 8000
CMD ["uvicorn", "review_console.main:app", "--host", "0.0.0.0", "--port", "8000"]
