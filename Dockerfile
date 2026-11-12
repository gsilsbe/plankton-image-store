FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py manage_users.py ./

# Large zip uploads are staged here before being loaded into MongoDB
ENV UPLOAD_TMP=/uploads
RUN mkdir -p /uploads

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
