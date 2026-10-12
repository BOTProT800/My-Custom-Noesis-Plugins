# Dawn of Titans - texturas ATC (NaturalMotion / Echo engine)
#
# Abre en Noesis las texturas del juego tal y como estan en disco, sin pasar por
# ANANSI: los .atc.bof comprimidos (zstd o zlib), el .atc ya descomprimido y los
# .atc sueltos con un DDS dentro (envuelto o no en $Nmg). Desde Noesis se
# exportan a PNG, DDS o lo que haga falta con su Exportar de siempre.
#
# Es tambien la biblioteca de texturas de fmt_DOT.py: con ella las mallas salen
# texturizadas -texturas incrustadas en el propio archivo o buscadas por nombre
# en Media/3d/Textures- y Noesis las escribe al exportar la malla.
#
# Todo es Python puro porque el Python de Noesis no trae ni zstd ni ATC:
#   - zstd segun la RFC 8878 (comprobado byte a byte contra python-zstandard);
#   - ATC como lo decodifica texture2ddecoder, con el alfa explicito de ATCA de
#     4 bits por pixel (comprobado pixel a pixel).
# Las dos texturas PVRTC del corpus las decodifica el propio Noesis.
# Las pruebas estan en tests/test_dot_text_plugin.py y el barrido del corpus en
# scripts/dot_texture_coverage.py.
#
# Los comentarios en ASCII sin acentos son a proposito, como en fmt_DOT.py: el
# interprete es el Python 3.2 que Noesis lleva dentro (sin f-strings).
#
# Instalar: copiar text_DOT.py y fmt_DOT.py a Noesis/plugins/python/ y
# reiniciar Noesis.

import os
import struct
import zlib

try:
    from inc_noesis import *
    import noesis
    import rapi
except ImportError:
    # Fuera de Noesis (pruebas, arneses): queda la biblioteca sin el registro.
    noesis = None
    rapi = None


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
NMG_MAGIC = b"$Nmg"
DDS_MAGIC = b"DDS "
DOT_MAGIC = b"VERSION\x00"
ECHO_MARKER = b"Echo BOF format 1.0"

DOT_NAME_FIELD = 32
DOT_SECTION_HEADER = 36

# Formato del TEXTURE-HEADER (campo 4). Medido en el corpus: 3 = ATC RGB con
# flags 16, 4 = ATCA con flags 17; 0 = textura externa, sin TEXTURE-IMAGE.
DOT_FORMAT_ATC = 3
DOT_FORMAT_ATCA = 4
# El ultimo campo es el que manda: dos texturas del corpus (TD_FARMFIELDS y
# TD_ELEMENTALBOWSTRING) declaran formato 4 pero llevan 10 ahi, y son PVRTC de
# 4 bits -restos de la version de iOS-: su nivel 0 mide 4 bits por pixel y
# solo PVRTC da una imagen limpia. Las decodifica Noesis (imageDecodePVRTC).
DOT_PIXEL_PVRTC4 = 10


def registerNoesisTypes():
    for ext in (".bof", ".atc"):
        handle = noesis.register("Dawn of Titans ATC texture", ext)
        noesis.setHandlerTypeCheck(handle, noepyCheckType)
        noesis.setHandlerLoadRGBA(handle, noepyLoadRGBA)
    return 1


# --------------------------------------------------------------------------
# Zstandard (RFC 8878)
#
# Lo justo para los .atc.bof: tramas sin diccionario, bloques crudos, RLE y
# comprimidos. Toda la salida de la trama se queda en memoria, asi que los
# offsets de las coincidencias son simples restas. El checksum opcional de la
# trama no se comprueba; si se comprueba el tamano declarado.
# --------------------------------------------------------------------------

_LL_BASE = tuple(range(16)) + (
    16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048, 4096,
    8192, 16384, 32768, 65536)
_LL_BITS = (0,) * 16 + (
    1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
_ML_BASE = tuple(range(3, 35)) + (
    35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051,
    4099, 8195, 16387, 32771, 65539)
_ML_BITS = (0,) * 32 + (
    1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)

# Distribuciones predefinidas (RFC 8878, 3.1.1.3.2.2).
_LL_DEFAULT = ([4, 3] + [2] * 11 + [1] * 3 + [2] * 9 + [3, 2] + [1] * 5
               + [-1] * 4, 6)
_ML_DEFAULT = ([1, 4, 3] + [2] * 6 + [1] * 37 + [-1] * 7, 6)
_OF_DEFAULT = ([1] * 6 + [2] * 3 + [1] * 15 + [-1] * 5, 5)

# (simbolo maximo, precision maxima) por tabla.
_LL_LIMITS = (35, 9)
_ML_LIMITS = (52, 9)
_OF_LIMITS = (31, 8)


class ZstdError(ValueError):
    pass


def _fse_table(norm, accuracy):
    """Tabla de decodificacion FSE: una entrada (simbolo, bits, base) por estado."""
    size = 1 << accuracy
    symbols = [0] * size
    high = size - 1
    next_state = [0] * len(norm)
    for sym, count in enumerate(norm):
        if count == -1:
            symbols[high] = sym
            high -= 1
            next_state[sym] = 1
        else:
            next_state[sym] = count
    pos = 0
    step = (size >> 1) + (size >> 3) + 3
    mask = size - 1
    for sym, count in enumerate(norm):
        for _ in range(count):
            symbols[pos] = sym
            pos = (pos + step) & mask
            while pos > high:
                pos = (pos + step) & mask
    if pos != 0:
        raise ZstdError("tabla FSE mal repartida")
    table = []
    for state in range(size):
        sym = symbols[state]
        nxt = next_state[sym]
        next_state[sym] = nxt + 1
        nbits = accuracy - (nxt.bit_length() - 1)
        table.append((sym, nbits, (nxt << nbits) - size))
    return table


def _read_fse_description(data, pos, end, max_symbol, max_accuracy):
    """Cuentas normalizadas de una tabla FSE. Devuelve (cuentas, precision, pos)."""
    chunk = data[pos:min(end, pos + 512)]
    bits = int.from_bytes(bytes(chunk), "little")
    accuracy = (bits & 0xF) + 5
    if accuracy > max_accuracy:
        raise ZstdError("precision FSE %d fuera de rango" % accuracy)
    bitpos = 4
    remaining = (1 << accuracy) + 1
    threshold = 1 << accuracy
    nbits = accuracy + 1
    norm = []
    prev_zero = False
    while remaining > 1 and len(norm) <= max_symbol:
        if prev_zero:
            n0 = len(norm)
            while ((bits >> bitpos) & 0xFFFF) == 0xFFFF:
                n0 += 24
                bitpos += 16
            while ((bits >> bitpos) & 3) == 3:
                n0 += 3
                bitpos += 2
            n0 += (bits >> bitpos) & 3
            bitpos += 2
            if n0 > max_symbol:
                raise ZstdError("ceros de mas en la tabla FSE")
            norm.extend([0] * (n0 - len(norm)))
        biggest = (2 * threshold - 1) - remaining
        value = bits >> bitpos
        if (value & (threshold - 1)) < biggest:
            count = value & (threshold - 1)
            bitpos += nbits - 1
        else:
            count = value & (2 * threshold - 1)
            if count >= threshold:
                count -= biggest
            bitpos += nbits
        count -= 1
        remaining -= -count if count < 0 else count
        norm.append(count)
        prev_zero = count == 0
        while remaining < threshold:
            nbits -= 1
            threshold >>= 1
    if remaining != 1:
        raise ZstdError("cuentas FSE que no suman")
    used = (bitpos + 7) >> 3
    if pos + used > end:
        raise ZstdError("tabla FSE truncada")
    return norm, accuracy, pos + used


_PREDEFINED = {}


def _predefined(name):
    if name not in _PREDEFINED:
        norm, accuracy = {"ll": _LL_DEFAULT, "ml": _ML_DEFAULT,
                          "of": _OF_DEFAULT}[name]
        _PREDEFINED[name] = (_fse_table(norm, accuracy), accuracy)
    return _PREDEFINED[name]


class _BackBits(object):
    """Flujo de bits que se lee hacia atras, desde la marca del ultimo byte.

    Leer n bits devuelve los n bits mas altos de lo que queda. Pasado el
    principio se leen ceros: el ultimo estado de un flujo FSE puede pedirlos.
    """

    def __init__(self, data, start, end):
        if end <= start:
            raise ZstdError("flujo de bits vacio")
        last = data[end - 1]
        if last == 0:
            raise ZstdError("flujo de bits sin marca final")
        self.data = data
        self.start = start
        self.pos = (end - start - 1) * 8 + last.bit_length() - 1

    def read(self, n):
        if n == 0:
            return 0
        low = self.pos - n
        if low >= 0:
            b = self.start + (low >> 3)
            nbytes = ((low & 7) + n + 7) >> 3
            value = int.from_bytes(bytes(self.data[b:b + nbytes]), "little")
            self.pos = low
            return (value >> (low & 7)) & ((1 << n) - 1)
        avail = self.pos
        value = 0
        if avail > 0:
            value = int.from_bytes(
                bytes(self.data[self.start:self.start + ((avail + 7) >> 3)]),
                "little") & ((1 << avail) - 1)
            value <<= n - avail
        self.pos = low
        return value


def _huffman_weights(data, pos, end):
    """Pesos de la descripcion del arbol Huffman (sin el ultimo, implicito)."""
    head = data[pos]
    pos += 1
    if head >= 128:
        count = head - 127
        weights = []
        for i in range(count):
            byte = data[pos + (i >> 1)]
            weights.append(byte >> 4 if i % 2 == 0 else byte & 15)
        return weights, pos + ((count + 1) >> 1)
    stop = pos + head
    if stop > end:
        raise ZstdError("pesos Huffman truncados")
    norm, accuracy, pos = _read_fse_description(data, pos, stop, 255, 6)
    table = _fse_table(norm, accuracy)
    bits = _BackBits(data, pos, stop)
    s1 = bits.read(accuracy)
    s2 = bits.read(accuracy)
    weights = []
    while len(weights) < 255:
        sym, nb, base = table[s1]
        weights.append(sym)
        s1 = base + bits.read(nb)
        if bits.pos < 0:
            weights.append(table[s2][0])
            break
        sym, nb, base = table[s2]
        weights.append(sym)
        s2 = base + bits.read(nb)
        if bits.pos < 0:
            weights.append(table[s1][0])
            break
    else:
        raise ZstdError("demasiados pesos Huffman")
    return weights, stop


def _huffman_table(weights):
    """(simbolo por entrada, bits por entrada, bits maximos)."""
    total = 0
    for w in weights:
        if w > 12:
            raise ZstdError("peso Huffman fuera de rango")
        if w:
            total += 1 << (w - 1)
    if total == 0:
        raise ZstdError("arbol Huffman vacio")
    max_bits = total.bit_length()
    left = (1 << max_bits) - total
    if left & (left - 1):
        raise ZstdError("arbol Huffman incompleto")
    weights = list(weights) + [left.bit_length()]
    if max_bits > 11:
        raise ZstdError("arbol Huffman demasiado profundo")
    per_weight = [0] * (max_bits + 2)
    for w in weights:
        per_weight[w] += 1
    start = [0] * (max_bits + 2)
    acc = 0
    for w in range(1, max_bits + 1):
        start[w] = acc
        acc += per_weight[w] << (w - 1)
    size = 1 << max_bits
    symbols = [0] * size
    lengths = [0] * size
    for sym, w in enumerate(weights):
        if not w:
            continue
        first = start[w]
        span = 1 << (w - 1)
        nbits = max_bits + 1 - w
        for u in range(first, first + span):
            symbols[u] = sym
            lengths[u] = nbits
        start[w] = first + span
    return symbols, lengths, max_bits


def _huffman_stream(data, start, end, count, table, out):
    """Decodifica *count* literales de un flujo Huffman y los anade a *out*."""
    symbols, lengths, max_bits = table
    if end <= start:
        raise ZstdError("flujo Huffman vacio")
    last = data[end - 1]
    if last == 0:
        raise ZstdError("flujo Huffman sin marca final")
    pos = (end - start - 1) * 8 + last.bit_length() - 1
    mask = (1 << max_bits) - 1
    append = out.append
    left = count
    # Ventanas de 56 bits: varios simbolos por cada lectura de bytes.
    while left > 0 and pos >= 56:
        low = pos - 56
        b = start + (low >> 3)
        window = int.from_bytes(bytes(data[b:b + 8]), "little") >> (low & 7)
        avail = 56
        while avail >= max_bits and left > 0:
            v = (window >> (avail - max_bits)) & mask
            append(symbols[v])
            avail -= lengths[v]
            left -= 1
        pos = low + avail
    if left > 0:
        window = 0
        if pos > 0:
            window = int.from_bytes(
                bytes(data[start:start + ((pos + 7) >> 3)]), "little")
        avail = pos
        while left > 0:
            if avail >= max_bits:
                v = (window >> (avail - max_bits)) & mask
            elif avail > 0:
                v = ((window & ((1 << avail) - 1)) << (max_bits - avail)) & mask
            else:
                raise ZstdError("flujo Huffman agotado")
            append(symbols[v])
            avail -= lengths[v]
            left -= 1
        pos = avail
    if pos != 0:
        raise ZstdError("flujo Huffman mal terminado")


class _FrameState(object):
    def __init__(self):
        self.huffman = None
        self.ll = None
        self.of = None
        self.ml = None
        self.reps = [1, 4, 8]


def _literals(data, pos, end, state):
    b0 = data[pos]
    kind = b0 & 3
    size_format = (b0 >> 2) & 3
    if kind < 2:
        if size_format in (0, 2):
            size = b0 >> 3
            pos += 1
        elif size_format == 1:
            size = (b0 >> 4) + (data[pos + 1] << 4)
            pos += 2
        else:
            size = (b0 >> 4) + (data[pos + 1] << 4) + (data[pos + 2] << 12)
            pos += 3
        if kind == 0:
            if pos + size > end:
                raise ZstdError("literales crudos truncados")
            return bytes(data[pos:pos + size]), pos + size
        return bytes((data[pos],)) * size, pos + 1

    if size_format == 0:
        streams, head, width = 1, 3, 10
    elif size_format == 1:
        streams, head, width = 4, 3, 10
    elif size_format == 2:
        streams, head, width = 4, 4, 14
    else:
        streams, head, width = 4, 5, 18
    value = int.from_bytes(bytes(data[pos:pos + head]), "little")
    regen = (value >> 4) & ((1 << width) - 1)
    csize = (value >> (4 + width)) & ((1 << width) - 1)
    pos += head
    stop = pos + csize
    if stop > end:
        raise ZstdError("literales comprimidos truncados")
    if kind == 2:
        weights, pos = _huffman_weights(data, pos, stop)
        state.huffman = _huffman_table(weights)
    elif state.huffman is None:
        raise ZstdError("literales sin arbol Huffman previo")
    out = bytearray()
    if streams == 1:
        _huffman_stream(data, pos, stop, regen, state.huffman, out)
    else:
        s1, s2, s3 = struct.unpack_from("<3H", data, pos)
        pos += 6
        sizes = (s1, s2, s3, stop - pos - s1 - s2 - s3)
        seg = (regen + 3) >> 2
        counts = (seg, seg, seg, regen - 3 * seg)
        if sizes[3] < 0 or counts[3] < 0:
            raise ZstdError("tabla de saltos de literales incoherente")
        for i in range(4):
            _huffman_stream(data, pos, pos + sizes[i], counts[i],
                            state.huffman, out)
            pos += sizes[i]
    if len(out) != regen:
        raise ZstdError("literales de tamano distinto al declarado")
    return out, stop


def _sequence_table(data, pos, end, mode, previous, name, limits):
    if mode == 0:
        return _predefined(name), pos
    if mode == 1:
        return ([(data[pos], 0, 0)], 0), pos + 1
    if mode == 2:
        norm, accuracy, pos = _read_fse_description(
            data, pos, end, limits[0], limits[1])
        return (_fse_table(norm, accuracy), accuracy), pos
    if previous is None:
        raise ZstdError("tabla repetida sin tabla anterior")
    return previous, pos


def _compressed_block(data, pos, end, state, out):
    literals, pos = _literals(data, pos, end, state)
    b0 = data[pos]
    if b0 == 0:
        out += literals
        return
    if b0 < 128:
        count = b0
        pos += 1
    elif b0 < 255:
        count = ((b0 - 128) << 8) + data[pos + 1]
        pos += 2
    else:
        count = data[pos + 1] + (data[pos + 2] << 8) + 0x7F00
        pos += 3
    modes = data[pos]
    pos += 1
    state.ll, pos = _sequence_table(
        data, pos, end, modes >> 6, state.ll, "ll", _LL_LIMITS)
    state.of, pos = _sequence_table(
        data, pos, end, (modes >> 4) & 3, state.of, "of", _OF_LIMITS)
    state.ml, pos = _sequence_table(
        data, pos, end, (modes >> 2) & 3, state.ml, "ml", _ML_LIMITS)

    ll_table, ll_acc = state.ll
    of_table, of_acc = state.of
    ml_table, ml_acc = state.ml
    bits = _BackBits(data, pos, end)
    read = bits.read
    s_ll = read(ll_acc)
    s_of = read(of_acc)
    s_ml = read(ml_acc)
    rep0, rep1, rep2 = state.reps
    lit_pos = 0
    for i in range(count):
        of_code = of_table[s_of][0]
        ml_code = ml_table[s_ml][0]
        ll_code = ll_table[s_ll][0]
        offset_value = (1 << of_code) + read(of_code)
        match_len = _ML_BASE[ml_code] + read(_ML_BITS[ml_code])
        lit_len = _LL_BASE[ll_code] + read(_LL_BITS[ll_code])

        if offset_value > 3:
            offset = offset_value - 3
            rep2 = rep1
            rep1 = rep0
            rep0 = offset
        else:
            idx = offset_value - 1 + (1 if lit_len == 0 else 0)
            if idx == 0:
                offset = rep0
            elif idx == 1:
                offset = rep1
                rep1 = rep0
                rep0 = offset
            elif idx == 2:
                offset = rep2
                rep2 = rep1
                rep1 = rep0
                rep0 = offset
            else:
                offset = rep0 - 1
                if offset == 0:
                    raise ZstdError("offset repetido a cero")
                rep2 = rep1
                rep1 = rep0
                rep0 = offset

        if lit_len:
            if lit_pos + lit_len > len(literals):
                raise ZstdError("la secuencia pide mas literales de los que hay")
            out += literals[lit_pos:lit_pos + lit_len]
            lit_pos += lit_len
        src = len(out) - offset
        if src < 0:
            raise ZstdError("offset antes del principio de la trama")
        if offset >= match_len:
            out += out[src:src + match_len]
        else:
            chunk = out[src:]
            out += (chunk * (match_len // offset + 1))[:match_len]

        if i != count - 1:
            entry = ll_table[s_ll]
            s_ll = entry[2] + read(entry[1])
            entry = ml_table[s_ml]
            s_ml = entry[2] + read(entry[1])
            entry = of_table[s_of]
            s_of = entry[2] + read(entry[1])
    if bits.pos != 0:
        raise ZstdError("flujo de secuencias mal terminado")
    state.reps = [rep0, rep1, rep2]
    out += literals[lit_pos:]


def _zstd_frame(data, pos, result, limit):
    """Decodifica una trama desde *pos* (tras el magic). Devuelve (pos, completa)."""
    fhd = data[pos]
    pos += 1
    if fhd & 0x08:
        raise ZstdError("bit reservado en la cabecera de trama")
    fcs_flag = fhd >> 6
    single = (fhd >> 5) & 1
    has_checksum = (fhd >> 2) & 1
    did_size = (0, 1, 2, 4)[fhd & 3]
    if not single:
        pos += 1
    if did_size:
        if int.from_bytes(bytes(data[pos:pos + did_size]), "little"):
            raise ZstdError("trama con diccionario: no soportada")
        pos += did_size
    fcs_size = (1 if single else 0, 2, 4, 8)[fcs_flag]
    content_size = None
    if fcs_size:
        content_size = int.from_bytes(bytes(data[pos:pos + fcs_size]), "little")
        if fcs_size == 2:
            content_size += 256
        pos += fcs_size

    state = _FrameState()
    out = bytearray()
    while True:
        if pos + 3 > len(data):
            raise ZstdError("bloque truncado")
        head = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16)
        pos += 3
        last = head & 1
        kind = (head >> 1) & 3
        size = head >> 3
        if kind == 0:
            if pos + size > len(data):
                raise ZstdError("bloque crudo truncado")
            out += data[pos:pos + size]
            pos += size
        elif kind == 1:
            out += bytes((data[pos],)) * size
            pos += 1
        elif kind == 2:
            if pos + size > len(data):
                raise ZstdError("bloque comprimido truncado")
            _compressed_block(data, pos, pos + size, state, out)
            pos += size
        else:
            raise ZstdError("tipo de bloque reservado")
        if last:
            break
        if limit is not None and len(result) + len(out) >= limit:
            result += out
            return pos, False
    if has_checksum:
        pos += 4
    if content_size is not None and len(out) != content_size:
        raise ZstdError("la trama da %d bytes y declara %d" % (len(out), content_size))
    result += out
    return pos, True


def zstd_decompress(data, limit=None):
    """Descomprime todas las tramas zstd de *data*.

    Con *limit* se para en cuanto la salida alcanza ese tamano (al final de un
    bloque): basta para leer la cabecera de un archivo sin descomprimirlo entero.
    """
    result = bytearray()
    pos = 0
    while pos < len(data):
        if pos + 4 > len(data):
            raise ZstdError("restos tras la ultima trama")
        magic = struct.unpack_from("<I", data, pos)[0]
        if 0x184D2A50 <= magic <= 0x184D2A5F:
            pos += 8 + struct.unpack_from("<I", data, pos + 4)[0]
            continue
        if magic != 0xFD2FB528:
            raise ZstdError("magic zstd invalido")
        pos, complete = _zstd_frame(data, pos + 4, result, limit)
        if not complete or (limit is not None and len(result) >= limit):
            break
    return bytes(result)


# --------------------------------------------------------------------------
# Contenedores: .atc.bof (zstd, zlib o crudo), $Nmg y DDS
# --------------------------------------------------------------------------


def _is_zlib(head):
    return (len(head) >= 2 and (head[0] & 0x0F) == 8
            and ((head[0] << 8) | head[1]) % 31 == 0)


def container_kind(data):
    head = bytes(data[:20])
    if head[:4] == ZSTD_MAGIC:
        return "zstd"
    if head[:8] == DOT_MAGIC:
        return "raw"
    if head[:4] == DDS_MAGIC:
        return "dds"
    if head[1:5] == NMG_MAGIC:
        return "nmg"
    if _is_zlib(head):
        return "zlib"
    return None


_PAYLOAD_CACHE = []
_PAYLOAD_CACHE_MAX = 4


def _cache_key(data):
    return (len(data), bytes(data[:64]), bytes(data[-64:]))


def bof_payload(data):
    """El Echo BOF descomprimido, o None si *data* no es un .bof/.atc del juego."""
    kind = container_kind(data)
    if kind == "raw":
        return data
    if kind not in ("zstd", "zlib"):
        return None
    key = _cache_key(data)
    for k, payload in _PAYLOAD_CACHE:
        if k == key:
            return payload
    try:
        if kind == "zstd":
            payload = zstd_decompress(data)
        else:
            payload = zlib.decompress(bytes(data))
    except Exception as exc:
        print("DOT: no se pudo descomprimir (%s): %s" % (kind, exc))
        return None
    if payload[:8] != DOT_MAGIC:
        return None
    _PAYLOAD_CACHE.append((key, payload))
    del _PAYLOAD_CACHE[:-_PAYLOAD_CACHE_MAX]
    return payload


_HEAD_CACHE = [None, None]


def payload_head(data, need=65536):
    """El principio del payload, descomprimiendo solo lo necesario.

    Noesis comprueba el tipo una vez por manejador registrado para la
    extension, asi que el ultimo resultado se guarda.
    """
    kind = container_kind(data)
    if kind == "raw":
        return data
    if kind not in ("zstd", "zlib"):
        return None
    key = (_cache_key(data), need)
    if _HEAD_CACHE[0] == key:
        return _HEAD_CACHE[1]
    for k, payload in _PAYLOAD_CACHE:
        if k == key[0]:
            return payload
    if kind == "zstd":
        head = zstd_decompress(data, limit=need)
    else:
        head = zlib.decompressobj().decompress(bytes(data), need)
    _HEAD_CACHE[0] = key
    _HEAD_CACHE[1] = head
    return head


def _nmg_dds(data):
    """El DDS de un .atc envuelto en $Nmg: 16 bytes de cabecera + zstd o zlib."""
    body = bytes(data[16:])
    if body[:4] == ZSTD_MAGIC:
        return zstd_decompress(body)
    return zlib.decompress(body)


# --------------------------------------------------------------------------
# Secciones de nivel raiz del Echo BOF
#
# [nombre: 32 bytes con ceros][u32 size][cuerpo]. Aqui solo hace falta la raiz
# y el bloque TEXTURES; el arbol completo lo recorre fmt_DOT.py.
# --------------------------------------------------------------------------


def _section_name(data, off, end):
    if off < 0 or off + DOT_SECTION_HEADER > end:
        return None
    raw = bytes(data[off:off + DOT_NAME_FIELD])
    z = raw.find(b"\x00")
    if z <= 0 or raw[z:].strip(b"\x00"):
        return None
    name = raw[:z]
    for c in name:
        if not (65 <= c <= 90 or 48 <= c <= 57 or c in (45, 95)):
            return None
    return name.decode("ascii")


def _sections(data, off, end):
    """[(nombre, cuerpo, fin)] de los hermanos en [off, end)."""
    out = []
    while off + DOT_SECTION_HEADER <= end:
        name = _section_name(data, off, end)
        if name is None:
            break
        size = struct.unpack_from("<I", data, off + DOT_NAME_FIELD)[0]
        body = off + DOT_SECTION_HEADER
        if body + size > end:
            break
        out.append((name, body, body + size))
        off = body + size
    return out


def _children(data, body, end):
    """Hijos de un contenedor, con o sin u32 de cuenta delante."""
    if _section_name(data, body, end) is not None:
        return _sections(data, body, end)
    if _section_name(data, body + 4, end) is not None:
        return _sections(data, body + 4, end)
    return []


def _root(sections, name):
    for sec in sections:
        if sec[0] == name:
            return sec
    return None


def name_table(data, sec):
    """Cuerpo de una seccion NAME-* o DEPENDENCIES: [u32 n][u32 bytes][cadenas]."""
    if sec is None or sec[2] - sec[1] < 8:
        return []
    count, blob = struct.unpack_from("<2I", data, sec[1])
    pos = sec[1] + 8
    end = min(sec[2], pos + blob)
    names = []
    while pos < end and len(names) < count:
        z = data.find(b"\x00", pos, end)
        if z < 0:
            z = end
        try:
            names.append(bytes(data[pos:z]).decode("ascii"))
        except Exception:
            names.append("")
        pos = z + 1
    return names


def declared_mesh_count(data):
    """La cuenta de NAME-MESHES: mayor que cero si y solo si hay MESHES.

    Medido en los 4319 .atc.bof del corpus: con esto cada plugin sabe si el
    archivo es suyo leyendo solo la cabecera.
    """
    for name, body, end in _sections(data, 0, len(data)):
        if name == "NAME-MESHES":
            if end - body < 4:
                return None
            return struct.unpack_from("<I", data, body)[0]
    return None


class DotTexture(object):
    """Un bloque TEXTURE: su nombre y, si va incrustada, donde estan los pixeles."""

    def __init__(self, index, name):
        self.index = index
        self.name = name
        self.width = 0
        self.height = 0
        self.format = 0
        self.mips = 0
        self.pixel = 0              # ultimo campo del TEXTURE-HEADER
        self.image = None           # (inicio, fin) del nivel 0

    @property
    def embedded(self):
        return self.image is not None

    def __repr__(self):
        return "<TEXTURE %s %dx%d fmt=%d %s>" % (
            self.name, self.width, self.height, self.format,
            "incrustada" if self.embedded else "externa")


def texture_entries(data):
    """Los bloques TEXTURE del archivo, en el orden de NAME-TEXTURES.

    El bloque i es NAME-TEXTURES[i]: en los 4246 archivos del corpus con
    texturas, los dos primeros campos del TEXTURE-HEADER valen i. Sin
    TEXTURE-IMAGE la textura es externa y vive en <nombre>.atc.bof.
    """
    roots = _sections(data, 0, len(data))
    names = name_table(data, _root(roots, "NAME-TEXTURES"))
    textures = _root(roots, "TEXTURES")
    out = []
    if textures is None:
        return out
    for pos, (name, body, end) in enumerate(_children(data, textures[1], textures[2])):
        if name != "TEXTURE":
            continue
        tex = DotTexture(len(out), names[len(out)] if len(out) < len(names) else "")
        if not tex.name:
            tex.name = "TEXTURE_%d" % tex.index
        for cname, cbody, cend in _sections(data, body, end):
            if cname == "TEXTURE-HEADER" and cend - cbody >= 28:
                fields = struct.unpack_from("<7I", data, cbody)
                tex.width, tex.height = fields[2], fields[3]
                tex.format, tex.mips, tex.pixel = fields[4], fields[5], fields[6]
            elif cname == "TEXTURE-IMAGE" and cend - cbody >= 4:
                # Niveles [u32 size][pixeles] seguidos; el 0 es el primero.
                size = struct.unpack_from("<I", data, cbody)[0]
                if cbody + 4 + size <= cend:
                    tex.image = (cbody + 4, cbody + 4 + size)
        out.append(tex)
    return out


# --------------------------------------------------------------------------
# ATC (Adreno Texture Compression)
#
# Bloques de 4x4. El de color son 8 bytes: c0 (u16, RGB555 y el bit alto elige
# el modo), c1 (u16, RGB565) y 16 indices de 2 bits, pixel i en los bits
# 32 + 2i. Con el bit alto a 0 la paleta es c0, (5c0+3c1)/8, (3c0+5c1)/8, c1;
# con el bit a 1, negro, c0 - c1/4, c0, c1. Asi decodifica texture2ddecoder,
# y tests/test_dot_text_plugin.py lo compara pixel a pixel.
#
# ATCA (explicito) antepone 8 bytes de alfa a 4 bits por pixel, estilo DXT3;
# ATCI (interpolado) antepone un bloque de alfa estilo DXT5. Los .atc del juego
# declaran ATCA; ATCI no aparece en el corpus pero sale gratis.
# --------------------------------------------------------------------------

_E5 = [(v << 3) | (v >> 2) for v in range(32)]
_E6 = [(v << 2) | (v >> 4) for v in range(64)]
_NIBBLE_PAIRS = [bytes(((b & 15) * 17, (b >> 4) * 17)) for b in range(256)]


def _atc_palette(c0, c1):
    """Los cuatro colores RGBA (opacos) de un bloque."""
    r1 = _E5[(c1 >> 11) & 31]
    g1 = _E6[(c1 >> 5) & 63]
    b1 = _E5[c1 & 31]
    r0 = _E5[(c0 >> 10) & 31]
    g0 = _E5[(c0 >> 5) & 31]
    b0 = _E5[c0 & 31]
    if c0 & 0x8000:
        return (b"\x00\x00\x00\xff",
                bytes((max(0, r0 - (r1 >> 2)), max(0, g0 - (g1 >> 2)),
                       max(0, b0 - (b1 >> 2)), 255)),
                bytes((r0, g0, b0, 255)),
                bytes((r1, g1, b1, 255)))
    return (bytes((r0, g0, b0, 255)),
            bytes(((5 * r0 + 3 * r1) >> 3, (5 * g0 + 3 * g1) >> 3,
                   (5 * b0 + 3 * b1) >> 3, 255)),
            bytes(((3 * r0 + 5 * r1) >> 3, (3 * g0 + 5 * g1) >> 3,
                   (3 * b0 + 5 * b1) >> 3, 255)),
            bytes((r1, g1, b1, 255)))


def _dxt5_alpha_rows(block):
    """Las cuatro filas de alfa (4 bytes cada una) de un bloque estilo DXT5."""
    a0 = block[0]
    a1 = block[1]
    if a0 > a1:
        pal = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    else:
        pal = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
    bits = int.from_bytes(bytes(block[2:8]), "little")
    rows = []
    for r in range(4):
        rows.append(bytes(pal[(bits >> (3 * (4 * r + c))) & 7] for c in range(4)))
    return rows


def decode_atc(raw, width, height, variant):
    """Pixeles RGBA32 (filas de arriba abajo) de una imagen ATC.

    *variant* es "ATC" (8 bytes por bloque), "ATCA" o "ATCI" (16 bytes).
    """
    bw = (width + 3) >> 2
    bh = (height + 3) >> 2
    step = 8 if variant == "ATC" else 16
    color_at = 0 if variant == "ATC" else 8
    need = bw * bh * step
    if len(raw) < need:
        raise ValueError("%s %dx%d necesita %d bytes y hay %d" % (
            variant, width, height, need, len(raw)))
    pw = bw * 4
    stride = pw * 4
    out = bytearray(pw * bh * 4 * 4)
    alpha = bytearray(pw * bh * 4) if variant != "ATC" else None
    unpack = struct.unpack_from
    cache = {}
    src = 0
    for by in range(bh):
        row0 = by * 4 * stride
        for bx in range(bw):
            key = bytes(raw[src + color_at:src + color_at + 8])
            rows = cache.get(key)
            if rows is None:
                c0, c1 = unpack("<HH", key, 0)
                pal = _atc_palette(c0, c1)
                rows = []
                for r in range(4):
                    b = key[4 + r]
                    rows.append(pal[b & 3] + pal[(b >> 2) & 3]
                                + pal[(b >> 4) & 3] + pal[b >> 6])
                cache[key] = rows
            o = row0 + bx * 16
            out[o:o + 16] = rows[0]
            out[o + stride:o + stride + 16] = rows[1]
            out[o + 2 * stride:o + 2 * stride + 16] = rows[2]
            out[o + 3 * stride:o + 3 * stride + 16] = rows[3]
            if alpha is not None:
                a = by * 4 * pw + bx * 4
                if variant == "ATCA":
                    for r in range(4):
                        alpha[a + r * pw:a + r * pw + 4] = (
                            _NIBBLE_PAIRS[raw[src + 2 * r]]
                            + _NIBBLE_PAIRS[raw[src + 2 * r + 1]])
                else:
                    arows = _dxt5_alpha_rows(raw[src:src + 8])
                    for r in range(4):
                        alpha[a + r * pw:a + r * pw + 4] = arows[r]
            src += step
    if alpha is not None:
        out[3::4] = alpha
    if pw != width or bh * 4 != height:
        cropped = bytearray()
        for y in range(height):
            cropped += out[y * stride:y * stride + width * 4]
        out = cropped
    return out


def texture_codec(tex):
    """Codec de una textura incrustada: "ATC", "ATCA" o "PVRTC4"."""
    if tex.pixel == DOT_PIXEL_PVRTC4:
        return "PVRTC4"
    if tex.format == DOT_FORMAT_ATCA:
        return "ATCA"
    if tex.format == DOT_FORMAT_ATC:
        return "ATC"
    # Formato desconocido: el tamano del nivel 0 decide (8 o 4 bits por pixel).
    size = tex.image[1] - tex.image[0]
    blocks = ((tex.width + 3) >> 2) * ((tex.height + 3) >> 2)
    return "ATCA" if size >= blocks * 16 else "ATC"


def decode_pvrtc4(raw, width, height):
    """PVRTC de 4 bits con el decodificador de Noesis; fuera de Noesis no hay."""
    if rapi is None:
        raise ValueError("PVRTC de 4 bits: solo se decodifica dentro de Noesis")
    return rapi.imageDecodePVRTC(bytes(raw), width, height, 4)


def decode_embedded(data, tex):
    """(ancho, alto, RGBA32) de una textura incrustada."""
    start, end = tex.image
    codec = texture_codec(tex)
    if codec == "PVRTC4":
        return tex.width, tex.height, decode_pvrtc4(data[start:end], tex.width, tex.height)
    return tex.width, tex.height, decode_atc(
        data[start:end], tex.width, tex.height, codec)


_DDS_FOURCC = {b"ATC ": "ATC", b"ATCA": "ATCA", b"ATCI": "ATCI"}


def decode_dds(dds):
    """(ancho, alto, RGBA32) del nivel 0 de un DDS con FourCC ATC."""
    if bytes(dds[:4]) != DDS_MAGIC:
        raise ValueError("no es un DDS")
    height, width = struct.unpack_from("<2I", dds, 12)
    fourcc = bytes(dds[84:88])
    variant = _DDS_FOURCC.get(fourcc)
    if variant is None:
        raise ValueError("FourCC %r no es ATC" % fourcc)
    return width, height, decode_atc(dds[128:], width, height, variant)


def file_textures(data):
    """[(nombre, ancho, alto, RGBA32)] de cualquier archivo de textura del juego.

    Un error en una textura no tumba las demas: se avisa por consola y se sigue.
    """
    kind = container_kind(data)
    if kind == "dds":
        width, height, rgba = decode_dds(data)
        return [("", width, height, rgba)]
    if kind == "nmg":
        width, height, rgba = decode_dds(_nmg_dds(data))
        return [("", width, height, rgba)]
    payload = bof_payload(data)
    if payload is None:
        return []
    out = []
    for tex in texture_entries(payload):
        if not tex.embedded:
            continue
        try:
            width, height, rgba = decode_embedded(payload, tex)
        except Exception as exc:
            print("DOT: %s no se pudo decodificar: %s" % (tex.name, exc))
            continue
        out.append((tex.name, width, height, rgba))
    return out


def is_flat(rgba):
    """True si todos los pixeles son iguales: el sintoma de un decodificador malo."""
    return len(rgba) >= 4 and rgba == rgba[:4] * (len(rgba) // 4)


# --------------------------------------------------------------------------
# Texturas externas: <nombre>.atc.bof junto a la malla o en Media/3d/Textures
# --------------------------------------------------------------------------

_DIR_INDEX = {}


def _stem(filename):
    low = filename.lower()
    if low.endswith(".atc.bof"):
        return low[:-8]
    if low.endswith(".atc"):
        return low[:-4]
    return None


def _index_dir(path, recursive):
    """{nombre en minusculas: ruta} de los .atc.bof y .atc de una carpeta."""
    key = (os.path.normcase(os.path.abspath(path)), recursive)
    if key in _DIR_INDEX:
        return _DIR_INDEX[key]
    index = {}
    try:
        if recursive:
            walk = os.walk(path)
        else:
            walk = [(path, [], os.listdir(path))]
        for folder, _dirs, files in walk:
            for filename in sorted(files):
                stem = _stem(filename)
                if stem is None:
                    continue
                prev = index.get(stem)
                # El .atc.bof original gana a una copia .atc descomprimida.
                if prev is None or (not prev.lower().endswith(".bof")
                                    and filename.lower().endswith(".bof")):
                    index[stem] = os.path.join(folder, filename)
    except (IOError, OSError):
        pass
    _DIR_INDEX[key] = index
    return index


def texture_dirs(base_dir, levels=6):
    """Donde buscar texturas externas: la carpeta de la malla y las Textures
    hermanas de sus antepasados (Media/3d/Textures para Media/3d/Troops/...)."""
    out = []
    if not base_dir:
        return out
    current = os.path.abspath(base_dir)
    out.append((current, False))
    for _ in range(levels):
        for name in ("Textures", "textures"):
            candidate = os.path.join(current, name)
            if os.path.isdir(candidate) and (candidate, True) not in out:
                out.append((candidate, True))
                break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return out


def find_texture_file(name, base_dir, dependency=None):
    """Ruta del archivo que trae la textura *name*, o None.

    *dependency* es el nombre de DEPENDENCIES (TD_RiverWater02.atc.bof), que
    conserva las mayusculas; NAME-TEXTURES las pierde.
    """
    stems = []
    if dependency:
        stem = _stem(os.path.basename(dependency.replace("\\", "/")))
        if stem:
            stems.append(stem)
    if name and name.lower() not in stems:
        stems.append(name.lower())
    for folder, recursive in texture_dirs(base_dir):
        index = _index_dir(folder, recursive)
        for stem in stems:
            path = index.get(stem)
            if path and os.path.isfile(path):
                return path
    return None


_RGBA_CACHE = []
_RGBA_CACHE_BYTES = 256 * 1024 * 1024


def _cache_get(key):
    for k, value in _RGBA_CACHE:
        if k == key:
            return value
    return None


def _cache_put(key, value):
    _RGBA_CACHE.append((key, value))
    total = 0
    for i in range(len(_RGBA_CACHE) - 1, -1, -1):
        total += len(_RGBA_CACHE[i][1][2])
        if total > _RGBA_CACHE_BYTES:
            del _RGBA_CACHE[:i + 1]
            break


def load_texture_file(path, name):
    """(ancho, alto, RGBA32) de la textura *name* dentro del archivo *path*.

    Si el archivo no la nombra, la primera que traiga: un TD_X.atc.bof suelto
    lleva una sola. Memoriza el resultado durante la sesion de Noesis.
    """
    try:
        stat = os.stat(path)
    except (IOError, OSError):
        return None
    key = (os.path.normcase(path), stat.st_size, stat.st_mtime, name.upper())
    hit = _cache_get(key)
    if hit is not None:
        return hit
    with open(path, "rb") as handle:
        data = handle.read()
    found = None
    kind = container_kind(data)
    if kind in ("dds", "nmg"):
        textures = file_textures(data)
        found = textures[0][1:] if textures else None
    else:
        payload = bof_payload(data)
        if payload is None:
            return None
        entries = [t for t in texture_entries(payload) if t.embedded]
        chosen = None
        for tex in entries:
            if tex.name.upper() == name.upper():
                chosen = tex
                break
        if chosen is None and entries:
            chosen = entries[0]
        if chosen is not None:
            found = decode_embedded(payload, chosen)
    if found is not None:
        _cache_put(key, found)
    return found


class TextureSource(object):
    """Resuelve por nombre las texturas que usa una malla.

    Orden: incrustada en el propio archivo, archivo del juego (.atc.bof/.atc)
    junto a la malla o en Media/3d/Textures, y por ultimo una imagen suelta
    (PNG/DDS/TGA/JPG) junto a la malla, que es lo que dejaba el antiguo
    mesh-export de ANANSI.
    """

    def __init__(self, data, base_dir):
        self.data = data
        self.base_dir = base_dir
        self.entries = {}
        for tex in texture_entries(data):
            self.entries[tex.name.upper()] = tex
        self.dependencies = {}
        roots = _sections(data, 0, len(data))
        for dep in name_table(data, _root(roots, "DEPENDENCIES")):
            stem = _stem(os.path.basename(dep.replace("\\", "/")))
            if stem:
                self.dependencies[stem.upper()] = dep
        self.textures = {}          # nombre en mayusculas -> NoeTexture o None
        self.sources = {}           # nombre en mayusculas -> de donde salio

    def display_name(self, name):
        """El nombre con sus mayusculas originales si DEPENDENCIES las trae."""
        dep = self.dependencies.get(name.upper())
        if dep:
            base = os.path.basename(dep.replace("\\", "/"))
            for ext in (".atc.bof", ".atc"):
                if base.lower().endswith(ext):
                    return base[:-len(ext)]
        return name

    def resolve(self, name):
        """(ancho, alto, RGBA32, origen) o None; origen describe de donde salio."""
        tex = self.entries.get(name.upper())
        if tex is not None and tex.embedded:
            width, height, rgba = decode_embedded(self.data, tex)
            return width, height, rgba, "incrustada"
        path = find_texture_file(
            name, self.base_dir, self.dependencies.get(name.upper()))
        if path:
            found = load_texture_file(path, name)
            if found is not None:
                return found[0], found[1], found[2], os.path.basename(path)
        return None

    def get(self, name):
        """NoeTexture de la textura *name* (cacheada por malla), o None."""
        key = name.upper()
        if key in self.textures:
            return self.textures[key]
        noe = None
        try:
            found = self.resolve(name)
        except Exception as exc:
            print("DOT: textura %s ilegible: %s" % (name, exc))
            found = None
        label = self.display_name(name)
        if found is not None:
            width, height, rgba, origin = found
            # Con la extension en el nombre, el FBX que exporta Noesis guarda
            # TD_X.png y no TD_X a secas: Blender lo encuentra junto al FBX.
            noe = NoeTexture(label + ".png", width, height, bytes(rgba),
                             noesis.NOESISTEX_RGBA32)
            self.sources[key] = origin
            if is_flat(rgba):
                print("DOT: aviso - %s sale de un solo color (%s)" % (label, origin))
        else:
            noe = self._loose_image(name, label)
        self.textures[key] = noe
        return noe

    def _loose_image(self, name, label):
        if rapi is None or not self.base_dir:
            return None
        for ext in (".png", ".dds", ".tga", ".jpg"):
            for stem in (label, name):
                for path in (os.path.join(self.base_dir, stem + ext),
                             os.path.join(self.base_dir, stem, stem + ext)):
                    if rapi.checkFileExists(path):
                        tex = rapi.loadExternalTex(path)
                        if tex:
                            tex.name = os.path.basename(path)
                            self.sources[name.upper()] = os.path.basename(path)
                            return tex
        return None


# --------------------------------------------------------------------------
# Noesis
# --------------------------------------------------------------------------


def noepyCheckType(data):
    """Texturas del juego. Las mallas (NAME-MESHES > 0) son de fmt_DOT.py."""
    if data is None or len(data) < 16:
        return 0
    kind = container_kind(data)
    if kind == "dds":
        return 1 if bytes(data[84:88]) in _DDS_FOURCC else 0
    if kind == "nmg":
        return 1
    if kind is None:
        return 0
    try:
        head = payload_head(data)
    except Exception:
        return 0
    if head is None or bytes(head[:8]) != DOT_MAGIC or head.find(ECHO_MARKER) < 0:
        return 0
    meshes = declared_mesh_count(head)
    if meshes is None:
        # La cabecera no cupo en el trozo leido: hace falta el archivo entero.
        payload = bof_payload(data)
        meshes = declared_mesh_count(payload) if payload is not None else None
    return 1 if meshes == 0 else 0


def noepyLoadRGBA(data, texList):
    textures = file_textures(data)
    if not textures:
        print("DOT: el archivo no trae texturas legibles")
        return 0
    base = ""
    try:
        base = rapi.getExtensionlessName(rapi.getLocalFileName(rapi.getInputName()))
        if base.lower().endswith(".atc"):
            base = base[:-4]
    except Exception:
        pass
    for name, width, height, rgba in textures:
        label = name or base or "textura"
        texList.append(NoeTexture(label, width, height, bytes(rgba),
                                  noesis.NOESISTEX_RGBA32))
        flat = " (un solo color: revisar)" if is_flat(rgba) else ""
        print("DOT: %s %dx%d%s" % (label, width, height, flat))
    return 1
