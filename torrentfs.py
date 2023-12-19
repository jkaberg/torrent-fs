import os
import sys
import errno
import logging
import warnings
import argparse

import libtorrent as lt
from torrentstream import TorrentSession
from fuse import FUSE, FuseOSError, Operations

# we're getting deprectation warnings from libtorrent.torrent_info()
warnings.filterwarnings("ignore", category=DeprecationWarning)


class TorrentFS(Operations):
    def __init__(self, root):#, torrent_session):
        self.root = root
        self.torrent_session = TorrentSession()
        logger.debug(f"Started torrent session: {self.torrent_session}")

    # Helpers
    # =======

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.join(self.root, partial)
        return path

    def _open_torrent(self, torrent_path):
        with self.torrent_session.add_torrent(torrent_path=torrent_path, remove_after=True) as torrent:
            torrent.sequential(True)

            while torrent.wait_for('started'):
                logger.debug(f"{torrent.info.name()}: {torrent.info.state()}")
                time.sleep(1)

            return torrent

    def _torrent_path(self, full_path):
        """
        Iterate over the full path and try to find the torrent file.

        """
        paths = full_path.split('/')
        tmp_path = ''

        for p in paths:
            tmp_path = os.path.join(tmp_path, p)
            torrent_file = f"{tmp_path}.torrent"
            
            if os.path.isfile(torrent_file):
                return torrent_file

        return None

    def _get_paths(self, full_path):
        sub_path = None
        fake_base = None
        torrent_path = self._torrent_path(full_path)

        if torrent_path:
            fake_base = torrent_path.replace('.torrent', '')
            sub_path = full_path.replace(fake_base, '')

        return torrent_path, fake_base, sub_path

    def _find_fpath(self, path, torrent_name):
        dirpath, filename = os.path.split(path)

        if dirpath.startswith(torrent_name):
            dirpath = dirpath.replace(torrent_name, '')

        if not dirpath.startswith('/'):
            dirpath = f"/{dirpath}"

        return dirpath, filename


    def _file_in_torrent(self, sub_path, torrent_path):
        info = lt.torrent_info(torrent_path)
        paths = []

        for f in info.files():
            dirpath, filename = self._find_fpath(f.path, info.name())
            paths.append(os.path.join(dirpath, filename))

        return sub_path in paths
        
    # Filesystem methods
    # ==================

    getxattr = None

    def getattr(self, path, fh=None):
        full_path = self._full_path(path)
        torrent_path, fake_base, sub_path = self._get_paths(full_path)

        DIR_MASK = 0o044555

        if torrent_path:
            st = os.lstat(torrent_path) # use the torrent file for permissions etc.
            rewrite_st = list(st)

            if torrent_path and full_path.startswith(f"{fake_base}/"):
                info = lt.torrent_info(torrent_path)
                torrent_name = info.name()
                torrent_files = info.files()

                for f in torrent_files:
                    dirpath, filename = self._find_fpath(f.path, torrent_name)

                    if sub_path == dirpath:
                        rewrite_st[0] = DIR_MASK
                    elif sub_path == f"{os.path.join(dirpath, filename)}":
                        rewrite_st[6] = f.size

                st = os.stat_result(rewrite_st)
            
            else: # If it's an torrent file, present the file as an directory
                rewrite_st[0] = DIR_MASK

            st = os.stat_result(rewrite_st)
        else:
            st = os.lstat(full_path)

        return dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                    'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))


    def readdir(self, path, fh):
        full_path = self._full_path(path)
        torrent_path, fake_base, sub_path = self._get_paths(full_path)
        sub_path = '/' if not sub_path else sub_path

        dirents = ['.', '..']

        if os.path.isdir(full_path):
            dirents.extend(os.listdir(full_path))

        elif torrent_path and full_path.startswith(fake_base):
            info = lt.torrent_info(torrent_path)
            torrent_name = info.name()
            torrent_files = info.files()

            for f in torrent_files:
                dirpath, filename = self._find_fpath(f.path, torrent_name)

                if sub_path == dirpath:
                    dirents.append(filename)

                    # TODO: Clean this up.. :-)
                    for d in torrent_files:
                        dpath, _ = self._find_fpath(d.path, torrent_name)

                        if dpath.startswith(dirpath):
                            dpath = dpath.removeprefix(f"{dirpath}").lstrip('/')
                            directory = dpath.split('/')[0]

                            if directory and directory not in dirents:
                                dirents.append(directory)
                   
        for r in dirents:
            if r.endswith('.torrent'):
                r = r.replace('.torrent', '')
            yield r


    def statfs(self, path):
        # TODO: walk the mounted path and calc fs size
        full_path = self._full_path(path)
        stv = os.statvfs(full_path)
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    # File methods
    # ============

    def open(self, path, flags):
        full_path = self._full_path(path)
        torrent_path, fake_base, sub_path = self._get_paths(full_path)
        dirpath, filename = os.path.split(full_path)

        logger.debug(f"Open path: {path}")

        if torrent_path:
            logger.debug(f"Found torrent: {torrent_path}")
            torrent_full_path = os.path.abspath(torrent_path)

            if self._file_in_torrent(sub_path, torrent_full_path): 
                return True
            
            return False

            with self._open_torrent(torrent_full_path) as torrent:
                torrent_file = next((f for f in torrent if os.path.split(f.path)[1] == filename), None)

                if torrent_file:
                    while torrent_file.wait_for_completion(100): # we need the whole one mac!
                        logger.debug(f"{filename} downloaded {torrent_file.file_progress}%")
                        sleep(1)

                    torrent_file_path = os.path.join(torrent_file.root, torrent_file.path)

                    if os.path.isfile(torrent_file_path):
                        logger.debug(f"Open torrent file: {torrent_file_path}")
                        return os.open(torrent_file_path, flags)

        else:
            return os.open(full_path, flags)
    # https://gist.github.com/tizbac/2df2609726d6058b3c99
    def read(self, path, length, offset, fh):
        full_path = self._full_path(path)
        torrent_path, fake_base, sub_path = self._get_paths(full_path)
        dirpath, filename = os.path.split(full_path)

        logger.debug(f"Read path: {path}")

        if torrent_path:
            logger.debug(f"Found torrent: {torrent_path}")
            torrent_full_path = os.path.abspath(torrent_path)

            if not self._file_in_torrent(sub_path, torrent_full_path): return False
            
            with self._open_torrent(torrent_full_path) as torrent:
                torrent_file = next((f for f in torrent if os.path.split(f.path)[1] == filename), None)

                if torrent_file:
                    logger.debug(f"Reading file: {filename} | Length: {length} | Offset: {offset}")
                    return torrent_file.read(length, offset)

        else:
            os.lseek(fh, offset, os.SEEK_SET)
            return os.read(fh, length)

def main(mountpoint, root):
    FUSE(TorrentFS(root), mountpoint, foreground=True, ro=True, allow_other=True) #, threaded=True)

if __name__ == '__main__':
    # parse args
    parser = argparse.ArgumentParser()
    parser.add_argument("mountpoint", help="Path to target mountpoint")
    parser.add_argument("root", help="Path to root directory")
    parser.add_argument("-v", "--verbose", help="Set loglevel to debug", action="store_true")
    args = parser.parse_args()

    # set logging
    logger = logging.getLogger()
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if os.environ.get('DEBUG') or args.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    main(args.mountpoint, args.root)