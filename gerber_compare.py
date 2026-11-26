#!/usr/bin/env python3
"""2つの Gerber を同じ格子で画像にして重ね、違いを数える(変換の確認用)。

全体の平行移動(原点の違い)は自動で合わせる。違いの画像では、元だけにある所を赤、変換後だけにある所を青で示す。

対応している命令は、元の Gerber(EasyPC 版)と KiCad の Gerber で使われているものだけである。
使い方: python3 gerber_compare.py <元の.gbr> <変換後の.gbr> <出力の画像.png>
2つの Gerber の原点が違っていてもよい。座標の範囲(外接する長方形)の左下をそろえてから、細かいずれを探して合わせる。
"""
import math
import re
import sys

import numpy as np
from PIL import Image, ImageDraw

RES = 0.02          # 1画素の大きさ(mm)
X0, Y0 = -2.0, -2.0  # 描く範囲の左下(mm)。bbox() の結果に合わせて main で決め直す
W_MM, H_MM = 66.0, 66.0
MARGIN = 10.0       # 座標の範囲の外側に取る余白(mm)。パッドや円弧のはみ出しと、位置合わせの移動の分


class Raster:
    def __init__(self, x0=None, y0=None):
        self.x0 = X0 if x0 is None else x0
        self.y0 = Y0 if y0 is None else y0
        self.w = int(W_MM / RES)
        self.h = int(H_MM / RES)
        self.img = Image.new('L', (self.w, self.h), 0)
        self.d = ImageDraw.Draw(self.img)

    def px(self, x, y):
        return ((x - self.x0) / RES, self.h - (y - self.y0) / RES)

    def poly(self, pts, val):
        if len(pts) >= 3:
            self.d.polygon([self.px(*p) for p in pts], fill=val)

    def circle(self, x, y, dia, val):
        cx, cy = self.px(x, y)
        r = dia / 2 / RES
        self.d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=val)

    def line(self, a, b, width, val):
        (x1, y1), (x2, y2) = a, b
        L = math.hypot(x2 - x1, y2 - y1)
        if L > 0:
            nx, ny = -(y2 - y1) / L * width / 2, (x2 - x1) / L * width / 2
            self.poly([(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)], val)
        self.circle(x1, y1, width, val)
        self.circle(x2, y2, width, val)


def rot(pts, deg, cx=0.0, cy=0.0):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [(cx + x * c - y * s, cy + x * s + y * c) for x, y in pts]


def evalexpr(e, params):
    e = re.sub(r'\$(\d+)', lambda m: repr(params.get(int(m.group(1)), 0.0)), e)
    e = e.replace('x', '*').replace('X', '*')
    return float(eval(e, {'__builtins__': {}}))


def render(path, raster, dx=0.0, dy=0.0):
    text = open(path, encoding='latin-1').read()
    # 拡張命令(%...%)と通常の命令(...*)に分ける
    tokens = []
    for m in re.finditer(r'%([^%]*)%|([^%*]+)\*', text, re.S):
        if m.group(1) is not None:
            tokens.append(('ext', m.group(1).replace('\n', '').replace('\r', '')))
        else:
            tokens.append(('cmd', m.group(2).strip()))
    scale = 1.0
    xd, yd = 6, 6
    aps, macros = {}, {}
    cur = None
    x = y = 0.0
    mode = 'G01'
    region = False
    contour = []
    contours = []
    pol = 255
    last_op = 2  # D01/D02/D03 は省略されると直前のものが続く

    def coord(s, digits):
        v = int(s) / (10 ** digits)
        return v * scale

    for kind, t in tokens:
        if kind == 'ext':
            for part in [p for p in t.split('*') if p]:
                if part.startswith('FS'):
                    m = re.search(r'X(\d)(\d)Y(\d)(\d)', part)
                    xd, yd = int(m.group(2)), int(m.group(4))
                elif part.startswith('MO'):
                    scale = 25.4 if 'IN' in part else 1.0
                elif part.startswith('LP'):
                    pol = 255 if part[2] == 'D' else 0
                elif part.startswith('AD'):
                    m = re.match(r'ADD(\d+)([A-Za-z_][A-Za-z0-9_.]*),?(.*)', part)
                    num, typ, args = int(m.group(1)), m.group(2), m.group(3)
                    vals = [float(v) for v in args.split('X') if v] if args else []
                    aps[num] = (typ, vals)
            if t.startswith('AM'):
                parts = [p for p in t.split('*') if p]
                name = parts[0][2:]
                macros[name] = [p for p in parts[1:] if not p.startswith('0 ') and p != '0']
            continue
        if not t:
            continue
        if t.startswith('G04'):
            continue
        for g in re.findall(r'G0?(\d+)', t):
            g = int(g)
            if g in (1, 2, 3):
                mode = 'G0%d' % g
            elif g == 36:
                region, contours, contour = True, [], [(x, y)]
            elif g == 37:
                if contour:
                    contours.append(contour)
                for c in contours:
                    raster.poly(c, pol)
                region = False
        m = re.search(r'D(\d+)$', t)
        if m and int(m.group(1)) >= 10 and 'X' not in t and 'Y' not in t:
            cur = int(m.group(1))
            continue
        mx = re.search(r'X(-?\d+)', t)
        my = re.search(r'Y(-?\d+)', t)
        mi = re.search(r'I(-?\d+)', t)
        mj = re.search(r'J(-?\d+)', t)
        nx = coord(mx.group(1), xd) - dx if mx else x
        ny = coord(my.group(1), yd) - dy if my else y
        op = re.search(r'D0?([123])$', t)
        if op:
            op = int(op.group(1))
            last_op = op
        elif mx or my:
            op = last_op
        else:
            continue
        if op == 2:
            if region and contour:
                contours.append(contour)
            contour = [(nx, ny)] if region else []
            x, y = nx, ny
        elif op == 1:
            if mode == 'G01':
                seg = [(nx, ny)]
            else:
                ci = coord(mi.group(1), xd) if mi else 0.0
                cj = coord(mj.group(1), yd) if mj else 0.0
                cx, cy = x + ci, y + cj
                r = math.hypot(x - cx, y - cy)
                a0 = math.atan2(y - cy, x - cx)
                a1 = math.atan2(ny - cy, nx - cx)
                if mode == 'G02':
                    sw = a1 - a0
                    if sw >= -1e-9:
                        sw -= 2 * math.pi
                else:
                    sw = a1 - a0
                    if sw <= 1e-9:
                        sw += 2 * math.pi
                n = max(2, int(abs(sw) / math.radians(3)))
                seg = [(cx + r * math.cos(a0 + sw * k / n), cy + r * math.sin(a0 + sw * k / n)) for k in range(1, n + 1)]
            if region:
                contour.extend(seg)
            else:
                typ, vals = aps[cur]
                width = vals[0] if typ in ('C', 'O') else (min(vals[0], vals[1]) if typ == 'R' else 0.1)
                p = (x, y)
                for q in seg:
                    raster.line(p, q, width * scale if scale != 1.0 else width, pol)
                    p = q
            x, y = nx, ny
        elif op == 3:
            x, y = nx, ny
            flash(raster, aps[cur], macros, x, y, pol, scale)


def flash(raster, ap, macros, x, y, pol, scale):
    typ, vals = ap
    s = scale
    if typ == 'C':
        raster.circle(x, y, vals[0] * s, pol)
    elif typ == 'R':
        w, h = vals[0] * s / 2, vals[1] * s / 2
        raster.poly([(x - w, y - h), (x + w, y - h), (x + w, y + h), (x - w, y + h)], pol)
    elif typ == 'O':
        w, h = vals[0] * s, vals[1] * s
        if w > h:
            raster.line((x - (w - h) / 2, y), (x + (w - h) / 2, y), h, pol)
        else:
            raster.line((x, y - (h - w) / 2), (x, y + (h - w) / 2), w, pol)
    elif typ in macros:
        params = {k + 1: v for k, v in enumerate(vals)}
        for prim in macros[typ]:
            f = prim.split(',')
            code = int(f[0])
            a = [evalexpr(v, params) for v in f[1:]]
            if code == 1:      # 円
                exp = pol if a[0] else 255 - pol
                raster.circle(x + a[2] * s, y + a[3] * s, a[1] * s, exp)
            elif code == 21:   # 中心で決める長方形
                exp = pol if a[0] else 255 - pol
                w, h, cx, cy, r = a[1] * s / 2, a[2] * s / 2, a[3] * s, a[4] * s, a[5]
                pts = rot([(cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h)], r)
                raster.poly([(x + px, y + py) for px, py in pts], exp)
            elif code == 20:   # 太さのある線
                exp = pol if a[0] else 255 - pol
                wd, x1, y1, x2, y2, r = a[1] * s, a[2] * s, a[3] * s, a[4] * s, a[5] * s, a[6]
                (x1, y1), (x2, y2) = rot([(x1, y1), (x2, y2)], r)
                L = math.hypot(x2 - x1, y2 - y1) or 1
                nx, ny = -(y2 - y1) / L * wd / 2, (x2 - x1) / L * wd / 2
                raster.poly([(x + x1 + nx, y + y1 + ny), (x + x2 + nx, y + y2 + ny), (x + x2 - nx, y + y2 - ny), (x + x1 - nx, y + y1 - ny)], exp)
            elif code == 4:    # 多角形
                exp = pol if a[0] else 255 - pol
                n = int(a[1])
                pts = [(a[2 + 2 * k] * s, a[3 + 2 * k] * s) for k in range(n + 1)]
                pts = rot(pts, a[2 + 2 * (n + 1)])
                raster.poly([(x + px, y + py) for px, py in pts], exp)


def bbox(path):
    """Gerber に出てくる座標の範囲(mm)を、描かずにざっと求める。"""
    text = open(path, encoding='latin-1').read()
    m = re.search(r'%FSLAX(\d)(\d)Y(\d)(\d)', text)
    xd, yd = int(m.group(2)), int(m.group(4))
    scale = 25.4 if '%MOIN' in text else 1.0
    body = re.sub(r'%[^%]*%', '', text)  # 拡張命令(アパーチャの定義など)の中の X は座標ではない
    xs = [int(v) / 10 ** xd * scale for v in re.findall(r'X(-?\d+)', body)]
    ys = [int(v) / 10 ** yd * scale for v in re.findall(r'Y(-?\d+)', body)]
    return min(xs), min(ys), max(xs), max(ys)


def main():
    a, b, out = sys.argv[1], sys.argv[2], sys.argv[3]
    global W_MM, H_MM
    ba, bb = bbox(a), bbox(b)
    # それぞれの座標の範囲の左下から描き、同じ大きさの画像にする。位置の違いは後で相互相関で合わせる
    W_MM = max(ba[2] - ba[0], bb[2] - bb[0]) + 2 * MARGIN
    H_MM = max(ba[3] - ba[1], bb[3] - bb[1]) + 2 * MARGIN
    ra, rb = Raster(ba[0] - MARGIN, ba[1] - MARGIN), Raster(bb[0] - MARGIN, bb[1] - MARGIN)
    render(a, ra)
    render(b, rb)
    A = np.array(ra.img) > 127
    B = np.array(rb.img) > 127
    px_area = RES * RES
    # 原点の違いによる全体のずれを、画像の相互相関(FFT)で画素単位で求めて合わせる
    fa = np.fft.rfft2(A.astype(np.float32))
    fb = np.fft.rfft2(B.astype(np.float32))
    corr = np.fft.irfft2(fa * np.conj(fb), s=A.shape)
    sy, sx = np.unravel_index(np.argmax(corr), corr.shape)
    if sy > A.shape[0] // 2:
        sy -= A.shape[0]
    if sx > A.shape[1] // 2:
        sx -= A.shape[1]
    sy, sx = int(sy), int(sx)
    B = np.roll(np.roll(B, sy, 0), sx, 1)
    only_a = A & ~B
    only_b = B & ~A
    from PIL import ImageFilter

    def thick(mask):
        # 縁の1画素のずれ(描き方の丸めの差)を除き、太さのある違いだけを残す
        im = Image.fromarray((mask * 255).astype('uint8')).filter(ImageFilter.MinFilter(3))
        return np.array(im) > 127
    ta, tb = thick(only_a), thick(only_b)
    print('合わせたずれ          x %.2f mm, y %.2f mm' % (bb[0] - ba[0] - sx * RES, bb[1] - ba[1] + sy * RES))
    print('元の銅箔の面積        %.2f mm2' % (A.sum() * px_area))
    print('変換後の銅箔の面積    %.2f mm2' % (B.sum() * px_area))
    print('違い(縁の1画素を除く) 元だけ %.3f mm2 / 変換後だけ %.3f mm2' % (ta.sum() * px_area, tb.sum() * px_area))
    rgb = np.zeros(A.shape + (3,), dtype='uint8')
    rgb[A & B] = (200, 200, 200)
    rgb[ta] = (255, 0, 0)
    rgb[tb] = (0, 160, 255)
    Image.fromarray(rgb).save(out)


if __name__ == '__main__':
    main()
