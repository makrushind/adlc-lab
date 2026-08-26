# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1


FROM python-base AS python-downloads

COPY requirements.lock /downloads/requirements.lock
RUN python -m pip download --index-url https://pypi.org/simple --require-hashes --only-binary=:all: --dest /wheelhouse -r /downloads/requirements.lock


FROM python-base AS runtime-python

COPY --from=python-downloads /wheelhouse /wheelhouse
COPY requirements.lock /opt/adlc/requirements.lock
RUN --network=none python -m pip install --no-index --find-links=/wheelhouse --only-binary=:all: --require-hashes -r /opt/adlc/requirements.lock
COPY src /opt/adlc/src
ENV PYTHONPATH=/opt/adlc/src
RUN --network=none install -d -o 10001 -g 10001 \
    /target/workspace \
    /target/corpus \
    /target/rag-index


FROM runtime-python AS repo-rag

COPY --chown=10001:10001 scenarios /opt/adlc/scenarios
USER 10001:10001
ENTRYPOINT ["python", "-m", "aiweekend_target"]
CMD ["repo-rag"]


FROM runtime-python AS hf-gateway

USER 10001:10001
ENTRYPOINT ["python", "-m", "aiweekend_target"]
CMD ["hf-gateway"]


FROM runtime-python AS agent-runtime

COPY --chown=10001:10001 scenarios /opt/adlc/scenarios
USER 10001:10001
ENTRYPOINT ["python", "-m", "aiweekend_target"]
CMD ["agent"]
