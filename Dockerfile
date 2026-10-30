FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-ocr.txt ./
ARG OCR=0
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$OCR" = "1" ]; then pip install --no-cache-dir -r requirements-ocr.txt; fi
COPY . .
ENV DATA_DIR=/data PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "--access-logfile", "-", "app:app"]
