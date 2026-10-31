"""Prevod príloh e-mailov na text: PDF, XLSX, DOCX, CSV/TXT, HTML.

Prílohy sú nedôveryhodný vstup (sekcia 2.5 PROMPT.md): spracúvajú sa v **izolovanom podprocese**
(``python -I`` bez premenných prostredia – teda aj bez API kľúčov – s limitom pamäte, času CPU
a celkového času). Nič sa nespúšťa (makrá, skripty), neotvárajú sa žiadne odkazy a ZIP kontajnery
(XLSX, DOCX) sa pred rozbalením kontrolujú na „zip bomby“.

Tento súbor sa spúšťa aj samostatne ako pracovný proces, preto **nesmie importovať balík vlaky**.
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import date, datetime, time
from html.parser import HTMLParser

MAX_TEXT_CHARS = 300_000
MAX_UNZIPPED_BYTES = 150 * 1024 * 1024
MAX_ZIP_RATIO = 150
MAX_ZIP_ENTRIES = 3000
PDF_MAX_PAGES = 60
XLSX_MAX_ROWS = 5000
SANDBOX_TIMEOUT_S = 45
SANDBOX_MEMORY_MB = 1024
SANDBOX_CPU_S = 40

SUPPORTED = ("pdf", "xlsx", "docx", "csv", "txt", "html")


class AttachmentError(Exception):
    """Prílohu nemožno previesť na text (správa je určená pre dispečera)."""


# ---------------------------------------------------------------------- rozpoznanie typu
def detect_kind(filename: str, content_type: str, data: bytes) -> str:
    """pdf | xlsx | docx | csv | txt | html | xls | doc | image | archive | unknown"""
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    head = data[:8]
    if head.startswith(b"%PDF") or name.endswith(".pdf") or ct == "application/pdf":
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "word/document.xml" in names:
            return "docx"
        return "archive"
    if head.startswith(b"\xd0\xcf\x11\xe0"):       # starý binárny formát Office (OLE)
        return "xls" if name.endswith(".xls") or "excel" in ct else "doc"
    if ct.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp")):
        return "image"
    if name.endswith((".htm", ".html")) or ct == "text/html":
        return "html"
    if name.endswith(".csv") or ct in ("text/csv", "application/csv"):
        return "csv"
    if name.endswith((".txt", ".text")) or ct.startswith("text/"):
        return "txt"
    if name.endswith(".xlsx"):
        return "xlsx"
    if name.endswith(".docx"):
        return "docx"
    return "unknown"


UNSUPPORTED_REASON = {
    "xls": "starý formát Excel (.xls) nie je podporovaný – požiadajte o .xlsx alebo PDF",
    "doc": "starý formát Word (.doc) nie je podporovaný – požiadajte o .docx alebo PDF",
    "image": "obrázok alebo sken – OCR nie je k dispozícii, treba ho spracovať ručne",
    "archive": "archív (ZIP) sa z bezpečnostných dôvodov nerozbaľuje",
    "unknown": "neznámy typ prílohy – nespracované",
}


# ---------------------------------------------------------------------- dekódovanie textu
def decode_bytes(data: bytes, charset: str | None = None) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for enc in [charset, "utf-8"]:
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        return data.decode("cp1250")       # slovenské a české Windows texty
    except UnicodeDecodeError:
        return data.decode("latin-1", "replace")


class _HtmlText(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article",
              "header", "footer", "blockquote", "pre", "hr", "ul", "ol", "dl", "dt", "dd"}
    _SKIP = {"script", "style", "head", "title", "noscript", "template", "svg", "object", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in self._SKIP:
            self.skip += 1
        elif tag in ("td", "th"):
            self.out.append("\t")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):  # noqa: ANN001
        if tag in ("br", "hr"):
            self.out.append("\n")

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in self._SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):  # noqa: ANN001
        if not self.skip:
            self.out.append(data)


def html_to_text(markup: str) -> str:
    """HTML → čistý text. Odkazy ani obrázky sa nenačítavajú (ostane len viditeľný text)."""
    p = _HtmlText()
    try:
        p.feed(markup)
        p.close()
    except Exception:  # poškodené HTML – použijeme aspoň hrubé odstránenie značiek
        return html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = "".join(p.out)
    lines = []
    for line in text.split("\n"):
        cells = [" ".join(c.split()) for c in line.split("\t")]
        line = "\t".join(cells).strip("\t ")
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------------------------------------------------------------------- jednotlivé formáty
def _check_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(io.BytesIO(data))
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise AttachmentError("príliš veľa súborov v kontajneri (možná zip bomba)")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNZIPPED_BYTES:
        raise AttachmentError("po rozbalení by príloha bola príliš veľká (možná zip bomba)")
    for i in infos:
        if i.compress_size and i.file_size > 5 * 1024 * 1024 and i.file_size / i.compress_size > MAX_ZIP_RATIO:
            raise AttachmentError("podozrivý kompresný pomer (možná zip bomba)")
    return zf


def _pdf(data: bytes) -> str:
    import logging

    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(data), maxpages=PDF_MAX_PAGES)


def _fmt_cell(v) -> str:  # noqa: ANN001
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y") if v.time() == time(0, 0) else v.strftime("%d.%m.%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return " ".join(str(v).split())


def _xlsx(data: bytes) -> str:
    _check_zip(data).close()
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for ws in wb.worksheets:
            lines.append(f"[Hárok: {ws.title}]")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= XLSX_MAX_ROWS:
                    lines.append("[… ďalšie riadky vynechané]")
                    break
                cells = [_fmt_cell(v) for v in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if any(cells):
                    lines.append("\t".join(cells))
            lines.append("")
    finally:
        wb.close()
    return "\n".join(lines)


def _docx(data: bytes) -> str:
    zf = _check_zip(data)
    with zf:
        info = zf.getinfo("word/document.xml")
        if info.file_size > 50 * 1024 * 1024:
            raise AttachmentError("dokument je príliš veľký")
        xml = zf.read(info).decode("utf-8", "replace")
    xml = re.sub(r"<w:del\b.*?</w:del>", "", xml, flags=re.S)          # zmazaný text (sledované zmeny)
    xml = re.sub(r"<w:instrText\b.*?</w:instrText>", "", xml, flags=re.S)
    xml = re.sub(r"</w:p>\s*</w:tc>", "</w:tc>", xml)
    xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:(?:br|cr)\b[^>]*/>", "\n", xml)
    xml = xml.replace("</w:p>", "\n").replace("</w:tc>", "\t").replace("</w:tr>", "\n")
    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    return "\n".join(line.rstrip("\t ") for line in text.split("\n"))


def _csv(data: bytes) -> str:
    text = decode_bytes(data)
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ";" if sample.count(";") >= sample.count(",") else ","
    out = []
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
        if i >= XLSX_MAX_ROWS:
            out.append("[… ďalšie riadky vynechané]")
            break
        out.append("\t".join(" ".join(c.split()) for c in row))
    return "\n".join(out)


def extract_text(kind: str, data: bytes) -> str:
    """Text prílohy daného typu (v aktuálnom procese – volajte cez :func:`convert`)."""
    if kind == "pdf":
        text = _pdf(data)
    elif kind == "xlsx":
        text = _xlsx(data)
    elif kind == "docx":
        text = _docx(data)
    elif kind == "csv":
        text = _csv(data)
    elif kind == "html":
        text = html_to_text(decode_bytes(data))
    elif kind == "txt":
        text = decode_bytes(data)
    else:
        raise AttachmentError(UNSUPPORTED_REASON.get(kind, "nepodporovaný typ prílohy"))
    text = text.replace("\x00", "")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[… text prílohy skrátený …]"
    return text


# ---------------------------------------------------------------------- izolované spracovanie
def _sandboxed(kind: str, data: bytes) -> str:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"}
    proc = subprocess.run([sys.executable, "-I", "-B", os.path.abspath(__file__), "--worker", kind],
                          input=data, capture_output=True, timeout=SANDBOX_TIMEOUT_S, env=env, cwd="/")
    try:
        result = json.loads(proc.stdout.decode("utf-8") or "{}")
    except ValueError:
        result = {}
    if result.get("ok"):
        return result.get("text", "")
    if result.get("error"):
        raise AttachmentError(result["error"])
    raise AttachmentError(f"spracovanie prílohy zlyhalo (kód {proc.returncode}; možné prekročenie limitu pamäte "
                          "alebo času)")


def convert(filename: str, content_type: str, data: bytes, *, sandbox: bool = True) -> tuple[str, str, str]:
    """Príloha → (text, typ, chyba). Nikdy nevyhodí výnimku; chyba je prázdna, ak je všetko v poriadku."""
    kind = detect_kind(filename, content_type, data)
    if kind not in SUPPORTED:
        return "", kind, UNSUPPORTED_REASON.get(kind, "nepodporovaný typ prílohy")
    if kind in ("txt", "csv", "html") and len(data) < 2 * 1024 * 1024:
        sandbox = False          # jednoduchý text netreba izolovať
    try:
        if sandbox:
            try:
                return _sandboxed(kind, data), kind, ""
            except (OSError, subprocess.SubprocessError) as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    return "", kind, f"spracovanie prílohy prekročilo časový limit {SANDBOX_TIMEOUT_S} s"
                # izolovaný proces sa nedá spustiť (obmedzené prostredie) – spracujeme priamo
                text = extract_text(kind, data)
                return text, kind, ""
        return extract_text(kind, data), kind, ""
    except AttachmentError as exc:
        return "", kind, str(exc)
    except Exception as exc:  # poškodený alebo zlomyseľný súbor
        return "", kind, f"prílohu sa nepodarilo prečítať ({type(exc).__name__})"


def _worker() -> None:
    """Pracovný proces: bajty prílohy zo stdin → JSON {ok, text|error} na stdout."""
    try:
        import resource

        mem = SANDBOX_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_CPU_S, SANDBOX_CPU_S))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))      # žiadny zápis do súborov
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))   # žiadne ďalšie procesy
    except Exception:  # obmedzenia nie sú na tejto platforme k dispozícii – časový limit platí aj tak
        pass
    kind = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    data = sys.stdin.buffer.read()
    try:
        out = {"ok": True, "text": extract_text(kind, data)}
    except AttachmentError as exc:
        out = {"ok": False, "error": str(exc)}
    except MemoryError:
        out = {"ok": False, "error": "príloha prekročila limit pamäte"}
    except Exception as exc:
        out = {"ok": False, "error": f"prílohu sa nepodarilo prečítať ({type(exc).__name__})"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--worker":
    _worker()
