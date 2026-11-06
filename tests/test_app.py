"""Uji otomatis (tanpa Langflow/Gemini asli): python -m pytest tests -q"""
import importlib, io, os, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import mock_langflow as m  # noqa: E402

PORT = 7871
S = m.State()
os.environ.update(LANGFLOW_URL=f"http://127.0.0.1:{PORT}/api/v1/run/app-medis", LANGFLOW_API_KEY="k", RETRY_WAITS="0,0")
import langflow_client as c  # noqa: E402


def make_pdf(lines):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO(); cv = canvas.Canvas(buf); y = 800
    for ln in lines:
        cv.drawString(50, y, ln); y -= 20
    cv.save(); return buf.getvalue()


@pytest.fixture(autouse=True)
def server():
    S.upload_status, S.run_script, S.last_run, S.health_ok, S.variant = 200, [], None, True, "classic"
    srv = m.start(S, PORT)
    c._node_cache.clear(); c._flow_cache.clear()
    yield
    srv.shutdown(); srv.server_close()


def test_pdf_text_read():
    text, note = c.read_pdf(make_pdf(["Pasien: Budi", "Hemoglobin 5.2 g/dL (rendah)"]))
    assert "Hemoglobin 5.2" in text and note == ""


def test_pdf_empty_and_corrupt_give_reason():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO(); cv = canvas.Canvas(buf); cv.showPage(); cv.save()
    assert "scan" in c.read_pdf(buf.getvalue())[1]
    assert "tidak dapat dibuka" in c.read_pdf(b"bukan pdf")[1]


def test_pdf_truncation_noted(monkeypatch):
    monkeypatch.setattr(c, "MAX_LAB_CHARS", 20)
    text, note = c.read_pdf(make_pdf(["A" * 60]))
    assert len(text) == 20 and "dipotong" in note


def test_nodes_detected_and_payload():
    p = c.build_payload("tanya", "demam 3 hari", "f/x.pdf", "s", "Hb 5")
    assert p["tweaks"]["TextInput-jE0Kv"]["input_value"].startswith("demam 3 hari")
    assert "Hb 5" in p["tweaks"]["TextInput-jE0Kv"]["input_value"]
    assert p["tweaks"]["File-Nn09e"] == {"path": ["f/x.pdf"]}


def test_bug_cache_not_poisoned_when_server_starts_late():
    """Dulu: deteksi gagal tersimpan permanen -> anamnesis tak pernah terkirim."""
    S.health_ok = True
    c.FLOW_ID_ENV = ""
    srv_nodes = c.detect_nodes(force=True)
    assert srv_nodes["ok"] and srv_nodes["anamnesis"] == "TextInput-jE0Kv"


def test_detection_failure_not_cached():
    c._node_cache.clear()
    old = c.BASE_URL
    c.BASE_URL = "http://127.0.0.1:1"          # server tidak ada
    try:
        assert c.detect_nodes()["ok"] is False
    finally:
        c.BASE_URL = old
    assert c.detect_nodes()["ok"] is True       # server ada -> pulih tanpa restart


def test_ask_success_and_layers():
    ans = c.ask("tes", "demam", None, "s", "Hb 5")
    layers = c.split_layers(ans)
    assert layers["1"] == "Hb rendah" and layers["2"] == "Darah kurang" and layers["3"] == "Anemia"
    assert "Hb 5" in str(S.last_run)


def test_retry_on_overload_then_success():
    S.run_script = [(503, "model overloaded"), (429, "quota")]
    assert "LAPISAN 1" in c.ask("tes", "a", None, "s")


def test_non_overload_error_has_hint_and_no_retry():
    S.run_script = [(404, "not found"), (404, "not found")]
    with pytest.raises(c.LangflowError) as e:
        c.ask("tes", "a", None, "s")
    assert "Salin ulang URL" in str(e.value) and len(S.run_script) == 1


def test_number_429_in_lab_text_not_treated_as_overload():
    S.run_script = [(500, "error: nilai 429 mg/dL tidak valid")]
    with pytest.raises(c.LangflowError):
        c.ask("tes", "a", None, "s")
    assert S.run_script == []                   # hanya 1x dipanggil


def test_all_overload_gives_friendly_error():
    S.run_script = [(503, "overloaded")] * 10
    with pytest.raises(c.LangflowError) as e:
        c.ask("tes", "a", None, "s")
    assert "overload" in str(e.value)


def test_split_layers_ignores_inline_mentions():
    t = "Ringkasan: lihat Lapisan 1 untuk detail.\n## LAPISAN 1 - Dokter\n- Hb rendah\n## LAPISAN 2\nHalo\n"
    l = c.split_layers(t)
    assert l["1"] == "- Hb rendah" and l["2"] == "Halo"
    assert c.split_layers("jawaban biasa tanpa lapisan") == {"raw": "jawaban biasa tanpa lapisan"}


def test_extract_text_variants():
    assert c.extract_text({"outputs": [{"outputs": [{"results": {"message": {"data": {"text": "A"}}}}]}]}) == "A"
    assert c.extract_text({"outputs": [{"outputs": [{"outputs": {"message": {"message": "B"}}}]}]}) == "B"
    with pytest.raises(c.LangflowError):
        c.extract_text({"outputs": []})


def test_diagnose_all_ok():
    res = c.diagnose()
    assert all(ok for _, ok, _ in res), res


def test_wrong_api_key_is_explained():
    old = c.API_KEY; c.API_KEY = "salah"
    try:
        res = c.diagnose()
    finally:
        c.API_KEY = old
    bad = [d for s, ok, d in res if not ok and "API key diterima" in s]
    assert bad and "API key" in bad[0]


# ---------------- UI (Streamlit AppTest) ----------------
def _app():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)


def test_ui_renders_without_error():
    at = _app().run()
    assert not at.exception


def test_ui_pdf_upload_failure_still_works():
    """Skenario asli yang gagal: upload ditolak Langflow, tapi PDF tetap harus terbaca & dikirim ke AI."""
    S.upload_status = 404
    at = _app().run()
    at.file_uploader[0].upload("lab.pdf", make_pdf(["Hemoglobin 5.2 g/dL"]), "application/pdf").run()
    assert not at.exception
    assert any("PDF siap" in x.value for x in at.success), [x.value for x in at.error]
    at.text_area[0].set_value("demam 4 hari").run()
    at.chat_input[0].set_value("analisis").run()
    assert not at.exception and not at.error
    sent = S.last_run["tweaks"]["TextInput-jE0Kv"]["input_value"]
    assert "demam 4 hari" in sent and "Hemoglobin 5.2" in sent


def test_ui_pdf_removed_clears_data():
    at = _app().run()
    at.file_uploader[0].upload("lab.pdf", make_pdf(["Hemoglobin 5.2"]), "application/pdf").run()
    assert at.session_state["lab_text"]
    at.file_uploader[0].set_value(None).run()    # pengguna menekan X pada file
    assert at.session_state["lab_text"] == "" and at.session_state["file_key"] is None


def test_ui_scanned_pdf_shows_reason():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO(); cv = canvas.Canvas(buf); cv.showPage(); cv.save()
    S.upload_status = 404
    at = _app().run()
    at.file_uploader[0].upload("scan.pdf", buf.getvalue(), "application/pdf").run()
    assert not at.exception
    assert any("scan" in w.value for w in at.warning)
    assert any("belum bisa dipakai" in e.value for e in at.error)


def test_ui_error_shown_not_crash():
    S.run_script = [(404, "x")]
    at = _app().run()
    at.session_state["lab_text"] = "Hb 5"
    at.text_area[0].set_value("demam").run()
    at.chat_input[0].set_value("tanya").run()
    assert not at.exception and len(at.error) >= 1


# ---------------- jawaban terpotong ----------------
def test_looks_truncated():
    full = "## LAPISAN 1\n" + "a" * 200 + "\n## LAPISAN 2\n" + "b" * 100 + "\n## LAPISAN 3\nEMR selesai."
    assert not c.looks_truncated(full)
    assert c.looks_truncated("LAPISAN 1 — UNTUK DOKTER\n\nKONTEKS TERDETEKSI: PEDIATRI\n\nTINGKAT PERHATIAN")
    assert c.looks_truncated("SUMMARY KLINIS: An. Aisyah Putri,")
    assert c.looks_truncated("")


def test_agent_limits_detected_and_override(monkeypatch):
    nd = c.detect_nodes(force=True)
    assert nd["agent_limits"]["max_tokens"] == 1000 and nd["token_key"] == "max_tokens"
    monkeypatch.setattr(c, "AGENT_MAX_TOKENS", 8192)
    p = c.build_payload("q", "a", None, "s")
    assert p["tweaks"]["Agent-xyz"] == {"max_tokens": 8192}


def test_override_kept_in_fallback_model(monkeypatch):
    monkeypatch.setattr(c, "AGENT_MAX_TOKENS", 8192)
    S.run_script = [(503, "overloaded")] * 3      # model utama gagal 3x (1 + 2 retry) -> pakai model cadangan
    c.ask("q", "a", None, "s")
    t = S.last_run["tweaks"]["Agent-xyz"]
    assert t["max_tokens"] == 8192 and t["model"][0]["name"] == "gemini-2.5-flash-lite"


def test_raw_response_saved():
    c.ask("q", "a", None, "s")
    assert "outputs" in c.LAST_RESPONSE


def test_ui_truncated_warning_and_raw_panel():
    S.run_script = []
    at = _app().run()
    at.session_state["lab_text"] = "Hb 5"
    at.text_area[0].set_value("demam").run()
    at.session_state["history"] = [("q", "LAPISAN 1 — UNTUK DOKTER\n\nTINGKAT PERHATIAN", False, {})]
    at.run()
    assert not at.exception
    assert any("terpotong" in w.value for w in at.warning)


# ---------------- deteksi lewat edge (dua Text Input bernama sama) ----------------
def test_edge_based_detection_two_identically_named_text_inputs():
    S.variant = "appmode"
    nd = c.detect_nodes(force=True)
    assert nd["doc"] == "TextInput-BBB" and nd["anamnesis"] == "TextInput-AAA"


def test_appmode_payload_separates_anamnesis_and_lab():
    S.variant = "appmode"
    c.detect_nodes(force=True)
    p = c.build_payload("q", "demam 4 hari", None, "s", "Hb 10.8")
    assert p["tweaks"]["TextInput-AAA"]["input_value"] == "demam 4 hari"
    assert p["tweaks"]["TextInput-BBB"]["input_value"] == "Hb 10.8"
