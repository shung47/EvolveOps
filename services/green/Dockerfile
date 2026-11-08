# Self-contained build context -- no shared redloop package needed
# here (the Green Team doesn't reuse Red/Blue team code, see app.py).
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
# ADK reaches Gemini through Vertex AI, not the Gemini Developer API.
ENV GOOGLE_GENAI_USE_VERTEXAI=TRUE
CMD exec gunicorn -b :$PORT -w 2 --timeout 120 app:app
