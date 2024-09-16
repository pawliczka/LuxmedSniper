FROM python:3.11-slim
WORKDIR /usr/src/app
COPY . .
RUN pip install -r /usr/src/app/requirements.txt
ENTRYPOINT ["python3", "/usr/src/app/luxmedSnip.py"]
CMD ["-d", "300"]
