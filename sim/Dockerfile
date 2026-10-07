# Partner simulators image (Corp A MCF API, Corp B order management). Build from the repo root:
#   docker build -f sim/Dockerfile -t partner-sims .
FROM python:3.11-slim-bookworm
WORKDIR /app
COPY sim/requirements.txt ./sim/requirements.txt
RUN pip install --no-cache-dir -r sim/requirements.txt
COPY sim ./sim
RUN useradd --uid 10001 --no-create-home sim
USER sim
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["python", "-m", "sim.corpa"]
