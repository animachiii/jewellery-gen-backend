FROM python:3.12-slim

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app

WORKDIR /srv

COPY pyproject.toml ./
# Dummy package so the editable install can resolve during the deps-only layer;
# the real source overwrites it in the next layer without invalidating this one.
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir -e .

COPY app ./app

RUN chown -R app:app /srv
USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
