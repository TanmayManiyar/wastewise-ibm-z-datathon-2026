# WasteWise container image - builds on amd64, arm64 and s390x (IBM LinuxONE).
#   podman build -t wastewise -f Containerfile .
#   podman run -d -p 8501:8501 -p 8600:8600 -v wastewise-data:/app/data -v wastewise-secrets:/app/secrets wastewise
#
# On s390x there are no PyPI wheels for numpy / scipy / pandas / scikit-learn / pyarrow / LightGBM,
# so the image uses Ubuntu's s390x builds of the scientific stack, builds Apache Arrow (pyarrow) from
# the verified source release, and installs IBM Snap ML + zDNN for the Telum accelerator.
FROM docker.io/library/ubuntu:24.04

ARG ARROW_VERSION=24.0.0
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    WASTEWISE_DATA=/app/data WASTEWISE_SECRETS=/app/secrets WASTEWISE_MODELS=/app/models

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip python3-dev build-essential cmake ninja-build pkg-config curl ca-certificates \
        python3-numpy python3-scipy python3-pandas python3-sklearn python3-joblib python3-threadpoolctl python3-pil \
        libre2-dev libutf8proc-dev rapidjson-dev libssl-dev sqlite3 \
    && if [ "$(uname -m)" = "s390x" ]; then apt-get install -y --no-install-recommends libzdnn0 libzdnn-dev; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN python3 -m venv --system-site-packages /app/.venv
ENV PATH=/app/.venv/bin:$PATH
RUN python - > /app/constraints.txt <<'EOF'
import numpy, scipy, pandas, sklearn, PIL
for n, m in [("numpy", numpy), ("scipy", scipy), ("pandas", pandas), ("scikit-learn", sklearn), ("pillow", PIL)]:
    print(f"{n}=={m.__version__}")
EOF

COPY scripts/vm_build_pyarrow.sh /tmp/vm_build_pyarrow.sh
RUN if [ "$(uname -m)" = "s390x" ]; then \
        sed -i 's/^sudo //' /tmp/vm_build_pyarrow.sh && \
        APP_DIR=/app BUILD_DIR=/tmp/build ARROW_VERSION=$ARROW_VERSION bash /tmp/vm_build_pyarrow.sh && rm -rf /tmp/build; \
    else \
        pip install -c /app/constraints.txt "pyarrow==${ARROW_VERSION}"; \
    fi

RUN pip install -c /app/constraints.txt streamlit plotly pytest fpdf2 \
    && if [ "$(uname -m)" = "s390x" ]; then \
           pip install -c /app/constraints.txt snapml && \
           pip install -c /app/constraints.txt --no-binary xgboost,lightgbm xgboost lightgbm; \
       else \
           pip install -c /app/constraints.txt xgboost lightgbm; \
       fi

COPY wastewise /app/wastewise
COPY app /app/app
COPY scripts /app/scripts
COPY tests /app/tests
COPY .streamlit /app/.streamlit
COPY docs /app/docs
COPY requirements.txt README.md /app/

RUN useradd --create-home --uid 1001 wastewise && mkdir -p /app/data /app/secrets /app/models \
    && chown -R wastewise /app/data /app/secrets /app/models
USER wastewise
EXPOSE 8501 8600
VOLUME ["/app/data", "/app/secrets", "/app/models"]
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://127.0.0.1:8600/api/health || exit 1
ENTRYPOINT ["bash", "/app/scripts/container_start.sh"]
