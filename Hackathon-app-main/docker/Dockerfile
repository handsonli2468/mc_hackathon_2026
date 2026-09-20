# App Gateway + built web app in one image. Build from the repo root:
#   docker compose build

FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ros:humble-ros-base
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3-pip python3-numpy python3-opencv ros-humble-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY gateway/requirements.txt gateway/requirements.txt
RUN pip3 install --no-cache-dir -r gateway/requirements.txt
COPY gateway/app gateway/app
COPY docker/cyclonedds.xml /app/cyclonedds.xml
COPY --from=web /web/dist web/dist

ENV STATIC_DIR=/app/web/dist \
    DATA_DIR=/app/data \
    ROS_LOG_DIR=/tmp/ros_log \
    APP_PORT=8000
WORKDIR /app/gateway
CMD ["bash", "-c", "source /opt/ros/humble/setup.bash && exec uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]
