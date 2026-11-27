FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VAHTA_SERVER=1 VAHTA_DATA_DIR=/data
WORKDIR /app
COPY deploy/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home vahta
COPY vahta /app/vahta
USER 10001:10001
EXPOSE 8420
STOPSIGNAL SIGTERM
CMD ["python", "-m", "vahta.server"]
