FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY service.py metadata.py metadata_corrections.json restore_backup.py app.js library.js index.html style.css ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).read()"
CMD ["python", "-B", "-u", "/app/service.py"]
