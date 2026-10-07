# Agents image: one image, every agent; the Container App / Job command picks what runs.
FROM python:3.11-slim-bookworm

# Microsoft ODBC Driver 18 for the Azure SQL audit ledger (pyodbc).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
      > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tower ./tower
RUN pip install --no-cache-dir ".[azure]"

RUN useradd --uid 10001 --no-create-home tower
USER tower
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "tower.main"]
