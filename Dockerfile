FROM python:alpine AS build

COPY . .
RUN pip install --no-cache-dir -r requirements.txt

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "prompt_protector:app"]
