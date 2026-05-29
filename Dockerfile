# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=loop.settings \
    HF_HOME=/hf-cache

# Create the cache directory eagerly so it exists whether or not a volume
# is mounted over it. sentence-transformers / transformers / huggingface_hub
# all read HF_HOME, so a single dir covers every download path.
RUN mkdir -p /hf-cache

WORKDIR /app

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        gfortran \
        git \
        libopenblas-dev \
        liblapack-dev \
        libjpeg62-turbo-dev \
        zlib1g-dev \
        libpng-dev \
        libfreetype6-dev \
        pkg-config \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

RUN git clone --depth 1 https://github.com/AdvancedPhotonSource/GSAS-II.git /opt/GSAS-II \
    && cd /opt/GSAS-II \
    && pip install ".[useful]"

ENV GSAS2_PATH=/opt/GSAS-II

COPY . .
RUN sed -i 's/\r$//' docker/entrypoint.sh \
    && install -m 0755 docker/entrypoint.sh /usr/local/bin/loop-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/bin/sh", "/usr/local/bin/loop-entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
