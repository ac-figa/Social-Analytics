FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY brand_keywords.json .

ENTRYPOINT ["python", "-m", "src.pipeline"]
