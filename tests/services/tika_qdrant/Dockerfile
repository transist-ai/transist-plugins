FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS server
WORKDIR /fixture
COPY server.lock /fixture/server.lock
RUN python -m pip install --no-cache-dir --require-hashes -r server.lock && python -m pip check
COPY *.py /fixture/
COPY model.lock.json /fixture/model.lock.json
ENV HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 TRANSFORMERS_OFFLINE=1
CMD ["python", "gateway.py"]

FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS client
WORKDIR /fixture
COPY client.lock /fixture/client.lock
RUN python -m pip install --no-cache-dir --require-hashes -r client.lock && python -m pip check
COPY *.py /fixture/
CMD ["python", "probe.py"]
