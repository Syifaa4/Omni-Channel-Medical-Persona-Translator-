"""Klien Langflow untuk flow 'Omni-Channel Medical Persona Translator'.

Fitur:
  - ID komponen (File, Anamnesis, Agent) dideteksi otomatis dari flow
  - URL boleh memakai nama endpoint (mis. app-medis) atau UUID
  - Retry otomatis + model cadangan saat Gemini overload (503/429)
  - Teks PDF juga dikirim langsung (redundan) agar data lab pasti sampai ke model
"""
import io
import os
import re
import time
import uuid
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

LANGFLOW_URL = os.getenv(
    "LANGFLOW_URL",
    "http://localhost:7860/api/v1/run/e88b98a7-591a-4429-843e-bccc5dfeb231",
)
API_KEY = os.getenv("LANGFLOW_API_KEY", "")
TIMEOUT = int(os.getenv("LANGFLOW_TIMEOUT", "180"))

FILE_NODE_ID_ENV = os.getenv("FILE_NODE_ID", "").strip()
ANAMNESIS_NODE_ID_ENV = os.getenv("ANAMNESIS_NODE_ID", "").strip()
FLOW_ID_ENV = os.getenv("LANGFLOW_FLOW_ID", "").strip()

# jeda (detik) antar percobaan ulang saat Gemini overload
RETRY_WAITS = [int(x) for x in os.getenv("RETRY_WAITS", "5,10,20").split(",") if x.strip()]
# model cadangan, dipakai jika model utama tetap overload (pisahkan dengan koma; kosongkan untuk mematikan)
FALLBACK_MODELS = [m.strip() for m in os.getenv("FALLBACK_MODELS", "gemini-2.5-flash-lite").split(",") if m.strip()]
MAX_LAB_CHARS = int(os.getenv("MAX_LAB_CHARS", "12000"))

_parsed = urlparse(LANGFLOW_URL)
BASE_URL = f"{_parsed.scheme}://{_parsed.netloc}"
URL_TAIL = _parsed.path.rstrip("/").split("/")[-1]  # UUID atau nama endpoint
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_flow_cache: dict = {}
_node_cache: dict = {}


class LangflowError(Exception):
    pass


def new_session_id() -> str:
    return str(uuid.uuid4())


def _headers() -> dict:
    return {"x-api-key": API_KEY} if API_KEY else {}


# ---------------------------------------------------------------- flow & node
def resolve_flow_id() -> str:
    if FLOW_ID_ENV:
        return FLOW_ID_ENV
    if _UUID_RE.match(URL_TAIL):
        return URL_TAIL
    if "id" in _flow_cache:
        return _flow_cache["id"]
    try:
        r = requests.get(
            f"{BASE_URL}/api/v1/flows/",
            params={"get_all": "true", "header_flows": "true"},
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        flows = r.json()
    except requests.exceptions.ConnectionError:
        raise LangflowError("Tidak bisa terhubung ke Langflow (cek server di localhost:7860).")
    except (requests.exceptions.RequestException, ValueError) as e:
        raise LangflowError(f"Gagal membaca daftar flow (cek API key): {e}")
    for f in flows if isinstance(flows, list) else []:
        if f.get("endpoint_name") == URL_TAIL or f.get("name") == URL_TAIL:
            _flow_cache["id"] = f["id"]
            return f["id"]
    raise LangflowError(
        f"Flow dengan endpoint '{URL_TAIL}' tidak ditemukan. Isi LANGFLOW_FLOW_ID di .env dengan UUID flow."
    )


def detect_nodes(force: bool = False) -> dict:
    """Deteksi ID komponen File, Anamnesis, Dokumen-lab (TextInput) dan Agent dari flow."""
    if _node_cache and not force:
        return _node_cache
    file_id = anam_id = doc_id = agent_id = None
    model_value = None
    source, err = "auto", ""
    try:
        r = requests.get(f"{BASE_URL}/api/v1/flows/{resolve_flow_id()}", headers=_headers(), timeout=30)
        r.raise_for_status()
        text_inputs = []
        for n in r.json()["data"]["nodes"]:
            d = n.get("data", {})
            ntype = d.get("type") or n.get("id", "").split("-")[0]
            name = (d.get("node", {}).get("display_name") or "").lower()
            if ntype == "File" and not file_id:
                file_id = n["id"]
            elif ntype == "TextInput":
                text_inputs.append((n["id"], name))
            elif ntype == "Agent" and not agent_id:
                agent_id = n["id"]
                model_value = d.get("node", {}).get("template", {}).get("model", {}).get("value")
        for nid, name in text_inputs:
            if "anamnesis" in name:
                anam_id = nid
            elif "dokumen" in name or "lab" in name:
                doc_id = nid
        if not anam_id:
            rest = [t for t in text_inputs if t[0] != doc_id]
            if rest:
                anam_id = rest[0][0]
    except LangflowError as e:
        err, source = str(e), "env"
    except Exception as e:
        err, source = f"Gagal membaca flow: {e}", "env"
    file_id = file_id or FILE_NODE_ID_ENV or None
    anam_id = anam_id or ANAMNESIS_NODE_ID_ENV or None
    if not (anam_id and (file_id or doc_id)):
        source = "env"
    _node_cache.clear()
    _node_cache.update(
        {"file": file_id, "anamnesis": anam_id, "doc": doc_id, "agent": agent_id,
         "model_value": model_value, "source": source, "error": err}
    )
    return _node_cache


# ---------------------------------------------------------------------- file
def upload_file(filename: str, content: bytes) -> str:
    flow_id = resolve_flow_id()
    try:
        r = requests.post(
            f"{BASE_URL}/api/v1/files/upload/{flow_id}",
            files={"file": (filename, content)},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        raise LangflowError("Tidak bisa terhubung ke Langflow (cek server di localhost:7860).")
    except requests.exceptions.RequestException as e:
        raise LangflowError(f"Gagal upload file: {e}")
    if r.status_code >= 400:
        hint = ""
        if r.status_code in (401, 403):
            hint = " -> API key salah/kosong/kedaluwarsa, atau flow milik user lain. Jalankan 'Cek koneksi' di Diagnostik."
        raise LangflowError(f"Gagal upload file (HTTP {r.status_code}): {r.text[:300]}{hint}")
    try:
        return r.json()["file_path"]
    except (KeyError, ValueError) as e:
        raise LangflowError(f"Respons upload tidak dikenali: {e}")


def extract_pdf_text(content: bytes) -> str:
    """Ekstrak teks PDF secara lokal. String kosong jika gagal / PDF hasil scan."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        return text[:MAX_LAB_CHARS]
    except Exception:
        return ""


# ----------------------------------------------------------------------- run
def extract_text(data: dict) -> str:
    try:
        out = data["outputs"][0]["outputs"][0]
        msg = out.get("results", {}).get("message", {})
        if isinstance(msg, dict) and msg.get("text"):
            return msg["text"]
        m2 = out.get("outputs", {}).get("message", {}).get("message")
        if isinstance(m2, str) and m2:
            return m2
        if isinstance(m2, dict) and m2.get("text"):
            return m2["text"]
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return str(data)


class _RunError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


def _is_overload(e: _RunError) -> bool:
    t = e.body.lower()
    keys = ("503", "unavailable", "overloaded", "high demand", "resource_exhausted", "429", "rate limit", "quota")
    return e.status in (429, 503) or any(k in t for k in keys)


def _run_once(payload: dict) -> str:
    try:
        r = requests.post(
            LANGFLOW_URL,
            json=payload,
            headers={"Content-Type": "application/json", **_headers()},
            timeout=TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        raise LangflowError("Tidak bisa terhubung ke Langflow (cek server di localhost:7860).")
    except requests.exceptions.Timeout:
        raise LangflowError("Permintaan timeout. Naikkan LANGFLOW_TIMEOUT di .env atau coba lagi.")
    if r.status_code >= 400:
        raise _RunError(r.status_code, r.text)
    try:
        return extract_text(r.json())
    except ValueError as e:
        raise LangflowError(f"Respons bukan JSON valid: {e}")


def ask(question: str, anamnesis: str, file_path: str | None, session_id: str, lab_text: str = "") -> str:
    nodes = detect_nodes()

    anam = anamnesis.strip() or "Tidak ada data anamnesis."
    tweaks = {}
    if nodes.get("doc"):
        # Flow "App Mode": teks lab masuk lewat Text Input khusus, tanpa upload file
        tweaks[nodes["doc"]] = {"input_value": lab_text or "Tidak ada dokumen laboratorium."}
    elif lab_text:
        anam += "\n\n[HASIL LABORATORIUM - teks langsung dari PDF]\n" + lab_text
    if nodes["anamnesis"]:
        tweaks[nodes["anamnesis"]] = {"input_value": anam}
    if file_path and nodes["file"]:
        tweaks[nodes["file"]] = {"path": [file_path]}

    payload = {
        "input_value": question,
        "input_type": "chat",
        "output_type": "chat",
        "session_id": session_id,
        "tweaks": tweaks,
    }

    last: _RunError | None = None
    # 1) model utama dengan retry; 2) model cadangan masing-masing sekali
    candidates = [None] + (FALLBACK_MODELS if nodes.get("agent") and nodes.get("model_value") else [])
    for model in candidates:
        p = dict(payload)
        if model:
            mv = nodes["model_value"]
            if isinstance(mv, list) and mv and isinstance(mv[0], dict):
                p["tweaks"] = {**tweaks, nodes["agent"]: {"model": [{**mv[0], "name": model}]}}
        tries = 1 + len(RETRY_WAITS) if model is None else 1
        for i in range(tries):
            try:
                return _run_once(p)
            except _RunError as e:
                last = e
                if model is None and not _is_overload(e):
                    raise LangflowError(str(e))
                if i < tries - 1:
                    time.sleep(RETRY_WAITS[i])
    raise LangflowError(
        "Server Gemini masih overload setelah beberapa percobaan dan model cadangan. "
        "Tunggu beberapa menit lalu coba lagi, atau ganti model di komponen Agent. "
        f"Detail terakhir: {last}"
    )


def key_info() -> str:
    if not API_KEY:
        return "KOSONG"
    if API_KEY == "YOUR_API_KEY_HERE":
        return "MASIH PLACEHOLDER (YOUR_API_KEY_HERE)"
    return f"{API_KEY[:5]}...{API_KEY[-3:]} ({len(API_KEY)} karakter)"


def diagnose() -> list:
    """Cek koneksi bertahap. Mengembalikan list (langkah, ok, detail)."""
    out = []

    def add(step, ok, detail=""):
        out.append((step, ok, detail))
        return ok

    add("API key terbaca dari .env", API_KEY not in ("", "YOUR_API_KEY_HERE"), key_info())
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=10)
        add("Server Langflow aktif", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        add("Server Langflow aktif", False, f"Tidak terhubung: {e}")
        return out
    try:
        r = requests.get(f"{BASE_URL}/api/v1/flows/", params={"get_all": "true", "header_flows": "true"},
                         headers=_headers(), timeout=20)
        ok = add("API key diterima Langflow", r.status_code == 200, f"HTTP {r.status_code} {'' if r.ok else r.text[:200]}")
        if not ok:
            return out
    except Exception as e:
        add("API key diterima Langflow", False, str(e))
        return out
    try:
        fid = resolve_flow_id()
        add("Flow ditemukan", True, fid)
    except LangflowError as e:
        add("Flow ditemukan", False, str(e))
        return out
    nodes = detect_nodes(force=True)
    add("Komponen flow terdeteksi", bool(nodes["anamnesis"] and (nodes["file"] or nodes["doc"])),
        f"file={nodes['file']} | dokumen_lab={nodes['doc']} | anamnesis={nodes['anamnesis']}")
    if nodes["file"] and not nodes["doc"]:
        try:
            r = requests.post(f"{BASE_URL}/api/v1/files/upload/{fid}",
                              files={"file": ("cek-koneksi.txt", b"tes")}, headers=_headers(), timeout=30)
            add("Izin upload file", r.status_code in (200, 201), f"HTTP {r.status_code} {'' if r.ok else r.text[:200]}")
        except Exception as e:
            add("Izin upload file", False, str(e))
    elif nodes["doc"]:
        add("Mode tanpa upload (App Mode)", True, "Flow memakai Text Input untuk teks lab, upload tidak diperlukan")
    return out


def split_layers(text: str) -> dict:
    pat = re.compile(r"(?im)^.*LAPISAN\s*([123])\b.*$")
    marks = [(m.start(), m.end(), m.group(1)) for m in pat.finditer(text)]
    if len(marks) < 2:
        return {"raw": text}
    layers = {}
    for i, (_, end, num) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        layers[num] = text[end:stop].strip()
    layers["raw"] = text
    return layers
