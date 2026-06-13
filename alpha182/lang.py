"""
Harfex — Dil / Language support
Kullanım: from lang import _t, set_lang, current_lang
"""

_lang = "tr"   # default

def current_lang() -> str:
    return _lang

def set_lang(code: str):
    global _lang
    _lang = code if code in ("tr", "en") else "tr"

def _t(key: str, **kwargs) -> str:
    """Return translated string for key in current language."""
    entry = _STRINGS.get(key, {})
    text = entry.get(_lang) or entry.get("tr") or key
    return text.format(**kwargs) if kwargs else text


_STRINGS = {

    # ── Ana pencere ──────────────────────────────────────────────────────────
    "app_title":            {"tr": "Harfex — Channel Letter 3D",
                             "en": "Harfex — Channel Letter 3D"},

    # ── Menü: Dosya ──────────────────────────────────────────────────────────
    "menu_file":            {"tr": "&Dosya",            "en": "&File"},
    "menu_open_dxf":        {"tr": "DXF Aç…",          "en": "Open DXF…"},
    "menu_export":          {"tr": "Dışa Aktar",        "en": "Export"},
    "menu_exp_stl":         {"tr": "STL…",              "en": "STL…"},
    "menu_exp_3mf":         {"tr": "3MF…",              "en": "3MF…"},
    "menu_exp_cover_stl":   {"tr": "Back Cover STL…",   "en": "Back Cover STL…"},
    "menu_exp_plex":        {"tr": "Plexiglas DXF…",    "en": "Plexiglas DXF…"},
    "menu_exp_foam":        {"tr": "Back Foam DXF…",    "en": "Back Foam DXF…"},
    "menu_exp_fill":        {"tr": "Fill Pattern DXF…", "en": "Fill Pattern DXF…"},
    "menu_clear":           {"tr": "Yeni Sayfa",        "en": "New Page"},
    "menu_exit":            {"tr": "Çıkış",             "en": "Exit"},

    # ── Menü: Düzenle ─────────────────────────────────────────────────────────
    "menu_edit":            {"tr": "&Düzenle",              "en": "&Edit"},
    "menu_wall_color":      {"tr": "Duvar Rengi…",          "en": "Wall Color…"},
    "menu_face_color":      {"tr": "Yüz Rengi…",            "en": "Face Color…"},
    "menu_cover_color":     {"tr": "Back Cover Rengi…",     "en": "Back Cover Color…"},
    "menu_reset_view":      {"tr": "Görünümü Sıfırla",      "en": "Reset View"},
    "menu_contour":         {"tr": "Contour Göster / Gizle","en": "Show / Hide Contour"},

    # ── Menü: Profil ──────────────────────────────────────────────────────────
    "menu_profile":         {"tr": "&Profil",               "en": "&Profile"},
    "menu_prof_save":       {"tr": "Profil Kaydet…  (R)",   "en": "Save Profile…  (R)"},
    "menu_prof_manage":     {"tr": "Profil Yönet…   (P)",   "en": "Manage Profiles…  (P)"},

    # ── Menü: Yardım ──────────────────────────────────────────────────────────
    "menu_help":            {"tr": "&Yardım",               "en": "&Help"},
    "menu_about":           {"tr": "Hakkında…",             "en": "About…"},
    "menu_guide":           {"tr": "Çevrimiçi Kılavuz…",   "en": "Online Guide…"},

    # ── Dil menüsü ────────────────────────────────────────────────────────────
    "menu_lang":            {"tr": "&Dil",                  "en": "&Language"},
    "lang_tr":              {"tr": "Türkçe",                "en": "Turkish"},
    "lang_en":              {"tr": "İngilizce",             "en": "English"},
    "lang_restart_title":   {"tr": "Dil Değişikliği",       "en": "Language Changed"},
    "lang_restart_msg":     {"tr": "Dil değişikliği için programı yeniden başlatın.",
                             "en": "Please restart the application to apply the language change."},

    # ── Viewport toolbar ──────────────────────────────────────────────────────
    "vp_wireframe":         {"tr": "Wireframe",     "en": "Wireframe"},
    "vp_shaded":            {"tr": "Shaded",        "en": "Shaded"},
    "vp_rendered":          {"tr": "Rendered",      "en": "Rendered"},
    "vp_tip_wireframe":     {"tr": "Sadece kenarlar",       "en": "Edges only"},
    "vp_tip_shaded":        {"tr": "Düzgün gölgeli yüzey", "en": "Smooth shaded surface"},
    "vp_tip_rendered":      {"tr": "Gölgeli + kenarlar",   "en": "Shaded + edges"},
    "vp_group":             {"tr": "Grup Oluştur",          "en": "Group"},
    "vp_ungroup":           {"tr": "Grubu Çöz",             "en": "Ungroup"},
    "vp_tip_group":         {"tr": "Seçili nesneleri grupla  (Ctrl+tıkla → çoklu seç)",
                             "en": "Group selected objects  (Ctrl+click → multi-select)"},
    "vp_tip_ungroup":       {"tr": "Seçili nesnelerin grubunu kaldır",
                             "en": "Dissolve group of selected objects"},

    # ── Sol panel ─────────────────────────────────────────────────────────────
    "lp_generate":          {"tr": "Generate 3D",   "en": "Generate 3D"},
    "lp_objects":           {"tr": "Objects",       "en": "Objects"},
    "lp_export_stl":        {"tr": "Export STL",    "en": "Export STL"},
    "lp_export_3mf":        {"tr": "Export 3MF",    "en": "Export 3MF"},
    "lp_tip_select":        {"tr": "3D görünümde seç: {label}",
                             "en": "Select in 3D view: {label}"},

    # ── Renk dialog ───────────────────────────────────────────────────────────
    "color_dlg_title":      {"tr": "Renk Ata — Filament Renkleri",
                             "en": "Assign Colors — Filament Colors"},
    "color_dlg_header":     {"tr": "Her parçanın baskı rengini seç",
                             "en": "Select print color for each part"},
    "color_dlg_note":       {"tr": "Renk değişimi viewport'ta anında görünür.\nOrcaSlicer'a gönderildiğinde filament ataması otomatik yapılır.",
                             "en": "Color changes are reflected in the viewport instantly.\nFilament assignment is applied automatically when sent to OrcaSlicer."},
    "btn_ok":               {"tr": "Tamam",         "en": "OK"},
    "btn_cancel":           {"tr": "İptal",         "en": "Cancel"},
    "btn_export":           {"tr": "Export",        "en": "Export"},
    "btn_apply":            {"tr": "▶  Uygula",     "en": "▶  Apply"},
    "btn_update":           {"tr": "↺  Güncelle",   "en": "↺  Update"},
    "btn_rename":           {"tr": "✎  Yeniden Adlandır", "en": "✎  Rename"},
    "btn_delete":           {"tr": "✕  Sil",        "en": "✕  Delete"},

    # ── Export 3MF dialog ─────────────────────────────────────────────────────
    "exp3mf_title":         {"tr": "Export 3MF — Nesne Seçimi",
                             "en": "Export 3MF — Object Selection"},
    "exp3mf_header":        {"tr": "Export edilecek nesneleri seçin:",
                             "en": "Select objects to export:"},

    # ── Slot dialog ───────────────────────────────────────────────────────────
    "slot_title":           {"tr": "Slot Dimension",        "en": "Slot Dimension"},
    "slot_tip_bot":         {"tr": "0 = alt slot kapalı",   "en": "0 = bottom slot closed"},
    "slot_tip_top":         {"tr": "0 = üst slot kapalı",   "en": "0 = top slot closed"},

    # ── Profil dialog ─────────────────────────────────────────────────────────
    "prof_dlg_title":       {"tr": "Profil Seç",            "en": "Select Profile"},
    "prof_dlg_header":      {"tr": "Profil Listesi",        "en": "Profile List"},
    "prof_save_title":      {"tr": "Profil Kaydet",         "en": "Save Profile"},
    "prof_save_prompt":     {"tr": "Profil adı:",           "en": "Profile name:"},
    "prof_overwrite":       {"tr": '"{name}" zaten var. Üzerine yazılsın mı?',
                             "en": '"{name}" already exists. Overwrite?'},
    "prof_saved":           {"tr": "Profil kaydedildi: {name} ✓",
                             "en": "Profile saved: {name} ✓"},
    "prof_empty":           {"tr": "Henüz kayıtlı profil yok. R ile kaydedin.",
                             "en": "No saved profiles yet. Use R to save one."},
    "prof_no_dxf":          {"tr": "Önce DXF yükleyin.",   "en": "Load a DXF file first."},
    "prof_rename_prompt":   {"tr": "Yeni ad:",              "en": "New name:"},
    "prof_rename_exists":   {"tr": '"{name}" zaten var.',   "en": '"{name}" already exists.'},
    "prof_delete_confirm":  {"tr": '"{name}" silinsin mi?', "en": 'Delete "{name}"?'},

    # ── Fill pattern dialog ───────────────────────────────────────────────────
    "fill_dlg_title":       {"tr": "Yüzey Dolgu Deseni",   "en": "Face Fill Pattern"},
    "fill_dlg_header":      {"tr": "Yüzey Dolgu Deseni",   "en": "Face Fill Pattern"},
    "fill_cell_mm":         {"tr": "Hücre boyutu (mm):",   "en": "Cell size (mm):"},
    "fill_wall_mm":         {"tr": "Petek duvarı (mm):",   "en": "Wall thickness (mm):"},
    "fill_margin_mm":       {"tr": "Kenar payı (mm):",     "en": "Edge margin (mm):"},

    # ── Back cover dialog ─────────────────────────────────────────────────────
    "cover_dlg_title":      {"tr": "Back Cover Ayarları",  "en": "Back Cover Settings"},
    "cover_no_model":       {"tr": "Önce model oluşturun.","en": "Generate a model first."},

    # ── Sağ tık menüsü ────────────────────────────────────────────────────────
    "ctx_cover_settings":   {"tr": "⚙  Cover Ayarları…",  "en": "⚙  Cover Settings…"},
    "ctx_stl_export":       {"tr": "↓  STL Dışa Aktar…",  "en": "↓  Export STL…"},
    "ctx_3mf_export":       {"tr": "↓  3MF Dışa Aktar…",  "en": "↓  Export 3MF…"},
    "ctx_color":            {"tr": "🎨  Renk Değiştir…",  "en": "🎨  Change Color…"},

    # ── Mesaj kutuları ────────────────────────────────────────────────────────
    "msg_error":            {"tr": "Hata",                 "en": "Error"},
    "msg_saved":            {"tr": "Kaydedildi",           "en": "Saved"},
    "msg_no_mesh":          {"tr": "Mesh verisi bulunamadı.", "en": "No mesh data found."},
    "msg_no_model":         {"tr": "Önce model oluşturun.", "en": "Generate a model first."},
    "msg_numeric":          {"tr": "Sayısal değer girin.", "en": "Enter numeric values."},
    "msg_done":             {"tr": "Done ✓",               "en": "Done ✓"},

    # ── Status bar ────────────────────────────────────────────────────────────
    "status_error":         {"tr": "Hata",                 "en": "Error"},

    # ── Hakkında ──────────────────────────────────────────────────────────────
    "about_title":          {"tr": "Harfex Hakkında",      "en": "About Harfex"},
    "about_body":           {
        "tr": (
            "<h3>Harfex — Channel Letter Former</h3>"
            "<p>DXF, EPS ve SVG tabanlı kutu harf (Channel Letter) 3D boyutlandırma aracı.</p>"
            "<p>Vektör dosyanızı içe aktarın; duvar tipi, önyüz yapısı ve arka kapak "
            "ayarlarını belirleyin — Harfex geometriyi otomatik hesaplar ve 3D yazıcıya "
            "hazır çıktı üretir. Profilinizi kaydedin, bir sonraki işi tek tuşla tamamlayın.</p>"
            "<p><b>Desteklenen çıktılar:</b> STL · 3MF · Back Cover STL · Plexiglas DXF · Foam DXF</p>"
            "<hr>"
            "<p>Developed by <b>R. Degerli</b><br>"
            "<small>Designed for sign makers, architects and visual communication professionals "
            "who need a fast, reliable bridge between their vector artwork and 3D production.</small></p>"
            "<p><a href='https://harfex3d.com'>harfex3d.com</a> — "
            "Uygulama videoları ve ayrıntılı kılavuz için ziyaret edin.</p>"
        ),
        "en": (
            "<h3>Harfex — Channel Letter Former</h3>"
            "<p>3D dimensioning tool for channel letters based on DXF, EPS and SVG files.</p>"
            "<p>Import your vector file, set wall type, face structure and back cover options — "
            "Harfex calculates the geometry automatically and produces print-ready output. "
            "Save your profile and complete the next job with a single click.</p>"
            "<p><b>Supported outputs:</b> STL · 3MF · Back Cover STL · Plexiglas DXF · Foam DXF</p>"
            "<hr>"
            "<p>Developed by <b>R. Degerli</b><br>"
            "<small>Designed for sign makers, architects and visual communication professionals "
            "who need a fast, reliable bridge between their vector artwork and 3D production.</small></p>"
            "<p><a href='https://harfex3d.com'>harfex3d.com</a> — "
            "Visit for application videos and detailed guide.</p>"
        ),
    },
}
