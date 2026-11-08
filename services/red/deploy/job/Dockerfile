# Build context is this directory; deploy.sh copies the shared
# redloop package and seeds/ into this folder before building.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "job_main.py"]
