FROM python:3.12-slim
RUN apt-get update && apt-get install -y libjpeg62-turbo-dev
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["streamlit", "run", "main.py"]
