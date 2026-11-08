# Single-service red team (stdlib-only). Build after copying the package and
# seeds in: cp -r ../../redloop . && cp ../../seeds/seeds.json .
FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python3", "app.py"]
