LetterFormer Alpha 18.2 — Solid Cap Fix

SORUN (18.0 ve 18.1):
  mapbox_earcut.triangulate_float64(pts, hole_indices) → YANLIŞ API kullanımı
  hole_indices: deliğin başlangıç indeksini bekleniyordu
  Gerçek API: ring_end_indices → her ring'in BİTİŞ indeksi

  Bu yüzden earcut ValueError fırlatıp boş döndürüyordu.
  Sonuç: üst/alt kapaklar hiç oluşmuyordu → tüp görünümü.

ÇÖZÜM (18.2):
  ring_ends = [len(exterior), len(exterior)+len(hole1), ...]
  Her ring'in bitiş indeksi geçiliyor → earcut doğru çalışıyor
  Solid cap üretiliyor → tam kapalı solid mesh

Kurulum: pip install -r requirements.txt
Çalıştır: python main.py
