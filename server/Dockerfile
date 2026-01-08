FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application files directly to /app
COPY server/ ./

# Expose port
EXPOSE 8080

# Run with gunicorn (1 worker to share in-memory state)
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--timeout", "120", "server.main:app"]