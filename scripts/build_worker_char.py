"""从 Quaternius Universal Animation Library 提取自带人形，做成可用的工人角色。

为什么不再用扫描件
------------------
用户提供的 Tripo 扫描件是 6 头身、头大颈细、T-pose 五指张开、无手指骨。自动蒙皮后
颈部无分界（转头把脸拉成楔形）、垂手即五指大张——这两条是"丧尸感"的硬件来源，
蒙皮和动画都改不动。反复修补三轮均未通过目视验收。

UAL 自带的 Mannequin 是 7.3 头身、完整人形网格、**骨架与动作原生匹配**（同一文件），
重定向误差恒为 0。代价是低模白模无脸，靠安全帽 + 顶点色分带补足识别度。

输出
----
assets/models/worker_char.glb   约 8.5k 顶点 / 5 段动画 / 顶点色
  - 顶点色按部位刷：安全帽面 / 脸 / 颈 / 反光背心 / 反光带 / 袖 / 裤 / 靴 / 手套
  - 颜色以 sRGB 写入，并在写 buffer 前转线性（three r128 把顶点色当线性值）

素材：universal_animation_librarystandard.zip
  sha256 18ff1a7215f4852b320203e8aaf02a1578b5c8eef9027fbaedfcedc7b85a3ac2
  包内 Godot/AnimationLibrary_Godot_Standard.glb
  sha256 1b7bf67866360665426bb99e4c71bd619f19b408453c24e30f0c3071601eee5c
可重新下载：https://opengameart.org/content/universal-animation-library
"""

import json
import math
import os
import struct
import sys

import numpy as np

FPS = 30
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "assets", "models", "worker_char.glb")

# 只保留用得到的片段。kneel 是完整叙事（起→跪→撬→起），前端按相位截取，
# 因此这里保留原片，不在这里切——切分逻辑放在 bake 之外，避免两处维护。
KEEP_ANIMS = ["Fixing_Kneeling", "Crouch_Idle_Loop", "Idle_Loop", "Walk_Loop", "Push_Loop"]

# ---- 工人配色（sRGB 0-255）----
C_HELMET = (0xF2, 0xC1, 0x2E)
C_HELMET_D = (0xB8, 0x8C, 0x1E)
C_SKIN = (0xC0, 0x8E, 0x6E)
C_HAIR = (0x3A, 0x30, 0x29)
C_VEST = (0xE0, 0x55, 0x1A)
C_BAND = (0xD2, 0xDA, 0xE0)
C_SLEEVE = (0x2E, 0x33, 0x3B)
# 裤子必须比袖子亮一档、比靴子亮两档，否则整条腿在阴雨光下糊成一团深色，
# 看上去"没有裤子"。这组明度关系是刻意拉开的，不要为了"高级灰"再压暗。
C_PANTS = (0x3B, 0x4B, 0x66)
C_BOOT = (0x10, 0x12, 0x16)
C_GLOVE = (0x24, 0x28, 0x2F)
C_BELT = (0x5A, 0x46, 0x32)
# 五官
C_EYE = (0xF4, 0xF1, 0xEB)
C_PUPIL = (0x1B, 0x15, 0x12)
C_BROW = (0x2A, 0x21, 0x1A)
C_MOUTH = (0x6B, 0x39, 0x2E)
C_NOSE = (0xB2, 0x80, 0x62)


def srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lin(rgb):
    return [srgb_to_linear(v) for v in rgb]


def load_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, length = struct.unpack("<III", data[:12])
    assert magic == 0x46546C67, "not glb"
    off = 12
    js, bin_ = None, None
    while off < length:
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            bin_ = chunk
    return js, bin_


def acc_read(g, bins, i):
    a = g["accessors"][i]
    bv = g["bufferViews"][a["bufferView"]]
    base = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[a["type"]]
    dt = {5126: np.float32, 5123: np.uint16, 5121: np.uint8, 5125: np.uint32}[a["componentType"]]
    return np.frombuffer(bins, dtype=dt, count=a["count"] * ncomp, offset=base).reshape(a["count"], ncomp)


# ---------------------------------------------------------------- 部位判定
def paint(P, prim_index):
    """按 T-pose 位置给每个顶点定颜色。坐标：Y 上、X 左右、Z 前后，身高 1.829。"""
    n = len(P)
    col = np.zeros((n, 4), dtype=np.float32)
    y = P[:, 1]
    ax = np.abs(P[:, 0])

    def put(mask, rgb):
        if mask.any():
            col[mask, :3] = lin(rgb)
            col[mask, 3] = 1.0

    # 默认：裤 + 靴
    put(np.ones(n, bool), C_PANTS)
    # 靴筒到小腿下段 0.195（真实工装靴约 20cm 高），腿在靴筒以上才是裤，
    # 这样"裤—靴"有一条明确的分界，不会整条腿一个色。
    put(y < 0.195, C_BOOT)

    # 腿：裤
    put((y >= 0.195) & (y < 0.965), C_PANTS)

    # 躯干 0.965~1.47：反光背心 + 两条反光带 + 腰带
    # ax 上限放到 0.245：原 0.215 卡在胯宽以内，两侧留下两条裤子色的边，
    # 看着像背心下摆露出半截内裤。手臂区域在下面单独刷（arm 后执行会覆盖重叠部分）。
    torso = (y >= 0.965) & (y < 1.470) & (ax < 0.245)
    put(torso, C_VEST)
    band = torso & (((y > 1.130) & (y < 1.168)) | ((y > 1.285) & (y < 1.323)))
    put(band, C_BAND)
    # 腰带：加宽到 4cm 并提亮成棕色皮带，让裤腰有明确起止
    put((y >= 0.955) & (y < 0.995) & (ax < 0.245), C_BELT)

    # 立领：脖子细长是这具模型的固有比例（下巴 1.615 / 肩 1.47，中间 14.5cm），
    # 补一圈深色衣领把视觉下半段吃掉，脖子看起来就是正常长度。
    put((y >= 1.462) & (y < 1.545) & (ax < 0.098), C_SLEEVE)

    # 肩：背心包不住肩头，肩部直接是深色工作服
    put((y >= 1.395) & (y < 1.560) & (ax > 0.150) & (ax < 0.260), C_SLEEVE)

    # 颈：1.545~1.615，横向窄
    neck = (y >= 1.530) & (y < 1.618) & (ax < 0.105)
    put(neck, C_SKIN)

    # 头：1.618 以上先铺肤色，再把后脑与头顶刷发色。
    # 帽子交给几何（见 build_head_geo），这里**不刷帽色**——头顶只有 1.615/1.662/1.722
    # 三条环带，一旦把 1.722 环刷成黄色，肤色到黄之间会插值出一整片黄额头，
    # 眼睛和眉毛都泡在渐变里。整颗头顶刷深发色，帽子几何从 1.702 盖上去。
    head = y >= 1.618
    put(head, C_SKIN)
    # 后脑与鬓角：z<0 侧、且在帽檐以下
    put(head & (P[:, 2] < -0.008) & (y < 1.745), C_HAIR)
    # 头顶：帽壳是单层曲面，帽箍内壁背面剔除，低角度看会直接看进帽子里面。
    # 这里刷成发色而不是肤色——万一露出来是头发，比露出一颗粉色头顶自然。
    put(head & (y >= 1.745), C_HAIR)

    # 手臂：1.28~1.56 之间 |x|>0.20
    arm = (y >= 1.280) & (y < 1.560) & (ax > 0.200)
    put(arm, C_SLEEVE)
    # 手套：|x| 超过 0.78（腕关节外侧）
    put(arm & (ax > 0.780), C_GLOVE)
    return col


# ---------------------------------------------------------------- 头部几何
# UAL 的 Mannequin 是拿一颗光球当脑袋：整颗头只有约 150 个顶点、三条水平环带
# （y≈1.615 / 1.662 / 1.722）。顶点色只够分出「帽子黄 / 脸肤色」两大块，环带之间
# 全靠插值渐变，任何阈值都切不出五官——这具模型天生没有脸。
# 所以脸和帽子改成几何补：帽壳 + 帽箍 + 帽檐 + 眼球 + 瞳孔 + 眉 + 鼻 + 嘴，
# 全部 JOINTS 指向 DEF-head、WEIGHTS=(1,0,0,0)，跟蒙皮同一套变换，低头时一起转。
#
# 坐标即 T-pose 世界坐标（Y 上 / X 左右 / 脸朝 +Z），数值由实测头部顶点定：
# 脸前沿 z≈0.113~0.118，下巴环 1.615、颧环 1.662、额环 1.722，整头高 1.615~1.829。

class Geo:
    """累积小几何，最后一次性拼进主 primitive。"""

    def __init__(self):
        self.V = []
        self.N = []
        self.C = []
        self.I = []

    def add(self, V, N, rgb, idx):
        base = len(self.V)
        self.V.extend(V)
        self.N.extend(N)
        c = lin(rgb)
        self.C.extend([[c[0], c[1], c[2], 1.0]] * len(V))
        self.I.extend([i + base for i in idx])

    def arrays(self):
        return (np.array(self.V, np.float32).reshape(-1, 3),
                np.array(self.N, np.float32).reshape(-1, 3),
                np.array(self.C, np.float32).reshape(-1, 4),
                np.array(self.I, np.uint32).reshape(-1))


def add_ellipsoid(g, ctr, rad, rgb, seg_u=18, seg_v=7, th0=0.0, th1=math.pi):
    """椭球面片。th 从 0（顶点）向下张开。绕序已按外法线校正。"""
    cx, cy, cz = ctr
    rx, ry, rz = rad
    V, N = [], []
    for j in range(seg_v + 1):
        th = th0 + (th1 - th0) * j / seg_v
        st, ct = math.sin(th), math.cos(th)
        for i in range(seg_u):
            ph = 2 * math.pi * i / seg_u
            sp, cp = math.sin(ph), math.cos(ph)
            nx, ny, nz = st * cp / rx, ct / ry, st * sp / rz
            L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            V.append((cx + rx * st * cp, cy + ry * ct, cz + rz * sp))
            N.append((nx / L, ny / L, nz / L))
    I = []
    for j in range(seg_v):
        for i in range(seg_u):
            a = j * seg_u + i
            b = j * seg_u + (i + 1) % seg_u
            c = (j + 1) * seg_u + (i + 1) % seg_u
            d = (j + 1) * seg_u + i
            I += [a, c, b, a, d, c]
    g.add(V, N, rgb, I)


def add_tube(g, y0, h, rx, rz, cz, rgb, seg=22):
    """竖直椭圆筒侧壁（不开盖），用来做帽壳和帽箍。"""
    V, N = [], []
    for yy in (y0, y0 + h):
        for i in range(seg):
            ph = 2 * math.pi * i / seg
            cp, sp = math.cos(ph), math.sin(ph)
            V.append((rx * cp, yy, cz + rz * sp))
            nx, nz = cp / rx, sp / rz
            L = math.hypot(nx, nz) or 1.0
            N.append((nx / L, 0.0, nz / L))
    I = []
    for i in range(seg):
        a, b = i, (i + 1) % seg
        c, d = seg + (i + 1) % seg, seg + i
        I += [a, c, b, a, d, c]
    g.add(V, N, rgb, I)


def add_annulus(g, y, r0x, r0z, r1x, r1z, cz, thick, rgb, a0, a1, seg=18):
    """水平扇形环（帽檐），带上下两层厚度。

    外沿按 sin^0.45 收口：两端外径收到等于内径，帽檐自然融进帽壳。
    直接切一刀留两个断口，从侧面看是两片翘出帽壳的薄片。
    """
    V, N, I = [], [], []
    for yy, ny in ((y + thick, 1.0), (y, -1.0)):
        for j in range(seg + 1):
            ph = a0 + (a1 - a0) * j / seg
            k = math.sin(math.pi * j / seg) ** 0.45
            ox = r0x + (r1x - r0x) * k
            oz = r0z + (r1z - r0z) * k
            sp, cp = math.sin(ph), math.cos(ph)
            V.append((r0x * sp, yy, cz + r0z * cp))
            V.append((ox * sp, yy, cz + oz * cp))
            N.append((0.0, ny, 0.0))
            N.append((0.0, ny, 0.0))
    row = (seg + 1) * 2
    for j in range(seg):
        a = j * 2
        b, c, d = a + 1, a + 2, a + 3
        I += [a, b, c, b, d, c]                                      # 上表面，法线 +Y
        I += [row + a, row + c, row + b, row + b, row + c, row + d]   # 下表面，法线 -Y
    g.add(V, N, rgb, I)


_BOX_FACES = [((4, 5, 6, 7), (0, 0, 1)), ((1, 0, 3, 2), (0, 0, -1)),
              ((0, 4, 7, 3), (-1, 0, 0)), ((5, 1, 2, 6), (1, 0, 0)),
              ((7, 6, 2, 3), (0, 1, 0)), ((0, 1, 5, 4), (0, -1, 0))]


def add_box(g, ctr, size, rgb):
    """轴对齐盒，每个面独立顶点以获得硬边法线。"""
    cx, cy, cz = ctr
    hx, hy, hz = size[0] / 2.0, size[1] / 2.0, size[2] / 2.0
    corners = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
               (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    V, N, I = [], [], []
    for quad, n in _BOX_FACES:
        base = len(V)
        for k in quad:
            V.append((cx + corners[k][0], cy + corners[k][1], cz + corners[k][2]))
            N.append(n)
        I += [base, base + 1, base + 2, base, base + 2, base + 3]
    g.add(V, N, rgb, I)


def build_head_geo():
    """安全帽 + 五件套面部零件。尺寸按 1.83m 身高、头宽 17.4cm 定。

    帽子是个贴合颅骨的椭球壳（不是圆筒）。早先做成直筒 + 平板檐，比头宽 4cm，
    侧看就是个扣在脑袋上的水桶；现在按头顶轮廓收口，只在前额留一圈 2.8cm 的檐。
    檐不能长：默认机位是 37° 俯视，檐外沿 z 一旦超过 0.155 就会把眼睛遮死。
    """
    g = Geo()
    # 帽壳 = 短筒 + 浅拱。安全帽本来就是这个构型：一圈很短的筒套住颅骨上段，
    # 顶上扣一顶浅拱。筒做高就成了礼帽，拱做尖就成了洋葱。
    # rx 必须 ≥ 0.095：实测头在 y=1.69 处 |x| 已到 0.088，帽壳窄一点脑袋就穿出侧面，
    # 渲染出来像"顶着一片半透明黄塑料"。
    add_tube(g, 1.716, 0.066, 0.0955, 0.1255, 0.004, C_HELMET)
    add_ellipsoid(g, (0, 1.782, 0.004), (0.0955, 0.056, 0.1255), C_HELMET,
                  24, 6, 0.0, math.pi / 2)
    # 帽箍：帽壳下沿的加厚圈，把筒底开口和头之间的接缝盖掉。
    # 顶边必须和筒底 1.716 齐平，早先高 4mm，箍的顶沿露在筒外一圈台阶。
    add_tube(g, 1.702, 0.014, 0.0995, 0.1295, 0.004, C_HELMET_D)
    # 前帽檐：弧形薄片，包住前额 ±46°。
    # 角度以 +Z（脸的方向）为 0：add_annulus 里 x=r·sin(ph)、z=r·cos(ph)，
    # 早先传 π/2±0.8，结果帽子横在耳朵那一侧，还在帽壳上切出一个缺口。
    add_annulus(g, 1.7055, 0.096, 0.126, 0.1085, 0.158, 0.004, 0.0075, C_HELMET,
                -0.80, 0.80)
    # 五官一律做成"贴面扁片"而不是球体。球体在低模脸上会变成三颗往外鼓的零件：
    # 眼白露一大圈、鼻头是个方块、下巴像外星人。压扁到只前凸 2~3mm 才服帖。
    # 脸表面在 y≈1.67 处 z≈0.1135，下面每个件的前沿都算过，凸出量写在注释里。
    for sx in (-1, 1):
        # 眼白：前凸 2.2mm。高度 1.665：帽檐内沿 1.7055，留 4cm 才在俯视机位下露全，
        # 早先放 1.6705，帽檐正好压掉上半，看着像一直眯着眼。
        add_ellipsoid(g, (sx * 0.0302, 1.6650, 0.1105),
                      (0.0118, 0.0138, 0.0052), C_EYE, 14, 6)
        # 瞳：再往前 1.3mm
        add_ellipsoid(g, (sx * 0.0302, 1.6647, 0.1135),
                      (0.0058, 0.0070, 0.0035), C_PUPIL, 12, 5)
    # 眉：完整露出一条，位置贴着帽檐下缘
    for sx in (-1, 1):
        add_box(g, (sx * 0.0312, 1.6885, 0.1085), (0.034, 0.005, 0.007), C_BROW)
    # 鼻：前凸 2mm
    add_box(g, (0, 1.6440, 0.1085), (0.016, 0.024, 0.014), C_NOSE)
    # 嘴：前凸 1.5mm
    add_box(g, (0, 1.6250, 0.1105), (0.030, 0.0055, 0.007), C_MOUTH)
    return g.arrays()


# ---------------------------------------------------------------- GLB 打包
class Packer:
    def __init__(self):
        self.buf = bytearray()
        self.views = []
        self.accessors = []

    def add(self, arr, ctype, atype, target=None, minmax=False, name=None):
        arr = np.ascontiguousarray(arr)
        while len(self.buf) % 4:
            self.buf.append(0)
        off = len(self.buf)
        raw = arr.tobytes()
        self.buf += raw
        bv = {"buffer": 0, "byteOffset": off, "byteLength": len(raw)}
        if target:
            bv["target"] = target
        self.views.append(bv)
        acc = {"bufferView": len(self.views) - 1, "componentType": ctype,
               "count": int(arr.shape[0]), "type": atype}
        if name:
            acc["name"] = name
        if minmax:
            lo = np.atleast_1d(arr.min(axis=0))
            hi = np.atleast_1d(arr.max(axis=0))
            acc["min"] = [float(x) for x in lo]
            acc["max"] = [float(x) for x in hi]
        self.accessors.append(acc)
        return len(self.accessors) - 1


ARRAY_BUFFER = 34962
ELEMENT_ARRAY_BUFFER = 34963


def main():
    src = os.environ.get("UAL_GLB", "")
    if not src or not os.path.exists(src):
        raise SystemExit("请设置 UAL_GLB 指向 AnimationLibrary_Godot_Standard.glb（见文件头下载地址）")
    g, bins = load_glb(src)

    # ---- 骨架节点：从 Rig 根往下全部保留（含 Mannequin）----
    roots = g["scenes"][g.get("scene", 0)]["nodes"]
    order = []
    seen = set()

    def walk(i):
        if i in seen:
            return
        seen.add(i)
        order.append(i)
        for c in g["nodes"][i].get("children", []):
            walk(c)

    for r in roots:
        walk(r)
    old2new = {o: k for k, o in enumerate(order)}

    nodes = []
    for i in order:
        n = dict(g["nodes"][i])
        if "children" in n:
            n["children"] = [old2new[c] for c in n["children"] if c in old2new]
        nodes.append(n)

    # ---- 网格：重映射 JOINTS_0 的关节序号 ----
    skin0 = g["skins"][0]
    joint_old = skin0["joints"]
    joint_new = [old2new[j] for j in joint_old]
    jmap = {o: k for k, o in enumerate(joint_old)}

    pk = Packer()
    prims = []

    # 五官与安全帽挂在 DEF-head 上：JOINTS_0 存的是 skin.joints 数组下标
    head_idx = None
    for k, jn in enumerate(skin0["joints"]):
        if g["nodes"][jn].get("name") == "DEF-head":
            head_idx = k
            break
    if head_idx is None:
        raise SystemExit("源模型里找不到 DEF-head 骨骼，五官无处可挂")
    GV, GN, GC, GI = build_head_geo()
    print("  头部几何 %d 顶点 / %d 三角，挂到 joint[%d]=DEF-head"
          % (len(GV), len(GI) // 3, head_idx))

    for pi, p in enumerate(g["meshes"][0]["primitives"]):
        P = acc_read(g, bins, p["attributes"]["POSITION"]).astype(np.float32)
        N = acc_read(g, bins, p["attributes"]["NORMAL"]).astype(np.float32)
        UV = acc_read(g, bins, p["attributes"]["TEXCOORD_0"]).astype(np.float32)
        J = acc_read(g, bins, p["attributes"]["JOINTS_0"]).astype(np.uint16)
        W = acc_read(g, bins, p["attributes"]["WEIGHTS_0"]).astype(np.float32)
        IDX = acc_read(g, bins, p["indices"]).reshape(-1).astype(np.uint32)

        C = paint(P, pi)

        if pi == 0:
            # 并进身体那一 prim：同一副骨架、同一个材质，少一个 draw call。
            # 权重 1.0 全给 head，帽子和五官随头骨一起动；UV 给 0（材质无贴图）。
            n0 = P.shape[0]
            P = np.vstack([P, GV])
            N = np.vstack([N, GN])
            UV = np.vstack([UV, np.zeros((GV.shape[0], 2), np.float32)])
            C = np.vstack([C, GC])
            J = np.vstack([J, np.tile(np.array([[head_idx, 0, 0, 0]], np.uint16),
                                      (GV.shape[0], 1))])
            W = np.vstack([W, np.tile(np.array([[1.0, 0.0, 0.0, 0.0]], np.float32),
                                      (GV.shape[0], 1))])
            IDX = np.concatenate([IDX, GI + n0])

        attrs = {}
        attrs["POSITION"] = pk.add(P, 5126, "VEC3", ARRAY_BUFFER, minmax=True, name="p%d" % pi)
        attrs["NORMAL"] = pk.add(N, 5126, "VEC3", ARRAY_BUFFER)
        attrs["TEXCOORD_0"] = pk.add(UV, 5126, "VEC2", ARRAY_BUFFER)
        attrs["COLOR_0"] = pk.add(C, 5126, "VEC4", ARRAY_BUFFER)
        # JOINTS_0 存的是 skin.joints 数组的下标，而 joint_new 保持了原顺序，
        # 所以这里必须原样保留。早先误把它当成"节点索引"去查映射表重编号，
        # 蒙皮整片错位、模型渲染不出来。
        J2 = J.astype(np.uint16)
        attrs["JOINTS_0"] = pk.add(J2, 5123, "VEC4", ARRAY_BUFFER)
        attrs["WEIGHTS_0"] = pk.add(W, 5126, "VEC4", ARRAY_BUFFER)
        idx = pk.add(IDX, 5125, "SCALAR", ELEMENT_ARRAY_BUFFER)
        prims.append({"attributes": attrs, "indices": idx, "material": 0})

    # ---- 逆向绑定矩阵 ----
    ibm = acc_read(g, bins, skin0["inverseBindMatrices"]).astype(np.float32)
    ibm_idx = pk.add(ibm, 5126, "MAT4", name="ibm")

    # ---- 动画：只留 KEEP_ANIMS ----
    anims = []
    src_anims = {a.get("name", ""): a for a in g.get("animations", [])}
    for nm in KEEP_ANIMS:
        a = src_anims.get(nm)
        if a is None:
            print("  跳过缺失片段", nm)
            continue
        samplers, channels = [], []
        for ch in a["channels"]:
            s = a["samplers"][ch["sampler"]]
            tgt = ch["target"]
            node_i = tgt.get("node")
            if node_i is None or node_i not in old2new:
                continue
            tin = acc_read(g, bins, s["input"]).astype(np.float32).reshape(-1)
            vout = acc_read(g, bins, s["output"]).astype(np.float32)
            ip = pk.add(tin, 5126, "SCALAR", minmax=True)
            vtype = "VEC4" if vout.shape[1] == 4 else ("VEC3" if vout.shape[1] == 3 else "SCALAR")
            op = pk.add(vout, 5126, vtype)
            samplers.append({"input": ip, "output": op,
                             "interpolation": s.get("interpolation", "LINEAR")})
            channels.append({"sampler": len(samplers) - 1,
                             "target": {"node": old2new[node_i], "path": tgt["path"]}})
        anims.append({"name": nm, "channels": channels, "samplers": samplers})
        print("  %-20s %d 通道" % (nm, len(channels)))

    # ---- 组装 ----
    # 材质：白底 + 顶点色。baseColorFactor 给 1，颜色全在 COLOR_0 里
    materials = [{
        "name": "M_Worker",
        "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1],
                                 "metallicFactor": 0.02, "roughnessFactor": 0.86},
        "doubleSided": False,
    }]

    # 找 Mannequin 节点，改它的 material 引用（primitive 里已指向 0）
    out = {
        "asset": {"version": "2.0", "generator": "build_worker_char.py"},
        "scene": 0,
        "scenes": [{"nodes": [old2new[r] for r in roots]}],
        "nodes": nodes,
        "meshes": [{"name": "Worker", "primitives": prims}],
        "materials": materials,
        "skins": [{"joints": joint_new, "inverseBindMatrices": ibm_idx,
                   "skeleton": joint_new[0]}],
        "animations": anims,
        "accessors": pk.accessors,
        "bufferViews": pk.views,
        "buffers": [{"byteLength": len(pk.buf)}],
    }

    while len(pk.buf) % 4:
        pk.buf.append(0)
    js = json.dumps(out, separators=(",", ":")).encode("utf-8")
    while len(js) % 4:
        js += b" "
    total = 12 + 8 + len(js) + 8 + len(pk.buf)
    with open(OUT, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack("<II", len(pk.buf), 0x004E4942))
        f.write(bytes(pk.buf))

    tp = sum(a["count"] for a in pk.accessors)
    print("\n写出 %s  %.2f MB  顶点 %d  动画 %d"
          % (OUT, os.path.getsize(OUT) / 1048576.0,
             sum(acc_read(g, bins, p["attributes"]["POSITION"]).shape[0] for p in g["meshes"][0]["primitives"]),
             len(anims)))


if __name__ == "__main__":
    main()
