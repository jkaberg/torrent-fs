## torrent-fs
An simple torrent file system in python. `torrent-fs` will transparently present the underlying filesystem, and if there is .torrent files present `torrent-fs` will hide the .torrent file and expand the internal .torrent filestructure in an subfolder. `torrent-fs` also supports read and seek operations.

# Usage

`python torrentfs.py <mountpoint> <source folder>`

# Installation

`apt-get install libfuse-dev`
`pip install -r requirements.txt`