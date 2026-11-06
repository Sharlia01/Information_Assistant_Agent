FROM golang:1.20-alpine AS builder
RUN apk add --no-cache git
WORKDIR /src
ADD https://codeload.github.com/minio/minio/tar.gz/refs/tags/RELEASE.2023-03-20T20-16-18Z /tmp/minio.tar.gz
RUN tar -xzf /tmp/minio.tar.gz --strip-components=1 -C /src
RUN CGO_ENABLED=0 go build -trimpath -o /minio .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates curl
COPY --from=builder /minio /usr/local/bin/minio
ENTRYPOINT ["minio"]
CMD ["server", "/data"]
