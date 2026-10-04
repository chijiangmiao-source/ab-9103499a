FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/lib

WORKDIR /app
COPY lib/ /app/lib/
COPY static/ /app/static/
COPY tests/ /app/tests/
COPY verify/ /app/verify/
RUN chmod +x /app/verify/entrypoint.sh && python -m py_compile /app/lib/*.py

# Default command runs the control service; the registries and the verify
# service override the command.
CMD ["python", "/app/lib/controller_server.py"]
