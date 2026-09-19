FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Ensure Chromium is present (base image usually has it)
RUN playwright install chromium || true

COPY . .

# Create data dir for volume mount
RUN mkdir -p /app/data/screenshots

CMD ["python", "bot.py"]