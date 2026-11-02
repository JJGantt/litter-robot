FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY litter_watcher.py ./

# Logs go to stdout (fly logs). LOG_DIR can point at a mounted volume for
# persistent file logs; defaults to ephemeral ./logs otherwise.
ENV LOG_DIR=/app/logs
CMD ["python", "litter_watcher.py"]
