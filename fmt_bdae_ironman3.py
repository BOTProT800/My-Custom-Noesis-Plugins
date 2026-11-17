# Noesis plugin - Gameloft BDAE Iron Man 3 (version 0,0,0,868)
#
# COPY THIS FILE to: Noesis/plugins/python/fmt_bdae_ironman3.py
# REMOVE from that folder: fmt_SpiderManUnlimited_bdae.py (conflicts with .bdae)
#
# On Noesis startup you should see: "Gameloft BDAE 868 (Iron Man 3) plugin loaded"
#
# IM3: BRES, header_size=44, version "0,0,0,868"
# Disney 965 plugin (header 64) does NOT work with Iron Man 3 files.
#
# Skinned/static vertex: float3 position @0 after VB base correction.
# header_ext points near attrib tables; real VB starts at header_ext+48.
# Format 2 = SHORT (UVs). Loads mesh only for now (no bones / weights / anim).

from inc_noesis import *
import noesis
import rapi
import re
import struct
import sys

_USE_BYTES = sys.version_info[0] >= 3

BDAE_SIGNATURE = "BRES"
BDAE_VERSION_MARKER = "0,0,0,868"
BDAE_VERSION_MARKER_ALT = "0.0.0.868"
BDAE_BOM = 0xFFFE
IM3_HEADER_SIZE = 44
IM3_MODEL_INFO_OFFSET = 0x20
MAX_VERTEX_COUNT = 100000
SUPPORTED_VERSIONS = ("0,0,0,868", "0.0.0.868")
VB_AFTER_HEADER_EXT = 48
FMT_FLOAT = 6
FMT_SHORT = 2


def _is_ascii_text(text):
    try:
        text.encode("ascii")
        return True
    except (UnicodeEncodeError, AttributeError):
        return False


def _bdae_info_offset(base, rel):
    return base - 4 + rel


def _ensure_binary(data):
    if not data:
        return b"" if _USE_BYTES else ""
    if _USE_BYTES:
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("latin1", "replace")
        return bytes(data)
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = None
    if unicode_type is not None and isinstance(data, unicode_type):
        return data.encode("latin1", "replace")
    return data


def _read_cstring(data, offset):
    if offset <= 0 or offset >= len(data):
        return ""
    if _USE_BYTES:
        end = data.find(b"\x00", offset)
        if end < 0:
            return ""
        return data[offset:end].decode("ascii", "replace")
    end = data.find("\x00", offset)
    if end < 0:
        return ""
    return data[offset:end]


def _looks_like_mesh_name(data, offset):
    name = _read_cstring(data, offset)
    if len(name) < 2 or len(name) > 64:
        return False
    if not _is_ascii_text(name):
        return False
    lower = name.lower()
    if any(ext in lower for ext in (".tga", ".pvr", ".png", ".dds")):
        return False
    if lower.endswith("-translation") or lower.endswith("-rotation") or lower.endswith("-scale"):
        return False
    return re.match(r"^[A-Za-z][\w\-.]*$", name) is not None


def _is_mesh_label(name):
    if not name or len(name) > 64:
        return False
    lower = name.lower()
    if any(ext in lower for ext in (".tga", ".pvr", ".png", ".dds")):
        return False
    return re.match(r"^[A-Za-z0-9_#.\-]+$", name) is not None


def _read_model_sections(data, model_off):
    p = model_off + 4 + 104
    sections = {}
    tex_count, tex_rel = struct.unpack_from("<II", data, p)
    sections["textures"] = (tex_count, _bdae_info_offset(p, tex_rel))
    p += 16
    mat_count, mat_rel = struct.unpack_from("<II", data, p)
    sections["materials"] = (mat_count, _bdae_info_offset(p, mat_rel))
    p += 8
    mesh_count, mesh_rel = struct.unpack_from("<II", data, p)
    sections["meshes"] = (mesh_count, _bdae_info_offset(p, mesh_rel))
    p += 8
    morph_count, morph_rel = struct.unpack_from("<II", data, p)
    sections["mesh_morphers"] = (morph_count, _bdae_info_offset(p, morph_rel))
    p += 32
    node_count, node_rel = struct.unpack_from("<II", data, p)
    sections["node_collections"] = (node_count, _bdae_info_offset(p, node_rel))
    return sections


def _score_mesh_entry_offsets(data, offsets):
    mesh_hits = 0
    label_hits = 0
    for entry_off in offsets:
        if entry_off + 16 > len(data):
            continue
        name_off, friendly_off, _, _ = struct.unpack_from("<IIII", data, entry_off)
        primary = _looks_like_mesh_name(data, name_off)
        secondary = _looks_like_mesh_name(data, friendly_off)
        if not primary and not secondary:
            continue
        label_hits += 1
        name = _read_cstring(data, name_off) if primary else ""
        friendly = _read_cstring(data, friendly_off) if secondary else ""
        label = name or friendly
        if label.lower().endswith("-mesh") or friendly.lower().endswith("-mesh"):
            mesh_hits += 1
    return mesh_hits, label_hits


def _mesh_entry_offsets_for_layout(table_off, count, preamble, stride):
    base = table_off + preamble
    return [base + i * stride for i in range(count)]


def _find_mesh_table(data, model_off):
    best = None
    scan_end = min(len(data), model_off + 512)
    for off in range(model_off, scan_end, 4):
        count, rel = struct.unpack_from("<II", data, off)
        if not (1 <= count <= 64):
            continue
        table_off = off - 4 + rel
        if table_off < 0x1000:
            continue
        for preamble, stride in ((0, 24), (8, 16), (0, 16)):
            if table_off + preamble + count * stride > len(data):
                continue
            valid = 0
            primary_names = 0
            mesh_names = 0
            for i in range(count):
                entry_off = table_off + preamble + i * stride
                name_off, friendly_off, _, rel2 = struct.unpack_from("<IIII", data, entry_off)
                data_off = entry_off + 8 + rel2
                if rel2 < 0x100000 and data_off < 0x100:
                    continue
                primary = _looks_like_mesh_name(data, name_off)
                friendly = _looks_like_mesh_name(data, friendly_off)
                if primary:
                    primary_names += 1
                    name = _read_cstring(data, name_off)
                    if name.lower().endswith("-mesh"):
                        mesh_names += 1
                if friendly:
                    fname = _read_cstring(data, friendly_off)
                    if fname.lower().endswith("-mesh"):
                        mesh_names += 1
                if primary or friendly:
                    valid += 1
            if valid == 0:
                continue
            score = mesh_names * 1000 + valid * 100 + primary_names * 10 - abs(count - 12)
            if best is None or score > best[3]:
                best = (count, table_off + preamble, valid, score)
    if best is None:
        return 0, 0
    return best[0], best[1]


def _resolve_mesh_entries(data, model_off):
    candidates = []
    try:
        sections = _read_model_sections(data, model_off)
        sec_count, sec_off = sections["meshes"]
    except Exception:
        sec_count, sec_off = 0, 0

    layouts = (
        (8, 16, "section+8/16"),
        (0, 16, "section/16"),
        (0, 24, "section/24"),
        (8, 24, "section+8/24"),
    )
    if 1 <= sec_count <= 64 and 0 < sec_off < len(data):
        for preamble, stride, label in layouts:
            offsets = _mesh_entry_offsets_for_layout(sec_off, sec_count, preamble, stride)
            if offsets and offsets[-1] + 16 <= len(data):
                mesh_hits, label_hits = _score_mesh_entry_offsets(data, offsets)
                score = mesh_hits * 1000 + label_hits * 10
                candidates.append((score, mesh_hits, offsets, label))

    heur_count, heur_off = _find_mesh_table(data, model_off)
    if 1 <= heur_count <= 64 and 0 < heur_off < len(data):
        for preamble, stride, label in (
            (0, 24, "heuristic/24"),
            (0, 16, "heuristic/16"),
            (8, 16, "heuristic+8/16"),
        ):
            offsets = _mesh_entry_offsets_for_layout(heur_off, heur_count, preamble, stride)
            if offsets and offsets[-1] + 16 <= len(data):
                mesh_hits, label_hits = _score_mesh_entry_offsets(data, offsets)
                score = mesh_hits * 1000 + label_hits * 10
                candidates.append((score, mesh_hits, offsets, label))

    if not candidates:
        return [], "none"
    candidates.sort(key=lambda item: (item[0], item[1], -len(item[2])), reverse=True)
    return candidates[0][2], candidates[0][3]


def _guess_prop_count(data, hdr):
    for prop_count in (5, 3, 4, 6, 7, 8, 10):
        meta_off = hdr + 8 + prop_count * 8
        if meta_off + 16 > len(data):
            continue
        header_ext, zero, one, _ = struct.unpack_from("<IIII", data, meta_off)
        if zero == 0 and one == 1 and 32 <= header_ext <= 4096:
            return prop_count
    return 5


def _read_mesh_header_meta(data, hdr):
    submesh_count, vertex_count = struct.unpack_from("<II", data, hdr)
    prop_count = _guess_prop_count(data, hdr)
    pairs = []
    for i in range(prop_count):
        pairs.append(struct.unpack_from("<II", data, hdr + 8 + i * 8))
    meta_off = hdr + 8 + prop_count * 8
    header_ext, _, _, data_rel = struct.unpack_from("<IIII", data, meta_off)
    return submesh_count, vertex_count, prop_count, pairs, header_ext, data_rel


def _mesh_prop_info_offset(pair_off, rel):
    return pair_off + 4 + rel


def _read_prop_tables(data, hdr, pairs):
    tables = []
    for idx, pair in enumerate(pairs):
        count, rel = pair
        base = _mesh_prop_info_offset(hdr + 8 + idx * 8, rel)
        vals = []
        if 0 < count <= 16 and 0 <= base < len(data):
            for j in range(count):
                off = base + j * 4
                if off + 4 > len(data):
                    break
                vals.append(struct.unpack_from("<I", data, off)[0])
        tables.append(vals)
    return tables


def _read_attrib_tables_after_bbox(data, hdr, prop_count):
    """IM3 stores bbox (6 floats) then contiguous attrib tables before the VB."""
    meta_off = hdr + 8 + prop_count * 8
    table_off = meta_off + 16 + 24
    need = prop_count * 4 * 4
    if table_off < 0 or table_off + need > len(data):
        return None
    first = struct.unpack_from("<I", data, table_off)[0]
    if not (12 <= first <= 128):
        return None
    tables = []
    for t in range(4):
        base = table_off + t * prop_count * 4
        tables.append(list(struct.unpack_from("<%dI" % prop_count, data, base)))
    return tables


def _read_vertex_layout(data, hdr, pairs):
    prop_count = len(pairs)
    tables = _read_attrib_tables_after_bbox(data, hdr, prop_count)
    if tables is None:
        tables = _read_prop_tables(data, hdr, pairs)
    while len(tables) < 4:
        tables.append([])

    stride_values = []
    for table in tables[:2]:
        for value in table:
            if 12 <= value <= 128:
                stride_values.append(value)
    stride = max(stride_values) if stride_values else 32
    offsets = tables[1] if tables[1] else [0, 12, 24]
    formats = tables[2] if len(tables) > 2 else []
    if not offsets or max(offsets) >= 128 or offsets[0] > 16:
        if stride >= 48:
            offsets = [0, 12, 24, 36, 48]
        elif stride >= 28:
            offsets = [0, 12, 24]
        else:
            offsets = [0, 12, 24]
            stride = 32
    if stride < 24 or stride > 256:
        offsets = [0, 12, 24]
        stride = 32
    return stride, offsets, formats


def _is_finite_vec3(x, y, z):
    return -10000.0 < x < 10000.0 and -10000.0 < y < 10000.0 and -10000.0 < z < 10000.0


def _decode_position(vb_bytes, base, stride, mode):
    need = {"xyz0": 12, "skin48": 52, "xy_f48": 52, "yz_f48": 52}.get(mode, 12)
    if stride < need or base + need > len(vb_bytes):
        return None
    if mode == "xyz0":
        x, y, z = struct.unpack_from("<3f", vb_bytes, base)
    elif mode == "skin48":
        sx, sy = struct.unpack_from("<2f", vb_bytes, base)
        depth = struct.unpack_from("<f", vb_bytes, base + 48)[0]
        x, y, z = depth, sx, sy
    elif mode == "xy_f48":
        x, y = struct.unpack_from("<2f", vb_bytes, base)
        z = struct.unpack_from("<f", vb_bytes, base + 48)[0]
    elif mode == "yz_f48":
        x, y = struct.unpack_from("<2f", vb_bytes, base + 4)
        z = struct.unpack_from("<f", vb_bytes, base + 48)[0]
    else:
        return None
    if not _is_finite_vec3(x, y, z):
        return None
    return (x, y, z)


def _position_modes_for_stride(stride):
    if stride >= 52:
        return ("xyz0", "xy_f48", "skin48", "yz_f48")
    return ("xyz0",)


def _vec_len(x, y, z):
    return (x * x + y * y + z * z) ** 0.5


def _mesh_has_solid_vertices(data, vb, vertex_count, stride, mode):
    non_zero = 0
    for i in range(vertex_count):
        base = vb + i * stride
        if base + 12 > len(data):
            continue
        pos = _decode_position(data, base, stride, mode)
        if pos is None:
            continue
        x, y, z = pos
        if (abs(x) + abs(y) + abs(z)) > 1e-5:
            non_zero += 1
    if non_zero < max(3, vertex_count // 10):
        return False
    if vertex_count > 100 and float(non_zero) / float(vertex_count) < 0.5:
        return False
    return True


def _score_positions(positions, faces):
    if len(positions) < 3 or not faces:
        return -1e9
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    dz = max(zs) - min(zs)
    diag = (dx * dx + dy * dy + dz * dz) ** 0.5
    if diag < 1e-4:
        return -1e9
    limit = diag * 0.45
    spikes = 0
    max_e = 0.0
    for a, b, c in faces:
        for i, j in ((a, b), (b, c), (c, a)):
            if i >= len(positions) or j >= len(positions):
                continue
            pa, pb = positions[i], positions[j]
            e = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 + (pa[2] - pb[2]) ** 2) ** 0.5
            if e > max_e:
                max_e = e
        # spike if any edge too long
        pa, pb, pc = positions[a], positions[b], positions[c]
        e1 = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 + (pa[2] - pb[2]) ** 2) ** 0.5
        e2 = ((pb[0] - pc[0]) ** 2 + (pb[1] - pc[1]) ** 2 + (pb[2] - pc[2]) ** 2) ** 0.5
        e3 = ((pc[0] - pa[0]) ** 2 + (pc[1] - pa[1]) ** 2 + (pc[2] - pa[2]) ** 2) ** 0.5
        if max(e1, e2, e3) > limit:
            spikes += 1
    kept = len(faces) - spikes
    ratio = max_e / diag if diag else 99.0
    return kept * 10.0 - spikes * 50.0 - ratio * 20.0 + min(diag, 10.0)

def _probe_index_buffer(data, ib_off, vertex_count, min_tris=1, max_tris=None):
    if ib_off < 0 or ib_off + 6 > len(data):
        return None
    tris = []
    pos = ib_off
    limit = max_tris if max_tris is not None else 10 ** 9
    while pos + 6 <= len(data) and len(tris) < limit:
        a, b, c = struct.unpack_from("<3H", data, pos)
        if max(a, b, c) >= vertex_count:
            break
        if a == b or b == c or a == c:
            break
        tris.append((a, b, c))
        pos += 6
    if len(tris) >= min_tris:
        return tris
    return None


def _find_index_buffer(data, start, end, vertex_count, min_tris=1):
    best = None
    scan_end = min(end, len(data) - 6)
    for off in range(max(0, start), scan_end, 2):
        tris = _probe_index_buffer(data, off, vertex_count, min_tris)
        if tris and (best is None or len(tris) > len(best[1])):
            best = (off, tris)
    return best


def _read_declared_face_count(data, hdr, data_rel):
    if data_rel < 0x1000:
        return None
    ext = hdr + data_rel
    if ext + 0x68 > len(data):
        return None
    face_count = struct.unpack_from("<I", data, ext + 0x44)[0]
    index_count = struct.unpack_from("<I", data, ext + 0x64)[0]
    if 1 <= face_count <= 500000 and index_count == face_count * 3:
        return face_count
    if 1 <= face_count <= 500000:
        return face_count
    return None


def _parse_inline_mesh(data, hdr):
    if hdr + 64 > len(data):
        return None
    try:
        submesh_count, vertex_count, _, pairs, header_ext, data_rel = _read_mesh_header_meta(data, hdr)
    except Exception:
        return None
    if not (1 <= submesh_count <= 32 and 3 <= vertex_count <= MAX_VERTEX_COUNT):
        return None

    stride, offsets, formats = _read_vertex_layout(data, hdr, pairs)
    pos_off = offsets[0] if offsets else 0
    norm_off = offsets[1] if len(offsets) > 1 else 12
    uv_off = 24
    for off, fmt in zip(offsets, formats or []):
        if fmt == FMT_SHORT:
            uv_off = off
            break
    else:
        if len(offsets) > 2:
            uv_off = offsets[2]

    vb_candidates = []
    if 32 <= header_ext <= 4096:
        vb_candidates.append(hdr + header_ext + VB_AFTER_HEADER_EXT)
        vb_candidates.append(hdr + header_ext)
    for rel in (0x7C, 0xA0, 0xC0, 0xAC):
        candidate = hdr + rel
        if candidate not in vb_candidates:
            vb_candidates.append(candidate)
    if data_rel < 0x1000:
        candidate = hdr + data_rel
        if candidate not in vb_candidates:
            vb_candidates.append(candidate)

    declared_faces = _read_declared_face_count(data, hdr, data_rel)
    ib_candidates = []
    if data_rel >= 0x1000:
        ib_candidates.append(hdr + data_rel + 0x74)
    ib_hints = [hdr + 0x134, hdr + 0x12C8]

    best = None
    best_score = -1e18
    search_end = min(len(data), hdr + max(data_rel, 0x3000) + 0x4000)
    min_tris = max(1, submesh_count)
    modes = _position_modes_for_stride(stride)

    for vb in vb_candidates:
        if vb <= 0 or vb >= len(data):
            continue

        faces = None
        for ib_off in ib_candidates:
            faces = _probe_index_buffer(
                data, ib_off, vertex_count, min_tris=min_tris, max_tris=declared_faces
            )
            if faces:
                break
        if not faces:
            ib_hit = _find_index_buffer(
                data, vb + vertex_count * stride, search_end, vertex_count, min_tris=min_tris
            )
            if ib_hit:
                _, faces = ib_hit
        if not faces:
            for hint in ib_hints:
                faces = _probe_index_buffer(data, hint, vertex_count, min_tris=1)
                if faces:
                    break
        if not faces:
            continue
        if declared_faces is not None and len(faces) > declared_faces:
            faces = faces[:declared_faces]

        vb_size = vertex_count * stride
        if vb + vb_size > len(data):
            continue
        vb_bytes = data[vb:vb + vb_size]

        for mode in modes:
            if not _mesh_has_solid_vertices(data, vb, vertex_count, stride, mode):
                continue
            positions = []
            ok = True
            for i in range(vertex_count):
                pos = _decode_position(vb_bytes, i * stride, stride, mode)
                if pos is None:
                    ok = False
                    break
                positions.append(pos)
            if not ok:
                continue
            score = _score_positions(positions, faces)
            if score > best_score:
                best_score = score
                best = (vb_bytes, stride, faces, pos_off, norm_off, uv_off, mode)

    return best


def _vertex_count(vb_bytes, stride):
    if stride <= 0:
        return 0
    return len(vb_bytes) // stride


def _sanitize_faces(positions, faces):
    """Drop spike triangles (usually touching corrupt header-overlap verts)."""
    if not positions or not faces:
        return faces, 0, 0.0
    min_x = min_y = min_z = 1e9
    max_x = max_y = max_z = -1e9
    for p in positions:
        x, y, z = p[0], p[1], p[2]
        if x < min_x:
            min_x = x
        if y < min_y:
            min_y = y
        if z < min_z:
            min_z = z
        if x > max_x:
            max_x = x
        if y > max_y:
            max_y = y
        if z > max_z:
            max_z = z
    dx, dy, dz = max_x - min_x, max_y - min_y, max_z - min_z
    diag = (dx * dx + dy * dy + dz * dz) ** 0.5
    max_edge = diag * 0.45 if diag > 1.0 else 20.0

    cleaned = []
    dropped = 0
    for a, b, c in faces:
        if a >= len(positions) or b >= len(positions) or c >= len(positions):
            dropped += 1
            continue
        pa, pb, pc = positions[a], positions[b], positions[c]

        def dist2(p, q):
            vx, vy, vz = p[0] - q[0], p[1] - q[1], p[2] - q[2]
            return vx * vx + vy * vy + vz * vz

        limit2 = max_edge * max_edge
        if dist2(pa, pb) > limit2 or dist2(pb, pc) > limit2 or dist2(pc, pa) > limit2:
            dropped += 1
            continue
        cleaned.append((a, b, c))
    return cleaned, dropped, max_edge


def _recompute_normals(positions, faces):
    normals = [[0.0, 0.0, 0.0] for _ in range(len(positions))]
    for a, b, c in faces:
        pa, pb, pc = positions[a], positions[b], positions[c]
        ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
        vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
        cx = uy * vz - uz * vy
        cy = uz * vx - ux * vz
        cz = ux * vy - uy * vx
        normals[a][0] += cx
        normals[a][1] += cy
        normals[a][2] += cz
        normals[b][0] += cx
        normals[b][1] += cy
        normals[b][2] += cz
        normals[c][0] += cx
        normals[c][1] += cy
        normals[c][2] += cz
    out = []
    for nx, ny, nz in normals:
        length = _vec_len(nx, ny, nz)
        if length < 1e-8:
            out.append(NoeVec3((0.0, 0.0, 1.0)))
        else:
            out.append(NoeVec3((nx / length, ny / length, nz / length)))
    return out


def _build_noe_mesh(label, vb_bytes, stride, faces, pos_off, norm_off, uv_off, pos_mode):
    vertex_count = _vertex_count(vb_bytes, stride)
    if vertex_count == 0:
        return None

    raw_positions = []
    uvs = []
    for i in range(vertex_count):
        base = i * stride
        pos = _decode_position(vb_bytes, base, stride, pos_mode)
        if pos is None:
            return None
        raw_positions.append(pos)
        u, v = 0.0, 0.0
        if base + uv_off + 4 <= len(vb_bytes):
            # Format 2 = SHORT2 UVs mapped into [0,1].
            su, sv = struct.unpack_from("<2h", vb_bytes, base + uv_off)
            u = (su + 32768) / 65535.0
            v = (sv + 32768) / 65535.0
        uvs.append(NoeVec3((u, 1.0 - v, 0.0)))

    cleaned, dropped, _max_edge = _sanitize_faces(raw_positions, faces)
    if len(cleaned) < 1:
        cleaned, dropped = faces, 0

    indices = []
    for a, b, c in cleaned:
        indices.extend((a, b, c))

    positions = [NoeVec3(p) for p in raw_positions]
    normals = _recompute_normals(raw_positions, cleaned)

    mesh = NoeMesh(indices, positions, "im3_" + label)
    mesh.setNormals(normals)
    mesh.setUVs(uvs)
    print(
        "BDAE 868: " + label
        + " - " + str(vertex_count) + " verts, "
        + str(len(cleaned)) + " tris"
        + (" (dropped " + str(dropped) + " spikes)" if dropped else "")
        + ", stride " + str(stride)
        + ", mode=" + pos_mode
    )
    return mesh


def _mesh_entry_header_offset(data, entry_off):
    if entry_off + 16 > len(data):
        return None
    tag, rel = struct.unpack_from("<II", data, entry_off + 8)
    if tag == 0:
        hdr = entry_off + 12 + rel
    else:
        hdr = 0
        candidates = []
        if entry_off + 28 <= len(data):
            candidates.append(entry_off + 24)
        if entry_off + 24 <= len(data):
            hdr_abs = struct.unpack_from("<I", data, entry_off + 20)[0]
            if hdr_abs not in candidates:
                candidates.append(hdr_abs)
        for candidate in candidates:
            if candidate <= 0 or candidate >= len(data):
                continue
            try:
                submesh_count, vertex_count, _, _, header_ext, _ = _read_mesh_header_meta(data, candidate)
            except Exception:
                continue
            if 1 <= submesh_count <= 32 and 3 <= vertex_count <= MAX_VERTEX_COUNT and 64 <= header_ext <= 4096:
                hdr = candidate
                break
        if hdr <= 0:
            return None
    if hdr <= 0 or hdr >= len(data):
        return None
    return hdr


def _parse_mesh_entry(data, entry_off):
    if entry_off + 16 > len(data):
        return None
    name_off, friendly_off, tag, _ = struct.unpack_from("<IIII", data, entry_off)
    name = _read_cstring(data, name_off)
    friendly = _read_cstring(data, friendly_off)
    if not _looks_like_mesh_name(data, name_off) and not _looks_like_mesh_name(data, friendly_off):
        if friendly and _is_mesh_label(friendly):
            name = friendly
        else:
            return None
    if not _looks_like_mesh_name(data, name_off) and _looks_like_mesh_name(data, friendly_off):
        name = friendly
    if not name and friendly:
        name = friendly
    label = friendly or name
    if not _is_mesh_label(label) and not _is_mesh_label(name):
        return None

    hdr = _mesh_entry_header_offset(data, entry_off)
    if hdr is None:
        return None

    if tag == 0:
        parsed = _parse_inline_mesh(data, hdr)
    else:
        parsed = _parse_inline_mesh(data, entry_off + 24)
        if parsed is None and entry_off + 24 <= len(data):
            hdr_abs = struct.unpack_from("<I", data, entry_off + 20)[0]
            if 0x100 < hdr_abs < len(data):
                parsed = _parse_inline_mesh(data, hdr_abs)

    if parsed is None:
        return None
    return label, parsed


def _decode_ascii(raw):
    if raw is None:
        return ""
    if hasattr(raw, "decode"):
        return raw.decode("ascii")
    return raw


def _read_version_bs(data):
    try:
        bs = NoeBitStream(data)
        if _decode_ascii(bs.readBytes(4)) != "BRES":
            return None
        bs.seek(IM3_MODEL_INFO_OFFSET)
        model_off = bs.readInt()
        if model_off <= 0:
            return None
        bs.seek(model_off)
        version_off = bs.readInt()
        if version_off <= 0:
            return None
        bs.seek(version_off)
        version = bs.readString()
        if version and "868" in version:
            return version
    except Exception:
        pass
    return None


def _read_version(data):
    version = _read_version_bs(data)
    if version:
        return version
    if len(data) < 40:
        return None
    try:
        if _USE_BYTES:
            if data[0:4] != b"BRES":
                return None
        else:
            if data[0:4] != BDAE_SIGNATURE:
                return None
        if struct.unpack_from("<H", data, 4)[0] != BDAE_BOM:
            return None
        if struct.unpack_from("<I", data, 8)[0] != IM3_HEADER_SIZE:
            return None
        model_off = struct.unpack_from("<I", data, IM3_MODEL_INFO_OFFSET)[0]
        if model_off <= 0 or model_off + 4 >= len(data):
            return None
        version_off = struct.unpack_from("<I", data, model_off)[0]
        return _read_cstring(data, version_off)
    except Exception:
        return None


def registerNoesisTypes():
    handle = noesis.register("Gameloft BDAE 868 (Iron Man 3)", ".bdae")
    noesis.setHandlerTypeCheck(handle, noepyCheckType)
    noesis.setHandlerLoadModel(handle, noepyLoadModel)
    print("Gameloft BDAE 868 (Iron Man 3) plugin loaded")
    return 1


def _load_file_data(data):
    try:
        input_name = rapi.getInputName()
        if input_name:
            full_data = rapi.loadIntoByteArray(input_name)
            if full_data:
                return _ensure_binary(full_data)
    except Exception:
        pass
    return _ensure_binary(data)


def noepyCheckType(data):
    try:
        bs = NoeBitStream(data)
        magic = _decode_ascii(bs.readBytes(4))
        if magic != "BRES":
            return 0
        bs.readShort()
        bs.readShort()
        if bs.readInt() != IM3_HEADER_SIZE:
            return 0
        return 1
    except Exception:
        return 0


def noepyLoadModel(data, mdlList):
    data = _load_file_data(data)
    version = _read_version(data)
    if version is None:
        print("BDAE 868: could not read version string")
        return 0

    print("BDAE 868: loading " + version)
    try:
        rapi.setPreviewOption("setAngOfs", "0 90 0")
    except Exception:
        pass

    model_off = struct.unpack_from("<I", data, IM3_MODEL_INFO_OFFSET)[0]
    entry_offsets, table_layout = _resolve_mesh_entries(data, model_off)
    if not entry_offsets:
        print("BDAE 868: no mesh table found in " + version)
        return 0
    print("BDAE 868: mesh table " + table_layout + " (" + str(len(entry_offsets)) + " entries)")

    meshes = []
    parsed_entries = []
    for entry_off in entry_offsets:
        parsed = _parse_mesh_entry(data, entry_off)
        if parsed is None:
            continue
        label, mesh_data = parsed
        vb_bytes, stride, faces, pos_off, norm_off, uv_off, pos_mode = mesh_data
        vert_count = len(vb_bytes) // stride if stride else 0
        parsed_entries.append((label, mesh_data, vert_count))

    # Attachment Plane* quads sit at local origin without node TRS in this
    # container — showing them alongside the LOD looks "exploded". Prefer the
    # body/LOD (and skip tiny planes when a real mesh exists).
    has_body = any(vc >= 100 for _, _, vc in parsed_entries)
    for label, mesh_data, vert_count in parsed_entries:
        lower = (label or "").lower()
        if has_body and vert_count <= 4 and "plane" in lower:
            print("BDAE 868: skip attachment " + label + " (no bind TRS in file)")
            continue
        if has_body and "particle" in lower:
            print("BDAE 868: skip " + label + " (particles)")
            continue
        vb_bytes, stride, faces, pos_off, norm_off, uv_off, pos_mode = mesh_data
        noe_mesh = _build_noe_mesh(
            label, vb_bytes, stride, faces, pos_off, norm_off, uv_off, pos_mode
        )
        if noe_mesh is not None:
            meshes.append(noe_mesh)

    if not meshes:
        # Fallback: load everything we parsed.
        for label, mesh_data, _vc in parsed_entries:
            vb_bytes, stride, faces, pos_off, norm_off, uv_off, pos_mode = mesh_data
            noe_mesh = _build_noe_mesh(
                label, vb_bytes, stride, faces, pos_off, norm_off, uv_off, pos_mode
            )
            if noe_mesh is not None:
                meshes.append(noe_mesh)

    if not meshes:
        print("BDAE 868: no exportable meshes (version " + version + ")")
        return 0

    mdl = NoeModel(meshes)
    mdlList.append(mdl)
    return 1
