# SatDump processing container for NOAA-20 HRD DigIF → imagery
# Used by ECS Fargate task to process raw I/Q baseband files
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install SatDump dependencies and SatDump itself
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    wget \
    ca-certificates \
    python3 \
    python3-pip \
    && add-apt-repository ppa:altillimity/satdump \
    && apt-get update \
    && apt-get install -y --no-install-recommends satdump \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for VITA-49 extraction and S3 interaction
RUN pip3 install --no-cache-dir boto3

# Copy processing scripts
COPY scripts/ /app/
WORKDIR /app

ENTRYPOINT ["python3", "/app/process.py"]
