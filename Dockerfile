FROM python:3.14-slim AS base
WORKDIR /srv/inventory-management-service
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY inventory_management_service inventory_management_service
COPY migrations migrations
COPY alembic.ini .
RUN useradd --system inventra

FROM base AS test
COPY requirements-dev.txt pytest.ini ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY tests tests
USER inventra
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
USER inventra
EXPOSE 8000
CMD ["uvicorn", "inventory_management_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
