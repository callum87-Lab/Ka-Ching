# Base image pinned by digest (its exact fingerprint); Dependabot keeps it current.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /srv

COPY requirements/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app

RUN mkdir -p /data
ENV DB_PATH=/data/kaching.db
ENV SHIPPING_ESTIMATE=4.00
# No telemetry: FastAPI's OpenTelemetry hooks are switched off in the app,
# and this makes sure nothing in the container can turn them back on.
ENV OTEL_SDK_DISABLED=true

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
