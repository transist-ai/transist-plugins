FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
WORKDIR /service
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY service.py .
ENV PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--log-level", "critical", "--lifespan", "off"]
