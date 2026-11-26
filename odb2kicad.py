#!/usr/bin/env python3
"""ODB++(展開済みのフォルダ)を KiCad 9 の基板ファイル(.kicad_pcb)に変換する。

DesignSpark PCB が書き出した ODB++ を対象にしている。部品、パッド、ネット、配線、円弧、ビア、
ベタ面(フィル済みのゾーン)、シルク、基板外形、穴を移す。
寸法は ODB++ の値をそのまま写し、KiCad 側での再計算(ゾーンの再フィルなど)はしない前提で作る。

使い方: python3 odb2kicad.py <ODB++のフォルダか .tgz> <出力.kicad_pcb>
"""
import gzip
import math
import os
import re
import sys
import uuid
from collections import defaultdict

INCH = 25.4
OX, OY = 100.0, 150.0  # 基板を図面の枠の中に置くためのずらし(mm)
ARC_STEP_DEG = 5.0      # ベタ面の円弧を折れ線にするときの角度の刻み

COPPER_MAP = {}  # ODB++ の層名 -> KiCad の層名(matrix から決める)


def U():
    return str(uuid.uuid4())


def X(x):
    return round(OX + x * INCH, 6)


def Y(y):
    return round(OY - y * INCH, 6)


def q(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


# ---------------------------------------------------------------- 読み込み

def read(path):
    """ファイルを行の一覧で読む。無ければ、圧縮された path + '.z' を探して展開する。"""
    if not os.path.exists(path) and os.path.exists(path + '.z'):
        with open(path + '.z', 'rb') as f:
            raw = f.read()
        if raw[:2] != b'\x1f\x8b':
            raise ValueError('%s.z は gzip 形式ではない。gzip -d などで展開してから使う。' % path)
        return gzip.decompress(raw).decode('latin-1').splitlines()
    with open(path, encoding='latin-1') as f:
        return f.read().splitlines()


def exists(path):
    return os.path.exists(path) or os.path.exists(path + '.z')


def parse_matrix(root):
    layers = []
    cur = None
    for line in read(os.path.join(root, 'matrix', 'matrix')):
        line = line.strip()
        if line.startswith('LAYER'):
            cur = {}
        elif line == '}' and cur is not None:
            layers.append(cur)
            cur = None
        elif cur is not None and '=' in line:
            k, v = line.split('=', 1)
            cur[k] = v
    layers.sort(key=lambda l: int(l['ROW']))
    return layers


def parse_attrs(rest):
    """'; 0=2,1' のような属性の部分を {番号: 値} にする。"""
    out = {}
    if not rest:
        return out
    for item in rest.split(','):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            out[k.strip()] = v.strip()
        elif item:
            out[item] = ''
    return out


def parse_contours(lines, i):
    """S の後の OB...OE の並びを読む。戻り値: ([(kind, [(x,y)...])...], 次の行番号)。kind は 'I' か 'H'。"""
    polys = []
    pts = None
    kind = None
    while i < len(lines):
        t = lines[i].split()
        i += 1
        if not t:
            continue
        if t[0] == 'OB':
            pts = [(float(t[1]), float(t[2]))]
            kind = t[3] if len(t) > 3 else 'I'
        elif t[0] == 'OS':
            pts.append((float(t[1]), float(t[2])))
        elif t[0] == 'OC':
            xe, ye, xc, yc = map(float, t[1:5])
            cw = t[5].upper() == 'Y' if len(t) > 5 else True
            pts.extend(arc_points(pts[-1], (xe, ye), (xc, yc), cw))
        elif t[0] == 'OE':
            polys.append((kind, pts))
            pts = None
        elif t[0] == 'SE':
            break
    return polys, i


def arc_points(s, e, c, cw):
    """ODB++ の座標(y 上向き)で、s から e への円弧を折れ線にする(s は含めず e は含める)。"""
    r = math.hypot(s[0] - c[0], s[1] - c[1])
    a0 = math.atan2(s[1] - c[1], s[0] - c[0])
    a1 = math.atan2(e[1] - c[1], e[0] - c[0])
    if cw:
        sweep = a0 - a1
        if sweep <= 1e-9:
            sweep += 2 * math.pi
        sweep = -sweep
    else:
        sweep = a1 - a0
        if sweep <= 1e-9:
            sweep += 2 * math.pi
    n = max(1, int(abs(math.degrees(sweep)) / ARC_STEP_DEG))
    out = []
    for k in range(1, n):
        a = a0 + sweep * k / n
        out.append((c[0] + r * math.cos(a), c[1] + r * math.sin(a)))
    out.append(e)
    return out


def arc_mid(s, e, c, cw):
    r = math.hypot(s[0] - c[0], s[1] - c[1])
    a0 = math.atan2(s[1] - c[1], s[0] - c[0])
    a1 = math.atan2(e[1] - c[1], e[0] - c[0])
    if cw:
        sweep = a0 - a1
        if sweep <= 1e-9:
            sweep += 2 * math.pi
        am = a0 - sweep / 2
    else:
        sweep = a1 - a0
        if sweep <= 1e-9:
            sweep += 2 * math.pi
        am = a0 + sweep / 2
    return (c[0] + r * math.cos(am), c[1] + r * math.sin(am)), sweep


def parse_features(path):
    """features ファイルを読む。戻り値: (シンボル表, 属性名表, 図形の一覧)。図形の番号は一覧の添字と一致する。"""
    lines = read(path)
    syms, attrnames, feats = {}, {}, []
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('$'):
            k, v = line[1:].split(None, 1)
            syms[int(k)] = v.split()[0]
            continue
        if line.startswith('@'):
            k, v = line[1:].split(None, 1)
            attrnames[int(k)] = v.strip()
            continue
        if line.startswith('UNITS'):
            if line.split('=', 1)[1].strip().upper() != 'INCH':
                raise SystemExit('%s は mm 単位である。今はインチ単位の ODB++ だけに対応している。' % path)
            continue
        if line.startswith('&') or line.startswith('ID='):
            continue
        body, _, rest = line.partition(';')
        t = body.split()
        a = parse_attrs(rest)
        kind = t[0]
        if kind == 'P':
            f = {'t': 'P', 'x': float(t[1]), 'y': float(t[2]), 'sym': int(t[3]), 'pol': t[4]}
            orient = t[6] if len(t) > 6 else '0'
            if orient in ('8', '9'):
                f['rot'] = float(t[7])
                f['mir'] = orient == '9'
            else:
                o = int(orient)
                f['rot'] = (o % 4) * 90.0
                f['mir'] = o >= 4
        elif kind == 'L':
            f = {'t': 'L', 'xs': float(t[1]), 'ys': float(t[2]), 'xe': float(t[3]), 'ye': float(t[4]),
                 'sym': int(t[5]), 'pol': t[6]}
        elif kind == 'A':
            f = {'t': 'A', 'xs': float(t[1]), 'ys': float(t[2]), 'xe': float(t[3]), 'ye': float(t[4]),
                 'xc': float(t[5]), 'yc': float(t[6]), 'sym': int(t[7]), 'pol': t[8],
                 'cw': t[10].upper() == 'Y' if len(t) > 10 else True}
        elif kind == 'S':
            polys, i = parse_contours(lines, i)
            f = {'t': 'S', 'pol': t[1], 'polys': polys}
        elif kind in ('T', 'B'):
            f = {'t': kind}
        else:
            continue
        f['attr'] = a
        feats.append(f)
    return syms, attrnames, feats


def parse_symbol_dir(root):
    """symbols/ の中の利用者定義シンボルを読み、名前 -> 多角形の一覧(インチ)にする。"""
    out = {}
    d = os.path.join(root, 'symbols')
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        p = os.path.join(d, name, 'features')
        if not exists(p):
            continue
        _, _, feats = parse_features(p)
        polys = []
        for f in feats:
            if f['t'] == 'S':
                polys.extend(f['polys'])
        out[name] = polys
    return out


def parse_eda(path):
    lines = read(path)
    lyr = []
    nets = []          # [名前]
    feat_net = {}      # (LYR の番号, 図形の番号) -> ネットの番号
    toe_fids = defaultdict(list)  # (面 'T'/'B', 部品の番号, ピンの番号) -> [(LYR の番号, 図形の番号)]
    via_fids = []      # [(ネットの番号, [(LYR の番号, 図形の番号)])]
    cur_net = None
    cur_snt = None
    for line in lines:
        t = line.split()
        if not t:
            continue
        if t[0] == 'LYR':
            lyr = t[1:]
        elif t[0] == 'NET':
            nets.append(t[1])
            cur_net = len(nets) - 1
            cur_snt = None
        elif t[0] == 'SNT':
            if t[1] == 'TOP':
                cur_snt = ('TOP', (t[2], int(t[3]), int(t[4])))
            elif t[1] == 'VIA':
                via_fids.append((cur_net, []))
                cur_snt = ('VIA', len(via_fids) - 1)
            else:
                cur_snt = (t[1], None)
        elif t[0] == 'FID' and cur_net is not None:
            key = (int(t[2]), int(t[3]))
            feat_net[key] = cur_net
            if cur_snt and cur_snt[0] == 'TOP':
                toe_fids[cur_snt[1]].append(key)
            elif cur_snt and cur_snt[0] == 'VIA':
                via_fids[cur_snt[1]][1].append(key)
        elif t[0] == 'PKG':
            cur_net = None
    return lyr, nets, feat_net, toe_fids, via_fids


def parse_components(path):
    comps = []
    if not exists(path):
        return comps
    for line in read(path):
        body = line.split(';')[0]
        t = body.split()
        if not t:
            continue
        if t[0] == 'CMP':
            comps.append({'pkg': int(t[1]), 'x': float(t[2]), 'y': float(t[3]), 'rot': float(t[4]),
                          'mir': t[5] == 'M', 'ref': t[6], 'part': t[7] if len(t) > 7 else '', 'pins': []})
        elif t[0] == 'TOP' and comps:
            comps[-1]['pins'].append({'num': int(t[1]), 'x': float(t[2]), 'y': float(t[3]),
                                      'net': int(t[6]), 'name': t[8] if len(t) > 8 else str(int(t[1]) + 1)})
    return comps


def pkg_names(path):
    names = []
    for line in read(path):
        if line.startswith('PKG '):
            names.append(line.split()[1])
    return names


# ---------------------------------------------------------------- 形の変換

def num(s):
    return float(s)


def std_symbol(name):
    """標準シンボルを (形, 幅mm, 高さmm, 角の丸めの比) にする。分からなければ None。寸法の単位はミル。"""
    m = re.fullmatch(r'r([\d.]+)', name)
    if m:
        d = num(m.group(1)) * INCH / 1000
        return ('circle', d, d, 0)
    m = re.fullmatch(r's([\d.]+)', name)
    if m:
        d = num(m.group(1)) * INCH / 1000
        return ('rect', d, d, 0)
    m = re.fullmatch(r'rect([\d.]+)x([\d.]+)(?:xr([\d.]+))?', name)
    if m:
        w, h = num(m.group(1)), num(m.group(2))
        if m.group(3):
            ratio = min(0.5, num(m.group(3)) / min(w, h))
            return ('roundrect', w * INCH / 1000, h * INCH / 1000, ratio)
        return ('rect', w * INCH / 1000, h * INCH / 1000, 0)
    m = re.fullmatch(r'oval([\d.]+)x([\d.]+)', name)
    if m:
        return ('oval', num(m.group(1)) * INCH / 1000, num(m.group(2)) * INCH / 1000, 0)
    return None


def rotate_odb(x, y, deg, mirror):
    """ODB++ の回転(時計回り)と鏡像を、y 上向きの座標で行う。"""
    if mirror:
        x = -x
    a = math.radians(deg)
    return (x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a))


# ---------------------------------------------------------------- 多角形の穴をつなぐ(KiCad の filled_polygon 用)

def signed_area(p):
    return sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1] for i in range(len(p))) / 2


def dedupe(p):
    out = []
    for pt in p:
        if not out or abs(pt[0] - out[-1][0]) > 1e-9 or abs(pt[1] - out[-1][1]) > 1e-9:
            out.append(pt)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < 1e-9 and abs(out[0][1] - out[-1][1]) < 1e-9:
        out.pop()
    return out


def point_in_tri(a, b, c, p):
    def s(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = s(p, a, b), s(p, b, c), s(p, c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def bridge_hole(outer, hole):
    """earcut と同じ考え方で、穴の右端の点から右へ伸ばした線が当たる外周の点と穴をつなぐ。"""
    mi = max(range(len(hole)), key=lambda k: hole[k][0])
    M = hole[mi]
    best_x, best_i = -math.inf, None
    for i in range(len(outer)):
        p1, p2 = outer[i], outer[(i + 1) % len(outer)]
        if (p1[1] - M[1]) * (p2[1] - M[1]) <= 0 and p1[1] != p2[1]:
            x = p1[0] + (M[1] - p1[1]) * (p2[0] - p1[0]) / (p2[1] - p1[1])
            if M[0] <= x and (best_i is None or x < best_x):
                best_x = x
                best_i = i if p1[0] > p2[0] else (i + 1) % len(outer)
    if best_i is None:
        best_i = min(range(len(outer)), key=lambda k: math.hypot(outer[k][0] - M[0], outer[k][1] - M[1]))
    else:
        I = (best_x, M[1])
        P = outer[best_i]
        cand, best_t = best_i, math.inf
        for k, pt in enumerate(outer):
            if pt is P or pt[0] < M[0]:
                continue
            if point_in_tri(M, I, P, pt) and (pt[0], pt[1]) != (M[0], M[1]):
                tan = abs(M[1] - pt[1]) / (pt[0] - M[0]) if pt[0] != M[0] else math.inf
                if tan < best_t or (tan == best_t and pt[0] > outer[cand][0]):
                    cand, best_t = k, tan
        best_i = cand
    h = hole[mi:] + hole[:mi]
    P = outer[best_i]
    return outer[:best_i + 1] + h + [M, P] + outer[best_i + 1:]


def fracture(outer, holes):
    outer = dedupe(outer)
    if signed_area(outer) < 0:
        outer = outer[::-1]
    hs = []
    for h in holes:
        h = dedupe(h)
        if len(h) < 3:
            continue
        if signed_area(h) > 0:
            h = h[::-1]
        hs.append(h)
    hs.sort(key=lambda h: -max(p[0] for p in h))
    for h in hs:
        outer = bridge_hole(outer, h)
    return outer


def pts_sexpr(pts, conv=True):
    if conv:
        return ' '.join('(xy %s %s)' % (X(x), Y(y)) for x, y in pts)
    return ' '.join('(xy %s %s)' % (round(x, 6), round(y, 6)) for x, y in pts)


# ---------------------------------------------------------------- ネットの推定

def point_in_poly(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def dist_to_poly(pt, poly):
    best = math.inf
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
        dx, dy = x2 - x1, y2 - y1
        L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0, min(1, ((pt[0] - x1) * dx + (pt[1] - y1) * dy) / L))
        best = min(best, math.hypot(pt[0] - (x1 + t * dx), pt[1] - (y1 + t * dy)))
    return best


def symbol_half(name, usersyms):
    """パッドの形の大きさの半分(インチ)。接しているかの判定に使う。"""
    st = std_symbol(name)
    if st:
        return max(st[1], st[2]) / 2 / INCH
    polys = usersyms_cache.get(name) if usersyms_cache else None
    if polys:
        return max(math.hypot(x, y) for _, pts in polys for x, y in pts)
    return 0.01


usersyms_cache = {}


def islands_of(f):
    islands = []
    for kind, pts in f['polys']:
        if kind == 'I':
            islands.append([pts, []])
        elif islands:
            islands[-1][1].append(pts)
    return islands


def seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


class CopperItem:
    """ネットのある銅箔の図形(パッドか配線)。点が触れているかを、実際の形で判定する。"""

    def __init__(self, f, symname, net):
        self.net = net
        self.kind = f['t']
        if self.kind == 'P':
            self.c = (f['x'], f['y'])
            self.rot = f['rot']
            self.mir = f['mir']
            st = std_symbol(symname)
            self.poly = None
            if st:
                self.shape, w, h = st[0], st[1] / INCH, st[2] / INCH
                self.hw, self.hh = w / 2, h / 2
            else:
                polys = usersyms_cache.get(symname) or []
                self.shape = 'poly'
                self.poly = [pts for k, pts in polys if k == 'I']
                self.hw = self.hh = 0.01
            self.ref_pts = [self.c]
        else:
            st = std_symbol(symname)
            self.w2 = (min(st[1], st[2]) / INCH / 2) if st else 0.004
            if self.kind == 'L':
                self.pts = [(f['xs'], f['ys']), (f['xe'], f['ye'])]
            else:
                s0, e0, c0 = (f['xs'], f['ys']), (f['xe'], f['ye']), (f['xc'], f['yc'])
                self.pts = [s0] + arc_points(s0, e0, c0, f['cw'])
            self.ref_pts = [self.pts[0], self.pts[-1]]

    def touches(self, p, r):
        tol = r + 1e-4
        if self.kind == 'P':
            x, y = p[0] - self.c[0], p[1] - self.c[1]
            if self.shape == 'circle':
                return math.hypot(x, y) <= self.hw + tol
            if self.shape == 'poly':
                q = rotate_odb(x, y, -self.rot, False)
                if self.mir:
                    q = (-q[0], q[1])
                return any(point_in_poly(q, pl) or dist_to_poly(q, pl) <= tol for pl in self.poly)
            # 長方形と長丸: パッドの向きに戻して比べる
            q = rotate_odb(x, y, -self.rot, False)
            return abs(q[0]) <= self.hw + tol and abs(q[1]) <= self.hh + tol
        return any(seg_dist(p, self.pts[i], self.pts[i + 1]) <= self.w2 + tol for i in range(len(self.pts) - 1))


def infer_nets(feats, copper_map, lyr_index, feat_net, via_fids, lyr, comps_by_side, top_cu, bot_cu, drills, pin_keys):
    """ネットの無い銅箔(ベタ面と配線)のネットを、つながっている相手から推定する。
    考え方: 配線の端がネットの分かったベタ面の中にあれば、そのネット。そうでなければ、端が実際に触れている
    パッドや配線のネット。ベタ面は、中にあるパッドと配線の端のネットの多数決。これを変化が無くなるまで繰り返す。
    戻り値: {(層名, 図形の番号): ネット} と {(層名, 図形の番号, 島の番号): ネット}。"""
    all_cu = list(copper_map)
    items = {name: [] for name in all_cu}
    pending = []
    zones = []  # [層名, 図形の番号, 島の番号, 外周, 穴, ネット]
    for name in all_cu:
        syms, _, fl = feats[name]
        li = lyr_index[name]
        for fi, f in enumerate(fl):
            n = feat_net.get((li, fi))
            if f['t'] in ('P', 'L', 'A'):
                if n is not None:
                    items[name].append(CopperItem(f, syms[f['sym']], n))
                elif f['t'] in ('L', 'A') or (li, fi) not in pin_keys:
                    # ネットの無い配線と、部品に属さない単独のパッド(ピンのネットの無いものは、元から未接続なので除く)
                    pending.append((name, fi, CopperItem(f, syms[f['sym']], None)))
            elif f['t'] == 'S' and n is None:
                for ii, (outer, holes) in enumerate(islands_of(f)):
                    zones.append([name, fi, ii, outer, holes, None])

    def in_zone(z, p):
        return point_in_poly(p, z[3]) and not any(point_in_poly(p, h) for h in z[4])

    out = {}
    conflicts = 0
    for rnd in range(20):
        changed = False
        # ベタ面: 中にある、ネットの分かったパッドの中心と配線の端の多数決
        for z in zones:
            if z[5] is not None:
                continue
            votes = defaultdict(int)
            for it in items[z[0]]:
                if any(in_zone(z, p) for p in it.ref_pts):
                    votes[it.net] += 1
            if not votes and rnd >= 3:
                # 中に何も無い島は、縁(外周か穴の縁)に触れている配線とパッドから決める
                for it in items[z[0]]:
                    if it.kind == 'P':
                        if any(dist_to_poly(it.c, pl) <= min(it.hw, it.hh) + 1e-4 for pl in [z[3]] + z[4]):
                            votes[it.net] += 1
                    else:
                        for k in range(len(it.pts) - 1):
                            a, b = it.pts[k], it.pts[k + 1]
                            steps = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.0005))
                            if any(any(dist_to_poly((a[0] + (b[0] - a[0]) * t / steps, a[1] + (b[1] - a[1]) * t / steps), pl) <= it.w2 + 1e-4
                                       for pl in [z[3]] + z[4]) for t in range(steps + 1)):
                                votes[it.net] += 1
                                break
            if votes:
                if len(votes) > 1:
                    conflicts += 1
                z[5] = max(votes, key=votes.get)
                out[(z[0], z[1], z[2])] = z[5]
                changed = True
        # 配線: 端がネットの分かったベタ面の中にあるか、ネットの分かった銅箔に触れていれば、そのネット
        rest = []
        for name, fi, it in pending:
            votes = defaultdict(int)
            r = it.w2 if it.kind != 'P' else 0.0
            for p in it.ref_pts:
                for z in zones:
                    if z[0] == name and z[5] is not None and in_zone(z, p):
                        votes[z[5]] += 1
                for other in items[name]:
                    if other.touches(p, r):
                        votes[other.net] += 1
            if it.kind == 'P':
                # パッドは、ネットの分かった配線の端が触れているかで決める
                for other in items[name]:
                    if other.kind != 'P' and any(it.touches(q, other.w2) for q in other.ref_pts):
                        votes[other.net] += 1
            if votes:
                if len(votes) > 1:
                    conflicts += 1
                it.net = max(votes, key=votes.get)
                items[name].append(it)
                out[(name, fi)] = it.net
                changed = True
            else:
                rest.append((name, fi, it))
        pending = rest
        if not changed and rnd >= 3:
            break
    print('net_inferred_tracks          %d' % sum(1 for k in out if len(k) == 2))
    print('net_inferred_zone_islands    %d' % sum(1 for k in out if len(k) == 3))
    print('net_inference_conflicts      %d' % conflicts)

    # 名前の付いたネットに届かない銅箔は、つながっている同士をまとめて仮のネットにする
    nodes = [('t', name, fi, it) for name, fi, it in pending] + [('z', z[0], z, None) for z in zones if z[5] is None]
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def connected(a, b):
        if a[1] != b[1]:
            return False
        if a[0] == 't' and b[0] == 't':
            ia, ib = a[3], b[3]
            ra = ia.w2 if ia.kind != 'P' else 0.0
            rb = ib.w2 if ib.kind != 'P' else 0.0
            return any(ib.touches(p, ra) for p in ia.ref_pts) or any(ia.touches(p, rb) for p in ib.ref_pts)
        if a[0] == 'z' and b[0] == 'z':
            return False
        t, z = (a, b) if a[0] == 't' else (b, a)
        it, zz = t[3], z[2]
        r = it.w2 if it.kind != 'P' else 0.0
        return any(in_zone(zz, p) or any(dist_to_poly(p, pl) <= r + 1e-4 for pl in [zz[3]] + zz[4]) for p in it.ref_pts)

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if find(i) != find(j) and connected(nodes[i], nodes[j]):
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(len(nodes)):
        groups[find(i)].append(nodes[i])
    extra = []
    for g in groups.values():
        if len(g) < 2 and g[0][0] == 't':
            continue  # 1本だけの孤立した線はネット無しのまま
        idx = -(len(extra) + 1)  # 仮のネットは負の番号で返し、main で番号を振り直す
        extra.append('ODB_NoNet_%d' % (len(extra) + 1))
        for node in g:
            if node[0] == 't':
                out[(node[1], node[2])] = idx
            else:
                z = node[2]
                out[(z[0], z[1], z[2])] = idx
    print('unnamed_net_groups           %d' % len(extra))
    print('items_left_without_net       %d' % sum(1 for g in groups.values() if len(g) < 2 and g[0][0] == 't'))
    out['__extra_nets__'] = extra
    return out


# ---------------------------------------------------------------- 本体

def main(root, out_path):
    step_dirs = os.listdir(os.path.join(root, 'steps'))
    step = os.path.join(root, 'steps', step_dirs[0])
    matrix = parse_matrix(root)
    usersyms = parse_symbol_dir(root)
    usersyms_cache.update(usersyms)
    lyr, nets, feat_net, toe_fids, via_fids = parse_eda(os.path.join(step, 'eda', 'data'))
    pkgs = pkg_names(os.path.join(step, 'eda', 'data'))

    signal = [l['NAME'] for l in matrix if l['TYPE'] in ('SIGNAL', 'POWER_GROUND', 'MIXED') and l['CONTEXT'] == 'BOARD']
    kc = ['F.Cu'] + ['In%d.Cu' % k for k in range(1, len(signal) - 1)] + ['B.Cu']
    for n, k in zip(signal, kc):
        COPPER_MAP[n] = k
    top_cu, bot_cu = signal[0], signal[-1]

    def side_of(name):
        """シルクなどの層が表か裏か。matrix で表の銅箔より前なら表。"""
        rows = {l['NAME']: int(l['ROW']) for l in matrix}
        return 'F' if rows[name] < rows[top_cu] else 'B'

    feats = {}
    for l in matrix:
        p = os.path.join(step, 'layers', l['NAME'], 'features')
        if exists(p):
            feats[l['NAME']] = parse_features(p)

    # DesignSpark はベタ面や一部の配線を「$NONE$」というネットで書き出す。ネットなしとして扱い、後で推定する。
    none_idx = nets.index('$NONE$') if '$NONE$' in nets else None
    if none_idx is not None:
        feat_net = {k: v for k, v in feat_net.items() if v != none_idx}
        via_fids = [(None if n == none_idx else n, f) for n, f in via_fids]

    lyr_index = {n: i for i, n in enumerate(lyr)}
    drill_layers = [l['NAME'] for l in matrix if l['TYPE'] == 'DRILL']

    # 穴: 位置 -> (直径mm, 種類)
    drills = []
    for dl in drill_layers:
        syms, an, fl = feats[dl]
        drill_attr = next((k for k, v in an.items() if v == '.drill'), None)
        for f in fl:
            if f['t'] != 'P':
                continue
            m = re.fullmatch(r'r([\d.]+)', syms[f['sym']])
            dia = num(m.group(1)) * INCH / 1000 if m else 0.5
            kind = {'0': 'plated', '1': 'non_plated', '2': 'via'}.get(f['attr'].get(str(drill_attr), '0'), 'plated')
            drills.append({'x': f['x'], 'y': f['y'], 'd': dia, 'kind': kind, 'used': False})

    def find_drill(x, y, kinds):
        for d in drills:
            if d['kind'] in kinds and abs(d['x'] - x) < 2e-4 and abs(d['y'] - y) < 2e-4:
                return d
        return None

    comps_by_side = {side: parse_components(os.path.join(step, 'layers', cl, 'components'))
                     for side, cl in (('T', 'comp_+_top'), ('B', 'comp_+_bot'))}
    for cl in comps_by_side.values():
        for c in cl:
            for pin in c['pins']:
                if pin['net'] == none_idx or not (0 <= pin['net'] < len(nets)):
                    pin['net'] = None

    pin_keys = {(l, fi) for fids in toe_fids.values() for l, fi in fids}
    net_override = infer_nets(feats, COPPER_MAP, lyr_index, feat_net, via_fids, lyr, comps_by_side,
                              top_cu, bot_cu, drills, pin_keys)
    # 仮のネットを、ネットの一覧の後ろに足す
    extra = net_override.pop('__extra_nets__')
    base = len(nets)
    nets = nets + extra
    net_override = {k: (base + (-v - 1) if v < 0 else v) for k, v in net_override.items()}

    out = []
    w = out.append
    net_names = [''] + [n for n in nets]

    def net_sexpr(ni):
        """パッド用(番号と名前)。"""
        if ni is None:
            return '(net 0 "")'
        return '(net %d %s)' % (ni + 1, q(net_names[ni + 1]))

    def net_num(ni):
        """配線とビア用(番号だけ)。"""
        return '(net %d)' % (0 if ni is None else ni + 1)

    used = set()  # (層名, 図形の番号) で、パッドやビアとして使ったもの
    stats = defaultdict(int)

    def pad_shape_sexpr(layer, fidx, rot_extra=0.0):
        """銅箔の P 図形からパッドの形の部分を作る。戻り値: (形の文字列, 角度, 中心(x,y), 本体の追加分)。"""
        syms, _, fl = feats[layer]
        f = fl[fidx]
        name = syms[f['sym']]
        st = std_symbol(name)
        if st:
            shape, wmm, hmm, ratio = st
            ang = (-f['rot']) % 360
            if f['mir'] and shape != 'circle':
                pass  # 左右対称の形なので鏡像は無視できる
            extra = ''
            if shape == 'roundrect':
                extra = ' (roundrect_rratio %.4f)' % ratio
            return shape, ang, (f['x'], f['y']), '(size %s %s)%s' % (round(wmm, 6), round(hmm, 6), extra)
        polys = usersyms.get(name)
        if polys:
            prims = []
            for kind, pts in polys:
                if kind != 'I':
                    continue
                rp = [rotate_odb(px, py, f['rot'], f['mir']) for px, py in pts]
                prims.append('(gr_poly (pts %s) (width 0) (fill yes))' % ' '.join(
                    '(xy %s %s)' % (round(px * INCH, 6), round(-py * INCH, 6)) for px, py in dedupe(rp)))
            stats['custom_pads'] += 1
            return 'custom', 0, (f['x'], f['y']), '(size 0.1 0.1) (options (clearance outline) (anchor circle)) (primitives %s)' % ' '.join(prims)
        stats['unknown_symbol'] += 1
        return 'circle', 0, (f['x'], f['y']), '(size 0.3 0.3)'

    # ---- 部品
    fp_out = []
    for side, comp_layer in (('T', 'comp_+_top'), ('B', 'comp_+_bot')):
        comps = comps_by_side[side]
        cu_layer = top_cu if side == 'T' else bot_cu
        S = 'F' if side == 'T' else 'B'
        for ci, c in enumerate(comps):
            pads = []
            tht = False
            for pin in c['pins']:
                fids = toe_fids.get((side, ci, pin['num']), [])
                cu_fids = [(lyr[l], fi) for l, fi in fids if lyr[l] in COPPER_MAP]
                for k in cu_fids:
                    used.add(k)
                main = next((k for k in cu_fids if k[0] == cu_layer), cu_fids[0] if cu_fids else None)
                netnum = pin['net']
                if main is None:
                    stats['pin_without_copper'] += 1
                    shape, ang, (px, py), body = 'circle', 0, (pin['x'], pin['y']), '(size 0.3 0.3)'
                else:
                    shape, ang, (px, py), body = pad_shape_sexpr(main[0], main[1])
                drill = find_drill(px, py, ('plated', 'non_plated'))
                lx, ly = X(px) - X(c['x']), Y(py) - Y(c['y'])
                if drill and drill['kind'] == 'non_plated':
                    drill['used'] = True
                    pads.append('(pad %s np_thru_hole %s (at %s %s %s) %s (drill %s) (layers "*.Cu" "*.Mask") %s (uuid %s))' % (
                        q(pin['name']), shape, round(lx, 6), round(ly, 6), round(ang, 3), body, round(drill['d'], 6), net_sexpr(netnum), q(U())))
                elif drill:
                    drill['used'] = True
                    tht = True
                    pads.append('(pad %s thru_hole %s (at %s %s %s) %s (drill %s) (layers "*.Cu" "*.Mask") (remove_unused_layers no) %s (uuid %s))' % (
                        q(pin['name']), shape, round(lx, 6), round(ly, 6), round(ang, 3), body, round(drill['d'], 6), net_sexpr(netnum), q(U())))
                else:
                    layers = '"%s.Cu" "%s.Paste" "%s.Mask"' % (S, S, S)
                    pads.append('(pad %s smd %s (at %s %s %s) %s (layers %s) %s (uuid %s))' % (
                        q(pin['name']), shape, round(lx, 6), round(ly, 6), round(ang, 3), body, layers, net_sexpr(netnum), q(U())))
                stats['pads'] += 1
            pkg = pkgs[c['pkg']] if c['pkg'] < len(pkgs) else 'pkg%d' % c['pkg']
            fab = '%s.Fab' % S
            mirror = ' (justify mirror)' if S == 'B' else ''
            fp_out.append('(footprint %s (layer "%s.Cu") (uuid %s) (at %s %s) '
                          '(property "Reference" %s (at 0 0 0) (layer "%s") (uuid %s) (effects (font (size 0.5 0.5) (thickness 0.08))%s)) '
                          '(property "Value" %s (at 0 0.8 0) (layer "%s") (hide yes) (uuid %s) (effects (font (size 0.5 0.5) (thickness 0.08))%s)) '
                          '(attr %s) %s)' % (
                              q('ODB:' + pkg), S, q(U()), X(c['x']), Y(c['y']),
                              q(c['ref']), fab, q(U()), mirror, q(c['part']), fab, q(U()), mirror,
                              'through_hole' if tht else 'smd', ' '.join(pads)))
            stats['footprints'] += 1

    # ---- ビア
    via_out = []
    for netnum, fids in via_fids:
        cu = [(lyr[l], fi) for l, fi in fids if lyr[l] in COPPER_MAP]
        if not cu:
            continue
        for k in cu:
            used.add(k)
        ref = next((k for k in cu if k[0] == top_cu), cu[0])
        syms, _, fl = feats[ref[0]]
        f = fl[ref[1]]
        st = std_symbol(syms[f['sym']])
        size = st[1] if st else 0.6
        d = find_drill(f['x'], f['y'], ('via', 'plated'))
        if d:
            d['used'] = True
        layer_names = [COPPER_MAP[k[0]] for k in cu]
        via_out.append('(via (at %s %s) (size %s) (drill %s) (layers "F.Cu" "B.Cu") %s (uuid %s))' % (
            X(f['x']), Y(f['y']), round(size, 6), round(d['d'] if d else size / 2, 6), net_num(netnum), q(U())))
        stats['vias'] += 1
        if not (layer_names and 'F.Cu' in layer_names and 'B.Cu' in layer_names):
            stats['via_not_through'] += 1

    def net_at(x, y):
        for ln in COPPER_MAP:
            fl2 = feats[ln][2]
            li2 = lyr_index.get(ln)
            for fj, g in enumerate(fl2):
                if g['t'] == 'P' and abs(g['x'] - x) < 2e-4 and abs(g['y'] - y) < 2e-4:
                    n = feat_net.get((li2, fj))
                    if n is None:
                        n = net_override.get((ln, fj))
                    if n is not None:
                        return n
        return None

    # ---- 銅箔の配線、ベタ面、残りのパッド
    cu_out = []
    for lname, klayer in COPPER_MAP.items():
        syms, _, fl = feats[lname]
        li = lyr_index.get(lname)
        for fi, f in enumerate(fl):
            netnum = feat_net.get((li, fi))
            if netnum is None:
                netnum = net_override.get((lname, fi))
            if f['t'] == 'L':
                st = std_symbol(syms[f['sym']])
                width = min(st[1], st[2]) if st else 0.2
                cu_out.append('(segment (start %s %s) (end %s %s) (width %s) (layer "%s") %s (uuid %s))' % (
                    X(f['xs']), Y(f['ys']), X(f['xe']), Y(f['ye']), round(width, 6), klayer, net_num(netnum), q(U())))
                stats['segments'] += 1
            elif f['t'] == 'A':
                st = std_symbol(syms[f['sym']])
                width = min(st[1], st[2]) if st else 0.2
                s, e, c = (f['xs'], f['ys']), (f['xe'], f['ye']), (f['xc'], f['yc'])
                if abs(s[0] - e[0]) < 1e-9 and abs(s[1] - e[1]) < 1e-9:
                    # 円一周: 2つの円弧に分ける
                    opp = (2 * c[0] - s[0], 2 * c[1] - s[1])
                    parts = [(s, opp), (opp, e)]
                else:
                    parts = [(s, e)]
                for ps, pe in parts:
                    mid, _ = arc_mid(ps, pe, c, f['cw'])
                    cu_out.append('(arc (start %s %s) (mid %s %s) (end %s %s) (width %s) (layer "%s") %s (uuid %s))' % (
                        X(ps[0]), Y(ps[1]), X(mid[0]), Y(mid[1]), X(pe[0]), Y(pe[1]), round(width, 6), klayer, net_num(netnum), q(U())))
                    stats['arcs'] += 1
            elif f['t'] == 'S':
                islands = []
                for kind, pts in f['polys']:
                    if kind == 'I':
                        islands.append([pts, []])
                    elif islands:
                        islands[-1][1].append(pts)
                for ii, (outer, holes) in enumerate(islands):
                    zn = netnum if netnum is not None else net_override.get((lname, fi, ii))
                    o = dedupe(outer)
                    if len(o) < 3:
                        continue
                    filled = fracture(outer, holes)
                    nn = '(net %d) (net_name %s)' % ((zn + 1, q(net_names[zn + 1])) if zn is not None else (0, '""'))
                    cu_out.append('(zone %s (layer "%s") (uuid %s) (name "ODB") (hatch edge 0.5) (priority 1) '
                                  '(connect_pads yes (clearance 0)) (min_thickness 0.0254) (filled_areas_thickness no) '
                                  '(fill yes (thermal_gap 0.254) (thermal_bridge_width 0.254) (island_removal_mode 1)) '
                                  '(polygon (pts %s)) (filled_polygon (layer "%s") (pts %s)))' % (
                                      nn, klayer, q(U()), pts_sexpr(o), klayer, pts_sexpr(filled)))
                    stats['zones'] += 1
            elif f['t'] == 'P' and (lname, fi) not in used:
                shape, ang, (px, py), body = pad_shape_sexpr(lname, fi)
                drill = find_drill(px, py, ('plated', 'via'))
                if drill and drill['used']:
                    continue  # ほかの層で既に置いた貫通パッド
                if drill:
                    drill['used'] = True
                    if netnum is None:
                        # 貫通パッドは層ごとに図形がある。どれかの層でネットが分かっていれば、それを使う
                        netnum = net_at(px, py)
                    pad = '(pad "1" thru_hole %s (at 0 0 %s) %s (drill %s) (layers "*.Cu" "*.Mask") (remove_unused_layers no) %s (uuid %s))' % (
                        shape, round(ang, 3), body, round(drill['d'], 6), net_sexpr(netnum), q(U()))
                    attr = 'through_hole'
                else:
                    S = 'B' if klayer == 'B.Cu' else 'F'
                    layers = '"%s"' % klayer if klayer.startswith('In') else '"%s.Cu" "%s.Mask"' % (S, S)
                    pad = '(pad "1" smd %s (at 0 0 %s) %s (layers %s) %s (uuid %s))' % (
                        shape, round(ang, 3), body, layers, net_sexpr(netnum), q(U()))
                    attr = 'smd'
                fl_layer = 'B.Cu' if klayer == 'B.Cu' else 'F.Cu'
                fp_out.append('(footprint "ODB:FreePad" (layer "%s") (uuid %s) (at %s %s) '
                              '(property "Reference" "FP%d" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid %s) (effects (font (size 0.5 0.5) (thickness 0.08)))) '
                              '(property "Value" "FreePad" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid %s) (effects (font (size 0.5 0.5) (thickness 0.08)))) '
                              '(attr %s) %s)' % (fl_layer, q(U()), X(px), Y(py), stats['freepads'] + 1, q(U()), q(U()), attr, pad))
                stats['freepads'] += 1

    # ---- 使っていない穴(取り付け穴など)
    for d in drills:
        if d['used']:
            continue
        kind = 'np_thru_hole' if d['kind'] == 'non_plated' else 'thru_hole'
        layers = '"*.Cu" "*.Mask"'
        fp_out.append('(footprint "ODB:Hole" (layer "F.Cu") (uuid %s) (at %s %s) '
                      '(property "Reference" "H%d" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid %s) (effects (font (size 0.5 0.5) (thickness 0.08)))) '
                      '(property "Value" "Hole" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid %s) (effects (font (size 0.5 0.5) (thickness 0.08)))) '
                      '(attr through_hole) (pad "" %s circle (at 0 0) (size %s %s) (drill %s) (layers %s) (uuid %s)))' % (
                          q(U()), X(d['x']), Y(d['y']), stats['holes'] + 1, q(U()), q(U()), kind,
                          round(d['d'], 6), round(d['d'], 6), round(d['d'], 6), layers, q(U())))
        stats['holes_' + d['kind']] += 1
        stats['holes'] += 1

    # ---- シルク、図面の層
    gr_out = []
    graphic_layers = []
    for l in matrix:
        if l['TYPE'] == 'SILK_SCREEN':
            graphic_layers.append((l['NAME'], '%s.SilkS' % side_of(l['NAME'])))
        elif l['TYPE'] == 'DOCUMENT':
            graphic_layers.append((l['NAME'], 'Dwgs.User'))
    for lname, klayer in graphic_layers:
        if lname not in feats:
            continue
        syms, _, fl = feats[lname]
        n_before = len(gr_out)
        for f in fl:
            if f['t'] == 'L':
                st = std_symbol(syms[f['sym']])
                width = min(st[1], st[2]) if st else 0.15
                gr_out.append('(gr_line (start %s %s) (end %s %s) (stroke (width %s) (type solid)) (layer "%s") (uuid %s))' % (
                    X(f['xs']), Y(f['ys']), X(f['xe']), Y(f['ye']), round(width, 6), klayer, q(U())))
            elif f['t'] == 'A':
                st = std_symbol(syms[f['sym']])
                width = min(st[1], st[2]) if st else 0.15
                s, e, c = (f['xs'], f['ys']), (f['xe'], f['ye']), (f['xc'], f['yc'])
                if abs(s[0] - e[0]) < 1e-9 and abs(s[1] - e[1]) < 1e-9:
                    r = math.hypot(s[0] - c[0], s[1] - c[1])
                    gr_out.append('(gr_circle (center %s %s) (end %s %s) (stroke (width %s) (type solid)) (fill no) (layer "%s") (uuid %s))' % (
                        X(c[0]), Y(c[1]), X(c[0] + r), Y(c[1]), round(width, 6), klayer, q(U())))
                else:
                    mid, _ = arc_mid(s, e, c, f['cw'])
                    gr_out.append('(gr_arc (start %s %s) (mid %s %s) (end %s %s) (stroke (width %s) (type solid)) (layer "%s") (uuid %s))' % (
                        X(s[0]), Y(s[1]), X(mid[0]), Y(mid[1]), X(e[0]), Y(e[1]), round(width, 6), klayer, q(U())))
            elif f['t'] == 'S':
                islands = []
                for kind, pts in f['polys']:
                    if kind == 'I':
                        islands.append([pts, []])
                    elif islands:
                        islands[-1][1].append(pts)
                for outer, holes in islands:
                    filled = fracture(outer, holes)
                    if len(filled) >= 3:
                        gr_out.append('(gr_poly (pts %s) (stroke (width 0) (type solid)) (fill yes) (layer "%s") (uuid %s))' % (
                            pts_sexpr(filled), klayer, q(U())))
            elif f['t'] == 'P':
                st = std_symbol(syms[f['sym']])
                if st:
                    r = st[1] / 2
                    gr_out.append('(gr_circle (center %s %s) (end %s %s) (stroke (width 0) (type solid)) (fill yes) (layer "%s") (uuid %s))' % (
                        X(f['x']), Y(f['y']), round(X(f['x']) + r, 6), Y(f['y']), klayer, q(U())))
        stats['graphics_' + klayer] = len(gr_out) - n_before

    # ---- 基板外形
    prof_lines = read(os.path.join(step, 'profile'))
    edge = []
    i = 0
    while i < len(prof_lines):
        t = prof_lines[i].split()
        i += 1
        if not t:
            continue
        if t[0] == 'OB':
            cur = (float(t[1]), float(t[2]))
        elif t[0] == 'OS':
            nxt = (float(t[1]), float(t[2]))
            edge.append('(gr_line (start %s %s) (end %s %s) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid %s))' % (
                X(cur[0]), Y(cur[1]), X(nxt[0]), Y(nxt[1]), q(U())))
            cur = nxt
        elif t[0] == 'OC':
            nxt = (float(t[1]), float(t[2]))
            c = (float(t[3]), float(t[4]))
            mid, _ = arc_mid(cur, nxt, c, t[5].upper() == 'Y' if len(t) > 5 else True)
            edge.append('(gr_arc (start %s %s) (mid %s %s) (end %s %s) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid %s))' % (
                X(cur[0]), Y(cur[1]), X(mid[0]), Y(mid[1]), X(nxt[0]), Y(nxt[1]), q(U())))
            cur = nxt

    # ---- 書き出し
    # 内層の番号は KiCad 9 では In1=4, In2=6, ...
    inner = ''.join('(%d "In%d.Cu" signal) ' % (2 + 2 * k, k) for k in range(1, len(signal) - 1))
    head = ('(kicad_pcb (version 20241229) (generator "odb2kicad") (generator_version "9.0") '
            '(general (thickness 1.6) (legacy_teardrops no)) (paper "A4") '
            '(layers (0 "F.Cu" signal) %s(2 "B.Cu" signal) (9 "F.Adhes" user "F.Adhesive") (11 "B.Adhes" user "B.Adhesive") '
            '(13 "F.Paste" user) (15 "B.Paste" user) (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen") '
            '(1 "F.Mask" user) (3 "B.Mask" user) (17 "Dwgs.User" user "User.Drawings") (19 "Cmts.User" user "User.Comments") '
            '(21 "Eco1.User" user "User.Eco1") (23 "Eco2.User" user "User.Eco2") (25 "Edge.Cuts" user) (27 "Margin" user) '
            '(31 "F.CrtYd" user "F.Courtyard") (29 "B.CrtYd" user "B.Courtyard") (35 "F.Fab" user) (33 "B.Fab" user)) '
            '(setup (pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints no) '
            '(pcbplotparams (layerselection 0x00000000_00000000_55555555_5755f5ff) (plot_on_all_layers_selection 0x00000000_00000000_00000000_00000000) (outputdirectory "")))'
            % inner)
    w(head)
    w('(net 0 "")')
    for k, n in enumerate(nets):
        w('(net %d %s)' % (k + 1, q(n)))
    out.extend(fp_out)
    out.extend(edge)
    out.extend(gr_out)
    out.extend(cu_out)
    out.extend(via_out)
    w(')')
    with open(out_path, 'w') as fo:
        fo.write('\n'.join(out) + '\n')
    stats['nets'] = len(nets)
    unused_drills = sum(1 for d in drills if not d['used'])
    for k in sorted(stats):
        print('%-28s %s' % (k, stats[k]))
    print('%-28s %s' % ('drills_total', len(drills)))


def find_root(path):
    """matrix/matrix があるフォルダを探す。"""
    for dirpath, dirnames, filenames in os.walk(path):
        if os.path.basename(dirpath) == 'matrix' and 'matrix' in filenames:
            return os.path.dirname(dirpath)
    raise SystemExit('%s の中に ODB++ の matrix/matrix が見つからない。' % path)


def cli():
    import argparse
    import tarfile
    import tempfile
    ap = argparse.ArgumentParser(description='ODB++ を KiCad 9 の基板ファイル(.kicad_pcb)に変換する。')
    ap.add_argument('odb', help='ODB++ のフォルダ、または .tgz / .tar.gz')
    ap.add_argument('output', help='出力する .kicad_pcb')
    args = ap.parse_args()
    if os.path.isfile(args.odb) and (args.odb.endswith('.tgz') or args.odb.endswith('.tar.gz')):
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(args.odb) as tf:
                try:
                    tf.extractall(tmp, filter='data')
                except TypeError:  # filter が無い古い Python
                    tf.extractall(tmp)
            main(find_root(tmp), args.output)
    else:
        main(find_root(args.odb), args.output)


if __name__ == '__main__':
    cli()
