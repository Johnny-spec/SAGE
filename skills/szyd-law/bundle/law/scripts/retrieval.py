"""Explicit service adapters: no chat model, arbitrary Cypher, or implicit backfill."""

import hashlib
import importlib
import json
import os
from pathlib import Path


class ServiceError(RuntimeError):
    pass


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ServiceError(f"Missing environment configuration: {name}")
    return value


def driver():
    neo4j = importlib.import_module("neo4j")
    return neo4j.GraphDatabase.driver(required("NEO4J_URI"), auth=(required("NEO4J_USER"), required("NEO4J_PASSWORD")), connection_timeout=10)


def session_options():
    return {"database": required("NEO4J_DATABASE"), "default_access_mode": "READ"}


def evidence(backend, records):
    found = []
    for row in records:
        content = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        identity = hashlib.sha256((backend + content).encode("utf-8")).hexdigest()[:24]
        found.append({"id": f"{backend}:{identity}", "source": backend, "locator": str(row.get("article_id") or row.get("id") or identity),
                      "content": content, "kind": "law" if row.get("law") or row.get("law_name") else "graph", "verified": False})
    return found


def neo4j_retrieve(value):
    names = value.get("entity_names", [])
    keywords = value.get("keywords", [])
    concept_ids = value.get("concept_ids", [])
    categories = value.get("categories", [])
    for items in (names, keywords, concept_ids, categories):
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError("Neo4j query inputs must be lists of strings")
    queries = {
        "linked_entities": (
            "MATCH (n:Entity) WHERE n.name IN $names OR any(alias IN coalesce(n.aliases, []) WHERE alias IN $names) "
            "RETURN elementId(n) AS id, n.name AS name, labels(n) AS labels LIMIT 50", {"names": names}),
        "related_laws": (
            "MATCH (law:Law) WHERE any(term IN $keywords WHERE law.name CONTAINS term OR law.title CONTAINS term) "
            "OPTIONAL MATCH (law)-[:HAS_ARTICLE]->(article:Article) "
            "RETURN elementId(law) AS id, law.name AS law, law.status AS status, law.effective_date AS effective_date, "
            "law.content AS law_content, article.number AS article, article.content AS content, elementId(article) AS article_id LIMIT 50", {"keywords": keywords}),
        "concept_laws": (
            "MATCH (concept:LegalConcept) WHERE concept.id IN $concept_ids OR concept.name IN $keywords "
            "OPTIONAL MATCH (concept)-[:BELONGS_TO]->(:LawDomain)-[:HAS_LAW]->(law:Law)-[:HAS_ARTICLE]->(article:Article) "
            "OPTIONAL MATCH (article)-[:HAS_RISK]->(risk:RiskPoint) "
            "RETURN concept.id AS concept_id, concept.name AS concept, law.name AS law, law.status AS status, "
            "law.effective_date AS effective_date, article.number AS article, article.content AS content, "
            "elementId(article) AS article_id, risk.name AS risk_point LIMIT 100", {"concept_ids": concept_ids, "keywords": keywords}),
        "related_cases": (
            "MATCH (case:Case) WHERE any(term IN $keywords WHERE case.title CONTAINS term OR case.facts CONTAINS term) "
            "RETURN elementId(case) AS id, case.title AS title, case.case_number AS case_number, case.facts AS facts, "
            "case.judgment AS judgment, case.date AS date LIMIT 10", {"keywords": keywords}),
        "compliance_rules": (
            "MATCH (rule:ComplianceRule) WHERE any(category IN $categories WHERE rule.category = category OR rule.applicable_to CONTAINS category) "
            "RETURN elementId(rule) AS id, rule.name AS name, rule.description AS description, rule.requirements AS requirements, "
            "rule.required_relations AS required_relations, rule.prohibited_relations AS prohibited_relations, "
            "rule.allowed_relations AS allowed_relations, rule.legal_basis AS legal_basis LIMIT 30", {"categories": categories}),
    }
    with driver() as connection:
        connection.verify_connectivity()
        with connection.session(**session_options()) as session:
            result = {key: [record.data() for record in session.run(query, **parameters)] for key, (query, parameters) in queries.items()}
            ids = [row["id"] for row in result["linked_entities"]]
            neighbors = session.run(
                "MATCH (n) WHERE elementId(n) IN $ids MATCH (n)-[r]-(m) "
                "RETURN elementId(n) AS from_id, n.name AS from_name, type(r) AS relation, "
                "elementId(m) AS to_id, m.name AS to_name, labels(m) AS labels LIMIT 200", ids=ids)
            result["linked_neighbors"] = [record.data() for record in neighbors]
            linked = session.run(
                "MATCH (n) WHERE elementId(n) IN $ids MATCH (n)-[*1..2]-(rule:ComplianceRule) "
                "RETURN DISTINCT elementId(rule) AS id, rule.name AS name, rule.requirements AS requirements, "
                "rule.required_relations AS required_relations, rule.prohibited_relations AS prohibited_relations, "
                "rule.allowed_relations AS allowed_relations, rule.legal_basis AS legal_basis LIMIT 30", ids=ids)
            result["linked_rules"] = [record.data() for record in linked]
    records = [row for group in result.values() for row in group]
    return {"status": "OK", "backend": "neo4j", "results": result, "evidence": evidence("neo4j", records), "empty": not records}


def open_collection():
    path = Path(required("CHROMA_PERSIST_DIRECTORY")).expanduser()
    if not path.is_absolute():
        raise ServiceError("CHROMA_PERSIST_DIRECTORY must be absolute")
    if not (path / "chroma.sqlite3").is_file():
        raise ServiceError("Existing Chroma database not found; automatic creation is disabled")
    chromadb = importlib.import_module("chromadb")
    client = chromadb.PersistentClient(path=str(path))
    return client.get_collection(name=required("CHROMA_COLLECTION"), embedding_function=None)


def embed(queries):
    openai = importlib.import_module("openai")
    with openai.AzureOpenAI(azure_endpoint=required("AZURE_OPENAI_ENDPOINT"), api_key=required("AZURE_OPENAI_API_KEY"),
                            api_version=required("AZURE_OPENAI_API_VERSION"), timeout=30, max_retries=1) as client:
        response = client.embeddings.create(model=required("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"), input=queries)
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


def metadata_validation(metadata, expected):
    matched, mismatched = [], []
    for key, values in expected.items():
        if not values:
            continue
        actual = metadata.get(key)
        options = actual if isinstance(actual, list) else [actual]
        passed = actual is not None and any(str(option).strip() and str(value).lower() in str(option).lower() for option in options for value in values)
        (matched if passed else mismatched).append(key)
    confidence = len(matched) / (len(matched) + len(mismatched)) if matched or mismatched else 1.0
    return {"mode": "soft", "matched_fields": matched, "mismatched_fields": mismatched, "confidence": confidence, "soft_pass": confidence >= 0.2}


def chroma_retrieve(value):
    queries = value.get("queries", [])
    if not isinstance(queries, list) or any(not isinstance(query, str) for query in queries):
        raise ValueError("queries must be strings")
    queries = list(dict.fromkeys(query.strip() for query in queries if query.strip()))[:10]
    if not queries:
        raise ValueError("Agent must supply at least one query")
    collection = open_collection()
    count = collection.count()
    if count == 0:
        return {"status": "OK", "backend": "chroma", "results": [], "evidence": [], "empty": True, "retrieval_mode": "empty_collection"}
    embeddings = embed(queries)
    filters = value.get("retrieval_filters", {})
    found, mode = {}, "semantic_fallback"
    for candidate_filters, candidate_mode in ((filters.get("strict_filters", []), "structured_strict"),
                                               (filters.get("fallback_filters", []), "structured_fallback"), ([None], "semantic_fallback")):
        for where in candidate_filters:
            arguments = {"query_embeddings": embeddings, "n_results": min(count, 5), "include": ["documents", "metadatas", "distances"]}
            if where:
                arguments["where"] = where
            result = collection.query(**arguments)
            for query_index, ids in enumerate(result["ids"]):
                for index, identifier in enumerate(ids):
                    content = result["documents"][query_index][index]
                    metadata = (result.get("metadatas") or [[]])[query_index][index] or {}
                    if not content:
                        continue
                    row = {"id": identifier, "content": content, "metadata": metadata,
                           "distance": result["distances"][query_index][index], "reference": metadata.get("source", "Unknown Source"),
                           "validation": metadata_validation(metadata, filters.get("expected", {}))}
                    previous = found.get(identifier)
                    if previous is None or row["distance"] < previous["distance"]:
                        found[identifier] = row
        if found:
            mode = candidate_mode
            break
    candidates = sorted(found.values(), key=lambda row: row["distance"])[:20]
    evidence_rows = []
    for row in candidates:
        identity = hashlib.sha256((row["id"] + row["content"]).encode("utf-8")).hexdigest()[:24]
        row["evidence_id"] = f"chroma:{identity}"
        evidence_rows.append({"id": row["evidence_id"], "source": row["reference"], "locator": row["id"], "content": row["content"],
                              "kind": "law", "verified": False})
    return {"status": "OK", "backend": "chroma", "results": candidates, "evidence": evidence_rows, "empty": not candidates,
            "retrieval_mode": mode, "rerank_required": True}


def rerank(value):
    candidates = value["candidates"]
    by_id = {item["id"]: item for item in candidates}
    ids = value["selected_ids"]
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
        raise ValueError("selected_ids must be a nonempty list of strings")
    if len(ids) != len(set(ids)) or set(ids) - set(by_id):
        raise ValueError("Duplicate or unknown rerank IDs")
    return {"results": [by_id[key] for key in ids], "selection_method": "agent_rerank"}


def reasoning(value):
    from rules import asset
    actions = {item["canonical_type"] for item in value.get("canonical_relations", [])}
    conflicts, missing = [], []
    for rule in value.get("rules", []):
        for field, destination in (("required_relations", missing), ("prohibited_relations", conflicts)):
            tokens = rule.get(field) or []
            if isinstance(tokens, str):
                tokens = [token.strip() for token in tokens.replace(";", ",").split(",") if token.strip()]
            for token in tokens:
                if (field == "required_relations" and token not in actions) or (field == "prohibited_relations" and token in actions):
                    destination.append({"rule_id": rule.get("id"), "relation": token, "requires_agent_verification": True})
    for first, second in asset("ontology.json").get("conflict_pairs", []):
        if first in actions and second in actions:
            conflicts.append({"relations": [first, second], "requires_agent_verification": True})
    return {"conflicts": conflicts, "missing_requirements": missing, "write_performed": False}


def backfill(value, apply=False):
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Backfill requires an isolated run_id")
    allowed = {"run_id", "concept_ids", "linked_entity_ids"}
    if set(value) - allowed:
        raise ValueError("Backfill only accepts run_id, concept_ids and linked_entity_ids")
    for key in ("concept_ids", "linked_entity_ids"):
        if not isinstance(value.get(key, []), list) or any(not isinstance(item, str) for item in value.get(key, [])):
            raise ValueError("Backfill IDs must be lists of strings")
    plan = {"run_id": run_id, "concept_ids": list(dict.fromkeys(value.get("concept_ids", []))),
            "linked_entity_ids": list(dict.fromkeys(value.get("linked_entity_ids", [])))}
    if not apply:
        return {"mode": "dry_run", "plan": plan, "write_performed": False}
    if os.environ.get("LAW_ALLOW_BACKFILL") != "1":
        raise ServiceError("Backfill needs explicit approval, --apply and LAW_ALLOW_BACKFILL=1")
    query = (
        "MERGE (d:DocumentFact {run_id: $run_id}) SET d.updated_at = datetime() "
        "WITH d CALL { WITH d UNWIND $concept_ids AS concept_id MATCH (c:LegalConcept {id: concept_id}) "
        "MERGE (d)-[:MENTIONS]->(c) } "
        "WITH d UNWIND $linked_entity_ids AS entity_id MATCH (n:Entity) WHERE elementId(n) = entity_id "
        "MERGE (d)-[:LINKED_TO]->(n)"
    )
    with driver() as connection:
        with connection.session(database=required("NEO4J_DATABASE")) as session:
            def write(transaction):
                for key, label, predicate in (("concept_ids", "LegalConcept", "n.id IN $ids"), ("linked_entity_ids", "Entity", "elementId(n) IN $ids")):
                    ids = plan[key]
                    record = transaction.run(f"MATCH (n:{label}) WHERE {predicate} RETURN count(n) AS count", ids=ids).single()
                    if record["count"] != len(ids):
                        raise ValueError("Backfill references missing or ambiguous nodes")
                return transaction.run(query, **plan).consume().counters
            counters = session.execute_write(write)
    return {"mode": "applied", "write_performed": True, "nodes_created": counters.nodes_created, "relationships_created": counters.relationships_created}


def preflight_services():
    results = {}
    for backend in ("neo4j", "chroma"):
        try:
            if backend == "neo4j":
                with driver() as connection:
                    connection.verify_connectivity()
                    with connection.session(**session_options()) as session:
                        labels = [record["label"] for record in session.run("CALL db.labels() YIELD label RETURN label")]
                        indexes = [record.data() for record in session.run("SHOW INDEXES YIELD name, type, labelsOrTypes, state RETURN name, type, labelsOrTypes, state")]
                results[backend] = {"status": "OK", "labels": labels, "indexes": indexes,
                                    "missing_labels": sorted({"Entity", "LegalConcept", "Law", "Article", "Case", "ComplianceRule"} - set(labels)),
                                    "linking_mode": "exact_name_or_alias; fulltext not required"}
            else:
                collection = open_collection()
                sample = embed(["connection check"])
                count = collection.count()
                if count:
                    collection.query(query_embeddings=sample, n_results=1)
                results[backend] = {"status": "OK", "count": count, "embedding_dimension": len(sample[0]), "embedding_compatibility": "dimension checked; owner must confirm model identity"}
        except Exception as error:
            results[backend] = {"status": "BLOCKED", "error_type": type(error).__name__,
                                "reason": str(error) if isinstance(error, ServiceError) else "Service check failed; inspect configuration privately"}
    return {"status": "OK" if all(item["status"] == "OK" for item in results.values()) else "BLOCKED", "services": results}