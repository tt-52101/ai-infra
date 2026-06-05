FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install \
      --index-url "${PIP_INDEX_URL}" \
      ${PIP_EXTRA_INDEX_URL:+--extra-index-url} ${PIP_EXTRA_INDEX_URL} \
      ${PIP_TRUSTED_HOST:+--trusted-host} ${PIP_TRUSTED_HOST} \
      --timeout "${PIP_DEFAULT_TIMEOUT}" \
      --retries "${PIP_RETRIES}" \
      --prefer-binary \
      --no-cache-dir \
      -r requirements.txt

COPY gateway.py .

EXPOSE 63012

CMD ["sh", "-c", "uvicorn gateway:app --host 0.0.0.0 --port ${GATEWAY_PORT:-63012}"]
