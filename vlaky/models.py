"""Dátový model (sekcia 11 v PROMPT.md). Všetky časy sú v UTC (timezone-aware)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


def _ts(nullable: bool = True):
    return mapped_column(DateTime(timezone=True), nullable=nullable)


# ---------------------------------------------------------------- používatelia, audit, parametre
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    full_name: Mapped[str] = mapped_column(String(160), default="")
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="dispecer")  # dispecer|veduci|planovac|obchod|admin
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # zvýši sa pri odhlásení – všetky staršie relácie (cookies) používateľa prestanú platiť
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80))          # používateľ alebo "agent"
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(60), default="")
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class ParameterSet(Base):
    __tablename__ = "parameter_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- sieť a hranice
class BorderCrossing(Base):
    __tablename__ = "border_crossings"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)          # napr. KUTY-LANZHOT
    name: Mapped[str] = mapped_column(String(160))                      # „Kúty – Lanžhot“
    country: Mapped[str] = mapped_column(String(2))                     # CZ, PL, UA, HU, AT
    sk_station: Mapped[str] = mapped_column(String(120))
    foreign_station: Mapped[str] = mapped_column(String(120))
    handover_station: Mapped[str] = mapped_column(String(120))          # pohraničná prechodová stanica (PPS)
    handover_side: Mapped[str] = mapped_column(String(2), default="SK")
    gauge: Mapped[str] = mapped_column(String(20), default="1435")
    traction: Mapped[str] = mapped_column(String(200), default="")
    freight: Mapped[str] = mapped_column(String(20), default="")       # hlavny|pravidelny|obcasny|ziadny
    notes: Mapped[str] = mapped_column(Text, default="")
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dwell_p50_min: Mapped[float] = mapped_column(Float, default=60.0)
    dwell_p90_min: Mapped[float] = mapped_column(Float, default=120.0)
    contact: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(Text, default="")


class LocationInfo(Base):
    """Doplnkové údaje o dopravni, ktoré nie sú v OSM (spravuje dispečing)."""

    __tablename__ = "location_info"
    id: Mapped[int] = mapped_column(primary_key=True)
    station: Mapped[str] = mapped_column(String(120), unique=True)      # názov dopravne
    can_wait: Mapped[bool] = mapped_column(Boolean, default=True)
    can_stable: Mapped[bool] = mapped_column(Boolean, default=False)
    can_change_crew: Mapped[bool] = mapped_column(Boolean, default=False)
    max_train_length_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- e-maily a extrakcia
class Email(Base):
    __tablename__ = "emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(300), unique=True)   # idempotencia
    source: Mapped[str] = mapped_column(String(40), default="directory")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    sender: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    auth_results: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(String(20), default="ok")     # ok | karantena | podozrivy
    verdict_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="prijaty")  # prijaty|spracovany|chyba
    error: Mapped[str] = mapped_column(Text, default="")
    raw_path: Mapped[str] = mapped_column(String(500), default="")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="email", cascade="all, delete-orphan")


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    filename: Mapped[str] = mapped_column(String(300), default="")
    content_type: Mapped[str] = mapped_column(String(120), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[Email] = relationship(back_populates="attachments")


class ExtractedEvent(Base):
    """Udalosť vytiahnutá z e-mailu (výluka, trasa, avízo, objednávka…)."""

    __tablename__ = "extracted_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[Optional[int]] = mapped_column(ForeignKey("emails.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    # vyluka_nova|vyluka_zmena|vyluka_zrusenie|pomala_jazda|trasa_pridelena|trasa_zmena|trasa_zrusena|
    # mimoriadnost|avizo|objednavka|ine
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extractor: Mapped[str] = mapped_column(String(40), default="pravidla")
    status: Mapped[str] = mapped_column(String(20), default="navrh")   # navrh|potvrdene|zamietnute|aplikovane
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_by: Mapped[str] = mapped_column(String(80), default="")
    confirmed_at: Mapped[Optional[datetime]] = _ts()


# ---------------------------------------------------------------- výluky a obmedzenia
class Closure(Base):
    __tablename__ = "closures"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), default="uplna")
    # uplna|kolaj|trakcne_vedenie|pomala_jazda|obmedzenie|mimoriadnost
    from_station: Mapped[str] = mapped_column(String(120))
    to_station: Mapped[str] = mapped_column(String(120))
    line_ref: Mapped[str] = mapped_column(String(40), default="")
    track: Mapped[str] = mapped_column(String(40), default="")
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    daily_from: Mapped[str] = mapped_column(String(5), default="")      # opakovanie: "08:00" (miestny čas)
    daily_to: Mapped[str] = mapped_column(String(5), default="")
    electric_only: Mapped[bool] = mapped_column(Boolean, default=False)
    speed_kmh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="navrh")    # navrh|overene|zrusene
    source: Mapped[str] = mapped_column(String(40), default="manual")
    source_event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("extracted_events.id"), nullable=True)
    external_ref: Mapped[str] = mapped_column(String(120), default="")  # číslo rozkazu/výluky, ak je
    note: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- objednávky, vlaky, zdroje
class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(120))                    # stanica, vlečka alebo hraničný priechod
    destination: Mapped[str] = mapped_column(String(120))
    earliest_departure_utc: Mapped[Optional[datetime]] = _ts()
    deadline_utc: Mapped[Optional[datetime]] = _ts()
    deadline_type: Mapped[str] = mapped_column(String(20), default="pevny")  # pevny|pokuta|orientacny
    cargo: Mapped[str] = mapped_column(String(200), default="")
    rid: Mapped[bool] = mapped_column(Boolean, default=False)
    wagons: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    weight_t: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    length_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="nova")
    notes: Mapped[str] = mapped_column(Text, default="")
    source_event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("extracted_events.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Locomotive(Base):
    __tablename__ = "locomotives"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    type: Mapped[str] = mapped_column(String(80), default="")
    traction: Mapped[str] = mapped_column(String(1), default="E")      # E | M
    systems: Mapped[list] = mapped_column(JSON, default=list)          # napr. ["25kV50Hz","3kV","15kV16.7Hz"]
    owner: Mapped[str] = mapped_column(String(120), default="")
    telematics_id: Mapped[str] = mapped_column(String(120), default="")


class Driver(Base):
    __tablename__ = "drivers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    home_station: Mapped[str] = mapped_column(String(120), default="")
    route_knowledge: Mapped[list] = mapped_column(JSON, default=list)  # zoznam čísel tratí / úsekov
    languages: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DriverShift(Base):
    __tablename__ = "driver_shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    train_id: Mapped[Optional[int]] = mapped_column(ForeignKey("trains.id"), nullable=True)
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))       # nástup (prevzatie rušňa)
    planned_end_utc: Mapped[Optional[datetime]] = _ts()
    actual_end_utc: Mapped[Optional[datetime]] = _ts()
    status: Mapped[str] = mapped_column(String(20), default="planovana")       # planovana|prebieha|ukoncena
    driver: Mapped[Driver] = relationship()


class Train(Base):
    __tablename__ = "trains"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), index=True)
    run_date: Mapped[str] = mapped_column(String(10), default="")      # YYYY-MM-DD (deň odchodu)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    customer: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(120))                   # prvá dopravňa v SR (často PPS)
    destination: Mapped[str] = mapped_column(String(120))
    entry_border_id: Mapped[Optional[int]] = mapped_column(ForeignKey("border_crossings.id"), nullable=True)
    exit_border_id: Mapped[Optional[int]] = mapped_column(ForeignKey("border_crossings.id"), nullable=True)
    via: Mapped[list] = mapped_column(JSON, default=list)              # povinné body trasy (voliteľné)
    length_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    weight_t: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gauge: Mapped[str] = mapped_column(String(10), default="1435")
    traction: Mapped[str] = mapped_column(String(1), default="E")
    vmax_kmh: Mapped[float] = mapped_column(Float, default=100.0)
    rid: Mapped[bool] = mapped_column(Boolean, default=False)
    locomotive_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locomotives.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="planovany")
    # planovany|na_ceste_k_hranici|prevzaty|v_jazde|odstaveny|odovzdany|ukonceny|zruseny
    ready_utc: Mapped[Optional[datetime]] = _ts()                      # kedy bude (je) pripravený
    planned_departure_utc: Mapped[Optional[datetime]] = _ts()
    deadline_utc: Mapped[Optional[datetime]] = _ts()
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    locomotive: Mapped[Optional[Locomotive]] = relationship()
    entry_border: Mapped[Optional[BorderCrossing]] = relationship(foreign_keys=[entry_border_id])
    exit_border: Mapped[Optional[BorderCrossing]] = relationship(foreign_keys=[exit_border_id])


class Path(Base):
    """Trasa vlaku (pridelená ŽSR, žiadaná alebo navrhnutá agentom)."""

    __tablename__ = "paths"
    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[Optional[int]] = mapped_column(ForeignKey("trains.id"), nullable=True)
    train_number: Mapped[str] = mapped_column(String(40), default="")
    kind: Mapped[str] = mapped_column(String(20), default="ad_hoc")    # gvd|ad_hoc|navrh
    status: Mapped[str] = mapped_column(String(20), default="pridelena")  # navrh|ziadana|pridelena|zmenena|zrusena
    departure_utc: Mapped[Optional[datetime]] = _ts()
    points: Mapped[list] = mapped_column(JSON, default=list)          # [{station, arr, dep}] (ISO UTC)
    source_event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("extracted_events.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Handover(Base):
    """Preberanie (alebo odovzdanie) vlaku na hraničnom priechode."""

    __tablename__ = "handovers"
    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id"))
    border_id: Mapped[Optional[int]] = mapped_column(ForeignKey("border_crossings.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String(10), default="vstup")  # vstup|vystup
    avizo_eta_utc: Mapped[Optional[datetime]] = _ts()
    arrived_utc: Mapped[Optional[datetime]] = _ts()
    taken_over_utc: Mapped[Optional[datetime]] = _ts()
    findings: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ocakavany")  # ocakavany|prisiel|prevzaty|odovzdany
    source_event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("extracted_events.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    border: Mapped[Optional[BorderCrossing]] = relationship()


class PositionReport(Base):
    __tablename__ = "position_reports"
    __table_args__ = (Index("ix_position_reports_train_ts", "train_id", "ts_utc"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[Optional[int]] = mapped_column(ForeignKey("trains.id"), nullable=True, index=True)
    train_number: Mapped[str] = mapped_column(String(40), default="")
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    speed_kmh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="gps")    # gps|zsr|tis|manual|driver|sim
    accuracy_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    matched_edge: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    snapped_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    snapped_lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    nearest_station: Mapped[str] = mapped_column(String(120), default="")
    off_track_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    received_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- odporúčania a notifikácie
class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    decision: Mapped[str] = mapped_column(String(20))                 # VYPRAVIŤ|POČKAŤ|ODSTAVIŤ|PRESMEROVAŤ|ESKALOVAŤ
    color: Mapped[str] = mapped_column(String(10))                    # ZELENÁ|ORANŽOVÁ|ČERVENÁ
    priority: Mapped[str] = mapped_column(String(12), default="INFO")  # KRITICKÉ|DÔLEŽITÉ|INFO
    summary: Mapped[str] = mapped_column(Text, default="")
    chosen: Mapped[dict] = mapped_column(JSON, default=dict)
    variants: Mapped[list] = mapped_column(JSON, default=list)
    windows: Mapped[dict] = mapped_column(JSON, default=dict)
    path_proposal: Mapped[dict] = mapped_column(JSON, default=dict)   # návrh trasy (stanice, časy)
    crew: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    trigger: Mapped[str] = mapped_column(Text, default="")            # čo vyvolalo prepočet
    data_age: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)
    state: Mapped[str] = mapped_column(String(20), default="nove")    # nove|prijate|upravene|zamietnute|zastarane
    dispatcher_note: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(80), default="")
    decided_at: Mapped[Optional[datetime]] = _ts()
    train: Mapped[Train] = relationship()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    recommendation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recommendations.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default="odporucanie")
    # odporucanie|upozornenie|suhrn|zdravie|odovzdavka|test
    channel: Mapped[str] = mapped_column(String(10))                  # email|teams|sms|app
    recipient: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(12), default="INFO")
    dedup_key: Mapped[str] = mapped_column(String(120), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="nova")   # nova|odoslana|chyba|zablokovana|potlacena
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[Optional[datetime]] = _ts()
    read_at: Mapped[Optional[datetime]] = _ts()


class AgentHealth(Base):
    __tablename__ = "agent_health"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    component: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10))                   # ok|warn|error
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class ShiftHandoverReport(Base):
    __tablename__ = "shift_handover_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed_by: Mapped[str] = mapped_column(String(80), default="")
    confirmed_at: Mapped[Optional[datetime]] = _ts()
    note: Mapped[str] = mapped_column(Text, default="")


class KeyValue(Base):
    """Drobný stav agenta (napr. posledné spracované UID schránky)."""

    __tablename__ = "kv"
    __table_args__ = (UniqueConstraint("key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[Any] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
