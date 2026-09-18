# Connected build stage fetches hash-locked wheels for the target Linux platform.
# Runtime installation and the preflight process need no network access.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
FROM ${PYTHON_IMAGE} AS wheels
COPY requirements-runtime.txt /build/requirements-runtime.txt
RUN python -m pip download --only-binary=:all: --require-hashes \
    --dest /wheelhouse -r /build/requirements-runtime.txt
COPY dist/*.whl /wheelhouse/

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_INDEX=1
COPY --from=wheels /wheelhouse /wheelhouse
COPY requirements-runtime.txt /build/requirements-runtime.txt
RUN --network=none python -m pip install --no-cache-dir --no-index \
    --find-links=/wheelhouse --require-hashes -r /build/requirements-runtime.txt \
    && python -m pip install --no-cache-dir --no-index --no-deps /wheelhouse/containment_eval_harness-*.whl \
    && rm -rf /wheelhouse /build
COPY assets/ /opt/containment/assets/
USER 10001:10001
WORKDIR /opt/containment
ENTRYPOINT ["containment"]
CMD ["--help"]
