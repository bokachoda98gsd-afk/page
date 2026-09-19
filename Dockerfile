# Use official Playwright image (includes Chromium & system deps)
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binaries (if not already in base image, ensures correct version)
RUN playwright install chromium

# Copy the rest of the application
COPY . .

# Ensure tasks.json and users.json exist and are writable
RUN touch tasks.json users.json && chmod 666 tasks.json users.json

# Run the bot
CMD ["python", "bot.py"]