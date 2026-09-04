FROM python:3.11-slim

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt gunicorn

COPY code/ /app/code/
COPY static/ /app/code/static/
COPY templates/ /app/code/templates/

EXPOSE 8080

WORKDIR /app/code

CMD ["python", "main.py", "--config", "../data/config.json"]