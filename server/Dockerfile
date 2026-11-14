FROM alpine:3.20
RUN apk add --no-cache python3 openssh-client docker-cli ca-certificates tzdata
ENV TZ=Asia/Krasnoyarsk
WORKDIR /app
COPY app.py index.html /app/
EXPOSE 8090
CMD ["python3", "-u", "/app/app.py"]
