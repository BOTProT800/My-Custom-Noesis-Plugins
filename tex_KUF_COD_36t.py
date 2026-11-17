# -*- coding: utf-8 -*-
"""Kingdom Under Fire: Circle of Doom (Blueside, Xbox 360): texturas `.36t`.

Plugin de Noesis. Copiar a `<Noesis>/plugins/python/` y reiniciar.

El `.36t` es un recurso de textura de Direct3D de Xbox 360 volcado tal cual:
0x1000 bytes de cabecera y detrás los datos. En 0x24 de la cabecera está la
constante de fetch de la GPU Xenos (seis palabras big-endian), que lo declara
todo: formato, endianness, si va entrelazada (*tiled*), el pitch, si es 2D, 3D
o cubo, las medidas y dónde empieza la cadena de mips. Aquí no se infiere nada
del tamaño del archivo; la lectura cuadra con los 6518 `.36t` del juego.

Qué se ve en Noesis:

* 2D: la imagen, nivel 0. Las DXT se entregan comprimidas, así que exportar a
  DDS no recodifica nada.
* Cubo: las seis caras en cruz horizontal (4 x 3 caras).
* 3D (secuencias de efectos): los cortes en una rejilla de 4 columnas.
* Flotantes (sondas HDR y tablas de pesos): vista previa de 8 bits.

Corre en el Python 3.2 que Noesis lleva dentro: sin f-strings y sin nada
posterior. Las funciones de lectura no tocan `noesis` ni `rapi`, para que
`tests/test_kuf_36t_noesis_plugin.py` las pueda ejercitar fuera de Noesis.

El entrelazado sigue las fórmulas de `Tiled2D` y `Tiled3D` de Xenia
(`src/xenia/gpu/texture_address.h`) y el sitio del nivel 0 de las texturas
diminutas sigue su `GetPackedMipOffset`; ver `CREDITS.md`.
"""

import struct

try:
    from inc_noesis import *  # noqa: F401,F403 -- lo exige Noesis
except ImportError:  # fuera de Noesis (tests): solo sirven las funciones puras
    noesis = None
    rapi = None

HEADER_SIZE = 0x1000
FETCH_OFFSET = 0x24
# Bytes 0x08..0x10 de la cabecera: iguales en los 6518 archivos del juego.
RESOURCE_TAG = b"\x00\x00\x00\x03\x00\x00\x00\x01"

DIM_2D = 1
DIM_3D = 2
DIM_CUBE = 3
DIM_NAMES = {0: "1D", DIM_2D: "2D", DIM_3D: "3D", DIM_CUBE: "cubo"}

# Formatos de Xenos que usa el juego. Columnas: nombre, lado del bloque en
# téxeles, bytes por bloque.
FMT_1555 = 0x03
FMT_8888 = 0x06
FMT_DXT1 = 0x12
FMT_DXT3 = 0x13
FMT_DXT5 = 0x14
FMT_RGBA16F = 0x20
FMT_R32F = 0x24
FORMATS = {
    FMT_1555: ("A1R5G5B5", 1, 2),
    FMT_8888: ("A8R8G8B8", 1, 4),
    FMT_DXT1: ("DXT1", 4, 8),
    FMT_DXT3: ("DXT3", 4, 16),
    FMT_DXT5: ("DXT5", 4, 16),
    FMT_RGBA16F: ("RGBA16F", 1, 8),
    FMT_R32F: ("R32F", 1, 4),
}

TILE = 32  # la superficie se guarda en múltiplos de 32 bloques
SUBRESOURCE_ALIGN = 4096  # y cada cara o nivel, en múltiplos de 4096 bytes
SLICE_COLUMNS = 4  # columnas de la rejilla de una textura 3D
MAX_SIDE = 8192


class Tex36tError(Exception):
    """El archivo no es un `.36t` legible. El mensaje dice por qué."""


def _align(value, step):
    return (value + step - 1) // step * step


def _log2_ceil(value):
    bits = 0
    while (1 << bits) < value:
        bits += 1
    return bits


class Header(object):
    """La constante de fetch, ya desempaquetada."""

    def __init__(self, data):
        if len(data) <= HEADER_SIZE:
            raise Tex36tError("archivo de %d bytes: no cabe la cabecera" % len(data))
        size_a, size_b = struct.unpack_from(">II", data, 0)
        if size_a != size_b or size_a == 0:
            raise Tex36tError("los dos campos de longitud no coinciden")
        if bytes(data[8:16]) != RESOURCE_TAG:
            raise Tex36tError("la cabecera no es la de un recurso de textura")
        d0, d1, d2, _d3, _d4, d5 = struct.unpack_from(">6I", data, FETCH_OFFSET)
        if d0 & 3 != 2:
            raise Tex36tError("la constante de fetch no es de textura")

        self.data_size = size_a
        self.tiled = bool(d0 >> 31)
        self.pitch = ((d0 >> 22) & 0x1FF) * 32  # en téxeles
        self.format = d1 & 0x3F
        self.endian = (d1 >> 6) & 3
        self.dimension = (d5 >> 9) & 3
        self.packed_mips = bool((d5 >> 11) & 1)
        self.mip_offset = (d5 >> 12) << 12  # 0 si no trae mips
        if self.dimension == DIM_3D:
            self.width = (d2 & 0x7FF) + 1
            self.height = ((d2 >> 11) & 0x7FF) + 1
            self.depth = (d2 >> 22) + 1
        else:
            self.width = (d2 & 0x1FFF) + 1
            self.height = ((d2 >> 13) & 0x1FFF) + 1
            self.depth = 1
        self.faces = 6 if self.dimension == DIM_CUBE else 1

    def format_name(self):
        if self.format in FORMATS:
            return FORMATS[self.format][0]
        return "formato 0x%02X" % self.format

    def describe(self):
        size = "%dx%d" % (self.width, self.height)
        if self.dimension == DIM_3D:
            size += "x%d" % self.depth
        return "%s %s %s%s" % (
            size,
            self.format_name(),
            DIM_NAMES.get(self.dimension, "?"),
            ", con mips" if self.mip_offset else "",
        )


class Layout(object):
    """Dónde está el nivel 0 dentro de los datos, en bloques."""

    def __init__(self, header):
        if header.format not in FORMATS:
            raise Tex36tError("%s sin decodificador" % header.format_name())
        if header.dimension not in (DIM_2D, DIM_3D, DIM_CUBE):
            raise Tex36tError("textura 1D: no hay ninguna en el juego")
        if not header.tiled:
            # Ninguno de los 6518 archivos viene lineal: sin muestra no se
            # puede comprobar el pitch de fila, y adivinarlo sería inventar.
            raise Tex36tError("superficie lineal: no hay ninguna en el juego")
        if header.endian == 3:
            raise Tex36tError("endianness 16 en 32: no hay ninguna en el juego")
        if header.width > MAX_SIDE or header.height > MAX_SIDE:
            raise Tex36tError("medidas absurdas: %dx%d" % (header.width, header.height))

        _name, block, self.block_bytes = FORMATS[header.format]
        self.block = block
        self.blocks_w = (header.width + block - 1) // block
        self.blocks_h = (header.height + block - 1) // block

        # Nivel 0 diminuto con mips empaquetados: vive dentro de la cola, a 16
        # téxeles del origen siguiendo el lado corto.
        self.origin_x = self.origin_y = 0
        log_w, log_h = _log2_ceil(header.width), _log2_ceil(header.height)
        if header.packed_mips and header.dimension != DIM_3D and min(log_w, log_h) <= 4:
            if log_w > log_h:
                self.origin_y = 16 // block
            else:
                self.origin_x = 16 // block

        pitch_blocks = (header.pitch + block - 1) // block
        self.pitch = _align(max(pitch_blocks, self.origin_x + self.blocks_w), TILE)
        self.rows = _align(self.origin_y + self.blocks_h, TILE)
        if header.dimension == DIM_3D:
            self.slice_bytes = _align(
                self.pitch * self.rows * _align(header.depth, 4) * self.block_bytes,
                SUBRESOURCE_ALIGN,
            )
        else:
            self.slice_bytes = _align(
                self.pitch * self.rows * self.block_bytes, SUBRESOURCE_ALIGN
            )
        self.base_bytes = self.slice_bytes * header.faces

        limit = header.mip_offset if header.mip_offset else header.data_size
        if self.base_bytes > limit:
            raise Tex36tError(
                "el nivel 0 pide %d bytes y el archivo declara %d" % (self.base_bytes, limit)
            )


# --- Entrelazado de Xenos --------------------------------------------------
#
# La dirección de un bloque es una permutación de bits de su índice más tres
# campos fijos (paridad de fila, pipe y bank). Como es una permutación, se
# puede calcular la parte de la fila una vez y la de la columna con una tabla.

def _spread(offset):
    """Reparte los bits del offset lineal por los huecos que dejan los fijos."""
    return (
        (offset & 0xF)
        | (((offset >> 4) & 1) << 5)
        | (((offset >> 5) & 7) << 8)
        | ((offset >> 8) << 12)
    )


def _column_tables(log_bytes):
    """Aportación de la columna dentro de su macrobloque, sin y con bank."""
    tables = []
    for flip in (0, 2):
        tables.append(
            [
                _spread((x & 7) << log_bytes) | ((((x >> 3) & 3) ^ flip) << 6)
                for x in range(TILE)
            ]
        )
    return tables


def tiled_offset_2d(x, y, pitch, log_bytes):
    """Offset en bytes del bloque (x, y). `pitch` en bloques, múltiplo de 32."""
    outer = ((y >> 5) * (pitch >> 5) + (x >> 5)) << 6
    inner = (((y >> 1) & 7) << 3) | (x & 7)
    pipe = ((x >> 3) & 3) ^ (((y >> 3) & 1) << 1)
    return (
        _spread((outer | inner) << log_bytes)
        | ((y & 1) << 4)
        | (pipe << 6)
        | (((y >> 4) & 1) << 11)
    )


def tiled_offset_3d(x, y, z, pitch, rows, log_bytes):
    """Lo mismo para un volumen. `pitch` y `rows` en bloques, múltiplos de 32."""
    outer = (((z >> 2) * (rows >> 4) + (y >> 4)) * (pitch >> 5) + (x >> 5)) << 7
    inner = ((z & 3) << 5) | (((y >> 1) & 3) << 3) | (x & 7)
    bank = ((y >> 3) ^ (z >> 2)) & 1
    pipe = ((x >> 3) & 3) ^ (bank << 1)
    return (
        _spread((outer | inner) << log_bytes)
        | ((y & 1) << 4)
        | (pipe << 6)
        | (bank << 11)
    )


def _copy_row(out, dst, src, high, table, x0, count, size):
    """Copia `count` bloques de una fila, a partir de la columna `x0`."""
    x = x0
    end = x0 + count
    while x < end:
        macro = x >> 5
        base = high(macro)
        stop = min(end, (macro + 1) << 5)
        while x < stop:
            pos = base | table[x & 31]
            out[dst:dst + size] = src[pos:pos + size]
            dst += size
            x += 1
    return dst


def untile_2d(src, layout):
    """Nivel 0 de una superficie 2D, en orden lineal y ya recortado."""
    size = layout.block_bytes
    log_bytes = _log2_ceil(size)
    tables = _column_tables(log_bytes)
    macro_pitch = layout.pitch >> 5
    out = bytearray(layout.blocks_w * layout.blocks_h * size)
    dst = 0
    for row in range(layout.blocks_h):
        y = row + layout.origin_y
        fixed = ((y & 1) << 4) | (((y >> 4) & 1) << 11)
        macro_row = (y >> 5) * macro_pitch
        inner = ((y >> 1) & 7) << 3

        def high(macro, macro_row=macro_row, inner=inner, fixed=fixed):
            return _spread((((macro_row + macro) << 6) | inner) << log_bytes) | fixed

        dst = _copy_row(
            out, dst, src, high, tables[(y >> 3) & 1], layout.origin_x, layout.blocks_w, size
        )
    return out


def untile_3d(src, layout, depth):
    """Los cortes de un volumen, uno detrás de otro, en orden lineal."""
    size = layout.block_bytes
    log_bytes = _log2_ceil(size)
    tables = _column_tables(log_bytes)
    macro_pitch = layout.pitch >> 5
    macro_rows = layout.rows >> 4
    out = bytearray(layout.blocks_w * layout.blocks_h * depth * size)
    dst = 0
    for z in range(depth):
        for y in range(layout.blocks_h):
            bank = ((y >> 3) ^ (z >> 2)) & 1
            fixed = ((y & 1) << 4) | (bank << 11)
            macro_row = ((z >> 2) * macro_rows + (y >> 4)) * macro_pitch
            inner = ((z & 3) << 5) | (((y >> 1) & 3) << 3)

            def high(macro, macro_row=macro_row, inner=inner, fixed=fixed):
                return _spread((((macro_row + macro) << 7) | inner) << log_bytes) | fixed

            dst = _copy_row(out, dst, src, high, tables[bank], 0, layout.blocks_w, size)
    return out


# --- Endianness ------------------------------------------------------------

def swap_endian(data, endian):
    """Deja los datos en little-endian según el campo de la constante."""
    data = bytearray(data)
    if endian == 0:
        return data
    out = bytearray(len(data))
    if endian == 1:  # 8 en 16
        whole = len(data) & ~1
        out[0:whole:2] = data[1:whole:2]
        out[1:whole:2] = data[0:whole:2]
    else:  # 8 en 32
        whole = len(data) & ~3
        for i in range(4):
            out[i:whole:4] = data[3 - i:whole:4]
    out[whole:] = data[whole:]
    return out


# --- Lectura ---------------------------------------------------------------

def read_levels(data):
    """Cabecera, layout y los bloques del nivel 0 en orden lineal.

    Devuelve `(header, layout, images)`. `images` trae una entrada por cara
    (cubo) o por corte (3D), cada una de `blocks_w * blocks_h` bloques.
    """
    header = Header(data)
    layout = Layout(header)
    payload = data[HEADER_SIZE:HEADER_SIZE + layout.base_bytes]
    if len(payload) < layout.base_bytes:
        raise Tex36tError(
            "archivo truncado: faltan %d bytes del nivel 0" % (layout.base_bytes - len(payload))
        )
    payload = swap_endian(payload, header.endian)

    if header.dimension == DIM_3D:
        linear = untile_3d(payload, layout, header.depth)
        step = layout.blocks_w * layout.blocks_h * layout.block_bytes
        images = [linear[z * step:(z + 1) * step] for z in range(header.depth)]
    else:
        images = []
        for face in range(header.faces):
            start = face * layout.slice_bytes
            images.append(untile_2d(payload[start:start + layout.slice_bytes], layout))
    return header, layout, images


# --- De bloques a RGBA -----------------------------------------------------

def _half_to_float(value):
    exponent = (value >> 10) & 0x1F
    mantissa = value & 0x3FF
    if exponent == 0:
        result = mantissa * 2.0 ** -24
    elif exponent == 31:
        result = 65504.0  # infinito o NaN: al tope
    else:
        result = (1.0 + mantissa / 1024.0) * 2.0 ** (exponent - 15)
    return -result if value & 0x8000 else result


_TONE_TABLE = []


def _tone_table():
    """Medio flotante -> byte: Reinhard y gamma 2.2, que es solo para mirar."""
    if not _TONE_TABLE:
        for value in range(0x10000):
            v = _half_to_float(value)
            if v <= 0.0:
                _TONE_TABLE.append(0)
            else:
                _TONE_TABLE.append(int(255.0 * (v / (1.0 + v)) ** (1.0 / 2.2) + 0.5))
    return _TONE_TABLE


def rgba16f_to_rgba(linear, count):
    table = _tone_table()
    words = struct.unpack("<%dH" % (count * 4), bytes(linear[:count * 8]))
    out = bytearray(count * 4)
    out[0::4] = bytes([table[w] for w in words[0::4]])
    out[1::4] = bytes([table[w] for w in words[1::4]])
    out[2::4] = bytes([table[w] for w in words[2::4]])
    out[3::4] = b"\xff" * count
    return out


def alpha_bit_set(linear):
    """¿Algún téxel 1_5_5_5 (little-endian) trae el bit de alfa?"""
    return any(b & 0x80 for b in bytes(linear[1::2]))


def r32f_range(images, count):
    """Mínimo y máximo de una textura de un canal flotante, entre todas sus imágenes."""
    low = high = None
    for linear in images:
        values = struct.unpack("<%df" % count, bytes(linear[:count * 4]))
        finite = [v for v in values if v == v and abs(v) != float("inf")]
        if finite:
            low = min(finite) if low is None else min(low, min(finite))
            high = max(finite) if high is None else max(high, max(finite))
    if low is None:
        return 0.0, 0.0
    return low, high


def r32f_to_rgba(linear, count, low, high):
    """Gris normalizado entre `low` y `high`: los pesos no están en 0..1."""
    values = struct.unpack("<%df" % count, bytes(linear[:count * 4]))
    span = high - low
    if span <= 0.0:
        gray = bytes(count)
    else:
        scale = 255.0 / span
        gray = bytes(
            [
                int((min(max(v, low), high) - low) * scale + 0.5) if v == v else 0
                for v in values
            ]
        )
    out = bytearray(count * 4)
    out[0::4] = gray
    out[1::4] = gray
    out[2::4] = gray
    out[3::4] = b"\xff" * count
    return out


def crop_rgba(rgba, src_w, width, height):
    """Recorta a `width` x `height` una imagen RGBA de ancho `src_w`."""
    if src_w == width and len(rgba) == width * height * 4:
        return rgba
    out = bytearray(width * height * 4)
    for y in range(height):
        out[y * width * 4:(y + 1) * width * 4] = rgba[y * src_w * 4:y * src_w * 4 + width * 4]
    return out


def compose(tiles, width, height, places, columns, rows):
    """Pega imágenes RGBA iguales en una hoja. `places` da (columna, fila)."""
    sheet_w = width * columns
    out = bytearray(sheet_w * height * rows * 4)
    line = width * 4
    for tile, (col, row) in zip(tiles, places):
        for y in range(height):
            dst = ((row * height + y) * sheet_w + col * width) * 4
            out[dst:dst + line] = tile[y * line:(y + 1) * line]
    return out, sheet_w, height * rows


# Cruz horizontal de un cubo de Direct3D, caras en el orden del archivo:
# +X, -X, +Y, -Y, +Z, -Z.
#          [+Y]
#     [-X] [+Z] [+X] [-Z]
#          [-Y]
CROSS_PLACES = ((2, 1), (0, 1), (1, 0), (1, 2), (1, 1), (3, 1))


def slice_places(depth):
    columns = min(depth, SLICE_COLUMNS)
    rows = (depth + columns - 1) // columns
    return [(z % columns, z // columns) for z in range(depth)], columns, rows


# --- Noesis ----------------------------------------------------------------

def registerNoesisTypes():
    handle = noesis.register("Kingdom Under Fire: Circle of Doom (X360) texture", ".36t")
    noesis.setHandlerTypeCheck(handle, noepy_check_type)
    noesis.setHandlerLoadRGBA(handle, noepy_load_rgba)
    return 1


def noepy_check_type(data):
    try:
        Header(data)
    except Tex36tError:
        return 0
    return 1


def _dxt_type(fmt):
    if fmt == FMT_DXT1:
        return noesis.NOESISTEX_DXT1
    if fmt == FMT_DXT3:
        return noesis.NOESISTEX_DXT3
    return noesis.NOESISTEX_DXT5


def _to_rgba(header, layout, images):
    """Cada imagen del nivel 0, en RGBA de 8 bits y recortada a su tamaño."""
    stored_w = layout.blocks_w * layout.block
    stored_h = layout.blocks_h * layout.block
    count = stored_w * stored_h
    fmt = header.format
    if fmt == FMT_R32F:
        low, high = r32f_range(images, count)
        print("36t: R32F entre %g y %g; la vista previa lo normaliza" % (low, high))
    opaque = fmt == FMT_1555 and not any(alpha_bit_set(linear) for linear in images)
    if opaque:
        # D3DFMT_X1R5G5B5 también se guarda como 1_5_5_5: sin ningún téxel con
        # el bit de alfa, la imagen entera se vería transparente.
        print("36t: ningún téxel trae el bit de alfa; se muestra como X1R5G5B5")
    out = []
    for linear in images:
        if fmt in (FMT_DXT1, FMT_DXT3, FMT_DXT5):
            rgba = rapi.imageDecodeDXT(linear, stored_w, stored_h, _dxt_type(fmt))
        elif fmt == FMT_8888:
            rgba = rapi.imageDecodeRaw(linear, stored_w, stored_h, "b8g8r8a8")
        elif fmt == FMT_1555:
            rgba = bytearray(rapi.imageDecodeRaw(linear, stored_w, stored_h, "b5g5r5a1"))
            if opaque:
                rgba[3::4] = b"\xff" * count
        elif fmt == FMT_RGBA16F:
            rgba = rgba16f_to_rgba(linear, count)
        else:
            rgba = r32f_to_rgba(linear, count, low, high)
        out.append(crop_rgba(rgba, stored_w, header.width, header.height))
    return out


def noepy_load_rgba(data, texList):
    try:
        header, layout, images = read_levels(data)
    except Tex36tError as exc:
        print("36t: no se puede abrir: %s" % exc)
        return 0
    print("36t: " + header.describe())

    name = rapi.getExtensionlessName(rapi.getLocalFileName(rapi.getInputName()))
    width, height = header.width, header.height
    compressed = header.format in (FMT_DXT1, FMT_DXT3, FMT_DXT5)
    if (
        compressed
        and header.dimension == DIM_2D
        and width % 4 == 0
        and height % 4 == 0
    ):
        # Tal cual vienen los bloques: exportar a DDS no pierde nada.
        texList.append(NoeTexture(name, width, height, images[0], _dxt_type(header.format)))
        return 1

    tiles = _to_rgba(header, layout, images)
    if header.dimension == DIM_CUBE:
        rgba, width, height = compose(tiles, width, height, CROSS_PLACES, 4, 3)
    elif header.dimension == DIM_3D:
        places, columns, rows = slice_places(header.depth)
        rgba, width, height = compose(tiles, width, height, places, columns, rows)
    else:
        rgba = tiles[0]
    texList.append(NoeTexture(name, width, height, rgba, noesis.NOESISTEX_RGBA32))
    return 1
