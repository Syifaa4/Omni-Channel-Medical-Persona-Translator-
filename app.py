"""Medical Persona Translator - antarmuka web. Jalankan: streamlit run app.py"""
import streamlit as st

from langflow_client import (
    LangflowError, ask, detect_nodes, diagnose, extract_pdf_text, key_info,
    new_session_id, split_layers, upload_file,
)

st.set_page_config(page_title="Medical Persona Translator", page_icon="🩺", layout="wide")


def reset():
    st.session_state.session_id = new_session_id()
    st.session_state.history = []
    st.session_state.file_path = None
    st.session_state.file_name = None
    st.session_state.lab_text = ""


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

    if pdf and pdf.name != st.session_state.file_name:
        with st.spinner("Memproses PDF..."):
            content = pdf.getvalue()
            nd = detect_nodes()
            st.session_state.lab_text = extract_pdf_text(content)
            try:
                # Flow "App Mode" (Text Input Dokumen_lab) tidak butuh upload file
                st.session_state.file_path = None if nd.get("doc") else upload_file(pdf.name, content)
                st.session_state.file_name = pdf.name
            except LangflowError as e:
                st.session_state.file_name = None
                st.error(str(e))
    if pdf and st.session_state.file_name == pdf.name:
        n = len(st.session_state.lab_text)
        st.success(f"PDF siap: {pdf.name}")
        if n:
            st.caption(f"Teks terbaca: {n} karakter")
        else:
            st.warning("Teks PDF tidak terbaca (mungkin PDF scan).")

    if st.button("Pasien / sesi baru", use_container_width=True):
        reset()
        st.rerun()
    st.caption(f"Session: `{st.session_state.session_id[:8]}`")

    with st.expander("🔧 Diagnostik", expanded=False):
        st.caption(f"API key terbaca: {key_info()}")
        if st.button("✅ Cek koneksi", use_container_width=True):
            for step, ok, detail in diagnose():
                (st.success if ok else st.error)(f"**{step}**\n\n{detail}")
        nd = detect_nodes()
        st.write(f"File: `{nd['file']}`")
        st.write(f"Dokumen_lab: `{nd['doc']}`")
        st.write(f"Anamnesis: `{nd['anamnesis']}`")
        st.write(f"Agent: `{nd['agent']}`")
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
    if col.button(label, use_container_width=True):
        clicked = q

question = st.chat_input("Tulis pertanyaan, mis. 'Bagaimana kondisi pasien terakhir?'") or clicked

# ---------- Kirim ----------
if question:
    has_lab = bool(st.session_state.file_path or st.session_state.lab_text)
    if not has_lab and not anamnesis.strip():
        st.warning("Unggah PDF lab dan/atau isi anamnesis terlebih dahulu.")
    else:
        with st.spinner("AI sedang menganalisis... (jika server Gemini sibuk, aplikasi mencoba ulang otomatis)"):
            try:
                ans = ask(question, anamnesis, st.session_state.file_path, st.session_state.session_id,
                          st.session_state.lab_text if include_text else "")
            except LangflowError as e:
                ans = f"⚠️ {e}"
                st.session_state.session_id = new_session_id()  # sesi bersih setelah gagal
        st.session_state.history.append((question, ans))

# ---------- Riwayat ----------
if not st.session_state.history:
    st.info("Unggah PDF lab & isi anamnesis di sidebar, lalu klik tombol cepat atau ketik pertanyaan.")

for i, (q, a) in enumerate(reversed(st.session_state.history)):
    with st.container(border=True):
        st.markdown(f"**🙋 {q}**")
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
