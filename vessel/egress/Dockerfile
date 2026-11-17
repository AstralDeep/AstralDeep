# AstralVessel egress: model passthrough (holds the API key) on :8080 and the guarded web proxy on :3128.
# Runs unprivileged on a read-only filesystem; compose gives it the only route out.
FROM python:3.15-slim
LABEL org.opencontainers.image.source="https://github.com/AstralDeep/AstralVessel"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY __init__.py app.py proxy.py main.py egress/

RUN useradd --create-home --uid 1002 --shell /usr/sbin/nologin egress
USER egress

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080 3128
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"
CMD ["python", "-m", "egress.main"]
