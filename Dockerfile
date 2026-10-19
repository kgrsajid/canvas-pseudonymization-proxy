FROM python:3.13-slim

WORKDIR /srv/app

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir .

EXPOSE 8000

# Non-root
RUN useradd -m -u 10001 proxyuser && mkdir -p /srv/data && chown proxyuser /srv/data
USER proxyuser

ENV VAULT_SQLITE=/srv/data/vault.db

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
