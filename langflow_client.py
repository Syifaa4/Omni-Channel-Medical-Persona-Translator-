"""Klien Langflow untuk flow 'Omni-Channel Medical Persona Translator'.

Fitur:
  - ID komponen (File, Anamnesis, Dokumen_lab, Agent) dideteksi otomatis dari flow
  - Hasil deteksi TIDAK di-cache kalau gagal (jadi tetap benar walau Langflow baru dinyalakan belakangan)
  - Teks PDF diekstrak lokal dan dikirim langsung -> data lab sampai ke model walau upload file gagal
  - Upload file bersifat opsional (best-effort), bukan penghalang
  - Retry otomatis + model cadangan saat Gemini overload (503/429)
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# .env dicari di folder proyek (bukan folder tempat perintah dijalankan);
# utf-8-sig supaya .env hasil Notepad Windows (dengan BOM) tetap terbaca.
load_dotenv(Path(__file__).resolve().with_name(".env"), encoding="utf-8-sig")


def _env(name: str, default: str = "") -> str:
    """Baca env var, buang spasi dan tanda kutip yang sering ikut tertulis."""
    return os.getenv(name, default).strip().strip("\"'").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_int_list(name: str, default: str) -> list[int]:
    out = []
    for x in _env(name, default).split(","):
        try:
            out.append(int(x))
        except ValueError:
            pass
    return out


LANGFLOW_URL = _env("LANGFLOW_URL", "http://localhost:7860/api/v1/run/35a26c4b-ebaf-4cac-a624-70c747e810cd")
API_KEY = _env("LANGFLOW_API_KEY")
TIMEOUT = _env_int("LANGFLOW_TIMEOUT", 180)

FILE_NODE_ID_ENV = _env("FILE_NODE_ID")
ANAMNESIS_NODE_ID_ENV = _env("ANAMNESIS_NODE_ID")
FLOW_ID_ENV = _env("LANGFLOW_FLOW_ID")

RETRY_WAITS = _env_int_list("RETRY_WAITS", "5,10,20")
FALLBACK_MODELS = [m.strip() for m in _env("FALLBACK_MODELS", "gemini-2.5-flash-lite").split(",") if m.strip()]
MAX_LAB_CHARS = _env_int("MAX_LAB_CHARS", 12000)
# Opsional: paksa batas token output Agent (hanya dipakai bila komponen Agent punya field-nya)
AGENT_MAX_TOKENS = _env_int("AGENT_MAX_TOKENS", 0)

# URL boleh berisi "?stream=false" dsb.; hanya path yang dipakai untuk mencari flow.
_parsed = urlparse(LANGFLOW_URL)
BASE_URL = f"{_parsed.scheme}://{_parsed.netloc}"
URL_TAIL = _parsed.path.rstrip("/").split("/")[-1]  # UUID atau nama endpoint
RUN_URL = f"{BASE_URL}{_parsed.path}"
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_flow_cache: dict = {}
_node_cache: dict = {}
LAST_RESPONSE: dict = {}  # respons mentah Langflow terakhir (untuk diagnosis jawaban terpotong)

NO_SERVER = "Tidak bisa terhubung ke Langflow di {base}. Pastikan Langflow sedang berjalan (perintah: langflow run)."


class LangflowError(Exception):
    pass


def new_session_id() -> str:
    return str(uuid.uuid4())


def _headers() -> dict:
    return {"x-api-key": API_KEY} if API_KEY else {}


def _http_hint(status: int) -> str:
    return {
        401: " -> API key salah/kosong. Buat key baru di Langflow (Settings > Langflow API Keys) lalu isi di .env.",
        403: " -> API key ditolak atau flow ini milik user lain. Buat key baru dengan user yang sama dengan pemilik flow.",
        404: " -> Flow/endpoint tidak ditemukan. Salin ulang URL dari Langflow (tombol API) ke LANGFLOW_URL di .env.",
        422: " -> Langflow menolak isi permintaan (biasanya ID komponen di tweaks tidak cocok). Klik 'Deteksi ulang komponen'.",
    }.get(status, "")


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
        raise LangflowError(NO_SERVER.format(base=BASE_URL))
    except (requests.exceptions.RequestException, ValueError) as e:
        raise LangflowError(f"Gagal membaca daftar flow (cek API key): {e}")
    for f in flows if isinstance(flows, list) else []:
        if f.get("endpoint_name") == URL_TAIL or f.get("name") == URL_TAIL:
            _flow_cache["id"] = f["id"]
            return f["id"]
    raise LangflowError(
        f"Flow dengan endpoint '{URL_TAIL}' tidak ditemukan. Isi LANGFLOW_FLOW_ID di .env dengan UUID flow."
    )


def _edge_field(edge: dict) -> str:
    """Nama field tujuan sebuah edge (mis. 'anamnesis_pasien'); format handle beda antar versi Langflow."""
    h = (edge.get("data") or {}).get("targetHandle") or edge.get("targetHandle")
    if isinstance(h, str):
        try:
            h = json.loads(h.replace("\u0153", '"'))  # Langflow lama mengganti tanda kutip dengan 'œ'
        except ValueError:
            h = None
    return str(h.get("fieldName", "")).lower() if isinstance(h, dict) else ""


def _file_input_key(template: dict) -> str:
    """Nama field untuk path file di komponen File (beda antar versi Langflow)."""
    for key in ("path", "files", "file_path"):
        if key in template:
            return key
    return "path"


def detect_nodes(force: bool = False) -> dict:
    """Deteksi ID komponen File, Anamnesis, Dokumen-lab (TextInput) dan Agent dari flow.

    Hasil hanya di-cache bila deteksi BERHASIL; kalau gagal akan dicoba lagi pada panggilan berikutnya.
    """
    if _node_cache.get("ok") and not force:
        return _node_cache

    file_id = anam_id = doc_id = agent_id = None
    file_key = "path"
    model_value = None
    agent_limits: dict = {}
    token_key = None
    err = ""
    try:
        try:
            r = requests.get(f"{BASE_URL}/api/v1/flows/{resolve_flow_id()}", headers=_headers(), timeout=30)
        except requests.exceptions.ConnectionError:
            raise LangflowError(NO_SERVER.format(base=BASE_URL))
        if r.status_code >= 400:
            raise LangflowError(f"Gagal membaca flow (HTTP {r.status_code}){_http_hint(r.status_code)}")
        text_inputs = []
        flow_data = r.json()["data"]
        for n in flow_data["nodes"]:
            d = n.get("data", {})
            node_def = d.get("node", {})
            ntype = d.get("type") or n.get("id", "").split("-")[0]
            name = (node_def.get("display_name") or "").lower()
            if ntype == "File" and not file_id:
                file_id = n["id"]
                file_key = _file_input_key(node_def.get("template", {}))
            elif ntype == "TextInput":
                text_inputs.append((n["id"], name))
            elif ntype == "Agent" and not agent_id:
                agent_id = n["id"]
                tpl = node_def.get("template", {})
                model_value = tpl.get("model", {}).get("value")
                for k, v in tpl.items():
                    if isinstance(v, dict) and re.search(r"token|thinking|temperature", k, re.I):
                        agent_limits[k] = v.get("value")
                token_key = next((k for k in ("max_tokens", "max_output_tokens") if k in tpl), None)
        # 1) paling andal: lihat ke field Prompt mana sebuah Text Input tersambung (nama komponen bisa sama-sama "Text Input")
        ti_ids = {nid for nid, _ in text_inputs}
        for e in flow_data.get("edges", []):
            if e.get("source") in ti_ids:
                field = _edge_field(e)
                if "anamnesis" in field:
                    anam_id = e["source"]
                elif "dokumen" in field or "lab" in field:
                    doc_id = e["source"]
        # 2) cadangan: dari nama tampilan komponen
        for nid, name in text_inputs:
            if "anamnesis" in name and not anam_id:
                anam_id = nid
            elif ("dokumen" in name or "lab" in name) and not doc_id and nid != anam_id:
                doc_id = nid
        if not anam_id:
            rest = [t for t in text_inputs if t[0] != doc_id]
            if rest:
                anam_id = rest[0][0]
    except LangflowError as e:
        err = str(e)
    except Exception as e:  # noqa: BLE001 - struktur flow tak terduga
        err = f"Gagal membaca flow: {e}"

    file_id = file_id or FILE_NODE_ID_ENV or None
    anam_id = anam_id or ANAMNESIS_NODE_ID_ENV or None
    _node_cache.clear()
    _node_cache.update(
        {
            "file": file_id, "file_key": file_key, "anamnesis": anam_id, "doc": doc_id,
            "agent": agent_id, "model_value": model_value, "error": err,
            "agent_limits": agent_limits, "token_key": token_key,
            "ok": not err,  # cache hanya dipakai kalau deteksi sukses
        }
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
        raise LangflowError(NO_SERVER.format(base=BASE_URL))
    except requests.exceptions.RequestException as e:
        raise LangflowError(f"Gagal upload file: {e}")
    if r.status_code >= 400:
        raise LangflowError(f"Gagal upload file (HTTP {r.status_code}): {r.text[:300]}{_http_hint(r.status_code)}")
    try:
        return r.json()["file_path"]
    except (KeyError, ValueError) as e:
        raise LangflowError(f"Respons upload tidak dikenali: {e}")


def read_pdf(content: bytes) -> tuple[str, str]:
    """Ekstrak teks PDF lokal. Mengembalikan (teks, catatan). Catatan menjelaskan kalau teks kosong/terpotong."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "Library pypdf belum terpasang. Jalankan: pip install -r requirements.txt"
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                return "", "PDF terkunci password, tidak bisa dibaca. Hilangkan password lalu unggah ulang."
        pages = []
        for p in reader.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:  # noqa: BLE001 - satu halaman rusak jangan menggagalkan semuanya
                pages.append("")
        text = "\n".join(pages).strip()
    except Exception as e:  # noqa: BLE001
        return "", f"File tidak dapat dibuka sebagai PDF: {e}"
    if not text:
        return "", "Tidak ada teks di PDF (kemungkinan hasil scan/foto). Gunakan PDF yang teksnya bisa diseleksi."
    if len(text) > MAX_LAB_CHARS:
        return text[:MAX_LAB_CHARS], f"Teks dipotong ke {MAX_LAB_CHARS} karakter pertama (naikkan MAX_LAB_CHARS di .env bila perlu)."
    return text, ""


def extract_pdf_text(content: bytes) -> str:
    """Kompatibilitas lama: hanya teksnya."""
    return read_pdf(content)[0]


# ----------------------------------------------------------------------- run
def _find_text(obj, depth: int = 0) -> str:
    """Cari teks jawaban di struktur respons Langflow yang bentuknya berbeda-beda antar versi."""
    if depth > 8:
        return ""
    if isinstance(obj, dict):
        for key in ("text", "message"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v
        for v in obj.values():
            found = _find_text(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_text(v, depth + 1)
            if found:
                return found
    return ""


def extract_text(data: dict) -> str:
    try:
        out = data["outputs"][0]["outputs"][0]
    except (KeyError, IndexError, TypeError):
        out = None
    if out:
        # jalur yang paling umum dulu, lalu pencarian umum di dalam output tsb
        results = out.get("results") or {}
        msg = results.get("message") if isinstance(results, dict) else None
        if isinstance(msg, dict):
            if isinstance(msg.get("text"), str) and msg["text"]:
                return msg["text"]
            inner = msg.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str) and inner["text"]:
                return inner["text"]
        found = _find_text(out)
        if found:
            return found
    found = _find_text(data)
    if found:
        return found
    raise LangflowError(
        "Flow berjalan tetapi tidak mengeluarkan teks jawaban. Pastikan flow berakhir di komponen Chat Output. "
        f"Respons mentah: {str(data)[:300]}"
    )


class _RunError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


_OVERLOAD_WORDS = ("unavailable", "overloaded", "high demand", "resource_exhausted", "rate limit", "quota", "too many requests")


def _is_overload(e: _RunError) -> bool:
    t = e.body.lower()
    return e.status in (429, 503) or any(k in t for k in _OVERLOAD_WORDS)


def _run_once(payload: dict) -> str:
    try:
        r = requests.post(
            RUN_URL,
            json=payload,
            headers={"Content-Type": "application/json", **_headers()},
            timeout=TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        raise LangflowError(NO_SERVER.format(base=BASE_URL))
    except requests.exceptions.Timeout:
        raise LangflowError("Permintaan timeout. Naikkan LANGFLOW_TIMEOUT di .env atau coba lagi.")
    if r.status_code >= 400:
        raise _RunError(r.status_code, r.text)
    try:
        data = r.json()
    except ValueError as e:
        raise LangflowError(f"Respons bukan JSON valid: {e}")
    LAST_RESPONSE.clear()
    LAST_RESPONSE.update(data if isinstance(data, dict) else {"data": data})
    return extract_text(data)


def build_payload(question: str, anamnesis: str, file_path: str | None, session_id: str, lab_text: str = "") -> dict:
    nodes = detect_nodes()
    if nodes.get("error") and not (nodes.get("anamnesis") and (nodes.get("file") or nodes.get("doc"))):
        raise LangflowError(nodes["error"])

    anam = anamnesis.strip() or "Tidak ada data anamnesis."
    tweaks: dict = {}
    extra_context = ""  # konteks yang dititipkan ke pertanyaan bila komponen tujuan tidak ditemukan

    if nodes.get("doc"):
        # Flow "App Mode": teks lab masuk lewat Text Input khusus
        tweaks[nodes["doc"]] = {"input_value": lab_text or "Tidak ada dokumen laboratorium."}
    elif lab_text:
        if nodes.get("anamnesis"):
            anam += "\n\n[HASIL LABORATORIUM - teks langsung dari PDF]\n" + lab_text
        else:
            extra_context += "\n\n[HASIL LABORATORIUM - teks langsung dari PDF]\n" + lab_text

    if nodes.get("anamnesis"):
        tweaks[nodes["anamnesis"]] = {"input_value": anam}
    else:
        extra_context = f"\n\n[ANAMNESIS PASIEN]\n{anam}" + extra_context

    if file_path and nodes.get("file"):
        tweaks[nodes["file"]] = {nodes.get("file_key") or "path": [file_path]}

    if AGENT_MAX_TOKENS and nodes.get("agent") and nodes.get("token_key"):
        tweaks[nodes["agent"]] = {nodes["token_key"]: AGENT_MAX_TOKENS}

    return {
        "input_value": question + extra_context,
        "input_type": "chat",
        "output_type": "chat",
        "session_id": session_id,
        "tweaks": tweaks,
    }


def ask(question: str, anamnesis: str, file_path: str | None, session_id: str, lab_text: str = "") -> str:
    payload = build_payload(question, anamnesis, file_path, session_id, lab_text)
    nodes = detect_nodes()
    tweaks = payload["tweaks"]

    # model cadangan hanya dipakai bila bentuk setting model di Agent dikenali
    mv = nodes.get("model_value")
    can_fallback = bool(nodes.get("agent") and isinstance(mv, list) and mv and isinstance(mv[0], dict))
    candidates = [None] + (FALLBACK_MODELS if can_fallback else [])

    last: _RunError | None = None
    for model in candidates:
        p = dict(payload)
        if model:
            p["tweaks"] = {**tweaks, nodes["agent"]: {**tweaks.get(nodes["agent"], {}), "model": [{**mv[0], "name": model}]}}
        tries = 1 + len(RETRY_WAITS) if model is None else 1
        for i in range(tries):
            try:
                return _run_once(p)
            except _RunError as e:
                last = e
                if not _is_overload(e):
                    raise LangflowError(f"{e}{_http_hint(e.status)}")
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
        add("Server Langflow aktif", r.status_code == 200, f"{BASE_URL} -> HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        add("Server Langflow aktif", False, NO_SERVER.format(base=BASE_URL) + f" ({type(e).__name__})")
        return out
    try:
        r = requests.get(f"{BASE_URL}/api/v1/flows/", params={"get_all": "true", "header_flows": "true"},
                         headers=_headers(), timeout=20)
        ok = add("API key diterima Langflow", r.status_code == 200,
                 f"HTTP {r.status_code}" + ("" if r.ok else f" {r.text[:150]}{_http_hint(r.status_code)}"))
        if not ok:
            return out
    except Exception as e:  # noqa: BLE001
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
        nodes["error"] or f"file={nodes['file']} | dokumen_lab={nodes['doc']} | anamnesis={nodes['anamnesis']} | agent={nodes['agent']}")
    if nodes["file"] and not nodes["doc"]:
        try:
            r = requests.post(f"{BASE_URL}/api/v1/files/upload/{fid}",
                              files={"file": ("cek-koneksi.txt", b"tes")}, headers=_headers(), timeout=30)
            add("Izin upload file (opsional)", r.status_code in (200, 201),
                f"HTTP {r.status_code}. " + ("" if r.ok else "Tidak fatal: aplikasi tetap bisa memakai teks PDF langsung."))
        except Exception as e:  # noqa: BLE001
            add("Izin upload file (opsional)", False, str(e))
    elif nodes["doc"]:
        add("Mode tanpa upload (App Mode)", True, "Flow memakai Text Input untuk teks lab, upload tidak diperlukan")
    return out


def looks_truncated(text: str) -> bool:
    """Tebakan apakah jawaban terpotong: judul lapisan kurang dari 3, berakhir menggantung, atau terlalu pendek."""
    t = text.strip()
    if not t:
        return True
    layers = {k for k in split_layers(t) if k in "123"}
    if re.search(r"(?i)\b(?:LAPISAN|LAYER)\s*[123]", t) and len(layers) < 3:
        return True
    return len(t) < 250 or t[-1] in ",:;(-/&" 


# Baris judul lapisan, mis. "## LAPISAN 1 - Dokter", "**Lapisan 2**", "1. LAPISAN 3: EMR".
# Baris biasa yang hanya menyebut "lapisan 1" di tengah kalimat TIDAK dianggap judul.
_LAYER_RE = re.compile(r"(?im)^[ \t]*(?:[#>*_\-\d.)\[ \t]*)(?:LAPISAN|LAYER)\s*([123])\b[^\n]{0,100}$")


def split_layers(text: str) -> dict:
    marks = [(m.start(), m.end(), m.group(1)) for m in _LAYER_RE.finditer(text)]
    if len(marks) < 2:
        return {"raw": text}
    layers: dict = {}
    for i, (_, end, num) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = re.sub(r"\n[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", "", text[end:stop].strip()).strip()
        layers.setdefault(num, body)
    layers["raw"] = text
    return layers
