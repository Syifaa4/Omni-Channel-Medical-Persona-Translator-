"""Medical Persona Translator - antarmuka web. Jalankan: streamlit run app.py"""
import hashlib

import copy

import streamlit as st

import langflow_client as lf
from langflow_client import (
    LangflowError, ask, detect_nodes, diagnose, key_info, looks_truncated, new_session_id,
    read_pdf, split_layers, upload_file,
)

st.set_page_config(page_title="Medical Persona Translator", page_icon="🩺", layout="wide")


def reset():
    st.session_state.session_id = new_session_id()
    st.session_state.history = []
    clear_pdf()


def clear_pdf():
    st.session_state.file_path = None
    st.session_state.file_key = None
    st.session_state.lab_text = ""
    st.session_state.pdf_note = ""
    st.session_state.upload_note = ""


if "session_id" not in st.session_state:
    reset()

# ---------- Sidebar ----------
with st.sidebar:
    st.header("📋 Data Pasien")
    pdf = st.file_uploader("1. Hasil laboratorium (PDF)", type=["pdf"])
    anamnesis = st.text_area(
        "2. Anamnesis / keluhan pasien",
        height=200,
        placeholder="Contoh: Anak perempuan 8 tahun, demam tinggi 4 hari, batuk berdahak kuning, sesak napas...",
    )
    include_text = st.checkbox("Sertakan teks PDF langsung (disarankan)", value=True,
                               help="Teks PDF dikirim juga ke AI supaya data lab pasti sampai.")

    if pdf is None:
        # PDF dihapus dari uploader -> buang data lama supaya tidak ikut terkirim
        if st.session_state.file_key:
            clear_pdf()
    else:
        content = pdf.getvalue()
        key = f"{pdf.name}:{hashlib.md5(content).hexdigest()}"  # nama sama + isi beda tetap diproses ulang
        if key != st.session_state.file_key:
            with st.spinner("Memproses PDF..."):
                clear_pdf()
                text, note = read_pdf(content)
                st.session_state.lab_text, st.session_state.pdf_note = text, note
                # Upload ke Langflow hanya pelengkap. Gagal upload TIDAK membatalkan PDF
                # selama teksnya sudah terbaca.
                nd = detect_nodes()
                if nd.get("file") and not nd.get("doc"):
                    try:
                        st.session_state.file_path = upload_file(pdf.name, content)
                    except LangflowError as e:
                        st.session_state.upload_note = str(e)
                st.session_state.file_key = key

    if pdf is not None and st.session_state.file_key:
        n = len(st.session_state.lab_text)
        if n or st.session_state.file_path:
            st.success(f"PDF siap: {pdf.name}")
        else:
            st.error(f"PDF {pdf.name} belum bisa dipakai.")
        if n:
            st.caption(f"Teks terbaca: {n} karakter")
        if st.session_state.pdf_note:
            (st.warning if not n else st.info)(st.session_state.pdf_note)
        if st.session_state.upload_note:
            (st.info if n else st.error)(
                ("Upload file ke Langflow dilewati, memakai teks PDF langsung. " if n else "") + st.session_state.upload_note
            )

    if st.button("Pasien / sesi baru", width="stretch"):
        reset()
        st.rerun()
    st.caption(f"Session: `{st.session_state.session_id[:8]}`")

    with st.expander("🔧 Diagnostik", expanded=False):
        st.caption(f"API key terbaca: {key_info()}")
        if st.button("✅ Cek koneksi", width="stretch"):
            for step, ok, detail in diagnose():
                (st.success if ok else st.error)(f"**{step}**\n\n{detail}")
        nd = detect_nodes()
        st.write(f"File: `{nd['file']}`")
        st.write(f"Dokumen_lab: `{nd['doc']}`")
        st.write(f"Anamnesis: `{nd['anamnesis']}`")
        st.write(f"Agent: `{nd['agent']}`")
        if nd.get("agent_limits"):
            st.write("Setelan Agent (token/suhu):")
            st.json(nd["agent_limits"])
        if nd.get("error"):
            st.warning(nd["error"])
        if st.button("Deteksi ulang komponen"):
            detect_nodes(force=True)
            st.rerun()
        if st.button("🧪 Uji data yang diterima AI"):
            if not (st.session_state.file_path or st.session_state.lab_text):
                st.warning("Unggah PDF dulu.")
            else:
                with st.spinner("Menguji..."):
                    try:
                        res = ask(
                            "TES KONEKSI DATA (abaikan format 3 lapisan). Jawab singkat: "
                            "(1) nama, usia dan jenis kelamin pasien dari dokumen lab, "
                            "(2) tiga nilai lab abnormal beserta angkanya, "
                            "(3) satu kalimat isi anamnesis.",
                            anamnesis, st.session_state.file_path, new_session_id(),
                            st.session_state.lab_text if include_text else "",
                        )
                    except LangflowError as e:
                        res = f"⚠️ {e}"
                st.info(res)

# ---------- Header ----------
st.title("🩺 Omni-Channel Medical Persona Translator")
st.caption(
    "Menerjemahkan hasil lab menjadi narasi untuk dokter, pasien/keluarga, dan ringkasan EMR. "
    "Alat bantu edukasi — bukan diagnosis definitif."
)

presets = {
    "Analisis lengkap": "Analisis hasil laboratorium dan anamnesis pasien ini secara lengkap.",
    "Perlu rawat inap?": "Apakah pasien perlu rawat inap? Jelaskan alasannya.",
    "Jelaskan ke keluarga": "Jelaskan hasil ini kepada keluarga pasien dengan bahasa sederhana.",
    "Tindakan prioritas": "Apa tindakan yang perlu dilakukan terlebih dahulu?",
}
clicked = None
for col, (label, q) in zip(st.columns(4), presets.items()):
    if col.button(label, width="stretch"):
        clicked = q

question = st.chat_input("Tulis pertanyaan, mis. 'Bagaimana kondisi pasien terakhir?'") or clicked

# ---------- Kirim ----------
if question:
    has_lab = bool(st.session_state.file_path or st.session_state.lab_text)
    if not has_lab and not anamnesis.strip():
        st.warning("Unggah PDF lab dan/atau isi anamnesis terlebih dahulu.")
    else:
        with st.spinner("AI sedang menganalisis... (jika server Gemini sibuk, aplikasi mencoba ulang otomatis)"):
            raw = {}
            try:
                lf.LAST_RESPONSE.clear()
                ans, failed = ask(question, anamnesis, st.session_state.file_path, st.session_state.session_id,
                                  st.session_state.lab_text if include_text else ""), False
                raw = copy.deepcopy(lf.LAST_RESPONSE)
            except LangflowError as e:
                ans, failed = str(e), True
                st.session_state.session_id = new_session_id()  # sesi bersih setelah gagal
        st.session_state.history.append((question, ans, failed, raw))

# ---------- Riwayat ----------
if not st.session_state.history:
    st.info("Unggah PDF lab & isi anamnesis di sidebar, lalu klik tombol cepat atau ketik pertanyaan.")

for i, (q, a, failed, raw) in enumerate(reversed(st.session_state.history)):
    with st.container(border=True):
        st.markdown(f"**🙋 {q}**")
        if failed:
            st.error(a, icon="⚠️")
            continue
        if looks_truncated(a):
            st.warning(
                f"Jawaban AI tampak **terpotong** ({len(a)} karakter). Ini berasal dari model/flow di Langflow, bukan dari "
                "aplikasi ini. Coba: naikkan **Max Tokens** di komponen Agent, ganti model, atau lihat 'Respons mentah' di bawah.",
                icon="✂️",
            )
        layers = split_layers(a)
        if "1" in layers or "2" in layers:
            t1, t2, t3, t4 = st.tabs(["🩺 Dokter / Tenaga Medis", "👤 Pasien / Keluarga", "📄 EMR Summary", "Teks mentah"])
            t1.markdown(layers.get("1", "_tidak tersedia_"))
            t2.markdown(layers.get("2", "_tidak tersedia_"))
            t3.markdown(layers.get("3", "_tidak tersedia_"))
            t4.text(a)
            if "3" in layers:
                t3.download_button("Unduh EMR (.txt)", layers["3"], file_name="emr_summary.txt", key=f"dl{i}")
        else:
            st.markdown(a)
        with st.expander("🔍 Respons mentah Langflow", expanded=False):
            st.caption(f"Panjang teks jawaban: {len(a)} karakter")
            st.text(a)
            if raw:
                st.json(raw, expanded=False)
