FROM alpine:3.23
RUN apk add --no-cache postgresql18-client restic bash ca-certificates
COPY backup.sh /usr/local/bin/backup
RUN chmod 0555 /usr/local/bin/backup
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/backup"]
