"""Simple wrapper over libtorrent"""
from urllib.parse import quote
import tempfile
import os
import time
from datetime import datetime
import logging
import mimetypes
from collections import namedtuple
from functools import cached_property
from random import randint

import libtorrent as lt

mimetypes.init()

STATUSES = [
    'queued', 'checking', 'downloading_metadata', 'downloading', 'finished',
    'seeding', 'allocating', 'checking_fastresume'
]

TRACKERS = ("udp://tracker.openbittorrent.com:80/announce",
            "udp://tracker.publicbt.com:80/announce")

DHT = (("router.utorrent.com", 6881), ("router.bittorrent.com", 6881),
       ("dht.transmissionbt.com", 6881), ("router.bitcomet.com",
                                          6881), ("dht.aelitis.com", 6881))

EXTENSIONS = ('ut_pex', 'ut_metadata', 'smart_ban', 'metadata_transfoer')

PORTS = (randint(20000, 25000), randint(30000, 35000))


############################
#  ----- DISCLAMIER -----  #
############################
# This file is shamlessly copied from https://github.com/XayOn/torrentstream/blob/master/torrentstream/torrent.py
# Lot's of credits go to XayOn (https://github.com/XayOn) for the initial work. Thanks dude! *high five*
############################
#  ----- DISCLAMIER -----  #
############################

def get_indexed(func):
    """Return currently indedex torrent"""
    def inner(*args, **kwargs):
        """Executes a method, and returns result[class_instance.index]"""
        return list(func(*args, **kwargs)())[args[0].index]

    return inner


class TorrentSession:
    """Represent a torrent session. May handle multiple torrents"""
    def __init__(self, ports=PORTS, extensions=EXTENSIONS, dht_routers=DHT):
        self.session = lt.session()
        #self.session.set_severity_level(lt.alert.severity_levels.critical)
        self.session.listen_on(*ports)
        for extension in extensions:
            self.session.add_extension(extension)
        self.session.start_dht()
        self.session.start_lsd()
        self.session.start_upnp()
        self.session.start_natpmp()
        for router in dht_routers:
            self.session.add_dht_router(*router)
        self.torrents = []

    def __exit__(self):
        """ Remove all torrents on exit """
        logger.debug(f"Cleaning up torrents: {self.torrents}")
        for torrent in self.torrents:
            if torrent.temp_dir and torrent.remove_after:
                torrent.temp_dir.cleanup()

                self.session.remove_torrent(torrent.handle)
    
    def __call__(self):
        return self.__init__()

    def __repr__(self):
        return f"Torrentstream listening on {PORTS}"

    @property
    def alerts(self):
        if all(a.finished for a in self):
            raise StopIteration()
        for alert in self.session.pop_alerts():
            yield alert

    def remove_torrent(self, *args, **kwargs):
        """Remove torrent from session."""
        self.session.remove_torrent(*args, **kwargs)
        del self.torrents[args]

    def add_torrent(self, *args, **kwargs):
        """Add a torrent to this session

        For accepted parameters reference, see over `Torrent` definition.
        """
        torrent = self.find_torrent(*args, **kwargs)

        if torrent:
            logging.debug(f"Reusing: {torrent}")
        else:
            torrent = Torrent(session=self.session, *args, **kwargs)
            self.torrents.append(torrent)
            logging.debug(f"Starting: {torrent}")
        
        return torrent

    def find_torrent(self, *args, **kwargs):
        """ Finds an torrent givee an torrent file

        """
        info = lt.torrent_info(kwargs.get('torrent_path'))

        for torrent in self.torrents:
            if info.info_hash() == torrent.info.info_hash():
                return torrent
        return None

    def __iter__(self):
        """Iterating trough a session will give you all the currently-downloading torrents"""
        return iter(self.torrents)


class Torrent:
    """Wrapper over libtorrent"""
    def __init__(self,
                 torrent_path: str,
                 session: TorrentSession,
                 trackers: tuple = TRACKERS,
                 remove_after: bool = False,
                 **params):

        self.session = session
        self.temp_dir = None
        self.time_added = datetime.now()
        self.remove_after = remove_after
        self.info = lt.torrent_info(torrent_path)

        for tracker in trackers: # insert additional trackers
            self.info.add_tracker(tracker)

        self.params = {
            'ti': self.info,
            'save_path': None,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
            **params
        }

        self.handle = None

    def __enter__(self):
        if not self.params.get('save_path'):
            self.temp_dir = tempfile.TemporaryDirectory()
            self.params['save_path'] = self.temp_dir.name

        self.handle = self.session.add_torrent(self.params)
        return self

    def __exit__(self, *args, **kwargs):
        pass

    def __repr__(self):
        return self.info.name()

    def sequential(self, value: bool):
        """Set sequential download"""
        self.handle.set_sequential_download(value)

    @property
    def queue(self):
        """ Download queue """
        return self.handle.get_download_queue()

    @property
    def queue_status(self):
        """ Returns a represented queue status """
        state_char = [' ', '-', '=', '#']

        def repr_piece(piece):
            """ Represents a piece """
            return {
                piece['piece_index']:
                [state_char[block['state']] for block in piece['blocks']]
            }

        return [repr_piece(piece) for piece in self.queue]

    @property
    def name(self):
        """ Torrent name """
        if not self.handle.status.has_metadata():
            return "N/A"
        return self.torrent_info.name()

    @property
    def status(self):
        """
            Return a status dict.
        """
        if not hasattr(self, 'handle'):
            return
        status = self.handle.status()
        result = {
            'name': self.name,
            'download': status.download_rate,
            'total_download': status.total_download,
            'upload': status.upload_rate,
            'total_upload': status.total_upload
        }

        if not self.finished:
            result.update({
                'state': STATUSES[status.state],
                'total_downloaded': status.total_done,
                'peers': status.num_peers,
                'seeds': status.num_seeds,
                'progress': '%5.4f%%' % (status.progress * 100),
            })

        return result

    @property
    def finished(self):
        """Checks if torrent is finished."""
        return self.handle.is_finished()

    @property
    def started(self):
        """ Checks if handle has metadata"""
        return self.handle.has_metadata()

    @property
    def torrent_info(self):
        """Return handle.torrent_info"""
        return self.handle.get_torrent_info()

    @cached_property
    def files(self):
        """Returns a `TorrentFile` object for each file"""
        fnum = range(len(self.torrent_info.files()))
        return [TorrentFile(self, i) for i in fnum]

    def update_priorities(self):
        """Update file priorities with self.files."""
        self.handle.prioritize_files([a.priority for a in self.files])

    def download_only(self, file):
        """ Filter out priorities for every file except this one"""
        if file not in self.files:
            return None
        for file_ in self.files:
            file.priority = 7 if file == file_ else 0
        return file

    def wait_for(self, status):
        """Wait for a specific status

        Example:
            >>> # This will wait for a torrent to start, and return the torrent
            >>> torrent = await Torrent("magnet:...").wait_for('started')

            >>> # This will wait for a torrent to finish, and return the torrent
            >>> torrent = await Torrent("magnet:...").wait_for('finished')
        """
        while not getattr(self, status):
            time.sleep(1)

    def __iter__(self):
        """Iterating trough a Torrent instance will return each TorrentFile"""
        return iter(self.files)


class TorrentFile:
    """ Wrapper over libtorrent.file """
    def __init__(self, parent: Torrent, index: int):
        self.root = parent.params.get('save_path')
        self.index = index
        self.handle = parent.handle
        self.torrent = parent

    def __repr__(self):
        return str(self.path)

    def wait_for_completion(self, percent):
        while self.completed_percent < percent:
            time.sleep(5)

    @cached_property
    def path(self):
        """Return torrent path on filesystem"""
        return self.hfile.path

    @cached_property
    def file(self):
        """Return a file object with this file's path open in rb mode """
        return open(self.path, 'rb')

    def read(self, length, offset):
        # TODO: while this works okayish
        # we have issues with buffering
        # especially on larger files
        # so we need some form of readahead

        # TODO: can we find inspiration here?
        # https://github.com/animeshkundu/pyflix/blob/master/torrent/strategy.py
        offset += self.offset
        info = self.handle.get_torrent_info()
        piece_length = info.piece_length()

        logging.debug(f"{info.name()} - length: {length} | offset: {offset} | piece length: {piece_length}")

        needed_pieces = range(offset // piece_length, (offset + length) // piece_length + 1)
        completed_pieces = all(self.handle.have_piece(p) for p in needed_pieces)
        #prioritized_pieces = []

        if not completed_pieces:  # We don't have the needed pieces
            logging.debug(f"Asking for pieces: {needed_pieces}")

            # https://www.libtorrent.org/streaming.html
            deadline = 10000

            for p in range(needed_pieces[-1], min(needed_pieces[-1] + 4, info.num_pieces())):
                logging.debug(f"Setting deadline for piece: {p}")
                #self.handle.piece_priority(p, 7)
                self.handle.piece_priority(p, 7)
                self.handle.set_piece_deadline(p, deadline)
                deadline -= 1000

            completed_pieces = False

            logging.debug(f"Waiting to complete pieces: {needed_pieces}")
            while not completed_pieces:
                completed_pieces = all(self.handle.have_piece(p) for p in needed_pieces)
                time.sleep(0.1)
            
            self.handle.flush_cache()
        else:
            logging.debug(f"Pieces already downloaded: {needed_pieces}")
   
        with open(os.path.join(self.root, self.path), 'rb') as file:
            file.seek(offset - self.offset)
            return file.read(length)

    @property
    def filehash(self):
        """File hash"""
        return self.hfile.filehash

    @property
    def size(self):
        """File size"""
        return self.hfile.size

    @property
    def offset(self):
        """File offset"""
        return self.hfile.offset

    @property
    @get_indexed
    def hfile(self):
        """ Return file from libtorrent """
        return self.handle.get_torrent_info().files

    @property
    @get_indexed
    def priority(self):
        """ Readonly file priority from libtorrent """
        return self.handle.file_priorities

    @priority.setter
    def priority(self, value):
        self._priority = value
        self.torrent.update_priorities()

    @property
    @get_indexed
    def file_progress(self):
        """ Returns file progress """
        return self.handle.file_progress

    @property
    def completed_percent(self):
        """ Returns this file completed percentage """
        return (self.file_progress / self.size) * 100
