# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# No system build deps needed: psycopg[binary] ships prebuilt libpq, and
# pillow / imagehash / tgcrypto all have manylinux wheels for 3.11-slim.
# (Previously installed gcc/libpq-dev/postgresql-client — ~150-200 MB of
# dead weight.)

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run as a non-root user — the bot holds a Pyrogram user-session credential,
# so we don't want it running with root inside the container.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "run.py"]
