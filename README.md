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
| File (`File-Nn09e`, PDF lab) | upload `/api/v1/files/upload/<flow_id>` + `tweaks` |

## Menjalankan
1. Jalankan Langflow, impor JSON flow, pastikan API key Gemini di komponen Agent sudah terisi.
2. `pip install -r requirements.txt`
3. Salin `.env.example` → `.env`, isi `LANGFLOW_API_KEY`. Pastikan `LANGFLOW_URL` sama dengan endpoint flow kamu.
4. `streamlit run app.py`

## Catatan
- Jika ID komponen di flow kamu berbeda (mis. setelah impor ulang), ubah `FILE_NODE_ID` / `ANAMNESIS_NODE_ID` di `.env`.
- Hasil hanya alat bantu; keputusan klinis tetap oleh tenaga medis.
