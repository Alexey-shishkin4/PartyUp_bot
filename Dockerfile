FROM python:3.10-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8765 DATA_DIR=/app/storage
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home appuser
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser data/catalog.json data/affinity.json data/model_report.json ./data/
COPY --chown=appuser:appuser data/test_profiles.json ./data/
COPY --chown=appuser:appuser data/test_profiles ./data/test_profiles
COPY --chown=appuser:appuser static ./static
COPY --chown=appuser:appuser licenses ./licenses
RUN mkdir -p /app/storage && chown -R appuser:appuser /app
USER appuser
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8765') + '/health', timeout=3).close()"
CMD ["python", "-m", "app.main"]
