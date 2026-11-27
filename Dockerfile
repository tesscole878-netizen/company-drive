ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home drive
COPY drive ./drive
COPY downloads ./downloads
COPY run.py .
COPY run_legacy_portal.py .
RUN mkdir /data /state && chown drive:drive /data /state
USER drive
ENV DRIVE_DATA_DIR=/data DRIVE_STATE_DIR=/state DRIVE_HOST=0.0.0.0 DRIVE_PORT=8765
EXPOSE 8765
CMD ["python", "run.py"]
