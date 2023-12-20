# torrent-fs
An simple torrent file system in python. `torrent-fs` will transparently present the underlying filesystem, and if there is .torrent files present `torrent-fs` will hide the .torrent file and expand the internal .torrent filestructure in an subfolder. `torrent-fs` does also supports read and seek operations, which has the benefit of only pulling the data one needs.

While there are obvious drawbacks with the current apphroach where .torrent files are expanded on each directory listing, it also has the benefit of always beening up-to-date which is one of my main goals with the filesystem.

## Issues

While I'm sure there are many issues with the current code, the best way to resolve them is to send an pull request. 

## Usage

`python torrentfs.py <mountpoint> <source folder>`

## Installation

`apt-get install libfuse-dev`
`pip install -r requirements.txt`