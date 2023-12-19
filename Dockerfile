FROM python:bullseye

ENV DEBUG=False
ENV SOURCE=/media/source
ENV MOUNTPOINT=/media/mountpoint

RUN apt update && apt upgrade -y \
    && apt install -y libfuse-dev git \
    && git clone https://github.com/jkaberg/torrent-fs.git /torrent-fs \
    && cd /torrent-fs \
    && pip install -r requirements.txt \
    && mkdir -p /media/{source,mountpoint}

WORKDIR /torrent-fs

CMD python /torrent-fs/torrentfs.py $MOUNTPOINT $SOURCE