# Dawn of Titans - Echo BOF format 1.0 (NaturalMotion / Echo engine)
#
# Abre las mallas descomprimidas con sus UVs, sus texturas y su skinning.
# El formato es un arbol TLV autodescriptivo, asi que aqui no se adivina nada:
# todo -- cuantas mallas hay, como se llaman, donde empieza cada buffer y con
# que stride -- se lee del propio archivo. El plan y las medidas estan en
# docs/dawn-of-titans-mesh-plan.md, y scripts/dot_mesh_coverage.py comprueba
# que lo declarado y lo cargado cuadran.
#
# Los comentarios en ASCII sin acentos son a proposito: este archivo lo ejecuta
# el interprete que Noesis lleva dentro, no el del repositorio.
#
# Instalar: copiar fmt_DOT.py y text_DOT.py a Noesis/plugins/python/
# (sobrescribiendo las copias viejas) y reiniciar Noesis.
# Abrir: el .atc.bof del juego tal cual, el .atc descomprimido o un .dotmesh
# (los mismos bytes). Las texturas las pone text_DOT.py: las incrustadas en el
# archivo y las que nombra DEPENDENCIES, buscadas junto a la malla y en
# Media/3d/Textures. Al exportar la malla, Noesis escribe tambien las texturas.
# Sin text_DOT.py solo se abren los .atc descomprimidos, sin texturas del juego.
# Opcion -dotnotex: carga sin texturas, mucho mas rapido.
# Ver las UVs en Noesis: tecla U.

from inc_noesis import *

import math
import noesis
import rapi
import struct

try:
    import text_DOT
except ImportError:
    text_DOT = None

DOT_MAGIC = b"VERSION\x00"

# Parent map in UPPERCASE; lookup is case-insensitive.
DOT_PARENTS = {
    "HIPS": None,
    "SPINEA": "HIPS", "SPINEB": "SPINEA", "SPINEC": "SPINEB",
    "NECKA": "SPINEC", "NECKB": "NECKA", "HEAD": "NECKA",
    "L_CLAVICLE": "SPINEC", "L_SHOULDER": "L_CLAVICLE", "L_ELBOW": "L_SHOULDER",
    "L_HAND": "L_ELBOW", "L_WIELD": "L_HAND",
    "R_CLAVICLE": "SPINEC", "R_SHOULDER": "R_CLAVICLE", "R_ELBOW": "R_SHOULDER",
    "R_HAND": "R_ELBOW", "R_WIELD": "R_HAND",
    "L_UPLEG": "HIPS", "L_KNEE": "L_UPLEG", "L_ANKLE": "L_KNEE", "L_BALL": "L_ANKLE",
    "R_UPLEG": "HIPS", "R_KNEE": "R_UPLEG", "R_ANKLE": "R_KNEE", "R_BALL": "R_ANKLE",
    "QUIVER_01": "SPINEC", "QUIVER_02": "QUIVER_01",
}


DOT_OPT_MARKERS = "-dotfx"
DOT_OPT_NOTEX = "-dotnotex"


def registerNoesisTypes():
    # .bof solo con text_DOT.py: es quien descomprime el .atc.bof del juego.
    extensions = [".dotmesh", ".atc"]
    if text_DOT is not None:
        extensions.append(".bof")
    handles = []
    for ext in extensions:
        handle = noesis.register("Dawn of Titans Echo BOF", ext)
        noesis.setHandlerTypeCheck(handle, noepyCheckType)
        noesis.setHandlerLoadModel(handle, noepyLoadModel)
        handles.append(handle)

    for opt_handle in handles:
        try:
            noesis.addOption(
                opt_handle, DOT_OPT_MARKERS,
                "dibuja tambien los anclajes de VFX y los volumenes de dano", 0)
            noesis.addOption(
                opt_handle, DOT_OPT_NOTEX,
                "carga la malla sin decodificar sus texturas", 0)
        except Exception:
            pass
    return 1


def want_markers():
    """True si se ha pedido ver las cajas de efectos (-dotfx)."""
    try:
        return bool(noesis.optWasInvoked(DOT_OPT_MARKERS))
    except Exception:
        return False


def want_textures():
    """False si se ha pedido cargar sin texturas (-dotnotex) o falta text_DOT."""
    if text_DOT is None:
        return False
    try:
        return not noesis.optWasInvoked(DOT_OPT_NOTEX)
    except Exception:
        return True


def _byte_at(data, idx):
    b = data[idx]
    return b if isinstance(b, int) else ord(b)


# --------------------------------------------------------------------------
# Fase 0 — arbol de secciones
#
# Echo BOF es un arbol TLV autodescriptivo. No hay nada que adivinar:
#
#     [nombre: 32 bytes ASCII rellenados con ceros][u32 size][cuerpo: size]
#
# El hermano siguiente esta en off + 36 + size. Una seccion es contenedor si su
# cuerpo empieza por otro nombre de seccion valido, o por un u32 de cuenta
# seguido de uno. Recorrido validado al 100 % de los bytes en las 19 muestras.
# --------------------------------------------------------------------------

DOT_NAME_FIELD = 32
DOT_SECTION_HEADER = 36
DOT_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def read_section_name(data, off, limit=None):
    """Nombre de la seccion que empieza en *off*, o None si ahi no hay una."""
    if limit is None:
        limit = len(data)
    if off < 0 or off + DOT_SECTION_HEADER > limit:
        return None
    end = -1
    for i in range(DOT_NAME_FIELD):
        if _byte_at(data, off + i) == 0:
            end = i
            break
    if end <= 0:
        return None
    for i in range(end):
        if chr(_byte_at(data, off + i)) not in DOT_NAME_CHARS:
            return None
    # El relleno tiene que ser todo ceros: si no, no es una cabecera.
    for i in range(end, DOT_NAME_FIELD):
        if _byte_at(data, off + i) != 0:
            return None
    return data[off:off + end].decode("ascii")


class DotSection(object):
    """Una seccion del arbol: donde empieza su cuerpo, donde acaba y sus hijos."""

    def __init__(self, name, off, size):
        self.name = name
        self.off = off
        self.size = size
        self.body = off + DOT_SECTION_HEADER
        self.end = self.body + size
        self.children = []

    def child(self, name):
        for node in self.children:
            if node.name == name:
                return node
        return None

    def __repr__(self):
        return "<%s off=%d size=%d hijos=%d>" % (
            self.name, self.off, self.size, len(self.children))


def _walk_sections(data, off, end, out):
    """Recorre los hermanos de [off, end). Devuelve (offset final, sin restos)."""
    while off + DOT_SECTION_HEADER <= end:
        name = read_section_name(data, off, end)
        if name is None:
            return off, False
        size = struct.unpack_from("<I", data, off + DOT_NAME_FIELD)[0]
        node = DotSection(name, off, size)
        if node.end > end:
            return off, False
        out.append(node)
        if read_section_name(data, node.body, node.end) is not None:
            _walk_sections(data, node.body, node.end, node.children)
        elif read_section_name(data, node.body + 4, node.end) is not None:
            # Contenedor con un u32 de cuenta antes de los hijos.
            _walk_sections(data, node.body + 4, node.end, node.children)
        off = node.end
    return off, True


def parse_section_tree(data):
    """(secciones raiz, offset alcanzado, recorrido sin restos)."""
    roots = []
    stop, clean = _walk_sections(data, 0, len(data), roots)
    return roots, stop, clean


_TREE_CACHE = [None, None]


def section_tree(data):
    """parse_section_tree con memoria del ultimo archivo (se llama varias veces)."""
    key = (len(data), data[:64], data[-64:])
    if _TREE_CACHE[0] == key:
        return _TREE_CACHE[1]
    tree = parse_section_tree(data)
    _TREE_CACHE[0] = key
    _TREE_CACHE[1] = tree
    return tree


def find_root(roots, name):
    for node in roots:
        if node.name == name:
            return node
    return None


def parse_name_table(data, node):
    """Cuerpo de una seccion NAME-*: [u32 cuenta][u32 bytes][cadenas con \0]."""
    if node is None or node.size < 8:
        return []
    count, blob = struct.unpack_from("<2I", data, node.body)
    pos = node.body + 8
    end = min(node.end, pos + blob)
    names = []
    while pos < end and len(names) < count:
        z = data.find(b"\x00", pos, end)
        if z < 0:
            z = end
        try:
            names.append(data[pos:z].decode("ascii"))
        except Exception:
            names.append("")
        pos = z + 1
    return names


# --------------------------------------------------------------------------
# Fase 1 — enumerar las mallas desde el arbol
#
# Un registro por bloque MESH. Los contadores salen de MESH-HEADER y el buffer
# de su propio MATERIALS-GEOMETRY2: no hay busqueda ni puntuacion de por medio.
# --------------------------------------------------------------------------


class DotMeshInfo(object):
    """Una malla tal y como la declara el archivo."""

    def __init__(self):
        self.ordinal = 0            # posicion dentro de MESHES
        self.index = 0              # MESH-HEADER[0]
        self.name_index = 0         # MESH-HEADER[1] -> NAME-MESHES
        self.name = ""
        self.vert_count = 0
        self.index_count = 0
        self.layer_count = 0
        self.vertex_color = 0     # MESH-HEADER[6]: color de vertice en el buffer
        self.node = None
        self.geo = None             # seccion MATERIALS-GEOMETRY2
        self.mat_header = None
        self.uv_info = None
        self.compression = 0
        self.submeshes = []
        self.resolved = False

    @property
    def tri_count(self):
        return self.index_count // 3

    def __repr__(self):
        return "<MESH %s v=%d i=%d capas=%d>" % (
            self.name, self.vert_count, self.index_count, self.layer_count)


def _read_mesh_block(data, node, mesh_names, ordinal):
    header = node.child("MESH-HEADER")
    geo = node.child("MATERIALS-GEOMETRY2")
    if header is None or header.size < 28 or geo is None:
        return None

    fields = struct.unpack_from("<7I", data, header.body)
    info = DotMeshInfo()
    info.ordinal = ordinal
    info.index = fields[0]
    info.name_index = fields[1]
    info.vert_count = fields[2]
    info.index_count = fields[3]
    info.layer_count = fields[5]
    info.vertex_color = fields[6]
    info.node = node
    info.geo = geo
    info.mat_header = node.child("MATERIALS-HEADER")
    info.uv_info = node.child("MATERIALS-UVINFORMATION2")

    comp = node.child("MATERIALS-COMPRESSION")
    if comp is not None and comp.size >= 4:
        info.compression = struct.unpack_from("<I", data, comp.body)[0]

    if info.name_index < len(mesh_names):
        info.name = mesh_names[info.name_index]
    if not info.name:
        info.name = "MESH_%d" % ordinal
    return info


def enumerate_meshes(data, roots=None):
    """Los bloques MESH declarados, en el orden del archivo."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    mesh_names = parse_name_table(data, find_root(roots, "NAME-MESHES"))
    meshes_node = find_root(roots, "MESHES")
    out = []
    if meshes_node is None:
        return out
    for node in meshes_node.children:
        if node.name != "MESH":
            continue
        info = _read_mesh_block(data, node, mesh_names, len(out))
        if info is not None:
            out.append(info)
    return out


def noepyCheckType(data):
    if data is None or len(data) < 64:
        return 0
    if text_DOT is not None and text_DOT.container_kind(data) in ("zstd", "zlib"):
        # Comprimido: basta la cabecera. NAME-MESHES > 0 si y solo si hay
        # MESHES (4319 de 4319 en el corpus); sin mallas es de text_DOT.
        try:
            head = text_DOT.payload_head(data)
        except Exception:
            return 0
        if head is None or head[:8] != DOT_MAGIC:
            return 0
        count = text_DOT.declared_mesh_count(head)
        if count is not None:
            return 1 if count > 0 else 0
        data = text_DOT.bof_payload(data)
        if data is None:
            return 0
    if _byte_at(data, 0) == 0x28 and _byte_at(data, 1) == 0xB5:
        return 0
    if data[:8] != DOT_MAGIC:
        return 0
    if data.find(b"Echo BOF format 1.0") < 0:
        return 0
    # El arbol tiene que abrirse y traer mallas dentro.
    roots, _stop, _clean = section_tree(data)
    if find_root(roots, "MESHES") is None:
        return 0
    return 1


# --------------------------------------------------------------------------
# Fase 2 — stride y reparto del buffer de geometria
#
# MATERIALS-HEADER no es una cabecera suelta: es la lista de submallas de la
# malla, una por capa de material. Cada registro son 5 u32
# [material, unk, vert_count, index_count, n_enlaces] seguidos de n_enlaces
# registros de 8 u32, donde [1] es la capa de NAME-MATERIALLAYERS y [2] la
# textura de NAME-TEXTURES. Los contadores de las submallas suman exactamente
# los del MESH-HEADER.
#
# El cuerpo de MATERIALS-GEOMETRY2 es la concatenacion, en orden, de
# [vertices: vert_count x stride][indices: index_count x u16] por submalla.
# Con una submalla el stride es una division exacta. Con varias, el de cada una
# queda fijado por dos condiciones que el archivo mismo comprueba: que todos
# sus indices caigan dentro de su vert_count y que la cadena acabe justo al
# final del bloque. En las 19 muestras la solucion es unica.
# --------------------------------------------------------------------------

DOT_MIN_STRIDE = 12          # float3 de posicion
DOT_MAX_STRIDE = 128
DOT_SKIN_BLOCK = 8           # huesos u8x4 + pesos u8x4 al final del vertice


class DotSubmesh(object):
    """Un tramo de MATERIALS-GEOMETRY2 con su material propio."""

    def __init__(self):
        self.mesh = None
        self.name = ""
        self.material_index = 0
        self.vert_count = 0
        self.index_count = 0
        self.bindings = []      # [(capa, textura)]
        self.stride = 0
        self.vstart = -1
        self.istart = -1
        self.uv_off = None
        self.uv_fmt = None
        self.skinned = False

    @property
    def tri_count(self):
        return self.index_count // 3

    def __repr__(self):
        return "<%s v=%d i=%d stride=%d>" % (
            self.name, self.vert_count, self.index_count, self.stride)


def parse_submeshes(data, info):
    """Las submallas declaradas en MATERIALS-HEADER."""
    subs = []
    sec = info.mat_header
    if sec is not None:
        pos = sec.body
        while pos + 20 <= sec.end:
            mat, _unk, vert_count, index_count, n_bind = struct.unpack_from(
                "<5I", data, pos)
            pos += 20
            sub = DotSubmesh()
            sub.mesh = info
            sub.material_index = mat
            sub.vert_count = vert_count
            sub.index_count = index_count
            for _ in range(n_bind):
                if pos + 32 > sec.end:
                    break
                rec = struct.unpack_from("<8I", data, pos)
                pos += 32
                sub.bindings.append((rec[1], rec[2]))
            subs.append(sub)

    total_v = sum(s.vert_count for s in subs)
    total_i = sum(s.index_count for s in subs)
    if not subs or total_v != info.vert_count or total_i != info.index_count:
        # Sin tabla utilizable, la malla es una sola submalla.
        sub = DotSubmesh()
        sub.mesh = info
        sub.vert_count = info.vert_count
        sub.index_count = info.index_count
        subs = [sub]

    for i, sub in enumerate(subs):
        # El sufijo lleva "#" a proposito: NAME-MESHES ya usa "_0", "_1" para
        # mallas distintas y no queremos dos GEO_BODY_0 en la lista de Noesis.
        sub.name = info.name if len(subs) == 1 else "%s#%d" % (info.name, i)
    return subs


def _indices_in_range(data, off, count, vert_count):
    """True si los *count* u16 de *off* son todos indices validos."""
    if off < 0 or off + count * 2 > len(data):
        return False
    for i in range(count):
        if struct.unpack_from("<H", data, off + i * 2)[0] >= vert_count:
            return False
    return True


def _solve_strides(data, end, subs, k, cur, acc, out):
    """Encadena las submallas probando el stride de cada una contra sus indices."""
    if k == len(subs):
        if cur == end:
            out.append(list(acc))
        return
    sub = subs[k]
    if sub.vert_count <= 0:
        return
    for stride in range(DOT_MIN_STRIDE, DOT_MAX_STRIDE + 1):
        istart = cur + sub.vert_count * stride
        nxt = istart + sub.index_count * 2
        if nxt > end:
            break
        if not _indices_in_range(data, istart, sub.index_count, sub.vert_count):
            continue
        acc.append(stride)
        _solve_strides(data, end, subs, k + 1, nxt, acc, out)
        acc.pop()
        if len(out) > 1:
            return


def resolve_geometry(data, info, subs):
    """Fija stride, vstart e istart de cada submalla. False si no cuadra."""
    body = info.geo.body
    end = info.geo.end

    if len(subs) == 1:
        sub = subs[0]
        rest = info.geo.size - sub.index_count * 2
        if sub.vert_count <= 0 or rest <= 0 or rest % sub.vert_count != 0:
            return False
        strides = [rest // sub.vert_count]
    else:
        found = []
        _solve_strides(data, end, subs, 0, body, [], found)
        if not found:
            return False
        if len(found) > 1:
            print("DOT: aviso — %s admite mas de un reparto de stride" % info.name)
        strides = found[0]

    cursor = body
    for sub, stride in zip(subs, strides):
        if stride < DOT_MIN_STRIDE:
            return False
        sub.stride = stride
        sub.vstart = cursor
        sub.istart = cursor + sub.vert_count * stride
        cursor = sub.istart + sub.index_count * 2
        if cursor > end:
            return False
    return cursor == end


def read_indices(data, sub):
    """Los indices de la submalla, tal cual estan en el archivo."""
    out = []
    for i in range(sub.index_count):
        out.append(struct.unpack_from("<H", data, sub.istart + i * 2)[0])
    return out


def read_positions(data, sub):
    """Las posiciones de la submalla: float3 al principio de cada vertice."""
    out = []
    for vi in range(sub.vert_count):
        out.append(struct.unpack_from("<3f", data, sub.vstart + vi * sub.stride))
    return out


# --------------------------------------------------------------------------
# El arbol de escenas — donde va cada malla
#
# SCENES cuelga una SCENE por escena raiz (NAME-ROOTSCENES) y, dentro de
# SCENE-CHILDREN, una SCENE por malla. El cuerpo de SCENE-DATA son 52 bytes:
#
#     [u32 nombre][u32 malla][u32 hijos][3f traslacion][4f giro xyzw][3f escala]
#
# El nombre sale de NAME-ROOTSCENES en las raices y de NAME-SUBSCENES en las
# hojas. El campo de malla es la posicion del bloque MESH (0xFFFFFFFF en las
# raices); comprobado en 218 nodos de 42 archivos, todos con el nombre del
# bloque igual al que le da NAME-SUBSCENES.
#
# Los vertices estan en el espacio del nodo, no en el del modelo. Sin aplicar
# esta transformacion GEO_TEETH1 se queda tirado junto a los pies y los cubos
# de un centimetro que marcan donde engancha cada VFX (GEN_FREEZE_*,
# FX_WEAPONSHIELD_SPARK...) se apilan en el origen. Con ella los dientes caen
# dentro de la cabeza y cada marcador se posa donde le toca.
# --------------------------------------------------------------------------

DOT_SCENE_DATA = 52
DOT_SCENE_NO_MESH = 0xFFFFFFFF


def _quat_rotate(quat, x, y, z):
    """Gira (x, y, z) por el cuaternion (qx, qy, qz, qw)."""
    qx, qy, qz, qw = quat
    cx = qy * z - qz * y
    cy = qz * x - qx * z
    cz = qx * y - qy * x
    ccx = qy * cz - qz * cy
    ccy = qz * cx - qx * cz
    ccz = qx * cy - qy * cx
    return (
        x + 2.0 * (qw * cx + ccx),
        y + 2.0 * (qw * cy + ccy),
        z + 2.0 * (qw * cz + ccz),
    )


def _quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


class DotSceneNode(object):
    """Donde pone el arbol de escenas a una malla: traslacion, giro y escala."""

    def __init__(self, mesh_index, name, scene, trans, quat, scale):
        self.mesh_index = mesh_index
        self.name = name
        self.scene = scene          # escena raiz de la que cuelga el nodo
        self.trans = trans
        self.quat = quat
        self.scale = scale

    def is_identity(self):
        tx, ty, tz = self.trans
        qx, qy, qz, qw = self.quat
        sx, sy, sz = self.scale
        return (
            abs(tx) + abs(ty) + abs(tz) < 1e-6
            and abs(qx) + abs(qy) + abs(qz) + abs(qw - 1.0) < 1e-6
            and abs(sx - 1.0) + abs(sy - 1.0) + abs(sz - 1.0) < 1e-6
        )

    def point(self, x, y, z):
        """Lleva un vertice del espacio del nodo al del modelo."""
        sx, sy, sz = self.scale
        rx, ry, rz = _quat_rotate(self.quat, x * sx, y * sy, z * sz)
        tx, ty, tz = self.trans
        return rx + tx, ry + ty, rz + tz

    def direction(self, x, y, z):
        """Igual que point() pero sin trasladar: para las normales."""
        return _quat_rotate(self.quat, x, y, z)

    def __repr__(self):
        return "<nodo %s escena=%s t=(%.3f,%.3f,%.3f)>" % (
            (self.name, self.scene) + tuple(self.trans))


def _place_under(parent, node):
    """Compone el nodo con el de su padre. Sin padre se queda como esta."""
    if parent is None:
        return node
    node.trans = parent.point(*node.trans)
    node.quat = _quat_mul(parent.quat, node.quat)
    node.scale = tuple(
        parent.scale[k] * node.scale[k] for k in range(3))
    return node


def parse_scene_nodes(data, roots=None):
    """{posicion del bloque MESH: DotSceneNode} recorriendo SCENES."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    scenes = find_root(roots, "SCENES")
    if scenes is None:
        return {}
    sub_names = parse_name_table(data, find_root(roots, "NAME-SUBSCENES"))
    root_names = parse_name_table(data, find_root(roots, "NAME-ROOTSCENES"))
    out = {}

    def walk(section, parent, scene):
        record = section.child("SCENE-DATA")
        placed = parent
        if record is not None and record.size >= DOT_SCENE_DATA:
            fields = struct.unpack_from("<3I10f", data, record.body)
            mesh_index = fields[1]
            table = root_names if mesh_index == DOT_SCENE_NO_MESH else sub_names
            name = table[fields[0]] if fields[0] < len(table) else ""
            if mesh_index == DOT_SCENE_NO_MESH:
                scene = name or scene
            placed = _place_under(parent, DotSceneNode(
                mesh_index, name, scene,
                fields[3:6], fields[6:10], fields[10:13]))
            if mesh_index != DOT_SCENE_NO_MESH:
                out[mesh_index] = placed
        children = section.child("SCENE-CHILDREN")
        if children is not None:
            for kid in children.children:
                walk(kid, placed, scene)

    for scene_section in scenes.children:
        walk(scene_section, None, "")
    return out


# --------------------------------------------------------------------------
# Marcadores de efectos — cajas que no son la malla
#
# Las escenas raiz clasifican lo que cuelga de ellas. La geometria de verdad
# vive en ENVIRONMENT, y en los edificios en LEVEL1..5, BUILDING_TREES_*,
# WANDERERS_*, FIRE, FREEZE o ACID. Aparte van los puntos de anclaje de los
# VFX y los volumenes de dano: RENDERABLES*, SFX_* y WEAPONDAMAGE.
#
# Medido en los 37 archivos: las escenas de marcadores contienen 170 submallas
# y *todas* son cajas de 12 triangulos con 24 vertices o menos. Ninguna malla
# de verdad cuelga de ellas. Aun asi se exige tambien la forma de caja antes de
# esconder nada: si algun dia aparece un archivo con geometria ahi dentro,
# preferimos ensenarla de mas que perderla.
#
# El nombre de la escena no basta por si solo: FIRE, FREEZE y ACID son
# geometria, mientras que RENDERABLES_FIRE, RENDERABLES_FREEZE y
# RENDERABLES_ACID son marcadores.
# --------------------------------------------------------------------------

DOT_MARKER_SCENES = ("WEAPONDAMAGE",)
DOT_MARKER_PREFIXES = ("RENDERABLES", "SFX_")
DOT_MARKER_TRIS = 12
DOT_MARKER_VERTS = 24


def is_marker_scene(name):
    """True si la escena raiz solo lleva anclajes de efectos."""
    if not name:
        return False
    upper = name.upper()
    if upper in DOT_MARKER_SCENES:
        return True
    for prefix in DOT_MARKER_PREFIXES:
        if upper.startswith(prefix):
            return True
    return False


def is_marker_mesh(sub, node):
    """Una caja de efectos: escena de marcadores y forma de caja."""
    if node is None or not is_marker_scene(node.scene):
        return False
    return sub.tri_count == DOT_MARKER_TRIS and sub.vert_count <= DOT_MARKER_VERTS


# --------------------------------------------------------------------------
# Esqueleto
#
# El nombre y el padre de cada hueso salen de NAME-JOINTS y de las tablas de
# arriba. La pose de reparto esta en la seccion SKELETON de la escena raiz, una
# por hueso; la SKELETON que cuelga de cada MESH es otra cosa: la paleta de
# huesos de esa malla (ver bone_palette). Cuando el archivo no trae la pose,
# las posiciones salen de la heuristica.
# --------------------------------------------------------------------------


def _upper(s):
    return s.upper()


def guess_parent(name, name_set, sk_root):
    up = _upper(name)
    if name == sk_root:
        return None
    if up in DOT_PARENTS:
        parent_up = DOT_PARENTS[up]
        if parent_up is None:
            return sk_root
        # Find original-cased parent in the set.
        for n in name_set:
            if _upper(n) == parent_up:
                return n
        return sk_root
    # Numbered chains: Foo_02 -> Foo_01
    if len(name) > 3 and name[-3] == "_" and name[-2] == "0" and name[-1].isdigit() and name[-1] != "1":
        prev = name[:-1] + str(int(name[-1]) - 1)
        if prev in name_set:
            return prev
    if "SKIRT" in up or "CAPE" in up:
        for cand in ("Hips", "HIPS", "Pelvis"):
            if cand in name_set:
                return cand
        return sk_root
    # Roll / helper bones: L_ElbowRoll_A -> L_Elbow
    if "ROLL" in up or "HELPER" in up:
        base = name.split("Roll")[0].split("Helper")[0].split("roll")[0]
        if base.endswith("_"):
            base = base[:-1]
        for n in name_set:
            if n == base or _upper(n) == _upper(base):
                return n
    return sk_root


def split_dot_bone_name(name):
    """Split SK_Root, HIPS_A_MID, UPPERLEG_A_RT, WEAPON_SP, etc."""
    up = name.upper()
    if up.startswith("SK_"):
        return up, None
    if up == "WEAPON_SP":
        return "WEAPON", "SP"
    for side in ("RT", "LF", "MID"):
        suf = "_A_" + side
        if up.endswith(suf):
            return up[: -len(suf)], side
    return up, None


# Parent base name -> parent base name (Echo _A_RT/_A_LF/_A_MID rigs).
DOT_A_PARENT = {
    "ROOT": "SK_ROOT",
    "HIPS": "ROOT",
    "SPINE_1": "HIPS",
    "SPINE_2": "SPINE_1",
    "NECK": "SPINE_2",
    "HEAD": "NECK",
    "CLAVICLE": "SPINE_2",
    "UPPERARM": "CLAVICLE",
    "LOWERARM": "UPPERARM",
    "HAND": "LOWERARM",
    "ARMTWISTER": "LOWERARM",
    "WEAPON": "HAND",
    "UPPERLEG": "HIPS",
    "LOWERLEG": "UPPERLEG",
    "ANKLE": "LOWERLEG",
    "GDE_FOOTHEEL": "ANKLE",
    "BALL": "GDE_FOOTHEEL",
    "TOE": "BALL",
    "CLOTH_1": "SPINE_2",
    "CLOTH_2": "CLOTH_1",
}


def resolve_dot_bone_name(base, side, names, sk_root):
    """Find the actual joint name for a base + side in the file."""
    if base == "SK_ROOT":
        return sk_root
    if side:
        cand = base + "_A_" + side
        if cand in names:
            return cand
    cand = base + "_A_MID"
    if cand in names:
        return cand
    for n in names:
        if n.upper().startswith(base + "_"):
            return n
    return None


def guess_parent_dot_a(name, names, sk_root):
    if name == sk_root:
        return None

    base, side = split_dot_bone_name(name)
    if base == "SK_" + sk_root.split("_", 1)[-1] if sk_root and "_" in sk_root else "":
        return None
    if sk_root and base == sk_root.upper().replace("SK_", "", 1):
        return None

    if base == "ROOT" or name.upper().startswith("ROOT_"):
        return sk_root

    if name.upper() == "WEAPON_SP":
        for cand in ("HAND_A_RT", "HAND_A_MID"):
            if cand in names:
                return cand
        return sk_root

    parent_base = DOT_A_PARENT.get(base)
    if not parent_base:
        return guess_parent(name, set(names), sk_root)

    parent_name = resolve_dot_bone_name(parent_base, side, names, sk_root)
    if parent_name and parent_name != name:
        return parent_name
    return sk_root


def _bone_norm_pos(base, side):
    """Normalized (0-1) position inside the mesh bounds per joint type."""
    table = {
        "SK_ROOT": (0.50, 0.46, 0.50),
        "ROOT": (0.50, 0.46, 0.50),
        "HIPS": (0.50, 0.48, 0.50),
        "SPINE_1": (0.50, 0.56, 0.50),
        "SPINE_2": (0.50, 0.64, 0.50),
        "NECK": (0.50, 0.78, 0.50),
        "HEAD": (0.50, 0.90, 0.50),
        "CLAVICLE": (0.50, 0.70, 0.50),
        "UPPERARM": (0.50, 0.66, 0.50),
        "LOWERARM": (0.50, 0.58, 0.50),
        "HAND": (0.50, 0.52, 0.50),
        "ARMTWISTER": (0.50, 0.56, 0.50),
        "WEAPON": (0.50, 0.50, 0.50),
        "UPPERLEG": (0.50, 0.40, 0.50),
        "LOWERLEG": (0.50, 0.24, 0.50),
        "ANKLE": (0.50, 0.08, 0.50),
        "GDE_FOOTHEEL": (0.50, 0.04, 0.50),
        "BALL": (0.50, 0.02, 0.52),
        "TOE": (0.50, 0.00, 0.55),
        "CLOTH_1": (0.50, 0.50, 0.45),
        "CLOTH_2": (0.50, 0.42, 0.42),
    }
    nx, ny, nz = table.get(base, (0.50, 0.50, 0.50))

    if side == "RT":
        if base in ("CLAVICLE", "UPPERARM", "LOWERARM", "HAND", "ARMTWISTER", "WEAPON"):
            nx = 0.78
        elif base in ("UPPERLEG", "LOWERLEG", "ANKLE", "GDE_FOOTHEEL", "BALL", "TOE"):
            nx = 0.62
        else:
            nx = 0.58
    elif side == "LF":
        if base in ("CLAVICLE", "UPPERARM", "LOWERARM", "HAND", "ARMTWISTER", "WEAPON"):
            nx = 0.22
        elif base in ("UPPERLEG", "LOWERLEG", "ANKLE", "GDE_FOOTHEEL", "BALL", "TOE"):
            nx = 0.38
        else:
            nx = 0.42
    elif side == "SP":
        nx = 0.82

    return nx, ny, nz


def _world_bone_pos(name, names, sk_root, bounds):
    min_x, max_x, min_y, max_y, min_z, max_z = bounds
    width = max(max_x - min_x, 0.001)
    height = max(max_y - min_y, 0.001)
    depth = max(max_z - min_z, 0.001)

    if name == sk_root:
        nx, ny, nz = _bone_norm_pos("SK_ROOT", None)
    else:
        base, side = split_dot_bone_name(name)
        if base.startswith("SK_"):
            nx, ny, nz = _bone_norm_pos("SK_ROOT", None)
        else:
            nx, ny, nz = _bone_norm_pos(base, side)

    return (
        min_x + width * nx,
        min_y + height * ny,
        min_z + depth * nz,
    )


# --------------------------------------------------------------------------
# La pose de reparto
#
# La escena raiz lleva una seccion SKELETON con [u32 cuenta][registros], un
# registro de 140 bytes por hueso, y el registro se lee entero:
#
#     4    u32   su propio indice
#     8    u32   el padre (indice de registro; 0xFFFFFFFF en la raiz)
#     16   u32   cuantos hijos tiene
#     20   u32   su nombre: indice en NAME-JOINTS
#     24   16f   matriz inversa de bind, 4x4 por filas (v_hueso = v_mundo*M + t)
#     88   4f    cuaternion local, repetido en el 104
#     120  3f    traslacion local respecto al padre
#
# Los registros no van en el orden de NAME-JOINTS —van por anchura y la tabla
# de nombres va en profundidad—, asi que el nombre sale del campo del 20 y no
# de la posicion en la lista.
#
# Medido en 18 archivos y 1418 huesos: el indice propio y el padre cuadran en
# todos, la 3x3 es ortonormal en todos, el campo del 20 es una permutacion
# completa de NAME-JOINTS en todos, y componer la traslacion local con la
# matriz del padre reproduce la posicion del hijo en los 1418.
#
# Lo de antes leia cuatro floats del 56 y tres del 72, que caen dentro de esa
# matriz: de ahi salian huesos en sitios imposibles, con el nombre cambiado y
# el padre adivinado por el nombre.
# --------------------------------------------------------------------------

DOT_BONE_STRIDE = 140
DOT_BONE_PARENT = 8
DOT_BONE_NAME = 20
DOT_BONE_INVBIND = 24
DOT_BONE_INDEX = 4
DOT_BONE_NO_PARENT = 0xFFFFFFFF


def find_scene_skeleton(data, roots):
    """(inicio, stride, cuenta) de la SKELETON con mas huesos de las escenas.

    Las escenas RENDERABLES traen su propio esqueleto, mas corto, con los
    huesos a los que se enganchan los efectos; el del modelo es el mayor.
    """
    scenes = find_root(roots, "SCENES")
    if scenes is None:
        return None
    best = None
    for scene in scenes.children:
        sec = scene.child("SKELETON")
        if sec is None or sec.size < 8:
            continue
        count = struct.unpack_from("<I", data, sec.body)[0]
        rest = sec.size - 8
        if count <= 0 or rest <= 0 or rest % count != 0:
            continue
        stride = rest // count
        body = sec.body + 4
        if stride < DOT_BONE_STRIDE or body + count * stride > len(data):
            continue
        # El primer registro tiene que decir que es el 0: si no, no es esto.
        if struct.unpack_from("<I", data, body + DOT_BONE_INDEX)[0] != 0:
            continue
        if best is None or count > best[2]:
            best = (body, stride, count)
    return best


def invbind_to_model(mat16):
    """Invierte la matriz de bind: (filas de la matriz de modelo, posicion)."""
    rows = (
        (mat16[0], mat16[4], mat16[8]),
        (mat16[1], mat16[5], mat16[9]),
        (mat16[2], mat16[6], mat16[10]),
    )
    t = (mat16[12], mat16[13], mat16[14])
    pos = tuple(
        -(t[0] * rows[0][j] + t[1] * rows[1][j] + t[2] * rows[2][j])
        for j in range(3))
    return rows, pos


def parse_skeleton(data, roots=None):
    """[(padre, indice de nombre, filas, posicion)] en el orden del archivo."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    found = find_scene_skeleton(data, roots)
    if found is None:
        return []
    body, stride, count = found
    out = []
    for i in range(count):
        off = body + i * stride
        parent = struct.unpack_from("<I", data, off + DOT_BONE_PARENT)[0]
        name_index = struct.unpack_from("<I", data, off + DOT_BONE_NAME)[0]
        rows, pos = invbind_to_model(
            struct.unpack_from("<16f", data, off + DOT_BONE_INVBIND))
        out.append((parent, name_index, rows, pos))
    return out


def skeleton_bone_names(names, records):
    """El nombre de cada hueso, en el orden en que estan los registros."""
    out = []
    for i, (_parent, name_index, _rows, _pos) in enumerate(records):
        if name_index < len(names) and names[name_index]:
            out.append(names[name_index])
        else:
            out.append("BONE_%d" % i)
    return out


# --------------------------------------------------------------------------
# Los enganches sin pose
#
# Casi todo personaje trae un par de huesos -R_Wield y L_Wield, y algun
# ROOT_A_MID- cuya matriz de bind los deja en el origen del mundo aunque
# cuelguen de la mano. Medido sobre los 482 archivos de Troops/heroes: 458
# los tienen, 890 huesos en total, y 862 son Wield. No es un error de
# lectura -el registro dice eso, y la traslacion local del 120 lleva justo
# al origen-: son enganches que el juego coloca en marcha, con la pose de
# reparto sin rellenar.
#
# Dibujados tal cual, cruzan el modelo entero de la mano al suelo y ensucian
# la vista. Se cuelgan del padre, que es donde estan de verdad cuando el
# juego los usa. Solo si nadie les manda peso: si alguna malla se apoya en
# uno, su matriz de bind es la que sostiene esos vertices y no se toca.
# --------------------------------------------------------------------------

DOT_ORIGIN_EPS = 1e-4


def _at_origin(pos):
    return abs(pos[0]) < DOT_ORIGIN_EPS and abs(pos[1]) < DOT_ORIGIN_EPS \
        and abs(pos[2]) < DOT_ORIGIN_EPS


def _bones_from_skeleton(names, records, con_peso=None):
    """Un NoeBone por registro, con la matriz de modelo que trae el archivo.

    La lista va en el orden del archivo, no en el de NAME-JOINTS: es el orden
    que indexan las paletas de las mallas, asi que los pesos apuntan al hueso
    que les toca sin traducir nada. El nombre sale del campo del registro.

    *con_peso* son los huesos que alguna malla usa: los enganches sin pose que
    esten ahi se quedan donde el archivo los pone.
    """
    bone_names = skeleton_bone_names(names, records)
    con_peso = con_peso or set()
    colgados = []
    bones = []
    for i, (parent, _name_index, rows, pos) in enumerate(records):
        if (parent != DOT_BONE_NO_PARENT and parent < len(records) and parent != i
                and i not in con_peso and _at_origin(pos)
                and not _at_origin(records[parent][3])):
            rows = records[parent][2]
            pos = records[parent][3]
            colgados.append(bone_names[i])
        mat = _noe_mat(rows, pos)
        parent_index = -1
        if parent != DOT_BONE_NO_PARENT and parent < len(records) and parent != i:
            parent_index = parent
        parent_name = bone_names[parent_index] if parent_index >= 0 else ""
        bones.append(NoeBone(i, bone_names[i], mat, parent_name, parent_index))

    print("DOT: bind pose SKELETON (%d huesos, jerarquia del archivo)" % len(bones))
    if colgados:
        print("DOT: %d enganche(s) sin pose colgados de su padre: %s" % (
            len(colgados), ", ".join(colgados[:4])
            + (", ..." if len(colgados) > 4 else "")))
    if len(bones) != len(names):
        print("DOT: aviso — %d hueso(s) para %d nombre(s) en NAME-JOINTS"
              % (len(bones), len(names)))
    return bones


def _build_bones_heuristic(names, bounds):
    sk_root = names[0] if names[0].upper().startswith("SK_") else None
    name_to_idx = {name: idx for idx, name in enumerate(names)}
    world = {}
    for name in names:
        world[name] = _world_bone_pos(name, names, sk_root, bounds)

    children = {}
    parents = {}
    for name in names:
        parent = guess_parent_dot_a(name, names, sk_root)
        parents[name] = parent
        if parent:
            children.setdefault(parent, []).append(name)

    bones = []
    for i, name in enumerate(names):
        parent = parents[name]
        wx, wy, wz = world[name]
        mat = NoeMat43()
        if parent is None:
            mat[3] = NoeVec3((wx, wy, wz))
        else:
            px, py, pz = world[parent]
            mat[3] = NoeVec3((wx - px, wy - py, wz - pz))
            child_list = children.get(name)
            if child_list:
                child_name = child_list[0]
                cx, cy, cz = world[child_name]
                mat = _bone_points_to_child(mat, (wx, wy, wz), (cx, cy, cz))
                mat[3] = NoeVec3((wx - px, wy - py, wz - pz))
        parent_idx = name_to_idx[parent] if parent in name_to_idx else -1
        bones.append(NoeBone(i, name, mat, parent or "", parent_idx))
    return bones


def build_bones(names, bounds, skeleton=None, con_peso=None):
    if skeleton:
        return _bones_from_skeleton(names, skeleton, con_peso)
    if not names:
        return []

    print("DOT: bind pose heuristic (no SKELETON block)")
    return _build_bones_heuristic(names, bounds)


def _bone_points_to_child(local_mat, parent_world, child_world):
    """Rotate bone +Y to point at child (common FBX/DCC convention)."""
    try:
        dx = child_world[0] - parent_world[0]
        dy = child_world[1] - parent_world[1]
        dz = child_world[2] - parent_world[2]
        length = (dx * dx + dy * dy + dz * dz) ** 0.5
        if length <= 1e-5:
            return local_mat
        dx /= length
        dy /= length
        dz /= length
        # Default bone axis +Y -> direction
        up = NoeVec3((0.0, 1.0, 0.0))
        dirv = NoeVec3((dx, dy, dz))
        cross = NoeVec3((
            up[1] * dirv[2] - up[2] * dirv[1],
            up[2] * dirv[0] - up[0] * dirv[2],
            up[0] * dirv[1] - up[1] * dirv[0],
        ))
        dot = up[0] * dirv[0] + up[1] * dirv[1] + up[2] * dirv[2]
        dot = max(-1.0, min(1.0, dot))
        if abs(dot - 1.0) < 1e-5:
            return local_mat
        if abs(dot + 1.0) < 1e-5:
            local_mat[0] = NoeVec3((1.0, 0.0, 0.0))
            local_mat[1] = NoeVec3((0.0, -1.0, 0.0))
            local_mat[2] = NoeVec3((0.0, 0.0, -1.0))
            return local_mat
        angle = math.acos(dot)
        axis_len = (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5
        if axis_len <= 1e-8:
            return local_mat
        axis = NoeVec3((cross[0] / axis_len, cross[1] / axis_len, cross[2] / axis_len))
        # Rodrigues rotation matrix
        c = math.cos(angle)
        s = math.sin(angle)
        t = 1.0 - c
        x, y, z = axis[0], axis[1], axis[2]
        rot = NoeMat43()
        rot[0] = NoeVec3((t * x * x + c, t * x * y + s * z, t * x * z - s * y))
        rot[1] = NoeVec3((t * x * y - s * z, t * y * y + c, t * y * z + s * x))
        rot[2] = NoeVec3((t * x * z + s * y, t * y * z - s * x, t * z * z + c))
        rot[3] = local_mat[3]
        return rot
    except Exception:
        return local_mat


def half_to_float(h):
    """IEEE half -> float (Noesis Python has no struct 'e')."""
    h &= 0xFFFF
    s = (h >> 15) & 1
    e = (h >> 10) & 0x1F
    m = h & 0x3FF
    if e == 0:
        if m == 0:
            return 0.0
        val = (m / 1024.0) * (2 ** -14)
        return -val if s else val
    if e == 31:
        return 0.0
    val = (1.0 + m / 1024.0) * (2 ** (e - 15))
    return -val if s else val


def resolve_texture_path(base_dir, tex_name):
    if not base_dir:
        return None
    base_dir = base_dir.replace("\\", "/")
    if not base_dir.endswith("/"):
        base_dir += "/"
    candidates = [
        base_dir + tex_name + ".png",
        base_dir + tex_name + "/" + tex_name + ".png",
        base_dir + tex_name + ".dds",
        base_dir + tex_name + ".tga",
        base_dir + tex_name + ".jpg",
    ]
    for path in candidates:
        if rapi.checkFileExists(path):
            return path
    return None


# --------------------------------------------------------------------------
# Fase 3 — nombre, material y skinning de cada submalla
#
# El vertice de Echo BOF, medido sobre las 19 muestras:
#
#     0   posicion          float3
#     12  normal            int8 x3, unidad = 127
#     15  color de vertice  4 bytes, solo si MESH-HEADER[6] != 0
#     ..  UV                half2 por juego de UVs (la primera es la difusa)
#     ..  tangente y copias de la UV, segun el formato
#     -8  huesos            uint8 x4, indices en la paleta SKELETON de la malla
#     -4  pesos             uint8 x4, suman 255
#
# Los indices de hueso no son globales: apuntan a la paleta que cada MESH lleva
# en su seccion SKELETON, que a su vez indexa NAME-JOINTS.
# --------------------------------------------------------------------------

DOT_UV_OFFSET = 15
DOT_VERTEX_COLOR = 4


def parse_joint_names(data, roots=None):
    """NAME-JOINTS. El primero es la raiz SK_ del esqueleto."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    return parse_name_table(data, find_root(roots, "NAME-JOINTS"))


def parse_texture_names(data, roots=None):
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    return parse_name_table(data, find_root(roots, "NAME-TEXTURES"))


def bone_palette(data, info):
    """[u32 cuenta][cuenta x u32] con los huesos a los que se pega la malla."""
    sec = info.node.child("SKELETON")
    if sec is None or sec.size < 4:
        return []
    count = struct.unpack_from("<I", data, sec.body)[0]
    if count <= 0 or sec.body + 4 + count * 4 > sec.end:
        return []
    return list(struct.unpack_from("<%dI" % count, data, sec.body + 4))


def _weights_sum_255(data, sub, samples=64):
    """Los pesos ocupan los 4 ultimos bytes del vertice y suman 255."""
    if sub.stride < DOT_UV_OFFSET + DOT_SKIN_BLOCK:
        return False
    step = max(1, sub.vert_count // samples)
    for vi in range(0, sub.vert_count, step):
        base = sub.vstart + vi * sub.stride + sub.stride - 4
        total = 0
        for k in range(4):
            total += _byte_at(data, base + k)
        if total != 255:
            return False
    return True


def resolve_vertex_layout(data, info, joint_count):
    """Marca en cada submalla si esta pesada y donde tiene la UV."""
    color = DOT_VERTEX_COLOR if info.vertex_color else 0
    for sub in info.submeshes:
        sub.skinned = bool(joint_count) and _weights_sum_255(data, sub)
        sub.uv_off = None
        if not sub.bindings:
            continue
        off = DOT_UV_OFFSET + color
        limit = sub.stride - (DOT_SKIN_BLOCK if sub.skinned else 0)
        if off + 4 <= limit:
            sub.uv_off = off


def read_normal(data, sub, vi):
    """Normal int8 x3 justo detras de la posicion."""
    base = sub.vstart + vi * sub.stride + 12
    out = []
    for k in range(3):
        b = _byte_at(data, base + k)
        out.append((b - 256 if b > 127 else b) / 127.0)
    return out


def read_uv(data, sub, vi):
    if sub.uv_off is None:
        return 0.0, 0.0
    u, v = struct.unpack_from("<HH", data, sub.vstart + vi * sub.stride + sub.uv_off)
    return half_to_float(u), half_to_float(v)


def read_skin(data, sub, vi, palette):
    """[(hueso global, peso)] del vertice, con los pesos ya normalizados."""
    if not sub.skinned or not palette:
        return []
    base = sub.vstart + vi * sub.stride + sub.stride - DOT_SKIN_BLOCK
    pairs = []
    for k in range(4):
        weight = _byte_at(data, base + 4 + k)
        if weight <= 0:
            continue
        slot = _byte_at(data, base + k)
        if slot >= len(palette):
            continue
        pairs.append((int(palette[slot]), weight / 255.0))
    return pairs


def submesh_material_name(data, sub, roots=None):
    """El nombre que NAME-MATERIALS le da al material de la submalla."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    names = parse_name_table(data, find_root(roots, "NAME-MATERIALS"))
    if sub.material_index < len(names) and names[sub.material_index]:
        return names[sub.material_index]
    return "MATERIAL_%d" % sub.material_index


def submesh_bindings(data, sub, roots=None):
    """[(capa, textura)] de la submalla, con los nombres del archivo."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    layers = parse_name_table(data, find_root(roots, "NAME-MATERIALLAYERS"))
    textures = parse_texture_names(data, roots)
    pairs = []
    for layer_idx, tex_idx in sub.bindings:
        layer = layers[layer_idx] if layer_idx < len(layers) else "LAYER%d" % layer_idx
        if tex_idx < len(textures) and textures[tex_idx]:
            pairs.append((layer, textures[tex_idx]))
    return pairs


def _pick_texture(pairs, layer_names, prefixes):
    for layer, tex in pairs:
        if layer.upper() in layer_names:
            return tex
    for layer, tex in pairs:
        if tex.upper().startswith(prefixes):
            return tex
    return None


def _loose_texture(base_dir, tex_name):
    """Sin text_DOT.py: una imagen suelta junto a la malla, como antes."""
    path = resolve_texture_path(base_dir, tex_name)
    if not path:
        return None
    tex = rapi.loadExternalTex(path)
    if tex:
        tex.name = tex_name
    return tex


def build_submesh_material(data, sub, textures, roots, cache, materials,
                           tex_list):
    """Un NoeMaterial por combinacion de material y texturas.

    *textures* es un text_DOT.TextureSource, una carpeta (sin text_DOT.py, solo
    imagenes sueltas) o None para no poner ninguna. Los NoeTexture que se usan
    se anaden a *tex_list*: un NoeMaterial solo nombra la textura, y Noesis la
    busca por ese nombre en la lista de texturas del modelo.
    """
    pairs = submesh_bindings(data, sub, roots)
    key = (sub.material_index, tuple(pairs))
    if key in cache:
        return cache[key]

    name = submesh_material_name(data, sub, roots)
    if any(name == mat.name for mat in materials):
        name = "%s_%d" % (name, len(materials))

    mat = NoeMaterial(name, "")
    diffuse = _pick_texture(pairs, ("DIFFUSE", "DIFFUSEMAP", "ALBEDO"), ("TD_",))
    normal = _pick_texture(pairs, ("NORMALMAP", "NORMAL", "BUMP"), ("TN_",))
    spec = _pick_texture(pairs, ("SPECULARMAP", "SPECULAR", "SPEC"), ("TS_",))

    found = {}
    for tex_name, slot in ((diffuse, "diffuse"), (normal, "normal"), (spec, "specular")):
        if not tex_name or textures is None:
            continue
        if isinstance(textures, str):
            tex = _loose_texture(textures, tex_name)
        else:
            tex = textures.get(tex_name)
        if tex is None:
            print("DOT: %s sin textura %s" % (name, tex_name))
            continue
        if not any(t is tex for t in tex_list):
            tex_list.append(tex)
        found[slot] = tex
        if slot == "diffuse":
            mat.setTexture(tex.name)
        elif slot == "normal":
            mat.setNormalTexture(tex.name)
        else:
            mat.setSpecularTexture(tex.name)

    try:
        flags = noesis.NMATFLAG_TWOSIDED
        if "normal" in found:
            flags |= noesis.NMATFLAG_NORMALMAP
        mat.setFlags(flags)
    except Exception:
        pass

    if "diffuse" not in found:
        try:
            mat.setDiffuseColor(NoeVec4((0.82, 0.82, 0.86, 1.0)))
        except Exception:
            pass

    materials.append(mat)
    cache[key] = mat
    return mat


# --------------------------------------------------------------------------
# Las animaciones
#
# SCENE-ANIMATIONS trae un bloque ANIMATION por nodo animado:
#
#     ANIMATION-HEADER       3 x u32: [0, fps (24 o 30), n_claves]
#     ANIMATION-FLAGS        4 x u32
#     ANIMATION-KEYFRAMES    n_claves x 44 bytes
#     ANIMATION-KEYFRAMESVIS n_claves x u32 (1 = visible)
#
# Los 44 bytes son 11 floats: traslacion (3), cuaternion (4) en orden x y z w,
# escala (3) y un hueco a cero. Una clave por fotograma, sin comprimir.
#
# Las pistas van en el orden de NAME-JOINTS -la tabla que va en profundidad, no
# el de los registros de SKELETON, que van por anchura- y se agrupan por escena,
# saltandose la raiz de cada una: en SK_ChampionHumanMale_VIP_04, 96 pistas son
# las 81 del ENVIRONMENT menos su raiz mas las 17 de RENDERABLES menos la suya.
# La tanda siguiente se reconoce porque reempieza por Hips.
#
# Casarlas no es cuestion de contar: la primera clave de cada pista lleva
# exactamente la traslacion local de su hueso, tres floats que sirven de firma.
# Componer esa primera clave por la jerarquia devuelve la pose de reparto -46 de
# 46 huesos y 81 de 81 por debajo de 1e-5 en las dos muestras medidas-, asi que
# el propio archivo trae con que comprobar que el reparto es el correcto.
# --------------------------------------------------------------------------

DOT_BONE_QUAT = 88
DOT_BONE_LOCAL = 120
DOT_ANIM_KEY = 44
DOT_ANIM_FPS = 30
DOT_ANIM_EPS = 1e-4


class DotTrack(object):
    """Una pista: los fotogramas de un nodo, tal como estan en el archivo."""

    def __init__(self, fps, keys):
        self.fps = fps
        self.keys = keys        # [(traslacion, cuaternion, escala)]

    def __repr__(self):
        return "<pista %d claves a %d fps>" % (len(self.keys), self.fps)


def skeleton_locals(data, roots=None):
    """[(cuaternion, traslacion)] local de cada registro de SKELETON."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    found = find_scene_skeleton(data, roots)
    if found is None:
        return []
    body, stride, count = found
    out = []
    for i in range(count):
        off = body + i * stride
        out.append((struct.unpack_from("<4f", data, off + DOT_BONE_QUAT),
                    struct.unpack_from("<3f", data, off + DOT_BONE_LOCAL)))
    return out


def parse_animations(data, roots=None):
    """Las pistas de SCENE-ANIMATIONS, en el orden del archivo."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    sec = find_root(roots, "SCENE-ANIMATIONS")
    if sec is None:
        return []
    tracks = []
    for anim in sec.children:
        head = anim.child("ANIMATION-HEADER")
        keys = anim.child("ANIMATION-KEYFRAMES")
        if head is None or keys is None or head.size < 12:
            continue
        _zero, fps, count = struct.unpack_from("<3I", data, head.body)
        if count <= 0 or keys.size < count * DOT_ANIM_KEY:
            continue
        frames = []
        for k in range(count):
            v = struct.unpack_from("<11f", data, keys.body + k * DOT_ANIM_KEY)
            frames.append((v[0:3], v[3:7], v[7:10]))
        tracks.append(DotTrack(fps or DOT_ANIM_FPS, frames))
    return tracks


def joint_order(records, names):
    """Los registros en el orden de NAME-JOINTS, que es el de las pistas."""
    by_name = {}
    for i, (_parent, name_index, _rows, _pos) in enumerate(records):
        if name_index < len(names):
            by_name.setdefault(names[name_index], i)
    return [by_name[n] for n in names if n in by_name]


def map_tracks(tracks, records, names, locals_):
    """{pista: registro} de la primera tanda, la del esqueleto del modelo.

    Avanza a la vez por las pistas y por los huesos en orden de NAME-JOINTS,
    y da por casada una pista cuando su primera clave repite la traslacion
    local del hueso. Los huesos sin pista no rompen la marcha; una pista que no
    encaje en lo que queda de esqueleto es ya de otra escena y corta la tanda.
    """
    order = joint_order(records, names)
    layout = [locals_[bone][1] if bone < len(locals_) else (1e9, 1e9, 1e9)
              for bone in order]
    return dict((index, order[step])
                for index, step in _align(tracks, range(len(tracks)), layout).items())


def _same_point(a, b):
    return (abs(a[0] - b[0]) < DOT_ANIM_EPS and abs(a[1] - b[1]) < DOT_ANIM_EPS
            and abs(a[2] - b[2]) < DOT_ANIM_EPS)


def _mat_from_quat(quat):
    """El cuaternion del archivo como filas de una 3x3, en la convencion de
    NoeMat43: las filas son los ejes y el vector va por la izquierda.

    Es la misma que usa `invbind_to_model()` para la pose de reparto, y es la
    que hay que respetar: componer estas matrices por la jerarquia devuelve las
    posiciones del bind en las muestras medidas.
    """
    x, y, z, w = quat
    return (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y + z * w), 2.0 * (x * z - y * w)),
        (2.0 * (x * y - z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z + x * w)),
        (2.0 * (x * z + y * w), 2.0 * (y * z - x * w), 1.0 - 2.0 * (x * x + y * y)),
    )


def _noe_mat(rows, trans):
    """Una NoeMat43 a partir de nuestras filas, transponiendo la 3x3.

    Aqui esta el detalle que costo encontrar: lo que este plugin llama *filas*
    son los ejes del hueso -la convencion en la que el vector va por la
    izquierda-, y NoeMat43 guarda las filas de la matriz. Mientras no hubo
    animacion daba igual: el esqueleto se dibuja por las posiciones, que son la
    fila 3, y sin animacion Noesis no deforma nada. En cuanto hay animacion, la
    cuenta que hace Noesis es anim por la inversa del bind, y con las dos en
    convenciones distintas el personaje sale hecho trizas. Medido contra la
    deformacion de referencia del arnes: transponiendo las dos, el FBX que
    escribe Noesis reproduce vertice a vertice lo que calcula el arnes.
    """
    mat = NoeMat43()
    mat[0] = NoeVec3((rows[0][0], rows[1][0], rows[2][0]))
    mat[1] = NoeVec3((rows[0][1], rows[1][1], rows[2][1]))
    mat[2] = NoeVec3((rows[0][2], rows[1][2], rows[2][2]))
    mat[3] = NoeVec3(trans)
    return mat


def build_anim(name, tracks, records, names, locals_, bones):
    """Un NoeAnim con la matriz local de cada hueso en cada fotograma.

    Se entrega en matrices y no en cuaterniones a proposito: NoeKeyFramedAnim
    ocupa menos pero obliga a acertar la convencion del NoeQuat de Noesis, y
    equivocarse ahi no da un error, da un personaje hecho trizas moviendose.

    Y las matrices van en el espacio del modelo, compuestas por la jerarquia,
    igual que las de los huesos: pasarlas en local dejaba el cuerpo entero mas
    o menos donde toca pero las puntas de la cadena -dedos, empunadura- por los
    aires, que es justo lo que se ve cuando una animacion se entrega en el
    espacio equivocado. El orden de la lista es el que pide NoeAnim: un bloque
    de huesos por fotograma.

    Los huesos sin pista se quedan en su transformada de reparto.
    """
    if not tracks or not records:
        return None, {}
    mapping = map_tracks(tracks, records, names, locals_)
    if not mapping:
        return None, {}
    fps = tracks[0].fps or DOT_ANIM_FPS
    frames = max(len(tracks[i].keys) for i in mapping) or 1
    by_bone = dict((bone, tracks[i]) for i, bone in mapping.items())
    quietos = []
    for i in range(len(records)):
        quat, trans = locals_[i] if i < len(locals_) else ((0.0, 0.0, 0.0, 1.0),
                                                          (0.0, 0.0, 0.0))
        quietos.append((_mat_from_quat(quat), trans))

    mats = []
    for frame in range(frames):
        for bone in range(len(records)):
            track = by_bone.get(bone)
            if track is None:
                rows, trans = quietos[bone]
            else:
                key = track.keys[frame if frame < len(track.keys) else -1]
                rows, trans = _mat_from_quat(key[1]), key[0]
            mats.append(_noe_mat(rows, trans))

    anim = NoeAnim(name or "DEFAULT", bones, frames, mats, float(fps))
    return anim, {"pistas": len(tracks), "casadas": len(mapping),
                  "fps": fps, "fotogramas": frames, "mapeo": mapping}


def animation_name(data, roots=None):
    """El nombre del clip, de NAME-SCENEANIMATIONS."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    names = parse_name_table(data, find_root(roots, "NAME-SCENEANIMATIONS"))
    return names[0] if names else "DEFAULT"


def scene_skeletons(data, roots=None):
    """[(escena, inicio, stride, cuenta)] de cada SKELETON del archivo.

    `find_scene_skeleton()` se queda con el mayor, que es el del modelo. Este
    los devuelve todos, que hacen falta para saber de donde sale cada pista:
    las escenas de efectos traen su propio esqueleto, mas corto.
    """
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    scenes = find_root(roots, "SCENES")
    if scenes is None:
        return []
    names = parse_name_table(data, find_root(roots, "NAME-ROOTSCENES"))
    out = []
    for scene in scenes.children:
        sec = scene.child("SKELETON")
        if sec is None or sec.size < 8:
            continue
        count = struct.unpack_from("<I", data, sec.body)[0]
        rest = sec.size - 8
        if count <= 0 or rest <= 0 or rest % count != 0:
            continue
        stride = rest // count
        body = sec.body + 4
        if stride < DOT_BONE_STRIDE or body + count * stride > len(data):
            continue
        record = scene.child("SCENE-DATA")
        name = ""
        if record is not None and record.size >= 12:
            fields = struct.unpack_from("<3I", data, record.body)
            name = names[fields[0]] if fields[0] < len(names) else ""
        out.append((name, body, stride, count))
    return out


def skeleton_locals_at(data, body, stride, count):
    """Las traslaciones locales en el orden de NAME-JOINTS, que es el de las
    pistas: los registros van por anchura y la tabla de nombres en profundidad,
    asi que ordenarlos por su indice de nombre alinea las dos listas."""
    rows = []
    for i in range(count):
        off = body + i * stride
        rows.append((struct.unpack_from("<I", data, off + DOT_BONE_NAME)[0],
                     struct.unpack_from("<3f", data, off + DOT_BONE_LOCAL)))
    rows.sort(key=lambda row: row[0])
    return [loc for _name, loc in rows]


def _align(tracks, indices, locals_):
    """Las pistas de *indices* que encajan, en orden, contra *locals_*."""
    mapping = {}
    bone = 0
    for index in indices:
        track = tracks[index]
        if not track.keys:
            continue
        trans = track.keys[0][0]
        step = bone
        while step < len(locals_):
            if _same_point(locals_[step], trans):
                mapping[index] = step
                bone = step + 1
                break
            step += 1
        else:
            break
    return mapping


def classify_tracks(data, roots, tracks, played):
    """(en otras escenas, sin atribuir) de las pistas que no se reproducen.

    Las pistas van en tandas seguidas, una por escena, pero ni el orden de las
    tandas es el de SCENES ni toda escena trae tanda: se avanza por lo que
    queda y en cada corte se prueba con que esqueleto se llega mas lejos.

    Medido el 2026-09-13 sobre los 114 archivos de Troops con animacion: de
    3722 pistas, 3318 caen en el esqueleto de alguna escena y 404 no se
    atribuyen -318 de ellas en SK_MinotaurHumanMale_SuperRare_06_Shop, que trae
    342 pistas de una sola clave-. Esas son las que animan nodos de escena, que
    quedan fuera de esta tanda.
    """
    pending = [i for i in range(len(tracks)) if i not in played]
    if not pending:
        return 0, 0
    layouts = [skeleton_locals_at(data, body, stride, count)
               for _name, body, stride, count in scene_skeletons(data, roots)]
    others = 0
    while pending and layouts:
        best = {}
        for locals_ in layouts:
            mapping = _align(tracks, pending, locals_)
            if len(mapping) > len(best):
                best = mapping
        if not best:
            break
        others += len(best)
        pending = [i for i in pending if i not in best]
    return others, len(pending)


# --------------------------------------------------------------------------
# Animacion de nodos de escena
#
# Las explosiones, los arcos, los escudos y el hacha que gira no tienen
# esqueleto: lo que se mueve son los propios nodos de la escena. Sus pistas van
# en el mismo SCENE-ANIMATIONS, tambien en tandas, pero alineadas contra los
# nodos *con malla* en profundidad, no contra huesos.
#
# Medido el 2026-09-13 sobre los 128 archivos sin esqueleto con animacion: de
# 1777 pistas, 1164 casan asi y 102 de los 128 archivos casan enteros.
#
# La malla no se mueve de donde esta: se le inventa un hueso por nodo con la
# matriz del nodo como pose de reparto y se le atan los vertices con peso 1. En
# el fotograma 0 la cuenta de Noesis -anim por la inversa del bind- da la
# identidad, asi que la colocacion estatica queda exactamente igual que antes;
# lo unico que se anade es que ahora se mueve.
#
# Solo entran los nodos que cuelgan de un padre identidad, que son 1614 de los
# 1665 medidos: para los demas habria que componer la cadena y eso es otra
# vuelta.
# --------------------------------------------------------------------------


def mesh_scene_nodes(data, roots=None):
    """[(ordinal de malla, nombre, trans, quat, padre identidad)] en profundidad."""
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    scenes = find_root(roots, "SCENES")
    if scenes is None:
        return []
    names = parse_name_table(data, find_root(roots, "NAME-SUBSCENES"))
    out = []

    def walk(section, parent_identity):
        record = section.child("SCENE-DATA")
        identity = parent_identity
        if record is not None and record.size >= DOT_SCENE_DATA:
            fields = struct.unpack_from("<3I10f", data, record.body)
            trans, quat = fields[3:6], fields[6:10]
            if fields[1] != DOT_SCENE_NO_MESH:
                name = names[fields[0]] if fields[0] < len(names) else "NODO"
                out.append((fields[1], name, trans, quat, parent_identity))
            identity = (parent_identity
                        and all(abs(v) < 1e-6 for v in trans)
                        and abs(quat[3] - 1.0) < 1e-6)
        children = section.child("SCENE-CHILDREN")
        if children is not None:
            for kid in children.children:
                walk(kid, identity)

    for scene in scenes.children:
        walk(scene, True)
    return out


def map_node_tracks(tracks, nodes):
    """{pista: posicion en *nodes*}, por tandas como las de los huesos."""
    mapping = {}
    pending = list(range(len(tracks)))
    while pending:
        batch = {}
        node = 0
        for index in pending:
            keys = tracks[index].keys
            if not keys:
                continue
            step = node
            while step < len(nodes):
                _ordinal, _name, trans, quat, _identity = nodes[step]
                if _same_point(trans, keys[0][0]) and _same_quat(quat, keys[0][1]):
                    batch[index] = step
                    node = step + 1
                    break
                step += 1
            else:
                break
        if not batch:
            break
        mapping.update(batch)
        pending = [i for i in pending if i not in batch]
    return mapping


def _same_quat(a, b):
    return all(abs(x - y) < DOT_ANIM_EPS for x, y in zip(a, b))


def plan_node_animation(data, roots, tracks):
    """(huesos, sus matrices de reparto, {malla: hueso}, {hueso: pista}).

    Devuelve listas vacias si el archivo no es de este caso.
    """
    nodes = mesh_scene_nodes(data, roots)
    if not nodes or not tracks:
        return [], [], {}, {}
    mapping = map_node_tracks(tracks, nodes)
    if not mapping:
        return [], [], {}, {}

    # La raiz comun no es un adorno: sin ella Noesis escribe cada hueso suelto
    # como un esqueleto propio, y el FBX sale con un armature vacio por nodo y
    # la malla sin modificador.
    raiz = _noe_mat(_mat_from_quat((0.0, 0.0, 0.0, 1.0)), (0.0, 0.0, 0.0))
    bones = [NoeBone(0, "SCENE", raiz)]
    binds = [raiz]
    por_malla = {}
    por_hueso = {}
    for index in sorted(mapping):
        ordinal, name, trans, quat, identity = nodes[mapping[index]]
        if not identity or ordinal in por_malla:
            continue
        bone = len(bones)
        mat = _noe_mat(_mat_from_quat(quat), trans)
        bones.append(NoeBone(bone, name, mat, "SCENE", 0))
        binds.append(mat)
        por_malla[ordinal] = bone
        por_hueso[bone] = tracks[index]
    if len(bones) == 1:
        return [], [], {}, {}
    return bones, binds, por_malla, por_hueso


def build_node_anim(name, bones, binds, por_hueso, fps):
    """Un NoeAnim con la matriz de cada nodo animado, fotograma a fotograma."""
    if not bones or not por_hueso:
        return None, 0
    frames = max(len(track.keys) for track in por_hueso.values()) or 1
    mats = []
    for frame in range(frames):
        for bone in range(len(bones)):
            track = por_hueso.get(bone)
            if track is None:
                mats.append(binds[bone])
                continue
            key = track.keys[frame if frame < len(track.keys) else -1]
            mats.append(_noe_mat(_mat_from_quat(key[1]), key[0]))
    return NoeAnim(name or "DEFAULT", bones, frames, mats, float(fps)), frames


def set_preview_fps(fps):
    """Le dice al visor a que velocidad reproducir, que no la deduce del clip.

    Noesis no usa los fps del NoeAnim para reproducir: tiene un ajuste propio
    -30 por defecto- que manda sobre lo que diga la animacion, y su ReadMe lo
    cuenta al presentar la opcion "always use anim framerate" del data viewer.
    Medido sobre SK_RangerRagnarFemale_SuperRare_01_Game, que declara 24: el
    contador del visor avanzaba a 30,3 fps, un 26 % rapido. Con esto el clip
    corre a lo que dice la cabecera sin tocar ningun ajuste a mano.
    """
    try:
        rapi.setPreviewOption("setAnimSpeed", str(int(round(fps))))
    except Exception as exc:
        print("DOT: no se pudo fijar la velocidad del visor (%s)" % exc)


def load_node_animation(data, roots, mdl, tracks, bones, binds, por_hueso):
    """La animacion de los nodos de escena, para los archivos sin esqueleto."""
    anim, frames = build_node_anim(
        animation_name(data, roots), bones, binds, por_hueso,
        tracks[0].fps if tracks else DOT_ANIM_FPS)
    if anim is None:
        return
    try:
        mdl.setAnims([anim])
    except Exception as exc:
        print("DOT: Noesis no acepto la animacion de nodos (%s)" % exc)
        return
    set_preview_fps(tracks[0].fps if tracks else DOT_ANIM_FPS)
    print("DOT: animacion de nodos %s — %d fotograma(s) a %d fps, %d nodo(s)"
          " animado(s) de %d pista(s)" % (
              animation_name(data, roots), frames,
              tracks[0].fps if tracks else DOT_ANIM_FPS, len(por_hueso),
              len(tracks)))
    sueltas = len(tracks) - len(por_hueso)
    if sueltas > 0:
        print("DOT: %d pista(s) de nodo sin casar; se quedan quietas" % sueltas)


def load_animation(data, roots, mdl, records, names, bones):
    """Le pasa a Noesis la animacion del esqueleto del modelo, si la hay."""
    tracks = parse_animations(data, roots)
    if not tracks:
        # Decirlo: la mayoria de los archivos no traen clip, y sin una linea
        # aqui uno se queda mirando el visor sin saber si falla el plugin.
        print("DOT: el archivo no trae animacion (SCENE-ANIMATIONS vacia)")
        return
    anim, info = build_anim(
        animation_name(data, roots), tracks, records, names,
        skeleton_locals(data, roots), bones)
    if anim is None:
        print("DOT: %d pista(s) de animacion que no casan con el esqueleto"
              % len(tracks))
        return
    try:
        mdl.setAnims([anim])
    except Exception as exc:
        print("DOT: Noesis no acepto la animacion (%s)" % exc)
        return
    set_preview_fps(info["fps"])
    print("DOT: animacion %s — %d fotograma(s) a %d fps, %d de %d pista(s)"
          " en el esqueleto" % (
              animation_name(data, roots), info["fotogramas"], info["fps"],
              info["casadas"], info["pistas"]))
    sobran = info["pistas"] - info["casadas"]
    if sobran:
        otras, sueltas = classify_tracks(data, roots, tracks, info["mapeo"])
        detalle = "%d en el esqueleto de otra escena" % otras if otras else ""
        if sueltas:
            detalle += (", " if detalle else "") + "%d de nodos de escena" % sueltas
        print("DOT: %d pista(s) mas que no se reproducen: %s" % (sobran, detalle))


def report_textures(textures, tex_list):
    """Una linea con de donde salio cada textura que la malla usa."""
    if textures is None:
        if not want_textures() and text_DOT is not None:
            print("DOT: sin texturas (%s)" % DOT_OPT_NOTEX)
        return
    sources = getattr(textures, "sources", None)
    if sources is None:
        print("DOT: %d textura(s) sueltas" % len(tex_list))
        return
    embedded = sum(1 for origin in sources.values() if origin == "incrustada")
    print("DOT: %d textura(s): %d incrustadas, %d de otro archivo" % (
        len(tex_list), embedded, len(sources) - embedded))
    for key in sorted(sources):
        print("DOT:   %-28s <- %s" % (textures.display_name(key), sources[key]))


def collect_meshes(data, roots=None):
    """Las mallas que el archivo declara, con su geometria ya situada.

    Una entrada por bloque MESH del arbol; dentro, una submalla por capa de
    material, cada una con su propio buffer de vertices e indices.
    """
    if roots is None:
        roots, _stop, _clean = section_tree(data)
    joint_count = len(parse_joint_names(data, roots))
    meshes = enumerate_meshes(data, roots)
    for info in meshes:
        info.submeshes = parse_submeshes(data, info)
        info.resolved = resolve_geometry(data, info, info.submeshes)
        if not info.resolved:
            print("DOT: %s sin reparto de geometria (v=%d i=%d geo=%d)" % (
                info.name, info.vert_count, info.index_count, info.geo.size))
            continue
        resolve_vertex_layout(data, info, joint_count)
    return meshes


def noepyLoadModel(data, mdlList):
    noesis.logPopup()

    if text_DOT is not None:
        payload = text_DOT.bof_payload(data)
        if payload is None:
            print("DOT: no es un Echo BOF legible")
            return 0
        data = payload

    roots, stop, clean = section_tree(data)
    if not clean or stop != len(data):
        print("DOT: aviso — el arbol solo cubre %d de %d bytes" % (stop, len(data)))

    meshes = collect_meshes(data, roots)
    if not meshes:
        print("DOT: el archivo no declara ninguna malla")
        return 0

    joint_names = parse_joint_names(data, roots)
    skeleton = parse_skeleton(data, roots)
    scene_nodes = parse_scene_nodes(data, roots)
    scene_count = len(set(node.scene for node in scene_nodes.values()))
    input_name = rapi.getInputName()
    base_dir = rapi.getDirForFilePath(input_name) if input_name else ""
    if not want_textures():
        textures = base_dir if text_DOT is None and base_dir else None
    else:
        textures = text_DOT.TextureSource(data, base_dir)

    # Los archivos sin esqueleto -explosiones, armas, props- animan sus nodos.
    # Se les inventa un hueso por nodo y se les atan los vertices, sin mover
    # nada de sitio: la colocacion estatica queda igual.
    node_bones, node_binds, node_por_malla, node_por_hueso = [], [], {}, {}
    node_tracks = [] if skeleton else parse_animations(data, roots)
    if node_tracks:
        (node_bones, node_binds, node_por_malla,
         node_por_hueso) = plan_node_animation(data, roots, node_tracks)

    mat_cache = {}
    materials = []
    tex_list = []
    noe_meshes = []
    min_x = min_y = min_z = 1e9
    max_x = max_y = max_z = -1e9
    total_verts = 0
    total_tris = 0
    skinned_verts = 0
    con_peso = set()          # huesos que alguna malla usa de verdad
    placed_meshes = 0
    skipped_markers = 0
    markers_on = want_markers()

    for info in meshes:
        if not info.resolved:
            continue
        palette = bone_palette(data, info)
        node = scene_nodes.get(info.ordinal)
        if scene_nodes and node is None:
            print("DOT: aviso — %s sin nodo de escena, se deja en su sitio"
                  % info.name)
        place = None if node is None or node.is_identity() else node
        if place is not None:
            placed_meshes += 1
        for sub in info.submeshes:
            # Las cajas de efectos ensucian la vista: se cargan solo si se piden.
            if is_marker_mesh(sub, node) and not markers_on:
                skipped_markers += 1
                continue
            positions = []
            normals = []
            uvs = []
            weights = []
            for vi in range(sub.vert_count):
                px, py, pz = struct.unpack_from(
                    "<3f", data, sub.vstart + vi * sub.stride)
                if place is not None:
                    px, py, pz = place.point(px, py, pz)
                positions.append(NoeVec3((px, py, pz)))
                if px < min_x: min_x = px
                if py < min_y: min_y = py
                if pz < min_z: min_z = pz
                if px > max_x: max_x = px
                if py > max_y: max_y = py
                if pz > max_z: max_z = pz

                nx, ny, nz = read_normal(data, sub, vi)
                if place is not None:
                    nx, ny, nz = place.direction(nx, ny, nz)
                length = (nx * nx + ny * ny + nz * nz) ** 0.5
                if length > 1e-6:
                    normals.append(NoeVec3((nx / length, ny / length, nz / length)))
                else:
                    normals.append(NoeVec3((0.0, 1.0, 0.0)))

                u, v = read_uv(data, sub, vi)
                uvs.append(NoeVec3((u, v, 0.0)))

                pairs = read_skin(data, sub, vi, palette)
                if skeleton:
                    pairs = [pair for pair in pairs if pair[0] < len(skeleton)]
                if pairs:
                    weights.append(NoeVertWeight(
                        [b for b, _w in pairs], [w for _b, w in pairs]))
                    con_peso.update(b for b, w in pairs if w > 0.0)
                    skinned_verts += 1
                else:
                    weights.append(NoeVertWeight([], []))

            indices = read_indices(data, sub)
            indices = indices[: len(indices) - (len(indices) % 3)]

            mat = build_submesh_material(
                data, sub, textures, roots, mat_cache, materials, tex_list)
            mesh = NoeMesh(indices, positions, sub.name, mat.name)
            mesh.setNormals(normals)
            mesh.setUVs(uvs)
            if sub.skinned and palette:
                mesh.setWeights(weights)
            elif info.ordinal in node_por_malla:
                hueso = node_por_malla[info.ordinal]
                mesh.setWeights([NoeVertWeight([hueso], [1.0])
                                 for _ in range(sub.vert_count)])
            noe_meshes.append(mesh)

            total_verts += sub.vert_count
            total_tris += len(indices) // 3
            extra = ""
            if scene_count > 1 and info.ordinal in scene_nodes:
                extra += "  escena=%s" % scene_nodes[info.ordinal].scene
            if place is not None:
                extra += "  nodo=(%.3f,%.3f,%.3f)" % tuple(place.trans)
            print("DOT: %-30s v=%-6d t=%-6d stride=%-3d mat=%-16s%s" % (
                sub.name, sub.vert_count, len(indices) // 3, sub.stride,
                mat.name, extra))

    if placed_meshes:
        print("DOT: %d malla(s) colocadas por su nodo de escena" % placed_meshes)
    if skipped_markers:
        print("DOT: %d caja(s) de efectos fuera de la vista"
              " (RENDERABLES/SFX/WEAPONDAMAGE); %s para dibujarlas" % (
                  skipped_markers, DOT_OPT_MARKERS))
    print("DOT: %d malla(s), %d submalla(s), %d verts, %d triangulos" % (
        len(meshes), len(noe_meshes), total_verts, total_tris))
    print("DOT: caja (%.3f,%.3f,%.3f)-(%.3f,%.3f,%.3f)" % (
        min_x, min_y, min_z, max_x, max_y, max_z))

    bones = build_bones(
        joint_names, (min_x, max_x, min_y, max_y, min_z, max_z), skeleton, con_peso)
    if bones:
        print("DOT: %d hueso(s), %d vertice(s) con peso" % (
            len(bones), skinned_verts))

    if not bones and node_bones:
        bones = node_bones

    report_textures(textures, tex_list)

    mdl = NoeModel(noe_meshes, bones)
    mdl.setModelMaterials(NoeModelMaterials(tex_list, materials))
    if bones:
        mdl.setBones(bones)
        if node_bones and bones is node_bones:
            load_node_animation(data, roots, mdl, node_tracks, node_bones,
                                node_binds, node_por_hueso)
        else:
            load_animation(data, roots, mdl, skeleton, joint_names, bones)

    mdlList.append(mdl)
    print("DOT: OK — UV: tecla U")
    return 1
