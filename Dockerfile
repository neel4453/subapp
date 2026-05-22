FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Volume for persistent SQLite data
VOLUME ["/app/data"]

ENV DB_PATH=/app/data/substitution.db
ENV SECRET_KEY=change-this-in-production
ENV ALLOWED_ORIGINS=*
ENV PORT=8000

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
