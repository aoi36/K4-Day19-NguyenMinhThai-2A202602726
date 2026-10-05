"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

logger = logging.getLogger(__name__)

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    candidate = normalize(name)
    if not candidate:
        return None

    normalized_known = [(canonical, normalize(canonical)) for canonical in known]
    exact = next((canonical for canonical, normalized in normalized_known if normalized == candidate), None)
    if exact is not None:
        return exact

    choices = {normalized: canonical for canonical, normalized in normalized_known if normalized}
    match = difflib.get_close_matches(candidate, list(choices), n=1, cutoff=0.8)
    return choices[match[0]] if match else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|[.;]|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "version": doc.metadata.get("document_version", "unknown"),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "event_anchor": "cụm trích dẫn ngắn, nguyên văn và đặc trưng để nhận diện vụ việc",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "proceedings": [{{"stage": "điều tra|truy tố|xét xử sơ thẩm|xét xử phúc thẩm|khác",
                   "date": "ngày hoặc chuỗi rỗng", "court": "tên tòa hoặc chuỗi rỗng",
                   "status": "kết quả/trạng thái được bài báo nêu",
                   "charges": ["tội danh ở giai đoạn này"]}}],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "proceeding_stage": "giai đoạn áp dụng mức án/charge nếu rõ",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        response = json.loads(llm_fn(prompt, json_mode=True))
    except json.JSONDecodeError as error:
        logger.warning("Could not parse news extraction JSON for %s: %s", doc.id, error)
        return []
    if not isinstance(response, dict) or not isinstance(response.get("cases"), list):
        logger.warning("News extraction for %s must be a JSON object with a cases list", doc.id)
        return []
    cases = response["cases"]
    for case in cases:
        if not isinstance(case, dict):
            logger.warning("Ignoring malformed case extraction for %s: expected an object", doc.id)
            continue
        charges = case.get("charges")
        if not isinstance(charges, list):
            logger.warning("Ignoring malformed case charges for %s: expected a list", doc.id)
            charges = []
        case["charges"] = sorted({
            link_entity(str(charge), known_crimes) or normalize_crime(str(charge))
            for charge in charges if str(charge).strip()
        })
        proceedings = case.get("proceedings")
        if not isinstance(proceedings, list):
            proceedings = []
            case["proceedings"] = proceedings
        for proceeding in proceedings:
            if isinstance(proceeding, dict):
                stage_charges = proceeding.get("charges")
                if isinstance(stage_charges, list):
                    proceeding["charges"] = sorted({
                        link_entity(str(charge), known_crimes) or normalize_crime(str(charge))
                        for charge in stage_charges if str(charge).strip()
                    })
        people = case.get("people")
        if not isinstance(people, list):
            logger.warning("Ignoring malformed case people for %s: expected a list", doc.id)
            people = []
            case["people"] = people
        for person in people:
            if isinstance(person, dict):
                charge = str(person.get("charge") or "").strip()
                person["charge"] = link_entity(charge, known_crimes) or normalize_crime(charge) if charge else ""
    return [case for case in cases if isinstance(case, dict)]


def _stable_name_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _substance_key(name: str) -> str:
    canonical = {item.lower(): item for item in SUBSTANCES}
    canonical.update({"ketamin": "Ketamine", "ma túy mdma": "MDMA"})
    normalized = _stable_name_key(name)
    return canonical.get(normalized, normalized)


def _split_provisions(clause: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a numbered paragraph into its lettered points, retaining the paragraph penalty."""
    text = clause["text"]
    points = list(re.finditer(r"(?m)^([a-zđ])\)\s+", text))
    if not points:
        return [{"point": "", "text": text}]
    provisions = []
    for index, point in enumerate(points):
        end = points[index + 1].start() if index + 1 < len(points) else len(text)
        provisions.append({"point": point.group(1), "text": text[point.start():end].strip()})
    return provisions


def _quantity_value(value: str, unit: str) -> tuple[float, str]:
    amount = float(value.replace(",", "."))
    normalized_unit = unit.lower()
    if normalized_unit in {"kg", "kilôgam", "kilogram"}:
        return amount * 1000, "g"
    if normalized_unit in {"gam", "g"}:
        return amount, "g"
    if normalized_unit in {"ml", "mililít", "mililit"}:
        return amount, "ml"
    return amount, normalized_unit


def _parse_threshold(text: str) -> dict[str, Any] | None:
    quantity = r"(\d+(?:[.,]\d+)?)\s*(kilôgam|kilogram|kg|gam|g|mililít|mililit|ml)"
    range_match = re.search(
        rf"(?:từ\s+)?{quantity}\s+đến\s+dưới\s+{quantity}", text, re.IGNORECASE
    )
    if range_match:
        match = range_match
        minimum_raw, minimum_unit, maximum_raw, maximum_unit = match.groups()
    else:
        match = re.search(
            rf"(?:khối lượng|thể tích)[^0-9]{{0,16}}(?:từ\s+)?{quantity}\s+trở lên",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        minimum_raw, minimum_unit = match.groups()
        maximum_raw = maximum_unit = None

    minimum, unit = _quantity_value(minimum_raw, minimum_unit)
    maximum = None
    if maximum_raw is not None:
        maximum, max_unit = _quantity_value(maximum_raw, maximum_unit)
        if unit != max_unit:
            return None
    return {
        "min_amount": minimum,
        "min_inclusive": True,
        "max_amount": maximum,
        "max_exclusive": maximum is not None,
        "unit": unit,
        "criterion_text": match.group(0),
    }


def _parse_penalty(text: str) -> dict[str, Any]:
    matches = re.findall(r"(\d+(?:[.,]\d+)?)\s*(năm|tháng)", text.lower())
    months = [
        float(value.replace(",", ".")) * (12 if unit == "năm" else 1)
        for value, unit in matches
    ]
    return {
        "imprisonment_min_months": min(months) if months else None,
        "imprisonment_max_months": max(months) if months else None,
        "life_imprisonment": "chung thân" in text.lower(),
        "death_penalty": "tử hình" in text.lower(),
    }


def _extract_amount(amount_text: str) -> dict[str, Any]:
    quantity = r"(\d+(?:[.,]\d+)?)\s*(kilôgam|kilogram|kg|gam|g|mililít|mililit|ml)"
    match = re.search(quantity, amount_text, re.IGNORECASE)
    if not match:
        return {"amount_text": amount_text}
    amount, unit = _quantity_value(match.group(1), match.group(2))
    return {
        "amount": amount,
        "unit": unit,
        "amount_g": amount if unit == "g" else None,
        "amount_ml": amount if unit == "ml" else None,
        "amount_text": amount_text,
    }

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()
        self._known_crimes: list[str] = []

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- KG-2 — source-aware ontology writes

    def ontology_constraints(self) -> None:
        for label, key in [
            ("Document", "doc_id"), ("CaseEvent", "event_key"), ("Proceeding", "proceeding_key"),
            ("Person", "person_key"), ("Offense", "offense_key"), ("LegalArticle", "article_key"),
            ("Provision", "provision_key"), ("PenaltyRule", "rule_key"), ("Threshold", "threshold_key"),
            ("Substance", "substance_key"), ("Location", "location_key"),
        ]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def _add_document(self, doc: Document) -> None:
        self.run(
            """
            MERGE (d:Document {doc_id: $doc_id})
            SET d.kb = $kb, d.title = $title, d.source_url = $source_url,
                d.document_version = $document_version
            """,
            doc_id=doc.id,
            kb=doc.metadata.get("kb", ""),
            title=doc.metadata.get("title", ""),
            source_url=doc.metadata.get("source_url", ""),
            document_version=doc.metadata.get("document_version", ""),
        )

    def add_law_article(self, article: dict) -> None:
        article_no_match = re.search(r"\d+", article["id"])
        article_no = int(article_no_match.group()) if article_no_match else article["id"]
        article_key = ":".join((
            article.get("law", "BLHS"), str(article_no), article.get("version", "unknown"),
        ))
        self.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            MERGE (a:LegalArticle {article_key: $article_key})
            SET a.article_no = $article_no, a.title = $title, a.law_code = $law,
                a.version = $version, a.doc_id = $doc_id
            MERGE (a)-[:SUPPORTED_BY]->(d)
            """,
            article_key=article_key,
            article_no=article_no,
            title=article["title"],
            law=article.get("law", ""),
            version=article.get("version", "unknown"),
            doc_id=article["doc_id"],
        )
        if article.get("crime"):
            self._merge_offense(article["crime"], article["doc_id"], article_key)

        for clause in article.get("clauses", []):
            number = int(clause["number"])
            for provision in _split_provisions(clause):
                point = provision["point"]
                provision_key = f"{article_key}:{number}:{point}"
                self.run(
                    """
                    MATCH (a:LegalArticle {article_key: $article_key})
                    MATCH (d:Document {doc_id: $doc_id})
                    MERGE (v:Provision {provision_key: $provision_key})
                    SET v.paragraph_no = $number, v.point = $point, v.text = $text, v.doc_id = $doc_id
                    MERGE (a)-[:HAS_PROVISION]->(v)
                    MERGE (v)-[:SUPPORTED_BY]->(d)
                    """,
                    article_key=article_key,
                    provision_key=provision_key,
                    number=number,
                    point=point,
                    text=provision["text"],
                    doc_id=article["doc_id"],
                )
                if article.get("crime"):
                    self.run(
                        """
                        MATCH (v:Provision {provision_key: $provision_key})
                        MATCH (o:Offense {offense_key: $offense_key})
                        MERGE (v)-[:DEFINES]->(o)
                        """,
                        provision_key=provision_key,
                        offense_key=self._offense_key(article["crime"]),
                    )
                rule_key = f"{provision_key}:penalty"
                penalty = _parse_penalty(clause.get("penalty", ""))
                self.run(
                    """
                    MATCH (v:Provision {provision_key: $provision_key})
                    MATCH (d:Document {doc_id: $doc_id})
                    MERGE (r:PenaltyRule {rule_key: $rule_key})
                    SET r.penalty_text = $penalty, r.imprisonment_min_months = $min_months,
                        r.imprisonment_max_months = $max_months,
                        r.life_imprisonment = $life_imprisonment,
                        r.death_penalty = $death_penalty, r.doc_id = $doc_id
                    MERGE (v)-[:HAS_RULE]->(r)
                    MERGE (r)-[:SUPPORTED_BY]->(d)
                    """,
                    provision_key=provision_key,
                    rule_key=rule_key,
                    penalty=clause.get("penalty", ""),
                    min_months=penalty["imprisonment_min_months"],
                    max_months=penalty["imprisonment_max_months"],
                    life_imprisonment=penalty["life_imprisonment"],
                    death_penalty=penalty["death_penalty"],
                    doc_id=article["doc_id"],
                )
                threshold = _parse_threshold(provision["text"])
                if threshold:
                    threshold_key = f"{provision_key}:threshold"
                    self.run(
                        """
                        MATCH (r:PenaltyRule {rule_key: $rule_key})
                        MATCH (d:Document {doc_id: $doc_id})
                        MERGE (t:Threshold {threshold_key: $threshold_key})
                        SET t.min_amount = $min_amount, t.min_inclusive = $min_inclusive,
                            t.max_amount = $max_amount, t.max_exclusive = $max_exclusive,
                            t.unit = $unit, t.criterion_text = $criterion_text, t.doc_id = $doc_id
                        MERGE (r)-[:APPLIES_IF]->(t)
                        MERGE (t)-[:SUPPORTED_BY]->(d)
                        """,
                        rule_key=rule_key,
                        threshold_key=threshold_key,
                        doc_id=article["doc_id"],
                        **threshold,
                    )
                    for substance in find_substances(provision["text"]):
                        substance_key = _substance_key(substance)
                        self._merge_substance(substance, substance_key, article["doc_id"])
                        self.run(
                            """
                            MATCH (t:Threshold {threshold_key: $threshold_key})
                            MATCH (s:Substance {substance_key: $substance_key})
                            MERGE (t)-[:FOR_SUBSTANCE]->(s)
                            """,
                            threshold_key=threshold_key,
                            substance_key=substance_key,
                        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        event_ref = str(case["event_ref"])
        event_key = f"{doc.id}|event:{event_ref}"
        self.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            MERGE (e:CaseEvent {event_key: $event_key})
            SET e.name = $name, e.summary = $summary, e.date = $date, e.doc_id = $doc_id
            MERGE (e)-[:SUPPORTED_BY]->(d)
            """,
            event_key=event_key,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""),
            date=case.get("date", ""),
            doc_id=doc.id,
        )
        proceedings = case.get("proceedings")
        if not isinstance(proceedings, list) or not proceedings:
            proceedings = [{
                "stage": case.get("stage") or self._infer_stage(doc.content),
                "date": case.get("date", ""),
                "court": case.get("court", ""),
                "status": case.get("status", ""),
                "charges": case.get("charges", []),
            }]
        people = [person for person in case.get("people", []) if isinstance(person, dict) and person.get("name")]
        substances = [item for item in case.get("substances", []) if isinstance(item, dict) and item.get("name")]
        for proceeding_index, proceeding in enumerate(proceedings):
            if not isinstance(proceeding, dict):
                logger.warning("Ignoring malformed proceeding in %s", doc.id)
                continue
            stage = str(proceeding.get("stage") or "không rõ")
            proceeding_date = str(proceeding.get("date") or "")
            court = str(proceeding.get("court") or "")
            proceeding_key = f"{doc.id}|{event_ref}|{stage}|{proceeding_date}|{court}|{proceeding_index}"
            self.run(
                """
                MATCH (e:CaseEvent {event_key: $event_key})
                MATCH (d:Document {doc_id: $doc_id})
                MERGE (p:Proceeding {proceeding_key: $proceeding_key})
                SET p.stage = $stage, p.date = $date, p.court = $court,
                    p.status = $status, p.doc_id = $doc_id
                MERGE (e)-[:HAS_PROCEEDING]->(p)
                MERGE (p)-[:SUPPORTED_BY]->(d)
                """,
                event_key=event_key,
                proceeding_key=proceeding_key,
                stage=stage,
                date=proceeding_date,
                court=court,
                status=proceeding.get("status", ""),
                doc_id=doc.id,
            )
            charges = proceeding.get("charges") or case.get("charges", [])
            for charge in charges:
                canonical = link_entity(str(charge), self._known_crimes) if self._known_crimes else None
                offense = canonical or normalize_crime(str(charge))
                self._merge_offense(offense, doc.id)
                self.run(
                    """
                    MATCH (p:Proceeding {proceeding_key: $proceeding_key})
                    MATCH (o:Offense {offense_key: $offense_key})
                    MERGE (p)-[r:HAS_CHARGE]->(o)
                    SET r.stage = $stage, r.charge_status = $charge_status,
                        r.quoted_text = $quoted_text, r.doc_id = $doc_id
                    """,
                    proceeding_key=proceeding_key,
                    offense_key=self._offense_key(offense),
                    stage=stage,
                    charge_status=proceeding.get("status", ""),
                    quoted_text=str(charge),
                    doc_id=doc.id,
                )
            for person in people:
                person_key = f"{doc.id}|{_stable_name_key(person['name'])}"
                self.run(
                    """
                    MATCH (d:Document {doc_id: $doc_id})
                    MERGE (x:Person {person_key: $person_key})
                    SET x.name = $name, x.aliases = $aliases, x.age_as_reported = $age,
                        x.doc_id = $doc_id
                    MERGE (x)-[:SUPPORTED_BY]->(d)
                    WITH x
                    MATCH (p:Proceeding {proceeding_key: $proceeding_key})
                    MERGE (p)-[r:INVOLVES_PERSON]->(x)
                    SET r.role = $role, r.age_as_reported = $age, r.doc_id = $doc_id,
                        r.sentence = $sentence
                    """,
                    person_key=person_key,
                    name=person["name"],
                    aliases=person.get("aliases") or [],
                    age=person.get("age_as_reported"),
                    doc_id=doc.id,
                    proceeding_key=proceeding_key,
                    role=person.get("role", ""),
                    sentence=person.get("sentence", ""),
                )
                if person.get("charge"):
                    person_charge = link_entity(person["charge"], self._known_crimes) or person["charge"]
                    self._merge_offense(person_charge, doc.id)
                    self.run(
                        """
                        MATCH (p:Proceeding {proceeding_key: $proceeding_key})
                        MATCH (o:Offense {offense_key: $offense_key})
                        MERGE (p)-[r:HAS_CHARGE]->(o)
                        SET r.stage = $stage, r.charge_status = $charge_status,
                            r.quoted_text = $quoted_text, r.doc_id = $doc_id
                        """,
                        proceeding_key=proceeding_key,
                        offense_key=self._offense_key(person_charge),
                        stage=stage,
                        charge_status=proceeding.get("status", ""),
                        quoted_text=person_charge,
                        doc_id=doc.id,
                    )
            for item in substances:
                substance_name = str(item["name"])
                substance_key = _substance_key(substance_name)
                amount = _extract_amount(str(item.get("amount") or ""))
                self._merge_substance(substance_name, substance_key, doc.id)
                self.run(
                    """
                    MATCH (e:CaseEvent {event_key: $event_key})
                    MATCH (s:Substance {substance_key: $substance_key})
                    MERGE (e)-[r:INVOLVES_SUBSTANCE]->(s)
                    SET r.amount = $amount, r.unit = $unit, r.amount_g = $amount_g,
                        r.amount_ml = $amount_ml, r.quantity_text = $quantity_text,
                        r.certainty = $certainty, r.doc_id = $doc_id
                    """,
                    event_key=event_key,
                    substance_key=substance_key,
                    amount=amount.get("amount"),
                    unit=amount.get("unit", ""),
                    amount_g=amount.get("amount_g"),
                    amount_ml=amount.get("amount_ml"),
                    quantity_text=amount.get("amount_text", ""),
                    certainty=item.get("certainty", ""),
                    doc_id=doc.id,
                )
            location = str(case.get("location") or "").strip()
            if location:
                location_key = _stable_name_key(location)
                self.run(
                    """
                    MATCH (d:Document {doc_id: $doc_id})
                    MERGE (l:Location {location_key: $location_key})
                    ON CREATE SET l.doc_id = $doc_id, l.doc_ids = []
                    SET l.name = $name,
                        l.doc_ids = CASE WHEN $doc_id IN coalesce(l.doc_ids, [])
                            THEN coalesce(l.doc_ids, []) ELSE coalesce(l.doc_ids, []) + $doc_id END
                    MERGE (l)-[:SUPPORTED_BY]->(d)
                    WITH l
                    MATCH (e:CaseEvent {event_key: $event_key})
                    MERGE (e)-[r:LOCATED_IN]->(l)
                    SET r.location_role = $location_role, r.doc_id = $doc_id
                    """,
                    location_key=location_key,
                    name=location,
                    doc_id=doc.id,
                    event_key=event_key,
                    location_role=case.get("location_role", ""),
                )

    def _offense_key(self, offense: str) -> str:
        canonical = link_entity(offense, self._known_crimes) if self._known_crimes else None
        prefix = "BLHS:title" if canonical else "unlinked"
        return f"{prefix}:{normalize_crime(canonical or offense)}"

    def _merge_offense(self, name: str, doc_id: str, article_key: str | None = None) -> None:
        self.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            MERGE (o:Offense {offense_key: $offense_key})
            ON CREATE SET o.doc_id = $doc_id, o.doc_ids = [], o.aliases = []
            SET o.canonical_name = $name,
                o.doc_ids = CASE WHEN $doc_id IN coalesce(o.doc_ids, [])
                    THEN coalesce(o.doc_ids, []) ELSE coalesce(o.doc_ids, []) + $doc_id END,
                o.aliases = CASE WHEN $name IN coalesce(o.aliases, [])
                    THEN coalesce(o.aliases, []) ELSE coalesce(o.aliases, []) + $name END
            MERGE (o)-[:SUPPORTED_BY]->(d)
            """,
            offense_key=self._offense_key(name),
            name=name,
            doc_id=doc_id,
        )
        if article_key:
            self.run(
                """
                MATCH (o:Offense {offense_key: $offense_key})
                MATCH (a:LegalArticle {article_key: $article_key})
                MERGE (o)-[:CODIFIED_BY]->(a)
                """,
                offense_key=self._offense_key(name),
                article_key=article_key,
            )

    def _merge_substance(self, name: str, substance_key: str, doc_id: str) -> None:
        self.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            MERGE (s:Substance {substance_key: $substance_key})
            ON CREATE SET s.doc_id = $doc_id, s.doc_ids = [], s.aliases = []
            SET s.canonical_name = $name,
                s.doc_ids = CASE WHEN $doc_id IN coalesce(s.doc_ids, [])
                    THEN coalesce(s.doc_ids, []) ELSE coalesce(s.doc_ids, []) + $doc_id END,
                s.aliases = CASE WHEN $name IN coalesce(s.aliases, [])
                    THEN coalesce(s.aliases, []) ELSE coalesce(s.aliases, []) + $name END
            MERGE (s)-[:SUPPORTED_BY]->(d)
            """,
            substance_key=substance_key,
            name=name,
            doc_id=doc_id,
        )

    @staticmethod
    def _infer_stage(text: str) -> str:
        lowered = text.lower()
        if "phúc thẩm" in lowered:
            return "xét xử phúc thẩm"
        if "sơ thẩm" in lowered or "tuyên án" in lowered:
            return "xét xử sơ thẩm"
        if "truy tố" in lowered or "cáo trạng" in lowered:
            return "truy tố"
        if "bị bắt" in lowered or "khởi tố" in lowered or "điều tra" in lowered:
            return "điều tra"
        return "không rõ"

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Return seed facts plus case-to-law and directly requested legal provisions."""
        seed_ids, facts = self.seed_facts(question, doc_ids, limit=max_facts)

        article_nos = sorted({
            int(number) for number in re.findall(r"\bđiều\s+(\d+)\b", question, re.IGNORECASE)
        })
        substance_keys = sorted({_substance_key(name) for name in find_substances(question)})

        case_law = self.run(
            """
            MATCH (e:CaseEvent)-[:HAS_PROCEEDING]->(p:Proceeding)-[:HAS_CHARGE]->(o:Offense)
                  <-[:DEFINES]-(v:Provision)<-[:HAS_PROVISION]-(a:LegalArticle)
            WHERE elementId(e) IN $ids
               OR EXISTS { MATCH (e)-[:HAS_PROCEEDING]->(seed:Proceeding)
                           WHERE elementId(seed) IN $ids }
               OR EXISTS { MATCH (e)-[:HAS_PROCEEDING]->(:Proceeding)-[:INVOLVES_PERSON]->(seed:Person)
                           WHERE elementId(seed) IN $ids }
               OR EXISTS { MATCH (e)-[:INVOLVES_SUBSTANCE]->(seed:Substance)
                           WHERE elementId(seed) IN $ids }
            OPTIONAL MATCH (v)-[:HAS_RULE]->(r:PenaltyRule)
            OPTIONAL MATCH (r)-[:APPLIES_IF]->(t:Threshold)-[:FOR_SUBSTANCE]->(s:Substance)
                           <-[inv:INVOLVES_SUBSTANCE]-(e)
            WITH DISTINCT e, p, o, a, v, r,
                 collect(DISTINCT CASE WHEN inv IS NULL THEN null ELSE
                     {substance: s.canonical_name, amount: inv.quantity_text,
                      threshold: t.criterion_text} END) AS matching_thresholds
            WHERE v.paragraph_no = 1 OR size(matching_thresholds) > 0
            RETURN e.name AS case_name, e.summary AS summary, p.stage AS stage,
                   p.status AS status, o.canonical_name AS offense, a.article_no AS article_no,
                   a.title AS article_title, v.paragraph_no AS paragraph_no,
                   v.point AS point, v.text AS provision_text, r.penalty_text AS penalty,
                   matching_thresholds
            ORDER BY article_no, paragraph_no, point
            LIMIT $limit
            """,
            ids=seed_ids,
            limit=max_facts,
        )
        for row in case_law:
            case_label = row.get("case_name") or "Vụ việc"
            offense = row.get("offense") or "chưa xác định tội danh"
            article_title = row.get("article_title") or "BLHS"
            clause = f"Điều {row.get('article_no')} {article_title}, khoản {row.get('paragraph_no')}"
            if row.get("point"):
                clause += f" điểm {row['point']}"
            detail = row.get("provision_text") or row.get("penalty") or ""
            facts.append(
                f"Vụ việc '{case_label}' ({row.get('stage') or 'giai đoạn không rõ'}"
                f"{': ' + row['status'] if row.get('status') else ''}), "
                f"tội danh '{offense}' -> {clause}: {detail}"
            )
            for threshold in row.get("matching_thresholds") or []:
                facts.append(
                    f"Trong vụ '{case_label}', {threshold.get('substance') or 'chất ma túy'} "
                    f"có khối lượng {threshold.get('amount') or 'không rõ'}; "
                    f"điều kiện khoản luật: {threshold.get('threshold') or 'không rõ'}."
                )

        provisions = self.run(
            """
            MATCH (a:LegalArticle)-[:HAS_PROVISION]->(v:Provision)
            WHERE elementId(a) IN $ids OR a.article_no IN $article_nos
            OPTIONAL MATCH (v)-[:HAS_RULE]->(r:PenaltyRule)
            OPTIONAL MATCH (r)-[:APPLIES_IF]->(t:Threshold)-[:FOR_SUBSTANCE]->(s:Substance)
            WITH DISTINCT a, v, r, collect(DISTINCT s.substance_key) AS provision_substances
            WHERE v.paragraph_no = 1
               OR any(substance IN provision_substances WHERE substance IN $substance_keys)
            RETURN a.article_no AS article_no, a.title AS article_title,
                   v.paragraph_no AS paragraph_no, v.point AS point,
                   v.text AS provision_text, r.penalty_text AS penalty
            ORDER BY article_no, paragraph_no, point
            LIMIT $limit
            """,
            ids=seed_ids,
            article_nos=article_nos,
            substance_keys=substance_keys,
            limit=max_facts,
        )
        for row in provisions:
            clause = f"Điều {row.get('article_no')} {row.get('article_title') or 'BLHS'}, khoản {row.get('paragraph_no')}"
            if row.get("point"):
                clause += f" điểm {row['point']}"
            facts.append(f"[Căn cứ pháp luật] {clause}: {row.get('provision_text') or row.get('penalty') or ''}")

        unique_facts = list(dict.fromkeys(fact for fact in facts if fact))
        return unique_facts[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.ontology_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    known_crimes = sorted({article["crime"] for article in articles if article.get("crime")})
    graph._known_crimes = known_crimes

    for doc, article in zip(law_docs, articles):
        graph._add_document(doc)
        graph.add_law_article(article)

    for doc in news_docs:
        graph._add_document(doc)
        cases = extract_news_cases(doc, llm_fn, known_crimes)
        used_anchors: set[str] = set()
        for index, case in enumerate(cases):
            anchor = case.get("event_anchor") or case.get("name") or f"event-{index}"
            event_ref = _stable_name_key(str(anchor))
            if event_ref in used_anchors:
                event_ref = f"{event_ref}|duplicate:{index}"
            used_anchors.add(event_ref)
            case["event_ref"] = event_ref
            graph.add_news_case(case, doc)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(
            chunk["metadata"].get("doc_id", chunk["id"].split("#", 1)[0])
            for chunk in chunks
        ))
        facts = self.graph.context(question, doc_ids)
        chunk_context = "\n\n".join(
            f"[{index}] {chunk['content']}" for index, chunk in enumerate(chunks, start=1)
        )
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(facts) if facts else "(Không tìm thấy dữ kiện trong graph.)",
            chunks=chunk_context or "(Không tìm thấy đoạn văn bản liên quan.)",
            question=question,
        )
        return self.llm_fn(prompt)
