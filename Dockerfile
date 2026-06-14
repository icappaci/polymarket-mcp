FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render / Glama / PaaS set $PORT; default 8000 for local
ENV PORT=8000
EXPOSE 8000

# server.py auto-enables HTTP transport when PORT env is present
CMD ["python", "server.py"]
