"""
processor.py — HarfexProcessor
================================
Viewport'tan çıkarılan saf hesaplama katmanı.
GUI bağımlılığı yok — FastAPI ve masaüstü paylaşır.

Kullanım:
    proc = HarfexProcessor()
    proc.load_dxf("logo.dxf")
    proc.set_params(wall_mm=5, height_mm=45, wall_type=0, face_mode=1, face_thickness=3)
    proc.build()
    stl_bytes   = proc.export_stl_bytes()
    face_bytes  = proc.export_face_stl_bytes()
    cover_bytes = proc.export_cover_stl_bytes()
"""

import sys
import math
import os
import io
import time
from pathlib import Path

import numpy as np

# harfex_engine'i bul (api/ klasörü alpha182'nin kardeşi)
_ROOT = Path(__file__).resolve().parent.parent / "alpha182"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from harfex_engine import (
    MB, DXFImporter, cleanup,
    _face_fill_pattern, _as_polys, _clean,
    dist, clean_points, signed_area,
    StageTimer, _tic, _toc,
    GEOMETRY_EXECUTOR,
    Polygon, MultiPolygon, LineString,
    unary_union, polygonize, make_valid,
    _ACILI_PROFILE, _KAVISLI_PROFILE,
    _ALT_TIRNAK_PROFILE, _ALT_TIRNAK_ASPECT,
    _UST_TIRNAK_PROFILE, _UST_TIRNAK_ASPECT,
)


class HarfexProcessor:
    """
    Viewport'ın GUI-free karşılığı.
    Tüm parametreler ModelSlot ile özdeş varsayılan değerlere sahip.
    """

    def __init__(self):
        # ── Paths ────────────────────────────────────────────────────────────
        self.paths      = []
        self.base_paths = []

        # ── Model parametreleri ───────────────────────────────────────────────
        self.wall_mm          = 5.0
        self.height_mm        = 45.0
        self.arc_sm           = 16.0   # arc smoothness (segments per 90°)
        self.round_c          = 1.0    # corner rounding radius
        self.simplify         = 0.02   # Shapely simplify tolerance

        self.wall_type        = 0      # 0=düz  1=açılı  2=kavisli
        self.wall_taper       = 13.81  # maks açılma mesafesi (mm)

        self.face_mode        = 1      # 0=yok  1=iç yüz  2=dış yüz  3=sadece yüz
        self.face_thickness   = 3.0
        self.face_fill        = 0      # 0=solid 1=honeycomb 2=grid 3=lines
        self.face_fill_cell   = 8.0
        self.face_fill_wall   = 1.6
        self.face_fill_border = 1.5
        self.face_fill_solid_pos = 0   # 0=off 1=front 2=back 3=both

        self.plexiglas_offset = 0.0
        self.flip_z           = False
        self.mx               = False
        self.my               = False

        # Slot (tırnak) sistemi
        self.top_tab          = False
        self.bot_tab          = False
        self.top_proj         = 4.0   # üst tırnak çıkıntısı (mm)
        self.bot_proj         = 4.0   # alt tırnak çıkıntısı (mm)
        self.top_tab_z        = -2.0  # üst kenardan offset (negatif = üstten aşağı)
        self.bot_tab_z        = 2.0   # alt kenardan offset (pozitif = alttan yukarı)

        # Arka kapak
        self.cover_ct         = 2.0    # kapak kalınlığı
        self.cover_wh         = 15.0   # tab yüksekliği
        self.cover_clearance  = 0.05   # boşluk (clearance)
        self.cover_wt         = 3.0    # tab duvar kalınlığı

        # ── İç durum ─────────────────────────────────────────────────────────
        self._last_base   = None
        self._last_wg     = None
        self._last_ig     = None
        self.last_v       = None
        self.last_f       = None
        self.last_face_v  = None
        self.last_face_f  = None
        self.timer        = StageTimer()

    # ─────────────────────────────────────────────────────────────────────────
    # Parametre yükleyici
    # ─────────────────────────────────────────────────────────────────────────
    def set_params(self, **kw):
        """Herhangi bir parametreyi anahtar-değer çiftiyle ayarla."""
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                raise ValueError(f"Bilinmeyen parametre: {k!r}")

    # ─────────────────────────────────────────────────────────────────────────
    # DXF yükleme
    # ─────────────────────────────────────────────────────────────────────────
    def load_dxf(self, path: str):
        """DXF dosyasını yükle ve yolları normalize et."""
        new_paths = DXFImporter.read_paths(path)
        if not new_paths:
            raise RuntimeError("DXF'de görüntülenebilir yol bulunamadı.")
        self.paths      = new_paths
        self.base_paths = [{"points": list(p["points"]), "closed": p["closed"]}
                           for p in self.paths]
        self._normalize()

    def load_dxf_bytes(self, data: bytes, suffix: str = ".dxf"):
        """Byte stream'den DXF yükle — API için (geçici dosya)."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            self.load_dxf(tmp_path)
        finally:
            os.unlink(tmp_path)

    def _normalize(self):
        """Path'leri merkeze al (0,0)."""
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
    # Ana üretim pipeline
    # ─────────────────────────────────────────────────────────────────────────
    def build(self):
        """Tam üretim: duvar + yüz mesh'lerini hesapla."""
        if not self.paths:
            raise RuntimeError("Önce DXF yükleyin.")

        self.timer.clear()

        # 1. Shapely polygon
        s = _tic()
        base = self._build_letter()
        _toc(self.timer, "1-letter", s)
        if base is None or base.is_empty:
            raise RuntimeError("Path'lerden polygon oluşturulamadı.")

        # 2. Duvar geometrisi
        s = _tic()
        wg, ig = self._build_wall(base)
        _toc(self.timer, "2-wall", s)
        self._last_base = base
        self._last_wg   = wg
        self._last_ig   = ig

        # 3. Duvar mesh
        s = _tic()
        av, af, off = [], [], 0
        def _ap(vl, fl):
            nonlocal off
            if not vl or not fl: return
            av.extend(vl)
            af.extend([(a+off,b+off,c+off) for a,b,c in fl])
            off += len(vl)

        if self.wall_type == 0:
            v, f = MB.extrude_geom(wg, 0.0, self.height_mm)
        else:
            wall_mf = self._build_wall_mf(0.0, self.height_mm)
            if wall_mf is not None:
                import manifold3d as _m3d
                mesh = wall_mf.to_mesh()
                va2 = np.array(mesh.vert_properties, dtype=np.float32)
                fa2 = np.array(mesh.tri_verts, dtype=np.int32)
                v = [tuple(p) for p in va2]
                f = [tuple(t) for t in fa2]
            else:
                v, f = [], []
        _ap(v, f)

        # Slot (tırnak) mesh'leri
        if ig is not None and not ig.is_empty:
            if self.bot_tab and self.bot_proj > 0.01:
                z0, z1 = self._tab_z_range('bot', ft=0.0)
                ig_b = self._ig_at_z((z0+z1)/2.0)
                sv, sf = self._tab_mesh(ig_b, self.bot_proj, z0, z1, circular=True, profile=None)
                _ap(sv, sf)
            if self.top_tab and self.top_proj > 0.01:
                z0, z1 = self._tab_z_range('top', ft=0.0)
                ig_t = self._ig_at_z((z0+z1)/2.0)
                sv, sf = self._tab_mesh(ig_t, self.top_proj, z0, z1, circular=True, profile=_UST_TIRNAK_PROFILE)
                _ap(sv, sf)

        v, f = av, af
        _toc(self.timer, "3-extrude", s)

        self.last_v = v
        self.last_f = f

        # 4. Yüz mesh
        s = _tic()
        fv, ff = self._build_face_mesh()
        _toc(self.timer, "4-face", s)
        self.last_face_v = fv
        self.last_face_f = ff

        return {
            "wall_verts":  len(v) if v else 0,
            "wall_faces":  len(f) if f else 0,
            "face_verts":  len(fv) if fv is not None else 0,
            "face_faces":  len(ff) if ff is not None else 0,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Geometri hesaplama (Viewport'tan kopyalandı)
    # ─────────────────────────────────────────────────────────────────────────
    def _build_letter(self):
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
                except Exception:
                    pass
            rp.sort(key=lambda x: x["area"], reverse=True)
            used = set()
            for i, r in enumerate(rp):
                if i in used: continue
                ou = r["poly"]; holes = []
                for j, h in enumerate(rp):
                    if i == j or j in used: continue
                    try:
                        if ou.contains(h["poly"].representative_point()):
                            holes.append(list(h["poly"].exterior.coords))
                            used.add(j)
                    except Exception:
                        pass
                try:
                    poly = Polygon(list(ou.exterior.coords), holes)
                    if not poly.is_valid: poly = poly.buffer(0)
                    if not poly.is_empty: polys.append(poly)
                except Exception:
                    if not ou.is_empty: polys.append(ou)

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
            geom = (geom
                    .buffer( r, quad_segs=qs, join_style="round")
                    .buffer(-r, quad_segs=qs, join_style="round")
                    .buffer(0))
        # Mirror X
        if self.mx:
            from shapely.affinity import scale
            mirrored = scale(geom, xfact=-1, yfact=1, origin=(0,0))
            geom = unary_union([geom, mirrored])
            if make_valid: geom = make_valid(geom)
            geom = geom.buffer(0)
        # Mirror Y
        if self.my:
            from shapely.affinity import scale
            mirrored = scale(geom, xfact=1, yfact=-1, origin=(0,0))
            geom = unary_union([geom, mirrored])
            if make_valid: geom = make_valid(geom)
            geom = geom.buffer(0)
        return geom

    # ── Slot (tırnak) yardımcıları ────────────────────────────────────────────
    def _tab_z_range(self, which, ft=0.0):
        h = self.height_mm
        if which == 'top':
            proj = self.top_proj
            z1 = min(h * 0.99, h + self.top_tab_z)
            slot_h = max(proj * _UST_TIRNAK_ASPECT + 1.0, proj * 0.8)
            z0 = max(ft + 0.01, z1 - slot_h)
        else:
            proj = self.bot_proj
            z0 = max(ft + 0.01, self.bot_tab_z)
            slot_h = max(proj * _ALT_TIRNAK_ASPECT + 1.0, proj * 1.2)
            z1 = min(z0 + slot_h, h * 0.98)
        return z0, z1

    def _ig_at_z(self, z):
        if self._last_ig is None: return self._last_ig
        o = self._wall_offset_at_z(z)
        if o < 0.001: return self._last_ig
        qs = max(4, int(self.arc_sm))
        adj = self._last_ig.buffer(o, quad_segs=qs, join_style='mitre').buffer(0)
        return adj if (adj is not None and not adj.is_empty) else self._last_ig

    def _wall_offset_at_z(self, z):
        if self.wall_type == 0: return 0.0
        profile = _ACILI_PROFILE if self.wall_type == 1 else _KAVISLI_PROFILE
        t = max(0.0, min(1.0, z / max(self.height_mm, 1.0)))
        ph = [p[0] for p in profile]; pd = [p[1] for p in profile]
        lean = float(np.interp(t, ph, pd))
        return lean * self.wall_taper

    def _tab_mesh(self, ig, proj, z0, z1, circular=False, profile=None):
        proj = max(0.0, float(proj))
        if proj <= 0.01 or ig is None or ig.is_empty: return [], []
        qs = max(4, int(self.arc_sm))
        _gap = 0.001
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
                av.extend(v)
                af.extend([(a+off,b+off,c+off) for a,b,c in f])
                off += len(v)
            for i in range(N):
                t = (i + 0.5) / N
                t_lookup = (1.0-t) if _reverse else t
                w = proj * float(np.interp(t_lookup, _ph, _pd))
                if w < 0.01: continue
                z_s = z0 + i * dz
                try:
                    outer = ig.buffer(-_gap, quad_segs=qs, join_style='round').buffer(0)
                    inner = ig.buffer(-(_gap+w), quad_segs=qs, join_style='round').buffer(0)
                    if outer is None or outer.is_empty: continue
                    ring = outer if (inner is None or inner.is_empty) else outer.difference(inner)
                    ring = _clean(ring, 0.005)
                    if ring is None or ring.is_empty: continue
                    v, f = MB.extrude_geom(ring, z_s, z_s+dz)
                    _ap(v, f)
                except Exception: pass
            return av, af
        # Lineer ramp
        ramp_h = min(proj, height)
        N = max(4, min(10, int(round(ramp_h/0.4))))
        step_w = proj/N; _sep = 0.004
        av, af, off = [], [], 0
        def ap(v, f):
            nonlocal off
            if not v or not f: return
            av.extend(v); af.extend([(a+off,b+off,c+off) for a,b,c in f]); off += len(v)
        for k in range(N):
            za = z0 + ramp_h*k/N; zb = z0 + ramp_h*(k+1)/N
            oi = _gap + k*(step_w+_sep); ii = oi + step_w
            try:
                outer = ig.buffer(-oi, quad_segs=qs, join_style="round").buffer(0)
                if outer is None or outer.is_empty: continue
                inner2 = ig.buffer(-ii, quad_segs=qs, join_style="round").buffer(0)
                ring = outer if (inner2 is None or inner2.is_empty) else outer.difference(inner2)
                ring = _clean(ring, 0.005)
                if ring is None or ring.is_empty: continue
                v, f = MB.extrude_geom(ring, za, zb); ap(v, f)
            except Exception: pass
        return av, af

    def _build_wall(self, base):
        t  = self.wall_mm
        qs = max(4, int(self.arc_sm))
        base  = _clean(base, 0.05)
        inner = base.buffer(-t, quad_segs=qs, join_style="round").buffer(0)
        inner = _clean(inner, max(0.02, t * t * 0.05)) if not inner.is_empty else inner
        wall  = base if inner.is_empty else base.difference(inner)
        return _clean(wall, max(0.02, t * t * 0.03)), inner

    def _geom_to_mf(self, geom, z0, z1):
        import manifold3d as m3d
        result = None
        for poly in _as_polys(geom, 0.01):
            ext = list(poly.exterior.coords)
            if ext[0] == ext[-1]: ext = ext[:-1]
            contours = [[[float(x), float(y)] for x, y in ext]]
            for interior in poly.interiors:
                ic = list(interior.coords)
                if ic[0] == ic[-1]: ic = ic[:-1]
                contours.append([[float(x), float(y)] for x, y in ic])
            cs = m3d.CrossSection(contours, m3d.FillRule.EvenOdd)
            if cs.area() < 1e-6: continue
            mf = m3d.Manifold.extrude(cs, height=float(z1 - z0))
            if float(z0) != 0.0:
                mf = mf.translate([0.0, 0.0, float(z0)])
            if not mf.is_empty():
                result = mf if result is None else result + mf
        return result

    def _build_wall_mf(self, z0, z1):
        if self.wall_type == 1:
            return self._profile_wall_mf(z0, z1, _ACILI_PROFILE, self.wall_taper)
        if self.wall_type == 2:
            return self._profile_wall_mf(z0, z1, _KAVISLI_PROFILE, self.wall_taper)
        return self._geom_to_mf(self._last_wg, z0, z1)

    @staticmethod
    def _interp_profile(t, profile):
        if t <= profile[0][0]:  return profile[0][1]
        if t >= profile[-1][0]: return profile[-1][1]
        for i in range(len(profile) - 1):
            t0, s0 = profile[i]; t1, s1 = profile[i + 1]
            if t0 <= t <= t1:
                return s0 + (s1 - s0) * (t - t0) / (t1 - t0)
        return 0.0

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

    def _profile_wall_mf(self, z0, z1, profile, max_lean):
        import manifold3d as m3d
        if self._last_base is None or self._last_ig is None: return None
        qs = max(4, int(self.arc_sm))
        h_span = float(z1 - z0)
        n  = max(24, int(round(h_span / 0.6)))
        dz = h_span / n
        slices = []
        for i in range(n):
            t = (i + 0.5) / n
            o = self._interp_profile(t, profile) * max_lean
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
                mf = mf.translate([0.0, 0.0, float(z0 + i * dz)])
                slices.append(mf)
        if not slices: return None
        while len(slices) > 1:
            nxt = []
            for j in range(0, len(slices), 2):
                nxt.append(slices[j] + slices[j + 1] if j + 1 < len(slices) else slices[j])
            slices = nxt
        return slices[0]

    def _build_face_mesh(self):
        if self.face_mode == 0 or self._last_base is None or self.face_thickness <= 0:
            return None, None
        geom = self._last_base if self.face_mode in (1, 3) else (
            self._last_ig if (self._last_ig and not self._last_ig.is_empty) else None)
        if geom is None: return None, None

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
            z0_mid   = SOLID_D if has_front else 0.0
            z1_mid   = ft - SOLID_D if has_back else ft
            mid_geom = _face_fill_pattern(geom, fill, cell, wall, border)
            sections = []
            if has_front: sections.append((geom,     0.0,          SOLID_D))
            sections.append(              (mid_geom,  z0_mid,       z1_mid ))
            if has_back:  sections.append((geom,     ft - SOLID_D, ft     ))
            combined = None
            for g, z0, z1 in sections:
                mf = self._geom_to_mf(g, z0, z1)
                if mf and not mf.is_empty():
                    combined = mf if combined is None else combined + mf
            if combined is None or combined.is_empty(): return None, None
            av, af = _mf_to_arrays(combined)
        else:
            if fill:
                geom = _face_fill_pattern(geom, fill, cell, wall, border)
            mf = self._geom_to_mf(geom, 0.0, ft)
            if mf is None or mf.is_empty(): return None, None
            av, af = _mf_to_arrays(mf)

        return av, af

    # ─────────────────────────────────────────────────────────────────────────
    # STL yardımcı
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _vf_to_stl_bytes(v, f) -> bytes:
        """(verts, faces) → binary STL bytes."""
        cv, cf, info = cleanup(v, f)
        if len(cf) == 0:
            raise RuntimeError("Cleanup sonrası triangle kalmadı.")
        V = np.asarray(cv, dtype=np.float32)
        F = np.asarray(cf, dtype=np.int32)
        tri = V[F]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n = (n / ln[:, None]).astype(np.float32)
        dt = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
        data = np.zeros(len(F), dtype=dt)
        data["normal"] = n; data["vertices"] = tri.astype(np.float32)
        buf = io.BytesIO()
        hdr = b"Harfex" + b" " * 74
        buf.write(hdr)
        buf.write(np.array([len(F)], dtype=np.uint32).tobytes())
        buf.write(data.tobytes())
        return buf.getvalue()

    # ─────────────────────────────────────────────────────────────────────────
    # Export (bytes döndürür — dosyaya yazmaz)
    # ─────────────────────────────────────────────────────────────────────────
    def export_stl_bytes(self) -> bytes:
        """Duvar/gövde STL'ini bytes olarak döndür."""
        if not self.last_v or not self.last_f:
            raise RuntimeError("Önce build() çağrılmalı.")
        return self._vf_to_stl_bytes(self.last_v, self.last_f)

    def export_face_stl_bytes(self) -> bytes:
        """Yüz (face) STL'ini bytes olarak döndür."""
        if self.last_face_v is None or self.last_face_f is None:
            raise RuntimeError("Yüz mesh boş — face_mode=0 veya build() çağrılmadı.")
        v = np.asarray(self.last_face_v, dtype=np.float32)
        f = np.asarray(self.last_face_f, dtype=np.int32)
        cv, cf, _ = cleanup(v, f)
        if len(cf) == 0:
            raise RuntimeError("Yüz cleanup sonrası boş.")
        return self._vf_to_stl_bytes(cv, cf)

    def export_cover_stl_bytes(self) -> bytes:
        """Arka kapak STL'ini bytes olarak döndür."""
        if self._last_base is None or self._last_ig is None:
            raise RuntimeError("Önce build() çağrılmalı.")
        ct = self.cover_ct; wh = self.cover_wh
        cl = self.cover_clearance; wt = self.cover_wt

        base_geom = self._base_at_z(self.height_mm)
        ig_back   = self._ig_at_z(self.height_mm)

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

        # Y-flip: doğru montaj yönü
        minx, miny, maxx, maxy = base_geom.bounds
        center_y = (miny + maxy) / 2.0
        va[:, 1] = 2.0 * center_y - va[:, 1]
        fa = fa[:, [0, 2, 1]]

        return self._vf_to_stl_bytes(va, fa)

    def export_plexiglas_dxf_bytes(self) -> bytes:
        """Pleksiglas kesim DXF'ini bytes olarak döndür."""
        if self._last_base is None:
            raise RuntimeError("Önce build() çağrılmalı.")
        import ezdxf
        offset = self.plexiglas_offset
        geom = self._last_base
        if offset != 0:
            geom = geom.buffer(offset, join_style="round").buffer(0)

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        for poly in _as_polys(geom, 0.01):
            coords = list(poly.exterior.coords)
            msp.add_lwpolyline([(x, y) for x, y in coords], close=True)
            for interior in poly.interiors:
                ic = list(interior.coords)
                msp.add_lwpolyline([(x, y) for x, y in ic], close=True)

        buf = io.StringIO()
        doc.write(buf)
        return buf.getvalue().encode("utf-8")
