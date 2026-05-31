"""
Generate PWA icons for Filmlog.
Run once: python3 generate_icons.py
Requires no dependencies — uses pure SVG → PNG via built-in tools.
"""

import struct
import zlib


def create_png(width, height, pixels):
    """Create a minimal PNG file from RGBA pixel data."""
    def chunk(chunk_type, data):
        c = chunk_type + data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + c + struct.pack('>I', crc)

    # PNG signature
    sig = b'\x89PNG\r\n\x1a\n'
    # IHDR
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    # IDAT
    raw = b''
    for y in range(height):
        raw += b'\x00'  # filter none
        for x in range(width):
            idx = (y * width + x) * 4
            raw += pixels[idx:idx+4]
    idat = chunk(b'IDAT', zlib.compress(raw))
    # IEND
    iend = chunk(b'IEND', b'')
    return sig + ihdr + idat + iend


def draw_filmlog_icon(size):
    """Draw a simple film-strip icon: dark bg with a golden film frame."""
    pixels = bytearray(size * size * 4)

    bg = (10, 10, 10, 255)       # #0a0a0a
    accent = (212, 175, 55, 255) # golden
    inner = (30, 30, 30, 255)    # dark inner

    border = max(size // 16, 2)
    sprocket_size = max(size // 12, 3)
    margin = max(size // 8, 4)

    for y in range(size):
        for x in range(size):
            idx = (y * size + x) * 4
            r, g, b, a = bg

            # Rounded rectangle frame
            cx, cy = size // 2, size // 2
            half = size // 2 - margin
            dx = abs(x - cx)
            dy = abs(y - cy)
            corner_r = max(size // 10, 4)

            in_rect = False
            if dx <= half and dy <= half:
                # Check rounded corners
                if dx > half - corner_r and dy > half - corner_r:
                    cdx = dx - (half - corner_r)
                    cdy = dy - (half - corner_r)
                    if cdx * cdx + cdy * cdy <= corner_r * corner_r:
                        in_rect = True
                else:
                    in_rect = True

            if in_rect:
                # Border
                in_inner = False
                inner_half = half - border
                inner_cr = max(corner_r - border, 1)
                idx2 = abs(x - cx)
                idy2 = abs(y - cy)
                if idx2 <= inner_half and idy2 <= inner_half:
                    if idx2 > inner_half - inner_cr and idy2 > inner_half - inner_cr:
                        cdx = idx2 - (inner_half - inner_cr)
                        cdy = idy2 - (inner_half - inner_cr)
                        if cdx * cdx + cdy * cdy <= inner_cr * inner_cr:
                            in_inner = True
                    else:
                        in_inner = True

                if in_inner:
                    # Sprocket holes (top and bottom rows)
                    is_sprocket = False
                    sprocket_y_top = margin + border + sprocket_size
                    sprocket_y_bot = size - margin - border - sprocket_size
                    num_sprockets = 4
                    spacing = (2 * inner_half) // (num_sprockets + 1)
                    start_x = cx - inner_half + spacing

                    for i in range(num_sprockets):
                        sx = start_x + i * spacing
                        for sy in [sprocket_y_top, sprocket_y_bot]:
                            ddx = x - sx
                            ddy = y - sy
                            if ddx * ddx + ddy * ddy <= sprocket_size * sprocket_size:
                                is_sprocket = True
                                break
                        if is_sprocket:
                            break

                    if is_sprocket:
                        r, g, b, a = accent
                    else:
                        r, g, b, a = inner
                else:
                    r, g, b, a = accent

            pixels[idx] = r
            pixels[idx+1] = g
            pixels[idx+2] = b
            pixels[idx+3] = a

    return bytes(pixels)


if __name__ == "__main__":
    from pathlib import Path
    static = Path(__file__).parent / "static"

    for size in [192, 512]:
        pixels = draw_filmlog_icon(size)
        png_data = create_png(size, size, pixels)
        path = static / f"icon-{size}.png"
        path.write_bytes(png_data)
        print(f"✓ Generated {path} ({len(png_data):,} bytes)")
