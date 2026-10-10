FROM python:3.11-slim

# fonts-dejavu-core: DejaVuSans-Bold for title cards (render.TITLE_FONT).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY render/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt boto3

COPY render/render.py render/ecs_task.py ./

ENTRYPOINT ["python", "ecs_task.py"]
