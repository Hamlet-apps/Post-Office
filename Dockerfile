FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends gettext \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Translations and static files are built into the image; no Node.js is involved.
RUN python manage.py compilemessages --ignore=.venv 2>/dev/null || true \
    && SECRET_KEY=build python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn postoffice.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 60"]
