# Base image pinned by digest (its exact fingerprint); Dependabot keeps it current.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app

RUN mkdir -p /data
ENV DB_PATH=/data/kaching.db
ENV SHIPPING_ESTIMATE=4.00

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
