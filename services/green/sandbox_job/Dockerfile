# A disposable sandbox: clones the target repo, applies one proposed
# diff, optionally runs its test command, records pass/fail to Firestore,
# then the whole container is thrown away. Deliberately separate from the
# main green_team service's image/credentials -- see sandbox_main.py.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "sandbox_main.py"]
