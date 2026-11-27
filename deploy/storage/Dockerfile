FROM suyan-storage-ubuntu:noble

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends samba \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 19555 suyan_drive \
    && useradd --uid 19555 --gid 19555 --no-create-home --shell /usr/sbin/nologin suyan_drive \
    && mkdir -p /run/samba /var/lib/samba/private

COPY smb.conf /etc/samba/suyan.conf
RUN testparm --suppress-prompt /etc/samba/suyan.conf >/dev/null
EXPOSE 445/tcp
CMD ["/usr/sbin/smbd", "--foreground", "--no-process-group", "--configfile=/etc/samba/suyan.conf"]
