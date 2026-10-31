"""Rozbor e-mailov (RFC 822/MIME) a prevod príloh na text."""
from __future__ import annotations

import io
import subprocess
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

import pytest

from vlaky.posta import prilohy
from vlaky.posta.parser import parse_message

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "ukazkove_emaily"
ALLOWED = ("zsr.example", "partner.example", "zakaznik.example", "dopravca.example")


def sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def _mail(body: str = "Text", attachments: tuple = (), msg_id: str | None = "<t1@zsr.example>") -> bytes:
    m = EmailMessage(policy=SMTP)
    m["From"] = "Test <test@zsr.example>"
    m["To"] = "agent.dispecing@dopravca.example"
    m["Subject"] = "FIKTÍVNE – ukážka: test"
    m["Date"] = "Mon, 02 Nov 2026 07:00:00 +0100"
    if msg_id:
        m["Message-ID"] = msg_id
    m.set_content(body)
    for data, maintype, subtype, name in attachments:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


def test_all_samples_parse_and_are_fictional(cfg):
    files = sorted(SAMPLES.glob("*.eml"))
    assert 16 <= len(files) <= 20
    ids = set()
    for f in files:
        pm = parse_message(f.read_bytes())
        assert pm.message_id and pm.message_id not in ids
        ids.add(pm.message_id)
        assert "FIKTÍVNE – ukážka" in pm.subject + "\n" + pm.body_text, f.name
        domain = pm.sender.split("@")[1]
        assert domain in ALLOWED or domain == "falosne-zsr.example", f.name
        assert not pm.errors, (f.name, pm.errors)
    assert (SAMPLES / "README.md").exists()


def test_headers_are_decoded(cfg):
    pm = parse_message(sample("01_rov_vyluka_kolaje_poprad_lm.eml"))
    assert pm.message_id == "<ukazka-01.fiktivne@zsr.example>"
    assert pm.sender == "vyluky.orke@zsr.example"
    assert pm.sender_name == "ŽSR OR Košice – výluky (FIKTÍVNE)"
    assert "Rozkaz o výluke (ROV) č. 1142/2026" in pm.subject
    assert pm.date == datetime(2026, 11, 2, 6, 12, tzinfo=timezone.utc)
    assert len(pm.auth_results) == 1 and "dmarc=pass" in pm.auth_results[0]
    assert "Úsek: Poprad-Tatry – Liptovský Mikuláš" in pm.body_text


def test_alternative_prefers_plain_text(cfg):
    pm = parse_message(sample("06_mimoriadna_udalost.eml"))
    assert "pretrhnutý trolejový drôt" in pm.body_text       # je len v text/plain
    assert "<p>" not in pm.body_text


def test_html_only_body_is_converted_and_links_are_not_followed(cfg):
    pm = parse_message(sample("11_avizo_rusovce_rajka.eml"))
    assert "Predpokladaný príchod na hranicu:" in pm.body_text
    assert "04.11.2026 06:20" in pm.body_text
    assert "Sledovanie vlaku" in pm.body_text
    assert "https://" not in pm.body_text and "<" not in pm.body_text


def test_forwarded_message_is_part_of_text(cfg):
    pm = parse_message(sample("04_zmena_vyluky_preposlana.eml"))
    assert "Preposlaná správa" in pm.body_text
    assert "Nový termín: od 12.11.2026 09:30 do 12.11.2026 16:00" in pm.body_text
    assert pm.attachments == []


def test_xlsx_attachment_becomes_tab_separated_text(cfg):
    pm = parse_message(sample("12_srov_tabulka_xlsx.eml"))
    (att,) = pm.attachments
    assert att.filename == "SROV_48_2026.xlsx" and att.kind == "xlsx" and not att.error
    assert "[Hárok: SROV 48-2026]" in att.text
    assert "Trnava – Leopoldov\t1. TK\tvýluka koľaje\t23.11.2026 08:00\t23.11.2026 15:00" in att.text


def test_pdf_attachment_becomes_text(cfg):
    pm = parse_message(sample("13_objednavka_pdf_cz.eml"))
    (att,) = pm.attachments
    assert att.kind == "pdf" and not att.error
    assert "Odesilaci stanice: Kuty" in att.text
    assert "Termin dodani: 21.11.2026 14:00" in att.text


def _docx(paragraphs: list[str], table: list[list[str]]) -> bytes:
    def run(t: str) -> str:
        return f"<w:r><w:t>{t}</w:t></w:r>"

    body = "".join(f"<w:p>{run(p)}</w:p>" for p in paragraphs)
    body += "<w:p><w:del><w:r><w:delText>ZMAZANÝ TEXT</w:delText></w:r></w:del></w:p>"
    rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p>{run(c)}</w:p></w:tc>" for c in r) + "</w:tr>" for r in table)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           f'wordprocessingml/2006/main"><w:body>{body}<w:tbl>{rows}</w:tbl></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_docx_and_csv_attachments(cfg):
    docx = _docx(["Oznámenie o výluke – FIKTÍVNE", "Úsek Kraľovany – Ružomberok"],
                 [["Začiatok", "Koniec"], ["12.11.2026 08:00", "12.11.2026 14:00"]])
    csv_bytes = "Úsek;Začiatok;Koniec\nŽilina – Vrútky;23.11.2026 08:00;23.11.2026 15:00\n".encode("cp1250")
    raw = _mail(attachments=((docx, "application", "vnd.openxmlformats-officedocument.wordprocessingml.document",
                              "oznamenie.docx"), (csv_bytes, "text", "csv", "vyluky.csv")))
    pm = parse_message(raw)
    by_name = {a.filename: a for a in pm.attachments}
    d = by_name["oznamenie.docx"]
    assert d.kind == "docx" and "Úsek Kraľovany – Ružomberok" in d.text
    assert "12.11.2026 08:00\t12.11.2026 14:00" in d.text
    assert "ZMAZANÝ TEXT" not in d.text
    c = by_name["vyluky.csv"]
    assert c.kind == "csv" and "Žilina – Vrútky\t23.11.2026 08:00\t23.11.2026 15:00" in c.text


def test_attachment_over_limit_is_not_processed(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "max_attachment_mb", 0)
    pm = parse_message(sample("13_objednavka_pdf_cz.eml"))
    (att,) = pm.attachments
    assert att.text == "" and "limit" in att.error


def test_quarantine_mode_only_registers_attachments(cfg):
    pm = parse_message(sample("12_srov_tabulka_xlsx.eml"), extract_attachments=False)
    (att,) = pm.attachments
    assert att.text == "" and "karanténe" in att.error and att.size > 1000


def test_unsupported_attachments_are_reported(cfg):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    raw = _mail(attachments=((png, "image", "png", "sken.png"), (b"\xd0\xcf\x11\xe0" + b"\x00" * 50, "application",
                                                                 "vnd.ms-excel", "stary.xls")))
    pm = parse_message(raw)
    errs = {a.filename: a.error for a in pm.attachments}
    assert "OCR" in errs["sken.png"]
    assert ".xls" in errs["stary.xls"]


def test_missing_message_id_uses_hash_and_garbage_does_not_crash(cfg):
    raw = _mail(msg_id=None)
    a, b = parse_message(raw), parse_message(raw)
    assert a.message_id.startswith("sha256:") and a.message_id == b.message_id
    for junk in (b"", b"\x00\xff\xfe garbage \x00", b"From: <<<>>>\n\n\xff" * 50, b"Content-Type: multipart/mixed\n\n--"):
        pm = parse_message(junk)
        assert pm.message_id


def test_zip_bomb_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", b"\x00" * (8 * 1024 * 1024))
    data = buf.getvalue()
    assert prilohy.detect_kind("x.xlsx", "", data) == "xlsx"
    with pytest.raises(prilohy.AttachmentError):
        prilohy.extract_text("xlsx", data)
    text, kind, err = prilohy.convert("x.xlsx", "", data, sandbox=False)
    assert text == "" and "zip bomba" in err


def test_sandbox_falls_back_when_subprocess_cannot_start(monkeypatch):
    pdf = parse_message(sample("13_objednavka_pdf_cz.eml"), extract_attachments=False)  # len overenie vzorky
    assert pdf.attachments

    def boom(*a, **k):
        raise OSError("fork zakázaný")

    monkeypatch.setattr(subprocess, "run", boom)
    data = (SAMPLES / "13_objednavka_pdf_cz.eml").read_bytes()
    from email import message_from_bytes, policy

    msg = message_from_bytes(data, policy=policy.default)
    att = next(msg.iter_attachments())
    text, kind, err = prilohy.convert("objednavka.pdf", "application/pdf", att.get_content(), sandbox=True)
    assert kind == "pdf" and not err and "Pocet vozu: 22" in text


def test_html_to_text_skips_scripts_and_keeps_table_cells():
    html = ("<html><head><title>x</title><script>alert('x')</script></head><body><p>Riadok&nbsp;1</p>"
            "<table><tr><td>A</td><td>B</td></tr></table><img src='https://example.invalid/a.png'></body></html>")
    out = prilohy.html_to_text(html)
    assert "alert" not in out and "Riadok 1" in out and "A\tB" in out and "example.invalid" not in out
