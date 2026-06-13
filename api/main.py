"""
Harfex Cloud API — FastAPI
==========================
Çalıştırma:
    pip install fastapi uvicorn python-multipart
    uvicorn api.main:app --reload --port 8000

Endpoints:
    POST /api/process        → DXF yükle + parametreler → STL/DXF bytes (senkron)
    GET  /api/health         → sunucu durumu
"""

import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse

# engine modülünü bul
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import HarfexEngine as HarfexProcessor


# ── Uygulama ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Harfex Cloud API",
    description="DXF/SVG vektör dosyasından üretim kalitesinde 3D kutu harf modeli",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # production'da domain kısıtla
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Sağlık kontrolü ───────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    try:
        import manifold3d
        import shapely
        return {
            "status": "ok",
            "manifold3d": getattr(manifold3d, "__version__", "ok"),
            "shapely":    shapely.__version__,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


# ── Ana işlem endpoint'i ──────────────────────────────────────────────────────
@app.post("/api/process")
async def process(
    file:              UploadFile = File(...,        description="DXF dosyası"),
    output:            str        = Form("wall_stl", description="wall_stl | face_stl | cover_stl | plexiglas_dxf | wall_3mf | combined_3mf | foam_dxf"),
    wall_mm:           float      = Form(5.0,        description="Duvar kalınlığı (mm)"),
    height_mm:         float      = Form(45.0,       description="Harf derinliği (mm)"),
    wall_type:         int        = Form(0,          description="0=düz  1=açılı  2=kavisli"),
    wall_taper:        float      = Form(13.81,      description="Açılma mesafesi (mm, wall_type 1-2)"),
    face_mode:         int        = Form(1,          description="0=yok  1=iç  2=dış  3=sadece yüz"),
    face_thickness:    float      = Form(3.0,        description="Yüz kalınlığı (mm)"),
    face_fill:            int        = Form(0,    description="0=solid  1=honeycomb  2=grid  3=lines"),
    face_fill_cell:       float      = Form(8.0,  description="Dolgu hücre boyutu (mm)"),
    face_fill_wall:       float      = Form(1.6,  description="Dolgu duvar kalınlığı (mm)"),
    face_fill_solid_pos:  int        = Form(0,    description="0=kapalı 1=ön 2=arka 3=her ikisi"),
    face_fill_border:     float      = Form(1.5,  description="Dolgu kenar payı (mm)"),
    arc_sm:               float      = Form(8.0,  description="Eğri yumuşatma"),
    round_c:           float      = Form(1.0,        description="Köşe yumuşatma yarıçapı (mm)"),
    simplify:          float      = Form(0.02,       description="Shapely simplify toleransı"),
    cover_ct:          float      = Form(2.0,        description="Arka kapak kalınlığı (mm)"),
    cover_wh:          float      = Form(15.0,       description="Arka kapak tab yüksekliği (mm)"),
    cover_clearance:   float      = Form(0.05,       description="Kapak boşluğu (mm)"),
    cover_wt:          float      = Form(3.0,        description="Kapak tab duvar kalınlığı (mm)"),
    plexiglas_offset:  float      = Form(-0.5,        description="Pleksiglas offset (mm)"),
    foam_offset:       float      = Form(0.0,         description="Foam kesim offset (mm)"),
    mirror_x:          int        = Form(0,           description="1=X ayna"),
    mirror_y:          int        = Form(0,           description="1=Y ayna"),
    top_tab:           bool       = Form(False,      description="Üst tırnak slot"),
    bot_tab:           bool       = Form(False,      description="Alt tırnak slot"),
    top_proj:          float      = Form(4.0,        description="Üst tırnak çıkıntısı (mm)"),
    bot_proj:          float      = Form(4.0,        description="Alt tırnak çıkıntısı (mm)"),
    top_tab_z:         float      = Form(-2.0,       description="Üst tırnak Z offset (negatif)"),
    bot_tab_z:         float      = Form(2.0,        description="Alt tırnak Z offset"),
    include_cover:     int        = Form(0,          description="1=3MF'e cover dahil et"),
    include_band:      int        = Form(0,          description="1=3MF'e kuşak bölümü dahil et"),
    band_pos:          float      = Form(0.0,        description="Kuşak başlangıç Z (mm)"),
    band_h:            float      = Form(0.0,        description="Kuşak yüksekliği (mm)"),
):
    """
    DXF dosyasını işle ve istenen çıktı dosyasını döndür.

    `output` değerleri:
    - **wall_stl**      → Duvar/gövde STL (1 kredi)
    - **face_stl**      → Yüz STL (1 kredi)
    - **cover_stl**     → Arka kapak STL (1 kredi)
    - **plexiglas_dxf** → Pleksiglas kesim DXF (1 kredi)
    """
    # Dosya kontrolü
    if not file.filename:
        raise HTTPException(400, "Dosya adı boş.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".dxf", ".svg", ".eps"):
        raise HTTPException(400, f"Desteklenmeyen format: {suffix!r}. DXF/SVG/EPS gerekli.")

    data = await file.read()
    if len(data) == 0:
        raise HTTPException(400, "Boş dosya yüklendi.")

    # Processor
    proc = HarfexProcessor()

    # Önce DXF yükle — load_dxf_bytes içinde mx/my/top_tab vs. sıfırlanıyor
    # set_params SONRA çağrılmalı ki sıfırlama üzerine yazılsın
    try:
        proc.load_dxf_bytes(data, suffix=suffix)
    except Exception as e:
        raise HTTPException(422, f"DXF okunamadı: {e}")

    proc.set_params(
        wall_mm=wall_mm, height_mm=height_mm,
        wall_type=wall_type, wall_taper=wall_taper,
        face_mode=face_mode, face_thickness=face_thickness,
        face_fill=face_fill, face_fill_cell=face_fill_cell,
        face_fill_wall=face_fill_wall,
        face_fill_solid_pos=face_fill_solid_pos,
        face_fill_border=face_fill_border,
        arc_sm=arc_sm,
        round_c=round_c, simplify=simplify,
        cover_ct=cover_ct, cover_wh=cover_wh,
        cover_clearance=cover_clearance, cover_wt=cover_wt,
        plexiglas_offset=plexiglas_offset,
        foam_offset=foam_offset,
        mx=bool(mirror_x), my=bool(mirror_y),
        top_tab=top_tab, bot_tab=bot_tab,
        top_proj=top_proj, bot_proj=bot_proj,
        top_tab_z=top_tab_z, bot_tab_z=bot_tab_z,
    )

    try:
        stats = proc.build()
    except Exception as e:
        raise HTTPException(500, f"3D üretim hatası: {e}")

    # Çıktı seçimi
    print(f"[API] output={output!r}")
    try:
        if output == "wall_preview":
            # Wall + face tek istekte (base64 JSON)
            import base64, json as _json
            wall_b64 = base64.b64encode(proc.export_stl_bytes()).decode()
            face_b64 = None
            if face_mode != 0:
                try:
                    face_b64 = base64.b64encode(proc.export_face_stl_bytes()).decode()
                except Exception:
                    face_b64 = None
            content    = _json.dumps({"wall": wall_b64, "face": face_b64,
                                      "wall_faces": stats.get("wall_faces", 0)}).encode()
            media_type = "application/json"
            filename   = "harfex_preview.json"

        elif output == "wall_stl":
            content      = proc.export_stl_bytes()
            media_type   = "model/stl"
            filename     = "harfex_wall.stl"

        elif output == "face_stl":
            if face_mode == 0:
                raise HTTPException(400, "face_mode=0 — yüz devre dışı.")
            content      = proc.export_face_stl_bytes()
            media_type   = "model/stl"
            filename     = "harfex_face.stl"

        elif output == "cover_stl":
            print(f"[API] cover_stl branch — cover_ct={cover_ct}, cover_wh={cover_wh}")
            content      = proc.export_cover_stl_bytes()
            print(f"[API] cover_stl OK — {len(content)} bytes")
            media_type   = "model/stl"
            filename     = "harfex_cover.stl"

        elif output == "plexiglas_dxf":
            content    = proc.export_plexiglas_dxf_bytes(mode="kanal")
            media_type = "application/dxf"
            filename   = "harfex_plexiglas.dxf"

        elif output == "foam_dxf":
            content    = proc.export_foam_dxf_bytes()
            media_type = "application/dxf"
            filename   = "harfex_foam.dxf"

        elif output == "wall_3mf":
            content    = proc.export_3mf_bytes(include_face=False,
                             band_pos=band_pos if include_band else None,
                             band_h=band_h   if include_band else None)
            media_type = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
            filename   = "harfex_wall.3mf"

        elif output == "combined_3mf":
            content    = proc.export_3mf_bytes(include_face=True, include_cover=bool(include_cover),
                             band_pos=band_pos if include_band else None,
                             band_h=band_h   if include_band else None)
            media_type = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
            filename   = "harfex_combined.3mf"

        else:
            raise HTTPException(400, f"Bilinmeyen output değeri: {output!r}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Export hatası: {e}")

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Wall-Faces":  str(stats.get("wall_faces", 0)),
            "X-Face-Faces":  str(stats.get("face_faces", 0)),
        },
    )


# ── Back Cover STL ───────────────────────────────────────────────────────────
@app.post("/api/cover")
async def cover(
    file:             UploadFile = File(...),
    wall_mm:          float      = Form(5.0),
    height_mm:        float      = Form(45.0),
    wall_type:        int        = Form(0),
    wall_taper:       float      = Form(13.81),
    arc_sm:           float      = Form(8.0),
    round_c:          float      = Form(1.0),
    simplify:         float      = Form(0.02),
    mirror_x:         int        = Form(0),
    cover_ct:         float      = Form(2.0),
    cover_wh:         float      = Form(15.0),
    cover_clearance:  float      = Form(0.05),
    cover_wt:         float      = Form(3.0),
):
    """DXF yükle → Back Cover STL üret (main.py export_back_cover_stl ile özdeş)."""
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".dxf", ".svg", ".eps"):
        raise HTTPException(400, f"Desteklenmeyen format: {suffix!r}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Boş dosya")
    print(f"[COVER] ct={cover_ct} wh={cover_wh} cl={cover_clearance} wt={cover_wt}")
    try:
        proc = HarfexProcessor()
        proc.load_dxf_bytes(data, suffix=suffix)
        proc.set_params(
            wall_mm=wall_mm, height_mm=height_mm,
            wall_type=wall_type, wall_taper=wall_taper,
            arc_sm=arc_sm, round_c=round_c, simplify=simplify,
            mx=bool(mirror_x),
            cover_ct=cover_ct, cover_wh=cover_wh,
            cover_clearance=cover_clearance, cover_wt=cover_wt,
        )
        # build() → _last_base, _last_ig set edilir
        proc.build()
        content = proc.export_cover_stl_bytes()
        print(f"[COVER] OK — {len(content)} bytes")
    except Exception as e:
        print(f"[COVER] ERROR: {e}")
        raise HTTPException(500, f"Cover üretim hatası: {e}")
    return Response(
        content=content,
        media_type="model/stl",
        headers={"Content-Disposition": 'attachment; filename="harfex_cover.stl"'},
    )


# ── DXF kontur önizleme ───────────────────────────────────────────────────────
@app.post("/api/contour")
async def contour(
    file:     UploadFile = File(...),
    arc_sm:   float      = Form(8.0),
    round_c:  float      = Form(1.0),
    simplify: float      = Form(0.05),
    mirror_x: int        = Form(0),
):
    """DXF/SVG yükle → arc/round/simplify uygulanmış 2D kontur JSON."""
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".dxf", ".svg", ".eps"):
        raise HTTPException(400, f"Desteklenmeyen format: {suffix!r}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Boş dosya")
    try:
        eng = HarfexProcessor()
        eng.load_dxf_bytes(data, suffix=suffix)
        eng.set_params(arc_sm=arc_sm, round_c=round_c, simplify=simplify, mx=bool(mirror_x))
        return eng.get_processed_contour_json()
    except Exception as e:
        raise HTTPException(422, f"DXF parse hatası: {e}")


# ── Parametre şeması (frontend formu için) ────────────────────────────────────
@app.get("/api/schema")
def schema():
    """Frontend formunun kullanacağı parametre listesi ve varsayılan değerler."""
    return {
        "wall_types":  {0: "Düz", 1: "Açılı", 2: "Kavisli"},
        "face_modes":  {0: "Yok", 1: "İç Yüz", 2: "Dış Yüz", 3: "Sadece Yüz"},
        "face_fills":  {0: "Solid", 1: "Honeycomb", 2: "Grid", 3: "Lines"},
        "outputs":     ["wall_stl", "face_stl", "cover_stl", "plexiglas_dxf"],
        "defaults": {
            "wall_mm": 5.0, "height_mm": 45.0,
            "wall_type": 0, "wall_taper": 13.81,
            "face_mode": 1, "face_thickness": 3.0,
            "face_fill": 0, "face_fill_cell": 8.0, "face_fill_wall": 1.6,
            "round_c": 1.0, "simplify": 0.02,
            "cover_ct": 2.0, "cover_wh": 15.0,
            "cover_clearance": 0.05, "cover_wt": 3.0,
            "plexiglas_offset": 0.0,
        },
    }
