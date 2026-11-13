"""把 Quaternius Universal Animation Library 的动作烘到 worker_rig.glb 的 21 骨上。

产物：assets/models/worker_anim.json（前端 fetch 后现场拼 THREE.AnimationClip）。
素材：Quaternius Universal Animation Library 标准版，CC0 1.0。
  https://opengameart.org/content/universal-animation-library
  universal_animation_librarystandard.zip
    sha256 18ff1a7215f4852b320203e8aaf02a1578b5c8eef9027fbaedfcedc7b85a3ac2  14,541,205 B
  包内 Godot/AnimationLibrary_Godot_Standard.glb
    sha256 1b7bf67866360665426bb99e4c71bd619f19b408453c24e30f0c3071601eee5c   6,671,104 B
  该 GLB 不入版本库（6.7MB，且随时可重新下载）；只提交烘出来的 JSON。

对齐算法（三个坑，按踩到的顺序）：
  1) 世界增量法：delta = S_world(t)·S_rest⁻¹，T_world = delta·T_rest，local = parent(T_world)⁻¹·T_world。
     肩/肘/髋/膝/踝朝向误差 0°，只有 neck 差 11°（脖子长短不同，可忽略）。
  2) 世界增量必须做共轭 delta' = C·delta·C⁻¹。
     源骨架手臂沿 ±X、目标沿 ±Z（差 90°），不共轭的话"抬臂"会变成绕自身轴扭转，
     表现是手臂平举 / 举过头顶——我第一次烘出来就是这样。
  3) 髋部平移单独搬、按身高比缩放。不搬跪姿悬空；整量搬又不缩放到目标单位会陷进地面。
"""

import os
import json
import math
import re
import struct

import numpy as np

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TGT = os.path.join(_HERE, "assets", "models", "worker_rig.glb")
OUT = os.path.join(_HERE, "assets", "models", "worker_anim.json")
UAL = os.environ.get("UAL_GLB", "")   # 必填：AnimationLibrary_Godot_Standard.glb 的路径
FPS = 30

COMP = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2),
        5125: ("I", 4), 5126: ("f", 4)}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_RESERVED = re.compile(r"[\[\]\.:/]")

# (输出名, 源动作, 是否循环, 起始帧, 结束帧, 是否往返)
# 循环动作要「首尾闭合」，否则播到最后跳回首帧会抖一下；Fixing_Kneeling 是
# 「起→跪→撬→起」的完整叙事，不能直接循环。这里截它跪地段（帧 34~57，
# rootY 恒定 0.223、手持续下探做撬）做往返循环，循环时首尾角差 ≈3°，几乎无跳变。
# 帧号基于 30fps 重采样后的序列。
CLIPS = [
    ("kneel", "Fixing_Kneeling", True, 34, 57, True),    # 0 号：跪在井口持续撬盖
    ("crouch", "Crouch_Idle_Loop", True, 0, None, False),  # 1 号：半蹲扶管
    ("idle", "Idle_Loop", True, 0, None, False),           # 2 号：站着值守
    ("walk", "Walk_Loop", True, 0, None, False),           # 下车走向工位
    ("push", "Push_Loop", True, 0, None, False),           # 备用：双手前撑
]

# 源骨骼名（sanitize 后）→ 目标骨骼名
MAP = {
    "hips": "root",
    # 脊柱：源是 spine001/002/003 三节，目标是 spine/chest 两节。
    # 必须取「累积到最上面那节」的朝向，否则躯干前倾会被吃掉——
    # Fixing_Kneeling 里 spine003 摆了 36°、spine002 才 12°、spine001 是 0°。
    # 早先写 spine002→chest、丢掉 spine003，结果弯腰只剩 12°，
    # 人跪着但上身笔直、两臂下垂，看着就是丧尸。
    "spine002": "spine", "spine003": "chest",
    "neck": "neck", "head": "head",
    # 锁骨不参与重定向：源 shoulder 是从脊柱斜向肩关节的，目标是纯 +Z 直线，
    # 静止方向差 38.5°，用任何单一旋转都消不掉，留着会把误差传进上臂。
    # 它本身的旋转量极小（idle 全段 <4°），保持静止即可。
    "upper_armL": "upperArm_L",
    "forearmL": "foreArm_L", "handL": "hand_L",
    "upper_armR": "upperArm_R",
    "forearmR": "foreArm_R", "handR": "hand_R",
    "thighL": "thigh_L", "shinL": "shin_L", "footL": "foot_L", "toeL": "toe_L",
    "thighR": "thigh_R", "shinR": "shin_R", "footR": "foot_R", "toeR": "toe_R",
}


def load_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, length = struct.unpack("<III", data[:12])
    assert magic == 0x46546C67, "not a glb"
    off, js, bins = 12, None, None
    while off < length:
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            bins = chunk
    return js, bins


def read_accessor(g, bins, idx):
    acc = g["accessors"][idx]
    bv = g["bufferViews"][acc["bufferView"]]
    fmt, size = COMP[acc["componentType"]]
    n = NCOMP[acc["type"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or (size * n)
    out = np.empty((acc["count"], n), dtype=np.float32)
    for i in range(acc["count"]):
        out[i] = struct.unpack_from("<" + fmt * n, bins, start + i * stride)
    return out.reshape(acc["count"], n)


def q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], dtype=np.float64)


def q_inv(q):
    return np.array([-q[0], -q[1], -q[2], q[3]], dtype=np.float64)


def q_slerp(a, b, t):
    d = float(np.dot(a, b))
    if d < 0:
        b = -b
        d = -d
    if d > 0.9995:
        r = a + (b - a) * t
        return r / (np.linalg.norm(r) or 1.0)
    th0 = math.acos(max(-1.0, min(1.0, d)))
    th = th0 * t
    return a * (math.cos(th) - d * math.sin(th) / math.sin(th0)) + b * (math.sin(th) / math.sin(th0))


def q_from_unit_vectors(a, b):
    """把单位向量 a 旋到 b 的最短旋转"""
    a = a / (np.linalg.norm(a) or 1)
    b = b / (np.linalg.norm(b) or 1)
    d = float(np.dot(a, b))
    if d > 0.999999:
        return np.array([0.0, 0.0, 0.0, 1.0])
    if d < -0.999999:
        # 反向：任取一个垂直轴转 180°
        axis = np.cross(a, np.array([0.0, 1.0, 0.0]))
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(a, np.array([1.0, 0.0, 0.0]))
        axis = axis / np.linalg.norm(axis)
        return np.array([axis[0], axis[1], axis[2], 0.0])
    c = np.cross(a, b)
    q = np.array([c[0], c[1], c[2], 1.0 + d])
    return q / np.linalg.norm(q)


def q_rotate(q, v):
    u = np.array(q[:3])
    return v + 2 * np.cross(u, np.cross(u, v) + q[3] * v)


def mat_to_quat(m):
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return np.array([(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s,
                         (m[1, 0] - m[0, 1]) / s, 0.25 * s])
    if m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        return np.array([0.25 * s, (m[0, 1] + m[1, 0]) / s,
                         (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s])
    if m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        return np.array([(m[0, 1] + m[1, 0]) / s, 0.25 * s,
                         (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s])
    s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
    return np.array([(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s,
                     0.25 * s, (m[1, 0] - m[0, 1]) / s])


def node_trs(nd):
    if "matrix" in nd:
        m = np.array(nd["matrix"], dtype=np.float64).reshape(4, 4).T
        return m[:3, 3].copy(), mat_to_quat(m[:3, :3]), np.array(
            [np.linalg.norm(m[:3, 0]), np.linalg.norm(m[:3, 1]), np.linalg.norm(m[:3, 2])])
    return (np.array(nd.get("translation", [0, 0, 0]), dtype=np.float64),
            np.array(nd.get("rotation", [0, 0, 0, 1]), dtype=np.float64),
            np.array(nd.get("scale", [1, 1, 1]), dtype=np.float64))


def sanitize(name):
    n = _RESERVED.sub("", name.replace(" ", "_"))
    return n[4:] if n.startswith("DEF-") else n


def mirror_quat(q):
    """X 轴镜像下的共轭 M·R·M⁻¹；四元数分量顺序是 (x,y,z,w)，所以是 (x,−y,−z,w)。

    推导：M=diag(−1,1,1) 翻转手性，镜像后的旋转＝绕轴 M·a 转 −θ，
    等价于绕 −M·a 转 +θ，故向量部分取 −M·v = (v_x, −v_y, −v_z)，w 不变。
    写成 (x,y,−z,−w) 会把整具骨架翻个个儿（症状：髋高从 +0.917 变成 −0.917）。"""
    return np.array([q[0], -q[1], -q[2], q[3]])


class Rig:
    def __init__(self, path, mirror_x=False):
        self.g, self.bins = load_glb(path)
        self.nodes = self.g["nodes"]
        self.trs = [node_trs(n) for n in self.nodes]
        if mirror_x:
            # 源骨架（Rigify 导出）是「+X 左 / +Z 前」，目标骨架是「+Z 左 / +X 前」，
            # 两者手性相反。逐骨对齐轴向修不了这件事：滚转误差会沿骨链累积，
            # 实测到脊柱拧 18°、大腿前倾 49°、上臂前后反向——这就是"动作像丧尸"的真因。
            # 先把源沿 X 镜像，手性对齐后剩下的就只是一个纯旋转，可以一次拟合。
            for i, (t, r, _s) in enumerate(self.trs):
                t = np.array(t, dtype=np.float64); t[0] = -t[0]
                self.trs[i] = (t, mirror_quat(np.array(r, dtype=np.float64)), _s)
        self.parent = {}
        for i, nd in enumerate(self.nodes):
            for c in nd.get("children", []):
                self.parent[c] = i
        self.order = sorted(range(len(self.nodes)), key=self._depth)
        self.world = [None] * len(self.nodes)
        self.wpos = [None] * len(self.nodes)
        self.fk()

    def _depth(self, i):
        d = 0
        while self.parent.get(i) is not None:
            i = self.parent[i]
            d += 1
        return d

    def fk(self, rot=None, pos=None):
        for i in self.order:
            t, r, _ = self.trs[i]
            if rot is not None and rot[i] is not None:
                r = rot[i]
            if pos is not None and pos[i] is not None:
                t = pos[i]
            p = self.parent.get(i)
            if p is None:
                self.world[i] = np.array(r, dtype=np.float64)
                self.wpos[i] = np.array(t, dtype=np.float64)
            else:
                self.world[i] = q_mul(self.world[p], r)
                self.wpos[i] = self.wpos[p] + q_rotate(self.world[p], t)

    def by_name(self):
        return {sanitize(n.get("name", "")): i for i, n in enumerate(self.nodes)}


def tracks_of(g, bins, anim):
    out = {}
    for ch in anim["channels"]:
        target = ch["target"]
        smp = anim["samplers"][ch["sampler"]]
        times = read_accessor(g, bins, smp["input"])[:, 0].astype(np.float64)
        vals = read_accessor(g, bins, smp["output"]).astype(np.float64)
        interp = smp.get("interpolation", "LINEAR")
        if interp == "CUBICSPLINE":
            vals = vals.reshape(-1, 3, vals.shape[1])[:, 1, :]
        out.setdefault(target["node"], {})[target["path"]] = (times, vals, interp)
    return out


def sample(track, t):
    times, vals, interp = track
    if t <= times[0]:
        return vals[0]
    if t >= times[-1]:
        return vals[-1]
    i = max(0, min(int(np.searchsorted(times, t) - 1), len(times) - 2))
    span = times[i + 1] - times[i]
    u = 0.0 if span <= 0 else (t - times[i]) / span
    if interp == "STEP":
        return vals[i]
    if vals.shape[1] == 4:
        return q_slerp(vals[i], vals[i + 1], u)
    return vals[i] * (1 - u) + vals[i + 1] * u


def main():
    global UAL
    if not UAL:
        raise SystemExit("请设置环境变量 UAL_GLB 指向 AnimationLibrary_Godot_Standard.glb（见文件头下载地址）")
    src, tgt = Rig(UAL, mirror_x=True), Rig(TGT)
    si, ti = src.by_name(), tgt.by_name()

    pairs = [(sk, tk, si[sk], ti[tk]) for sk, tk in MAP.items() if sk in si and tk in ti]
    assert len(pairs) == 19, f"mapping incomplete: {len(pairs)}"

    s_rest = {sk: src.world[s].copy() for sk, tk, s, t in pairs}
    t_rest = {tk: tgt.world[t].copy() for sk, tk, s, t in pairs}
    t_rest_pos = {tk: tgt.wpos[t].copy() for sk, tk, s, t in pairs}

    # 身高比：直接取两个模型 POSITION 的包围盒高度（不受骨骼摆放影响）
    def bbox_h(rig):
        lo, hi = 1e9, -1e9
        for m in rig.g.get("meshes", []):
            for pr in m["primitives"]:
                acc = rig.g["accessors"][pr["attributes"]["POSITION"]]
                lo = min(lo, acc["min"][1])
                hi = max(hi, acc["max"][1])
        return hi - lo

    h_src, h_tgt = bbox_h(src), bbox_h(tgt)
    k = h_tgt / h_src
    print(f"模型高度 源 {h_src:.3f} 目标 {h_tgt:.3f} → 平移比例 k = {k:.4f}")
    tname_of = {t: tk for sk, tk, s, t in pairs}

    # 前向量用脚掌方向（脚踝→脚趾），比髋→颈稳：后者几乎垂直，水平分量接近 0
    def foot_fwd(rig, idx, foot, toe):
        v = rig.wpos[idx[toe]] - rig.wpos[idx[foot]]
        v[1] = 0
        return v / (np.linalg.norm(v) or 1)

    sf = foot_fwd(src, si, "footL", "toeL") + foot_fwd(src, si, "footR", "toeR")
    tf = foot_fwd(tgt, ti, "foot_L", "toe_L") + foot_fwd(tgt, ti, "foot_R", "toe_R")
    sf = sf / (np.linalg.norm(sf) or 1)
    tf = tf / (np.linalg.norm(tf) or 1)
    # 绕 Y 的朝向差
    ang_s = math.atan2(sf[0], sf[2])
    ang_t = math.atan2(tf[0], tf[2])
    yaw_fix = ang_t - ang_s
    print(f"静止朝向修正 yaw = {math.degrees(yaw_fix):.1f}°")
    cy, sy = math.cos(yaw_fix), math.sin(yaw_fix)

    def rot_y(v):
        return np.array([v[0] * cy + v[2] * sy, v[1], -v[0] * sy + v[2] * cy])

    anims = {a.get("name", ""): a for a in src.g.get("animations", [])}
    hips_rest_pos = src.wpos[si["hips"]].copy()

    # ---- 全局旋转 C ----
    # 源已镜像，两侧手性一致，剩下的差异是一个纯旋转。用 Kabsch 在所有骨的
    # 静止方向上做最小二乘拟合（不动点：逐骨各算一个 C 会把滚转误差沿途放大）。
    NEXT = {"root": "spine", "spine": "chest", "chest": "neck", "neck": "head",
            "upperArm_L": "foreArm_L", "foreArm_L": "hand_L",
            "upperArm_R": "foreArm_R", "foreArm_R": "hand_R",
            "thigh_L": "shin_L", "shin_L": "foot_L", "foot_L": "toe_L",
            "thigh_R": "shin_R", "shin_R": "foot_R", "foot_R": "toe_R"}
    s_of = {tk: sk for sk, tk, s, t in pairs}
    t_idx = {tk: t for sk, tk, s, t in pairs}

    def bone_dir(rig, idx, a, b):
        v = rig.wpos[idx[b]] - rig.wpos[idx[a]]
        n = np.linalg.norm(v)
        return None if n < 1e-9 else v / n

    Av, Bv = [], []
    for tk, nb in NEXT.items():
        sk, snb = s_of.get(tk), s_of.get(nb)
        if not sk or not snb or tk not in t_idx or nb not in t_idx:
            continue
        a = bone_dir(src, si, sk, snb)
        b = bone_dir(tgt, ti, tk, nb)
        if a is None or b is None:
            continue
        Av.append(a); Bv.append(b)
    Av = np.array(Av); Bv = np.array(Bv)
    H = Av.T @ Bv
    U_, S_, Vt_ = np.linalg.svd(H)
    R_ = Vt_.T @ U_.T
    if np.linalg.det(R_) < 0:
        Vt_[-1, :] *= -1
        R_ = Vt_.T @ U_.T
    C_GLOBAL = mat_to_quat(R_)
    C_GLOBAL = C_GLOBAL / np.linalg.norm(C_GLOBAL)
    _res = [math.degrees(math.acos(max(-1.0, min(1.0, float((R_ @ Av[i]) @ Bv[i]))))) for i in range(len(Av))]
    print(f"全局 C：{len(Av)} 根骨拟合，残差中位 {np.median(_res):.1f}° 最大 {max(_res):.1f}°，"
          f"旋转角 {math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R_) - 1) / 2)))):.1f}°")

    out_clips = []
    for clip_name, src_name, loop, f0, f1, pingpong in CLIPS:
        anim = anims[src_name]
        tr = tracks_of(src.g, src.bins, anim)
        dur = max(float(t["rotation"][0][-1]) for t in tr.values() if "rotation" in t)
        nframes = int(round(dur * FPS)) + 1
        # 截取帧区间；f1=None 表示到结尾
        seg = list(range(f0, (f1 if f1 is not None else nframes)))
        if pingpong:
            # 往返：正序 + 倒序（不含首尾重复帧），得到首尾闭合的循环
            seg = seg + seg[-2:0:-1]

        rot_out = {tk: [] for sk, tk, s, t in pairs}
        pos_out = []
        # 逐帧记录目标脚底高度，最后整体下移，保证脚踩在地上。
        # 不校正的话人会整体悬空 2~5cm（模型单位），在场景里等于 12~30cm，
        # 远看就是"飘着的假人"——这是"丧尸感"的一半来源。
        lowest = 1e9

        for fi in seg:
            t = min(dur, fi / FPS)
            local_rot = [None] * len(src.nodes)
            local_pos = [None] * len(src.nodes)
            for ni, paths in tr.items():
                if "rotation" in paths:
                    local_rot[ni] = mirror_quat(sample(paths["rotation"], t))
                if "translation" in paths:
                    _pp = np.array(sample(paths["translation"], t), dtype=np.float64)
                    _pp[0] = -_pp[0]
                    local_pos[ni] = _pp
            src.fk(rot=local_rot, pos=local_pos)

            # 逐骨：世界增量 → 目标期望世界 → 目标局部
            # DAMP：源动作的躯干弯曲是按"人体比例"设计的，搬到这个头大颈细的
            # 扫描件上，48° 的腰弯 + 24° 的低头会叠成"头埋进胸口、整个人对折"，
            # 看着像栽倒而不是干活。这里把脊柱/颈/头的增量按比例收一点，
            # 保留"俯身在操作"的意图，去掉对折感。
            DAMP = {"chest": 0.68, "neck": 0.44, "head": 0.36}
            desired = {}
            for sk, tk, s, t in pairs:
                c = C_GLOBAL
                d = q_mul(src.world[s], q_inv(s_rest[sk]))      # 源的世界增量
                d = q_mul(q_mul(c, d), q_inv(c))                # 搬到目标静止朝向的坐标系
                f = DAMP.get(tk)
                if f:
                    d = q_slerp(np.array([0.0, 0.0, 0.0, 1.0]), d, f)
                desired[tk] = q_mul(d, t_rest[tk])
            local = {}
            for sk, tk, s, t in sorted(pairs, key=lambda p: depth(tgt, p[3])):
                p = tgt.parent.get(t)
                pw = desired.get(tname_of.get(p))
                if pw is None:
                    pw = tgt.world[p] if p is not None else np.array([0.0, 0.0, 0.0, 1.0])
                local[tk] = q_mul(q_inv(pw), desired[tk])

            for tk in rot_out:
                q = local[tk]
                n = np.linalg.norm(q)
                rot_out[tk].append([round(float(x / n), 4) for x in q])

            # root 位置：源髋部位移 × k，绕 Y 修正到目标朝向
            d = (src.wpos[si["hips"]] - hips_rest_pos) * k
            rp = t_rest_pos["root"] + q_rotate(C_GLOBAL, d)
            pos_out.append([float(x) for x in rp])
            # 目标这一帧的脚底（脚踝/脚尖最低点）
            for b in ("toe_L", "toe_R", "foot_L", "foot_R"):
                lowest = min(lowest, float(tgt.wpos[ti[b]][1]))

        # 整体下移到脚底贴地（留 0.012 的鞋底厚度）
        if lowest < 1e8:
            shift = lowest - 0.012
            pos_out = [[round(p[0], 4), round(p[1] - shift, 4), round(p[2], 4)] for p in pos_out]
        out_dur = round(len(seg) / FPS, 3)
        out_clips.append({"name": clip_name, "src": src_name, "dur": out_dur,
                          "loop": loop, "rot": rot_out, "rootPos": pos_out})
        print(f"  {clip_name:7s} <- {src_name:18s} {out_dur:5.2f}s {len(seg):4d} 帧"
              + (" (往返)" if pingpong else ""))

    doc = {"fps": FPS, "scale": round(k, 4), "bones": sorted(rot_out.keys()), "clips": out_clips}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    import os
    print(f"\n写出 {OUT}  {os.path.getsize(OUT)/1024:.0f} KB")


def depth(rig, n):
    d = 0
    while rig.parent.get(n) is not None:
        n = rig.parent[n]
        d += 1
    return d


if __name__ == "__main__":
    main()
