FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .
RUN useradd --create-home bot && chown -R bot:bot /app
USER bot
CMD ["python", "-m", "stokozavr_bot"]
