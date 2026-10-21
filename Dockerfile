# Base image pinned by digest (its exact fingerprint); Dependabot keeps it current.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app

RUN mkdir -p /data
ENV DB_PATH=/data/kaching.db
ENV SHIPPING_ESTIMATE=4.00

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
