import os
import sys
import errno
import logging
import warnings
import argparse
import tempfile
import time
import threading
import signal
import stat
from datetime import datetime
from random import randint

import mimetypes
import libtorrent as lt
from fuse import FUSE, FuseOSError, Operations

from functools import wraps
from concurrent.futures import ThreadPoolExecutor, TimeoutError


# Suppress deprecation warnings from libtorrent
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Set up logging
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

STATUSES = [
    'queued', 'checking', 'downloading_metadata', 'downloading', 'finished',
    'seeding', 'allocating', 'checking_fastresume'
]

TRACKERS = (
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.publicbt.com:80/announce"
)

DHT = (
    ("router.utorrent.com", 6881),
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.bitcomet.com", 6881),
    ("dht.aelitis.com", 6881)
)

EXTENSIONS = [
    lt.create_ut_metadata_plugin,
    lt.create_ut_pex_plugin,
    lt.create_smart_ban_plugin
]

PORTS = (randint(20000, 25000), randint(30000, 35000))

class AsyncExecutor:
    """Utility class to handle deferred function execution via thread pool."""
    def __init__(self, max_workers=4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def shutdown(self):
        self.executor.shutdown(wait=False)

    def defer_to_thread(self, timeout=2):
        """Decorator to run the function in a separate thread with a timeout."""
        def decorator(func):
            @wraps(func)
            def wrapper(instance, *args, **kwargs):
                future = self.executor.submit(func, instance, *args, **kwargs)
                try:
                    # Wait for the result up to the specified timeout
                    return future.result(timeout=timeout)
                except TimeoutError:
                    logger.warning(f"{func.__name__} for {args} timed out.")
                    raise FuseOSError(errno.ETIMEDOUT)  # Timeout error for FUSE
                except Exception as e:
                    logger.error(f"Error in deferred function {func.__name__}: {e}")
                    raise e  # Re-raise any other exceptions for FUSE to handle
            return wrapper
        return decorator

async_executor = AsyncExecutor()

class TorrentSession:
    def __init__(self, ports=PORTS, extensions=EXTENSIONS, dht_routers=DHT):
        self.session = lt.session()
        self.session.listen_on(*ports)
        for ext in extensions:
            self.session.add_extension(ext)
        self.session.start_dht()
        self.session.start_lsd()
        self.session.start_upnp()
        self.session.start_natpmp()
        for router in dht_routers:
            self.session.add_dht_router(*router)
        self.torrents = {}
        self._stop_event = threading.Event()
        self._progress_thread = threading.Thread(target=self._progress_monitor)
        self._progress_thread.daemon = True
        self._progress_thread.start()
        logger.debug("TorrentSession initialized.")

    def _progress_monitor(self):
        while not self._stop_event.is_set():
            for torrent in self.torrents.values():
                if torrent.initialized and torrent.handle.is_valid():
                    torrent.log_progress()
            time.sleep(5)  # Log progress every 5 seconds


    def add_torrent(self, torrent_path, remove_after=False, trackers=TRACKERS, **params):
        if torrent_path in self.torrents:
            torrent = self.torrents[torrent_path]
            #logger.debug(f"Reusing torrent: {torrent}")
        else:
            torrent = Torrent(
                session=self.session,
                torrent_path=torrent_path,
                remove_after=remove_after,
                trackers=trackers,
                **params
            )
            self.torrents[torrent_path] = torrent
            logger.debug(f"Starting new torrent: {torrent}")
        return torrent

    def find_torrent(self, torrent_path):
        return self.torrents.get(torrent_path)

    def remove_torrent(self, torrent):
        if torrent.handle:
            self.session.remove_torrent(torrent.handle)
            logger.debug(f"Removed torrent handle: {torrent}")
        torrent_path = torrent.torrent_path
        if torrent_path in self.torrents:
            del self.torrents[torrent_path]
            logger.debug(f"Removed torrent from session: {torrent}")

    def shutdown(self):
        logger.debug("Shutting down TorrentSession.")
        self._stop_event.set()
        self._progress_thread.join()
        for torrent in list(self.torrents.values()):
            torrent.close()
        self.session.pause()
        logger.debug("TorrentSession paused.")


class Torrent:
    def __init__(
        self,
        session: lt.session,
        torrent_path: str,
        trackers: tuple = TRACKERS,
        remove_after: bool = False,
        **params
    ):
        self.session = session
        self.torrent_path = torrent_path
        self.remove_after = remove_after
        self.info = lt.torrent_info(torrent_path)

        for tracker in trackers:
            self.info.add_tracker(tracker)

        self.params = {
            'ti': self.info,
            'save_path': None,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
            **params
        }

        self.handle = None
        self.files = []
        self.initialized = False
        self.download_rate = 0
        self.total_downloaded = 0
        self.tree = None
        self._last_log_time = 0

    def initialize(self):
        if not self.params.get('save_path'):
            self.temp_dir = tempfile.TemporaryDirectory()
            self.params['save_path'] = self.temp_dir.name

        self.handle = self.session.add_torrent(self.params)
        self.handle.set_sequential_download(True)
        logger.info(f"Started downloading torrent: {self.info.name()}")

        num_files = self.info.files().num_files()
        self.files = [TorrentFile(self, i) for i in range(num_files)]
        logger.debug(f"Initialized {num_files} files for torrent '{self.info.name()}'")
        self.initialized = True

    def __repr__(self):
        return self.info.name()

    @property
    def finished(self):
        return self.handle.is_finished() if self.handle else False

    @property
    def started(self):
        return self.handle.has_metadata() if self.handle else False

    def close(self):
        if self.handle:
            self.session.remove_torrent(self.handle)
            self.handle = None
        if hasattr(self, 'temp_dir') and self.remove_after:
            self.temp_dir.cleanup()
            logger.debug(f"Torrent cleaned up: {self.info.name()}")

    def log_progress(self):
        now = time.time()
        if now - self._last_log_time < 5:
            return  # Do not log if less than 5 seconds have passed
        self._last_log_time = now

        status = self.handle.status()
        progress = status.progress * 100
        download_rate = status.download_rate / 1000  # KB/s
        total_downloaded = status.total_done / (1024 * 1024)  # MB

        logger.info(f"Downloading '{self.info.name()}': {progress:.2f}% at {download_rate:.2f} KB/s, "
                    f"Downloaded: {total_downloaded:.2f} MB")

        file_progress = self.handle.file_progress()
        files = self.info.files()
        for index, fp in enumerate(file_progress):
            file_size = files.file_size(index)
            file_name = files.file_path(index)
            file_progress_percent = (fp / file_size) * 100 if file_size > 0 else 0
            logger.info(f"File '{file_name}': {file_progress_percent:.2f}% ({fp}/{file_size} bytes)")

    def build_tree(self):
        """Build an in-memory directory tree from the torrent's file list."""
        files = self.info.files()
        tree = {}
        for idx in range(files.num_files()):
            file_entry = files.file_path(idx)
            file_size = files.file_size(idx)
            parts = file_entry.strip('/').split('/')
            current = tree
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {'_type': 'dir', '_children': {}}
                current = current[part]['_children']
            # Add file entry
            current[parts[-1]] = {
                '_type': 'file',
                '_size': file_size,
                '_index': idx
            }
        self.tree = tree
        logger.debug(f"Built directory tree for torrent '{self.info.name()}': {self.tree}")



class TorrentFile:
    """Wrapper over a file in a torrent."""
    def __init__(self, torrent: Torrent, index: int):
        self.torrent = torrent
        self.index = index
        files = torrent.info.files()
        self.path = files.file_path(index)
        self.size = files.file_size(index)
        self.offset = files.file_offset(index)
        self.root = self.torrent.params['save_path']
        logger.debug(f"Initialized TorrentFile: index={index}, path='{self.path}', size={self.size}, offset={self.offset}")

    def __repr__(self):
        return self.path

    def read_data(self, length, offset):
        """Read data from the file."""
        if not self.torrent.initialized:
            self.torrent.initialize()

        piece_length = self.torrent.info.piece_length()
        file_offset = self.offset + offset
        start_piece = file_offset // piece_length
        end_piece = (file_offset + length - 1) // piece_length

        needed_pieces = range(start_piece, end_piece + 1)

        self._fetch_pieces(needed_pieces)

        while not all(self.torrent.handle.have_piece(piece) for piece in needed_pieces):
            self.torrent.log_progress()
            time.sleep(0.1)

        file_path = os.path.join(self.root, self.path)
        try:
            with open(file_path, 'rb') as f:
                f.seek(offset)
                data = f.read(length)
                self.torrent.log_progress()
                return data
        except FileNotFoundError:
            logger.error(f"File not found: {file_path}")
            raise FuseOSError(errno.ENOENT)

    def _fetch_pieces(self, pieces):
        for piece in pieces:
            self.torrent.handle.piece_priority(piece, 7)
            self.torrent.handle.set_piece_deadline(piece, 0)

        logger.debug(f"Requested pieces: {list(pieces)}")


class TorrentFS(Operations):
    def __init__(self, root):
        self.root = root
        self.torrent_session = TorrentSession()
        logger.info("TorrentFS initialized.")
        self._setup_signal_handlers()
        self.async_executor = async_executor

    def __del__(self):
        self.async_executor.shutdown()

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        return os.path.join(self.root, partial)

    def _find_torrent_file(self, path):
        current_path = path
        while current_path != '/':
            torrent_file = self._full_path(current_path + '.torrent')
            if os.path.isfile(torrent_file):
                return torrent_file, current_path
            current_path = os.path.dirname(current_path)
        return None, None

    def _get_torrent_info(self, torrent_path):
        return lt.torrent_info(torrent_path)

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down.")
        self.torrent_session.shutdown()
        sys.exit(0)

    def _calculate_directory_size(self, node):
        total_size = 0
        for child in node['_children'].values():
            if child['_type'] == 'file':
                total_size += child['_size']
            elif child['_type'] == 'dir':
                total_size += self._calculate_directory_size(child)
        return total_size

    def _get_node(self, torrent, path):
        if torrent.tree is None:
            torrent.build_tree()

        if path == '.' or path == '':
            return {'_type': 'dir', '_children': torrent.tree}

        parts = path.strip('/').split('/')
        current = {'_type': 'dir', '_children': torrent.tree}
        for part in parts:
            if current['_type'] == 'dir' and part in current['_children']:
                current = current['_children'][part]
            else:
                return None
        return current

    #@async_executor.defer_to_thread(timeout=4)
    def getattr(self, path, fh=None):
        full_path = self._full_path(path)
        logger.debug(f"getattr called on path: {path}")

        if os.path.exists(full_path):
            st = os.lstat(full_path)
            attrs = dict((key, getattr(st, key)) for key in (
                'st_atime', 'st_ctime', 'st_gid', 'st_mode', 'st_mtime',
                'st_nlink', 'st_size', 'st_uid', 'st_ino', 'st_dev'))
            logger.debug(f"getattr returning (existing path): {attrs}")
            return attrs
        else:
            torrent_file, torrent_path = self._find_torrent_file(path)
            if torrent_file:
                torrent = self.torrent_session.add_torrent(torrent_file)
                # Get the attributes of the .torrent file
                torrent_st = os.lstat(torrent_file)
                relative_path = os.path.relpath(path, torrent_path)
                node = self._get_node(torrent, relative_path)
                if node:
                    if node['_type'] == 'file':
                        st_mode = stat.S_IFREG | (torrent_st.st_mode & 0o644)
                        st_nlink = 1
                        st_size = node['_size']
                    elif node['_type'] == 'dir':
                        st_mode = stat.S_IFDIR | (torrent_st.st_mode & 0o755)
                        st_nlink = 2
                        # Calculate directory size
                        st_size = self._calculate_directory_size(node)
                    else:
                        raise FuseOSError(errno.ENOENT)

                    attrs = {
                        'st_atime': torrent_st.st_atime,
                        'st_ctime': torrent_st.st_ctime,
                        'st_gid': torrent_st.st_gid,
                        'st_mode': st_mode,
                        'st_mtime': torrent_st.st_mtime,
                        'st_nlink': st_nlink,
                        'st_size': st_size,
                        'st_uid': torrent_st.st_uid,
                        'st_ino': hash(path) & 0xffffffff,
                        'st_dev': 0
                    }
                    logger.debug(f"getattr returning (node in torrent): {attrs}")
                    return attrs
                else:
                    logger.debug("getattr raising ENOENT (path not in torrent)")
                    raise FuseOSError(errno.ENOENT)

            logger.debug("getattr raising ENOENT")
            raise FuseOSError(errno.ENOENT)

    #@async_executor.defer_to_thread(timeout=4)
    def readdir(self, path, fh):
        full_path = self._full_path(path)
        logger.debug(f"readdir called on path: {path}")
        dirents = ['.', '..']

        if os.path.isdir(full_path):
            entries = os.listdir(full_path)
            for entry in entries:
                if entry.endswith('.torrent'):
                    torrent_name = entry[:-8]
                    dirents.append(torrent_name)
                else:
                    dirents.append(entry)
        else:
            torrent_file, torrent_path = self._find_torrent_file(path)
            if torrent_file:
                torrent = self.torrent_session.add_torrent(torrent_file)
                relative_path = os.path.relpath(path, torrent_path)
                node = self._get_node(torrent, relative_path)
                if node and node['_type'] == 'dir':
                    dirents.extend(node['_children'].keys())
                else:
                    logger.debug("readdir raising ENOTDIR (not a directory)")
                    raise FuseOSError(errno.ENOTDIR)
        for r in dirents:
            yield r

    @async_executor.defer_to_thread(timeout=60)
    def open(self, path, flags):
        full_path = self._full_path(path)
        logger.debug(f"open called on path: {path}")
        if os.path.exists(full_path):
            return os.open(full_path, flags)
        else:
            torrent_file, torrent_path = self._find_torrent_file(path)
            if torrent_file:
                torrent = self.torrent_session.add_torrent(torrent_file)
                relative_path = os.path.relpath(path, torrent_path)
                node = self._get_node(torrent, relative_path)
                if node and node['_type'] == 'file':
                    return 0  # File handle is not used
                else:
                    raise FuseOSError(errno.ENOENT)
            raise FuseOSError(errno.ENOENT)

    @async_executor.defer_to_thread(timeout=60)
    def read(self, path, length, offset, fh):
        full_path = self._full_path(path)
        logger.debug(f"read called on path: {path}, length: {length}, offset: {offset}")
        if os.path.exists(full_path):
            os.lseek(fh, offset, os.SEEK_SET)
            return os.read(fh, length)
        else:
            torrent_file, torrent_path = self._find_torrent_file(path)
            if torrent_file:
                torrent = self.torrent_session.add_torrent(torrent_file)
                # Ensure the torrent is initialized before accessing its files
                if not torrent.initialized:
                    torrent.initialize()
                relative_path = os.path.relpath(path, torrent_path)
                node = self._get_node(torrent, relative_path)
                if node and node['_type'] == 'file':
                    file_index = node['_index']
                    logger.debug(f"File index: {file_index}, number of files in torrent: {len(torrent.files)}")
                    if file_index >= len(torrent.files) or file_index < 0:
                        logger.error(f"Invalid file index: {file_index}")
                        raise FuseOSError(errno.ENOENT)
                    torrent_file_obj = torrent.files[file_index]
                    try:
                        data = torrent_file_obj.read_data(length, offset)
                        return data
                    except FuseOSError as e:
                        if e.errno == errno.EAGAIN:
                            logger.debug("Data not available yet, returning empty data.")
                            time.sleep(0.1)  # Brief sleep before retrying
                            return b''
                        else:
                            raise
                else:
                    raise FuseOSError(errno.ENOENT)
            else:
                raise FuseOSError(errno.ENOENT)

def main(mountpoint, root):
    fuse = FUSE(TorrentFS(root), mountpoint, foreground=True, ro=True, allow_other=True, default_permissions=True)


if __name__ == '__main__':
    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("mountpoint", help="Path to target mountpoint")
    parser.add_argument("root", help="Path to root directory")
    parser.add_argument("-v", "--verbose", help="Set loglevel to debug", action="store_true")
    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    main(args.mountpoint, args.root)
