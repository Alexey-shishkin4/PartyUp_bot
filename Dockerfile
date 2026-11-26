FROM python:3.12-slim AS build
RUN python -m pip install --no-cache-dir --only-binary=:all: uv==0.11.3
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-python-downloads

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --create-home appuser
COPY --from=build /app/.venv /app/.venv
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY licenses ./licenses
USER appuser
CMD ["/app/.venv/bin/python", "-m", "app"]
