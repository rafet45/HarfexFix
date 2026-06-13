# Harfex — solid caps via correct mapbox_earcut ring_end_indices API
#
# Root cause of hollow-shell bug (18.0/18.1):
#   earcut_lib.triangulate_float64(flat, None)  → TypeError in v2.0.0
#   earcut_lib.triangulate_float64(flat, hole_start_indices) → wrong API
#
# Fix (18.2):
#   earcut_lib.triangulate_float64(vertices_2d, ring_end_indices)
#   ring_end_indices[i] = exclusive end index of ring i
#   e.g. exterior 4 pts + hole 4 pts → ring_ends=[4, 8]
#   The last value MUST equal the total vertex count (validated by the library).

import sys, math, os, time, subprocess, json
from pathlib import Path
from lang import _t, set_lang, current_lang

# ── Harfex Engine — GUI'den bağımsız hesaplama katmanı ──────────────────────
from harfex_engine import (
    MB, DXFImporter, cleanup,
    _face_fill_pattern, _earcut_polygon,
    _as_polys, _clean,
    dist, clean_points, signed_area,
    StageTimer, _tic, _toc,
    GEOMETRY_EXECUTOR, HAS_EARCUT, HAS_SHAPELY,
    Polygon, MultiPolygon, LineString,
    unary_union, polygonize, make_valid,
    _ACILI_PROFILE, _KAVISLI_PROFILE,
    _ALT_TIRNAK_PROFILE, _ALT_TIRNAK_ASPECT,
    _UST_TIRNAK_PROFILE, _UST_TIRNAK_ASPECT,
    _test_earcut_startup,
)

def _app_dir() -> Path:
    """Bundled data dosyaları — PyInstaller'da _MEIPASS, normalde __file__ klasörü."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent

def _user_dir() -> Path:
    """Yazılabilir dosyalar (profiles, settings) — her zaman exe'nin yanı."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent

# ── Splash screen — pencere hazır olunca kapatılacak (aşağıda MainWindow.__init__) ──
try:
    import pyi_splash as _pyi_splash  # type: ignore
    _pyi_splash.update_text('Harfex yükleniyor…')
    _HAS_SPLASH = True
except Exception:
    _HAS_SPLASH = False
from concurrent.futures import ThreadPoolExecutor
import numpy as np


# ── Model Slot — all per-model state for one independently loaded DXF ────────
class ModelSlot:
    """All per-model state for one independently loaded DXF."""
    _ATTRS = [
        'paths', 'base_paths', 'has_model',
        'wall_mm', 'height_mm', 'arc_sm', 'round_c', 'simplify',
        'face_mode', 'face_thickness', 'plexiglas_offset',
        'wall_type', 'wall_taper', 'flip_z', 'mx', 'my',
        'top_tab', 'bot_tab', 'top_proj', 'bot_proj', 'top_tab_z', 'bot_tab_z',
        'cover_ct', 'cover_wh', 'cover_clearance', 'cover_wt',
        'face_fill', 'face_fill_cell', 'face_fill_wall',
        '_last_base', '_last_wg', '_last_ig', '_last_wall_v', '_last_wall_f',
        'last_v', 'last_f', 'last_face_v', 'last_face_f',
        'mesh_items', 'line_items', 'cover_items',
        '_wall_gl_item', '_face_gl_item',
        'color_wall', 'color_face',
    ]

    def __init__(self, label="Model", x_offset=0.0, filename=""):
        self.label      = label
        self.x_offset   = x_offset
        self.filename   = filename   # short DXF filename for display
        self.paths = []; self.base_paths = []
        self.has_model  = False
        self.wall_mm    = 5.0;  self.height_mm  = 45.0
        self.arc_sm     = 8.0;  self.round_c    = 1.0;   self.simplify = 0.05
        self.face_mode  = 1;    self.face_thickness = 3.0; self.plexiglas_offset = 0.0
        self.foam_offset = 0.0
        self.foam_corner_r = 0.0;  self.plexiglas_corner_r = 0.0
        self.wall_type  = 0;    self.wall_taper = 13.81
        self.flip_z     = False; self.mx = False; self.my = False
        self.top_tab    = False; self.bot_tab = False
        self.top_proj   = 4.0;  self.bot_proj   = 4.0
        self.top_tab_z  = -2.0; self.bot_tab_z  = 2.0
        self.cover_ct   = 2.0;  self.cover_wh   = 15.0
        self.cover_clearance = 0.05; self.cover_wt = 3.0
        self._last_base = None; self._last_wg = None; self._last_ig = None
        self._last_wall_v = None; self._last_wall_f = None
        self.last_v = None; self.last_f = None
        self.last_face_v = None; self.last_face_f = None
        self.mesh_items  = []; self.line_items = []; self.cover_items = []
        self._wall_gl_item = None; self._face_gl_item = None
        self.color_wall = (0.35, 0.88, 0.85, 1.0)   # light turquoise
        self.color_face = (0.95, 0.45, 0.10, 1.0)
        self.face_fill              = 0    # 0=Solid 1=Honeycomb 2=Grid 3=Lines
        self.face_fill_cell         = 8.0  # mm — cell size / pitch
        self.face_fill_wall         = 1.6  # mm — wall thickness between voids
        self.face_fill_solid_pos = 0    # 0=Kapalı 1=Ön yüz 2=Arka yüz 3=Her ikisi
        self.face_fill_border   = 1.5  # mm — dolgu kenar payı (solid kontur genişliği)



# ── Pickable GL View (mouse selection support) ────────────────────────────────
class PickableGLView(gl.GLViewWidget):
    """GLViewWidget subclass that emits object_clicked(key) on single-click.
    Drag (rotate/pan/zoom) is unchanged — only a click with no movement triggers picking."""

    object_clicked       = Signal(str, bool)   # (key, ctrl_held) — key="" for empty space
    object_right_clicked = Signal(str, QPoint) # (key, global_pos) — right-click context

    _CLICK_MAX_DIST = 6    # px — max movement to count as click (not drag)
    _CLICK_MAX_MS   = 400  # ms — max duration to count as click

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos   = None
        self._press_time  = 0.0
        self._press_ctrl  = False
        # Reference to Viewport's scene_objects dict (set by Viewport after creation)
        self._scene_objects = {}

    # ── Mouse events ──────────────────────────────────────────────────────────
    def mousePressEvent(self, ev):
        self._press_pos  = ev.pos()
        self._press_time = time.time()
        self._press_ctrl = bool(ev.modifiers() & Qt.ControlModifier)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        super().mouseReleaseEvent(ev)
        if self._press_pos is None:
            return
        dp   = ev.pos() - self._press_pos
        dist = (dp.x() ** 2 + dp.y() ** 2) ** 0.5
        dt   = time.time() - self._press_time
        if dist < self._CLICK_MAX_DIST and dt < self._CLICK_MAX_MS / 1000.0:
            self._pick(ev.pos(), self._press_ctrl)
        self._press_pos = None

    def contextMenuEvent(self, ev):
        """Right-click: pick object under cursor, emit object_right_clicked signal."""
        key = self._pick_key(ev.pos())
        self.object_right_clicked.emit(key, ev.globalPos())

    # ── Screen-space picking ──────────────────────────────────────────────────
    _PICK_AABB_PAD = np.array([8.0, 8.0, 8.0], dtype=np.float64)  # mm — expand AABB for picking

    def _pick_key(self, pos):
        """Hybrid picking:
        1. Cast 3D ray; test against each object's AABB expanded by _PICK_AABB_PAD
           so thin/distant objects (back cover) are easier to hit.
        2. Among all 3D hits, pick the one whose AABB *centre* projects closest
           to the click pixel in screen space — this correctly prefers the cover
           when clicking its visual position, even though it sits further from the
           camera than the body (larger ray-t)."""
        try:
            origin, direction = self._ray_from_mouse(pos)
        except Exception as e:
            print(f"[PICK] ray error: {e}")
            return ""

        w  = max(self.width(),  1)
        h  = max(self.height(), 1)
        cx = pos.x()
        cy = pos.y()

        best_key   = ""
        best_dist2 = float("inf")

        for key, obj in self._scene_objects.items():
            aabb = obj.get("aabb")
            if aabb is None:
                continue
            if not obj.get("visible", True):
                continue
            # Expand AABB for picking so thin/far objects are still hittable
            mn = aabb[0] - self._PICK_AABB_PAD
            mx = aabb[1] + self._PICK_AABB_PAD
            if self._ray_aabb(origin, direction, mn, mx) is None:
                continue  # ray misses even the expanded AABB — skip

            # Project AABB centre to screen space
            centre = (aabb[0] + aabb[1]) * 0.5
            sc = self._world_to_screen(centre, w, h)
            if sc is None:
                continue
            d2 = (sc[0] - cx) ** 2 + (sc[1] - cy) ** 2
            if d2 < best_dist2:
                best_dist2 = d2
                best_key   = key

        return best_key

    def _world_to_screen(self, pt, w, h, _origin=None, _dir=None):
        """Project a 3D world point to screen-pixel (x, y). Returns None if behind camera."""
        try:
            opts    = self.opts
            dist_c  = float(opts["distance"])
            el      = math.radians(float(opts["elevation"]))
            az      = math.radians(float(opts["azimuth"]))
            ctr     = opts["center"]
            fov     = float(opts.get("fov", 60.0))

            # Camera position (same formula as _ray_from_mouse)
            ccx = float(ctr.x()) + dist_c * math.cos(el) * math.sin(az)
            ccy = float(ctr.y()) - dist_c * math.cos(el) * math.cos(az)
            ccz = float(ctr.z()) + dist_c * math.sin(el)
            cam = np.array([ccx, ccy, ccz], dtype=np.float64)

            fwd = np.array([float(ctr.x()) - ccx,
                            float(ctr.y()) - ccy,
                            float(ctr.z()) - ccz], dtype=np.float64)
            fwd /= np.linalg.norm(fwd)
            up0 = np.array([0.0, 0.0, 1.0])
            rgt = np.cross(fwd, up0)
            if np.linalg.norm(rgt) < 1e-6:
                up0 = np.array([0.0, 1.0, 0.0])
                rgt = np.cross(fwd, up0)
            rgt /= np.linalg.norm(rgt)
            upv = np.cross(rgt, fwd)
            upv /= np.linalg.norm(upv)

            p      = np.asarray(pt, dtype=np.float64) - cam
            z_cam  = np.dot(p, fwd)
            if z_cam <= 1e-6:
                return None
            x_cam  = np.dot(p, rgt)
            y_cam  = np.dot(p, upv)

            aspect = w / h
            t_fov  = math.tan(math.radians(fov / 2.0))
            nx     = x_cam / (z_cam * t_fov * aspect)
            ny     = y_cam / (z_cam * t_fov)
            sx     = (nx + 1.0) * 0.5 * w
            sy     = (1.0 - ny) * 0.5 * h
            return sx, sy
        except Exception:
            return None

    def _pick(self, pos, ctrl_held=False):
        """Cast a ray from mouse pos, find nearest object, emit signal."""
        best_key = self._pick_key(pos)
        self.object_clicked.emit(best_key, ctrl_held)

    def _ray_from_mouse(self, pos):
        """Return (origin, direction) numpy arrays in world space."""
        w = self.width()
        h = self.height()
        if w == 0 or h == 0:
            raise ValueError("zero-size widget")

        opts  = self.opts
        dist  = float(opts["distance"])
        el    = math.radians(float(opts["elevation"]))
        az    = math.radians(float(opts["azimuth"]))
        ctr   = opts["center"]                    # QVector3D
        fov   = float(opts.get("fov", 60.0))

        # Camera position in world space (spherical → Cartesian, pyqtgraph convention)
        cx = float(ctr.x()) + dist * math.cos(el) * math.sin(az)
        cy = float(ctr.y()) - dist * math.cos(el) * math.cos(az)
        cz = float(ctr.z()) + dist * math.sin(el)
        cam_pos = np.array([cx, cy, cz], dtype=np.float64)

        # Camera basis vectors
        forward = np.array([float(ctr.x()) - cx,
                            float(ctr.y()) - cy,
                            float(ctr.z()) - cz], dtype=np.float64)
        forward /= np.linalg.norm(forward)

        world_up = np.array([0.0, 0.0, 1.0])
        right    = np.cross(forward, world_up)
        if np.linalg.norm(right) < 1e-6:
            world_up = np.array([0.0, 1.0, 0.0])
            right    = np.cross(forward, world_up)
        right /= np.linalg.norm(right)
        up = np.cross(right, forward)
        up /= np.linalg.norm(up)

        # NDC → ray direction
        nx     = (2.0 * pos.x() / w) - 1.0
        ny     = 1.0 - (2.0 * pos.y() / h)
        aspect = w / h
        t_fov  = math.tan(math.radians(fov / 2.0))
        ray_dir = forward + right * (nx * t_fov * aspect) + up * (ny * t_fov)
        ray_dir /= np.linalg.norm(ray_dir)

        return cam_pos, ray_dir

    @staticmethod
    def _ray_aabb(origin, direction, aabb_min, aabb_max):
        """Ray-AABB slab intersection. Returns t (positive) or None."""
        tmin, tmax = -np.inf, np.inf
        for i in range(3):
            if abs(direction[i]) < 1e-9:
                if origin[i] < aabb_min[i] or origin[i] > aabb_max[i]:
                    return None
            else:
                t1 = (aabb_min[i] - origin[i]) / direction[i]
                t2 = (aabb_max[i] - origin[i]) / direction[i]
                tmin = max(tmin, min(t1, t2))
                tmax = min(tmax, max(t1, t2))
        if tmax < max(tmin, 0.0):
            return None
        return tmin if tmin > 0.0 else tmax


# ── Object selection dialog ───────────────────────────────────────────────────
class ObjectSelectDialog(QDialog):
    """Checkbox dialog: user picks which scene objects to export."""

    def __init__(self, scene_objects, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export 3MF — Nesne Seçimi")
        self.setMinimumWidth(340)
        self.setStyleSheet(
            "QDialog{background:#1e2430;color:#ccc;}"
            "QLabel{color:#ccc;font-size:11px;}"
            "QCheckBox{color:#ddd;font-size:12px;padding:4px;}"
            "QCheckBox::indicator{width:16px;height:16px;}"
            "QCheckBox::indicator:checked{background:#2277ff;border:1px solid #448;border-radius:3px;}"
            "QCheckBox::indicator:unchecked{background:#333;border:1px solid #555;border-radius:3px;}"
            "QPushButton{background:#2a3348;color:#ccc;border:1px solid #445;"
            "border-radius:4px;padding:5px 18px;font-size:11px;}"
            "QPushButton:hover{background:#334;color:#fff;}"
        )

        self._checks = {}   # key → QCheckBox

        vl = QVBoxLayout(self)
        vl.setSpacing(6)
        vl.setContentsMargins(18, 16, 18, 12)

        hdr = QLabel("Export edilecek nesneleri seçin:")
        hdr.setStyleSheet("color:#aaa;font-size:10px;margin-bottom:4px;")
        vl.addWidget(hdr)

        for key, obj in scene_objects.items():
            v = obj.get("v")
            tri_count = len(obj.get("f", [])) if obj.get("f") is not None else 0
            label = obj.get("label", key)
            cb = QCheckBox(f"  {label}   ({tri_count:,} üçgen)")
            cb.setChecked(True)
            vl.addWidget(cb)
            self._checks[key] = cb

        vl.addSpacing(8)

        # Buttons
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export")
        bb.button(QDialogButtonBox.Cancel).setText("İptal")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        vl.addWidget(bb)

    def selected_keys(self):
        """Return list of checked object keys."""
        return [k for k, cb in self._checks.items() if cb.isChecked()]








# ── Viewport ──────────────────────────────────────────────────────────────────
class Viewport(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("cadViewport")

        # ── State (initialised before UI so callbacks are safe) ───────────────
        self._show_contour = False
        self._boundary_items = []          # 3-D outline rings drawn on top of model
        self.on_dxf_loaded = None          # callable() — fired after load_dxf succeeds
        self.paths = []; self.base_paths = []
        self.mesh_items = []; self.line_items = []
        self.wall_mm = 5.0; self.height_mm = 45.0
        self.arc_sm = 8.0; self.round_c = 1.0; self.simplify = 0.05
        self.bot_tab = False; self.top_tab = False
        self.bot_proj = 4.0; self.top_proj = 4.0
        # bot_tab_z: mm above bottom edge (e.g. 2 = slot starts 2 mm from bottom)
        # top_tab_z: mm below top edge, entered as negative (e.g. -2 = 2 mm from top)
        self.bot_tab_z = 2.0; self.top_tab_z = -2.0
        self.flip_z = False
        self.has_model = False; self.mx = False; self.my = False
        self.face_mode = 1        # default Mod1 (Outer): Front F Thick field is active by default
        self.face_thickness = 3.0
        self.face_fill              = 0    # 0=Solid 1=Honeycomb 2=Grid 3=Lines
        self.face_fill_cell         = 8.0  # mm — cell pitch
        self.face_fill_wall         = 1.6  # mm — wall thickness between voids
        self.face_fill_solid_pos = 0    # 0=Kapalı 1=Ön yüz 2=Arka yüz 3=Her ikisi
        self.face_fill_border   = 1.5  # mm — dolgu kenar payı (solid kontur genişliği)
        self.plexiglas_offset = 0.0
        self.foam_offset = 0.0
        self.foam_corner_r = 0.0;  self.plexiglas_corner_r = 0.0
        self._last_base = None; self._last_wg = None; self._last_ig = None
        self._last_wall_v = None; self._last_wall_f = None
        self.last_face_v = None; self.last_face_f = None
        self.last_v = None; self.last_f = None
        self.fast = True; self.timer = StageTimer()
        self.cover_ct = 2.0; self.cover_wh = 15.0; self.cover_clearance = 0.05; self.cover_wt = 3.0
        self.wall_type = 0
        self.wall_taper = 13.81
        self.cover_items = []
        # Color separate: per-part filament colors
        self.color_wall  = (0.35, 0.88, 0.85, 1.0)   # filament 1 — light turquoise default
        self.color_face  = (0.95, 0.45, 0.10, 1.0)   # filament 2 — orange default
        self._wall_gl_item = None   # live reference for color update
        self._face_gl_item = None
        # Display mode: 'wireframe', 'shaded', 'rendered'
        self._display_mode = 'rendered'
        self._disp_btns: dict = {}   # populated in _make_vp_bar
        # Object registry — key → {v, f, gl_item, color, label, aabb}
        self.scene_objects    = {}
        self.selected_obj_key  = None   # last selected key (compat)
        self.selected_obj_keys: set = set()  # multi-select set
        self.groups: dict = {}           # group_id → [key, key, ...]
        self._group_counter: int = 0

        # ── Slot system ───────────────────────────────────────────────────────
        self.slots = []
        self.active_slot_idx = 0
        self.on_slot_switch         = None   # callable(slot_idx)
        self.on_object_list_changed = None   # callable() — refresh object list panel

        # ── Layout ────────────────────────────────────────────────────────────
        l = QVBoxLayout(self); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(0)
        l.addWidget(self._make_vp_bar())

        self.view = PickableGLView()
        self.view.setBackgroundColor((210, 213, 218, 255))  # light grey — better detail visibility
        self.view.opts.update({"distance": 500, "elevation": 35, "azimuth": -45})
        self.view._scene_objects = self.scene_objects   # share reference for picking
        l.addWidget(self.view, 1)

        self.grid = gl.GLGridItem()
        self.grid.setSize(700, 700); self.grid.setSpacing(25, 25)
        self.grid.setColor((80, 80, 90, 120))              # medium grey lines for light background
        self.view.addItem(self.grid)
        self.axis = gl.GLAxisItem()
        self.axis.setSize(80, 80, 80)
        self.view.addItem(self.axis)

        # Initialize default slot 0 from flat attributes already set above
        s0 = ModelSlot(label="Model 1", x_offset=0.0)
        # Share list references so mutations on flat attrs are reflected in slot
        s0.mesh_items  = self.mesh_items
        s0.line_items  = self.line_items
        s0.cover_items = self.cover_items
        self.slots = [s0]
        self.active_slot_idx = 0

    # ── Viewport toolbar ──────────────────────────────────────────────────────
    def _make_vp_bar(self):
        bar = QFrame(); bar.setObjectName("vpBar")
        bar.setFixedHeight(34)
        bar.setStyleSheet(
            "#vpBar{background:#161b24;border-bottom:1px solid #333;}"
        )
        h = QHBoxLayout(bar); h.setContentsMargins(8, 2, 8, 2); h.setSpacing(3)

        _mode_style = (
            "QPushButton{background:#252c3a;color:#aab;border:1px solid #3a4255;"
            "border-radius:4px;padding:0 10px;font-size:11px;}"
            "QPushButton:hover{background:#2e3750;color:#fff;}"
            "QPushButton:checked{background:#1a5a9e;color:#fff;border:1px solid #2e88ee;}"
        )

        # Display mode radio buttons
        modes = [
            ("wireframe", _t("vp_wireframe"), _t("vp_tip_wireframe")),
            ("shaded",    _t("vp_shaded"),    _t("vp_tip_shaded")),
            ("rendered",  _t("vp_rendered"),  _t("vp_tip_rendered")),
        ]
        self._disp_btns = {}
        for mode_key, mode_label, mode_tip in modes:
            b = QPushButton(mode_label)
            b.setCheckable(True)
            b.setChecked(mode_key == self._display_mode)
            b.setFixedHeight(26)
            b.setStyleSheet(_mode_style)
            b.setToolTip(mode_tip)
            b.clicked.connect(lambda _, m=mode_key: self.set_display_mode(m))
            h.addWidget(b)
            self._disp_btns[mode_key] = b

        h.addStretch()

        _grp_style = (
            "QPushButton{background:#252c3a;color:#aab;border:1px solid #3a4255;"
            "border-radius:4px;padding:0 10px;font-size:11px;}"
            "QPushButton:hover{background:#2e3750;color:#fff;}"
            "QPushButton:disabled{background:#1a1f2a;color:#444;"
            "border:1px solid #252c3a;}"
        )
        self._btn_group = QPushButton(_t("vp_group"))
        self._btn_group.setFixedHeight(26)
        self._btn_group.setStyleSheet(_grp_style)
        self._btn_group.setToolTip(_t("vp_tip_group"))
        self._btn_group.setEnabled(False)
        self._btn_group.clicked.connect(self._do_create_group)
        h.addWidget(self._btn_group)

        self._btn_ungroup = QPushButton(_t("vp_ungroup"))
        self._btn_ungroup.setFixedHeight(26)
        self._btn_ungroup.setStyleSheet(_grp_style)
        self._btn_ungroup.setToolTip(_t("vp_tip_ungroup"))
        self._btn_ungroup.setEnabled(False)
        self._btn_ungroup.clicked.connect(self._do_dissolve_group)
        h.addWidget(self._btn_ungroup)

        return bar

    def _toggle_contour(self):
        self._show_contour = not self._show_contour
        for item in self.line_items:
            item.setVisible(self._show_contour)
        for item in self._boundary_items:
            item.setVisible(self._show_contour)

    def _set_view(self, elevation, azimuth):
        self.view.opts['elevation'] = elevation
        self.view.opts['azimuth']   = azimuth
        self.view.update()

    # ── Display mode ──────────────────────────────────────────────────────────
    def _disp_params(self):
        """Returns (drawFaces, drawEdges, shader) for current display mode."""
        if self._display_mode == 'wireframe':
            return False, True, None
        elif self._display_mode == 'shaded':
            return True, True, 'shaded'    # faces + edges
        else:   # rendered
            return True, False, 'shaded'   # faces only, no edges

    def set_display_mode(self, mode: str):
        self._display_mode = mode
        for m, btn in self._disp_btns.items():
            btn.setChecked(m == mode)
        df, de, sh = self._disp_params()
        for item in self.mesh_items + self.cover_items:
            # setMeshData without meshdata creates an empty MeshData — avoid it.
            # Directly mutate opts and force re-parse for edge buffers.
            item.opts['drawFaces'] = df
            item.opts['drawEdges'] = de
            item.opts['shader']    = sh
            item.opts['edgeColor'] = (0.0, 0.0, 0.0, 1.0)   # siyah kenar
            item.meshDataChanged()

    # ── Color Separate ────────────────────────────────────────────────────────
    def show_color_dialog(self, parent=None):
        """Open color assignment dialog. Updates viewport live on each pick."""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog

        def _rgba_to_qcolor(rgba):
            r, g, b, a = [int(c * 255) for c in rgba]
            return QColor(r, g, b, a)

        def _qcolor_to_rgba(qc):
            return (qc.red()/255, qc.green()/255, qc.blue()/255, 1.0)

        def _swatch_style(qc):
            return (f"background:{qc.name()}; border:2px solid #555; border-radius:6px;"
                    f"min-width:80px; min-height:36px;")

        dlg = QDialog(parent)
        dlg.setWindowTitle("Renk Ata — Filament Renkleri")
        dlg.setFixedWidth(320)
        dlg.setStyleSheet(
            "QDialog{background:#2a2f3a;}"
            "QLabel{color:#ddd; font-size:12px;}"
            "QPushButton#okBtn{background:#336699;color:white;border-radius:6px;"
            "font-size:12px;padding:6px 20px;border:none;}"
            "QPushButton#okBtn:hover{background:#4477aa;}"
        )

        vl = QVBoxLayout(dlg); vl.setSpacing(14); vl.setContentsMargins(18, 16, 18, 16)

        title = QLabel("Her parçanın baskı rengini seç")
        title.setStyleSheet("color:#aaa; font-size:11px; margin-bottom:4px;")
        vl.addWidget(title)

        # ── Row builder ───────────────────────────────────────────────────────
        wall_qc = [_rgba_to_qcolor(self.color_wall)]
        face_qc = [_rgba_to_qcolor(self.color_face)]

        def make_row(label_text, qc_ref, apply_fn):
            row = QHBoxLayout()
            lbl = QLabel(label_text); lbl.setFixedWidth(130)
            btn = QPushButton(); btn.setObjectName("swatchBtn")
            btn.setStyleSheet(_swatch_style(qc_ref[0]))
            btn.setFixedSize(80, 36)

            def pick():
                chosen = QColorDialog.getColor(qc_ref[0], dlg, "Renk Seç")
                if chosen.isValid():
                    qc_ref[0] = chosen
                    btn.setStyleSheet(_swatch_style(chosen))
                    apply_fn(_qcolor_to_rgba(chosen))   # live viewport update

            btn.clicked.connect(pick)
            row.addWidget(lbl); row.addStretch(); row.addWidget(btn)
            return row

        def apply_wall(rgba):
            self.color_wall = rgba
            if self._wall_gl_item is not None:
                self._wall_gl_item.setColor(rgba)

        def apply_face(rgba):
            self.color_face = rgba
            if self._face_gl_item is not None:
                self._face_gl_item.setColor(rgba)

        vl.addLayout(make_row("Gövde / Duvar  (F1)", wall_qc, apply_wall))

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#444;"); vl.addWidget(sep)

        vl.addLayout(make_row("Ön Yüz  (F2)", face_qc, apply_face))

        # info note
        note = QLabel("Renk değişimi viewport'ta anında görünür.\n"
                      "Export 3MF, bu renkleri OrcaSlicer'a taşır.")
        note.setStyleSheet("color:#777; font-size:10px; margin-top:4px;")
        vl.addWidget(note)

        ok = QPushButton("Tamam"); ok.setObjectName("okBtn")
        ok.clicked.connect(dlg.accept)
        vl.addWidget(ok)

        dlg.exec()

    def clear_model(self):
        for x in self.mesh_items: self.view.removeItem(x)
        self.mesh_items.clear()
        for x in self.line_items: self.view.removeItem(x)
        self.line_items.clear()
        for x in self._boundary_items: self.view.removeItem(x)
        self._boundary_items.clear()
        for x in self.cover_items: self.view.removeItem(x)
        self.cover_items.clear()
        # Unregister active slot's scene objects only
        slot_idx = self.active_slot_idx
        self.scene_objects.pop(f"body_{slot_idx}", None)
        self.scene_objects.pop(f"face_{slot_idx}", None)
        self.scene_objects.pop(f"cover_{slot_idx}", None)
        self.selected_obj_key  = None
        self.selected_obj_keys = set()
        self._update_group_btns()

    def build_cover_preview(self):
        """Build and display back cover in viewport: base plate (base) + inner tab (ig shrunk by clearance)."""
        for x in self.cover_items: self.view.removeItem(x)
        self.cover_items.clear()
        if self._last_base is None or self._last_ig is None:
            return
        try:
            ct  = float(self.cover_ct)
            wh  = float(self.cover_wh)
            cl  = float(self.cover_clearance)
            # Use back-face geometry (z=h) so cover fits the expanded wall opening
            base_geom = self._base_at_z(self.height_mm)
            ig_back   = self._ig_at_z(self.height_mm)
            base_mf = self._geom_to_mf(base_geom, 0.0, ct)
            tab_outer = ig_back.buffer(-cl, join_style="mitre").buffer(0)
            tab_inner = tab_outer.buffer(-self.cover_wt, join_style="mitre").buffer(0)
            tab_geom  = tab_outer.difference(tab_inner) if not tab_inner.is_empty else tab_outer
            if tab_geom is None or tab_geom.is_empty:
                return
            wall_mf = self._geom_to_mf(tab_geom, ct, ct + wh)
            if base_mf is None or wall_mf is None:
                return
            cover_mf = base_mf + wall_mf
            mesh = cover_mf.to_mesh()
            va = np.array(mesh.vert_properties, dtype=np.float32)
            fa = np.array(mesh.tri_verts, dtype=np.int32)
            # Y-flip around center → correct closure direction, print orientation (plate at z=0)
            minx, miny, maxx, maxy = base_geom.bounds
            center_y = (miny + maxy) / 2.0
            va[:, 1] = 2.0 * center_y - va[:, 1]   # Y-flip
            fa = fa[:, [0, 2, 1]]                    # restore winding after single flip
            va[:, 1] += (maxy - miny) + 5.0          # offset above the letter in Y
            # Apply slot x_offset for display
            _x_off = self.slots[self.active_slot_idx].x_offset if self.slots else 0.0
            if _x_off != 0.0:
                va[:, 0] += _x_off
            _cover_color = (0.65, 0.65, 0.65, 1.0)
            _df, _de, _sh = self._disp_params()
            gl_mesh = gl.MeshData(vertexes=va, faces=fa)
            item = gl.GLMeshItem(meshdata=gl_mesh, smooth=True, drawFaces=_df,
                                 drawEdges=_de, color=_cover_color,
                                 edgeColor=(0,0,0,1),
                                 shader=_sh, glOptions="opaque")
            self.view.addItem(item)
            self.cover_items.append(item)
            # Register in scene object registry (slot-keyed)
            _c_idx   = self.active_slot_idx
            _c_slot  = self.slots[_c_idx] if self.slots else None
            _c_file  = _c_slot.filename if _c_slot else ""
            self.scene_objects[f"cover_{_c_idx}"] = {
                "v":        va,
                "f":        fa,
                "gl_item":  item,
                "color":    _cover_color,
                "label":    f"Back Cover {_c_idx + 1}" if len(self.slots) > 1 else "Back Cover",
                "filename": _c_file,
                "obj_type": "Back Cover",
                "aabb":     self._compute_aabb(va),
                "slot_idx": _c_idx,
                "visible":  False,
            }
            if callable(self.on_object_list_changed):
                self.on_object_list_changed()
        except Exception as e:
            print(f"[COVER PREVIEW] {e}")

    def remove_cover(self):
        """Remove back cover from viewport and scene_objects registry."""
        for x in self.cover_items:
            self.view.removeItem(x)
        self.cover_items.clear()
        _c_idx = self.active_slot_idx
        self.scene_objects.pop(f"cover_{_c_idx}", None)
        if callable(self.on_object_list_changed):
            self.on_object_list_changed()

    def _snapshot(self):
        """Return a lightweight snapshot of scene_objects mesh data for undo."""
        snap = {}
        for key, obj in self.scene_objects.items():
            v = obj.get("v"); f = obj.get("f")
            snap[key] = {
                "v":        v.copy() if v is not None else None,
                "f":        f.copy() if f is not None else None,
                "color":    obj.get("color"),
                "label":    obj.get("label"),
                "visible":  obj.get("visible", False),
                "obj_type": obj.get("obj_type"),
                "slot_idx": obj.get("slot_idx"),
                "filename": obj.get("filename"),
                "group_id": obj.get("group_id"),
                "aabb":     obj.get("aabb"),
            }
        return snap

    def _restore_from_snapshot(self, snap):
        """Rebuild GL items from a snapshot; replaces current scene content."""
        for x in self.mesh_items:  self.view.removeItem(x)
        for x in self.cover_items: self.view.removeItem(x)
        self.mesh_items.clear(); self.cover_items.clear()
        self.scene_objects.clear()
        self.selected_obj_key  = None
        self.selected_obj_keys = set()
        _df, _de, _sh = self._disp_params()
        for key, data in snap.items():
            v, f = data.get("v"), data.get("f")
            if v is None or f is None or len(f) == 0:
                continue
            color = data.get("color") or (0.75, 0.75, 0.75, 1.0)
            gl_mesh = gl.MeshData(vertexes=np.asarray(v, dtype=np.float32),
                                  faces=np.asarray(f, dtype=np.int32))
            item = gl.GLMeshItem(meshdata=gl_mesh, smooth=True,
                                 drawFaces=_df, drawEdges=_de,
                                 color=color, edgeColor=(0, 0, 0, 1),
                                 shader=_sh, glOptions="opaque")
            self.view.addItem(item)
            entry = dict(data); entry["gl_item"] = item
            self.scene_objects[key] = entry
            if key.startswith("cover_"):
                self.cover_items.append(item)
            else:
                self.mesh_items.append(item)
        self._update_group_btns()
        if callable(self.on_object_list_changed):
            self.on_object_list_changed()

    # ── Object registry helpers ───────────────────────────────────────────────
    @staticmethod
    def _compute_aabb(verts):
        """Return (min_xyz, max_xyz) numpy arrays for vertex array."""
        v = np.asarray(verts, dtype=np.float64)
        return v.min(axis=0), v.max(axis=0)

    @staticmethod
    @staticmethod
    def _harden_cap_edges(v, f):
        """Yan yüz / tavan-taban sınırındaki vertex'leri çoğaltır → keskin kenar.
        Tamamen vektörize: büyük mesh'lerde de hızlı çalışır."""
        va = np.asarray(v, dtype=np.float32)
        fa = np.asarray(f, dtype=np.int32)
        # Çok büyük mesh'lerde atla (tapered sliced walls)
        if len(fa) > 60_000:
            return va, fa
        t  = va[fa]
        fn = np.cross(t[:,1]-t[:,0], t[:,2]-t[:,0]).astype(np.float32)
        ln = np.linalg.norm(fn, axis=1, keepdims=True); ln[ln < 1e-12] = 1.0
        fn /= ln
        is_side  = np.abs(fn[:, 2]) < 0.7
        n_v = len(va)
        side_mask = np.zeros(n_v, dtype=bool)
        cap_mask  = np.zeros(n_v, dtype=bool)
        side_mask[fa[is_side].ravel()]  = True
        cap_mask[fa[~is_side].ravel()]  = True
        shared = np.where(side_mask & cap_mask)[0]
        if len(shared) == 0:
            return va, fa
        # Hızlı remap dizisi: shared[i] → n_v + i
        remap = np.full(n_v, -1, dtype=np.int32)
        remap[shared] = np.arange(n_v, n_v + len(shared), dtype=np.int32)
        new_va = np.concatenate([va, va[shared]], axis=0)
        # Tavan yüzlerini vektörize olarak yeniden indeksle
        new_fa   = fa.copy()
        cap_mask_rows = ~is_side
        cap_fa   = new_fa[cap_mask_rows]          # (n_cap, 3)
        for col in range(3):
            col_v   = cap_fa[:, col]
            new_idx = remap[col_v]
            replace = new_idx >= 0
            cap_fa[replace, col] = new_idx[replace]
        new_fa[cap_mask_rows] = cap_fa
        return new_va, new_fa

    @staticmethod
    def _smooth_side_normals(v, f):
        """Per-vertex normals using ONLY side faces (|nz|<0.7).
        Cap/horizontal faces are excluded from the average so slice-ring
        boundaries don't create shading ridges on tapered walls."""
        va = np.asarray(v, dtype=np.float32)
        fa = np.asarray(f, dtype=np.int32)
        # Per-face normals
        t  = va[fa]
        fn = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]).astype(np.float32)
        ln = np.linalg.norm(fn, axis=1, keepdims=True); ln[ln < 1e-12] = 1.0
        fn /= ln
        # Split: side (|nz|<0.7) vs cap
        is_side = np.abs(fn[:, 2]) < 0.7
        vn = np.zeros_like(va)
        # Accumulate side contributions
        s_fa = fa[is_side]; s_fn = fn[is_side]
        np.add.at(vn, s_fa[:, 0], s_fn)
        np.add.at(vn, s_fa[:, 1], s_fn)
        np.add.at(vn, s_fa[:, 2], s_fn)
        # Cap contributions only for vertices with no side face
        has_side = np.zeros(len(va), dtype=bool)
        has_side[s_fa.ravel()] = True
        c_fa = fa[~is_side]; c_fn = fn[~is_side]
        for col in range(3):
            mask = ~has_side[c_fa[:, col]]
            np.add.at(vn, c_fa[mask, col], c_fn[mask])
        # Normalize
        ln = np.linalg.norm(vn, axis=1, keepdims=True); ln[ln < 1e-12] = 1.0
        return (vn / ln).astype(np.float32)

    _SEL_COLOR = (1.0, 0.80, 0.15, 1.0)   # bright yellow for selection

    def _highlight_keys(self, keys):
        """Set highlight color on all keys in set."""
        for k in keys:
            obj = self.scene_objects.get(k, {})
            gi  = obj.get("gl_item")
            if gi is not None:
                gi.setColor(self._SEL_COLOR)

    def _restore_keys(self, keys):
        """Restore display colors after selection clears.
        If checkbox is ticked (check_color set) keep that color; else original."""
        for k in keys:
            obj = self.scene_objects.get(k, {})
            gi  = obj.get("gl_item")
            if gi is not None:
                gi.setColor(obj.get("check_color", obj.get("color", (1, 1, 1, 1))))

    def set_selected(self, key):
        """Single-click select: deselect previous, then select key + all group members.
        Mod 1: body↔face are always highlighted together (auto-pair)."""
        self._restore_keys(self.selected_obj_keys)
        if not key:
            self.selected_obj_keys = set()
            self.selected_obj_key  = None
            self._update_group_btns()
            return
        # Expand to group if this key belongs to one
        gid = self.scene_objects.get(key, {}).get("group_id")
        if gid and gid in self.groups:
            keys_to_select = set(self.groups[gid])
        else:
            keys_to_select = {key}
        # Mod 1: auto-pair body↔face — clicking either highlights both
        if self.face_mode == 1:
            slot_idx = self.scene_objects.get(key, {}).get("slot_idx", self.active_slot_idx)
            if key.startswith("body_") and f"face_{slot_idx}" in self.scene_objects:
                keys_to_select.add(f"face_{slot_idx}")
            elif key.startswith("face_") and f"body_{slot_idx}" in self.scene_objects:
                keys_to_select.add(f"body_{slot_idx}")
        self.selected_obj_keys = keys_to_select
        self.selected_obj_key  = key
        self._highlight_keys(keys_to_select)
        self._update_group_btns()

    def set_selected_add(self, key):
        """Ctrl+click: toggle key in current selection (multi-select)."""
        if key in self.selected_obj_keys:
            self.selected_obj_keys.discard(key)
            self._restore_keys({key})
        else:
            self.selected_obj_keys.add(key)
            self._highlight_keys({key})
        self.selected_obj_key = key if self.selected_obj_keys else None
        self._update_group_btns()

    def deselect_all(self):
        """Restore all objects. Checkbox-ticked objects keep their gold color."""
        self._restore_keys(self.selected_obj_keys)
        for key, obj in self.scene_objects.items():
            gi = obj.get("gl_item")
            if gi is not None:
                gi.setColor(obj.get("check_color", obj.get("color", (1, 1, 1, 1))))
        self.selected_obj_keys = set()
        self.selected_obj_key  = None
        self._update_group_btns()

    # ── Group helpers ─────────────────────────────────────────────────────────
    def create_group_from_selection(self):
        """Create a named group from currently selected objects. Returns group_id."""
        if len(self.selected_obj_keys) < 2:
            return None
        self._group_counter += 1
        gid = f"grup_{self._group_counter}"
        self.groups[gid] = list(self.selected_obj_keys)
        for k in self.selected_obj_keys:
            if k in self.scene_objects:
                self.scene_objects[k]["group_id"] = gid
        return gid

    def dissolve_group_for_selection(self):
        """Remove group membership for all selected objects."""
        removed = set()
        for k in self.selected_obj_keys:
            gid = self.scene_objects.get(k, {}).get("group_id")
            if gid and gid in self.groups:
                removed.add(gid)
        for gid in removed:
            for k in self.groups.pop(gid, []):
                if k in self.scene_objects:
                    self.scene_objects[k].pop("group_id", None)

    def _do_create_group(self):
        gid = self.create_group_from_selection()
        if gid:
            n = len(self.groups[gid])
            self._update_group_btns()
            if callable(self.on_object_list_changed):
                self.on_object_list_changed()

    def _do_dissolve_group(self):
        self.dissolve_group_for_selection()
        self._update_group_btns()
        if callable(self.on_object_list_changed):
            self.on_object_list_changed()

    def _update_group_btns(self):
        """Enable/disable Grup and Çöz buttons based on selection state."""
        if not hasattr(self, "_btn_group"):
            return
        n = len(self.selected_obj_keys)
        self._btn_group.setEnabled(n >= 2)
        has_group = any(
            self.scene_objects.get(k, {}).get("group_id")
            for k in self.selected_obj_keys
        )
        self._btn_ungroup.setEnabled(bool(self.selected_obj_keys) and has_group)

    def _auto_group_mod1_slot(self, slot_idx):
        """Mod 1: automatically group body+face of this slot into a permanent group.
        Replaces any previous group for this slot."""
        bk = f"body_{slot_idx}"
        fk = f"face_{slot_idx}"
        if bk not in self.scene_objects or fk not in self.scene_objects:
            return
        # Remove old group for these keys (if any)
        old_gid = self.scene_objects[bk].get("group_id")
        if old_gid and old_gid in self.groups:
            for k in self.groups.pop(old_gid, []):
                if k in self.scene_objects:
                    self.scene_objects[k].pop("group_id", None)
        # Create new group
        self._group_counter += 1
        gid = f"grup_{self._group_counter}"
        self.groups[gid] = [bk, fk]
        self.scene_objects[bk]["group_id"] = gid
        self.scene_objects[fk]["group_id"] = gid

    # ── Slot system helpers ───────────────────────────────────────────────────
    _SLOT_SAVE = [
        'paths', 'base_paths', 'has_model',
        'wall_mm', 'height_mm', 'arc_sm', 'round_c', 'simplify',
        'face_mode', 'face_thickness', 'plexiglas_offset',
        'wall_type', 'wall_taper', 'flip_z', 'mx', 'my',
        'top_tab', 'bot_tab', 'top_proj', 'bot_proj', 'top_tab_z', 'bot_tab_z',
        'cover_ct', 'cover_wh', 'cover_clearance', 'cover_wt',
        'face_fill', 'face_fill_cell', 'face_fill_wall',
        '_last_base', '_last_wg', '_last_ig', '_last_wall_v', '_last_wall_f',
        'last_v', 'last_f', 'last_face_v', 'last_face_f',
        '_wall_gl_item', '_face_gl_item',
        'color_wall', 'color_face',
    ]
    # mesh_items, line_items, cover_items are shared references — not in _SLOT_SAVE

    def _snapshot_to_slot(self, slot):
        """Copy flat viewport attributes → slot."""
        for attr in self._SLOT_SAVE:
            setattr(slot, attr, getattr(self, attr))
        # Lists: copy so the slot has its own list object
        slot.paths      = list(self.paths)
        slot.base_paths = list(self.base_paths)
        # GL item lists are shared references — assign same list object
        slot.mesh_items  = self.mesh_items
        slot.line_items  = self.line_items
        slot.cover_items = self.cover_items

    def _restore_from_slot(self, slot):
        """Copy slot → flat viewport attributes."""
        for attr in self._SLOT_SAVE:
            setattr(self, attr, getattr(slot, attr))
        self.paths      = list(slot.paths)
        self.base_paths = list(slot.base_paths)
        # Restore shared list references
        self.mesh_items  = slot.mesh_items
        self.line_items  = slot.line_items
        self.cover_items = slot.cover_items

    def _next_x_offset(self, new_paths_raw):
        """Compute x_offset (mm) for a new slot, placed to the right of current."""
        # Current slot's half-width from its normalized paths
        if self.paths:
            xs = [x for p in self.paths for x, y in p.get("points", [])]
            prev_hw = (max(xs) - min(xs)) / 2.0 if xs else 50.0
        else:
            prev_hw = 50.0
        prev_x = self.slots[self.active_slot_idx].x_offset if self.slots else 0.0
        # New slot's half-width from raw paths (normalize preserves width)
        xs_new = [x for p in new_paths_raw for x, y in p.get("points", [])]
        if xs_new:
            raw_w  = max(xs_new) - min(xs_new)
            new_hw = raw_w / 2.0
        else:
            new_hw = 50.0
        gap = 20.0   # mm between models
        return prev_x + prev_hw + gap + new_hw

    def switch_to_slot(self, idx):
        """Save current state, switch active slot, restore state, notify UI."""
        if idx == self.active_slot_idx or idx < 0 or idx >= len(self.slots):
            return
        self._snapshot_to_slot(self.slots[self.active_slot_idx])
        self.active_slot_idx = idx
        self._restore_from_slot(self.slots[idx])
        if callable(self.on_slot_switch):
            self.on_slot_switch(idx)

    def _slot_label(self):
        """Return label of active slot."""
        if self.slots:
            return self.slots[self.active_slot_idx].label
        return "Model"

    def load_dxf(self, path):
        if not Polygon:
            raise RuntimeError("shapely not installed: pip install shapely")
        new_paths = DXFImporter.read_paths(path)
        if not new_paths:
            raise RuntimeError("No displayable paths in DXF.")

        import os as _os
        fname = _os.path.basename(path)

        # If the current active slot is empty (no DXF loaded yet), reuse it at x=0.
        # Otherwise snapshot it and create a new slot placed to the right.
        current_empty = not self.paths   # True when slot has never had a DXF

        if current_empty:
            # ── First load (or empty slot): stay at x=0 ─────────────────────
            x_off   = 0.0
            new_idx = self.active_slot_idx
            if self.slots:
                self.slots[new_idx].filename = fname
                self.slots[new_idx].label    = f"Model {new_idx + 1}"
                self.slots[new_idx].x_offset = 0.0
            label = f"Model {new_idx + 1}"
        else:
            # ── Subsequent load: save current slot, create new to the right ──
            self._snapshot_to_slot(self.slots[self.active_slot_idx])
            x_off   = self._next_x_offset(new_paths)
            label   = f"Model {len(self.slots) + 1}"
            new_slot = ModelSlot(label=label, x_offset=x_off, filename=fname)
            self.slots.append(new_slot)
            new_idx = len(self.slots) - 1
            self.active_slot_idx = new_idx
            self._restore_from_slot(new_slot)

        # Set new paths on the active slot
        self.paths     = new_paths
        self.has_model = False
        self.mx = False; self.my = False; self.flip_z = False
        # Yeni DXF yüklenince tab aktif durumları sıfırla (değerler korunur)
        self.top_tab = False; self.bot_tab = False

        # Normalize: centers paths at (0,0), resets camera to model center
        self.normalize()
        self.base_paths = [{"points": list(p["points"]), "closed": p["closed"]}
                           for p in self.paths]

        # Widen grid when multiple models are visible
        if len(self.slots) > 1 and not current_empty:
            total_span = abs(x_off) + 300.0
            self.grid.setSize(max(total_span * 2, 600), max(total_span * 2, 600))

        self.show_2d()

        # Pending entry: list shows file immediately, before Generate 3D
        self.scene_objects.pop(f"body_{new_idx}", None)   # clear stale entry
        self.scene_objects[f"body_{new_idx}"] = {
            "v": None, "f": None, "gl_item": None,
            "color":    (0.22, 0.62, 0.95, 1.0),
            "label":    label,
            "filename": fname,
            "obj_type": "Ana Gövde",
            "slot_idx": new_idx,
            "visible":  False,
            "pending":  True,
        }

        # Notify UI that object list changed and DXF dimensions are available
        if callable(self.on_object_list_changed):
            self.on_object_list_changed()
        if callable(self.on_dxf_loaded):
            self.on_dxf_loaded()

    def normalize(self):
        xs, ys = [], []
        for p in self.paths:
            for x, y in p["points"]: xs.append(x); ys.append(y)
        minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
        size = max(maxx - minx, maxy - miny, 1.0)
        self.paths = [
            {"points": clean_points([(x - cx, y - cy) for x, y in p["points"]], 0.0001),
             "closed": p["closed"]}
            for p in self.paths
        ]
        # Center camera on the newly loaded model (display position = x_offset)
        x_off = self.slots[self.active_slot_idx].x_offset if self.slots else 0.0
        try:
            import pyqtgraph as _pg
            self.view.opts["center"] = _pg.Vector(x_off, 0, 0)
        except Exception:
            self.view.opts["center"] = QVector3D(x_off, 0, 0)
        self.view.opts["distance"]  = max(size * 2.5, 180)
        self.view.opts["elevation"] = 35
        self.view.opts["azimuth"]   = -45
        self.view.update()   # force camera redraw
        self.grid.setSize(max(size * 1.8, 300), max(size * 1.8, 300))
        sp = max(round(size / 20, 1), 5)
        self.grid.setSpacing(sp, sp)

    def show_2d(self):
        x_off = self.slots[self.active_slot_idx].x_offset if self.slots else 0.0
        for p in self.paths:
            pts = p["points"]
            if len(pts) < 2: continue
            pos = np.array([[x + x_off, y, 0.0] for x, y in pts], dtype=float)
            item = gl.GLLinePlotItem(pos=pos, color=(0.05, 0.25, 0.70, 1.0),
                                     width=2.0, antialias=True, mode="line_strip")
            self.view.addItem(item); self.line_items.append(item)

    def set_curve(self, arc, rc, simp):
        self.arc_sm = max(1.0, min(float(arc), 32.0))
        self.round_c = max(0.0, min(float(rc), 30.0))
        self.simplify = max(0.0, min(float(simp), 5.0))

    def build_mesh_data(self, wall, height, fast=True):
        if not self.paths:
            raise RuntimeError("Load a DXF first.")
        self.timer.clear(); t0 = _tic()
        self.wall_mm = max(0.2, min(float(wall), 80.0))
        self.height_mm = max(1.0, min(float(height), 250.0))
        old_arc = self.arc_sm; old_simp = self.simplify
        self.fast = fast
        if fast:
            self.arc_sm = min(self.arc_sm, 8.0)
            self.simplify = max(self.simplify, 0.04)
        try:
            s = _tic(); base = self.build_letter(); _toc(self.timer, "1-letter", s)
            if base is None or base.is_empty:
                raise RuntimeError("Could not build polygon from paths.")

            s = _tic(); wg, ig = self.build_wall(base); _toc(self.timer, "2-wall", s)
            self._last_base = base; self._last_wg = wg; self._last_ig = ig

            av, af, off = [], [], 0

            def ap(v, f):
                nonlocal off
                if not v or not f: return
                av.extend(v)
                af.extend([(a + off, b + off, c + off) for a, b, c in f])
                off += len(v)

            s = _tic()
            ft_wall   = self.face_thickness if self.face_mode == 1 and self.face_thickness > 0 else 0.0
            mod1_wall = self.face_mode == 1 and ft_wall > 0
            cap_bot   = not mod1_wall
            h_mm      = self.height_mm

            if self.wall_type == 0:
                v, f = MB.extrude_geom(wg, ft_wall, h_mm, cap_bottom=cap_bot, cap_top=True)
            else:
                import manifold3d as _m3d
                wall_mf = self._build_wall_mf(ft_wall, h_mm)
                if wall_mf is not None:
                    _mesh = wall_mf.to_mesh()
                    _va = np.array(_mesh.vert_properties, dtype=np.float32)
                    _fa = np.array(_mesh.tri_verts, dtype=np.int32)
                    v = [tuple(p) for p in _va]
                    f = [tuple(t) for t in _fa]
                else:
                    v, f = [], []
            _toc(self.timer, "3-extrude", s)
            ap(v, f)

            # Mod1: ig kapağı burada eklenmez — face mesh zaten z=ft'yi kapatıyor

            real_ft = ft_wall
            if ig is not None and not ig.is_empty:
                if self.bot_tab and self.bot_proj > 0.01:
                    z0, z1 = self._tab_z_range('bot', ft=real_ft)
                    ig_b = self._ig_at_z((z0 + z1) / 2.0)
                    s = _tic(); v, f = self.tab_mesh(ig_b, self.bot_proj, z0, z1, circular=True)
                    _toc(self.timer, "4-bot_tab", s); ap(v, f)
                if self.top_tab and self.top_proj > 0.01:
                    z0, z1 = self._tab_z_range('top', ft=real_ft)
                    ig_t = self._ig_at_z((z0 + z1) / 2.0)
                    s = _tic(); v, f = self.tab_mesh(ig_t, self.top_proj, z0, z1, circular=True, profile=_UST_TIRNAK_PROFILE)
                    _toc(self.timer, "5-top_tab", s); ap(v, f)

            if not av or not af:
                raise RuntimeError("Mesh generation produced no geometry.")
            _toc(self.timer, "TOTAL", t0); self.timer.report()
            self._last_wall_v = np.asarray(av, dtype=np.float32)
            self._last_wall_f = np.asarray(af, dtype=np.int32)
            return self._last_wall_v, self._last_wall_f, base.boundary
        finally:
            self.arc_sm = old_arc; self.simplify = old_simp

    def tab_mesh(self, ig, proj, z0, z1, circular=False, profile=None):
        """Build tab slot mesh.
        circular=True  → DXF profil kullanır (aşağı eğim).
            profile=None → _ALT_TIRNAK_PROFILE (alta tam çıkıntı, ters interp)
            profile=list → verilen profili doğrudan kullanır (alta tam çıkıntı varsayılır)
            FDM'de desteksiz basılabilir; pleksiglas üst eğimden kayarak snap-fit oturur.
        circular=False → mevcut lineer ramp profil (üst tırnak için).
        """
        proj = max(0.0, float(proj))
        if proj <= 0.01 or ig is None or ig.is_empty: return [], []
        qs = max(4, int(self.arc_sm))
        _gap = 0.001
        height = float(z1) - float(z0)

        # ── DXF profilden türetilmiş tırnak ──────────────────────────────────
        if circular:
            _prof = profile if profile is not None else _ALT_TIRNAK_PROFILE
            _ph = [p[0] for p in _prof]
            _pd = [p[1] for p in _prof]
            # Her iki slot da eğim aşağıya (tabana) bakacak: z0=dar/sıfır, z1=geniş/tam.
            # _ALT_TIRNAK_PROFILE 0→1 gidiyor → doğrudan (reverse=False): z0≈0, z1≈1 ✓
            # _UST_TIRNAK_PROFILE 1→0 gidiyor → ters      (reverse=True):  z0=0,  z1=1 ✓
            _reverse = (profile is not None)
            N = max(10, min(24, int(round(height / 0.3))))
            dz = height / N
            av, af, off = [], [], 0
            def _ap(v, f):
                nonlocal off
                if not v or not f: return
                av.extend(v)
                af.extend([(a+off, b+off, c+off) for a, b, c in f])
                off += len(v)
            for i in range(N):
                t  = (i + 0.5) / N
                t_lookup = (1.0 - t) if _reverse else t
                w  = proj * float(np.interp(t_lookup, _ph, _pd))
                if w < 0.01: continue
                z_s = z0 + i * dz
                try:
                    outer = ig.buffer(-_gap,        quad_segs=qs, join_style='round').buffer(0)
                    inner = ig.buffer(-(_gap + w),  quad_segs=qs, join_style='round').buffer(0)
                    if outer is None or outer.is_empty: continue
                    ring = outer if (inner is None or inner.is_empty) else outer.difference(inner)
                    ring = _clean(ring, 0.005)
                    if ring is None or ring.is_empty: continue
                    v, f = MB.extrude_geom(ring, z_s, z_s + dz)
                    _ap(v, f)
                except Exception as _e:
                    print(f"[TAB_PROF i={i}] {_e}")
            return av, af

        # ── Lineer ramp profil (üst tırnak — orijinal kod) ───────────────────
        ramp_h = min(proj, height)          # 45° ramp: height == proj (1 mm up per 1 mm in)
        N = max(4, min(10, int(round(ramp_h / 0.4))))
        step_w = proj / N
        _sep = 0.004    # horizontal gap between adjacent steps (> weld grid 0.0001 mm)
        _z_gap = 0.01   # tiny z gap between ramp top and full-width section

        av, af, off = [], [], 0

        def ap(v, f):
            nonlocal off
            if not v or not f: return
            av.extend(v)
            af.extend([(a + off, b + off, c + off) for a, b, c in f])
            off += len(v)

        try:
            # Staircase ramp: N independent manifold rings, each in a different XY band
            # Step 0 (bottom): thin ring near ig inner wall
            # Step k: progressively deeper/wider ring, stacked higher
            # Each step is fully closed (nm=0); _sep gap prevents shared edges between steps
            for k in range(N):
                za = z0 + ramp_h * k / N
                zb = z0 + ramp_h * (k + 1) / N
                oi = _gap + k * (step_w + _sep)
                ii = oi + step_w
                try:
                    outer = ig.buffer(-oi, quad_segs=qs, join_style="round").buffer(0)
                    if outer is None or outer.is_empty: continue
                    inner = ig.buffer(-ii, quad_segs=qs, join_style="round").buffer(0)
                    ring = outer if (inner is None or inner.is_empty) else outer.difference(inner)
                    ring = _clean(ring, 0.005)
                    if ring is None or ring.is_empty: continue
                    v, f = MB.extrude_geom(ring, za, zb)
                    ap(v, f)
                except Exception as ek:
                    print(f"[TAB k={k}] {ek}")

            # Full-width rectangular block above the ramp
            z_fw = z0 + ramp_h + _z_gap
            if z1 > z_fw + 0.01:
                try:
                    outer = ig.buffer(-_gap, quad_segs=qs, join_style="round").buffer(0)
                    inner = ig.buffer(-(_gap + proj), quad_segs=qs, join_style="round").buffer(0)
                    if outer and not outer.is_empty:
                        ring = outer if (inner is None or inner.is_empty) else outer.difference(inner)
                        ring = _clean(ring, 0.005)
                        if ring and not ring.is_empty:
                            v, f = MB.extrude_geom(ring, z_fw, z1)
                            ap(v, f)
                except Exception as ef:
                    print(f"[TAB full] {ef}")

        except Exception as e:
            print(f"[TAB] {e}")

        return av, af

    def apply_mesh(self, verts, faces, boundary=None):
        self.clear_model(); self.has_model = True
        self.last_v = np.asarray(verts, dtype=float)
        self.last_f = np.asarray(faces, dtype=int)
        # Apply x_offset for display only (last_v keeps original coords for export)
        _x_off = self.slots[self.active_slot_idx].x_offset if self.slots else 0.0
        if _x_off != 0.0:
            _disp_v = self.last_v.copy()
            _disp_v[:, 0] += _x_off
        else:
            _disp_v = self.last_v
        _df, _de, _sh = self._disp_params()
        if self.wall_type != 0:
            _de = False   # tapered: no edge lines
        # Keskin kenar + düzgün yan normal: tüm duvar tipleri için
        try:
            _hv, _hf = self._harden_cap_edges(_disp_v, self.last_f)
            _vn = self._smooth_side_normals(_hv, _hf)
            mesh = gl.MeshData(vertexes=_hv, faces=_hf)
            if hasattr(mesh, 'setVertexNormals'):
                mesh.setVertexNormals(_vn)
            else:
                mesh._vertexNormals = _vn
        except Exception:
            _hv, _hf = _disp_v, self.last_f
            mesh = gl.MeshData(vertexes=_hv, faces=_hf)
        item = gl.GLMeshItem(meshdata=mesh, smooth=True, drawFaces=_df, drawEdges=_de,
                             color=self.color_wall, edgeColor=(0,0,0,1),
                             shader=_sh, glOptions="opaque")
        self.view.addItem(item); self.mesh_items.append(item)
        self._wall_gl_item = item   # keep reference for live color updates
        # Register in scene object registry (slot-keyed); remove pending entry if present
        _slot_idx  = self.active_slot_idx
        _slot      = self.slots[_slot_idx] if self.slots else None
        _slot_lbl  = _slot.label    if _slot else "Model"
        _slot_file = _slot.filename if _slot else ""
        self.scene_objects[f"body_{_slot_idx}"] = {
            "v":         self.last_v,   # original coords for export
            "f":         self.last_f,
            "gl_item":   item,
            "color":     self.color_wall,
            "label":     _slot_lbl,
            "filename":  _slot_file,
            "obj_type":  "Ana Gövde",
            "aabb":      self._compute_aabb(_disp_v),
            "slot_idx":  _slot_idx,
            "visible":   False,
            "pending":   False,
        }
        if callable(self.on_object_list_changed):
            self.on_object_list_changed()
        if boundary:
            # Boundary outline: goes into _boundary_items (separate from 2-D DXF lines)
            # so the contour toggle controls it independently.
            _before = len(self.line_items)
            self.draw_lines(boundary, z=self.height_mm + 0.3, color=(0.05, 0.30, 0.55, 1.0))
            new_bdry = self.line_items[_before:]
            self._boundary_items.extend(new_bdry)
            del self.line_items[_before:]
            # Respect current contour toggle state
            for _bi in new_bdry:
                _bi.setVisible(self._show_contour)

    def build_letter(self):
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
        return geom

    def _tab_z_range(self, which, ft=0.0):
        """Return (z0, z1) for a tab slot using edge-relative offsets.
        which='top': top_tab_z is negative offset from the top edge.
                     e.g. top_tab_z=-2  → slot top at (h-2), slot fills downward.
        which='bot': bot_tab_z is positive offset from the bottom (z=0).
                     e.g. bot_tab_z=2   → slot bottom at z=2, slot fills upward.
        ft: minimum z floor (face plate thickness, 0 for non-combined builds).
        """
        h = self.height_mm
        if which == 'top':
            proj = self.top_proj
            z1 = min(h * 0.99, h + self.top_tab_z)         # top edge of slot
            slot_h = max(proj * _UST_TIRNAK_ASPECT + 1.0, proj * 0.8)
            z0 = max(ft + 0.01, z1 - slot_h)               # bottom edge of slot
        else:
            proj = self.bot_proj
            z0 = max(ft + 0.01, self.bot_tab_z)            # bottom edge of slot
            # Alt tırnak: DXF profilinin doğal yükseklik/çıkıntı oranı kullanılır
            slot_h = max(proj * _ALT_TIRNAK_ASPECT + 1.0, proj * 1.2)
            z1 = min(z0 + slot_h, h * 0.98)               # top edge of slot
        return z0, z1

    def build_wall(self, base):
        t = self.wall_mm; qs = max(4, int(self.arc_sm))
        base = _clean(base, 0.05)
        inner = base.buffer(-t, quad_segs=qs, join_style="round").buffer(0)
        inner = _clean(inner, max(0.02, t * t * 0.05)) if not inner.is_empty else inner
        wall = base if inner.is_empty else base.difference(inner)
        return _clean(wall, max(0.02, t * t * 0.03)), inner

    @staticmethod
    def _add_anchor(v, f, z0, z1, width=0.001):
        """Append a phantom anchor fin from z0 to z1 (width mm).
        The fin is thinner than any nozzle (< 0.2 mm) so the slicer generates
        zero toolpath for it — but z_min of the mesh becomes z0, preventing
        OrcaSlicer's 'Place on Bed' from shifting the object."""
        v = np.asarray(v, dtype=np.float32)
        f = np.asarray(f, dtype=np.int32)
        # Anchor point: vertex whose z is closest to z1 (bottom of real geometry)
        bot_idx = int(np.argmin(np.abs(v[:, 2] - z1)))
        ax, ay  = float(v[bot_idx, 0]), float(v[bot_idx, 1])
        w = width / 2.0
        nv = len(v)
        extra_v = np.array([
            [ax - w, ay, z0],   # nv
            [ax + w, ay, z0],   # nv+1
            [ax - w, ay, z1],   # nv+2
            [ax + w, ay, z1],   # nv+3
        ], dtype=np.float32)
        extra_f = np.array([
            [nv,   nv+1, nv+2],
            [nv+1, nv+3, nv+2],
        ], dtype=np.int32)
        return np.vstack([v, extra_v]), np.vstack([f, extra_f])

    @staticmethod
    def _write_stl(v, f, filename):
        """Write v/f arrays to binary STL."""
        tri = np.asarray(v, dtype=np.float32)[np.asarray(f, dtype=np.int32)]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n = (n / ln[:, None]).astype(np.float32)
        dt = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
        data = np.zeros(len(f), dtype=dt)
        data["normal"] = n; data["vertices"] = tri
        with open(filename, "wb") as fh:
            hdr = b"Harfex"[:80]
            fh.write(hdr + b" " * (80 - len(hdr)))
            np.array([len(f)], dtype=np.uint32).tofile(fh)
            data.tofile(fh)

    def export_stl(self, filename):
        if self.last_v is None or self.last_f is None or len(self.last_f) == 0:
            raise RuntimeError("Run 'Generate 3D Model' first.")
        s = _tic()
        cv, cf, info = cleanup(self.last_v, self.last_f)
        if len(cf) == 0:
            raise RuntimeError("No triangles remain after cleanup.")
        V = cv; F = cf; tri = V[F]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n = (n / ln[:, None]).astype(np.float32)
        dt = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
        data = np.zeros(len(F), dtype=dt)
        data["normal"] = n; data["vertices"] = tri.astype(np.float32)
        with open(filename, "wb") as f:
            hdr = b"Harfex"[:80]
            f.write(hdr + b" " * (80 - len(hdr)))
            np.array([len(F)], dtype=np.uint32).tofile(f)
            data.tofile(f)
        self.timer.add("7-stl", time.perf_counter() - s)
        self.timer.report()
        try:
            with open(Path(filename).with_name("export_report.txt"), "w", encoding="utf-8") as f:
                f.write("Harfex Export\n")
                for k, v in info.items():
                    f.write(f"{k}:{v}\n")
                f.write(f"Manifold: {'CLEAN' if info['nm'] == 0 else f'WARNING {info[chr(110)+chr(109)]}'}\n")
        except Exception:
            pass
        return info

    def build_trimmed_wall_mesh(self, z0, z1):
        """Wall mesh from z0 to z1 — manifold3d garantili, NM=0."""
        if self._last_wg is None:
            return None, None
        if self.wall_type != 0:
            wall_mf = self._build_wall_mf(z0, z1)
        else:
            wall_mf = self._geom_to_mf(self._last_wg, z0, z1)
        if wall_mf is None or wall_mf.is_empty():
            return None, None
        _mesh = wall_mf.to_mesh()
        import numpy as _np2
        va = _np2.array(_mesh.vert_properties, dtype=_np2.float32)
        fa = _np2.array(_mesh.tri_verts, dtype=_np2.int32)
        return va, fa

    def build_mod1_wall_with_tabs(self, ft, H):
        """Mod 1 wall (z=ft..H) + tab slots if enabled.
        Tab z-ranges are relative to the WALL extents (ft=bottom, H=top)."""
        wv, wf = self.build_trimmed_wall_mesh(ft, H)
        if wv is None or len(wf) == 0:
            return wv, wf
        if self._last_ig is None or self._last_ig.is_empty:
            return wv, wf
        if not (self.bot_tab and self.bot_proj > 0.01) and \
           not (self.top_tab and self.top_proj > 0.01):
            return wv, wf

        all_v = [wv]; all_f = [wf]; off = len(wv)

        def _append(tv, tf):
            nonlocal off
            if not tv or not tf: return
            ev = np.asarray(tv, dtype=np.float32)
            ef = np.asarray(tf, dtype=np.int32) + off
            all_v.append(ev); all_f.append(ef); off += len(ev)

        if self.bot_tab and self.bot_proj > 0.01:
            proj  = self.bot_proj
            tz0   = max(ft + 0.01, ft + self.bot_tab_z)   # ft + offset from wall bottom
            slot_h = max(proj * _ALT_TIRNAK_ASPECT + 1.0, proj * 1.2)
            tz1   = min(tz0 + slot_h, H * 0.98)
            ig_b  = self._ig_at_z((tz0 + tz1) / 2.0)
            _append(*self.tab_mesh(ig_b, proj, tz0, tz1, circular=True))

        if self.top_tab and self.top_proj > 0.01:
            proj  = self.top_proj
            tz1   = min(H * 0.99, H + self.top_tab_z)
            slot_h = max(proj * _UST_TIRNAK_ASPECT + 1.0, proj * 0.8)
            tz0   = max(ft + 0.01, tz1 - slot_h)
            ig_t  = self._ig_at_z((tz0 + tz1) / 2.0)
            _append(*self.tab_mesh(ig_t, proj, tz0, tz1, circular=True, profile=_UST_TIRNAK_PROFILE))

        return np.concatenate(all_v, axis=0), np.concatenate(all_f, axis=0)

    def build_mod1_combined_mesh(self):
        """Mod 1: boolean union of face (full polygon, z=0..ft) + wall ring (z=ft-ε..H).
        Single manifold mesh in PRINT coords (z_min=0). H = face_thickness + height_mm.
        Solves OrcaSlicer auto-drop and height mismatch in one shot."""
        if self._last_base is None or self._last_wg is None:
            return None, None
        import manifold3d as _m3d
        ft = self.face_thickness
        H  = ft + self.height_mm
        try:
            # Face: full letter polygon z=0..ft
            face_mf = self._geom_to_mf(self._last_base, 0.0, ft)
            if face_mf is None or face_mf.is_empty():
                return None, None
            # Wall: manifold3d ile extrude (MB.extrude_geom bypass — NM'siz garantili)
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(ft - 0.001, H)
            else:
                wall_mf = self._geom_to_mf(self._last_wg, ft - 0.001, H)
            if wall_mf is None or wall_mf.is_empty():
                return None, None
            combined = face_mf + wall_mf   # boolean union → one connected manifold
            if combined.is_empty():
                return None, None
            out = combined.to_mesh()
            v = np.array(out.vert_properties, dtype=np.float32)[:, :3]
            f = np.array(out.tri_verts, dtype=np.int32)
            return v, f
        except Exception:
            return None, None

    def build_face_mesh(self):
        """Build face plate mesh based on current face_mode and fill pattern.

        Solid-layer mode (face_fill_solid_layers=True):
          alt 0.8 mm → %100 solid  |  orta → desen  |  üst 0.8 mm → %100 solid
          Yalnızca face_thickness > 2.4 mm ve fill != 0 olduğunda aktif.
        """
        if self.face_mode == 0 or self._last_base is None or self.face_thickness <= 0:
            return None, None
        if self.face_mode in (1, 3):   # 3 = sadece yüz (duvar yok)
            geom = self._last_base
        else:
            if self._last_ig is None or self._last_ig.is_empty:
                return None, None
            geom = self._last_ig

        fill      = getattr(self, 'face_fill', 0)
        solid_pos = getattr(self, 'face_fill_solid_pos', 0)  # 0=off 1=front 2=back 3=both
        SOLID_D   = 0.8   # 2 × 0.4 mm standart katman yüksekliği
        ft        = self.face_thickness
        cell      = getattr(self, 'face_fill_cell', 8.0)
        wall      = getattr(self, 'face_fill_wall', 1.6)
        border    = getattr(self, 'face_fill_border', 1.5)

        has_front = solid_pos in (1, 3)
        has_back  = solid_pos in (2, 3)
        min_ft    = SOLID_D * (1 + (1 if has_front else 0) + (1 if has_back else 0) + 0.5)

        def _mf_to_arrays(mf):
            """manifold3d nesnesini (verts, faces) numpy array'e çevirir."""
            mesh = mf.to_mesh()
            _av = np.array(mesh.vert_properties, dtype=np.float32)[:, :3]
            _af = np.array(mesh.tri_verts,        dtype=np.int32)
            return _av, _af

        # ── Koruyucu solid katmanlar ──────────────────────────────────────────
        if fill and (has_front or has_back) and ft > min_ft:
            z0_mid   = SOLID_D if has_front else 0.0
            z1_mid   = ft - SOLID_D if has_back else ft
            mid_geom = _face_fill_pattern(geom, fill, cell, wall, border)

            sections = []
            if has_front:
                sections.append((geom,     0.0,          SOLID_D,  0))
            sections.append(   (mid_geom,  z0_mid,       z1_mid,   fill))
            if has_back:
                sections.append((geom,     ft - SOLID_D, ft,       0))

            combined = None
            for g, z0, z1, sec_fill in sections:
                if sec_fill:
                    mf = self._face_fill_mf(g, z0, z1, sec_fill, cell, wall, border)
                else:
                    mf = self._geom_to_mf(g, z0, z1)
                if mf and not mf.is_empty():
                    combined = mf if combined is None else combined + mf

            if combined is None or combined.is_empty():
                return None, None
            av, af = _mf_to_arrays(combined)

        # ── Normal mod: fill varsa _face_fill_mf (difference yok, NM=0 garantili) ──
        else:
            if fill:
                mf = self._face_fill_mf(geom, 0.0, ft, fill, cell, wall, border)
            else:
                mf = self._geom_to_mf(geom, 0.0, ft)
            if mf is None or mf.is_empty():
                return None, None
            av, af = _mf_to_arrays(mf)

        self.last_face_v = av; self.last_face_f = af
        return av, af

    def _clear_body_mesh(self):
        """Duvar/gövde GL öğesini temizle (cover ve face dokunulmaz)."""
        if self._wall_gl_item is not None:
            try:
                self.view.removeItem(self._wall_gl_item)
            except Exception:
                pass
            try:
                self.mesh_items.remove(self._wall_gl_item)
            except ValueError:
                pass
            self._wall_gl_item = None
        for x in self.line_items: self.view.removeItem(x)
        self.line_items.clear()
        for x in self._boundary_items: self.view.removeItem(x)
        self._boundary_items.clear()
        slot_idx = self.active_slot_idx
        self.scene_objects.pop(f"body_{slot_idx}", None)

    def _clear_face_mesh(self):
        """Mevcut yüz GL öğesini viewport ve listelerden temizle."""
        if self._face_gl_item is not None:
            try:
                self.view.removeItem(self._face_gl_item)
            except Exception:
                pass
            try:
                self.mesh_items.remove(self._face_gl_item)
            except ValueError:
                pass
            self._face_gl_item = None
        slot_idx = self.active_slot_idx
        self.scene_objects.pop(f"face_{slot_idx}", None)

    def apply_face_mesh(self, verts, faces):
        if verts is None or faces is None or len(faces) == 0:
            return
        self._clear_face_mesh()   # eski yüz mesh'ini temizle
        _df, _de, _sh = self._disp_params()
        _has_fill = getattr(self, 'face_fill', 0) != 0

        if _has_fill:
            # Honeycomb/Grid/Lines: yüzlerce küçük tüp yüzü var.
            # harden + smooth normal hesabı artefakt üretir → flat shading daha temiz.
            mesh = gl.MeshData(vertexes=np.asarray(verts, dtype=np.float32),
                               faces=np.asarray(faces, dtype=np.int32))
            smooth = False
        else:
            try:
                _hv, _hf = self._harden_cap_edges(verts, faces)
                _vn = self._smooth_side_normals(_hv, _hf)
                mesh = gl.MeshData(vertexes=_hv, faces=_hf)
                if hasattr(mesh, 'setVertexNormals'):
                    mesh.setVertexNormals(_vn)
                else:
                    mesh._vertexNormals = _vn
            except Exception:
                mesh = gl.MeshData(vertexes=verts, faces=faces)
            smooth = True

        item = gl.GLMeshItem(meshdata=mesh, smooth=smooth, drawFaces=_df, drawEdges=_de,
                             color=self.color_face, edgeColor=(0,0,0,1),
                             shader=_sh, glOptions="opaque")
        self.view.addItem(item); self.mesh_items.append(item)
        self._face_gl_item = item
        # Register in scene_objects so 3MF / STL export can include it separately
        _slot_idx = self.active_slot_idx
        _slot     = self.slots[_slot_idx] if self.slots else None
        _lbl      = (f"Önyüz {_slot_idx + 1}" if len(self.slots) > 1 else "Önyüz")
        self.scene_objects[f"face_{_slot_idx}"] = {
            "v":        np.asarray(verts, dtype=float),
            "f":        np.asarray(faces, dtype=int),
            "gl_item":  item,
            "color":    self.color_face,
            "label":    _lbl,
            "filename": _slot.filename if _slot else "",
            "obj_type": "Önyüz",
            "aabb":     self._compute_aabb(np.asarray(verts, dtype=float)),
            "slot_idx": _slot_idx,
            "visible":  False,
            "pending":  False,
        }
        if callable(self.on_object_list_changed):
            self.on_object_list_changed()

    def build_combined_mesh(self):
        """Single manifold mesh.

        Mod1 (Outer face, face_mode=1):
          • Full outer slab (base)  z=0..ft  — solid front face
          • Wall ring (wg)          z=ft..h  — behind the face
          • ig floor cap at z=ft   — seals channel floor

        Mod2 (Inner/embedded face, face_mode=2):
          • Wall ring (wg)          z=0..h   — full-height walls, surrounding everything
          • Inner face plate (ig)   z=0..ft  — embedded INSIDE the walls at the front
          • ig floor cap at z=ft   — seals channel floor at depth ft
          The face is flush with the front opening and recessed inside the wall shell.
        """
        if self._last_base is None or self._last_wg is None or self._last_ig is None:
            return None, None

        base = self._last_base
        wg   = self._last_wg
        ig   = self._last_ig
        h    = self.height_mm
        ft   = self.face_thickness
        has_face = self.face_mode in (1, 2) and ft > 0
        mod2 = (self.face_mode == 2) and (ig is not None) and (not ig.is_empty) and ft > 0

        av, af, off = [], [], 0

        def ap(v, f):
            nonlocal off
            if not v or not f: return
            av.extend(v)
            af.extend([(a + off, b + off, c + off) for a, b, c in f])
            off += len(v)

        if mod2:
            # ── Mod2: wall ring full height, inner face embedded at front ──────
            # 1) Wall ring 0..h (with bottom + top caps for the ring itself)
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(0.0, h)
                if wall_mf is not None:
                    _mesh = wall_mf.to_mesh()
                    import numpy as _np2
                    _va = _np2.array(_mesh.vert_properties, dtype=_np2.float32)
                    _fa = _np2.array(_mesh.tri_verts, dtype=_np2.int32)
                    v = [tuple(p) for p in _va]
                    f = [tuple(t) for t in _fa]
                else:
                    v, f = [], []
            else:
                v, f = MB.extrude_geom(wg, 0.0, h, cap_bottom=True, cap_top=True)
            ap(v, f)

            # 2) Inner face plate (ig) 0..ft — sits inside the wall opening
            v, f = MB.extrude_geom(ig, 0.0, ft, cap_bottom=True, cap_top=False)
            ap(v, f)

            # 3) ig floor cap at z=ft — seals the channel floor at depth ft
            for poly in _as_polys(ig, 0.01):
                cv2, cf2 = MB.cap(poly, ft, top=True)
                ap(cv2, cf2)

        elif has_face:
            # ── Mod1: full outer face slab + walls behind ──────────────────────
            # 1) Face plate (base) 0..ft
            v, f = MB.extrude_geom(base, 0.0, ft, cap_bottom=True, cap_top=False)
            ap(v, f)

            # 2) Body walls ft..h
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(ft, h)
                if wall_mf is not None:
                    _mesh = wall_mf.to_mesh()
                    import numpy as _np2
                    _va = _np2.array(_mesh.vert_properties, dtype=_np2.float32)
                    _fa = _np2.array(_mesh.tri_verts, dtype=_np2.int32)
                    v = [tuple(p) for p in _va]
                    f = [tuple(t) for t in _fa]
                else:
                    v, f = [], []
            else:
                v, f = MB.extrude_geom(wg, ft, h, cap_bottom=False, cap_top=True)
            ap(v, f)

            # 3) ig floor cap at z=ft — seals inner channel floor
            for poly in _as_polys(ig, 0.01):
                cv2, cf2 = MB.cap(poly, ft, top=True)
                ap(cv2, cf2)

        else:
            # ── No face (ft=0): plain wall ring only ───────────────────────────
            if self.wall_type != 0:
                wall_mf = self._build_wall_mf(0.0, h)
                if wall_mf is not None:
                    _mesh = wall_mf.to_mesh()
                    import numpy as _np2
                    _va = _np2.array(_mesh.vert_properties, dtype=_np2.float32)
                    _fa = _np2.array(_mesh.tri_verts, dtype=_np2.int32)
                    v = [tuple(p) for p in _va]
                    f = [tuple(t) for t in _fa]
                else:
                    v, f = [], []
            else:
                v, f = MB.extrude_geom(wg, 0.0, h, cap_bottom=True, cap_top=True)
            ap(v, f)

        # 4) Tabs (inside channel, above face plate)
        if ig is not None and not ig.is_empty:
            if self.bot_tab and self.bot_proj > 0.01:
                z0, z1 = self._tab_z_range('bot', ft=ft)
                ig_b = self._ig_at_z((z0 + z1) / 2.0)
                v, f = self.tab_mesh(ig_b, self.bot_proj, z0, z1, circular=True)
                ap(v, f)
            if self.top_tab and self.top_proj > 0.01:
                z0, z1 = self._tab_z_range('top', ft=ft)
                ig_t = self._ig_at_z((z0 + z1) / 2.0)
                v, f = self.tab_mesh(ig_t, self.top_proj, z0, z1, circular=True, profile=_UST_TIRNAK_PROFILE)
                ap(v, f)

        if not av or not af:
            return None, None
        return np.asarray(av, dtype=np.float32), np.asarray(af, dtype=np.int32)

    def export_combined_stl(self, filename):
        """Export body + face as a single integrated STL."""
        v, f = self.build_combined_mesh()
        if v is None or f is None:
            raise RuntimeError("Birleşik mesh oluşturulamadı.")
        cv, cf, info = cleanup(v, f)
        if len(cf) == 0:
            raise RuntimeError("Cleanup sonrası üçgen kalmadı.")
        tri = cv[cf]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n = (n / ln[:, None]).astype(np.float32)
        dt = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
        data = np.zeros(len(cf), dtype=dt)
        data["normal"] = n; data["vertices"] = tri.astype(np.float32)
        with open(filename, "wb") as fh:
            hdr = b"Harfex"[:80]
            fh.write(hdr + b" " * (80 - len(hdr)))
            np.array([len(cf)], dtype=np.uint32).tofile(fh)
            data.tofile(fh)
        return info

    # ── Color helpers for 3MF export ─────────────────────────────────────────
    @staticmethod
    def _rgba_to_hex(rgba):
        """Convert (r,g,b,a) 0-1 floats to #RRGGBB hex string."""
        r, g, b = (int(c * 255) for c in rgba[:3])
        return f"#{r:02X}{g:02X}{b:02X}"

    def _3mf_color_group_xml(self, group_id=10):
        """Return <m:colorgroup> XML block for wall+face colors."""
        c1 = self._rgba_to_hex(self.color_wall)
        c2 = self._rgba_to_hex(self.color_face)
        return (f'    <m:colorgroup id="{group_id}">\n'
                f'      <m:color color="{c1}"/>\n'   # index 0 → filament 1
                f'      <m:color color="{c2}"/>\n'   # index 1 → filament 2
                f'    </m:colorgroup>')

    def _build_model_cfg(self, obj_names):
        """Return Metadata/model_settings.config XML assigning extruders to objects.
        obj_names: list of (obj_id, name, extruder_number) tuples."""
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<config>']
        for obj_id, name, extruder in obj_names:
            lines.append(f'  <object id="{obj_id}" instances_count="1">')
            lines.append(f'    <metadata key="name" value="{name}"/>')
            lines.append(f'    <metadata key="extruder" value="{extruder}"/>')
            lines.append( '  </object>')
        lines.append('</config>')
        return '\n'.join(lines)

    def export_3mf(self, filename):
        """Export body + face as a single .3mf (two separate objects, same coordinate space)."""
        import zipfile

        if self.last_v is None or self.last_f is None or len(self.last_f) == 0:
            raise RuntimeError("Generate model first.")
        if self.last_face_v is None or self.last_face_f is None or len(self.last_face_f) == 0:
            raise RuntimeError("Face mesh is empty — set face mode and generate first.")

        cv, cf, body_info = cleanup(self.last_v, self.last_f)
        fv, ff, face_info = cleanup(self.last_face_v, self.last_face_f)
        if len(cf) == 0: raise RuntimeError("Body mesh empty after cleanup.")
        if len(ff) == 0: raise RuntimeError("Face mesh empty after cleanup.")

        def _obj_xml(obj_id, verts, faces, pindex):
            parts = [f'    <object id="{obj_id}" type="model" m:pid="10" m:pindex="{pindex}">',
                     '      <mesh>', '        <vertices>']
            for x, y, z in verts:
                parts.append(f'          <vertex x="{float(x):.6f}" y="{float(y):.6f}" z="{float(z):.6f}"/>')
            parts.append('        </vertices>')
            parts.append('        <triangles>')
            for a, b, c in faces:
                parts.append(f'          <triangle v1="{int(a)}" v2="{int(b)}" v3="{int(c)}"/>')
            parts += ['        </triangles>', '      </mesh>', '    </object>']
            return '\n'.join(parts)

        model = '\n'.join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<model unit="millimeter"'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
            ' xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
            '  <resources>',
            self._3mf_color_group_xml(10),    # colorgroup id=10
            _obj_xml(1, cv, cf, pindex=0),    # wall → filament 1
            _obj_xml(2, fv, ff, pindex=1),    # face → filament 2
            '  </resources>',
            '  <build>',
            '    <item objectid="1"/>',
            '    <item objectid="2"/>',
            '  </build>',
            '</model>',
        ])

        content_types = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            '  <Override PartName="/Metadata/model_settings.config" ContentType="application/xml"/>\n'
            '</Types>'
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '  <Relationship Target="/3D/3dmodel.model" Id="rel0"'
            ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>'
        )
        meta = self._build_model_cfg([
            (1, "Wall",  1),   # object 1 → extruder/filament 1
            (2, "Face",  2),   # object 2 → extruder/filament 2
        ])

        with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("3D/3dmodel.model", model)
            zf.writestr("Metadata/model_settings.config", meta)

        return body_info, face_info

    def export_mod1_3mf(self, filename, include_body=True, include_face=True, include_cover=False):
        """Mod 1: wall (z=ft..H) + face (z=0..ft) grouped as 3MF components.
        Components share a parent object → OrcaSlicer preserves relative Z positions,
        no auto-drop. Same structure Rhino uses for grouped export.
        include_body / include_face / include_cover control which parts are written."""
        import zipfile

        if not include_body and not include_face and not include_cover:
            raise RuntimeError("En az bir nesne seçili olmalı.")

        ft = self.face_thickness
        h  = self.height_mm
        H  = h + ft   # total height

        def _mesh_obj_xml(obj_id, verts, faces, pindex):
            parts = [f'    <object id="{obj_id}" type="model" m:pid="10" m:pindex="{pindex}">',
                     '      <mesh>', '        <vertices>']
            for x, y, z in verts:
                parts.append(f'          <vertex x="{float(x):.6f}" y="{float(y):.6f}" z="{float(z):.6f}"/>')
            parts += ['        </vertices>', '        <triangles>']
            for a, b, c in faces:
                parts.append(f'          <triangle v1="{int(a)}" v2="{int(b)}" v3="{int(c)}"/>')
            parts += ['        </triangles>', '      </mesh>', '    </object>']
            return '\n'.join(parts)

        objects_xml   = []
        settings_list = []
        component_ids = []
        next_id       = 1
        wall_info = face_info = cover_info = None

        # ── Wall (Gövde) ──
        if include_body:
            wv, wf = self.build_mod1_wall_with_tabs(ft, H)
            if wv is None or len(wf) == 0:
                raise RuntimeError("Duvar mesh oluşturulamadı.")
            cwv, cwf, wall_info = cleanup(wv, wf)
            if len(cwf) == 0:
                raise RuntimeError("Duvar mesh cleanup sonrası boş.")
            wall_id = next_id; next_id += 1
            objects_xml.append(_mesh_obj_xml(wall_id, cwv, cwf, pindex=0))
            settings_list.append((wall_id, "Gövde", 1))
            component_ids.append(wall_id)

        # ── Face (Önyüz) ──
        if include_face:
            if self.last_face_v is None or self.last_face_f is None or len(self.last_face_f) == 0:
                raise RuntimeError("Önyüz mesh yok — Generate'i tekrar çalıştırın.")
            cfv, cff, face_info = cleanup(self.last_face_v, self.last_face_f)
            if len(cff) == 0:
                raise RuntimeError("Önyüz mesh cleanup sonrası boş.")
            face_id = next_id; next_id += 1
            objects_xml.append(_mesh_obj_xml(face_id, cfv, cff, pindex=1))
            settings_list.append((face_id, "Önyüz", 2))
            component_ids.append(face_id)

        # ── Back cover (Arka Kapak) ──
        if include_cover:
            if self._last_base is None or self._last_ig is None:
                raise RuntimeError("Arka kapak için önce model oluşturun.")
            _ct  = float(self.cover_ct)
            _cwh = float(self.cover_wh)
            _cl  = float(self.cover_clearance)
            _wt  = float(self.cover_wt)
            base_geom  = self._base_at_z(self.height_mm)
            ig_back    = self._ig_at_z(self.height_mm)
            base_plate = self._geom_to_mf(base_geom, 0.0, _ct)
            if base_plate is None:
                raise RuntimeError("Arka kapak taban plakası oluşturulamadı.")
            tab_outer = ig_back.buffer(-_cl, join_style="mitre").buffer(0)
            tab_inner = tab_outer.buffer(-_wt, join_style="mitre").buffer(0)
            tab_geom  = tab_outer.difference(tab_inner) if not tab_inner.is_empty else tab_outer
            inner_wall = self._geom_to_mf(tab_geom, _ct, _ct + _cwh)
            if inner_wall is None:
                raise RuntimeError("Arka kapak iç tab oluşturulamadı.")
            cover_mf   = base_plate + inner_wall
            cov_mesh   = cover_mf.to_mesh()
            cov_v = np.array(cov_mesh.vert_properties, dtype=np.float32)[:, :3]
            cov_f = np.array(cov_mesh.tri_verts, dtype=np.int32)
            # Y-flip + same Y-offset as the viewport preview
            minx, miny, maxx, maxy = base_geom.bounds
            center_y = (miny + maxy) / 2.0
            cov_v[:, 1] = 2.0 * center_y - cov_v[:, 1]   # Y-flip
            cov_f = cov_f[:, [0, 2, 1]]                    # restore winding
            cov_v[:, 1] += (maxy - miny) + 5.0             # offset (viewport match)
            ccov_v, ccov_f, cover_info = cleanup(cov_v, cov_f)
            cover_id = next_id; next_id += 1
            objects_xml.append(_mesh_obj_xml(cover_id, ccov_v, ccov_f, pindex=0))
            settings_list.append((cover_id, "Arka Kapak", 3))
            component_ids.append(cover_id)

        # ── Group object (only when 2+ components) ──
        if len(component_ids) >= 2:
            group_id   = next_id
            comp_lines = [f'        <component objectid="{cid}"/>' for cid in component_ids]
            objects_xml.append('\n'.join([
                f'    <object id="{group_id}" type="model">',
                '      <components>',
                *comp_lines,
                '      </components>',
                '    </object>',
            ]))
            build_id = group_id
        else:
            build_id = component_ids[0]

        content_types = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            '  <Override PartName="/Metadata/model_settings.config" ContentType="application/xml"/>\n'
            '</Types>'
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '  <Relationship Target="/3D/3dmodel.model" Id="rel0"'
            ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>'
        )
        model = '\n'.join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<model unit="millimeter"'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
            ' xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
            '  <resources>',
            self._3mf_color_group_xml(10),
            *objects_xml,
            '  </resources>',
            '  <build>',
            f'    <item objectid="{build_id}"/>',
            '  </build>',
            '</model>',
        ])
        meta = self._build_model_cfg(settings_list)
        with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("3D/3dmodel.model", model)
            zf.writestr("Metadata/model_settings.config", meta)
        return wall_info, face_info

    def export_mode2_3mf(self, filename):
        """Mode 2: wall+tabs as one manifold3d boolean union, face plates separate.
        All items nm=0 guaranteed by manifold3d."""
        import zipfile
        import manifold3d as m3d

        if self._last_wg is None or self._last_ig is None:
            raise RuntimeError("Önce model oluşturun.")

        h  = self.height_mm
        ft = self.face_thickness
        ig = self._last_ig

        # ── Helper: self._geom_to_mf wrapper (tek CrossSection, nm=0 garantili) ──
        def _geom_to_mf(geom, z0, z1):
            return self._geom_to_mf(geom, z0, z1)

        # ── Helper: build tab manifold (replicates tab_mesh staircase) ──
        def _tab_mf(proj, z0_tab, z1_tab, ig_ref=None):
            ig_use  = ig_ref if ig_ref is not None else ig
            qs      = max(4, int(self.arc_sm))
            _gap    = 0.001
            _sep    = 0.004
            ramp_h  = min(proj, z1_tab - z0_tab)
            N       = max(4, min(10, int(round(ramp_h / 0.4))))
            step_w  = proj / N
            result  = None
            for k in range(N):
                za = z0_tab + ramp_h * k / N
                zb = z0_tab + ramp_h * (k + 1) / N
                oi = _gap + k * (step_w + _sep)
                ii = oi + step_w
                try:
                    outer = ig_use.buffer(-oi, quad_segs=qs, join_style="round").buffer(0)
                    if outer is None or outer.is_empty: continue
                    inner = ig_use.buffer(-ii, quad_segs=qs, join_style="round").buffer(0)
                    ring  = outer if (inner is None or inner.is_empty) \
                            else outer.difference(inner)
                    ring  = _clean(ring, 0.005)
                    if ring is None or ring.is_empty: continue
                    mf = _geom_to_mf(ring, za, zb)
                    if mf: result = mf if result is None else result + mf
                except Exception as ek:
                    print(f"[TAB k={k}] {ek}")
            # full-width block above ramp
            z_fw = z0_tab + ramp_h + 0.01
            if z1_tab > z_fw + 0.01:
                try:
                    outer = ig_use.buffer(-_gap, quad_segs=qs, join_style="round").buffer(0)
                    inner = ig_use.buffer(-(_gap + proj), quad_segs=qs, join_style="round").buffer(0)
                    if outer and not outer.is_empty:
                        ring = outer if (inner is None or inner.is_empty) \
                               else outer.difference(inner)
                        ring = _clean(ring, 0.005)
                        if ring and not ring.is_empty:
                            mf = _geom_to_mf(ring, z_fw, z1_tab)
                            if mf: result = mf if result is None else result + mf
                except Exception as ef:
                    print(f"[TAB full] {ef}")
            return result

        # ── 1. Wall manifold ──
        wall_mf = self._build_wall_mf(0.0, h)
        if wall_mf is None:
            raise RuntimeError("Duvar manifold oluşturulamadı.")

        # ── 2. Tab manifolds → boolean union with wall ──
        if ig is not None and not ig.is_empty:
            if self.bot_tab and self.bot_proj > 0.01:
                z0, z1 = self._tab_z_range('bot', ft=ft)
                tab = _tab_mf(self.bot_proj, z0, z1, self._ig_at_z((z0 + z1) / 2.0))
                if tab: wall_mf = wall_mf + tab
            if self.top_tab and self.top_proj > 0.01:
                z0, z1 = self._tab_z_range('top', ft=ft)
                tab = _tab_mf(self.top_proj, z0, z1, self._ig_at_z((z0 + z1) / 2.0))
                if tab: wall_mf = wall_mf + tab

        # ── 3. Convert wall+tab manifold → vertex/face arrays ──
        def _mf_to_vf(mf):
            mesh = mf.to_mesh()
            va = np.array(mesh.vert_properties, dtype=np.float32)
            fa = np.array(mesh.tri_verts, dtype=np.int32)
            return va, fa

        wall_va, wall_fa = _mf_to_vf(wall_mf)

        # ── 4. Face plate manifolds (separate items, ft > 0) ──
        face_items = []
        if ft > 0:
            ig_face = ig.buffer(-0.05, quad_segs=16, join_style="round").buffer(0)
            if ig_face is not None and not ig_face.is_empty:
                face_mf = _geom_to_mf(ig_face, 0.0, ft)
                if face_mf:
                    fva, ffa = _mf_to_vf(face_mf)
                    face_items.append((fva, ffa))

        # ── 5. Build 3MF ──
        def _mesh_xml(obj_id, va, fa, pindex):
            parts = [f'    <object id="{obj_id}" type="model" m:pid="10" m:pindex="{pindex}">',
                     '      <mesh>', '        <vertices>']
            for v in va:
                parts.append(f'          <vertex x="{float(v[0]):.6f}"'
                              f' y="{float(v[1]):.6f}" z="{float(v[2]):.6f}"/>')
            parts += ['        </vertices>', '        <triangles>']
            for t in fa:
                parts.append(f'          <triangle v1="{int(t[0])}"'
                              f' v2="{int(t[1])}" v3="{int(t[2])}"/>')
            parts += ['        </triangles>', '      </mesh>', '    </object>']
            return '\n'.join(parts)

        # wall=obj1 (filament 1), face=obj2+ (filament 2)
        all_items = [(wall_va, wall_fa, 0)] + [(v, f, 1) for v, f in face_items]
        resource_lines = [_mesh_xml(i + 1, v, f, pi) for i, (v, f, pi) in enumerate(all_items)]
        build_lines    = [f'    <item objectid="{i + 1}"/>' for i in range(len(all_items))]

        # OrcaSlicer extruder assignments
        orca_objs = [(1, "Wall", 1)] + [(i + 2, "Face", 2) for i in range(len(face_items))]
        meta = self._build_model_cfg(orca_objs)

        model = '\n'.join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<model unit="millimeter"'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
            ' xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
            '  <resources>',
            self._3mf_color_group_xml(10),
            '\n'.join(resource_lines),
            '  </resources>',
            '  <build>',
            '\n'.join(build_lines),
            '  </build>',
            '</model>',
        ])
        content_types = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            '  <Override PartName="/Metadata/model_settings.config" ContentType="application/xml"/>\n'
            '</Types>'
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '  <Relationship Target="/3D/3dmodel.model" Id="rel0"'
            ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>'
        )
        with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("3D/3dmodel.model", model)
            zf.writestr("Metadata/model_settings.config", meta)

        wall_info = {"out_f": int(len(wall_fa))}
        face_info = {"out_f": sum(len(f) for _, f in face_items)}
        return wall_info, face_info

    # ── Fill pattern: difference yerine direkt CrossSection ─────────────────
    def _face_fill_mf(self, geom, z0, z1, fill, cell, wall, border):
        """Honeycomb/grid/lines void'ları letter outline ile tek CrossSection'a koy.
        Shapely difference kullanmaz → touching edge yok → NM=0 garantili."""
        import manifold3d as m3d
        import math

        if fill == 0 or geom is None or geom.is_empty:
            return self._geom_to_mf(geom, z0, z1)

        # Letter outline contour'ları
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

        # Inner area (border offset)
        try:
            from shapely.geometry import Polygon as _Poly
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
            if fill == 1:   # Honeycomb
                r = cell * 0.5
                col_step = r * math.sqrt(3)
                row_step = r * 1.5
                r_void_h = (col_step - wall) / math.sqrt(3)
                r_void_v = (row_step - wall) / 2.0
                r_void   = min(r_void_h, r_void_v) - _EPS
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
                            from shapely.geometry import Polygon as _P
                            cell_p = _P(pts)
                            clipped = cell_p.intersection(inner)
                            if not clipped.is_empty and clipped.area > 0.01:
                                voids.append(clipped)
                            x += col_step
                        y += row_step
                        row_idx += 1

            elif fill == 2:  # Grid
                void_sz = max(0.1, cell - wall - _EPS * 2)
                period  = cell
                from shapely.geometry import Polygon as _P
                y = my0 + wall * 0.5
                while y <= my1:
                    x = mx0 + wall * 0.5
                    while x <= mx1:
                        cell_p = _P([(x, y), (x+void_sz, y), (x+void_sz, y+void_sz), (x, y+void_sz)])
                        clipped = cell_p.intersection(inner)
                        if not clipped.is_empty and clipped.area > 0.01:
                            voids.append(clipped)
                        x += period
                    y += period

            elif fill == 3:  # Lines
                void_w = max(0.1, cell - wall - _EPS * 2)
                period = cell
                from shapely.geometry import Polygon as _P
                x = mx0 + wall * 0.5
                while x <= mx1:
                    cell_p = _P([(x, my0), (x+void_w, my0), (x+void_w, my1), (x, my1)])
                    clipped = cell_p.intersection(inner)
                    if not clipped.is_empty and clipped.area > 0.01:
                        voids.append(clipped)
                    x += period
        except Exception:
            return self._geom_to_mf(geom, z0, z1)

        # Her void'u hole olarak ekle (EvenOdd: iç içe ring = delik)
        for v in voids:
            for vpoly in _as_polys(v, 0.005):
                ext = list(vpoly.exterior.coords)
                if ext[0] == ext[-1]: ext = ext[:-1]
                if len(ext) < 3: continue
                # Void contour'ları ters yönde ekle → EvenOdd'da delik olarak tanınır
                all_contours.append([[float(x), float(y)] for x, y in reversed(ext)])

        cs = m3d.CrossSection(all_contours, m3d.FillRule.EvenOdd)
        if cs.area() < 1e-6:
            return self._geom_to_mf(geom, z0, z1)
        mf = m3d.Manifold.extrude(cs, height=float(z1 - z0))
        if float(z0) != 0.0:
            mf = mf.translate([0.0, 0.0, float(z0)])
        return mf if not mf.is_empty() else None

    # ── Shared manifold3d helper ─────────────────────────────────────────────
    def _geom_to_mf(self, geom, z0, z1):
        """Tüm contour'ları tek CrossSection olarak extrude et — union yok, nm=0 garantili."""
        import manifold3d as m3d
        polys = _as_polys(geom, 0.01)
        if not polys:
            return None
        # Tüm polygon ve deliklerini tek contour listesine topla
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
        # Tek CrossSection → tek extrude → T-junction yok, manifold garantili
        cs = m3d.CrossSection(all_contours, m3d.FillRule.EvenOdd)
        if cs.area() < 1e-6:
            return None
        mf = m3d.Manifold.extrude(cs, height=float(z1 - z0))
        if float(z0) != 0.0:
            mf = mf.translate([0.0, 0.0, float(z0)])
        return mf if not mf.is_empty() else None

    def _wall_offset_at_z(self, z):
        """Return outward lean offset (mm) at height z for current wall_type."""
        if self.wall_type not in (1, 2):
            return 0.0
        profile = _ACILI_PROFILE if self.wall_type == 1 else _KAVISLI_PROFILE
        t = max(0.0, min(1.0, z / max(self.height_mm, 1.0)))
        return self._interp_profile(t, profile) * self.wall_taper

    def _ig_at_z(self, z):
        """Return inner guide offset to match angled/curved wall inner face at height z."""
        if self._last_ig is None: return self._last_ig
        o = self._wall_offset_at_z(z)
        if o < 0.001: return self._last_ig
        qs = max(4, int(self.arc_sm))
        adj = self._last_ig.buffer(o, quad_segs=qs, join_style='mitre').buffer(0)
        return adj if (adj is not None and not adj.is_empty) else self._last_ig

    def _base_at_z(self, z):
        """Return outer contour offset to match angled/curved wall outer face at height z."""
        if self._last_base is None: return self._last_base
        o = self._wall_offset_at_z(z)
        if o < 0.001: return self._last_base
        qs = max(4, int(self.arc_sm))
        adj = self._last_base.buffer(o, quad_segs=qs, join_style='mitre').buffer(0)
        return adj if (adj is not None and not adj.is_empty) else self._last_base

    @staticmethod
    def _interp_profile(t, profile):
        """Interpolate normalized profile at t ∈ [0,1]. Returns shape factor ∈ [0,1]."""
        if t <= profile[0][0]:  return profile[0][1]
        if t >= profile[-1][0]: return profile[-1][1]
        for i in range(len(profile) - 1):
            t0, s0 = profile[i]; t1, s1 = profile[i + 1]
            if t0 <= t <= t1:
                alpha = (t - t0) / (t1 - t0)
                return s0 + alpha * (s1 - s0)
        return 0.0

    def _profile_wall_mf(self, z0, z1, profile, max_lean):
        """Build tapered wall: buffer+mitre per slice, no scale_top (correct for all letter shapes)."""
        import manifold3d as m3d
        if self._last_base is None or self._last_ig is None: return None

        qs = max(4, int(self.arc_sm))
        h_span = float(z1 - z0)
        # ~0.6mm per slice: 1.6× finer than original — smoother without 40s freeze
        n = max(24, int(round(h_span / 0.6)))
        dz = h_span / n
        slices = []  # collect all slice manifolds first, then union via binary tree

        for i in range(n):
            t = (i + 0.5) / n                              # slice midpoint
            o = self._interp_profile(t, profile) * max_lean   # outward offset

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
        # Binary-tree union: merge pairs at each level so no single manifold grows
        # too large too early — much faster than linear left-accumulation.
        while len(slices) > 1:
            nxt = []
            for j in range(0, len(slices), 2):
                if j + 1 < len(slices):
                    nxt.append(slices[j] + slices[j + 1])
                else:
                    nxt.append(slices[j])
            slices = nxt
        return slices[0]

    def _curved_wall_mf(self, z0, z1):
        """Curved-tapered wall: sine-interpolated scale, arc_smoothness slices."""
        import manifold3d as m3d
        try:
            from shapely import affinity as _aff
        except ImportError:
            import shapely.affinity as _aff
        if self._last_base is None or self._last_ig is None: return None
        n = max(4, int(self.arc_sm))
        cx = float(self._last_base.centroid.x)
        cy = float(self._last_base.centroid.y)
        dz = (z1 - z0) / n
        result = None
        for i in range(n):
            t = (i + 0.5) / n
            s = 1.0 + self.wall_taper * math.sin(math.pi / 2.0 * t)
            outer_s = _aff.scale(self._last_base, xfact=s, yfact=s, origin=(cx, cy))
            inner_s = _aff.scale(self._last_ig,   xfact=s, yfact=s, origin=(cx, cy))
            ring_s  = outer_s.difference(inner_s) if not inner_s.is_empty else outer_s
            if ring_s is None or ring_s.is_empty: continue
            slice_mf = self._geom_to_mf(ring_s, z0 + i * dz, z0 + (i + 1) * dz)
            if slice_mf is None: continue
            result = slice_mf if result is None else result + slice_mf
        return result

    def _build_wall_mf(self, z0, z1):
        """Wall manifold dispatcher: straight / açılı / kavisli."""
        if self.wall_type == 1: return self._profile_wall_mf(z0, z1, _ACILI_PROFILE, self.wall_taper)
        if self.wall_type == 2: return self._profile_wall_mf(z0, z1, _KAVISLI_PROFILE, self.wall_taper)
        return self._geom_to_mf(self._last_wg, z0, z1)

    def export_back_cover_3mf(self, filename, cover_thickness=2.0, wall_height=15.0, clearance=0.05, wall_thickness=3.0):
        """Back cover: base plate (base polygon) + inner tab (ig shrunk by clearance).
        Tab fits inside main body walls with clearance gap.
        Exported alongside main body in same 3MF, offset 2mm to the side."""
        import zipfile, manifold3d as m3d

        if self._last_base is None or self._last_ig is None or self._last_wg is None:
            raise RuntimeError("Önce model oluşturun.")

        ct = float(cover_thickness)
        wh = float(wall_height)
        cl = float(clearance)
        wt = float(wall_thickness)

        # ── 1. Base plate = base (outer contour) extruded to ct ──
        base_plate = self._geom_to_mf(self._last_base, 0.0, ct)
        if base_plate is None:
            raise RuntimeError("Taban plakası oluşturulamadı.")

        # ── 2. Inner tab = hollow ring that fits inside body walls ──
        tab_outer = self._last_ig.buffer(-cl, join_style="mitre").buffer(0)
        tab_inner = tab_outer.buffer(-wt, join_style="mitre").buffer(0)
        tab_geom  = tab_outer.difference(tab_inner) if not tab_inner.is_empty else tab_outer
        if tab_geom is None or tab_geom.is_empty:
            raise RuntimeError("Tab geometrisi oluşturulamadı — clearance çok büyük olabilir.")
        inner_wall = self._geom_to_mf(tab_geom, ct, ct + wh)
        if inner_wall is None:
            raise RuntimeError("İç tab manifold oluşturulamadı.")

        cover_mf = base_plate + inner_wall

        # ── 3. Main body (wall + tabs) at original position ──
        h  = self.height_mm
        ft = self.face_thickness
        ig = self._last_ig

        def _tab_mf_local(proj, z0_tab, z1_tab):
            qs = max(4, int(self.arc_sm))
            _gap = 0.001; _sep = 0.004
            ramp_h = min(proj, z1_tab - z0_tab)
            N = max(4, min(10, int(round(ramp_h / 0.4))))
            step_w = proj / N
            result = None
            for k in range(N):
                za = z0_tab + ramp_h * k / N
                zb = z0_tab + ramp_h * (k + 1) / N
                oi = _gap + k * (step_w + _sep); ii = oi + step_w
                try:
                    outer = ig.buffer(-oi, quad_segs=qs, join_style="round").buffer(0)
                    if outer is None or outer.is_empty: continue
                    inner = ig.buffer(-ii, quad_segs=qs, join_style="round").buffer(0)
                    ring  = outer if (inner is None or inner.is_empty) else outer.difference(inner)
                    ring  = _clean(ring, 0.005)
                    if ring is None or ring.is_empty: continue
                    mf = self._geom_to_mf(ring, za, zb)
                    if mf: result = mf if result is None else result + mf
                except Exception as ek:
                    print(f"[TAB k={k}] {ek}")
            z_fw = z0_tab + ramp_h + 0.01
            if z1_tab > z_fw + 0.01:
                try:
                    outer = ig.buffer(-_gap, quad_segs=qs, join_style="round").buffer(0)
                    inner = ig.buffer(-(_gap + proj), quad_segs=qs, join_style="round").buffer(0)
                    if outer and not outer.is_empty:
                        ring = outer if (inner is None or inner.is_empty) else outer.difference(inner)
                        ring = _clean(ring, 0.005)
                        if ring and not ring.is_empty:
                            mf = self._geom_to_mf(ring, z_fw, z1_tab)
                            if mf: result = mf if result is None else result + mf
                except Exception as ef:
                    print(f"[TAB full] {ef}")
            return result

        body_mf = self._geom_to_mf(self._last_wg, 0.0, h)
        if body_mf is None:
            raise RuntimeError("Gövde manifold oluşturulamadı.")
        if ig is not None and not ig.is_empty:
            if self.bot_tab and self.bot_proj > 0.01:
                z0, z1 = self._tab_z_range('bot', ft=ft)
                tab = _tab_mf_local(self.bot_proj, z0, z1)
                if tab: body_mf = body_mf + tab
            if self.top_tab and self.top_proj > 0.01:
                z0, z1 = self._tab_z_range('top', ft=ft)
                tab = _tab_mf_local(self.top_proj, z0, z1)
                if tab: body_mf = body_mf + tab

        # ── 4. Offset cover 2mm to the right of body bbox ──
        minx, _, maxx, _ = self._last_base.bounds
        offset_x = (maxx - minx) + 2.0
        cover_mf = cover_mf.translate([offset_x, 0.0, 0.0])

        # ── 5. Mesh conversion ──
        def _mf_to_arrays(mf):
            mesh = mf.to_mesh()
            return (np.array(mesh.vert_properties, dtype=np.float32),
                    np.array(mesh.tri_verts, dtype=np.int32))

        body_va,  body_fa  = _mf_to_arrays(body_mf)
        cover_va, cover_fa = _mf_to_arrays(cover_mf)

        # ── 6. Build 3MF ──
        def _obj_xml(oid, va, fa):
            parts = [f'    <object id="{oid}" type="model">',
                     '      <mesh>', '        <vertices>']
            for v in va:
                parts.append(f'          <vertex x="{float(v[0]):.6f}"'
                              f' y="{float(v[1]):.6f}" z="{float(v[2]):.6f}"/>')
            parts += ['        </vertices>', '        <triangles>']
            for t in fa:
                parts.append(f'          <triangle v1="{int(t[0])}"'
                              f' v2="{int(t[1])}" v3="{int(t[2])}"/>')
            parts += ['        </triangles>', '      </mesh>', '    </object>']
            return '\n'.join(parts)

        model = '\n'.join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
            '  <resources>',
            _obj_xml(1, body_va,  body_fa),
            _obj_xml(2, cover_va, cover_fa),
            '  </resources>',
            '  <build>',
            '    <item objectid="1"/>',
            '    <item objectid="2"/>',
            '  </build>',
            '</model>',
        ])
        ct_xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                  '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                  '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                  '</Types>')
        rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                '  <Relationship Target="/3D/3dmodel.model" Id="rel0"'
                ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
                '</Relationships>')
        with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", ct_xml)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("3D/3dmodel.model", model)

        return {"body_f": int(len(body_fa)), "cover_f": int(len(cover_fa))}

    def export_back_cover_stl(self, filename, cover_thickness=2.0, wall_height=15.0,
                               clearance=0.05, wall_thickness=3.0):
        """Export back cover as single-piece STL with Y-flip for correct assembly orientation."""
        import struct
        ct = float(cover_thickness); wh = float(wall_height)
        cl = float(clearance);       wt = float(wall_thickness)

        if self._last_base is None or self._last_ig is None:
            raise RuntimeError("Önce model oluşturun.")

        # Use back-face geometry (z=h) so cover fits the expanded wall opening
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

        # Y-flip for correct assembly orientation (same as viewport preview)
        minx, miny, maxx, maxy = base_geom.bounds
        center_y = (miny + maxy) / 2.0
        va[:, 1] = 2.0 * center_y - va[:, 1]
        fa = fa[:, [0, 2, 1]]

        tri = va[fa]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n = (n / ln[:, None]).astype(np.float32)

        dt = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
        data = np.zeros(len(fa), dtype=dt)
        data["normal"] = n; data["vertices"] = tri.astype(np.float32)
        with open(filename, "wb") as f:
            hdr = b"Harfex"[:80]
            f.write(hdr + b" " * (80 - len(hdr)))
            np.array([len(fa)], dtype=np.uint32).tofile(f)
            data.tofile(f)

        nm = cover_mf.num_prop_vert() - cover_mf.num_vert() if hasattr(cover_mf, 'num_prop_vert') else 0
        return {"cover_f": int(len(fa)), "nm": 0}

    def export_face_stl(self, filename):
        if self.last_face_v is None or self.last_face_f is None or len(self.last_face_f) == 0:
            raise RuntimeError("Face mesh is empty — set a face mode and generate model first.")
        cv, cf, info = cleanup(self.last_face_v, self.last_face_f)
        if len(cf) == 0:
            raise RuntimeError("No triangles remain after cleanup.")
        tri = cv[cf]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1); ln[ln < 1e-12] = 1.0
        n = (n / ln[:, None]).astype(np.float32)
        dt = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
        data = np.zeros(len(cf), dtype=dt)
        data["normal"] = n; data["vertices"] = tri.astype(np.float32)
        with open(filename, "wb") as fh:
            hdr = b"Harfex"[:80]
            fh.write(hdr + b" " * (80 - len(hdr)))
            np.array([len(cf)], dtype=np.uint32).tofile(fh)
            data.tofile(fh)
        return info

    def export_selected_3mf(self, filename, keys):
        """Export selected scene objects as a multi-object 3MF.

        keys  — ordered list of scene_objects keys to include (e.g. ['body','cover'])
        Each object gets its own filament index so OrcaSlicer can assign separate colors.
        """
        import zipfile

        # ── Gather geometry ───────────────────────────────────────────────────
        objects_data = []
        for key in keys:
            obj = self.scene_objects.get(key)
            if obj is None:
                raise RuntimeError(f"'{key}' sahnede bulunamadı.")
            v = obj.get("v"); f = obj.get("f")
            if v is None or f is None or len(f) == 0:
                raise RuntimeError(f"'{obj.get('label', key)}' geometrisi boş.")
            cv, cf, _ = cleanup(np.asarray(v, dtype=np.float32),
                                np.asarray(f, dtype=np.int32))
            if len(cf) == 0:
                raise RuntimeError(f"'{obj.get('label', key)}' cleanup sonrası boş.")
            objects_data.append({
                "label": obj.get("label", key),
                "v": cv, "f": cf,
                "color": obj.get("color", (0.5, 0.5, 0.5, 1.0)),
            })

        if not objects_data:
            raise RuntimeError("Seçili nesnelerde dışa aktarılabilir geometri yok.")

        # ── XML helpers ───────────────────────────────────────────────────────
        def _obj_xml(obj_id, verts, faces, pindex):
            lines = [
                f'    <object id="{obj_id}" type="model"'
                f' m:pid="10" m:pindex="{pindex}">',
                '      <mesh>', '        <vertices>',
            ]
            for x, y, z in verts:
                lines.append(
                    f'          <vertex x="{float(x):.6f}"'
                    f' y="{float(y):.6f}" z="{float(z):.6f}"/>'
                )
            lines += ['        </vertices>', '        <triangles>']
            for a, b, c in faces:
                lines.append(
                    f'          <triangle v1="{int(a)}" v2="{int(b)}" v3="{int(c)}"/>'
                )
            lines += ['        </triangles>', '      </mesh>', '    </object>']
            return '\n'.join(lines)

        def _color_hex(rgba):
            r, g, b = (int(c * 255) for c in rgba[:3])
            return f"#{r:02X}{g:02X}{b:02X}"

        # ── Color group ───────────────────────────────────────────────────────
        colors_xml = ['    <m:colorgroup id="10">']
        for od in objects_data:
            colors_xml.append(f'      <m:color color="{_color_hex(od["color"])}"/>')
        colors_xml.append('    </m:colorgroup>')

        # ── Model XML ─────────────────────────────────────────────────────────
        resources = ['  <resources>'] + colors_xml
        build_items = []
        for idx, od in enumerate(objects_data):
            obj_id = idx + 1
            resources.append(_obj_xml(obj_id, od["v"], od["f"], pindex=idx))
            build_items.append(f'    <item objectid="{obj_id}"/>')
        resources.append('  </resources>')

        model = '\n'.join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<model unit="millimeter"'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
            ' xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
        ] + resources + ['  <build>'] + build_items + ['  </build>', '</model>'])

        # ── OrcaSlicer model_settings.config ──────────────────────────────────
        obj_entries = []
        for idx, od in enumerate(objects_data):
            obj_entries.append((idx + 1, od["label"], idx + 1))   # (id, name, filament)
        meta = self._build_model_cfg(obj_entries)

        content_types = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels"'
            ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="model"'
            ' ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            '  <Override PartName="/Metadata/model_settings.config"'
            ' ContentType="application/xml"/>\n'
            '</Types>'
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships'
            ' xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '  <Relationship Target="/3D/3dmodel.model" Id="rel0"'
            ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>'
        )

        with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("3D/3dmodel.model", model)
            zf.writestr("Metadata/model_settings.config", meta)

        return {od["label"]: len(od["f"]) for od in objects_data}

    def export_foam(self, filename, offset=0.0, corner_radius=0.0):
        """Export 2D outer contour as DXF for back-foam cutting.
        Straight wall  → base contour (z=0).
        Tapered wall   → wide-end contour (z=H, full taper offset) because the foam
                         is cut to cover the wide opening of the tapered letter.
        offset         → additional inward (−) or outward (+) buffer in mm.
        corner_radius  → round sharp corners by this radius (mm); 0 = no rounding."""
        if self._last_base is None or self._last_base.is_empty:
            raise RuntimeError("No base geometry — generate model first.")
        if not ezdxf:
            raise RuntimeError("ezdxf not installed: pip install ezdxf")
        qs = max(8, int(self.arc_sm) * 2)
        # For tapered/curved walls use the wide end (z=H); straight walls use base directly.
        if self.wall_type != 0:
            foam_geom = self._base_at_z(self.height_mm)
            if foam_geom is None or foam_geom.is_empty:
                foam_geom = self._last_base
        else:
            foam_geom = self._last_base
        # Apply user offset
        if abs(offset) > 0.001:
            foam_geom = foam_geom.buffer(offset, quad_segs=qs, join_style='mitre').buffer(0)
            if foam_geom is None or foam_geom.is_empty:
                raise RuntimeError("Offset çok büyük — kontur çöktü.")
        # Round corners: erode then dilate (morphological opening)
        if corner_radius > 0.001:
            foam_geom = foam_geom.buffer(-corner_radius, quad_segs=qs, join_style='round').buffer(0)
            if foam_geom is None or foam_geom.is_empty:
                raise RuntimeError("Köşe yarıçapı çok büyük — kontur çöktü.")
            foam_geom = foam_geom.buffer(corner_radius, quad_segs=qs, join_style='round').buffer(0)
        doc = ezdxf.new("R2010"); msp = doc.modelspace()
        for poly in _as_polys(foam_geom, 0.01):
            pts = [(float(x), float(y), 0.0) for x, y in poly.exterior.coords]
            msp.add_lwpolyline(pts, close=True)
            for hole in poly.interiors:
                pts_h = [(float(x), float(y), 0.0) for x, y in hole.coords]
                msp.add_lwpolyline(pts_h, close=True)
        doc.saveas(filename)

    def export_plexiglas(self, filename, fmt="dxf", mode="kanal", corner_radius=0.0):
        """Export 2D cutting contour for plexiglas.
        mode='kanal'  → inner guide (ig) contour — plexiglas fits INSIDE the channel wall.
        mode='kapak'  → outer base contour — plexiglas covers the OUTSIDE (lid/cover style).
        Tapered walls: kapak uses wide-end (z=H) outer contour; kanal uses ig as-is.
        corner_radius → round sharp corners by this radius (mm); 0 = no rounding."""
        qs = max(8, int(self.arc_sm) * 2)
        if mode == "kapak":
            # Cover/lid: outer contour — wide end for tapered walls
            if self._last_base is None or self._last_base.is_empty:
                raise RuntimeError("No base geometry — generate model first.")
            if self.wall_type != 0:
                base_geom = self._base_at_z(self.height_mm)
                if base_geom is None or base_geom.is_empty:
                    base_geom = self._last_base
            else:
                base_geom = self._last_base
            contour = base_geom.buffer(self.plexiglas_offset, quad_segs=qs, join_style="mitre").buffer(0)
        else:
            # Kanal: inner guide contour — plexiglas sits inside the channel
            if self._last_ig is None or self._last_ig.is_empty:
                raise RuntimeError("No inner geometry — generate model first.")
            contour = self._last_ig.buffer(self.plexiglas_offset, quad_segs=qs, join_style="round").buffer(0)
        if contour is None or contour.is_empty:
            raise RuntimeError("Offset çok büyük — kontur çöktü.")
        # Round corners: morphological opening (erode + dilate)
        if corner_radius > 0.001:
            contour = contour.buffer(-corner_radius, quad_segs=qs, join_style='round').buffer(0)
            if contour is None or contour.is_empty:
                raise RuntimeError("Köşe yarıçapı çok büyük — kontur çöktü.")
            contour = contour.buffer(corner_radius, quad_segs=qs, join_style='round').buffer(0)

        def _rings(geom):
            polys = _as_polys(geom, 0.01)
            rings = []
            for p in polys:
                rings.append(list(p.exterior.coords))
                for h in p.interiors:
                    rings.append(list(h.coords))
            return rings

        rings = _rings(contour)

        if fmt == "dxf":
            if not ezdxf:
                raise RuntimeError("ezdxf not installed: pip install ezdxf")
            doc = ezdxf.new("R2010"); msp = doc.modelspace()
            for r in rings:
                pts = [(float(x), float(y), 0.0) for x, y in r]
                msp.add_lwpolyline(pts, close=True)
            doc.saveas(filename)

        elif fmt == "svg":
            xs = [x for r in rings for x, y in r]
            ys = [y for r in rings for x, y in r]
            if not xs: raise RuntimeError("Empty contour.")
            mx2, my2 = min(xs), min(ys); Mx, My = max(xs), max(ys)
            W, H = Mx - mx2 or 1, My - my2 or 1
            pad = max(W, H) * 0.05
            vb = f"{mx2-pad:.3f} {my2-pad:.3f} {W+2*pad:.3f} {H+2*pad:.3f}"
            lines = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">']
            for r in rings:
                d = "M " + " L ".join(f"{x:.4f},{y:.4f}" for x, y in r) + " Z"
                lines.append(f'  <path d="{d}" fill="none" stroke="black" stroke-width="0.5"/>')
            lines.append("</svg>")
            with open(filename, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))

        elif fmt == "eps":
            xs = [x for r in rings for x, y in r]
            ys = [y for r in rings for x, y in r]
            if not xs: raise RuntimeError("Empty contour.")
            mx2, my2 = min(xs), min(ys); Mx, My = max(xs), max(ys)
            lines = [
                "%!PS-Adobe-3.0 EPSF-3.0",
                f"%%BoundingBox: {int(mx2)-2} {int(my2)-2} {int(Mx)+2} {int(My)+2}",
                "%%EndComments", "0 setlinewidth", "newpath"
            ]
            for r in rings:
                if not r: continue
                lines.append(f"{r[0][0]:.4f} {r[0][1]:.4f} moveto")
                for x, y in r[1:]:
                    lines.append(f"{x:.4f} {y:.4f} lineto")
                lines.append("closepath")
            lines += ["stroke", "%%EOF"]
            with open(filename, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))

    def draw_lines(self, geom, z=0.0, color=(0, 0, 1, 1), width=2.0):
        def dc(c):
            p = np.array([[x, y, z] for x, y in c], dtype=float)
            if len(p) < 2: return
            item = gl.GLLinePlotItem(pos=p, color=color, width=width,
                                     antialias=True, mode="line_strip")
            self.view.addItem(item); self.mesh_items.append(item)

        if geom is None or geom.is_empty: return
        t = geom.geom_type
        if t == "LineString":         dc(list(geom.coords))
        elif t == "MultiLineString":  [dc(list(g.coords)) for g in geom.geoms]
        elif t == "GeometryCollection":[self.draw_lines(g, z, color, width) for g in geom.geoms]

    def toggle_mx(self):
        self.mx = not self.mx
        for n in ("paths", "base_paths"):
            a = getattr(self, n)
            setattr(self, n, [{"points": [(-x, y) for x, y in p["points"]], "closed": p["closed"]} for p in a])
        self._redraw()

    def toggle_my(self):
        self.my = not self.my
        # X-mirror: harf dik durur, sağ-sol ayna (kanal harf üretim standardı)
        for n in ("paths", "base_paths"):
            a = getattr(self, n)
            setattr(self, n, [{"points": [(-x, y) for x, y in p["points"]], "closed": p["closed"]} for p in a])
        self._redraw()

    def _redraw(self):
        if not self.paths: return
        self.clear_model()
        if self.has_model: self.build_and_apply(self.wall_mm, self.height_mm)
        else: self.show_2d()

    def build_and_apply(self, w, h, fast=True):
        # z-flip uygulanmaz — mesh yönelimi üreticide zaten doğru (face=z=0, duvar yukarı)
        v, f, b = self.build_mesh_data(w, h, fast)
        if self.face_mode in (1, 2) and self.face_thickness > 0:
            cv, cf = self.build_combined_mesh()
            if cv is not None:
                self.apply_mesh(cv, cf, b)
                return
        self.apply_mesh(v, f, b)

    def build_all_data(self, w, h, fast=True):
        """Run ALL heavy computation in background thread; returns data ready for apply_mesh.
        Returns (verts, faces, boundary, use_combined) — no GL calls made here.
        z-flip uygulanmaz — mesh yönelimi üreticide zaten doğru (face=z=0)."""
        v, f, b = self.build_mesh_data(w, h, fast)
        if self.face_mode in (1, 2) and self.face_thickness > 0:
            cv, cf = self.build_combined_mesh()
            if cv is not None:
                return cv, cf, b, True   # combined mesh ready
        # Plain wall — no flip needed
        return v, f, b, False

# ── UI ────────────────────────────────────────────────────────────────────────
class LeftPanel(QWidget):
    """Left panel — pixel-accurate layout from sol inaktif plan.svg
    Scale: 268px / 247.829mm = 1.0814 px/mm
    All positions computed from SVG subpath bounding-boxes.
    """

    # ── Style constants (kept for external reference compatibility) ────────
    _BTN = (
        "QPushButton{background:#C9E0F7;color:#1a2533;border:1px solid #88aacc;"
        "border-radius:3px;font-size:7px;font-weight:bold;}"
        "QPushButton:hover{background:#a8d0ef;border-color:#5588bb;}"
        "QPushButton:pressed{background:#7ab0d8;}"
    )
    _BTN_ORG = (
        "QPushButton{background:#FF6600;color:white;border:none;"
        "border-radius:4px;font-size:14px;font-weight:bold;}"
        "QPushButton:hover{background:#ff8833;}"
    )
    _BTN_DARK = (
        "QPushButton{background:#4a5060;color:#dde;border:1px solid #666;"
        "border-radius:4px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#5a6070;color:white;}"
    )
    _BTN_BLUE = (
        "QPushButton{background:#336699;color:white;border:none;"
        "border-radius:3px;font-size:7px;font-weight:bold;}"
        "QPushButton:hover{background:#4477aa;}"
    )
    _BTN_GREEN = (
        "QPushButton{background:#2a4a2a;color:#8dba8d;"
        "border:1px solid #3a6a3a;border-radius:3px;font-size:7px;}"
        "QPushButton:hover{background:#336633;color:white;}"
    )
    _INPUT = (                            # kept for compatibility
        "QLineEdit{background:white;color:#111;border:1px solid #aaa;"
        "border-radius:2px;font-size:8px;font-weight:bold;}"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(406)
        self.setObjectName("leftPanel")

        from PySide6.QtWidgets import QScrollArea
        sa = QScrollArea()
        sa.setWidgetResizable(False)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        sa.setStyleSheet(
            "QScrollArea{border:none;background:#333333;}"
            "QScrollBar:vertical{width:6px;background:#222;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#555;border-radius:3px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        # Inner widget: 406 × 820 px — coordinates from butonların tamamı.svg
        # Scale: 107.311mm / 1502 SVG units × 96px/25.4mm = 0.2700 px/unit
        inner = QWidget()
        inner.setFixedSize(406, 1020)
        inner.setStyleSheet("background:#333333;")
        self._build(inner)
        sa.setWidget(inner)

        lo = QVBoxLayout(self)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(0)
        lo.addWidget(sa)

    # ── Objects section refresh ───────────────────────────────────────────────
    def refresh_objects(self, scene_objects, on_select=None):
        """Rebuild the checkbox list from current scene_objects dict.
        on_select(key): optional callback to highlight object in 3D view."""
        # Remove old widgets
        for w in self.obj_checks.findChildren(QCheckBox):
            w.setParent(None); w.deleteLater()
        for w in self.obj_checks.findChildren(QPushButton):
            w.setParent(None); w.deleteLater()

        _CB_READY = (
            "QCheckBox{color:#cccccc;font-size:11px;"
            "font-family:'Segoe UI',Arial;background:transparent;spacing:6px;}"
            "QCheckBox::indicator{width:14px;height:14px;border-radius:3px;}"
            "QCheckBox::indicator:checked{background:#ffcc22;"
            "border:1px solid #ffdd55;border-radius:3px;}"
            "QCheckBox::indicator:unchecked{background:#2a2a2a;"
            "border:1px solid #555;border-radius:3px;}"
        )
        _CB_PENDING = (
            "QCheckBox{color:#777777;font-size:11px;"
            "font-family:'Segoe UI',Arial;background:transparent;spacing:6px;}"
            "QCheckBox::indicator{width:14px;height:14px;border-radius:3px;}"
            "QCheckBox::indicator:checked{background:#335577;"
            "border:1px solid #446688;border-radius:3px;}"
            "QCheckBox::indicator:unchecked{background:#222;"
            "border:1px solid #444;border-radius:3px;}"
        )

        def _sort_key(k):
            if k.startswith("body_"):  return (0, k)
            if k.startswith("face_"):  return (1, k)
            if k.startswith("cover_"): return (2, k)
            return (3, k)

        y = 0
        for key in sorted(scene_objects.keys(), key=_sort_key):
            obj     = scene_objects[key]
            fname   = obj.get("filename", "")
            otype   = obj.get("obj_type", obj.get("label", key))
            visible = obj.get("visible", True)
            pending = obj.get("pending", False)

            # Label: "filename  •  type  [G]  (…)" if pending
            short_fname = fname if len(fname) <= 20 else fname[:17] + "…"
            if short_fname:
                display = f"{short_fname}  •  {otype}"
            else:
                display = otype
            gid = obj.get("group_id", "")
            if gid:
                display += f"  [{gid}]"
            if pending:
                display += "  (…)"

            # "◉" select button — click to highlight in 3D view
            if on_select is not None and not pending:
                _SEL_BTN_S = (
                    "QPushButton{background:transparent;color:#667;border:none;"
                    "font-size:12px;padding:0;}"
                    "QPushButton:hover{color:#ffd700;}"
                )
                sbtn = QPushButton("◉", self.obj_checks)
                sbtn.setGeometry(2, y + 1, 20, 20)
                sbtn.setToolTip(f"3D görünümde seç: {display}")
                sbtn.setStyleSheet(_SEL_BTN_S)
                sbtn.clicked.connect(
                    (lambda k: lambda: on_select(k))(key)
                )
                sbtn.show()
                cb_x, cb_w = 24, 352
            else:
                cb_x, cb_w = 4, 372

            cb = QCheckBox(display, self.obj_checks)
            cb.setGeometry(cb_x, y, cb_w, 22)
            cb.setChecked(visible)
            cb.setStyleSheet(_CB_PENDING if pending else _CB_READY)
            # Pending entries: checkbox disabled (no mesh to hide yet)
            cb.setEnabled(not pending)
            cb.show()

            if not pending:
                _SEL = (1.0, 0.80, 0.15, 1.0)   # gold — same as mouse-click selection color
                _base_color = obj.get("color", (1.0, 1.0, 1.0, 1.0))
                _gi = obj.get("gl_item")

                # Apply initial state right now so scene matches the checkbox
                if _gi is not None:
                    _gi.setVisible(True)           # never hide
                    if visible:
                        obj["check_color"] = _SEL
                        _gi.setColor(_SEL)
                    else:
                        obj.pop("check_color", None)
                        _gi.setColor(_base_color)

                def _make_toggle(o, gi, base_col):
                    _gold = (1.0, 0.80, 0.15, 1.0)
                    def _toggle(checked):
                        o["visible"] = checked      # used by export / 3MF logic
                        if gi is not None:
                            gi.setVisible(True)     # always shown
                            if checked:
                                o["check_color"] = _gold
                                gi.setColor(_gold)  # gold = ticked / included
                            else:
                                o.pop("check_color", None)
                                gi.setColor(base_col)  # normal = unticked
                    return _toggle
                cb.toggled.connect(_make_toggle(obj, obj.get("gl_item"), _base_color))

            y += 26

    # ── Absolute-position helpers ─────────────────────────────────────────────
    def _btn(self, parent, text, x, y, w, h, style, fs=7):
        b = QPushButton(text, parent)
        b.setGeometry(x, y, w, h)
        b.setStyleSheet(style)
        b.setFont(QFont("Arial", fs, QFont.Bold))
        return b

    def _lbl(self, parent, text, x, y, w, h, style=""):
        l = QLabel(text, parent)
        l.setGeometry(x, y, w, h)
        l.setAlignment(Qt.AlignCenter)
        l.setStyleSheet(style or "background:transparent;color:#aaa;font-size:7px;")
        return l

    def _inp(self, parent, val, x, y, w, h):
        i = QLineEdit(val, parent)
        i.setGeometry(x, y, w, h)
        i.setAlignment(Qt.AlignCenter)
        i.setStyleSheet(self._INPUT)
        i.setFont(QFont("Arial", 8, QFont.Bold))
        return i

    def _build(self, p):
        # All coordinates from butonların tamamı.svg
        # Scale: 107.311mm / 1502 SVG units × 96px / 25.4mm = 0.2700 px/SVG-unit
        W = 406

        # ══ SVG BACKGROUND ════════════════════════════════════════════════════
        # Render the full SVG as a background QLabel — icons, labels, zones all from SVG
        import os
        _here = os.path.dirname(os.path.abspath(
            globals().get('__file__', os.path.join(os.getcwd(), 'main.py'))
        ))
        svg_path = os.path.join(_here, "butonların tamamı.svg")
        if HAS_SVG and os.path.exists(svg_path):
            renderer = QSvgRenderer(svg_path)
            pix = QPixmap(406, 820)
            pix.fill(Qt.transparent)
            paint = QPainter(pix)
            paint.setRenderHint(QPainter.Antialiasing)
            paint.setRenderHint(QPainter.SmoothPixmapTransform)
            renderer.render(paint)
            paint.end()
            bg = QLabel(p)
            bg.setGeometry(0, 0, 406, 820)
            bg.setPixmap(pix)
            bg.lower()   # push behind all interactive widgets
        else:
            # Fallback: draw basic zone backgrounds if SVG missing
            for geom, color in [
                ((0, 0, 167, 37),    "#CCCC33"),
                ((167, 0, 239, 37),  "#666666"),
                ((0, 37, 406, 41),   "#336699"),
                ((0, 78, 406, 114),  "#2a2f3a"),
                ((0, 192, 406, 53),  "#4D4D4D"),
                ((0, 245, 406, 540), "#333333"),
                ((0, 785, 406, 35),  "#222222"),
            ]:
                f = QFrame(p); f.setGeometry(*geom)
                f.setStyleSheet(f"background:{color};")

        # ── Transparent hover style for all button overlays ───────────────────
        _T = (
            "QPushButton{background:transparent;border:none;}"
            "QPushButton:hover{background:rgba(255,255,255,55);border-radius:5px;}"
            "QPushButton:pressed{background:rgba(0,0,0,70);border-radius:5px;}"
        )

        # ══ INTERACTIVE OVERLAYS ══════════════════════════════════════════════

        # ── Import / Mirror: x=31,92  y=118  w=42  h=40 ──────────────────────
        self.import_btn = self._btn(p, "", 31, 118, 42, 40, _T, 9)

        # Mirror F SVG icon — created BEFORE button so button stays on top
        # Active state: F flipped horizontally (mirrored). Inactive: normal F.
        # Mirror F icon — drawn programmatically for clean rendering at any size.
        # F shape polygon scaled from SVG viewBox 0 0 6 8  (uniform 4x → 24×32 px)
        self._f_pix_normal = None
        self._f_pix_mirror = None
        self._mirror_icon  = None
        _IW, _IH = 16, 22           # smaller F icon
        def _make_f_pix(mirrored=False):
            pix = QPixmap(_IW, _IH); pix.fill(Qt.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(40, 40, 40))      # dark gray — visible on lighter button bg
            # F polygon: original coords in 6×8 grid, scaled to IW×IH
            sx, sy = _IW / 6, _IH / 8
            raw = [(round(x * sx), round(y * sy)) for x, y in
                   [(0,8),(0,0),(6,0),(6,1),(2,1),(2,3),(5,3),(5,5),(2,5),(2,8)]]
            if mirrored:
                raw = [(_IW - x, y) for x, y in raw]
            path = QPainterPath()
            path.moveTo(raw[0][0], raw[0][1])
            for xp, yp in raw[1:]:
                path.lineTo(xp, yp)
            path.closeSubpath()
            painter.drawPath(path)
            painter.end()
            return pix
        self._f_pix_normal = _make_f_pix(False)
        self._f_pix_mirror = _make_f_pix(True)
        # Icon label — centred on mirror button (x=92,y=118,w=42,h=40)
        _lx = 92 + (42 - _IW) // 2
        _ly = 118 + (40 - _IH) // 2
        self._mirror_icon = QLabel(p)
        self._mirror_icon.setGeometry(_lx, _ly, _IW, _IH)
        self._mirror_icon.setPixmap(self._f_pix_normal)
        self._mirror_icon.setStyleSheet("background:transparent;")

        # Mirror button — transparent click area on top of icon
        self.mr2 = self._btn(p, "", 92, 118, 42, 40, _T, 9)

        # ── Arc / Round / Simp inputs ─────────────────────────────────────────
        # Enlarged: w=52 h=22 font=11  — centred in the 40px button row (y=118)
        # x positions keep icons visible: ~=198  @=268  ---=346
        _INP_ARC = (
            "QLineEdit{background:rgba(220,220,220,230);color:#111;"
            "border:1px solid #aaa;border-radius:3px;"
            "font-size:15px;font-weight:bold;}"
        )
        _ay = 118 + (40 - 26) // 2          # vertically centred in 40px row → y=125
        for attr, val, x in [("arc","8",198), ("rc","1.0",268), ("simp","0.05",346)]:
            inp = QLineEdit(val, p)
            inp.setGeometry(x, _ay, 52, 26)
            inp.setAlignment(Qt.AlignCenter)
            inp.setStyleSheet(_INP_ARC)
            inp.setFont(QFont("Arial", 15, QFont.Bold))
            setattr(self, attr, inp)

        # ── Param inputs: x=62,193,318  y=213  w=65  h=26 ────────────────────
        # Small gray labels above each input
        _INP_LBL = (
            "QLabel{color:#bbbbbb;font-size:10px;font-family:'Segoe UI',Arial;"
            "background:transparent;}"
        )
        for lbl_txt, lx in [("Wall Height", 62), ("Wall Thickness", 193), ("Front F Thick.", 318)]:
            ll = QLabel(lbl_txt, p); ll.setGeometry(lx, 198, 65, 14)
            ll.setAlignment(Qt.AlignCenter); ll.setStyleSheet(_INP_LBL)
        _INP_P = (
            "QLineEdit{background:white;color:#111;border:none;"
            "font-size:15px;font-family:'Segoe UI',Arial;font-weight:normal;}"
        )
        for attr, val, x in [("height","50",62), ("wall","3",193), ("fp_val","2.4",318)]:
            inp = QLineEdit(val, p)
            inp.setGeometry(x, 213, 65, 26)
            inp.setAlignment(Qt.AlignCenter)
            inp.setStyleSheet(_INP_P)
            inp.setFont(QFont("Segoe UI", 15))
            setattr(self, attr, inp)

        # Fast Preview — hidden widget, always True (removed from UI)
        self.fp = QCheckBox("", p)
        self.fp.setGeometry(0, 0, 0, 0)
        self.fp.setChecked(True)
        self.fp.setVisible(False)

        # ── Button label style — 10px, word-wrap enabled, 66px wide ─────────────
        _LBL = (
            "QLabel{color:#aaaaaa;font-size:10px;font-family:'Segoe UI',Arial;"
            "background:transparent;}"
        )
        def _lbl_above(text, bx, bw, by, lh=24):
            """Create a centered, word-wrapped label lh px above button."""
            lw = 66
            lx = bx + (bw - lw) // 2
            ly = by - lh - 1
            l = QLabel(text, p)
            l.setGeometry(lx, ly, lw, lh)
            l.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
            l.setWordWrap(True)
            l.setStyleSheet(_LBL)

        # ── Channel buttons + labels: y=290  ─────────────────────────────────
        # ws_deg = taper degree button — label says "Taper °" not "Front F thick."
        _ch_labels = ["Straight Ch.", "Tapered Ch.", "Flared Ch.", "Taper °"]
        for (attr, x), lbl in zip([("ws1",62),("ws2",139),("ws3",216),("ws_deg",292)],
                                   _ch_labels):
            _lbl_above(lbl, x, 52, 290)
            b = self._btn(p, "", x, 290, 52, 49, _T, 9)
            setattr(self, attr, b)

        # SVG arka planda kalan eski mod butonu grafiklerini kapat
        _cover = QLabel(p)
        _cover.setGeometry(32, 336, 342, 90)
        _cover.setStyleSheet("background:#333333;border:none;")

        # ── Mod buttons: y=372 — ortalanmış (3×53 + 2×20 = 199px, start=103) ──
        for (attr, x), lbl in zip([("face_tab",103), ("mod2_btn",176)],
                                   ["Mod 1", "Mod 2"]):
            _lbl_above(lbl, x, 53, 372)
            b = self._btn(p, "", x, 372, 53, 48, _T, 9)
            setattr(self, attr, b)
        _m1_svg = os.path.join(str(_app_dir()), "mod 1 butonu.svg")
        if HAS_SVG and os.path.exists(_m1_svg):
            _m1r = QSvgRenderer(_m1_svg)
            _m1p = QPixmap(53, 48); _m1p.fill(Qt.transparent)
            _m1pa = QPainter(_m1p)
            _m1r.render(_m1pa)
            _m1pa.end()
            self.face_tab.setIcon(QIcon(_m1p))
            self.face_tab.setIconSize(_m1p.size())
        _m2_svg = os.path.join(str(_app_dir()), "mod 2 butonu.svg")
        if HAS_SVG and os.path.exists(_m2_svg):
            _m2r = QSvgRenderer(_m2_svg)
            _m2p = QPixmap(53, 48); _m2p.fill(Qt.transparent)
            _m2pa = QPainter(_m2p)
            _m2r.render(_m2pa)
            _m2pa.end()
            self.mod2_btn.setIcon(QIcon(_m2p))
            self.mod2_btn.setIconSize(_m2p.size())
        # Only Wall butonu — ortalanmış grubun sağı (x=249)
        _lbl_above("Only Wall", 249, 53, 372)
        self.only_wall_btn = self._btn(p, "", 249, 372, 53, 48, _T, 7)
        _ow_svg = os.path.join(str(_app_dir()), "only wall.svg")
        if HAS_SVG and os.path.exists(_ow_svg):
            _owr = QSvgRenderer(_ow_svg)
            _owp = QPixmap(53, 48); _owp.fill(Qt.transparent)
            _owpa = QPainter(_owp)
            _owr.render(_owpa)
            _owpa.end()
            self.only_wall_btn.setIcon(QIcon(_owp))
            self.only_wall_btn.setIconSize(_owp.size())

        # ── 5-tab buttons + labels: y=453 ────────────────────────────────────
        _tab_labels = ["Fill Des.", "Top Slot", "Bottom", "Back Cover p.", "Color"]
        for (attr, x), lbl in zip(
                [("tbot",32),("ttop",104),("topbot_btn",176),
                 ("cover_btn",248),("color_sep",320)], _tab_labels):
            _lbl_above(lbl, x, 52, 453)
            b = self._btn(p, "", x, 453, 52, 49, _T, 6)
            setattr(self, attr, b)

        # Fill Des. buton SVG ikonu
        _fd_svg = os.path.join(str(_app_dir()),
                               "fill des butonu.svg")
        if HAS_SVG and os.path.exists(_fd_svg):
            _fdr = QSvgRenderer(_fd_svg)
            _fdp = QPixmap(52, 49); _fdp.fill(Qt.transparent)
            _fdpa = QPainter(_fdp)
            _fdr.render(_fdpa)
            _fdpa.end()
            self.tbot.setIcon(QIcon(_fdp))
            self.tbot.setIconSize(_fdp.size())

        # Color buton SVG ikonu
        _color_svg = os.path.join(str(_app_dir()),
                                  "color buton yeni.svg")
        if HAS_SVG and os.path.exists(_color_svg):
            _cr = QSvgRenderer(_color_svg)
            _cp = QPixmap(52, 49); _cp.fill(Qt.transparent)
            _cpa = QPainter(_cp)
            _cr.render(_cpa)
            _cpa.end()
            self.color_sep.setIcon(QIcon(_cp))
            self.color_sep.setIconSize(_cp.size())
            self.color_sep.setText("")

        # ── DXF buttons: x=141,213  y=534  w=52  h=49 ────────────────────────
        _dxf_labels = ["Plexiglas DXF", "Back Foam DXF"]
        for (attr, x), lbl in zip([("plexiglas_btn", 141), ("foam_btn", 213)], _dxf_labels):
            _lbl_above(lbl, x, 52, 534)
            b = self._btn(p, "", x, 534, 52, 49, _T, 6)
            setattr(self, attr, b)

        # ── Generate: x=146  y=598  w=116  h=19 ──────────────────────────────
        # "Generate 3D" label — overlaid on button, shows through transparent button
        _gen_lbl = QLabel("Generate 3D", p)
        _gen_lbl.setGeometry(146, 598, 116, 19)
        _gen_lbl.setAlignment(Qt.AlignCenter)
        _gen_lbl.setStyleSheet(
            "QLabel{color:#3399ff;font-size:11px;font-weight:bold;"
            "font-family:'Segoe UI',Arial;background:transparent;}"
        )
        self.gen_btn = self._btn(p, "", 146, 598, 116, 19,
            "QPushButton{background:transparent;border:none;}"
            "QPushButton:hover{background:rgba(255,255,255,40);border-radius:3px;}"
            "QPushButton:pressed{background:rgba(0,0,0,60);border-radius:3px;}"
            "QPushButton:disabled{background:transparent;}", 8)

        # Status label — below R/P buttons (y=711+25+4=740)
        self.status = QLabel("", p)
        self.status.setGeometry(13, 742, 380, 16)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet(
            "QLabel{color:#aaaaaa;font-size:9px;font-family:'Segoe UI',Arial;"
            "background:rgba(0,0,0,50);border-radius:3px;}"
        )
        self.status.setVisible(True)

        # ── Objects section ────────────────────────────────────────────────────
        sep = QFrame(p); sep.setGeometry(0, 766, 406, 1)
        sep.setStyleSheet("background:#444444;")

        obj_hdr = QLabel("Objects", p)
        obj_hdr.setGeometry(13, 774, 76, 22)
        obj_hdr.setAlignment(Qt.AlignCenter)
        obj_hdr.setStyleSheet(
            "QLabel{background:#2a5a8a;color:#cce;font-size:11px;font-weight:bold;"
            "border-radius:11px;font-family:'Segoe UI',Arial;}"
        )

        # Dynamic container — checkboxes are added/removed here at runtime
        self.obj_checks = QWidget(p)
        self.obj_checks.setGeometry(13, 802, 380, 210)
        self.obj_checks.setStyleSheet("background:transparent;")

        # ── Export STL: x=140  y=656  w=66  h=22 ─────────────────────────────
        # ── Export 3MF: x=209  y=656  w=64  h=22 ─────────────────────────────
        _EXP_LBL = (
            "QLabel{color:#cccccc;font-size:8px;font-weight:bold;"
            "font-family:'Segoe UI',Arial;background:transparent;}"
        )
        # Labels exactly match button geometry so text stays inside
        _estl = QLabel("Export STL", p); _estl.setGeometry(140, 656, 66, 22)
        _estl.setAlignment(Qt.AlignCenter); _estl.setStyleSheet(_EXP_LBL)
        self.exp_btn    = self._btn(p, "", 140, 656, 66, 22, _T, 7)
        _e3mf = QLabel("Export 3MF", p); _e3mf.setGeometry(209, 656, 64, 22)
        _e3mf.setAlignment(Qt.AlignCenter); _e3mf.setStyleSheet(_EXP_LBL)
        self.exp3mf_btn = self._btn(p, "", 209, 656, 64, 22, _T, 7)

        # ── Cover export (below export row) ───────────────────────────────────
        self.cover_exp_btn = self._btn(p, "", 140, 682, 133, 18, _T, 7)

        # ── R / P: x=170,203  y=711  w=33  h=25 ──────────────────────────────
        _RP_LBL = (
            "QLabel{color:#ffffff;font-size:16px;font-weight:bold;"
            "font-family:'Segoe UI',Arial;background:transparent;}"
        )
        _r_lbl = QLabel("R", p); _r_lbl.setGeometry(170, 711, 33, 25)
        _r_lbl.setAlignment(Qt.AlignCenter); _r_lbl.setStyleSheet(_RP_LBL)
        self.replay_r = self._btn(p, "", 170, 711, 33, 25, _T, 10)
        _p_lbl = QLabel("P", p); _p_lbl.setGeometry(203, 711, 33, 25)
        _p_lbl.setAlignment(Qt.AlignCenter); _p_lbl.setStyleSheet(_RP_LBL)
        self.replay_p = self._btn(p, "", 203, 711, 33, 25, _T, 10)


# ── Profile selection / management dialog ────────────────────────────────────
class SlotDimDialog(QDialog):
    """Alt ve üst tırnak genişliklerini ayrı ayrı girmek için diyalog."""
    def __init__(self, bot_proj: float, top_proj: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Slot Dimension")
        self.setFixedWidth(280)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 14, 16, 14)

        form = QFormLayout()
        form.setSpacing(8)

        self.sp_bot = QDoubleSpinBox()
        self.sp_bot.setRange(0.0, 60.0)
        self.sp_bot.setDecimals(2)
        self.sp_bot.setSuffix(" mm")
        self.sp_bot.setValue(bot_proj)
        self.sp_bot.setToolTip("0 = alt slot kapalı")
        form.addRow("Alt Slot Dim:", self.sp_bot)

        self.sp_top = QDoubleSpinBox()
        self.sp_top.setRange(0.0, 60.0)
        self.sp_top.setDecimals(2)
        self.sp_top.setSuffix(" mm")
        self.sp_top.setValue(top_proj)
        self.sp_top.setToolTip("0 = üst slot kapalı")
        form.addRow("Üst Slot Dim:", self.sp_top)

        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


class ProfileDialog(QDialog):
    def __init__(self, profiles: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Profil Seç")
        self.setFixedSize(480, 460)
        self.setStyleSheet(
            "QDialog{background:#1e1e1e;color:#ddd;}"
            "QListWidget{background:#2a2a2a;color:#ddd;border:1px solid #444;"
            "font-size:13px;}"
            "QListWidget::item{padding:6px 8px;white-space:nowrap;}"
            "QListWidget::item:selected{background:#0066cc;color:#fff;}"
            "QPushButton{background:#333;color:#ddd;border:1px solid #555;"
            "padding:5px 10px;border-radius:3px;font-size:12px;}"
            "QPushButton:hover{background:#444;}"
            "QPushButton:disabled{color:#666;background:#2a2a2a;}"
        )
        self.profiles = profiles
        self.action   = None   # 'apply' | 'update' | 'rename' | 'delete'
        self.selected_name = None

        vl = QVBoxLayout(self)
        vl.setContentsMargins(12, 12, 12, 12); vl.setSpacing(8)

        title = QLabel("Profil Listesi")
        title.setStyleSheet("font-size:14px;font-weight:bold;color:#fff;")
        vl.addWidget(title)

        self.lst = QListWidget()
        self.lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.lst.setWordWrap(False)
        self.lst.addItems(sorted(profiles.keys()))
        self.lst.setCurrentRow(0)
        self.lst.itemDoubleClicked.connect(self._apply)
        vl.addWidget(self.lst)

        btn_row1 = QHBoxLayout()
        self.btn_apply  = QPushButton("▶  Uygula")
        self.btn_update = QPushButton("↺  Güncelle")
        btn_row1.addWidget(self.btn_apply)
        btn_row1.addWidget(self.btn_update)
        vl.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        self.btn_rename = QPushButton("✎  Yeniden Adlandır")
        self.btn_delete = QPushButton("✕  Sil")
        self.btn_delete.setStyleSheet(
            "QPushButton{background:#5a1a1a;color:#ff8888;border:1px solid #844;}"
            "QPushButton:hover{background:#7a2a2a;}"
        )
        btn_row2.addWidget(self.btn_rename)
        btn_row2.addWidget(self.btn_delete)
        vl.addLayout(btn_row2)

        cancel = QPushButton("İptal")
        vl.addWidget(cancel)

        self.btn_apply.clicked.connect(self._apply)
        self.btn_update.clicked.connect(self._update)
        self.btn_rename.clicked.connect(self._rename)
        self.btn_delete.clicked.connect(self._delete)
        cancel.clicked.connect(self.reject)

        self._update_buttons()
        self.lst.currentRowChanged.connect(lambda _: self._update_buttons())

    def _update_buttons(self):
        has = self.lst.currentItem() is not None
        for b in (self.btn_apply, self.btn_update, self.btn_rename, self.btn_delete):
            b.setEnabled(has)

    def _current(self):
        item = self.lst.currentItem()
        return item.text() if item else None

    def _apply(self):
        name = self._current()
        if not name: return
        self.selected_name = name; self.action = 'apply'; self.accept()

    def _update(self):
        name = self._current()
        if not name: return
        self.selected_name = name; self.action = 'update'; self.accept()

    def _rename(self):
        name = self._current()
        if not name: return
        new_name, ok = QInputDialog.getText(self, "Yeniden Adlandır",
                                            "Yeni profil adı:", text=name)
        if not ok or not new_name.strip(): return
        new_name = new_name.strip()
        if new_name in self.profiles and new_name != name:
            QMessageBox.warning(self, "Hata", f'"{new_name}" zaten var.'); return
        self.profiles[new_name] = self.profiles.pop(name)
        self.lst.currentItem().setText(new_name)
        self.selected_name = new_name; self.action = 'rename'; self.accept()

    def _delete(self):
        name = self._current()
        if not name: return
        r = QMessageBox.question(self, "Sil", f'"{name}" silinsin mi?',
                                 QMessageBox.Yes | QMessageBox.No)
        if r != QMessageBox.Yes: return
        self.profiles.pop(name)
        row = self.lst.currentRow()
        self.lst.takeItem(row)
        if self.lst.count() == 0:
            self.selected_name = None; self.action = 'delete'; self.accept()
        self._update_buttons()


# ── Face Fill Pattern Dialog ──────────────────────────────────────────────────
class FaceFillDialog(QDialog):
    """Yüzey dolgu deseni seçimi, hücre boyutu, duvar kalınlığı ve solid katman ayarı."""

    _PATTERNS = [
        (0, "Solid",     "Dolu yüzey — boşluksuz, maksimum mukavemet"),
        (1, "Honeycomb", "Bal peteği — altıgen hücreler, hafif ve sağlam"),
        (2, "Grid",      "Kafes — kare delikler, düzgün yoğunluk"),
        (3, "Lines",     "Çizgiler — dikey şeritler, hızlı baskı"),
    ]

    _DARK  = "#2a2a2a"
    _BTN_A = ("QPushButton{background:#1e5c24;color:#fff;border:none;border-radius:4px;"
              "font-size:12px;font-weight:bold;padding:0 14px;}"
              "QPushButton:hover{background:#2a7d33;}")
    _BTN_C = ("QPushButton{background:#484848;color:#ccc;border:none;border-radius:4px;"
              "font-size:12px;padding:0 14px;}"
              "QPushButton:hover{background:#5a5a5a;}")
    # solid_pos: 0=Kapalı  1=Ön yüz (z=0)  2=Arka yüz (z=ft)  3=Her ikisi
    _POS_LABELS = ["Kapalı", "Arka yüz", "Ön yüz", "Her ikisi"]

    def __init__(self, parent, fill_idx=0, cell_mm=8.0, wall_mm=1.6, solid_pos=0, border_mm=1.5, only_face=False):
        super().__init__(parent)
        self.setWindowTitle("Yüzey Dolgu Deseni")
        self.setFixedWidth(500)
        self.setStyleSheet(
            f"QDialog{{background:{self._DARK};color:#dddddd;}}"
            "QLabel{background:transparent;color:#dddddd;}"
            "QRadioButton{background:transparent;color:#dddddd;spacing:8px;}"
            "QRadioButton::indicator{width:14px;height:14px;"
            "border-radius:7px;border:2px solid #88aadd;background:#222;}"
            "QRadioButton::indicator:checked{background:#4488ff;"
            "border:2px solid #66aaff;}"
            "QRadioButton::indicator:hover{border:2px solid #aaccff;}"
            "QDoubleSpinBox{background:#383838;color:#eee;"
            "border:1px solid #555;border-radius:3px;padding:1px 4px;}")

        self.fill_idx  = fill_idx    # _on_radio tarafından güncellenir
        self.cell_mm   = cell_mm
        self.wall_mm   = wall_mm
        self.border_mm = border_mm
        self.solid_pos = solid_pos   # _on_pos tarafından güncellenir
        self.only_face = only_face

        vl = QVBoxLayout(self)
        vl.setContentsMargins(18, 14, 18, 12)
        vl.setSpacing(6)

        # ── Header ────────────────────────────────────────────────────────
        hdr = QLabel("Yüzey Dolgu Deseni")
        hdr.setStyleSheet("font-size:14px;font-weight:bold;color:#aaccff;"
                          "background:transparent;")
        vl.addWidget(hdr)
        sep0 = QFrame(); sep0.setFrameShape(QFrame.HLine)
        sep0.setStyleSheet("color:#444;"); vl.addWidget(sep0)

        # ── Pattern rows (QFrame ile highlight desteği) ───────────────────
        self._radios      = []
        self._prev_lbl    = []
        self._row_frames  = []
        self._bg = QButtonGroup(self)
        self._bg.setExclusive(True)

        for pidx, name, desc in self._PATTERNS:
            frm = QFrame()
            frm.setContentsMargins(0, 0, 0, 0)
            fl = QHBoxLayout(frm)
            fl.setSpacing(10); fl.setContentsMargins(6, 3, 6, 3)

            rb = QRadioButton()
            rb.setAutoExclusive(False)   # QButtonGroup exclusivity yönetir
            rb.setChecked(pidx == fill_idx)
            self._bg.addButton(rb, pidx)
            self._radios.append(rb)
            fl.addWidget(rb)

            lbl = QLabel()
            lbl.setFixedSize(80, 56)
            lbl.setStyleSheet("border:1px solid #555;background:#111;")
            lbl.setPixmap(self._make_preview(pidx, cell_mm, wall_mm))
            self._prev_lbl.append(lbl)
            fl.addWidget(lbl)

            tc = QVBoxLayout(); tc.setSpacing(1)
            n_l = QLabel(name)
            n_l.setStyleSheet("font-size:13px;font-weight:bold;color:#eeeeee;"
                              "background:transparent;")
            d_l = QLabel(desc)
            d_l.setStyleSheet("font-size:10px;color:#888;background:transparent;")
            tc.addWidget(n_l); tc.addWidget(d_l)
            fl.addLayout(tc, 1)

            rb.toggled.connect(
                lambda checked, pid=pidx: self._on_radio(pid, checked))

            self._row_frames.append(frm)
            vl.addWidget(frm)

        self._update_row_highlights(fill_idx)

        # ── Density controls ──────────────────────────────────────────────
        sep1 = QFrame(); sep1.setFrameShape(QFrame.HLine)
        sep1.setStyleSheet("color:#444;"); vl.addWidget(sep1)

        ctrl = QHBoxLayout(); ctrl.setSpacing(12)
        ctrl.addWidget(QLabel("Hücre boyutu (mm):"))
        self._cell_sp = QDoubleSpinBox()
        self._cell_sp.setRange(3.0, 40.0); self._cell_sp.setSingleStep(0.5)
        self._cell_sp.setValue(cell_mm); self._cell_sp.setDecimals(1)
        self._cell_sp.setFixedWidth(74)
        self._cell_sp.valueChanged.connect(self._on_cell)
        ctrl.addWidget(self._cell_sp)
        ctrl.addSpacing(18)
        ctrl.addWidget(QLabel("Petek duvarı (mm):"))
        self._wall_sp = QDoubleSpinBox()
        self._wall_sp.setRange(0.2, 8.0); self._wall_sp.setSingleStep(0.1)
        self._wall_sp.setValue(wall_mm); self._wall_sp.setDecimals(1)
        self._wall_sp.setFixedWidth(74)
        self._wall_sp.valueChanged.connect(self._on_wall)
        ctrl.addWidget(self._wall_sp)
        ctrl.addStretch()
        vl.addLayout(ctrl)

        ctrl2 = QHBoxLayout(); ctrl2.setSpacing(12)
        ctrl2.addWidget(QLabel("Kenar payı (mm):"))
        self._border_sp = QDoubleSpinBox()
        self._border_sp.setRange(0.0, 20.0); self._border_sp.setSingleStep(0.5)
        self._border_sp.setValue(border_mm); self._border_sp.setDecimals(1)
        self._border_sp.setFixedWidth(74)
        self._border_sp.setToolTip("Harfin kenarından bırakılan solid kontur payı (0 = kenar payı yok)")
        self._border_sp.valueChanged.connect(lambda v: setattr(self, 'border_mm', v))
        ctrl2.addWidget(self._border_sp)
        ctrl2.addStretch()
        vl.addLayout(ctrl2)

        # ── Sadece yüz plakası seçeneği ───────────────────────────────────
        sep_of = QFrame(); sep_of.setFrameShape(QFrame.HLine)
        sep_of.setStyleSheet("color:#444;"); vl.addWidget(sep_of)

        self._only_face_cb = QCheckBox("Sadece yüz plakası  (duvar olmadan)")
        self._only_face_cb.setChecked(only_face)
        self._only_face_cb.setStyleSheet(
            "QCheckBox{color:#88ccff;font-size:11px;font-weight:bold;"
            "background:transparent;spacing:8px;}"
            "QCheckBox::indicator{width:15px;height:15px;"
            "border-radius:3px;border:2px solid #6699cc;background:#1a2a3a;}"
            "QCheckBox::indicator:checked{background:#3377cc;"
            "border:2px solid #88bbff;"
            "image:url(none);}"
            "QCheckBox::indicator:checked{background:#3377cc;border:2px solid #88bbff;}")
        self._only_face_cb.setToolTip(
            "İşaretlenirse yalnızca yüz plakası oluşturulur — kanal duvarı oluşmaz.\n"
            "Kesim/lazer plakası veya standalone yüzey için idealdir.")
        self._only_face_cb.toggled.connect(lambda v: setattr(self, 'only_face', v))
        vl.addWidget(self._only_face_cb)

        # ── Solid koruyucu katmanlar ───────────────────────────────────────
        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color:#444;"); vl.addWidget(sep2)

        pos_hdr = QLabel("Koruyucu solid katmanlar  (0.8 mm  —  dış mekân):")
        pos_hdr.setStyleSheet("color:#ccddff;font-size:11px;font-weight:bold;"
                              "background:transparent;")
        vl.addWidget(pos_hdr)

        pos_row = QHBoxLayout(); pos_row.setSpacing(5); pos_row.setContentsMargins(0,2,0,4)
        self._pos_btns = []
        self._pos_bg   = QButtonGroup(self)
        self._pos_bg.setExclusive(True)
        for i, lbl_text in enumerate(self._POS_LABELS):
            btn = QPushButton(lbl_text)
            btn.setCheckable(True)
            btn.setChecked(i == solid_pos)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(90)
            self._pos_bg.addButton(btn, i)
            self._pos_btns.append(btn)
            pos_row.addWidget(btn)
        pos_row.addStretch()
        for _btn in self._pos_btns:
            _bid = self._pos_bg.id(_btn)
            _btn.toggled.connect(
                lambda checked, b=_bid: self._on_pos(b, checked))
        self._update_pos_styles(solid_pos)
        vl.addLayout(pos_row)

        # ── OK / Cancel ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.addStretch()
        ok = QPushButton("Uygula"); ok.setFixedHeight(28)
        ok.setStyleSheet(self._BTN_A); ok.clicked.connect(self._on_accept)
        cn = QPushButton("İptal");  cn.setFixedHeight(28)
        cn.setStyleSheet(self._BTN_C); cn.clicked.connect(self.reject)
        btn_row.addWidget(ok); btn_row.addSpacing(8); btn_row.addWidget(cn)
        vl.addLayout(btn_row)

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_accept(self):
        # Spinbox değerlerini garantili oku (valueChanged sinyali kaçırılmış olabilir)
        self.cell_mm   = self._cell_sp.value()
        self.wall_mm   = self._wall_sp.value()
        self.border_mm = self._border_sp.value()
        self.only_face = self._only_face_cb.isChecked()
        # fill_idx → _on_radio, solid_pos → _on_pos tarafından takip edilir
        self.accept()

    def _on_radio(self, bid, checked):
        if checked and bid >= 0:
            self.fill_idx = bid
            self._update_row_highlights(bid)

    def _on_pos(self, bid, checked):
        if checked and bid >= 0:
            self.solid_pos = bid
            self._update_pos_styles(bid)

    def _update_row_highlights(self, active_idx):
        """Seçili desen satırını açık yeşil yapar; Solid ve inaktif satırlar şeffaf."""
        for i, frm in enumerate(self._row_frames):
            pidx = self._PATTERNS[i][0]
            if pidx == active_idx and active_idx != 0:
                frm.setStyleSheet(
                    "QFrame{background:rgba(20,110,45,90);border-radius:5px;}")
            else:
                frm.setStyleSheet("QFrame{background:transparent;}")

    def _update_pos_styles(self, active):
        """Aktif konum butonunu yeşil, diğerlerini gri gösterir."""
        for btn in self._pos_btns:
            bid = self._pos_bg.id(btn)
            if bid == active:
                btn.setStyleSheet(
                    "QPushButton{background:#1e5c24;color:#fff;border:none;"
                    "border-radius:3px;font-size:11px;}"
                    "QPushButton:hover{background:#2a7d33;}")
            else:
                btn.setStyleSheet(
                    "QPushButton{background:#383838;color:#999;border:none;"
                    "border-radius:3px;font-size:11px;}"
                    "QPushButton:hover{background:#484848;color:#ddd;}")

    def _on_cell(self, val):
        self.cell_mm = val
        self._refresh_previews()

    def _on_wall(self, val):
        self.wall_mm = val
        self._refresh_previews()

    def _refresh_previews(self):
        for i, lbl in enumerate(self._prev_lbl):
            lbl.setPixmap(self._make_preview(i, self.cell_mm, self.wall_mm))

    # ── Preview painter ───────────────────────────────────────────────────────
    @staticmethod
    def _make_preview(fill_idx, cell_mm=8.0, wall_mm=1.6, w=80, h=58):
        """Draw a small pattern preview using QPainter."""
        from PySide6.QtGui import QPolygonF
        from PySide6.QtCore import QPointF, QRectF

        px = QPixmap(w, h)
        px.fill(QColor(17, 17, 17))
        pa = QPainter(px)
        pa.setRenderHint(QPainter.Antialiasing)

        M = 4          # margin px
        mat = QColor(160, 195, 225)    # material colour
        void_c = QColor(17, 17, 17)    # void colour

        if fill_idx == 0:              # ── Solid ────────────────────────
            pa.fillRect(M, M, w - 2*M, h - 2*M, mat)

        elif fill_idx == 1:            # ── Honeycomb ────────────────────
            pa.fillRect(M, M, w - 2*M, h - 2*M, mat)
            pa.setBrush(void_c); pa.setPen(Qt.NoPen)
            # scale so ~3-4 cells fit across the preview
            scale = (w - 2*M) / 3.8   # px per cell pitch
            r     = scale * 0.5
            # Uniform wall: r_void = r - wall_mm/sqrt(3), scaled to preview
            rv    = max(0.5, r - (wall_mm / cell_mm) * r / math.sqrt(3))
            cs    = r * math.sqrt(3)   # col step
            rs    = r * 1.5            # row step
            ri = 0
            cy = float(M) - rs
            while cy < h - M + rs:
                cx = float(M) - cs + (cs * 0.5 if ri % 2 else 0.0)
                while cx < w - M + cs:
                    pts = [QPointF(cx + rv * math.cos(math.radians(90 + 60*k)),
                                   cy + rv * math.sin(math.radians(90 + 60*k)))
                           for k in range(6)]
                    pa.drawPolygon(QPolygonF(pts))
                    cx += cs
                cy += rs; ri += 1

        elif fill_idx == 2:            # ── Grid ─────────────────────────
            pa.fillRect(M, M, w - 2*M, h - 2*M, mat)
            pa.setBrush(void_c); pa.setPen(Qt.NoPen)
            scale = (w - 2*M) / 3.8
            ratio = max(0.05, min(0.95, (cell_mm - wall_mm) / cell_mm))
            vs  = scale * ratio        # void size px
            wp  = (scale - vs) * 0.5  # half-wall px
            y   = float(M) + wp
            while y < h - M:
                x = float(M) + wp
                while x < w - M:
                    vw = min(vs, w - M - x)
                    vh = min(vs, h - M - y)
                    if vw > 0.3 and vh > 0.3:
                        pa.fillRect(QRectF(x, y, vw, vh), void_c)
                    x += scale
                y += scale

        elif fill_idx == 3:            # ── Lines ────────────────────────
            pa.fillRect(M, M, w - 2*M, h - 2*M, mat)
            pa.setBrush(void_c); pa.setPen(Qt.NoPen)
            scale = (w - 2*M) / 3.8
            ratio = max(0.05, min(0.95, (cell_mm - wall_mm) / cell_mm))
            vw  = scale * ratio
            wp  = (scale - vw) * 0.5
            x   = float(M) + wp
            while x < w - M:
                _vw = min(vw, w - M - x)
                if _vw > 0.3:
                    pa.fillRect(QRectF(x, M, _vw, h - 2*M), void_c)
                x += scale

        pa.end()
        return px


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Dil tercihini menüden önce yükle
        _lp = _user_dir() / 'lang_pref.json'
        try:
            if _lp.exists():
                set_lang(json.loads(_lp.read_text(encoding='utf-8')).get('lang', 'tr'))
        except Exception:
            pass

        self.setWindowTitle("Harfex — Channel Letter 3D"); self.resize(1450, 857)

        # ── Menu bar ──────────────────────────────────────────────────────────
        mb = self.menuBar()
        mb.setStyleSheet(
            "QMenuBar{background:#2a2a2a;color:#dddddd;font-size:12px;padding:2px 4px;}"
            "QMenuBar::item{padding:4px 10px;border-radius:3px;}"
            "QMenuBar::item:selected{background:#444;}"
            "QMenu{background:#2a2a2a;color:#dddddd;border:1px solid #555;}"
            "QMenu::item{padding:6px 24px 6px 16px;}"
            "QMenu::item:selected{background:#0066cc;color:#fff;}"
            "QMenu::separator{height:1px;background:#555;margin:4px 8px;}"
        )

        # ── Dosya ─────────────────────────────────────────────────────────────
        fm = mb.addMenu(_t("menu_file"))
        fm.addAction(_t("menu_open_dxf"),          self._import)
        fm.addSeparator()
        ex = fm.addMenu(_t("menu_export"))
        ex.addAction(_t("menu_exp_stl"),           self._export)
        ex.addAction(_t("menu_exp_3mf"),           self._export_3mf_direct)
        ex.addSeparator()
        ex.addAction(_t("menu_exp_cover_stl"),     self._save_back_cover)
        ex.addAction(_t("menu_exp_plex"),          self._export_plexiglas)
        ex.addAction(_t("menu_exp_foam"),          self._export_foam)
        ex.addAction(_t("menu_exp_fill"),          self._export_fill_dxf)
        fm.addSeparator()
        fm.addAction(_t("menu_clear"),             self._clear_scene)
        fm.addSeparator()
        fm.addAction(_t("menu_exit"),              self.close)

        # ── Düzenle ───────────────────────────────────────────────────────────
        em = mb.addMenu(_t("menu_edit"))
        em.addAction(_t("menu_wall_color"),        self._pick_wall_color)
        em.addAction(_t("menu_face_color"),        self._pick_face_color)
        em.addAction(_t("menu_cover_color"),       self._pick_cover_color)
        em.addSeparator()
        em.addAction(_t("menu_reset_view"),        lambda: self.vp._set_view(35, -45))
        self._act_contour = em.addAction(_t("menu_contour"), self._toggle_contour_menu)
        self._act_contour.setCheckable(True)
        self._act_contour.setChecked(False)

        # ── Profil ────────────────────────────────────────────────────────────
        pm = mb.addMenu(_t("menu_profile"))
        pm.addAction(_t("menu_prof_save"),         self._replay_record)
        pm.addAction(_t("menu_prof_manage"),       self._replay_play)

        # ── Dil ───────────────────────────────────────────────────────────────
        lm = mb.addMenu(_t("menu_lang"))
        lm.addAction(_t("lang_tr"), lambda: self._set_language("tr"))
        lm.addAction(_t("lang_en"), lambda: self._set_language("en"))

        # ── Yardım ────────────────────────────────────────────────────────────
        hm = mb.addMenu(_t("menu_help"))
        hm.addAction(_t("menu_about"),             self._about)
        hm.addAction(_t("menu_guide"),             self._open_guide)

        # ── Root widget: VBox → [topbar | content] ────────────────────────────
        root = QWidget(); rv = QVBoxLayout(root)
        rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(0)

        # ── Top bar: Former + Prepare (406 px total, above left panel only) ──────
        # SVG "former ve prepare barı.svg": viewBox 0 0 2770 249 → 406×37px
        # Topbar boyutları — eski ölçüler korundu
        import os as _os
        _BAR_H  = 37    # topbar yüksekliği (orijinal)
        _LOGO_W = 167   # sol logo bölgesi genişliği
        _PREP_W = 239   # sağ Slice bölgesi genişliği
        _BAR_TOTAL = _LOGO_W + _PREP_W   # 406

        topbar = QFrame(); topbar.setFixedHeight(_BAR_H)
        topbar.setObjectName("topbar")
        topbar.setStyleSheet("#topbar{background:#222222;}")
        th = QHBoxLayout(topbar); th.setContentsMargins(0, 0, 0, 0); th.setSpacing(0)

        # Bar widget — yeşil arka plan yok, sade koyu arka plan
        bar_w = QWidget()
        bar_w.setFixedSize(_BAR_TOTAL, _BAR_H)
        bar_w.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #2a2a2a, stop:0.41 #2a2a2a,"
            "stop:0.42 #1a1a1a, stop:1 #1a1a1a);"
        )

        # Logo — 37px bara sığacak şekilde küçültülmüş tam logo
        _logo_png = _os.path.join(str(_app_dir()), "logo yeni.png")
        if _os.path.exists(_logo_png):
            try:
                _logo_pix = QPixmap(_logo_png)
                # Önce yüksekliğe göre ölçekle (37-4=33px)
                _logo_pix = _logo_pix.scaledToHeight(
                    _BAR_H - 4, Qt.SmoothTransformation)
                # Genişlik zona sığmazsa genişliğe göre tekrar ölçekle
                if _logo_pix.width() > _LOGO_W - 6:
                    _logo_pix = _logo_pix.scaledToWidth(
                        _LOGO_W - 6, Qt.SmoothTransformation)
                _logo_lbl = QLabel(bar_w)
                _logo_lbl.setPixmap(_logo_pix)
                _lx = (_LOGO_W - _logo_pix.width()) // 2
                _ly = (_BAR_H  - _logo_pix.height()) // 2
                _logo_lbl.setGeometry(_lx, _ly, _logo_pix.width(), _logo_pix.height())
                _logo_lbl.setStyleSheet("background:transparent;")
            except Exception:
                pass

        # "Former" button — logo üstünde şeffaf tıklanabilir alan
        self._former_btn = QPushButton("", bar_w)
        self._former_btn.setGeometry(0, 0, _LOGO_W, _BAR_H)
        self._former_btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;}"
            "QPushButton:hover{background:rgba(255,255,255,8);}"
            "QPushButton:pressed{background:rgba(0,0,0,30);}"
        )
        self._former_btn.clicked.connect(self._show_former)

        # "Slice exp." button — sağ koyu alan
        self._prepare_btn = QPushButton("Slice  exp.", bar_w)
        self._prepare_btn.setGeometry(_LOGO_W, 0, _PREP_W, _BAR_H)
        self._prepare_btn.setStyleSheet(
            "QPushButton{color:#dddddd;font-size:14px;font-weight:bold;"
            "font-family:Arial;background:transparent;border:none;"
            "text-align:center;}"
            "QPushButton:hover{background:rgba(255,255,255,18);}"
            "QPushButton:pressed{background:rgba(0,0,0,50);}"
        )
        self._prepare_btn.clicked.connect(self._prepare)

        th.addWidget(bar_w)

        # Remainder (above viewport)
        th.addStretch(1)
        th.addSpacing(10)

        rv.addWidget(topbar)

        # ── Main content (left panel + stacked viewport) ──────────────────────
        body = QWidget(); bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(0)
        self.lp = LeftPanel(); self.vp = Viewport()

        # PreparePanel: placeholder → OrcaSlicer buraya embed edilecek
        from orca_integration import PreparePanel as _PreparePanel
        self._prepare_panel = _PreparePanel(
            on_resize=lambda x, y, w, h: (
                self._orca.resize(x, y, w, h)
                if getattr(self, '_orca', None) and self._orca.is_embedded else None
            )
        )

        self._view_stack = QStackedWidget()
        self._view_stack.addWidget(self.vp)              # index 0 — Former
        self._view_stack.addWidget(self._prepare_panel)  # index 1 — Prepare

        bl.addWidget(self.lp)
        bl.addWidget(self._view_stack, 1)
        rv.addWidget(body, 1)

        self.setCentralWidget(root)
        # ── Bottom status bar ──────────────────────────────────────────────────
        sb = self.statusBar()
        sb.setStyleSheet(
            "QStatusBar { background:#1a1a1a; color:#cccccc; font-size:12px; padding:2px 8px; border-top:1px solid #333; }"
            "QStatusBar::item { border:none; }"
        )
        self._sb_dims  = QLabel("—")          # model dimensions
        self._sb_tris  = QLabel("—")          # triangle count
        self._sb_wall  = QLabel("—")          # wall type + params
        self._sb_time  = QLabel("—")          # processing time
        for lbl in (self._sb_dims, self._sb_tris, self._sb_wall, self._sb_time):
            lbl.setStyleSheet("color:#cccccc; padding:0 12px;")
        sb.addWidget(self._sb_dims)
        sb.addWidget(QLabel("|")); sb.addWidget(self._sb_tris)
        sb.addWidget(QLabel("|")); sb.addWidget(self._sb_wall)
        sb.addPermanentWidget(self._sb_time)

        self._fut = None; self._t0 = 0.0
        self._cancel_pending = False   # True → _check discards result, no mesh applied
        self._undo_stack = []          # max 10 snapshots for Ctrl+Z
        self._replay_settings = None; self._replay_cover = False

        # ── OrcaSlicer exe yolu (startup'ta başlatma yok) ────────────────────
        from orca_integration import OrcaIntegration as _OrcaInt
        self._orca = _OrcaInt.create()
        if self._orca:
            print(f"[ORCA] exe bulundu: {self._orca._exe}")
        else:
            print("[ORCA] exe bulunamadı")
        self._profiles_path  = _user_dir() / 'profiles.json'
        self._settings_path  = _user_dir() / 'last_session.json'
        self._lang_path      = _user_dir() / 'lang_pref.json'
        self._profiles = self._load_profiles()
        # Dil tercihini yükle
        try:
            if self._lang_path.exists():
                lp = json.loads(self._lang_path.read_text(encoding='utf-8'))
                set_lang(lp.get('lang', 'tr'))
        except Exception:
            pass
        self._timer = QTimer(self); self._timer.setInterval(80); self._timer.timeout.connect(self._check)
        self.lp.import_btn.clicked.connect(self._import)
        self.lp.gen_btn.clicked.connect(self._generate)
        self.lp.replay_r.clicked.connect(self._replay_record)
        self.lp.replay_p.clicked.connect(self._replay_play)
        self.lp.exp_btn.clicked.connect(self._export)
        self.lp.exp3mf_btn.clicked.connect(self._export_3mf_direct)
        self.lp.cover_btn.clicked.connect(self._export_back_cover)
        self.vp.on_slot_switch         = self._on_slot_switch
        self.vp.on_object_list_changed = self._refresh_object_list
        self.vp.on_dxf_loaded          = self._on_dxf_loaded
        self.vp.view.object_clicked.connect(self._on_object_clicked)
        self.vp.view.object_right_clicked.connect(self._on_object_right_clicked)
        self.lp.cover_exp_btn.clicked.connect(self._save_back_cover)
        self.lp.tbot.clicked.connect(self._face_fill_dialog)   # Fill Des. — sol tık: dialog
        self.lp.tbot.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lp.tbot.customContextMenuRequested.connect(self._fill_btn_context_menu)
        self.lp.ttop.clicked.connect(self._top_slot)
        self.lp.topbot_btn.clicked.connect(self._bot_slot)
        self.lp.ws1.clicked.connect(lambda: self._set_wall_type(0))
        self.lp.ws2.clicked.connect(lambda: self._set_wall_type(1))
        self.lp.ws3.clicked.connect(lambda: self._set_wall_type(2))
        self.lp.ws_deg.clicked.connect(lambda: self._set_wall_type(1))   # Degree = same as Tapered
        self.lp.mr2.clicked.connect(self._my)
        self.lp.face_tab.clicked.connect(lambda: self._face_mode_direct(1))   # Mod 1
        self.lp.mod2_btn.clicked.connect(lambda: self._face_mode_direct(2))   # Mod 2
        self.lp.only_wall_btn.clicked.connect(self._only_wall)
        self.lp.color_sep.clicked.connect(self._color_separate)
        self.lp.plexiglas_btn.clicked.connect(self._export_plexiglas)
        self.lp.foam_btn.clicked.connect(self._export_foam)
        self._style()
        # Set initial button highlights
        self._update_mod_highlight()
        self._update_slot_highlight()

        # Restore last session settings
        # Son oturum yükleme kaldırıldı — program her açılışta varsayılan değerlerle başlar

        # ── Splash screen kapat — tüm yükleme bitti, pencere hazır ──────────────
        if _HAS_SPLASH:
            try:
                _pyi_splash.close()
            except Exception:
                pass

    def closeEvent(self, event):
        if getattr(self, '_orca', None):
            self._orca.terminate()
        super().closeEvent(event)

    def _load_last_session(self):
        try:
            if self._settings_path.exists():
                return json.loads(self._settings_path.read_text(encoding='utf-8'))
        except Exception:
            pass
        return None

    def _import(self):
        p, _ = QFileDialog.getOpenFileName(self, "Import DXF", "", "DXF Files (*.dxf)")
        if not p: return
        try: self.vp.load_dxf(p)
        except Exception as e: QMessageBox.critical(self, "DXF Error", str(e))

    # ── DXF yüklenince status bar'ı güncelle ──────────────────────────────────
    def _on_dxf_loaded(self):
        """Show DXF bounding-box dimensions in status bar immediately on load."""
        if not self.vp.paths: return
        xs = [x for p in self.vp.paths for x, y in p["points"]]
        ys = [y for p in self.vp.paths for x, y in p["points"]]
        if not xs: return
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        fname = ""
        if self.vp.slots:
            fname = self.vp.slots[self.vp.active_slot_idx].filename
        lbl = f"  {fname}  —  " if fname else "  "
        self._sb_dims.setText(f"{lbl}DXF: {w:.1f} × {h:.1f} mm")
        # Yeni DXF → slot butonlarını sıfırla (highlight kapatılır, değerler korunur)
        self._update_slot_highlight()
        self._sb_tris.setText("—")
        self._sb_wall.setText("—")
        self._sb_time.setText("")

    def _toggle_contour_menu(self):
        checked = self._act_contour.isChecked()
        self.vp._show_contour = checked
        for item in self.vp.line_items:
            item.setVisible(checked)
        for item in self.vp._boundary_items:
            item.setVisible(checked)

    # ── Renk seçiciler ────────────────────────────────────────────────────────
    def _pick_wall_color(self):
        r, g, b, a = self.vp.color_wall
        c = QColorDialog.getColor(QColor(int(r*255), int(g*255), int(b*255)), self, "Duvar Rengi")
        if not c.isValid(): return
        self.vp.color_wall = (c.redF(), c.greenF(), c.blueF(), 1.0)
        if self.vp._wall_gl_item:
            self.vp._wall_gl_item.setColor(self.vp.color_wall)

    def _pick_face_color(self):
        r, g, b, a = self.vp.color_face
        c = QColorDialog.getColor(QColor(int(r*255), int(g*255), int(b*255)), self, "Yüz Rengi")
        if not c.isValid(): return
        self.vp.color_face = (c.redF(), c.greenF(), c.blueF(), 1.0)
        if self.vp._face_gl_item:
            self.vp._face_gl_item.setColor(self.vp.color_face)

    def _pick_cover_color(self):
        # Use first cover item's color as starting point
        start = QColor(38, 191, 63)   # default green
        c = QColorDialog.getColor(start, self, "Back Cover Rengi")
        if not c.isValid(): return
        col = (c.redF(), c.greenF(), c.blueF(), 1.0)
        for item in self.vp.cover_items:
            item.setColor(col)

    # ── Sahneyi temizle ───────────────────────────────────────────────────────
    def _clear_scene(self):
        """Remove all models and reset viewport to initial empty state."""
        # Remove all GL items from every slot
        for slot in self.vp.slots:
            for x in slot.mesh_items:  self.vp.view.removeItem(x)
            for x in slot.line_items:  self.vp.view.removeItem(x)
            for x in slot.cover_items: self.vp.view.removeItem(x)
        for x in self.vp._boundary_items: self.vp.view.removeItem(x)
        self.vp._boundary_items.clear()
        self.vp.scene_objects.clear()
        # Reset to a single empty slot
        s0 = ModelSlot(label="Model 1", x_offset=0.0)
        self.vp.slots = [s0]
        self.vp.active_slot_idx = 0
        self.vp.mesh_items  = s0.mesh_items
        self.vp.line_items  = s0.line_items
        self.vp.cover_items = s0.cover_items
        # Clear all model state
        self.vp.paths = []; self.vp.base_paths = []
        self.vp.has_model = False
        self.vp._last_base = self.vp._last_ig = self.vp._last_wg = None
        self.vp.last_v = self.vp.last_f = None
        self.vp._wall_gl_item = self.vp._face_gl_item = None
        self.vp.selected_obj_key  = None
        self.vp.selected_obj_keys = set()
        self.vp.groups = {}
        self._refresh_object_list()
        self._sb_dims.setText("—"); self._sb_tris.setText("—")
        self._sb_wall.setText("—"); self._sb_time.setText("")

    def _generate(self):
        if self._fut and not self._fut.done():
            self._regen_queued = True   # bitince tekrar çalıştır
            return
        self._push_undo()
        try:
            w = float(self.lp.wall.text().replace(",", "."))
            h = float(self.lp.height.text().replace(",", "."))
            self.vp.set_curve(
                float(self.lp.arc.text().replace(",", ".")),
                float(self.lp.rc.text().replace(",", ".")),
                float(self.lp.simp.text().replace(",", "."))
            )
            self.vp.wall_mm = max(0.2, min(w, 80.0))
            self.vp.height_mm = max(1.0, min(h, 250.0))
            # Front F Thick — read from direct input field
            # ft_val <= 0 means "no face" regardless of face_mode
            try:
                ft_val = float(self.lp.fp_val.text().replace(",", "."))
                self.vp.face_thickness = max(0.0, ft_val)
            except ValueError:
                pass
            self.lp.gen_btn.setEnabled(False)
            self.lp.exp_btn.setEnabled(False)
            self.lp.exp3mf_btn.setEnabled(False)
            self.lp.tbot.setEnabled(False)
            # Mod + duvar tipi butonlarını da kapat — işlem sırasında state değişmesin
            for _b in [self.lp.face_tab, self.lp.mod2_btn,
                       self.lp.only_wall_btn, self.lp.ws1,
                       self.lp.ws2, self.lp.ws3]:
                _b.setEnabled(False)
            self._set_status("Building…")
            self._t0 = time.time()

            def _build_all(vp, _w, _h, _fp):
                """Tüm ağır hesaplamayı background thread'de yap — main thread bloke olmaz."""
                v, f, b = vp.build_mesh_data(_w, _h, _fp)

                # Mod 1 / Only Face: duvar meshini de burada hesapla
                vp._bg_wall_v = vp._bg_wall_f = None
                if vp.face_mode in (1, 3) and vp.face_thickness > 0:
                    ft = vp.face_thickness
                    H  = _h + ft
                    try:
                        wv, wf = vp.build_mod1_wall_with_tabs(ft, H)
                        vp._bg_wall_v = wv
                        vp._bg_wall_f = wf
                    except Exception as _e:
                        print(f"[BUILD_ALL] wall error: {_e}")

                # Face mesh
                fv, ff = vp.build_face_mesh()
                vp._bg_face_v = fv
                vp._bg_face_f = ff
                return v, f, b

            self._fut = GEOMETRY_EXECUTOR.submit(
                _build_all, self.vp, w, h, self.lp.fp.isChecked())
            self._timer.start()
        except ValueError:
            QMessageBox.warning(self, "Error", "Enter numeric values.")
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    # ── Mod button highlight styles ───────────────────────────────────────────
    _MOD_ON  = (
        "QPushButton{background:rgba(30,180,60,90);border:none;border-radius:4px;}"
        "QPushButton:hover{background:rgba(30,220,80,110);border-radius:4px;}"
        "QPushButton:pressed{background:rgba(0,0,0,60);border-radius:4px;}"
    )
    _MOD_OFF = (
        "QPushButton{background:transparent;border:none;}"
        "QPushButton:hover{background:rgba(255,255,255,55);border-radius:5px;}"
        "QPushButton:pressed{background:rgba(0,0,0,60);border-radius:5px;}"
    )

    def _update_mod_highlight(self):
        """Apply green highlight to the active Mod button (face_mode 0-3)."""
        m = self.vp.face_mode
        self.lp.face_tab.setStyleSheet(self._MOD_ON      if m in (1, 3) else self._MOD_OFF)
        self.lp.mod2_btn.setStyleSheet(self._MOD_ON      if m == 2      else self._MOD_OFF)
        self.lp.only_wall_btn.setStyleSheet(self._MOD_ON if m == 0      else self._MOD_OFF)

    def _update_slot_highlight(self):
        """Apply green highlight to active slot buttons (Top Slot / Bottom)."""
        self.lp.ttop.setStyleSheet(self._MOD_ON if self.vp.top_tab else self._MOD_OFF)
        self.lp.topbot_btn.setStyleSheet(self._MOD_ON if self.vp.bot_tab else self._MOD_OFF)

    def _set_status(self, text, timeout_ms=0):
        """Show text in both the main status bar and the panel status label."""
        if timeout_ms:
            self.statusBar().showMessage(text, timeout_ms)
        else:
            self.statusBar().showMessage(text)
        self.lp.status.setText(text)
        if timeout_ms:
            QTimer.singleShot(timeout_ms, lambda: self.lp.status.setText(""))

    def _update_statusbar(self, tri_count, elapsed):
        """Update bottom status bar with model dimensions, triangle count, wall info."""
        # Dimensions from last geometry
        if self.vp._last_base is not None:
            minx, miny, maxx, maxy = self.vp._last_base.bounds
            w = maxx - minx; h = maxy - miny; d = self.vp.height_mm
            self._sb_dims.setText(f"  {w:.1f} × {h:.1f} × {d:.1f} mm")
        else:
            self._sb_dims.setText("—")
        self._sb_tris.setText(f"{tri_count:,} üçgen")
        # Wall type info
        wt_names = {0: "Düz Duvar", 1: "Açılı Duvar", 2: "Kavisli Duvar"}
        wt = wt_names.get(self.vp.wall_type, "—")
        if self.vp.wall_type in (1, 2):
            h_mm = max(self.vp.height_mm, 1.0)
            deg = math.degrees(math.atan(self.vp.wall_taper / h_mm))
            wt += f"  {deg:.1f}°"
        self._sb_wall.setText(f"  {wt}  |  Kalınlık: {self.vp.wall_mm:.1f} mm")
        self._sb_time.setText(f"{elapsed:.1f}s  ")

    def _check(self):
        if not self._fut: self._timer.stop(); return
        if not self._fut.done():
            self._set_status(f"Building… {time.time() - self._t0:.1f}s"); return
        self._timer.stop()
        elapsed = time.time() - self._t0
        # İptal bekleniyorsa: sonucu al (thread'i temizle) ama mesh uygulanmaz
        if self._cancel_pending:
            self._cancel_pending = False
            try: self._fut.result()
            except Exception: pass
            self._fut = None
            self.lp.gen_btn.setEnabled(True)
            self.lp.exp_btn.setEnabled(True)
            self.lp.exp3mf_btn.setEnabled(True)
            self._set_status("İptal edildi ✕", 2000)
            return
        try:
            v, f, b = self._fut.result()
            h = self.vp.height_mm
            # z-flip UYGULANMAZ: mesh üreticileri zaten doğru yönelimde üretir.
            # face z=0 (taban/yatak), duvar z=H'ye uzanır — tüm wall_type ve flip_z için geçerli.
            # Pre-computed face mesh (background thread'de hesaplandı)
            _fv = getattr(self.vp, '_bg_face_v', None)
            _ff = getattr(self.vp, '_bg_face_f', None)
            _wv = getattr(self.vp, '_bg_wall_v', None)
            _wf = getattr(self.vp, '_bg_wall_f', None)
            self.vp._bg_face_v = self.vp._bg_face_f = None
            self.vp._bg_wall_v = self.vp._bg_wall_f = None

            if self.vp.face_mode == 3 and self.vp.face_thickness > 0:
                # Only Face — sadece yüz plakası; eski duvarı temizle
                self.vp._clear_body_mesh()
                if _fv is not None:
                    self.vp.apply_face_mesh(_fv, _ff)
                    self._set_status("Done ✓", 4000)
                    self._update_statusbar(len(_ff), elapsed)
                else:
                    self._set_status("Yüz oluşturulamadı", 4000)
            elif self.vp.face_mode == 2 and self.vp.face_thickness > 0:
                # Mod 2: floating face — geometrically separate, display with own color.
                self.vp.apply_mesh(v, f, b)
                if _fv is not None:
                    self.vp.apply_face_mesh(_fv, _ff)
                self._set_status("Done ✓", 4000)
                self._update_statusbar(len(f), elapsed)
            elif self.vp.face_mode == 1 and self.vp.face_thickness > 0:
                # Mod 1: duvar background'da hesaplandı (_bg_wall_v)
                self.vp.apply_mesh(_wv if _wv is not None else v,
                                   _wf if _wf is not None else f, b)
                if _fv is not None:
                    self.vp.apply_face_mesh(_fv, _ff)
                # Auto-group body+face for this slot
                self.vp._auto_group_mod1_slot(self.vp.active_slot_idx)
                self._set_status("Done ✓", 4000)
                self._update_statusbar(len(_wf) if _wf is not None else len(f), elapsed)
            else:
                self.vp.apply_mesh(v, f, b)
                self._set_status("Done ✓", 4000)
                self._update_statusbar(len(f), elapsed)
        except Exception as e:
            self._set_status("Error", 5000); QMessageBox.warning(self, "Error", str(e))
        finally:
            self.lp.gen_btn.setEnabled(True)
            self.lp.exp_btn.setEnabled(True)
            self.lp.exp3mf_btn.setEnabled(True)
            self.lp.tbot.setEnabled(True)
            for _b in [self.lp.face_tab, self.lp.mod2_btn,
                       self.lp.only_wall_btn, self.lp.ws1,
                       self.lp.ws2, self.lp.ws3]:
                _b.setEnabled(True)
            self._fut = None
            if getattr(self, '_regen_queued', False):
                self._regen_queued = False
                QTimer.singleShot(50, self._generate)   # kısa gecikme sonra yeniden üret
            if self._replay_cover:
                self._replay_cover = False
                self.vp.build_cover_preview()

    def _export(self, force_3mf=False):
        if force_3mf and self.vp._last_base is not None:
            p, _ = QFileDialog.getSaveFileName(self, "Export 3MF", "model.3mf", "3MF Files (*.3mf)")
            if not p: return
            if not p.lower().endswith(".3mf"): p += ".3mf"
            try:
                self.vp.export_mode2_3mf(p)
                QMessageBox.information(self, "3MF Saved", f"Kaydedildi:\n{p}")
            except Exception as e:
                QMessageBox.warning(self, "Export Error", str(e))
            return
        if self.vp.face_mode == 1 and self.vp._last_base is not None:
            # Mod 1: STL kullanılamaz — 3MF'ye yönlendir
            QMessageBox.information(self, "Mod 1 — 3MF Gerekli",
                "Mod 1'de STL export desteklenmez.\n\n"
                "Önyüz ve gövde Z konumlarını korumak için\n"
                "lütfen 3MF Export butonunu kullanın.")
            self._export_3mf_direct()

        elif self.vp.face_mode == 2 and self.vp._last_base is not None:
            p, _ = QFileDialog.getSaveFileName(self, "Export 3MF", "model.3mf", "3MF Files (*.3mf)")
            if not p: return
            if not p.lower().endswith(".3mf"): p += ".3mf"
            try:
                wall_info, face_info = self.vp.export_mode2_3mf(p)
                QMessageBox.information(self, "3MF Saved",
                    f"Mod 2 — gruplu 3MF kaydedildi.\n"
                    f"Duvar: {wall_info.get('out_f', 0):,} üçgen\n"
                    f"Yüzey: {face_info.get('out_f', 0):,} üçgen\n"
                    f"OrcaSlicer'da her parçaya ayrı renk atayın.")
            except Exception as e:
                QMessageBox.warning(self, "Export Error", str(e))

        else:
            p, _ = QFileDialog.getSaveFileName(self, "Export STL", "body.stl", "STL Files (*.stl)")
            if not p: return
            if not p.lower().endswith(".stl"): p += ".stl"
            try:
                info = self.vp.export_stl(p); nm = info.get("nm", 0)
                QMessageBox.information(self, "STL Saved",
                    f"Kaydedildi.\nÜçgen: {info.get('out_f', 0):,}\n"
                    f"{'Manifold temiz ✓' if nm == 0 else f'Uyarı: nm={nm}'}")
            except Exception as e:
                QMessageBox.warning(self, "STL Error", str(e))

    def _push_undo(self):
        """Save current scene state to undo stack (max 10)."""
        self._undo_stack.append(self.vp._snapshot())
        if len(self._undo_stack) > 10:
            self._undo_stack.pop(0)

    def _undo(self):
        """Restore previous scene state from undo stack."""
        if not self._undo_stack:
            self._set_status("Geri alınacak işlem yok.", 2000)
            return
        self.vp._restore_from_snapshot(self._undo_stack.pop())
        n = len(self._undo_stack)
        self._set_status(f"Geri alındı.{'  (' + str(n) + ' adım daha)' if n else ''}", 3000)

    def keyPressEvent(self, ev):
        from PySide6.QtCore import Qt
        if ev.key() == Qt.Key_Z and (ev.modifiers() & Qt.ControlModifier):
            self._undo()
            ev.accept()
            return
        if ev.key() == Qt.Key_Delete:
            key = self.vp.selected_obj_key or ""
            if key.startswith("cover_"):
                self._push_undo()
                self.vp.remove_cover()
                self._set_status("Back Cover kaldırıldı.", 3000)
                ev.accept()
                return
        super().keyPressEvent(ev)

    def _export_back_cover(self):
        if self.vp._last_base is None:
            QMessageBox.warning(self, "Back Cover", "Önce model oluşturun."); return
        from PySide6.QtWidgets import (QDialog, QFormLayout, QDialogButtonBox,
                                       QDoubleSpinBox, QPushButton, QHBoxLayout,
                                       QVBoxLayout)
        dlg = QDialog(self)
        dlg.setWindowTitle("Back Cover Ayarları")
        dlg.setMinimumWidth(280)

        sp_ct = QDoubleSpinBox(); sp_ct.setRange(0.5, 20.0); sp_ct.setDecimals(1)
        sp_ct.setSuffix(" mm"); sp_ct.setValue(self.vp.cover_ct)

        sp_wh = QDoubleSpinBox(); sp_wh.setRange(1.0, 200.0); sp_wh.setDecimals(1)
        sp_wh.setSuffix(" mm"); sp_wh.setValue(self.vp.cover_wh)

        sp_cl = QDoubleSpinBox(); sp_cl.setRange(0.0, 2.0); sp_cl.setDecimals(2)
        sp_cl.setSuffix(" mm"); sp_cl.setValue(self.vp.cover_clearance)

        sp_wt = QDoubleSpinBox(); sp_wt.setRange(0.5, 20.0); sp_wt.setDecimals(1)
        sp_wt.setSuffix(" mm"); sp_wt.setValue(self.vp.cover_wt)

        form = QFormLayout()
        form.addRow("Taban kalınlığı:",       sp_ct)
        form.addRow("İç duvar yüksekliği:",   sp_wh)
        form.addRow("Boşluk / Clearance:",    sp_cl)
        form.addRow("Tab duvar kalınlığı:",   sp_wt)

        # Standard OK / Cancel
        bbox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bbox.accepted.connect(dlg.accept)
        bbox.rejected.connect(dlg.reject)

        # Extra "Kaldır" button — removes cover and closes
        _removed = [False]
        btn_off = QPushButton("Back Cover'ı Kaldır")
        btn_off.setStyleSheet("color:#ff6655;")
        def _do_remove():
            _removed[0] = True
            dlg.reject()
        btn_off.clicked.connect(_do_remove)

        btn_row = QHBoxLayout()
        btn_row.addWidget(btn_off)
        btn_row.addStretch()

        vlay = QVBoxLayout(dlg)
        vlay.addLayout(form)
        vlay.addSpacing(6)
        vlay.addLayout(btn_row)
        vlay.addWidget(bbox)

        if dlg.exec() == QDialog.Accepted:
            self._push_undo()
            self.vp.cover_ct          = sp_ct.value()
            self.vp.cover_wh          = sp_wh.value()
            self.vp.cover_clearance   = sp_cl.value()
            self.vp.cover_wt          = sp_wt.value()
            self.vp.build_cover_preview()
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()
        elif _removed[0]:
            self._push_undo()
            self.vp.remove_cover()
            self._set_status("Back Cover kaldırıldı.", 3000)

    def _save_back_cover(self):
        if self.vp._last_base is None:
            QMessageBox.warning(self, "Back Cover", "Önce model oluşturun."); return
        # Ask format
        fmt, ok = QInputDialog.getItem(self, "Back Cover — Format",
            "Export formatı:", ["STL", "3MF"], 0, False)
        if not ok: return
        if fmt == "3MF":
            p, _ = QFileDialog.getSaveFileName(self, "Back Cover 3MF",
                "back_cover.3mf", "3MF Files (*.3mf)")
            if not p: return
            if not p.lower().endswith(".3mf"): p += ".3mf"
            try:
                info = self.vp.export_back_cover_3mf(p,
                    cover_thickness=self.vp.cover_ct,
                    wall_height=self.vp.cover_wh,
                    clearance=self.vp.cover_clearance,
                    wall_thickness=self.vp.cover_wt)
                QMessageBox.information(self, "Back Cover 3MF Kaydedildi",
                    f"Kaydedildi.\nCover: {info.get('cover_f', 0):,} üçgen")
            except Exception as e:
                QMessageBox.warning(self, "Back Cover Error", str(e))
        else:
            p, _ = QFileDialog.getSaveFileName(self, "Back Cover STL",
                "back_cover.stl", "STL Files (*.stl)")
            if not p: return
            if not p.lower().endswith(".stl"): p += ".stl"
            try:
                info = self.vp.export_back_cover_stl(p,
                    cover_thickness=self.vp.cover_ct,
                    wall_height=self.vp.cover_wh,
                    clearance=self.vp.cover_clearance,
                    wall_thickness=self.vp.cover_wt)
                QMessageBox.information(self, "Back Cover STL Kaydedildi",
                    f"Kaydedildi.\n{info.get('cover_f', 0):,} üçgen  ✓")
            except Exception as e:
                QMessageBox.warning(self, "Back Cover Error", str(e))

    def _set_wall_type(self, wtype):
        # Only Face modundaysa Mod 1'e geri dön + fill deseni sıfırla
        if self.vp.face_mode == 3:
            self.vp.face_mode  = 1
            self.vp.face_fill  = 0   # Solid'e dön
            self._update_mod_highlight()

        deg = 0.0
        if wtype != 0:
            h = max(self.vp.height_mm, 1.0)
            current_deg = round(math.degrees(math.atan(self.vp.wall_taper / h)), 1)
            deg, ok = QInputDialog.getDouble(self, "Duvar Açısı",
                "Eğim açısı (°):", current_deg, 0.1, 89.0, 1)
            if not ok: return
            self.vp.wall_taper = h * math.tan(math.radians(deg))
        type_names = {0: "Düz", 1: "Açılı", 2: "Kavisli"}
        info = type_names.get(wtype, "")
        if wtype != 0:
            info += f"  {deg:.1f}°"
        self._set_status(f"Duvar tipi: {info}", 3000)
        self.vp.wall_type = wtype
        if self.vp.paths: self._generate()

    # ── Profile helpers ───────────────────────────────────────────────────────
    def _load_profiles(self) -> dict:
        try:
            if self._profiles_path.exists():
                return json.loads(self._profiles_path.read_text(encoding='utf-8'))
        except Exception:
            pass
        return {}

    def _save_profiles(self):
        self._profiles_path.write_text(
            json.dumps(self._profiles, ensure_ascii=False, indent=2), encoding='utf-8')

    def _collect_settings(self) -> dict:
        return {
            'wall':           self.lp.wall.text(),
            'height':         self.lp.height.text(),
            'arc':            self.lp.arc.text(),
            'rc':             self.lp.rc.text(),
            'simp':           self.lp.simp.text(),
            'fp':             self.lp.fp.isChecked(),
            'wall_type':      self.vp.wall_type,
            'wall_taper':     self.vp.wall_taper,
            'bot_tab':        self.vp.bot_tab,
            'top_tab':        self.vp.top_tab,
            'bot_proj':       self.vp.bot_proj,
            'top_proj':       self.vp.top_proj,
            'bot_tab_z':      self.vp.bot_tab_z,
            'top_tab_z':      self.vp.top_tab_z,
            'face_mode':      self.vp.face_mode,
            'face_thickness': self.vp.face_thickness,
            'face_fill':           getattr(self.vp, 'face_fill', 0),
            'face_fill_cell':      getattr(self.vp, 'face_fill_cell', 8.0),
            'face_fill_wall':      getattr(self.vp, 'face_fill_wall', 1.6),
            'face_fill_solid_pos': getattr(self.vp, 'face_fill_solid_pos', 0),
            'face_fill_border':    getattr(self.vp, 'face_fill_border', 1.5),
            'cover_ct':       self.vp.cover_ct,
            'cover_wh':       self.vp.cover_wh,
            'cover_clearance':self.vp.cover_clearance,
            'cover_wt':       self.vp.cover_wt,
            'my':             self.vp.my,
            'mx':             self.vp.mx,
        }

    def _apply_settings(self, s: dict):
        self.lp.wall.setText(s['wall'])
        self.lp.height.setText(s['height'])
        self.lp.arc.setText(s['arc'])
        self.lp.rc.setText(s['rc'])
        self.lp.simp.setText(s['simp'])
        self.lp.fp.setChecked(s['fp'])
        self.vp.wall_type       = s['wall_type']
        self.vp.wall_taper      = s['wall_taper']
        # Tab aktif durumları RESTORE EDİLMEZ — her oturumda manuel aktive edilir.
        # Değerler (proj, z) restore edilir, buton durumu (tab=True/False) restore edilmez.
        self.vp.bot_tab         = False
        self.vp.top_tab         = False
        self.vp.bot_proj        = s['bot_proj']
        self.vp.top_proj        = s['top_proj']
        self.vp.bot_tab_z       = s['bot_tab_z']
        self.vp.top_tab_z       = s['top_tab_z']
        self.vp.face_mode       = s['face_mode']
        self.vp.face_thickness  = s['face_thickness']
        self.vp.face_fill           = s.get('face_fill', 0)
        self.vp.face_fill_cell      = s.get('face_fill_cell', 8.0)
        self.vp.face_fill_wall      = s.get('face_fill_wall', 1.6)
        self.vp.face_fill_solid_pos = s.get('face_fill_solid_pos', 0)
        self.vp.face_fill_border    = s.get('face_fill_border', 1.5)
        self.vp.cover_ct        = s['cover_ct']
        self.vp.cover_wh        = s['cover_wh']
        self.vp.cover_clearance = s['cover_clearance']
        self.vp.cover_wt        = s['cover_wt']
        if s['my'] != self.vp.my: self.vp.toggle_my()
        if s['mx'] != self.vp.mx: self.vp.toggle_mx()
        self._update_mod_highlight()
        self._update_slot_highlight()

    def _set_language(self, code: str):
        set_lang(code)
        try:
            self._lang_path.write_text(
                json.dumps({"lang": code}), encoding='utf-8')
        except Exception:
            pass
        QMessageBox.information(self, _t("lang_restart_title"), _t("lang_restart_msg"))

    def _open_guide(self):
        import webbrowser
        webbrowser.open("https://harfex3d.com")

    def _about(self):
        QMessageBox.about(self, _t("about_title"), _t("about_body"))

    # ── R / P ─────────────────────────────────────────────────────────────────
    def _replay_record(self):
        """R: Save current settings as a named profile."""
        name, ok = QInputDialog.getText(self, _t("prof_save_title"), _t("prof_save_prompt"))
        if not ok or not name.strip(): return
        name = name.strip()
        if name in self._profiles:
            r = QMessageBox.question(self, _t("prof_save_title"),
                _t("prof_overwrite", name=name),
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes: return
        self._profiles[name] = self._collect_settings()
        self._replay_settings = self._profiles[name]
        self._save_profiles()
        self._set_status(_t("prof_saved", name=name), 3000)

    def _replay_play(self):
        """P: Select a profile and apply it to current DXF."""
        if not self._profiles:
            QMessageBox.information(self, _t("menu_profile"), _t("prof_empty"))
            return
        dlg = ProfileDialog(self._profiles, parent=self)
        if dlg.exec() != QDialog.Accepted: return
        action = dlg.action
        if action == 'rename' or action == 'delete':
            self._save_profiles()
            self._set_status("Profil listesi güncellendi ✓", 2000)
            return
        if action == 'update':
            name = dlg.selected_name
            self._profiles[name] = self._collect_settings()
            self._replay_settings = self._profiles[name]
            self._save_profiles()
            self._set_status(f'Profil güncellendi: {name} ✓', 3000)
            return
        if action == 'apply':
            if not self.vp.paths:
                QMessageBox.warning(self, _t("menu_profile"), _t("prof_no_dxf")); return
            name = dlg.selected_name
            self._apply_settings(self._profiles[name])
            self._replay_settings = self._profiles[name]
            self._replay_cover = True
            self._generate()
            self._set_status(f'Profil uygulandı: {name}', 3000)

    def _slot_dim(self):
        """Slot Dim — alt ve üst tırnak genişliklerini ayrı ayrı ayarla.
        Görsel: Top Slot butonu → bot_tab/bot_proj  (z~0, ekranda üst)
                Bottom butonu   → top_tab/top_proj  (z~H, ekranda alt)
        Diyalog: sp_bot=Alt Slot Dim → top_proj,  sp_top=Üst Slot Dim → bot_proj"""
        dlg = SlotDimDialog(self.vp.top_proj, self.vp.bot_proj, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        # sp_bot → alt (görsel) → top_proj ;  sp_top → üst (görsel) → bot_proj
        bot_v = dlg.sp_bot.value()   # "Alt Slot Dim"  → top_proj
        top_v = dlg.sp_top.value()   # "Üst Slot Dim"  → bot_proj

        changed = False

        # Alt slot dim (görsel alt = Bottom butonu = top_tab/top_proj)
        if bot_v <= 0.0:
            if self.vp.top_tab or self.vp.top_proj > 0.0:
                self.vp.top_tab  = False
                self.vp.top_proj = 0.0
                changed = True
        else:
            if self.vp.top_proj != bot_v:
                self.vp.top_proj = bot_v
                changed = True

        # Üst slot dim (görsel üst = Top Slot butonu = bot_tab/bot_proj)
        if top_v <= 0.0:
            if self.vp.bot_tab or self.vp.bot_proj > 0.0:
                self.vp.bot_tab  = False
                self.vp.bot_proj = 0.0
                changed = True
        else:
            if self.vp.bot_proj != top_v:
                self.vp.bot_proj = top_v
                changed = True

        self._update_slot_highlight()

        parts = []
        if bot_v > 0.0: parts.append(f"alt {bot_v:.1f} mm")
        else:            parts.append("alt kapalı")
        if top_v > 0.0: parts.append(f"üst {top_v:.1f} mm")
        else:            parts.append("üst kapalı")
        self._set_status("Slot: " + ", ".join(parts), 2500)

        if changed and (self.vp.bot_tab or self.vp.top_tab) and self.vp.paths:
            self._generate()

    def _top_slot(self):
        """Top Slot butonu — modelin ÜSTÜNDE tırnak slotu (top_tab/top_proj/top_tab_z).
        Tekrar basılırsa slotu kapatır — generate tetiklenmez, çalışan işlem iptal edilir.
        top_tab_z: negatif giriş (örn: -2 → üst kenardan 2 mm içeride)."""
        if self.vp.top_tab:
            self.vp.top_tab = False
            self._update_slot_highlight()
            # Çalışan generate varsa iptal et (sonucu atar, mesh uygulanmaz)
            if self._fut and not self._fut.done():
                self._cancel_pending = True
            self._set_status("Top slot kapatıldı — Generate ile güncelle", 2000)
            return
        # 1) Tırnak büyüklüğü (projeksiyon)
        proj, ok1 = QInputDialog.getDouble(self, "Top Slot",
            "Tırnak genişliği / proj (mm, örn: 4):",
            self.vp.top_proj if self.vp.top_proj > 0 else 4.0,
            0.1, 60.0, 1)
        if not ok1: return
        # 2) Üst kenardan mesafe (negatif girilir, örn: -2)
        z, ok2 = QInputDialog.getDouble(self, "Top Slot",
            "Üst kenardan mesafe (negatif — örn: -2):",
            self.vp.top_tab_z if self.vp.top_tab_z < 0 else -2.0,
            -self.vp.height_mm, -0.1, 1)
        if not ok2: return
        self.vp.top_proj  = proj
        self.vp.top_tab_z = z
        self.vp.top_tab   = True
        self._update_slot_highlight()
        self._set_status(f"Top slot aktif — proj {proj:.1f} mm, kenardan {abs(z):.1f} mm", 2500)
        if self.vp.paths: self._generate()

    def _bot_slot(self):
        """Bottom Slot butonu — modelin ALTINDA tırnak slotu (bot_tab/bot_proj/bot_tab_z).
        Tekrar basılırsa slotu kapatır — generate tetiklenmez, çalışan işlem iptal edilir.
        bot_tab_z: negatif giriş (örn: -2 → alt kenardan 2 mm içeride)."""
        if self.vp.bot_tab:
            self.vp.bot_tab = False
            self._update_slot_highlight()
            if self._fut and not self._fut.done():
                self._cancel_pending = True
            self._set_status("Bottom slot kapatıldı — Generate ile güncelle", 2000)
            return
        # 1) Tırnak büyüklüğü (projeksiyon)
        proj, ok1 = QInputDialog.getDouble(self, "Bottom Slot",
            "Tırnak genişliği / proj (mm, örn: 4):",
            self.vp.bot_proj if self.vp.bot_proj > 0 else 4.0,
            0.1, 60.0, 1)
        if not ok1: return
        # 2) Alt kenardan mesafe (negatif girilir, örn: -2)
        z_in, ok2 = QInputDialog.getDouble(self, "Bottom Slot",
            "Alt kenardan mesafe (negatif — örn: -2):",
            -abs(self.vp.bot_tab_z) if self.vp.bot_tab_z != 0 else -2.0,
            -self.vp.height_mm, -0.1, 1)
        if not ok2: return
        self.vp.bot_proj  = proj
        self.vp.bot_tab_z = abs(z_in)   # _tab_z_range('bot') pozitif bekler
        self.vp.bot_tab   = True
        self._update_slot_highlight()
        self._set_status(f"Bottom slot aktif — proj {proj:.1f} mm, kenardan {abs(z_in):.1f} mm", 2500)
        if self.vp.paths: self._generate()

    def _my(self):
        self.vp.toggle_my()
        # Flip the F icon: mirrored when active, normal when inactive
        if self.lp._mirror_icon and self.lp._f_pix_normal and self.lp._f_pix_mirror:
            pix = self.lp._f_pix_mirror if self.vp.my else self.lp._f_pix_normal
            self.lp._mirror_icon.setPixmap(pix)

    def _face_settings(self):
        modes = ["Off", "Outer Face (Mod 1)", "Inner Face (Mod 2)", "Plexiglas Cut File (Mod 3)"]
        cur = modes[min(self.vp.face_mode, 3)]
        mode_str, ok = QInputDialog.getItem(self, "Face Plate", "Mode:", modes, modes.index(cur), False)
        if not ok: return
        mode_idx = modes.index(mode_str)

        if mode_idx in (1, 2):
            th, ok2 = QInputDialog.getDouble(self, "Face Plate",
                "Thickness (mm):", self.vp.face_thickness, 0.0, 20.0, 1)
            if not ok2: return
            self.vp.face_thickness = th
            self.vp.face_mode = mode_idx
            self._update_mod_highlight()
            if self.vp.paths: self._generate()

        elif mode_idx == 3:
            tip_str, ok2 = QInputDialog.getItem(
                self, "Plexiglas Cut", "Plexiglas türü:",
                ["Kanal — Kanala yerleşen (iç offset)", "Kapak — Dış kapak (dış offset)"],
                0, False)
            if not ok2: return
            plex_mode = "kanal" if tip_str.startswith("Kanal") else "kapak"
            ins, ok3 = QInputDialog.getDouble(self, "Plexiglas Cut",
                "Offset (mm)  + = dışa genişlet  − = içe küçült:", self.vp.plexiglas_offset, -20.0, 20.0, 2)
            if not ok3: return
            self.vp.plexiglas_offset = ins
            fmt_str, ok4 = QInputDialog.getItem(self, "Plexiglas Cut",
                "Export format:", ["DXF", "SVG", "EPS"], 0, False)
            if not ok4: return
            fmt = fmt_str.lower()
            ext_map = {"dxf": "DXF Files (*.dxf)", "svg": "SVG Files (*.svg)", "eps": "EPS Files (*.eps)"}
            fp, _ = QFileDialog.getSaveFileName(self, "Export Plexiglas Cut",
                f"plexiglas_{plex_mode}.{fmt}", ext_map[fmt])
            if not fp: return
            if not fp.lower().endswith(f".{fmt}"): fp += f".{fmt}"
            try:
                self.vp.export_plexiglas(fp, fmt, mode=plex_mode)
                tip_lbl = "Kanal (iç)" if plex_mode == "kanal" else "Kapak (dış)"
                QMessageBox.information(self, "Plexiglas Cut", f"{tip_lbl} — Kaydedildi: {fp}")
            except Exception as e:
                QMessageBox.warning(self, "Export Error", str(e))

        else:  # Off
            self.vp.face_mode = 0
            self._update_mod_highlight()
            if self.vp.paths: self._generate()

    def _color_separate(self):
        """Open color assignment dialog for wall and face filament colors."""
        self.vp.show_color_dialog(parent=self)

    def _export_3mf_direct(self):
        """Export 3MF — includes only objects whose checkbox is ticked in Objects list."""
        if not self.vp.scene_objects:
            QMessageBox.warning(self, "Export 3MF", "Önce model oluşturun.")
            return

        slot_idx   = self.vp.active_slot_idx
        so         = self.vp.scene_objects
        # Read which objects are currently visible (checked) in Objects list
        body_on    = so.get(f"body_{slot_idx}",  {}).get("visible", False)
        face_on    = so.get(f"face_{slot_idx}",  {}).get("visible", False)
        cover_on   = so.get(f"cover_{slot_idx}", {}).get("visible", False)

        if self.vp.face_mode == 1 and self.vp.last_face_v is not None:
            # ── Mod 1: component-grouped 3MF ──────────────────────────────────
            # If nothing is ticked, treat as if everything is selected (export all)
            if not body_on and not face_on and not cover_on:
                body_on  = f"body_{slot_idx}"  in so
                face_on  = f"face_{slot_idx}"  in so
                cover_on = f"cover_{slot_idx}" in so
            p, _ = QFileDialog.getSaveFileName(self, "Export 3MF — Mod 1",
                                               "model.3mf", "3MF Files (*.3mf)")
            if not p: return
            if not p.lower().endswith(".3mf"): p += ".3mf"
            try:
                wall_info, face_info = self.vp.export_mod1_3mf(
                    p,
                    include_body=body_on,
                    include_face=face_on,
                    include_cover=cover_on)
                lines = ["Kaydedildi:\n" + p + "\n"]
                if body_on:
                    lines.append(f"  • Gövde: {wall_info.get('out_f',0):,} üçgen (Filament 1)")
                if face_on:
                    lines.append(f"  • Önyüz: {face_info.get('out_f',0):,} üçgen (Filament 2)")
                if cover_on:
                    lines.append(f"  • Back Cover: ek parça olarak")
                QMessageBox.information(self, "3MF Kaydedildi", "\n".join(lines))
            except Exception as e:
                QMessageBox.warning(self, "Export Error", str(e))
            return

        # ── Genel / Mod 2 case ────────────────────────────────────────────────
        # Use visibility flags from scene_objects as selection;
        # if nothing is ticked, include all objects (export all by default)
        keys = [k for k, obj in so.items() if obj.get("visible", False)]
        if not keys:
            keys = list(so.keys())
        p, _ = QFileDialog.getSaveFileName(self, "Export 3MF", "model.3mf",
                                           "3MF Files (*.3mf)")
        if not p: return
        if not p.lower().endswith(".3mf"): p += ".3mf"
        try:
            info = self.vp.export_selected_3mf(p, keys)
            detail = "\n".join(f"  • {lbl}: {tri:,} üçgen"
                               for lbl, tri in info.items())
            QMessageBox.information(self, "3MF Kaydedildi",
                                    f"Kaydedildi:\n{p}\n\n{detail}")
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _refresh_object_list(self):
        """Rebuild the Objects checkbox list in the left panel."""
        self.lp.refresh_objects(self.vp.scene_objects,
                                on_select=self._select_object_by_key)

    def _select_object_by_key(self, key):
        """Select (highlight) an object by key — called from left panel ◉ button."""
        obj = self.vp.scene_objects.get(key)
        if obj is None:
            return
        self.vp.set_selected(key)
        keys = self.vp.selected_obj_keys
        if len(keys) > 1:
            labels = [self.vp.scene_objects.get(k, {}).get("label", k) for k in keys]
            self._set_status(f"Seçili: {', '.join(labels)}", 4000)
        else:
            label = obj.get("label", key)
            tri   = len(obj.get("f", [])) if obj.get("f") is not None else 0
            self._set_status(f"Seçili: {label}  ({tri:,} üçgen)", 4000)

    def _on_slot_switch(self, slot_idx):
        """Called when active slot changes — update UI inputs to match slot's params."""
        vp = self.vp
        try:
            self.lp.height.setText(f"{vp.height_mm:.1f}")
            self.lp.wall.setText(f"{vp.wall_mm:.1f}")
            self.lp.fp_val.setText(f"{vp.face_thickness:.1f}")
        except Exception:
            pass
        self._update_mod_highlight()
        self._update_slot_highlight()
        slot_label = vp.slots[slot_idx].label
        self._set_status(f"Aktif: {slot_label}", 3000)

    def _on_object_clicked(self, key, ctrl_held=False):
        """Handle object selection from 3D view mouse click (Ctrl = multi-select)."""
        if not key:
            self.vp.deselect_all()
            self._set_status("", 0)
            return

        obj      = self.vp.scene_objects.get(key, {})
        label    = obj.get("label", key)
        slot_idx = obj.get("slot_idx", self.vp.active_slot_idx)

        # Switch active slot if needed (only on single-click)
        if not ctrl_held and slot_idx != self.vp.active_slot_idx:
            self.vp.switch_to_slot(slot_idx)

        if ctrl_held:
            # Ctrl+click → toggle in multi-select
            self.vp.set_selected_add(key)
            n = len(self.vp.selected_obj_keys)
            if n == 0:
                self._set_status("", 0)
            elif n == 1:
                only = next(iter(self.vp.selected_obj_keys))
                lbl2 = self.vp.scene_objects.get(only, {}).get("label", only)
                self._set_status(f"Seçili: {lbl2}", 4000)
            else:
                labels = [self.vp.scene_objects.get(k, {}).get("label", k)
                          for k in self.vp.selected_obj_keys]
                self._set_status(
                    f"{n} nesne seçili: {', '.join(labels)}  —  Grup Oluştur butonunu kullan", 5000)
        else:
            # Normal click — always select the picked object (switch or re-affirm).
            # Never deselect on same-key: accidental same-key pick from ambiguous area
            # would otherwise deselect the current object instead of switching.
            # To deselect: click empty space (key="") handled above.
            self.vp.set_selected(key)
            keys = self.vp.selected_obj_keys
            if len(keys) > 1:
                labels = [self.vp.scene_objects.get(k, {}).get("label", k) for k in keys]
                gid = obj.get("group_id", "")
                self._set_status(
                    f"Grup seçili ({gid}): {', '.join(labels)}", 4000)
            else:
                tri = len(obj.get("f", [])) if obj.get("f") is not None else 0
                self._set_status(
                    f"Seçili: {label}  ({tri:,} üçgen) — boş alana tıkla: kaldır", 4000)

    # ── Right-click context menu in viewport ──────────────────────────────────
    def _on_object_right_clicked(self, key, global_pos):
        """Show context menu when user right-clicks an object in the 3D viewport."""
        if not key:
            return   # clicked empty space — no menu

        obj   = self.vp.scene_objects.get(key, {})
        label = obj.get("label", key)

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#1e1e1e;color:#cccccc;border:1px solid #444;"
            "font-family:'Segoe UI',Arial;font-size:12px;}"
            "QMenu::item{padding:6px 24px 6px 12px;}"
            "QMenu::item:selected{background:#2277ff;color:#fff;}"
            "QMenu::separator{height:1px;background:#444;margin:3px 0;}"
        )
        title_act = menu.addAction(f"  {label}")
        title_act.setEnabled(False)
        menu.addSeparator()

        if key.startswith("cover_"):
            # ── Back Cover context ──────────────────────────────────────────
            act_settings = menu.addAction("⚙  Cover Ayarları…")
            act_stl      = menu.addAction("↓  STL Dışa Aktar…")
            act_3mf      = menu.addAction("↓  3MF Dışa Aktar…")

            chosen = menu.exec(global_pos)
            if chosen == act_settings:
                self._export_back_cover()
            elif chosen == act_stl:
                self._save_back_cover_fmt("STL")
            elif chosen == act_3mf:
                self._save_back_cover_fmt("3MF")

        elif key.startswith("body_") or key.startswith("face_"):
            # ── Main body / face context ────────────────────────────────────
            act_stl    = menu.addAction("↓  STL Dışa Aktar…")
            act_3mf    = menu.addAction("↓  3MF Dışa Aktar…")
            menu.addSeparator()
            act_color  = menu.addAction("🎨  Renk Değiştir…")

            chosen = menu.exec(global_pos)
            if chosen == act_stl:
                self._export_selected_stl(key)
            elif chosen == act_3mf:
                self._export_3mf_direct()
            elif chosen == act_color:
                self._color_separate()

    def _save_back_cover_fmt(self, fmt):
        """Export back cover directly in given format ('STL' or '3MF')."""
        if self.vp._last_base is None:
            QMessageBox.warning(self, "Back Cover", "Önce model oluşturun."); return
        if fmt == "3MF":
            p, _ = QFileDialog.getSaveFileName(self, "Back Cover 3MF",
                "back_cover.3mf", "3MF Files (*.3mf)")
            if not p: return
            if not p.lower().endswith(".3mf"): p += ".3mf"
            try:
                info = self.vp.export_back_cover_3mf(p,
                    cover_thickness=self.vp.cover_ct,
                    wall_height=self.vp.cover_wh,
                    clearance=self.vp.cover_clearance,
                    wall_thickness=self.vp.cover_wt)
                QMessageBox.information(self, "Back Cover 3MF",
                    f"Kaydedildi.\nCover: {info.get('cover_f', 0):,} üçgen")
            except Exception as e:
                QMessageBox.warning(self, "Back Cover Error", str(e))
        else:
            p, _ = QFileDialog.getSaveFileName(self, "Back Cover STL",
                "back_cover.stl", "STL Files (*.stl)")
            if not p: return
            if not p.lower().endswith(".stl"): p += ".stl"
            try:
                info = self.vp.export_back_cover_stl(p,
                    cover_thickness=self.vp.cover_ct,
                    wall_height=self.vp.cover_wh,
                    clearance=self.vp.cover_clearance,
                    wall_thickness=self.vp.cover_wt)
                QMessageBox.information(self, "Back Cover STL",
                    f"Kaydedildi.\n{info.get('cover_f', 0):,} üçgen  ✓")
            except Exception as e:
                QMessageBox.warning(self, "Back Cover Error", str(e))

    def _export_selected_stl(self, key):
        """Export only the selected object (body or face) as STL."""
        obj = self.vp.scene_objects.get(key, {})
        v   = obj.get("v")
        f   = obj.get("f")
        if v is None or f is None:
            QMessageBox.warning(self, "Export STL", "Mesh verisi bulunamadı."); return
        label = obj.get("label", key)
        p, _ = QFileDialog.getSaveFileName(self, f"STL — {label}",
            f"{label.replace(' ', '_')}.stl", "STL Files (*.stl)")
        if not p: return
        if not p.lower().endswith(".stl"): p += ".stl"
        try:
            import struct
            va = np.asarray(v, dtype=np.float32)
            fa = np.asarray(f, dtype=np.int32)
            with open(p, "wb") as fh:
                fh.write(b"\x00" * 80)
                fh.write(struct.pack("<I", len(fa)))
                for tri in fa:
                    t  = va[tri]
                    n  = np.cross(t[1] - t[0], t[2] - t[0]).astype(np.float32)
                    ln = np.linalg.norm(n)
                    if ln > 1e-12: n /= ln
                    fh.write(struct.pack("<fff", *n))
                    for vt in t:
                        fh.write(struct.pack("<fff", *vt))
                    fh.write(b"\x00\x00")
            QMessageBox.information(self, "STL Dışa Aktar",
                f"Kaydedildi:\n{p}\n{len(fa):,} üçgen")
        except Exception as e:
            QMessageBox.warning(self, "STL Error", str(e))

    def _show_former(self):
        """Former butonuna basınca Harfex öne gelir, OrcaSlicer arka planda kalır."""
        self._view_stack.setCurrentIndex(0)
        # Harfex penceresini öne getir
        hwnd = int(self.winId())
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(hwnd, 9)       # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self.activateWindow()
        self.raise_()
        self._set_status("Former — modelleme sahnesi", 2000)

    def _prepare(self):
        """Prepare — 3MF export edip OrcaSlicer'ı --no-splash ile aç."""
        if not self.vp.scene_objects:
            QMessageBox.information(self, "Prepare",
                "Önce model oluşturun (Generate 3D).")
            return

        if not getattr(self, '_orca', None):
            QMessageBox.warning(self, "Prepare",
                "OrcaSlicer bulunamadı.\n"
                "Portable klasörü uygulama dizinine koyun.")
            return

        # ── 3MF export ────────────────────────────────────────────────────
        import tempfile
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".3mf", delete=False,
                                              prefix="harfex_prepare_")
            tmp_path = tmp.name; tmp.close()
            slot_idx = self.vp.active_slot_idx
            so       = self.vp.scene_objects
            if self.vp.face_mode == 1 and self.vp.last_face_v is not None:
                body_on  = so.get(f"body_{slot_idx}", {}).get("visible", False)
                face_on  = so.get(f"face_{slot_idx}", {}).get("visible", False)
                cover_on = so.get(f"cover_{slot_idx}", {}).get("visible", False)
                if not body_on and not face_on and not cover_on:
                    body_on  = f"body_{slot_idx}" in so
                    face_on  = f"face_{slot_idx}" in so
                    cover_on = f"cover_{slot_idx}" in so
                self.vp.export_mod1_3mf(tmp_path,
                    include_body=body_on, include_face=face_on,
                    include_cover=cover_on)
            else:
                keys = [k for k, obj in so.items() if obj.get("visible", False)]
                if not keys: keys = list(so.keys())
                self.vp.export_selected_3mf(tmp_path, keys)
        except Exception as e:
            QMessageBox.warning(self, "Prepare", f"3MF oluşturulamadı:\n{e}")
            return

        # ── OrcaSlicer'ı aç / öne getir ──────────────────────────────────
        self._orca.open_with_model(tmp_path)
        self._set_status("Model OrcaSlicer'a gönderildi — Former ile geri dönün", 6000)

    def _fill_btn_context_menu(self, pos):
        """Fill Des. butonuna sağ tık → hızlı menü."""
        menu = QMenu(self)
        menu.addAction("Fill Des. Ayarları…",  self._face_fill_dialog)
        menu.addSeparator()
        menu.addAction("Fill Pattern DXF Aktar…", self._export_fill_dxf)
        menu.exec(self.lp.tbot.mapToGlobal(pos))

    def _face_fill_dialog(self):
        """Open FaceFillDialog (Fill Des. button) — choose pattern and density."""
        dlg = FaceFillDialog(
            self,
            fill_idx  = getattr(self.vp, 'face_fill', 0),
            cell_mm   = getattr(self.vp, 'face_fill_cell', 8.0),
            wall_mm   = getattr(self.vp, 'face_fill_wall', 1.6),
            solid_pos = getattr(self.vp, 'face_fill_solid_pos', 0),
            border_mm = getattr(self.vp, 'face_fill_border', 1.5),
            only_face = (self.vp.face_mode == 3),
        )
        if dlg.exec() == QDialog.Accepted:
            self.vp.face_fill           = dlg.fill_idx
            self.vp.face_fill_cell      = dlg.cell_mm
            self.vp.face_fill_wall      = dlg.wall_mm
            self.vp.face_fill_solid_pos = dlg.solid_pos
            self.vp.face_fill_border    = dlg.border_mm
            # Sadece yüz / normal mod geçişi
            if dlg.only_face:
                if self.vp.face_thickness <= 0.0:
                    self.vp.face_thickness = 3.0
                self.vp.face_mode = 3
            else:
                if self.vp.face_mode == 3:   # only_face'den çıkıldı → Mod 1'e dön
                    self.vp.face_mode = 1
            self._update_mod_highlight()
            _names = {0: "Solid", 1: "Honeycomb",
                      2: "Grid", 3: "Lines"}
            self._set_status(
                f"Yüzey desen: {_names.get(dlg.fill_idx, '')}  "
                f"hücre={dlg.cell_mm:.1f} mm  duvar={dlg.wall_mm:.1f} mm", 4000)
            if self.vp.paths and self.vp.face_mode != 0:
                self._generate()

    def _face_mode_direct(self, mode):
        """Set face mode directly (1=Outer, 2=Inner) and ask thickness if needed."""
        if self.vp.face_thickness <= 0.0:
            self.vp.face_thickness = 3.0
        th, ok = QInputDialog.getDouble(self, "Face Thickness",
            "Kalınlık (mm):", self.vp.face_thickness, 0.0, 20.0, 1)
        if not ok: return
        self.vp.face_thickness = th
        self.vp.face_mode = mode
        self._update_mod_highlight()
        mode_names = {1: "Mod 1 (Outer)", 2: "Mod 2 (Inner)"}
        self._set_status(f"Yüzey: {mode_names.get(mode, '')}  {th:.1f} mm", 3000)
        if self.vp.paths: self._generate()

    def _only_wall(self):
        """Only Wall — sadece duvar (face_mode=0). Wall Height + Wall Thickness değerleri
        otomatik kullanılır. Tırnak slotları aktifse dahil edilir."""
        self.vp.face_mode = 0
        self.vp.face_fill = 0   # Only Wall'da fill anlamsız — sıfırla
        self._update_mod_highlight()
        self._set_status("Only Wall — sadece duvar", 2500)
        if self.vp.paths: self._generate()

    def _export_foam(self):
        """Export back-foam DXF — outer letter contour (wide end for tapered walls)."""
        if self.vp._last_base is None:
            QMessageBox.warning(self, "Back Foam DXF", "Önce model oluşturun."); return
        from PySide6.QtWidgets import (QDialog, QFormLayout, QDialogButtonBox,
                                       QDoubleSpinBox, QVBoxLayout)
        dlg = QDialog(self); dlg.setWindowTitle("Back Foam DXF"); dlg.setMinimumWidth(260)
        sp_off = QDoubleSpinBox(); sp_off.setRange(-50.0, 50.0); sp_off.setDecimals(2)
        sp_off.setSuffix(" mm"); sp_off.setValue(self.vp.foam_offset)
        sp_cr  = QDoubleSpinBox(); sp_cr.setRange(0.0, 30.0); sp_cr.setDecimals(1)
        sp_cr.setSuffix(" mm"); sp_cr.setValue(self.vp.foam_corner_r)
        sp_cr.setSpecialValueText("Yok (0)")
        form = QFormLayout()
        form.addRow("Offset (+ dışa / − içe):", sp_off)
        form.addRow("Köşe yumuşatma yarıçapı:", sp_cr)
        bbox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bbox.accepted.connect(dlg.accept); bbox.rejected.connect(dlg.reject)
        vlay = QVBoxLayout(dlg); vlay.addLayout(form); vlay.addWidget(bbox)
        if dlg.exec() != QDialog.Accepted: return
        self.vp.foam_offset   = sp_off.value()
        self.vp.foam_corner_r = sp_cr.value()
        fp, _ = QFileDialog.getSaveFileName(self, "Back Foam DXF", "back_foam.dxf",
                                            "DXF Files (*.dxf)")
        if not fp: return
        if not fp.lower().endswith(".dxf"): fp += ".dxf"
        try:
            self.vp.export_foam(fp, offset=self.vp.foam_offset,
                                corner_radius=self.vp.foam_corner_r)
            cr_txt = f"  |  Köşe: {self.vp.foam_corner_r} mm" if self.vp.foam_corner_r > 0 else ""
            QMessageBox.information(self, "Back Foam DXF", f"Kaydedildi:\n{fp}{cr_txt}")
        except Exception as e:
            QMessageBox.warning(self, "Back Foam DXF Error", str(e))

    def _export_fill_dxf(self):
        """Export face fill pattern (honeycomb/grid/lines) as 2D DXF."""
        if self.vp._last_base is None:
            QMessageBox.warning(self, "Fill DXF", "Önce model oluşturun."); return
        fill = getattr(self.vp, 'face_fill', 0)
        if fill == 0:
            QMessageBox.warning(self, "Fill DXF",
                "Fill Des. menüsünden önce bir desen seçin (Honeycomb/Grid/Lines)."); return
        if self.vp.face_mode == 0:
            QMessageBox.warning(self, "Fill DXF", "Yüzey modu aktif değil."); return

        fp, _ = QFileDialog.getSaveFileName(
            self, "Fill Pattern DXF", "fill_pattern.dxf", "DXF Files (*.dxf)")
        if not fp: return
        if not fp.lower().endswith(".dxf"): fp += ".dxf"

        try:
            if not ezdxf:
                raise RuntimeError("ezdxf kurulu değil: pip install ezdxf")

            # Yüz geometrisini al
            geom = (self.vp._last_base if self.vp.face_mode == 1
                    else self.vp._last_ig)
            if geom is None or geom.is_empty:
                raise RuntimeError("Yüzey geometrisi boş.")

            # Fill deseni uygula
            cell = getattr(self.vp, 'face_fill_cell', 8.0)
            wall = getattr(self.vp, 'face_fill_wall', 1.6)
            filled = _face_fill_pattern(geom, fill, cell, wall)

            # Tüm ring'leri al (dış kontur + iç delikler)
            def _rings(g):
                polys = _as_polys(g, 0.01)
                rings = []
                for p in polys:
                    rings.append(list(p.exterior.coords))
                    for h in p.interiors:
                        rings.append(list(h.coords))
                return rings

            rings = _rings(filled)
            if not rings:
                raise RuntimeError("Desen oluşturulamadı.")

            doc = ezdxf.new("R2010")
            msp = doc.modelspace()
            for r in rings:
                pts = [(float(x), float(y), 0.0) for x, y in r]
                msp.add_lwpolyline(pts, close=True)
            doc.saveas(fp)

            _names = {1: "Honeycomb", 2: "Grid", 3: "Lines"}
            QMessageBox.information(self, "Fill DXF",
                f"{_names.get(fill, 'Desen')} DXF kaydedildi:\n{fp}\n"
                f"({len(rings)} ring, hücre={cell:.1f} mm, duvar={wall:.1f} mm)")
        except Exception as e:
            QMessageBox.warning(self, "Fill DXF Error", str(e))

    def _export_plexiglas(self):
        """Export plexiglas cut DXF/SVG — kanal (inner) or kapak (outer) mode."""
        if self.vp._last_base is None:
            QMessageBox.warning(self, "Plexiglas", "Önce model oluşturun."); return
        from PySide6.QtWidgets import (QDialog, QFormLayout, QDialogButtonBox,
                                       QDoubleSpinBox, QComboBox, QVBoxLayout)
        dlg = QDialog(self); dlg.setWindowTitle("Plexiglas Cut"); dlg.setMinimumWidth(280)
        cb_tip = QComboBox()
        cb_tip.addItems(["Kanal — Kanala yerleşen (iç)", "Kapak — Dış kapak"])
        cb_tip.setCurrentIndex(0)
        sp_off = QDoubleSpinBox(); sp_off.setRange(-50.0, 50.0); sp_off.setDecimals(2)
        sp_off.setSuffix(" mm"); sp_off.setValue(self.vp.plexiglas_offset)
        sp_cr  = QDoubleSpinBox(); sp_cr.setRange(0.0, 30.0); sp_cr.setDecimals(1)
        sp_cr.setSuffix(" mm"); sp_cr.setValue(self.vp.plexiglas_corner_r)
        sp_cr.setSpecialValueText("Yok (0)")
        cb_fmt = QComboBox(); cb_fmt.addItems(["DXF", "SVG", "EPS"])
        form = QFormLayout()
        form.addRow("Plexiglas türü:",              cb_tip)
        form.addRow("Offset (+ dışa / − içe):",    sp_off)
        form.addRow("Köşe yumuşatma yarıçapı:",     sp_cr)
        form.addRow("Format:",                      cb_fmt)
        bbox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bbox.accepted.connect(dlg.accept); bbox.rejected.connect(dlg.reject)
        vlay = QVBoxLayout(dlg); vlay.addLayout(form); vlay.addWidget(bbox)
        if dlg.exec() != QDialog.Accepted: return
        mode = "kanal" if cb_tip.currentIndex() == 0 else "kapak"
        self.vp.plexiglas_offset   = sp_off.value()
        self.vp.plexiglas_corner_r = sp_cr.value()
        fmt = cb_fmt.currentText().lower()
        ext_map = {"dxf": "DXF Files (*.dxf)", "svg": "SVG Files (*.svg)", "eps": "EPS Files (*.eps)"}
        fp, _ = QFileDialog.getSaveFileName(self, "Export Plexiglas Cut",
            f"plexiglas_{mode}.{fmt}", ext_map[fmt])
        if not fp: return
        if not fp.lower().endswith(f".{fmt}"): fp += f".{fmt}"
        try:
            self.vp.export_plexiglas(fp, fmt, mode=mode,
                                     corner_radius=self.vp.plexiglas_corner_r)
            tip_lbl = "Kanal (iç)" if mode == "kanal" else "Kapak (dış)"
            cr_txt = f"  |  Köşe: {self.vp.plexiglas_corner_r} mm" if self.vp.plexiglas_corner_r > 0 else ""
            QMessageBox.information(self, "Plexiglas Cut",
                f"{tip_lbl} — Kaydedildi:\n{fp}{cr_txt}")
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _style(self):
        self.setStyleSheet("""
            QMainWindow{background:#222}
            #topbar{background:#111}
            #designTab{background:#c8d322;color:#333}
            #menuTab{background:#3c78ad;color:white}
            #letterTab{background:#1a1a1a;color:white}
            #topbarEmpty{background:#4a4a4a}
            #leftPanel{background:#1b1b1b}
            #blueHeader{background:#356fa5;color:white}
            #sectionHeader{background:#b8b8b8;color:#111}
            #whiteLabel{color:white}
            QCheckBox#whiteLabel{color:white;font-weight:bold}
            #footer{color:#777;font-size:16px}
            #importButton{background:#555;color:white;border-radius:6px;border:1px solid #000}
            QLineEdit{background:#d8d8d8;border:1px solid #777;border-radius:3px;color:black}
            #tabButton{background:#d0d0d0;color:#111;border:1px solid #999;
                       border-bottom-left-radius:16px;border-bottom-right-radius:16px;font-weight:bold}
            #iconButton{background:#efffff;color:#111;border:1px solid #8bb;border-radius:7px}
            #generateButton{background:#c9c9c9;color:#111;border-radius:7px;border:1px solid #999}
            #cadViewport{background:#161b24;border-left:1px solid #2a3040}
        """)


def main():
    # Verify earcut cap generation before starting the GUI
    _test_earcut_startup()

    # Enable MSAA anti-aliasing + depth buffer before QApplication
    _fmt = QSurfaceFormat()
    _fmt.setSamples(8)
    _fmt.setDepthBufferSize(24)
    QSurfaceFormat.setDefaultFormat(_fmt)

    pg.setConfigOptions(antialias=True)
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
