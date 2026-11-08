# Build context is this directory; deploy.sh copies the shared
# redloop package into ./vendor/redloop before building, so this
# image stays self-contained (no parent-directory build context needed).
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD exec gunicorn -b :$PORT -w 2 --timeout 60 app:app
