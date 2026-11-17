"""Quick 014: what the knowledge graph extracts — entity types, relation types
and the extraction instructions. This file is prompt text, not code paths:
graphiti-core puts each type's docstring into its extraction prompt, so the
docstrings below ARE the instructions the extraction model follows.

memoryservice passes ENTITY_TYPES, EDGE_TYPES and EXTRACTION_INSTRUCTIONS to
every add_episode. The models are deliberately field-less: a field would make
graphiti run an extra attribute-extraction LLM call per node or fact.

Why this exists: with no types, graphiti 0.29.2 keeps only things that "could
have their own Wikipedia article", so a business's own concepts (a fuel drop,
the rule that defines one, excavator 11, the DailyFuelReports table) were
dropped and the graph stayed nearly empty (Quick 014 findings). No graphiti
import here: memoryservice stays the only module that imports Graphiti.
"""

from pydantic import BaseModel


# ── entity types (node labels) ──────────────────────────────────────────────

class Machine(BaseModel):
    """A specific machine, vehicle, generator or other physical asset the business runs, identified by a code, number, plate or name. GOOD: "Excavator machine code 11", "DG set 2", "truck MH12AB1234". BAD: "machines", "the fleet"."""


class Site(BaseModel):
    """A named site, depot, plant, project, branch, warehouse or location where the business operates. GOOD: "Pune depot", "Hinjewadi site"."""


class Metric(BaseModel):
    """A business measure or KPI the business tracks, named by what is measured. GOOD: "fuel level", "fuel consumption per hour", "daily sales", "missed fuel drop count"."""


class Rule(BaseModel):
    """A definition, threshold, detection rule, time window or business rule, named after what it defines. GOOD: "fuel drop definition", "fuel drop detection window", "credit limit rule". Keep the numbers in the facts, not in the name."""


class EventType(BaseModel):
    """A kind of business event or incident that happens repeatedly and gets detected, counted or reported. GOOD: "fuel drop", "refuel", "late delivery", "missed payment". Use the singular, lowercase name."""


class Table(BaseModel):
    """A table or view in the business database, written exactly as its identifier. GOOD: "DailyFuelReports", "Machines", "sales_orders"."""


class Column(BaseModel):
    """A column of a business database table, written as Table.Column. GOOD: "Machines.OperatingState", "DailyFuelReports.FuelLevel"."""


class Case(BaseModel):
    """An operational case the assistant tracked. Name it "Case #<number>" when the text gives the number, e.g. "Case #12"."""


class Person(BaseModel):
    """A named person, or a business role that acts as one. GOOD: "Priya", "site supervisor", "fuel manager"."""


class Organization(BaseModel):
    """A named company: customer, vendor, supplier, transporter or the business itself. GOOD: "Northwind", "BlueDart"."""


class Preference(BaseModel):
    """A standing preference of the owner or a user about how future work is done or shown. GOOD: "fuel shown in litres", "weekly fuel summary on Monday". A one-off request is not a preference."""


class Report(BaseModel):
    """A named report, dashboard or document the business uses. GOOD: "daily fuel report", "Fuel dashboard", "monthly sales report"."""


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Machine": Machine,
    "Site": Site,
    "Metric": Metric,
    "Rule": Rule,
    "EventType": EventType,
    "Table": Table,
    "Column": Column,
    "Case": Case,
    "Person": Person,
    "Organization": Organization,
    "Preference": Preference,
    "Report": Report,
}


# ── relation types (RELATES_TO edge names) ──────────────────────────────────

class HAS_ISSUE(BaseModel):
    """A machine, site, table or metric has a problem, fault, anomaly or repeated incident. Example: Excavator machine code 11 HAS_ISSUE fuel drop (32 confirmed drops, possibly a sensor calibration fault)."""


class DEFINED_AS(BaseModel):
    """A metric, event type or term is defined by a rule, threshold or formula. Example: fuel drop DEFINED_AS fuel drop definition (at least 40 L or 12.5% within 5 minutes, down for 2 hours)."""


class MEASURED_IN(BaseModel):
    """A metric, event type or rule is measured, recorded or stored in a table or column. Example: fuel level MEASURED_IN DailyFuelReports."""


class HAS_COLUMN(BaseModel):
    """A table has a column. Example: Machines HAS_COLUMN Machines.OperatingState."""


class BELONGS_TO(BaseModel):
    """A machine, asset, person or site belongs to or is located at a site, depot or organization."""


class OWNED_BY(BaseModel):
    """Something is owned, managed, handled or approved by a person, role or organization."""


class PREFERS(BaseModel):
    """A person or the owner holds a standing preference about a report, unit, format, view or cadence."""


class APPLIES_TO(BaseModel):
    """A rule, window or threshold applies to an event type, metric, machine, site or table. Example: fuel drop detection window APPLIES_TO fuel drop."""


class INVOLVES(BaseModel):
    """A case is about or involves a machine, site, event type, metric, person or table. Example: Case #12 INVOLVES Excavator machine code 11."""


class REPORTED_IN(BaseModel):
    """A metric, event type or machine appears in a report or dashboard."""


EDGE_TYPES: dict[str, type[BaseModel]] = {
    "HAS_ISSUE": HAS_ISSUE,
    "DEFINED_AS": DEFINED_AS,
    "MEASURED_IN": MEASURED_IN,
    "HAS_COLUMN": HAS_COLUMN,
    "BELONGS_TO": BELONGS_TO,
    "OWNED_BY": OWNED_BY,
    "PREFERS": PREFERS,
    "APPLIES_TO": APPLIES_TO,
    "INVOLVES": INVOLVES,
    "REPORTED_IN": REPORTED_IN,
}


EXTRACTION_INSTRUCTIONS = """BUSINESS MEMORY RULES (these override the general guidance above where they differ):
- This text is a business's own operational knowledge: its machines, sites, measures, rules, database tables, cases and people. The business's own concepts ARE entities even though nobody wrote an encyclopedia article about them: an event type ("fuel drop"), a rule or definition ("fuel drop definition", "fuel drop detection window"), a measure ("fuel level"), a machine identified by a code ("Excavator machine code 11"), a database table or column (CamelCase or snake_case identifiers such as DailyFuelReports or Machines.OperatingState), a report or dashboard, a case ("Case #12").
- Use ONE consistent name per thing so different texts link to the same node: singular and lowercase for concepts ("fuel drop", never "Fuel Drops"), the exact identifier for tables and columns, "Case #N" for cases, and the machine's code as written.
- Name a rule after what it defines and keep its numbers in the facts: the fact carries "at least 40 L or 12.5% within 5 minutes", the rule is named "fuel drop definition".
- Connect things: a rule to what it defines or applies to, a machine to the events and issues it has, a metric to the table it lives in, a case to the machines, events and people it is about.
- Never make numbers, quantities, dates or time spans into entities; keep them inside the fact text."""
