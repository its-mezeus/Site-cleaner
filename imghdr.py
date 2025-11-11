# Minimal shim for the removed stdlib 'imghdr' (Python 3.13+).
# Provides imghdr.what(filename=None, h=None) -> type or None
# Types supported: 'rgb', 'gif', 'pbm', 'pgm', 'ppm', 'tiff', 'rast',
# 'xbm', 'jpeg', 'bmp', 'png', 'webp'

def what(filename=None, h=None):
    if h is None:
        if filename is None:
            return None
        try:
            with open(filename, 'rb') as f:
                h = f.read(64)
        except Exception:
            return None

    # signatures
    data = h[:64]

    # JPEG
    if len(data) >= 3 and data[0:3] == b'\xff\xd8\xff':
        return 'jpeg'
    # PNG
    if len(data) >= 8 and data[0:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    # GIF
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'gif'
    # BMP
    if data.startswith(b'BM'):
        return 'bmp'
    # WEBP (RIFF....WEBP)
    if len(data) >= 12 and data[0:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    # TIFF
    if data.startswith(b'II*\x00') or data.startswith(b'MM\x00*'):
        return 'tiff'
    # PBM/PGM/PPM (magic P1..P6 text headers)
    if len(data) >= 2 and data[0:1] == b'P' and data[1:2] in b'123456':
        magic = data[1:2]
        return {'1': 'pbm', '2': 'pgm', '3': 'ppm', '4': 'pbm', '5': 'pgm', '6': 'ppm'}.get(magic.decode(), None)
    # XBM (starts with '#define' often, weak)
    if data.startswith(b'#define'):
        return 'xbm'
    # Sun rasterfile
    if data.startswith(b'\x59\xa6\x6a\x95'):
        return 'rast'
    # SGI RGB
    if data.startswith(b'\x01\xda'):
        return 'rgb'

    return None
