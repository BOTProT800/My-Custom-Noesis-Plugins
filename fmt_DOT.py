# Dawn of Titans - Echo BOF format 1.0 (NaturalMotion / Echo engine)
# Opens decompressed skeletal meshes with UVs and textures.
#
# Install: copy to Noesis/plugins/python/ (overwrite older copy), restart Noesis.
# Open: *.dotmesh with TD_/TN_ PNGs in the same folder (Anansi mesh export).
# UV view in Noesis: press U.

from inc_noesis import *

import math
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
    handle = noesis.register("Dawn of Titans Echo BOF", ".dotmesh")
    noesis.setHandlerTypeCheck(handle, noepyCheckType)
    noesis.setHandlerLoadModel(handle, noepyLoadModel)

    handle_atc = noesis.register("Dawn of Titans Echo BOF", ".atc")
    noesis.setHandlerTypeCheck(handle_atc, noepyCheckType)
    noesis.setHandlerLoadModel(handle_atc, noepyLoadModel)
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


def _skeleton_stride(body_size, bone_count):
    if bone_count <= 0:
        return None
    rem = body_size - 8
    if rem <= 0 or rem % bone_count != 0:
        return None
    return rem // bone_count


def find_bind_pose_skeleton(data, joint_count):
    marker = b"SKELETON\x00"
    pos = 0
    while True:
        idx = data.find(marker, pos)
        if idx < 0:
            return None
        if idx + 64 <= len(data):
            hdr = struct.unpack_from("<8I", data, idx + 32)
            body_size, bone_count = hdr[0], hdr[1]
            if bone_count != joint_count:
                pos = idx + 1
                continue
            stride = _skeleton_stride(body_size, bone_count)
            if stride is None or stride < 84:
                pos = idx + 1
                continue
            body = idx + 32 + 8
            if body + bone_count * stride > len(data):
                pos = idx + 1
                continue
            return body, stride
        pos = idx + 1


def parse_skeleton_bind_pose(data, joint_names):
    found = find_bind_pose_skeleton(data, len(joint_names))
    if found is None:
        return None
    body, stride = found
    poses = []
    for i in range(len(joint_names)):
        rec = data[body + i * stride : body + (i + 1) * stride]
        if len(rec) < 84:
            return None
        q = struct.unpack_from("<4f", rec, 56)
        t = struct.unpack_from("<3f", rec, 72)
        poses.append((q, t))
    return poses


def quat_trans_to_mat43(qx, qy, qz, qw, tx, ty, tz):
    # Echo root records often store (0,0,±1,0) — treat as identity for FBX bind.
    if (
        abs(qx) < 1e-4
        and abs(qy) < 1e-4
        and abs(abs(qz) - 1.0) < 1e-4
        and abs(qw) < 1e-4
    ):
        qx, qy, qz, qw = 0.0, 0.0, 0.0, 1.0

    length = (qx * qx + qy * qy + qz * qz + qw * qw) ** 0.5
    if length > 1e-8:
        qx, qy, qz, qw = qx / length, qy / length, qz / length, qw / length
    else:
        qx, qy, qz, qw = 0.0, 0.0, 0.0, 1.0

    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    mat = NoeMat43()
    mat[0] = NoeVec3((1.0 - 2.0 * (yy + zz), 2.0 * (xy + wz), 2.0 * (xz - wy)))
    mat[1] = NoeVec3((2.0 * (xy - wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz + wx)))
    mat[2] = NoeVec3((2.0 * (xz + wy), 2.0 * (yz - wx), 1.0 - 2.0 * (xx + yy)))
    mat[3] = NoeVec3((tx, ty, tz))
    return mat


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


def build_bones(names, bounds, data=None):
    if not names:
        return []

    sk_root = names[0] if names[0].upper().startswith("SK_") else None
    name_to_idx = {name: idx for idx, name in enumerate(names)}
    poses = parse_skeleton_bind_pose(data, names) if data else None
    if poses:
        bones = []
        for i, name in enumerate(names):
            (qx, qy, qz, qw), (tx, ty, tz) = poses[i]
            mat = quat_trans_to_mat43(qx, qy, qz, qw, tx, ty, tz)
            parent = guess_parent_dot_a(name, names, sk_root)
            parent_idx = name_to_idx[parent] if parent in name_to_idx else -1
            bones.append(NoeBone(i, name, mat, parent or "", parent_idx))
        print("DOT: bind pose SKELETON (%d bones)" % len(bones))
        return bones

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


def read_section_strings(data, section_name, max_strings=256):
    off = find_section(data, section_name)
    if off < 0:
        return []
    pos = off + 32
    end = min(len(data), off + 65536)
    strings = []
    while pos < end and len(strings) < max_strings:
        z = data.find(b"\x00", pos, end)
        if z <= pos:
            pos += 1
            continue
        raw = data[pos:z]
        pos = z + 1
        if len(raw) < 2:
            continue
        try:
            text = raw.decode("ascii")
        except Exception:
            continue
        if text.startswith("NAME-") and text != section_name:
            break
        if text.isprintable():
            strings.append(text)
    return strings


def parse_texture_names(data):
    names = read_section_strings(data, "NAME-TEXTURES")
    if names:
        return names
    return parse_texture_dependencies(data)


def parse_texture_dependencies(data):
    """TD_/TN_/TL_/TS_ texture names referenced by the BOF."""
    names = read_section_strings(data, "NAME-TEXTURES")
    if names:
        return names

    seen = set()
    deps = []
    pos = 0
    while pos < len(data):
        z = data.find(b"\x00", pos)
        if z <= pos:
            pos += 1
            continue
        raw = data[pos:z]
        pos = z + 1
        if len(raw) < 3 or len(raw) > 120:
            continue
        if not all(32 <= b < 127 for b in raw):
            continue
        try:
            text = raw.decode("ascii")
        except Exception:
            continue
        if text.endswith(".atc.bof"):
            base = text[: -len(".atc.bof")]
            if base not in seen:
                seen.add(base)
                deps.append(base)
            continue
        if text.startswith(("TD_", "TN_", "TL_", "TS_")) and not text.endswith(".atc"):
            if text not in seen:
                seen.add(text)
                deps.append(text)
    return deps


def _read_uv_u16(data, off):
    a, b = struct.unpack_from("<HH", data, off)
    return a / 65535.0, b / 65535.0


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


def _read_uv_half(data, off):
    a, b = struct.unpack_from("<HH", data, off)
    return half_to_float(a), half_to_float(b)


def _uv_edge_ratio_score(data, vstart, vert_count, stride, istart, uoff, fmt, samples=160):
    """Higher = UV stretch better matches 3D triangle stretch."""
    if istart < 0 or istart + 6 > len(data):
        return 0

    def pos(vi):
        return struct.unpack_from("<3f", data, vstart + vi * stride)

    def uv(vi):
        if fmt == "HH":
            return _read_uv_u16(data, vstart + vi * stride + uoff)
        return _read_uv_half(data, vstart + vi * stride + uoff)

    score = 0
    tested = 0
    for t in range(samples):
        off = istart + t * 6
        if off + 6 > len(data):
            break
        i0, i1, i2 = struct.unpack_from("<3H", data, off)
        if max(i0, i1, i2) >= vert_count:
            continue

        def edge3(a, b):
            pa, pb = pos(a), pos(b)
            return ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 + (pa[2] - pb[2]) ** 2) ** 0.5

        def edge_uv(a, b):
            ua, va = uv(a)
            ub, vb = uv(b)
            return ((ua - ub) ** 2 + (va - vb) ** 2) ** 0.5

        e3 = sorted([edge3(i0, i1), edge3(i1, i2), edge3(i2, i0)])
        euv = sorted([edge_uv(i0, i1), edge_uv(i1, i2), edge_uv(i2, i0)])
        if e3[2] <= 1e-6 or euv[2] <= 1e-8:
            continue
        tested += 1
        if abs(e3[0] / e3[2] - euv[0] / euv[2]) < 0.45:
            score += 1

    return score, tested


def detect_uv_offset(data, vstart, vert_count, stride):
    """
    Echo BOF stores UVs as half2 in the vertex (typ. offset 19 on stride 35).
    uint16 pairs at offset 18 are a byte-shifted misread of the same stream.
    """
    if vert_count < 1 or stride < 22:
        return None, None

    istart = vstart + vert_count * stride
    best = None

    for uoff in range(16, min(stride - 3, 28)):
        for fmt in ("half", "HH"):
            ok = 0
            min_u = min_v = 1e9
            max_u = max_v = -1e9
            sample_n = min(vert_count, 512)
            for vi in range(sample_n):
                off = vstart + vi * stride + uoff
                if off + 4 > len(data):
                    break
                if fmt == "HH":
                    u, v = _read_uv_u16(data, off)
                else:
                    u, v = _read_uv_half(data, off)
                if 0.0 <= u <= 1.05 and 0.0 <= v <= 1.05 and u > 0.001 and v > 0.001:
                    ok += 1
                    if u < min_u:
                        min_u = u
                    if v < min_v:
                        min_v = v
                    if u > max_u:
                        max_u = u
                    if v > max_v:
                        max_v = v
            if ok < max(32, sample_n * 0.25):
                continue
            spread_u = max_u - min_u
            spread_v = max_v - min_v
            if spread_u < 0.08 or spread_v < 0.08:
                continue

            corr, tested = _uv_edge_ratio_score(
                data, vstart, vert_count, stride, istart, uoff, fmt
            )
            if tested > 0 and corr < tested * 0.65:
                continue

            ratio = float(corr) / float(tested) if tested > 0 else 0.0
            score = ratio * 10000.0 + ok + spread_u * 10.0 + spread_v * 10.0
            rec = (score, uoff, fmt, ok, spread_u, spread_v, corr, tested)
            if best is None or rec[0] > best[0]:
                best = rec

    if best is None:
        return None, None
    _score, uoff, fmt, ok, spread_u, spread_v, corr, tested = best
    print("DOT: UV offset=%d fmt=%s valid=%d spread=(%.3f,%.3f) stretch=%d/%d" % (
        uoff, fmt, ok, spread_u, spread_v, corr, tested))
    return uoff, fmt


def read_vertex_uv(data, vstart, vi, stride, uv_off, uv_fmt):
    off = vstart + vi * stride + uv_off
    if uv_fmt == "HH":
        u, v = _read_uv_u16(data, off)
    else:
        u, v = _read_uv_half(data, off)
    return u, v


def parse_skin_weights(data, vstart, vi, stride, bone_count, w_off=25, b_off=28):
    """
    Echo BOF skin block (stride 35):
      weights uint8 x3 @25, bone indices uint8 x3 @28.
    Index 0 with tiny weight is an unused slot, not SKL_RAGNAR.
    """
    if stride < b_off + 3 or bone_count <= 0:
        return []
    base = vstart + vi * stride
    if base + b_off + 3 > len(data):
        return []

    w0, w1, w2 = struct.unpack_from("<3B", data, base + w_off)
    b0, b1, b2 = struct.unpack_from("<3B", data, base + b_off)

    raw = []
    for weight, bone_idx in ((w0, b0), (w1, b1), (w2, b2)):
        if weight <= 0 or bone_idx >= bone_count:
            continue
        raw.append((int(bone_idx), float(weight)))

    if not raw:
        return []

    active = [(bi, wt) for bi, wt in raw if bi > 0]
    if not active:
        active = list(raw)

    total = sum(wt for _, wt in active)
    if total <= 1e-6:
        return []
    return [(bi, wt / total) for bi, wt in active]


def detect_skin_offsets(data, vstart, vert_count, stride, bone_count):
    """Find weight/bone byte offsets inside the vertex."""
    if vert_count < 32 or stride < 32 or bone_count <= 0:
        return 25, 28

    best = None
    for w_off in range(16, min(stride - 6, 30)):
        b_off = w_off + 3
        if b_off + 3 > stride:
            continue
        valid = 0
        for vi in range(min(vert_count, 512)):
            pairs = parse_skin_weights(
                data, vstart, vi, stride, bone_count, w_off, b_off
            )
            if pairs:
                valid += 1
        if valid < max(32, min(vert_count, 512) * 0.35):
            continue
        rec = (valid, w_off, b_off)
        if best is None or rec[0] > best[0]:
            best = rec

    if best is None:
        return 25, 28
    valid, w_off, b_off = best
    print("DOT: skin w@%d b@%d valid=%d" % (w_off, b_off, valid))
    return w_off, b_off


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


def load_material_textures(data):
    """Un material con diffuse + normal según NAME-TEXTURES / capas del BOF."""
    input_name = rapi.getInputName()
    base_dir = rapi.getDirForFilePath(input_name) if input_name else ""

    bindings = parse_material_texture_bindings(data)
    diffuse_name = None
    normal_name = None
    spec_name = None

    for layer, tex_name in bindings:
        layer_up = layer.upper()
        tex_up = tex_name.upper()
        if diffuse_name is None and (
            layer_up in ("DIFFUSE", "DIFFUSEMAP", "ALBEDO") or tex_up.startswith("TD_")
        ):
            diffuse_name = tex_name
        elif normal_name is None and (
            layer_up in ("NORMALMAP", "NORMAL", "BUMP") or tex_up.startswith("TN_")
        ):
            normal_name = tex_name
        elif spec_name is None and (
            layer_up in ("SPECULAR", "SPEC") or tex_up.startswith("TS_")
        ):
            spec_name = tex_name

    if diffuse_name is None or normal_name is None:
        for tex_name in parse_texture_names(data):
            tex_up = tex_name.upper()
            if diffuse_name is None and tex_up.startswith("TD_"):
                diffuse_name = tex_name
            elif normal_name is None and tex_up.startswith("TN_"):
                normal_name = tex_name
            elif spec_name is None and tex_up.startswith("TS_"):
                spec_name = tex_name

    mat = NoeMaterial("DOT_mat", "")
    try:
        flags = noesis.NMATFLAG_TWOSIDED
        if normal_name:
            flags |= noesis.NMATFLAG_NORMALMAP
        mat.setFlags(flags)
    except Exception:
        pass

    if diffuse_name:
        tex_path = resolve_texture_path(base_dir, diffuse_name)
        if tex_path:
            tex = rapi.loadExternalTex(tex_path)
            if tex:
                try:
                    mat.setTexture("Diffuse", tex)
                except Exception:
                    pass
                try:
                    mat.setDiffuseTexture(tex)
                except Exception:
                    pass
                print("DOT: diffuse=%s" % tex_path)

    if normal_name:
        n_path = resolve_texture_path(base_dir, normal_name)
        if n_path:
            ntex = rapi.loadExternalTex(n_path)
            if ntex:
                try:
                    mat.setTexture("Normal", ntex)
                except Exception:
                    pass
                try:
                    mat.setNormalTexture(ntex)
                except Exception:
                    pass
                print("DOT: normal=%s" % n_path)

    if spec_name:
        s_path = resolve_texture_path(base_dir, spec_name)
        if s_path:
            stex = rapi.loadExternalTex(s_path)
            if stex:
                try:
                    mat.setTexture("Specular", stex)
                except Exception:
                    pass
                print("DOT: spec=%s" % s_path)

    if diffuse_name is None and normal_name is None:
        try:
            mat.setDiffuseColor(NoeVec4((0.82, 0.82, 0.86, 1.0)))
        except Exception:
            pass

    print("DOT: material bindings=%s" % (
        ", ".join("%s:%s" % (layer, tex) for layer, tex in bindings) or "none"))
    return [mat]


def parse_material_slots(data):
    slots = []
    off = find_section(data, "NAME-MATERIALLAYERS")
    if off < 0:
        off = find_section(data, "NAME-MATERIALS")
    if off < 0:
        return slots
    end = off + 4096
    pos = off + 32
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
        if s.startswith("NAME-"):
            break
        if s.isprintable() and s not in slots:
            slots.append(s)
    return slots


def parse_material_texture_bindings(data):
    layers = parse_material_slots(data)
    textures = parse_texture_names(data)
    if not layers or not textures:
        return [(tex, tex) for tex in textures]
    pairs = []
    for i, layer in enumerate(layers):
        if i < len(textures):
            pairs.append((layer, textures[i]))
    for i in range(len(layers), len(textures)):
        pairs.append((textures[i], textures[i]))
    return pairs


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


def _mesh_bounds_key(min_x, max_x, min_y, max_y, vc_eff):
    return (
        int(round(min_x * 100)),
        int(round(max_x * 100)),
        int(round(min_y * 100)),
        int(round(max_y * 100)),
        int(vc_eff),
    )


def _find_best_mesh_on_geo(data, geo_off, mesh_counts, seen_bounds):
    """Best validated mesh for one MATERIALS-GEOMETRY2 block."""
    best = None
    for vc, ic in sorted(mesh_counts, key=lambda pair: pair[0], reverse=True):
        for delta in (4, 0, 8):
            for stride in (39, 43, 47, 35, 51, 31, 37, 41, 45):
                vstart = geo_off + 32 + delta
                info = validate_submesh(data, vstart, vc, ic, stride, relaxed=True)
                if info is None:
                    continue
                vc_eff, idxs, min_y, max_y, min_x, max_x, min_z, max_z, long_frac = info
                if vc_eff < int(vc * 0.95):
                    continue
                keep_ratio = float(len(idxs)) / float(ic)
                if keep_ratio < 0.95:
                    continue
                if long_frac > 0.10:
                    continue
                span_x = max_x - min_x
                span_y = max_y - min_y
                if span_x < 0.05 or span_y < 0.05:
                    continue
                bounds_key = _mesh_bounds_key(min_x, max_x, min_y, max_y, vc_eff)
                if bounds_key in seen_bounds:
                    continue
                score = vc_eff + keep_ratio * 1000.0 + span_y * 10.0
                rec = (
                    score,
                    vstart,
                    vc_eff,
                    idxs,
                    stride,
                    min_y,
                    max_y,
                    min_x,
                    max_x,
                    vc,
                    ic,
                    bounds_key,
                )
                if best is None or rec[0] > best[0]:
                    best = rec
    return best


def collect_meshes(data):
    """
    Collect all render meshes:
      1) MATERIALS-HEADER chain on the first GEOMETRY2 (only verified links)
      2) Best MESH-HEADER mesh on every GEOMETRY2 block (multi-part heroes)
    """
    loaded = []
    seen_starts = set()
    seen_bounds = set()

    def add_mesh(vstart, vc, ic, stride, label, relaxed=False):
        key = (vstart, vc, ic, stride, relaxed)
        if key in seen_starts:
            return False
        info = validate_submesh(data, vstart, vc, ic, stride, relaxed=relaxed)
        if info is None:
            return False
        seen_starts.add(key)
        vc_eff, idxs, min_y, max_y, min_x, max_x, min_z, max_z, _lf = info
        span_x = max_x - min_x
        span_y = max_y - min_y
        if span_x < 0.05 or span_y < 0.05:
            return False
        bounds_key = _mesh_bounds_key(min_x, max_x, min_y, max_y, vc_eff)
        if bounds_key in seen_bounds:
            return False
        range_key = (vstart, vc_eff, stride)
        for item in loaded:
            if (item[0], item[1], item[3]) == range_key:
                return False
        seen_bounds.add(bounds_key)
        loaded.append((vstart, vc_eff, idxs, stride, min_y, max_y, min_x, max_x))
        print("DOT: %s vc=%d/%d tris=%d stride=%d Y=[%.3f,%.3f] X=[%.3f,%.3f]" % (
            label, vc_eff, vc, len(idxs) // 3, stride, min_y, max_y, min_x, max_x))
        return True

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
            chain_parts = []
            for si, (vc, ic) in enumerate(submesh_counts):
                info = validate_submesh(data, cursor, vc, ic, stride, relaxed=True)
                if info is None:
                    print("DOT: chain stopped at part %d" % si)
                    break
                chain_parts.append((cursor, vc, ic))
                cursor = cursor + vc * stride + ic * 2
            for si, (cursor, vc, ic) in enumerate(chain_parts):
                add_mesh(cursor, vc, ic, stride, "part%d" % si, relaxed=True)

    geos = find_all_sections(data, "MATERIALS-GEOMETRY2")
    mesh_counts = parse_mesh_header_counts(data)
    for gi, geo_off in enumerate(geos):
        best = _find_best_mesh_on_geo(data, geo_off, mesh_counts, seen_bounds)
        if best is None:
            continue
        (
            _score,
            vstart,
            vc_eff,
            idxs,
            stride,
            min_y,
            max_y,
            min_x,
            max_x,
            vc_decl,
            ic_decl,
            bounds_key,
        ) = best
        key = (vstart, vc_decl, ic_decl, stride, True)
        if key in seen_starts:
            continue
        seen_starts.add(key)
        seen_bounds.add(bounds_key)
        loaded.append((vstart, vc_eff, idxs, stride, min_y, max_y, min_x, max_x))
        print("DOT: geo%d vc=%d/%d tris=%d stride=%d Y=[%.3f,%.3f] X=[%.3f,%.3f]" % (
            gi, vc_eff, vc_decl, len(idxs) // 3, stride, min_y, max_y, min_x, max_x))

    print("DOT: collected %d mesh piece(s)" % len(loaded))
    return loaded


def noepyLoadModel(data, mdlList):
    noesis.logPopup()

    print("DOT: file=%d" % len(data))
    loaded = collect_meshes(data)
    if not loaded:
        print("DOT: no submeshes loaded")
        return 0

    names = parse_joint_names(data)
    bone_count = len(names)

    # Merge all parts into one mesh (indices offset per part).
    pos_tuples = []
    uv_tuples = []
    vert_weights = []
    indices = []
    min_x = min_y = min_z = 1e9
    max_x = max_y = max_z = -1e9
    uv_off = None
    uv_fmt = None
    skin_w_off = None
    skin_b_off = None
    skinned_verts = 0

    for part_i, (vstart_i, vc, idxs, stride, _miny, _maxy, _minx, _maxx) in enumerate(loaded):
        if uv_off is None:
            uv_off, uv_fmt = detect_uv_offset(data, vstart_i, vc, stride)
        if skin_w_off is None and bone_count > 0:
            skin_w_off, skin_b_off = detect_skin_offsets(
                data, vstart_i, vc, stride, bone_count
            )

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

            if uv_off is not None and uv_fmt is not None:
                u, v = read_vertex_uv(data, vstart_i, vi, stride, uv_off, uv_fmt)
                uv_tuples.append((u, v))
            else:
                uv_tuples.append((0.0, 0.0))

            if bone_count > 0 and skin_w_off is not None:
                vw = parse_skin_weights(
                    data,
                    vstart_i,
                    vi,
                    stride,
                    bone_count,
                    skin_w_off,
                    skin_b_off,
                )
                if vw:
                    bone_ids = [bi for bi, _w in vw]
                    bone_wts = [_w for _bi, _w in vw]
                    vert_weights.append(NoeVertWeight(bone_ids, bone_wts))
                    skinned_verts += 1
                else:
                    vert_weights.append(NoeVertWeight([], []))

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
    uvs = []
    for vi in range(vert_count):
        px, py, pz = pos_tuples[vi]
        positions.append(NoeVec3((px, py, pz)))
        nx, ny, nz = nrm_acc[vi]
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length > 1e-8:
            normals.append(NoeVec3((nx / length, ny / length, nz / length)))
        else:
            normals.append(NoeVec3((0.0, 1.0, 0.0)))
        u, v = uv_tuples[vi]
        uvs.append(NoeVec3((u, v, 0.0)))

    both = list(indices)
    for t in range(tri_count):
        i0 = indices[t * 3]
        i1 = indices[t * 3 + 1]
        i2 = indices[t * 3 + 2]
        both.extend((i0, i2, i1))

    materials = load_material_textures(data)
    mat_name = materials[0].name if materials else "DOT_mat"

    mesh = NoeMesh(both, positions, "GEO_BODY", mat_name)
    mesh.setNormals(normals)
    mesh.setUVs(uvs)
    if vert_weights and len(vert_weights) == vert_count:
        mesh.setWeights(vert_weights)
        print("DOT: skin weights=%d/%d" % (skinned_verts, vert_count))

    bones = build_bones(names, (min_x, max_x, min_y, max_y, min_z, max_z), data)
    print("DOT: parts=%d joints=%d bones=%d tex=%d" % (
        len(loaded), len(names), len(bones), len(parse_texture_names(data))))

    mdl = NoeModel([mesh], bones)
    mdl.setModelMaterials(NoeModelMaterials([], materials))
    if bones:
        mdl.setBones(bones)

    mdlList.append(mdl)
    print("DOT: OK — texturas: PNGs TD_/TN_ junto al .dotmesh | UV: tecla U")
    return 1
