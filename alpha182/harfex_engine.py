"""
harfex_engine.py — Harfex Geometry Engine
==========================================
GUI'den bağımsız, saf hesaplama katmanı.

Kullanım:
  from harfex_engine import MB, DXFImporter, cleanup, ...

Hem masaüstü (main.py) hem web API (FastAPI) bu modülü import eder.
Manifold3d + Shapely + mapbox_earcut burada yaşar — PySide6 bağımlılığı YOK.
"""

import sys
import math
import os
import time
import subprocess
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np


# ── Auto-install gerekli paketler ─────────────────────────────────────────────
def _ensure_package(import_name, pip_name=None):
    try:
        __import__(import_name)
    except ImportError:
        pkg = pip_name or import_name
        print(f"[SETUP] {pkg} bulunamadı, kuruluyor...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[SETUP] {pkg} kuruldu.")

_ensure_package("manifold3d")
_ensure_package("mapbox_earcut")


# ── Thread pool (paralel extrude işlemleri) ───────────────────────────────────
os.environ.setdefault("OMP_NUM_THREADS",      str(max(1, (os.cpu_count() or 4) - 1)))
os.environ.setdefault("OPENBLAS_NUM_THREADS", str(max(1, (os.cpu_count() or 4) - 1)))
CPU_WORKERS = max(1, min(4, (os.cpu_count() or 4) - 1))
GEOMETRY_EXECUTOR = ThreadPoolExecutor(max_workers=CPU_WORKERS)


# ── mapbox_earcut ─────────────────────────────────────────────────────────────
try:
    import mapbox_earcut as earcut_lib
    HAS_EARCUT = True
except ImportError:
    HAS_EARCUT = False
    print("[WARN] mapbox_earcut not found: pip install mapbox_earcut")


# ── Shapely ───────────────────────────────────────────────────────────────────
try:
    from shapely.geometry import Polygon, MultiPolygon, LineString
    from shapely.ops import unary_union, polygonize
    from shapely.validation import make_valid
    from shapely.prepared import prep
    HAS_SHAPELY = True
except Exception:
    Polygon = MultiPolygon = LineString = unary_union = polygonize = make_valid = prep = None
    HAS_SHAPELY = False


# ── ezdxf ─────────────────────────────────────────────────────────────────────
try:
    import ezdxf
except Exception:
    ezdxf = None

# ── svgelements ────────────────────────────────────────────────────────────────
try:
    import svgelements
except Exception:
    svgelements = None


# ── Timer ─────────────────────────────────────────────────────────────────────
class StageTimer:
    def __init__(self): self.records = []
    def clear(self): self.records.clear()
    def add(self, name, s):
        self.records.append((name, float(s)))
        print(f"[TIME] {name}: {s:.3f}s")
    def report(self, path="timing.txt"):
        try:
            with open(path, "w", encoding="utf-8") as f:
                total = sum(v for _, v in self.records)
                f.write(f"TOTAL:{total:.3f}s\n")
                for n, d in self.records:
                    f.write(f"{n}:{d:.3f}s ({d/total*100:.1f}%)\n")
        except Exception:
            pass

def _tic(): return time.perf_counter()
def _toc(t, l, s): t.add(l, time.perf_counter() - s)


# ── Temel geometri yardımcıları ───────────────────────────────────────────────
def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])

def clean_points(pts, eps=0.001):
    out = []
    for p in pts:
        p = (float(p[0]), float(p[1]))
        if not out or dist(out[-1], p) > eps:
            out.append(p)
    return out

def signed_area(pts):
    p = list(pts)
    if len(p) > 1 and dist(p[0], p[-1]) < 0.001:
        p = p[:-1]
    a = 0.0
    for i in range(len(p)):
        x1, y1 = p[i]; x2, y2 = p[(i + 1) % len(p)]
        a += x1 * y2 - x2 * y1
    return a * 0.5

def _as_polys(geom, min_area=0.01):
    if geom is None or geom.is_empty: return []
    try:
        if make_valid: geom = make_valid(geom)
        geom = geom.buffer(0)
    except Exception:
        pass
    if isinstance(geom, Polygon):        polys = [geom]
    elif isinstance(geom, MultiPolygon): polys = list(geom.geoms)
    elif hasattr(geom, "geoms"):         polys = [g for g in geom.geoms if isinstance(g, Polygon)]
    else:                                polys = []
    return [p if p.is_valid else p.buffer(0)
            for p in polys if not p.is_empty and p.area >= min_area]

def _clean(geom, min_area=0.01):
    ps = _as_polys(geom, min_area)
    if not ps: return geom
    try: return unary_union(ps).buffer(0)
    except Exception: return ps[0]


# ── Yüz dolgu deseni (honeycomb / grid / lines) ───────────────────────────────
def _face_fill_pattern(geom, fill_idx, cell_mm=8.0, wall_mm=1.6, border_mm=1.5):
    """Yüz Shapely geometrisine tekrarlayan boşluk deseni keser.
    fill_idx : 0=Solid (no-op), 1=Honeycomb, 2=Grid, 3=Lines
    """
    if fill_idx == 0 or geom is None or geom.is_empty:
        return geom
    if Polygon is None or unary_union is None:
        return geom

    minx, miny, maxx, maxy = geom.bounds
    pad = cell_mm * 2.0
    mx0, my0 = minx - pad, miny - pad
    mx1, my1 = maxx + pad, maxy + pad

    _EPS = 0.004

    if wall_mm >= cell_mm * 0.85:
        return geom

    voids = []
    try:
        if fill_idx == 1:           # ── Honeycomb ──────────────────────────
            r = cell_mm * 0.5
            # Yatay duvar: col_step - 2*r_void*cos(30°) = wall_mm → r_void_h = (col_step - wall_mm) / sqrt(3)
            # Dikey duvar: row_step - 2*r_void = wall_mm → r_void_v = (row_step - wall_mm) / 2
            col_step = r * math.sqrt(3)
            row_step = r * 1.5
            r_void_h = (col_step - wall_mm) / math.sqrt(3)
            r_void_v = (row_step - wall_mm) / 2.0
            r_void = min(r_void_h, r_void_v) - _EPS
            if r_void <= 0.2:
                return geom
            row_idx = 0
            y = my0
            while y <= my1 + row_step:
                x_off = col_step * 0.5 if (row_idx % 2 == 1) else 0.0
                x = mx0 + x_off
                while x <= mx1 + col_step:
                    pts = [
                        (x + r_void * math.cos(math.radians(90 + 60 * k)),
                         y + r_void * math.sin(math.radians(90 + 60 * k)))
                        for k in range(6)
                    ]
                    voids.append(Polygon(pts))
                    x += col_step
                y += row_step
                row_idx += 1

        elif fill_idx == 2:         # ── Grid (kare delikler) ─────────────
            void_sz = max(0.1, cell_mm - wall_mm - _EPS * 2)
            period  = cell_mm
            y = my0 + wall_mm * 0.5
            while y <= my1:
                x = mx0 + wall_mm * 0.5
                while x <= mx1:
                    voids.append(Polygon([
                        (x,           y          ),
                        (x + void_sz, y          ),
                        (x + void_sz, y + void_sz),
                        (x,           y + void_sz),
                    ]))
                    x += period
                y += period

        elif fill_idx == 3:         # ── Lines (dikey şeritler) ───────────
            void_w  = max(0.1, cell_mm - wall_mm - _EPS * 2)
            period  = cell_mm
            x = mx0 + wall_mm * 0.5
            while x <= mx1:
                voids.append(Polygon([
                    (x,          my0),
                    (x + void_w, my0),
                    (x + void_w, my1),
                    (x,          my1),
                ]))
                x += period

        if not voids:
            return geom

        void_union = MultiPolygon(voids)

        if border_mm > 0.0:
            inner = geom.buffer(-border_mm, join_style="round", quad_segs=8)
            if inner is None or inner.is_empty:
                return geom
            try:
                void_union = void_union.intersection(inner)
            except Exception:
                pass

        if void_union is None or void_union.is_empty:
            return geom

        result = geom.difference(void_union)
        if result is None or result.is_empty:
            return geom
        result = result.buffer(0)
        if result is None or result.is_empty:
            return geom
        if make_valid:
            result = make_valid(result)
        return result

    except Exception as e:
        print(f"[FACE FILL] pattern error fill={fill_idx}: {e}")
        return geom


# ── Earcut triangülasyon ──────────────────────────────────────────────────────
def _earcut_polygon(poly):
    """Shapely Polygon → düz kapak (2D) triangülasyonu.
    mapbox_earcut 2.x API: triangulate_float64(vertices, ring_end_indices)
    """
    if not HAS_EARCUT: return [], []
    if poly is None or poly.is_empty or poly.area < 1e-8: return [], []

    rings = []
    ring_ends = []

    ext = list(poly.exterior.coords)
    if len(ext) > 1 and ext[0] == ext[-1]:
        ext = ext[:-1]
    if len(ext) < 3:
        return [], []
    rings.extend(ext)
    ring_ends.append(len(rings))

    for interior in poly.interiors:
        ic = list(interior.coords)
        if len(ic) > 1 and ic[0] == ic[-1]:
            ic = ic[:-1]
        if len(ic) < 3:
            continue
        rings.extend(ic)
        ring_ends.append(len(rings))

    flat = np.array([[c[0], c[1]] for c in rings], dtype=np.float64)
    ends = np.array(ring_ends, dtype=np.uint32)

    _ctr = flat.mean(axis=0)
    _scl = max(float(np.abs(flat - _ctr).max()), 1e-10)
    tris = None
    for _attempt, _fin in enumerate([
        (flat - _ctr) / _scl,
        flat - _ctr,
        flat - flat[0],
    ]):
        try:
            _t = earcut_lib.triangulate_float64(_fin, ends)
            if len(_t) > 0:
                tris = _t.reshape(-1, 3); break
        except Exception as _e:
            print(f"[EARCUT] attempt {_attempt+1}: {_e}")

    if tris is None:
        return [], []
    verts = [(float(flat[i, 0]), float(flat[i, 1])) for i in range(len(flat))]
    faces = [tuple(int(x) for x in t) for t in tris]
    return verts, faces


# ── MB — Manifold3D mesh builder (Harfex'in kalbi) ───────────────────────────
class MB:

    @staticmethod
    def ring_walls(coords, z0, z1):
        """Kapalı bir halka için z0–z1 arasında dörtgen yan duvarlar üretir."""
        v, f, m = [], [], {}

        def vi(x, y, z):
            k = (round(x, 6), round(y, 6), round(z, 6))
            if k not in m:
                m[k] = len(v); v.append(k)
            return m[k]

        c = list(coords)
        if len(c) > 1 and dist(c[0], c[-1]) < 0.0001:
            c = c[:-1]
        n = len(c)
        if n < 2:
            return v, f
        for i in range(n):
            p0, p1 = c[i], c[(i + 1) % n]
            b0 = vi(p0[0], p0[1], z0); b1 = vi(p1[0], p1[1], z0)
            t0 = vi(p0[0], p0[1], z1); t1 = vi(p1[0], p1[1], z1)
            f.append((b0, b1, t1))
            f.append((b0, t1, t0))
        return v, f

    @staticmethod
    def cap(poly, z, top=True):
        """z yüksekliğinde düz kapak — earcut triangülasyonu ile."""
        if poly is None or poly.is_empty or poly.area < 1e-8:
            return [], []
        v2d, faces = _earcut_polygon(poly)
        if not faces:
            return [], []
        v3d = [(x, y, z) for x, y in v2d]
        if not top:
            faces = [(a, c, b) for a, b, c in faces]
        return v3d, faces

    @staticmethod
    def extrude(poly, z0, z1, cap_bottom=True, cap_top=True):
        """
        Tek Shapely Polygon → tam manifold solid mesh.
        Her iki kapak gerektiğinde manifold3d kullanır (nm=0 garantili).
        Kısmi kapak durumlarında earcut fallback.
        """
        # ── manifold3d path ───────────────────────────────────────────────
        if cap_bottom and cap_top:
            try:
                import manifold3d as m3d
                ext = list(poly.exterior.coords)
                if ext[0] == ext[-1]:
                    ext = ext[:-1]
                contours = [[[float(x), float(y)] for x, y in ext]]
                for interior in poly.interiors:
                    ic = list(interior.coords)
                    if ic[0] == ic[-1]:
                        ic = ic[:-1]
                    contours.append([[float(x), float(y)] for x, y in ic])
                cs = m3d.CrossSection(contours, m3d.FillRule.EvenOdd)
                if cs.area() < 1e-6:
                    return [], []
                mf = m3d.Manifold.extrude(cs, height=float(z1 - z0))
                if mf.is_empty():
                    return [], []
                mesh = mf.to_mesh()
                va = np.array(mesh.vert_properties, dtype=np.float32)
                fa = np.array(mesh.tri_verts, dtype=np.int32)
                va[:, 2] += float(z0)
                verts = [(float(v[0]), float(v[1]), float(v[2])) for v in va]
                faces = [(int(t[0]), int(t[1]), int(t[2])) for t in fa]
                return verts, faces
            except Exception as _me:
                print(f"[MANIFOLD3D FAIL] {_me} — falling back to earcut")

        if not HAS_EARCUT:
            return [], []

        _EPS = 1e-4

        def _snap(pts):
            out = []
            for p in pts:
                p2 = (float(p[0]), float(p[1]))
                if not out or math.hypot(p2[0]-out[-1][0], p2[1]-out[-1][1]) > _EPS:
                    out.append(p2)
            while len(out) > 1 and math.hypot(out[0][0]-out[-1][0], out[0][1]-out[-1][1]) <= _EPS:
                out = out[:-1]
            return out

        ext = list(poly.exterior.coords)
        if len(ext) > 1 and ext[0] == ext[-1]:
            ext = ext[:-1]
        ext = _snap(ext)
        if len(ext) < 3:
            return [], []

        int_rings = []
        for interior in poly.interiors:
            ic = list(interior.coords)
            if len(ic) > 1 and ic[0] == ic[-1]:
                ic = ic[:-1]
            ic = _snap(ic)
            if len(ic) >= 3:
                int_rings.append(ic)

        all_xy    = list(ext)
        ring_ends = [len(ext)]
        for ring in int_rings:
            all_xy.extend(ring)
            ring_ends.append(len(all_xy))

        N = len(all_xy)

        verts = ([(float(x), float(y), float(z0)) for x, y in all_xy] +
                 [(float(x), float(y), float(z1)) for x, y in all_xy])

        faces = []

        ext_n = len(ext)
        for i in range(ext_n):
            b0 = i;      b1 = (i + 1) % ext_n
            t0 = b0 + N; t1 = b1 + N
            faces.append((b0, b1, t1))
            faces.append((b0, t1, t0))

        offset = ext_n
        for ring in int_rings:
            rn = len(ring)
            for i in range(rn):
                b0 = offset + i;       b1 = offset + (i + 1) % rn
                t0 = b0 + N;            t1 = b1 + N
                faces.append((b0, b1, t1))
                faces.append((b0, t1, t0))
            offset += rn

        if cap_top or cap_bottom:
            flat = np.array([[xy[0], xy[1]] for xy in all_xy], dtype=np.float64)
            ends = np.array(ring_ends, dtype=np.uint32)

            _ctr = flat.mean(axis=0)
            _scl = max(float(np.abs(flat - _ctr).max()), 1e-10)
            tris_2d = None
            for _attempt, _fin in enumerate([
                (flat - _ctr) / _scl,
                flat - _ctr,
                flat - flat[0],
            ]):
                try:
                    _t = earcut_lib.triangulate_float64(_fin, ends)
                    if len(_t) > 0:
                        tris_2d = _t.reshape(-1, 3)
                        if _attempt > 0:
                            print(f"[CAP] needed attempt {_attempt+1} N={N}")
                        break
                except Exception as _e:
                    print(f"[CAP] attempt {_attempt+1} error: {_e}")

            if tris_2d is None:
                print(f"[CAP FAIL→SHAPELY] N={N} ends={ring_ends}")
                try:
                    from shapely.ops import triangulate as _sh_tri
                    from shapely.geometry import Polygon as _SPoly
                    _snap_poly = _SPoly(ext, int_rings)
                    if not _snap_poly.is_valid:
                        _snap_poly = _snap_poly.buffer(0)
                    _coord_idx = {(round(x, 6), round(y, 6)): i
                                  for i, (x, y) in enumerate(all_xy)}
                    _buf = _snap_poly.buffer(1e-4)
                    _good = [t for t in _sh_tri(_snap_poly)
                             if _buf.contains(t.representative_point())]
                    _fb = []
                    for _t in _good:
                        _cs = list(_t.exterior.coords)[:-1]
                        _ix = [_coord_idx.get((round(cx, 6), round(cy, 6)))
                               for cx, cy in _cs]
                        if None not in _ix and len(_ix) == 3:
                            _fb.append(_ix)
                    if _fb:
                        tris_2d = np.array(_fb, dtype=np.int64)
                        print(f"[CAP SHAPELY] {len(_fb)} tris OK")
                except Exception as _e:
                    print(f"[CAP SHAPELY FAIL] {_e}")

            if tris_2d is not None and len(tris_2d) > 0:
                if cap_top:
                    for a, b, c in tris_2d:
                        faces.append((int(a) + N, int(b) + N, int(c) + N))
                if cap_bottom:
                    for a, b, c in tris_2d:
                        faces.append((int(a), int(c), int(b)))

        if faces:
            _F = np.array(faces, dtype=np.int64)
            _e = np.sort(
                np.vstack([_F[:, [0, 1]], _F[:, [1, 2]], _F[:, [2, 0]]]), axis=1)
            _, _cnt = np.unique(_e, axis=0, return_counts=True)
            _nm = int(np.sum(_cnt != 2))
            if _nm:
                print(f"[EXTRUDE NM] nm={_nm} open={int(np.sum(_cnt==1))} "
                      f"over={int(np.sum(_cnt>2))} "
                      f"ext={len(ext)} holes={len(int_rings)} N={N}")

        return verts, faces

    @staticmethod
    def extrude_geom(geom, z0, z1, cap_bottom=True, cap_top=True):
        """Shapely geometry içindeki tüm polygon'ları tek mesh'e extrude eder."""
        polys = _as_polys(geom, 0.001)
        if not polys:
            return [], []
        print(f"[EXTRUDE_GEOM] {len(polys)} sub-polygons")

        def one(poly):
            try:
                if poly.is_empty: return [], []
                if isinstance(poly, MultiPolygon):
                    sub_polys = list(poly.geoms)
                elif isinstance(poly, Polygon):
                    sub_polys = [poly]
                else:
                    return [], []
                av2, af2, off2 = [], [], 0
                for sp in sub_polys:
                    if sp.is_empty or sp.area < 0.001: continue
                    v, f = MB.extrude(sp, z0, z1, cap_bottom, cap_top)
                    if not v or not f: continue
                    av2.extend(v)
                    af2.extend([(a + off2, b + off2, c + off2) for a, b, c in f])
                    off2 += len(v)
                return av2, af2
            except Exception as e:
                print(f"[EXTRUDE] {e}"); return [], []

        results = (
            [one(p) for p in polys] if len(polys) <= 3
            else list(GEOMETRY_EXECUTOR.map(one, polys))
        )
        av, af, off = [], [], 0
        for v, f in results:
            if not v or not f: continue
            av.extend(v)
            af.extend([(a + off, b + off, c + off) for a, b, c in f])
            off += len(v)
        return av, af


# ── Mesh cleanup (vertex welding + degenerate face temizliği) ─────────────────
def cleanup(verts, faces, dec=6):
    V = np.asarray(verts, dtype=np.float64)
    F = np.asarray(faces, dtype=np.int64)
    info = {"in_v": len(V), "in_f": len(F)}
    if not len(V) or not len(F):
        info.update({"out_v": 0, "out_f": 0, "nm": 0})
        return V.astype(np.float32), F.astype(np.int32), info

    WELD_GRID = 1e4
    Vkey = np.round(V * WELD_GRID).astype(np.int64)
    Vkey_flat = Vkey.view(np.dtype((np.void, Vkey.dtype.itemsize * 3))).ravel()
    _, fwd, inv = np.unique(Vkey_flat, return_index=True, return_inverse=True)
    U = V[fwd]
    F = inv[F]

    mask = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    F = F[mask]

    if len(F):
        tri = U[F]
        a2 = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        F = F[a2 > 1e-12]

    if len(F):
        _, k = np.unique(F, axis=0, return_index=True)
        F = F[np.sort(k)]

    nm = 0
    if len(F):
        used = np.unique(F.reshape(-1))
        remap = np.full(len(U), -1, dtype=np.int64)
        remap[used] = np.arange(len(used), dtype=np.int64)
        U = U[used]; F = remap[F]
        edges = np.sort(np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
        _, cnt = np.unique(edges, axis=0, return_counts=True)
        nm = int(np.sum(cnt != 2))
        if nm:
            print(f"[CLEANUP] nm={nm} | open={int(np.sum(cnt==1))} "
                  f"over={int(np.sum(cnt>2))} | verts={len(U)} faces={len(F)}")

    info.update({"out_v": int(len(U)), "out_f": int(len(F)), "nm": nm})
    return U.astype(np.float32), F.astype(np.int32), info


# ── DXF Importer ─────────────────────────────────────────────────────────────
class DXFImporter:

    @staticmethod
    def read_paths(path):
        if not ezdxf:
            raise RuntimeError("ezdxf not installed: pip install ezdxf")
        doc = ezdxf.readfile(path)
        msp = doc.modelspace()
        paths = []

        def add(pts, closed=False):
            p = clean_points(pts, 0.0001)
            if len(p) < 2: return
            if closed and dist(p[0], p[-1]) > 0.0001:
                p.append(p[0])
            paths.append({
                "points": p,
                "closed": closed or (len(p) > 2 and dist(p[0], p[-1]) < 0.001)
            })

        def arc_pts(cx, cy, r, s, e, n=96):
            if e < s: e += 360
            return [
                (cx + r * math.cos(math.radians(s + (e - s) * i / n)),
                 cy + r * math.sin(math.radians(s + (e - s) * i / n)))
                for i in range(n + 1)
            ]

        def virt(v):
            t = v.dxftype()
            if t == "LINE":
                add([(v.dxf.start.x, v.dxf.start.y), (v.dxf.end.x, v.dxf.end.y)])
            elif t == "ARC":
                c = v.dxf.center
                add(arc_pts(c.x, c.y, float(v.dxf.radius),
                            float(v.dxf.start_angle), float(v.dxf.end_angle)))
            elif t == "CIRCLE":
                c = v.dxf.center
                add(arc_pts(c.x, c.y, float(v.dxf.radius), 0, 360, 192), closed=True)

        for e in msp:
            t = e.dxftype()
            if t == "LINE":
                add([(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)])
            elif t == "LWPOLYLINE":
                used_virtual = False
                try:
                    if any(abs(p[2]) > 1e-9 for p in e.get_points("xyb")):
                        for v in e.virtual_entities(): virt(v)
                        used_virtual = True
                except Exception:
                    pass
                if not used_virtual:
                    add([(p[0], p[1]) for p in e.get_points()], closed=bool(e.closed))
            elif t == "POLYLINE":
                add([(v.dxf.location.x, v.dxf.location.y) for v in e.vertices],
                    closed=bool(e.is_closed))
            elif t == "CIRCLE":
                c = e.dxf.center
                add(arc_pts(c.x, c.y, float(e.dxf.radius), 0, 360, 192), closed=True)
            elif t == "ARC":
                c = e.dxf.center
                add(arc_pts(c.x, c.y, float(e.dxf.radius),
                            float(e.dxf.start_angle), float(e.dxf.end_angle)))
            elif t == "SPLINE":
                pts = []
                try:
                    for p in e.construction_tool().flattening(distance=0.2, segments=16):
                        pts.append((float(p[0]), float(p[1])))
                except Exception:
                    pass
                if len(pts) < 2:
                    try: pts = [(p[0], p[1]) for p in e.fit_points]
                    except Exception: pass
                add(pts, closed=bool(getattr(e, "closed", False)))
            elif t == "ELLIPSE":
                try:
                    add([(float(p[0]), float(p[1])) for p in e.flattening(distance=0.2)],
                        closed=bool(e.closed))
                except Exception:
                    pass

        return DXFImporter.merge(paths)

    @staticmethod
    def merge(paths, eps=0.20):
        closed = [p for p in paths if p["closed"]]
        op = [p for p in paths if not p["closed"]]
        changed = True
        while changed:
            changed = False; used = [False] * len(op); np2 = []
            for i, p in enumerate(op):
                if used[i]: continue
                pts = list(p["points"]); used[i] = True; lc = True
                while lc:
                    lc = False
                    for j, q in enumerate(op):
                        if used[j]: continue
                        qp = q["points"]
                        if   dist(pts[-1], qp[0])  <= eps: pts.extend(qp[1:]);                  used[j] = lc = changed = True
                        elif dist(pts[-1], qp[-1]) <= eps: pts.extend(list(reversed(qp[:-1]))); used[j] = lc = changed = True
                        elif dist(pts[0],  qp[-1]) <= eps: pts = qp[:-1] + pts;                 used[j] = lc = changed = True
                        elif dist(pts[0],  qp[0])  <= eps: pts = list(reversed(qp[1:])) + pts;  used[j] = lc = changed = True
                ic = len(pts) > 2 and dist(pts[0], pts[-1]) <= eps
                if ic and dist(pts[0], pts[-1]) > 0.0001:
                    pts.append(pts[0])
                np2.append({"points": clean_points(pts, 0.0001), "closed": ic})
            op = np2
        return closed + op


# ── SVG Importer ───────────────────────────────────────────────────────────────
class SVGImporter:
    """SVG path/shape -> {"points":[(x,y),...], "closed": bool} listesi.
    svgelements ile parse edilir; transformlar reify ile bakeleşir,
    eğriler düzleştirilir, px -> mm dönüşümü (96dpi) ve Y-flip uygulanır."""

    PX_TO_MM = 25.4 / 96.0

    @staticmethod
    def read_paths(path):
        if not svgelements:
            raise RuntimeError("svgelements not installed: pip install svgelements")
        from svgelements import (
            SVG, Path as SEPath, Shape, Move, Close, Line,
            CubicBezier, QuadraticBezier, Arc,
        )

        svg = SVG.parse(path, reify=True)
        scale = SVGImporter.PX_TO_MM
        paths = []

        def to_mm(pt):
            return (pt.x * scale, -pt.y * scale)

        def approx_length(seg):
            """seg.length() üzerindeki maliyetli adaptif alt-bölmeyi önlemek için
            kontrol noktaları arası mesafelerin toplamıyla hızlı bir üst sınır hesaplar."""
            pts = [p for p in (getattr(seg, a, None)
                                for a in ("start", "control1", "control2", "control", "end"))
                   if p is not None]
            return sum(abs(b - a) for a, b in zip(pts[:-1], pts[1:]))

        def flatten_curve(seg):
            length = approx_length(seg)
            n = max(4, min(64, int(length / 1.5)))
            return [to_mm(seg.point(i / n)) for i in range(1, n + 1)]

        def add(pts, closed):
            p = clean_points(pts, 0.0001)
            if len(p) < 2:
                return
            if closed and dist(p[0], p[-1]) > 0.0001:
                p.append(p[0])
            paths.append({
                "points": p,
                "closed": closed or (len(p) > 2 and dist(p[0], p[-1]) < 0.001)
            })

        for e in svg.elements():
            if not isinstance(e, Shape):
                continue
            if isinstance(e, SVG):
                continue
            vals = getattr(e, "values", {}) or {}
            if vals.get("display") == "none" or vals.get("visibility") == "hidden":
                continue
            try:
                sub = SEPath(e)
            except Exception:
                continue
            if len(sub) == 0:
                continue

            current, start = [], None
            for seg in sub:
                if isinstance(seg, Move):
                    if len(current) >= 2:
                        add(current, closed=False)
                    start = seg.end
                    current = [to_mm(start)] if start is not None else []
                elif isinstance(seg, Close):
                    if start is not None:
                        current.append(to_mm(start))
                    if len(current) >= 2:
                        add(current, closed=True)
                    current, start = [], None
                elif isinstance(seg, Line):
                    if seg.end is not None:
                        current.append(to_mm(seg.end))
                elif isinstance(seg, (CubicBezier, QuadraticBezier, Arc)):
                    try:
                        current.extend(flatten_curve(seg))
                    except Exception:
                        if seg.end is not None:
                            current.append(to_mm(seg.end))
            if len(current) >= 2:
                add(current, closed=False)

        return DXFImporter.merge(paths)


# ── Duvar profil sabitleri ────────────────────────────────────────────────────
# (height_normalized, lean_normalized) çiftleri

_ACILI_PROFILE = [
    (0.000, 0.000), (0.037, 0.000), (0.117, 0.000), (0.198, 0.000),
    (0.235, 0.000), (0.313, 0.156), (0.485, 0.500), (0.658, 0.844),
    (0.736, 1.000), (0.777, 1.000), (0.868, 1.000), (0.959, 1.000),
    (1.000, 1.000),
]

_KAVISLI_PROFILE = [
    (0.000, 0.000), (0.080, 0.000), (0.159, 0.000),
    (0.250, 0.050), (0.340, 0.180),
    (0.481, 0.505),
    (0.620, 0.760), (0.720, 0.926),
    (0.832, 1.000), (0.916, 1.000), (1.000, 1.000),
]

_ALT_TIRNAK_PROFILE = [
    (0.0000, 0.0081), (0.0345, 0.4144), (0.0690, 0.5204), (0.1034, 0.5879),
    (0.1379, 0.6408), (0.1724, 0.6850), (0.2069, 0.7233), (0.2414, 0.7569),
    (0.2759, 0.7869), (0.3103, 0.8139), (0.3448, 0.8381), (0.3793, 0.8599),
    (0.4138, 0.8795), (0.4483, 0.8970), (0.4828, 0.9128), (0.5172, 0.9268),
    (0.5517, 0.9393), (0.5862, 0.9504), (0.6207, 0.9601), (0.6552, 0.9686),
    (0.6897, 0.9759), (0.7241, 0.9821), (0.7586, 0.9873), (0.7931, 0.9915),
    (0.8276, 0.9949), (0.8621, 0.9973), (0.8966, 0.9990), (0.9310, 0.9998),
    (0.9655, 1.0000), (1.0000, 0.9997),
]
_ALT_TIRNAK_ASPECT = 36.35 / 42.18

_UST_TIRNAK_PROFILE = [
    (0.0000, 1.0000), (0.0345, 0.9853), (0.0690, 0.9700), (0.1034, 0.9542),
    (0.1379, 0.9377), (0.1724, 0.9207), (0.2069, 0.9029), (0.2414, 0.8845),
    (0.2759, 0.8654), (0.3103, 0.8455), (0.3448, 0.8248), (0.3793, 0.8032),
    (0.4138, 0.7807), (0.4483, 0.7572), (0.4828, 0.7326), (0.5172, 0.7068),
    (0.5517, 0.6798), (0.5862, 0.6514), (0.6207, 0.6215), (0.6552, 0.5898),
    (0.6897, 0.5562), (0.7241, 0.5203), (0.7586, 0.4819), (0.7931, 0.4402),
    (0.8276, 0.3948), (0.8621, 0.3444), (0.8966, 0.2872), (0.9310, 0.2202),
    (0.9655, 0.1356), (1.0000, 0.0000),
]
_UST_TIRNAK_ASPECT = 187.16 / 293.21


# ── Earcut başlangıç testi ────────────────────────────────────────────────────
def _test_earcut_startup():
    """Program açılışında earcut + MB.extrude doğrulaması yapar."""
    if not HAS_EARCUT:
        print("[EARCUT TEST] SKIP — mapbox_earcut not installed")
        return

    ok = True

    sq = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float64)
    ends1 = np.array([4], dtype=np.uint32)
    try:
        t1 = earcut_lib.triangulate_float64(sq, ends1)
        n1 = len(t1) // 3
        status = "PASS" if n1 == 2 else "FAIL"
        print(f"[EARCUT TEST] square cap: {status}  ({n1} triangles)")
        if n1 != 2: ok = False
    except Exception as e:
        print(f"[EARCUT TEST] square cap: FAIL  exception: {e}"); ok = False

    ring = np.array([
        [0, 0], [4, 0], [4, 4], [0, 4],
        [1, 1], [1, 3], [3, 3], [3, 1],
    ], dtype=np.float64)
    ends2 = np.array([4, 8], dtype=np.uint32)
    try:
        t2 = earcut_lib.triangulate_float64(ring, ends2)
        n2 = len(t2) // 3
        status = "PASS" if n2 == 8 else "FAIL"
        print(f"[EARCUT TEST] ring cap: {status}  ({n2} triangles)")
        if n2 != 8: ok = False
    except Exception as e:
        print(f"[EARCUT TEST] ring cap: FAIL  exception: {e}"); ok = False

    if Polygon:
        try:
            poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
            av, af = MB.extrude(poly, 0.0, 1.0)
            status = "PASS" if len(af) == 12 else "FAIL"
            print(f"[EARCUT TEST] MB.extrude square: {status}  ({len(af)} faces)")
            if len(af) != 12: ok = False
        except Exception as e:
            print(f"[EARCUT TEST] MB.extrude square: FAIL  exception: {e}"); ok = False

    msg = "ALL TESTS PASSED" if ok else "SOME TESTS FAILED"
    print(f"[EARCUT TEST] {msg}")
    return ok
