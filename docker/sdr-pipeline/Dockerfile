# NOAA-20 DigIF to SDR Pipeline — Multi-tool Docker Image
# Contains: Python IQ extractor, SatDump v1.2.2, RT-STPS 7.0+P1, CSPP SDR 4.1.1
# Target: AWS CodeBuild BUILD_GENERAL1_LARGE (8 vCPU, 16 GB RAM)

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# -------------------------------------------------------------------
# System dependencies
# -------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    openjdk-17-jre-headless \
    ocl-icd-libopencl1 \
    wget \
    curl \
    unzip \
    libtiff5 \
    libfftw3-3 \
    libnng1 \
    libjemalloc2 \
    libhdf5-103-1 \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------------------
# AWS CLI v2
# -------------------------------------------------------------------
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp/ && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws

# -------------------------------------------------------------------
# Python dependencies
# -------------------------------------------------------------------
RUN pip3 install --no-cache-dir \
    sgp4 \
    pyorbital \
    boto3 \
    numpy \
    requests

# -------------------------------------------------------------------
# SatDump v1.2.2 — QPSK demod + Viterbi + Reed-Solomon + frame sync
# -------------------------------------------------------------------
COPY satdump_1.2.2_ubuntu_22.04_amd64.deb /tmp/
RUN apt-get update && \
    dpkg -i /tmp/satdump_1.2.2_ubuntu_22.04_amd64.deb || apt-get install -f -y && \
    rm /tmp/satdump_1.2.2_ubuntu_22.04_amd64.deb && \
    rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------------------
# RT-STPS 7.0 + Patch 1 (NASA/CIMSS) — CADU → RDR (HDF5 Level 0)
# Full NOAA-20/JPSS-1/JPSS-2 support
# Patch jpss1.xml: set PnEncoded=false (SatDump outputs PN-decoded frames)
# and fix link chain to skip pn node (frame_sync → reed_solomon directly)
# -------------------------------------------------------------------
COPY RT-STPS_7.0.tar.gz /opt/
COPY RT-STPS_7.0_PATCH_1.tar.gz /opt/
RUN tar xzf /opt/RT-STPS_7.0.tar.gz -C /opt/ && \
    cd /opt/rt-stps && \
    tar xzf /opt/RT-STPS_7.0_PATCH_1.tar.gz -C /opt/rt-stps/ && \
    chmod +x /opt/rt-stps/bin/* 2>/dev/null || true && \
    chmod +x /opt/rt-stps/jsw/bin/*.sh 2>/dev/null || true && \
    sed -i 's/PnEncoded="true"/PnEncoded="false"/' /opt/rt-stps/config/jpss1.xml && \
    sed -i 's|from="frame_sync" to="pn"|from="frame_sync" to="reed_solomon"|' /opt/rt-stps/config/jpss1.xml && \
    sed -i '/from="pn" to="reed_solomon"/d' /opt/rt-stps/config/jpss1.xml && \
    rm /opt/RT-STPS_7.0.tar.gz /opt/RT-STPS_7.0_PATCH_1.tar.gz

# -------------------------------------------------------------------
# CSPP SDR 4.1 + 4.1.1 patch + J01 data (CIMSS/NOAA) — RDR → SDR + GEO
# -------------------------------------------------------------------
COPY CSPP_SDR_V4.1.tar.gz /opt/
COPY CSPP_SDR_V4.1.1_patch.tar.gz /opt/
COPY CSPP_SDR_V4.1_straylight_luts_j01.tar.gz /opt/
COPY CSPP_SDR_V4.1_static_tiles.tar.gz /opt/
RUN tar xzf /opt/CSPP_SDR_V4.1.tar.gz -C /opt/ && \
    tar xzf /opt/CSPP_SDR_V4.1.1_patch.tar.gz -C /opt/ && \
    tar xzf /opt/CSPP_SDR_V4.1_straylight_luts_j01.tar.gz -C /opt/ && \
    tar xzf /opt/CSPP_SDR_V4.1_static_tiles.tar.gz -C /opt/ && \
    chmod +x /opt/SDR_4_1/bin/*.sh 2>/dev/null || true && \
    rm /opt/CSPP_SDR_V4.1.tar.gz /opt/CSPP_SDR_V4.1.1_patch.tar.gz \
       /opt/CSPP_SDR_V4.1_straylight_luts_j01.tar.gz \
       /opt/CSPP_SDR_V4.1_static_tiles.tar.gz

# Download and install CSPP LUTs — creates SDR_4_1_DB database required at runtime
ENV CSPP_SDR_HOME="/opt/SDR_4_1"
RUN /opt/SDR_4_1/bin/sdr_luts.sh --spacecraft j01 || \
    echo "WARNING: sdr_luts.sh failed — SDR_4_1_DB may not be created"

# -------------------------------------------------------------------
# Pipeline scripts
# -------------------------------------------------------------------
COPY scripts/ /opt/scripts/
RUN chmod +x /opt/scripts/*.py /opt/scripts/*.sh 2>/dev/null || true

# -------------------------------------------------------------------
# Environment variables
# -------------------------------------------------------------------
ENV PATH="/opt/rt-stps/bin:/opt/rt-stps/jsw/bin:/opt/SDR_4_1/bin:/opt/scripts:${PATH}"
ENV CSPP_SDR_HOME="/opt/SDR_4_1"
ENV CSPP_HOME="/opt/SDR_4_1"
ENV RTSTPS_HOME="/opt/rt-stps"

WORKDIR /workspace
