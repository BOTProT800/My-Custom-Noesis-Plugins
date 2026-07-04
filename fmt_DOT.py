# Dawn of Titans - Echo BOF format 1.0 (NaturalMotion / Echo engine)
# Opens decompressed skeletal meshes.
#
# Install: copy to Noesis/plugins/python/ (overwrite older copy), restart Noesis.
# Open: files in the dotmesh/ folder (*.dotmesh), or decompressed *.atc BOF payloads.
#
# Vertex stride is NOT fixed (seen: 31, 39, 43). It is auto-detected per file.

from inc_noesis import *

import noesis
import rapi
import struct

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


def registerNoesisTypes():
    for ext in (".dotmesh", ".atc"):
        handle = noesis.register("Dawn of Titans Echo BOF", ext)
        noesis.setHandlerTypeCheck(handle, noepyCheckType)
        noesis.setHandlerLoadModel(handle, noepyLoadModel)
    return 1


def _byte_at(data, idx):
    b = data[idx]
    return b if isinstance(b, int) else ord(b)


def _is_null(data, idx):
    return idx >= 0 and _byte_at(data, idx) == 0


def find_section(data, name, start=0):
    marker = name.encode("ascii") + b"\x00"
    pos = start
    while True:
        idx = data.find(marker, pos)
        if idx < 0:
            return -1
        if idx == 0 or _is_null(data, idx - 1):
            return idx
        pos = idx + 1


def noepyCheckType(data):
    if data is None or len(data) < 64:
        return 0
    if _byte_at(data, 0) == 0x28 and _byte_at(data, 1) == 0xB5:
        return 0
    if data[:8] != DOT_MAGIC:
        return 0
    if data.find(b"Echo BOF format 1.0") < 0:
        return 0
    if find_section(data, "MATERIALS-GEOMETRY2") < 0:
        return 0
    if find_section(data, "MATERIALS-HEADER") < 0:
        return 0
    return 1


def parse_materials_header(data):
    off = find_section(data, "MATERIALS-HEADER")
    if off < 0:
        return 0, 0, 0
    # (hdrSize, unk, unk2, vertCount, indexCount, ...)
    _a, _b, _c, vert_count, index_count = struct.unpack_from("<5I", data, off + 32)
    return _c, vert_count, index_count


def parse_submesh_counts(data):
    """
    Collect (vert_count, index_count) pairs from MATERIALS-HEADER.
    Multi-part characters store several co-located submeshes in a chain:
    [verts0][idx0][verts1][idx1]...
    """
    off = find_section(data, "MATERIALS-HEADER")
    if off < 0:
        return []
    pairs = []
    seen = set()
    for pos in range(off + 32, min(len(data) - 16, off + 600), 4):
        vc, ic, a, b = struct.unpack_from("<4I", data, pos)
        if vc < 50 or vc > 200000:
            continue
        if ic < 100 or ic > 1000000 or ic % 3 != 0 or ic <= vc:
            continue
        if a > 16 or b != 0:
            continue
        key = (vc, ic)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def parse_mesh_header_counts(data):
    """All MESH-HEADER (vert_count, index_count) entries in the file."""
    pairs = []
    seen = set()
    pos = 0
    while True:
        off = data.find(b"MESH-HEADER\x00", pos)
        if off < 0:
            break
        _a, _b, _c, vc, ic = struct.unpack_from("<5I", data, off + 32)
        if vc >= 50 and ic >= 100 and ic % 3 == 0 and ic > vc:
            key = (vc, ic)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
        pos = off + 1
    return pairs


def validate_submesh(data, vstart, vert_count, index_count, stride, relaxed=False):
    """
    Validate a co-located submesh.
    Returns (vc_eff, indices, min_y, max_y, min_x, max_x, min_z, max_z) or None.

    relaxed=True: allow a short invalid tail on positions and drop out-of-range
    indices (secondary MESH-HEADER bodies often have a few garbage verts at end).
    """
    if vstart < 0 or vert_count < 50 or index_count < 100:
        return None

    # Effective vertex count = longest valid run from vstart.
    vc_eff = 0
    min_x = min_y = min_z = 1e9
    max_x = max_y = max_z = -1e9
    limit = vert_count
    for vi in range(vert_count):
        off = vstart + vi * stride
        if off + 12 > len(data):
            break
        x, y, z = struct.unpack_from("<3f", data, off)
        if not _valid_pos(x, y, z):
            break
        vc_eff += 1
        if x < min_x: min_x = x
        if y < min_y: min_y = y
        if z < min_z: min_z = z
        if x > max_x: max_x = x
        if y > max_y: max_y = y
        if z > max_z: max_z = z

    if vc_eff < 50:
        return None
    if not relaxed and vc_eff != vert_count:
        return None
    # Secondary MESH-HEADER bodies often end with a short invalid float run.
    min_run = 0.80 if relaxed else 1.0
    if relaxed and vc_eff < vert_count * min_run:
        return None

    # Pick the vertex count whose trailing index buffer is healthiest.
    # Secondary bodies often have 1-2 "valid-looking" garbage floats before indices.
    vc_candidates = [vc_eff]
    if relaxed:
        for delta in (1, 2, 3, 4, 5, 8, 16, 32, 64):
            vc_try = vc_eff - delta
            if vc_try >= int(vert_count * min_run):
                vc_candidates.append(vc_try)
        if vert_count not in vc_candidates and vert_count <= vc_eff:
            vc_candidates.append(vert_count)

    best = None
    istart_options = []
    for vc_try in vc_candidates:
        istart_options.append((vc_try, vstart + vc_try * stride))
    if relaxed:
        istart_options.append((None, vstart + vert_count * stride))

    for vc_try, istart in istart_options:
        if istart + index_count * 2 > len(data):
            continue
        # If vc_try is None, derive it from max valid index after a pass.
        if vc_try is None:
            max_seen = 0
            for i in range(index_count):
                idx = struct.unpack_from("<H", data, istart + i * 2)[0]
                if idx < vc_eff and idx > max_seen:
                    max_seen = idx
            vc_try = max_seen + 1
            if vc_try < int(vert_count * 0.85):
                continue
        indices = []
        max_idx = 0
        for i in range(index_count):
            idx = struct.unpack_from("<H", data, istart + i * 2)[0]
            if idx < vc_try:
                indices.append(int(idx))
                if idx > max_idx:
                    max_idx = idx
            elif not relaxed:
                indices = []
                break
        if not indices:
            continue
        keep_ratio = float(len(indices)) / float(index_count)
        min_keep = 0.80 if relaxed else 0.95
        if keep_ratio < min_keep or max_idx < vc_try * 0.25:
            continue
        indices = indices[: len(indices) - (len(indices) % 3)]
        if len(indices) < 3:
            continue
        long_frac = _edge_long_fraction_list(
            data, vstart, stride, vc_try, indices
        )
        if long_frac > 0.10:
            continue
        # Recompute bounds for vc_try
        min_x = min_y = min_z = 1e9
        max_x = max_y = max_z = -1e9
        for vi in range(vc_try):
            x, y, z = struct.unpack_from("<3f", data, vstart + vi * stride)
            if x < min_x: min_x = x
            if y < min_y: min_y = y
            if z < min_z: min_z = z
            if x > max_x: max_x = x
            if y > max_y: max_y = y
            if z > max_z: max_z = z
        score = keep_ratio * 1000.0 + (1.0 - long_frac) * 100.0 + vc_try * 0.001
        rec = (score, vc_try, indices, min_y, max_y, min_x, max_x, min_z, max_z, long_frac)
        if best is None or rec[0] > best[0]:
            best = rec

    if best is None:
        return None
    _score, vc_try, indices, min_y, max_y, min_x, max_x, min_z, max_z, long_frac = best
    return vc_try, indices, min_y, max_y, min_x, max_x, min_z, max_z, long_frac


def _edge_long_fraction_list(data, vstart, stride, vert_count, indices):
    edges = []
    for t in range(0, len(indices), 3):
        i0, i1, i2 = indices[t], indices[t + 1], indices[t + 2]
        if max(i0, i1, i2) >= vert_count:
            continue
        p0 = struct.unpack_from("<3f", data, vstart + i0 * stride)
        p1 = struct.unpack_from("<3f", data, vstart + i1 * stride)
        p2 = struct.unpack_from("<3f", data, vstart + i2 * stride)
        for a, b in ((p0, p1), (p1, p2), (p2, p0)):
            dx = a[0] - b[0]
            dy = a[1] - b[1]
            dz = a[2] - b[2]
            edges.append((dx * dx + dy * dy + dz * dz) ** 0.5)
    if len(edges) < 32:
        return 1.0
    edges.sort()
    med = edges[len(edges) // 2]
    if med <= 1e-8:
        return 1.0
    long_n = 0
    limit = med * 5.0
    for e in edges:
        if e > limit:
            long_n += 1
    return float(long_n) / float(len(edges))


def parse_joint_names(data):
    joff = find_section(data, "NAME-JOINTS")
    if joff < 0:
        return []
    end = data.find(b"NAME-BLENDTARGETS", joff)
    if end < 0:
        end = joff + 8192
    pos = joff + 32
    names = []
    root = None
    while pos < end:
        z = data.find(b"\x00", pos)
        if z <= pos:
            pos += 1
            continue
        raw = data[pos:z]
        pos = z + 1
        if len(raw) < 2 or len(raw) > 64:
            continue
        try:
            s = raw.decode("ascii")
        except Exception:
            continue
        bad = False
        for c in s:
            if not (c.isalnum() or c == "_"):
                bad = True
                break
        if bad:
            continue
        if s.startswith("NAME-"):
            break
        if s.startswith("SK_") or s.startswith("Sk_") or s.startswith("sk_"):
            root = s
            continue
        # Accept mixed-case joint names (Hips, SpineA, L_Shoulder, ...).
        if s[0].isalpha() and s not in names:
            names.append(s)
    if root:
        return [root] + names
    return names


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


def decode_packed_normal(packed):
    x = (packed & 0x3FF) / 1023.0 * 2.0 - 1.0
    y = ((packed >> 10) & 0x3FF) / 1023.0 * 2.0 - 1.0
    z = ((packed >> 20) & 0x3FF) / 1023.0 * 2.0 - 1.0
    length = (x * x + y * y + z * z) ** 0.5
    if length > 1e-6:
        return x / length, y / length, z / length
    return 0.0, 1.0, 0.0


def _valid_pos(x, y, z, limit=20.0):
    if x != x or y != y or z != z:
        return False
    return abs(x) < limit and abs(y) < limit and abs(z) < limit


def find_all_sections(data, name):
    out = []
    pos = 0
    while True:
        idx = find_section(data, name, pos)
        if idx < 0:
            return out
        out.append(idx)
        pos = idx + 1


def _score_positions(data, base, vert_count, stride):
    """Return (score, min_y, max_y) or None if positions are not usable."""
    end = base + vert_count * stride
    if end > len(data):
        return None
    ok = 0
    min_x = min_y = min_z = 1e9
    max_x = max_y = max_z = -1e9
    for vi in range(vert_count):
        x, y, z = struct.unpack_from("<3f", data, base + vi * stride)
        if _valid_pos(x, y, z):
            ok += 1
            if x < min_x: min_x = x
            if y < min_y: min_y = y
            if z < min_z: min_z = z
            if x > max_x: max_x = x
            if y > max_y: max_y = y
            if z > max_z: max_z = z
    if ok < vert_count * 0.9:
        return None
    span_x = max_x - min_x
    span_y = max_y - min_y
    span_z = max_z - min_z
    # Character-sized bounds only (reject huge false-positive float runs).
    if span_y < 0.5 or span_y > 2.5:
        return None
    if span_x < 0.3 or span_x > 2.0:
        return None
    if span_z > 1.5:
        return None
    # Reject cube-like garbage (common false positive).
    if span_x > 1.5 and abs(span_x - span_y) < 0.2 and abs(span_y - span_z) < 0.5:
        return None

    centered = 1.0 if abs(min_x + max_x) < max(span_x * 0.35, 0.05) else 0.0
    # Prefer humanoid proportions: tall, moderately wide, shallow.
    proportion = 1.0 if span_y >= span_x * 0.9 and span_y > span_z else 0.0

    def _fit(span, ideal, tolerance):
        dist = abs(span - ideal)
        if dist >= tolerance:
            return 0.0
        return 1.0 - dist / tolerance

    score = (
        ok
        + _fit(span_y, 1.55, 0.9) * 3000.0
        + _fit(span_x, 1.30, 0.7) * 2500.0
        + _fit(span_z, 0.35, 0.4) * 1000.0
        + centered * 1000.0
        + proportion * 1000.0
    )
    return score, min_y, max_y, span_x, span_y


def _index_ok(data, istart, index_count, vert_count):
    if istart < 0 or istart + index_count * 2 > len(data):
        return 0, 0
    ok = 0
    max_idx = 0
    for i in range(index_count):
        idx = struct.unpack_from("<H", data, istart + i * 2)[0]
        if idx < vert_count:
            ok += 1
            if idx > max_idx:
                max_idx = idx
    return ok, max_idx


def _edge_long_fraction(data, vstart, stride, vert_count, istart, index_count):
    """Fraction of triangle edges longer than 5x the median edge (spike detector)."""
    edges = []
    usable = index_count - (index_count % 3)
    for t in range(0, usable, 3):
        tri = []
        bad = False
        for k in range(3):
            idx = struct.unpack_from("<H", data, istart + (t + k) * 2)[0]
            if idx >= vert_count:
                bad = True
                break
            tri.append(struct.unpack_from("<3f", data, vstart + idx * stride))
        if bad:
            continue
        for a, b in ((0, 1), (1, 2), (2, 0)):
            dx = tri[a][0] - tri[b][0]
            dy = tri[a][1] - tri[b][1]
            dz = tri[a][2] - tri[b][2]
            edges.append((dx * dx + dy * dy + dz * dz) ** 0.5)
    if len(edges) < 32:
        return 1.0
    edges.sort()
    med = edges[len(edges) // 2]
    if med <= 1e-8:
        return 1.0
    long_n = 0
    limit = med * 5.0
    for e in edges:
        if e > limit:
            long_n += 1
    return float(long_n) / float(len(edges))


def detect_vertex_layout(data, vert_count, index_count):
    """
    Auto-detect (vstart, stride, istart).

    Positions and indices MUST come from the same GEOMETRY2 layout
    (istart = vstart + vert_count * stride). Mixing blocks causes a
    vertex-index mismatch and a tangled spike mesh.
    """
    geos = find_all_sections(data, "MATERIALS-GEOMETRY2")
    if not geos:
        return None

    best = None
    for geo_off in geos:
        for delta in (4, 0, 8, 12):
            for stride in range(12, 65):
                vstart = geo_off + 32 + delta
                istart = vstart + vert_count * stride
                iok, max_idx = _index_ok(data, istart, index_count, vert_count)
                if iok < index_count * 0.95 or max_idx < vert_count * 0.25:
                    continue

                # Bounds from positions
                ok = 0
                min_x = min_y = min_z = 1e9
                max_x = max_y = max_z = -1e9
                for vi in range(vert_count):
                    x, y, z = struct.unpack_from("<3f", data, vstart + vi * stride)
                    if not _valid_pos(x, y, z):
                        continue
                    ok += 1
                    if x < min_x: min_x = x
                    if y < min_y: min_y = y
                    if z < min_z: min_z = z
                    if x > max_x: max_x = x
                    if y > max_y: max_y = y
                    if z > max_z: max_z = z
                if ok < vert_count * 0.9:
                    continue

                span_x = max_x - min_x
                span_y = max_y - min_y
                span_z = max_z - min_z
                if span_y < 0.2 or span_y > 3.0:
                    continue
                if span_x < 0.05 or span_x > 3.0:
                    continue

                # Reject tangled connectivity (mixed-block symptom).
                long_frac = _edge_long_fraction(
                    data, vstart, stride, vert_count, istart, index_count
                )
                if long_frac > 0.10:
                    continue

                centered = 1.0 if abs(min_x + max_x) < max(span_x * 0.35, 0.05) else 0.0
                proportion = 1.0 if span_y >= span_x * 0.8 else 0.0

                def _fit(span, ideal, tolerance):
                    dist = abs(span - ideal)
                    if dist >= tolerance:
                        return 0.0
                    return 1.0 - dist / tolerance

                score = (
                    ok
                    + iok
                    + _fit(span_y, 1.55, 1.2) * 3000.0
                    + _fit(span_x, 1.30, 1.0) * 2500.0
                    + _fit(span_z, 0.35, 0.5) * 1000.0
                    + centered * 1000.0
                    + proportion * 1000.0
                    + (1.0 - long_frac) * 2000.0
                    + (100.0 if delta == 4 else 0.0)
                )
                rec = (score, vstart, stride, istart, span_y, min_y, max_y, geo_off)
                if best is None or rec[0] > best[0]:
                    best = rec

    if best is None:
        return None
    _score, vstart, stride, istart, span_y, min_y, max_y, geo_off = best
    return vstart, stride, istart, span_y, min_y, max_y, geo_off


def build_bones(names, min_y, max_y, cx, cz):
    if not names:
        return []
    height = max(max_y - min_y, 0.001)
    sk_root = names[0] if names[0].upper().startswith("SK_") else None
    name_set = set(names)
    bones = []
    for i, name in enumerate(names):
        parent = guess_parent(name, name_set, sk_root)
        y = min_y + height * (0.15 + 0.7 * (float(i) / max(len(names) - 1, 1)))
        mat = NoeMat43()
        try:
            mat[3] = NoeVec3((cx, y, cz))
        except Exception:
            pass
        bones.append(NoeBone(i, name, mat, parent))
    return bones


def collect_meshes(data):
    """
    Collect all co-located render meshes:
      1) MATERIALS-HEADER chain at the first working GEOMETRY2
      2) Each MESH-HEADER count pair on every GEOMETRY2 (full-body LODs, etc.)
    """
    # Each entry: (vstart, vc_eff, indices, stride, min_y, max_y, min_x, max_x)
    loaded = []
    seen_starts = set()

    def add_mesh(vstart, vc, ic, stride, label, relaxed=False):
        key = (vstart, vc, ic, stride, relaxed)
        if key in seen_starts:
            return False
        info = validate_submesh(data, vstart, vc, ic, stride, relaxed=relaxed)
        if info is None:
            return False
        seen_starts.add(key)
        vc_eff, idxs, min_y, max_y, min_x, max_x, min_z, max_z, _lf = info
        # De-dupe by effective range.
        range_key = (vstart, vc_eff, stride)
        for item in loaded:
            if (item[0], item[1], item[3]) == range_key:
                return False
        loaded.append((vstart, vc_eff, idxs, stride, min_y, max_y, min_x, max_x))
        print("DOT: %s vc=%d/%d tris=%d stride=%d Y=[%.3f,%.3f] X=[%.3f,%.3f]" % (
            label, vc_eff, vc, len(idxs) // 3, stride, min_y, max_y, min_x, max_x))
        return True

    # --- 1) Materials chain (strict) ---
    submesh_counts = parse_submesh_counts(data)
    _sub, vert_count, index_count = parse_materials_header(data)
    if not submesh_counts and vert_count > 0 and index_count > 0:
        submesh_counts = [(vert_count, index_count)]

    if submesh_counts:
        first_vc, first_ic = submesh_counts[0]
        layout = detect_vertex_layout(data, first_vc, first_ic)
        if layout is None:
            for vc, ic in submesh_counts[1:]:
                layout = detect_vertex_layout(data, vc, ic)
                if layout is not None:
                    submesh_counts = [(vc, ic)] + [p for p in submesh_counts if p != (vc, ic)]
                    break
        if layout is not None:
            vstart, stride, _istart, _span, _miny, _maxy, geo_off = layout
            print("DOT: chain geo=%s vstart=%s stride=%d" % (
                hex(geo_off), hex(vstart), stride))
            cursor = vstart
            for si, (vc, ic) in enumerate(submesh_counts):
                if not add_mesh(cursor, vc, ic, stride, "part%d" % si, relaxed=False):
                    # Header chain parts: allow slight tail garbage.
                    if not add_mesh(cursor, vc, ic, stride, "part%d" % si, relaxed=True):
                        if si == 0:
                            break
                        print("DOT: chain stopped at part %d" % si)
                        break
                # Advance using declared counts (layout in file).
                cursor = cursor + vc * stride + ic * 2

    # --- 2) Standalone MESH-HEADER meshes (relaxed: secondary bodies) ---
    geos = find_all_sections(data, "MATERIALS-GEOMETRY2")
    mesh_counts = parse_mesh_header_counts(data)
    for gi, geo_off in enumerate(geos):
        for vc, ic in mesh_counts:
            already = sum(m[1] for m in loaded)
            if vc == already and len(loaded) > 1:
                continue
            for delta in (4, 0):
                for stride in (31, 39, 43, 47, 51, 35, 37, 41, 45):
                    vstart = geo_off + 32 + delta
                    if add_mesh(vstart, vc, ic, stride, "meshhdr_g%d" % gi, relaxed=True):
                        break
                else:
                    continue
                break

    print("DOT: collected %d mesh piece(s)" % len(loaded))
    return loaded


def noepyLoadModel(data, mdlList):
    noesis.logPopup()

    print("DOT: file=%d" % len(data))
    loaded = collect_meshes(data)
    if not loaded:
        print("DOT: no submeshes loaded")
        return 0

    # Merge all parts into one mesh (indices offset per part).
    pos_tuples = []
    indices = []
    min_x = min_y = min_z = 1e9
    max_x = max_y = max_z = -1e9
    for vstart_i, vc, idxs, stride, _miny, _maxy, _minx, _maxx in loaded:
        base_vertex = len(pos_tuples)
        for vi in range(vc):
            px, py, pz = struct.unpack_from("<3f", data, vstart_i + vi * stride)
            pos_tuples.append((px, py, pz))
            if px < min_x: min_x = px
            if py < min_y: min_y = py
            if pz < min_z: min_z = pz
            if px > max_x: max_x = px
            if py > max_y: max_y = py
            if pz > max_z: max_z = pz
        for idx in idxs:
            indices.append(base_vertex + int(idx))

    vert_count = len(pos_tuples)
    tri_count = len(indices) // 3
    indices = indices[: tri_count * 3]
    print("DOT: merged verts=%d triangles=%d" % (vert_count, tri_count))
    print("DOT: bounds (%.3f,%.3f,%.3f)-(%.3f,%.3f,%.3f)" % (
        min_x, min_y, min_z, max_x, max_y, max_z))

    # Normals from triangles (file normals are unreliable).
    nrm_acc = [[0.0, 0.0, 0.0] for _ in range(vert_count)]
    for t in range(tri_count):
        i0 = indices[t * 3]
        i1 = indices[t * 3 + 1]
        i2 = indices[t * 3 + 2]
        p0 = pos_tuples[i0]
        p1 = pos_tuples[i1]
        p2 = pos_tuples[i2]
        ax, ay, az = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        bx, by, bz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
        nx = ay * bz - az * by
        ny = az * bx - ax * bz
        nz = ax * by - ay * bx
        for i in (i0, i1, i2):
            nrm_acc[i][0] += nx
            nrm_acc[i][1] += ny
            nrm_acc[i][2] += nz

    positions = []
    normals = []
    for vi in range(vert_count):
        px, py, pz = pos_tuples[vi]
        positions.append(NoeVec3((px, py, pz)))
        nx, ny, nz = nrm_acc[vi]
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length > 1e-8:
            normals.append(NoeVec3((nx / length, ny / length, nz / length)))
        else:
            normals.append(NoeVec3((0.0, 1.0, 0.0)))

    both = list(indices)
    for t in range(tri_count):
        i0 = indices[t * 3]
        i1 = indices[t * 3 + 1]
        i2 = indices[t * 3 + 2]
        both.extend((i0, i2, i1))

    mat_name = "DOT_default"
    material = NoeMaterial(mat_name, "")
    try:
        material.setDiffuseColor(NoeVec4((0.82, 0.82, 0.86, 1.0)))
    except Exception:
        pass
    try:
        material.setFlags(noesis.NMATFLAG_TWOSIDED)
    except Exception:
        pass

    mesh = NoeMesh(both, positions, "GEO_BODY", mat_name)
    mesh.setNormals(normals)

    names = parse_joint_names(data)
    cx = (min_x + max_x) * 0.5
    cz = (min_z + max_z) * 0.5
    bones = build_bones(names, min_y, max_y, cx, cz)
    print("DOT: parts=%d joints=%d bones=%d" % (len(loaded), len(names), len(bones)))

    mdl = NoeModel([mesh], bones)
    mdl.setModelMaterials(NoeModelMaterials([], [material]))
    if bones:
        mdl.setBones(bones)

    mdlList.append(mdl)
    print("DOT: OK — press F or A if the view is empty")
    return 1
