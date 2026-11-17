# Sandbox image for agent code execution.
# Run with: docker run --rm --network none --memory 384m --cpus 0.5 \
#   -v <scratch>:/work agentic-sandbox:latest python /work/script.py
# Quick 042: the always-on service variant (no Docker socket needed, for Railway)
# is Dockerfile.service in this folder; both install requirements.txt.
FROM python:3.12-slim

# The packaged test suites are pruned in the same layer (about 140 MB), so the
# image stays the size it was before statsmodels and scikit-learn were added.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt \
 && find /usr/local/lib/python3.12/site-packages -depth -type d -name tests -exec rm -rf {} +

# A run gets half a CPU: one BLAS/OpenMP thread avoids oversubscription (and
# pids) from pools sized to the host's cores. Agg: there is no display.
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 MPLBACKEND=Agg

WORKDIR /work
# Non-root execution
RUN useradd -m runner
USER runner
# Build matplotlib's font cache once here, not on every fresh run.
RUN python -c "import matplotlib.pyplot"
