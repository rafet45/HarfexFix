"""
OrcaIntegration — OrcaSlicer'ı Harfex içine gömme yardımcı modülü.

Aşama 1 : Hidden launch + ana pencere tespiti
Aşama 2 : Win32 SetParent ile PreparePanel içine gömme
Aşama 3 : Tek-instance IPC ile model yükleme (yeniden başlatmadan)

Kullanım (MainWindow içinden):
    self._orca = OrcaIntegration.create()      # exe'yi otomatik bulur
    self._orca.signals.ready.connect(...)      # pencere hazır
    self._orca.start_hidden()                  # Harfex açılırken
    self._orca.embed(panel.host_hwnd(), 0, 0, w, h)   # Prepare paneli hazır olunca
    self._orca.load_model("/path/to/model.3mf")        # Prepare tıklanınca
"""

import os
import subprocess
import threading
import time
import ctypes
import ctypes.wintypes
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout
from PySide6.QtCore import Qt


# ── Sinyal köprüsü (thread → main thread) ────────────────────────────────────
class _OrcaSignals(QObject):
    ready      = Signal()   # Ana pencere bulundu, embed edilmeye hazır
    embed_done = Signal()   # SetParent başarılı
    lost       = Signal()   # Process beklenmedik şekilde kapandı


# ── Prepare placeholder paneli ────────────────────────────────────────────────
class PreparePanel(QWidget):
    """
    Harfex viewport'un yerini alan gömme konteyneri.
    WA_NativeWindow ile gerçek bir Win32 HWND'ye sahip olur —
    OrcaSlicer bu HWND'nin child'ı yapılır.
    """

    def __init__(self, on_resize=None, parent=None):
        super().__init__(parent)
        self._on_resize = on_resize          # callable(x, y, w, h)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setObjectName("preparePanel")
        self.setStyleSheet("#preparePanel{background:#111111;}")

        self._lbl = QLabel("OrcaSlicer hazırlanıyor…", self)
        self._lbl.setAlignment(Qt.AlignCenter)
        self._lbl.setStyleSheet(
            "color:#555555;font-size:18px;font-family:'Segoe UI',Arial;"
            "background:transparent;"
        )
        vl = QVBoxLayout(self)
        vl.addStretch()
        vl.addWidget(self._lbl)
        vl.addStretch()

    def set_status(self, text: str):
        self._lbl.setText(text)

    def host_hwnd(self) -> int:
        """Gerçek Win32 HWND — OrcaSlicer bu pencereye SetParent edilir."""
        return int(self.winId())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._on_resize:
            self._on_resize(0, 0, self.width(), self.height())


# ── Ana entegrasyon sınıfı ────────────────────────────────────────────────────
class OrcaIntegration:

    def __init__(self, exe_path: str):
        self._exe       = exe_path
        self._proc      = None
        self._hwnd      = None          # OrcaSlicer ana pencere HWND
        self._embedded  = False
        self._host_hwnd = None
        self.signals    = _OrcaSignals()

    # ── Fabrika metodu ────────────────────────────────────────────────────────
    @classmethod
    def create(cls) -> "OrcaIntegration | None":
        """
        Proje klasöründe 'orca*' adlı bir alt klasörde
        orca-slicer.exe / OrcaSlicer.exe arar.
        Bulamazsa None döner.
        """
        import sys as _sys
        if getattr(_sys, 'frozen', False):
            # PyInstaller: OrcaSlicer _MEIPASS içinde bundle edildi
            base = Path(getattr(_sys, '_MEIPASS', Path(_sys.executable).parent))
        else:
            base = Path(__file__).parent
        try:
            for folder in base.iterdir():
                if folder.is_dir() and folder.name.lower().startswith("orca"):
                    for name in ("orca-slicer.exe", "OrcaSlicer.exe", "orcaslicer.exe"):
                        c = folder / name
                        if c.exists():
                            return cls(str(c))
        except Exception as e:
            print(f"[ORCA] exe arama hatası: {e}")
        return None

    # ── Gizli başlatma ────────────────────────────────────────────────────────
    def start_hidden(self):
        """
        OrcaSlicer'ı tamamen gizli başlatır.
        Arka plan thread'i ana pencereyi bulduğunda signals.ready emit eder.
        """
        if self._proc and self._proc.poll() is None:
            return  # zaten çalışıyor

        si = subprocess.STARTUPINFO()
        si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0          # SW_HIDE

        try:
            self._proc = subprocess.Popen(
                [self._exe],
                startupinfo=si,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            print(f"[ORCA] başlatıldı pid={self._proc.pid}")
        except Exception as e:
            print(f"[ORCA] başlatma hatası: {e}")
            return

        threading.Thread(target=self._wait_for_window, daemon=True).start()

    # ── Pencere bekleme (thread) ──────────────────────────────────────────────
    def _wait_for_window(self):
        """
        OrcaSlicer yüklenene kadar bekler.
        İlk 4 saniye sabit bekleme (splash geçmesi için),
        sonra 0,5s aralıklarla 20 saniye boyunca dener.
        """
        time.sleep(4.0)                # minimum yükleme süresi

        for attempt in range(40):      # 40 × 0,5s = 20s ek bekleme
            if self._proc and self._proc.poll() is not None:
                print("[ORCA] process erken kapandı")
                self.signals.lost.emit()
                return

            hwnd = self._find_main_window()
            if hwnd:
                self._hwnd = hwnd
                print(f"[ORCA] ana pencere bulundu hwnd={hwnd:#x} ({attempt} deneme)")
                self.signals.ready.emit()
                return

            time.sleep(0.5)

        print("[ORCA] zaman aşımı — ana pencere bulunamadı")

    def _find_main_window(self) -> int:
        """
        Process'e ait üst düzey (parent=0) ve başlığı olan pencereyi döner.
        Splash screen genellikle başlığı olmayan veya çok kısa olan penceredir.
        """
        if not self._proc:
            return 0

        user32   = ctypes.windll.user32
        ENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        pid        = self._proc.pid
        candidates = []

        def _cb(hwnd, _):
            lp = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lp))
            if lp.value != pid:
                return True
            if user32.GetParent(hwnd) != 0:
                return True                             # alt pencere, atla
            n = user32.GetWindowTextLengthW(hwnd)
            if n < 4:
                return True                             # başlık çok kısa / boş
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                candidates.append((hwnd, buf.value))
            return True

        try:
            user32.EnumWindows(ENUMPROC(_cb), 0)
        except Exception:
            pass

        if not candidates:
            return 0

        # Tercihen "orca" veya "slicer" içeren başlık
        for hwnd, title in candidates:
            tl = title.lower()
            if "orca" in tl or "slicer" in tl:
                return hwnd

        # Geri dönüş: en uzun başlıklı pencere
        candidates.sort(key=lambda x: len(x[1]), reverse=True)
        return candidates[0][0]

    # ── Win32 gömme ───────────────────────────────────────────────────────────
    def embed(self, host_hwnd: int, x: int = 0, y: int = 0,
              w: int = 800, h: int = 600):
        """
        OrcaSlicer penceresini host_hwnd'nin child'ı yapar.
        Başlık çubuğu ve kenarlıkları kaldırır, WS_CHILD ekler.
        """
        if not self._hwnd:
            print("[ORCA] embed: pencere henüz hazır değil")
            return False

        u32 = ctypes.windll.user32
        GWL_STYLE   = -16
        GWL_EXSTYLE = -20

        try:
            # ── Stil temizleme ───────────────────────────────────────────────
            style = u32.GetWindowLongW(self._hwnd, GWL_STYLE)
            style &= ~0x00C00000    # kaldır: WS_CAPTION (başlık çubuğu)
            style &= ~0x00040000    # kaldır: WS_THICKFRAME (yeniden boyutlandırma kenarlığı)
            style &= ~0x00020000    # kaldır: WS_MINIMIZEBOX
            style &= ~0x00010000    # kaldır: WS_MAXIMIZEBOX
            style &= ~0x00800000    # kaldır: WS_BORDER
            style &= ~0x00080000    # kaldır: WS_SYSMENU
            style |=  0x40000000    # ekle:   WS_CHILD
            u32.SetWindowLongW(self._hwnd, GWL_STYLE, style)

            ex_style = u32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
            ex_style &= ~0x00000001     # WS_EX_DLGMODALFRAME
            ex_style &= ~0x00040000     # WS_EX_APPWINDOW (taskbar'dan kaldır)
            ex_style &= ~0x00000100     # WS_EX_WINDOWEDGE
            u32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex_style)

            # ── Yeniden ebeveynleme ──────────────────────────────────────────
            self._host_hwnd = host_hwnd
            u32.SetParent(self._hwnd, host_hwnd)

            # ── Konumlandır ve göster ────────────────────────────────────────
            u32.MoveWindow(self._hwnd, x, y, w, h, True)
            u32.ShowWindow(self._hwnd, 1)       # SW_SHOW
            u32.UpdateWindow(self._hwnd)
            u32.SetFocus(self._hwnd)

            self._embedded = True
            print(f"[ORCA] gömme başarılı {self._hwnd:#x} → {host_hwnd:#x}"
                  f"  {w}×{h}")
            self.signals.embed_done.emit()
            return True

        except Exception as e:
            print(f"[ORCA] gömme hatası: {e}")
            return False

    # ── Yeniden boyutlandırma ────────────────────────────────────────────────
    def resize(self, x: int, y: int, w: int, h: int):
        if self._hwnd and self._embedded:
            ctypes.windll.user32.MoveWindow(self._hwnd, x, y, w, h, True)

    # ── Model yükleme (minimize başlat → restore) ────────────────────────────
    def open_with_model(self, path: str, harfex_hwnd: int = 0):  # harfex_hwnd artık kullanılmıyor
        """
        OrcaSlicer'ı SW_SHOWMINNOACTIVE (7) ile başlatır — minimize, splash gizli.
        Arka plan thread'i ana pencereyi bulunca SW_RESTORE ile öne getirir.
        OrcaSlicer kapanınca harfex_hwnd varsa Harfex öne gelir.
        """
        si = subprocess.STARTUPINFO()
        si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7   # SW_SHOWMINNOACTIVE
        try:
            proc = subprocess.Popen(
                [self._exe, path],
                startupinfo=si,
            )
            print(f"[ORCA] minimize başlatıldı pid={proc.pid}: {path}")
            threading.Thread(
                target=self._wait_and_restore,
                args=(proc, harfex_hwnd),
                daemon=True
            ).start()
        except Exception as e:
            print(f"[ORCA] açma hatası: {e}")

    def _wait_and_restore(self, proc, harfex_hwnd: int = 0):
        """
        4s bekle + 0.5s aralıklarla OrcaSlicer ana penceresini ara.
        Bulunca SW_RESTORE ile öne getir.
        Kullanıcı Former butonu ile Harfex'e, Prepare ile OrcaSlicer'a geçer.
        """
        time.sleep(4.0)

        user32     = ctypes.windll.user32
        SW_RESTORE = 9

        for _ in range(50):   # max 25s
            if proc.poll() is not None:
                print("[ORCA] process erken kapandı")
                return
            hwnd = self._find_window_for_pid(proc.pid)
            if hwnd:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
                print(f"[ORCA] ana pencere restore edildi hwnd={hwnd:#x}")
                return
            time.sleep(0.5)

        print("[ORCA] zaman aşımı — restore yapılamadı")

    def _find_window_for_pid(self, pid: int) -> int:
        """Verilen PID'e ait, üst düzey, başlığı olan pencereyi döner."""
        user32   = ctypes.windll.user32
        ENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        candidates = []

        def _cb(hwnd, _):
            lp = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lp))
            if lp.value != pid:
                return True
            if user32.GetParent(hwnd) != 0:
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n < 4:
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                candidates.append((hwnd, buf.value))
            return True

        try:
            user32.EnumWindows(ENUMPROC(_cb), 0)
        except Exception:
            pass

        if not candidates:
            return 0
        for hwnd, title in candidates:
            tl = title.lower()
            if "orca" in tl or "slicer" in tl:
                return hwnd
        candidates.sort(key=lambda x: len(x[1]), reverse=True)
        return candidates[0][0]

    def load_model(self, path: str):
        """
        3MF / STL dosyasını gömülü OrcaSlicer'a yükler.
        OrcaSlicer tek-instance modundaysa ikinci Popen çağrısı
        dosya yolunu çalışan instance'a IPC ile iletir ve hemen kapanır.
        Model yeniden başlatma gerekmeden anlık yüklenir.
        """
        if not self._proc or self._proc.poll() is not None:
            # Çalışmıyor — dosyayla birlikte yeniden başlat
            self._restart_with_file(path)
            return

        si = subprocess.STARTUPINFO()
        si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        try:
            subprocess.Popen([self._exe, path], startupinfo=si)
            print(f"[ORCA] model IPC ile gönderildi: {path}")
        except Exception as e:
            print(f"[ORCA] model yükleme IPC hatası: {e}")

    def _restart_with_file(self, path: str):
        si = subprocess.STARTUPINFO()
        si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        self._hwnd     = None
        self._embedded = False
        try:
            self._proc = subprocess.Popen(
                [self._exe, path],
                startupinfo=si,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._wait_for_window, daemon=True).start()
        except Exception as e:
            print(f"[ORCA] yeniden başlatma hatası: {e}")

    # ── Göster / Gizle / Kapat ───────────────────────────────────────────────
    def show_window(self):
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, 1)   # SW_SHOW

    def hide_window(self):
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, 0)   # SW_HIDE

    def terminate(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    # ── Özellikler ────────────────────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    @property
    def is_embedded(self) -> bool:
        return self._embedded

    @property
    def hwnd(self) -> int:
        return self._hwnd or 0
