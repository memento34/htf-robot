FROM python:3.12-slim
WORKDIR /app
COPY app.py engine.py market.py ./
COPY static ./static
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PAPER_DB_PATH=/data/paper.sqlite
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "app.py"]
