FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Make start script executable
RUN chmod +x start.sh

EXPOSE 8000

# Use start script instead of direct uvicorn
CMD ["./start.sh"]
