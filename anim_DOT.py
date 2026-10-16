# Dawn of Titans - animaciones de RuntimeNetwork (NaturalMotion morpheme)
#
# El .atc de un personaje trae, como mucho, un clip en SCENE-ANIMATIONS. Todo
# lo demas -andar, atacar, morir, celebrar- lo reproduce morpheme, y vive
# aparte, en Media/RuntimeNetwork:
#
#     RuntimeNetwork/<modelo>/<modelo>.nmb          la red: esqueleto, mapas
#                                                   de canales y lista de clips
#     RuntimeNetwork/<modelo>/<clip>.nsa.redirect   una linea de texto con la
#                                                   ruta del clip, relativa a
#                                                   la raiz del juego
#     RuntimeNetwork/<juego de clips>/<clip>.nsa    el clip comprimido (NSA)
#
# Los .nsa se comparten: MagicianCoreMaleAnimation_Game sirve a todos los
# magos. Por eso un clip no dice a que hueso va cada canal; eso lo dice el
# RigToAnimMap del .nmb, que lleva cada canal del clip a un hueso del esqueleto
# de morpheme, y de ahi se pasa por nombre al esqueleto del .atc.
#
# Las estructuras son las del runtime de morpheme 4 en 64 bits: el .nmb es un
# "simple bundle" de recursos con cabecera de 48 bytes, y los punteros de cada
# recurso son desplazamientos desde el propio objeto. Medido sobre los 61
# clips de MagicianCoreMaleAnimation_Game: cabecera, mapas de canales,
# secciones y datos constantes cuadran byte a byte con lo que declaran.
#
# Los comentarios en ASCII sin acentos son a proposito, como en fmt_DOT.py: el
# interprete es el Python 3.2 que Noesis lleva dentro (sin f-strings).
#
# Instalar: copiar anim_DOT.py junto a fmt_DOT.py en Noesis/plugins/python/.

import os
import struct

try:
    from inc_noesis import *
    import noesis
except ImportError:
    # Fuera de Noesis (pruebas, arneses): queda la biblioteca sin el registro.
    noesis = None


def registerNoesisTypes():
    # Biblioteca de fmt_DOT.py: no abre nada por si misma.
    return 1


# --------------------------------------------------------------------------
# El bundle (.nmb)
#
# Una tira de recursos, cada uno con esta cabecera:
#
#     0   u32  version del runtime (0x18)
#     4   u32  plataforma: 2 little endian | 8 de 64 bits (el _32.nmb lleva 4)
#     8   u32  tipo de recurso
#     12  u32  id del recurso
#     16  16   guid
#     32  u64  tamano del cuerpo
#     40  u32  alineacion del cuerpo (+4 de relleno)
#
# El cuerpo empieza en la siguiente direccion alineada tras la cabecera.
# --------------------------------------------------------------------------

NMB_HEADER = 48
NMB_64BIT = 8

NMB_RIG = 1
NMB_RIG_TO_ANIM = 2
NMB_ANIM_LOOKUP = 13

RIG_TO_ANIM_PAIRS = 0
RIG_TO_ANIM_TABLE = 1


class NmbAsset(object):
    def __init__(self, kind, asset_id, body, size):
        self.kind = kind
        self.asset_id = asset_id
        self.body = body
        self.size = size
        self.end = body + size

    def __repr__(self):
        return "<recurso %d id=%08x en %x (%d bytes)>" % (
            self.kind, self.asset_id, self.body, self.size)


def bundle_assets(data):
    """Los recursos del bundle, en orden. Lista vacia si no es de 64 bits."""
    out = []
    off = 0
    while off + NMB_HEADER <= len(data):
        _ver, platform, kind, asset_id = struct.unpack_from("<4I", data, off)
        if not platform & NMB_64BIT:
            break
        size, align = struct.unpack_from("<QI", data, off + 32)
        body = off + NMB_HEADER
        if align > 1:
            body = (body + align - 1) // align * align
        if size <= 0 or body + size > len(data):
            break
        out.append(NmbAsset(kind, asset_id, body, size))
        off = body + size
    return out


def _cstr(data, start, end):
    z = data.find(b"\x00", start, end)
    if z < 0:
        z = end
    return data[start:z].decode("ascii", "replace")


def string_table(data, off, end):
    """[u32 cuenta][u32 bytes][u64 -> desplazamientos][u64 -> texto].

    Es la NMP::IDMappedStringTable de morpheme tal como la escribe este juego:
    sin la tabla de ids, que seria la identidad.
    """
    if off + 24 > end:
        return []
    count, length = struct.unpack_from("<2I", data, off)
    offsets_ptr, text_ptr = struct.unpack_from("<2Q", data, off + 8)
    offsets = off + offsets_ptr
    text = off + text_ptr
    if offsets + count * 4 > end or text + length > end:
        return []
    names = []
    for i in range(count):
        o = struct.unpack_from("<I", data, offsets + i * 4)[0]
        names.append(_cstr(data, text + o, text + length) if o < length else "")
    return names


# --------------------------------------------------------------------------
# El esqueleto de morpheme (AnimRigDef)
#
#     0    4f   orientacion del marco de mezcla
#     16   4f   traslacion del marco de mezcla
#     32   u64  -> jerarquia
#     40   u32  hueso de la trayectoria
#     44   u32  hueso raiz del personaje
#     48   u64  -> nombres de los huesos
#     56   u64  -> pose de reparto
#
# El hueso 0 es CharacterWorldSpaceTM y el ultimo, Trajectory: ninguno de los
# dos esta en el .atc. Los demas se llaman igual que en NAME-JOINTS.
# --------------------------------------------------------------------------


class MorphemeRig(object):
    def __init__(self, asset_id, names, parents, trajectory, root):
        self.asset_id = asset_id
        self.names = names
        self.parents = parents      # padre de cada hueso, o None si no se lee
        self.trajectory = trajectory
        self.root = root

    def parent_name(self, bone):
        if self.parents is None or bone >= len(self.parents) or self.parents[bone] < 0:
            return None
        return self.names[self.parents[bone]]

    def __repr__(self):
        return "<rig %08x %d huesos>" % (self.asset_id, len(self.names))


def _rig_parents(data, off, end, count):
    """La jerarquia: [u32 cuenta] y, 16 bytes despues, un i32 por hueso.

    El puntero de la tabla viene a cero en estos .nmb; la tabla va justo
    detras de la cabecera. Solo se da por buena si es un arbol bien formado.
    """
    if off + 16 + count * 4 > end:
        return None
    if struct.unpack_from("<I", data, off)[0] != count:
        return None
    parents = list(struct.unpack_from("<%di" % count, data, off + 16))
    if not parents or parents[0] != -1:
        return None
    for i in range(1, count):
        if not (0 <= parents[i] < i):
            return None
    return parents


def parse_rig(data, asset):
    if asset.size < 64:
        return None
    hier_ptr = struct.unpack_from("<Q", data, asset.body + 32)[0]
    trajectory, root = struct.unpack_from("<2I", data, asset.body + 40)
    names_ptr = struct.unpack_from("<Q", data, asset.body + 48)[0]
    names = string_table(data, asset.body + names_ptr, asset.end)
    if not names:
        return None
    parents = _rig_parents(data, asset.body + hier_ptr, asset.end, len(names))
    return MorphemeRig(asset.asset_id, names, parents, trajectory, root)


# --------------------------------------------------------------------------
# RigToAnimMap: que hueso anima cada canal del clip
#
#     0    u32  tipo: 0 parejas (hueso, canal), 1 tabla canal -> hueso
#     8    u64  -> BitArray de huesos usados ([u32 bits][u32 palabras][...])
#     16   u64  tamano de los datos  (+ u32 alineacion + relleno)
#     32   u64  -> datos
#
# La tabla (AnimToRigTableMap) es [u32 canales][u16 usados][u16 del LOD]
# [u64 -> entradas], y las entradas son un u16 por canal con el hueso de
# destino. El hueso 0 hace de sumidero: un canal que va ahi no anima nada.
# --------------------------------------------------------------------------


class RigToAnimMap(object):
    def __init__(self, asset_id, rig_bones, anim_to_rig):
        self.asset_id = asset_id
        self.rig_bones = rig_bones          # huesos del rig al que sirve
        self.anim_to_rig = anim_to_rig      # hueso del rig por canal del clip

    @property
    def channels(self):
        return len(self.anim_to_rig)

    def __repr__(self):
        return "<rig->anim %08x %d canales, rig de %d huesos>" % (
            self.asset_id, self.channels, self.rig_bones)


def parse_rig_to_anim(data, asset):
    body = asset.body
    if asset.size < 40:
        return None
    kind = struct.unpack_from("<I", data, body)[0]
    flags_ptr = struct.unpack_from("<Q", data, body + 8)[0]
    data_ptr = struct.unpack_from("<Q", data, body + 32)[0]
    rig_bones = struct.unpack_from("<I", data, body + flags_ptr)[0]
    table = body + data_ptr
    if table + 16 > asset.end:
        return None
    if kind == RIG_TO_ANIM_TABLE:
        count = struct.unpack_from("<I", data, table)[0]
        entries = table + struct.unpack_from("<Q", data, table + 8)[0]
        if entries + count * 2 > asset.end:
            return None
        anim_to_rig = list(struct.unpack_from("<%dH" % count, data, entries))
    elif kind == RIG_TO_ANIM_PAIRS:
        count = struct.unpack_from("<I", data, table)[0]
        pairs = table + struct.unpack_from("<Q", data, table + 8)[0]
        if pairs + count * 4 > asset.end:
            return None
        found = {}
        for i in range(count):
            rig, chan = struct.unpack_from("<2H", data, pairs + i * 4)
            found[chan] = rig
        anim_to_rig = [found.get(c, 0) for c in range(max(found) + 1 if found else 0)]
    else:
        return None
    return RigToAnimMap(asset.asset_id, rig_bones, anim_to_rig)


# --------------------------------------------------------------------------
# La lista de clips (SimpleAnimRuntimeIDtoFilenameLookup)
#
# Cinco punteros: nombres de archivo, formato ("nsa"), archivo de origen
# (el .xmd de la herramienta), toma de origen y CRC. Las cuatro primeras son
# tablas de cadenas con una entrada por id de clip.
#
# Cada clip aparece una vez por juego de animacion: con dos rigs, la misma
# animacion sale dos veces con ids y nombres de archivo distintos, y los dos
# .redirect llevan al mismo .nsa.
# --------------------------------------------------------------------------


class ClipEntry(object):
    def __init__(self, index, filename, fmt, source, take):
        self.index = index
        self.filename = filename
        self.format = fmt
        self.source = source
        self.take = take

    @property
    def title(self):
        """El nombre que le dio el animador: el del .xmd, sin ruta."""
        base = self.source.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in base:
            base = base.rsplit(".", 1)[0]
        if not base:
            base = self.filename.rsplit(".", 1)[0]
        if self.take and self.take.lower() != "untitled":
            base += "_" + self.take
        return base

    def __repr__(self):
        return "<clip %d %s>" % (self.index, self.filename)


def parse_anim_lookup(data, asset):
    body = asset.body
    if asset.size < 40:
        return []
    ptrs = struct.unpack_from("<4Q", data, body)
    tables = [string_table(data, body + p, asset.end) for p in ptrs]
    files, formats, sources, takes = tables
    out = []
    for i, filename in enumerate(files):
        out.append(ClipEntry(
            i, filename,
            formats[i] if i < len(formats) else "",
            sources[i] if i < len(sources) else "",
            takes[i] if i < len(takes) else ""))
    return out


class MorphemeNetwork(object):
    """Lo que hace falta del .nmb: rigs, mapas de canales y clips."""

    def __init__(self, path, data):
        self.path = path
        self.rigs = []
        self.maps = []
        self.clips = []
        for asset in bundle_assets(data):
            if asset.kind == NMB_RIG:
                rig = parse_rig(data, asset)
                if rig is not None:
                    self.rigs.append(rig)
            elif asset.kind == NMB_RIG_TO_ANIM:
                mapping = parse_rig_to_anim(data, asset)
                if mapping is not None:
                    self.maps.append(mapping)
            elif asset.kind == NMB_ANIM_LOOKUP:
                self.clips = parse_anim_lookup(data, asset)

    def __repr__(self):
        return "<red %d rig(s), %d mapa(s), %d clip(s)>" % (
            len(self.rigs), len(self.maps), len(self.clips))


# --------------------------------------------------------------------------
# El clip (AnimSourceNSA)
#
# Cabecera, en 64 bits:
#
#     0x20  f32  duracion (s)            0x24  f32  muestras por segundo
#     0x28  u32  canales                 0x2c  u32  secciones por fotogramas
#     0x30  u32  secciones por canales   0x34  u32  maximo de canales
#     0x38  u64  -> canales constantes de posicion
#     0x40  u64  -> canales constantes de giro
#     0x48  u64  -> punteros a los canales muestreados de posicion (uno por
#                   seccion de canales)
#     0x50  u64  -> idem de giro
#     0x58  6f   cuantizacion de las medias de posicion (desplazamiento, escala)
#     0x70  u32  juegos de cuantizacion de posicion
#     0x74  u32  idem de giro
#     0x78  u64  -> juegos de posicion (6f cada uno)
#     0x80  u64  -> juegos de giro
#     0x88  u64  -> datos constantes
#     0x90  u32  mayor seccion
#     0x98  u64  -> fotograma inicial de cada seccion (+1 al final)
#     0xa0  u64  -> tamano de cada seccion
#     0xa8  u64  -> puntero a cada seccion (fotogramas x canales)
#     0xb0  u64  -> trayectoria
#     0xb8  u64  -> nombres de los canales (casi siempre nulo)
#
# Un mapa de canales es [u16 cuenta][u16 canal...]: la posicion en el mapa es
# el indice del dato comprimido y el valor, el canal del clip.
#
# Las posiciones constantes son u16 x3 por la cuantizacion comun; las
# muestreadas, 11:11:10 bits por la cuantizacion de su juego mas una media por
# canal. Los giros se guardan como vector de rotacion de tangente del cuarto
# de angulo (TQA), que vuelve a cuaternion sin raices ni trigonometria; los
# muestreados son relativos a una media por canal, tambien en TQA.
# --------------------------------------------------------------------------

NSA_HEADER = 0xc0


class NsaError(ValueError):
    pass


def _tqa_quat(x, y, z):
    """Vector de rotacion de tangente del cuarto de angulo -> (x, y, z, w)."""
    mag2 = x * x + y * y + z * z
    opm2 = 1.0 + mag2
    fac = 2.0 / opm2
    return (x * fac, y * fac, z * fac, (1.0 - mag2) / opm2)


def _quat_mul(a, b):
    """Producto de Hamilton a * b, con los cuaterniones en orden x y z w."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by + ay * bw + az * bx - ax * bz,
        aw * bz + az * bw + ax * by - ay * bx,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _comp_map(data, off):
    count = struct.unpack_from("<H", data, off)[0]
    return list(struct.unpack_from("<%dH" % count, data, off + 2)) if count else []


def _align4(n):
    return (n + 3) & ~3


class NsaClip(object):
    """Un clip ya descomprimido: posicion y giro de cada canal por fotograma.

    pos[c] y quat[c] son listas con un valor por fotograma, o con uno solo si
    el canal no cambia. trajectory es [(cuaternion, traslacion)] por fotograma,
    o None si el clip no la trae.
    """

    def __init__(self, duration, fps, channels, frames):
        self.duration = duration
        self.fps = fps
        self.channels = channels
        self.frames = frames
        self.pos = [None] * channels
        self.quat = [None] * channels
        self.trajectory = None

    def sampled_pos(self, channel):
        """True si la traslacion del canal se mueve a lo largo del clip."""
        pos = self.pos[channel]
        return pos is not None and len(pos) > 1

    def key(self, channel, frame):
        """(cuaternion, traslacion) local del canal en el fotograma."""
        pos = self.pos[channel]
        quat = self.quat[channel]
        p = pos[frame if len(pos) > 1 else 0] if pos else (0.0, 0.0, 0.0)
        q = quat[frame if len(quat) > 1 else 0] if quat else (0.0, 0.0, 0.0, 1.0)
        return q, p

    def __repr__(self):
        return "<nsa %d canales, %d fotogramas a %g fps>" % (
            self.channels, self.frames, self.fps)


def nsa_header(data):
    """(duracion, fps, canales, fotogramas) o NsaError si no es un NSA.

    Un clip sin secciones es una pose: todos sus canales son constantes. Los
    hay de 800 bytes -las miras de RangedAttackAim*- que duran un fotograma.
    """
    if len(data) < NSA_HEADER:
        raise NsaError("demasiado corto")
    duration, fps = struct.unpack_from("<2f", data, 0x20)
    channels, frame_secs, chan_secs = struct.unpack_from("<3I", data, 0x28)
    if channels == 0:
        # Hay 19 en el juego, de 304 bytes: TiltHead* de las tiendas y tres
        # de la hidra. Solo cabecera.
        raise NsaError("clip vacio, sin canales")
    if not (0.0 < fps <= 1000.0) or channels > 4096:
        raise NsaError("cabecera fuera de rango")
    # Muchos clips guardan 29.999998: el exportador de glTF de Noesis trunca
    # a 29 y el clip sale un 3 % mas lento (Walk: 1,62 s en vez de 1,57).
    if abs(fps - round(fps)) < 1e-3:
        fps = float(round(fps))
    if not (0.0 <= duration <= 3600.0):
        raise NsaError("duracion fuera de rango")
    if frame_secs > 4096 or chan_secs > 4096:
        raise NsaError("secciones fuera de rango")
    if frame_secs == 0 or chan_secs == 0:
        return duration, fps, channels, max(1, int(round(duration * fps)) + 1)
    starts_ptr = struct.unpack_from("<Q", data, 0x98)[0]
    if starts_ptr + (frame_secs + 1) * 4 > len(data):
        raise NsaError("tabla de secciones fuera del archivo")
    starts = struct.unpack_from("<%dI" % (frame_secs + 1), data, starts_ptr)
    return duration, fps, channels, starts[-1] + 1


def decode_nsa(data):
    """El clip entero, fotograma a fotograma. NsaError si algo no cuadra."""
    duration, fps, channels, frames = nsa_header(data)
    frame_secs, chan_secs = struct.unpack_from("<2I", data, 0x2c)
    clip = NsaClip(duration, fps, channels, frames)
    size = len(data)

    def ptr(off):
        value = struct.unpack_from("<Q", data, off)[0]
        if value <= 0 or value >= size:
            raise NsaError("puntero fuera del archivo en 0x%x" % off)
        return value

    # ---- canales constantes
    upos_map = _comp_map(data, ptr(0x38))
    uquat_map = _comp_map(data, ptr(0x40))
    ud = ptr(0x88)
    upos_n, uquat_n = struct.unpack_from("<2I", data, ud)
    if upos_n != len(upos_map) or uquat_n != len(uquat_map):
        raise NsaError("los datos constantes no cuadran con sus mapas")
    po = struct.unpack_from("<6f", data, ud + 8)
    qo = struct.unpack_from("<6f", data, ud + 32)
    upos_ptr, uquat_ptr = struct.unpack_from("<2Q", data, ud + 56)
    for k, chan in enumerate(upos_map):
        x, y, z = struct.unpack_from("<3H", data, ud + upos_ptr + k * 6)
        if chan < channels:
            clip.pos[chan] = [(x * po[3] + po[0], y * po[4] + po[1], z * po[5] + po[2])]
    for k, chan in enumerate(uquat_map):
        x, y, z = struct.unpack_from("<3H", data, ud + uquat_ptr + k * 6)
        if chan < channels:
            clip.quat[chan] = [_tqa_quat(
                x * qo[3] + qo[0], y * qo[4] + qo[1], z * qo[5] + qo[2])]

    clip.trajectory = decode_trajectory(data, frames)
    if frame_secs == 0 or chan_secs == 0:
        return clip

    # ---- canales muestreados
    pos_maps_tbl = ptr(0x48)
    quat_maps_tbl = ptr(0x50)
    pos_maps = []
    quat_maps = []
    for cs in range(chan_secs):
        pos_maps.append(_comp_map(data, ptr(pos_maps_tbl + cs * 8)))
        quat_maps.append(_comp_map(data, ptr(quat_maps_tbl + cs * 8)))
    means = struct.unpack_from("<6f", data, 0x58)
    pos_sets_n, quat_sets_n = struct.unpack_from("<2I", data, 0x70)
    pos_sets = []
    quat_sets = []
    if pos_sets_n:
        base = ptr(0x78)
        pos_sets = [struct.unpack_from("<6f", data, base + i * 24) for i in range(pos_sets_n)]
    if quat_sets_n:
        base = ptr(0x80)
        quat_sets = [struct.unpack_from("<6f", data, base + i * 24) for i in range(quat_sets_n)]
    starts = struct.unpack_from("<%dI" % (frame_secs + 1), data, ptr(0x98))
    refs = ptr(0xa8)

    sampled_pos = {}
    sampled_quat = {}
    for fs in range(frame_secs):
        first = starts[fs]
        count = starts[fs + 1] - first + 1
        for cs in range(chan_secs):
            sec = ptr(refs + (fs * chan_secs + cs) * 8)
            n_frames, n_pos, n_quat = struct.unpack_from("<3I", data, sec)
            if n_frames != count:
                raise NsaError("la seccion %d,%d declara %d fotogramas y no %d"
                               % (fs, cs, n_frames, count))
            if n_pos != len(pos_maps[cs]) or n_quat != len(quat_maps[cs]):
                raise NsaError("la seccion %d,%d no cuadra con sus mapas" % (fs, cs))
            pos_data, pos_q, quat_data, quat_q = struct.unpack_from("<4Q", data, sec + 16)
            # La ultima muestra de una seccion es la primera de la siguiente.
            last = count if fs == frame_secs - 1 else count - 1
            if n_pos:
                _sampled_pos(data, sec + pos_data, sec + pos_q, n_pos, last,
                             means, pos_sets, pos_maps[cs], sampled_pos)
            if n_quat:
                _sampled_quat(data, sec + quat_data, sec + quat_q, n_quat, last,
                              quat_sets, quat_maps[cs], sampled_quat)

    for chan, keys in sampled_pos.items():
        if chan < channels:
            clip.pos[chan] = keys
    for chan, keys in sampled_quat.items():
        if chan < channels:
            clip.quat[chan] = keys
    for chan in range(channels):
        for keys in (clip.pos[chan], clip.quat[chan]):
            if keys is not None and len(keys) not in (1, frames):
                raise NsaError("el canal %d tiene %d fotogramas de %d"
                               % (chan, len(keys), frames))
    return clip


# --------------------------------------------------------------------------
# La trayectoria (TrajectorySourceNSA)
#
# Morpheme saca el desplazamiento del personaje del clip: la cadera va
# relativa a la trayectoria, y la trayectoria lleva por su cuenta cuanto se
# ha movido y girado el personaje desde el primer fotograma. Medido en
# Magician_Core_Game_Walk: la cadera se queda en el sitio y la trayectoria
# avanza 1,47 m en Z; en TurnLeft90 gira 88 grados sobre Y.
#
#     0    u32  tipo (2)
#     16   u64  tamano (+ alineacion)
#     32   f32  muestras por segundo     36  u32  fotogramas
#     40   6f   cuantizacion de las posiciones (desplazamiento, escala)
#     64   6f   cuantizacion de los giros (TQA)
#     88   u64  -> posiciones, 11:11:10 bits por fotograma (0 = constante)
#     96   u64  -> giros, u16 x3 por fotograma (0 = constante)
#
# Un canal constante se guarda en la propia cuantizacion: la posicion son los
# tres primeros floats y el giro, los cuatro primeros (x y z w).
# --------------------------------------------------------------------------


def decode_trajectory(data, frames):
    """[(cuaternion, traslacion)] por fotograma, o None si no hay."""
    base = struct.unpack_from("<Q", data, 0xb0)[0]
    if base <= 0 or base + 104 > len(data):
        return None
    count = struct.unpack_from("<I", data, base + 36)[0]
    if count <= 0:
        return None
    pi = struct.unpack_from("<6f", data, base + 40)
    qi = struct.unpack_from("<6f", data, base + 64)
    pos_ptr, quat_ptr = struct.unpack_from("<2Q", data, base + 88)
    if pos_ptr and base + pos_ptr + count * 4 > len(data):
        return None
    if quat_ptr and base + quat_ptr + count * 6 > len(data):
        return None
    out = []
    for f in range(frames):
        k = min(f, count - 1)
        if pos_ptr:
            v = struct.unpack_from("<I", data, base + pos_ptr + k * 4)[0]
            pos = ((v >> 21) * pi[3] + pi[0],
                   ((v >> 10) & 0x7ff) * pi[4] + pi[1],
                   (v & 0x3ff) * pi[5] + pi[2])
        else:
            pos = (pi[0], pi[1], pi[2])
        if quat_ptr:
            x, y, z = struct.unpack_from("<3H", data, base + quat_ptr + k * 6)
            quat = _tqa_quat(x * qi[3] + qi[0], y * qi[4] + qi[1], z * qi[5] + qi[2])
        else:
            quat = (qi[0], qi[1], qi[2], qi[3])
        out.append((quat, pos))
    return out


def trajectory_moves(clip, eps=1e-4):
    """True si la trayectoria del clip se mueve o gira."""
    if not clip.trajectory:
        return False
    q0, p0 = clip.trajectory[0]
    for q, p in clip.trajectory:
        if (abs(p[0] - p0[0]) > eps or abs(p[1] - p0[1]) > eps or abs(p[2] - p0[2]) > eps
                or abs(q[0] - q0[0]) > eps or abs(q[1] - q0[1]) > eps
                or abs(q[2] - q0[2]) > eps):
            return True
    return False


def _sampled_pos(data, keys, qdata, n, frames, means, sets, chan_map, out):
    stride = _align4(4 * n)
    for k in range(n):
        mx, my, mz, sx, sy, sz = struct.unpack_from("<6B", data, qdata + k * 6)
        bx = mx * means[3] + means[0]
        by = my * means[4] + means[1]
        bz = mz * means[5] + means[2]
        ox, scx = sets[sx][0], sets[sx][3]
        oy, scy = sets[sy][1], sets[sy][4]
        oz, scz = sets[sz][2], sets[sz][5]
        bx += ox
        by += oy
        bz += oz
        track = out.setdefault(chan_map[k], [])
        off = keys + k * 4
        for f in range(frames):
            v = struct.unpack_from("<I", data, off + f * stride)[0]
            track.append(((v >> 21) * scx + bx,
                          ((v >> 10) & 0x7ff) * scy + by,
                          (v & 0x3ff) * scz + bz))


def _sampled_quat(data, keys, qdata, n, frames, sets, chan_map, out):
    stride = _align4(6 * n)
    step = 2.0 / 255.0
    for k in range(n):
        mx, my, mz, sx, sy, sz = struct.unpack_from("<6B", data, qdata + k * 6)
        mean = _tqa_quat(mx * step - 1.0, my * step - 1.0, mz * step - 1.0)
        ox, scx = sets[sx][0], sets[sx][3]
        oy, scy = sets[sy][1], sets[sy][4]
        oz, scz = sets[sz][2], sets[sz][5]
        track = out.setdefault(chan_map[k], [])
        off = keys + k * 6
        for f in range(frames):
            x, y, z = struct.unpack_from("<3H", data, off + f * stride)
            track.append(_quat_mul(mean, _tqa_quat(
                x * scx + ox, y * scy + oy, z * scz + oz)))


# --------------------------------------------------------------------------
# Encontrar la red de un modelo y sus clips
# --------------------------------------------------------------------------


def model_stem(path):
    """SK_Foo_Game de .../SK_Foo_Game.atc.bof (o .atc, .dotmesh)."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    lower = base.lower()
    for ext in (".bof", ".atc", ".dotmesh"):
        if lower.endswith(ext):
            base = base[:-len(ext)]
            lower = lower[:-len(ext)]
    return base


def find_network(model_path):
    """(ruta del .nmb, raiz del juego) del modelo, o (None, None).

    La raiz es la carpeta que contiene Media: las rutas de los .redirect son
    relativas a ella.
    """
    if not model_path:
        return None, None
    path = model_path.replace("\\", "/")
    stem = model_stem(path)
    parts = path.split("/")[:-1]
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() != "media":
            continue
        media = "/".join(parts[:i + 1])
        nmb = "%s/RuntimeNetwork/%s/%s.nmb" % (media, stem, stem)
        if os.path.isfile(nmb):
            return nmb, "/".join(parts[:i])
    return None, None


def resolve_clip_file(folder, root, filename):
    """La ruta real del clip: junto a la red o a traves de su .redirect."""
    direct = folder + "/" + filename
    if os.path.isfile(direct):
        return direct
    redirect = direct + ".redirect"
    if not os.path.isfile(redirect):
        return None
    with open(redirect, "rb") as f:
        target = f.read().decode("ascii", "replace").strip().strip("\x00")
    target = target.replace("\\", "/")
    for candidate in (root + "/" + target, folder + "/" + target.rsplit("/", 1)[-1]):
        if os.path.isfile(candidate):
            return candidate
    return None


def map_score(rig, mapping, wanted):
    """Cuantos canales del mapa van a huesos que el modelo tiene."""
    score = 0
    for bone in mapping.anim_to_rig:
        if 0 < bone < len(rig.names) and rig.names[bone].upper() in wanted:
            score += 1
    return score


def mapping_candidates(network, joint_names):
    """[(puntos, rig, mapa)] de cada mapa con cada rig al que puede servir.

    Un RigToAnimMap no dice de que rig es; solo cuantos huesos tiene ese rig.
    Una red suele traer dos rigs -uno por juego de animacion- y en las de
    tienda cada uno anima clips distintos (los IdleTick de la tienda van con
    el segundo). Cada clip se queda con el par que, con sus canales, lleve mas
    canales a huesos que el modelo tiene. Medido en las 500 redes de 64 bits:
    dentro de un rig, dos mapas con el mismo numero de canales son siempre
    identicos, asi que el numero de canales basta para elegir el mapa.
    """
    wanted = set(name.upper() for name in joint_names)
    out = []
    for rig in network.rigs:
        for mapping in network.maps:
            if mapping.rig_bones == len(rig.names):
                out.append((map_score(rig, mapping, wanted), rig, mapping))
    return out


class LoadedClip(object):
    """Un clip listo para pasarle a Noesis, con el rig y el mapa que usa."""

    def __init__(self, name, path, clip, rig, mapping):
        self.name = name
        self.path = path
        self.clip = clip
        self.rig = rig
        self.mapping = mapping

    def __repr__(self):
        return "<%s %r>" % (self.name, self.clip)


def load_network_clips(model_path, joint_names, log=None):
    """(red, [LoadedClip]) con todas las animaciones del modelo.

    *joint_names* son los nombres del esqueleto del .atc; sirven para elegir
    rig y mapa. Cada .nsa se carga una vez aunque la red lo nombre dos veces.
    *log* recibe lineas de texto para la consola de Noesis. La red es None si
    el modelo no tiene.
    """
    def say(msg):
        if log is not None:
            log(msg)

    nmb, root = find_network(model_path)
    if nmb is None:
        return None, []
    with open(nmb, "rb") as f:
        network = MorphemeNetwork(nmb, f.read())
    candidates = mapping_candidates(network, joint_names)
    if not candidates:
        say("%s: sin rig ni mapa de canales utilizable" % nmb)
        return network, []
    folder = nmb.rsplit("/", 1)[0]

    seen = {}
    names = {}
    out = []
    skipped = {}
    for entry in network.clips:
        if entry.format and entry.format.lower() != "nsa":
            skipped[entry.format] = skipped.get(entry.format, 0) + 1
            continue
        path = resolve_clip_file(folder, root, entry.filename)
        if path is None:
            skipped["sin archivo"] = skipped.get("sin archivo", 0) + 1
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen[key] = True
        try:
            with open(path, "rb") as f:
                clip = decode_nsa(f.read())
        except (NsaError, struct.error) as exc:
            say("%s: no se pudo leer (%s)" % (entry.filename, exc))
            continue
        best = None
        for score, rig, mapping in candidates:
            if mapping.channels == clip.channels and (best is None or score > best[0]):
                best = (score, rig, mapping)
        if best is None or best[0] == 0:
            say("%s: %d canales y ningun mapa de ese tamano"
                % (entry.filename, clip.channels))
            continue
        name = entry.title
        if name in names:
            names[name] += 1
            name = "%s_%d" % (name, names[name])
        else:
            names[name] = 1
        out.append(LoadedClip(name, path, clip, best[1], best[2]))
    for reason in sorted(skipped):
        say("%d clip(s) sin cargar: %s" % (skipped[reason], reason))
    out.sort(key=lambda c: c.name.lower())
    return network, out
