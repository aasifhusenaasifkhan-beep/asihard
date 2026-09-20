FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Render only runs the controller bot (main.py). Encoding happens on GitHub Actions.
COPY main.py .

CMD ["python", "main.py"]
