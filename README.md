# 🩺 Omni-Channel Medical Persona Translator (Aplikasi Web)

Aplikasi sederhana untuk flow Langflow karya Syifa Nurul Afifah.
Pengguna mengunggah PDF lab, mengisi anamnesis, lalu bertanya. Jawaban AI
ditampilkan dalam 3 tab: **Dokter**, **Pasien/Keluarga**, **EMR Summary**.

## Cara kerja
Flow punya 3 input; aplikasi mengisinya lewat API Langflow:
| Input flow | Dikirim lewat |
|---|---|
| Chat Input (pertanyaan) | `input_value` |
| Anamnesis_pasien (`TextInput-jE0Kv`) | `tweaks` |
| File (`File-Nn09e`, PDF lab) | teks PDF diekstrak lokal & dikirim langsung (utama) + upload `/api/v1/files/upload/<flow_id>` + `tweaks` (pelengkap) |

Upload file ke Langflow bersifat **pelengkap**. Bila gagal (beda versi Langflow, izin, dsb.),
aplikasi tetap jalan memakai teks PDF yang diekstrak lokal.

## Menjalankan
Butuh **Python 3.10+** (disarankan 3.11+).

1. Jalankan Langflow (`langflow run`), impor JSON flow, pastikan API key Gemini di komponen Agent sudah terisi.
2. Buat virtual environment lalu pasang dependensi:
   ```
   python -m venv .venv
   .venv\Scripts\activate        # Windows   (macOS/Linux: source .venv/bin/activate)
   pip install -r requirements.txt
   ```
3. Salin `.env.example` → `.env`, isi `LANGFLOW_API_KEY` dan pastikan `LANGFLOW_URL` sama dengan endpoint flow kamu.
4. Cek koneksi dulu: `python check.py` (semua baris harus `[OK]`).
5. `streamlit run app.py`

## Pemecahan masalah
| Gejala | Penyebab / solusi |
|---|---|
| `Tidak bisa terhubung ke Langflow` | Langflow belum jalan, atau port di `LANGFLOW_URL` salah. |
| HTTP 401/403 | API key salah/kosong, atau dibuat oleh user lain. Buat key baru. |
| HTTP 404 | URL/ID flow salah. Salin ulang dari tombol **API** di Langflow. |
| "Tidak ada teks di PDF" | PDF hasil scan/foto. Gunakan PDF yang teksnya bisa diseleksi. |
| "Gemini overload" | Server Gemini sibuk; aplikasi coba ulang otomatis lalu pakai model cadangan. |
| Jawaban terpotong di tengah kata/kalimat | Batas token model di komponen Agent terlalu kecil (Gemini 2.5 memakai sebagian token untuk "berpikir"). Naikkan **Max Tokens** di Agent, atau isi `AGENT_MAX_TOKENS=8192` di `.env`. Cek **Respons mentah** di bawah jawaban. |
| Komponen tidak terdeteksi | Buka **Diagnostik → Deteksi ulang**, atau isi `FILE_NODE_ID` / `ANAMNESIS_NODE_ID` di `.env`. |

## Catatan
- Hasil hanya alat bantu; keputusan klinis tetap oleh tenaga medis.
- Data pasien sensitif: jangan commit `.env` atau PDF pasien ke repositori publik.
