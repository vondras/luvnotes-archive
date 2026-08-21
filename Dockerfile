FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libimage-exiftool-perl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY luvnotes_archive.py .
COPY entrypoint.py .
COPY runner.py .

ENTRYPOINT ["python", "/app/runner.py"]
