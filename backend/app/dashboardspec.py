"""Phase 18 (SPEC-01, SPEC-03): the chart-spec engine for dashboards.

A dashboard version is a declarative chart spec that fixed, app-owned renderer code draws.
Agent strings are plain text and are never parsed as markup; the browser controls (hash-only CSP,
Trusted Types) are the security boundary, this validator only makes errors explicit.

Pure module: no database, no network, no LLM, no clock. It owns the authoring models, `parse`,
the generated JSON Schema, measure compilation into frozen ordered SQL (through
`modelcompiler`), column binding, the SQL-free render spec, tile diff and summary.
"""
import copy
import json
import re
from datetime import date
from typing import Annotated, ClassVar, Literal, Union

import sqlglot
from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Discriminator, Field, Tag, ValidationError,
    model_validator,
)
from sqlglot import exp

from . import dashboards, modelcompiler
from .config import DASHBOARD_LOCALES as LOCALES
from .guardrails import SQLGuardrailError, validate_readonly_sql

SPEC_VERSION = 1
MAX_SPEC_BYTES = 65536
MAX_TILES = 24
MAX_CHARTS = 6
MAX_ROWS = 12
MAX_PER_ROW = 4
MEASURE_QID_PREFIX = "m_"
FORMATS = ("number", "integer", "decimal1", "decimal2", "percent", "percent_value",
           "currency", "compact")
TILE_TYPES = ("stat", "line", "area", "bar", "stacked_bar", "sparkline", "table", "text")
CHART_TYPES = ("line", "area", "bar", "stacked_bar")
GRAIN_DAYS = {"day": 1, "week": 7, "month": 31, "quarter": 92, "year": 366}
DEFAULT_WINDOW_DAYS = {"day": 90, "week": 182, "month": 400, "quarter": 730, "year": 1825}
KPI_COLUMNS = ("day", "value", "min", "max", "n", "metric")
FORECAST_COLUMNS = ("period", "actual", "forecast", "low", "high", "method", "wape", "as_of")
RENDER_VERSION = 1

_RULES = {"spec", "layout", "query", "size", "binding", "charts", "not_modeled", "measure",
          "columns", "queries", "sql"}
_HTML_MSG = "spec must be a JSON object (a chart spec); HTML dashboards are no longer accepted"
_QID = r"^[a-z][a-z0-9_]{0,47}$"
_COL = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
_DIM = r'^[^\s.;"\'()]{1,63}(\.[^\s.;"\'()]{1,63}){1,2}$'
_DATE = r"^\d{4}-\d{2}-\d{2}$"
_TAGS = {"q_sql", "q_kpi", "q_forecast", "w_days", "w_periods", "w_fixed"}


# ---------------------------------------------------------------- plain text types

def _plain(value: str, newline: bool) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    for ch in value:
        o = ord(ch)
        if ch == "\n" and newline:
            continue
        if o < 32 or 0x7F <= o <= 0x9F or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069:
            raise ValueError(f"control or bidirectional character U+{o:04X} is not allowed in text")
    return value


def _line_ok(v: str) -> str:
    return _plain(v, False)


def _note_ok(v: str) -> str:
    return _plain(v, True)


Text24 = Annotated[str, Field(min_length=1, max_length=24), AfterValidator(_line_ok)]
Text60 = Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_line_ok)]
Text120 = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_line_ok)]
Text200 = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_line_ok)]
Text300 = Annotated[str, Field(min_length=1, max_length=300), AfterValidator(_line_ok)]
NoteText = Annotated[str, Field(min_length=1, max_length=300), AfterValidator(_note_ok)]
BodyText = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_note_ok)]
TileId = Annotated[str, Field(pattern=_QID)]
QueryId = Annotated[str, Field(pattern=_QID)]
Col = Annotated[str, Field(pattern=_COL)]
Dim = Annotated[str, Field(pattern=_DIM)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Refresh = Annotated[int, Field(ge=dashboards.REFRESH_MIN, le=dashboards.REFRESH_MAX)]
FormatName = Literal["number", "integer", "decimal1", "decimal2", "percent", "percent_value",
                     "currency", "compact"]
GrainName = Literal["day", "week", "month", "quarter", "year"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ---------------------------------------------------------------- declared queries

class SqlQuery(_Base):
    id: QueryId
    sql: Annotated[str, Field(min_length=1, max_length=20000)]
    refresh_minutes: Refresh = dashboards.REFRESH_DEFAULT


class KpiQuery(_Base):
    id: QueryId
    source: Literal["kpi_history"]
    watch_id: Annotated[int, Field(ge=1)]
    days: Annotated[int, Field(ge=1, le=400)] = 30
    refresh_minutes: Refresh = dashboards.REFRESH_DEFAULT


class ForecastQuery(_Base):
    id: QueryId
    source: Literal["forecast"]
    forecast_id: Annotated[int, Field(ge=1)]
    refresh_minutes: Refresh = dashboards.REFRESH_DEFAULT


def _query_kind(v):
    if isinstance(v, dict):
        s = v.get("source")
        return {None: "q_sql", "kpi_history": "q_kpi", "forecast": "q_forecast"}.get(s)
    if isinstance(v, SqlQuery):
        return "q_sql"
    if isinstance(v, KpiQuery):
        return "q_kpi"
    if isinstance(v, ForecastQuery):
        return "q_forecast"
    return None


QueryDecl = Annotated[
    Union[Annotated[SqlQuery, Tag("q_sql")], Annotated[KpiQuery, Tag("q_kpi")],
          Annotated[ForecastQuery, Tag("q_forecast")]],
    Discriminator(_query_kind, custom_error_type="query",
                  custom_error_message="a query is {id, sql, refresh_minutes?}, a kpi_history "
                                       "query or a forecast query (source 'measure' is reserved)"),
]


# ---------------------------------------------------------------- measure binding

class WindowLastDays(_Base):
    last_days: Annotated[int, Field(ge=1, le=modelcompiler.MAX_WINDOW_DAYS)]


class WindowLastPeriods(_Base):
    last_periods: Annotated[int, Field(ge=1, le=60)]


class WindowFixed(_Base):
    start: Annotated[str, Field(pattern=_DATE)]
    end: Annotated[str, Field(pattern=_DATE, description="exclusive")]

    @model_validator(mode="after")
    def _ordered(self):
        try:
            s, e = date.fromisoformat(self.start), date.fromisoformat(self.end)
        except ValueError:
            raise ValueError("binding: start and end must be real YYYY-MM-DD dates") from None
        if s >= e:
            raise ValueError("binding: start must be before end (end is exclusive)")
        return self


def _window_kind(v):
    if isinstance(v, dict):
        if "last_days" in v:
            return "w_days"
        if "last_periods" in v:
            return "w_periods"
        if "start" in v or "end" in v:
            return "w_fixed"
        return None
    return {WindowLastDays: "w_days", WindowLastPeriods: "w_periods",
            WindowFixed: "w_fixed"}.get(type(v))


Window = Annotated[
    Union[Annotated[WindowLastDays, Tag("w_days")], Annotated[WindowLastPeriods, Tag("w_periods")],
          Annotated[WindowFixed, Tag("w_fixed")]],
    Discriminator(_window_kind, custom_error_type="binding",
                  custom_error_message="window is {last_days}, {last_periods} or {start, end}; "
                                       "range tokens such as this_month are not accepted"),
]

FilterValue = Union[Annotated[str, Field(max_length=200)], int, float, bool]


class MeasureFilter(_Base):
    dimension: Dim
    op: Literal["=", "!=", "in", "not_in", ">", ">=", "<", "<=", "is_null", "is_not_null"]
    values: Annotated[list[FilterValue], Field(max_length=50)] = []


class MeasureBinding(_Base):
    name: Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]
    by: Annotated[list[Dim], Field(max_length=2)] = []
    grain: GrainName | None = None
    filters: Annotated[list[MeasureFilter], Field(max_length=10)] = []
    window: Window | None = None
    top: Annotated[int, Field(ge=1, le=50)] | None = None
    refresh_minutes: Refresh = dashboards.REFRESH_DEFAULT

    @model_validator(mode="after")
    def _rules(self):
        if isinstance(self.window, WindowLastPeriods) and self.grain is None:
            raise ValueError("binding: window last_periods needs a grain")
        if self.top is not None and self.grain is not None:
            raise ValueError("binding: top only applies without a grain")
        return self


# ---------------------------------------------------------------- tiles

class SeriesRef(_Base):
    column: Col
    label: Text60 | None = None
    dashed: bool = False


class Band(_Base):
    low: Col
    high: Col
    label: Text60 | None = None


class CompareColumn(_Base):
    column: Col


class OwedWhen(_Base):
    op: Literal[">", ">=", "<", "<=", "=", "!="]
    value: Union[int, float]


class TableColumn(_Base):
    column: Col
    label: Text60 | None = None
    format: FormatName | None = None
    currency: Currency | None = None
    unit: Text24 | None = None


class _TileBase(_Base):
    id: TileId
    note: NoteText | None = None


class _Bound(_TileBase):
    title: Text120
    query: QueryId | None = None
    measure: MeasureBinding | None = None
    grain: GrainName | None = None
    format: FormatName = "number"
    currency: Currency | None = None
    unit: Text24 | None = None

    @model_validator(mode="after")
    def _binding(self):
        kind = getattr(self, "type", "")
        if (self.query is None) == (self.measure is None):
            raise ValueError("binding: set exactly one of query or measure")
        if self.format == "currency" and self.currency is None:
            raise ValueError("binding: format currency needs a currency code such as USD")
        if self.format != "currency" and self.currency is not None:
            raise ValueError("binding: currency is only allowed with format currency")
        if self.measure is not None:
            if self.grain is not None:
                raise ValueError("binding: a measure tile sets its grain inside measure")
            if self.measure.top is not None and kind not in ("bar", "stacked_bar", "table"):
                raise ValueError("binding: top only applies to bar, stacked_bar and table tiles")
        return self


class StatTile(_Bound):
    type: Literal["stat"]
    column: Col | None = None
    x: Col | None = None
    pick: Literal["last", "last_complete"] = "last"
    compare: Union[Literal["none", "previous_row"], CompareColumn] = "none"
    compare_label: Text60 | None = None
    good_direction: Literal["up", "down", "none"] = "none"
    sparkline: bool = False
    owed_when: OwedWhen | None = None

    @model_validator(mode="after")
    def _stat(self):
        grain = self.grain or (self.measure.grain if self.measure else None)
        if self.pick == "last_complete" and grain is None:
            raise ValueError("binding: pick last_complete needs a grain")
        if self.measure is not None and self.measure.by:
            raise ValueError("binding: a stat measure has no by dimensions")
        if self.query is not None:
            if self.column is None:
                raise ValueError("binding: a query-bound stat needs column")
            if self.sparkline and self.x is None:
                raise ValueError("binding: a sparkline needs x")
        return self


class _Chart(_Bound):
    x: Col | None = None
    y: Annotated[list[SeriesRef], Field(max_length=6)] = []
    series: Col | None = None
    x_label: Text60 | None = None
    y_label: Text60 | None = None

    @model_validator(mode="after")
    def _chart(self):
        if self.query is not None:
            if self.x is None or not self.y:
                raise ValueError("binding: a query-bound chart needs x and at least one y")
            if getattr(self, "type", "") == "stacked_bar" and self.series is None \
                    and len(self.y) < 2:
                raise ValueError("binding: stacked_bar needs series or at least two y")
        return self


class LineTile(_Chart):
    type: Literal["line"]
    band: Band | None = None


class AreaTile(_Chart):
    type: Literal["area"]
    band: Band | None = None


class BarTile(_Chart):
    type: Literal["bar"]
    horizontal: bool = False


class StackedBarTile(_Chart):
    type: Literal["stacked_bar"]
    horizontal: bool = False


class SparklineTile(_Bound):
    type: Literal["sparkline"]
    x: Col | None = None
    y: Annotated[list[SeriesRef], Field(max_length=1)] = []

    @model_validator(mode="after")
    def _spark(self):
        if self.query is not None and (self.x is None or not self.y):
            raise ValueError("binding: a query-bound sparkline needs x and y")
        return self


class TableTile(_Bound):
    type: Literal["table"]
    columns: Annotated[list[TableColumn], Field(max_length=12)] = []
    max_rows: Annotated[int, Field(ge=1, le=100)] = 25


class TextTile(_TileBase):
    type: Literal["text"]
    title: Text120 | None = None
    text: BodyText


Tile = Annotated[
    Union[StatTile, LineTile, AreaTile, BarTile, StackedBarTile, SparklineTile, TableTile,
          TextTile],
    Field(discriminator="type"),
]


class Layout(_Base):
    rows: Annotated[list[Annotated[list[TileId], Field(min_length=1, max_length=MAX_PER_ROW)]],
                    Field(min_length=1, max_length=MAX_ROWS)]


class Spec(_Base):
    """A chart spec. Every string is plain text and renders only as text. Declared query ids
    never start with m_ (reserved for compiled measures). There are no colour fields: the
    renderer owns the palette."""

    spec_version: Annotated[int, Field(ge=1, le=1)]
    title: Text200
    subtitle: Text300 | None = None
    queries: Annotated[list[QueryDecl], Field(max_length=dashboards.MAX_QUERIES)] = []
    layout: Layout
    tiles: Annotated[list[Tile], Field(min_length=1, max_length=MAX_TILES)]


# ---- Phase 22: spec v2 ----------------------------------------------------------------------

SPEC_VERSION_2 = 2
MAX_SPEC_BYTES_V2 = 131072
MAX_TILES_V2 = 40
MAX_CHARTS_V2 = 12
MAX_QUERIES_V2 = 30
MAX_SECTIONS = 10
MAX_SECTION_TILES = 16
MAX_FILTERS = 3
PRESETS = ("today", "yesterday", "last_7_days", "last_30_days", "last_90_days", "this_month",
           "last_month", "this_quarter", "this_fy")
WIDTHS = ("quarter", "third", "half", "two_thirds", "full")
TONES = ("inward", "outward", "neutral", "accent")
CHART_TYPES_V2 = ("line", "area", "bar", "stacked_bar", "sparkline", "combo", "paired_bar",
                  "aging", "bullet", "waterfall", "calendar", "donut", "ranked_bar", "funnel")
TILE_TYPES_V2 = ("stat", "table", "text") + CHART_TYPES_V2
DEFAULT_WIDTH = {t: "half" for t in TILE_TYPES_V2}
DEFAULT_WIDTH.update({"stat": "quarter", "sparkline": "quarter", "text": "full",
                      "table": "full", "calendar": "full"})
_TOP_TYPES = ("bar", "stacked_bar", "table", "ranked_bar", "donut", "funnel", "paired_bar")
_TARGET_TYPES = ("stat", "bullet", "line", "bar", "combo")
_FILTER_ID = r"^[a-z][a-z0-9_]{0,23}$"

Preset = Literal["today", "yesterday", "last_7_days", "last_30_days", "last_90_days",
                 "this_month", "last_month", "this_quarter", "this_fy"]
Width = Literal["quarter", "third", "half", "two_thirds", "full"]
Tone = Literal["inward", "outward", "neutral", "accent"]


class DateControl(_Base):
    default: Preset = Field("last_7_days", description="The range shown on first load.")
    presets: Annotated[list[Preset], Field(min_length=1, max_length=9)] | None = Field(
        None, description="Ranges the viewer may pick; null means all nine.")
    horizon_days: Annotated[int, Field(ge=31, le=732)] = Field(
        400, description="Days of history stored for controlled tiles.")
    fy_start_month: Annotated[int, Field(ge=1, le=12)] = Field(
        4, description="Month number the financial year starts in.")

    @model_validator(mode="after")
    def _distinct(self):
        if self.presets is not None and len(set(self.presets)) != len(self.presets):
            raise ValueError("binding: presets must be distinct")
        return self


class PageFilter(_Base):
    id: Annotated[str, Field(pattern=_FILTER_ID)]
    dimension: Dim
    label: Text60


class Section(_Base):
    title: Text60 | None = None
    description: Text200 | None = None
    tiles: Annotated[list[TileId], Field(min_length=1, json_schema_extra={
        "maxItems": MAX_SECTION_TILES})]


class Target(_Base):
    value: Union[int, float] | None = Field(None, description="A fixed target number.")
    column: Col | None = Field(None, description="Query column that holds the target.")
    label: Text60 | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.value is None) == (self.column is None):
            raise ValueError("binding: a target has exactly one of value or column")
        return self


class SeriesRefV2(_Base):
    column: Col
    label: Text60 | None = None
    dashed: bool = False
    tone: Tone | None = Field(None, description="Semantic colour role; the renderer owns hues.")
    as_: Literal["bar", "line"] | None = Field(
        None, alias="as", description="Draw this series as bars or a line (combo tiles only).")


class MeasureBindingV2(MeasureBinding):
    other: bool = Field(False, description="Fold the rest into one Other row (needs top).")


class MeasuresBinding(_Base):
    names: Annotated[list[Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]],
                     Field(min_length=2, max_length=6)] = Field(
        description="Two to six distinct measure names read side by side.")
    by: Annotated[list[Dim], Field(max_length=2)] = []
    grain: GrainName | None = None
    filters: Annotated[list[MeasureFilter], Field(max_length=10)] = []
    window: Window | None = None
    top: Annotated[int, Field(ge=1, le=50)] | None = None
    other: bool = Field(False, description="Fold the rest into one Other row (needs top and exactly one by; only for measures that add up).")
    refresh_minutes: Refresh = dashboards.REFRESH_DEFAULT

    @model_validator(mode="after")
    def _rules(self):
        if len(set(self.names)) != len(self.names):
            raise ValueError("binding: measure names must be distinct")
        if isinstance(self.window, WindowLastPeriods) and self.grain is None:
            raise ValueError("binding: window last_periods needs a grain")
        if self.top is not None and self.grain is not None:
            raise ValueError("binding: top only applies without a grain")
        return self


class DueRule(_Base):
    warn_days: Annotated[int, Field(ge=0, le=90)] = Field(
        description="Days before the date at which a row is flagged.")


class TableSort(_Base):
    column: Col
    dir: Literal["asc", "desc"] = "asc"


class TableColumnV2(_Base):
    column: Col
    label: Text60 | None = None
    format: FormatName | None = None
    currency: Currency | None = None
    unit: Text24 | None = None
    owed_when: OwedWhen | None = None
    delta: bool = False
    good_direction: Literal["up", "down", "none"] = "none"
    bar: bool = Field(False, description="Draw an in-cell bar (at most one column).")
    total: bool | None = None
    due: DueRule | None = None


class _TileBaseV2(_Base):
    id: TileId
    note: NoteText | None = None
    width: Width | None = Field(None, description="Tile width; defaults by tile type.")
    hidden: bool = Field(False, description="Hide the tile without deleting it.")


class _BoundV2(_TileBaseV2):
    title: Text120
    query: QueryId | None = None
    measure: MeasureBindingV2 | None = None
    measures: MeasuresBinding | None = None
    grain: GrainName | None = None
    format: FormatName = "number"
    currency: Currency | None = None
    unit: Text24 | None = None
    date: Literal["fixed", "control"] = Field(
        "fixed", description="control follows the page date control.")
    follow_filters: bool = Field(False, description="Follow the page filters (needs control).")
    target: Target | None = None

    @model_validator(mode="after")
    def _binding(self):
        kind = getattr(self, "type", "")
        bound = self.measure if self.measure is not None else self.measures
        if sum(x is not None for x in (self.query, self.measure, self.measures)) != 1:
            raise ValueError("binding: set exactly one of query, measure or measures")
        if self.format == "currency" and self.currency is None:
            raise ValueError("binding: format currency needs a currency code such as USD")
        if self.format != "currency" and self.currency is not None:
            raise ValueError("binding: currency is only allowed with format currency")
        if bound is not None:
            if self.grain is not None:
                raise ValueError("binding: a measure tile sets its grain inside measure")
            if bound.top is not None and kind not in _TOP_TYPES:
                raise ValueError(f"binding: top does not apply to a {kind} tile")
            if getattr(bound, "other", False) and (bound.top is None or len(bound.by) != 1):
                raise ValueError("binding: other needs top and exactly one by dimension")
        if self.follow_filters and self.date != "control":
            raise ValueError("binding: follow_filters needs date control")
        if self.date == "control":
            if bound is None:
                raise ValueError("binding: date control needs a measure or measures binding, "
                                 "not a query")
            else:
                if bound.window is not None:
                    raise ValueError("binding: a controlled tile takes no window")
                if bound.grain not in (None, "day"):
                    raise ValueError("binding: a controlled tile takes no grain or day only")
                if bound.top is not None:
                    raise ValueError("binding: top is not allowed under date control "
                                     "(use ranked_bar top)")
        if self.target is not None and kind not in _TARGET_TYPES:
            raise ValueError(f"binding: a {kind} tile takes no target")
        return self


class StatTileV2(_BoundV2):
    type: Literal["stat"]
    column: Col | None = None
    x: Col | None = None
    pick: Literal["last", "last_complete"] = "last"
    compare: Union[Literal["none", "previous_row"], CompareColumn] = "none"
    compare_label: Text60 | None = None
    comparison: Literal["previous_period", "last_year", "both", "none"] = Field(
        "previous_period", description="Comparison under the date control.")
    good_direction: Literal["up", "down", "none"] = "none"
    sparkline: bool = False
    owed_when: OwedWhen | None = None

    @model_validator(mode="after")
    def _stat(self):
        b = self.measure if self.measure is not None else self.measures
        grain = self.grain or (b.grain if b is not None else None)
        if self.pick == "last_complete" and grain is None:
            raise ValueError("binding: pick last_complete needs a grain")
        if b is not None and b.by:
            raise ValueError("binding: a stat measure has no by dimensions")
        if self.query is not None:
            if self.column is None:
                raise ValueError("binding: a query-bound stat needs column")
            if self.sparkline and self.x is None:
                raise ValueError("binding: a sparkline needs x")
        return self


class _ChartV2(_BoundV2):
    Y_RANGE: ClassVar[tuple] = (1, 6)
    x: Col | None = None
    y: Annotated[list[SeriesRefV2], Field(max_length=6)] = []
    series: Col | None = None
    x_label: Text60 | None = None
    y_label: Text60 | None = None

    @model_validator(mode="after")
    def _chart(self):
        kind = getattr(self, "type", "")
        lo, hi = self.Y_RANGE
        if self.query is not None and (self.x is None or not self.y):
            raise ValueError("binding: a query-bound chart needs x and at least one y")
        if (self.query is not None or self.y) and not lo <= len(self.y) <= hi:
            want = str(lo) if lo == hi else f"{lo} to {hi}"
            raise ValueError(f"binding: a {kind} tile needs {want} y series, got {len(self.y)}")
        if kind != "combo" and any(y.as_ is not None for y in self.y):
            raise ValueError("binding: as is only allowed on combo tiles")
        if kind == "stacked_bar" and self.query is not None and self.series is None \
                and len(self.y) < 2:
            raise ValueError("binding: stacked_bar needs series or at least two y")
        return self


class _GhostChart(_ChartV2):
    ghost: Literal["none", "previous_period", "last_year"] = Field(
        "none", description="Grey copy of the previous period or last year.")

    @model_validator(mode="after")
    def _ghost(self):
        if self.ghost != "none" and self.date != "control":
            raise ValueError("binding: ghost needs date control")
        return self


class LineTileV2(_GhostChart):
    type: Literal["line"]
    band: Band | None = None


class AreaTileV2(_GhostChart):
    type: Literal["area"]
    band: Band | None = None


class BarTileV2(_GhostChart):
    type: Literal["bar"]
    horizontal: bool = False


class StackedBarTileV2(_ChartV2):
    type: Literal["stacked_bar"]
    horizontal: bool = False
    percent: bool = Field(False, description="Draw 100 percent stacked bars.")


class ComboTile(_ChartV2):
    type: Literal["combo"]
    Y_RANGE: ClassVar[tuple] = (2, 4)
    y: Annotated[list[SeriesRefV2], Field(max_length=4)] = []

    @model_validator(mode="after")
    def _combo(self):
        if self.query is not None or self.y:
            kinds = {y.as_ for y in self.y}
            if None in kinds or "bar" not in kinds or "line" not in kinds:
                raise ValueError("binding: a combo tile needs every y as bar or line, "
                                 "with at least one of each")
        return self


class PairedBarTile(_ChartV2):
    type: Literal["paired_bar"]
    Y_RANGE: ClassVar[tuple] = (2, 2)
    y: Annotated[list[SeriesRefV2], Field(max_length=2)] = []
    mirror: bool = Field(False, description="Draw the second series mirrored below zero.")


class _OneY(_ChartV2):
    Y_RANGE: ClassVar[tuple] = (1, 1)
    y: Annotated[list[SeriesRefV2], Field(max_length=1)] = []


class AgingTile(_OneY):
    type: Literal["aging"]


class WaterfallTile(_OneY):
    type: Literal["waterfall"]
    ends: Literal["first_last", "none"] = "first_last"


class CalendarTile(_OneY):
    type: Literal["calendar"]


class DonutTile(_OneY):
    type: Literal["donut"]


class RankedBarTile(_OneY):
    type: Literal["ranked_bar"]
    top: Annotated[int, Field(ge=1, le=50)] | None = None
    other: bool = False
    cumulative: bool = False


class FunnelTile(_OneY):
    type: Literal["funnel"]


class BulletTile(_BoundV2):
    type: Literal["bullet"]
    column: Col | None = Field(None, description="Query column holding the actual value.")
    bands: Annotated[list[Union[int, float]], Field(min_length=1, max_length=3)] | None = None

    @model_validator(mode="after")
    def _bullet(self):
        if self.target is None:
            raise ValueError("binding: a bullet tile needs a target")
        if self.query is not None and self.column is None:
            raise ValueError("binding: a query-bound bullet needs column")
        if self.bands is not None and any(a >= b for a, b in zip(self.bands, self.bands[1:])):
            raise ValueError("binding: bands must be ascending")
        return self


class SparklineTileV2(_BoundV2):
    type: Literal["sparkline"]
    x: Col | None = None
    y: Annotated[list[SeriesRefV2], Field(max_length=1)] = []

    @model_validator(mode="after")
    def _spark(self):
        if self.query is not None and (self.x is None or not self.y):
            raise ValueError("binding: a query-bound sparkline needs x and y")
        if any(y.as_ is not None for y in self.y):
            raise ValueError("binding: as is only allowed on combo tiles")
        return self


class TableTileV2(_BoundV2):
    type: Literal["table"]
    columns: Annotated[list[TableColumnV2], Field(max_length=12)] = []
    max_rows: Annotated[int, Field(ge=1, le=100)] = 25
    sort: TableSort | None = None
    totals: bool = False

    @model_validator(mode="after")
    def _table(self):
        if sum(1 for c in self.columns if c.bar) > 1:
            raise ValueError("binding: at most one table column may set bar")
        return self


class TextTileV2(_TileBaseV2):
    type: Literal["text"]
    title: Text120 | None = None
    text: BodyText


TileV2 = Annotated[
    Union[StatTileV2, LineTileV2, AreaTileV2, BarTileV2, StackedBarTileV2, ComboTile,
          PairedBarTile, AgingTile, BulletTile, WaterfallTile, CalendarTile, DonutTile,
          RankedBarTile, FunnelTile, SparklineTileV2, TableTileV2, TextTileV2],
    Field(discriminator="type"),
]


class SpecV2(_Base):
    """A chart spec, version 2. Plain text only; tones are roles, never colours."""

    spec_version: Annotated[int, Field(ge=2, le=2)]
    title: Text200
    subtitle: Text300 | None = None
    number_locale: Literal["en-IN", "en-US", "en-GB", "en-AE", "en-SG", "en-AU", "en-CA"] \
        | None = Field(None, description="Number and date locale for the page.")
    date_control: DateControl | None = None
    filters: Annotated[list[PageFilter], Field(max_length=MAX_FILTERS)] = []
    queries: Annotated[list[QueryDecl], Field(max_length=MAX_QUERIES_V2)] = []
    sections: Annotated[list[Section], Field(min_length=1, json_schema_extra={
        "maxItems": MAX_SECTIONS})]
    tiles: Annotated[list[TileV2], Field(min_length=1, max_length=MAX_TILES_V2)]


# ---------------------------------------------------------------- errors, parse, schema

class SpecError(ValueError):
    def __init__(self, findings: list[dict]):
        self.findings = findings
        super().__init__("; ".join(f"{f['path']}: {f['message']} [{f['rule']}]" for f in findings)
                         or "invalid spec")


def _f(rule: str, path: str, message: str) -> dict:
    return {"rule": rule, "path": path, "message": message}


def _path(loc) -> str:
    out, prev = "", None
    loc = list(loc)
    for i, p in enumerate(loc):
        if isinstance(p, int):
            out += f"[{p}]"
        elif isinstance(p, str):
            if p in _TAGS:
                prev = p
                continue
            if isinstance(prev, int) and p in TILE_TYPES_V2 and i >= 2 and loc[i - 2] == "tiles":
                prev = p
                continue
            out += ("." if out else "") + p
        prev = p
    return out or "spec"


def findings_from(err: ValidationError) -> list[dict]:
    out = []
    for e in err.errors():
        msg = e.get("msg", "invalid")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        rule = "spec"
        head, sep, rest = msg.partition(": ")
        if sep and head in _RULES:
            rule, msg = head, rest
        elif e.get("type") in ("binding", "query"):
            rule = e["type"]
        out.append(_f(rule, _path(e.get("loc", ())), msg))
    return out


def _size(raw) -> int:
    if isinstance(raw, str):
        return len(raw.encode("utf-8"))
    return len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _semantic(obj: dict) -> list[dict]:
    """Cross-field rules over the raw object, so they are reported next to shape errors."""
    out: list[dict] = []
    tiles = [t for t in (obj.get("tiles") if isinstance(obj.get("tiles"), list) else [])]
    ids, seen = [], set()
    for i, t in enumerate(tiles):
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str):
            continue
        if tid in seen:
            out.append(_f("layout", f"tiles[{i}].id", f"duplicate tile id {tid}"))
        seen.add(tid)
        ids.append(tid)
    layout = obj.get("layout")
    rows = layout.get("rows") if isinstance(layout, dict) else None
    placed: dict[str, int] = {}
    if isinstance(rows, list):
        for r, row in enumerate(rows):
            for c, tid in enumerate(row if isinstance(row, list) else []):
                if not isinstance(tid, str):
                    continue
                if tid not in seen:
                    out.append(_f("layout", f"layout.rows[{r}][{c}]", f"unknown tile {tid}"))
                elif tid in placed:
                    out.append(_f("layout", f"layout.rows[{r}][{c}]",
                                  f"tile {tid} is placed twice"))
                placed[tid] = placed.get(tid, 0) + 1
        for tid in dict.fromkeys(ids):
            if tid not in placed:
                out.append(_f("layout", "layout.rows", f"tile {tid} is not placed in any row"))
    declared: set = set()
    queries = obj.get("queries") if isinstance(obj.get("queries"), list) else []
    for i, q in enumerate(queries):
        qid = q.get("id") if isinstance(q, dict) else None
        if not isinstance(qid, str):
            continue
        if qid.startswith(MEASURE_QID_PREFIX):
            out.append(_f("query", f"queries[{i}].id",
                          f"query id {qid} may not start with m_ (reserved for measures)"))
        if qid in declared:
            out.append(_f("query", f"queries[{i}].id", f"duplicate query id {qid}"))
        declared.add(qid)
    for i, t in enumerate(tiles):
        q = t.get("query") if isinstance(t, dict) else None
        if isinstance(q, str) and q not in declared:
            out.append(_f("query", f"tiles[{i}].query", f"query {q} is not declared in queries"))
    n = sum(1 for t in tiles if isinstance(t, dict) and t.get("type") in CHART_TYPES)
    if n > MAX_CHARTS:
        out.append(_f("charts", "tiles", f"{n} chart tiles; at most {MAX_CHARTS}"))
    return out


def _parse_v2(obj: dict) -> SpecV2:
    found: list[dict] = []
    spec = None
    try:
        spec = SpecV2.model_validate(obj)
    except ValidationError as e:
        found = findings_from(e)
    found += _semantic_v2(obj)
    if found:
        raise SpecError(found)
    return spec


def _semantic_v2(obj: dict) -> list[dict]:
    """Cross-field rules of a spec v2 over the raw object (sections, queries, date control)."""
    out: list[dict] = []
    tiles = obj.get("tiles") if isinstance(obj.get("tiles"), list) else []
    seen: set = set()
    ids: list = []
    for i, t in enumerate(tiles):
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str):
            continue
        if tid in seen:
            out.append(_f("layout", f"tiles[{i}].id", f"duplicate tile id {tid}"))
        seen.add(tid)
        ids.append(tid)
    sections = obj.get("sections") if isinstance(obj.get("sections"), list) else []
    if len(sections) > MAX_SECTIONS:
        out.append(_f("layout", "sections", f"{len(sections)} sections; at most {MAX_SECTIONS}"))
    placed: set = set()
    for s, sec in enumerate(sections):
        lst = sec.get("tiles") if isinstance(sec, dict) and isinstance(sec.get("tiles"), list) \
            else []
        if len(lst) > MAX_SECTION_TILES:
            out.append(_f("layout", f"sections[{s}].tiles",
                          f"{len(lst)} tiles in a section; at most {MAX_SECTION_TILES}"))
        for c, tid in enumerate(lst):
            if not isinstance(tid, str):
                continue
            if tid not in seen:
                out.append(_f("layout", f"sections[{s}].tiles[{c}]", f"unknown tile {tid}"))
            elif tid in placed:
                out.append(_f("layout", f"sections[{s}].tiles[{c}]",
                              f"tile {tid} is placed twice"))
            placed.add(tid)
    if sections:
        for tid in dict.fromkeys(ids):
            if tid not in placed:
                out.append(_f("layout", "sections", f"tile {tid} is not placed in any section"))
    declared: set = set()
    queries = obj.get("queries") if isinstance(obj.get("queries"), list) else []
    for i, q in enumerate(queries):
        qid = q.get("id") if isinstance(q, dict) else None
        if not isinstance(qid, str):
            continue
        if qid.startswith(MEASURE_QID_PREFIX):
            out.append(_f("query", f"queries[{i}].id",
                          f"query id {qid} may not start with m_ (reserved for measures)"))
        if qid in declared:
            out.append(_f("query", f"queries[{i}].id", f"duplicate query id {qid}"))
        declared.add(qid)
    for i, t in enumerate(tiles):
        q = t.get("query") if isinstance(t, dict) else None
        if isinstance(q, str) and q not in declared:
            out.append(_f("query", f"tiles[{i}].query", f"query {q} is not declared in queries"))
    n = sum(1 for t in tiles if isinstance(t, dict) and t.get("type") in CHART_TYPES_V2)
    if n > MAX_CHARTS_V2:
        out.append(_f("charts", "tiles", f"{n} chart tiles; at most {MAX_CHARTS_V2}"))
    dc = obj.get("date_control")
    filters = obj.get("filters") if isinstance(obj.get("filters"), list) else []
    if isinstance(dc, dict):
        presets = dc.get("presets")
        default = dc.get("default", "last_7_days")
        if isinstance(presets, list) and default not in presets:
            out.append(_f("binding", "date_control.presets",
                          f"presets must contain the default range {default}"))
    if filters and not isinstance(dc, dict):
        out.append(_f("binding", "filters", "filters need a date_control"))
    fids: set = set()
    fdims: list = []
    for i, fl in enumerate(filters):
        if not isinstance(fl, dict):
            continue
        if isinstance(fl.get("id"), str):
            if fl["id"] in fids:
                out.append(_f("binding", f"filters[{i}].id", f"duplicate filter id {fl['id']}"))
            fids.add(fl["id"])
        if isinstance(fl.get("dimension"), str):
            fdims.append(fl["dimension"])
    for i, t in enumerate(tiles):
        if not isinstance(t, dict) or t.get("date") != "control":
            continue
        if not isinstance(dc, dict):
            out.append(_f("binding", f"tiles[{i}].date",
                          "date control needs a date_control on the spec"))
        b = t.get("measure") if isinstance(t.get("measure"), dict) else (
            t.get("measures") if isinstance(t.get("measures"), dict) else {})
        by = [d for d in (b.get("by") if isinstance(b.get("by"), list) else [])
              if isinstance(d, str)]
        dims = set(by) | (set(fdims) if t.get("follow_filters") is True else set())
        if len(dims) > 2:
            out.append(_f("binding", f"tiles[{i}]",
                          f"{len(dims)} distinct dimensions across by and followed filters; "
                          "at most 2 under date control"))
    return out


_V2_HINT = re.compile(r'"spec_version"\s*:\s*2\s*[,}]')


def _is_v2(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    v = obj.get("spec_version")
    if "layout" in obj and "sections" not in obj:
        return False  # a v1-shaped object claiming version 2 keeps the v1 version finding
    return type(v) is int and v == SPEC_VERSION_2


def _v1_size_error(size: int) -> SpecError:
    return SpecError([_f("size", "spec", f"spec is {size} bytes; at most {MAX_SPEC_BYTES}")])


def parse(raw) -> Spec | SpecV2:
    """Validate a spec given as a dict or JSON text, dispatching on spec_version (1 or 2).
    Raises SpecError with every finding."""
    try:
        size = _size(raw)
    except (TypeError, ValueError):
        raise SpecError([_f("spec", "spec", _HTML_MSG)]) from None
    if size > MAX_SPEC_BYTES:
        hinted = _is_v2(raw) if isinstance(raw, dict) else (
            isinstance(raw, str) and _V2_HINT.search(raw) is not None)
        if not hinted:
            raise _v1_size_error(size)
        if size > MAX_SPEC_BYTES_V2:
            raise SpecError([_f("size", "spec",
                                f"spec is {size} bytes; at most {MAX_SPEC_BYTES_V2}")])
    obj = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except ValueError:
            if size > MAX_SPEC_BYTES:
                raise _v1_size_error(size) from None
            raise SpecError([_f("spec", "spec", _HTML_MSG)]) from None
    if _is_v2(obj):
        return _parse_v2(obj)
    if size > MAX_SPEC_BYTES:
        raise _v1_size_error(size)
    if not isinstance(obj, dict):
        raise SpecError([_f("spec", "spec", _HTML_MSG)])
    found: list[dict] = []
    spec = None
    try:
        spec = Spec.model_validate(obj)
    except ValidationError as e:
        found = findings_from(e)
    found += _semantic(obj)
    if found:
        raise SpecError(found)
    return spec


def schema_dict(version: int = 1) -> dict:
    return (SpecV2 if version == 2 else Spec).model_json_schema()


def schema_text(version: int = 1) -> str:
    return json.dumps(schema_dict(version), sort_keys=True, indent=2) + "\n"


# ---------------------------------------------------------------- measure compilation

def has_measures(spec: "Spec | SpecV2") -> bool:
    return any(getattr(t, "measure", None) is not None
               or getattr(t, "measures", None) is not None for t in spec.tiles)


def _window_params(b: MeasureBinding):
    """(window_days, start, end) as plain values; never a range token."""
    w = b.window
    if isinstance(w, WindowLastDays):
        return w.last_days, None, None
    if isinstance(w, WindowLastPeriods):
        return min(w.last_periods * GRAIN_DAYS[b.grain], modelcompiler.MAX_WINDOW_DAYS), None, None
    if isinstance(w, WindowFixed):
        return None, w.start, w.end
    if b.grain:
        return DEFAULT_WINDOW_DAYS[b.grain], None, None
    return None, None, None


def _filters_key(b: MeasureBinding):
    items = [(f.dimension, f.op, json.dumps(f.values, sort_keys=True)) for f in b.filters]
    return tuple(sorted(items))


def _group_key(b: MeasureBinding):
    return (b.name, tuple(b.by), b.grain, _filters_key(b), _window_params(b), b.top)


def _groups(spec: Spec) -> dict:
    """key -> {"qid", "tiles": [(index, tile)], "binding"} in tile order."""
    out: dict = {}
    used: set = set()
    for i, t in enumerate(spec.tiles):
        b = getattr(t, "measure", None)
        if b is None:
            continue
        k = _group_key(b)
        if k not in out:
            base = MEASURE_QID_PREFIX + t.id[:46]
            qid, n = base, 1
            while qid in used:
                n += 1
                suf = f"_{n}"
                qid = MEASURE_QID_PREFIX + t.id[:46 - len(suf)] + suf
            used.add(qid)
            out[k] = {"qid": qid, "tiles": [], "binding": b}
        out[k]["tiles"].append((i, t))
    return out


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fixed_sql(inner: str, *, grain, window, by_cols, order_col, top) -> str:
    """The v1 fixed-window wrapper: whole-period trimming, ORDER BY and the top LIMIT."""
    if grain:
        order = ", ".join([_quote(f"metric_time__{grain}")] + [_quote(c) for c in by_cols])
        tail = f" ORDER BY {order}"
    else:
        tail = f" ORDER BY {_quote(order_col)} DESC NULLS LAST"
        if top:
            tail += f" LIMIT {int(top)}"
    keep = ""
    if grain:
        n = (window.last_periods if isinstance(window, WindowLastPeriods)
             else DEFAULT_WINDOW_DAYS[grain] // GRAIN_DAYS[grain]
             if window is None else None)
        if n:
            # Trim the over-fetched window to whole periods so the first period is complete.
            unit, mult = {"quarter": ("month", 3)}.get(grain, (grain, 1))
            back = (int(n) - 1) * mult
            keep = (f" WHERE {_quote(f'metric_time__{grain}')} >= "
                    f"(date_trunc('{grain}', CURRENT_DATE) - interval '{back} {unit}')::date")
    return f"SELECT * FROM ({inner}) AS _m{keep}{tail}"


def compile_measures(spec, model, *, naive_utc: bool = False) -> list[dict]:
    """One frozen query entry per distinct measure binding, in tile order."""
    if isinstance(spec, SpecV2):
        return compile_measures_v2(spec, model, naive_utc=naive_utc)
    return _compile_measures_v1(spec, model)


def _compile_measures_v1(spec: Spec, model) -> list[dict]:
    """Spec v1: one frozen query entry per distinct measure binding, in tile order."""
    from .modeltools import _expand_dim  # lazy: modeltools imports langchain and the DB layer

    found: list[dict] = []
    entries: list[dict] = []
    for key, g in _groups(spec).items():
        b: MeasureBinding = g["binding"]
        i0 = g["tiles"][0][0]
        path = f"tiles[{i0}].measure"
        if model.measure(b.name) is None:
            found.append(_f("not_modeled", path, f"measure {b.name} is not modeled"))
            continue
        try:
            days, start, end = _window_params(b)
            by = [_expand_dim(model, d) for d in b.by]
            filters = [{"dimension": _expand_dim(model, f.dimension), "op": f.op,
                        "values": list(f.values)} for f in b.filters]
            req = modelcompiler.make_request(b.name, by=by, grain=b.grain, filters=filters,
                                             start=start, end=end, window_days=days)
            comp = modelcompiler.compile_measure(model, req)
        except modelcompiler.NotModeled as e:
            found.append(_f("measure", path, str(e)))
            continue
        except modelcompiler.InvalidRequest as e:
            found.append(_f("measure", path, str(e)))
            continue
        cols = list(comp.columns)
        by_cols = cols[(1 if b.grain else 0):-1]
        sql = _fixed_sql(comp.sql, grain=b.grain, window=b.window, by_cols=by_cols,
                         order_col=comp.measure, top=b.top)
        try:
            validate_readonly_sql(sql)
        except SQLGuardrailError as e:
            found.append(_f("sql", path, f"compiled SQL was refused: {e}"))
            continue
        request = {"measure": req.measure, "by": list(req.by), "grain": req.grain,
                   "filters": filters}
        if req.window_days is not None:
            request["window_days"] = req.window_days
        if req.start is not None:
            request["start"] = req.start.isoformat()
            request["end"] = req.end.isoformat() if req.end else None
        if b.top:
            request["top"] = b.top
        refresh = min(t.measure.refresh_minutes for _, t in g["tiles"])
        entries.append({"id": g["qid"], "source": "measure", "measure": comp.measure,
                        "request": request, "columns": cols, "sql": sql,
                        "refresh_minutes": refresh})
    if found:
        raise SpecError(found)
    return entries


def sql_columns(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        raise SpecError([_f("columns", "queries", "could not read the SQL output columns")]) \
            from None
    sel = tree if isinstance(tree, exp.Query) else None
    if sel is None:
        raise SpecError([_f("columns", "queries", "the query must be a SELECT")])
    out = []
    for e in sel.selects:
        name = e.alias_or_name
        if isinstance(e, exp.Star) or not name or "*" in name:
            raise SpecError([_f("columns", "queries",
                                "alias every output column (no SELECT * and no unaliased "
                                "expressions)")])
        out.append(name)
    return out


# ---------------------------------------------------------------- binding

def _query_columns(entry: dict) -> list[str]:
    if entry.get("source") == "measure":
        return list(entry["columns"])
    if entry.get("source") == dashboards.KPI_SOURCE:
        return list(KPI_COLUMNS)
    if entry.get("source") == dashboards.FORECAST_SOURCE:
        return list(FORECAST_COLUMNS)
    return sql_columns(entry["sql"])


def _check_cols(found, path, cols, avail, qid):
    for c in cols:
        if c is not None and c not in avail:
            found.append(_f("columns", path, f"column {c} is not an output of query {qid}; "
                            f"valid: {', '.join(avail)}"))


def bind(spec, declared: list[dict], compiled: list[dict]) -> dict:
    """The stored form: the authoring dump with a qid on every bound tile and the column
    references filled in."""
    if isinstance(spec, SpecV2):
        return bind_v2(spec, declared, compiled)
    entries = {e["id"]: e for e in list(declared) + list(compiled)}
    qid_of = {}
    for g in _groups(spec).values():
        for _, t in g["tiles"]:
            qid_of[t.id] = g["qid"]
    stored = spec.model_dump(mode="json", exclude_none=True)
    found: list[dict] = []
    for i, t in enumerate(spec.tiles):
        d = stored["tiles"][i]
        if t.type == "text":
            continue
        qid = t.query if t.query is not None else qid_of.get(t.id)
        entry = entries.get(qid)
        if entry is None:
            found.append(_f("query", f"tiles[{i}]", f"query {qid} is not available"))
            continue
        d["qid"] = qid
        try:
            avail = _query_columns(entry)
        except SpecError as e:
            found.extend(_f(f["rule"], f"tiles[{i}]", f["message"]) for f in e.findings)
            continue
        b = t.measure
        time_col = f"metric_time__{b.grain}" if b is not None and b.grain else None
        by_cols = avail[(1 if time_col else 0):-1] if b is not None else []
        mcol = avail[-1] if b is not None else None
        p = f"tiles[{i}]"
        if t.type == "stat":
            if b is not None:
                d.setdefault("column", mcol)
                if time_col:
                    d.setdefault("x", time_col)
            if d.get("sparkline") and not d.get("x"):
                found.append(_f("binding", f"{p}.sparkline", "a sparkline needs x"))
            cmp_ = d.get("compare")
            _check_cols(found, p, [d.get("column"), d.get("x"),
                                   cmp_["column"] if isinstance(cmp_, dict) else None],
                        avail, qid)
        elif t.type in ("line", "area", "sparkline"):
            if b is not None:
                if not time_col:
                    found.append(_f("binding", f"{p}.measure.grain",
                                    f"a {t.type} tile over a measure needs a grain"))
                    continue
                d.setdefault("x", time_col)
                if t.type == "sparkline" and by_cols:
                    found.append(_f("binding", f"{p}.measure.by",
                                    "a sparkline over a measure takes no by"))
                    continue
                if len(by_cols) > 1:
                    found.append(_f("binding", f"{p}.measure.by",
                                    f"a {t.type} tile with a grain takes at most one by dimension"))
                    continue
                if by_cols:
                    d.setdefault("series", by_cols[0])
                if not d.get("y"):
                    d["y"] = [{"column": mcol, "dashed": False}]
            band = d.get("band") or {}
            _check_cols(found, p, [d.get("x"), d.get("series"), band.get("low"), band.get("high")]
                        + [y["column"] for y in d.get("y", [])], avail, qid)
        elif t.type in ("bar", "stacked_bar"):
            if b is not None:
                if not time_col and not by_cols:
                    found.append(_f("binding", f"{p}.measure",
                                    f"a {t.type} tile over a measure needs a grain or by"))
                    continue
                if time_col:
                    if len(by_cols) > 1:
                        found.append(_f("binding", f"{p}.measure.by",
                                        f"a {t.type} tile with a grain takes at most one by dimension"))
                        continue
                    d.setdefault("x", time_col)
                    if by_cols:
                        d.setdefault("series", by_cols[0])
                else:
                    d.setdefault("x", by_cols[0])
                    if len(by_cols) > 1:
                        d.setdefault("series", by_cols[1])
                if not d.get("y"):
                    d["y"] = [{"column": mcol, "dashed": False}]
            if t.type == "stacked_bar" and not d.get("series") and len(d.get("y", [])) < 2:
                found.append(_f("binding", p, "stacked_bar needs series or at least two y"))
            _check_cols(found, p, [d.get("x"), d.get("series")]
                        + [y["column"] for y in d.get("y", [])], avail, qid)
        elif t.type == "table":
            if not d.get("columns"):
                d["columns"] = [{"column": c} for c in avail]
            _check_cols(found, p, [c["column"] for c in d["columns"]], avail, qid)
    if found:
        raise SpecError(found)
    return stored


def stored_queries(declared: list[dict], compiled: list[dict],
                   max_queries: int = dashboards.MAX_QUERIES) -> list[dict]:
    out = list(declared) + list(compiled)
    if len(out) > max_queries:
        raise SpecError([_f("queries", "queries",
                            f"{len(out)} queries (declared plus compiled); at most "
                            f"{max_queries}")])
    return out


# ---------------------------------------------------------------- render, authoring, diff, summary

def _render_tile(d: dict) -> dict:
    t = d["type"]
    base = {"id": d["id"], "type": t, "title": d.get("title"), "note": d.get("note")}
    if t == "text":
        base.update({"qid": None, "grain": None, "format": None, "currency": None, "unit": None,
                     "text": d["text"]})
        return base
    m = d.get("measure") or {}
    base.update({"qid": d.get("qid"), "grain": d.get("grain") or m.get("grain"),
                 "format": d.get("format", "number"), "currency": d.get("currency"),
                 "unit": d.get("unit")})
    ys = [{"column": y["column"], "label": y.get("label"), "dashed": bool(y.get("dashed"))}
          for y in d.get("y", [])]
    if t == "stat":
        base.update({"column": d.get("column"), "x": d.get("x"), "pick": d.get("pick", "last"),
                     "compare": d.get("compare", "none"),
                     "compare_label": d.get("compare_label"),
                     "good_direction": d.get("good_direction", "none"),
                     "sparkline": bool(d.get("sparkline", False)),
                     "owed_when": d.get("owed_when")})
    elif t in CHART_TYPES:
        band = d.get("band")
        base.update({"x": d.get("x"), "y": ys, "series": d.get("series"),
                     "band": ({"low": band["low"], "high": band["high"],
                               "label": band.get("label")} if band else None),
                     "horizontal": bool(d.get("horizontal", False)),
                     "x_label": d.get("x_label"), "y_label": d.get("y_label")})
    elif t == "sparkline":
        base.update({"x": d.get("x"), "y": ys})
    elif t == "table":
        base.update({"columns": [{"column": c["column"], "label": c.get("label"),
                                  "format": c.get("format"), "currency": c.get("currency"),
                                  "unit": c.get("unit")} for c in d.get("columns", [])],
                     "max_rows": d.get("max_rows", 25)})
    return base


def render_spec(stored: dict, queries: list[dict]) -> dict:
    """What the browser gets: tiles, layout, query ids, column names and refresh minutes.
    No SQL, no measure binding, no filter values."""
    if stored.get("spec_version") == SPEC_VERSION_2:
        return render_spec_v2(stored, queries)
    refresh_by = {q["id"]: q["refresh_minutes"] for q in queries}
    tiles = [_render_tile(t) for t in stored["tiles"]]
    refresh = {t["qid"]: refresh_by[t["qid"]] for t in tiles
               if t["qid"] is not None and t["qid"] in refresh_by}
    return {"render_version": RENDER_VERSION, "title": stored["title"],
            "subtitle": stored.get("subtitle"), "rows": copy.deepcopy(stored["layout"]["rows"]),
            "tiles": tiles, "refresh": refresh}


def authoring_form(stored: dict) -> dict:
    out = copy.deepcopy(stored)
    for t in out["tiles"]:
        t.pop("qid", None)
    return out


def _canon(tile: dict) -> str:
    return json.dumps({k: v for k, v in tile.items() if k != "qid"}, sort_keys=True,
                      ensure_ascii=False)


def tile_diff(old: dict | None, new: dict) -> dict:
    n = {t["id"]: _canon(t) for t in new["tiles"]}
    if old is None:
        return {"added": sorted(n), "removed": [], "changed": []}
    o = {t["id"]: _canon(t) for t in old["tiles"]}
    return {"added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)),
            "changed": sorted(k for k in set(n) & set(o) if n[k] != o[k])}


def summary(stored: dict, queries: list[dict]) -> dict:
    tiles, measures = [], set()
    for t in stored["tiles"]:
        if t["type"] == "text":
            binding, ref = "none", None
        elif t.get("measure"):
            binding, ref = "measure", t["measure"]["name"]
            measures.add(ref)
        elif t.get("measures"):
            names = t["measures"]["names"]
            binding, ref = "measure", ",".join(names)
            measures.update(names)
        else:
            binding, ref = "query", t.get("query") or t.get("qid")
        tiles.append({"id": t["id"], "type": t["type"], "title": t.get("title"),
                      "binding": binding, "ref": ref})
    qs = []
    for q in queries:
        src = q.get("source") or "sql"
        qs.append({"id": q["id"], "source": src})
    return {"tiles": tiles, "measures": sorted(measures), "queries": qs}


# ---- Phase 22: v2 compile, bind, render ---------------------------------------------------------

_MEASURES_TYPES = ("table", "line", "area", "bar", "stacked_bar", "combo", "paired_bar")
_MEMBER_TYPES = ("ranked_bar", "donut", "aging", "funnel")
_TIME_OR_MEMBER_TYPES = ("line", "area", "bar", "stacked_bar", "combo", "paired_bar", "waterfall",
                         "calendar")


def _binding_of(t):
    return t.measure if getattr(t, "measure", None) is not None else getattr(t, "measures", None)


def _names_of(t) -> tuple:
    if getattr(t, "measure", None) is not None:
        return (t.measure.name,)
    return tuple(t.measures.names)


def _group_key_v2(t, spec: SpecV2):
    b = _binding_of(t)
    control = t.date == "control"
    follow = tuple(f.dimension for f in spec.filters) if control and t.follow_filters else ()
    comps = control or (t.type == "table" and bool(getattr(t, "totals", False)))
    window = None if control else _window_params(b)
    grain = "day" if control else b.grain
    return (_names_of(t), tuple(b.by), grain, _filters_key(b), window, b.top,
            bool(getattr(b, "other", False)), control, follow, comps)


def _groups_v2(spec: SpecV2) -> dict:
    """key -> {"qid", "tiles": [(index, tile)], "binding", "names", "control", "follow"}."""
    out: dict = {}
    used: set = set()
    for i, t in enumerate(spec.tiles):
        if _binding_of(t) is None:
            continue
        k = _group_key_v2(t, spec)
        if k not in out:
            base = MEASURE_QID_PREFIX + t.id[:46]
            qid, n = base, 1
            while qid in used:
                n += 1
                suf = f"_{n}"
                qid = MEASURE_QID_PREFIX + t.id[:46 - len(suf)] + suf
            used.add(qid)
            out[k] = {"qid": qid, "tiles": [], "binding": _binding_of(t), "names": _names_of(t),
                      "control": k[7], "follow": k[8], "comps": k[9]}
        out[k]["tiles"].append((i, t))
    return out


def _expanded_filters(model, b, expand) -> list[dict]:
    return [{"dimension": expand(model, f.dimension), "op": f.op, "values": list(f.values)}
            for f in b.filters]


def compile_measures_v2(spec: SpecV2, model, *, naive_utc: bool = False) -> list[dict]:
    """Spec v2: one frozen query entry per distinct tile binding (plus a presets entry per
    controlled tile with one count-distinct measure), in tile order."""
    from .modeltools import _expand_dim  # lazy: modeltools imports langchain and the DB layer

    found: list[dict] = []
    entries: list[dict] = []
    groups = _groups_v2(spec)
    used = {g["qid"] for g in groups.values()}
    dc = spec.date_control or DateControl()
    for g in groups.values():
        i0, t0 = g["tiles"][0]
        b = g["binding"]
        names = list(g["names"])
        control = g["control"]
        path = f"tiles[{i0}].measure" if getattr(t0, "measure", None) is not None \
            else f"tiles[{i0}].measures"
        kinds: dict = {}
        missing = False
        for n in names:
            m = model.measure(n)
            if m is None:
                found.append(_f("not_modeled", path, f"measure {n} is not modeled"))
                missing = True
            else:
                kinds[n] = modelcompiler.measure_kind(model, m)
        if missing:
            continue
        try:
            by = [_expand_dim(model, d) for d in b.by]
            follow = []
            for d in g["follow"]:
                x = _expand_dim(model, d)
                if x not in by and x not in follow:
                    follow.append(x)
            all_by = by + follow
            filters = _expanded_filters(model, b, _expand_dim)
        except modelcompiler.InvalidRequest as e:
            found.append(_f("measure", path, str(e)))
            continue
        distinct = [n for n in names if kinds[n] == "distinct"]
        mode = "fixed"
        horizon = None
        days = start = end = None
        grain = None
        if control:
            bad = next((n for n in names if kinds[n] == "fixed"), None)
            if bad:
                found.append(_f("measure", path, f"measure {bad} compares periods or is not "
                                "additive, so it cannot follow the date control"))
                continue
            nowm = next((n for n in names if kinds[n] == "now"), None)
            if nowm and len(names) > 1:
                found.append(_f("measure", path, f"measure {nowm} has no date; put it in its "
                                "own tile"))
                continue
            if nowm and g["follow"]:
                found.append(_f("binding", f"tiles[{i0}]", f"{nowm} has no date, so it cannot "
                                "follow page filters"))
                continue
            if len(distinct) > 1:
                found.append(_f("measure", path, f"measures {distinct[0]} and {distinct[1]} "
                                "both count distinct values; put each in its own tile"))
                continue
            if distinct and all_by:
                found.append(_f("binding", f"tiles[{i0}]", f"{distinct[0]} counts distinct "
                                "values, so under the date control it cannot be split by a "
                                "dimension or follow page filters; use a fixed window, or a "
                                "per-day chart without by"))
                continue
            if nowm:
                mode = "now"
            else:
                mode = "control"
                grain = "day"
                horizon = min(dc.horizon_days, 90) if all_by else dc.horizon_days
                days = horizon
        else:
            days, start, end = _window_params(b)
            grain = b.grain
        other = bool(getattr(b, "other", False))
        try:
            comp = modelcompiler.compile_tile(
                model, names, by=all_by, grain=grain, filters=filters, window_days=days,
                start=start, end=end, components=g["comps"] and mode != "now",
                naive_utc=naive_utc, top=b.top if not control else None, other=other)
        except (modelcompiler.NotModeled, modelcompiler.InvalidRequest) as e:
            found.append(_f("measure", path, str(e)))
            continue
        cols = list(comp.columns)
        if mode == "fixed" and not other:
            skip = 1 if grain else 0
            sql = _fixed_sql(comp.sql, grain=grain, window=b.window,
                             by_cols=cols[skip:skip + len(all_by)], order_col=names[0],
                             top=b.top)
            try:
                validate_readonly_sql(sql)
            except SQLGuardrailError as e:
                found.append(_f("sql", path, f"compiled SQL was refused: {e}"))
                continue
        else:
            sql = comp.sql
        request: dict = {"kind": "tile", "tile": t0.id, "names": names, "by": all_by,
                         "grain": grain, "filters": filters, "other": other,
                         "follow": mode, "horizon_days": horizon, "metas": list(comp.metas),
                         "notes": [str(x) for x in comp.notes]}
        if mode != "now":
            if days is not None:
                request["window_days"] = days
            if start is not None:
                request["start"] = start
                request["end"] = end
        if b.top is not None and not control:
            request["top"] = b.top
        refresh = min(_binding_of(t).refresh_minutes for _, t in g["tiles"])
        entries.append({"id": g["qid"], "source": "measure", "measure": names[0],
                        "request": request, "columns": cols, "sql": sql,
                        "refresh_minutes": refresh})
        if mode == "control" and distinct:
            pq, n = MEASURE_QID_PREFIX + t0.id[:44] + "_p", 1
            while pq in used:
                n += 1
                pq = MEASURE_QID_PREFIX + t0.id[:44 - len(str(n))] + f"{n}_p"
            used.add(pq)
            presets = list(dc.presets or PRESETS)
            try:
                pc = modelcompiler.compile_presets(
                    model, distinct[0], filters=filters, presets=presets,
                    fy_start_month=dc.fy_start_month, naive_utc=naive_utc)
            except (modelcompiler.NotModeled, modelcompiler.InvalidRequest) as e:
                found.append(_f("measure", path, str(e)))
                continue
            entries.append({
                "id": pq, "source": "measure", "measure": distinct[0],
                "request": {"kind": "presets", "tile": t0.id, "names": [distinct[0]], "by": [],
                            "grain": None, "filters": filters, "follow": "control",
                            "horizon_days": None, "presets": presets,
                            "fy_start_month": dc.fy_start_month},
                "columns": list(pc.columns), "sql": pc.sql, "refresh_minutes": refresh})
    if found:
        raise SpecError(found)
    return entries


def _by_columns_v2(binding: dict) -> list[str]:
    """Output column names of a stored binding's own by entries, in binding order."""
    return [modelcompiler._out_name(d) for d in (binding or {}).get("by", [])]


def _col_default(c: str) -> dict:
    return {"column": c, "delta": False, "good_direction": "none", "bar": False}


def _y_default(kind: str, names) -> list[dict]:
    if kind == "combo":
        return [{"column": n, "dashed": False, "as": "bar" if i == 0 else "line"}
                for i, n in enumerate(names)]
    return [{"column": n, "dashed": False} for n in names]


def bind_v2(spec: SpecV2, declared: list[dict], compiled: list[dict]) -> dict:
    """The stored form of a v2 spec: the authoring dump with a qid on every bound tile and the
    column references filled in."""
    entries = {e["id"]: e for e in list(declared) + list(compiled)}
    qid_of = {}
    for g in _groups_v2(spec).values():
        for _, t in g["tiles"]:
            qid_of[t.id] = g["qid"]
    stored = spec.model_dump(mode="json", exclude_none=True, by_alias=True)
    found: list[dict] = []
    for i, t in enumerate(spec.tiles):
        d = stored["tiles"][i]
        if t.type == "text":
            continue
        qid = t.query if t.query is not None else qid_of.get(t.id)
        entry = entries.get(qid)
        p = f"tiles[{i}]"
        if entry is None:
            found.append(_f("query", p, f"query {qid} is not available"))
            continue
        d["qid"] = qid
        try:
            avail = _query_columns(entry)
        except SpecError as e:
            found.extend(_f(f["rule"], p, f["message"]) for f in e.findings)
            continue
        b = _binding_of(t)
        names = list(_names_of(t)) if b is not None else []
        if t.measures is not None and t.type not in _MEASURES_TYPES:
            found.append(_f("binding", f"{p}.measures",
                            f"a {t.type} tile takes one measure, not measures"))
            continue
        grain_req = (entry.get("request") or {}).get("grain") if b is not None else None
        time_col = f"metric_time__{grain_req}" if grain_req else None
        own = d.get("measure") or d.get("measures")
        by_cols = _by_columns_v2(own) if b is not None else []
        ok = True
        if b is not None:
            ty = t.type
            if ty == "stat":
                d.setdefault("column", names[0])
                if time_col:
                    d.setdefault("x", time_col)
            elif ty == "bullet":
                d.setdefault("column", names[0])
            elif ty == "sparkline":
                if not time_col:
                    found.append(_f("binding", f"{p}.measure.grain",
                                    "a sparkline over a measure needs a grain or the date control"))
                    ok = False
                elif by_cols:
                    found.append(_f("binding", f"{p}.measure.by",
                                    "a sparkline over a measure takes no by"))
                    ok = False
                else:
                    d.setdefault("x", time_col)
                    if not d.get("y"):
                        d["y"] = [{"column": names[0], "dashed": False}]
            elif ty in _MEMBER_TYPES:
                if not by_cols:
                    found.append(_f("binding", f"{p}.measure.by",
                                    f"a {ty} tile over a measure needs a by dimension"))
                    ok = False
                else:
                    d.setdefault("x", by_cols[0])
                    if not d.get("y"):
                        d["y"] = _y_default(ty, names[:1])
            elif ty in _TIME_OR_MEMBER_TYPES:
                if ty == "paired_bar" and not d.get("y") and len(names) != 2:
                    found.append(_f("binding", f"{p}.measures",
                                    "a paired_bar tile needs exactly two measures (measures)"))
                    ok = False
                elif ty == "combo" and not d.get("y") and not 2 <= len(names) <= 4:
                    found.append(_f("binding", f"{p}.measures",
                                    "a combo tile needs two to four measures (measures)"))
                    ok = False
                elif ty in ("waterfall", "calendar") and len(names) != 1:
                    found.append(_f("binding", f"{p}.measure",
                                    f"a {ty} tile takes one measure"))
                    ok = False
                if ok and not time_col and not by_cols:
                    found.append(_f("binding", f"{p}.measure",
                                    f"a {ty} tile over a measure needs a grain, the date "
                                    "control or by"))
                    ok = False
                if ok:
                    series_ok = ty in ("line", "area", "bar", "stacked_bar")
                    if time_col:
                        if len(by_cols) > 1 and series_ok:
                            found.append(_f("binding", f"{p}.measure.by",
                                            f"a {ty} tile with a grain or the date control "
                                            "takes at most one by dimension"))
                            ok = False
                        else:
                            d.setdefault("x", time_col)
                            if by_cols and series_ok:
                                d.setdefault("series", by_cols[0])
                    else:
                        d.setdefault("x", by_cols[0])
                        if len(by_cols) > 1 and series_ok:
                            d.setdefault("series", by_cols[1])
                    if ok and not d.get("y"):
                        d["y"] = _y_default(ty, names)
            elif ty == "table":
                if not d.get("columns"):
                    keys = ([time_col] if time_col else []) + by_cols
                    d["columns"] = [_col_default(c) for c in keys + names]
        elif t.type == "table" and not d.get("columns"):
            d["columns"] = [_col_default(c) for c in avail]
        if t.type == "stacked_bar" and ok and not d.get("series") and len(d.get("y", [])) < 2:
            found.append(_f("binding", p, "stacked_bar needs series or at least two y"))
            continue
        if not ok:
            continue
        cmp_ = d.get("compare")
        band = d.get("band") or {}
        tgt = d.get("target") or {}
        refs = [d.get("x"), d.get("series"), d.get("column"), band.get("low"), band.get("high"),
                tgt.get("column"), (d.get("sort") or {}).get("column"),
                cmp_["column"] if isinstance(cmp_, dict) else None]
        refs += [y["column"] for y in d.get("y", [])]
        refs += [c["column"] for c in d.get("columns", [])]
        refs += by_cols
        _check_cols(found, p, refs, avail, qid)
        if t.type == "stat" and d.get("sparkline") and not d.get("x"):
            found.append(_f("binding", f"{p}.sparkline", "a sparkline needs x"))
    if found:
        raise SpecError(found)
    return stored


def _y_render(d: dict) -> list[dict]:
    return [{"column": y["column"], "label": y.get("label"), "dashed": bool(y.get("dashed")),
             "tone": y.get("tone"), "as": y.get("as")} for y in d.get("y", [])]


def _render_tile_v2(d: dict, by_qid: dict, queries: list[dict]) -> dict:
    t = d["type"]
    base = {"id": d["id"], "type": t, "title": d.get("title"), "note": d.get("note"),
            "width": d.get("width") or DEFAULT_WIDTH[t]}
    if t == "text":
        base.update({"qid": None, "grain": None, "format": None, "currency": None, "unit": None,
                     "follow": "fixed", "follow_filters": False, "horizon_days": None,
                     "presets_qid": None, "target": None, "measures": [], "by_columns": [],
                     "text": d["text"]})
        return base
    own = d.get("measure") or d.get("measures") or {}
    qid = d.get("qid")
    req = (by_qid.get(qid) or {}).get("request") or {}
    control = d.get("date") == "control" and bool(own)
    follow = (req.get("follow") or "control") if control else "fixed"
    presets_qid = None
    if own and control:
        for q in queries:
            r = q.get("request") or {}
            if q.get("source") == "measure" and r.get("kind") == "presets" \
                    and r.get("tile") == req.get("tile"):
                presets_qid = q["id"]
                break
    metas = [{"column": m["column"], "label": m.get("label"),
              "description": m.get("description"), "kind": m.get("kind"),
              "parts": m.get("parts"), "formula": m.get("formula"),
              "date_basis": m.get("date_basis")} for m in req.get("metas", [])] if own else []
    base.update({
        "qid": qid, "grain": d.get("grain") or own.get("grain"),
        "format": d.get("format", "number"), "currency": d.get("currency"),
        "unit": d.get("unit"), "follow": follow,
        "follow_filters": bool(d.get("follow_filters", False)),
        "horizon_days": req.get("horizon_days") if follow == "control" else None,
        "presets_qid": presets_qid, "target": d.get("target"), "measures": metas,
        "by_columns": _by_columns_v2(own) if own else [],
        # C-07: the compiled SQL ends a fixed top+other result with an Other row
        "other_row": bool(own and own.get("other") and own.get("top") is not None),
        # the renderer confirms the Other row from the data: top + 1 rows ending in 'Other'
        "other_top": own.get("top") if own and own.get("other") and own.get("top") is not None else None})
    if t == "stat":
        base.update({"column": d.get("column"), "x": d.get("x"), "pick": d.get("pick", "last"),
                     "compare": d.get("compare", "none"),
                     "compare_label": d.get("compare_label"),
                     "comparison": d.get("comparison", "previous_period"),
                     "good_direction": d.get("good_direction", "none"),
                     "sparkline": bool(d.get("sparkline", False)),
                     "owed_when": d.get("owed_when")})
    elif t == "bullet":
        base.update({"column": d.get("column"), "bands": d.get("bands")})
    elif t == "sparkline":
        base.update({"x": d.get("x"), "y": _y_render(d)})
    elif t == "table":
        base.update({"columns": [
            {"column": c["column"], "label": c.get("label"), "format": c.get("format"),
             "currency": c.get("currency"), "unit": c.get("unit"),
             "owed_when": c.get("owed_when"), "delta": bool(c.get("delta", False)),
             "good_direction": c.get("good_direction", "none"), "bar": bool(c.get("bar", False)),
             "total": c.get("total"), "due": c.get("due")} for c in d.get("columns", [])],
            "max_rows": d.get("max_rows", 25), "sort": d.get("sort"),
            "totals": bool(d.get("totals", False))})
    else:
        band = d.get("band")
        base.update({"x": d.get("x"), "y": _y_render(d), "series": d.get("series"),
                     "x_label": d.get("x_label"), "y_label": d.get("y_label"),
                     "band": ({"low": band["low"], "high": band["high"],
                               "label": band.get("label")} if band else None),
                     "horizontal": bool(d.get("horizontal", False)),
                     "ghost": d.get("ghost", "none"), "percent": bool(d.get("percent", False)),
                     "mirror": bool(d.get("mirror", False)), "ends": d.get("ends", "first_last"),
                     "top": d.get("top"), "other": bool(d.get("other", False)),
                     "cumulative": bool(d.get("cumulative", False))})
    return base


def render_spec_v2(stored: dict, queries: list[dict]) -> dict:
    """C-02: what the browser gets for a v2 version. No SQL, no measure binding, no filter
    values, no window. `timezone` is filled at serve time."""
    by_qid = {q["id"]: q for q in queries}
    hidden = {t["id"] for t in stored["tiles"] if t.get("hidden")}
    shown = [t for t in stored["tiles"] if t["id"] not in hidden]
    tiles = [_render_tile_v2(t, by_qid, queries) for t in shown]
    refresh: dict = {}
    for t in tiles:
        for q in (t["qid"], t["presets_qid"]):
            if q is not None and q in by_qid and q not in refresh:
                refresh[q] = by_qid[q]["refresh_minutes"]
    dc = stored.get("date_control")
    if dc is not None:
        dc = {"default": dc.get("default", "last_7_days"),
              "presets": list(dc.get("presets") or PRESETS),
              "horizon_days": dc.get("horizon_days", 400),
              "fy_start_month": dc.get("fy_start_month", 4)}
    sections = []
    for sec in stored["sections"]:
        ids = [x for x in sec["tiles"] if x not in hidden]
        if ids:
            sections.append({"title": sec.get("title"), "description": sec.get("description"),
                             "tiles": ids})
    return {"render_version": 2, "title": stored["title"], "subtitle": stored.get("subtitle"),
            "number_locale": stored.get("number_locale"), "timezone": None, "date_control": dc,
            "filters": [{"id": f["id"], "label": f["label"],
                         "column": modelcompiler._out_name(f["dimension"])}
                        for f in stored.get("filters", [])],
            "sections": sections, "tiles": tiles, "refresh": refresh}


# ---- Phase 22: owner layout tweak (DCAP-15) -----------------------------------------------------

_LAYOUT_KEYS = ("title", "width", "hidden", "qid")


def _lay(msg: str, path: str = "spec") -> SpecError:
    return SpecError([_f("layout", path, msg)])


def _eff_width(t: dict) -> str:
    return t.get("width") or DEFAULT_WIDTH.get(t.get("type"), "half")


def layout_tweak(stored: dict, changes: dict) -> dict:
    """Pure. A new stored v2 dict where only section order and titles, tile width, hidden and
    title, and date_control.default differ. Raises SpecError (rule layout or binding)."""
    if not isinstance(stored, dict) or stored.get("spec_version") != SPEC_VERSION_2:
        raise _lay("layout editing needs a spec v2 dashboard")
    if not isinstance(changes, dict):
        raise _lay("changes must be an object")
    base_tiles = {t["id"]: t for t in stored["tiles"]}
    base_secs = stored["sections"]
    secs = changes.get("sections")
    if not isinstance(secs, list) or len(secs) != len(base_secs):
        raise _lay("the number of sections cannot change", "sections")
    seen: list[str] = []
    for i, s in enumerate(secs):
        ids = s.get("tiles") if isinstance(s, dict) else None
        if not isinstance(ids, list) or not ids:
            raise _lay("a section cannot be empty", f"sections[{i}]")
        if len(ids) > MAX_SECTION_TILES:
            raise _lay(f"a section holds at most {MAX_SECTION_TILES} tiles", f"sections[{i}]")
        if not all(isinstance(x, str) for x in ids):
            raise _lay("tile ids are text", f"sections[{i}]")
        seen.extend(ids)
    if len(seen) != len(set(seen)) or set(seen) != set(base_tiles):
        raise _lay("the set of tiles cannot change; every tile appears once", "sections")
    widths = changes.get("widths") or {}
    titles = changes.get("titles") or {}
    hidden = changes.get("hidden")
    if not isinstance(widths, dict) or not isinstance(titles, dict) \
            or (hidden is not None and not isinstance(hidden, list)):
        raise _lay("widths and titles are objects, hidden is a list")
    for tid, w in widths.items():
        if tid not in base_tiles:
            raise _lay(f"unknown tile {tid}", f"widths.{tid}")
        if w not in WIDTHS:
            raise _lay(f"width must be one of {', '.join(WIDTHS)}", f"widths.{tid}")
    for tid, ttl in titles.items():
        if tid not in base_tiles:
            raise _lay(f"unknown tile {tid}", f"titles.{tid}")
        if not isinstance(ttl, str):
            raise _lay("a title is text", f"titles.{tid}")
    if hidden is not None:
        if not all(isinstance(h, str) for h in hidden):
            raise _lay("tile ids are text", "hidden")
        bad = [h for h in hidden if h not in base_tiles]
        if bad:
            raise _lay(f"unknown tile {bad[0]}", "hidden")
        if set(hidden) == set(base_tiles):
            raise _lay("at least one tile must stay visible", "hidden")
    out = copy.deepcopy(stored)
    for i, s in enumerate(secs):
        out["sections"][i]["tiles"] = list(s["tiles"])
        if "title" in s:
            ttl = s["title"]
            if ttl in (None, ""):
                out["sections"][i].pop("title", None)
            else:
                out["sections"][i]["title"] = ttl
    for t in out["tiles"]:
        # only a real change writes a width: the version diff must not list every tile as changed
        if t["id"] in widths and widths[t["id"]] != _eff_width(t):
            t["width"] = widths[t["id"]]
        if t["id"] in titles:
            t["title"] = titles[t["id"]]
        if hidden is not None:
            t["hidden"] = t["id"] in hidden
    rng = changes.get("default_range")
    if rng is not None:
        dc = out.get("date_control")
        if rng not in PRESETS:
            raise _lay(f"unknown range {rng}", "default_range")
        if dc is None:
            raise _lay("this dashboard has no date control", "default_range")
        if dc.get("presets") is not None and rng not in dc["presets"]:
            raise _lay("the default range must be one of the offered ranges", "default_range")
        dc["default"] = rng

    def core(tiles):
        return {t["id"]: {k: v for k, v in t.items() if k not in _LAYOUT_KEYS} for t in tiles}

    if core(out["tiles"]) != core(stored["tiles"]):  # pragma: no cover - defence in depth
        raise SpecError([_f("binding", "tiles", "a layout change cannot alter tile content")])
    # C-13 deviation: the stored queries and measures are not re-bound here. The equality check above
    # proves every tile's content (queries, measures, bindings) is untouched, so a re-bind could not
    # differ from the base version's, which already bound.
    parse(authoring_form(out))  # titles and shape validated as plain text
    return out


def layout_summary(base: dict, new: dict, changes: dict | None = None) -> dict:
    """What a layout change did, for the audit row and the change note."""
    pos = lambda s: {tid: (i, j) for i, sec in enumerate(s["sections"])  # noqa: E731
                     for j, tid in enumerate(sec["tiles"])}
    pb, pn = pos(base), pos(new)
    bt = {t["id"]: t for t in base["tiles"]}
    nt = {t["id"]: t for t in new["tiles"]}
    moved = sorted(k for k in pb if pb[k] != pn[k])
    widths = {k: _eff_width(nt[k]) for k in nt if _eff_width(nt[k]) != _eff_width(bt[k])}
    renamed = sorted(k for k in nt if nt[k].get("title") != bt[k].get("title"))
    renamed += [f"section {i + 1}" for i, s in enumerate(new["sections"])
                if s.get("title") != base["sections"][i].get("title")]
    hid = sorted(k for k in nt if nt[k].get("hidden") and not bt[k].get("hidden"))
    shown = sorted(k for k in nt if bt[k].get("hidden") and not nt[k].get("hidden"))
    dd = (new.get("date_control") or {}).get("default")
    dr = dd if dd != (base.get("date_control") or {}).get("default") else None
    return {"moved": moved, "widths": widths, "renamed": renamed, "hidden": hid,
            "shown": shown, "default_range": dr}
