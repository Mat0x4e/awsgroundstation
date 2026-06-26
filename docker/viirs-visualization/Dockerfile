FROM python:3.12-slim

# System dependencies for Cartopy and GEOS
RUN apt-get update && apt-get install -y \
    libgeos-dev libproj-dev proj-data \
    libgdal-dev gdal-bin \
    && rm -rf /var/lib/apt/lists/*

# Python scientific stack
RUN pip install --no-cache-dir \
    cartopy==0.23.0 \
    matplotlib==3.9.0 \
    numpy==1.26.4 \
    Pillow==10.3.0 \
    cbor2==5.6.0 \
    sgp4==2.23 \
    h5py==3.11.0 \
    rasterio==1.3.10 \
    scipy==1.13.0 \
    boto3==1.34.0

# Natural Earth data for Cartopy (10m resolution)
RUN python -c "import cartopy; cartopy.io.shapereader.natural_earth(resolution='10m', category='physical', name='coastline')"
RUN python -c "import cartopy; cartopy.io.shapereader.natural_earth(resolution='10m', category='cultural', name='admin_0_boundary_lines_land')"

# Pipeline scripts
COPY scripts/ /opt/scripts/
RUN find /opt/scripts -name "*.py" -exec chmod +x {} \;

ENV MPLBACKEND=Agg
