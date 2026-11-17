FROM python:3.10-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8765
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home appuser
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser data/catalog.json data/affinity.json data/model_report.json ./data/
COPY --chown=appuser:appuser data/test_profiles.json ./data/
COPY --chown=appuser:appuser data/test_profiles ./data/test_profiles
COPY --chown=appuser:appuser static ./static
COPY --chown=appuser:appuser licenses ./licenses
RUN chown -R appuser:appuser /app
USER appuser
EXPOSE 8765
CMD ["python", "-m", "app.main"]
