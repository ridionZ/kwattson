# kWattson — single container: web dashboard + background worker.
# Build:  docker build -t kwattson .
# 1. Build the dashboard from source (frontend/ → kwattson/web/static/).
FROM node:24-slim AS web
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci --ignore-scripts --no-audit --no-fund
COPY frontend ./frontend
RUN cd frontend && npm run build

# 2. Install the Python app with the freshly built dashboard.
FROM python:3.13-slim AS build
WORKDIR /src
COPY deploy/requirements.lock .
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY kwattson ./kwattson
COPY --from=web /src/kwattson/web/static ./kwattson/web/static
RUN pip install --no-cache-dir --no-deps --prefix=/install .

FROM python:3.13-slim
# Unraid convention is nobody:users (99:100); override with --build-arg.
ARG UID=99
ARG GID=100
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data HOST=0.0.0.0 PORT=8080
COPY --from=build /install /usr/local
RUN mkdir -p /data && chown ${UID}:${GID} /data && chmod 700 /data
USER ${UID}:${GID}
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["kwattson"]
CMD ["serve"]
