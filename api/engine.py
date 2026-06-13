"""
engine.py — HarfexEngine
========================
ModelSlot'u subclass eder ve LeftPanel'in tüm saf hesaplama
metodlarını Qt bağımlılığı olmadan içerir.

Kullanım:
    from engine import HarfexEngine
    eng = HarfexEngine()
    eng.load_dxf_bytes(data, suffix=".dxf")
    eng.wall_mm = 5; eng.height_mm = 45; ...
    eng.build()
    stl_bytes  = eng.export_stl_bytes()
    face_bytes = eng.export_face_stl_bytes()
    cover_bytes = eng.export_cover_stl_bytes()
"""

import sys
import io
import math
import os
import time
import tempfile
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent / "alpha182"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from harfex_engine import (
    MB, DXFImporter, SVGImporter, cleanup,
    _face_fill_pattern, _as_polys, _clean,
    dist, clean_points, signed_area,
    StageTimer, _tic, _toc,
    Polygon, MultiPolygon, LineString,
    unary_union, polygonize, make_valid, prep,
    _ACILI_PROFILE, _KAVISLI_PROFILE,
    _ALT_TIRNAK_PROFILE, _ALT_TIRNAK_ASPECT,
    _UST_TIRNAK_PROFILE, _UST_TIRNAK_ASPECT,
)
try:
    import ezdxf
except ImportError:
    ezdxf = None

class HarfexEngine:
    """
    ModelSlot + tüm hesaplama metodları.
    LeftPanel'in saf (Qt-sız) karşılığı.
    """

    def __init__(self):
        # ── Paths ────────────────────────────────────────────────────────────
        self.paths      = []; self.base_paths = []
        self.has_model  = False
        # ── Parametreler ─────────────────────────────────────────────────────
        self.wall_mm    = 5.0;  self.height_mm  = 45.0
        self.arc_sm     = 8.0;  self.round_c    = 1.0;   self.simplify = 0.05
        self.face_mode  = 1;    self.face_thickness = 3.0
        self.plexiglas_offset = -0.5; self.foam_offset = 0.0
        self.wall_type  = 0;    self.wall_taper = 13.81
        self.flip_z     = False; self.mx = False; self.my = False
        self.top_tab    = False; self.bot_tab = False
        self.top_proj   = 4.0;  self.bot_proj   = 4.0
        self.top_tab_z  = -2.0; self.bot_tab_z  = 2.0
        self.cover_ct   = 2.0;  self.cover_wh   = 15.0
        self.cover_clearance = 0.05; self.cover_wt = 3.0
        self.face_fill              = 0
        self.face_fill_cell         = 8.0
        self.face_fill_wall         = 1.6
        self.face_fill_border       = 1.5
        self.face_fill_solid_pos    = 0
        # ── İç durum ─────────────────────────────────────────────────────────
        self._last_base = None; self._last_wg = None; self._last_ig = None
        self._last_wall_v = None; self._last_wall_f = None
        self.last_v = None; self.last_f = None
        self.last_face_v = None; self.last_face_f = None
        self.timer = StageTimer()
        self.fast  = False

    # ─────────────────────────────────────────────────────────────────────────
    # Yükleme
    # ─────────────────────────────────────────────────────────────────────────
    def load_dxf_file(self, path: str):
        """Disk'ten DXF/SVG yükle (uzantıya göre)."""
        suffix = Path(path).suffix.lower()
        if suffix == ".svg":
            new_paths = SVGImporter.read_paths(path)
        else:
            new_paths = DXFImporter.read_paths(path)
        if not new_paths:
            raise RuntimeError("Dosyada görüntülenebilir yol bulunamadı.")
        self.paths      = new_paths
        self.has_model  = False
        self.mx = False; self.my = False; self.flip_z = False
        self.top_tab = False; self.bot_tab = False
        self.normalize()
        self.base_paths = [{"points": list(p["points"]), "closed": p["closed"]}
                           for p in self.paths]

    def load_dxf_bytes(self, data: bytes, suffix: str = ".dxf"):
        """Byte stream'den DXF yükle (API için)."""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            self.load_dxf_file(tmp_path)
        finally:
            os.unlink(tmp_path)

    def set_params(self, **kw):
        """Herhangi bir parametreyi anahtar-değer çiftiyle ayarla."""
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)

    # ─────────────────────────────────────────────────────────────────────────
    # Normalize
    # ─────────────────────────────────────────────────────────────────────────
    def normalize(self):
        xs, ys = [], []
        for p in self.paths:
            for x, y in p["points"]:
                xs.append(x); ys.append(y)
        if not xs: return
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
        for p in self.paths:
            p["points"] = [(x - cx, y - cy) for x, y in p["points"]]
        self.base_paths = [{"points": list(p["points"]), "closed": p["closed"]}
                           for p in self.paths]

    # ─────────────────────────────────────────────────────────────────────────
    # Ana build pipeline
    # ─────────────────────────────────────────────────────────────────────────
    def build(self, fast=False):
        """Duvar + yüz mesh'lerini hesapla. Tüm parametreler self'ten okunur."""
        if not self.paths:
            raise RuntimeError("Önce DXF yükleyin.")
        self.timer.clear(); t0 = _tic()
        old_arc  = self.arc_sm
        old_simp = self.simplify
        if fast:
            self.arc_sm  = min(self.arc_sm, 8.0)
            self.simplify = max(self.simplify, 0.04)
        try:
            # 1. Letter polygon
            s = _tic()
            base = self.build_letter()
            _toc(self.timer, "1-letter", s)
            if base is None or base.is_empty:
                raise RuntimeError("Path'lerden polygon oluşturulamadı.")

            # 2. Wall geometry
            s = _tic()
            wg, ig = self.build_wall(base)
            _toc(self.timer, "2-wall", s)
            self._last_base = base
            self._last_wg   = wg
            self._last_ig   = ig

            # 3. Wall mesh + slots — main.py build_combined_mesh() mantığı
            s = _tic()
            ft       = self.face_thickness
            h        = self.height_mm
            has_face = self.face_mode in (1, 2) and ft > 0
            mod1     = self.face_mode == 1 and has_face
            mod2     = self.face_mode == 2 and has_face and ig is not None and not ig.is_empty

            av, af, off = [], [], 0
            def ap(v, f):
                nonlocal off
                if not v or not f: return
                av.extend(v)
                af.extend([(a+off, b+off, c+off) for a, b, c in f])
                off += len(v)

            # Duvar z aralığı: Mod1'de ft..h, diğerlerinde 0..h
            z_wall_start = ft if mod1 else 0.0
            cap_bot = not mod1  # Mod1'de alt yüz yok (face_stl kapatıyor)

            if self.wall_type == 0:
                wall_mf = self._geom_to_mf(wg, z_wall_start, h)
            else:
                wall_mf = self._build_wall_mf(z_wall_start, h)
            if wall_mf is not None:
                _mesh = wall_mf.to_mesh()
                v = [tuple(p) for p in np.array(_mesh.vert_properties, dtype=np.float32)]
                f = [tuple(t) for t in np.array(_mesh.tri_verts, dtype=np.int32)]
            else:
                v, f = [], []
            ap(v, f)

            # Mod1: ig kapağı eklenmez — face_stl (base 0..ft) zaten z=ft'yi kapatıyor

            if ig is not None and not ig.is_empty:
                real_ft = ft if has_face else 0.0
                if self.bot_tab and self.bot_proj > 0.01:
                    z0, z1 = self._tab_z_range('bot', ft=real_ft)
                    ap(*self.tab_mesh(self._ig_at_z((z0+z1)/2.0), self.bot_proj, z0, z1, circular=True))
                if self.top_tab and self.top_proj > 0.01:
                    z0, z1 = self._tab_z_range('top', ft=real_ft)
                    ap(*self.tab_mesh(self._ig_at_z((z0+z1)/2.0), self.top_proj, z0, z1,
                                     circular=True, profile=_UST_TIRNAK_PROFILE))

            if not av or not af:
                raise RuntimeError("Mesh üretimi geometri üretemedi.")

            _toc(self.timer, "WALL+SLOTS", s)
            self._last_wall_v = np.asarray(av, dtype=np.float32)
            self._last_wall_f = np.asarray(af, dtype=np.int32)
            self.last_v = av; self.last_f = af

            # 4. Face mesh
            s = _tic()
            fv, ff = self.build_face_mesh()
            _toc(self.timer, "4-face", s)
            self.last_face_v = fv; self.last_face_f = ff
            self.has_model = True

            return {
                "wall_faces": len(af),
                "face_faces": len(ff) if ff is not None else 0,
            }
        finally:
            self.arc_sm  = old_arc
            self.simplify = old_simp

    # ─────────────────────────────────────────────────────────────────────────
    # Geometri: harf + duvar
    # ─────────────────────────────────────────────────────────────────────────
    def build_letter(self):
        """Shapely polygon from paths. Aynısı LeftPanel.build_letter()."""
        src = self.base_paths or self.paths
        rings, lines = [], []
        for p in src:
            pts = clean_points(p["points"], 0.0001)
            if len(pts) < 2: continue
            if p["closed"] and len(pts) >= 4:
                if dist(pts[0], pts[-1]) > 0.001:
                    pts.append(pts[0])
                if abs(signed_area(pts)) > 0.01:
                    rings.append(pts)
            else:
                try: lines.append(LineString(pts))
                except Exception: pass

        polys = []
        if rings:
            rp = []
            for r in rings:
                try:
                    pg = Polygon(r)
                    if not pg.is_valid: pg = pg.buffer(0)
                    if not pg.is_empty and pg.area > 0.01:
                        rp.append({"poly": pg, "area": abs(pg.area)})
                except Exception: pass
            # Sort descending by area: a ring's parent (the ring directly
            # enclosing it) always has a larger area and therefore a lower
            # index here, so parent[i] < i always — no cycles possible.
            rp.sort(key=lambda x: x["area"], reverse=True)
            n = len(rp)
            for r in rp:
                r["bounds"] = r["poly"].bounds
                r["prep"] = prep(r["poly"])
            parent = [-1] * n
            for i in range(n):
                try:
                    pt = rp[i]["poly"].representative_point()
                except Exception:
                    continue
                px, py = pt.x, pt.y
                best_j, best_area = -1, None
                for j in range(i):
                    bx0, by0, bx1, by1 = rp[j]["bounds"]
                    if px < bx0 or px > bx1 or py < by0 or py > by1:
                        continue
                    try:
                        if rp[j]["prep"].contains(pt):
                            if best_area is None or rp[j]["area"] < best_area:
                                best_area, best_j = rp[j]["area"], j
                    except Exception:
                        pass
                parent[i] = best_j
            depth = [0] * n
            for i in range(n):
                depth[i] = depth[parent[i]] + 1 if parent[i] != -1 else 0
            # Even-depth rings are filled outlines; their direct children
            # (depth+1) are holes. Children of those holes (depth+2) become
            # their own filled "island" polygons, recursively.
            for i, r in enumerate(rp):
                if depth[i] % 2 != 0:
                    continue
                holes = [list(rp[j]["poly"].exterior.coords)
                         for j in range(n) if parent[j] == i]
                try:
                    poly = Polygon(list(r["poly"].exterior.coords), holes)
                    if not poly.is_valid: poly = poly.buffer(0)
                    if not poly.is_empty: polys.append(poly)
                except Exception:
                    if not r["poly"].is_empty: polys.append(r["poly"])

        if not polys and lines:
            try: polys = list(polygonize(unary_union(lines)))
            except Exception: polys = []

        if not polys: return None
        geom = unary_union(polys)
        if make_valid: geom = make_valid(geom)
        geom = geom.buffer(0)
        if self.simplify > 0:
            geom = geom.simplify(self.simplify, preserve_topology=True)
        if self.round_c > 0:
            r = self.round_c; qs = int(self.arc_sm)
            geom = (geom.buffer(r, quad_segs=qs, join_style="round")
                        .buffer(-r, quad_segs=qs, join_style="round")
                        .buffer(0))
        # Mirror — desktop toggle_my gibi X-flip, buffer(0) ile winding normalize
        if self.mx:
            from shapely.affinity import scale as _sc
            geom = _sc(geom, xfact=-1, yfact=1, origin=(0, 0)).buffer(0)
        if self.my:
            from shapely.affinity import scale as _sc
            geom = _sc(geom, xfact=1, yfact=-1, origin=(0, 0)).buffer(0)
        return geom

    def build_wall(self, base):
        t  = self.wall_mm; qs = max(4, int(self.arc_sm))
        base  = _clean(base, 0.05)
        inner = base.buffer(-t, quad_segs=qs, join_style="round").buffer(0)
        inner = _clean(inner, max(0.02, t*t*0.05)) if not inner.is_empty else inner
        wall  = base if inner.is_empty else base.difference(inner)
        return _clean(wall, max(0.02, t*t*0.03)), inner

    # ─────────────────────────────────────────────────────────────────────────
    # Yüz mesh
    # ─────────────────────────────────────────────────────────────────────────
    def build_face_mesh(self):
        """Face plate mesh — LeftPanel.build_face_mesh() ile özdeş."""
        if self.face_mode == 0 or self._last_base is None or self.face_thickness <= 0:
            return None, None
        if self.face_mode in (1, 3):
            geom = self._last_base
        else:
            if self._last_ig is None or self._last_ig.is_empty:
                return None, None
            geom = self._last_ig

        fill      = self.face_fill
        solid_pos = self.face_fill_solid_pos
        SOLID_D   = 0.8
        ft        = self.face_thickness
        cell      = self.face_fill_cell
        wall      = self.face_fill_wall
        border    = self.face_fill_border

        has_front = solid_pos in (1, 3)
        has_back  = solid_pos in (2, 3)
        min_ft    = SOLID_D * (1 + (1 if has_front else 0) + (1 if has_back else 0) + 0.5)

        def _mf_to_arrays(mf):
            mesh = mf.to_mesh()
            av = np.array(mesh.vert_properties, dtype=np.float32)[:, :3]
            af = np.array(mesh.tri_verts, dtype=np.int32)
            return av, af

        if fill and (has_front or has_back) and ft > min_ft:
            z0_mid = SOLID_D if has_front else 0.0
            z1_mid = ft - SOLID_D if has_back else ft
            sections = []
            if has_front: sections.append((geom, 0.0,          SOLID_D, 0))
            sections.append(             (geom, z0_mid,        z1_mid,  fill))
            if has_back:  sections.append((geom, ft - SOLID_D, ft,      0))
            combined = None
            for g, z0, z1, sec_fill in sections:
                mf = self._face_fill_mf(g, z0, z1, sec_fill, cell, wall, border) if sec_fill \
                     else self._geom_to_mf(g, z0, z1)
                if mf and not mf.is_empty():
                    combined = mf if combined is None else combined + mf
            if combined is None or combined.is_empty():
                return None, None
            av, af = _mf_to_arrays(combined)
        else:
            mf = self._face_fill_mf(geom, 0.0, ft, fill, cell, wall, border) if fill \
                 else self._geom_to_mf(geom, 0.0, ft)
            if mf is None or mf.is_empty():
                return None, None
            av, af = _mf_to_arrays(mf)

        self.last_face_v = av; self.last_face_f = af
        return av, af

    # ─────────────────────────────────────────────────────────────────────────
    # Birleşik mesh (Mod1 / Mod2)
    # ─────────────────────────────────────────────────────────────────────────
    def build_combined_mesh(self):
        """Body + face tek mesh. LeftPanel.build_combined_mesh() ile özdeş."""
        if self._last_base is None or self._last_wg is None or self._last_ig is None:
            return None, None
        base = self._last_base; wg = self._last_wg; ig = self._last_ig
        h = self.height_mm; ft = self.face_thickness
        has_face = self.face_mode in (1, 2) and ft > 0
        mod2 = (self.face_mode == 2) and (ig is not None) and (not ig.is_empty) and ft > 0
        av, af, off = [], [], 0
        def ap(v, f):
            nonlocal off
            if not v or not f: return
            av.extend(v); af.extend([(a+off,b+off,c+off) for a,b,c in f]); off += len(v)

        if mod2:
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(0.0, h)
                if wall_mf is not None:
                    m = wall_mf.to_mesh()
                    v = [tuple(p) for p in np.array(m.vert_properties, dtype=np.float32)]
                    f = [tuple(t) for t in np.array(m.tri_verts, dtype=np.int32)]
                else: v, f = [], []
            else:
                v, f = MB.extrude_geom(wg, 0.0, h, cap_bottom=True, cap_top=True)
            ap(v, f)
            v, f = MB.extrude_geom(ig, 0.0, ft, cap_bottom=True, cap_top=False); ap(v, f)
            for poly in _as_polys(ig, 0.01): ap(*MB.cap(poly, ft, top=True))
        elif has_face:
            v, f = MB.extrude_geom(base, 0.0, ft, cap_bottom=True, cap_top=False); ap(v, f)
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(ft, h)
                if wall_mf is not None:
                    m = wall_mf.to_mesh()
                    v = [tuple(p) for p in np.array(m.vert_properties, dtype=np.float32)]
                    f = [tuple(t) for t in np.array(m.tri_verts, dtype=np.int32)]
                else: v, f = [], []
            else:
                v, f = MB.extrude_geom(wg, ft, h, cap_bottom=False, cap_top=True)
            ap(v, f)
            for poly in _as_polys(ig, 0.01): ap(*MB.cap(poly, ft, top=True))
        else:
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(0.0, h)
                if wall_mf is not None:
                    m = wall_mf.to_mesh()
                    v = [tuple(p) for p in np.array(m.vert_properties, dtype=np.float32)]
                    f = [tuple(t) for t in np.array(m.tri_verts, dtype=np.int32)]
                else: v, f = [], []
            else:
                v, f = MB.extrude_geom(wg, 0.0, h, cap_bottom=True, cap_top=True)
            ap(v, f)

        if ig is not None and not ig.is_empty:
            if self.bot_tab and self.bot_proj > 0.01:
                z0, z1 = self._tab_z_range('bot', ft=ft)
                ap(*self.tab_mesh(self._ig_at_z((z0+z1)/2.0), self.bot_proj, z0, z1, circular=True))
            if self.top_tab and self.top_proj > 0.01:
                z0, z1 = self._tab_z_range('top', ft=ft)
                ap(*self.tab_mesh(self._ig_at_z((z0+z1)/2.0), self.top_proj, z0, z1,
                                  circular=True, profile=_UST_TIRNAK_PROFILE))
        if not av or not af: return None, None
        return np.asarray(av, dtype=np.float32), np.asarray(af, dtype=np.int32)

    def build_mod1_combined_mesh(self):
        """Mod 1 manifold union (face + wall) — NM=0 garantili."""
        if self._last_base is None or self._last_wg is None: return None, None
        import manifold3d as _m3d
        ft = self.face_thickness; H = ft + self.height_mm
        try:
            face_mf = self._geom_to_mf(self._last_base, 0.0, ft)
            if face_mf is None or face_mf.is_empty(): return None, None
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(ft - 0.001, H)
            else:
                wall_mf = self._geom_to_mf(self._last_wg, ft - 0.001, H)
            if wall_mf is None or wall_mf.is_empty(): return None, None
            combined = face_mf + wall_mf
            if combined.is_empty(): return None, None
            out = combined.to_mesh()
            v = np.array(out.vert_properties, dtype=np.float32)[:, :3]
            f = np.array(out.tri_verts, dtype=np.int32)
            return v, f
        except Exception:
            return None, None

    def build_trimmed_wall_mesh(self, z0, z1):
        """Wall mesh — manifold3d garantili, MB.extrude_geom yok."""
        if self._last_wg is None: return None, None
        if self.wall_type != 0:
            wall_mf = self._build_wall_mf(z0, z1)
        else:
            wall_mf = self._geom_to_mf(self._last_wg, z0, z1)
        if wall_mf is None or wall_mf.is_empty(): return None, None
        m = wall_mf.to_mesh()
        return (np.array(m.vert_properties, dtype=np.float32),
                np.array(m.tri_verts, dtype=np.int32))

    # ─────────────────────────────────────────────────────────────────────────
    # Slot (tırnak) sistemi
    # ─────────────────────────────────────────────────────────────────────────
    def tab_mesh(self, ig, proj, z0, z1, circular=False, profile=None):
        """LeftPanel.tab_mesh() ile özdeş."""
        proj = max(0.0, float(proj))
        if proj <= 0.01 or ig is None or ig.is_empty: return [], []
        qs = max(4, int(self.arc_sm)); _gap = 0.001
        height = float(z1) - float(z0)
        if circular:
            _prof = profile if profile is not None else _ALT_TIRNAK_PROFILE
            _ph = [p[0] for p in _prof]; _pd = [p[1] for p in _prof]
            _reverse = (profile is not None)
            N = max(10, min(24, int(round(height / 0.3))))
            dz = height / N
            av, af, off = [], [], 0
            def _ap(v, f):
                nonlocal off
                if not v or not f: return
                av.extend(v); af.extend([(a+off,b+off,c+off) for a,b,c in f]); off += len(v)
            for i in range(N):
                t = (i+0.5)/N
                w = proj * float(np.interp((1.0-t) if _reverse else t, _ph, _pd))
                if w < 0.01: continue
                z_s = z0 + i*dz
                try:
                    outer = ig.buffer(-_gap, quad_segs=qs, join_style='round').buffer(0)
                    inner = ig.buffer(-(_gap+w), quad_segs=qs, join_style='round').buffer(0)
                    if outer is None or outer.is_empty: continue
                    ring = outer if (inner is None or inner.is_empty) else outer.difference(inner)
                    ring = _clean(ring, 0.005)
                    if ring is None or ring.is_empty: continue
                    _ap(*MB.extrude_geom(ring, z_s, z_s+dz))
                except Exception: pass
            return av, af
        # Linear ramp
        ramp_h = min(proj, height); N = max(4, min(10, int(round(ramp_h/0.4))))
        step_w = proj/N; _sep = 0.004
        av, af, off = [], [], 0
        def ap2(v, f):
            nonlocal off
            if not v or not f: return
            av.extend(v); af.extend([(a+off,b+off,c+off) for a,b,c in f]); off += len(v)
        for k in range(N):
            za = z0+ramp_h*k/N; zb = z0+ramp_h*(k+1)/N
            oi = _gap+k*(step_w+_sep); ii = oi+step_w
            try:
                outer = ig.buffer(-oi, quad_segs=qs, join_style="round").buffer(0)
                if outer is None or outer.is_empty: continue
                inner2 = ig.buffer(-ii, quad_segs=qs, join_style="round").buffer(0)
                ring = outer if (inner2 is None or inner2.is_empty) else outer.difference(inner2)
                ring = _clean(ring, 0.005)
                if ring is None or ring.is_empty: continue
                ap2(*MB.extrude_geom(ring, za, zb))
            except Exception: pass
        return av, af

    def _tab_z_range(self, which, ft=0.0):
        h = self.height_mm
        if which == 'top':
            proj  = self.top_proj
            z1    = min(h*0.99, h + self.top_tab_z)
            slot_h = max(proj*_UST_TIRNAK_ASPECT + 1.0, proj*0.8)
            z0    = max(ft+0.01, z1 - slot_h)
        else:
            proj  = self.bot_proj
            z0    = max(ft+0.01, self.bot_tab_z)
            slot_h = max(proj*_ALT_TIRNAK_ASPECT + 1.0, proj*1.2)
            z1    = min(z0+slot_h, h*0.98)
        return z0, z1

    # ─────────────────────────────────────────────────────────────────────────
    # Duvar yardımcıları
    # ─────────────────────────────────────────────────────────────────────────
    def _wall_offset_at_z(self, z):
        if self.wall_type not in (1, 2): return 0.0
        profile = _ACILI_PROFILE if self.wall_type == 1 else _KAVISLI_PROFILE
        t = max(0.0, min(1.0, z / max(self.height_mm, 1.0)))
        return self._interp_profile(t, profile) * self.wall_taper

    def _ig_at_z(self, z):
        if self._last_ig is None: return self._last_ig
        o = self._wall_offset_at_z(z)
        if o < 0.001: return self._last_ig
        qs = max(4, int(self.arc_sm))
        adj = self._last_ig.buffer(o, quad_segs=qs, join_style='mitre').buffer(0)
        return adj if (adj is not None and not adj.is_empty) else self._last_ig

    def _base_at_z(self, z):
        if self._last_base is None: return self._last_base
        o = self._wall_offset_at_z(z)
        if o < 0.001: return self._last_base
        qs = max(4, int(self.arc_sm))
        adj = self._last_base.buffer(o, quad_segs=qs, join_style='mitre').buffer(0)
        return adj if (adj is not None and not adj.is_empty) else self._last_base

    @staticmethod
    def _interp_profile(t, profile):
        if t <= profile[0][0]:  return profile[0][1]
        if t >= profile[-1][0]: return profile[-1][1]
        for i in range(len(profile)-1):
            t0, s0 = profile[i]; t1, s1 = profile[i+1]
            if t0 <= t <= t1:
                return s0 + (t-t0)/(t1-t0)*(s1-s0)
        return 0.0

    def _face_fill_mf(self, geom, z0, z1, fill, cell, wall, border):
        """Honeycomb/grid/lines void'ları letter outline ile tek CrossSection'a koy.
        Shapely difference kullanmaz → touching edge yok → NM=0 garantili."""
        import manifold3d as m3d
        import math

        if fill == 0 or geom is None or geom.is_empty:
            return self._geom_to_mf(geom, z0, z1)

        letter_polys = _as_polys(geom, 0.01)
        if not letter_polys:
            return None

        all_contours = []
        for poly in letter_polys:
            ext = list(poly.exterior.coords)
            if ext[0] == ext[-1]: ext = ext[:-1]
            all_contours.append([[float(x), float(y)] for x, y in ext])
            for interior in poly.interiors:
                ic = list(interior.coords)
                if ic[0] == ic[-1]: ic = ic[:-1]
                all_contours.append([[float(x), float(y)] for x, y in ic])

        try:
            inner = geom if border <= 0 else geom.buffer(-border, join_style="round", quad_segs=8)
            if inner is None or inner.is_empty:
                return self._geom_to_mf(geom, z0, z1)
        except Exception:
            return self._geom_to_mf(geom, z0, z1)

        minx, miny, maxx, maxy = inner.bounds
        pad = cell * 2.0
        mx0, my0 = minx - pad, miny - pad
        mx1, my1 = maxx + pad, maxy + pad
        _EPS = 0.004

        voids = []
        try:
            from shapely.geometry import Polygon as _P
            if fill == 1:   # Honeycomb
                r = cell * 0.5
                col_step = r * math.sqrt(3)
                row_step = r * 1.5
                r_void = min((col_step - wall) / math.sqrt(3),
                             (row_step - wall) / 2.0) - _EPS
                if r_void > 0.2:
                    row_idx = 0
                    y = my0
                    while y <= my1 + row_step:
                        x_off = col_step * 0.5 if (row_idx % 2 == 1) else 0.0
                        x = mx0 + x_off
                        while x <= mx1 + col_step:
                            pts = [(x + r_void * math.cos(math.radians(90 + 60 * k)),
                                    y + r_void * math.sin(math.radians(90 + 60 * k)))
                                   for k in range(6)]
                            clipped = _P(pts).intersection(inner)
                            if not clipped.is_empty and clipped.area > 0.01:
                                voids.append(clipped)
                            x += col_step
                        y += row_step
                        row_idx += 1
            elif fill == 2:  # Grid
                void_sz = max(0.1, cell - wall - _EPS * 2)
                y = my0 + wall * 0.5
                while y <= my1:
                    x = mx0 + wall * 0.5
                    while x <= mx1:
                        clipped = _P([(x,y),(x+void_sz,y),(x+void_sz,y+void_sz),(x,y+void_sz)]).intersection(inner)
                        if not clipped.is_empty and clipped.area > 0.01:
                            voids.append(clipped)
                        x += cell
                    y += cell
            elif fill == 3:  # Lines
                void_w = max(0.1, cell - wall - _EPS * 2)
                x = mx0 + wall * 0.5
                while x <= mx1:
                    clipped = _P([(x,my0),(x+void_w,my0),(x+void_w,my1),(x,my1)]).intersection(inner)
                    if not clipped.is_empty and clipped.area > 0.01:
                        voids.append(clipped)
                    x += cell
        except Exception:
            return self._geom_to_mf(geom, z0, z1)

        for v in voids:
            for vpoly in _as_polys(v, 0.005):
                ext = list(vpoly.exterior.coords)
                if ext[0] == ext[-1]: ext = ext[:-1]
                if len(ext) < 3: continue
                all_contours.append([[float(x), float(y)] for x, y in reversed(ext)])

        cs = m3d.CrossSection(all_contours, m3d.FillRule.EvenOdd)
        if cs.area() < 1e-6:
            return self._geom_to_mf(geom, z0, z1)
        mf = m3d.Manifold.extrude(cs, height=float(z1 - z0))
        if float(z0) != 0.0:
            mf = mf.translate([0.0, 0.0, float(z0)])
        return mf if not mf.is_empty() else None

    def _geom_to_mf(self, geom, z0, z1):
        """Tüm contour'ları tek CrossSection olarak extrude et — union yok, nm=0 garantili."""
        import manifold3d as m3d
        polys = _as_polys(geom, 0.01)
        if not polys:
            return None
        all_contours = []
        for poly in polys:
            ext = list(poly.exterior.coords)
            if ext[0] == ext[-1]: ext = ext[:-1]
            all_contours.append([[float(x), float(y)] for x, y in ext])
            for interior in poly.interiors:
                ic = list(interior.coords)
                if ic[0] == ic[-1]: ic = ic[:-1]
                all_contours.append([[float(x), float(y)] for x, y in ic])
        if not all_contours:
            return None
        cs = m3d.CrossSection(all_contours, m3d.FillRule.EvenOdd)
        if cs.area() < 1e-6:
            return None
        mf = m3d.Manifold.extrude(cs, height=float(z1 - z0))
        if float(z0) != 0.0:
            mf = mf.translate([0.0, 0.0, float(z0)])
        return mf if not mf.is_empty() else None

    def _profile_wall_mf(self, z0, z1, profile, max_lean):
        import manifold3d as m3d
        if self._last_base is None or self._last_ig is None: return None
        qs = max(4, int(self.arc_sm))
        h_span = float(z1-z0); n = max(24, int(round(h_span/0.6)))
        dz = h_span/n; slices = []
        for i in range(n):
            t = (i+0.5)/n; o = self._interp_profile(t, profile)*max_lean
            outer = self._last_base.buffer(o, quad_segs=qs, join_style='mitre').buffer(0)
            inner = self._last_ig.buffer(o, quad_segs=qs, join_style='mitre').buffer(0)
            if outer is None or outer.is_empty: continue
            ring = outer.difference(inner) if (inner is not None and not inner.is_empty) else outer
            if ring is None or ring.is_empty: continue
            for poly in _as_polys(ring, 0.01):
                ext = list(poly.exterior.coords)
                if ext[0] == ext[-1]: ext = ext[:-1]
                contours = [[[float(x), float(y)] for x, y in ext]]
                for interior in poly.interiors:
                    ic = list(interior.coords)
                    if ic[0] == ic[-1]: ic = ic[:-1]
                    contours.append([[float(x), float(y)] for x, y in ic])
                cs = m3d.CrossSection(contours, m3d.FillRule.EvenOdd)
                if cs.area() < 1e-6: continue
                mf = m3d.Manifold.extrude(cs, float(dz))
                if mf.is_empty(): continue
                mf = mf.translate([0.0, 0.0, float(z0+i*dz)])
                slices.append(mf)
        if not slices: return None
        while len(slices) > 1:
            nxt = []
            for j in range(0, len(slices), 2):
                nxt.append(slices[j]+slices[j+1] if j+1 < len(slices) else slices[j])
            slices = nxt
        return slices[0]

    def _build_wall_mf(self, z0, z1):
        if self.wall_type == 1: return self._profile_wall_mf(z0, z1, _ACILI_PROFILE, self.wall_taper)
        if self.wall_type == 2: return self._profile_wall_mf(z0, z1, _KAVISLI_PROFILE, self.wall_taper)
        return self._geom_to_mf(self._last_wg, z0, z1)

    # ─────────────────────────────────────────────────────────────────────────
    # STL byte export helpers
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _arrays_to_stl_bytes(v, f) -> bytes:
        cv, cf, _ = cleanup(np.asarray(v, dtype=np.float32), np.asarray(f, dtype=np.int32))
        if len(cf) == 0:
            raise RuntimeError("Cleanup sonrası üçgen kalmadı.")
        tri = cv[cf]
        n   = np.cross(tri[:,1]-tri[:,0], tri[:,2]-tri[:,0])
        ln  = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n   = (n / ln[:,None]).astype(np.float32)
        dt  = np.dtype([("normal","<f4",(3,)),("vertices","<f4",(3,3)),("attr","<u2")])
        data = np.zeros(len(cf), dtype=dt)
        data["normal"] = n; data["vertices"] = tri.astype(np.float32)
        buf = io.BytesIO()
        hdr = b"Harfex"[:80]; buf.write(hdr + b" "*(80-len(hdr)))
        buf.write(np.array([len(cf)], dtype=np.uint32).tobytes())
        buf.write(data.tobytes())
        return buf.getvalue()

    def export_stl_bytes(self) -> bytes:
        """Duvar STL → bytes."""
        if self.last_v is None or self.last_f is None or len(self.last_f) == 0:
            raise RuntimeError("Önce Generate 3D çalıştırın.")
        return self._arrays_to_stl_bytes(self.last_v, self.last_f)

    def export_face_stl_bytes(self) -> bytes:
        """Yüz STL → bytes."""
        if self.last_face_v is None or self.last_face_f is None or len(self.last_face_f) == 0:
            raise RuntimeError("Yüz mesh boş — face_mode ayarlayıp Generate yapın.")
        return self._arrays_to_stl_bytes(self.last_face_v, self.last_face_f)

    def export_combined_stl_bytes(self) -> bytes:
        """Body + face birleşik STL → bytes."""
        v, f = self.build_combined_mesh()
        if v is None or f is None:
            raise RuntimeError("Birleşik mesh oluşturulamadı.")
        return self._arrays_to_stl_bytes(v, f)

    def export_cover_stl_bytes(self) -> bytes:
        """Arka kapak STL → bytes. main.py export_back_cover_stl ile özdeş."""
        import struct
        print(f"[COVER] _last_base={self._last_base is not None}, _last_ig={self._last_ig is not None}")
        print(f"[COVER] cover_ct={self.cover_ct}, cover_wh={self.cover_wh}, cover_clearance={self.cover_clearance}, cover_wt={self.cover_wt}")
        if self._last_base is None or self._last_ig is None:
            raise RuntimeError("Önce Generate 3D çalıştırın.")
        ct = float(self.cover_ct);  wh = float(self.cover_wh)
        cl = float(self.cover_clearance); wt = float(self.cover_wt)

        base_geom  = self._base_at_z(self.height_mm)
        ig_back    = self._ig_at_z(self.height_mm)

        base_plate = self._geom_to_mf(base_geom, 0.0, ct)
        if base_plate is None:
            raise RuntimeError("Taban plakası oluşturulamadı.")

        tab_outer = ig_back.buffer(-cl, join_style="mitre").buffer(0)
        tab_inner = tab_outer.buffer(-wt, join_style="mitre").buffer(0)
        tab_geom  = tab_outer.difference(tab_inner) if not tab_inner.is_empty else tab_outer
        if tab_geom is None or tab_geom.is_empty:
            raise RuntimeError("Tab geometrisi oluşturulamadı.")
        inner_wall = self._geom_to_mf(tab_geom, ct, ct + wh)
        if inner_wall is None:
            raise RuntimeError("İç tab manifold oluşturulamadı.")

        cover_mf = base_plate + inner_wall
        mesh = cover_mf.to_mesh()
        va = np.array(mesh.vert_properties, dtype=np.float32)
        fa = np.array(mesh.tri_verts, dtype=np.int32)

        # Y-flip — doğru montaj yönü (main.py ile aynı)
        minx, miny, maxx, maxy = base_geom.bounds
        center_y = (miny + maxy) / 2.0
        va[:, 1] = 2.0 * center_y - va[:, 1]
        fa = fa[:, [0, 2, 1]]

        tri = va[fa]
        n   = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln  = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n   = (n / ln[:, None]).astype(np.float32)
        dt  = np.dtype([("normal","<f4",(3,)),("vertices","<f4",(3,3)),("attr","<u2")])
        data = np.zeros(len(fa), dtype=dt)
        data["normal"] = n; data["vertices"] = tri.astype(np.float32)
        buf = io.BytesIO()
        hdr = b"Harfex Cover"; buf.write(hdr + b" " * (80 - len(hdr)))
        buf.write(np.array([len(fa)], dtype=np.uint32).tobytes())
        buf.write(data.tobytes())
        return buf.getvalue()

    # ─────────────────────────────────────────────────────────────────────────
    # 3MF export
    # ─────────────────────────────────────────────────────────────────────────
    def _split_wall_mesh(self, z0, z1):
        """Duvar mesh'ini z0..z1 aralığına kır → (v, f) veya (None, None)."""
        if self._last_wg is None: return None, None
        if z1 <= z0 + 0.01: return None, None
        if self.wall_type != 0:
            mf = self._build_wall_mf(z0, z1)
        else:
            mf = self._geom_to_mf(self._last_wg, z0, z1)
        if mf is None or mf.is_empty(): return None, None
        m = mf.to_mesh()
        cv = np.array(m.vert_properties, dtype=np.float32)
        cf = np.array(m.tri_verts, dtype=np.int32)
        cv2, cf2, _ = cleanup(cv, cf)
        return cv2, cf2

    def export_3mf_bytes(self, include_face=True, include_cover=False,
                         band_pos=None, band_h=None) -> bytes:
        """Body (+ opsiyonel yüz + kapak + kuşak) → 3MF bytes."""
        import zipfile

        objects = []

        # ── Duvar bölümleri ──────────────────────────────────────────────
        ft = self.face_thickness
        has_face = self.face_mode in (1, 2) and ft > 0
        mod1 = self.face_mode == 1 and has_face
        z_start = ft if mod1 else 0.0
        z_end   = float(self.height_mm)

        use_band = (band_pos is not None and band_h is not None
                    and band_h > 0.1)
        if use_band:
            bp  = float(band_pos)
            bh  = float(band_h)
            bp  = max(z_start + 0.5, min(bp, z_end - bh - 0.5))
            bh  = min(bh, z_end - bp - 0.1)

            # Alt duvar
            v0, f0 = self._split_wall_mesh(z_start, bp)
            if v0 is not None and len(f0):
                objects.append({"label": "Wall_Bottom", "v": v0, "f": f0,
                                "color": (0.35, 0.88, 0.85, 1.0)})
            # Kuşak
            vb, fb = self._split_wall_mesh(bp, bp + bh)
            if vb is not None and len(fb):
                objects.append({"label": "Wall_Band", "v": vb, "f": fb,
                                "color": (0.20, 0.35, 0.90, 1.0)})
            # Üst duvar
            v1, f1 = self._split_wall_mesh(bp + bh, z_end)
            if v1 is not None and len(f1):
                objects.append({"label": "Wall_Top", "v": v1, "f": f1,
                                "color": (0.35, 0.88, 0.85, 1.0)})
        else:
            v = self.last_v; f = self.last_f
            if v is None or f is None or len(f) == 0:
                raise RuntimeError("Önce Generate 3D çalıştırın.")
            cv, cf, _ = cleanup(np.asarray(v, dtype=np.float32), np.asarray(f, dtype=np.int32))
            objects.append({"label": "Body", "v": cv, "f": cf,
                            "color": (0.35, 0.88, 0.85, 1.0)})
        # Face
        if include_face and self.last_face_v is not None and self.last_face_f is not None \
                and len(self.last_face_f) > 0:
            cv2, cf2, _ = cleanup(
                np.asarray(self.last_face_v, dtype=np.float32),
                np.asarray(self.last_face_f, dtype=np.int32))
            if len(cf2) > 0:
                objects.append({"label": "Face", "v": cv2, "f": cf2,
                                "color": (0.95, 0.45, 0.10, 1.0)})
        # Cover — STL bytes'tan oku, body'nin yanına Y offset uygula (web viewer ile aynı düzen)
        if include_cover:
            try:
                cov_bytes = self.export_cover_stl_bytes()
                import struct as _struct, io as _sio
                _buf = _sio.BytesIO(cov_bytes)
                _buf.read(80)
                _n = _struct.unpack('<I', _buf.read(4))[0]
                cov_v, cov_f = [], []
                for _i in range(_n):
                    _buf.read(12)
                    pts = [_struct.unpack('<3f', _buf.read(12)) for _ in range(3)]
                    idx = len(cov_v)
                    cov_v.extend(pts)
                    cov_f.append([idx, idx+1, idx+2])
                    _buf.read(2)
                # Vertex'leri birleştir (STL parse'ı shared vertex üretmez → non-manifold)
                _cv_raw = np.array(cov_v, dtype=np.float32)
                _cf_raw = np.array(cov_f, dtype=np.int32)
                _cv, _cf, _ = cleanup(_cv_raw, _cf_raw)
                # Body'nin Y sınırını al ve cover'ı body'nin yanına taşı (web viewer mantığı)
                if self._last_base is not None:
                    _bminx, _bminy, _bmaxx, _bmaxy = self._last_base.bounds
                    _cov_miny = float(_cv[:, 1].min())
                    _cv[:, 1] += _bmaxy + 5.0 - _cov_miny
                objects.append({"label": "Cover", "v": _cv, "f": _cf,
                                "color": (0.65, 0.65, 0.65, 1.0)})
            except Exception as _e:
                print(f"[3MF] Cover embed hatası: {_e}")

        return self._build_3mf_bytes(objects)

    @staticmethod
    def _build_3mf_bytes(objects: list) -> bytes:
        """objects: [{"label":str, "v":ndarray, "f":ndarray, "color":(r,g,b,a)}, ...]"""
        import zipfile

        def _color_hex(rgba):
            r,g,b,a = [max(0,min(255,int(c*255))) for c in rgba]
            return f"#{r:02X}{g:02X}{b:02X}{a:02X}"

        model_parts = []; resources_xml = []; build_items = []
        obj_id = 1
        for o in objects:
            verts = np.asarray(o["v"], dtype=np.float32)
            faces = np.asarray(o["f"], dtype=np.int32)
            verts_xml = "\n".join(
                f'          <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>'
                for v in verts)
            tris_xml = "\n".join(
                f'          <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>'
                for t in faces)
            color = _color_hex(o.get("color", (0.5,0.5,0.5,1.0)))
            resources_xml.append(f"""    <object id="{obj_id}" type="model" name="{o['label']}">
      <mesh>
        <vertices>
{verts_xml}
        </vertices>
        <triangles>
{tris_xml}
        </triangles>
      </mesh>
    </object>""")
            build_items.append(
                f'    <item objectid="{obj_id}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            obj_id += 1

        model_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
{chr(10).join(resources_xml)}
  </resources>
  <build>
{chr(10).join(build_items)}
  </build>
</model>"""

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                '</Types>')
            zf.writestr("_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" '
                'Target="/3D/model.model" Id="r1"/>'
                '</Relationships>')
            zf.writestr("3D/model.model", model_xml)
        return buf.getvalue()

    # ─────────────────────────────────────────────────────────────────────────
    # DXF / pleksiglas / foam export
    # ─────────────────────────────────────────────────────────────────────────
    def export_plexiglas_dxf_bytes(self, mode="kanal", offset=None,
                                   corner_radius=0.0) -> bytes:
        """Pleksiglas kesim DXF → bytes."""
        if not ezdxf:
            raise RuntimeError("ezdxf kurulu değil: pip install ezdxf")
        if offset is None: offset = self.plexiglas_offset
        qs = max(8, int(self.arc_sm)*2)
        if mode == "kapak":
            if self._last_base is None or self._last_base.is_empty:
                raise RuntimeError("Önce Generate 3D çalıştırın.")
            base_geom = self._base_at_z(self.height_mm) if self.wall_type != 0 else self._last_base
            contour = base_geom.buffer(offset, quad_segs=qs, join_style="mitre").buffer(0)
        else:
            if self._last_ig is None or self._last_ig.is_empty:
                raise RuntimeError("Önce Generate 3D çalıştırın.")
            contour = self._last_ig.buffer(offset, quad_segs=qs, join_style="round").buffer(0)
        if contour is None or contour.is_empty:
            raise RuntimeError("Offset çok büyük — kontur çöktü.")
        if corner_radius > 0.001:
            contour = contour.buffer(-corner_radius, quad_segs=qs, join_style='round').buffer(0)
            if contour is None or contour.is_empty:
                raise RuntimeError("Köşe yarıçapı çok büyük.")
            contour = contour.buffer(corner_radius, quad_segs=qs, join_style='round').buffer(0)

        doc = ezdxf.new("R2010"); msp = doc.modelspace()
        for poly in _as_polys(contour, 0.01):
            pts = [(float(x), float(y), 0.0) for x, y in poly.exterior.coords]
            msp.add_lwpolyline(pts, close=True)
            for hole in poly.interiors:
                msp.add_lwpolyline([(float(x),float(y),0.0) for x,y in hole.coords], close=True)
        tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
        tmp.close()
        try:
            doc.saveas(tmp.name)
            with open(tmp.name, 'rb') as f:
                return f.read()
        finally:
            os.unlink(tmp.name)

    def export_foam_dxf_bytes(self, offset=None, corner_radius=0.0) -> bytes:
        """Foam kesim DXF → bytes."""
        if not ezdxf:
            raise RuntimeError("ezdxf kurulu değil: pip install ezdxf")
        if offset is None: offset = self.foam_offset
        if self._last_base is None or self._last_base.is_empty:
            raise RuntimeError("Önce Generate 3D çalıştırın.")
        qs = max(8, int(self.arc_sm)*2)
        foam_geom = self._base_at_z(self.height_mm) if self.wall_type != 0 else self._last_base
        if abs(offset) > 0.001:
            foam_geom = foam_geom.buffer(offset, quad_segs=qs, join_style='mitre').buffer(0)
            if foam_geom is None or foam_geom.is_empty:
                raise RuntimeError("Offset çok büyük — kontur çöktü.")
        if corner_radius > 0.001:
            foam_geom = foam_geom.buffer(-corner_radius, quad_segs=qs, join_style='round').buffer(0)
            if foam_geom is None or foam_geom.is_empty:
                raise RuntimeError("Köşe yarıçapı çok büyük.")
            foam_geom = foam_geom.buffer(corner_radius, quad_segs=qs, join_style='round').buffer(0)
        doc = ezdxf.new("R2010"); msp = doc.modelspace()
        for poly in _as_polys(foam_geom, 0.01):
            pts = [(float(x), float(y), 0.0) for x, y in poly.exterior.coords]
            msp.add_lwpolyline(pts, close=True)
            for hole in poly.interiors:
                msp.add_lwpolyline([(float(x),float(y),0.0) for x,y in hole.coords], close=True)
        tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
        tmp.close()
        try:
            doc.saveas(tmp.name)
            with open(tmp.name, 'rb') as f:
                return f.read()
        finally:
            os.unlink(tmp.name)

    # ─────────────────────────────────────────────────────────────────────────
    # Kontur (2D önizleme için)
    # ─────────────────────────────────────────────────────────────────────────
    def get_contour_json(self) -> dict:
        """Paths → 2D line segments JSON (Three.js önizleme için)."""
        lines = []
        bbox = {"min_x": None, "max_x": None, "min_y": None, "max_y": None}
        for path in self.paths:
            pts = path.get("points", [])
            if len(pts) < 2: continue
            closed = path.get("closed", False)
            segs = list(range(len(pts)))
            if closed: segs.append(0)
            for i in range(len(segs)-1):
                x1, y1 = pts[segs[i]]; x2, y2 = pts[segs[i+1]]
                lines.append([x1, y1, x2, y2])
                for xv, yv in [(x1,y1),(x2,y2)]:
                    if bbox["min_x"] is None or xv < bbox["min_x"]: bbox["min_x"] = xv
                    if bbox["max_x"] is None or xv > bbox["max_x"]: bbox["max_x"] = xv
                    if bbox["min_y"] is None or yv < bbox["min_y"]: bbox["min_y"] = yv
                    if bbox["max_y"] is None or yv > bbox["max_y"]: bbox["max_y"] = yv
        return {"lines": lines, "bbox": bbox, "path_count": len(self.paths)}

    def get_processed_contour_json(self) -> dict:
        """arc/round/simplify uygulanmış shapely geometrisi → 2D line segments JSON."""
        from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
        geom = self.build_letter()
        if geom is None or geom.is_empty:
            return {"lines": [], "bbox": {}, "path_count": 0}

        lines = []
        bbox = {"min_x": None, "max_x": None, "min_y": None, "max_y": None}

        def _update_bbox(x, y):
            if bbox["min_x"] is None or x < bbox["min_x"]: bbox["min_x"] = x
            if bbox["max_x"] is None or x > bbox["max_x"]: bbox["max_x"] = x
            if bbox["min_y"] is None or y < bbox["min_y"]: bbox["min_y"] = y
            if bbox["max_y"] is None or y > bbox["max_y"]: bbox["max_y"] = y

        def _add_ring(coords):
            c = list(coords)
            for i in range(len(c) - 1):
                x1, y1 = c[i]; x2, y2 = c[i+1]
                lines.append([x1, y1, x2, y2])
                _update_bbox(x1, y1); _update_bbox(x2, y2)

        def _add_poly(poly):
            if poly.is_empty: return
            _add_ring(poly.exterior.coords)
            for hole in poly.interiors:
                _add_ring(hole.coords)

        if isinstance(geom, Polygon):
            _add_poly(geom)
        elif isinstance(geom, (MultiPolygon, GeometryCollection)):
            for g in geom.geoms:
                if isinstance(g, Polygon): _add_poly(g)

        polys = [g for g in (geom.geoms if hasattr(geom, 'geoms') else [geom]) if isinstance(g, Polygon)]
        path_count = len(polys) if polys else 1
        return {"lines": lines, "bbox": bbox, "path_count": path_count}
