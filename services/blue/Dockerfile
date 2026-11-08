# Warden is stdlib-only: no requirements to install.
FROM python:3.12-slim
WORKDIR /app
COPY app.py firestore_rest.py gcp_logging.py llm_gemini.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python3", "app.py"]
