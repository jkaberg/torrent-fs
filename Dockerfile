FROM python:3.9-slim

ENV ALLOW_OTHER=true

RUN apt-get update && apt-get install -y --no-install-recommends \
    fuse \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY torrentfs.py .

ENTRYPOINT ["python", "/app/torrentfs.py", "/mnt/target", "/mnt/root"]
