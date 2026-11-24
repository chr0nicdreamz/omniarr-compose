FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/omniarr
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
RUN mkdir -p /app/config

EXPOSE 8585
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8585"]
