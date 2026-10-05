FROM python:3.13-alpine

LABEL org.opencontainers.image.title="pangolin-gatus-sync" \
      org.opencontainers.image.description="Gatus sidecar that auto-discovers Pangolin resources and monitors their health checks" \
      org.opencontainers.image.source="https://github.com/reallovedone/pangolin-gatus-sync" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pangolin_gatus_sync.py /app/pangolin_gatus_sync.py

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD ["python", "/app/pangolin_gatus_sync.py", "--healthcheck"]

ENTRYPOINT ["python", "/app/pangolin_gatus_sync.py"]
