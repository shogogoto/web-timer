FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir poetry==2.1.3
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && poetry install --only main --no-interaction
COPY app ./app
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

