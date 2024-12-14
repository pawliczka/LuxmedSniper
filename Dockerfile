FROM python:3.11-slim
WORKDIR /usr/src/app
COPY . .
RUN pip install -r /usr/src/app/requirements.txt
ENTRYPOINT ["python3", "/usr/src/app/luxmed_sniper.py"]
CMD ["-d", "300"]
